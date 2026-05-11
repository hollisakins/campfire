"""
outlier_detect: campfire-native per-visit cosmic-ray rejection.

For each visit, drizzles all (visit + intra-program-overlap) inputs into a
**per-visit intermediate WCS** (input native pscale, ref-input rotation,
union-bbox shape — the same convention ``jwst.outlier_detection`` uses
internally), streams sci rasters through ``MedianComputer``, blots
median back via ``gwcs_blot``, and flags CRs in input DQ via
``flag_resampled_model_crs``. DQ updates land on the visit's own canonical
files via ``atomic_save`` with ``CFP_OUT`` stamped.

Only SCI is drizzled, matching ``jwst.outlier_detection``'s
``ResampleImage(enable_var=False, compute_err=None)`` setup. The
SNR comparison inside ``flag_resampled_model_crs`` uses each input
model's own native ERR — a blotted-median ERR would be smoother than
native and systematically inflate computed SNRs (over-flagging).

Reuses campfire's bbox-sliced drizzle primitive
(``drizzle.drizzle_tile_singles``) and stcal/jwst's median + flag helpers.
The per-visit intermediate WCS is small (~visit footprint at native scale,
not full mosaic tile), so per-input drizzle scaffolding and the median
buffer are both bounded by visit size — avoids the per-input full-tile
allocation cost that ``stcal.resample.Resample`` pays inside
``Image3Pipeline``.

Replaces the dead-end per-tile path (issue #138 Phase 2 v1). The
per-visit framing keeps the rotation/scale alignment quality of the
jwst path while routing the heavy lifting through the campfire drizzle
primitive (variance trick, bbox-sliced output, no Python-level masked
accumulator updates).
"""

import os
import tempfile
from copy import deepcopy

import numpy as np
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import atomic_save, log


def _build_visit_wcs(crf_files):
    """Build per-visit intermediate WCS via ``wcs_from_sregions`` auto-derivation.

    With ``pscale=None``, ``rotation=None``, ``shape=None`` (all defaults),
    ``wcs_from_sregions`` derives:
    - pixel scale from the reference CRF's gwcs at the field reference point
      (``compute_scale``);
    - rotation from ``ref_wcsinfo['roll_ref']`` / ``v3yangle`` / ``vparity``
      (instrument frame, not ICRS-aligned);
    - shape sized to enclose the union footprint of all input s_regions.

    The resulting gwcs has ``.pixel_shape = (Nx, Ny)`` baked in. Returning
    array-shape ``(Ny, Nx)`` for downstream use is the caller's job.
    """
    from stcal.alignment.util import wcs_from_sregions
    from stdatamodels.jwst.datamodels import ImageModel

    sregions = []
    with ImageModel(crf_files[0]) as ref:
        ref_wcs = deepcopy(ref.meta.wcs)
        ref_wcsinfo = ref.meta.wcsinfo.instance
        sregions.append(ref.meta.wcsinfo.s_region)
    for crf in crf_files[1:]:
        sregions.append(fits.getheader(crf, extname='SCI')['S_REGION'])
    return wcs_from_sregions(
        sregions, ref_wcs=ref_wcs, ref_wcsinfo=ref_wcsinfo,
    )


