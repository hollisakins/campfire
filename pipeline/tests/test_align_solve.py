"""Tests for the NIRCam align solve core (nircam/align/solve.py).

Uses a CRDS-free mock JWST gwcs (see _align_gwcs). For each detector we build a
"truth" gwcs mapping a pixel grid to a sky patch (-> the reference catalog) and
an "input" gwcs carrying a known injected offset; the solve must recover it.
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


def _mock(crval, roll=_ROLL):
    return make_mock_wcs(v2ref=_V2, v3ref=_V3, roll=roll,
                         crpix=[512, 512], cd=[[1e-5, 0], [0, 1e-5]],
                         crval=list(crval))


def _wcsinfo(roll=_ROLL):
    return {'v2_ref': _V2, 'v3_ref': _V3, 'roll_ref': roll}


def _build_group(n_det=3, n_src=40, offset=(2.0, 0.0), roll=_ROLL, seed=0,
                 per_det_extra=None):
    """Return (detectors, refcat) with a shared injected *offset* (arcsec).

    ``per_det_extra`` maps detector index -> (dx, dy) arcsec added on top of the
    shared offset for that detector only (to exercise the adaptive path).
    """
    rng = np.random.default_rng(seed)
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
        wo = _mock(crval_off, roll=roll)
        cat = Table({'x': x, 'y': y, 'mag': rng.uniform(18, 24, n_src)})
        detectors.append(DetectorInput(f'nrc{i}', wo, _wcsinfo(roll), cat))

    refcat = Table({'RA': np.concatenate(ref_ra),
                    'DEC': np.concatenate(ref_dec)})
    refcat.meta['name'] = 'truth'
    return detectors, refcat


# --- shared solve -----------------------------------------------------------

def test_recovers_shared_translation():
    detectors, refcat = _build_group(n_det=3, offset=(2.0, 0.0))
    sol = solve_exposure_group(detectors, refcat, key='exp')
    assert sol.status == 'SOLVED'
    assert len(sol.detectors) == 3
    assert all(ds.dof == 'shared' for ds in sol.detectors)
    assert all(ds.within_tolerance for ds in sol.detectors)
    assert all(ds.residual_arcsec < 0.02 for ds in sol.detectors)
    # shared shift magnitude ~ 2 arcsec
    assert abs(np.hypot(*sol.shift) - 2.0) < 0.1


def test_recovers_shift_and_rotation():
    detectors, refcat = _build_group(n_det=3, offset=(1.5, -0.8), roll=45.0)
    sol = solve_exposure_group(detectors, refcat, key='exp')
    assert sol.status == 'SOLVED'
    assert all(ds.within_tolerance for ds in sol.detectors)


def test_single_detector_group():
    detectors, refcat = _build_group(n_det=1, offset=(2.0, 0.0))
    sol = solve_exposure_group(detectors, refcat, key='exp')
    assert sol.status == 'SOLVED'
    assert len(sol.detectors) == 1
    assert sol.detectors[0].within_tolerance


# --- adaptive per-detector shift --------------------------------------------

def test_adaptive_frees_outlier_detector():
    # 4 aligned detectors + 1 carrying an extra per-detector dec offset.
    detectors, refcat = _build_group(
        n_det=5, offset=(2.0, 0.0), per_det_extra={4: (0.0, 0.3)})
    sol = solve_exposure_group(detectors, refcat, key='exp',
                               tolerance=0.15, adaptive=True)
    assert sol.status == 'SOLVED'
    good = sol.detectors[:4]
    outlier = sol.detectors[4]
    assert all(ds.dof == 'shared' and ds.within_tolerance for ds in good)
    assert outlier.dof == 'shift'
    assert outlier.within_tolerance
    assert outlier.residual_arcsec < 0.1


def test_adaptive_off_leaves_outlier_over_tolerance():
    detectors, refcat = _build_group(
        n_det=5, offset=(2.0, 0.0), per_det_extra={4: (0.0, 0.3)})
    sol = solve_exposure_group(detectors, refcat, key='exp',
                               tolerance=0.15, adaptive=False)
    outlier = sol.detectors[4]
    assert outlier.dof == 'shared'
    assert not outlier.within_tolerance


# --- NOT_ALIGNED ------------------------------------------------------------

def test_too_few_refcat_sources_not_aligned():
    detectors, _ = _build_group(n_det=2)
    refcat = Table({'RA': [80.0, 80.1], 'DEC': [-30.0, -30.1]})   # <3 rows
    originals = [copy.deepcopy(d.wcs) for d in detectors]
    sol = solve_exposure_group(detectors, refcat, key='exp')
    assert sol.status == 'NOT_ALIGNED'
    assert all(ds.dof == 'identity' for ds in sol.detectors)
    # original WCS preserved (same sky for a probe pixel)
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
    # refcat = in-frame truth + a large pile of far (~1 deg) decoys. Without the
    # footprint clip the bootstrap cap would keep mostly decoys and starve; with
    # it, only the in-frame sources reach the matcher and the solve succeeds.
    from astropy.table import vstack
    detectors, refcat = _build_group(n_det=2, offset=(2.0, 0.0))
    rng = np.random.default_rng(11)
    decoy = Table({'RA': _BASE_RA + 1.0 + rng.uniform(-0.05, 0.05, 500),
                   'DEC': _BASE_DEC + 1.0 + rng.uniform(-0.05, 0.05, 500)})
    big = vstack([refcat, decoy], metadata_conflicts='silent')

    sol = solve_exposure_group(detectors, big, key='exp', bootstrap_max=150)
    assert sol.status == 'SOLVED'
    assert all(ds.within_tolerance for ds in sol.detectors)
    assert abs(np.hypot(*sol.shift) - 2.0) < 0.1


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
    resid, n, ref_idx = _match(corr, d.catalog, ref_sky, match_radius=0.5)
    assert n >= 30
    assert 0.15 < resid < 0.25
    assert len(ref_idx) >= 30
