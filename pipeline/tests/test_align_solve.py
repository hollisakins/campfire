"""Tests for the NIRCam align solve core (nircam/align/solve.py).

Uses a CRDS-free mock JWST gwcs (see _align_gwcs). For each detector we build a
"truth" gwcs mapping a pixel grid to a sky patch (-> the reference catalog) and
an "input" gwcs carrying a known injected offset (and, optionally, a differential
roll); the coarse+fine solve must recover it.
"""

import copy

import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.table import Table

from _align_gwcs import HAVE_MOCK_WCS, make_mock_wcs
from campfire_pipeline.nircam.align.solve import (
    DetectorInput,
    solve_exposure_group,
)
from campfire_pipeline.nircam.align.solve import _match

pytestmark = pytest.mark.skipif(
    not HAVE_MOCK_WCS, reason="tweakwcs mock-gwcs test helper unavailable")

_V2, _V3, _ROLL = 120.0, 500.0, 30.0
_BASE_RA, _BASE_DEC = 80.0, -30.0
_COSD = np.cos(np.deg2rad(_BASE_DEC))


def _mock(crval, roll=_ROLL, cd=None):
    cd = [[1e-5, 0], [0, 1e-5]] if cd is None else cd
    return make_mock_wcs(v2ref=_V2, v3ref=_V3, roll=roll,
                         crpix=[512, 512], cd=cd, crval=list(crval))


def _rot_cd(deg, scale=1e-5):
    th = np.radians(deg)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    return (R @ (scale * np.eye(2))).tolist()


def _wcsinfo(roll=_ROLL):
    return {'v2_ref': _V2, 'v3_ref': _V3, 'roll_ref': roll}


def _build_group(n_det=3, n_src=40, offset=(2.0, 0.0), roll=_ROLL,
                 input_roll=None, seed=0, per_det_extra=None):
    """Return (detectors, refcat) with a shared injected *offset* (arcsec) and,
    when *input_roll* differs from *roll*, a shared differential roll.

    The refcat is generated from the truth WCS (at *roll*); each detector's input
    WCS carries the offset (and *input_roll*), so the solve must recover both.
    ``per_det_extra`` maps detector index -> (dx, dy) arcsec added on top of the
    shared offset for that detector only (to exercise the fine per-detector fit).
    """
    rng = np.random.default_rng(seed)
    input_roll = roll if input_roll is None else input_roll
    per_det_extra = per_det_extra or {}
    ref_ra, ref_dec, detectors = [], [], []
    for i in range(n_det):
        # each detector on its own sky patch so the pooled catalog is non-degenerate
        crval_truth = [_BASE_RA + i * 0.02 / _COSD, _BASE_DEC]
        wt = _mock(crval_truth, roll=roll)
        x = rng.uniform(50, 950, n_src)
        y = rng.uniform(50, 2000, n_src)
        ra_t, dec_t = wt(x, y)
        ref_ra.append(np.asarray(ra_t, float))
        ref_dec.append(np.asarray(dec_t, float))

        dx, dy = offset
        ex, ey = per_det_extra.get(i, (0.0, 0.0))
        crval_off = [crval_truth[0] + (dx + ex) / 3600.0 / _COSD,
                     crval_truth[1] + (dy + ey) / 3600.0]
        wo = _mock(crval_off, roll=input_roll)
        cat = Table({'x': x, 'y': y, 'mag': rng.uniform(18, 24, n_src)})
        detectors.append(DetectorInput(f'nrc{i}', wo, _wcsinfo(input_roll), cat))

    refcat = Table({'RA': np.concatenate(ref_ra),
                    'DEC': np.concatenate(ref_dec)})
    refcat.meta['name'] = 'truth'
    return detectors, refcat


# --- coarse pooled solve ----------------------------------------------------

