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


def test_sky_total_on_cutout_sums_every_covering_trace():
    """On the gratings a pixel carries the sky of every slitlet whose trace crosses it:
    the consumer gets the painted total through the exact pixelwise factor, with NaN
    where nothing covers the pixel and the slit's own slitlet kept for reference."""
    g = _synthetic_geometry(nrow=6, ncol=5); table = _flat_table(0.8); prod = _product(q2=1.0)
    pix_sr = 5e-13
    frame = dict(sci=np.full((6, 5), 2.0), bunit='MJy', pix_sr=pix_sr, pthloss='UNIFORM', pu=None, pp=None, bar=None, x0=10, y0=20)
    own = M.sky_on_geometry(prod, frame, g, table)
    own_sky = 0.4 * 0.8 * pix_sr
    assert np.allclose(own['sky'], own_sky)
    # the detector: rate = 0.05 DN/s everywhere; the painted sky says 3 traces (own + 2 others) on
    # the lower rows and the own trace alone on the upper rows; one pixel uncovered, one unpainted
    rate = np.full((40, 30), 0.05)
    mb = np.zeros((40, 30)); mb[20:23, 10:15] = 3 * 0.05; mb[23:26, 10:15] = 0.05
    dq = np.zeros((40, 30), np.uint8); dq[20, 10] = M.DQ_UNCOVERED; dq[21, 11] = M.DQ_UNPAINTED
    n2d = np.zeros((40, 30), np.uint8); n2d[20:23, 10:15] = 3; n2d[23:26, 10:15] = 1
    prod.update(mbkg2d=mb, dq2d=dq, n2d=n2d, rate=rate, path='unused')
    r = M.sky_total_on_cutout(prod, own, frame, mask_bits=0)
    F = 2.0 / 0.05                                   # sci / rate: the calibration factor
    assert np.allclose(r['sky'][1:3, 1:], 3 * 0.05 * F)         # three traces' sky, in the slit's units
    assert np.allclose(r['sky'][3:, :], 0.05 * F)
    assert np.isnan(r['sky'][0, 0]) and np.isfinite(r['sky'][1, 1])   # uncovered -> NaN; unpainted kept when unmasked
    assert r['dq'][1, 1] == M.DQ_UNPAINTED and r['n_cover'][0, 1] == 3
    assert np.isnan(M.sky_total_on_cutout(prod, own, frame, mask_bits=M.DQ_UNPAINTED)['sky'][1, 1])   # masked on request
    assert np.allclose(r['own_sky'], own_sky)
    # the variance scales with the total (the floor is a fraction of what is subtracted)
    assert np.allclose(r['var'][1, 1] / r['var'][3, 1], 9.0)


def test_pixelwise_factor_fills_flagged_pixels_from_the_column():
    sci = np.array([[2.0, 4.0], [2.0, 0.0], [2.0, 4.0]]); rate = np.array([[1.0, 1.0], [0.0, 1.0], [1.0, 1.0]])
    F = M.pixelwise_factor(sci, rate)
    assert np.allclose(F, [[2, 4], [2, 4], [2, 4]])


