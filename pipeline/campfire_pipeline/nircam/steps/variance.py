"""
variance: rescale ``VAR_RNOISE`` to match the measured sky variance.

Per-exposure step. Runs the iterative source masking + background fit from
``SubtractBackground`` *in memory only* (no scratch ``_cal_bkgsub.fits``
file written), uses the resulting mask + the original SCI to estimate the
sky variance via ``biweight_midvariance`` on a block-reduced image, and
scales ``VAR_RNOISE`` by the ratio of measured-to-current sky variance.
Also fixes any zero entries in ``VAR_RNOISE``/``VAR_POISSON``/``VAR_FLAT``
to ``inf`` so they don't propagate as bogus zero-uncertainty pixels.

Stamps ``CFP_VAR`` with the correction factor.
"""

import os
from datetime import datetime

import numpy as np
from astropy.nddata import block_reduce
from astropy.stats import biweight_location, biweight_midvariance

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


def variance_step(exposure_file, field, step_config, overwrite=False):
    """Rescale variance maps on a single canonical exposure."""
    rootname = os.path.basename(exposure_file).removesuffix('.fits')

    if not overwrite and cfp.has_step(exposure_file, 'CFP_VAR'):
        log(f"Skipping variance on {rootname}: CFP_VAR already set")
        return

    log(f"Rescaling variance for {rootname}")

    from jwst.datamodels import ImageModel
    from stdatamodels import util as stutil
    from campfire_pipeline.nircam.bkgsub import SubtractBackground

    bkg = SubtractBackground(
        ring_radius_in=step_config.get('ring_radius_in', 40),
        ring_width=step_config.get('ring_width', 3),
        ring_clip_max_sigma=step_config.get('ring_clip_max_sigma', 5.0),
        ring_clip_box_size=step_config.get('ring_clip_box_size', 100),
        ring_clip_filter_size=step_config.get('ring_clip_filter_size', 3),
        tier_kernel_size=step_config.get('tier_kernel_size', [25, 15, 5, 2]),
        tier_npixels=step_config.get('tier_npixels', [15, 15, 5, 2]),
        tier_nsigma=step_config.get('tier_nsigma', [3, 3, 3, 3]),
        tier_dilate_size=step_config.get('tier_dilate_size', [0, 0, 0, 3]),
        bg_box_size=step_config.get('bg_box_size', 10),
        bg_filter_size=step_config.get('bg_filter_size', 5),
        bg_exclude_percentile=step_config.get('bg_exclude_percentile', 90),
        bg_sigma=step_config.get('bg_sigma', 3),
        bg_interpolator=step_config.get('bg_interpolator', 'zoom'),
        suffix='bkgsub',
        replace_sci=True,
    )

    try:
        _, mask_final, _ = bkg.compute(exposure_file)
    except Exception:
        log(f"variance: bkgsub.compute failed on {exposure_file}")
        raise

    block_size = step_config.get('block_size', 7)

    with ImageModel(exposure_file) as model:
        sci = model.data
        var_rnoise = model.var_rnoise

        block_mask = block_reduce(mask_final, block_size)
        unmasked_frac = np.sum(block_mask == 0) / np.sum(block_mask >= 0)

        block_sci = block_reduce(sci, block_size)
        block_mask_bool = block_mask != 0
        unmasked_bins = block_sci[block_mask_bool == 0]
        variance = biweight_midvariance(unmasked_bins)
        skyvar = variance / block_size ** 2

        block_var_rnoise = block_reduce(var_rnoise, block_size)
        unmasked_bins = block_var_rnoise[block_mask_bool == 0]
        masked_mean_var_rnoise = (
            biweight_location(unmasked_bins) / block_size ** 2
        )

        correction_factor = skyvar / masked_mean_var_rnoise
        predicted_skyvar = correction_factor * var_rnoise
        model.var_rnoise = predicted_skyvar

        log(f"Robust masked mean VAR_RDNOISE: {masked_mean_var_rnoise:.3e}")
        log(f"Robust masked mean SKY_VARIANCE: {skyvar:.3e}")
        log(f"Correction factor: {correction_factor:.2f}")
        log(f"Fraction unmasked: {unmasked_frac * 100:.1f}%")

        # Replace any zero entries with inf so they don't masquerade as
        # zero-uncertainty pixels downstream.
        rnoise = model.var_rnoise
        poisson = model.var_poisson
        flat = model.var_flat
        rnoise[rnoise == 0] = np.inf
        poisson[poisson == 0] = np.inf
        flat[flat == 0] = np.inf
        model.var_rnoise = rnoise
        model.var_poisson = poisson
        model.var_flat = flat

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        model.history.append(stutil.create_history_entry(
            f'Rescaled variance {now}'
        ))

        atomic_save(
            model, exposure_file,
            header_updates=cfp.format(
                CFP_VAR=f'{float(correction_factor):.3f}',
            ),
        )
        log(f"Variance rescaled (factor={correction_factor:.2f}): {rootname}")
