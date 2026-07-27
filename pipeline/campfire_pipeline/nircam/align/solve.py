"""Per-pool astrometric solve for the NIRCam ``align`` phase.

Given, for one **pool** of detectors (a module — or, with ``pool_modules``, a
whole channel — of one exposure), each detector's gwcs + detected source catalog
and a static Gaia-tied reference catalog, this fits ONE shared shift+rotation
(``rshift``) for the pool via ``tweakwcs`` (SIAF distortion untouched), then —
per detector, gated on match count / coverage / shift significance — frees a
small individual fit up to a configurable ceiling geometry. It returns the
corrected gwcs per detector plus solve diagnostics; it does no FITS I/O (the
exposure reader/writer + ``CFP_ALGN`` stamp live in the orchestration layer).

**Coarse → fine.**

1. *Footprint-clip* the field refcat to this pool's detector union + border
   (``footprint.py``), so the coarse matcher works against in-frame sources.
2. *Coarse* — one pooled ``rshift`` recovered by the JHAT-ported
   ``OffsetHistogramMatch`` (``histmatch.py``): a 2-D-histogram gross-shift
   stage over ``coarse_searchrad`` (pass 0 only), then unbounded 1-NN pairing
   whose true correspondences are selected by the pairwise-offset histogram
   consensus (rotation-slope scan + sigma clip), iterated match→fit→rematch to
   convergence. Unlike ``tweakwcs.XYXYMatch`` (pair *enumeration*, which dies
   with ``MatchSourceConfusionError`` on clustered extragalactic catalogs),
   nothing here is enumerated and nothing can overflow.
3. *Group gate* — accept the pool only if enough sources match one-to-one and
   span enough sky to condition a rotation; else reject to NOT_ALIGNED.
4. *Fine* — EVERY detector with enough verified matches gets an individual
   fit on its own already-matched (mutual-NN) pairs — handed to ``align_wcs``
   row-aligned with ``match=None``, exactly JHAT's ``already_matched`` design
   (JHAT fits every detector, always) — its geometry chosen down a ladder
   (``general`` → ``rshift`` → ``shift`` → keep-coarse) by its match count and
   coverage, and accepted when the shift it applies is SIGNIFICANT against the
   noise adopting it costs (``|shift| > fine_min_significance * sigma/sqrt(n)``)
   and it does not materially degrade the residual. The ladder floors
   (``fine_min_shift`` = JHAT's ``minobj=3``) are the guard against
   noise-chasing; ``tolerance`` is reporting-only.

The pooled coarse is the mechanical expression of the pooling constraint: every
detector of one pool shares one ``group_id`` so a single rigid rshift is fit from
the pooled catalog and applied to all of them. Distinct per-pool ``group_id``s +
a static reference catalog + ``expand_refcat=False`` keep pools independent.

Per-detector residuals are recomputed DIRECTLY (``det_to_world`` vs matched
refcat positions) with **one-to-one** (mutual nearest-neighbour) matching, not
read from ``tweakwcs``'s group-level ``fit_info``.

**Diagnostics for independent validation.** The reported residual is measured on
the very sample the matcher selected, so it is circular: a solution locked onto
the wrong sources reports a small residual just as happily as a right one. The
solve therefore also carries out (a) the **per-source** matched mask / separation
/ matched-refcat position for every detection (:class:`DetectorSolution`), so
overlapping exposures can be cross-checked against each other without drizzling
anything, and (b) the matcher's own **peak-confidence** numbers
(:attr:`GroupSolution.diagnostics`, lifted off ``OffsetHistogramMatch.diag``), so
a low-contrast — i.e. ambiguous — lock can be triaged. Neither influences any
gate; the I/O layer persists them (``ALGNCAT`` + ``ALGN*`` header keywords).
"""

import copy
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.table import Table
from tweakwcs.correctors import JWSTWCSCorrector
from tweakwcs.imalign import align_wcs

from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.align.footprint import clip_refcat_to_exposure
from campfire_pipeline.nircam.align.histmatch import OffsetHistogramMatch

# A coarse iterate pass that moves the shared WCS by less than this (arcsec) has
# converged — well below a NIRCam pixel (31 mas SW / 63 mas LW).
_REFINE_CONVERGE_ARCSEC = 0.01

# Per-detector fine-fit geometry ladder, richest first. ``fine_fitgeom`` sets the
# ceiling; a detector drops down it by its unique-match count / coverage.
_FINE_LADDER = ('general', 'rshift', 'shift')
# Geometries that fit a rotation and so need spatial coverage to be conditioned.
_ROTATING = ('general', 'rshift')
# Free parameters per fine-fit geometry (both coordinates counted): shift is
# (dx, dy); rshift adds a rotation; rscale adds a scale; general is a full
# affine. Used to normalize the acceptance statistic — see _fine_significance.
_FITGEOM_NPARAM = {'shift': 2, 'rshift': 3, 'rscale': 4, 'general': 6}

