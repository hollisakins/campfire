"""
artifact: intra-visit detector-fixed artifact removal via dither self-calibration.

Per-(visit, detector) **ensemble** step. Runs **after ``image2`` and before
``striping``** (order: ... wisp -> image2 -> artifact -> striping -> ...) so
the artifacts are gone before the 1/f stripe offsets are measured — an
artifact sitting in an amp-row otherwise biases that row's clipped-median
offset and the striping step subtracts the wrong DC level.

The idea is detector-frame self-calibration. Stack the dithers of one
(visit, detector) group **by pixel index, with no WCS alignment**:

* Real sky signal (point sources, galaxy wings, ICL / diffuse emission) is
  fixed in *sky* coordinates, so the dither motion lands it on a *different*
  detector pixel each dither -> a robust per-pixel statistic across the stack
  **rejects** it. The dither throw itself is what protects the science.
* 1/f striping is positionally detector-fixed but its amplitude is a fresh
  draw each integration, so it **averages down**.
* Detector-fixed artifacts — persistence, scattered light, unique LW wisps —
  sit at the *same* detector pixel every dither, so they **survive**.

What survives the masked-robust stack is therefore (detector-fixed artifacts)
+ (a smooth detector/zodi pedestal). Subtracting a smooth model of the stack
isolates the structured artifact template, which subtracts identically from
every dither (optionally scaled per exposure to track persistence decay).

This is additive to the combine-phase ``outlier`` step: that one works in
*sky* coordinates, only flags DQ (punching holes), catches just the
high-contrast core, and runs far too late to help the stripe fit. This step
*subtracts* the detector-fixed component early and *keeps* the pixels.

**Safeguards.** Self-calibration needs enough dither positions, spread far
enough apart, that a source at a given detector pixel in one dither has moved
away in the others. The step reads the commanded dither geometry
(``XOFFSET``/``YOFFSET``, falling back to ``RA_REF``/``DEC_REF``) and no-ops
(stamping ``CFP_ART = 'skipped (...)'``) when there are too few distinct
positions or the minimum pairwise throw is too small. Disabled by default
(opt-in via ``[nircam.artifact].enabled = true`` or a per-field override).

The numerical core (``dither_positions_and_spacing``,
``build_artifact_template``, ``fit_template_scale``) is split into pure
functions so it is unit-testable without the jwst stack; ``artifact_step``
wraps them with ImageModel I/O and the shared tiered source mask.
"""

import os
import warnings
from datetime import datetime

import numpy as np
from astropy.io import fits
from astropy.stats import median_absolute_deviation, sigma_clip

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


# NIRCam pixel scales (arcsec/pix). Used only to convert the commanded dither
# throw into detector pixels for the spacing safeguard, so the nominal
# per-channel values are plenty accurate (the WCS-derived scale varies by
# <0.5% across the field).
PIXSCALE_ARCSEC = {'SHORT': 0.0311, 'LONG': 0.0630}

_VALID_MASK = ('none', 'standard', 'aggressive')
_VALID_SCALE = ('fit', 'fixed')
_VALID_SOURCE_REJECTION = ('stationary', 'skymodel')


def _channel_of(detector):
    """'SHORT' or 'LONG' from a detector token (e.g. 'nrcalong' -> 'LONG')."""
    return 'LONG' if detector.lower().endswith('long') else 'SHORT'


# ---------------------------------------------------------------------------
# Pure numerical core (no jwst dependency — unit-testable directly)
# ---------------------------------------------------------------------------

def dither_positions_and_spacing(xoffsets, yoffsets, pixscale,
                                 round_arcsec=0.05):
    """Distinct dither positions and the minimum pairwise throw (in pixels).

    Repeat exposures at the *same* commanded offset (NEXP/NINTS > 1) do not
    help reject moving sources, so they are collapsed to a single position by
    rounding to ``round_arcsec`` before counting. The gate quantity is the
    minimum pairwise separation among the *distinct* positions — the worst
    case for a source failing to move off a given detector pixel.

    Parameters
    ----------
    xoffsets, yoffsets : array-like
        Commanded ideal-frame dither offsets (arcsec), one per exposure.
    pixscale : float
        Detector pixel scale (arcsec/pix).
    round_arcsec : float
        Clustering tolerance for collapsing co-located exposures.

    Returns
    -------
    n_positions : int
        Number of distinct dither positions.
    min_spacing_pix : float
        Minimum pairwise separation among distinct positions, in detector
        pixels. ``inf`` when there is only one position (no pair to measure).
    """
    xy = np.column_stack([np.asarray(xoffsets, float),
                          np.asarray(yoffsets, float)])
    # Cluster by rounding to the tolerance grid, then take unique rows.
    keys = np.round(xy / round_arcsec).astype(np.int64)
    _, idx = np.unique(keys, axis=0, return_index=True)
    pos = xy[np.sort(idx)]
    n_positions = len(pos)
    if n_positions < 2:
        return n_positions, np.inf
    # Pairwise Euclidean distances among distinct positions.
    diff = pos[:, None, :] - pos[None, :, :]
    dist = np.sqrt((diff ** 2).sum(-1))
    iu = np.triu_indices(n_positions, k=1)
    min_spacing_arcsec = float(dist[iu].min())
    return n_positions, min_spacing_arcsec / pixscale


