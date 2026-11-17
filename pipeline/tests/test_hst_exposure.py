"""HST chip adapter: units, variance split, good-pixel mask, detector rules.

Synthetic HST-shaped files (no HST software or data needed): two SCI/ERR/DQ
imsets for ACS/WFC, one plus a TIME array for WFC3/IR, each with a FITS WCS
that carries a (zero) SIP distortion so the adapter accepts it.
"""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.hst import instruments
from campfire_pipeline.hst.exposure import (SKY_KEY, VARSCALE_KEY, open_exposure,
                                            photfnu)

_ARCSEC = np.pi / (180.0 * 3600.0)
_PHOTFLAM, _PHOTPLAM = 7.8981816e-20, 5921.8926      # ACS/WFC F606W, 2020


def _wcs_cards(scale_arcsec, crval, sip=True):
    s = scale_arcsec / 3600.0
    cards = {
        'CTYPE1': 'RA---TAN-SIP' if sip else 'RA---TAN',
        'CTYPE2': 'DEC--TAN-SIP' if sip else 'DEC--TAN',
        'CRPIX1': 16.0, 'CRPIX2': 12.0, 'CRVAL1': crval[0], 'CRVAL2': crval[1],
        'CD1_1': -s, 'CD1_2': 0.0, 'CD2_1': 0.0, 'CD2_2': s,
    }
    if sip:
        cards.update({'A_ORDER': 2, 'B_ORDER': 2, 'A_2_0': 0.0, 'B_0_2': 0.0})
    return cards


def _imset(extver, sci, err, dq, cards, extra=None):
    hdus = []
    for name, data in (('SCI', sci), ('ERR', err), ('DQ', dq)):
        hdu = fits.ImageHDU(data=data, name=name, ver=extver)
        if name == 'SCI':
            hdu.header.update(cards)
            hdu.header.update(extra or {})
        hdus.append(hdu)
    return hdus


def _acs_file(path, *, sip=True, sky_key=None, source=0.0):
    ny, nx, t, sky = 24, 32, 400.0, 40.0
    rng = np.random.default_rng(1)
    primary = fits.PrimaryHDU()
    primary.header.update({
        'INSTRUME': 'ACS', 'DETECTOR': 'WFC', 'ROOTNAME': 'jtest01aaq',
        'FILTER1': 'F606W', 'FILTER2': 'CLEAR2L', 'EXPTIME': t,
        'PROPOSID': 15843, 'DATE-OBS': '2020-09-27', 'CAL_VER': '10.4.1'})
    hdul = fits.HDUList([primary])
    for extver in (1, 2):
        sci = np.full((ny, nx), sky) + rng.normal(0, 0.01, (ny, nx))
        sci[10, 10] += source
        err = np.sqrt(np.clip(sci, 0, None) + 4.0 ** 2)
        dq = np.zeros((ny, nx), 'int16')
        dq[0, 0], dq[0, 1], dq[0, 2] = 64, 16, 4096     # warm, hot, CR
        extra = {'BUNIT': 'ELECTRONS', 'PHOTFLAM': _PHOTFLAM,
                 'PHOTPLAM': _PHOTPLAM, 'IDCSCALE': 0.05,
                 'WCSNAME': 'IDC_test', 'CCDCHIP': 3 - extver}
        if sky_key is not None:
            extra[SKY_KEY] = sky_key
        hdul += _imset(extver, sci.astype('float32'), err.astype('float32'), dq,
                       _wcs_cards(0.05, (40.0 + 0.001 * extver, 26.0), sip=sip),
                       extra)
    hdul.writeto(path)
    return ny, nx, t, sky


def test_filter_and_detector_rules():
    assert instruments.filter_name({'FILTER': 'F160W'}) == 'f160w'
    assert instruments.filter_name(
        {'FILTER1': 'CLEAR1L', 'FILTER2': 'F814W'}) == 'f814w'
    assert instruments.filter_name(
        {'FILTER1': 'F606W', 'FILTER2': 'CLEAR2L'}) == 'f606w'
    # a polarizer plus a filter is never reported as the plain filter
    assert instruments.filter_name(
        {'FILTER1': 'F606W', 'FILTER2': 'POL0V'}) == 'f606w+pol0v'
    assert instruments.detector_for('ACS', 'WFC').token == 'acswfc'
    assert instruments.detector_for('wfc3', 'ir').token == 'wfc3ir'
    for bad in (('WFC3', 'UVIS'), ('ACS', 'HRC'), ('NIRCAM', 'NRCA1')):
        with pytest.raises(ValueError):
            instruments.detector_for(*bad)


def test_photfnu_from_header_or_flam():
    expect = 3.33564e4 * _PHOTFLAM * _PHOTPLAM ** 2
    assert photfnu({'PHOTFLAM': _PHOTFLAM, 'PHOTPLAM': _PHOTPLAM}) == pytest.approx(expect)
    assert photfnu({'PHOTFNU': 1.5e-7, 'PHOTFLAM': 1.0}) == 1.5e-7
    # 1 e-/s in F606W is ~92 nJy
    assert expect * 1e9 == pytest.approx(92.39, rel=1e-3)
    with pytest.raises(ValueError):
        photfnu({})