# Median of a 2-D Gaussian's radial deviation, in units of the per-axis sigma.
_RAYLEIGH_MEDIAN = np.sqrt(2.0 * np.log(2.0))      # 1.1774
# A fine fit whose own residual is this much WORSE than the coarse one is
# rejected however significant its shift looks — a significant-but-degrading fit
# is a broken fit, not a correction. Sized well above the noise on the median
# residual (~1.06/sqrt(n), i.e. ~14% at n=60), so it only fires on real damage.
_FINE_MAX_DEGRADE = 1.25


@dataclass
class DetectorInput:
    """One detector's inputs to the solve."""

    detector: str      # e.g. 'nrca1'
    wcs: object        # gwcs.WCS (this detector's current gwcs)
    wcsinfo: dict      # {'v2_ref' (arcsec), 'v3_ref' (arcsec), 'roll_ref' (deg)}
    catalog: object    # astropy Table with 'x','y' (0-indexed) [+ 'mag']


@dataclass
class DetectorSolution:
    """The align outcome for one detector.

    Everything from ``n_detected`` down is **diagnostic only** — it gates
    nothing, and every field defaults so the positional constructions above stay
    valid. The four arrays are row-aligned with the detector's input catalog
    (``DetectorInput.catalog``), length ``n_detected``, and describe the FINAL
    accepted WCS (the post-fine re-match when a fine fit was accepted, else the
    pooled coarse match). On a residual-gate reject (``aligned=False``) they
    describe the *rejected* correction — which is exactly what one wants to
    triage the reject — while ``wcs`` is the untouched original; the I/O layer
    does not persist them in that case.
    """

    detector: str
    wcs: object              # corrected (or, when NOT_ALIGNED, original) gwcs
    dof: str                 # 'coarse' | 'shift' | 'rshift' | 'general' | 'identity'
    residual_arcsec: float   # median matched residual after alignment (nan if none)
    n_matched: int
    within_tolerance: bool
    aligned: bool = True     # False: rejected by the residual gate — the I/O
                             # layer stamps NOT_ALIGNED (WCS preserved)
    n_detected: int = 0      # rows in this detector's detection catalog
    matched: Optional[object] = None    # bool array: row paired one-to-one
    sep_arcsec: Optional[object] = None  # float array: separation to the NEAREST
                             # refcat source (all rows, matched or not)
    ref_ra: Optional[object] = None     # float array: matched refcat RA (deg),
    ref_dec: Optional[object] = None    # ... and Dec; nan on unmatched rows.
                             # VALUES, not indices: they index the
                             # footprint-clipped refcat, which no downstream
                             # consumer can reconstruct.


@dataclass
class GroupSolution:
    """The align outcome for one pool (module / channel)."""

    key: str
    status: str                          # 'SOLVED' | 'NOT_ALIGNED'
    shift: Optional[Tuple[float, float]]  # coarse (dx, dy), arcsec
    rot_deg: Optional[float]              # coarse rotation, degrees
    rmse_arcsec: Optional[float]
    n_matched: int                        # pool-level matched-source count
    detectors: List[DetectorSolution]
    # Diagnostics (defaults; gate nothing). ``n_refcat_in_footprint`` is the
    # footprint clip's ``n_kept`` — the reference density the coarse matcher
    # actually worked against. ``diagnostics`` is the matcher's peak confidence
    # (see ``OffsetHistogramMatch.diag``): the gross 2-D-histogram keys come from
    # coarse pass 0 (the only pass that runs the gross stage), the per-axis
    # consensus keys from the last pass that was KEPT.
    n_refcat_in_footprint: Optional[int] = None
    diagnostics: Optional[dict] = None


def _has_radec(refcat):
    return (refcat is not None and 'RA' in refcat.colnames
            and 'DEC' in refcat.colnames)


def _as_float(value, default=float('nan')):
    try:
        arr = np.asarray(value, dtype=float).ravel()
        return float(arr[0]) if arr.size else default
    except (TypeError, ValueError):
        return default


def _succeeded(fit_info):
    return str(fit_info.get('status', '')).startswith('SUCCESS')


def _coverage_arcsec(ra, dec):
    """Sky extent (bounding-box diagonal, arcsec) of a set of positions.

    A proxy for the rotation lever arm: matched sources clustered in a small
    patch cannot condition a rotation, so the fine fit demotes to shift-only (or
    the pool rejects) below ``min_coverage_arcsec``.
    """
    ra = np.asarray(ra, dtype=float)
    dec = np.asarray(dec, dtype=float)
    if ra.size < 2:
        return 0.0
    cd = np.cos(np.radians(float(np.mean(dec))))
    x = (ra - np.mean(ra)) * cd * 3600.0
    y = (dec - np.mean(dec)) * 3600.0
    return float(np.hypot(x.max() - x.min(), y.max() - y.min()))


