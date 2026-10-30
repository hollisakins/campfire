"""Unit tests for the master-background derivation (synthetic inputs, no data, no jwst)."""
import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nirspec import master_bkg as M


def _shutter(sb, err=0.01, q=1, k=1, sid=100, rate='r_00001_nrs1'):
    sb = np.asarray(sb, float)
    return M.ShutterSpectrum(rate_base=rate, source_id=sid, slitlet_id=sid, quadrant=q, k=k, n_pix=50,
                             sb=sb, err=np.full(sb.shape, err), n=np.full(sb.shape, 5))


def test_options_strip_the_prefix():
    o = M.options({'master_bkg_core_frac': 0.3, 'unrelated': 1})
    assert o['core_frac'] == 0.3 and o['bin_pixels'] == M.DEFAULTS['master_bkg_bin_pixels']
    assert 'unrelated' not in o


def test_bin_spectrum_median_and_counts():
    edges = np.array([1.0, 1.1, 1.2, 1.3])
    lam = np.array([1.01, 1.05, 1.09, 1.15, 1.25, 1.25, 1.25, 1.25])
    sb = np.array([1.0, 2.0, 3.0, 5.0, 1.0, 1.0, 1.0, 9.0])
    med, err, n = M.bin_spectrum(lam, sb, np.full(8, 0.04), edges)
    assert med[0] == 2.0 and n[0] == 3
    assert med[1] == 5.0 and n[1] == 1
    assert med[2] == 1.0 and n[2] == 4
    assert np.all(np.isfinite(err))
    assert err[2] >= 1.2533 * np.sqrt(0.04 / 4) - 1e-9     # never below the propagated error


def test_edges_follow_the_dispersion():
    rows = [np.linspace(1.0, 2.0, 101)] * 3          # 0.01 um per pixel
    edges = M.edges_from_rows(rows, bin_pixels=2.0)
    d = np.diff(edges)
    assert np.all(d > 0)
    assert np.allclose(d, 0.02, rtol=0.05)


def test_select_clean_marks_each_contaminant():
    rng = np.random.default_rng(3)
    nb = 60
    lam = np.linspace(1, 5, nb)
    base = 0.5 + 0.2 * np.sin(lam)
    shutters = [_shutter(base * (1 + rng.normal(0, 0.005, nb)), err=0.005, q=1 + i % 4, sid=i) for i in range(30)]
    shutters.append(_shutter(base * 1.30, err=0.005, sid=900))             # a neighbour's continuum
    shutters.append(_shutter(base * 0.50, err=0.005, sid=901))             # a half-closed shutter
    line = base.copy(); line[20:25] += 0.3                                  # an emission line
    shutters.append(_shutter(line, err=0.005, sid=902))
    opt = M.options({})
    sky = M.select_clean(shutters, M.median_seed(shutters), opt)
    reasons = {s.source_id: s.reason for s in shutters}
    assert reasons[900] == 'continuum_excess'
    assert reasons[901] == 'low_throughput'
    assert reasons[902] == 'line_excess'
    assert sum(s.clean for s in shutters) == 30
    assert np.allclose(sky[0], base, rtol=0.01)
    assert np.all(sky[2] == 30)


def test_quadrant_scales_recover_the_quadrant_term():
    nb = 40
    base = np.ones(nb)
    shutters = [_shutter(base * (1.02 if q in (2, 4) else 0.98), q=q, sid=10 * q + i) for q in (1, 2, 3, 4) for i in range(4)]
    for s in shutters:
        s.clean = True
    sky = (base, np.full(nb, 0.002), np.full(nb, 16))
    qs = M.quadrant_scales(shutters, sky, M.options({}))
    assert qs[1][0] == pytest.approx(0.98, abs=1e-3) and qs[2][0] == pytest.approx(1.02, abs=1e-3)
    assert qs[1][1] == 4


def test_scale_offset_fit():
    nb = 50
    pool = (np.linspace(0.2, 1.0, nb), np.full(nb, 0.003), np.full(nb, 20))
    sky = (1.1 * pool[0] + 0.05, np.full(nb, 0.003), np.full(nb, 5))
    a, ae, b, be, n = M.scale_offset(sky, pool)
    assert a == pytest.approx(1.1, abs=1e-6) and b == pytest.approx(0.05, abs=1e-6) and n == nb


def test_frame_factor_units():
    pix_sr = 5e-13
    pu = np.array([[0.8, 0.8]]); pp = np.array([[0.4, 0.4]]); bar = np.array([[0.5, 1.0]])
    assert M.frame_factor('MJy/sr', pix_sr, 'UNIFORM', None, None, None) == 1.0
    assert M.frame_factor('MJy', pix_sr, 'UNIFORM', None, None, None) == pytest.approx(pix_sr)
    f = M.frame_factor('MJy', pix_sr, 'POINT', pu, pp, None)
    assert np.allclose(f, pix_sr * 2.0)                     # the uniform / point pathloss ratio
    f = M.frame_factor('MJy/sr', pix_sr, 'UNIFORM', None, None, bar)
    assert np.allclose(f, [[2.0, 1.0]])                      # the file already divided by its bar shadow


