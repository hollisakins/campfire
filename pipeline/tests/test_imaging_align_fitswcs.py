"""The align solver on HST-style WCS (FITS keywords + SIP), via the arcsec corrector.

Two synthetic chips with a known pointing error (shift and roll) are solved
against a reference catalog made from the true WCS. Checks that the same solver
NIRCam uses recovers the truth when handed ``astropy.wcs.WCS`` objects, and that
the arcsec-plane corrector is a faithful rescaling of tweakwcs' pixel-plane one.
"""

import numpy as np
import pytest
from astropy.table import Table
from astropy.wcs import WCS

pytest.importorskip('tweakwcs')

from campfire_pipeline.imaging.align.fitswcs import ArcsecFITSWCSCorrector  # noqa: E402
from campfire_pipeline.imaging.align.footprint import _detector_corner_sky  # noqa: E402
from campfire_pipeline.nircam.align.solve import (  # noqa: E402
    DetectorInput, solve_exposure_group,
)

_NX, _NY, _SCALE = 800, 600, 0.05           # pixels, arcsec / pixel


def _chip_wcs(crpix, crval=(40.0, 26.0), roll_deg=30.0):
    """A TAN-SIP WCS with a mild quadratic distortion, like an HST chip."""
    s = _SCALE / 3600.0
    t = np.radians(roll_deg)
    header = {
        'NAXIS': 2, 'NAXIS1': _NX, 'NAXIS2': _NY,
        'CTYPE1': 'RA---TAN-SIP', 'CTYPE2': 'DEC--TAN-SIP',
        'CRPIX1': crpix[0], 'CRPIX2': crpix[1],
        'CRVAL1': crval[0], 'CRVAL2': crval[1],
        'CD1_1': -s * np.cos(t), 'CD1_2': s * np.sin(t),
        'CD2_1': s * np.sin(t), 'CD2_2': s * np.cos(t),
        'A_ORDER': 2, 'B_ORDER': 2,
        'A_2_0': 2e-6, 'A_0_2': -1e-6, 'A_1_1': 1e-6,
        'B_2_0': -1e-6, 'B_0_2': 2e-6, 'B_1_1': -2e-6,
    }
    w = WCS(header)
    w.pixel_shape = (_NX, _NY)
    return w


def _perturbed(w, dra_arcsec, ddec_arcsec, droll_deg):
    """The same chip with a pointing error: crval moved, CD rotated."""
    p = w.deepcopy()
    cosd = np.cos(np.radians(p.wcs.crval[1]))
    p.wcs.crval = [p.wcs.crval[0] + dra_arcsec / 3600.0 / cosd,
                   p.wcs.crval[1] + ddec_arcsec / 3600.0]
    t = np.radians(droll_deg)
    rot = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
    p.wcs.cd = rot @ p.wcs.cd
    p.wcs.set()
    return p


def test_arcsec_corrector_is_a_rescaled_fits_corrector():
    from tweakwcs.correctors import FITSWCSCorrector

    w = _chip_wcs((400.0, 300.0))
    a, f = ArcsecFITSWCSCorrector(w.deepcopy()), FITSWCSCorrector(w.deepcopy())
    assert a.units == 'arcsec' and a.tp_scale == pytest.approx(_SCALE, rel=1e-6)
    x, y = np.array([10.0, 700.0]), np.array([20.0, 550.0])
    np.testing.assert_allclose(np.array(a.det_to_tanp(x, y)),
                               np.array(f.det_to_tanp(x, y)) * a.tp_scale)
    # the scaled plane round-trips, and the sky mapping is untouched
    np.testing.assert_allclose(a.tanp_to_det(*a.det_to_tanp(x, y)), (x, y), atol=1e-6)
    np.testing.assert_allclose(a.tanp_to_world(*a.world_to_tanp(40.001, 26.001)),
                               (40.001, 26.001), atol=1e-10)
    np.testing.assert_allclose(a.det_to_world(x, y), f.det_to_world(x, y))
    # the pixel scale the matcher is given is arcsec per pixel
    assert a.tanp_center_pixel_scale == pytest.approx(_SCALE, rel=1e-3)

    # a pure shift of (0.2", -0.1") in the arcsec plane moves the sky by that much
    a.set_correction(shift=[0.2, -0.1])
    ra0, dec0 = f.det_to_world(400.0, 300.0)
    ra1, dec1 = a.det_to_world(400.0, 300.0)
    moved = np.hypot((ra1 - ra0) * np.cos(np.radians(dec0)), dec1 - dec0) * 3600.0
    assert moved == pytest.approx(np.hypot(0.2, 0.1), rel=1e-3)


def test_footprint_accepts_a_fits_wcs():
    w = _chip_wcs((400.0, 300.0))
    ra, dec = _detector_corner_sky(w)
    assert len(ra) == 8
    # the sampled boundary spans the chip (800 x 600 px at 0.05"), not 2048 px
    cosd = np.cos(np.radians(26.0))
    extent = np.hypot(np.ptp(ra) * cosd, np.ptp(dec)) * 3600.0
    assert 40.0 < extent < 80.0


def test_solver_recovers_a_pointing_error_on_fits_wcs():
    rng = np.random.default_rng(11)
    true = {'c1': _chip_wcs((400.0, 650.0)), 'c2': _chip_wcs((400.0, -50.0))}
    detectors, ref_ra, ref_dec, pixels = [], [], [], {}
    for name, w in true.items():
        x = rng.uniform(20, _NX - 20, 70)
        y = rng.uniform(20, _NY - 20, 70)
        ra, dec = w.all_pix2world(x, y, 0)
        ref_ra.append(ra)
        ref_dec.append(dec)
        pixels[name] = (x, y)
        bad = _perturbed(w, dra_arcsec=0.40, ddec_arcsec=-0.25, droll_deg=0.03)
        detectors.append(DetectorInput(detector=name, wcs=bad, wcsinfo=None,
                                       catalog=Table({'x': x, 'y': y})))
    refcat = Table({'RA': np.concatenate(ref_ra), 'DEC': np.concatenate(ref_dec)})

    # before: the perturbed WCS misses the reference by ~0.5"
    x, y = pixels['c1']
    ra, dec = detectors[0].wcs.all_pix2world(x, y, 0)
    tra, tdec = true['c1'].all_pix2world(x, y, 0)
    before = np.hypot((ra - tra) * np.cos(np.radians(26.0)), dec - tdec) * 3600.0
    assert 0.3 < np.median(before) < 0.7

    sol = solve_exposure_group(detectors, refcat, key='visit', dva_repivot=False)

    assert sol.status == 'SOLVED', sol.diagnostics
    assert sol.n_matched >= 100
    # the reported coarse shift is in arcsec and of the injected size
    assert np.hypot(*sol.shift) == pytest.approx(np.hypot(0.40, 0.25), rel=0.25)
    for d in sol.detectors:
        assert d.aligned and d.n_matched >= 50
        x, y = pixels[d.detector]
        ra, dec = d.wcs.all_pix2world(x, y, 0)
        tra, tdec = true[d.detector].all_pix2world(x, y, 0)
        after = np.hypot((ra - tra) * np.cos(np.radians(26.0)), dec - tdec) * 3600.0
        assert np.max(after) < 0.003, (d.detector, np.max(after))      # 3 mas
        # the distortion terms are untouched; only CRVAL / CD moved
        assert d.wcs.sip is not None
        np.testing.assert_array_equal(d.wcs.sip.a, true[d.detector].sip.a)