def _match(corrector, catalog, ref_sky, match_radius):
    """One-to-one residual for one detector: ``(residual_arcsec, n_matched,
    src_idx, ref_idx, keep, sep)``.

    Transform the detector's own sources through the (corrected) gwcs and match
    them to the reference positions by **mutual nearest neighbour** — a source
    and a reference pair up only if each is the other's closest — keeping pairs
    within *match_radius* arcsec. ``src_idx``/``ref_idx`` are the row-aligned
    pairing (one-to-one by the mutuality); the fine fit consumes it directly as
    a pre-matched list, and the group gate measures coverage from ``ref_idx``.

    ``keep`` (bool) and ``sep`` (arcsec) are the same information *per catalog
    row* — the matched mask and the separation to the nearest reference source
    for EVERY detection, unmatched ones included — which is what the per-source
    diagnostic catalog is built from. ``sep`` is nan where no separation could be
    measured; on unmatched rows it is a real distance to a source that simply
    isn't a counterpart, so read it together with ``keep``.
    """
    n_src = len(catalog)
    keep = np.zeros(n_src, dtype=bool)
    sep = np.full(n_src, np.nan, dtype=float)
    no_pairs = (float('nan'), 0, np.array([], dtype=int),
                np.array([], dtype=int), keep, sep)
    x = np.asarray(catalog['x'], dtype=float)
    y = np.asarray(catalog['y'], dtype=float)
    if x.size == 0 or len(ref_sky) == 0:
        return no_pairs
    ra, dec = corrector.det_to_world(x, y)
    src = SkyCoord(np.asarray(ra, dtype=float), np.asarray(dec, dtype=float),
                   unit='deg')
    s2r, d2d, _ = src.match_to_catalog_sky(ref_sky)
    r2s, _, _ = ref_sky.match_to_catalog_sky(src)
    sep = d2d.arcsec
    src_ix = np.arange(src.size)
    mutual = r2s[s2r] == src_ix
    keep = mutual & np.isfinite(sep) & (sep <= match_radius)
    if not np.any(keep):
        return (float('nan'), 0, np.array([], dtype=int),
                np.array([], dtype=int), keep, sep)
    return (float(np.median(sep[keep])), int(np.count_nonzero(keep)),
            src_ix[keep], np.asarray(s2r)[keep], keep, sep)


def _ref_positions(n_src, src_idx, ref_idx, ref_sky):
    """Per-catalog-row matched refcat ``(ra, dec)`` in degrees, nan unmatched.

    Resolves the pairing to POSITIONS here, on purpose: ``ref_idx`` indexes the
    footprint-clipped refcat, which is local to this solve and cannot be
    reconstructed by anything downstream, so bare indices would be unusable.
    """
    ref_ra = np.full(n_src, np.nan, dtype=float)
    ref_dec = np.full(n_src, np.nan, dtype=float)
    if len(src_idx):
        ref_ra[src_idx] = ref_sky.ra.deg[ref_idx]
        ref_dec[src_idx] = ref_sky.dec.deg[ref_idx]
    return ref_ra, ref_dec


def _not_aligned(key, detectors, wcs_getter, *, n_refcat=None, diag=None):
    """Reject the whole pool. *n_refcat*/*diag* carry whatever diagnostics the
    reject path already knows (refcat density, matcher peak confidence) — the
    numbers that explain WHY it rejected — for the caller's log/report; nothing
    per-source is persisted, since no corrected WCS is written."""
    return GroupSolution(
        key=key, status='NOT_ALIGNED', shift=None, rot_deg=None,
        rmse_arcsec=None, n_matched=0,
        detectors=[DetectorSolution(name, wcs_getter(name, obj), 'identity',
                                    float('nan'), 0, False)
                   for name, obj in detectors],
        n_refcat_in_footprint=n_refcat, diagnostics=(diag or None),
    )


def _pool_fit(detectors, wcs_by_det, refcat, match, *, key, fitgeom, nclip, sigma):
    """Fit one shared correction over the pool and return
    ``(correctors, fit_info)``.

    Builds a fresh corrector per detector from *wcs_by_det* (so each call starts
    from the current best WCS as an immutable baseline), all sharing *key* as
    ``group_id`` — one pooled rigid fit. ``align_wcs`` mutates the correctors in
    place on success, so the caller reads the corrected WCS back off them.
    """
    correctors = [
        JWSTWCSCorrector(
            wcs_by_det[d.detector], d.wcsinfo,
            meta={'catalog': d.catalog, 'group_id': key, 'name': d.detector},
        )
        for d in detectors
    ]
    align_wcs(correctors, refcat=refcat, enforce_user_order=True,
              expand_refcat=False, minobj=None, match=match,
              fitgeom=fitgeom, nclip=nclip, sigma=(sigma, 'rmse'))
    return correctors, correctors[0].meta.get('fit_info', {})


