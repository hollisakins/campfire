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
