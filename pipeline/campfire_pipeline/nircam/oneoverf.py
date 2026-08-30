"""
oneoverf: shared, pure numerics for the unified ``bkg`` step (and the opt-in
``diag_striping`` step).

Factored out of the retired ``steps/striping.py`` / ``steps/sky.py`` /
``steps/variance.py`` so the per-exposure background step and diag_striping
share one implementation with no datamodel / file-I/O coupling:

- ``peramp_pedestal``      — per-amp DC pedestal (owns the per-exposure DC; see
                             ``docs/design-nircam-unified-background.md`` §4.5).
- ``column_pattern``       — per-column (vertical) 1/f term.
- ``fit_residual_striping``/``_median_amprow_offsets`` — the median amp-row +
                             vertical striping estimator (moved verbatim; the
                             ``estimator='gp'`` arm dispatches to
                             ``gp_striping.gp_amprow_offsets``).
- ``variance_rescale``     — VAR_RNOISE rescale factor.

The column/pedestal primitives (``collapse_image``, ``measure_fullimage_striping``)
still live in ``skyfit.py`` and are imported here.
"""

import warnings

import numpy as np
from astropy.nddata import block_reduce
from astropy.stats import (
    biweight_location,
    biweight_midvariance,
    sigma_clipped_stats,
)

from campfire_pipeline.nircam.constants import NIR_AMPS
from campfire_pipeline.nircam.skyfit import (
    collapse_image,
    measure_fullimage_striping,
)


# ---------------------------------------------------------------------------
# Per-amp DC pedestal — owns the per-exposure DC (skymatch), §4.5
# ---------------------------------------------------------------------------

def peramp_pedestal(data, mask, sigma=3.0, maxiters=3):
    """Per-amp DC pedestal = σ-clipped median of unmasked pixels per amplifier.

    Generalizes the legacy single global ``sky`` pedestal (``fit_sky_tot`` over
    the whole frame) to one DC per amp, so the per-amp DC *steps* between the
    four amplifiers are removed. Because it removes each amp's background level,
    it (and only it) carries the per-exposure DC that the — absent — skymatch
    would otherwise own (design §4.5). Run it *before* the amp-row GPs so those
    fit a ~zero-per-amp-mean residual.

    Returns ``(ped, per_amp)`` where ``ped`` broadcasts each amp's DC across its
    science columns (reference columns stay 0), and ``per_amp`` is the dict of
    the four values (for provenance / diagnostics).
    """
    ped = np.zeros(data.shape)
    per_amp = {}
    for amp in ('A', 'B', 'C', 'D'):
        _, _, colstart, colstop = NIR_AMPS[amp]['data']
        ampdata = data[:, colstart:colstop]
        ampmask = mask[:, colstart:colstop]
        vals = ampdata[~ampmask & np.isfinite(ampdata)]
        if vals.size:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', category=RuntimeWarning)
                _, dc, _ = sigma_clipped_stats(
                    vals, sigma=sigma, maxiters=maxiters,
                )
            dc = float(dc) if np.isfinite(dc) else 0.0
        else:
            dc = 0.0
        ped[:, colstart:colstop] = dc
        per_amp[amp] = dc
    return ped, per_amp


def frame_pedestal(data, mask, sigma=3.0, maxiters=3):
    """Single full-frame DC pedestal (σ-clipped median of unmasked pixels).

    The ``scope='frame'`` alternative to :func:`peramp_pedestal`, used when
    the bkg step's applied 2-D background is on: a per-amp pedestal turns a
    smooth sky gradient into a per-amp sawtooth that the smooth Background2D
    mesh cannot reproduce, ringing at the amp boundaries. With one frame DC
    the 2-D fit owns the gradient; any real per-amp DC steps are left to the
    per-amp amp-row GP (which can carry a constant per amp).

    Same return contract as :func:`peramp_pedestal`: ``(ped, per_amp)`` with
    the single DC broadcast across every amp's science columns (reference
    columns stay 0) and repeated per amp in the dict.
    """
    ped = np.zeros(data.shape)
    vals = data[~mask & np.isfinite(data)]
    if vals.size:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', category=RuntimeWarning)
            _, dc, _ = sigma_clipped_stats(vals, sigma=sigma,
                                           maxiters=maxiters)
        dc = float(dc) if np.isfinite(dc) else 0.0
    else:
        dc = 0.0
    per_amp = {}
    for amp in ('A', 'B', 'C', 'D'):
        _, _, colstart, colstop = NIR_AMPS[amp]['data']
        ped[:, colstart:colstop] = dc
        per_amp[amp] = dc
    return ped, per_amp


# ---------------------------------------------------------------------------
# Per-column (vertical) 1/f term
# ---------------------------------------------------------------------------

