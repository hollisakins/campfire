"""
GP-based per-amp-row 1/f ("striping") offset estimator for NIRCam.

Alternative to the median + full-row-fallback estimator in
``steps/striping.fit_residual_striping``. Selected via
``[nircam.striping].estimator = "gp"``.

Motivation
----------
The production estimator fits one additive offset per *amp-row* (the
512-pixel segment of a row belonging to one of the four readout
amplifiers) as a 2σ-clipped median of its unmasked background pixels.
When a bright/extended source fills most of an amp-row, the per-amp
median is biased, an asymmetry guard trips, and the code falls back to
the **full-row** median — the median across all four amps in that row.
But the four amps carry physically distinct offsets (~3–5 % in SW), so
that fallback substitutes the wrong quantity exactly where a good local
estimate is hardest. The result is a step at the amplifier boundary and,
because the fallback only fires on the rows the source covers, a step in
the slow direction at the top/bottom of the source — the "box" of
amp-row artifacts around bright sources.

Idea
----
1/f noise is low-frequency and rows are clocked out sequentially, so for
a fixed amplifier the offset ``n[a, r]`` is a *smooth function of the
slow-axis row index r*. Model it per amplifier as a 1-D GP::

    y_r = f(r) + eps_r,    eps_r ~ N(0, sigma_r^2)

where ``y_r`` is the (sigma-clipped) amp-row median and ``sigma_r`` is
its *sampling* uncertainty (``1.25 * s_hat / sqrt(N_r)``, ``s_hat`` from
the MAD). The diagonal ``sigma_r^2`` carries each row's uncorrelated
estimation error; the kernel carries the correlated structure to keep. A
heavily-masked row gets a large ``sigma_r`` and is automatically
down-weighted — no hard threshold, no fallback. The GP posterior mean
interpolates over masked gaps using neighbouring rows *of the same
amplifier*; the kernel length scale sets how far that reaches. Each
amplifier carries its own DC mean term, so amp-to-amp steps are never
reintroduced.

Implementation uses **celerite2** on its default CPU backend
(semiseparable kernels → exact O(n) solve in 1-D; no GPU, no per-call
JIT warmup). Hyperparameters are *frozen* (calibrated offline; see
``scripts/calibrate_gp_striping.py``) — fitting them per exposure would
turn a deterministic linear solve into an optimization loop and invite
overfitting.
"""

import warnings

import numpy as np
from astropy.stats import mad_std, sigma_clip

from campfire_pipeline.nircam.constants import NIR_AMPS

# 1.25 ≈ sqrt(pi/2): asymptotic ratio of the standard error of the median
# to that of the mean for a Gaussian sample, so sigma_r ≈ 1.25 s_hat/sqrt(N).
_MEDIAN_SE_FACTOR = 1.2533

# Reference-pixel border (4 px on every side); science rows are the rest.
_REF_BORDER = 4


def _amprow_statistics(ampdata, ampmask, sigma, maxiters):
    """Per-row robust statistics of an amplifier's background pixels.

    Parameters
    ----------
    ampdata : (R, C) ndarray
        One amplifier's science columns (reference columns already
        excluded by the caller via ``NIR_AMPS``), all rows.
    ampmask : (R, C) bool ndarray
        True where a pixel is masked (DQ or source).
    sigma, maxiters : float, int
        Per-row sigma-clip parameters.

    Returns
    -------
    y_r : (R,) ndarray
        Sigma-clipped median of the surviving pixels in each amp-row
        (NaN where no pixel survives).
    s_hat : (R,) ndarray
        Robust per-pixel scatter (``mad_std``) of the surviving sample.
    n_r : (R,) int ndarray
        Number of surviving pixels per amp-row.
    """
    w = ampdata.astype(np.float64, copy=True)
    w[ampmask | ~np.isfinite(w)] = np.nan

    with warnings.catch_warnings():
        # Empty / all-NaN amp-rows (fully masked) flow through as NaN; the
        # caller drops them via ``n_r == 0``, so the inner RuntimeWarnings
        # are noise.
        warnings.filterwarnings('ignore', category=RuntimeWarning,
                                message='All-NaN slice encountered')
        warnings.filterwarnings('ignore', category=RuntimeWarning,
                                message='Mean of empty slice')
        warnings.filterwarnings('ignore', category=RuntimeWarning,
                                message='Degrees of freedom <= 0')
        warnings.filterwarnings('ignore', category=RuntimeWarning,
                                message='invalid value encountered')
        # sigma_clip emits an AstropyUserWarning when the input has NaNs
        # (our masked / fully-masked amp-rows); expected, suppress it.
        warnings.filterwarnings(
            'ignore', message='Input data contains invalid values')
        clipped = sigma_clip(
            w, sigma=sigma, maxiters=maxiters, axis=1,
            cenfunc='median', stdfunc='mad_std', masked=True,
        )
        wc = np.ma.filled(clipped, np.nan)
        y_r = np.nanmedian(wc, axis=1)
        s_hat = mad_std(wc, axis=1, ignore_nan=True)
        n_r = np.sum(np.isfinite(wc), axis=1)
    return y_r, s_hat, n_r.astype(np.int64)


