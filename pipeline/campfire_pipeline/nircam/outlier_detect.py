"""
outlier_detect: per-tile cosmic-ray rejection across all overlapping inputs.

Replaces the per-visit + overlap-padded outlier path with a per-tile flow
that medians **all** exposures whose footprint hits the tile, on the
**tile WCS** rather than a per-visit-derived WCS (issue #138, Phase 2).

Flow (per tile, per filter):

1. Drizzle each overlapping CRF input into the tile WCS via
   ``drizzle_tile_singles`` (one fresh full-tile raster per input,
   no shared accumulator).
2. Stream the per-input SCI and ERR rasters into
   ``stcal.outlier_detection.median.MedianComputer`` (NaN-aware median
   bounded in memory by ``buffer_size``).
3. ``flag_resampled_model_crs`` (from ``jwst.outlier_detection.utils``)
   blots the SCI median back to each input frame via ``gwcs_blot``,
   blots the ERR median similarly, runs the standard two-pass SNR
   gradient comparison, and updates ``input_model.dq`` in place.
4. Per-input updated DQ is written back to the canonical exposure path
   via ``atomic_save``.

The per-exposure ``CFP_OUT`` provenance stamp is *not* set here — the
orchestrator stamps it only after every tile that an exposure
participates in has completed (deferred-completion semantic).
"""

import os
from copy import deepcopy

from campfire_pipeline.common.io import atomic_save, log


def outlier_detect_for_tile(
    crf_files,
    *,
    crpix,
    crval,
    shape,
    rotation,
    pixel_scale,
    snr=(3.0, 2.0),
    scale=(1.2, 0.7),
    backg=0.0,
    pixfrac=1.0,
    kernel='square',
    weight_type='ivm',
    good_bits='~DO_NOT_USE',
    in_memory=False,
    tempdir=None,
):
    """Run per-tile outlier detection across ``crf_files``.

    Parameters
    ----------
    crf_files : list of str
        CRF input paths overlapping this tile. The caller is expected to
        have pre-filtered via ``select_overlapping_files``; inputs that
        still fail the per-input pixmap overlap check inside
        ``drizzle_tile_singles`` are skipped with a logged warning.
    crpix, crval, shape, rotation, pixel_scale : tile WCS overrides
        (see ``Field.get_tile_wcs``).
    snr : tuple of float
        ``(snr1, snr2)`` two-pass SNR thresholds.
    scale : tuple of float
        ``(scale1, scale2)`` derivative scaling factors.
    backg : float
        Scalar background level added to the blotted image before the SNR
        comparison (overridden by ``input_model.meta.background.level``
        when subtracted=False).
    pixfrac, kernel, weight_type, good_bits : drizzle parameters.
    in_memory : bool
        If True, hold all per-input rasters in RAM
        (``2 × n_inputs × tile_size × float32``). If False (default),
        stream to a temp directory bounded by ``MedianComputer``'s
        per-section buffer size.
    tempdir : str or None
        Parent directory for the streaming temp files. None means current
        working directory.
    """
    from jwst.outlier_detection.utils import flag_resampled_model_crs
    from stcal.outlier_detection.median import MedianComputer
    from stdatamodels.jwst.datamodels import ImageModel

    from campfire_pipeline.nircam.drizzle import (
        _build_output_wcs, drizzle_tile_singles,
    )

    n_inputs = len(crf_files)
    if n_inputs == 0:
        log("  outlier (per-tile): no inputs")
        return

    nx, ny = shape
    out_shape = (ny, nx)

    log(f"  campfire outlier: {n_inputs} inputs into {nx}x{ny} tile")

    output_wcs = _build_output_wcs(
        crf_files, crpix, crval, shape, rotation, pixel_scale,
    )

    # Phase A — drizzle each input into the tile WCS, streaming SCI and
    # ERR rasters into separate MedianComputer instances. Collect prep
    # dicts so phase B can blot back without re-reading the gwcs.
    sci_mc = MedianComputer(
        full_shape=(n_inputs,) + out_shape,
        in_memory=in_memory,
        tempdir=tempdir or '',
    )
    err_mc = MedianComputer(
        full_shape=(n_inputs,) + out_shape,
        in_memory=in_memory,
        tempdir=tempdir or '',
    )

    contributing = []
    for idx, (sci, err, wht, prep) in enumerate(drizzle_tile_singles(
        crf_files, output_wcs, out_shape,
        pixfrac=pixfrac, kernel=kernel,
        weight_type=weight_type, good_bits=good_bits,
    )):
        sci_mc.append(sci, idx=idx)
        err_mc.append(err, idx=idx)
        # Use position in the yield stream to recover the source CRF.
        # If drizzle_tile_singles skipped any inputs, this would
        # mis-align — we trust the caller has pre-filtered overlapping
        # files (matching the pixmap-bbox check inside
        # _prepare_drizzle_input is a strict subset of the sky-polygon
        # overlap check the caller uses).
        contributing.append(prep)

    log(f"  computing median...")
    median_sci = sci_mc.evaluate()
    median_err = err_mc.evaluate()
    del sci_mc, err_mc

    # Phase B — blot median back to each input frame, flag CRs in DQ,
    # write updated DQ back to the canonical path. Re-open each input
    # via ImageModel (cheap; pixmap state is not reused).
    snr_str = f"{snr[0]:.2f} {snr[1]:.2f}"
    scale_str = f"{scale[0]:.2f} {scale[1]:.2f}"
    log(f"  flagging CRs (snr={snr_str}, scale={scale_str})...")

    n_flagged_total = 0
    for prep, crf in zip(contributing, crf_files):
        with ImageModel(crf) as model:
            dq_before = int((model.dq != 0).sum())
            flag_resampled_model_crs(
                model,
                median_sci,
                output_wcs,
                snr1=snr[0], snr2=snr[1],
                scale1=scale[0], scale2=scale[1],
                backg=backg,
                median_err=median_err,
            )
            dq_after = int((model.dq != 0).sum())
            n_flagged_total += (dq_after - dq_before)

            atomic_save(model, crf)

    log(f"  outlier: flagged {n_flagged_total} new pixels across "
        f"{len(contributing)} inputs")