def column_pattern(data, mask, maxiters):
    """Per-column striping pattern (1-D x-collapse broadcast to 2-D).

    Identical to the vertical term inside :func:`fit_residual_striping`, exposed
    standalone so the ``bkg`` loop can run it as its own chain stage.
    """
    vertical_1d = collapse_image(data, mask, maxiters, dimension='x')
    return np.broadcast_to(vertical_1d, data.shape).copy()


# ---------------------------------------------------------------------------
# Median amp-row + vertical striping estimator (moved verbatim from striping.py)
# ---------------------------------------------------------------------------

def _median_amprow_offsets(data, mask, maxiters, asymmetry_threshold,
                           nmask_prefilter):
    """Per-amp per-row offset via 2σ-clipped median + full-row fallback.

    The production estimator. Returns ``(horizontal, ampcounts)`` where
    ``ampcounts`` reports the number of rows per amp that fell back to the
    full-image row median. See ``fit_residual_striping`` for the parameter
    semantics.
    """
    full_horizontal, _ = measure_fullimage_striping(data, mask, maxiters)

    horizontal = np.zeros(data.shape)
    ampcounts = []
    for amp in ('A', 'B', 'C', 'D'):
        _, _, colstart, colstop = NIR_AMPS[amp]['data']
        ampdata = data[:, colstart:colstop]
        ampmask = mask[:, colstart:colstop]
        # Replaces a separate `collapse_image(...)` call: returns the same
        # per-row median in `h_amp`, plus the mean and std needed for the
        # asymmetry test below — all from one sigma-clip pass.
        # Fully-masked rows produce all-NaN slices; the asymmetry guard
        # below treats them as contaminated, so the inner RuntimeWarnings
        # are noise.
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
            mean_amp, h_amp, std_amp = sigma_clipped_stats(
                ampdata, mask=ampmask, sigma=2.0,
                cenfunc=np.nanmedian, stdfunc=np.nanstd,
                axis=1, maxiters=maxiters,
            )
        with np.errstate(invalid='ignore', divide='ignore'):
            asymmetry = np.abs(mean_amp - h_amp) / std_amp
        # Non-finite (e.g. all-masked rows where std is 0/nan) → treat as
        # contaminated so the fallback path is taken.
        asymmetry = np.where(np.isfinite(asymmetry), asymmetry, np.inf)

        nmask = np.sum(ampmask, axis=1)
        ampcount = 0
        for i in range(ampmask.shape[0]):
            # Per-amp median in clean rows (where the source mask says
            # nothing's there); asymmetry test only in rows where the
            # source mask is meaningful AND there are still enough
            # unmasked pixels to estimate from.
            nmask_frac_i = nmask[i] / ampmask.shape[1]
            if nmask_frac_i > 0.95:
                # Too few unmasked pixels for any reliable per-amp
                # estimate — fall back regardless of asymmetry.
                horizontal[i, colstart:colstop] = full_horizontal[i]
                ampcount += 1
            elif (nmask_frac_i > nmask_prefilter
                  and asymmetry[i] > asymmetry_threshold):
                # Source mask is meaningful here AND the post-clip
                # distribution is still asymmetric: contamination biased
                # the per-amp median.
                horizontal[i, colstart:colstop] = full_horizontal[i]
                ampcount += 1
            else:
                horizontal[i, colstart:colstop] = h_amp[i]
        ampcounts.append(f'{amp}-{ampcount}')
    return horizontal, ampcounts


