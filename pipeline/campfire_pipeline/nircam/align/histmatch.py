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
the stage (the iterate passes, already roughly aligned, don't need it). The
candidate gross shift is then VETTED before it is adopted — it must not lose
tight-radius 1-NN pairs relative to the input WCS
(:meth:`OffsetHistogramMatch._vet_gross_shift`), because on a sparse catalog the
argmax can lock onto a clustering-scale peak that is internally self-consistent:
the 1-NN re-pairing, the ``d2d_max`` window and the fit's ``rmse`` all live
inside the false correspondence set, so a WCS tens of arcsec wrong reports
convergence.

Units: the ``*_px`` knobs are **image pixels** — the validated JHAT COSMOS
configuration carries over verbatim — converted per call via the ``tp_pscale``
the ``tweakwcs`` machinery passes (the pool's detector pixel size in
tangent-plane arcsec, so SW/LW each get physically correct values).
``searchrad`` and ``d2d_max`` are arcsec (matching JHAT's ``d2d_max``).

**Peak confidence.** ``tweakwcs`` calls the matcher through ``__call__`` and only
the surviving row indices escape, so the consensus peaks' own numbers — which is
what tells a decisive lock from an ambiguous one — cannot be returned. They are
stashed on the instance in :attr:`OffsetHistogramMatch.diag` instead (reset per
call), for the solve to lift onto its ``GroupSolution`` and the I/O layer to
stamp into the header. This is the *independent* half of the align diagnostics:
the solve's own residual is measured on the very pairs the matcher selected, so a
confident-looking residual on a wrong lock is indistinguishable from a right one
— the peak contrast is not, because a wrong lock is a low-contrast one.
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

# Half-width (in bins) of the winning gross peak's neighbourhood — the same
# +-2-bin patch the centroid refinement uses, excluded when hunting the runner-up
# so the runner-up is a genuinely different peak and not the winner's own skirt.
_GROSS_PEAK_HALFWIDTH_BINS = 2

# Radius (IMAGE PIXELS, scaled per pool by tp_pscale) at which the gross shift is
# vetted — the consensus stage's own rough-cut scale, ~0.157" LW / 0.078" SW.
# Deliberately a module constant rather than a config knob: re-tuning
# ``rough_cut_px_*`` must not be able to silently weaken the gross-stage guard.
_GROSS_TIGHT_PX = 2.5


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
    delta_mag_lim : (float, float) or None
        Keep a pair only if ``image_mag − refcat_mag`` lies in this window
        (JHAT's ``delta_mag_lim``; its validated COSMOS value is ``[-3, 4]``).
        Applied only to pairs where *both* mags are finite — a source or
        reference without a magnitude is never punished for it. Requires
        *image_mags* (``tweakwcs`` drops brightness columns when pooling, so
        image mags ride the surviving ``id`` column via this lookup).
    image_mags : dict or None
        ``{id: calibrated AB mag}`` for the pooled image sources, keyed by the
        ``id`` values the solve assigned to each detector catalog.
    refcat_mag_col : str
        Reference-catalog magnitude column (``'mag'`` in campfire-refcat-v1).
    gross_min_keep_frac : float
        Fraction of the input WCS's tight-radius 1-NN pairs a candidate gross
        shift must retain to be adopted (see :meth:`_vet_gross_shift`). ``0``
        disables the check and restores the unconditional pre-guard behaviour —
        for an A/B arm, not for production. Measured on real pools, a pool that
        NEEDS the prior scores 209 and one that does not scores <= 0.32, so the
        default 0.8 is not delicate.

    Attributes
    ----------
    diag : dict
        Peak-confidence numbers from the **most recent** ``__call__`` (cleared at
        the top of each call, so it never mixes two passes). Keys, all optional
        (a stage that did not run contributes nothing):

        ``gross_peak``, ``gross_runner_up``, ``gross_contrast``, ``gross_mass``
            Gross 2-D offset histogram (``searchrad`` passes only): the winning
            bin's smoothed height, the tallest height outside its +-2-bin
            neighbourhood, their ratio, and the pair count in the winner's
            neighbourhood (the mass the sub-bin centroid was measured from). A
            contrast near 1 means a second gross offset was nearly as popular —
            the clustering-scale false peak that dragged the COSMOS A2/A3
            exposures.
        ``dx_peak`` / ``dy_peak``, ``dx_fwhm_arcsec`` / ``dy_fwhm_arcsec``,
        ``dx_contrast`` / ``dy_contrast``
            Per-axis consensus peak from the rotation-slope scan: the winning
            slope's smoothed peak height, that peak's FWHM in tangent-plane
            arcsec, and its height over the tallest peak from a *well-separated*
            slope (see :meth:`_histogram_cut`). A ``nan`` contrast means the scan
            was degenerate — no slope was distinguishable from the winner — which
            is what a collapsed ``hist_binsize_arcsec`` produces.
        ``gross_tight_before`` / ``gross_tight_after``, ``gross_keep_ratio``
            Image sources with a reference inside ``_GROSS_TIGHT_PX`` pixels
            BEFORE and AFTER the candidate gross shift, and their ratio
            (:meth:`_vet_gross_shift`). Recorded on every ``searchrad`` pass
            whether or not the shift was declined, so a survey can ask "did an
            aligned exposure ship a gross shift that cost it matches?" from the
            header alone. A ``nan`` ratio means there were no tight pairs to
            lose — the acquisition-failure regime, where the shift is adopted.
        ``hist_binsize_arcsec``
            The consensus histogram bin as actually used (``binsize_px`` ×
            ``tp_pscale``). Expect a small fraction of an arcsec; a value near or
            above the offset distribution's own width means the whole consensus
            stage collapsed to one bin and selected nothing.
    """

    def __init__(self, *, searchrad=None, d2d_max=1.5, binsize_px=0.02,
                 gaussian_sigma_px=0.2, rough_cut_px_min=2.5,
                 rough_cut_px_max=2.5, nfwhm=2.5, nsigma=3.0,
                 histocut_order='dxdy', slope_max=10.0 / 2048.0,
                 slope_nsteps=200, delta_mag_lim=None, image_mags=None,
                 refcat_mag_col='mag', gross_min_keep_frac=0.8):
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
        self.delta_mag_lim = delta_mag_lim
        self.image_mags = image_mags
        self.refcat_mag_col = refcat_mag_col
        self.gross_min_keep_frac = float(gross_min_keep_frac)
        # Peak-confidence stash for the last __call__ (see the class docstring):
        # the MatchCatalogs contract lets only row indices out, so the numbers
        # ride the instance. An instance reused across coarse passes is
        # overwritten each call; the solve snapshots the pass it keeps.
        self.diag = {}

    # -- gross translation (2-D offset histogram) ---------------------------

    def _gross_shift(self, tree, ref_xy, im_xy):
        """Peak of the 2-D pairwise-offset histogram within ``searchrad``:
        ``(dx0, dy0)`` arcsec, or ``None`` when no pair exists.

        Offsets are accumulated straight into the histogram (chunked over
        image sources), so — unlike a pair-enumerating matcher — memory is
        bounded by the histogram, not the pair count. The peak is refined by
        an intensity-weighted centroid over its ±2-bin neighbourhood.

        Records the winner's height, the runner-up height (tallest bin outside
        that same neighbourhood), their contrast and the neighbourhood mass in
        ``self.diag`` — the triage numbers for a low-contrast (ambiguous) lock.
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
        h = _GROSS_PEAK_HALFWIDTH_BINS
        i0, i1 = max(i - h, 0), min(i + h + 1, nbins)
        j0, j1 = max(j - h, 0), min(j + h + 1, nbins)
        patch = acc[i0:i1, j0:j1]
        total = patch.sum()

        # Runner-up: the tallest bin OUTSIDE the winner's neighbourhood, so a
        # peak straddling bin edges cannot masquerade as its own rival.
        rival = acc.copy()
        rival[i0:i1, j0:j1] = 0.0
        peak = float(acc[i, j])
        runner_up = float(rival.max())
        self.diag.update(
            gross_peak=peak, gross_runner_up=runner_up,
            gross_contrast=(peak / runner_up if runner_up > 0
                            else float('nan')),
            gross_mass=float(total))

        if total <= 0:
            return None
        dx0 = float((patch.sum(axis=1) * centers[i0:i1]).sum() / total)
        dy0 = float((patch.sum(axis=0) * centers[j0:j1]).sum() / total)
        return dx0, dy0

    def _vet_gross_shift(self, tree, im_xy, gross, r_tight):
        """The gross shift, or ``(0, 0)`` when adopting it would LOSE tight
        1-NN pairs.

        The gross stage exists for exactly one purpose — to make the true
        counterpart *be* the nearest neighbour, which 1-NN needs and which is
        false after an acquisition failure — so judge it on exactly that and
        nothing else. Count image sources with a reference inside *r_tight*
        with and without the candidate shift; keep the shift only if it does
        not lose them. The test is RELATIVE, so it carries no per-filter value
        and nothing to remember.

        This is the guard the stage never had. ``argmax`` over a
        ``2*searchrad`` square of 0.5" bins is a maximum-likelihood estimate
        over a huge search volume, and its risk grows as the fraction of true
        counterparts falls: on a sparse catalog a clustering-scale peak can
        outvote the true one, and the wrong peak is INTERNALLY CONSISTENT — the
        1-NN re-pairing below happens at ``im_xy + shift``, ``d2d_max``
        confines the survivors to the false offset, and the fit's ``rmse`` is
        measured on those same pairs, so it reports convergence. A wrong peak
        cannot, however, manufacture tight counterparts, and it destroys the
        ones the input WCS already had.

        What the ratio actually separates is **"this pool needs a gross prior"**
        from **"it does not"** — not "clean" from "mis-locked". Measured on real
        pools at the shipped radius (jobs 871040 / 871048; 2 detectors per pool)::

            NEEDED     COSMOS f356w d3, real acquisition failure      2 -> 418   209.0
            HARMFUL    EGS f470n, 10 sparse pools                  43-65 -> 1-16  0.019-0.32
            HARMFUL    EGS f460m, 6 sparse pools                  87-141 -> 6-11  0.065-0.13
            REDUNDANT  COSMOS f356w d1/d2, clean and dense       1248 -> 41       0.033

        The clean pools land LOW, not near 1: the gross proposal is itself only
        good to ~0.25" (0.5" histogram bins, a +-2-bin centroid over a clustered
        skirt), so even a CORRECT proposal displaces sources out of a 0.157"
        radius. That is not a false positive — a pool whose input WCS already
        has 1248 tight counterparts does not need a translation prior, and
        declining it there is a measured no-op: both arms returned the same WCS
        and the same 16.4 / 14.8 mas independent astrometry.

        So the decision the threshold has to make is ``>= 209`` versus
        ``<= 0.32``, a gap of ~650x, and 0.8 sits in the middle of it.

        **Radius.** Swept 0.05"-1.0" on all three regimes (job 871048): every
        radius up to ~0.25" gives the same verdict everywhere, and the NEEDED
        case stays >= 19 even at 1.0". Above ~0.5" the discriminator dies — a
        mis-lock's random pairs refill the wider annulus (f470n at 0.5":
        57 -> 57, ratio 1.0) and the shift is wrongly kept. 2.5 px = 0.157" LW
        sits in the middle of the usable window on a log scale, and is below it
        for SW (0.078") where the pixels — and the astrometry — are finer.

        **Declining is cheap, adopting a wrong shift is not.** Falling back to
        ``(0, 0)`` leaves the matcher on its ``searchrad=None`` path — the one
        every iterate pass already runs — which recovers a sub-arcsec pointing
        error perfectly well without a gross prior. So a false decline costs
        nothing but a redundant translation estimate, while a false accept
        destroys the solve; the asymmetry is what licenses biasing the rule
        toward declining.
        """
        n_before = int(np.count_nonzero(tree.query(im_xy, k=1)[0] <= r_tight))
        n_after = int(np.count_nonzero(
            tree.query(im_xy + gross, k=1)[0] <= r_tight))
        self.diag.update(
            gross_tight_before=n_before, gross_tight_after=n_after,
            gross_keep_ratio=(n_after / n_before if n_before
                              else float('nan')))
        if self.gross_min_keep_frac <= 0:
            return gross                   # guard disabled (config / A-B arm)
        if n_before < _MIN_PAIRS:
            # Nothing to lose: this IS the acquisition-failure regime the gross
            # stage exists for. Adopt the shift — the group + residual gates
            # downstream still judge the result.
            return gross
        if n_after >= self.gross_min_keep_frac * n_before:
            return gross
        log(f"OffsetHistogramMatch: gross shift ({gross[0]:+.2f}, "
            f"{gross[1]:+.2f})\" LOSES tight pairs ({n_before} -> {n_after} "
            f"within {r_tight:.3f}\"); declining it and matching around the "
            f"input WCS.")
        return np.zeros(2)

    # -- per-axis histogram cut (rotation scan + rough cut + sigma clip) ----

    def _histogram_cut(self, d, c, mask, *, binsize, gaussian_sigma,
                       rough_min, rough_max, label=None):
        """One JHAT ``histogram_cut``: refine *mask* by the consensus peak of
        offsets *d* against cross coordinate *c* (rotation-slope scan).

        When *label* is given (``'dx'``/``'dy'``), the winning peak's height,
        FWHM (tangent-plane arcsec) and contrast are recorded in ``self.diag``.
        The contrast's denominator is the tallest peak from a slope **far enough
        from the winner to be a different consensus**: neighbouring scan steps
        de-rotate the offsets by far less than one peak width, so their heights
        are near-identical and a plain second-best would report ~1.0 for every
        solve, decisive or not. The exclusion half-width is therefore the slope
        change that displaces the outermost source by one peak FWHM.
        """
        idx = np.flatnonzero(mask)
        if idx.size < _MIN_PAIRS:
            return mask
        d_sel = d[idx]
        c_sel = c[idx]
        # rotate about the pool centre (JHAT: the detector centre) so the scan
        # shifts the histogram as little as possible
        c0 = 0.5 * (float(np.min(c_sel)) + float(np.max(c_sel)))
        c_rel = c_sel - c0

        slopes = np.linspace(-self.slope_max, self.slope_max,
                             self.slope_nsteps + 1)
        heights = np.empty(slopes.size, dtype=float)
        best = None   # (height, slope, center, fwhm)
        for k, slope in enumerate(slopes):
            center, height, fwhm = _smoothed_hist_peak(
                d_sel - slope * c_rel, binsize, gaussian_sigma)
            heights[k] = height
            if best is None or height > best[0]:
                best = (height, slope, center, fwhm)

        height, slope, center, fwhm = best
        if label is not None:
            far = np.abs(slopes - slope) > (
                fwhm / max(float(np.max(np.abs(c_rel))), 1e-12))
            runner_up = float(heights[far].max()) if np.any(far) else 0.0
            self.diag.update({
                f'{label}_peak': float(height),
                f'{label}_fwhm_arcsec': float(fwhm),
                f'{label}_contrast': (height / runner_up if runner_up > 0
                                      else float('nan')),
            })

        rough = float(np.clip(self.nfwhm * fwhm, rough_min, rough_max))
        d_rot = d - slope * (c - c0)
        rough_mask = mask & (np.abs(d_rot - center) <= rough)
        if np.count_nonzero(rough_mask) < _MIN_PAIRS:
            return rough_mask
        return _sigma_clip_median(d_rot, rough_mask, self.nsigma)

    # -- pair-level brightness agreement (JHAT delta_mag_lim) ----------------

    def _delta_mag_ok(self, refcat, imcat, nn):
        """Boolean mask over image sources: pair passes the ``delta_mag_lim``
        window, or lacks the information to be judged (missing mags / no
        lookup / no ``id`` column — never punish a source for missing data).
        """
        ok = np.ones(len(imcat), dtype=bool)
        if (self.delta_mag_lim is None or not self.image_mags
                or self.refcat_mag_col not in refcat.colnames
                or 'id' not in imcat.colnames):
            return ok
        im_mag = np.array([self.image_mags.get(int(i), np.nan)
                           for i in np.asarray(imcat['id'])], dtype=float)
        ref_col = refcat[self.refcat_mag_col]
        if hasattr(ref_col, 'filled'):                 # MaskedColumn -> NaN
            ref_mag = np.asarray(ref_col.filled(np.nan), dtype=float)
        else:
            ref_mag = np.asarray(ref_col, dtype=float)
        dmag = im_mag - ref_mag[nn]
        lo, hi = (float(self.delta_mag_lim[0]), float(self.delta_mag_lim[1]))
        judged = np.isfinite(dmag)
        ok[judged] = (dmag[judged] >= lo) & (dmag[judged] <= hi)
        return ok

    # -- MatchCatalogs entry point ------------------------------------------

    def __call__(self, refcat, imcat, tp_pscale=1.0, tp_units=None, **kwargs):
        from scipy.spatial import cKDTree

        # Peak confidence describes THIS call only — clear it up front so a
        # short-circuited call can never report the previous pass's numbers.
        self.diag = {}

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
        # The consensus histogram's bin size AS EXECUTED (arcsec) — recorded
        # because every ``*_px`` knob is scaled by the ``tp_pscale`` tweakwcs
        # hands us, so this one number says whether the consensus stage resolved
        # the offset distribution at all or collapsed it into a single bin.
        self.diag['hist_binsize_arcsec'] = float(binsize)

        tree = cKDTree(ref_xy)

        shift = np.zeros(2)
        if self.searchrad is not None and float(self.searchrad) > 0:
            gross = self._gross_shift(tree, ref_xy, im_xy)
            if gross is None:
                log(f"OffsetHistogramMatch: no reference source within "
                    f"{self.searchrad:.0f}\" of any image source; no match.")
                return _EMPTY
            # A gross shift is adopted only if it EARNS its keep (see
            # _vet_gross_shift); otherwise the pool is matched around its input
            # WCS, exactly as every iterate pass is.
            shift = self._vet_gross_shift(tree, im_xy, np.asarray(gross),
                                          _GROSS_TIGHT_PX * pscale)

        # Unbounded 1-NN: every image source pairs with its closest reference.
        # Most pairs are wrong at this stage by design — consensus decides.
        d2d, nn = tree.query(im_xy + shift, k=1)
        mask = np.isfinite(d2d)
        if self.d2d_max is not None:
            mask &= d2d <= float(self.d2d_max)
        mask &= self._delta_mag_ok(refcat, imcat, nn)
        if np.count_nonzero(mask) < _MIN_PAIRS:
            return _EMPTY

        # Pairwise offsets in the gross-corrected frame + the image-side
        # coordinates the rotation ramp is scanned against.
        dx = ref_xy[nn, 0] - (im_xy[:, 0] + shift[0])
        dy = ref_xy[nn, 1] - (im_xy[:, 1] + shift[1])
        im_x = im_xy[:, 0]
        im_y = im_xy[:, 1]

        if self.histocut_order == 'dxdy':
            axes = ((dx, im_y, 'dx'), (dy, im_x, 'dy'))
        else:
            axes = ((dy, im_x, 'dy'), (dx, im_y, 'dx'))
        for d, c, label in axes:
            mask = self._histogram_cut(
                d, c, mask, binsize=binsize, gaussian_sigma=gaussian_sigma,
                rough_min=rough_min, rough_max=rough_max, label=label)

        im_idx = np.flatnonzero(mask)
        if im_idx.size < _MIN_PAIRS:
            return _EMPTY
        return nn[im_idx].astype(int), im_idx.astype(int)
