"""Re-reference the differential velocity aberration (DVA) scale to a pool-common
pivot, so a pooled rigid fit is geometrically valid.

**The defect.** ``jwst.assign_wcs`` corrects DVA per detector, scaling about
*that detector's own* aperture reference (``nircam.py`` passes
``v2_ref``/``v3_ref`` from ``meta.wcsinfo`` into
:func:`jwst.assign_wcs.pointing.dva_corr_model`, which builds
``v' = v_ref + va_scale * (v - v_ref)``). Each detector's reference point is
therefore left exactly where it was, and the *separation between* two detectors'
reference points keeps the full, uncorrected aberration::

    residual = (1 - va_scale) * |v_ref,i - v_ref,j|

For the NIRCam LW pair (|Δv_ref| = 175.34") and COSMOS's
|1 - va_scale| ~ 7e-5..1e-4, that is ~13 mas — measured on 3258 COSMOS LW
exposures as a module-to-module differential matching this prediction in sign
and magnitude (the sign tracks the observatory's radial velocity, which reverses
between the field's two visibility windows).

**Why it matters for pooling.** The residual is a pure *scale* about a point
outside each detector. A pooled ``rshift`` (rotation + translation) cannot
express a scale, so pooling detectors whose DVA is referenced to different
pivots forces that ~13 mas differential into the fit as an irreducible residual,
split between the pool's members. Solving each detector separately hides the
problem by giving every detector its own translation to absorb it.

**The fix.** Rebuild the DVA transform about a pivot shared by the whole pool,
using the same jwst primitive that created it. Per detector this is a pure
shift in V2/V3::

    Δv = (va_scale - 1) * (v_ref,detector - v_ref,pivot)

With ``pivot='pool'`` (the default) the pivot is the centroid of the pool's
reference points, so the pool's mean position is unchanged and only the
*relative* geometry moves — the least disruptive choice, and one that leaves a
single-detector pool a strict no-op.

**Scope.** A no-op for any pool of one detector (LW with ``pool_modules=false``).
For a multi-detector pool it also removes the smaller *intra*-pool residual: an
SW module spans ~130", so its four detectors currently carry up to
~(1 - va_scale) x 65" ~ 5 mas of the same error, which the pooled ``rshift``
must absorb as fit residual.

**Which pivot is "physically right" is an open STScI question — and it does not
matter here.** ``spacetelescope/jwst`` issue #9400 (JIRA JP-3987, D. Law,
2025-04-18, still open) asks precisely this: the per-aperture pivot "would be
correct if the telescope attitude information … was the telescope boresight
v2=v3=0. If the telescope attitude information is set for the guide star though,
shouldn't the dva scale be applied to the difference (v2ref - v2guider) and
(v3ref - v3guider)?" (``set_telescope_pointing.calc_gs2gsapp`` does apply an
aberration correction "in the direction of the guide star", but as a *rotation*,
which cannot rescale aperture separations — so nothing upstream fixes this.)

For a pooled solve the question is moot, because the pivot cancels out of the
relative geometry::

    v'_i - v'_j = va_scale * (v_i - v_j)      for ANY pool-common pivot

Guide star, boresight and pool centroid therefore give identical *relative*
corrections; they differ only by an overall translation of the pool, which the
fit against the reference catalog absorbs. ``pivot='pool'`` is the default purely
because it makes that leftover translation zero.
"""

import copy

from campfire_pipeline.common.io import log

__all__ = ['repivot_pool_dva', 'va_scale_from_wcs', 'DVA_PIVOTS']

# Frame names of the DVA step in the JWST NIRCam gwcs pipeline.
_FROM_FRAME, _TO_FRAME = 'v2v3', 'v2v3vacorr'
_SCALE_SUBMODEL = 'dva_scale_v2'

DVA_PIVOTS = ('pool', 'boresight')

# Largest position error (mas), across the pool's own lever arm, that the
# per-detector spread in va_scale may imply before we refuse to average it.
_MAX_SPREAD_MAS = 1.0


def va_scale_from_wcs(wcs):
    """The velocity-aberration scale baked into *wcs*, or ``None``.

    Read back from the gwcs itself (the ``DVA_Correction`` compound model's
    scale submodel) rather than from a header keyword, so the value is
    guaranteed to be the one actually applied to this WCS. Returns ``None`` when
    the pipeline has no DVA step, when it is an ``Identity`` (``va_scale`` was 1
    or absent), or when the model is not the shape we expect — every one of
    which means "nothing to re-reference".
    """
    try:
        transform = wcs.get_transform(_FROM_FRAME, _TO_FRAME)
    except Exception:                                    # noqa: BLE001
        return None
    if transform is None:
        return None
    try:
        return float(transform[_SCALE_SUBMODEL].factor.value)
    except Exception:                                    # noqa: BLE001
        return None                                      # Identity / unexpected