def inpaint_nan(arr, sigma0=3.0, growth=2.0, max_iters=8):
    """Fill NaNs by iterated normalized Gaussian convolution (smooth diffusion).

    Each pass replaces NaNs with a Gaussian-weighted average of the surrounding
    *finite* pixels (``gaussian_filter(filled) / gaussian_filter(weights)``),
    growing the kernel each iteration so large gaps fill from progressively
    wider context. This diffuses the surrounding level smoothly across a gap —
    a gap inside the artifact fills with ~the local artifact level, a gap in
    clean background with ~0 — keeping the correction continuous (no hard step
    at gap boundaries, the failure mode of zero-filling).

    This replaces a nearest-valid-neighbour (EDT) fill, which tiled gaps with
    blocky Voronoi plateaus that showed through the template as spurious
    structure. Returns a copy; an all-NaN input returns zeros.
    """
    from scipy.ndimage import gaussian_filter
    out = arr.astype(np.float64, copy=True)
    if not (~np.isfinite(out)).any():
        return out
    if (~np.isfinite(out)).all():
        return np.zeros_like(out)
    sigma = sigma0
    for _ in range(max_iters):
        nan = ~np.isfinite(out)
        if not nan.any():
            break
        filled = np.where(nan, 0.0, out)
        w = (~nan).astype(np.float64)
        num = gaussian_filter(filled, sigma, mode='nearest')
        den = gaussian_filter(w, sigma, mode='nearest')
        with np.errstate(invalid='ignore', divide='ignore'):
            interp = num / den
        good_interp = nan & np.isfinite(interp) & (den > 1e-8)
        out[good_interp] = interp[good_interp]
        sigma *= growth
    out[~np.isfinite(out)] = 0.0
    return out


def despike(image, size=5, nsigma=4.0, maxiters=3, dilate=1):
    """Replace point-like positive outliers (hot/warm pixels) with the local median.

    Detector-fixed warm/hot pixels survive the cross-dither median (they're
    *temporally constant*, so a per-pixel sigma-clip across dithers can't reject
    them) and, once smoothed, become subtracted Gaussian blobs. They are
    point-like, whereas the artifact is smooth — so a size-``size`` median
    filter follows the artifact and ignores the spikes, and ``image -
    median_filter(image)`` isolates them *wherever they sit*, on blank
    background OR on top of the extended artifact (the failure mode of a
    segment-membership filter). Flagged pixels (residual > ``nsigma`` x the
    robust residual scale) are replaced with the local median — the artifact
    level there — so the spike is removed and the smooth artifact is preserved.

    The residual scale is estimated by **iteratively** sigma-clipping the
    median-filter residual itself (``maxiters`` passes, re-measuring ``mad_std``
    with the spikes excluded). A single global ``mad_std`` is inflated by sharp
    artifact edges / leaked source cores, which raises the threshold and lets
    marginal hot pixels through; excluding the spikes from the scale converges
    on the *clean* residual level and catches them. Each flagged spike is then
    grown by ``dilate`` px before replacement, since hot pixels carry IPC
    charge-bleed wings that the bare-peak replacement leaves behind (and
    smoothing would spread).

    Must run BEFORE smoothing, while spikes are still ~1 px; after smoothing
    each is a ~5 px Gaussian that no longer reads as a point outlier. Returns
    ``(despiked_image, n_spikes)`` where ``n_spikes`` counts the peak pixels
    (pre-dilation).
    """
    from astropy.stats import mad_std
    from scipy.ndimage import median_filter, binary_dilation
    finite = np.isfinite(image)
    if not finite.any():
        return image.copy(), 0
    work = np.where(finite, image, float(np.median(image[finite])))
    local = median_filter(work, size=int(size))
    resid = work - local
    # Iteratively measure the residual scale with the spikes excluded, so a
    # textured artifact / source cores don't inflate it and hide marginal spikes.
    keep = finite.copy()
    spikes = np.zeros_like(finite)
    for _ in range(max(1, int(maxiters))):
        sig = mad_std(resid[keep])
        if not np.isfinite(sig) or sig == 0:
            break
        new_spikes = finite & (resid > nsigma * sig)
        if np.array_equal(new_spikes, spikes):
            break
        spikes = new_spikes
        keep = finite & ~spikes
    if not spikes.any():
        return image.copy(), 0
    # Grow each spike by `dilate` px to catch IPC wings; replace with the local
    # median (the artifact level there), so the smooth artifact is preserved.
    repl = spikes
    if dilate and dilate > 0:
        repl = finite & binary_dilation(spikes, iterations=int(dilate))
    out = image.copy()
    out[repl] = local[repl]
    return out, int(spikes.sum())