def outlier_detect_for_visit(
    all_inputs,
    visit_files,
    *,
    snr=(3.0, 2.0),
    scale=(1.2, 0.7),
    backg=0.0,
    pixfrac=1.0,
    kernel='square',
    weight_type='ivm',
    good_bits='~DO_NOT_USE',
    in_memory=False,
    tempdir=None,
    extras_per_visit=None,
    plot=False,
):
    """Per-visit campfire-native outlier across ``all_inputs``, flag ``visit_files``.

    Parameters
    ----------
    all_inputs : list of str
        CRF paths to include in the median pool (visit's own files plus
        spatially-overlapping intra-program files). Drizzled into the
        per-visit intermediate WCS.
    visit_files : list of str
        The visit's own canonical CRF paths — the only files whose DQ
        gets updated and saved. Must be a subset of ``all_inputs``.
    snr, scale : tuples of float
        Two-pass ``(snr1, snr2)`` thresholds and ``(scale1, scale2)``
        derivative scaling factors for the median-comparison CR test.
    backg : float
        Scalar background added to the blotted image before the SNR
        comparison (overridden per-input by
        ``model.meta.background.level`` when ``subtracted=False``).
    pixfrac, kernel, weight_type, good_bits : drizzle parameters.
    in_memory : bool
        Hold per-input rasters in RAM
        (~``n_inputs × visit_wcs_pixels × float32 × 2``). At native scale
        per-visit this is typically a few hundred MB even for dense
        overlap regions — feasible at COSMOS scale.
    tempdir : str, optional
        Parent dir for ``MedianComputer`` temp files when ``in_memory=False``.
    extras_per_visit : dict, optional
        Map ``{rootname: [HDU, ...]}`` of extras (SRCMASK/CFMASK)
        captured before the run, preserved through ``atomic_save``.
    plot : bool
        If True, write ``<rootname>_outlier.pdf`` next to each visit
        exposure showing SCI plus the newly flagged OUTLIER pixels.

    Notes
    -----
    DQ updates are written via ``atomic_save`` with ``CFP_OUT`` stamped
    on the primary header. Caller does not need to re-stamp.
    """
    from jwst.datamodels.dqflags import pixel as pixel_flags
    from jwst.outlier_detection.utils import flag_resampled_model_crs
    from stcal.outlier_detection.median import MedianComputer
    from stdatamodels.jwst.datamodels import ImageModel

    from campfire_pipeline.nircam.drizzle import drizzle_tile_singles

    n_inputs = len(all_inputs)
    if n_inputs == 0:
        log("  outlier (per-visit): no inputs")
        return

    output_wcs = _build_visit_wcs(all_inputs)
    nx, ny = output_wcs.pixel_shape
    out_shape = (ny, nx)

    log(f"  campfire outlier (per-visit): {n_inputs} inputs into "
        f"{nx}x{ny} visit WCS")

    # tempdir='' makes MedianComputer drop its on-disk median scratch in the
    # current working directory (stcal/outlier_detection/median.py:269 calls
    # tempfile.TemporaryDirectory(dir=tempdir), and dir='' resolves against
    # CWD, not $TMPDIR). On networked-FS clusters CWD is the user's home
    # dir, which fills the home quota. Force a real tempdir.
    sci_mc = MedianComputer(
        full_shape=(n_inputs,) + out_shape,
        in_memory=in_memory, tempdir=tempdir or tempfile.gettempdir(),
    )

    # Reusable visit-shape scratch for pasting bbox-sliced rasters into
    # the full-shape array MedianComputer requires.
    scratch_sci = np.full(out_shape, np.nan, dtype=np.float32)

    contributing_paths = []
    for idx, ((sci_bbox, _wht_bbox, prep), crf) in enumerate(zip(
        drizzle_tile_singles(
            all_inputs, output_wcs, out_shape,
            pixfrac=pixfrac, kernel=kernel,
            weight_type=weight_type, good_bits=good_bits,
        ),
        all_inputs,
    )):
        sly, slx = prep['sly'], prep['slx']

        scratch_sci[sly, slx] = sci_bbox
        sci_mc.append(scratch_sci, idx=idx)
        scratch_sci[sly, slx] = np.nan

        contributing_paths.append(crf)

    log("  computing median...")
    median_sci = sci_mc.evaluate()
    del sci_mc

    snr_str = f"{snr[0]:.2f} {snr[1]:.2f}"
    scale_str = f"{scale[0]:.2f} {scale[1]:.2f}"
    log(f"  flagging CRs (snr={snr_str}, scale={scale_str})...")

    OUTLIER_BIT = pixel_flags['OUTLIER']
    visit_set = set(visit_files)
    extras_per_visit = extras_per_visit or {}

    n_flagged_total = 0
    n_visit_flagged = 0
    for crf in contributing_paths:
        if crf not in visit_set:
            continue  # only flag and save the visit's own files
        with ImageModel(crf) as model:
            dq_before = (model.dq & OUTLIER_BIT) != 0
            sci_for_plot = model.data.copy() if plot else None
            # Match jwst.outlier_detection.imaging.detect_outliers: pass
            # only median_sci. flag_resampled_model_crs falls back to the
            # input model's own ERR for the SNR comparison when median_err
            # is None — using a blotted-median ERR instead would
            # systematically over-flag (median_err is smoother than
            # native model.err, which inflates computed SNRs).
            flag_resampled_model_crs(
                model, median_sci, output_wcs,
                snr1=snr[0], snr2=snr[1],
                scale1=scale[0], scale2=scale[1],
                backg=backg,
            )
            dq_after = (model.dq & OUTLIER_BIT) != 0
            n_flagged_total += int((dq_after & ~dq_before).sum())
            n_visit_flagged += 1

            rootname = os.path.basename(crf).removesuffix('.fits')
            atomic_save(
                model, crf,
                header_updates=cfp.format(CFP_OUT=None),
                extra_hdus=extras_per_visit.get(rootname),
            )

            if plot:
                from campfire_pipeline.nircam.steps._plots import plot_outlier
                new_outlier = dq_after & ~dq_before
                out_pdf = os.path.join(
                    os.path.dirname(crf), f'{rootname}_outlier.pdf',
                )
                plot_outlier(
                    sci_for_plot, new_outlier,
                    save_file=out_pdf,
                    title=f'{rootname}: outlier (campfire)',
                )
                log(f"  saved {os.path.basename(out_pdf)}")

    log(f"  outlier: flagged {n_flagged_total} new pixels across "
        f"{n_visit_flagged} visit exposures")
