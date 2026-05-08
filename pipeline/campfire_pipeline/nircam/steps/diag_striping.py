"""
diag_striping: scattered-light diagonal stripe subtraction (opt-in).

Some NIRCam exposures show stripe-like artifacts running at an arbitrary
angle across the detector — scattered light from bright stars off the
field, not the conventional row/column 1/f noise that the ``striping``
step handles. The angle is set by the off-axis source geometry, so it
varies per exposure (and per detector within a visit).

Algorithm:
1. Read SRCMASK (written by ``striping`` and preserved through ``image2``).
2. Coarse + fine grid search over θ. Score each angle with a single
   full-image diagonal-bin median (no strip blending) — variance of the
   residual on unmasked pixels. Strip blending doesn't change the
   argmin, so we skip it during the search to save ~8x.
3. Apply: column-blended diagonal-bin median at the optimal θ. Strips
   capture the spatial amplitude variation (closer to the bright star ⇒
   higher amplitude) that a single global model would average over.
4. Re-fit horizontal + vertical residual 1/f via the shared
   ``fit_residual_striping`` helper from the ``striping`` module.
5. Atomic-save the corrected SCI with a ``CFP_DIAG`` provenance keyword.

Disabled by default. Enable per field with::

    [field.diag_striping]
        enabled = true
        theta_min = 25.0
        theta_max = 35.0
"""

import os
import warnings

import numpy as np
from astropy.io import fits
from astropy.stats import median_absolute_deviation

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.steps.striping import fit_residual_striping


def _bin_indices(shape, theta_deg, bin_width):
    """Diagonal-bin index for every pixel in an image of shape ``shape``.

    Bins are stacked perpendicular to the diagonal direction at angle
    ``theta_deg``. Returns an integer array of the same shape with
    contiguous bin indices starting at 0.
    """
    theta = np.radians(theta_deg)
    height, width = shape
    y, x = np.meshgrid(np.arange(height), np.arange(width), indexing='ij')
    # Perpendicular to the diagonal direction (theta + π/2)
    perp = theta + np.pi / 2
    proj = x * np.cos(perp) + y * np.sin(perp)
    proj -= proj.min()
    return (proj / bin_width).astype(np.int64)


def _per_bin_clipped_median(values, bin_idx, n_bins, sigma=3.0, maxiters=2,
                            min_pixels=5):
    """Sigma-clipped median per bin. Returns 1D array of length ``n_bins``.

    Bins with fewer than ``min_pixels`` finite values get NaN.

    Implementation: argsort/searchsorted to group pixels by bin index in
    O(N log N + n_bins), then a vectorized clip per bin. Faster than
    boolean-masking the full image per bin, which is what the legacy
    script does.
    """
    flat_bin = bin_idx.ravel()
    flat_val = values.ravel()
    order = np.argsort(flat_bin, kind='stable')
    sorted_bins = flat_bin[order]
    sorted_vals = flat_val[order]
    splits = np.searchsorted(sorted_bins, np.arange(n_bins + 1))

    out = np.full(n_bins, np.nan)
    for b in range(n_bins):
        vals = sorted_vals[splits[b]:splits[b + 1]]
        finite = vals[np.isfinite(vals)]
        if finite.size < min_pixels:
            continue
        m = np.median(finite)
        for _ in range(maxiters):
            s = np.std(finite)
            if s == 0:
                break
            keep = np.abs(finite - m) < sigma * s
            if keep.sum() < min_pixels:
                break
            finite = finite[keep]
            m = np.median(finite)
        out[b] = m
    return out


def diagonal_stripe_model(data, mask, theta_deg, bin_width,
                          sigma=3.0, maxiters=2, min_pixels=5):
    """Per-bin sigma-clipped median assigned back to each pixel.

    Pixels in masked or non-finite bins get NaN — caller decides whether
    to fill with 0 (single-pass apply) or skip in a weighted blend.
    """
    bin_idx = _bin_indices(data.shape, theta_deg, bin_width)
    work = np.where(mask | ~np.isfinite(data), np.nan, data)
    n_bins = int(bin_idx.max()) + 1
    bin_medians = _per_bin_clipped_median(
        work, bin_idx, n_bins,
        sigma=sigma, maxiters=maxiters, min_pixels=min_pixels,
    )
    return bin_medians[bin_idx]