def build_artifact_template(frames, masks, sigma=3.0, maxiters=5,
                            min_contributing=2, interpolate_gaps=True,
                            isolate_background=False, isolate_box=128,
                            isolate_filter=3, smooth_sigma=2.0,
                            despike_nsigma=4.0, despike_size=5, despike_dilate=1,
                            coverage_weight=True, coverage_n_lo=1,
                            coverage_n_hi=3, coverage_smooth_sigma=3.0):
    """Build the detector-fixed artifact template from a dither stack.

    Pure function. Each frame is pedestal-matched (its own robust background
    median is subtracted) so the stack shares a common zero, then combined
    with a per-pixel sigma-clipped median over the unmasked samples. Sky-fixed
    signal has been masked or moved away by the dither, so what survives is the
    detector-fixed structure on a near-zero baseline.

    Pixels with too few contributing dithers (source-crowded / extended-source
    overlap) are interpolated across — not zeroed — so the resulting correction
    is spatially continuous. An optional large-box smooth model can then be
    subtracted to isolate the *structured* artifact from any smooth detector
    background (off by default: it removes the broad artifact wings too, which
    the per-exposure scale fit then has to undo), and the result is lightly
    smoothed to suppress per-pixel stack noise.

    Finally a **coverage-confidence map** ``w(n_contributing) in [0, 1]`` is
    computed (smooth, -> 0 where too few dithers contributed) and returned in
    ``diag`` — but the template itself is returned *ungated*. The caller fits
    the per-exposure amplitude on the full template (continuity + an
    amplitude-stable fit) and multiplies only the *applied* correction by this
    weight, so a fabricated (inpainted) gap or a no-rejection 2-sample estimate
    is never subtracted at full strength — where leaked source/ICL flux
    otherwise turns into over-subtraction — without changing the correction in
    the well-covered field. This trades a little artifact removal in the
    lowest-coverage regions (crowded cluster cores) for not subtracting an
    unreliable correction there.

    Parameters
    ----------
    frames : list of (H, W) ndarray
        Pedestal-unmatched SCI arrays (cal-stage, flat-fielded), one per dither.
    masks : list of (H, W) bool ndarray
        True where a pixel must be excluded (DQ + moving-source mask), per frame.
    sigma, maxiters : float, int
        Per-pixel sigma-clip across the stack (rejects unmasked CRs / source
        residual that slipped through the mask).
    min_contributing : int
        Pixels with fewer than this many unmasked dithers are treated as gaps.
    interpolate_gaps : bool
        Fill gaps by nearest-neighbour inpainting (default). When ``False``,
        gaps are zeroed (no correction there) — kept for comparison only; it
        leaves hard steps at gap boundaries.
    isolate_background : bool
        Subtract a large-box background model of the (gap-filled) stack so only
        structure finer than ``isolate_box`` survives. Default ``False``.
    isolate_box, isolate_filter : int
        ``skyfit.fit_sky`` box / filter sizes for the isolation background.
    smooth_sigma : float
        Gaussian smoothing sigma (px) applied to the template; 0 disables.
        Artifacts are smooth/extended, so this trades a little resolution for
        much lower per-pixel noise injected into every exposure.
    despike_nsigma, despike_size, despike_dilate : float, int, int
        Despike the median before smoothing (see ``despike``): replace positive
        point outliers (hot/warm pixels) above ``despike_nsigma`` x the
        iteratively-clipped residual scale with the local ``despike_size``
        median, growing each spike by ``despike_dilate`` px (IPC wings).
        ``despike_nsigma = 0`` disables.
    coverage_weight : bool
        Compute the coverage-confidence weight map (default ``True``). When
        ``False``, ``diag['coverage_confidence']`` is ``None`` and the caller
        applies the template ungated (legacy behaviour).
    coverage_n_lo, coverage_n_hi : int
        Confidence ramp endpoints in units of contributing dithers: weight is 0
        at ``n_contributing <= coverage_n_lo``, ramps linearly to 1 at
        ``n_contributing >= coverage_n_hi``. Pixels below ``min_contributing``
        (inpainted) are forced to weight 0 regardless. Defaults ``(1, 3)``:
        full trust at >=3 dithers, half at 2 (no cross-dither rejection), none
        for pure inpaint.
    coverage_smooth_sigma : float
        Gaussian sigma (px) smoothing the weight map so the gate tapers
        gradually at gap edges (no step) and a salt-and-pepper coverage map
        can't re-inject structure into the correction. 0 disables smoothing.

    Returns
    -------
    template : (H, W) ndarray
        Artifact template (ungated). Fit the amplitude on this; multiply by
        ``diag['coverage_confidence']`` before subtracting.
    n_contributing : (H, W) int ndarray
        Number of unmasked dithers per pixel.
    pedestals : list of float
        The per-frame pedestal subtracted before stacking.
    diag : dict
        ``{'undefined_frac', 'n_despiked', 'coverage_confidence'}`` — fraction
        of pixels with too few contributing dithers, the number of hot-pixel
        spikes removed, and the coverage weight map to apply (``None`` when the
        gate is off).
    """
    n = len(frames)
    h, w = frames[0].shape
    pedestals = []
    stack = np.full((n, h, w), np.nan, dtype=np.float64)
    for i, (f, m) in enumerate(zip(frames, masks)):
        finite = np.isfinite(f) & ~m
        ped = float(np.median(f[finite])) if finite.any() else 0.0
        pedestals.append(ped)
        layer = f.astype(np.float64) - ped
        layer[~finite] = np.nan
        stack[i] = layer

    ma = np.ma.masked_invalid(stack)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        clipped = sigma_clip(ma, sigma=sigma, maxiters=maxiters, axis=0,
                             masked=True, cenfunc='median', stdfunc='mad_std')
        n_contributing = (~np.ma.getmaskarray(clipped)).sum(axis=0).astype(int)
        template = np.ma.median(clipped, axis=0).filled(np.nan)
    template[n_contributing < min_contributing] = np.nan
    undefined_frac = float(np.mean(n_contributing < min_contributing))

    # Fill gaps BEFORE isolation so fit_sky never sees zeros (which ring around
    # the bright artifact and dig a negative trough).
    if interpolate_gaps:
        template = inpaint_nan(template)

    # Despike BEFORE smoothing: remove detector-fixed hot/warm pixels (which the
    # cross-dither median can't reject) while they're still point-like, so
    # smoothing doesn't spread them into subtracted blobs. Works whether the
    # spike sits on blank sky or on top of the artifact.
    n_despiked = 0
    if despike_nsigma and despike_nsigma > 0:
        template, n_despiked = despike(template, size=despike_size,
                                       nsigma=despike_nsigma,
                                       dilate=despike_dilate)

    if isolate_background:
        # Lazy import: skyfit pulls in scipy/photutils helpers; keep the
        # pure-numpy parts of this module importable without them.
        from campfire_pipeline.nircam.skyfit import fit_sky
        bg_input = np.where(np.isfinite(template), template, 0.0)
        try:
            bg = fit_sky(bg_input, box_size=isolate_box,
                         filter_size=isolate_filter)
            template = template - bg
        except Exception as e:  # noqa: BLE001 — fall back to no isolation
            log(f"artifact: background isolation failed ({e}); "
                f"using raw stack template")

    if smooth_sigma and smooth_sigma > 0:
        from astropy.convolution import Gaussian2DKernel, convolve
        template = convolve(template, Gaussian2DKernel(smooth_sigma),
                            nan_treatment='interpolate', preserve_nan=False)

    # Any residual NaN (e.g. interpolate_gaps=False) -> 0: no correction there.
    template = np.where(np.isfinite(template), template, 0.0)

    # Coverage-confidence map: how many dithers actually constrained each pixel,
    # as a smooth weight in [0, 1]. The TEMPLATE is returned ungated — the caller
    # fits the per-exposure amplitude on this full template (continuity + a
    # well-posed, amplitude-stable fit) and multiplies only the *applied*
    # correction by this weight. A fabricated gap or a 2-sample (no-rejection)
    # estimate is therefore never subtracted at full strength — that is where
    # leaked source/ICL flux becomes over-subtraction — without changing the
    # correction in the well-covered field. Ramp 0->1 over
    # [coverage_n_lo, coverage_n_hi]; inpainted pixels (< min_contributing) are
    # forced to 0; Gaussian-smoothed so the taper is gradual (no step at gap
    # edges) and a peppery coverage map can't re-inject structure.
    coverage_confidence = None
    if coverage_weight:
        if coverage_n_hi <= coverage_n_lo:
            raise ValueError(
                f"coverage_n_hi ({coverage_n_hi}) must exceed coverage_n_lo "
                f"({coverage_n_lo})")
        w = np.clip((n_contributing - coverage_n_lo)
                    / float(coverage_n_hi - coverage_n_lo), 0.0, 1.0)
        w[n_contributing < min_contributing] = 0.0
        if coverage_smooth_sigma and coverage_smooth_sigma > 0:
            from scipy.ndimage import gaussian_filter
            w = np.clip(gaussian_filter(w.astype(np.float64),
                                        coverage_smooth_sigma, mode='nearest'),
                        0.0, 1.0)
        coverage_confidence = w

    return template, n_contributing, pedestals, {
        'undefined_frac': undefined_frac, 'n_despiked': n_despiked,
        'coverage_confidence': coverage_confidence}