def _coarse(detectors, wcs_by_det, refcat, key, *, match0, match_iter, niter,
            nclip, sigma):
    """Iterate a pooled ``rshift`` to convergence; return
    ``(correctors, last_info, first_info, wcs_by_det, diag)`` or
    ``(None, {}, {}, wcs_by_det, {})``.

    Pass 0 uses *match0* (the histogram matcher with its gross-translation
    stage over the full search radius); later passes start from the corrected
    WCS and use *match_iter* (the same consensus matcher without the gross
    stage) around the now-small residual, the rigid fit peeling off the roll
    each pass. *first_info* is pass 0's fit (the gross shift/rot the input WCS
    was off by, for provenance); *last_info* is the converged fit (its rmse is
    the final quality).

    *diag* mirrors that convention for the matcher's peak confidence
    (``OffsetHistogramMatch.diag``, overwritten on every call): pass 0's keys are
    laid down first — it alone runs the gross stage — and each KEPT later pass
    overwrites the per-axis consensus keys, so the result is pass 0's gross peak
    plus the final accepted pass's consensus peaks. Snapshotted only for passes
    that succeeded, since a crashing or failing pass is discarded (and the loop
    can break at any i, so the last call is not necessarily the last pass).
    """
    correctors, last_info, first_info, diag = None, {}, {}, {}
    for i in range(max(1, int(niter))):
        match = match0 if i == 0 else match_iter
        try:
            c, fi = _pool_fit(detectors, wcs_by_det, refcat, match, key=key,
                              fitgeom='rshift', nclip=nclip, sigma=sigma)
        except Exception as e:  # noqa: BLE001 — a matcher/fit crash degrades to
            # NOT_ALIGNED, never aborts the worker. Keep any prior good pass.
            log(f"align solve[{key}]: coarse pass {i} raised "
                f"{type(e).__name__}: {e}; keeping the last good transform.")
            break
        if not _succeeded(fi):
            break  # keep the last good pass (or None -> reject upstream)
        correctors, last_info = c, fi
        if not first_info:
            first_info = fi
        diag.update(getattr(match, 'diag', None) or {})
        wcs_by_det = {cc.meta['name']: cc.wcs for cc in c}
        if i > 0:
            delta = np.hypot(*np.asarray(fi['shift'], float).ravel()[:2])
            if delta < _REFINE_CONVERGE_ARCSEC:
                break
    return correctors, last_info, first_info, wcs_by_det, diag


def _choose_fitgeom(n, coverage, ceiling, mins, min_coverage):
    """Pick the per-detector fine geometry down the ladder from *ceiling*.

    Falls to a lower geometry when *n* matches are too few for the chosen one,
    and skips rotating geometries (``general``/``rshift``) when *coverage* is
    below *min_coverage* (an unconditioned rotation). ``None`` = keep the coarse
    attitude for this detector.
    """
    try:
        start = _FINE_LADDER.index(ceiling)
    except ValueError:
        start = _FINE_LADDER.index('rshift')
    for geom in _FINE_LADDER[start:]:
        if n < mins[geom]:
            continue
        if geom in _ROTATING and coverage < min_coverage:
            continue
        return geom
    return None


def _fine_shift_arcsec(coarse, trial, catalog, src_idx):
    """Median on-sky displacement (arcsec) the fine fit applies to this detector.

    Measured at the matched sources — where the fit is actually constrained and
    where the astrometry matters — rather than at one fiducial pixel, so a
    rotation-bearing fit is judged by how far it really moves things.
    """
    if len(src_idx) == 0:
        return float('nan')
    x = np.asarray(catalog['x'], dtype=float)[src_idx]
    y = np.asarray(catalog['y'], dtype=float)[src_idx]
    ra0, dec0 = coarse.det_to_world(x, y)
    ra1, dec1 = trial.det_to_world(x, y)
    ra0 = np.asarray(ra0, dtype=float); dec0 = np.asarray(dec0, dtype=float)
    # Wrap the longitude difference into (-180, 180]: a detector straddling
    # RA=0 otherwise reads a ~360 deg displacement for a milliarcsecond move,
    # which would make a noise-only fit look overwhelmingly significant.
    d_ra = (np.asarray(ra1, dtype=float) - ra0 + 180.0) % 360.0 - 180.0
    d_ra *= np.cos(np.radians(dec0))
    d_dec = np.asarray(dec1, dtype=float) - dec0
    return float(np.median(np.hypot(d_ra, d_dec)) * 3600.0)