def _project_pair_vec(a, b, max_ratio):
    """Vectorized projection so |a-b| ≤ max_ratio * max(|a|, |b|).

    For violating elements, both endpoints are pulled toward their midpoint
    by a multiplicative shrinkage that puts the new pair exactly on the
    constraint boundary. Sign-changing pairs (where the midpoint is near
    zero) collapse toward zero, which is the right behavior — opposite-sign
    adjacent strips at the same diagonal bin are noise, not real
    scattered-light amplitude variation.

    NaN entries pass through unchanged.
    """
    a_new = a.astype(np.float64, copy=True)
    b_new = b.astype(np.float64, copy=True)
    finite = np.isfinite(a) & np.isfinite(b)
    if not np.any(finite):
        return a_new, b_new
    delta = np.abs(a - b)
    scale = np.maximum(np.abs(a), np.abs(b))
    violate = finite & (delta > max_ratio * scale)
    if not np.any(violate):
        return a_new, b_new
    mid = 0.5 * (a + b)
    # |new_a - new_b| = d, max(|new_a|, |new_b|) = |mid| + d/2
    # Set d = max_ratio * (|mid| + d/2)  →  d = max_ratio*|mid| / (1 - max_ratio/2)
    d = max_ratio * np.abs(mid) / (1.0 - max_ratio / 2.0)
    a_larger = a > b
    a_proj = np.where(a_larger, mid + d / 2.0, mid - d / 2.0)
    b_proj = np.where(a_larger, mid - d / 2.0, mid + d / 2.0)
    a_new = np.where(violate, a_proj, a_new)
    b_new = np.where(violate, b_proj, b_new)
    return a_new, b_new


def regularize_strip_deltas(M, max_ratio, n_passes=20, tol=1e-6):
    """Smooth per-strip per-bin medians along the strip axis.

    Parameters
    ----------
    M : ndarray of shape (n_strips, n_bins)
        Per-strip per-bin median amplitudes; NaN where a strip has too
        few unmasked pixels in a bin.
    max_ratio : float
        Maximum fractional delta allowed between adjacent strips at the
        same bin: ``|M[k+1,b] - M[k,b]| ≤ max_ratio * max(|M[k,b]|, |M[k+1,b]|)``.
    n_passes : int
        Forward/backward sweep limit. The constraint set is non-convex in
        the sign-changing region, so alternating projection is a heuristic;
        for typical scattered-light amplitudes (same sign across strips)
        it converges in a handful of passes.

    Returns a copy of M with the constraint enforced as far as possible.
    """
    out = M.astype(np.float64, copy=True)
    n_strips = out.shape[0]
    if n_strips < 2 or max_ratio <= 0:
        return out
    for _ in range(n_passes):
        prev = out.copy()
        for k in range(n_strips - 1):
            out[k], out[k + 1] = _project_pair_vec(out[k], out[k + 1], max_ratio)
        for k in range(n_strips - 2, -1, -1):
            out[k], out[k + 1] = _project_pair_vec(out[k], out[k + 1], max_ratio)
        with np.errstate(invalid='ignore'):
            diff = np.abs(out - prev)
            if not np.any(np.isfinite(diff)) or np.nanmax(diff) < tol:
                break
    return out