def test_total_masks_nothing_by_default_and_carries_the_rate_unit_sky():
    """Nothing is masked unless asked (Hollis, 2026-10-30): an unpainted covering trace
    is only a flag; the retired tail bit masks nothing. Past the calibrated columns
    ``sky`` has no units to be in (NaN) while ``sky_rate`` carries the painted DN/s."""
    g = _synthetic_geometry(nrow=6, ncol=5); table = _flat_table(0.8); prod = _product(q2=1.0)
    sci = np.full((6, 5), 2.0); sci[:, 0] = np.nan                        # the first column is not calibrated
    frame = dict(sci=sci, bunit='MJy', pix_sr=5e-13, pthloss='UNIFORM', pu=None, pp=None, bar=None, x0=0, y0=0)
    own = M.sky_on_geometry(prod, frame, g, table)
    rate = np.full((6, 5), 0.05); mb = np.full((6, 5), 0.10)             # own + one other trace
    dq = np.zeros((6, 5), np.uint8); dq[0, 1] = M.DQ_UNPAINTED; dq[0, 2] = M.DQ_TAIL
    prod.update(mbkg2d=mb, dq2d=dq, n2d=np.full((6, 5), 2, np.uint8), rate=rate, path='unused')
    r = M.sky_total_on_cutout(prod, own, frame)
    assert np.isfinite(r['sky'][0, 1]) and np.isfinite(r['sky'][0, 2]) and np.isfinite(r['sky'][1, 1])
    assert np.isnan(r['sky'][:, 0]).all() and np.allclose(r['sky_rate'][:, 0], 0.10)     # uncalibrated: rate units only
    assert np.allclose(r['rate'], 0.05) and not r['calibrated'][:, 0].any() and r['calibrated'][:, 1:].all()
    r2 = M.sky_total_on_cutout(prod, own, frame, mask_bits=M.DQ_UNPAINTED)
    assert np.isnan(r2['sky'][0, 1]) and np.isnan(r2['sky_rate'][0, 1]) and np.isfinite(r2['sky'][0, 2])
    # the own part rescaled by 0.9 lowers the total by 0.1 x own, not 0.1 x total
    r3 = M.sky_total_on_cutout(prod, own, frame, own_scale=0.9)
    assert np.allclose(r3['sky'][1, 1], r['sky'][1, 1] - 0.1 * own['sky'][1, 1])


def test_rate_sky_falls_back_to_the_pool_then_the_other_detector_then_holds():
    """Bin by bin: the exposure's own shutters, else the same detector's group pool, else
    the other detector scaled over the bins both have, else the nearest filled bin."""
    nb = 8
    def spec(base, sb, k):
        sb = np.asarray(sb, float)
        return M.ShutterSpectrum(rate_base=base, source_id=0, slitlet_id=1, quadrant=1, k=k, n_pix=100,
                                 sb=sb, err=np.where(np.isfinite(sb), 0.01, np.nan), n=np.where(np.isfinite(sb), 10, 0))
    nan = np.nan
    own = [spec('r_00001_nrs1', [1, 1, 1, 1, nan, nan, nan, nan], k) for k in (1, 2, 3)]          # bins 0-3
    pool = [spec('r_00002_nrs1', [1, 1, 1, 1, 2, nan, nan, nan], k) for k in (1, 2, 3)]          # bin 4 too
    other = [spec('r_00001_nrs2', [nan, nan, 0.5, 0.5, 1.0, 1.5, 1.5, nan], k) for k in (1, 2, 3)]   # half the response
    specs_of = {'r_00001_nrs1': own, 'r_00002_nrs1': pool, 'r_00001_nrs2': other}
    sb, err, n, src = M.rate_sky_for('r_00001_nrs1', specs_of, clip_sigma=3.0, min_overlap=3)
    assert src.tolist() == [1, 1, 1, 1, 2, 3, 3, 4]
    assert np.allclose(sb[:4], 1.0) and sb[4] == 2.0
    assert np.allclose(sb[5:7], 3.0)                 # the other detector x the overlap scale (1 / 0.5 = 2)
    assert sb[7] == 3.0                              # held outward
    assert M.rate_sky_for('r_00003_nrs2', {}, 3.0) is None


def test_rate_sky_edges_extend_the_grid_with_the_end_steps():
    centres = np.array([1.0, 1.1, 1.2, 1.3])
    e = M.rate_sky_edges(centres, 0.72, 1.56)
    assert np.allclose(np.diff(e), 0.1)
    assert e[0] <= 0.72 < e[1] and e[-2] < 1.56 <= e[-1]
    assert np.allclose(M.rate_sky_edges(centres, 1.0, 1.3), [0.95, 1.05, 1.15, 1.25, 1.35])


def test_slitlet_scale_reads_the_slits_clean_shutters():
    from astropy.table import Table
    t = Table({'slit': ['5', '5', '5', 'BKG77'], 'clean': [True, True, False, True], 'scale': [0.9, 0.94, 1.5, 1.2]})
    assert M.slitlet_scale({'shutters': t}, '5') == pytest.approx(0.92)
    assert M.slitlet_scale({'shutters': t}, 'BKG77') == pytest.approx(1.2)
    assert M.slitlet_scale({'shutters': t}, '6') == 1.0