def _fine_significance(shift_arcsec, new_resid, new_n, geom='shift'):
    """``t = |shift| / se(geom)`` — the fine fit's shift measured against the
    estimation noise that adopting it costs.

    This is the quantity the accept/reject decision is really about. Keeping the
    pooled coarse attitude is right when the detector has no offset of its own,
    because the pooled fit averages ~M times more pairs; taking the per-detector
    fit is right when its offset exceeds the noise of estimating it. Comparing
    residuals instead cannot express that trade-off: the fit is judged on a
    RE-MATCHED sample rather than the one it minimized, so with no offset present
    the comparison is a coin flip, and roughly half of the detectors that do
    carry a real offset keep the coarse attitude anyway (measured on COSMOS
    f410m/f210m: median t ~ 3.1-3.4 against a null expectation of 1.18, yet the
    residual test accepted only 44-59%).

    ``sigma_axis`` comes from the post-fit residual: for a 2-D Gaussian the
    median radial deviation is ``sqrt(2 ln 2)`` per-axis sigmas.

    **Normalized for the fit geometry.** A richer geometry moves sources further
    on noise alone: summing the leverage over the fitted points gives a mean
    squared noise displacement of ``(p/2) * sigma^2/n`` for a ``p``-parameter fit,
    so the standard error carries a ``sqrt(p/2)`` factor — 1.0 for ``shift``,
    1.22 for ``rshift``, 1.41 for ``rscale``, 1.73 for ``general``. Without it a
    noise-only ``general`` fit would score ~1.7x too high and clear the gate on
    variance alone. With it, ``t`` is ~Rayleigh(1) under the null for EVERY
    geometry, so one threshold has the same false-accept rate
    (``exp(-k^2/2)``) throughout and the k derivation below stays valid.
    """
    if (not np.isfinite(shift_arcsec) or not np.isfinite(new_resid)
            or new_n < 2 or new_resid <= 0):
        return float('nan')
    nparam = _FITGEOM_NPARAM.get(geom, 2)
    se = (np.sqrt(nparam / 2.0) * (new_resid / _RAYLEIGH_MEDIAN)
          / np.sqrt(new_n))
    return float('inf') if se <= 0 else float(shift_arcsec / se)


def _fine_fit(corr, detector, catalog, refcat, src_idx, ref_idx, geom, *,
              key, nclip, sigma):
    """Individually refit one detector (geometry *geom*) on its already-matched
    pairs; return ``(trial_corrector, fit_info)``.

    Exactly JHAT's design (``already_matched=True``): the mutual-NN pairs from
    the group gate are handed to ``align_wcs`` as row-aligned catalogs with
    ``match=None`` — no matcher runs, the sigma-clipped fit alone decides the
    transform. Deep-copies the pooled corrector so a rejected fit never touches
    the shared solution.
    """
    trial = copy.deepcopy(corr)
    trial.meta['group_id'] = f'{key}:{detector}'   # distinct -> solo fit
    trial.meta['catalog'] = Table({
        'x': np.asarray(catalog['x'], dtype=float)[src_idx],
        'y': np.asarray(catalog['y'], dtype=float)[src_idx],
    })
    align_wcs([trial], refcat=refcat[ref_idx], enforce_user_order=True,
              expand_refcat=False, minobj=None, match=None,
              fitgeom=geom, nclip=nclip, sigma=(sigma, 'rmse'))
    return trial, trial.meta.get('fit_info', {})