def _column_weights(column_width, overlap):
    """Triangular taper across the overlap region; flat in the centre."""
    weights = np.ones(column_width)
    taper = max(overlap // 2, 1)
    ramp = np.arange(1, taper + 1) / taper
    weights[:min(taper, column_width)] = ramp[:min(taper, column_width)]
    weights[-min(taper, column_width):] = ramp[:min(taper, column_width)][::-1]
    return weights


def diagonal_stripe_model_blended(data, mask, theta_deg, bin_width,
                                  column_width=512, overlap=0,
                                  sigma=3.0, maxiters=2, min_pixels=5,
                                  max_strip_delta_ratio=None):
    """Strip-blended diagonal stripe model.

    The detector is split into vertical strips of width ``column_width``
    with ``overlap`` between adjacent strips. Each strip computes its
    own per-bin median; strips are merged with a triangular taper across
    the overlap so amplitude variations along x are captured without
    seams. NaN bins (insufficient unmasked pixels) drop out of the
    weighted average rather than zero-biasing it.

    Bin indices are computed once on the full image so bin ``b`` refers
    to the same diagonal in every strip. This is a precondition for the
    optional cross-strip regularization (``max_strip_delta_ratio``),
    which caps how much the per-bin amplitude is allowed to change
    between adjacent strips — see ``regularize_strip_deltas``.
    """
    height, width = data.shape
    step = max(column_width - overlap, 1)
    n_columns = max((width - overlap) // step + 1, 1)

    bin_idx = _bin_indices(data.shape, theta_deg, bin_width)
    n_bins = int(bin_idx.max()) + 1
    work = np.where(mask | ~np.isfinite(data), np.nan, data)

    strip_bounds = []
    strip_medians = []
    for col_idx in range(n_columns):
        x_start = col_idx * step
        x_end = min(x_start + column_width, width)
        actual_width = x_end - x_start
        if actual_width < column_width // 2 and col_idx > 0:
            continue
        col_work = work[:, x_start:x_end]
        col_bins = bin_idx[:, x_start:x_end]
        strip_medians.append(_per_bin_clipped_median(
            col_work, col_bins, n_bins,
            sigma=sigma, maxiters=maxiters, min_pixels=min_pixels,
        ))
        strip_bounds.append((x_start, x_end))

    if not strip_medians:
        return np.zeros(data.shape, dtype=np.float64)

    M = np.stack(strip_medians, axis=0)
    if max_strip_delta_ratio is not None and max_strip_delta_ratio > 0:
        M = regularize_strip_deltas(M, float(max_strip_delta_ratio))

    accumulator = np.zeros(data.shape, dtype=np.float64)
    weight_acc = np.zeros(data.shape, dtype=np.float64)
    for k, (x_start, x_end) in enumerate(strip_bounds):
        actual_width = x_end - x_start
        col_bins = bin_idx[:, x_start:x_end]
        col_model = M[k][col_bins]
        weights = _column_weights(actual_width, overlap)
        finite = np.isfinite(col_model)
        contribution = np.where(finite, col_model * weights, 0.0)
        eff_weight = np.where(finite, weights, 0.0)
        accumulator[:, x_start:x_end] += contribution
        weight_acc[:, x_start:x_end] += eff_weight

    return np.divide(accumulator, weight_acc,
                     out=np.zeros_like(accumulator),
                     where=weight_acc > 0)


def _score_angle(data, mask, theta_deg, bin_width):
    """Variance score for one angle; lower is better.

    Scoring uses the global (non-blended) per-bin median because strip
    blending is an amplitude correction, not an angle-selection signal —
    its argmin agrees with the global model's. Robust statistic
    (sigma-MAD²) so a few extreme residuals don't dominate.
    """
    model = diagonal_stripe_model(data, mask, theta_deg, bin_width)
    resid = data - np.where(np.isfinite(model), model, 0.0)
    sample = resid[~mask]
    sample = sample[np.isfinite(sample)]
    if sample.size == 0:
        return float('inf')
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', category=RuntimeWarning)
        mad = median_absolute_deviation(sample)
    return float(mad) ** 2


def _coarse_fine_search(data, mask, bin_width,
                        theta_min, theta_max,
                        coarse_step, fine_step,
                        fine_window=2.0):
    """Two-stage angle search. Returns (opt_theta, thetas, scores).

    ``thetas`` and ``scores`` cover both passes for diagnostic plotting,
    sorted by angle. ``fine_window`` is the half-width (in degrees) of
    the fine pass around the coarse argmin.
    """
    coarse_thetas = np.arange(theta_min, theta_max + 1e-9, coarse_step)
    coarse_scores = np.array([_score_angle(data, mask, t, bin_width)
                              for t in coarse_thetas])
    coarse_min = coarse_thetas[int(np.argmin(coarse_scores))]

    fine_lo = max(coarse_min - fine_window, theta_min)
    fine_hi = min(coarse_min + fine_window, theta_max)
    fine_thetas = np.arange(fine_lo, fine_hi + 1e-9, fine_step)
    fine_scores = np.array([_score_angle(data, mask, t, bin_width)
                            for t in fine_thetas])
    opt_theta = float(fine_thetas[int(np.argmin(fine_scores))])

    all_thetas = np.concatenate([coarse_thetas, fine_thetas])
    all_scores = np.concatenate([coarse_scores, fine_scores])
    order = np.argsort(all_thetas)
    return opt_theta, all_thetas[order], all_scores[order]


def _read_srcmask(exposure_file):
    """Return SRCMASK as a bool array, or None if the extension is absent."""
    with fits.open(exposure_file) as hdul:
        if 'SRCMASK' not in hdul:
            return None
        return hdul['SRCMASK'].data.astype(bool)


def diag_striping_step(exposure_file, field, step_config, overwrite=False,
                       status=None):
    """Subtract scattered-light diagonal striping from a canonical exposure.

    Parameters
    ----------
    exposure_file : str
        Canonical ``<rootname>.fits`` (post-image2 cal-stage data).
    field : Field
    step_config : dict
        Resolved ``[nircam.diag_striping]`` config.
    overwrite : bool
    status : StepStatus, optional
    """
    rootname = os.path.basename(exposure_file).removesuffix('.fits')

    if not overwrite:
        already_done = (status.has(exposure_file, 'CFP_DIAG')
                        if status is not None
                        else cfp.has_step(exposure_file, 'CFP_DIAG'))
        if already_done:
            log(f"Skipping diag_striping on {rootname}: CFP_DIAG already set")
            return

    log(f"Running diag_striping on {rootname}")

    theta_min = float(step_config.get('theta_min', 0.0))
    theta_max = float(step_config.get('theta_max', 90.0))
    coarse_step = float(step_config.get('theta_coarse_step', 1.0))
    fine_step = float(step_config.get('theta_fine_step', 0.1))
    fine_window = float(step_config.get('theta_fine_window', 2.0))
    bin_width = float(step_config.get('bin_width', 3.0))
    column_width = int(step_config.get('column_width', 512))
    overlap = int(step_config.get('column_overlap', 0))
    max_strip_delta_ratio = float(step_config.get('max_strip_delta_ratio', 0.3))
    maxiters = int(step_config.get('maxiters', 3))
    do_plot = bool(step_config.get('plot', True))

    from jwst.datamodels import ImageModel, dqflags

    model = ImageModel(exposure_file)
    sci_before = model.data.copy()

    seg = _read_srcmask(exposure_file)
    if seg is None:
        log(f"diag_striping: no SRCMASK on {rootname}; rebuilding from DQ only")
        seg = np.zeros(model.data.shape, dtype=bool)
    mask = (model.dq > 0) | seg | ~np.isfinite(model.data)

    log(f"diag_striping: searching θ in [{theta_min}, {theta_max}]° "
        f"(coarse {coarse_step}°, fine {fine_step}°)")
    opt_theta, thetas, scores = _coarse_fine_search(
        sci_before, mask, bin_width,
        theta_min, theta_max, coarse_step, fine_step,
        fine_window=fine_window,
    )
    log(f"diag_striping: optimal θ = {opt_theta:.2f}°  "
        f"min-score = {scores.min():.4e}")

    diag_model = diagonal_stripe_model_blended(
        sci_before, mask, opt_theta, bin_width,
        column_width=column_width, overlap=overlap,
        max_strip_delta_ratio=max_strip_delta_ratio,
    )
    sci_diag_subbed = sci_before - diag_model

    horizontal, vertical, ampcounts = fit_residual_striping(
        sci_diag_subbed, mask, maxiters,
    )
    log(f"diag_striping: residual full-row medians used: "
        f"{', '.join(ampcounts)}/{sci_before.shape[0]}")

    sci_after = sci_diag_subbed - horizontal - vertical
    sci_after[sci_before == 0] = 0
    wnan = np.isnan(sci_after)
    sci_after[wnan] = 0
    bpflag = dqflags.pixel['DO_NOT_USE']
    model.dq[wnan] = np.bitwise_or(model.dq[wnan], bpflag)
    model.data = sci_after

    # Preserve SRCMASK (image2 round-trip pattern: re-attach extension).
    srcmask_hdu = None
    with fits.open(exposure_file) as hdul:
        if 'SRCMASK' in hdul:
            hdu = hdul['SRCMASK']
            srcmask_hdu = fits.ImageHDU(
                hdu.data.copy(), header=hdu.header.copy(), name='SRCMASK',
            )

    cfp_value = (
        f'theta={opt_theta:.2f}, range=[{theta_min},{theta_max}], '
        f'bin={bin_width}, col={column_width}/{overlap}, '
        f'delta={max_strip_delta_ratio}'
    )
    atomic_save(
        model, exposure_file,
        header_updates=cfp.format(CFP_DIAG=cfp_value),
        extra_hdus=[srcmask_hdu] if srcmask_hdu is not None else None,
    )
    model.close()
    log(f"diag_striping done: {rootname}")

    if do_plot:
        from campfire_pipeline.nircam.steps._plots import plot_diag_striping
        diag_pdf = os.path.join(
            os.path.dirname(exposure_file), f'{rootname}_diag_striping.pdf',
        )
        plot_diag_striping(
            sci_before=sci_before,
            diag_model=diag_model,
            residual_model=horizontal + vertical,
            sci_after=sci_after,
            thetas=thetas,
            scores=scores,
            opt_theta=opt_theta,
            save_file=diag_pdf,
            title=rootname,
        )
        log(f"Saved {os.path.basename(diag_pdf)}")
