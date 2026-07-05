"""Per-exposure astrometric solve for the NIRCam ``align`` phase.

Given, for one exposure group, each detector's gwcs + detected source catalog
and a static Gaia-tied reference catalog, this fits ONE shared shift+rotation
for the whole exposure via ``tweakwcs`` (SIAF distortion untouched), then — where
a detector's residual still exceeds tolerance — frees an adaptive per-detector
shift. It returns the corrected gwcs per detector plus solve diagnostics; it does
no FITS I/O (the exposure reader/writer + ``CFP_ALGN`` stamp live in the
orchestration layer).

The shared solve is the mechanical expression of the pooling constraint: every
detector of one exposure shares one ``group_id`` so a single rigid rshift is fit
from the pooled catalog and applied to all of them. Distinct per-exposure
``group_id``s + a static reference catalog + ``expand_refcat=False`` keep
exposures independent (each ties to the same reference on its own); we
deliberately do NOT use ``stcal``'s ``group_id=987654`` global collapse.

Per-detector residuals are recomputed DIRECTLY (``det_to_world`` vs matched
refcat positions), not read from ``tweakwcs``'s group-level ``fit_info`` — which
carries no per-source residuals, only a fragile group-catalog join.
"""

import copy
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from astropy.coordinates import SkyCoord
from tweakwcs.correctors import JWSTWCSCorrector
from tweakwcs.imalign import align_wcs

from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.align.matcher import TriangleMatch


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
    """Direct residual for one detector: ``(residual_arcsec, n_matched,
    matched_ref_idx)``.

    Transform the detector's own sources through the (corrected) gwcs and
    nearest-neighbour match them to the reference positions, keeping only
    matches within *match_radius* arcsec. ``matched_ref_idx`` is the set of
    reference rows those sources landed on — reused to build a distractor-free
    local reference catalog for the adaptive per-detector refit.
    """
    empty = (float('nan'), 0, np.array([], dtype=int))
    x = np.asarray(catalog['x'], dtype=float)
    y = np.asarray(catalog['y'], dtype=float)
    if x.size == 0:
        return empty
    ra, dec = corrector.det_to_world(x, y)
    src = SkyCoord(np.asarray(ra, dtype=float), np.asarray(dec, dtype=float),
                   unit='deg')
    idx, d2d, _ = src.match_to_catalog_sky(ref_sky)
    sep = d2d.arcsec
    keep = np.isfinite(sep) & (sep <= match_radius)
    if not np.any(keep):
        return empty
    return (float(np.median(sep[keep])), int(np.count_nonzero(keep)),
            np.unique(np.asarray(idx)[keep]))


def _not_aligned(key, detectors, wcs_getter):
    return GroupSolution(
        key=key, status='NOT_ALIGNED', shift=None, rot_deg=None,
        rmse_arcsec=None, n_matched=0,
        detectors=[DetectorSolution(name, wcs_getter(name, obj), 'identity',
                                    float('nan'), 0, False)
                   for name, obj in detectors],
    )


def solve_exposure_group(detectors, refcat, *, key='group', matcher=None,
                         fitgeom='rshift', minobj=None, nclip=3, sigma=3.0,
                         tolerance=0.05, adaptive=True, adaptive_min_matches=3,
                         match_radius=0.5, min_matched=4):
    """Solve one exposure group; return a :class:`GroupSolution`.

    Parameters mirror ``tweakwcs.align_wcs`` where relevant. *tolerance* and
    *match_radius* are in **arcsec**. With ``adaptive=True`` (the default per
    decision D2), a detector whose residual exceeds *tolerance* gets an
    individual shift-only refit against a distractor-free local reference
    subset, accepted only if it has at least *adaptive_min_matches* matches and
    measurably reduces the residual.

    ``align_wcs`` reports ``status='SUCCESS'`` even for a geometrically wrong
    fit, so we reject to NOT_ALIGNED (reject-to-identity) when fewer than
    *min_matched* sources across the whole group land within *match_radius* of a
    reference source after the shared fit.
    """
    detectors = list(detectors)
    if not detectors:
        return GroupSolution(key, 'NOT_ALIGNED', None, None, None, 0, [])

    if not _has_radec(refcat) or len(refcat) < 3:
        log(f"align solve[{key}]: reference catalog missing 'RA'/'DEC' or "
            f"has <3 sources; NOT_ALIGNED (WCS preserved).")
        return _not_aligned(key, [(d.detector, d.wcs) for d in detectors],
                            lambda name, wcs: wcs)

    if matcher is None:
        matcher = TriangleMatch()

    # One corrector per detector, ALL sharing the exposure's group_id -> one
    # shared rigid fit for the exposure.
    correctors = [
        JWSTWCSCorrector(
            d.wcs, d.wcsinfo,
            meta={'catalog': d.catalog, 'group_id': key, 'name': d.detector},
        )
        for d in detectors
    ]

    align_wcs(correctors, refcat=refcat, enforce_user_order=True,
              expand_refcat=False, minobj=minobj, match=matcher,
              fitgeom=fitgeom, nclip=nclip, sigma=(sigma, 'rmse'))

    fit_info = correctors[0].meta.get('fit_info', {})
    if not str(fit_info.get('status', '')).startswith('SUCCESS'):
        log(f"align solve[{key}]: shared fit "
            f"{fit_info.get('status', 'FAILED')}; NOT_ALIGNED (WCS preserved).")
        # set_correction only runs on success, so the WCS is untouched; return
        # each corrector's original gwcs explicitly.
        return _not_aligned(
            key, [(c.meta['name'], c) for c in correctors],
            lambda name, c: c.original_wcs)

    shift = tuple(_as_float(v) for v in np.asarray(fit_info['shift']).ravel()[:2])
    rot_deg = _as_float(fit_info.get('proper_rot', fit_info.get('rot')))
    rmse = _as_float(fit_info.get('rmse'))
    group_nmatched = int(fit_info.get('nmatches', 0))

    ref_sky = SkyCoord(np.asarray(refcat['RA'], dtype=float),
                       np.asarray(refcat['DEC'], dtype=float), unit='deg')

    # Recompute matches directly, then reject-to-identity if the "successful"
    # fit did not actually put sources onto the reference (a garbage match).
    prelim = [_match(corr, d.catalog, ref_sky, match_radius)
              for corr, d in zip(correctors, detectors)]
    if sum(n for _, n, _ in prelim) < min_matched:
        log(f"align solve[{key}]: shared fit matched "
            f"{sum(n for _, n, _ in prelim)} < {min_matched} sources within "
            f"{match_radius}\"; rejecting to NOT_ALIGNED.")
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
            align_wcs([trial], refcat=local_refcat, enforce_user_order=True,
                      expand_refcat=False, minobj=None, match=matcher,
                      fitgeom='shift', nclip=nclip, sigma=(sigma, 'rmse'))
            tfi = trial.meta.get('fit_info', {})
            if (str(tfi.get('status', '')).startswith('SUCCESS')
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
