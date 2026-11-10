"""Master-background step 1: the compact rate-file geometry (``coverage.RateGeometry``) and
the step's own pieces (the grid rows, the judged/sky split of the selection, the CALLS
reader), on fake slit transforms and synthetic spectra."""
import os

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
    a = C.cache_path('/x/jw_rate.fits', '/c', (2.6, 5.7), None)
    g = C.geometry_cache_path('/x/jw_rate.fits', '/c', (2.6, 5.7), None)
    assert g != a and g.endswith('.npz') and '/geometry_jw_rate_' in g and '_cut' in g


def test_step1_builds_g140m_geometry_on_the_extended_range_with_the_extension_off(tmp_path, monkeypatch):
    """A G140M/F100LP rate file of an observation reduced without the extension: step 1
    hands its geometry workers the extended wavelengthrange (and records it in the
    product's cards), so the trace maps and counters carry the neighbours' red tails."""
    from types import SimpleNamespace
    from campfire_pipeline.nirspec import extended_wavelength as E
    ws = tmp_path / 'products' / 'nirspec' / 'obs'
    ws.mkdir(parents=True)
    rates = []
    for det, grating, filt in (('NRS2', 'G140M', 'F100LP'), ('NRS2', 'G395M', 'F290LP')):
        p = ws / f'jw01_0310{len(rates) + 1}_00001_{det.lower()}_rate.fits'
        hdu = fits.PrimaryHDU()
        for k, v in dict(EXP_TYPE='NRS_MSASPEC', MSAMETFL='jw01_msa.fits', DETECTOR=det, GRATING=grating, FILTER=filt).items():
            hdu.header[k] = v
        hdu.writeto(p)
        rates.append(str(p))
    ext_wr = str(tmp_path / 'extended_jwst_nirspec_wavelengthrange_0008.asdf')
    monkeypatch.setattr(E, 'geometry_wavelengthrange',
                        lambda rate: ext_wr if fits.getheader(rate)['GRATING'] in ('G140M', 'G235M') else None)
    monkeypatch.setattr(R.S, 'root_metafiles', lambda obs, roots=None: {})
    monkeypatch.setattr(R.S, 'load', lambda path, metafiles: {})
    monkeypatch.setattr(M, '_barshadow_reference', lambda grating, rate: (None, '/t/bs.fits'))
    monkeypatch.setattr(M, 'barshadow_stamp', lambda path: 'x')
    seen = {}

    class Stop(Exception):
        pass

    def fake_geometry(args):
        rate, cache_dir, cutoffs, wr = args
        seen[os.path.basename(rate)] = wr
        raise Stop()
    monkeypatch.setattr(R, '_worker_geometry', fake_geometry)
    obs = SimpleNamespace(name='obs', rate_files=rates, workspace_dir=str(ws),
                          stuck_closed_shutters_file=str(ws / 'stuck.toml'), stuck_closed_shutters={})
    try:
        R.run_step1(obs, {'extend_g140m_g235m': False}, write_calls=False)
    except Stop:
        pass
    assert seen.get(os.path.basename(rates[0])) == ext_wr
    cards = R._inputs_cards(rates[0], '/t/bs.fits', (0.96, None), 'h', ext_wr)
    assert cards['MBKGWR'][0] == os.path.basename(ext_wr)
    assert R._inputs_cards(rates[1], '/t/bs.fits', (2.6, 5.7), 'h', None)['MBKGWR'][0] == 'CRDS'


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


def test_read_calls_reads_the_first_rate_file_of_the_root_that_holds_any(tmp_path):
    calls = Table(rows=[(2, 195, 64, True, -0.01, 6, ''), (2, 195, 65, False, 1.02, 6, '')],
                  names=('q', 'row', 'col', 'stuck', 'a', 'n', 'note'),
                  dtype=(np.int16, np.int16, np.int16, bool, np.float32, np.int16, 'U48'))
    fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU(name='CALLS')]).writeto(
        tmp_path / 'jw07076001001_05101_00001_nrs1_rate.fits')                  # empty: no calls yet
    fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU(calls, name='CALLS')]).writeto(
        tmp_path / 'jw07076001001_05101_00001_nrs2_rate.fits')
    got = R.read_calls(str(tmp_path), 'jw07076001001_05101')
    assert got[(2, 195, 64)]['stuck'] and abs(got[(2, 195, 64)]['a'] + 0.01) < 1e-6
    assert not got[(2, 195, 65)]['stuck'] and got[(2, 195, 65)]['n'] == 6
    assert R.read_calls(str(tmp_path), 'jw07076001001_03101') == {}


