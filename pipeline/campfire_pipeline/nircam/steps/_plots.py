"""
Shared diagnostic plot helpers for per-step modules.

Independent of the legacy ``stage1.py`` / ``stage2.py`` so the new step
modules don't pull legacy stage code at import time. The legacy plot helpers
in those files keep their own copies for now and disappear with the cleanup
commit at the end of the restructure.
"""

import numpy as np

from astropy.io import fits
from astropy.visualization import ImageNormalize, ZScaleInterval


def plot_two(image1, image2, group=0, title1=None, title2=None,
             save_file=None, scaling=None):
    """Render two images side-by-side for visual comparison.

    Parameters
    ----------
    image1, image2 : str or array-like
        Either FITS paths (read from the ``SCI`` extension) or raw 2D arrays.
        4D inputs are sliced as ``[0, group, :, :]``.
    group : int
        Group index for 4D uncal data.
    title1, title2 : str, optional
    save_file : str, optional
        If given, the figure is written here and the matplotlib figure is
        closed.
    scaling : int or None
        ``None`` for independent ZScale per panel, ``1`` to share image1's
        ZScale, ``2`` to share image2's.
    """
    import matplotlib.pyplot as plt

    if isinstance(image1, str) or isinstance(image2, str):
        im1 = fits.getdata(image1, 'SCI')
        im2 = fits.getdata(image2, 'SCI')
    else:
        im1, im2 = image1, image2

    if im1.ndim == 4:
        im1 = im1[0, group, :, :]
    if im2.ndim == 4:
        im2 = im2[0, group, :, :]

    if scaling is None:
        norm1 = ImageNormalize(im1, interval=ZScaleInterval())
        norm2 = ImageNormalize(im2, interval=ZScaleInterval())
    elif scaling == 1:
        norm1 = ImageNormalize(im1, interval=ZScaleInterval())
        norm2 = norm1
    elif scaling == 2:
        norm2 = ImageNormalize(im2, interval=ZScaleInterval())
        norm1 = norm2
    else:
        raise ValueError(f"scaling must be None, 1, or 2 (got {scaling})")

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(8, 4), tight_layout=True)
    ax1.imshow(im1, origin='lower', interpolation='none', cmap='Greys', norm=norm1)
    ax2.imshow(im2, origin='lower', interpolation='none', cmap='Greys', norm=norm2)
    ax1.axis('off')
    ax2.axis('off')
    if title1:
        ax1.set_title(title1)
    if title2:
        ax2.set_title(title2)
    if save_file is not None:
        fig.savefig(save_file)
    plt.close(fig)


def plot_sky(sci_before, sci_after, hist_data, popt, pedestal,
             save_file=None, title=None):
    """Sky-step diagnostic: pedestal histogram + before/after stamps.

    Parameters
    ----------
    sci_before, sci_after : 2D array
        SCI before / after pedestal subtraction.
    hist_data : 1D array
        Sigma-clipped, masked sky-pixel sample that was passed to the
        Gaussian fit. The histogram is rebuilt with the same bin range
        ``fit_sky_tot`` uses internally so the overlaid Gaussian aligns
        without re-fitting.
    popt : (a, mu, sigma)
        Gaussian fit parameters from ``fit_sky_tot``. The histogram in
        the helper is normalized so its peak equals 1 — same as the fit
        — so ``popt`` plots directly on top.
    pedestal : float
        Subtracted pedestal value (== ``popt[1]``); drawn as a vertical
        dashed line.
    """
    import matplotlib.pyplot as plt
    from astropy.stats import sigma_clipped_stats

    fig = plt.figure(figsize=(10, 7), tight_layout=True)
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.4])
    ax_hist = fig.add_subplot(gs[0, :])
    ax_b = fig.add_subplot(gs[1, 0])
    ax_a = fig.add_subplot(gs[1, 1])

    _, med, std = sigma_clipped_stats(hist_data)
    bins = np.linspace(med - 10 * std, med + 10 * std, 200)
    h, b = np.histogram(hist_data, bins=bins)
    h = h / max(h.max(), 1)
    bc = 0.5 * (b[1:] + b[:-1])
    ax_hist.bar(bc, h, width=(b[1] - b[0]),
                color='C0', alpha=0.5, edgecolor='none')
    a, mu, sigma = popt
    xx = np.linspace(bc.min(), bc.max(), 500)
    yy = a * np.exp(-(xx - mu) ** 2 / (2 * sigma ** 2))
    ax_hist.plot(xx, yy, 'r-', lw=1.5,
                 label=f'Gaussian (μ={mu:.3e}, σ={sigma:.3e})')
    ax_hist.axvline(pedestal, color='k', ls='--', lw=1,
                    label=f'pedestal = {pedestal:.3e}')
    ax_hist.set_xlabel('SCI')
    ax_hist.set_ylabel('count (peak-normalized)')
    ax_hist.legend(loc='best', fontsize=9)
    if title:
        ax_hist.set_title(title)

    norm = ImageNormalize(sci_before, interval=ZScaleInterval())
    ax_b.imshow(sci_before, origin='lower', interpolation='none',
                cmap='Greys', norm=norm)
    ax_a.imshow(sci_after, origin='lower', interpolation='none',
                cmap='Greys', norm=norm)
    ax_b.set_title('SCI before sky')
    ax_a.set_title('SCI after sky')
    for ax in (ax_b, ax_a):
        ax.axis('off')

    if save_file is not None:
        fig.savefig(save_file)
    plt.close(fig)


