"""Per-tile exposure maps (EXPTIME + NEXP) — `campfire_pipeline.nircam.mosaic_exp`
and the drizzle_tile weight-change accumulator.

The accumulator in ``drizzle_tile`` must reproduce what the context cube gives,
without the cube: the integration test drives the real ``drizzle.resample.Drizzle``
with context enabled and checks the in-loop planes against ``exp_from_context``
(which also pins the bit ``i`` <-> ``i``-th ``add_image`` mapping the jwst
backend and the backfill rely on).
"""
import json
import os

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from campfire_pipeline.nircam import mosaic_exp as mx

Drizzle = pytest.importorskip("drizzle.resample").Drizzle


def _shift_pixmap(ny, nx, dx, dy):
    iy, ix = np.indices((ny, nx), dtype=np.float64)
    pm = np.empty((ny, nx, 2), np.float64)
    pm[..., 0] = ix + dx
    pm[..., 1] = iy + dy
    return pm


def test_in_drizzle_planes_match_context(tmp_path):
    """40 shifted inputs (> 32, so two context planes) with three distinct
    exposure times, masked pixels, and one fully masked input."""
    rng = np.random.default_rng(1)
    out_shape = (60, 70)
    n_in = 40
    outsci = np.zeros(out_shape, np.float32)
    outwht = np.zeros(out_shape, np.float32)
    outctx = np.zeros((2, *out_shape), np.int32)
    d = Drizzle(out_img=outsci, out_wht=outwht, out_ctx=outctx, kernel='square',
                fillval='NaN', max_ctx_id=n_in)
    nexp = np.zeros(out_shape, np.int16)
    expt = np.zeros(out_shape, np.float32)
    xposure = rng.choice([257.682, 515.364, 1030.7], n_in)
    used = 0
    for k in range(n_in):
        ny, nx = 30, 35
        dx, dy = rng.uniform(-10, 45), rng.uniform(-10, 40)
        w = rng.uniform(0.5, 2.0, (ny, nx)).astype(np.float32)
        w[rng.random((ny, nx)) < 0.1] = 0          # masked pixels
        if k == 7:
            w[:] = 0                                # fully masked input
        before = outwht.copy()
        d.add_image(data=np.ones((ny, nx), np.float32), weight_map=w,
                    exptime=1.0, pixmap=_shift_pixmap(ny, nx, dx, dy),
                    pixfrac=1.0, in_units='cps')
        landed = outwht != before
        nexp += landed
        expt += landed * np.float32(xposure[k])
        used += bool(landed.any())
    assert nexp.max() > 3                           # real overlaps
    np.testing.assert_array_equal(nexp, mx.popcount_planes(outctx))
    assert used == n_in - 1
    np.testing.assert_array_equal(nexp == 0, ~np.isfinite(outsci))

    p = tmp_path / 'con.fits'
    fits.HDUList([fits.PrimaryHDU(),
                  fits.CompImageHDU(outctx, name='CON', compression_type='GZIP_1')]
                 ).writeto(p)
    with fits.open(p) as h:
        t2, n2, nused = mx.exp_from_context(h['CON'], out_shape, xposure, rows=7)
    np.testing.assert_array_equal(n2, nexp)
    np.testing.assert_allclose(t2, expt, rtol=1e-6)
    assert nused == n_in - 1


def test_popcount_planes():
    ctx = np.zeros((2, 2, 3), np.int32)
    ctx[0, 0, 0] = -1                               # all 32 bits
    ctx[0, 1, 2] = 0b1011
    ctx[1, 1, 2] = 0b1
    np.testing.assert_array_equal(mx.popcount_planes(ctx),
                                  [[32, 0, 0], [0, 0, 4]])


def test_exp_from_context_weights_bits_by_xposure(tmp_path):
    ctx = np.zeros((2, 3, 4), np.int32)
    ctx[0, 0, 0] = 0b101                            # inputs 0, 2
    ctx[1, 2, 3] = 0b1                              # input 32
    ctx[0, 1, 1] = np.int32(-2**31)                 # input 31 (sign bit)
    xposure = np.arange(33) * 10.0 + 1.0            # input i -> 10 i + 1 s
    p = tmp_path / 'con.fits'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(ctx, name='CON')]).writeto(p)
    with fits.open(p) as h:
        t, n, nused = mx.exp_from_context(h['CON'], (3, 4), xposure, rows=2)
    assert n[0, 0] == 2 and t[0, 0] == pytest.approx(1 + 21)
    assert n[2, 3] == 1 and t[2, 3] == pytest.approx(321)
    assert n[1, 1] == 1 and t[1, 1] == pytest.approx(311)
    assert nused == 4 and n.sum() == 4


def _tile_header(ny=40, nx=50):
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval = [150.0, 2.0]
    w.wcs.crpix = [nx / 2 + 0.5, ny / 2 + 0.5]
    w.wcs.cdelt = [-1e-4, 1e-4]
    h = w.to_header()
    h['NAXIS'] = 2
    h['NAXIS1'] = nx
    h['NAXIS2'] = ny
    return h


def _detector_header(tile_hdr, x0, y0, w, h):
    """A detector whose pixel (0,0) centre sits on tile pixel (x0, y0)."""
    d = _tile_header()
    d['CRPIX1'] = tile_hdr['CRPIX1'] - x0
    d['CRPIX2'] = tile_hdr['CRPIX2'] - y0
    d['NAXIS1'], d['NAXIS2'] = w, h
    return d


