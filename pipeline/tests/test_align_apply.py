"""Integration tests for the NIRCam align I/O layer (nircam/align/apply.py).

Builds real canonical ImageModel FITS (persistable gwcs + injected offset + DAO-
detectable sources + SRCMASK), runs align_exposure_group, and checks the on-disk
contract: CFP_ALGN stamp, the corrected gwcs persisted and now maps sources onto
the reference catalog, SRCMASK survives, NOT_ALIGNED preserves the WCS,
idempotency, and overwrite does not double-correct.
"""

import os

import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.table import Table

from _align_gwcs import HAVE_PERSISTABLE_WCS, build_canonical, make_persistable_wcs
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam.align.apply import align_exposure_group
from campfire_pipeline.nircam.association import ExposureMember

pytestmark = pytest.mark.skipif(
    not HAVE_PERSISTABLE_WCS, reason="jwst persistable-gwcs builder unavailable")

_TOKEN = 'jw01727028001_04101_00003'
_DETS = ['nrca1', 'nrca2', 'nrca3', 'nrca4', 'nrcb1']
_COSD = float(np.cos(np.deg2rad(-30.0)))
_SHAPE = (2048, 1024)


def _inject(shape, xy, rng, noise=1.0, fwhm=2.5, amp=400.0):
    sigma = fwhm / 2.3548
    img = rng.normal(0.0, noise, shape).astype(float)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    for cx, cy in zip(*xy):
        img += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))
    return img


def _make_exposure(tmp_path, n_det=2, offset=(2.0, 0.0), per_det_extra=None,
                   seed=0, srcmask=True):
    """Return (members, refcat, xy_by_detector)."""
    rng = np.random.default_rng(seed)
    per_det_extra = per_det_extra or {}
    members, ref_ra, ref_dec, xy_by = [], [], [], {}
    for i, det in enumerate(_DETS[:n_det]):
        x = rng.uniform(50, 950, 40)
        y = rng.uniform(50, 2000, 40)
        xy_by[det] = (x, y)
        base_ra = 80.0 + i * 0.02 / _COSD
        truth = make_persistable_wcs(ra_ref=base_ra, dec_ref=-30.0)
        rt, dt = truth(x, y)
        ref_ra.append(np.asarray(rt, float))
        ref_dec.append(np.asarray(dt, float))

        dx, dy = offset
        ex, ey = per_det_extra.get(i, (0.0, 0.0))
        off_ra = base_ra + (dx + ex) / 3600.0 / _COSD
        off_dec = -30.0 + (dy + ey) / 3600.0
        sci = _inject(_SHAPE, (x, y), rng)
        sm = None
        if srcmask:
            sm = np.zeros(_SHAPE, 'uint8')
            sm[100:110, 100:110] = 1
        path = str(tmp_path / f'{_TOKEN}_{det}.fits')
        build_canonical(path, sci, ra_ref=off_ra, dec_ref=off_dec, srcmask=sm)
        members.append(ExposureMember(path=path, filter_name='f200w',
                                      rootname=f'{_TOKEN}_{det}', detector=det))
    refcat = Table({'RA': np.concatenate(ref_ra),
                    'DEC': np.concatenate(ref_dec)})
    refcat.meta['name'] = 't'
    return members, refcat, xy_by


def _cfp_algn(path):
    with fits.open(path) as h:
        return h[0].header.get('CFP_ALGN', '')


def _reload_residual(path, xy, refcat):
    from jwst.datamodels import ImageModel
    m = ImageModel(path, memmap=False)
    ra, dec = m.meta.wcs(xy[0], xy[1])
    m.close()
    ref = SkyCoord(refcat['RA'], refcat['DEC'], unit='deg')
    _, d2d, _ = SkyCoord(ra, dec, unit='deg').match_to_catalog_sky(ref)
    return float(np.median(d2d.arcsec))


# --- success ----------------------------------------------------------------