def test_recovers_shared_translation():
    detectors, refcat = _build_group(n_det=3, offset=(2.0, 0.0))
    sol = solve_exposure_group(detectors, refcat, key='exp')
    assert sol.status == 'SOLVED'
    assert len(sol.detectors) == 3
    # the fine fit now always runs; a healthy detector either keeps the coarse
    # attitude (no strict improvement) or accepts an rshift refinement
    assert all(ds.dof in ('coarse', 'rshift') for ds in sol.detectors)
    assert all(ds.within_tolerance for ds in sol.detectors)
    assert all(ds.residual_arcsec < 0.02 for ds in sol.detectors)
    assert abs(np.hypot(*sol.shift) - 2.0) < 0.1


def test_recovers_shift_and_rotation():
    # Inject a genuine 0.3 deg field rotation (rotated input CD matrix) on top of
    # a ~1" translation. The rotation gives up to ~0.3" displacement across the
    # frame that a shift-only fit could never remove, so recovering it to
    # < 0.03" proves the coarse rshift fit the ROTATION, and rot_deg reports it.
    rng = np.random.default_rng(3)
    crval = [_BASE_RA, _BASE_DEC]
    x = rng.uniform(50, 950, 60)
    y = rng.uniform(50, 2000, 60)
    ra_t, dec_t = _mock(crval)(x, y)                         # truth positions
    refcat = Table({'RA': np.asarray(ra_t, float),
                    'DEC': np.asarray(dec_t, float)})
    refcat.meta['name'] = 'truth'
    crval_off = [crval[0] + 1.0 / 3600.0 / _COSD, crval[1] - 0.5 / 3600.0]
    wo = _mock(crval_off, cd=_rot_cd(0.3))                   # rotated + shifted
    det = [DetectorInput('nrca1', wo, _wcsinfo(),
                         Table({'x': x, 'y': y,
                                'mag': rng.uniform(18, 24, 60)}))]
    sol = solve_exposure_group(det, refcat, key='exp')
    assert sol.status == 'SOLVED'
    assert sol.detectors[0].within_tolerance
    assert sol.detectors[0].residual_arcsec < 0.03
    assert abs(sol.rot_deg) > 0.1           # a real ~0.3 deg rotation was fit


def test_single_detector_group():
    # LW-per-module case: one detector solved on its own.
    detectors, refcat = _build_group(n_det=1, offset=(2.0, 0.0))
    sol = solve_exposure_group(detectors, refcat, key='exp')
    assert sol.status == 'SOLVED'
    assert len(sol.detectors) == 1
    assert sol.detectors[0].within_tolerance


# --- fine per-detector fit --------------------------------------------------

def test_fine_frees_outlier_detector():
    # 4 aligned detectors + 1 carrying an extra per-detector dec offset. The pool
    # coarse ties the 4; the outlier is over tolerance and gets a gated fine fit.
    detectors, refcat = _build_group(
        n_det=5, offset=(2.0, 0.0), per_det_extra={4: (0.0, 0.3)})
    # tolerance 0.15": the 0.3" outlier perturbs the pooled fit by ~0.06" (1/5 of
    # sources), so the 4 good detectors stay under tolerance while the outlier
    # (~0.24" residual) trips it and earns a fine fit.
    sol = solve_exposure_group(detectors, refcat, key='exp', tolerance=0.15)
    assert sol.status == 'SOLVED'
    good = sol.detectors[:4]
    outlier = sol.detectors[4]
    assert all(ds.within_tolerance for ds in good)
    assert outlier.dof in ('rshift', 'shift', 'general')
    assert outlier.within_tolerance
    assert outlier.residual_arcsec < 0.15


def test_fine_ceiling_shift_only():
    # With fine_fitgeom='shift' the ceiling caps the ladder at a shift, so an
    # over-tolerance detector is corrected shift-only (never rshift/general).
    detectors, refcat = _build_group(
        n_det=5, offset=(2.0, 0.0), per_det_extra={4: (0.0, 0.3)})
    sol = solve_exposure_group(detectors, refcat, key='exp', tolerance=0.15,
                               fine_fitgeom='shift')
    assert sol.detectors[4].dof in ('shift', 'coarse')