def artifact_dq_mask(dq):
    """Pixels always excluded from the artifact stack: DO_NOT_USE + SATURATED.

    SATURATED carries no valid flux, so it must not enter the template (the
    cross-dither sigma-clip catches most but is marginal at N=4). We
    deliberately do NOT mask JUMP_DET — it is ramp-corrected (fine for
    estimation) and can blanket a whole frame (>97% of pixels on bright-target
    MSATA exposures) — nor PERSISTENCE, which is itself an artifact we want to
    *measure*. Transient cosmic rays are rejected by the sigma-clip across
    dithers and by the moving-source mask.
    """
    from jwst.datamodels import dqflags
    bits = dqflags.pixel['DO_NOT_USE'] | dqflags.pixel['SATURATED']
    return np.bitwise_and(dq, bits) != 0


def build_sky_model(frames, shifts):
    """Per-frame sky model via a sky-aligned median (detector-fixed rejected).

    Each frame is shifted by ``shifts[i] = (dy, dx)`` onto frame 0's *sky* grid
    and median-combined: sky-fixed signal (sources, ICL) aligns across frames
    and survives the median, while the detector-fixed artifact lands at a
    different place in each shifted frame and is rejected. That sky median is
    reprojected back to each frame's detector grid and returned, so
    ``frame - sky_model`` is a source-free residual carrying the artifact.

    Pure (numpy + ``scipy.ndimage.shift``). Order-1 shifts spread NaNs by ~1 px
    at gaps/edges and leave small dipole residuals at sharp sources under
    imperfect (pre-jhat) alignment — both move between frames, so they reject
    in the downstream detector-frame median.

    Returns a list of per-frame sky models (NaN where unaligned/undefined).
    """
    from scipy.ndimage import shift as ndshift
    aligned = []
    for f, (dy, dx) in zip(frames, shifts):
        aligned.append(ndshift(np.asarray(f, dtype=np.float64), (dy, dx),
                               order=1, cval=np.nan, mode='constant',
                               prefilter=False))
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        sky_ref = np.nanmedian(np.stack(aligned), axis=0)
    out = []
    for (dy, dx) in shifts:
        out.append(ndshift(sky_ref, (-dy, -dx), order=1, cval=np.nan,
                           mode='constant', prefilter=False))
    return out