def test_write_and_load_product(tmp_path):
    nb = 30
    centres = np.linspace(1, 5, nb)
    sky = (np.full(nb, 0.4), np.full(nb, 0.004), np.full(nb, 12))
    pool = (np.full(nb, 0.41), np.full(nb, 0.002), np.full(nb, 60))
    shutters = [_shutter(np.full(nb, 0.4), q=2, sid=5), _shutter(np.full(nb, 0.4), q=3, sid=-77)]
    shutters[0].clean, shutters[0].reason, shutters[0].scale = True, 'clean', 1.0
    shutters[1].clean, shutters[1].reason, shutters[1].scale = False, 'continuum_excess', 1.3
    cards = {'CFMBKGV': (M.ALGORITHM_VERSION, 'v'), 'MBKGQ1': (0.99, 'q1'), 'MBKGQ2': (1.01, 'q2'), 'MBKGQ3': (np.nan, 'q3'),
             'MBKGQ4': (1.0, 'q4'), 'MBKGEFLR': (0.03, 'floor'), 'MBKGBSRF': ('empirical_jwst_nirspec_barshadow_prism.fits', 'bs')}
    out = tmp_path / 'x_mbkg.fits'
    M.write_product(str(out), cards, centres, sky, pool, None, shutters, mb2d=np.zeros((4, 4)), dq2d=np.ones((4, 4)),
                    history=['one line'])
    with fits.open(out) as h:
        assert [x.name for x in h] == ['PRIMARY', 'MBKG1D', 'SHUTTERS', 'MBKG2D', 'MBKG2DDQ']
        assert h['MBKG2D'].header['BUNIT'] == 'DN/s' and h['MBKG2DDQ'].data.dtype == np.uint8
        assert list(h['SHUTTERS'].data['slit']) == ['5', 'BKG77']
        assert list(h['SHUTTERS'].data['reason']) == ['clean', 'continuum_excess']
    prod = M.load_product(str(out))
    assert prod['qscales'][2] == pytest.approx(1.01) and np.isnan(prod['qscales'][3])
    assert prod['floor'] == pytest.approx(0.03) and prod['bs_ref'].startswith('empirical_')
    assert np.allclose(prod['surf_bright'], 0.4) and prod['n_shutters'][0] == 12


def _synthetic_geometry(nrow=6, ncol=5, quadrant=2):
    """A 3-shutter slitlet '1x1' cutout: every pixel in an open shutter's lower outer
    edge segment (so the bar-shadow table is read at a known row)."""
    from campfire_pipeline.nirspec import barshadow_calibration as B
    lam = np.full((nrow, ncol), 2.0) + 0.01 * np.arange(ncol)[None, :]
    yseg = np.full((nrow, ncol), -0.3)
    seg = np.full((nrow, ncol), B.SEGMENTS.index('edge_lower'))
    return dict(lam=lam, seg=seg, yseg=yseg, j=np.zeros((nrow, ncol), int), state='1x1', quadrant=quadrant,
                ys=np.zeros((nrow, ncol)), k=np.zeros((nrow, ncol)), f=np.zeros((nrow, ncol)),
                is_open=np.ones((nrow, ncol), bool), pitch=1.15, kx=1, slitlet_id=7)


def _flat_table(value=0.8):
    from campfire_pipeline.nirspec import barshadow_calibration as B
    grid = B.Grid(w0=1e-6, dw=0.05e-6, n_w=101, y0=-1.0, dy=0.002, n_y=1001)
    return dict(data1x1=np.full((grid.n_y, grid.n_w), value), data1x3=np.full((grid.n_y, grid.n_w), value), grid=grid)


def _product(q2=1.1, sb=0.4, err=0.004, floor=0.03):
    w = np.linspace(1, 5, 50)
    return dict(wavelength=w, surf_bright=np.full(50, sb), surf_bright_err=np.full(50, err), n_shutters=np.full(50, 9),
                qscales={1: 1.0, 2: q2, 3: np.nan, 4: 1.0}, floor=floor, bs_ref='CRDS')


def test_sky_on_geometry_units_point_and_extended_canonicals_and_background_slitlet():
    """The sky comes back in each file's own units: MJy per pixel for a canonical,
    whether jwst calibrated it as POINT (pathloss ratio applied) or EXTENDED (none),
    and MJy/sr for a background slitlet that carries its own bar-shadow division."""
    g = _synthetic_geometry(); table = _flat_table(0.8); prod = _product()
    pix_sr = 5e-13
    shape = g['lam'].shape
    pu = np.full(shape, 0.8); pp = np.full(shape, 0.4)
    sb_through = 0.4 * 1.1 * 0.8                         # sky x quadrant 2 scale x bar shadow
    point = dict(sci=np.ones(shape), bunit='MJy', pix_sr=pix_sr, pthloss='POINT', pu=pu, pp=pp, bar=None, x0=0, y0=0)
    r = M.sky_on_geometry(prod, point, g, table)
    assert np.allclose(r['sky'], sb_through * pix_sr * 2.0)      # MJy per pixel, uniform/point pathloss
    assert np.allclose(r['sb'], sb_through) and np.allclose(r['bs'], 0.8)
    extended = dict(point, pthloss='UNIFORM')
    r = M.sky_on_geometry(prod, extended, g, table)
    assert np.allclose(r['sky'], sb_through * pix_sr)             # MJy per pixel, no pathloss ratio
    expected_var = ((0.004 * 1.1 * 0.8) ** 2 + (0.03 * sb_through) ** 2) * pix_sr ** 2
    assert np.allclose(r['var'], expected_var)
    bkg = dict(point, bunit='MJy/sr', pthloss='UNIFORM', bar=np.full(shape, 0.8))
    r = M.sky_on_geometry(prod, bkg, g, table)
    assert np.allclose(r['sky'], 0.4 * 1.1)                       # MJy/sr, the file's own bar-shadow division undone
    # a quadrant without a scale (NaN) leaves the sky unscaled
    g3 = dict(g, quadrant=3)
    r = M.sky_on_geometry(prod, extended, g3, table)
    assert np.allclose(r['sky'], 0.4 * 0.8 * pix_sr)
    # outside the product's wavelength coverage the sky is NaN
    g_out = dict(g, lam=np.full(shape, 7.0))
    r = M.sky_on_geometry(prod, extended, g_out, table)
    assert np.all(np.isnan(r['sky']))
