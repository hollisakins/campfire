"""DVA re-referencing (align/dva.py).

The property under test is geometric, not statistical: after re-referencing a
pool to a shared pivot, the SEPARATION between two detectors' aperture reference
points must scale by ``va_scale`` (it does not move at all under jwst's
per-detector pivot), while the pool's centroid stays put.
"""
import numpy as np
import pytest

pytest.importorskip('jwst')
pytest.importorskip('gwcs')

from campfire_pipeline.nircam.align.dva import (  # noqa: E402
    DVA_PIVOTS, repivot_pool_dva, va_scale_from_wcs)
from campfire_pipeline.nircam.align.solve import DetectorInput  # noqa: E402

VA = 1.0000732          # a real COSMOS value
A = (85.93, -493.50)    # NRCALONG v2_ref, v3_ref (arcsec)
B = (-89.40, -491.36)   # NRCBLONG
SEP = float(np.hypot(A[0] - B[0], A[1] - B[1]))     # 175.34"


def _wcs_with_dva(v2ref, v3ref, va_scale=VA):
    """A JWST-structured gwcs carrying a DVA step about (v2ref, v3ref)."""
    import astropy.units as u
    import gwcs
    from astropy import coordinates as coord
    from astropy.modeling import models
    from jwst.assign_wcs.pointing import dva_corr_model, v23tosky
    from jwst.datamodels import ImageModel

    stub = ImageModel()
    for k, v in [('v2_ref', v2ref), ('v3_ref', v3ref), ('roll_ref', 30.0),
                 ('ra_ref', 150.1), ('dec_ref', 2.2),
                 ('v3yangle', 0.0), ('vparity', -1)]:
        setattr(stub.meta.wcsinfo, k, v)
    det = gwcs.coordinate_frames.Frame2D(name='detector', axes_order=(0, 1),
                                         unit=(u.pix, u.pix))
    v2v3 = gwcs.coordinate_frames.Frame2D(name='v2v3', axes_order=(0, 1),
                                          unit=(u.arcsec, u.arcsec))
    vacorr = gwcs.coordinate_frames.Frame2D(name='v2v3vacorr',
                                            axes_order=(0, 1),
                                            unit=(u.arcsec, u.arcsec))
    world = gwcs.coordinate_frames.CelestialFrame(
        reference_frame=coord.ICRS(), name='world')
    det2v2v3 = ((models.Shift(-512.0) & models.Shift(-512.0))
                | (models.Scale(0.063) & models.Scale(0.063))
                | (models.Shift(v2ref) & models.Shift(v3ref)))
    w = gwcs.wcs.WCS([(det, det2v2v3),
                      (v2v3, dva_corr_model(va_scale, v2ref, v3ref)),
                      (vacorr, v23tosky(stub)),
                      (world, None)])
    w.bounding_box = ((-0.5, 1023.5), (-0.5, 2047.5))
    return w


def _pool(va_a=VA, va_b=VA):
    return [
        DetectorInput('nrcalong', _wcs_with_dva(*A, va_scale=va_a),
                      {'v2_ref': A[0], 'v3_ref': A[1], 'roll_ref': 30.0}, None),
        DetectorInput('nrcblong', _wcs_with_dva(*B, va_scale=va_b),
                      {'v2_ref': B[0], 'v3_ref': B[1], 'roll_ref': 30.0}, None),
    ]


def _vacorr_of_ref(det):
    """Where this detector's own aperture reference lands in v2v3vacorr."""
    t = det.wcs.get_transform('v2v3', 'v2v3vacorr')
    return np.array(t(det.wcsinfo['v2_ref'], det.wcsinfo['v3_ref']), float)


def test_va_scale_read_back_from_wcs():
    assert va_scale_from_wcs(_wcs_with_dva(*A)) == pytest.approx(VA, rel=1e-12)


def test_va_scale_none_without_dva_step():
    from tests._align_gwcs import HAVE_PERSISTABLE_WCS, make_persistable_wcs
    if not HAVE_PERSISTABLE_WCS:
        pytest.skip('no persistable wcs builder')
    assert va_scale_from_wcs(make_persistable_wcs()) is None