def sky_source_mask(sky, nsigma=2.0, npixels=5, dilate=4):
    """Source mask from a sky model (sources present, artifact absent).

    Detecting on the sky model — not the science frame — is the trick that lets
    the skymodel path mask the source-subtraction *dipole residuals* (left by
    imperfect order-1 alignment) without masking the detector-fixed artifact,
    which by construction is not in the sky model. Dilated to cover the dipole
    wings.
    """
    from photutils.segmentation import detect_sources, detect_threshold
    from scipy.ndimage import binary_dilation
    finite = np.isfinite(sky)
    if not finite.any():
        return np.zeros(sky.shape, dtype=bool)
    data = np.where(finite, sky, 0.0)
    thr = detect_threshold(data, nsigma=nsigma, mask=~finite)
    seg = detect_sources(data, thr, npixels=npixels, mask=~finite)
    if seg is None:
        return np.zeros(sky.shape, dtype=bool)
    return binary_dilation(seg.data > 0, iterations=int(dilate))


def stationary_pixels(segs, k_unmask):
    """Detector-stationary segmentation pixels: flagged in >= ``k_unmask`` frames.

    A real source moves between dithers, so its segment is flagged at a given
    detector pixel in only one (or, for extended sources at a small throw, a
    few) exposures. A detector-fixed artifact is flagged at the *same* pixel in
    every exposure. Pixels flagged in at least ``k_unmask`` of the per-exposure
    segmentation masks are therefore the artifact (or static features) and are
    *unmasked* — kept in the stack so the artifact is measured rather than
    erased by its own detection. With ``k_unmask = N`` (all frames) only
    perfectly-stationary structure is unmasked; a compact moving source at a
    realistic dither throw never reaches it.

    Parameters
    ----------
    segs : list of (H, W) bool ndarray
        Per-exposure source-segmentation masks (True where flagged).
    k_unmask : int
        Minimum number of frames a pixel must be flagged in to count as
        detector-stationary.

    Returns
    -------
    (H, W) bool ndarray
        True where the segmentation is detector-stationary (to be unmasked).
    """
    count = np.zeros(segs[0].shape, dtype=int)
    for s in segs:
        count += s.astype(bool)
    return count >= k_unmask


def fit_template_scale(data, template, mask, c_max=3.0, support_nsigma=1.5,
                       clip_sigma=3.0, maxiters=5):
    """Per-exposure template amplitude by robust weighted least-squares.

    Solves ``c = Σ(d·t) / Σ(t²)`` over the template's support (pixels where
    ``|template|`` exceeds a robust threshold, so the fit keys on the actual
    artifact and not the vast clean background), iteratively sigma-clipping the
    residual to reject any source flux that leaked into the support. Unlike a
    MAD-of-residual minimization this stays well-posed for a *flat* artifact
    (smooth persistence / scattered light) — exactly the low-structure case
    where minimizing residual spread is degenerate. Clamped to ``[0, c_max]``:
    a missing artifact gives ``c -> 0`` (no subtraction), never a spurious
    negative that would *add* the template.

    Parameters
    ----------
    data : (H, W) ndarray
        Pedestal-subtracted SCI for this exposure.
    template : (H, W) ndarray
        Artifact template (0 outside its support).
    mask : (H, W) bool ndarray
        True where the pixel must be excluded (DQ + source mask).
    c_max : float
        Upper clamp on the fitted amplitude.
    support_nsigma : float
        Template support threshold in units of the MAD of the nonzero template
        values (0 for a flat template -> the full nonzero footprint).
    clip_sigma, maxiters : float, int
        Residual sigma-clip rejecting leaked source flux from the fit.

    Returns
    -------
    c : float
        Fitted amplitude in ``[0, c_max]`` (0 when no usable support).
    """
    nonzero = template[template != 0]
    if nonzero.size < 50:
        return 0.0
    tcut = support_nsigma * median_absolute_deviation(nonzero, ignore_nan=True)
    support = ((np.abs(template) > tcut) & ~mask
               & np.isfinite(data) & np.isfinite(template))
    if support.sum() < 50:
        return 0.0

    d = data[support].astype(np.float64)
    t = template[support].astype(np.float64)
    keep = np.ones(d.shape, dtype=bool)
    c = 0.0
    for _ in range(maxiters):
        denom = np.sum(t[keep] ** 2)
        if denom <= 0:
            return 0.0
        c = float(np.sum(d[keep] * t[keep]) / denom)
        resid = d - c * t
        # MAD -> Gaussian-equivalent sigma via the 1.4826 factor.
        sigma = 1.4826 * median_absolute_deviation(resid[keep], ignore_nan=True)
        if not np.isfinite(sigma) or sigma == 0:
            break
        newkeep = np.abs(resid) < clip_sigma * sigma
        if newkeep.sum() < 50 or np.array_equal(newkeep, keep):
            break
        keep = newkeep
    return float(np.clip(c, 0.0, c_max))


