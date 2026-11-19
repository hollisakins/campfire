"""Header cards of an HST mosaic.

A NIRCam mosaic gets its cards from the JWST pipeline. An HST mosaic is
written by ``imaging/mosaic_io.py``, which has to supply the ones a user of
the science image needs: the instrument's identity, the exposure time (once
per exposure, not once per chip), the pixel area, the photometric conversion,
the times of the inputs, the footprint and the calibration versions. The split
``_sci.fits`` is written from the SCI header, so SCI carries them itself.
"""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.hst.exposure import HstChip, open_exposure
from test_hst_exposure import _acs_file

_ARCSEC = np.pi / (180.0 * 3600.0)


def _two_exposures(tmp_path):
    """Two ACS/WFC exposures (four chips) at the same place, a day apart."""
    a = str(tmp_path / 'jtest01aaq_flc.fits')
    b = str(tmp_path / 'jtest02bbq_flc.fits')
    _acs_file(a, bkg_key=0.0, primary_extra={
        'EXPSTART': 59119.25, 'EXPEND': 59119.255, 'CRDS_CTX': 'hst_1372.pmap'})
    _acs_file(b, bkg_key=0.0, primary_extra={
        'ROOTNAME': 'jtest02bbq', 'PROPOSID': 9822,
        'EXPSTART': 59120.50, 'EXPEND': 59120.505,
        # calibrated before the context had its own card: the stamp names it
        'CFP_CAL': 'raw 10.4.1 hst_1372.pmap'})
    return open_exposure(a) + open_exposure(b)


def test_cards_count_an_exposure_once_and_name_the_instrument(tmp_path):
    chips = _two_exposures(tmp_path)
    assert len(chips) == 4
    primary, sci = (dict((k, v) for k, v, _c in cards)
                    for cards in HstChip.mosaic_header(chips))

    for cards in (primary, sci):
        assert (cards['TELESCOP'], cards['INSTRUME'], cards['DETECTOR'],
                cards['FILTER']) == ('HST', 'ACS', 'WFC', 'F606W')
    assert primary['PROPOSID'] == '15843,9822'
    assert primary['CAL_VER'] == '10.4.1'
    assert primary['CRDS_CTX'] == 'hst_1372.pmap'       # card and stamp agree

    # first start to last end; the middle weighted by exposure time (equal here)
    assert primary['EXPSTART'] == 59119.25 and primary['EXPEND'] == 59120.505
    assert primary['EXPMID'] == pytest.approx(0.5 * (59119.2525 + 59120.5025))
    assert primary['DATE-OBS'] == '2020-09-27' and primary['DATE-BEG'].startswith('2020-09-27T06:00:00')
    assert sci['MJD-BEG'] == 59119.25 and sci['MJD-END'] == 59120.505
    # two exposures of 0.005 d, not four chips
    assert sci['TELAPSE'] == pytest.approx(2 * 0.005 * 86400.0)

    expect = chips[0].sb_per_rate
    assert sci['PHOTMJSR'] == pytest.approx(expect)
    assert sci['PHOTUJA2'] == pytest.approx(expect * 1e12 * _ARCSEC ** 2)
    assert sci['PHOTUJA2'] / sci['PHOTMJSR'] == pytest.approx(23.5044, rel=1e-5)
    assert sci['PHOTFNU'] == pytest.approx(chips[0].photfnu)
    assert sci['PHOTSPRD'] == 0.0
    assert sci['PHOTPLAM'] == pytest.approx(5921.8926)


def test_time_cards_are_left_out_when_an_input_has_no_times(tmp_path):
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    _acs_file(path, bkg_key=0.0)                         # no EXPSTART / EXPEND
    primary, sci = HstChip.mosaic_header(open_exposure(path))
    keys = {k for k, _v, _c in primary} | {k for k, _v, _c in sci}
    assert not keys & {'EXPSTART', 'EXPEND', 'MJD-BEG', 'MJD-END', 'TELAPSE', 'DATE-OBS'}
    assert {'TELESCOP', 'PHOTMJSR', 'CAL_VER'} <= keys


def test_the_mosaic_and_its_science_header_carry_the_cards(tmp_path):
    pytest.importorskip("drizzle.resample")
    from campfire_pipeline.imaging.drizzle import drizzle_tile

    chips = _two_exposures(tmp_path)
    out = str(tmp_path / 'mosaic_i2d.fits')
    drizzle_tile(chips, out, crval=(40.0015, 26.0), crpix=(49.5, 49.5),
                 shape=(100, 100), rotation=0.0, pixel_scale=0.03,
                 weight_type='exptime', reduction_version='t')
    with fits.open(out) as hdul:
        p, s = hdul[0].header, hdul['SCI'].header
        # 2 exposures of 400 s in 4 chips: 800 s, not 1600
        assert p['TEXPTIME'] == 800.0 and s['XPOSURE'] == 800.0
        assert p['NCOMBINE'] == 2 and s['NCOMBINE'] == 2 and p['NDRIZ'] == 4
        assert float(np.nanmax(hdul['EXPTIME'].data)) == 800.0   # and so says the plane
        assert (p['TELESCOP'], p['INSTRUME'], p['DETECTOR'], p['FILTER']) == (
            'HST', 'ACS', 'WFC', 'F606W')
        assert (p['CFINSTR'], p['CFFILT']) == ('acswfc', 'f606w')
        assert p['CAL_VER'] == '10.4.1' and p['CRDS_CTX'] == 'hst_1372.pmap'
        assert 'DATE' in p and p['CMPFRVER'] == 't'
        # what a reader of the science image alone needs
        assert s['BUNIT'] == 'MJy/sr'
        assert s['PIXAR_A2'] == pytest.approx(0.03 ** 2)
        assert s['PIXAR_SR'] == pytest.approx((0.03 * _ARCSEC) ** 2)
        assert s['PHOTMJSR'] == pytest.approx(chips[0].sb_per_rate)
        assert s['S_REGION'].startswith('POLYGON ICRS') and s['S_REGION'] == p['S_REGION']
        assert (s['TELESCOP'], s['INSTRUME'], s['FILTER']) == ('HST', 'ACS', 'F606W')
        assert s['MJD-BEG'] == 59119.25 and s['CTYPE1'] == 'RA---TAN'
        # the input table records what the cards were made from
        t = hdul['INPUTS'].data
        assert set(t['crds_ctx']) == {'hst_1372.pmap'}
        assert sorted(set(t['expstart'])) == [59119.25, 59120.50]
