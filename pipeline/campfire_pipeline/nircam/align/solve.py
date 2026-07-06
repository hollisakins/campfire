"""Per-exposure astrometric solve for the NIRCam ``align`` phase.

Given, for one exposure group, each detector's gwcs + detected source catalog
and a static Gaia-tied reference catalog, this fits ONE shared shift+rotation
for the whole exposure via ``tweakwcs`` (SIAF distortion untouched), then — where
a detector's residual still exceeds tolerance — frees an adaptive per-detector
shift. It returns the corrected gwcs per detector plus solve diagnostics; it does
no FITS I/O (the exposure reader/writer + ``CFP_ALGN`` stamp live in the
orchestration layer).

**Bootstrap → refine.** The shared fit runs in two stages so the triangle cap
never gates the solution:

1. *Footprint-clip* the field refcat to this exposure's detector union + border,
   so the matcher's brightest-N cap keeps in-frame sources (``footprint.py``).
2. *Bootstrap* — ``TriangleMatch`` (``tristars``) recovers a coarse shared
   shift+rotation from a bounded triangle set. This is only a **seed** (§ matcher
   docstring); it is translation-invariant and leans on the pipeline WCS prior.
3. *Refine* — with the coarse transform applied, iterate ``tweakwcs.XYXYMatch``
   (one-to-one nearest-neighbour over **all** detected sources, no 2-D histogram
   re-acquisition) with a robust ``rshift`` refit, rebuilding fresh correctors
   from the current best WCS each pass, until the correction converges. The
   fitted WCS rests on this all-source refine, not on the capped bootstrap.

The shared solve is the mechanical expression of the pooling constraint: every
detector of one exposure shares one ``group_id`` so a single rigid rshift is fit
from the pooled catalog and applied to all of them. Distinct per-exposure
``group_id``s + a static reference catalog + ``expand_refcat=False`` keep
exposures independent (each ties to the same reference on its own); we
deliberately do NOT use ``stcal``'s ``group_id=987654`` global collapse.

Per-detector residuals are recomputed DIRECTLY (``det_to_world`` vs matched
refcat positions) with **one-to-one** (mutual nearest-neighbour) matching, not
read from ``tweakwcs``'s group-level ``fit_info`` — which carries no per-source
residuals, only a fragile group-catalog join, and whose match is not one-to-one.
"""

import copy
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from astropy.coordinates import SkyCoord
from tweakwcs.correctors import JWSTWCSCorrector
from tweakwcs.imalign import align_wcs
from tweakwcs.matchutils import XYXYMatch

from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.align.footprint import clip_refcat_to_exposure
from campfire_pipeline.nircam.align.matcher import TriangleMatch

# A refine pass that moves the shared WCS by less than this (arcsec) has
# converged — well below a NIRCam pixel (31 mas SW / 63 mas LW).
_REFINE_CONVERGE_ARCSEC = 0.01


@dataclass
class DetectorInput:
    """One detector's inputs to the solve."""

    detector: str      # e.g. 'nrca1'
    wcs: object        # gwcs.WCS (this detector's current gwcs)
    wcsinfo: dict      # {'v2_ref' (arcsec), 'v3_ref' (arcsec), 'roll_ref' (deg)}
    catalog: object    # astropy Table with 'x','y' (0-indexed) [+ 'mag']


@dataclass
class DetectorSolution:
    """The align outcome for one detector."""

    detector: str
    wcs: object              # corrected (or, when NOT_ALIGNED, original) gwcs
    dof: str                 # 'shared' | 'shift' | 'identity'
    residual_arcsec: float   # median matched residual after alignment (nan if none)
    n_matched: int
    within_tolerance: bool


@dataclass
class GroupSolution:
    """The align outcome for one exposure group."""

    key: str
    status: str                          # 'SOLVED' | 'NOT_ALIGNED'
    shift: Optional[Tuple[float, float]]  # shared (dx, dy), arcsec
    rot_deg: Optional[float]              # shared rotation, degrees
    rmse_arcsec: Optional[float]
    n_matched: int                        # group-level matched-source count
    detectors: List[DetectorSolution]


def _has_radec(refcat):
    return (refcat is not None and 'RA' in refcat.colnames
            and 'DEC' in refcat.colnames)