def test_combine_weights_shutters_equally_when_errors_follow_their_own_values():
    # each shutter's error grows with its own noisy value, as a pixel variance from its own
    # slope does: inverse-variance weights would pull the mean low, equal weights must not
    rng = np.random.default_rng(7)
    spectra = []
    for i in range(400):
        v = 1.0 + rng.normal(0, 0.3, 20)
        s = _shutter(v, sid=i)
        s.err = 0.05 + 0.25 * np.clip(v, 0.05, None)
        spectra.append(s)
    sb, err, n = M.combine(spectra, clip_sigma=5.0)
    S = np.array([s.sb for s in spectra]); W = 1 / np.array([s.err for s in spectra]) ** 2
    weighted = np.sum(W * S, 0) / np.sum(W, 0)
    assert np.all(np.abs(sb - S.mean(0)) < 0.01)                 # the plain mean of the shutters
    assert np.median(1 - weighted) > 0.03                         # what 1/err^2 would have given
    assert np.all(n == 400) and np.allclose(err, np.median(np.array([s.err for s in spectra]), 0) / 20)


def test_core_to_centre_reads_the_table_and_leaves_unmeasured_pixels_nan():
    from types import SimpleNamespace
    table = dict(data1x1=np.full((10, 5), 0.9), data1x3=np.linspace(0.97, 1.0, 10)[:, None] * np.ones((10, 5)),
                 grid=SimpleNamespace(y0=0.0, dy=1.0, w0=1e-6, dw=1e-6))
    seg = np.array([2, 2, -1, 0]); yseg = np.array([0.0, 9.0, 3.0, 4.0]); lam = np.array([1.0, 2.0, 2.0, 3.0])
    bs = M.core_to_centre(table, seg, yseg, lam)
    assert np.isclose(bs[0], 0.97) and np.isclose(bs[1], 1.0) and np.isnan(bs[2])
    assert np.isclose(bs[3], 0.9)                                 # an outer-edge pixel reads DATA1X1


def test_load_2d_needs_the_rate_file_beside_the_product(tmp_path):
    # without the rate file sky_for_slit cannot form the painted total and falls back to
    # the slit's own slitlet (with a warning); with it, the 2-D arrays and the rate load
    sky = (np.ones(4), np.full(4, 0.1), np.full(4, 3))
    path = str(tmp_path / 'r_00001_nrs1_mbkg.fits')
    M.write_product(path, {'MBKGRATE': ('r_00001_nrs1_rate.fits', '')}, np.linspace(1, 2, 4), sky, None, None, [],
                    mb2d=np.ones((3, 3)), dq2d=np.zeros((3, 3)), n2d=np.ones((3, 3)))
    assert not M._load_2d(M.load_product(path))
    fits.PrimaryHDU(np.full((3, 3), 0.5, np.float32)).writeto(tmp_path / 'r_00001_nrs1_rate.fits')
    prod = M.load_product(path)
    assert M._load_2d(prod) and np.allclose(prod['rate'], 0.5) and prod['mbkg2d'].shape == (3, 3)


def _exposure(rng, n, level, noise, nb=80, bump=None):
    out = []
    for i in range(n):
        sb = level * (1 + rng.normal(0, noise, nb)) * (1 + rng.normal(0, 0.02))      # pixel noise + per-shutter spread
        if bump is not None:
            sb[bump[0]:bump[1]] *= bump[2]
        out.append(_shutter(sb, err=noise, sid=i))
    return out


def test_pool_check_accepts_a_consistent_exposure_and_catches_a_local_excess():
    rng = np.random.default_rng(11)
    opt = M.options({})
    centres = np.linspace(3.0, 5.0, 80)
    pool = M.combine(_exposure(rng, 300, 1.0, 0.2), 3.0)
    ok = M.pool_check(_exposure(rng, 60, 1.0, 0.2), pool, centres, opt)
    assert ok['differs'] is False and ok['verdict'] == 'consistent' and ok['n_bands'] == 8
    # +18 % in one tenth of the range (ember_uds_p2 nod 2 at the 3 um sky minimum)
    bad = M.pool_check(_exposure(rng, 60, 1.0, 0.2, bump=(20, 28, 1.18)), pool, centres, opt)
    assert bad['differs'] is True and 0.08 < bad['dev'] < 0.2 and bad['lam_lo'] <= centres[24] <= bad['lam_hi']
    assert bad['verdict'].startswith('differs')