def test_few_matches_keeps_coarse():
    # A detector with too few matches for any fine geometry keeps the coarse
    # attitude rather than fitting an under-constrained per-detector correction.
    # (Gate disabled: this test exercises the ladder degrade, and the outlier's
    # ~0.24" coarse residual would otherwise trip the residual backstop.)
    detectors, refcat = _build_group(
        n_det=5, offset=(2.0, 0.0), per_det_extra={4: (0.0, 0.3)})
    sol = solve_exposure_group(detectors, refcat, key='exp', tolerance=0.15,
                               fine_min_shift=999, fine_min_rshift=999,
                               fine_min_general=999, max_residual_arcsec=None)
    assert sol.detectors[4].dof == 'coarse'
    assert not sol.detectors[4].within_tolerance


# --- residual gate (backstop) ------------------------------------------------

def test_residual_gate_rejects_bad_detector():
    # Random per-source scatter no rigid fit can remove: detector 0's residual
    # stays ~0.2" while the others solve to ~0. The gate must reject detector 0
    # individually (aligned=False, input WCS preserved) and keep the pool
    # SOLVED for the healthy detectors.
    detectors, refcat = _build_group(n_det=3, offset=(2.0, 0.0), seed=2)
    rng = np.random.default_rng(11)
    cat = detectors[0].catalog
    # mock WCS scale ~2.06"/px -> sigma 0.1 px ~ 0.21"/axis, median 2-D
    # separation ~0.24" — over the 0.1" gate, inside the 0.5" match radius.
    cat['x'] = np.asarray(cat['x'], float) + rng.normal(0, 0.1, len(cat))
    cat['y'] = np.asarray(cat['y'], float) + rng.normal(0, 0.1, len(cat))
    original = copy.deepcopy(detectors[0].wcs)
    sol = solve_exposure_group(detectors, refcat, key='exp')
    assert sol.status == 'SOLVED'
    bad, good = sol.detectors[0], sol.detectors[1:]
    assert not bad.aligned
    assert bad.dof == 'identity'
    assert bad.residual_arcsec > 0.1
    assert bad.wcs(500, 500) == original(500, 500)     # input WCS preserved
    assert all(g.aligned and g.residual_arcsec < 0.1 for g in good)


def test_residual_gate_all_rejected_pool_not_aligned():
    # Gate at 0: every detector's (tiny but nonzero) residual trips it, so the
    # pool as a whole reads NOT_ALIGNED and the orchestration warning fires.
    detectors, refcat = _build_group(n_det=2, offset=(2.0, 0.0))
    sol = solve_exposure_group(detectors, refcat, key='exp',
                               max_residual_arcsec=0.0)
    assert sol.status == 'NOT_ALIGNED'
    assert all(not ds.aligned for ds in sol.detectors)
    assert all(ds.dof == 'identity' for ds in sol.detectors)


# --- NOT_ALIGNED ------------------------------------------------------------

def test_too_few_refcat_sources_not_aligned():
    detectors, _ = _build_group(n_det=2)
    refcat = Table({'RA': [80.0, 80.1], 'DEC': [-30.0, -30.1]})   # <3 rows
    originals = [copy.deepcopy(d.wcs) for d in detectors]
    sol = solve_exposure_group(detectors, refcat, key='exp')
    assert sol.status == 'NOT_ALIGNED'
    assert all(ds.dof == 'identity' for ds in sol.detectors)
    for ds, orig in zip(sol.detectors, originals):
        assert ds.wcs(500, 500) == orig(500, 500)


