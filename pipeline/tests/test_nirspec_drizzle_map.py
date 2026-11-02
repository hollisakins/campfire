"""The product's pixel maps (``drizzle_map``): one mapping per exposure, reused for every
drizzle, reproduces stcal's resampling (the code ResampleSpecStep runs) to the bit, data,
weights, variances and ERR; an image drawn on the data takes its masks.

The full check against Spec3's own s2d needs a real crf (p1, design record section 4);
here stcal's ``Resample`` on a simple two-exposure WCS is the reference."""

import numpy as np
import pytest

from campfire_pipeline.nirspec import drizzle_map as DM


NY, NX = 12, 20


def _wcs(dx, dy, shape=None):
    """A detector -> sky WCS that shifts by (dx, dy) pixels (1 pixel = 1e-5 deg)."""
    from astropy import coordinates as coord
    from astropy.modeling import models
    from gwcs import coordinate_frames as cf
    from gwcs import wcs

    t = (models.Shift(dx) & models.Shift(dy)) | (models.Scale(1e-5) & models.Scale(1e-5)) \
        | (models.Shift(150.0) & models.Shift(2.0))
    det = cf.Frame2D(name='detector', axes_order=(0, 1))
    sky = cf.CelestialFrame(name='world', reference_frame=coord.ICRS())
    w = wcs.WCS([(det, t), (sky, None)])
    if shape is not None:
        w.array_shape = shape
    return w


def _exposures(seed=3):
    """Two exposures shifted by a fraction of a pixel, with NaNs, DO_NOT_USE and an
    OUTLIER-only flag (16, good under jwst's good bits)."""
    rng = np.random.default_rng(seed)
    out = []
    for i, (dx, dy) in enumerate(((0.0, 0.0), (0.37, -0.61))):
        data = rng.normal(10.0, 1.0, (NY, NX)).astype(np.float32)
        vp = rng.uniform(0.5, 1.5, (NY, NX)).astype(np.float32)
        vr = rng.uniform(0.1, 0.3, (NY, NX)).astype(np.float32)
        vf = rng.uniform(0.0, 0.01, (NY, NX)).astype(np.float32)
        dq = np.zeros((NY, NX), np.uint32)
        dq[2, 3 + i] = 1
        dq[5, 7] = 16
        data[4, 9 + i] = np.nan
        vp[6, 2] = np.nan
        out.append(dict(name=f'e{i}.fits', wcs=_wcs(dx, dy), data=data, dq=dq, err=np.sqrt(vp + vr + vf),
                        var_poisson=vp, var_rnoise=vr, var_flat=vf, exposure_time=100.0 + 10 * i,
                        measurement_time=90.0 + 10 * i))
    return out


SETTINGS = dict(pixfrac=1.0, kernel='square', fillval='NaN', weight_type='exptime', pixel_scale_ratio=1.0)


def _stcal(exps, out_wcs, weight_type='exptime'):
    """stcal's Resample (what jwst's ResampleSpecStep runs), with jwst's NaN matching."""
    from jwst.datamodels.dqflags import pixel
    from stcal.resample import Resample

    class R(Resample):
        dq_flag_name_map = pixel

    r = R(dict(wcs=out_wcs, pixel_scale=0.036, pixel_scale_ratio=1.0), pixfrac=1.0, kernel='square', fillval='NaN',
          weight_type=weight_type, good_bits=DM.GOOD_BITS, enable_ctx=True, enable_var=True, compute_err='from_var')
    for e in exps:
        a = DM.match_nans_and_flags({k: np.array(e[k]) for k in DM.NAN_EXTENSIONS + ('dq',)})
        r.add_model(dict(a, filename=e['name'], group_id=e['name'], wcs=e['wcs'], exposure_time=e['exposure_time'],
                         start_time=0.0, end_time=1.0, duration=1.0, measurement_time=e['measurement_time'],
                         pixelarea_steradians=1e-13, level=None, subtracted=True))
    r.finalize()
    return r.output_model


