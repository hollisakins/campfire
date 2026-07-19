"""JHAT-style 1-NN + offset-histogram consensus matcher for the ``align`` phase.

:class:`OffsetHistogramMatch` is the ``tweakwcs`` ``MatchCatalogs`` callable the
coarse solve uses to pair pooled image sources with the reference catalog. It is
a faithful port of JHAT's matching algorithm (``jhat.st_wcs_align
.find_good_refcat_matches`` + ``histogram_cut``), which is structurally
different from ``tweakwcs.XYXYMatch``:

* ``XYXYMatch`` *enumerates candidate pairs* (``stsci.stimage.xyxymatch``) and
  sizes its output by the detection count — on real extragalactic catalogs,
  where detections and refcat rows are the same (clustered) galaxies and the
  refcat resolves substructure, the number of reference sources finding a
  partner can exceed the detection count and the matcher dies with
  ``MatchSourceConfusionError`` (39% of COSMOS LW exposures).
* JHAT instead pairs **every** image source with its single nearest reference
  (unbounded 1-NN — most pairs are wrong at this stage *by design*) and then
  finds the true correspondence by **consensus**: true pairs pile into one
  narrow peak of the pairwise-offset histogram while false pairs scatter
  ~uniformly. Nothing is enumerated, nothing can overflow.

The consensus stage mirrors JHAT exactly: per axis (``histocut_order``, dx
first by default), a rotation-slope scan subtracts a linear ramp of dx vs the
cross coordinate (small-angle rotation), histograms the de-rotated offsets in
``binsize_px`` bins, Gaussian-smooths, and keeps the slope whose peak is
tallest; survivors of a rough cut around the peak (``nfwhm``·FWHM clamped to
``[rough_cut_px_min, rough_cut_px_max]``) are sigma-clipped
(median-anchored, ``nsigma``) and the second axis repeats the cut on what is
left. The returned pairs feed ``tweakwcs``'s robust rigid fit — the matcher
selects correspondences, the fit still decides the transform.

**Pooling is native.** The matcher sees the tangent-plane catalogs *after*
``tweakwcs`` concatenates a pool's detectors (shared ``group_id``), so every
detector's pairs accumulate into ONE shared offset histogram — more detectors
mean a stronger consensus peak. This is precisely the sparse-field robustness
argument for pooling, and it is why this port strengthens rather than replaces
the pooled design.

**Gross-offset stage.** JHAT's 1-NN needs the true counterpart to *be* the
nearest neighbour, which holds only when the WCS error is below the local
source spacing (JWST pointing is normally sub-arcsec). To keep the ability to
recover acquisition failures (tens of arcsec), an optional first stage finds
the gross translation as the peak of the 2-D pairwise-offset histogram within
``searchrad`` — the same idea ``XYXYMatch(use2dhist=True)`` uses for its
initial estimate (bake-off-validated at 97–100%), implemented here by direct
accumulation so no pair list is ever materialized. ``searchrad=None`` skips
the stage (the iterate passes, already roughly aligned, don't need it).

Units: the ``*_px`` knobs are **image pixels** — the validated JHAT COSMOS
configuration carries over verbatim — converted per call via the ``tp_pscale``
the ``tweakwcs`` machinery passes (the pool's detector pixel size in
tangent-plane arcsec, so SW/LW each get physically correct values).
``searchrad`` and ``d2d_max`` are arcsec (matching JHAT's ``d2d_max``).
"""

import numpy as np
from tweakwcs.matchutils import MatchCatalogs

from campfire_pipeline.common.io import log

_EMPTY = (np.array([], dtype=int), np.array([], dtype=int))

# Fewest surviving pairs worth returning (JHAT's floor before the fit).
_MIN_PAIRS = 3

# Gross-stage 2-D histogram bin (arcsec). Precision only needs to bring the
# residual offset under d2d_max/NN-spacing for the 1-NN stage; the ±2-bin
# centroid refinement below gives sub-bin accuracy.
_GROSS_BIN_ARCSEC = 0.5

# Cap on 1-D histogram length; if the (unclamped) offset span demands more
# bins, the bin size is coarsened. Only reachable with d2d_max=None on a
# pathologically wide offset distribution.
_MAX_BINS = 200_000


