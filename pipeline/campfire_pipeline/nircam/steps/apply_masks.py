"""
apply_masks: paint user region-file masks into the canonical exposure DQ.

First step of the mosaic phase. Reads ``.reg`` files from
``mask_dir/<filter>/<rootname>.reg``, rasterizes each region to a pixel mask
using the exposure WCS, and writes the result to a ``CFMASK`` extension on
the canonical file. Then OR's ``CFMASK`` into ``DQ`` (with the user-chosen
flag bit, default ``1024``) so downstream JWST steps (outlier detection,
resample) honor it through their ``dqbits`` parameters.

CFMASK is rebuilt from scratch every run, replacing any existing CFMASK
extension on the canonical file. This gives the user a clean way to *add*
mask regions iteratively (re-running with ``--overwrite`` widens DQ
correctly). Mask *removal* requires ``--reset-from apply_masks`` because
DQ updates are cumulative — the orchestrator enforces this.

If there is no ``.reg`` file for an exposure, the step still stamps
``CFP_MASK = 'no .reg file'`` so the status command can distinguish
"ran-but-n/a" from "not yet run". CFMASK is not created in this case.
"""

import os
import warnings

import numpy as np
from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


def apply_masks_step(exposure_file, field, step_config, overwrite=False,
                     status=None):
    """Apply region-file masks to a single canonical exposure.

    Parameters
    ----------
    exposure_file : str
    field : Field
    step_config : dict
        ``[nircam.apply_mask]`` (legacy ``[nircam.stage2.apply_mask]``).
        Keys: ``mask_flag`` (DQ bit, default 1024), ``mask_set_nan``
        (boolean, default False — also write NaN to SCI for masked pixels).
    overwrite : bool
    status : StepStatus, optional
        Pre-scanned CFP_* status cache.
    """
    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    filtname = exposure_file.split('/')[-2]

    if cfp.should_skip(exposure_file, 'CFP_MASK', rootname,
                       'apply_masks', status, overwrite):
        return

    reg_file = os.path.join(field.mask_dir, filtname, f'{rootname}.reg')

    if not os.path.exists(reg_file):
        log(f"No mask file for {rootname}; stamping CFP_MASK='no .reg file'")
        from jwst.datamodels import ImageModel
        with ImageModel(exposure_file) as m:
            atomic_save(
                m, exposure_file,
                header_updates=cfp.format(CFP_MASK='no .reg file'),
            )
        return

    flag = step_config.get('mask_flag', 1024)
    set_to_nan = step_config.get('mask_set_nan', False)

    log(f"Applying masks from {os.path.basename(reg_file)} to {rootname}")

    from regions import Regions
    from jwst.datamodels import ImageModel

    with ImageModel(exposure_file) as model:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            wcs = model.get_fits_wcs()
        shape = model.data.shape

        cfmask = np.zeros(shape, np.uint32)
        regs = Regions.read(reg_file)
        for reg in regs:
            try:
                reg_pix = reg.to_pixel(wcs)
                mask_obj = reg_pix.to_mask(mode='center')
                mask_arr = mask_obj.to_image(shape)
                mask_arr = mask_arr.astype(bool)
            except (ValueError, TypeError) as e:
                log(f"Warning: skipping region in {reg_file}: {e}")
                continue

            cfmask |= (mask_arr * flag).astype(np.uint32)
            if set_to_nan:
                model.data[mask_arr] = np.nan

        # OR user mask into DQ. Note: DQ updates are cumulative — re-running
        # with a smaller .reg does not unflag previously masked pixels.
        # Use --reset-from apply_masks for clean removal.
        model.dq |= cfmask

        cfmask_hdu = fits.ImageHDU(cfmask, name='CFMASK')
        atomic_save(
            model, exposure_file,
            header_updates=cfp.format(CFP_MASK=None),
            extra_hdus=[cfmask_hdu],
        )
        log(f"Masks applied: {rootname}")