def test_acs_file_gives_two_chips_in_surface_brightness(tmp_path):
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    ny, nx, t, sky = _acs_file(path, sky_key=40.0, source=4000.0)
    chips = open_exposure(path)

    assert [c.key for c in chips] == ['jtest01aaq_sci1', 'jtest01aaq_sci2']
    c = chips[0]
    assert (c.instrument, c.filter, c.shape) == ('acswfc', 'f606w', (ny, nx))
    assert c.exptime == c.xposure == t and not c.is_rate
    assert c.pixel_area_arcsec2 == pytest.approx(0.05 ** 2)

    a = c.load()
    f = c.photfnu * 1e-6 / (0.05 * _ARCSEC) ** 2       # MJy/sr per e-/s
    assert c.sb_per_rate == pytest.approx(f)
    # sky removed; the 4000 e- source is 10 e-/s
    assert abs(np.median(a.sci)) < 1e-3 * f
    assert a.sci[10, 10] == pytest.approx(10.0 * f, rel=1e-3)

    # variance. The weight term is ONE background level for the chip (sky 40 e-
    # plus read noise 4**2 e-), not a per-pixel quantity, so the drizzle weight
    # cannot follow the pixel value or the source.
    bkg = (40.0 + 16.0) / t ** 2 * f * f
    np.testing.assert_allclose(a.var_rnoise, bkg, rtol=2e-3)
    assert np.ptp(a.var_rnoise) == 0
    assert a.var_rnoise[10, 10] == a.var_rnoise[3, 3]       # same under the source
    # the source's Poisson variance lands in var_poisson, ~zero on blank sky
    assert a.var_poisson[10, 10] == pytest.approx(4000.0 / t ** 2 * f * f, rel=1e-2)
    assert np.median(a.var_poisson) < 1e-3 * bkg
    assert not a.var_flat.any()
    # together they are never below ERR**2, and equal it to a part in 1e3
    with fits.open(path) as hdul:
        err = hdul['ERR', 1].data.astype('float64')
    total = (err / t) ** 2 * f * f
    summed = a.var_rnoise + a.var_poisson
    assert np.all(summed >= total * (1 - 1e-5))
    np.testing.assert_allclose(summed, total, rtol=1e-3)

    # good mask: warm (64) tolerated; hot (16) and cosmic ray (4096) are not
    assert a.good[0, 0] and not a.good[0, 1] and not a.good[0, 2]
    assert a.good.sum() == ny * nx - 2

    # the two chips have their own WCS
    r1, _ = chips[0].pixel_to_world(np.array([15.0]), np.array([11.0]))
    r2, _ = chips[1].pixel_to_world(np.array([15.0]), np.array([11.0]))
    assert r2[0] - r1[0] == pytest.approx(0.001)

    row = c.provenance()
    assert row['sky_src'] == 'header' and row['sky'] == 40.0
    assert row['proposid'] == 15843 and row['wcsname'] == 'IDC_test'


def test_sky_is_estimated_when_the_keyword_is_absent(tmp_path):
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    _acs_file(path, sky_key=None)
    c = open_exposure(path)[0]
    a = c.load()
    assert abs(np.median(a.sci)) < 1e-3 * c.sb_per_rate
    assert c.provenance()['sky_src'] == 'median'
    assert c.provenance()['sky'] == pytest.approx(40.0, abs=0.05)


def test_file_without_distortion_model_is_refused(tmp_path):
    path = str(tmp_path / 'jtest01aaq_flc.fits')
    _acs_file(path, sip=False)
    with pytest.raises(ValueError, match='no distortion'):
        open_exposure(path)