# ---------------------------------------------------------------------------
# Step wrapper (ImageModel I/O + shared tiered source mask)
# ---------------------------------------------------------------------------

def _read_dither_geometry(group_files):
    """Per-exposure ``(xoffset, yoffset)`` arcsec from canonical headers.

    Primary path is the commanded ideal-frame ``XOFFSET``/``YOFFSET`` (ext 0).
    Falls back to the ``RA_REF``/``DEC_REF`` pointing (ext 1, SCI header),
    projected to a local tangent plane in arcsec, when the commanded offsets
    are absent. Returns ``None`` if neither is available on all files.
    """
    xs, ys = [], []
    have_offset = True
    for f in group_files:
        h0 = fits.getheader(f, ext=0)
        xo, yo = h0.get('XOFFSET'), h0.get('YOFFSET')
        if xo is None or yo is None:
            have_offset = False
            break
        xs.append(float(xo))
        ys.append(float(yo))
    if have_offset:
        return np.array(xs), np.array(ys)

    # Fallback: RA_REF/DEC_REF projected to arcsec about the group mean.
    ras, decs = [], []
    for f in group_files:
        h1 = fits.getheader(f, ext=('SCI', 1))
        ra, dec = h1.get('RA_REF'), h1.get('DEC_REF')
        if ra is None or dec is None:
            return None
        ras.append(float(ra))
        decs.append(float(dec))
    ras = np.array(ras)
    decs = np.array(decs)
    dec0 = np.deg2rad(decs.mean())
    x = (ras - ras.mean()) * np.cos(dec0) * 3600.0
    y = (decs - decs.mean()) * 3600.0
    return x, y


def _dither_shifts(models):
    """Per-model ``(dy, dx)`` px shift aligning each frame onto model[0]'s sky.

    Uses each exposure's GWCS (``meta.wcs``, from image2's assign_wcs — the
    commanded pointing, pre-jhat) to map frame 0's centre sky position into
    every frame's detector pixel; the offset is the translation that aligns
    the sky. A pure translation is adequate for a single dither sequence (same
    detector, negligible relative roll).
    """
    ny, nx = models[0].data.shape
    xc, yc = nx / 2.0, ny / 2.0
    ra0, dec0 = models[0].meta.wcs.pixel_to_world_values(xc, yc)
    shifts = []
    for m in models:
        xi, yi = m.meta.wcs.world_to_pixel_values(ra0, dec0)
        shifts.append((yc - float(yi), xc - float(xi)))
    return shifts


def _stamp_skip(group_files, reason):
    """Stamp ``CFP_ART = 'skipped (<reason>)'`` without touching SCI."""
    from jwst.datamodels import ImageModel
    log(f"artifact: skipping group — {reason}")
    for f in group_files:
        with ImageModel(f, memmap=False) as m:
            atomic_save(m, f,
                        header_updates=cfp.format(CFP_ART=f'skipped ({reason})'))