def _smoothed_hist_peak(d, binsize, gaussian_sigma):
    """Peak of the Gaussian-smoothed histogram of *d*: ``(center, height,
    fwhm)``.

    Mirrors JHAT's ``find_binmax_for_slope``: plain ``np.histogram`` in
    *binsize* bins, smoothed by an (unnormalized, like pandas' rolling-gaussian
    sum) Gaussian kernel of *gaussian_sigma*, peak located on the smoothed
    curve. ``fwhm`` is measured by walking to half-height on each side
    (clipped at the histogram edges).
    """
    lo = float(np.min(d))
    hi = float(np.max(d))
    span = max(hi - lo, binsize)
    nbins = int(np.ceil(span / binsize))
    if nbins > _MAX_BINS:
        binsize = span / _MAX_BINS
        nbins = _MAX_BINS
    hist, edges = np.histogram(d, bins=nbins, range=(lo, lo + nbins * binsize))

    sigma_bins = max(gaussian_sigma / binsize, 1e-3)
    half = max(int(np.ceil(3.0 * sigma_bins)), 1)
    t = np.arange(-half, half + 1, dtype=float)
    kernel = np.exp(-0.5 * (t / sigma_bins) ** 2)
    # centered window of the full (zero-padded) convolution — NOT mode='same',
    # which returns the KERNEL's length whenever the kernel outgrows a short
    # histogram, desynchronizing the peak index from `edges`
    conv = np.convolve(hist.astype(float), kernel, mode='full')
    smoothed = conv[half:half + hist.size]

    peak_i = int(np.argmax(smoothed))
    height = float(smoothed[peak_i])
    center = float(0.5 * (edges[peak_i] + edges[peak_i + 1]))

    # half-height walk for the FWHM
    half_h = 0.5 * height
    left = peak_i
    while left > 0 and smoothed[left - 1] >= half_h:
        left -= 1
    right = peak_i
    while right < smoothed.size - 1 and smoothed[right + 1] >= half_h:
        right += 1
    fwhm = max(right - left + 1, 1) * binsize
    return center, height, fwhm


def _sigma_clip_median(values, mask, nsigma, nitmax=10, first_percentile=75.0):
    """JHAT-style iterated clip of ``values[mask]``; returns the refined mask.

    Mirrors ``calcaverage_sigmacutloop``: the first iteration keeps the
    *first_percentile* smallest |residuals| about the median (robust against a
    heavy false-pair floor), then iterates a median-anchored ``nsigma``·std
    clip to convergence (≤ *nitmax* passes).
    """
    idx = np.flatnonzero(mask)
    d = values[idx]
    if d.size < _MIN_PAIRS:
        return mask
    res = np.abs(d - np.median(d))
    good = res <= np.percentile(res, first_percentile)
    if np.count_nonzero(good) < 2:
        return mask
    for _ in range(int(nitmax)):
        center = np.median(d[good])
        std = np.std(d[good], ddof=1)
        if not np.isfinite(std) or std <= 0:
            break
        new = np.abs(d - center) <= nsigma * std
        if np.count_nonzero(new) < 2 or np.array_equal(new, good):
            break
        good = new
    out = np.zeros_like(mask)
    out[idx[good]] = True
    return out