def test_jwst_pivot_leaves_reference_points_fixed():
    """The defect itself: under jwst's per-detector pivot the reference points
    do not move, so their separation keeps the full aberration."""
    a, b = _pool()
    pa, pb = _vacorr_of_ref(a), _vacorr_of_ref(b)
    assert pa == pytest.approx(np.array(A), abs=1e-9)
    assert pb == pytest.approx(np.array(B), abs=1e-9)
    assert float(np.hypot(*(pa - pb))) == pytest.approx(SEP, abs=1e-9)


def test_repivot_scales_the_separation():
    out, info = repivot_pool_dva(_pool(), pivot='pool', key='t')
    assert info['applied'] is True
    pa, pb = _vacorr_of_ref(out[0]), _vacorr_of_ref(out[1])
    # the whole point: separation now carries the aberration scale
    assert float(np.hypot(*(pa - pb))) == pytest.approx(VA * SEP, rel=1e-12)
    # ... and the pool centroid is untouched by the 'pool' pivot
    centroid = 0.5 * (pa + pb)
    assert centroid == pytest.approx(
        np.array([0.5 * (A[0] + B[0]), 0.5 * (A[1] + B[1])]), abs=1e-9)
    # reported shift matches (va_scale - 1) * half-separation
    assert info['max_shift_mas'] == pytest.approx(
        abs(VA - 1.0) * SEP / 2 * 1e3, rel=1e-9)


def test_repivot_boresight_also_scales_separation():
    out, info = repivot_pool_dva(_pool(), pivot='boresight', key='t')
    assert info['applied'] is True
    pa, pb = _vacorr_of_ref(out[0]), _vacorr_of_ref(out[1])
    assert float(np.hypot(*(pa - pb))) == pytest.approx(VA * SEP, rel=1e-12)


def test_single_detector_pool_is_a_noop():
    pool = _pool()[:1]
    out, info = repivot_pool_dva(pool, key='t')
    assert info['applied'] is False
    assert out[0].wcs is pool[0].wcs          # same object, not even copied


def test_inputs_are_not_mutated():
    pool = _pool()
    before = _vacorr_of_ref(pool[0]).copy()
    repivot_pool_dva(pool, key='t')
    assert _vacorr_of_ref(pool[0]) == pytest.approx(before, abs=1e-12)


def test_realistic_va_scale_spread_still_applies():
    """Regression: each detector's va_scale is evaluated at its own reference
    point, so real data carries a ~1e-8 spread. An exact-equality guard rejects
    every real pool (observed: 216/216 f410m pools skipped). The guard must
    judge the spread in mas across the pool's lever arm, not as a raw float."""
    out, info = repivot_pool_dva(_pool(va_b=VA + 1.4e-8), key='t')
    assert info['applied'] is True, info.get('reason')
    # 1.4e-8 over an ~88" half-lever is ~1e-3 mas — utterly negligible
    pa, pb = _vacorr_of_ref(out[0]), _vacorr_of_ref(out[1])
    assert float(np.hypot(*(pa - pb))) == pytest.approx(VA * SEP, rel=1e-8)


def test_inconsistent_va_scale_bails():
    """A spread large enough to imply >1 mas across the pool is a real anomaly."""
    out, info = repivot_pool_dva(_pool(va_b=VA * 1.001), key='t')
    assert info['applied'] is False
    assert 'inconsistent' in info['reason']


def test_unknown_pivot_bails():
    out, info = repivot_pool_dva(_pool(), pivot='nonsense', key='t')
    assert info['applied'] is False


def test_va_scale_one_is_a_noop():
    out, info = repivot_pool_dva(_pool(va_a=1.0, va_b=1.0), key='t')
    assert info['applied'] is False


def test_pivots_constant_is_honest():
    assert set(DVA_PIVOTS) == {'pool', 'boresight'}
