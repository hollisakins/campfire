"""Master-background step 1: the compact rate-file geometry (``coverage.RateGeometry``) and
the step's own pieces (the grid rows, the judged/sky split of the selection, the CALLS
reader), on fake slit transforms and synthetic spectra."""
import numpy as np
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.nirspec import coverage as C
from campfire_pipeline.nirspec import master_bkg as M
from campfire_pipeline.nirspec import master_bkg_rate as R


def _transform(row0):
    """A slit whose trace runs along x at detector row ``row0``: wavelength 1 + 0.001 x um,
    slit y in open-shutter heights, defined within the slitlet's pads."""
    def tr(x, y):
        x = np.asarray(x, float); y = np.asarray(y, float)
        ys = (y - row0) / 4.0 * 1.15            # 4 rows per pitch
        lam = (1.0 + 0.001 * x) * 1e-6
        inside = (ys >= -2.3) & (ys <= 2.3)
        return np.where(inside, x, np.nan), np.where(inside, ys, np.nan), np.where(inside, lam, np.nan)
    return tr


def _geometry(shape=(60, 80), slits=((15, '1x1', 7), (40, 'x11', 9))):
    """A RateGeometry of two slitlets built the way ``RateGeometry.from_rate`` does."""
    pix, lam, ys, meta, blocks = [], [], [], [], []
    for row0, state, name in slits:
        g = C.geometry_on_block(_transform(row0), 0, 0, row0 - 12, row0 + 12, state, 1.15,
                                cutoffs=(1.005, 1.07), ncols=shape[1], nrows=shape[0], with_segments=True)
        foot = g['footprint']
        rr, cc = np.nonzero(foot)
        pix.append(((rr + g['row_lo']) * shape[1] + cc).astype(np.int32))
        lam.append(g['lam'][foot].astype(np.float32)); ys.append(g['ys'][foot].astype(np.float32))
        meta.append(dict(kind='msa', name=str(name), quadrant=2, ycen=float(row0), source_id=1000 + name, state=state))
        blocks.append(g)
    offsets = np.cumsum([0] + [p.size for p in pix])
    return C.RateGeometry(np.concatenate(pix), np.concatenate(lam), np.concatenate(ys), offsets, meta, shape), blocks


def test_rate_geometry_round_trips_and_reproduces_the_block_geometry(tmp_path):
    geo, blocks = _geometry()
    path = str(tmp_path / 'g.npz')
    geo.save(path)
    back = C.RateGeometry.load(path)
    assert back.shape == geo.shape and back.meta == geo.meta and np.array_equal(back.pix, geo.pix)
    for b, g in zip(back.blocks(), blocks):
        foot = g['footprint']
        assert np.array_equal(b['j'], g['j'][foot]) and np.array_equal(b['is_open'], g['is_open'][foot])
        assert np.array_equal(b['seg'], g['seg'][foot])
        assert np.allclose(b['f'], g['f'][foot], atol=1e-6) and np.allclose(b['lam'], g['lam'][foot], atol=1e-6)


def test_the_counter_is_the_sum_of_the_footprints():
    geo, blocks = _geometry(slits=((15, '1x1', 7), (17, '11x', 8)))      # two slitlets that overlap
    frame = np.zeros(geo.shape, np.int16)
    for g in blocks:
        C.add_block(frame, g)
    assert np.array_equal(geo.counter(), frame) and geo.counter().max() == 2


def test_geometry_cache_sits_beside_the_counter_with_its_own_name():
    a = C.cache_path('/x/jw_rate.fits', '/c', (2.6, 5.7))
    g = C.geometry_cache_path('/x/jw_rate.fits', '/c', (2.6, 5.7))
    assert g != a and g.endswith('.npz') and '/geometry_jw_rate_' in g and '_cut' in g


def test_central_rows_take_one_pixel_per_column_nearest_the_source_row():
    geo, _ = _geometry()
    rows = R._central_rows(geo)
    assert len(rows) == 2
    for r in rows:
        assert np.all(np.diff(np.sort(r)) > 0)                      # one wavelength per column
        assert np.isclose(r.min(), 1.005, atol=1e-3) and np.isclose(r.max(), 1.07, atol=1e-3)


def _spec(sb, clean=False):
    nb = sb.size
    return M.ShutterSpectrum(rate_base='b', source_id=1, slitlet_id=7, quadrant=2, k=1, n_pix=100,
                             sb=sb.astype(float), err=np.full(nb, 0.01), n=np.full(nb, 5), clean=clean)


def test_select_judges_the_padded_spectrum_and_returns_the_whole_core_one():
    rng = np.random.default_rng(3)
    base = np.linspace(1.0, 2.0, 60)
    pairs = []
    for i in range(12):
        judged = base * (1 + rng.normal(0, 0.005, 60))
        if i == 0:
            judged = judged * 0.4                                     # a dark shutter
        pairs.append((_spec(judged), _spec(base * 1.01)))
    opt = M.options({})
    out = R._select(pairs, opt)
    assert len(out) == 12 and all(o is p[1] for o, p in zip(out, pairs))
    assert out[0].reason == 'low_throughput' and not out[0].clean
    assert all(o.clean for o in out[1:])
    assert np.allclose(out[1].sb, base * 1.01)                       # the sky keeps the whole-core values


def test_read_calls_reads_the_first_product_of_the_root(tmp_path):
    calls = Table(rows=[(2, 195, 64, True, -0.01, 6, ''), (2, 195, 65, False, 1.02, 6, '')],
                  names=('q', 'row', 'col', 'stuck', 'a', 'n', 'note'),
                  dtype=(np.int16, np.int16, np.int16, bool, np.float32, np.int16, 'U48'))
    hdus = fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU(calls, name='CALLS')])
    hdus.writeto(tmp_path / 'jw07076001001_05101_00001_nrs2_mbkgr.fits')
    got = R.read_calls(str(tmp_path), 'jw07076001001_05101')
    assert got[(2, 195, 64)]['stuck'] and abs(got[(2, 195, 64)]['a'] + 0.01) < 1e-6
    assert not got[(2, 195, 65)]['stuck'] and got[(2, 195, 65)]['n'] == 6
    assert R.read_calls(str(tmp_path), 'jw07076001001_03101') == {}


def test_product_current_compares_every_input_card(tmp_path):
    path = str(tmp_path / 'p_mbkgr.fits')
    cards = {'CFMB1VER': ('1.0', ''), 'MBKGDEC': ('abc', '')}
    assert not R.product_current(path, cards)
    h = fits.Header(); h['CFMB1VER'] = '1.0'; h['MBKGDEC'] = 'abc'
    fits.PrimaryHDU(header=h).writeto(path)
    assert R.product_current(path, cards)
    assert not R.product_current(path, dict(cards, MBKGDEC=('xyz', '')))