class OffsetHistogramMatch(MatchCatalogs):
    """Match pooled tangent-plane catalogs by 1-NN + offset-histogram consensus.

    Instances are passed as the ``match=`` callable to ``tweakwcs.align_wcs``.
    ``__call__`` follows the ``MatchCatalogs`` contract: it receives the
    reference and (pooled) image catalogs — astropy ``Table``s carrying
    ``TPx``/``TPy`` tangent-plane columns, arcsec for ``JWSTWCSCorrector`` —
    and returns ``(ref_idx, im_idx)`` of matched rows, reference indices first.

    Parameters
    ----------
    searchrad : float or None
        Arcsec. When set, a gross-translation stage first locates the peak of
        the 2-D pairwise-offset histogram within this radius and pre-shifts
        the image catalog by it, so acquisition-failure offsets far beyond the
        1-NN capture range are recovered. ``None`` skips the stage (use for
        iterate passes that start from an already-corrected WCS).
    d2d_max : float or None
        Arcsec. Drop 1-NN pairs farther than this (after the gross shift) —
        JHAT's ``d2d_max`` pre-trim. ``None`` keeps every pair (the histogram
        consensus still selects; the cut just thins the false-pair floor).
    binsize_px, gaussian_sigma_px : float
        Offset-histogram bin size and Gaussian smoothing sigma, in image
        pixels (JHAT's ``binsize_px``/``gaussian_sigma_px``; converted with
        ``tp_pscale``).
    rough_cut_px_min, rough_cut_px_max : float
        Clamp (image pixels) on the rough cut half-width around the histogram
        peak, which is ``nfwhm`` × the peak's FWHM before clamping. The
        validated COSMOS configuration pins both to 2.5 px.
    nfwhm : float
        Rough-cut half-width in units of the peak FWHM (JHAT ``Nfwhm``).
    nsigma : float
        Sigma-clip threshold for the post-rough-cut iterated clip
        (JHAT ``d_rotated_Nsigma``).
    histocut_order : str
        ``'dxdy'`` (default, the validated order) cuts dx-vs-y first then
        dy-vs-x; ``'dydx'`` swaps.
    slope_max : float
        Half-range of the rotation-slope scan, dimensionless (offset change
        per unit cross coordinate ≈ rotation in radians). JHAT's default
        ``10/2048`` px/px ≈ ±0.28°.
    slope_nsteps : int
        Number of slope-scan steps across ``[-slope_max, +slope_max]``.
    """

    def __init__(self, *, searchrad=None, d2d_max=1.5, binsize_px=0.02,
                 gaussian_sigma_px=0.2, rough_cut_px_min=2.5,
                 rough_cut_px_max=2.5, nfwhm=2.5, nsigma=3.0,
                 histocut_order='dxdy', slope_max=10.0 / 2048.0,
                 slope_nsteps=200):
        if histocut_order not in ('dxdy', 'dydx'):
            raise ValueError(f"histocut_order must be 'dxdy' or 'dydx', "
                             f"got {histocut_order!r}")
        self.searchrad = searchrad
        self.d2d_max = d2d_max
        self.binsize_px = float(binsize_px)
        self.gaussian_sigma_px = float(gaussian_sigma_px)
        self.rough_cut_px_min = float(rough_cut_px_min)
        self.rough_cut_px_max = float(rough_cut_px_max)
        self.nfwhm = float(nfwhm)
        self.nsigma = float(nsigma)
        self.histocut_order = histocut_order
        self.slope_max = float(slope_max)
        self.slope_nsteps = int(slope_nsteps)

    # -- gross translation (2-D offset histogram) ---------------------------

    def _gross_shift(self, tree, ref_xy, im_xy):
        """Peak of the 2-D pairwise-offset histogram within ``searchrad``:
        ``(dx0, dy0)`` arcsec, or ``None`` when no pair exists.

        Offsets are accumulated straight into the histogram (chunked over
        image sources), so — unlike a pair-enumerating matcher — memory is
        bounded by the histogram, not the pair count. The peak is refined by
        an intensity-weighted centroid over its ±2-bin neighbourhood.
        """
        r = float(self.searchrad)
        nbins = max(int(np.ceil(2.0 * r / _GROSS_BIN_ARCSEC)), 4)
        edges = np.linspace(-r, r, nbins + 1)
        acc = np.zeros((nbins, nbins), dtype=float)

        found = False
        for start in range(0, im_xy.shape[0], 256):
            chunk = im_xy[start:start + 256]
            neighbours = tree.query_ball_point(chunk, r=r)
            offs = [ref_xy[nbrs] - pt
                    for pt, nbrs in zip(chunk, neighbours) if nbrs]
            if not offs:
                continue
            found = True
            offs = np.concatenate(offs)
            acc += np.histogram2d(offs[:, 0], offs[:, 1],
                                  bins=(edges, edges))[0]
        if not found:
            return None

        # light smoothing so a peak split across bin edges still wins
        k = np.array([0.25, 0.5, 0.25])
        acc = np.apply_along_axis(np.convolve, 0, acc, k, 'same')
        acc = np.apply_along_axis(np.convolve, 1, acc, k, 'same')

        i, j = np.unravel_index(int(np.argmax(acc)), acc.shape)
        centers = 0.5 * (edges[:-1] + edges[1:])
        i0, i1 = max(i - 2, 0), min(i + 3, nbins)
        j0, j1 = max(j - 2, 0), min(j + 3, nbins)
        patch = acc[i0:i1, j0:j1]
        total = patch.sum()
        if total <= 0:
            return None
        dx0 = float((patch.sum(axis=1) * centers[i0:i1]).sum() / total)
        dy0 = float((patch.sum(axis=0) * centers[j0:j1]).sum() / total)
        return dx0, dy0

    # -- per-axis histogram cut (rotation scan + rough cut + sigma clip) ----

    def _histogram_cut(self, d, c, mask, *, binsize, gaussian_sigma,
                       rough_min, rough_max):
        """One JHAT ``histogram_cut``: refine *mask* by the consensus peak of
        offsets *d* against cross coordinate *c* (rotation-slope scan)."""
        idx = np.flatnonzero(mask)
        if idx.size < _MIN_PAIRS:
            return mask
        d_sel = d[idx]
        c_sel = c[idx]
        # rotate about the pool centre (JHAT: the detector centre) so the scan
        # shifts the histogram as little as possible
        c0 = 0.5 * (float(np.min(c_sel)) + float(np.max(c_sel)))
        c_rel = c_sel - c0

        best = None   # (height, slope, center, fwhm)
        for slope in np.linspace(-self.slope_max, self.slope_max,
                                 self.slope_nsteps + 1):
            center, height, fwhm = _smoothed_hist_peak(
                d_sel - slope * c_rel, binsize, gaussian_sigma)
            if best is None or height > best[0]:
                best = (height, slope, center, fwhm)

        _, slope, center, fwhm = best
        rough = float(np.clip(self.nfwhm * fwhm, rough_min, rough_max))
        d_rot = d - slope * (c - c0)
        rough_mask = mask & (np.abs(d_rot - center) <= rough)
        if np.count_nonzero(rough_mask) < _MIN_PAIRS:
            return rough_mask
        return _sigma_clip_median(d_rot, rough_mask, self.nsigma)

    # -- MatchCatalogs entry point ------------------------------------------

    def __call__(self, refcat, imcat, tp_pscale=1.0, tp_units=None, **kwargs):
        from scipy.spatial import cKDTree

        for cat, side in ((refcat, 'reference'), (imcat, 'image')):
            for col in ('TPx', 'TPy'):
                if col not in cat.colnames:
                    raise KeyError(
                        f"OffsetHistogramMatch: {side} catalog is missing the "
                        f"'{col}' column (tangent-plane coordinates).")
        ref_xy = np.column_stack([np.asarray(refcat['TPx'], dtype=float),
                                  np.asarray(refcat['TPy'], dtype=float)])
        im_xy = np.column_stack([np.asarray(imcat['TPx'], dtype=float),
                                 np.asarray(imcat['TPy'], dtype=float)])
        # A starved pool must yield zero matches so the solve rejects to
        # NOT_ALIGNED — never crash the align worker.
        if len(ref_xy) < _MIN_PAIRS or len(im_xy) < _MIN_PAIRS:
            return _EMPTY

        pscale = float(tp_pscale) if tp_pscale else 1.0
        binsize = self.binsize_px * pscale
        gaussian_sigma = self.gaussian_sigma_px * pscale
        rough_min = self.rough_cut_px_min * pscale
        rough_max = self.rough_cut_px_max * pscale

        tree = cKDTree(ref_xy)

        shift = np.zeros(2)
        if self.searchrad is not None and float(self.searchrad) > 0:
            gross = self._gross_shift(tree, ref_xy, im_xy)
            if gross is None:
                log(f"OffsetHistogramMatch: no reference source within "
                    f"{self.searchrad:.0f}\" of any image source; no match.")
                return _EMPTY
            shift = np.asarray(gross)

        # Unbounded 1-NN: every image source pairs with its closest reference.
        # Most pairs are wrong at this stage by design — consensus decides.
        d2d, nn = tree.query(im_xy + shift, k=1)
        mask = np.isfinite(d2d)
        if self.d2d_max is not None:
            mask &= d2d <= float(self.d2d_max)
        if np.count_nonzero(mask) < _MIN_PAIRS:
            return _EMPTY

        # Pairwise offsets in the gross-corrected frame + the image-side
        # coordinates the rotation ramp is scanned against.
        dx = ref_xy[nn, 0] - (im_xy[:, 0] + shift[0])
        dy = ref_xy[nn, 1] - (im_xy[:, 1] + shift[1])
        im_x = im_xy[:, 0]
        im_y = im_xy[:, 1]

        if self.histocut_order == 'dxdy':
            axes = ((dx, im_y), (dy, im_x))
        else:
            axes = ((dy, im_x), (dx, im_y))
        for d, c in axes:
            mask = self._histogram_cut(
                d, c, mask, binsize=binsize, gaussian_sigma=gaussian_sigma,
                rough_min=rough_min, rough_max=rough_max)

        im_idx = np.flatnonzero(mask)
        if im_idx.size < _MIN_PAIRS:
            return _EMPTY
        return nn[im_idx].astype(int), im_idx.astype(int)
