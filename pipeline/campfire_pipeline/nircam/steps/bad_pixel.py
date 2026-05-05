"""
bad_pixel: stack DQ across a filter's exposures to flag bad pixels.

Two entry points:

* ``build_bad_pixel_masks`` (ensemble): glob the per-filter canonical
  exposure files, stack their DQ arrays per detector, threshold the
  per-pixel hit fraction, and write
  ``fl_pixels_<filter>_<detector>.fits`` reference products into
  ``field.bad_pixel_dir``. This is a per-field reference product, not a
  per-exposure mutation.

* ``bad_pixel_step`` (per-exposure): OR the per-detector bad-pixel mask
  into the canonical DQ. Stamps ``CFP_BPIX`` with the threshold value.

The orchestrator calls ``build_bad_pixel_masks`` once per filter, then
dispatches ``bad_pixel_step`` across the exposures in parallel.
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
        ``[nircam.bad_pixel]`` block. Reads ``threshold`` (default 0.2 — the
        fraction of stacked exposures at which a pixel is considered bad).
    overwrite : bool
    """
    threshold = step_config.get('threshold', 0.2)
    detectors = _detectors_for_filter(filtname)

    target_files = [
        os.path.join(field.bad_pixel_dir, f'fl_pixels_{filtname}_{det}.fits')
        for det in detectors
    ]
    if not overwrite and all(os.path.exists(f) for f in target_files):
        log(f"Bad pixel masks for {filtname} exist; skipping build")
        return

    log(f"Building bad pixel masks for {filtname} from "
        f"{len(exposure_files)} exposures")

    det_arrays = {det: np.zeros((2048, 2048)) for det in detectors}
    with tqdm.tqdm(total=len(exposure_files)) as pbar:
        for ef in exposure_files:
            try:
                flag = fits.getdata(ef, extname='DQ').astype(np.float32)
            except KeyError:
                log(f"  skipped {os.path.basename(ef)} (no DQ)")
                pbar.update(1)
                continue
            flag[flag >= 1] = 1
            det = _detector_of(ef)
            if det in det_arrays:
                det_arrays[det] += flag
            pbar.update(1)

    # Raw stacked DQ for inspection (kept for diagnostics/debugging)
    for det in detectors:
        fits.writeto(
            os.path.join(field.bad_pixel_dir,
                         f'stack_dq_{filtname}_{det}.fits'),
            det_arrays[det],
            overwrite=True,
        )

    # Threshold to a 0/1 mask
    for det in detectors:
        arr = det_arrays[det]
        mx = float(np.max(arr))
        if mx > 0:
            arr /= mx
            arr[arr > threshold] = 1
            arr[arr <= threshold] = 0
        fits.writeto(
            os.path.join(field.bad_pixel_dir,
                         f'fl_pixels_{filtname}_{det}.fits'),
            arr,
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

    threshold = step_config.get('threshold', 0.2)
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