def artifact_step(group_key, group_files, field, step_config, overwrite=False,
                  status=None):
    """Remove detector-fixed artifacts from one (visit, detector) dither group.

    Parameters
    ----------
    group_key : str
        ``'<visit>_<spec>_<detector>'`` label for logging.
    group_files : list of str
        Canonical ``<rootname>.fits`` paths for the group's dithers.
    field : Field
    step_config : dict
        ``[nircam.artifact]`` block.
    overwrite : bool
    status : StepStatus, optional
        Pre-scanned CFP_* status cache.
    """
    # --- config + validation ------------------------------------------------
    min_dithers = int(step_config.get('min_dithers', 4))
    min_spacing = float(step_config.get('min_dither_spacing_pix', 20))
    min_contributing = int(step_config.get('min_contributing', 2))
    mask_level = step_config.get('mask', 'standard')
    if mask_level not in _VALID_MASK:
        raise ValueError(f"[nircam.artifact].mask = {mask_level!r} invalid "
                         f"(expected one of {_VALID_MASK})")
    scale_mode = step_config.get('scale', 'fit')
    if scale_mode not in _VALID_SCALE:
        raise ValueError(f"[nircam.artifact].scale = {scale_mode!r} invalid "
                         f"(expected one of {_VALID_SCALE})")
    source_rejection = step_config.get('source_rejection', 'stationary')
    if source_rejection not in _VALID_SOURCE_REJECTION:
        raise ValueError(
            f"[nircam.artifact].source_rejection = {source_rejection!r} invalid "
            f"(expected one of {_VALID_SOURCE_REJECTION})")
    sigma = float(step_config.get('sigma', 3.0))
    maxiters = int(step_config.get('maxiters', 5))
    unmask_stationary = bool(step_config.get('unmask_stationary', True))
    unmask_min_frac = float(step_config.get('unmask_min_frac', 1.0))
    interpolate_gaps = bool(step_config.get('interpolate_gaps', True))
    isolate_background = bool(step_config.get('isolate_background', False))
    isolate_box = int(step_config.get('isolate_box', 128))
    isolate_filter = int(step_config.get('isolate_filter', 3))
    smooth_sigma = float(step_config.get('template_smooth_sigma', 2.0))
    despike_nsigma = float(step_config.get('despike_nsigma', 4.0))
    despike_size = int(step_config.get('despike_size', 5))
    despike_dilate = int(step_config.get('despike_dilate', 1))
    coverage_weight = bool(step_config.get('coverage_weight', True))
    coverage_n_lo = int(step_config.get('coverage_n_lo', 1))
    coverage_n_hi = int(step_config.get('coverage_n_hi', 3))
    coverage_smooth_sigma = float(step_config.get('coverage_smooth_sigma', 3.0))
    c_max = float(step_config.get('c_max', 3.0))
    do_plot = bool(step_config.get('plot', True))

    # Skip when every member already carries CFP_ART (ensemble: all-or-none).
    if not overwrite:
        done = (all(status.has(f, 'CFP_ART') for f in group_files)
                if status is not None
                else all(cfp.has_step(f, 'CFP_ART') for f in group_files))
        if done:
            log(f"artifact: {group_key} already done (CFP_ART on all "
                f"{len(group_files)} members); skipping")
            return

    detector = os.path.basename(group_files[0]).removesuffix('.fits').split('_')[3]
    channel = _channel_of(detector)
    pixscale = PIXSCALE_ARCSEC[channel]

    # --- safeguards ---------------------------------------------------------
    geom = _read_dither_geometry(group_files)
    if geom is None:
        _stamp_skip(group_files, 'no dither geometry (XOFFSET/RA_REF missing)')
        return
    xoff, yoff = geom
    n_pos, spacing_pix = dither_positions_and_spacing(xoff, yoff, pixscale)
    patttype = fits.getheader(group_files[0], ext=0).get('PATTTYPE', '?')
    log(f"artifact: {group_key} — {len(group_files)} files, {n_pos} distinct "
        f"dither positions ({patttype}), min throw {spacing_pix:.1f} px "
        f"({channel} @ {pixscale:.4f}\"/px)")
    if n_pos < min_dithers:
        _stamp_skip(group_files,
                    f'{n_pos} positions < min_dithers={min_dithers}')
        return
    if spacing_pix < min_spacing:
        _stamp_skip(group_files,
                    f'spacing {spacing_pix:.1f}px < {min_spacing}px')
        return

    # --- load frames + build the template stack -----------------------------
    from jwst.datamodels import ImageModel, dqflags

    log(f"artifact: building template for {group_key} "
        f"(source_rejection={source_rejection}, scale={scale_mode})")
    models = [ImageModel(f, memmap=False) for f in group_files]
    sci_before = [m.data.copy() for m in models]

    # DO_NOT_USE + SATURATED are always masked (see artifact_dq_mask for why
    # JUMP_DET and PERSISTENCE are intentionally left in).
    dq_masks = [artifact_dq_mask(m.dq) for m in models]

    # The template is built from ``stack_frames`` (masked by ``stack_masks``).
    # The output always subtracts c*template from the raw cal SCI; only the
    # template-construction differs between the two source-rejection modes.
    if source_rejection == 'skymodel':
        # Subtract a sky-aligned median (sky-fixed sources + ICL) from each
        # frame, then build the template from the residuals — which carry the
        # detector-fixed artifact and no sources. The dither WCS does the
        # separation, so no source mask / stationarity heuristic is needed.
        shifts = _dither_shifts(models)
        log(f"artifact: {group_key} sky-model shifts (dy,dx) px: "
            + ', '.join(f'({dy:+.0f},{dx:+.0f})' for dy, dx in shifts))
        sky_models = build_sky_model(sci_before, shifts)
        stack_frames = [s - sk for s, sk in zip(sci_before, sky_models)]
        # Mask source positions (detected on the sky model, so the artifact is
        # not masked) to drop the alignment dipole residuals from the stack.
        src_masks = [sky_source_mask(sk) for sk in sky_models]
        nsrc = int(np.mean([m.mean() for m in src_masks]) * 100)
        log(f"artifact: {group_key} skymodel masking ~{nsrc}% (source dipoles)")
        stack_masks = [dq | sm for dq, sm in zip(dq_masks, src_masks)]
    else:  # 'stationary'
        from campfire_pipeline.nircam.steps.striping import _build_srcmask
        if mask_level == 'none':
            stack_masks = dq_masks
        else:
            extra = 4 if mask_level == 'aggressive' else 0
            segs = [_build_srcmask(m, extra_dilation=extra) > 0 for m in models]
            if unmask_stationary:
                # Keep detector-stationary segments (the artifact) in the stack;
                # mask only segments that move between dithers (real sources).
                k_unmask = max(2, int(np.ceil(unmask_min_frac * len(segs))))
                stationary = stationary_pixels(segs, k_unmask)
                log(f"artifact: {group_key} unmasking "
                    f"{int(stationary.sum())} detector-stationary px "
                    f"(flagged in >= {k_unmask}/{len(segs)} dithers)")
                stack_masks = [dq | (s & ~stationary)
                               for dq, s in zip(dq_masks, segs)]
            else:
                stack_masks = [dq | s for dq, s in zip(dq_masks, segs)]
        stack_frames = sci_before

    template, n_contrib, pedestals, diag = build_artifact_template(
        stack_frames, stack_masks, sigma=sigma, maxiters=maxiters,
        min_contributing=min_contributing, interpolate_gaps=interpolate_gaps,
        isolate_background=isolate_background, isolate_box=isolate_box,
        isolate_filter=isolate_filter, smooth_sigma=smooth_sigma,
        despike_nsigma=despike_nsigma, despike_size=despike_size,
        despike_dilate=despike_dilate,
        coverage_weight=coverage_weight, coverage_n_lo=coverage_n_lo,
        coverage_n_hi=coverage_n_hi, coverage_smooth_sigma=coverage_smooth_sigma,
    )
    log(f"artifact: {group_key} template built; undefined "
        f"{diag['undefined_frac'] * 100:.1f}% of pixels; "
        f"despiked {diag['n_despiked']} hot pixels")
    # The coverage gate scales only the APPLIED correction (not the fit): the
    # amplitude is fit on the full ungated template (stable, unchanged from the
    # no-gate case) and the gated template is what's subtracted, so the
    # well-covered field is untouched and only low-coverage regions taper off.
    conf = diag.get('coverage_confidence')
    applied_template = template if conf is None else template * conf
    if conf is not None:
        log(f"artifact: {group_key} coverage gate — "
            f"{np.mean(conf < 0.5) * 100:.1f}% of pixels weight<0.5, "
            f"{np.mean(conf <= 0.0) * 100:.1f}% fully gated")

    # --- subtract (optionally per-exposure scaled) + write ------------------
    # Fit the amplitude on the same frame the template was built from
    # (residual for skymodel, raw for stationary), then subtract from raw SCI.
    bpflag = dqflags.pixel['DO_NOT_USE']
    coeffs = []
    for m, raw, fitframe, mask, ped, f in zip(
            models, sci_before, stack_frames, stack_masks, pedestals,
            group_files):
        if scale_mode == 'fit':
            c = fit_template_scale(fitframe.astype(np.float64) - ped, template,
                                   mask, c_max=c_max)
        else:
            c = 1.0
        coeffs.append(c)

        outsci = raw - c * applied_template
        outsci[raw == 0] = 0
        wnan = np.isnan(outsci)
        outsci[wnan] = 0
        m.dq[wnan] = np.bitwise_or(m.dq[wnan], bpflag)
        m.data = outsci

        from stdatamodels import util as stutil
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        m.history.append(stutil.create_history_entry(
            f'Removed intra-visit artifact template (scale={scale_mode}, '
            f'c={c:.3f}); {now}'))

        cfp_value = (f'srcrej={source_rejection}, scale={scale_mode}, '
                     f'c={c:.3f}, N={n_pos}, spacing={spacing_pix:.1f}px, '
                     f'interp={int(interpolate_gaps)}, '
                     f'isolate={int(isolate_background)}, '
                     f'cov={int(coverage_weight)}')
        atomic_save(m, f, header_updates=cfp.format(CFP_ART=cfp_value))
        log(f"artifact: {os.path.basename(f)} — c={c:.3f}")

    rootbase = '_'.join(os.path.basename(group_files[0])
                        .removesuffix('.fits').split('_')[:2] + [detector])
    out_dir = os.path.dirname(group_files[0])

    # Write the applied (coverage-gated) template as a QA reference product (not
    # a jw* file, so the field exposure globs ignore it) — this is exactly what
    # was subtracted, per unit c.
    tmpl_path = os.path.join(out_dir, f'artifact_template_{rootbase}.fits')
    fits.PrimaryHDU(applied_template.astype(np.float32)).writeto(
        tmpl_path, overwrite=True)

    if do_plot:
        _plot_artifact(group_key, sci_before, applied_template, coeffs,
                       n_contrib,
                       os.path.join(out_dir, f'artifact_{rootbase}.pdf'))

    for m in models:
        m.close()
    log(f"artifact: {group_key} done — c={[f'{c:.2f}' for c in coeffs]}")