def solve_exposure_group(detectors, refcat, *, key='group', pool_modules=None,
                         coarse_searchrad=70.0, refine_niter=3,
                         d2d_max=1.5, binsize_px=0.02, gaussian_sigma_px=0.2,
                         rough_cut_px_min=2.5, rough_cut_px_max=2.5,
                         nfwhm=2.5, hist_nsigma=3.0, histocut_order='dxdy',
                         slope_max=10.0 / 2048.0, slope_nsteps=200,
                         delta_mag_lim=None,
                         fine_fitgeom='rshift', fine_min_general=10,
                         fine_min_rshift=4, fine_min_shift=3, tolerance=0.05,
                         match_radius=0.5, min_matched=6, min_coverage_arcsec=5.0,
                         ref_border_arcmin=1.2, nclip=3, sigma=3.0,
                         max_residual_arcsec=0.1,
                         dva_repivot=True, dva_pivot='pool',
                         fine_min_significance=1.4):
    """Solve one pool of detectors; return a :class:`GroupSolution`.

    *detectors* is one pool (a module, or a whole channel when the caller pooled
    modules). The pool is footprint-clipped, tied to the refcat with a single
    coarse ``rshift`` (``OffsetHistogramMatch``: gross 2-D-hist shift + 1-NN /
    offset-histogram consensus, iterated), gated on match count and sky
    coverage, then EVERY detector with enough verified matches gets a fine fit
    whose geometry is chosen down the ``fine_fitgeom`` ladder by its match
    count / coverage and accepted only if it reduces the residual
    (*tolerance* only flags ``within_tolerance`` in the result; it gates
    nothing). The ``d2d_max`` …
    ``slope_nsteps`` / ``delta_mag_lim`` knobs pass straight to
    :class:`OffsetHistogramMatch` (the ``*_px`` ones in image pixels,
    mirroring the validated JHAT configuration); ``delta_mag_lim`` reads image
    mags through the pool-unique ``id`` column assigned below, and judges only
    pairs where both mags are finite and calibrated.

    ``pool_modules`` is accepted for config-passthrough symmetry but unused here
    (the orchestration layer decides pooling before calling this).

    ``align_wcs`` reports ``status='SUCCESS'`` even for a geometrically wrong
    fit, so acceptance never rests on it alone: the pool rejects to NOT_ALIGNED
    unless at least *min_matched* sources match one-to-one within *match_radius*
    **and** they span at least *min_coverage_arcsec* of sky.

    *max_residual_arcsec* is a final per-detector backstop on top of all of the
    above: any detector whose post-fit one-to-one residual still exceeds it is
    individually rejected (``aligned=False`` — the I/O layer stamps NOT_ALIGNED,
    WCS preserved), so a plausible-looking but wrong solution can never reach a
    mosaic. Healthy solves sit well under it (~0.02–0.03"); the COSMOS A2/A3
    misregistrations this guards against sat at 0.20–0.24". ``None`` disables.

    Alongside the corrected WCSes the result carries diagnostics that permit
    INDEPENDENT validation of the solve (nothing below gates anything): the
    per-detection matched mask / separation / matched-refcat position for the
    accepted WCS (:class:`DetectorSolution`), the footprint-clipped refcat
    density, and the matcher's peak-confidence numbers
    (:attr:`GroupSolution.diagnostics`) — the residual alone cannot expose a
    confident lock onto the wrong sources, a low peak contrast can.
    """
    detectors = list(detectors)
    if not detectors:
        return GroupSolution(key, 'NOT_ALIGNED', None, None, None, 0, [])

    # 0. Re-reference the per-detector DVA scale to a pool-common pivot, so the
    #    pooled rigid fit is geometrically valid (see align/dva.py). A no-op for
    #    a single-detector pool. `original_wcs` keeps the untouched input WCSes:
    #    NOT_ALIGNED must preserve exactly what was on disk, and the re-pivot is
    #    an unverified refinement until the pool actually solves.
    original_wcs = {d.detector: d.wcs for d in detectors}
    dva_info = None
    if dva_repivot:
        from campfire_pipeline.nircam.align.dva import repivot_pool_dva
        detectors, dva_info = repivot_pool_dva(detectors, pivot=dva_pivot,
                                               key=key)

    def _orig(name, wcs):
        """Reject path: hand back the WCS exactly as it arrived."""
        return original_wcs.get(name, wcs)

    if not _has_radec(refcat) or len(refcat) < 3:
        log(f"align solve[{key}]: reference catalog missing 'RA'/'DEC' or "
            f"has <3 sources; NOT_ALIGNED (WCS preserved).")
        return _not_aligned(key, [(d.detector, d.wcs) for d in detectors],
                            _orig)

    # 1. Footprint-clip the field refcat to this pool's coverage + border. Fail
    #    open (keep the full refcat) on any geometry error — a per-pool worker
    #    must never crash the field.
    n_ref_footprint = None
    try:
        clip = clip_refcat_to_exposure(refcat, [d.wcs for d in detectors],
                                       border_arcmin=ref_border_arcmin)
        if clip.clipped:
            log(f"align solve[{key}]: refcat footprint clip "
                f"{clip.n_kept}/{clip.n_total} sources "
                f"(border {ref_border_arcmin:.2f}').")
        refcat = clip.table
        # Reference density the coarse matcher actually worked against — the
        # denominator for any independent read of the match count.
        n_ref_footprint = int(clip.n_kept)
    except Exception as e:  # noqa: BLE001 — worker robustness; fail open
        log(f"align solve[{key}]: footprint clip failed ({type(e).__name__}: "
            f"{e}); using the full refcat.")
    if len(refcat) < 3:
        log(f"align solve[{key}]: only {len(refcat)} refcat sources inside the "
            f"footprint; NOT_ALIGNED (source-starved / outside-acquisition-bound).")
        return _not_aligned(key, [(d.detector, d.wcs) for d in detectors],
                            _orig, n_refcat=n_ref_footprint)

    wcs_by_det = {d.detector: d.wcs for d in detectors}

    # Pool-unique source ids + an id->mag lookup: tweakwcs' pooled group
    # catalog keeps 'id' but drops every brightness column, so the matcher's
    # delta_mag_lim pair cut reads image mags through this map. Only catalogs
    # carrying CALIBRATED mags contribute (an uncalibrated mag vs a refcat AB
    # mag would cut on garbage); their sources just pass the cut unjudged.
    image_mags = {}
    for i, d in enumerate(detectors):
        ids = i * 10_000_000 + np.arange(len(d.catalog))
        d.catalog['id'] = ids
        if d.catalog.meta.get('mag_calibrated') and 'mag' in d.catalog.colnames:
            mags = np.asarray(d.catalog['mag'], dtype=float)
            image_mags.update(
                (int(s), float(m)) for s, m in zip(ids, mags)
                if np.isfinite(m))

    # 2. Coarse: one pooled rshift, iterated to convergence. Pass 0's matcher
    #    carries the gross-translation stage (acquisition-failure recovery);
    #    the iterate matcher drops it and re-pairs by pure 1-NN + consensus
    #    around the already-corrected WCS.
    hist_kwargs = dict(
        d2d_max=d2d_max, binsize_px=binsize_px,
        gaussian_sigma_px=gaussian_sigma_px,
        rough_cut_px_min=rough_cut_px_min, rough_cut_px_max=rough_cut_px_max,
        nfwhm=nfwhm, nsigma=hist_nsigma, histocut_order=histocut_order,
        slope_max=slope_max, slope_nsteps=slope_nsteps,
        delta_mag_lim=delta_mag_lim, image_mags=image_mags)
    # Bound to locals (not passed as anonymous temporaries) so ``_coarse`` can
    # read each matcher's peak-confidence stash back off the instance.
    match0 = OffsetHistogramMatch(searchrad=coarse_searchrad, **hist_kwargs)
    match_iter = OffsetHistogramMatch(searchrad=None, **hist_kwargs)
    correctors, last_info, first_info, wcs_by_det, diag = _coarse(
        detectors, wcs_by_det, refcat, key,
        match0=match0, match_iter=match_iter,
        niter=refine_niter, nclip=nclip, sigma=sigma)
    if correctors is None or not _succeeded(last_info):
        log(f"align solve[{key}]: coarse fit "
            f"{last_info.get('status', 'FAILED')}; NOT_ALIGNED (WCS preserved).")
        return _not_aligned(key, [(d.detector, d.wcs) for d in detectors],
                            _orig, n_refcat=n_ref_footprint,
                            diag=diag)

    # Provenance: the gross correction the input WCS was off by (pass 0), with
    # the converged fit's rmse as the quality.
    shift = tuple(_as_float(v)
                  for v in np.asarray(first_info['shift']).ravel()[:2])
    rot_deg = _as_float(first_info.get('proper_rot', first_info.get('rot')))
    rmse = _as_float(last_info.get('rmse'))

    ref_sky = SkyCoord(np.asarray(refcat['RA'], dtype=float),
                       np.asarray(refcat['DEC'], dtype=float), unit='deg')

    # 3. Group gate: recompute one-to-one matches directly, then reject unless
    #    enough sources match AND they span enough sky to condition a rotation.
    prelim = [_match(corr, d.catalog, ref_sky, match_radius)
              for corr, d in zip(correctors, detectors)]
    group_nmatched = sum(p[1] for p in prelim)
    matched_ref = np.unique(np.concatenate(
        [p[3] for p in prelim] + [np.array([], dtype=int)]))
    coverage = _coverage_arcsec(ref_sky.ra.deg[matched_ref],
                                ref_sky.dec.deg[matched_ref])
    if group_nmatched < min_matched or coverage < min_coverage_arcsec:
        log(f"align solve[{key}]: coarse matched {group_nmatched} sources "
            f"(need {min_matched}) spanning {coverage:.1f}\" (need "
            f"{min_coverage_arcsec:.1f}\"); rejecting to NOT_ALIGNED.")
        return _not_aligned(
            key, [(c.meta['name'], c) for c in correctors],
            # c.original_wcs is the corrector's input — i.e. the RE-PIVOTED wcs.
            # A rejected pool must hand back what was on disk.
            lambda name, c: original_wcs.get(name, c.original_wcs),
            n_refcat=n_ref_footprint,
            diag=diag)

    mins = {'general': fine_min_general, 'rshift': fine_min_rshift,
            'shift': fine_min_shift}

    # 4. Fine: per-detector fit for EVERY detector with matches (JHAT fits
    #    every detector, always — a sub-tolerance systematic SIAF offset must
    #    not survive just because it is small). The ladder floors are the real
    #    guard: the residual-improvement acceptance below is nearly
    #    tautological (the fit minimizes the same mutual-NN pairs it is judged
    #    on), so a detector below `fine_min_shift` verified matches keeps the
    #    pooled attitude — the better estimate at that point. `tolerance` no
    #    longer gates anything; it is the reporting threshold for `within`.
    solutions = []
    for corr, d, (resid, nmatch, src_idx, ref_idx, keep, sep) in zip(
            correctors, detectors, prelim):
        within = bool(np.isfinite(resid) and resid <= tolerance)
        dof, out_wcs = 'coarse', corr.wcs

        if np.isfinite(resid):
            det_cov = _coverage_arcsec(ref_sky.ra.deg[ref_idx],
                                       ref_sky.dec.deg[ref_idx])
            geom = _choose_fitgeom(len(ref_idx), det_cov, fine_fitgeom, mins,
                                   min_coverage_arcsec)
            if geom is not None:
                try:
                    trial, tfi = _fine_fit(corr, d.detector, d.catalog, refcat,
                                           src_idx, ref_idx, geom, key=key,
                                           nclip=nclip, sigma=sigma)
                except Exception as e:  # noqa: BLE001 — keep the coarse solution
                    log(f"align solve[{key}]: fine {geom} fit for {d.detector} "
                        f"raised {type(e).__name__}: {e}; keeping coarse.")
                    tfi = {}
                    trial = None
                if trial is not None and _succeeded(tfi):
                    (new_resid, new_n, new_src, new_ref, new_keep,
                     new_sep) = _match(trial, d.catalog, ref_sky, match_radius)
                    # Accept on the SIGNIFICANCE of the shift, not on a residual
                    # improvement (see _fine_significance) — but never adopt a
                    # fit that materially degrades the residual, however
                    # significant its shift.
                    tstat = _fine_significance(
                        _fine_shift_arcsec(corr, trial, d.catalog, src_idx),
                        new_resid, new_n, geom)
                    degraded = (np.isfinite(new_resid) and np.isfinite(resid)
                                and new_resid > resid * _FINE_MAX_DEGRADE)
                    if (np.isfinite(new_resid) and np.isfinite(tstat)
                            and tstat > fine_min_significance and not degraded):
                        out_wcs, dof = trial.wcs, geom
                        resid, nmatch = new_resid, new_n
                        within = bool(resid <= tolerance)
                        # Per-source diagnostics must describe the WCS actually
                        # accepted, so adopt the post-fine re-match wholesale.
                        src_idx, ref_idx = new_src, new_ref
                        keep, sep = new_keep, new_sep

        ref_ra, ref_dec = _ref_positions(len(d.catalog), src_idx, ref_idx,
                                         ref_sky)
        per_source = dict(n_detected=len(d.catalog), matched=keep,
                          sep_arcsec=sep, ref_ra=ref_ra, ref_dec=ref_dec)

        # Residual gate (backstop): no solution this bad may reach a mosaic.
        # Gates only a *measured* bad residual — a nan residual (no one-to-one
        # matches on this detector) keeps the pooled attitude, which the group
        # gate above already vetted.
        if (max_residual_arcsec is not None and np.isfinite(resid)
                and resid > float(max_residual_arcsec)):
            log(f"align solve[{key}]: {d.detector} residual {resid:.3f}\" "
                f"exceeds max_residual_arcsec={float(max_residual_arcsec):g}; "
                f"rejecting this detector to NOT_ALIGNED (WCS preserved).")
            solutions.append(DetectorSolution(d.detector,
                                              original_wcs.get(d.detector,
                                                               d.wcs),
                                              'identity',
                                              resid, nmatch, False,
                                              aligned=False, **per_source))
            continue

        solutions.append(DetectorSolution(d.detector, out_wcs, dof,
                                          resid, nmatch, within,
                                          **per_source))

    # If the gate rejected every detector, the pool as a whole failed — report
    # NOT_ALIGNED so the orchestration surfaces it in the end-of-run warning.
    status = ('SOLVED' if any(s.aligned for s in solutions) else 'NOT_ALIGNED')
    if dva_info is not None:
        diag = dict(diag or {})
        diag['dva_applied'] = bool(dva_info.get('applied'))
        if dva_info.get('applied'):
            diag['dva_pivot'] = dva_info['pivot']
            diag['dva_va_scale'] = dva_info['va_scale']
            diag['dva_max_shift_mas'] = dva_info['max_shift_mas']
    return GroupSolution(key, status, shift, rot_deg, rmse,
                         group_nmatched, solutions,
                         n_refcat_in_footprint=n_ref_footprint,
                         diagnostics=(diag or None))
