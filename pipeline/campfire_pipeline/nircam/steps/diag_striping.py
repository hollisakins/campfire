"""
diag_striping: scattered-light diagonal stripe subtraction (opt-in).

Some NIRCam exposures show stripe-like artifacts running at an arbitrary
angle across the detector — scattered light from bright stars off the
field, not the conventional row/column 1/f noise that the ``striping``
step handles. The angle is set by the off-axis source geometry, so it
varies per exposure (and per detector within a visit).

Runs after ``sky`` so the per-bin medians sit on a near-zero baseline
— required for the cross-strip ``max_strip_delta_ratio`` regularization,
which enforces ``|M[k+1,b] - M[k,b]| ≤ ratio · max(|M[k,b]|, |M[k+1,b]|)``
and would be effectively unconstrained against a non-zero sky pedestal.

Algorithm:
1. Read SRCMASK (written by ``striping`` and preserved through ``image2``,
   ``edge``, and ``sky`` via the JWST datamodel save round-trip).
2. Coarse + fine grid search over θ. Each angle scored as the MAD of
   ``data − strip_blended_model(θ)`` over unmasked finite pixels — the
   model used for scoring is the same one used in the actual subtraction
   (strip-blended with the configured ``column_width`` / ``overlap`` /
   ``max_strip_delta_ratio``), so the score directly measures the fit
   quality the pipeline will achieve. Strip blending matters because
   real scattered-light stripes have spatial amplitude variation that
   a single global per-bin median averages out; MAD over the full
   unmasked image gives n_pixels/n_bins more samples than scoring on
   the per-bin median array, with implicit pixel-count weighting.
3. **Apply (every iteration).** Subtract the strip-blended per-bin
   median plus an H+V residual 1/f fit. The per-strip estimator
   captures the spatial amplitude variation along x — closer to the
   bright off-axis source the amplitude is higher — that a single
   global median averages away. Pixels whose every covering strip has
   too few unmasked values fall back to the global per-bin median (a
   spatially-uniform estimate strictly better than 0); the trap of
   SRCMASK eating stripe peaks below ``min_pixels`` in every strip is
   mitigated this way without giving up per-strip resolution.
4. **Iter 2+ (optional).** If ``n_iterations > 1`` and
   ``rebuild_srcmask = true`` (the default), rebuild SRCMASK on the
   current residual before each subsequent iteration so stripe peaks
   initially flagged as sources are released as the amplitude bleeds
   into the running model. θ stays at the iter-1 estimate —
   re-scoring on a residual with most stripe-aligned signal already
   removed produces a flat score landscape and argmin walks rather
   than locks. Per-pass diagonal and H+V contributions accumulate
   into single cumulative models for the diagnostic plot.
5. Atomic-save the corrected SCI with a ``CFP_DIAG`` provenance keyword.
   NaN pixels in the input SCI are preserved as NaN in the output (the
   DO_NOT_USE bit is set on those pixels, but their value isn't zeroed).

Disabled by default. Enable per field with::

    [field.diag_striping]
        enabled = true
        theta_min = 25.0
        theta_max = 35.0
"""

import os

from functools import lru_cache

import numpy as np
from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.nircam import cfp
from campfire_pipeline.nircam.steps.striping import fit_residual_striping


@lru_cache(maxsize=4)
def _pixel_grid(shape):
    """Cached read-only ``(y, x)`` index grids for a given shape.

    The angle search calls ``_bin_indices`` ~130 times per exposure on the
    same image shape; reusing the index grids saves ~32 MB of allocations
    per call.
    """
    y, x = np.indices(shape)
    y.setflags(write=False)
    x.setflags(write=False)
    return y, x