def _gp_predict_amp(rows, y_r, yerr, dc_level, kernel_sigma, rho, q,
                    rows_predict):
    """Fit a 1-D SHOTerm GP on anchor rows; predict at ``rows_predict``.

    Returns ``(mu, var)`` over ``rows_predict``. ``celerite2`` imported
    lazily so the median estimator path carries no GP dependency.
    """
    import celerite2
    from celerite2 import terms

    kernel = terms.SHOTerm(sigma=float(kernel_sigma), rho=float(rho),
                           Q=float(q))
    gp = celerite2.GaussianProcess(kernel, mean=float(dc_level))
    gp.compute(rows.astype(np.float64), yerr=yerr.astype(np.float64))
    mu, var = gp.predict(
        y_r.astype(np.float64),
        t=rows_predict.astype(np.float64),
        return_var=True,
    )
    return mu, var


def gp_amprow_offsets(data, mask, rho, kernel_sigma=None,
                      kernel_sigma_factor=1.0, amplitude_data=None,
                      q=1.0 / np.sqrt(2.0),
                      sigma_clip_sigma=2.0, maxiters=3,
                      ref_border=_REF_BORDER, weak_frac=0.5):
    """Per-amp, per-row 1/f offset via 1-D GP smoothing along the slow axis.

    Drop-in replacement for the per-amp-row median + full-row fallback in
    ``fit_residual_striping``. Returns the **horizontal** correction only;
    the caller still measures the per-column (vertical) pattern on
    ``data - horizontal`` exactly as before.

    Geometry matches the existing code: amplifiers are the column strips of
    ``NIR_AMPS`` (reference columns already excluded), and the offset is one
    value per row (axis 0 = the slow read axis) broadcast across the amp's
    columns. Reference-border rows (the first/last ``ref_border``) are
    excluded from the fit but still receive a (GP-extrapolated) offset so
    the output array is full-frame; those weakly-constrained edge rows are
    flagged in the diagnostics.

    Hyperparameters
    ---------------
    Only ``rho`` (length scale, in rows) is a frozen hyperparameter — it is a
    detector readout/clocking property, independent of filter and of flux
    units. The kernel amplitude ``kernel_sigma`` is **self-adapting** by
    default: it is set per-exposure to the marginal ``mad_std`` of the
    per-amp-centered clean row medians times a frozen O(1)
    ``kernel_sigma_factor``. That is a deterministic robust statistic computed
    once per exposure — *not* an optimization, so it adds no per-exposure
    fitting — and it removes the cal-stage flux-unit (filter / detector /
    photometric-calibration) dependence that an absolute frozen amplitude
    would carry. Pass an explicit ``kernel_sigma`` to override (escape hatch
    / calibration reproduction). The amplitude is floored to the typical
    per-row sampling error so the solve stays conditioned.

    When the caller pre-subtracts a 2D background from ``data`` (the striping
    step does, to keep large-scale structure out of the per-amp-row fit), pass
    the **pre-subtraction** frame as ``amplitude_data`` so the amplitude is
    measured there. The prior amplitude must reflect how far the offset can
    vary *across a wide source gap* — i.e. the full pre-detrend 1/f marginal,
    not the post-detrend residual. Measuring it on the post-detrend ``data``
    underestimates the amplitude, over-regularizes the gap interpolation, and
    loses to the plain per-row median (verified on rj0911 f444w). ``None`` →
    measure on ``data`` itself (correct when no 2D-bg detrend was applied).

    Parameters
    ----------
    data : (H, W) ndarray
        Frame to measure (flat-fielded, pedestal/background pre-subtracted).
    mask : (H, W) bool ndarray
        True where masked (DQ flagged or source).
    rho : float
        SHOTerm length scale in *rows* (frozen hyperparameter).
    kernel_sigma : float, optional
        SHOTerm amplitude (data units). ``None`` (default) → self-adapt from
        the exposure (see above).
    kernel_sigma_factor : float
        Frozen O(1) multiplier on the self-adapted amplitude. Ignored when
        ``kernel_sigma`` is given.
    amplitude_data : (H, W) ndarray, optional
        Frame to measure the self-adapted amplitude on (the pre-2D-bg-detrend
        data). ``None`` → use ``data``. Ignored when ``kernel_sigma`` is given.
    q : float
        SHOTerm quality factor. ``1/sqrt(2)`` → smooth, non-oscillatory.
    sigma_clip_sigma, maxiters : float, int
        Per-amp-row sigma-clip used to build ``y_r`` / ``s_hat`` / ``N_r``.
    ref_border : int
        Reference-pixel border width to exclude from the science fit.
    weak_frac : float
        A predicted row is counted "weakly constrained" when its posterior
        std exceeds ``weak_frac * kernel_sigma`` — i.e. the GP has reverted
        toward the per-amp DC because no anchor row lies within ~rho. This
        is reported, not hidden: it flags wide source-filled gaps.

    Returns
    -------
    horizontal : (H, W) ndarray
        Per-amp per-row offset broadcast across each amp's columns.
    diagnostics : list[str]
        One ``'A:anchors/weak/maxσ'`` string per amplifier for logging.
    kernel_sigma_eff : float
        The amplitude actually used (self-adapted or the override value).
    """
    n_rows = data.shape[0]
    rows_all = np.arange(n_rows)
    sci = slice(ref_border, n_rows - ref_border)
    rows_sci = rows_all[sci]

    horizontal = np.zeros(data.shape, dtype=np.float64)
    diagnostics = []

    # ---- Pass 1: per-amp robust row statistics --------------------------
    # Everything needed to (a) self-calibrate the kernel amplitude from this
    # exposure and (b) fit each amp's GP in pass 2.
    # Self-adapt the amplitude on the pre-detrend frame when supplied.
    self_adapt = kernel_sigma is None
    amp_ref = amplitude_data if (self_adapt and amplitude_data is not None) \
        else data

    amp_stats = []
    centered = []   # per-amp-centered clean row medians, pooled over amps
    yerr_pool = []  # per-row sampling errors, pooled (amplitude floor)
    for amp in ('A', 'B', 'C', 'D'):
        _, _, colstart, colstop = NIR_AMPS[amp]['data']
        y_r, s_hat, n_r = _amprow_statistics(
            data[sci, colstart:colstop], mask[sci, colstart:colstop],
            sigma_clip_sigma, maxiters)
        good = (n_r > 0) & np.isfinite(y_r) & np.isfinite(s_hat)
        amp_stats.append((amp, colstart, colstop, y_r, s_hat, n_r, good))
        if good.any():
            sp = np.where(s_hat[good] > 0, s_hat[good], np.nan)
            yerr_pool.append(_MEDIAN_SE_FACTOR * sp / np.sqrt(n_r[good]))
        if self_adapt:
            if amp_ref is data:
                if good.any():
                    centered.append(y_r[good] - np.median(y_r[good]))
            else:
                ar, _, an = _amprow_statistics(
                    amp_ref[sci, colstart:colstop],
                    mask[sci, colstart:colstop], sigma_clip_sigma, maxiters)
                ag = (an > 0) & np.isfinite(ar)
                if ag.any():
                    centered.append(ar[ag] - np.median(ar[ag]))

    # ---- Self-adapting kernel amplitude ---------------------------------
    if kernel_sigma is None:
        marg = (float(mad_std(np.concatenate(centered))) if centered else 0.0)
        marg *= float(kernel_sigma_factor)
        floor = (float(np.nanmedian(np.concatenate(yerr_pool)))
                 if yerr_pool else 1.0)
        kernel_sigma_eff = float(max(marg, floor))
    else:
        kernel_sigma_eff = float(kernel_sigma)

    # ---- Pass 2: per-amp GP fit -----------------------------------------
    for amp, colstart, colstop, y_r, s_hat, n_r, good in amp_stats:
        n_anchor = int(good.sum())

        if n_anchor < 2:
            # Not enough anchors to fit a GP: hold the amp at its DC level
            # (robust median of whatever measured), or 0 if nothing did.
            dc = float(np.nanmedian(y_r[good])) if n_anchor else 0.0
            horizontal[:, colstart:colstop] = dc
            diagnostics.append(f'{amp}:{n_anchor}/--/-- (DC-only)')
            continue

        # Per-pixel scatter can be exactly 0 on rows whose surviving pixels
        # are all identical (rare; e.g. heavy clipping). Floor it to the
        # amp's typical scatter so the GP weight stays finite.
        s_pos = s_hat[good]
        s_floor = np.median(s_pos[s_pos > 0]) if np.any(s_pos > 0) else 1.0
        s_use = np.where(s_pos > 0, s_pos, s_floor)
        yerr = _MEDIAN_SE_FACTOR * s_use / np.sqrt(n_r[good])

        # Per-amp DC mean term: robust median of the well-measured rows.
        dc = float(np.median(y_r[good]))

        mu, var = _gp_predict_amp(
            rows_sci[good], y_r[good], yerr, dc,
            kernel_sigma_eff, rho, q, rows_all,
        )
        horizontal[:, colstart:colstop] = mu[:, None]

        post_sigma = np.sqrt(np.clip(var, 0.0, None))
        n_weak = int(np.sum(post_sigma[sci] > weak_frac * kernel_sigma_eff))
        diagnostics.append(
            f'{amp}:{n_anchor}/{n_weak}/{post_sigma.max():.2e}')

    return horizontal, diagnostics, kernel_sigma_eff
