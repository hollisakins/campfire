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

(Historical note: jump-flagged pixels also used to carry a coherent flux
zero-point offset — smooth light/dark disks at snowball footprints in these
renders — caused by frame-common ramp curvature meeting per-pattern segment
fitting. The ``jackknife`` step now removes that bias upstream; what remains
visible here is real residual structure worth a reviewer's attention.)

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
``CFP_PREV`` with the render-format marker ``snr`` (not a bare timestamp).
The skip check requires that marker, so a canonical file carrying an *old*
``CFP_PREV`` value (an ISO timestamp from the pre-SNR raw-SCI renderer) is
treated as stale and re-rendered on the next ``process`` run — no
``--overwrite`` needed. Bumping this marker is how a future preview-format
change forces regeneration.
"""

import os

import numpy as np

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.steps._plots import _block_reduce, _zscale_limits

# Render-format marker stamped into ``CFP_PREV``. The skip check requires it, so
# a canonical file whose ``CFP_PREV`` predates the SNR renderer (a bare ISO
# timestamp) is regenerated rather than served stale. Bump this on any future
# preview-format change to force a one-time regeneration.
_PREVIEW_KIND = 'snr'


def preview_step(exposure_file, field, step_config, overwrite=False,
                 status=None):
    """Render thumbnail + native-res SNR preview PNGs for a single exposure."""
    rootname = os.path.basename(exposure_file).removesuffix('.fits')
    out_dir = os.path.dirname(exposure_file)
    thumb_path = os.path.join(out_dir, f'{rootname}_preview.png')
    full_path = os.path.join(out_dir, f'{rootname}_full.png')

    # Skip only when NOT overwriting, *both* PNGs exist (regenerate if either was
    # deleted out-of-band), and the recorded CFP_PREV marks the current render
    # format. A pre-SNR stamp (ISO timestamp) fails the marker test and falls
    # through to re-render, so upgrading and re-running `process` replaces stale
    # raw-SCI previews without `--overwrite`. The status cache only tracks key
    # *presence*, so the one value read happens solely on genuine re-runs (both
    # PNGs already present), not on fresh exposures.
    if not overwrite and os.path.exists(thumb_path) and os.path.exists(full_path):
        has_prev = (status.has(exposure_file, 'CFP_PREV') if status is not None
                    else cfp.has_step(exposure_file, 'CFP_PREV'))
        if has_prev and str(
                cfp.step_value(exposure_file, 'CFP_PREV')).startswith(
                    _PREVIEW_KIND):
            log(f"Skipping preview on {rootname}: CFP_PREV ({_PREVIEW_KIND}) "
                f"already set")
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
            header_updates=cfp.format(CFP_PREV=_PREVIEW_KIND),
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