def _bin_indices(shape, theta_deg, bin_width):
    """Diagonal-bin index for every pixel in an image of shape ``shape``.

    Bins are stacked perpendicular to the diagonal direction at angle
    ``theta_deg``. Returns an integer array of the same shape with
    contiguous bin indices starting at 0.
    """
    theta = np.radians(theta_deg)
    y, x = _pixel_grid(shape)
    # Perpendicular to the diagonal direction (theta + π/2)
    perp = theta + np.pi / 2
    proj = x * np.cos(perp) + y * np.sin(perp)
    proj -= proj.min()
    return (proj / bin_width).astype(np.int64)


_MAD_TO_STD = 1.4826  # mad_std normalization for a Gaussian


def _per_bin_clipped_median(values, bin_idx, n_bins, sigma=3.0, maxiters=5,
                            min_pixels=5):
    """Sigma-clipped median per bin. Returns 1D array of length ``n_bins``.

    Bins with fewer than ``min_pixels`` finite values get NaN.

    Clip threshold uses ``mad_std = 1.4826 * MAD`` rather than
    ``np.std``: a non-robust threshold is inflated by the very SRCMASK
    leakers we want to reject — one stripe-peak leaker at +5σ
    contributes ≈ 25σ²/N to the variance, so for small per-bin N the
    threshold floats up above the leaker and it survives the clip,
    defeating the iteration. Inlined rather than calling
    ``astropy.stats.sigma_clipped_stats`` per bin: that helper has
    ~50–100 µs of per-call machinery overhead (input validation,
    ``MaskedArray`` construction), and we call it ~500 K times per
    exposure (n_bins × n_strips × n_angles).

    Implementation: argsort/searchsorted to group pixels by bin index
    in O(N log N + n_bins), then a hand-rolled mad-std clip per bin.
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
            dev = np.abs(finite - m)
            mad = np.median(dev)
            if mad == 0:
                break
            keep = dev < sigma * _MAD_TO_STD * mad
            n_keep = int(keep.sum())
            if n_keep == finite.size:
                break  # nothing was clipped this pass
            if n_keep < min_pixels:
                break  # would over-clip; keep the prior estimate
            finite = finite[keep]
            m = np.median(finite)
        out[b] = m
    return out


def diagonal_stripe_model(data, mask, theta_deg, bin_width,
                          sigma=3.0, maxiters=5, min_pixels=5):
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
    """Triangular taper across the FULL overlap region; flat in the centre.

    Each strip's weight ramps up over its first ``overlap`` pixels (where
    it overlaps the previous strip) and ramps down over its last
    ``overlap`` pixels (where it overlaps the next strip), with a flat
    centre at weight 1. Adjacent strips' ramps are complementary —
    ``ramp[i] + ramp[overlap-1-i] = 1`` — so the summed weight in the
    overlap region is constant (partition of unity) and the blended
    model is a true linear interpolation between the two strips' models.
    """
    weights = np.ones(column_width)
    if overlap <= 0:
        return weights
    n = min(overlap, column_width)
    ramp = np.arange(1, n + 1) / (n + 1)
    weights[:n] = ramp
    weights[-n:] = ramp[::-1]
    return weights


def diagonal_stripe_model_blended(data, mask, theta_deg, bin_width,
                                  column_width=512, overlap=0,
                                  sigma=3.0, maxiters=5, min_pixels=5,
                                  max_strip_delta_ratio=None,
                                  compute_fallback=True,
                                  regularize=True):
    """Strip-blended diagonal stripe model with global-median fallback.

    The detector is split into vertical strips of width ``column_width``
    with ``overlap`` between adjacent strips. Each strip computes its
    own per-bin median; strips are merged with a triangular taper across
    the overlap so amplitude variations along x are captured without
    seams. NaN bins (insufficient unmasked pixels) drop out of the
    weighted average rather than zero-biasing it.

    For pixels whose bin is ``NaN`` in *every* covering strip — typically
    when SRCMASK is aggressive enough to cut a bin below ``min_pixels``
    in each individual strip — the per-strip blend has no information.
    Falling back to zero would leave a stripe untouched at exactly the
    rows where the bin is hardest to estimate, so we instead substitute
    the *global* per-bin median (computed over all strips combined). The
    global estimator pools every strip's pixels into a single bin sample
    and almost never hits the ``min_pixels`` threshold, giving a
    spatially-uniform amplitude estimate that's strictly better than 0
    when the per-strip estimator gives up.

    Bin indices are computed once on the full image so bin ``b`` refers
    to the same diagonal in every strip. This is a precondition for the
    optional cross-strip regularization (``max_strip_delta_ratio``),
    which caps how much the per-bin amplitude is allowed to change
    between adjacent strips — see ``regularize_strip_deltas``.

    ``compute_fallback=False`` skips the global per-bin median pass and
    leaves uncovered pixels as ``NaN``. Scoring callers filter NaN model
    pixels out of the score anyway, so the fallback is wasted work
    there — about half of the per-bin-median Python loops per angle.

    ``regularize=False`` skips ``regularize_strip_deltas`` even when
    ``max_strip_delta_ratio > 0``. Used by the angle-search scoring
    path: the regularizer compresses ``Var(M)`` slightly without
    shifting its argmax, so paying for it on every angle is wasted.
    """
    work = np.where(mask | ~np.isfinite(data), np.nan, data)
    return _model_from_work(
        work, theta_deg, bin_width,
        column_width=column_width, overlap=overlap,
        sigma=sigma, maxiters=maxiters, min_pixels=min_pixels,
        max_strip_delta_ratio=max_strip_delta_ratio,
        compute_fallback=compute_fallback,
        regularize=regularize,
    )


def _model_from_work(work, theta_deg, bin_width,
                     column_width=512, overlap=0,
                     sigma=3.0, maxiters=5, min_pixels=5,
                     max_strip_delta_ratio=None,
                     compute_fallback=True,
                     regularize=True):
    """Inner: same as ``diagonal_stripe_model_blended`` but expects a
    pre-masked ``work`` array (NaN where mask | ~np.isfinite(data)).

    The angle-search loop calls this per angle on the same
    ``work`` — pre-computing it once outside the loop saves an O(N)
    ``np.where`` pass per angle.
    """
    height, width = work.shape
    step = max(column_width - overlap, 1)
    n_columns = max((width - overlap) // step + 1, 1)

    bin_idx = _bin_indices(work.shape, theta_deg, bin_width)
    n_bins = int(bin_idx.max()) + 1

    if compute_fallback:
        # Global per-bin median: fallback for pixels whose every covering
        # strip has fewer than min_pixels unmasked values in the bin.
        global_M = _per_bin_clipped_median(
            work, bin_idx, n_bins,
            sigma=sigma, maxiters=maxiters, min_pixels=min_pixels,
        )
        out = np.where(np.isfinite(global_M[bin_idx]),
                       global_M[bin_idx], 0.0)
    else:
        out = np.full(work.shape, np.nan, dtype=np.float64)

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
        return out

    M = np.stack(strip_medians, axis=0)
    if (regularize and max_strip_delta_ratio is not None
            and max_strip_delta_ratio > 0):
        M = regularize_strip_deltas(M, float(max_strip_delta_ratio))

    accumulator = np.zeros(work.shape, dtype=np.float64)
    weight_acc = np.zeros(work.shape, dtype=np.float64)
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

    # Reuse ``out`` as the divide target — it already holds the fallback
    # values where ``compute_fallback=True``, or NaN otherwise. ``where``
    # leaves the fallback in place at zero-weight pixels.
    np.divide(accumulator, weight_acc, out=out, where=weight_acc > 0)
    return out


def _score_angle(work, theta_deg, bin_width,
                 column_width, overlap, max_strip_delta_ratio,
                 maxiters):
    """``-Var(M(θ))`` on the strip-blended model image; lower is better.

    By the total-variance decomposition ``Var(D) = Var(M) + Var(D−M)``
    on unmasked pixels (with ``Var(D)`` and ``Cov(M, D−M)`` independent
    of θ for fixed mask), maximizing ``Var(M)`` is equivalent to
    minimizing residual variance — same argmin as a residual-MAD score,
    but the score is the captured signal itself. Two practical wins:
    (a) the un-modeled source-residual floor in the residual is θ-
    independent and adds noise to a residual-MAD score; ``Var(M)``
    isolates the θ-dependent piece. (b) ``Var(M)`` is computed only on
    model values, decoupling it from outliers in ``D``.

    The model is the strip-blended diagonal-bin median that the actual
    subtraction uses, so the angle search rewards exactly the model
    that will be applied. ``compute_fallback=False`` skips the global
    per-bin median pass and ``regularize=False`` skips the cross-strip
    delta regularizer — both compress ``Var(M)`` slightly without
    shifting its argmax, so paying for them per-angle is wasted.

    Takes pre-masked ``work`` (NaN where mask | ~np.isfinite(data))
    rather than ``(data, mask)`` so the masking pass isn't repeated
    across the ~130 angle evaluations of a coarse+fine search.
    """
    model = _model_from_work(
        work, theta_deg, bin_width,
        column_width=column_width, overlap=overlap,
        max_strip_delta_ratio=max_strip_delta_ratio,
        maxiters=maxiters,
        compute_fallback=False,
        regularize=False,
    )
    valid = np.isfinite(work) & np.isfinite(model)
    if not np.any(valid):
        return float('inf')
    return -float(np.var(model[valid]))


def _coarse_fine_search(data, mask, bin_width,
                        theta_min, theta_max,
                        coarse_step, fine_step,
                        fine_window=2.0,
                        column_width=512, overlap=0,
                        max_strip_delta_ratio=None,
                        maxiters=5):
    """Two-stage angle search. Returns (opt_theta, thetas, scores).

    ``thetas`` and ``scores`` cover both passes for diagnostic plotting,
    sorted by angle. ``fine_window`` is the half-width (in degrees) of
    the fine pass around the coarse argmin. Strip parameters are passed
    through to ``_score_angle`` so the score model matches the actual
    subtraction.
    """
    score_kwargs = dict(column_width=column_width, overlap=overlap,
                        max_strip_delta_ratio=max_strip_delta_ratio,
                        maxiters=maxiters)
    # Hoist the θ-independent masking pass out of the angle loop. Saves
    # one O(N) ``np.where`` per angle (~130 angles × full-frame).
    work = np.where(mask | ~np.isfinite(data), np.nan, data)
    coarse_thetas = np.arange(theta_min, theta_max + 1e-9, coarse_step)
    coarse_scores = np.array([_score_angle(work, t, bin_width,
                                           **score_kwargs)
                              for t in coarse_thetas])
    coarse_min = coarse_thetas[int(np.argmin(coarse_scores))]

    fine_lo = max(coarse_min - fine_window, theta_min)
    fine_hi = min(coarse_min + fine_window, theta_max)
    fine_thetas = np.arange(fine_lo, fine_hi + 1e-9, fine_step)
    fine_scores = np.array([_score_angle(work, t, bin_width,
                                         **score_kwargs)
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


def filter_srcmask_stripes(seg, theta_deg, aspect_min=3.0,
                           angle_tol_deg=10.0, min_size=20):
    """Unmask connected components in ``seg`` that look like θ-aligned stripes.

    The source-detection mask written by ``striping`` is built with
    Gaussian smoothing at scales up to 25 px after a 40-px ring-median
    subtraction; a bright scattered-light stripe survives the ring filter
    and gets connected into a "source" by the smoothing. Once masked, the
    pixels along that stripe disappear from the per-bin median sample —
    and because diagonal bins run *along* the stripe direction at the
    optimum θ, every pixel in the bin overlapping the stripe is masked,
    leaving the per-bin amplitude unestimable. The fallback yields 0 → no
    subtraction at exactly the rows that need it most (visible in the
    diagnostic as a residual stripe in the "Corrected" panel).

    This filter restores those pixels. For each connected component in
    ``seg`` it computes the 2D second-moment matrix from the component's
    pixel coordinates (x = column, y = row in array order), then:
      - ``aspect_ratio = sqrt(λ_major / λ_minor)`` where λ are the
        eigenvalues of the covariance matrix
      - principal-axis angle from the eigenvector for λ_major, taken
        modulo 180° (the major axis is undirected)
      - if ``aspect_ratio > aspect_min`` AND the principal angle lies
        within ``angle_tol_deg`` of ``theta_deg``, the component is
        unmasked

    Components smaller than ``min_size`` pixels skip the test — moments
    are noisy on tiny components and false positives risk releasing
    isolated bad pixels that DQ might not have caught.

    Trade-off: a real galaxy (or diffraction spike) whose major axis
    happens to align with θ will be unmasked too. In practice this is
    rare for the narrow ``angle_tol_deg`` we use, and the per-bin
    sigma-clipped median rejects compact bright sources as outliers
    regardless. Diffraction-spike pixel counts above ``min_size`` along
    one spike are uncommon at typical PRIMER depth.
    """
    from scipy.ndimage import label as nd_label

    if not seg.any():
        return seg.copy()

    labeled, n_components = nd_label(seg)
    if n_components == 0:
        return seg.copy()

    out = seg.copy()
    # Inclusive-1D histogram is cheaper than np.where per label.
    sizes = np.bincount(labeled.ravel())
    candidates = np.where(sizes[1:] >= min_size)[0] + 1
    if candidates.size == 0:
        return out

    # Group pixels by label via argsort, same idiom as _per_bin_clipped_median.
    flat_lab = labeled.ravel()
    order = np.argsort(flat_lab, kind='stable')
    sorted_lab = flat_lab[order]
    splits = np.searchsorted(sorted_lab, np.arange(n_components + 2))
    height, width = seg.shape
    ys_all, xs_all = np.divmod(order, width)

    n_unmasked = 0
    for lab in candidates:
        s, e = splits[lab], splits[lab + 1]
        ys = ys_all[s:e]
        xs = xs_all[s:e]
        size = ys.size
        cx = xs.mean()
        cy = ys.mean()
        dx = xs - cx
        dy = ys - cy
        mxx = float((dx * dx).sum()) / size
        myy = float((dy * dy).sum()) / size
        mxy = float((dx * dy).sum()) / size
        tr = mxx + myy
        det = mxx * myy - mxy * mxy
        disc = (tr * tr / 4.0 - det)
        disc = disc if disc > 0 else 0.0
        disc = disc ** 0.5
        lam_major = tr / 2.0 + disc
        lam_minor = tr / 2.0 - disc
        if lam_minor <= 0:
            # Degenerate (single-pixel-wide line): treat as max aspect ratio.
            aspect = np.inf
        else:
            aspect = (lam_major / lam_minor) ** 0.5
        if aspect < aspect_min:
            continue
        # Principal eigenvector for [[mxx, mxy], [mxy, myy]] at λ_major:
        #   (mxx - λ) vx + mxy vy = 0  →  v = (mxy, λ - mxx)
        # Falls back to (1, 0) if both components vanish (perfectly axis-aligned
        # line where mxy = 0 and mxx > myy → λ = mxx).
        vx = mxy
        vy = lam_major - mxx
        if vx == 0 and vy == 0:
            vx, vy = (1.0, 0.0) if mxx >= myy else (0.0, 1.0)
        angle = np.degrees(np.arctan2(vy, vx))
        delta = (angle - theta_deg) % 180.0
        if delta > 90.0:
            delta -= 180.0
        if abs(delta) <= angle_tol_deg:
            out[labeled == lab] = False
            n_unmasked += 1

    filter_srcmask_stripes.last_count = n_unmasked  # for caller logging
    return out


def evaluate_skip_condition(thetas, scores, opt_theta,
                            skip_abs_range, skip_abs_range_at_edge,
                            skip_boundary_dist):
    """Return ``(skip: bool, reason: str, abs_range, boundary_dist)``.

    Two-tier OR condition. The thresholds were chosen empirically from
    the F356W UDS audit (``scripts/diag_striping_score_audit.py``); for
    other fields, re-audit before lowering them. Pass ``skip_abs_range=0``
    (or the matching pair=0) to disable each tier.
    """
    abs_range = float(scores.max() - scores.min())
    theta_min = float(thetas.min())
    theta_max = float(thetas.max())
    boundary_dist = float(min(opt_theta - theta_min, theta_max - opt_theta))
    if skip_abs_range > 0 and abs_range < skip_abs_range:
        reason = (f"flat score curve "
                  f"(abs_range={abs_range:.2e} < {skip_abs_range:.0e})")
        return True, reason, abs_range, boundary_dist
    if (skip_abs_range_at_edge > 0 and skip_boundary_dist > 0
            and abs_range < skip_abs_range_at_edge
            and boundary_dist < skip_boundary_dist):
        reason = (f"shallow curve, optimum at search edge "
                  f"(abs_range={abs_range:.2e} < {skip_abs_range_at_edge:.0e}, "
                  f"boundary_dist={boundary_dist:.2f} < {skip_boundary_dist})")
        return True, reason, abs_range, boundary_dist
    return False, "", abs_range, boundary_dist


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

    if cfp.should_skip(exposure_file, 'CFP_DIAG', rootname,
                       'diag_striping', status, overwrite):
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
    n_iterations = max(int(step_config.get('n_iterations', 1)), 1)
    # Rebuild SRCMASK on the running residual between iterations so stripe
    # pixels that source detection masked at full amplitude (and biased the
    # per-bin median low) are released as the residual cleans up. Heavy: each
    # rebuild calls the four-tier ``striping._build_srcmask`` (FFT convs).
    rebuild_srcmask = bool(step_config.get('rebuild_srcmask',
                                            n_iterations > 1))
    # Filter the SRCMASK to release connected components that are elongated
    # along θ — bright stripes occasionally get detected as "sources" during
    # masking and would otherwise leave their host bin with no unmasked
    # samples (per-bin median NaN → fallback 0 → stripe unsubtracted).
    unmask_stripe_aligned = bool(step_config.get('unmask_stripe_aligned', True))
    stripe_aspect_min = float(step_config.get('stripe_aspect_min', 3.0))
    stripe_angle_tol = float(step_config.get('stripe_angle_tol_deg', 10.0))
    stripe_min_size = int(step_config.get('stripe_min_size', 20))
    # Skip the subtraction when the angle search hasn't found meaningful
    # stripe signal — applying a model fit to noise is the harm-not-help
    # regime. Two-tier OR condition (see ``scripts/diag_striping_score_audit.py``
    # for empirical justification on the F356W UDS audit set):
    #   - ``abs_range = scores.max() - scores.min()`` below
    #     ``skip_abs_range``: score curve is flat at any θ, no stripe.
    #   - ``abs_range`` below ``skip_abs_range_at_edge`` AND the optimum
    #     within ``skip_boundary_dist`` degrees of theta_min/theta_max:
    #     argmin walked to the search wall (no real interior minimum).
    # Set ``skip_abs_range = 0`` to disable both tiers; set only the
    # at-edge pair to 0 to disable just the boundary-walked tier.
    skip_abs_range = float(step_config.get('skip_abs_range', 0.0))
    skip_abs_range_at_edge = float(
        step_config.get('skip_abs_range_at_edge', 0.0))
    skip_boundary_dist = float(step_config.get('skip_boundary_dist', 0.0))
    do_plot = bool(step_config.get('plot', True))

    from jwst.datamodels import ImageModel, dqflags

    model = ImageModel(exposure_file)
    sci_before = model.data.copy()
    dq_before = model.dq.copy()
    err_before = model.err.copy()
    # Only DO_NOT_USE pixels are unusable for fitting — informational bits
    # like JUMP_DET flag already-corrected pixels. See note in striping.py.
    bp_before = np.bitwise_and(dq_before, dqflags.pixel['DO_NOT_USE']) != 0

    seg = _read_srcmask(exposure_file)
    if seg is None:
        log(f"diag_striping: no SRCMASK on {rootname}; rebuilding from DQ only")
        seg = np.zeros(model.data.shape, dtype=bool)
    mask = bp_before | seg | ~np.isfinite(sci_before)

    log(f"diag_striping: searching θ in [{theta_min}, {theta_max}]° "
        f"(coarse {coarse_step}°, fine {fine_step}°)")
    opt_theta, thetas, scores = _coarse_fine_search(
        sci_before, mask, bin_width,
        theta_min, theta_max, coarse_step, fine_step,
        fine_window=fine_window,
        column_width=column_width, overlap=overlap,
        max_strip_delta_ratio=max_strip_delta_ratio,
        maxiters=maxiters,
    )
    log(f"diag_striping: opt θ = {opt_theta:.2f}°  "
        f"min-score = {scores.min():.4e}")

    skip, skip_reason, abs_range, boundary_dist = evaluate_skip_condition(
        thetas, scores, opt_theta,
        skip_abs_range, skip_abs_range_at_edge, skip_boundary_dist,
    )
    if skip:
        log(f"diag_striping: SKIPPING subtraction — {skip_reason}")

    if not skip and unmask_stripe_aligned and seg.any():
        seg_in_count = int(seg.sum())
        seg = filter_srcmask_stripes(
            seg, opt_theta,
            aspect_min=stripe_aspect_min,
            angle_tol_deg=stripe_angle_tol,
            min_size=stripe_min_size,
        )
        n_unmasked_components = getattr(
            filter_srcmask_stripes, 'last_count', 0)
        seg_out_count = int(seg.sum())
        mask = bp_before | seg | ~np.isfinite(sci_before)
        log(f"diag_striping: unmasked {n_unmasked_components} "
            f"stripe-aligned components ({seg_in_count - seg_out_count} px, "
            f"{(seg_in_count - seg_out_count) / max(seg_in_count, 1):.3f} of "
            f"original SRCMASK)")
    # One trace per scoring pass — iter 1's coarse+fine plus each later
    # iter's fine refinement on the cleaned residual. Score landscapes
    # change across iters (Var(data) shrinks as stripe is subtracted), so
    # the diagnostic plot draws each in its own colour rather than
    # concatenating them onto a single y-axis.
    score_traces = [('iter 1: coarse + fine', thetas, scores)]

    diag_model = np.zeros(sci_before.shape, dtype=np.float64)
    hv_model = np.zeros(sci_before.shape, dtype=np.float64)
    working = sci_before.copy()
    ampcounts = []
    # When skipping, the iteration loop is bypassed: ``working`` stays at
    # ``sci_before`` and the model arrays stay zero, so the saved canonical
    # is bit-identical to the input (modulo CFP_DIAG header / DQ flag for
    # NaN propagation). The diagnostic plot still renders below so the
    # decision is traceable.
    for it in range(0 if skip else n_iterations):
        # Refresh SRCMASK between iterations. θ stays fixed at iter-1's
        # value: iter 1's score curve is computed on the original
        # high-signal data and has a clean minimum, while iter 2+ scores
        # on a residual with most stripe-aligned signal already removed —
        # the score landscape is flat over the fine window, so argmin
        # walks toward whichever edge has more residual sky/source slope
        # rather than toward a real angular optimum.
        if it > 0 and rebuild_srcmask:
            from campfire_pipeline.nircam.steps.striping import _build_srcmask
            stub = ImageModel()
            stub.data = working.astype(np.float32)
            stub.err = err_before
            stub.dq = dq_before
            seg = _build_srcmask(stub).astype(bool)
            stub.close()
            if unmask_stripe_aligned and seg.any():
                seg = filter_srcmask_stripes(
                    seg, opt_theta,
                    aspect_min=stripe_aspect_min,
                    angle_tol_deg=stripe_angle_tol,
                    min_size=stripe_min_size,
                )
            mask = bp_before | seg | ~np.isfinite(sci_before)
            log(f"diag_striping: iter {it + 1}: rebuilt SRCMASK "
                f"(masked frac = {seg.mean():.3f})")

        # Strip-blended every iteration: the SRCMASK-eats-stripe-peaks
        # trap that motivated a global-only iter 1 is already mitigated
        # by the global per-bin median fallback inside
        # ``diagonal_stripe_model_blended`` (used wherever every covering
        # strip lacks min_pixels in a bin). A global-only first pass
        # under-corrects when scattered-light amplitude varies across
        # strips — the dominant case the strip-blended model was added
        # to capture.
        diag_iter = diagonal_stripe_model_blended(
            working, mask, opt_theta, bin_width,
            column_width=column_width, overlap=overlap,
            max_strip_delta_ratio=max_strip_delta_ratio,
            maxiters=maxiters,
        )
        diag_model += diag_iter
        working = sci_before - diag_model - hv_model

        h_iter, v_iter, ampcounts = fit_residual_striping(
            working, mask, maxiters,
        )
        hv_model += h_iter + v_iter
        working = sci_before - diag_model - hv_model

        finite = np.isfinite(diag_iter)
        damp = float(np.nanmax(np.abs(diag_iter[finite]))) if finite.any() else 0.0
        hamp = float(np.nanmax(np.abs(h_iter + v_iter)))
        log(f"diag_striping: iter {it + 1}/{n_iterations}: "
            f"max |Δdiag| = {damp:.3e}, max |ΔHV| = {hamp:.3e}")

    if not skip:
        log(f"diag_striping: residual full-row medians used: "
            f"{', '.join(ampcounts)}/{sci_before.shape[0]}")

    sci_after = working
    # Restore exact-zero reference-border pixels (the model can drift them
    # off zero by tiny amounts).
    sci_after[sci_before == 0] = 0
    # Pre-existing NaNs propagate to the output as NaN — flag DO_NOT_USE
    # so downstream resampling skips them, but don't overwrite the value.
    wnan = np.isnan(sci_after)
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

    if skip:
        cfp_value = (
            f'SKIPPED: {skip_reason}; would-be theta={opt_theta:.2f}, '
            f'range=[{theta_min},{theta_max}]'
        )
    else:
        cfp_value = (
            f'theta={opt_theta:.2f}, range=[{theta_min},{theta_max}], '
            f'bin={bin_width}, col={column_width}/{overlap}, '
            f'delta={max_strip_delta_ratio}, niter={n_iterations}, '
            f'unmask_aligned={int(unmask_stripe_aligned)}'
            f'(ar={stripe_aspect_min},tol={stripe_angle_tol})'
        )
    atomic_save(
        model, exposure_file,
        header_updates=cfp.format(CFP_DIAG=cfp_value),
        extra_hdus=[srcmask_hdu] if srcmask_hdu is not None else None,
    )
    model.close()
    log(f"diag_striping done: {rootname}")

    if do_plot:
        from campfire_pipeline.common.imaging.plots import plot_diag_striping
        diag_pdf = os.path.join(
            os.path.dirname(exposure_file), f'{rootname}_diag_striping.pdf',
        )
        plot_title = rootname
        if skip:
            plot_title = (f"{rootname}  [SKIPPED — "
                          f"abs_range={abs_range:.2e}, "
                          f"bdy={boundary_dist:.2f}°]")
        plot_diag_striping(
            sci_before=sci_before,
            diag_model=diag_model,
            residual_model=hv_model,
            sci_after=sci_after,
            score_traces=score_traces,
            opt_theta=opt_theta,
            save_file=diag_pdf,
            title=plot_title,
        )
        log(f"Saved {os.path.basename(diag_pdf)}")