def test_pool_check_needs_both_the_tolerance_and_the_significance():
    rng = np.random.default_rng(12)
    opt = M.options({})
    centres = np.linspace(1.0, 5.0, 80)
    # many quiet shutters 0.6 % high: significant, but under the 2 % tolerance (the prism case)
    pool = M.combine(_exposure(rng, 600, 1.0, 0.01), 3.0)
    tiny = M.pool_check(_exposure(rng, 200, 1.006, 0.01), pool, centres, opt)
    assert tiny['nsig'] > 3 and tiny['differs'] is False
    # shutters 4 % high whose own errors are large: over the tolerance, not significant (a
    # grating exposure); their scatter is zero here, so only the error floor keeps them in
    pool = (np.ones(80), np.full(80, 0.01), np.full(80, 300))
    noisy = M.pool_check([_shutter(np.full(80, 1.04), err=0.6, sid=i) for i in range(8)], pool, centres, opt)
    assert noisy['differs'] is False and noisy['dev'] > 0.03 and noisy['nsig'] < 3
    # fewer than min_shutters in every band: untested, and the caller paints the pool
    assert M.pool_check(_exposure(rng, 4, 1.04, 0.3), pool, centres, opt)['differs'] is None
    assert M.pool_check([], pool, centres, opt)['differs'] is None


def test_rate_sky_takes_the_pool_first_when_asked():
    a = [_shutter(np.full(10, 1.10), sid=i, rate='r_00001_nrs1') for i in range(3)]
    b = [_shutter(np.full(10, 1.00), sid=10 + i, rate='r_00002_nrs1') for i in range(6)]
    specs_of = {'r_00001_nrs1': a, 'r_00002_nrs1': b}
    own = M.rate_sky_for('r_00001_nrs1', specs_of, 3.0)
    pooled = M.rate_sky_for('r_00001_nrs1', specs_of, 3.0, pool_first=True)
    assert np.allclose(own[0], 1.10) and np.all(own[3] == 1)
    assert np.allclose(pooled[0], M.combine(a + b, 3.0)[0]) and np.all(pooled[3] == 2)


def test_pool_members_drops_the_discrepant_exposure_and_keeps_the_rest_together():
    # ember_uds_p2: nod 2 is +18 % over part of the range; against a pool that holds it, nods 1
    # and 3 look low too. The iteration drops nod 2 and leaves nods 1 and 3 pooled.
    rng = np.random.default_rng(21)
    opt = M.options({})
    centres = np.linspace(1.0, 5.0, 80)
    clean_of = {'r_00001_nrs1': _exposure(rng, 150, 1.0, 0.03),
                'r_00002_nrs1': _exposure(rng, 150, 1.0, 0.03, bump=(30, 60, 1.18)),
                'r_00003_nrs1': _exposure(rng, 150, 1.0, 0.03)}
    members, checks = M.pool_members(clean_of, centres, opt)
    assert members == {'r_00001_nrs1', 'r_00003_nrs1'}
    assert checks['r_00002_nrs1']['differs'] is True and checks['r_00002_nrs1']['dev'] > 0.1
    assert all(checks[b]['verdict'] == 'consistent' for b in members)


def _layer(row, lam0, val, is_open, ncols=10, disp=0.002):
    """One slitlet's layer along a detector row: wavelength rising by ``disp`` per column."""
    cols = np.arange(ncols)
    return (row * ncols + cols, lam0 + disp * cols, np.full(ncols, float(val)), np.full(ncols, bool(is_open)))


def test_merge_painted_counts_one_msa_spot_once():
    shape = (4, 10)
    layers = [
        _layer(0, 3.0, 1.0, True), _layer(0, 3.1, 2.0, True),         # crossing traces: two spots, summed
        _layer(1, 3.0, 1.0, True), _layer(1, 3.0005, 0.1, False),     # margin over the next shutter in its column
        _layer(2, 3.0, 1.0, True), _layer(2, 3.05, 0.1, False),       # margin of a crossing trace: summed
        _layer(3, 3.0, 0.2, False), _layer(3, 3.0, 0.4, False),       # two margins over one bar: their mean
    ]
    mb, n_spots, n_layers = M.merge_painted(shape, layers)
    np.testing.assert_allclose(mb[:, 5], [3.0, 1.0, 1.1, 0.3], rtol=1e-6)
    assert list(n_spots[:, 5]) == [2, 1, 2, 1]
    assert np.all(n_layers == 2)


