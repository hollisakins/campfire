"""
preview: render a per-exposure quick-look PNG for web admin triage.

Per-exposure step. Reads SCI from the canonical exposure, block-reduces to a
configurable long-axis pixel cap, applies a ZScale stretch, and writes
``{rootname}_preview.png`` next to the canonical FITS file. The PNG is
intended to be picked up by ``campfire deploy nircam`` and uploaded to R2
for the ``/admin/nircam`` review UI, where the user tabs through exposures
to flag ones needing manual masking or exclusion.

Runs as the penultimate process step, just before ``jhat``: the preview
captures the data state after all per-exposure SCI mutations (wisp, 1/f,
sky, variance) but before WCS alignment, so reviewers see the science
pixels they are deciding to keep or drop without alignment-related warps
hiding artifacts.

Read-only with respect to pixel data — no SCI/DQ/ERR mutation. Stamps
``CFP_PREV`` with an ISO timestamp.
"""

import os

import numpy as np

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.steps._plots import _block_reduce, _zscale_limits


def preview_step(exposure_file, field, step_config, overwrite=False,
                 status=None):
    """Render a per-exposure preview PNG."""
    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    out_path = os.path.join(
        os.path.dirname(exposure_file), f'{rootname}_preview.png'
    )

    if not overwrite:
        already_done = (status.has(exposure_file, 'CFP_PREV')
                        if status is not None
                        else cfp.has_step(exposure_file, 'CFP_PREV'))
        # Both the header stamp and the PNG must exist for a skip — if the
        # PNG was deleted out-of-band the stamp alone is misleading.
        if already_done and os.path.exists(out_path):
            log(f"Skipping preview on {rootname}: CFP_PREV already set")
            return

    log(f"Rendering preview for {rootname}")

    import matplotlib.pyplot as plt
    from jwst.datamodels import ImageModel

    max_dim = int(step_config.get('max_dim', 1024))
    cmap = step_config.get('cmap', 'Greys')

    with ImageModel(exposure_file) as model:
        sci = np.asarray(model.data)

        long_axis = max(sci.shape)
        block_size = max(1, int(np.ceil(long_axis / max_dim)))
        sci_d = _block_reduce(sci, block_size)
        vmin, vmax = _zscale_limits(sci_d)

        tmp_path = out_path + '.tmp'
        # plt.imsave produces a PNG whose pixel dimensions match the
        # downsampled array exactly — no matplotlib decoration, small files.
        plt.imsave(tmp_path, sci_d, cmap=cmap, vmin=vmin, vmax=vmax,
                   origin='lower')
        os.replace(tmp_path, out_path)

        atomic_save(
            model, exposure_file,
            header_updates=cfp.format(CFP_PREV=None),
        )

    h, w = sci_d.shape
    log(f"Preview written: {os.path.basename(out_path)} ({w}×{h})")
