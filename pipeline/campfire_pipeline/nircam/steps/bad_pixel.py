"""
bad_pixel: stack DO_NOT_USE flags across a filter's exposures to derive
a "consistently bad" reference mask.

This step earns its keep only when N is large enough for the empirical
per-pixel DO_NOT_USE rate to add information beyond the CRDS bad-pixel
mask already in cal-file DQ — i.e. the COSMOS-Web-style regime of many
overlapping exposures. With small N (e.g. 8 exposures per filter), a
loose threshold turns transient flags into permanent ones; the step is
therefore **disabled by default** and must be opted in per-field via
``[nircam.bad_pixel].enabled = true``.

Two entry points:

* ``build_bad_pixel_masks`` (ensemble): glob the per-filter canonical
  exposure files, stack the DO_NOT_USE bit of their DQ arrays per
  detector, threshold the per-pixel fraction, and write
  ``fl_pixels_<filter>_<detector>.fits`` reference products into
  ``field.bad_pixel_dir``. This is a per-field reference product, not a
  per-exposure mutation.

* ``bad_pixel_step`` (per-exposure): OR the per-detector bad-pixel mask
  into the canonical DQ as DO_NOT_USE. Stamps ``CFP_BPIX`` with the
  threshold value.

Only the DO_NOT_USE bit is considered when stacking — flags such as
JUMP_DET, SATURATED, and PERSISTENCE are transient and should not
promote a pixel to "permanently bad" through repeated occurrence.
"""

import os
from datetime import datetime

import numpy as np
import tqdm
from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.constants import LW_FILTERS, SW_FILTERS


SW_DETECTORS = ['nrca1', 'nrca2', 'nrca3', 'nrca4',
                'nrcb1', 'nrcb2', 'nrcb3', 'nrcb4']
LW_DETECTORS = ['nrcalong', 'nrcblong']
ALL_DETECTORS = LW_DETECTORS + SW_DETECTORS  # LW first so the loop below
                                              # matches 'nrcalong' before
                                              # 'nrca1' on substring fallback


def _detectors_for_filter(filtname):
    if filtname.lower() in SW_FILTERS:
        return SW_DETECTORS
    if filtname.lower() in LW_FILTERS:
        return LW_DETECTORS
    raise ValueError(f"Unknown filter: {filtname}")


def _detector_of(exposure_path):
    """Detector from a canonical ``<rootname>.fits`` path (last token)."""
    base = os.path.basename(exposure_path).removesuffix('.fits')
    return base.rsplit('_', 1)[-1]


def build_bad_pixel_masks(filtname, exposure_files, field, step_config,
                          overwrite=False):
    """Stack DQ arrays from each detector and write bad-pixel reference files.

    Parameters
    ----------
    filtname : str
    exposure_files : list of str
        Canonical exposure paths for this filter.
    field : Field
    step_config : dict
        ``[nircam.bad_pixel]`` block. Reads ``threshold`` (default 0.8 — the
        fraction of stacked exposures at which a pixel must carry the
        DO_NOT_USE bit to be promoted to permanent bad).
    overwrite : bool
    """
    threshold = step_config.get('threshold', 0.8)
    detectors = _detectors_for_filter(filtname)

    target_files = [
        os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_{det}.fits')
        for det in detectors
    ]
    if not overwrite and all(os.path.exists(f) for f in target_files):
        log(f"Bad pixel masks for {filtname} exist; skipping build")
        return

    log(f"Building bad pixel masks for {filtname} from "
        f"{len(exposure_files)} exposures (threshold={threshold:.2f}, "
        f"DO_NOT_USE only)")

    det_arrays = {det: np.zeros((2048, 2048), dtype=np.float32)
                  for det in detectors}
    det_counts = {det: 0 for det in detectors}
    with tqdm.tqdm(total=len(exposure_files)) as pbar:
        for ef in exposure_files:
            try:
                dq = fits.getdata(ef, extname='DQ')
            except KeyError:
                log(f"  skipped {os.path.basename(ef)} (no DQ)")
                pbar.update(1)
                continue
            # Only consider DO_NOT_USE (bit 0). Transient bits like
            # JUMP_DET, SATURATED, PERSISTENCE must not contribute.
            flag = (np.asarray(dq).astype(np.int32) & 1).astype(np.float32)
            det = _detector_of(ef)
            if det in det_arrays:
                det_arrays[det] += flag
                det_counts[det] += 1
            pbar.update(1)

    # Raw stacked DQ for inspection (kept for diagnostics/debugging)
    for det in detectors:
        fits.writeto(
            os.path.join(field.bad_pixel_dir,
                         f'stack_dq_{filtname}_{det}.fits'),
            det_arrays[det],
            overwrite=True,
        )

    # Threshold to a 0/1 mask: pixel is "bad" if DO_NOT_USE in
    # >= threshold * n_exposures_for_this_detector. Normalising by the
    # actual count (not np.max) makes the threshold a true exposure
    # fraction independent of how many static defects the detector has.
    for det in detectors:
        arr = det_arrays[det]
        n = det_counts[det]
        if n > 0:
            frac = arr / float(n)
            mask = (frac >= threshold).astype(np.float32)
        else:
            mask = np.zeros_like(arr)
        fits.writeto(
            os.path.join(field.bad_pixel_dir,
                         f'fl_pixels_{filtname}_{det}.fits'),
            mask,
            overwrite=True,
        )

    log(f"Bad pixel masks written to {field.bad_pixel_dir}/")


def bad_pixel_step(exposure_file, field, step_config, overwrite=False,
                   status=None):
    """OR the per-detector bad-pixel mask into a canonical exposure's DQ."""
    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    filtname = exposure_file.split('/')[-2]

    if not overwrite:
        already_done = (status.has(exposure_file, 'CFP_BPIX')
                        if status is not None
                        else cfp.has_step(exposure_file, 'CFP_BPIX'))
        if already_done:
            log(f"Skipping bad_pixel on {rootname}: CFP_BPIX already set")
            return

    detector = _detector_of(exposure_file)
    if detector not in ALL_DETECTORS:
        log(f"bad_pixel: unknown detector '{detector}' for {rootname}")
        return

    fl_path = os.path.join(
        field.bad_pixel_dir, f'fl_pixels_{filtname}_{detector}.fits',
    )
    if not os.path.exists(fl_path):
        log(f"bad_pixel: {os.path.basename(fl_path)} not found; "
            f"run build_bad_pixel_masks first")
        return

    threshold = step_config.get('threshold', 0.8)
    log(f"Applying bad-pixel mask to {rootname}")

    from jwst.datamodels import ImageModel
    from stdatamodels import util as stutil

    fl = fits.getdata(fl_path).astype(bool)

    with ImageModel(exposure_file) as model:
        model.dq[fl] |= 1  # DO_NOT_USE

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        model.history.append(stutil.create_history_entry(
            f'Masked bad pixels; {now}'
        ))

        atomic_save(
            model, exposure_file,
            header_updates=cfp.format(
                CFP_BPIX=f'threshold={threshold:.2f}',
            ),
        )
        log(f"Bad pixels applied: {rootname}")