def _pivot_point(detectors, pivot):
    """``(v2, v3)`` in arcsec for the requested *pivot*."""
    if pivot == 'boresight':
        return 0.0, 0.0
    refs = [(float(d.wcsinfo['v2_ref']), float(d.wcsinfo['v3_ref']))
            for d in detectors]
    n = len(refs)
    return sum(v2 for v2, _ in refs) / n, sum(v3 for _, v3 in refs) / n


def repivot_pool_dva(detectors, *, pivot='pool', key='group'):
    """Return *detectors* with each DVA scale re-referenced to a common pivot.

    Parameters
    ----------
    detectors : list of DetectorInput
        One pool. Not mutated: each returned entry carries a deep-copied gwcs,
        so the caller keeps the untouched originals for the NOT_ALIGNED path
        (which contractually preserves the input WCS).
    pivot : {'pool', 'boresight'}
        ``'pool'`` — centroid of the pool's ``v2_ref``/``v3_ref`` (default;
        leaves the pool's mean position unchanged). ``'boresight'`` — V2=V3=0.
    key : str
        Pool key, for logging.

    Returns
    -------
    (list of DetectorInput, dict)
        The re-pivoted detectors and a diagnostics dict
        (``va_scale``, ``pivot``, ``pivot_v2``, ``pivot_v3``, ``max_shift_mas``).
        On any condition that makes re-referencing meaningless or unsafe the
        ORIGINAL list is returned unchanged with ``{'applied': False, ...}`` —
        this is an astrometric refinement, never a reason to fail a pool.
    """
    import dataclasses

    detectors = list(detectors)
    info = {'applied': False, 'pivot': pivot}
    if len(detectors) < 2:
        info['reason'] = 'single-detector pool (no relative geometry to fix)'
        return detectors, info
    if pivot not in DVA_PIVOTS:
        log(f"align dva[{key}]: unknown dva_pivot '{pivot}'; "
            f"expected one of {DVA_PIVOTS}. Skipping DVA re-reference.")
        info['reason'] = f'unknown pivot {pivot!r}'
        return detectors, info

    scales = [va_scale_from_wcs(d.wcs) for d in detectors]
    if any(s is None for s in scales):
        info['reason'] = 'no DVA step in one or more detector WCSes'
        return detectors, info

    pv2, pv3 = _pivot_point(detectors, pivot)
    lever = max(((float(d.wcsinfo['v2_ref']) - pv2) ** 2
                 + (float(d.wcsinfo['v3_ref']) - pv3) ** 2) ** 0.5
                for d in detectors)

    # One exposure has one velocity, but the per-detector va_scale values differ
    # in their last digits because each is evaluated at that detector's own
    # reference point. That spread is physically negligible (~1e-8 across NIRCam,
    # i.e. well under 0.1 mas even at a 500" lever arm), so average it rather
    # than refusing to act. Judge it in MILLIARCSECONDS, not as a bare float
    # comparison: what matters is the position error the spread would induce
    # across this pool. A spread big enough to matter means these detectors are
    # not really simultaneous, and averaging would be wrong.
    spread = max(scales) - min(scales)
    if spread * lever * 1e3 > _MAX_SPREAD_MAS:
        log(f"align dva[{key}]: va_scale spread {spread:.3e} over a "
            f"{lever:.1f}\" lever implies {spread * lever * 1e3:.2f} mas "
            f"(> {_MAX_SPREAD_MAS} mas); skipping DVA re-reference "
            f"(detectors may not be simultaneous).")
        info['reason'] = 'inconsistent va_scale across pool'
        return detectors, info

    va_scale = sum(scales) / len(scales)
    if va_scale == 1.0:
        info['reason'] = 'va_scale == 1 (nothing to correct)'
        return detectors, info

    from jwst.assign_wcs.pointing import dva_corr_model

    out, max_shift = [], 0.0
    for d in detectors:
        wcs = copy.deepcopy(d.wcs)
        try:
            wcs.set_transform(_FROM_FRAME, _TO_FRAME,
                              dva_corr_model(va_scale=va_scale,
                                             v2_ref=pv2, v3_ref=pv3))
        except Exception as e:                           # noqa: BLE001
            log(f"align dva[{key}]: could not re-reference DVA on "
                f"{d.detector} ({type(e).__name__}: {e}); leaving the pool "
                f"untouched.")
            return detectors, {**info, 'reason': f'{type(e).__name__}'}
        dv2 = (va_scale - 1.0) * (float(d.wcsinfo['v2_ref']) - pv2)
        dv3 = (va_scale - 1.0) * (float(d.wcsinfo['v3_ref']) - pv3)
        max_shift = max(max_shift, (dv2 ** 2 + dv3 ** 2) ** 0.5)
        out.append(dataclasses.replace(d, wcs=wcs))

    info.update(applied=True, va_scale=va_scale, pivot_v2=pv2, pivot_v3=pv3,
                max_shift_mas=max_shift * 1e3)
    log(f"align dva[{key}]: re-referenced DVA (va_scale={va_scale:.9f}) to the "
        f"{pivot} pivot (v2={pv2:.2f}\", v3={pv3:.2f}\"); largest detector "
        f"shift {max_shift * 1e3:.2f} mas.")
    return out, info