def test_a_current_root_still_merges_its_stored_calls_into_the_list(tmp_path, monkeypatch):
    """Step 1 skips a root whose products are current, but its stored CALLS still reach
    the stuck list when the calls are wanted (after a run with them off, say)."""
    from types import SimpleNamespace
    from campfire_pipeline.nirspec import stuck_list as S
    calls = Table(rows=[(2, 195, 64, True, -0.01, 6, ''), (2, 195, 65, False, 1.02, 6, '')],
                  names=('q', 'row', 'col', 'stuck', 'a', 'n', 'note'),
                  dtype=(np.int16, np.int16, np.int16, bool, np.float32, np.int16, 'U48'))
    hdu = fits.PrimaryHDU()
    for k, v in dict(EXP_TYPE='NRS_MSASPEC', MSAMETFL='jw_msa.fits', DETECTOR='NRS1', GRATING='PRISM',
                     FILTER='CLEAR').items():
        hdu.header[k] = v
    rate = tmp_path / 'jw07076001001_05101_00001_nrs1_rate.fits'
    fits.HDUList([hdu, fits.BinTableHDU(calls, name='CALLS')]).writeto(rate)
    monkeypatch.setattr(R, 'resolve_wavelengthrange', lambda rate: None)
    monkeypatch.setattr(R.S, 'root_metafiles', lambda obs, roots=None: {})
    monkeypatch.setattr(M, '_barshadow_reference', lambda grating, rate: (None, '/t/bs.fits'))
    monkeypatch.setattr(R, '_inputs_cards', lambda *a, **k: {})
    monkeypatch.setattr(R, 'product_current', lambda path, cards: True)
    stuck = str(tmp_path / 'stuck.toml')
    obs = SimpleNamespace(name='obs', rate_files=[str(rate)], workspace_dir=str(tmp_path),
                          stuck_closed_shutters_file=stuck, stuck_closed_shutters={})
    assert R.run_step1(obs, {}, write_calls=False) == [] and not os.path.exists(stuck)
    assert R.run_step1(obs, {}, write_calls=True) == []
    phys = S.read_file(stuck)[0]['jw07076001001_05101']
    assert phys == {(2, 195, 64): S.Entry('auto', True, -0.01)}


def test_product_current_reads_the_cards_of_the_rate_files_mbkg1dr(tmp_path):
    path = str(tmp_path / 'x_rate.fits')
    cards = {'CFMB1VER': ('1.0', ''), 'MBKGDEC': ('abc', '')}
    assert not R.product_current(path, cards)
    fits.PrimaryHDU().writeto(path)
    assert not R.product_current(path, cards)                            # a rate file step 1 has not written into
    t = fits.BinTableHDU(Table({'wavelength': [1.0]}), name='MBKG1DR')
    t.header['CFMB1VER'] = '1.0'; t.header['MBKGDEC'] = 'abc'
    R.write_into_rate(path, [t])
    assert R.product_current(path, cards)
    assert not R.product_current(path, dict(cards, MBKGDEC=('xyz', '')))


def _step1_hdus(value, trace_map=False):
    """The step's extensions: tables, and the trace map (a real compressed one, or empty)."""
    from campfire_pipeline.nirspec import trace_overlap as TO
    t = fits.BinTableHDU(Table({'wavelength': [1.0, 2.0], 'rate': [value, value]}), name='MBKG1DR')
    t.header['MBKGDATE'] = str(value)
    hdus = [t, fits.BinTableHDU(Table({'quadrant': [1], 'scale': [1.0]}), name='MBKGQSR'),
            fits.BinTableHDU(Table({'k': [0]}), name='MBKGSHR'), fits.BinTableHDU(name='SHUTAMP'),
            fits.BinTableHDU(name='CALLS')]
    if not trace_map:
        return hdus + TO.ShutterMap.empty_hdus()
    sh = np.full((2, 8, 8), -1, np.int16); sh[0, 2:5] = 0
    sm = TO.ShutterMap(slits=Table({'name': ['7'], 'source_id': [1007], 'kind': ['slitlet'], 'lit': [True]}),
                       shutters=Table({'slit': [0], 'j': [0], 'q': [2], 'row': [100], 'col': [10], 'source_id': [1007],
                                       'background': ['N'], 'primary': ['Y'], 'is_source': [True],
                                       'holds_source': [True], 'sources': ['1007']}),
                       sh1=sh, sh2=np.full_like(sh, -1), flag=np.zeros(sh.shape, np.uint8), meta={'SHMAPVER': '1'})
    return hdus + sm.hdus()


def test_write_into_rate_survives_a_jwst_save_and_a_rewrite(tmp_path):
    """The step-1 tables in a rate file: a jwst save records them in its ASDF tree, a
    rewrite with the same names keeps that tree valid, and the mask path's
    _drop_extra_fits removes them cleanly."""
    from jwst.datamodels import ImageModel
    from campfire_pipeline.nirspec.masks import _drop_extra_fits
    path = str(tmp_path / 'jw_rate.fits')
    m = ImageModel(np.ones((8, 8), np.float32)); m.save(path); m.close()
    R.write_into_rate(path, _step1_hdus(1.0, trace_map=True))
    with ImageModel(path) as m:                      # what stage 1 / masks do to a rate file
        m.save(path)
    from campfire_pipeline.nirspec import trace_overlap as TO
    assert TO.trace_map_from_rate(path).count.sum() == 24                # the compressed map survives the save
    R.write_into_rate(path, _step1_hdus(2.0))        # step 1 again, after the save (an empty map this time)
    with ImageModel(path) as m:
        assert np.all(m.data == 1)
    with fits.open(path) as h:
        assert [x.name for x in h].count('MBKG1DR') == 1
        assert all(n in h for n in R.STEP1_EXTENSIONS)
        assert h['MBKG1DR'].data['rate'][0] == 2.0 and h['CALLS'].header['NAXIS2'] == 0
    assert R.step1_header(path)['MBKGDATE'] == '2.0' and TO.trace_map_from_rate(path) is None
    sky, q = R.read_rate_sky(path)
    assert np.all(sky['rate'] == 2.0) and q == {1: 1.0}
    with ImageModel(path) as m:
        _drop_extra_fits(m, R.STEP1_EXTENSIONS)
        m.save(path)
    with ImageModel(path) as m:
        assert np.all(m.data == 1)
    assert R.step1_header(path) is None