def test_success_stamps_and_corrects(tmp_path):
    members, refcat, xy = _make_exposure(tmp_path, n_det=2, offset=(2.0, 0.0))
    sol = align_exposure_group(members, refcat, config={})
    assert sol.status == 'SOLVED'
    for m in members:
        assert _cfp_algn(m.path).startswith('dof=')          # non-sentinel
        # corrected gwcs persisted and now maps sources onto the reference
        assert _reload_residual(m.path, xy[m.detector], refcat) < 0.05


def test_srcmask_preserved(tmp_path):
    members, refcat, _ = _make_exposure(tmp_path, n_det=2, srcmask=True)
    align_exposure_group(members, refcat, config={})
    for m in members:
        with fits.open(m.path) as h:
            assert 'SRCMASK' in h


def test_wcs_bak_written(tmp_path):
    members, refcat, _ = _make_exposure(tmp_path, n_det=1)
    align_exposure_group(members, refcat, config={})
    with fits.open(members[0].path) as h:
        assert 'WCS_BAK' in h


# --- NOT_ALIGNED ------------------------------------------------------------

def test_not_aligned_preserves_wcs(tmp_path):
    members, _, _ = _make_exposure(tmp_path, n_det=2, seed=3)
    from jwst.datamodels import ImageModel
    before = {}
    for m in members:
        mo = ImageModel(m.path, memmap=False)
        before[m.path] = tuple(float(v) for v in mo.meta.wcs(500.0, 700.0))
        mo.close()
    rng = np.random.default_rng(99)
    badref = Table({'RA': 80.0 + rng.uniform(-0.05, 0.05, 30),
                    'DEC': -30.0 + rng.uniform(-0.05, 0.05, 30)})
    sol = align_exposure_group(members, badref, config={})
    assert sol.status == 'NOT_ALIGNED'
    for m in members:
        assert _cfp_algn(m.path) == 'NOT_ALIGNED'
        mo = ImageModel(m.path, memmap=False)
        after = tuple(float(v) for v in mo.meta.wcs(500.0, 700.0))
        mo.close()
        assert np.allclose(after, before[m.path], atol=1e-12)   # WCS untouched


# --- idempotency / overwrite ------------------------------------------------

def test_idempotent_skip(tmp_path):
    members, refcat, _ = _make_exposure(tmp_path, n_det=2)
    align_exposure_group(members, refcat, config={})
    mtimes = {m.path: os.path.getmtime(m.path) for m in members}
    sol = align_exposure_group(members, refcat, config={}, overwrite=False)
    assert sol.status == 'SKIPPED'
    assert all(os.path.getmtime(m.path) == mtimes[m.path] for m in members)


def test_overwrite_does_not_double_correct(tmp_path):
    members, refcat, xy = _make_exposure(tmp_path, n_det=2, offset=(2.0, 0.0))
    align_exposure_group(members, refcat, config={})
    align_exposure_group(members, refcat, config={}, overwrite=True)
    for m in members:
        # a double-correction would leave ~2 arcsec; a correct re-solve ~0
        assert _reload_residual(m.path, xy[m.detector], refcat) < 0.05


# --- adaptive end-to-end ----------------------------------------------------

def test_adaptive_dof_recorded(tmp_path):
    # Detector 4 carries a 0.6" per-detector offset — large enough that the
    # all-source shared refine sigma-clips it (rather than tilting the whole
    # exposure to absorb it), so its residual survives above tolerance and the
    # adaptive shift-only refit frees it. match_radius=0.8 keeps its sources
    # matchable at that offset.
    members, refcat, _ = _make_exposure(
        tmp_path, n_det=5, offset=(2.0, 0.0), per_det_extra={4: (0.0, 0.6)})
    align_exposure_group(members, refcat,
                         config={'tolerance': 0.15, 'match_radius': 0.8})
    # detectors 0-3 stay on the shared solution; detector 4 (nrcb1) is freed
    for m in members[:4]:
        assert 'dof=shared' in _cfp_algn(m.path)
    assert 'dof=shift' in _cfp_algn(members[4].path)