def test_wfc3ir_rate_file_uses_the_time_array(tmp_path):
    ny, nx = 20, 20
    primary = fits.PrimaryHDU()
    primary.header.update({'INSTRUME': 'WFC3', 'DETECTOR': 'IR',
                           'ROOTNAME': 'itest01aaq', 'FILTER': 'F160W',
                           'EXPTIME': 1000.0})
    sci = np.full((ny, nx), 1.0, 'float32')            # e-/s, all sky
    sci[5, 5] = 11.0                                   # 10 e-/s source
    time = np.full((ny, nx), 1000.0, 'float32')
    time[5, 5] = 500.0                                 # ramp cut short there
    err = np.sqrt(sci / time + 0.001).astype('float32')
    dq = np.zeros((ny, nx), 'int16')
    dq[1, 1], dq[1, 2] = 16, 512                       # stable hot, blob
    # CFVARSC as the sky step measures it on real WFC3/IR frames (pixel scatter
    # below ERR). It is applied, as for ACS/WFC and NIRCam: the background
    # variance follows the pixel scatter.
    extra = {'BUNIT': 'ELECTRONS/S', 'PHOTFNU': 1.5246e-07, 'IDCSCALE': 0.1282500,
             SKY_KEY: 1.0, VARSCALE_KEY: 0.7}
    hdul = fits.HDUList([primary] + _imset(1, sci, err, dq,
                                           _wcs_cards(0.128, (34.4, -5.2)), extra))
    hdul.append(fits.ImageHDU(data=time, name='TIME', ver=1))
    path = str(tmp_path / 'itest01aaq_flt.fits')
    hdul.writeto(path)

    (c,) = open_exposure(path)
    assert c.instrument == 'wfc3ir' and c.is_rate and c.filter == 'f160w'
    assert c.var_scale == 0.7                          # from the header, applied
    a = c.load()
    f = c.sb_per_rate
    assert a.sci[5, 5] == pytest.approx(10.0 * f, rel=1e-5)
    # The background variance scales with each pixel's effective time: the
    # pixel whose ramp was cut to 500 s carries twice the variance (half the
    # weight) of its 1000 s neighbours, whatever its flux.
    assert a.var_rnoise[5, 5] == pytest.approx(2.0 * a.var_rnoise[6, 6], rel=1e-5)
    assert a.var_rnoise[6, 6] == pytest.approx(0.7 * 0.002 * f * f, rel=1e-4)
    # blank sky carries no source term: its whole variance is the scaled one
    assert a.var_poisson[6, 6] == pytest.approx(0.0, abs=1e-6 * f * f)
    # at the source, the part of ERR**2 above the modelled background (0.004
    # at 500 s) is the source term, untouched by the scale
    source = (11.0 / 500.0 + 0.001 - 0.004) * f * f
    assert a.var_poisson[5, 5] == pytest.approx(source, rel=1e-4)
    assert a.good[1, 1] and not a.good[1, 2]           # hot kept, blob dropped
    # a caller may tolerate the blob bit for filters with the delta-flat fix
    (c2,) = open_exposure(path, tolerated_bits=16 + 512)
    assert c2.load().good[1, 2]


def test_surface_brightness_survives_the_drizzle(tmp_path):
    """A uniform sky of known surface brightness keeps its value when two chips
    at 50 mas are drizzled onto a 30 mas tile: no flux rescaling is needed."""
    pytest.importorskip("drizzle.resample")
    from campfire_pipeline.imaging.drizzle import drizzle_tile

    path = str(tmp_path / 'jtest01aaq_flc.fits')
    _acs_file(path, sky_key=0.0)                       # keep the 40 e- "sky"
    chips = open_exposure(path)
    out = str(tmp_path / 'mosaic_i2d.fits')
    drizzle_tile(chips, out, crval=(40.0015, 26.0), crpix=(49.5, 49.5),
                 shape=(100, 100), rotation=0.0, pixel_scale=0.03,
                 weight_type='exptime', reduction_version='t')
    expect = 40.0 / 400.0 * chips[0].sb_per_rate
    with fits.open(out) as hdul:
        sci, nexp = hdul['SCI'].data, hdul['NEXP'].data
        assert hdul[0].header['INSTRUME'] == 'acswfc'
        assert hdul[0].header['FILTER'] == 'f606w'
        assert len(hdul['INPUTS'].data) == 2
    inside = np.isfinite(sci) & (nexp > 0)
    assert inside.sum() > 1000
    assert np.median(sci[inside]) == pytest.approx(expect, rel=1e-3)


def test_exposure_map_reads_both_chips_through_their_wcs(tmp_path):
    """The field exposure maps (imaging/expmap.py, NIRCam's code) get an HST
    exposure's time from EXPTIME and its footprint from each chip's own WCS:
    two outlines for an ACS/WFC file, with the exposure time counted once
    under either chip and not at all in the gap between them."""
    from astropy.wcs import WCS

    from campfire_pipeline.hst.field import HstField
    from campfire_pipeline.imaging import expmap as em

    path = str(tmp_path / 'jtest01aaq_flc.fits')
    ny, nx, t, _sky = _acs_file(path, sky_key=0.0)
    field = HstField(name='f', instrument='acswfc', filters=['f606w'],
                     files=['jtest*'], grid=None)
    meta = field.expmap_metadata(path)
    assert meta.rootname == 'jtest01aaq' and meta.xposure == t
    assert len(meta.polygons) == 2                       # one outline per chip
    with fits.open(path) as hdul:
        centers = [WCS(hdul['SCI', v].header).pixel_to_world_values(
            (nx - 1) / 2, (ny - 1) / 2) for v in (1, 2)]

    wcs, shape = em._auto_wcs([meta], 0.1, 1.0)          # 0.1" pixels
    expmap = em._accumulate_expmap([meta], wcs, shape)
    for ra, dec in centers:
        x, y = wcs.world_to_pixel_values(ra, dec)
        assert expmap[int(round(float(y))), int(round(float(x)))] == t
    gap_ra = 0.5 * (centers[0][0] + centers[1][0])
    x, y = wcs.world_to_pixel_values(gap_ra, centers[0][1])
    assert expmap[int(round(float(y))), int(round(float(x)))] == 0
    assert set(np.unique(expmap)) == {0.0, t}            # never counted twice
    # the covered area is the two chips' (each nx x ny pixels of 0.05")
    area = np.count_nonzero(expmap) * 0.1 ** 2
    assert area == pytest.approx(2 * nx * ny * 0.05 ** 2, rel=0.1)