def _as_float(value, default=float('nan')):
    try:
        arr = np.asarray(value, dtype=float).ravel()
        return float(arr[0]) if arr.size else default
    except (TypeError, ValueError):
        return default


def _match(corrector, catalog, ref_sky, match_radius):
    """One-to-one residual for one detector: ``(residual_arcsec, n_matched,
    matched_ref_idx)``.

    Transform the detector's own sources through the (corrected) gwcs and match
    them to the reference positions by **mutual nearest neighbour** — a source
    and a reference pair up only if each is the other's closest — keeping pairs
    within *match_radius* arcsec. Mutual matching makes the count honest: unlike
    a plain ``match_to_catalog_sky`` (many sources can pile onto one reference),
    each reference is used at most once. ``matched_ref_idx`` is the set of
    reference rows those sources landed on — reused to build a distractor-free
    local reference catalog for the adaptive per-detector refit.
    """
    empty = (float('nan'), 0, np.array([], dtype=int))
    x = np.asarray(catalog['x'], dtype=float)
    y = np.asarray(catalog['y'], dtype=float)
    if x.size == 0 or len(ref_sky) == 0:
        return empty
    ra, dec = corrector.det_to_world(x, y)
    src = SkyCoord(np.asarray(ra, dtype=float), np.asarray(dec, dtype=float),
                   unit='deg')
    # source -> nearest reference, and reference -> nearest source; keep only
    # pairs that agree (mutual NN), within the match radius.
    s2r, d2d, _ = src.match_to_catalog_sky(ref_sky)
    r2s, _, _ = ref_sky.match_to_catalog_sky(src)
    sep = d2d.arcsec
    src_ix = np.arange(src.size)
    mutual = r2s[s2r] == src_ix
    keep = mutual & np.isfinite(sep) & (sep <= match_radius)
    if not np.any(keep):
        return empty
    return (float(np.median(sep[keep])), int(np.count_nonzero(keep)),
            np.unique(np.asarray(s2r)[keep]))


def _not_aligned(key, detectors, wcs_getter):
    return GroupSolution(
        key=key, status='NOT_ALIGNED', shift=None, rot_deg=None,
        rmse_arcsec=None, n_matched=0,
        detectors=[DetectorSolution(name, wcs_getter(name, obj), 'identity',
                                    float('nan'), 0, False)
                   for name, obj in detectors],
    )