def test_merge_painted_empty():
    mb, n_spots, n_layers = M.merge_painted((3, 4), [])
    assert mb.shape == (3, 4) and not mb.any() and not n_spots.any() and not n_layers.any()


def test_failed_open_transmission():
    rng = np.random.default_rng(1)
    n = 500
    val = np.full(n, 2.0)                      # the shutter's full-sky paint
    base = np.full(n, 1.0)                     # everything else painted on those pixels
    core = np.ones(n, bool)
    rate = base + 0.4 * val + rng.normal(0, 0.01, n)
    rate[:20] += 50.0                          # cosmic rays: the median ignores them
    a, used, scale = M.failed_open_transmission(rate, base, val, core)
    assert abs(a - 0.4) < 0.01 and used == n and scale == a
    # more light than an empty shutter's sky (an object behind it): painted at 1
    a, _, scale = M.failed_open_transmission(base + 1.5 * val, base, val, core)
    assert abs(a - 1.5) < 1e-9 and scale == 1.0
    # a shutter passing nothing: not painted
    _, _, scale = M.failed_open_transmission(base - 0.05 * val, base, val, core)
    assert scale == 0.0
    # too few pixels to measure: painted at 1
    few = np.zeros(n, bool)
    few[:50] = True
    a, used, scale = M.failed_open_transmission(rate, base, val, few)
    assert np.isnan(a) and used == 50 and scale == 1.0


def _fo(name, a, scale=None):
    s = float(np.clip(a, 0, 1)) if np.isfinite(a) else 1.0
    return dict(name=name, quadrant=3, a=a, scale=s if scale is None else scale)


def test_merge_painted_is_linear_in_each_layer():
    # layers sharing spots both ways: an open entry with another open entry at the same spot,
    # a margin under another layer's open shutter, two margins together, and a crossing
    rng = np.random.default_rng(3)
    shape = (4, 6)
    pix = np.arange(24)
    lam = 1.0 + 0.01 * (pix % 6)
    def layer(open_mask, dlam=0.0):
        return (pix.copy(), lam + dlam, rng.uniform(1, 2, 24), open_mask)
    layers = [layer(pix % 2 == 0), layer(pix % 3 == 0), layer(np.zeros(24, bool)), layer(pix % 4 == 0, dlam=0.5)]
    total, _, _, wts = M.merge_painted(shape, layers, weights_of=[1])
    w, alone = wts[1]
    scaled = list(layers)
    scaled[1] = (layers[1][0], layers[1][1], 0.3 * layers[1][2], layers[1][3])
    total2 = M.merge_painted(shape, scaled)[0]
    pred = total.ravel().astype(float)
    np.add.at(pred, layers[1][0], (0.3 - 1.0) * layers[1][2] * w)
    assert np.allclose(total2.ravel(), pred, rtol=1e-6)
    assert not alone.any() and set(np.unique(w)) <= {0.0, 1 / 3, 0.5, 1.0}


def test_combine_failed_open_over_nods():
    nods = [f'jw01_05101_0000{k}' for k in (1, 2, 3)]
    per = {f'{n}_nrs1': [_fo('14', a), _fo('19', b), _fo('5', c), _fo('7', d)]
           for n, a, b, c, d in zip(nods, (0.34, 0.02, 0.03), (0.51, 0.52, 0.49),
                                    (0.0, 0.5, 1.0), (0.4, np.nan, np.nan))}
    per['jw01_05101_00001_nrs2'] = [_fo('14', 0.25)]
    scales, report = M.combine_failed_open(per, tol=0.1)
    # 14: an object behind it in nod 1, the other two agree: every nod takes their median
    assert all(abs(scales[f'{n}_nrs1'][('14', 3)] - 0.025) < 1e-9 for n in nods)
    # the other detector is not combined with it (one measurement: its own)
    assert scales['jw01_05101_00001_nrs2'][('14', 3)] == 0.25
    assert all(abs(scales[f'{n}_nrs1'][('19', 3)] - 0.51) < 1e-9 for n in nods)
    # no majority within the tolerance: each nod keeps its own
    assert [scales[f'{n}_nrs1'][('5', 3)] for n in nods] == [0.0, 0.5, 1.0]
    # one measurement only: kept, and the unmeasured nods stay at 1
    assert [scales[f'{n}_nrs1'][('7', 3)] for n in nods] == [0.4, 1.0, 1.0]
    row = [r for r in report if r['name'] == '14' and r['detector'] == 'nrs1'][0]
    assert row['n_measured'] == 3 and row['n_agree'] == 2


