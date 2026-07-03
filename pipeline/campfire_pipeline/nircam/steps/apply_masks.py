"""
apply_masks: record user region-file masks on the canonical exposure.

First step of the combine phase. Reads ``.reg`` files from
``mask_dir/<filter>/<rootname>.reg``, rasterizes each region to a pixel mask
using the exposure WCS, and writes the union as a ``CFMASK`` extension on the
canonical file. The canonical's SCI and DQ are left untouched — the mask is a
per-exposure property recorded *alongside* the science data, not baked into it.

The mask's actual effect on the mosaic (exclusion from outlier detection and
resample, via ``good_bits='~DO_NOT_USE'``) happens later, on the disposable
combine working copy, where ``Field.materialize_work`` fuses CFMASK into the
working DQ as ``DO_NOT_USE``. Keeping the canonical DQ mask-free means (a) the
canonical stays byte-identical to the process output apart from the added
CFMASK extension — so it deploys and re-reviews cleanly — and (b) mask edits
are non-destructive and fully reversible, since CFMASK is rebuilt from scratch
every run.

Because ``apply_masks`` short-circuits when the canonical already carries
``CFP_MASK``, re-run with ``--reset-from apply_masks`` (or ``--overwrite``) to
pick up an edited ``.reg`` file.

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
        ``[nircam.apply_mask]`` (legacy ``[nircam.stage2.apply_mask]``). No
        tunable keys — the mask is recorded as a CFMASK extension and honored
        via DO_NOT_USE on the combine working copy (``Field.materialize_work``).
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

    log(f"Applying masks from {os.path.basename(reg_file)} to {rootname}")

    from regions import Regions
    from jwst.datamodels import ImageModel

    with ImageModel(exposure_file) as model:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            wcs = model.get_fits_wcs()
        shape = model.data.shape

        # CFMASK is a plain 0/1 union of the user regions, rebuilt from scratch
        # each run. It records *what* is masked; the DO_NOT_USE fuse (how the
        # mask is honored) happens on the combine working copy, so the canonical
        # SCI/DQ are never touched here.
        cfmask = np.zeros(shape, np.uint8)
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

            cfmask |= mask_arr.astype(np.uint8)

        cfmask_hdu = fits.ImageHDU(cfmask, name='CFMASK')
        atomic_save(
            model, exposure_file,
            header_updates=cfp.format(CFP_MASK=None),
            extra_hdus=[cfmask_hdu],
        )
        log(f"Masks applied: {rootname}")
