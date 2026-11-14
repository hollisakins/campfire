"""Star protection in the NIRCam outlier step.

Synthetic NIRCam files with a gwcs, three exposures of one pointing: two on one
date and one three years later. The catalog has a fast star (50 mas/yr), a slow
one (1 mas/yr) and a galaxy without a proper motion.
"""

import numpy as np
import pytest

pytest.importorskip('jwst')

from astropy.io import fits  # noqa: E402
from astropy.table import Table  # noqa: E402
from astropy.time import Time  # noqa: E402
from jwst.datamodels import ImageModel  # noqa: E402
from jwst.datamodels.dqflags import pixel as pf  # noqa: E402

from _align_gwcs import HAVE_PERSISTABLE_WCS, build_canonical  # noqa: E402
from campfire_pipeline.nircam import outlier_protect  # noqa: E402
from campfire_pipeline.nircam.steps.outlier import _star_protection  # noqa: E402

pytestmark = pytest.mark.skipif(not HAVE_PERSISTABLE_WCS,
                                reason='no persistable gwcs helper')

_SHAPE = (160, 160)
_SCALE = 1e-5                      # deg / pixel: 36 mas
_MJD_A, _MJD_B = 59800.0, 59800.0 + 3 * 365.25
_FAST, _SLOW, _GALAXY = (60.0, 70.0), (110.0, 40.0), (30.0, 120.0)   # x, y


def _scene(tmp_path, dates=(_MJD_A, _MJD_A + 0.01, _MJD_B)):
    """Three files of one pointing and the catalog, at its positions on the
    first date."""
    rng = np.random.default_rng(3)
    files = []
    for k, mjd in enumerate(dates):
        path = str(tmp_path / f'jw0000{k}001001_02101_00001_nrca1_crf.fits')
        wcs = build_canonical(path, rng.normal(0, 1, _SHAPE), crpix=(80.0, 80.0),
                              scale=_SCALE)
        fits.setval(path, 'EXPMID', value=mjd, ext=0)
        fits.setval(path, 'PIXAR_A2', value=(_SCALE * 3600) ** 2, extname='SCI')
        files.append(path)
    x = np.array([_FAST[0], _SLOW[0], _GALAXY[0]])
    y = np.array([_FAST[1], _SLOW[1], _GALAXY[1]])
    ra, dec = wcs(x, y, with_bounding_box=False)
    catalog = Table({'RA': ra, 'DEC': dec,
                     'pmra': [0.0, 0.0, np.nan], 'pmdec': [50.0, 1.0, np.nan],
                     'ref_epoch': [float(Time(dates[0], format='mjd').jyear)] * 3})
    return files, catalog, wcs


def _disk(shape, x, y, r):
    yy, xx = np.mgrid[:shape[0], :shape[1]]
    return np.hypot(xx - x, yy - y) <= r


def test_a_star_that_moved_between_the_dates_is_masked_at_its_place(tmp_path):
    files, catalog, wcs = _scene(tmp_path)
    masks, info = outlier_protect.protection_for_visit(
        files, [files[0], files[2]], catalog, radius=0.4, min_motion=0.15)
    # 150 mas in three years is 4 pixels; the slow star moved 0.08 pixel
    assert info['n_protected'] == 1
    assert info['span_years'] == pytest.approx(3.0, abs=0.01)
    assert set(masks) == {files[0], files[2]}          # the visit's files only

    early = masks[files[0]]
    assert early[_disk(_SHAPE, *_FAST, 10.0)].all()    # 0.4 arcsec is 11 pixels
    assert not early[~_disk(_SHAPE, *_FAST, 12.5)].any()

    # in the late exposure the circle sits where the star is three years on
    late = masks[files[2]]
    rows, cols = np.nonzero(late)
    ra, dec = wcs(cols.mean(), rows.mean(), with_bounding_box=False)
    assert (dec - catalog['DEC'][0]) * 3600 == pytest.approx(0.150, abs=0.01)
    assert abs(ra - catalog['RA'][0]) * 3600 < 0.01
    assert not late[_disk(_SHAPE, *_SLOW, 8.0)].any()
    assert not late[_disk(_SHAPE, *_GALAXY, 8.0)].any()


def test_exposures_of_one_date_protect_nothing(tmp_path):
    files, catalog, _ = _scene(tmp_path, dates=(_MJD_A, _MJD_A + 0.01, _MJD_A + 2))
    masks, info = outlier_protect.protection_for_visit(files, files, catalog)
    assert masks == {} and info['n_protected'] == 0


def test_a_catalog_without_proper_motions_or_a_file_without_a_date(tmp_path):
    files, catalog, _ = _scene(tmp_path)
    still = catalog['RA', 'DEC']
    assert outlier_protect.protection_for_visit(files, files, still)[0] == {}
    with fits.open(files[1], mode='update') as hdul:
        del hdul[0].header['EXPMID']
        for key in ('MJD-AVG', 'EXPSTART', 'EXPEND'):
            hdul[0].header.remove(key, ignore_missing=True)
    assert outlier_protect.protection_for_visit(files, files, catalog)[0] == {}


def test_the_step_protects_on_motion_alone(tmp_path):
    files, catalog, _ = _scene(tmp_path)
    # whichever exposures are compared; the settings are the only switches
    assert set(_star_protection({}, files, files, catalog)) == set(files)
    assert _star_protection({'protect_stars': False}, files, files, catalog) == {}
    assert _star_protection({}, files, files, None) == {}
    # with a threshold no star reaches, nothing
    assert _star_protection({'protect_min_motion': 10.0}, files, files,
                            catalog) == {}
    # the two exposures of one date, compared alone
    assert _star_protection({}, files[:2], files[:2], catalog) == {}


def test_release_takes_back_the_flags_and_the_blanked_values(tmp_path):
    files, catalog, _ = _scene(tmp_path)
    masks, _ = outlier_protect.protection_for_visit(files, files[:1], catalog)
    mask = masks[files[0]]
    flagged = np.uint32(pf['OUTLIER'] | pf['DO_NOT_USE'])
    with ImageModel(files[0]) as model:
        model.dq[70, 62] = pf['OUTLIER'] | pf['DO_NOT_USE']   # flagged before
        model.data[70, 62] = np.nan
        model.dq[72, 58] = pf['SATURATED']
        before = {name: np.array(getattr(model, name)) for name in ('data', 'err', 'dq')}
        kept = outlier_protect.snapshot(model, mask)

        # what the outlier test does: flag, and blank the values
        core = _disk(_SHAPE, *_FAST, 2.0)
        ray = _disk(_SHAPE, 130.0, 130.0, 1.5)
        for hit in (core, ray):
            model.dq[hit] |= flagged
            model.data[hit] = np.nan
            model.err[hit] = np.nan

        n = outlier_protect.release(model, mask, kept, pf['OUTLIER'])
        assert n == int(core.sum()) - 1                # one carried the bit already
        assert np.array_equal(model.dq[core], before['dq'][core])
        assert np.array_equal(model.data[mask], before['data'][mask],
                              equal_nan=True)
        assert np.isnan(model.data[70, 62])            # still blank, as before
        assert np.isfinite(model.err[core]).sum() == int(core.sum()) - 1
        assert np.array_equal(model.dq[mask], before['dq'][mask])
        assert model.dq[72, 58] == pf['SATURATED']
        # away from the star the cosmic ray stays flagged and blank
        assert np.all(model.dq[ray] & pf['OUTLIER'])
        assert np.isnan(model.data[ray]).all()
        # a second call finds nothing left to do
        assert outlier_protect.release(model, mask, kept, pf['OUTLIER']) == 0