def _map(exps, out_wcs, weight_type='exptime'):
    from drizzle.utils import calc_pixmap
    from stcal.resample.utils import resample_range
    xs = [DM.MappedExposure(e['name'], {k: np.array(e[k]) for k in DM.NAN_EXTENSIONS + ('dq',)},
                            dict(exposure_time=e['exposure_time'], measurement_time=e['measurement_time'],
                                 level=None, subtracted=True),
                            calc_pixmap(e['wcs'], out_wcs, e['data'].shape), 1.0, resample_range(e['data'].shape))
          for e in exps]
    return DM.DrizzleMap(xs, out_wcs.array_shape, dict(SETTINGS, weight_type=weight_type), 1.0)


@pytest.mark.parametrize('weight_type', ['exptime', 'ivm'])
def test_the_map_reproduces_stcal(weight_type):
    exps = _exposures()
    out_wcs = _wcs(0.0, 0.0, shape=(NY + 2, NX + 2))
    ref = _stcal(exps, out_wcs, weight_type)
    d = _map(exps, out_wcs, weight_type)
    got = d.drizzle({e['name']: {'data': e['data']} for e in exps}, variances=True)
    for k in ('data', 'wht', 'var_rnoise', 'var_poisson', 'var_flat', 'err'):
        np.testing.assert_array_equal(got[k], ref[k], err_msg=k)
    # the map is reused: a second drizzle through it gives the same again
    again = d.drizzle({e['name']: {'data': e['data']} for e in exps}, variances=True)
    np.testing.assert_array_equal(again['data'], got['data'])


def test_an_image_takes_the_data_masks():
    """A model drawn on the exposures (an array per name): its NaNs zeroed, the data's
    DO_NOT_USE pixels and NaN variances masked (the data's own NaNs are replaced by the
    image), as ``profile_fit.drizzle_arrays`` always did; a name not given contributes
    zeros with its weights."""
    exps = _exposures()
    out_wcs = _wcs(0.0, 0.0, shape=(NY + 2, NX + 2))
    d = _map(exps, out_wcs)
    ones = d.drizzle({e['name']: np.ones((NY, NX)) for e in exps})
    data = d.drizzle({e['name']: {'data': np.nan_to_num(e['data'])} for e in exps})
    np.testing.assert_array_equal(np.isnan(ones['data']), np.isnan(data['data']))
    np.testing.assert_array_equal(ones['wht'], data['wht'])
    assert not np.array_equal(ones['wht'], d.drizzle({e['name']: {'data': e['data']} for e in exps})['wht'])
    covered = np.isfinite(ones['data'])
    np.testing.assert_allclose(ones['data'][covered], 1.0, rtol=1e-6)
    half = d.drizzle({exps[0]['name']: np.full((NY, NX), np.nan)})       # NaN image: zeros, still weighted
    np.testing.assert_array_equal(half['wht'], data['wht'])
    assert np.nanmax(np.abs(half['data'])) == 0.0


def test_a_frame_replaces_every_array():
    """A dict of arrays (the master-background frames) is drizzled as data: its own
    masks, NaNs and variances, nothing of the crf's."""
    exps = _exposures()
    out_wcs = _wcs(0.0, 0.0, shape=(NY + 2, NX + 2))
    d = _map(exps, out_wcs)
    other = _exposures(seed=9)
    for e in other:
        e['dq'][8, 8] |= 1
    got = d.drizzle({e['name']: {k: e[k] for k in DM.NAN_EXTENSIONS + ('dq',)} for e in other}, variances=True)
    ref = _stcal([dict(o, wcs=e['wcs']) for e, o in zip(exps, other)], out_wcs)
    for k in ('data', 'wht', 'err'):
        np.testing.assert_array_equal(got[k], ref[k], err_msg=k)


def test_match_nans_and_flags_is_jwst():
    from jwst.datamodels import SlitModel
    from jwst.lib.pipe_utils import match_nans_and_flags
    e = _exposures()[1]
    m = SlitModel(data=e['data'].copy(), dq=e['dq'].copy(), err=e['err'].copy(), var_poisson=e['var_poisson'].copy(),
                  var_rnoise=e['var_rnoise'].copy(), var_flat=e['var_flat'].copy())
    match_nans_and_flags(m)
    a = DM.match_nans_and_flags({k: np.array(e[k]) for k in DM.NAN_EXTENSIONS + ('dq',)})
    for k in DM.NAN_EXTENSIONS + ('dq',):
        np.testing.assert_array_equal(a[k], getattr(m, k), err_msg=k)