def _plot_artifact(group_key, sci_before, template, coeffs, n_contrib,
                   save_file):
    """Diagnostic: representative before/after, template, coverage map."""
    import matplotlib.pyplot as plt
    from astropy.visualization import ImageNormalize, ZScaleInterval

    # Representative exposure = the one with the largest fitted amplitude.
    i = int(np.argmax(coeffs)) if len(coeffs) else 0
    before = sci_before[i]
    after = before - coeffs[i] * template

    norm = ImageNormalize(before, interval=ZScaleInterval())
    tnorm = ImageNormalize(template, interval=ZScaleInterval())
    fig, axes = plt.subplots(2, 2, figsize=(10, 10), tight_layout=True)
    for ax in axes.ravel():
        ax.axis('off')
    axes[0, 0].imshow(before, origin='lower', cmap='Greys', norm=norm,
                      interpolation='none')
    axes[0, 0].set_title(f'before (c={coeffs[i]:.2f})')
    axes[0, 1].imshow(after, origin='lower', cmap='Greys', norm=norm,
                      interpolation='none')
    axes[0, 1].set_title('after')
    axes[1, 0].imshow(template, origin='lower', cmap='RdBu_r', norm=tnorm,
                      interpolation='none')
    axes[1, 0].set_title('artifact template')
    axes[1, 1].imshow(n_contrib, origin='lower', cmap='viridis',
                      interpolation='none')
    axes[1, 1].set_title('contributing dithers')
    fig.suptitle(group_key)
    fig.savefig(save_file)
    plt.close(fig)
    log(f"artifact: saved {os.path.basename(save_file)}")
