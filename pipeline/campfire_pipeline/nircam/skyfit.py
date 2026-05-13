"""
Sky / pedestal / 1-f striping fit helpers used by NIRCam steps.

Pure-numerical helpers extracted from the legacy ``stage1.py`` /
``stage2.py`` modules so the canonical-exposure step modules
(``steps/striping.py``, ``steps/sky.py``) don't depend on legacy code.

The functions are intentionally simple wrappers around scipy / astropy /
photutils calls — no JWST datamodels, no I/O — so they're cheap to test
and reuse.
"""

import warnings

import numpy as np
from astropy.stats import SigmaClip, sigma_clipped_stats
from photutils.background import (
    Background2D,
    BiweightLocationBackground,
    BkgZoomInterpolator,
)
from scipy.optimize import curve_fit

from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.constants import NIR_AMPS


def _gaussian(x, a, mu, sig):
    return a * np.exp(-(x - mu) ** 2 / (2 * sig ** 2))


def fit_pedestal(data):
    """Fit the sky-flux distribution with a Gaussian and return its mean.

    Used by the 1/f striping step to subtract a pedestal before measuring
    the striping pattern. Histogram bins are fixed at 1e-3 from -1 to 1.5
    (matching the rate-stage flux scale of NIRCam exposures).
    """
    bins = np.arange(-1, 1.5, 0.001)
    h, b = np.histogram(data, bins=bins)
    bc = 0.5 * (b[1:] + b[:-1])
    p0 = [10, bc[np.argmax(h)], 0.01]
    popt, _ = curve_fit(_gaussian, bc, h, p0=p0)
    return popt[1]


def fit_sky_tot(data, return_diagnostics=False):
    """Fit the sky-flux distribution with a Gaussian; return its mean.

    Used by the sky-subtraction step (cal-stage data, where the flux
    scale is set by the photom step rather than rate-stage units, so the
    histogram range is scaled from sigma_clipped_stats rather than
    hard-coded).

    Parameters
    ----------
    data : 1D array
    return_diagnostics : bool
        If True, return ``(mean, popt)`` instead of just ``mean``.
        ``popt`` is the full ``(a, mu, sigma)`` Gaussian fit, suitable
        for overlaying on a histogram in a diagnostic plot.
    """
    _, median, std = sigma_clipped_stats(data)
    bins = np.linspace(median - 10 * std, median + 10 * std, 1000)
    h, b = np.histogram(data, bins=bins)
    h = h / np.max(h)
    bc = 0.5 * (b[1:] + b[:-1])
    p0 = [1, bc[np.argmax(h)], std]
    popt, _ = curve_fit(_gaussian, bc, h, p0=p0)
    if return_diagnostics:
        return popt[1], popt
    return popt[1]


def fit_sky(data, use_bottleneck=True):
    """Measure a 2D background on unmasked pixels via ``photutils.Background2D``.

    Used by the 1/f striping step for chips with a large, low-surface-
    brightness residual after wisp subtraction. The caller passes data
    with masked pixels already set to zero; the function masks zero
    pixels before fitting.

    Falls back through ``exclude_percentile`` 90 → 95 → 97.5 if
    ``Background2D`` raises (it raises when too many pixels are masked
    in any single box).
    """
    skystd = np.nanstd(data)
    data[data > (2 * skystd)] = 0
    mask = data == 0
    if use_bottleneck:
        # bottleneck wants native byte order
        data.byteswap(inplace=True)
        data = data.view(data.dtype.newbyteorder('='))

    for exclude_percentile in (90, 95, 97.5):
        try:
            bkg = Background2D(
                data, box_size=128,
                sigma_clip=SigmaClip(sigma=3),
                filter_size=5,
                bkg_estimator=BiweightLocationBackground(),
                exclude_percentile=exclude_percentile,
                mask=mask,
                interpolator=BkgZoomInterpolator(),
            )
            return bkg.background
        except Exception:
            continue
    # Final attempt: let the exception propagate
    bkg = Background2D(
        data, box_size=128,
        sigma_clip=SigmaClip(sigma=3),
        filter_size=5,
        bkg_estimator=BiweightLocationBackground(),
        exclude_percentile=97.5,
        mask=mask,
        interpolator=BkgZoomInterpolator(),
    )
    return bkg.background


def collapse_image(im, mask, maxiters, dimension='y', sig=2.0):
    """Collapse an image along one axis to a 1-D striping profile.

    ``dimension='y'`` returns the per-row sigma-clipped median (collapsing
    columns → horizontal striping); ``'x'`` returns the per-column median
    (vertical striping).
    """
    if dimension == 'y':
        axis = 1
    elif dimension == 'x':
        axis = 0
    else:
        raise ValueError(f"dimension must be 'y' or 'x' (got {dimension!r})")
    # Fully-masked rows/columns produce empty / all-NaN slices inside the
    # sigma-clipper's nanmean/nanmedian/nanstd; the caller treats these
    # rows as contaminated and falls back to the full-image profile, so
    # the inner RuntimeWarnings are noise.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            'ignore', category=RuntimeWarning,
            message='Mean of empty slice',
        )
        warnings.filterwarnings(
            'ignore', category=RuntimeWarning,
            message='All-NaN slice encountered',
        )
        warnings.filterwarnings(
            'ignore', category=RuntimeWarning,
            message='Degrees of freedom <= 0',
        )
        res = sigma_clipped_stats(
            im, mask=mask, sigma=sig,
            cenfunc=np.nanmedian, stdfunc=np.nanstd,
            axis=axis, maxiters=maxiters,
        )
    return res[1]


def measure_fullimage_striping(fitdata, mask, maxiters):
    """Measure horizontal + vertical striping over the full image.

    Returns ``(horizontal, vertical)`` 1-D arrays. Used as the fallback
    for amp-rows that are mostly masked.
    """
    horizontal = collapse_image(fitdata, mask, maxiters, dimension='y')
    temp = fitdata.T - horizontal
    temp = temp.T
    vertical = collapse_image(temp, mask, maxiters, dimension='x')
    return horizontal, vertical