def test_a_failed_rate_file_keeps_no_earlier_tables(tmp_path):
    """A rate file step 1 could not process this run has its earlier tables emptied, not
    left for step 2 to paint with: an empty MBKG1DR reads as no background, every
    extension stays present, and without the step's cards the file is stale."""
    from jwst.datamodels import ImageModel
    path = str(tmp_path / 'jw_rate.fits')
    m = ImageModel(np.ones((8, 8), np.float32)); m.save(path); m.close()
    hdus = _step1_hdus(1.0)
    hdus[0].header['CFMB1VER'] = R.STEP1_VERSION
    R.write_into_rate(path, hdus)
    R._clear_tables(path)
    assert R.read_rate_sky(path) == (None, {})
    with fits.open(path) as h:
        assert all(n in h for n in R.STEP1_EXTENSIONS)
    assert not R.product_current(path, {'CFMB1VER': (R.STEP1_VERSION, '')})
    with ImageModel(path) as m:
        assert np.all(m.data == 1)


def test_fixed_slit_exposures_get_no_master_background(tmp_path):
    """A standalone fixed-slit rate file (no MSA, no metafile) is skipped by both steps:
    there are no empty shutters to build a master background from."""
    from types import SimpleNamespace
    assert R.has_master_background({'EXP_TYPE': 'NRS_MSASPEC', 'MSAMETFL': 'jw_01_msa.fits'})
    assert not R.has_master_background({'EXP_TYPE': 'NRS_MSASPEC'})
    assert not R.has_master_background({'EXP_TYPE': 'NRS_FIXEDSLIT'})
    ws = tmp_path / 'products' / 'nirspec' / 'fs_obs'
    ws.mkdir(parents=True)
    rate = ws / 'jw01_03101_00001_nrs1_rate.fits'
    hdu = fits.PrimaryHDU()
    hdu.header['EXP_TYPE'] = 'NRS_FIXEDSLIT'
    fits.HDUList([hdu, fits.ImageHDU(np.zeros((4, 4), np.float32), name='SCI')]).writeto(rate)
    obs = SimpleNamespace(name='fs_obs', rate_files=[str(rate)], workspace_dir=str(ws), bkgslits_dir=None,
                          stuck_closed_shutters_mtime=0.0)
    assert M.run_master_bkg(obs, {}) == []
    assert not list(ws.glob('*_mbkg.fits'))


def test_the_sky_leaves_out_the_stuck_shutters_of_the_merged_list():
    from campfire_pipeline.nirspec.stuck_list import Entry
    entries = {(1, 1, 1): Entry('manual', stuck=False),    # vetoed by hand: open whatever the fit says
               (1, 1, 2): Entry('auto'),                   # an earlier run's call the fit now reverses
               (1, 1, 3): Entry('auto'),                   # an earlier call this run did not measure
               (1, 1, 4): Entry('web')}                    # marked from the web
    calls = {(1, 1, 1): {'stuck': True}, (1, 1, 2): {'stuck': False}, (1, 1, 5): {'stuck': True}}
    assert R.merged_stuck(entries, calls) == {(1, 1, 3), (1, 1, 4), (1, 1, 5)}


def test_the_skip_key_follows_the_rate_files_poisson_variance(tmp_path):
    """Step 1 reruns when the rate file's Poisson variance is rewritten (``MBKGVARP`` = its
    ``CFVARPS``, nirspec/variance.py), which leaves ``CFBKGDT`` as it was."""
    rate = str(tmp_path / 'jw00001001001_03101_00001_nrs1_rate.fits')
    h = fits.Header()
    h['CFBKGDT'] = '2026-11-10T00:00:00'
    fits.PrimaryHDU(header=h).writeto(rate)
    before = R._inputs_cards(rate, '/t/bs.fits', (0.96, None), 'h')
    assert before['MBKGVARP'][0] == 'N/A'
    with fits.open(rate, mode='update') as hd:
        hd[0].header['CFVARPS'] = 'nbr-mean-v1'
    after = R._inputs_cards(rate, '/t/bs.fits', (0.96, None), 'h')
    assert after['MBKGVARP'][0] == 'nbr-mean-v1' and after['MBKGBKDT'] == before['MBKGBKDT']