def _shared_fit(detectors, wcs_by_det, refcat, match, *, key, fitgeom, minobj,
                nclip, sigma):
    """Fit one shared correction over the pooled group and return
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
              expand_refcat=False, minobj=minobj, match=match,
              fitgeom=fitgeom, nclip=nclip, sigma=(sigma, 'rmse'))
    return correctors, correctors[0].meta.get('fit_info', {})


def _succeeded(fit_info):
    return str(fit_info.get('status', '')).startswith('SUCCESS')


def solve_exposure_group(detectors, refcat, *, key='group', matcher=None,
                         fitgeom='rshift', minobj=None, nclip=3, sigma=3.0,
                         tolerance=0.05, adaptive=True, adaptive_min_matches=3,
                         match_radius=0.5, min_matched=4, ref_border_arcmin=0.5,
                         bootstrap_max=150, refine_searchrad=2.0,
                         refine_tolerance=0.5, refine_niter=3):
    """Solve one exposure group; return a :class:`GroupSolution`.

    Parameters mirror ``tweakwcs.align_wcs`` where relevant. *tolerance*,
    *match_radius*, *ref_border_arcmin*'s buffer, and the ``refine_*`` radii are
    angular (arcsec, except *ref_border_arcmin* in arcmin). The shared fit runs
    as a **bootstrap** (``TriangleMatch``, capped at *bootstrap_max* vertices)
    followed by an all-source **refine** (``XYXYMatch``, up to *refine_niter*
    one-to-one passes within *refine_searchrad*/*refine_tolerance*), so the
    triangle cap bounds only the seed, never the final fit.

    With ``adaptive=True`` (decision D2), a detector whose residual exceeds
    *tolerance* gets an individual shift-only refit against a distractor-free
    local reference subset, accepted only if it has at least
    *adaptive_min_matches* matches and measurably reduces the residual.

    ``align_wcs`` reports ``status='SUCCESS'`` even for a geometrically wrong
    fit, so we reject to NOT_ALIGNED (reject-to-identity) when fewer than
    *min_matched* sources across the whole group match one-to-one within
    *match_radius* of a reference source after the fit.
    """
    detectors = list(detectors)
    if not detectors:
        return GroupSolution(key, 'NOT_ALIGNED', None, None, None, 0, [])

    if not _has_radec(refcat) or len(refcat) < 3:
        log(f"align solve[{key}]: reference catalog missing 'RA'/'DEC' or "
            f"has <3 sources; NOT_ALIGNED (WCS preserved).")
        return _not_aligned(key, [(d.detector, d.wcs) for d in detectors],
                            lambda name, wcs: wcs)

    # 1. Footprint-clip the field refcat to this exposure's coverage + border,
    #    so the bootstrap cap keeps in-frame sources rather than the globally
    #    brightest (nearly all off-frame). Fail-open (keep the full refcat) on
    #    any geometry error — a per-exposure worker must never crash the field.
    try:
        clip = clip_refcat_to_exposure(refcat, [d.wcs for d in detectors],
                                       border_arcmin=ref_border_arcmin)
        if clip.clipped:
            log(f"align solve[{key}]: refcat footprint clip "
                f"{clip.n_kept}/{clip.n_total} sources "
                f"(border {ref_border_arcmin:.2f}').")
        refcat = clip.table
    except Exception as e:  # noqa: BLE001 — worker robustness; fail open
        log(f"align solve[{key}]: footprint clip failed ({type(e).__name__}: "
            f"{e}); using the full refcat.")
    if len(refcat) < 3:
        log(f"align solve[{key}]: only {len(refcat)} refcat sources inside the "
            f"exposure footprint; NOT_ALIGNED (source-starved / "
            f"outside-acquisition-bound).")
        return _not_aligned(key, [(d.detector, d.wcs) for d in detectors],
                            lambda name, wcs: wcs)

    if matcher is None:
        matcher = TriangleMatch(bootstrap_max=bootstrap_max)

    wcs_by_det = {d.detector: d.wcs for d in detectors}

    # 2. Bootstrap: coarse shared shift+rotation from the triangle matcher. A
    #    matcher/align_wcs *exception* (e.g. tweakwcs source confusion on a
    #    crowded field) must degrade to NOT_ALIGNED, never crash the worker — the
    #    exposure is then surfaced loudly and quarantined, not silently dropped.
    try:
        correctors, fit_info = _shared_fit(
            detectors, wcs_by_det, refcat, matcher, key=key, fitgeom=fitgeom,
            minobj=minobj, nclip=nclip, sigma=sigma)
    except Exception as e:  # noqa: BLE001 — worker robustness; degrade gracefully
        log(f"align solve[{key}]: bootstrap raised {type(e).__name__}: {e}; "
            f"NOT_ALIGNED (WCS preserved).")
        return _not_aligned(key, [(d.detector, d.wcs) for d in detectors],
                            lambda name, wcs: wcs)
    if not _succeeded(fit_info):
        log(f"align solve[{key}]: bootstrap fit "
            f"{fit_info.get('status', 'FAILED')}; NOT_ALIGNED (WCS preserved).")
        return _not_aligned(
            key, [(c.meta['name'], c) for c in correctors],
            lambda name, c: c.original_wcs)

    shift = tuple(_as_float(v) for v in np.asarray(fit_info['shift']).ravel()[:2])
    rot_deg = _as_float(fit_info.get('proper_rot', fit_info.get('rot')))
    rmse = _as_float(fit_info.get('rmse'))
    wcs_by_det = {c.meta['name']: c.wcs for c in correctors}

    # 3. Refine: all-source one-to-one XYXYMatch, robust rshift, iterated to
    #    convergence. No 2-D histogram re-acquisition (the bootstrap already
    #    seeded the offset); each pass rebuilds correctors from the current best
    #    WCS so the fitted delta stays small and well-conditioned.
    for _ in range(max(0, int(refine_niter))):
        refine_match = XYXYMatch(use2dhist=False, searchrad=refine_searchrad,
                                 tolerance=refine_tolerance)
        try:
            r_correctors, r_info = _shared_fit(
                detectors, wcs_by_det, refcat, refine_match, key=key,
                fitgeom=fitgeom, minobj=minobj, nclip=nclip, sigma=sigma)
        except Exception as e:  # noqa: BLE001 — XYXYMatch can raise on crowded
            # fields (source confusion). A refine crash must not lose a good
            # bootstrap solution; keep the last good transform and stop refining.
            log(f"align solve[{key}]: refine pass raised {type(e).__name__}: "
                f"{e}; keeping the last good transform.")
            break
        if not _succeeded(r_info):
            break  # keep the last good (bootstrap or prior refine) transform
        wcs_by_det = {c.meta['name']: c.wcs for c in r_correctors}
        correctors = r_correctors
        rmse = _as_float(r_info.get('rmse'))
        delta = np.asarray(r_info['shift'], dtype=float).ravel()[:2]
        if float(np.hypot(*delta)) < _REFINE_CONVERGE_ARCSEC:
            break

    ref_sky = SkyCoord(np.asarray(refcat['RA'], dtype=float),
                       np.asarray(refcat['DEC'], dtype=float), unit='deg')

    # Recompute matches directly (one-to-one), then reject-to-identity if the
    # "successful" fit did not actually put sources onto the reference.
    prelim = [_match(corr, d.catalog, ref_sky, match_radius)
              for corr, d in zip(correctors, detectors)]
    group_nmatched = sum(n for _, n, _ in prelim)
    if group_nmatched < min_matched:
        log(f"align solve[{key}]: fit matched {group_nmatched} < {min_matched} "
            f"sources one-to-one within {match_radius}\"; rejecting to "
            f"NOT_ALIGNED.")
        return _not_aligned(
            key, [(c.meta['name'], c) for c in correctors],
            lambda name, c: c.original_wcs)

    solutions = []
    for corr, d, (resid, nmatch, ref_idx) in zip(correctors, detectors, prelim):
        within = bool(np.isfinite(resid) and resid <= tolerance)
        dof, out_wcs = 'shared', corr.wcs

        if (adaptive and not within and np.isfinite(resid)
                and len(ref_idx) >= adaptive_min_matches):
            # Refit this detector alone (shift only) against ONLY the reference
            # sources it already matched — a distractor-free local catalog, so a
            # single small detector cannot mismatch to a far region. Deep-copy
            # so a rejected refit never touches the shared corrector.
            trial = copy.deepcopy(corr)
            trial.meta['group_id'] = f'{key}:{d.detector}'   # distinct -> solo fit
            local_refcat = refcat[ref_idx]
            # Use the translation-invariant bootstrap matcher here, NOT XYXYMatch:
            # this detector is out of tolerance precisely because its residual is
            # large (up to match_radius), and a nearest-neighbour matcher capped at
            # refine_tolerance could not re-pair sources it can't already see. The
            # local refcat is distractor-free, so the triangle matcher recovers the
            # correspondence at any offset, then a shift-only fit removes it.
            try:
                align_wcs([trial], refcat=local_refcat, enforce_user_order=True,
                          expand_refcat=False, minobj=None, match=matcher,
                          fitgeom='shift', nclip=nclip, sigma=(sigma, 'rmse'))
                tfi = trial.meta.get('fit_info', {})
            except Exception as e:  # noqa: BLE001 — keep the shared solution
                log(f"align solve[{key}]: adaptive refit for {d.detector} raised "
                    f"{type(e).__name__}: {e}; keeping the shared solution.")
                tfi = {}
            if (_succeeded(tfi)
                    and int(tfi.get('nmatches', 0)) >= adaptive_min_matches):
                new_resid, new_n, _ = _match(trial, d.catalog, ref_sky,
                                             match_radius)
                if np.isfinite(new_resid) and new_resid < resid:
                    out_wcs, dof = trial.wcs, 'shift'
                    resid, nmatch = new_resid, new_n
                    within = bool(resid <= tolerance)

        solutions.append(DetectorSolution(d.detector, out_wcs, dof,
                                          resid, nmatch, within))

    return GroupSolution(key, 'SOLVED', shift, rot_deg, rmse,
                         group_nmatched, solutions)