def test_no_geometric_match_not_aligned():
    detectors, _ = _build_group(n_det=2, seed=1)
    rng = np.random.default_rng(99)
    # a reference catalog with no asterism in common with the sources
    refcat = Table({'RA': 80.0 + rng.uniform(-0.05, 0.05, 30),
                    'DEC': -30.0 + rng.uniform(-0.05, 0.05, 30)})
    sol = solve_exposure_group(detectors, refcat, key='exp')
    assert sol.status == 'NOT_ALIGNED'
    assert all(ds.dof == 'identity' for ds in sol.detectors)


# --- footprint clip ---------------------------------------------------------

def test_footprint_clip_survives_refcat_decoys():
    # refcat = in-frame truth + a large pile of far (~1 deg) decoys. The
    # footprint clip keeps only the in-frame sources so the coarse matcher sees
    # real correspondences and the solve succeeds.
    from astropy.table import vstack
    detectors, refcat = _build_group(n_det=2, offset=(2.0, 0.0))
    rng = np.random.default_rng(11)
    decoy = Table({'RA': _BASE_RA + 1.0 + rng.uniform(-0.05, 0.05, 500),
                   'DEC': _BASE_DEC + 1.0 + rng.uniform(-0.05, 0.05, 500)})
    big = vstack([refcat, decoy], metadata_conflicts='silent')

    sol = solve_exposure_group(detectors, big, key='exp')
    assert sol.status == 'SOLVED'
    assert all(ds.within_tolerance for ds in sol.detectors)
    assert abs(np.hypot(*sol.shift) - 2.0) < 0.1


# --- robustness: exceptions never crash the worker --------------------------

def test_matcher_exception_degrades_to_not_aligned(monkeypatch):
    # An unforeseen matcher crash must be swallowed and degrade to NOT_ALIGNED
    # (WCS preserved), never propagate out of the solve and abort the align
    # worker.
    from tweakwcs.matchutils import MatchCatalogs
    import campfire_pipeline.nircam.align.solve as _s

    class _BoomMatch(MatchCatalogs):
        def __init__(self, *a, **k):
            pass

        def __call__(self, refcat, imcat, **k):
            raise RuntimeError("simulated matcher crash")

    monkeypatch.setattr(_s, 'OffsetHistogramMatch', _BoomMatch)
    detectors, refcat = _build_group(n_det=2, offset=(2.0, 0.0))
    originals = [copy.deepcopy(d.wcs) for d in detectors]
    sol = solve_exposure_group(detectors, refcat, key='exp')
    assert sol.status == 'NOT_ALIGNED'
    for ds, orig in zip(sol.detectors, originals):
        assert ds.wcs(500, 500) == orig(500, 500)


# --- residual helper --------------------------------------------------------

def test_match_measures_small_offset():
    # a single detector offset by 0.2 arcsec, matched within a 0.5 arcsec radius
    detectors, refcat = _build_group(n_det=1, offset=(0.2, 0.0))
    from tweakwcs.correctors import JWSTWCSCorrector
    d = detectors[0]
    corr = JWSTWCSCorrector(d.wcs, d.wcsinfo,
                            meta={'catalog': d.catalog, 'group_id': 'g',
                                  'name': d.detector})
    ref_sky = SkyCoord(refcat['RA'], refcat['DEC'], unit='deg')
    resid, n, src_idx, ref_idx = _match(corr, d.catalog, ref_sky,
                                        match_radius=0.5)
    assert n >= 30
    assert 0.15 < resid < 0.25
    assert len(ref_idx) == len(src_idx) == n
    # mutual NN => a genuine one-to-one pairing on both sides
    assert len(np.unique(ref_idx)) == n
    assert len(np.unique(src_idx)) == n


# --- clustered extragalactic refcat (the XYXYMatch overflow regime) ---------

