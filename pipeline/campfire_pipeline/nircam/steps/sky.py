"""
sky: subtract a constant sky pedestal from the canonical exposure SCI.

Per-exposure step. Reads the ``SRCMASK`` extension written by striping (no
more cross-stage sidecar lookup), takes pixels with ``DQ == 0`` and
``SRCMASK == 0``, sigma-clips, fits a Gaussian to the sky distribution, and
subtracts the fitted mean from SCI. Updates ``meta.background.*`` and stamps
``CFP_SKY`` with the pedestal value.
"""

import os
from datetime import datetime

import numpy as np
from astropy.io import fits
from astropy.stats import sigma_clip

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.skyfit import fit_sky_tot


def sky_step(exposure_file, field, step_config, overwrite=False):
    """Subtract a fitted sky pedestal from a single canonical exposure."""
    rootname = os.path.basename(exposure_file).removesuffix('.fits')

    if not overwrite and cfp.has_step(exposure_file, 'CFP_SKY'):
        log(f"Skipping sky on {rootname}: CFP_SKY already set")
        return

    # Read SRCMASK from the canonical file's extension (was a sidecar in the
    # legacy layout).
    with fits.open(exposure_file) as hdul:
        if 'SRCMASK' not in hdul:
            log(f"No SRCMASK on {rootname}; cannot run sky step "
                f"(striping must run first)")
            return
        seg = hdul['SRCMASK'].data.copy()

    log(f"Running sky on {rootname}")

    from jwst.datamodels import ImageModel
    from stdatamodels import util as stutil

    with ImageModel(exposure_file) as model:
        sci = model.data
        dq = model.dq
        idx = np.where((dq == 0) & (seg == 0))
        data = sci[idx].flatten()

        data = sigma_clip(
            data, sigma_upper=3, sigma_lower=10, maxiters=5, masked=False,
        )
        data = data[~np.isinf(data) & ~np.isnan(data)]

        try:
            sky = float(fit_sky_tot(data))
        except Exception:
            log(f"Sky fit failed on {rootname}")
            raise

        model.data = sci - sky
        model.meta.background.level = sky
        model.meta.background.subtracted = True
        model.meta.background.method = 'local'

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        model.history.append(stutil.create_history_entry(
            f'Removed sky {now}'
        ))

        atomic_save(
            model, exposure_file,
            header_updates=cfp.format(CFP_SKY=f'{sky:.5e}'),
        )
        log(f"Sky removed (pedestal = {sky:.5e}): {rootname}")