def test_exp_from_footprints_counts_pixel_centres():
    tile = _tile_header()
    a = _detector_header(tile, 5, 4, 20, 10)        # tile x 5..24, y 4..13
    b = _detector_header(tile, 15, 8, 40, 40)       # clipped by the tile edge
    c = _detector_header(tile, 5, 4, 20, 10)        # same as a, other time
    t, n = mx.exp_from_footprints([a, b, c], [100.0, 300.0, 50.0], tile)
    exp_n = np.zeros((40, 50), np.int16)
    exp_t = np.zeros((40, 50), np.float32)
    exp_n[4:14, 5:25] += 2
    exp_t[4:14, 5:25] += 150
    exp_n[8:40, 15:50] += 1
    exp_t[8:40, 15:50] += 300
    np.testing.assert_array_equal(n, exp_n)
    np.testing.assert_array_equal(t, exp_t)


def test_write_exp_file_contract(tmp_path):
    """EXPTIME is the first image (what a 'first HDU with data' reader gets),
    NEXP the second; EXPTIME round-trips losslessly."""
    tile = _tile_header()
    rng = np.random.default_rng(3)
    n = rng.integers(0, 7, (40, 50)).astype(np.int16)
    t = (n * np.float32(257.682)).astype(np.float32)
    t[0, 0] = np.float32(1e-3) + np.float32(12345.678)   # not quantisation-friendly
    p = tmp_path / 'm_exp.fits'
    mx.write_exp_file(p, t, n, tile, src='drizzle', ninputs=12, nused=11)
    with fits.open(p) as h:
        images = [x for x in h if x.data is not None]
        assert [x.name for x in images] == ['EXPTIME', 'NEXP']
        assert images[0].data.dtype == np.float32
        assert images[1].data.dtype == np.int16
        np.testing.assert_array_equal(images[0].data, t)
        np.testing.assert_array_equal(images[1].data, n)
        assert images[0].header['BUNIT'] == 's'
        for x in (h[0], *images):
            assert x.header['EXPSRC'] == 'drizzle'
            assert x.header['NINPUTS'] == 12 and x.header['NUSED'] == 11
        assert images[1].header['CRPIX1'] == tile['CRPIX1']
    assert not os.path.exists(f'{p}.tmp')


def _fake_mosaic(tmp_path, con):
    """sci (NaN for x>=45), manifest with two inputs (XPOSURE 100 / 300 s),
    and an _aux with CON + HDRTAB."""
    tile = _tile_header()
    name = 'mosaic_nircam_f150w_f_30mas_T1'
    sci = np.ones((40, 50), np.float32)
    sci[:, 45:] = np.nan
    fits.PrimaryHDU(sci, header=tile).writeto(tmp_path / f'{name}_sci.fits')
    inputs = []
    for i, ((x0, y0), t) in enumerate(zip([(5, 4), (15, 8)], [100.0, 300.0])):
        fn = f'jw_{i}_cal.fits'
        det = _detector_header(tile, x0, y0, 40, 30)
        prim = fits.PrimaryHDU()
        prim.header['XPOSURE'] = t
        fits.HDUList([prim, fits.ImageHDU(np.zeros((30, 40), np.float32),
                                          header=det, name='SCI')]
                     ).writeto(tmp_path / fn)
        inputs.append({'filename': f'/elsewhere/{fn}'})
    (tmp_path / f'{name}_manifest.json').write_text(json.dumps(
        {'mosaic_name': name, 'tile': 'T1', 'inputs': inputs}))
    hdrtab = fits.BinTableHDU.from_columns(
        [fits.Column(name='FILENAME', format='20A', array=['jw_0', 'jw_1']),
         fits.Column(name='XPOSURE', format='D', array=[100.0, 300.0])],
        name='HDRTAB')
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(con, name='CON'), hdrtab]
                 ).writeto(tmp_path / f'{name}_aux.fits')
    return name


def test_backfill_uses_footprints_when_context_is_placeholder(tmp_path):
    name = _fake_mosaic(tmp_path, np.zeros((1, 1, 1), np.int32))
    msg = mx.backfill_mosaic_exp(str(tmp_path), name)
    assert 'footprint' in msg
    with fits.open(tmp_path / f'{name}_exp.fits') as h:
        t, n = h['EXPTIME'].data, h['NEXP'].data
        assert h['NEXP'].header['EXPSRC'] == 'footprint'
        assert h['NEXP'].header['NINPUTS'] == 2
    assert n[10, 20] == 2 and t[10, 20] == 400
    assert n[5, 6] == 1 and t[5, 6] == 100
    assert n[0, 0] == 0 and t[0, 0] == 0
    assert (n[:, 45:] == 0).all() and (t[:, 45:] == 0).all()   # SCI NaN
    assert mx.backfill_mosaic_exp(str(tmp_path), name).endswith('exists')


def test_backfill_prefers_real_context(tmp_path):
    con = np.zeros((40, 50), np.int32)
    con[:, :30] = 0b11
    con[:, 30:] = 0b10
    name = _fake_mosaic(tmp_path, con)
    mx.backfill_mosaic_exp(str(tmp_path), name)
    with fits.open(tmp_path / f'{name}_exp.fits') as h:
        t, n = h['EXPTIME'].data, h['NEXP'].data
        assert h['NEXP'].header['EXPSRC'] == 'context'
        assert h['NEXP'].header['NUSED'] == 2
    assert n[0, 0] == 2 and t[0, 0] == 400
    assert n[0, 40] == 1 and t[0, 40] == 300
    assert n[0, 47] == 0 and t[0, 47] == 0