def test_rescale_failed_open():
    mb = np.zeros((2, 3), np.float32)
    dq = np.zeros((2, 3), np.uint8)
    cover = np.ones((2, 3), np.uint8)
    pix = np.array([0, 1, 2])
    val = np.array([2.0, 4.0, 6.0], np.float32)
    mb.reshape(-1)[pix] += 0.5 * val                 # painted at 0.5
    dq.reshape(-1)[pix] |= M.DQ_FAILED_OPEN
    cover.reshape(-1)[pix] += 1
    f = dict(name='14', quadrant=3, applied=0.5, pix=pix, val=val, w=np.ones(3, np.float32),
             alone=np.ones(3, bool))
    assert M.rescale_failed_open(mb, dq, cover, [f], {('14', 3): 0.25})
    assert np.allclose(mb.reshape(-1)[:3], 0.25 * val) and f['applied'] == 0.25
    assert M.rescale_failed_open(mb, dq, cover, [f], {('14', 3): 0.0})
    assert np.allclose(mb, 0) and not (dq & M.DQ_FAILED_OPEN).any() and np.all(cover == 1)
    assert M.rescale_failed_open(mb, dq, cover, [f], {('14', 3): 1.0})
    assert np.allclose(mb.reshape(-1)[:3], val) and (dq.reshape(-1)[:3] == M.DQ_FAILED_OPEN).all()
    assert cover.reshape(-1)[:3].tolist() == [2, 2, 2]
    assert not M.rescale_failed_open(mb, dq, cover, [f], {('14', 3): 1.0})


def test_failed_open_over_nods_rewrites_the_products(tmp_path, monkeypatch):
    paths = {}
    val = np.array([2.0, 4.0], np.float32)
    fo_of = {}
    for k, a in ((1, 0.34), (2, 0.02), (3, 0.03)):
        base = f'jw01_05101_0000{k}_nrs1'
        mb = np.ones((2, 2), np.float32)
        mb.reshape(-1)[:2] += a * val
        hdul = fits.HDUList([fits.PrimaryHDU(),
                             fits.CompImageHDU(mb, name='MBKG2D', compression_type='GZIP_2', quantize_level=0),
                             fits.CompImageHDU(np.full((2, 2), 2, np.uint8), name='MBKG2DN', compression_type='RICE_1'),
                             fits.CompImageHDU(np.array([[8, 8], [0, 0]], np.uint8), name='MBKG2DDQ',
                                               compression_type='RICE_1')])
        hdul['MBKG2D'].header['BUNIT'] = 'DN/s'
        paths[base] = str(tmp_path / f'{base}_mbkg.fits')
        hdul.writeto(paths[base])
        fo_of[base] = [dict(name='14', quadrant=3, a=a, scale=a, applied=a, pix=np.array([0, 1]), val=val,
                            w=np.ones(2, np.float32), alone=np.zeros(2, bool))]
    monkeypatch.setattr(M, 'product_path', lambda obs, base: paths[base])
    M._failed_open_over_nods(None, fo_of, 0.1)
    for base, p in paths.items():
        with fits.open(p) as h:
            assert np.allclose(h['MBKG2D'].data.reshape(-1)[:2], 1 + 0.025 * val, rtol=1e-6)
            assert np.allclose(h['MBKG2D'].data.reshape(-1)[2:], 1.0)
            assert h['MBKG2D'].header['BUNIT'] == 'DN/s'
            assert h[0].header['MBKGFOSC'] == 'measured, nods' and h[0].header['MBKGNFO'] == 1
            assert h['MBKG2DDQ'].data.tolist() == [[8, 8], [0, 0]] and (h['MBKG2DN'].data == 2).all()
