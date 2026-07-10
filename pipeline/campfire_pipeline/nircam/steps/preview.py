"""
preview: render per-exposure quick-look PNGs for web admin triage.

Per-exposure step. Renders **per-pixel SNR** (``SCI / ERR``) — not raw SCI —
and writes two PNGs next to the canonical FITS file:

  * ``{rootname}_preview.png`` — downsampled (long-axis ``max_dim``) for the
    admin table thumbnail
  * ``{rootname}_full.png`` — native-resolution, used as the canvas for the
    in-browser polygon mask editor

SNR (rather than SCI) is deliberate: the ``jump`` step drops snowball/cosmic-ray
groups from the ramp fit, which inflates ``VAR_RNOISE`` → ``ERR`` on the
affected pixels. In an SNR render a snowball that the pipeline has already
error-weighted correctly sinks back into the ~N(0,1) noise floor, while a
residual the error model *under*-weights stays visibly significant — so the
reviewer's eye is drawn to what actually warrants a hand mask, not to every
bright-but-already-downweighted artifact. See the module change in
``pipeline/CHANGELOG.md``.

Both PNGs use the same ZScale stretch computed on the downsampled SNR map (so
the editor and the thumbnail look identical), and both are ``origin='lower'`` so
PNG row 0 corresponds to ``data[H-1, :]`` — the polygon editor's canvas inverts
``y`` accordingly when round-tripping to DS9 ``image`` coords.

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
    """Render thumbnail + native-res SNR preview PNGs for a single exposure."""
    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    out_dir = os.path.dirname(exposure_file)
    thumb_path = os.path.join(out_dir, f'{rootname}_preview.png')
    full_path = os.path.join(out_dir, f'{rootname}_full.png')

    # Both the header stamp and *both* PNGs must exist for a skip — if either
    # was deleted out-of-band, regenerate.
    if (os.path.exists(thumb_path) and os.path.exists(full_path)
            and cfp.should_skip(exposure_file, 'CFP_PREV', rootname,
                                'preview', status, overwrite)):
        return

    log(f"Rendering SNR preview for {rootname}")

    from jwst.datamodels import ImageModel

    max_dim = int(step_config.get('max_dim', 1024))
    cmap = step_config.get('cmap', 'Greys')

    with ImageModel(exposure_file) as model:
        snr = _snr_map(np.asarray(model.data, dtype=np.float64),
                       np.asarray(model.err, dtype=np.float64))

        # ZScale is computed on the downsampled SNR map (fast, robust) and
        # reused for the full-res render so both PNGs share contrast. The
        # full-res render is the mask-editor canvas; the thumbnail is a
        # block-mean downsample of the same SNR map.
        long_axis = max(snr.shape)
        block_size = max(1, int(np.ceil(long_axis / max_dim)))
        snr_d = _block_reduce(snr, block_size)
        vmin, vmax = _zscale_limits(snr_d)

        _atomic_imsave(thumb_path, snr_d, cmap=cmap, vmin=vmin, vmax=vmax)
        _atomic_imsave(full_path,  snr,   cmap=cmap, vmin=vmin, vmax=vmax)

        atomic_save(
            model, exposure_file,
            header_updates=cfp.format(CFP_PREV=None),
        )

    h_d, w_d = snr_d.shape
    h_f, w_f = snr.shape
    log(f"SNR preview written: {os.path.basename(thumb_path)} ({w_d}×{h_d}), "
        f"{os.path.basename(full_path)} ({w_f}×{h_f})")


def _snr_map(sci, err):
    """Per-pixel SNR = SCI / ERR, with non-finite / zero-ERR pixels set to 0.

    ERR is zero or non-finite on masked / zero-coverage pixels (and can be
    ``inf`` where the background step sets a degenerate variance); dividing
    those through would poison the render, so they collapse to 0 — the noise
    floor — which keeps the mask-editor canvas fully filled.
    """
    with np.errstate(divide='ignore', invalid='ignore'):
        snr = sci / err
    return np.where(np.isfinite(snr), snr, 0.0)


def _atomic_imsave(out_path, arr, *, cmap, vmin, vmax):
    """``plt.imsave`` with origin='lower', via a .tmp + rename for atomicity.

    ``format='png'`` is passed explicitly because matplotlib delegates
    extension sniffing to Pillow, which raises ``KeyError: 'TMP'`` on the
    transient ``.tmp`` suffix on newer Pillow versions.
    """
    import matplotlib.pyplot as plt
    tmp_path = out_path + '.tmp'
    plt.imsave(tmp_path, arr, cmap=cmap, vmin=vmin, vmax=vmax,
               origin='lower', format='png')
    os.replace(tmp_path, out_path)