def fit_residual_striping(
    data,
    mask,
    maxiters,
    asymmetry_threshold=0.1,
    nmask_prefilter=0.20,
    estimator='median',
    gp_params=None,
    amplitude_data=None,
):
    """Fit per-amp per-row horizontal + per-column vertical 1/f striping.

    Pure function. Operates on a flat-fielded (and ideally pedestal-/
    background-subtracted) frame. Returns 2D additive correction arrays
    that should be subtracted from the un-flat-fielded SCI.

    The per-amp-row **horizontal** estimate is dispatched on ``estimator``;
    the per-column **vertical** estimate (measured on ``data - horizontal``)
    is identical for both and is intentionally left untouched.

    Parameters
    ----------
    data : (H, W) ndarray
        Input frame (flat-fielded, pedestal/background pre-subtracted).
    mask : (H, W) bool ndarray
        True where pixels are masked (DQ flagged or source).
    maxiters : int
        Sigma-clipping iterations.
    asymmetry_threshold, nmask_prefilter : float
        ``estimator="median"`` only. Asymmetry test threshold and the
        mask-fraction below which it is skipped (see
        ``_median_amprow_offsets``). Clean / successfully clipped rows give
        asymmetry ≈ 0; heavy bright-source contamination gives ≳ 0.3. The
        prefilter avoids false positives from the asymmetry statistic's own
        sample-noise floor (~0.07 for N ≈ 508) in clean rows.
    estimator : {'median', 'gp'}
        ``'median'`` (default): the production 2σ-clipped median with
        full-row fallback. ``'gp'``: a 1-D Gaussian-Process smoother fit
        along the slow axis per amplifier (see
        ``campfire_pipeline.nircam.gp_striping``), which interpolates the
        offset across masked source rows *within the same amplifier* rather
        than substituting the full-row median.
    gp_params : dict, optional
        Required when ``estimator='gp'``. Only ``rho`` (frozen SHOTerm
        length scale, in rows) is required; ``kernel_sigma`` is optional and
        **self-adapts per exposure** when absent (see ``gp_amprow_offsets``),
        with an optional frozen O(1) ``kernel_sigma_factor`` (default 1.0).
        Plus optional ``q`` (default ``1/sqrt(2)``), ``sigma_clip``
        (default 2.0), and ``weak_frac`` (default 0.5).
    amplitude_data : (H, W) ndarray, optional
        ``estimator='gp'`` only. The pre-2D-bg-detrend frame on which to
        measure the self-adapting kernel amplitude (see ``gp_amprow_offsets``).
        ``None`` → measure on ``data``.

    Returns
    -------
    horizontal : (H, W) ndarray
        Per-amp, per-row striping pattern.
    vertical : (H, W) ndarray
        Per-column striping pattern (1D x-collapse broadcast to 2D).
    ampcounts : list[str]
        Per-amp diagnostic strings. For ``'median'``: ``'A-N'`` ... where
        ``N`` is the number of rows that fell back to the full-image
        median. For ``'gp'``: ``'A:anchors/weak/maxσ'`` per amp.
    diag : dict
        Extra diagnostics. For ``'gp'``: ``{'kernel_sigma_eff': float}`` (the
        self-adapted or overridden amplitude actually used). Empty otherwise.
    """
    diag = {}
    if estimator == 'gp':
        from campfire_pipeline.nircam.gp_striping import gp_amprow_offsets
        params = gp_params or {}
        if 'rho' not in params:
            raise ValueError("estimator='gp' requires gp_params with 'rho'")
        horizontal, ampcounts, kernel_sigma_eff = gp_amprow_offsets(
            data, mask,
            rho=params['rho'],
            kernel_sigma=params.get('kernel_sigma'),  # None → self-adapt
            kernel_sigma_factor=params.get('kernel_sigma_factor', 1.0),
            amplitude_data=amplitude_data,  # pre-2D-bg frame for the amplitude
            q=params.get('q', 1.0 / np.sqrt(2.0)),
            sigma_clip_sigma=params.get('sigma_clip', 2.0),
            maxiters=maxiters,
            weak_frac=params.get('weak_frac', 0.5),
        )
        diag['kernel_sigma_eff'] = kernel_sigma_eff
    elif estimator == 'median':
        horizontal, ampcounts = _median_amprow_offsets(
            data, mask, maxiters, asymmetry_threshold, nmask_prefilter)
    else:
        raise ValueError(
            f"unknown estimator {estimator!r} (expected 'median' or 'gp')")

    vertical_1d = collapse_image(
        data - horizontal, mask, maxiters, dimension='x',
    )
    vertical = np.broadcast_to(vertical_1d, data.shape).copy()

    return horizontal, vertical, ampcounts, diag


# ---------------------------------------------------------------------------
# VAR_RNOISE rescale (moved from steps/variance.py:72-88)
# ---------------------------------------------------------------------------

def variance_rescale(sci, var_rnoise, mask, block_size):
    """Return the factor that rescales ``VAR_RNOISE`` to the measured sky variance.

    Block-reduces SCI and VAR_RNOISE, measures the sky variance
    (``biweight_midvariance``) and the mean VAR_RNOISE (``biweight_location``)
    over unmasked bins, and returns ``skyvar / masked_mean_var_rnoise``. The
    caller multiplies ``VAR_RNOISE`` by this factor. Identical math to the
    retired ``variance_step``.
    """
    block_mask = block_reduce(mask, block_size)
    block_mask_bool = block_mask != 0

    block_sci = block_reduce(sci, block_size)
    unmasked_bins = block_sci[block_mask_bool == 0]
    variance = biweight_midvariance(unmasked_bins)
    skyvar = variance / block_size ** 2

    block_var_rnoise = block_reduce(var_rnoise, block_size)
    unmasked_bins = block_var_rnoise[block_mask_bool == 0]
    masked_mean_var_rnoise = (
        biweight_location(unmasked_bins) / block_size ** 2
    )

    return float(skyvar / masked_mean_var_rnoise)