def test_solves_substructured_refcat_regime():
    # The COSMOS failure mode: detections and refcat rows are the same
    # (clustered) galaxies, and the refcat resolves substructure — several
    # reference rows within ~2" of one detection. XYXYMatch's pair enumeration
    # died here with MatchSourceConfusionError (39% of COSMOS LW exposures);
    # the histogram-consensus matcher must solve it.
    rng = np.random.default_rng(55)
    crval = [_BASE_RA, _BASE_DEC]
    x = rng.uniform(50, 950, 300)
    y = rng.uniform(50, 2000, 300)
    wt = _mock(crval)
    ra_t, dec_t = (np.asarray(v, float) for v in wt(x, y))

    # substructure: clone layers offset ~1" around each truth position, plus a
    # diffuse junk floor — refcat ~6x denser than the detections
    cosd = _COSD
    ras, decs = [ra_t], [dec_t]
    for _ in range(6):
        sel = rng.random(300) < 0.6
        n = int(sel.sum())
        ras.append(ra_t[sel] + rng.normal(0, 1.0, n) / 3600.0 / cosd)
        decs.append(dec_t[sel] + rng.normal(0, 1.0, n) / 3600.0)
    ras.append(_BASE_RA + rng.uniform(-0.015, 0.015, 600) / cosd)
    decs.append(_BASE_DEC + rng.uniform(-0.015, 0.015, 600))
    refcat = Table({'RA': np.concatenate(ras), 'DEC': np.concatenate(decs)})
    refcat.meta['name'] = 'clustered'

    crval_off = [crval[0] + 0.4 / 3600.0 / cosd, crval[1] - 0.3 / 3600.0]
    det = [DetectorInput('nrcblong', _mock(crval_off), _wcsinfo(),
                         Table({'x': x, 'y': y,
                                'mag': rng.uniform(18, 24, 300)}))]
    sol = solve_exposure_group(det, refcat, key='exp')
    assert sol.status == 'SOLVED'
    assert sol.detectors[0].within_tolerance
    assert sol.detectors[0].n_matched >= 150
    assert abs(np.hypot(*sol.shift) - 0.5) < 0.1     # hypot(0.4, 0.3)


# --- calibrated-mag plumbing (delta_mag_lim through the solve) ---------------

def test_delta_mag_lim_plumbs_through_solve():
    # Catalogs marked calibrated + a refcat 'mag' column: with agreeing mags,
    # delta_mag_lim must not cost matches (pairs flow id->mag into the
    # matcher); an absurd window that rejects every judged pair must reject
    # the pool (proves the cut is actually reaching the matcher).
    detectors, refcat = _build_group(n_det=2, offset=(1.0, 0.0))
    refcat['mag'] = 22.0
    for d in detectors:
        d.catalog['mag'] = 22.0                     # agrees: dmag = 0
        d.catalog.meta['mag_calibrated'] = True

    sol = solve_exposure_group(detectors, refcat, key='exp',
                               delta_mag_lim=(-3.0, 4.0))
    assert sol.status == 'SOLVED'
    assert sol.n_matched >= 60

    sol_bad = solve_exposure_group(detectors, refcat, key='exp',
                                   delta_mag_lim=(5.0, 6.0))
    assert sol_bad.status == 'NOT_ALIGNED'


# --- fine fit is no longer gated on tolerance --------------------------------

def test_fine_removes_subtolerance_systematic_offset():
    # The motivating case for removing the tolerance gate: one detector carries
    # a small systematic offset (0.03" ~ one SW pixel) that stays UNDER the
    # 0.05" tolerance. Previously it kept the coarse attitude (gate never
    # fired) and the offset shipped; now the always-on fine fit removes it.
    detectors, refcat = _build_group(
        n_det=3, offset=(2.0, 0.0), per_det_extra={2: (0.0, 0.03)})
    sol = solve_exposure_group(detectors, refcat, key='exp')
    assert sol.status == 'SOLVED'
    biased = sol.detectors[2]
    assert biased.dof in ('rshift', 'shift', 'general')
    assert biased.residual_arcsec < 0.01
    assert biased.within_tolerance