def plot_outlier(sci, new_outlier, save_file=None, title=None):
    """Outlier-step diagnostic: SCI plus a side-panel with newly flagged pixels.

    Parameters
    ----------
    sci : 2D array
        SCI snapshot taken before outlier flagging (the array itself is
        unchanged by the step; only DQ is updated).
    new_outlier : 2D bool array
        ``(DQ_after & OUTLIER) & ~(DQ_before & OUTLIER)`` — pixels this
        run flagged that weren't already marked as outliers.
    """
    import matplotlib.pyplot as plt

    norm = ImageNormalize(sci, interval=ZScaleInterval())
    n_new = int(new_outlier.sum())

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 5), tight_layout=True)
    ax1.imshow(sci, origin='lower', interpolation='none',
               cmap='Greys', norm=norm)
    ax1.set_title(title or 'SCI')
    ax1.axis('off')

    ax2.imshow(sci, origin='lower', interpolation='none',
               cmap='Greys', norm=norm)
    overlay = np.ma.masked_where(~new_outlier,
                                 np.ones_like(sci, dtype=float))
    ax2.imshow(overlay, origin='lower', interpolation='none',
               cmap='autumn', vmin=0, vmax=1, alpha=0.9)
    ax2.set_title(f'New outliers ({n_new:,})')
    ax2.axis('off')

    if save_file is not None:
        fig.savefig(save_file)
    plt.close(fig)


def _zscale_limits(arr):
    """ZScale (vmin, vmax) on the finite subset of ``arr``."""
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0, 1.0
    return ZScaleInterval().get_limits(finite)


def _block_reduce(arr, block_size):
    """Block-mean downsample, NaN-aware. ``block_size=1`` returns ``arr``."""
    if block_size <= 1:
        return arr
    from astropy.nddata import block_reduce
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Mean of empty slice')
        warnings.filterwarnings('ignore', message='All-NaN slice encountered')
        return block_reduce(arr, block_size=block_size, func=np.nanmean)


def plot_mosaic_thumbnail(sci, save_file, downsample=4, cmap='Greys'):
    """Save a downsampled PNG of a mosaic SCI with no axes/borders.

    Uses ``plt.imsave`` so the output PNG has exactly the downsampled
    array's pixel dimensions — small files at native resolution, no
    matplotlib decoration to scale.
    """
    import matplotlib.pyplot as plt

    thumb = _block_reduce(sci, downsample)
    vmin, vmax = _zscale_limits(thumb)
    plt.imsave(save_file, thumb, cmap=cmap, vmin=vmin, vmax=vmax,
               origin='lower')


def plot_mosaic_bkgsub(before, after, model, save_file, downsample=4,
                       title=None):
    """Three-panel before/after/model PNG for mosaic background subtraction.

    Before and after share a ZScale (taken from ``before``) so the
    pedestal change is visible directly. The model panel uses an
    independent diverging colormap centred on the model's own ZScale —
    that's the diagnostic for over-subtraction of extended sources.
    """
    import matplotlib.pyplot as plt

    before_d = _block_reduce(before, downsample)
    after_d = _block_reduce(after, downsample)
    model_d = _block_reduce(model, downsample)

    sci_vmin, sci_vmax = _zscale_limits(before_d)
    mvmin, mvmax = _zscale_limits(model_d)
    # Symmetric around 0 for the diverging colormap so positive/negative
    # background residuals read the same way.
    mlim = max(abs(mvmin), abs(mvmax))

    h, w = before_d.shape
    aspect = w / max(h, 1)
    fig_h = 5.0
    fig_w = max(3 * fig_h * aspect, 9.0)
    fig, axes = plt.subplots(1, 3, figsize=(fig_w, fig_h), tight_layout=True)

    axes[0].imshow(before_d, origin='lower', interpolation='none',
                   cmap='Greys', vmin=sci_vmin, vmax=sci_vmax)
    axes[1].imshow(after_d, origin='lower', interpolation='none',
                   cmap='Greys', vmin=sci_vmin, vmax=sci_vmax)
    axes[2].imshow(model_d, origin='lower', interpolation='none',
                   cmap='RdBu_r', vmin=-mlim, vmax=+mlim)
    axes[0].set_title('Before bkgsub')
    axes[1].set_title('After bkgsub')
    axes[2].set_title('Background model (before − after)')
    for ax in axes:
        ax.axis('off')
    if title:
        fig.suptitle(title, fontsize=10)
    fig.savefig(save_file, dpi=120, bbox_inches='tight')
    plt.close(fig)
