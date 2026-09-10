"""UNICORN release photo-z reader, band ceiling, and catalog supersede."""

import numpy as np
import pytest
from astropy.io import fits

from campfire.deploy.photometry import (
    FILTER_WAVELENGTHS,
    SUPERSEDE_MIN_RATIO,
    UnicornPhotozData,
    _supersede_other_catalogs,
    build_photometry_payload,
    catalog_columns_needed,
    coerce_catalog_id,
    load_photoz,
    photoz_input_files,
    read_catalog,
)


# ---------------------------------------------------------------------------
# Synthetic UNICORN release files
# ---------------------------------------------------------------------------

N_OBJ, N_T, N_Z, N_W = 4, 3, 6, 50
Z_GRID = np.linspace(0.0, 10.0, N_Z)
WAVE_REST = np.linspace(1000.0, 50000.0, N_W)  # Å


def _cube() -> np.ndarray:
    """(n_templ, n_z, n_wave) in nJy, distinct per template and z."""
    t = np.arange(N_T)[:, None, None] + 1.0
    z = np.arange(N_Z)[None, :, None] + 1.0
    w = np.linspace(1.0, 2.0, N_W)[None, None, :]
    return t * z * w


def _write_release(tmp_path, single_row_cube=False):
    """Write photz + template files the way the real releases are laid out:
    one template-cube row per template holding a contiguous (n_z, n_w)
    block. *single_row_cube* instead packs the whole cube into one row (a
    layout the reader must refuse rather than read whole)."""
    ids = np.array([101, 205, 333, 404], dtype=np.int32)
    za = np.array([1.5, 6.9, np.nan, 3.2], dtype=np.float32)
    chia = np.array([10.0, 20.0, 30.0, 40.0], dtype=np.float32)
    zl68 = za - 0.1
    zu68 = za + 0.2
    coeffs = np.arange(N_OBJ * N_T, dtype=np.float32).reshape(N_OBJ, N_T) + 1.0
    ext1 = fits.BinTableHDU.from_columns([
        fits.Column(name='ZA', format='E', array=za),
        fits.Column(name='ZM', format='E', array=za),
        fits.Column(name='CHIA', format='E', array=chia),
        fits.Column(name='ZL68', format='E', array=zl68),
        fits.Column(name='ZU68', format='E', array=zu68),
        fits.Column(name='COEFFS', format=f'{N_T}E', array=coeffs),
        fits.Column(name='ID', format='J', array=ids),
    ])
    ext2 = fits.BinTableHDU.from_columns([
        fits.Column(name='F444W', format='E', array=np.ones(N_OBJ, dtype=np.float32)),
    ])
    pz = np.zeros((N_Z, N_OBJ), dtype=np.float32)
    for i in range(N_OBJ):
        pz[:, i] = np.exp(-0.5 * ((Z_GRID - (i + 1)) / 0.5) ** 2) * (i + 1)
    ext3 = fits.BinTableHDU.from_columns([
        fits.Column(name='ZGRID', format='E', array=Z_GRID.astype(np.float32)),
        fits.Column(name='PZ', format=f'{N_OBJ}E', array=pz),
        fits.Column(name='CHI2', format=f'{N_OBJ}D', array=pz.astype(np.float64)),
    ])
    ext4 = fits.BinTableHDU.from_columns([
        fits.Column(name='ZGRID_LOWZ', format='E', array=Z_GRID[:3].astype(np.float32)),
        fits.Column(name='PZ_LOWZ', format=f'{N_OBJ}E', array=pz[:3]),
    ])
    photz = tmp_path / 'test_photz_v0.95.fits'
    fits.HDUList([fits.PrimaryHDU(), ext1, ext2, ext3, ext4]).writeto(photz)

    cube = _cube().astype(np.float32)
    t1 = fits.BinTableHDU.from_columns([
        fits.Column(name='WAVE', format=f'{N_W}D', array=WAVE_REST[None, :]),
    ])
    t2 = fits.BinTableHDU.from_columns([
        fits.Column(name='ZGRID', format=f'{N_Z}D', array=Z_GRID[None, :]),
    ])
    t3 = fits.BinTableHDU.from_columns([
        fits.Column(name='TNAME', format='8A', array=np.array([f'tmpl{i}' for i in range(N_T)])),
    ])
    if single_row_cube:
        t4 = fits.BinTableHDU.from_columns([
            fits.Column(name='FLUX', format=f'{cube.size}E',
                        dim=f'({N_W},{N_Z},{N_T})', array=cube[None]),
        ])
    else:
        t4 = fits.BinTableHDU.from_columns([
            fits.Column(name='FLUX', format=f'{N_Z * N_W}E',
                        dim=f'({N_W},{N_Z})', array=cube),
        ])
    templates = tmp_path / 'unicorn_templates_fiducial.fits'
    fits.HDUList([fits.PrimaryHDU(), t1, t2, t3, t4]).writeto(templates)
    return photz, templates, coeffs, pz


@pytest.fixture
def release(tmp_path):
    return _write_release(tmp_path)


def _reader(photz, templates, **extra):
    return UnicornPhotozData({
        'format': 'unicorn', 'file': str(photz), 'templates': str(templates),
        'label': 'UNICORN test', 'color': '#123456', **extra,
    })


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def test_lookup_returns_bounds_and_skips_nonfinite(release):
    photz, templates, _, _ = release
    r = _reader(photz, templates)
    got = r.lookup(205)
    assert got['z_best'] == pytest.approx(6.9, abs=1e-6)
    assert got['chi2'] == pytest.approx(20.0)
    assert got['z_err_lo'] == pytest.approx(6.8, abs=1e-5)
    assert got['z_err_hi'] == pytest.approx(7.1, abs=1e-5)
    assert r.lookup(333) is None      # ZA is NaN
    assert r.lookup(999) is None      # not in the release
    assert r.lookup('404') is not None  # integral strings are accepted
    assert r.lookup('000404') is not None
    assert r.lookup('GN-404') is None   # non-integral ids: no photo-z, no exception
    assert r.generate_sidecar('GN-404') is None


def test_sidecar_pz_is_the_object_column_normalised_to_peak(release):
    photz, templates, _, pz = release
    r = _reader(photz, templates)
    sc = r.generate_sidecar(404)
    assert sc['label'] == 'UNICORN test' and sc['color'] == '#123456'
    assert sc['z_grid'] == pytest.approx(Z_GRID.tolist(), abs=1e-4)
    expected = pz[:, 3] / pz[:, 3].max()
    assert sc['pz'] == pytest.approx(expected.tolist(), abs=1e-4)
    assert max(sc['pz']) == pytest.approx(1.0)


def test_sidecar_model_sed_matches_coeffs_dot_templates(release):
    photz, templates, coeffs, _ = release
    r = _reader(photz, templates, template_wav_min_um=0.0, template_wav_max_um=100.0)
    sc = r.generate_sidecar(101)          # z=1.5 → nearest grid z=2.0 (index 1)
    iz = int(np.argmin(np.abs(Z_GRID - 1.5)))
    plane = _cube()[:, iz, :]
    expected_ujy = (coeffs[0] @ plane) / 1e3
    assert sc['template_wav'] == pytest.approx((WAVE_REST * 2.5 / 1e4).tolist(), rel=1e-4)
    assert sc['template_flux_ujy'] == pytest.approx(expected_ujy.tolist(), rel=1e-4)


def test_plane_reads_are_cached_by_grid_index(release):
    photz, templates, coeffs, pz = release
    r = _reader(photz, templates, template_wav_min_um=0.0, template_wav_max_um=100.0)
    sc = r.generate_sidecar(404)      # z=3.2 → nearest grid z=4.0 (index 2)
    iz = int(np.argmin(np.abs(Z_GRID - 3.2)))
    expected_ujy = (coeffs[3] @ _cube()[:, iz, :]) / 1e3
    assert sc['template_flux_ujy'] == pytest.approx(expected_ujy.tolist(), rel=1e-4)
    assert sc['pz'] == pytest.approx((pz[:, 3] / pz[:, 3].max()).tolist(), abs=1e-4)
    assert list(r._plane_cache) == [iz]


def test_reader_refuses_a_cube_not_laid_out_one_row_per_template(tmp_path):
    photz, templates, _, _ = _write_release(tmp_path, single_row_cube=True)
    with pytest.raises(ValueError, match='one row per template'):
        _reader(photz, templates)


def test_coerce_catalog_id():
    assert coerce_catalog_id(7) == 7
    assert coerce_catalog_id(np.int32(7)) == 7
    assert coerce_catalog_id(7.0) == 7
    assert coerce_catalog_id(np.float32(7.0)) == 7
    assert coerce_catalog_id('0042') == 42
    assert coerce_catalog_id(' 42 ') == 42
    assert coerce_catalog_id(np.str_('42')) == 42
    for bad in ('GN-42', 'abc', '', 7.5, float('nan'), None, True):
        assert coerce_catalog_id(bad) is None


def test_prefetch_gathers_pz_in_one_pass(release):
    photz, templates, _, pz = release
    r = _reader(photz, templates)
    assert r.prefetch([101, 404, 999, '205']) == 3     # unknown id ignored
    assert set(r._pz_cache) == {0, 1, 3}
    assert r.prefetch([101]) == 0                      # already cached
    assert r._pz_cache[1] == pytest.approx(pz[:, 1].tolist(), rel=1e-6)
    # A sidecar for an id that was not prefetched still works (single pass).
    r2 = _reader(photz, templates)
    sc = r2.generate_sidecar(404)
    assert max(sc['pz']) == pytest.approx(1.0)
    assert set(r2._pz_cache) == {3}


def test_sidecar_scale_and_wavelength_window(release):
    photz, templates, coeffs, _ = release
    r = _reader(photz, templates, template_wav_min_um=1.0, template_wav_max_um=5.0)
    plain = r.generate_sidecar(101)
    scaled = r.generate_sidecar(101, scale=2.0)
    assert all(1.0 <= w <= 5.0 for w in plain['template_wav'])
    assert len(plain['template_wav']) < N_W
    assert scaled['template_flux_ujy'] == pytest.approx(
        [2 * v for v in plain['template_flux_ujy']], rel=1e-4)
    # A NaN / non-positive scale is ignored rather than zeroing the SED.
    assert r.generate_sidecar(101, scale=float('nan'))['template_flux_ujy'] == plain['template_flux_ujy']


def test_sidecar_thins_long_templates(release):
    photz, templates, _, _ = release
    r = _reader(photz, templates, max_template_points=10,
                template_wav_min_um=0.0, template_wav_max_um=100.0)
    sc = r.generate_sidecar(101)
    assert len(sc['template_wav']) <= 10
    assert len(sc['template_wav']) == len(sc['template_flux_ujy'])


def test_reader_without_templates_gives_pz_only(release):
    photz, _, _, _ = release
    r = UnicornPhotozData({'file': str(photz)})
    sc = r.generate_sidecar(101)
    assert 'pz' in sc and 'template_wav' not in sc
    assert r.label == 'UNICORN photo-z'


def test_reader_rejects_missing_columns(release):
    photz, templates, _, _ = release
    with pytest.raises(ValueError, match="lacks column 'Z_BEST'"):
        _reader(photz, templates, z_best_column='Z_BEST')


def test_load_photoz_dispatch_and_inputs(release):
    photz, templates, _, _ = release
    cfg = {'format': 'unicorn', 'file': str(photz), 'templates': str(templates)}
    assert isinstance(load_photoz(cfg), UnicornPhotozData)
    assert photoz_input_files(cfg) == [str(photz), str(templates)]
    assert photoz_input_files({'file': 'x.fits'}) == ['x.fits']
    with pytest.raises(ValueError, match='Unknown photoz format'):
        load_photoz({'format': 'eazy', 'file': str(photz)})


# ---------------------------------------------------------------------------
# Payload: filters and the no-coverage ceiling
# ---------------------------------------------------------------------------

def test_unicorn_bands_have_wavelengths():
    for band in ('f070w', 'f775w', 'f850lp', 'f105w', 'f125w', 'f140w', 'f160w', 'f162m'):
        pivot, lo, hi = FILTER_WAVELENGTHS[band]
        assert lo < pivot < hi


def test_payload_drops_no_coverage_bands():
    row = {
        'FLUX_F444W': 1234.0, 'FLUXERR_F444W': 12.0,        # good
        'FLUX_F090W': 0.0, 'FLUXERR_F090W': 1.0e12,          # UNICORN "no coverage"
        'FLUX_F850L': 5.0, 'FLUXERR_F850L': float('nan'),    # masked
    }
    bands = {
        'f444w': {'flux': 'FLUX_F444W', 'err': 'FLUXERR_F444W'},
        'f090w': {'flux': 'FLUX_F090W', 'err': 'FLUXERR_F090W'},
        'f850lp': {'flux': 'FLUX_F850L', 'err': 'FLUXERR_F850L'},
    }
    payload = build_photometry_payload(row, bands, 'nJy', max_flux_err=1e6)
    assert set(payload['bands']) == {'f444w'}
    assert payload['bands']['f444w']['flux'] == pytest.approx(1.234)
    assert payload['bands']['f444w']['wav'] == FILTER_WAVELENGTHS['f444w'][0]
    # Without a ceiling the sentinel passes through unchanged (legacy behaviour).
    legacy = build_photometry_payload(row, bands, 'nJy')
    assert set(legacy['bands']) == {'f444w', 'f090w'}


# ---------------------------------------------------------------------------
# Catalog read: only the configured columns are materialised
# ---------------------------------------------------------------------------

def test_read_catalog_materialises_only_needed_columns(tmp_path):
    n = 5
    cols = [
        fits.Column(name='ID', format='J', array=np.arange(n)),
        fits.Column(name='RA', format='D', array=np.linspace(10, 11, n)),
        fits.Column(name='DEC', format='D', array=np.linspace(-1, 1, n)),
        fits.Column(name='FLUX_F444W', format='E', array=np.ones(n)),
        fits.Column(name='FLUXERR_F444W', format='E', array=np.ones(n)),
        fits.Column(name='SCALE_MODEL', format='E', array=np.full(n, 1.5)),
    ] + [fits.Column(name=f'JUNK{i}', format='E', array=np.zeros(n)) for i in range(20)]
    path = tmp_path / 'cat.fits'
    fits.BinTableHDU.from_columns(cols).writeto(path)

    cfg = {
        'ra_column': 'RA', 'dec_column': 'DEC', 'id_column': 'ID',
        'bands': {
            'f444w': {'flux': 'FLUX_F444W', 'err': 'FLUXERR_F444W'},
            'f090w': {'flux': 'FLUX_F090W', 'err': 'FLUXERR_F090W'},  # not in file
        },
        'photoz': {'format': 'unicorn', 'scale_column': 'SCALE_MODEL'},
    }
    wanted = catalog_columns_needed(cfg)
    assert wanted == ['RA', 'DEC', 'ID', 'FLUX_F444W', 'FLUXERR_F444W',
                      'FLUX_F090W', 'FLUXERR_F090W', 'SCALE_MODEL']
    t = read_catalog(str(path), 'fits', wanted)
    assert t.colnames == ['RA', 'DEC', 'ID', 'FLUX_F444W', 'FLUXERR_F444W', 'SCALE_MODEL']
    assert len(t) == n
    assert float(t['RA'][2]) == pytest.approx(10.5)
    assert float(t['SCALE_MODEL'][0]) == pytest.approx(1.5)


def test_read_catalog_decodes_like_table_read(tmp_path):
    """Strings, logicals and TSCAL/TZERO columns come back as Table.read gives them."""
    from astropy.table import Table
    n = 4
    cols = [
        fits.Column(name='NAME', format='8A', array=np.array(['abc', 'de', 'fghijklm', ''])),
        fits.Column(name='GOOD', format='L', array=np.array([True, False, True, False])),
        fits.Column(name='RA', format='D', array=np.array([10.0, 10.5, 11.0, 11.5])),
        # Scaled 16-bit integer: physical = raw * 0.01 + 100 (raw values here,
        # TSCAL/TZERO set on the header below).
        fits.Column(name='SCALED', format='I', array=np.array([0, 50, 100, -50], dtype=np.int16)),
        # Unsigned 32-bit convention: raw int32 + TZERO 2**31.
        fits.Column(name='UID', format='J',
                    array=(np.array([1, 2**31, 2**32 - 1, 7], dtype=np.int64) - 2**31).astype(np.int32)),
    ]
    path = tmp_path / 'typed.fits'
    hdu = fits.BinTableHDU.from_columns(cols)
    hdu.header['TSCAL4'] = 0.01
    hdu.header['TZERO4'] = 100.0
    hdu.header['TZERO5'] = 2**31
    hdu.writeto(path)

    ref = Table.read(path)
    got = read_catalog(str(path), 'fits', ['NAME', 'GOOD', 'RA', 'SCALED', 'UID'])
    # Table.read masks the empty string; read_catalog hands it back as ''.
    assert list(got['NAME']) == ['abc', 'de', 'fghijklm', '']
    assert list(ref['NAME'][:3]) == ['abc', 'de', 'fghijklm']
    assert got['NAME'].dtype.kind == 'U'
    assert list(got['GOOD']) == list(ref['GOOD'])
    assert got['GOOD'].dtype == bool
    assert np.allclose(got['RA'], ref['RA'])
    assert np.allclose(got['SCALED'], ref['SCALED'])
    assert np.allclose(got['SCALED'], [100.0, 100.5, 101.0, 99.5])
    assert got['UID'].dtype == ref['UID'].dtype == np.uint32
    assert list(got['UID']) == list(ref['UID']) == [1, 2**31, 2**32 - 1, 7]
    assert str(got['UID'][1]) == '2147483648'   # the catalog_id key text is unchanged
    # A string id survives the deploy's str() unchanged (no b'...' wrapper).
    assert str(got['NAME'][0]) == 'abc'


def test_read_catalog_uint64_and_signed_byte_conventions(tmp_path):
    from astropy.table import Table
    big = np.array([1, 2**63, 2**64 - 1, 2**63 + 5], dtype=np.uint64)
    cols = [
        fits.Column(name='K', format='K',
                    array=(big.astype(np.int64) if False else (big - np.uint64(2**63)).view(np.int64))),
        fits.Column(name='B', format='B', array=np.array([0, 127, 128, 255], dtype=np.uint8)),
    ]
    path = tmp_path / 'wide.fits'
    hdu = fits.BinTableHDU.from_columns(cols)
    hdu.header['TZERO1'] = 2**63
    hdu.header['TZERO2'] = -128
    hdu.writeto(path)
    ref = Table.read(path)
    got = read_catalog(str(path), 'fits', ['K', 'B'])
    assert got['K'].dtype == ref['K'].dtype == np.uint64
    assert list(got['K']) == list(ref['K']) == list(big)   # low bits intact
    # astropy applies TZERO=-128 as a plain scale (float64); so do we.
    assert got['B'].dtype == ref['B'].dtype == np.float64
    assert list(got['B']) == list(ref['B']) == [-128.0, -1.0, 0.0, 127.0]


def test_read_catalog_falls_back_for_gzip_and_non_binary_tables(tmp_path, capsys):
    import gzip
    from astropy.table import Table
    n = 3
    tbl = fits.BinTableHDU.from_columns([
        fits.Column(name='ID', format='J', array=np.arange(n)),
        fits.Column(name='RA', format='D', array=np.array([1.0, 2.0, 3.0])),
    ])
    plain = tmp_path / 'cat.fits'
    tbl.writeto(plain)
    gz = tmp_path / 'cat.fits.gz'
    with open(plain, 'rb') as src, gzip.open(gz, 'wb') as dst:
        dst.write(src.read())
    got = read_catalog(str(gz), 'fits', ['ID', 'RA'])
    assert list(got['RA']) == [1.0, 2.0, 3.0]
    assert 'gzipped' in capsys.readouterr().out

    # Image in HDU 1, table in HDU 2: the first binary table is used.
    multi = tmp_path / 'multi.fits'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((2, 2))), tbl]).writeto(multi)
    got = read_catalog(str(multi), 'fits', ['ID', 'RA'])
    assert list(got['ID']) == [0, 1, 2]
    assert 'using HDU 2' in capsys.readouterr().out

    # ASCII table: no streaming path, Table.read handles it.
    ascii_path = tmp_path / 'ascii.fits'
    fits.TableHDU.from_columns([
        fits.Column(name='ID', format='I5', array=np.arange(n)),
        fits.Column(name='RA', format='E12.4', array=np.array([1.5, 2.5, 3.5])),
    ]).writeto(ascii_path)
    got = read_catalog(str(ascii_path), 'fits', ['ID', 'RA'])
    assert np.allclose(got['RA'], Table.read(ascii_path)['RA'])
    assert got['RA'].dtype.kind == 'f'


def test_read_catalog_rejects_unsupported_formats(tmp_path):
    cols = [
        fits.Column(name='ID', format='J', array=np.arange(3)),
        fits.Column(name='BITS', format='8X', array=np.zeros((3, 8), dtype=bool)),
    ]
    path = tmp_path / 'bits.fits'
    fits.BinTableHDU.from_columns(cols).writeto(path)
    assert list(read_catalog(str(path), 'fits', ['ID'])['ID']) == [0, 1, 2]
    with pytest.raises(ValueError, match="'BITS'"):
        read_catalog(str(path), 'fits', ['ID', 'BITS'])


# ---------------------------------------------------------------------------
# Supersede
# ---------------------------------------------------------------------------

class _FakeQuery:
    def __init__(self, table):
        self._t = table
        self._filters = {}
        self._range = None
        self._delete_ids = None

    def select(self, *_):
        return self

    def eq(self, col, val):
        self._filters[col] = ('eq', val); return self

    def neq(self, col, val):
        self._filters[col] = ('neq', val); return self

    def order(self, *_):
        return self

    def range(self, lo, hi):
        self._range = (lo, hi); return self

    def delete(self):
        return self

    def in_(self, col, ids):
        assert col == 'id'
        self._delete_ids = list(ids); return self

    def execute(self):
        if self._delete_ids is not None:
            self._t.deleted.extend(self._delete_ids)
            self._t.rows = [r for r in self._t.rows if r['id'] not in self._delete_ids]
            return type('R', (), {'data': None})()
        rows = [r for r in self._t.rows if all(
            (r[c] == v) if op == 'eq' else (r[c] != v)
            for c, (op, v) in self._filters.items())]
        rows.sort(key=lambda r: r['id'])
        lo, hi = self._range
        return type('R', (), {'data': rows[lo:hi + 1]})()


class _FakeTable:
    def __init__(self, rows):
        self.rows = rows
        self.deleted = []


class _FakeClient:
    def __init__(self, rows):
        self.t = _FakeTable(rows)

    def table(self, name):
        assert name == 'object_photometry'
        return _FakeQuery(self.t)


def test_supersede_deletes_only_other_catalogs_in_field():
    rows = (
        [{'id': i, 'field': 'egs', 'catalog_name': 'UNICORN EGS v0.9'} for i in range(1, 1201)]
        + [{'id': 5000 + i, 'field': 'egs', 'catalog_name': 'UNICORN EGS v0.98'} for i in range(3)]
        + [{'id': 9000, 'field': 'egs', 'catalog_name': 'other'}]
        + [{'id': 9500, 'field': 'goods-s', 'catalog_name': 'UNICORN GOODS-S v0.91'}]
    )
    client = _FakeClient(rows)
    deleted = _supersede_other_catalogs(client, 'egs', 'UNICORN EGS v0.98')
    assert deleted == {'UNICORN EGS v0.9': 1200, 'other': 1}
    remaining = {(r['field'], r['catalog_name']) for r in client.t.rows}
    assert remaining == {('egs', 'UNICORN EGS v0.98'), ('goods-s', 'UNICORN GOODS-S v0.91')}
    assert len(client.t.deleted) == 1201


def test_supersede_dry_run_counts_without_deleting():
    rows = (
        [{'id': i, 'field': 'egs', 'catalog_name': 'UNICORN EGS v0.9'} for i in range(1, 11)]
        + [{'id': 50, 'field': 'egs', 'catalog_name': 'UNICORN EGS v0.98'}]
    )
    client = _FakeClient(rows)
    assert _supersede_other_catalogs(client, 'egs', 'UNICORN EGS v0.98', dry_run=True) == {
        'UNICORN EGS v0.9': 10}
    assert client.t.deleted == [] and len(client.t.rows) == 11


def _supersede_setup(tmp_path, monkeypatch, catalog_ra, n_old):
    """A one-source catalog at *catalog_ra* and one object at RA 214.9, with
    *n_old* rows of a previous catalog in the fake client."""
    import campfire.deploy.photometry as mod

    cat = tmp_path / 'cat.fits'
    fits.BinTableHDU.from_columns([
        fits.Column(name='ID', format='J', array=np.array([1])),
        fits.Column(name='RA', format='D', array=np.array([catalog_ra])),
        fits.Column(name='DEC', format='D', array=np.array([52.9])),
        fits.Column(name='FLUX_F444W', format='E', array=np.array([1.0])),
        fits.Column(name='FLUXERR_F444W', format='E', array=np.array([0.1])),
    ]).writeto(cat)
    cfg = tmp_path / 'photometry.toml'
    cfg.write_text(f"""
[egs]
catalog = "{cat}"
catalog_name = "UNICORN EGS v0.98"
ra_column = "RA"
dec_column = "DEC"
id_column = "ID"
[egs.bands]
f444w = {{ flux = "FLUX_F444W", err = "FLUXERR_F444W" }}
""")
    old_rows = [{'id': i, 'field': 'egs', 'catalog_name': 'UNICORN EGS v0.9'}
                for i in range(1, n_old + 1)]
    client = _FakeClient(old_rows)
    monkeypatch.setattr(mod, '_fetch_field_objects', lambda *_: [
        {'id': 1, 'object_id': 'egs_1', 'ra': 214.9, 'dec': 52.9}])
    monkeypatch.setattr(mod, '_upsert_photometry', lambda *_: 0)
    client.rpc = lambda *_a, **_k: type('R', (), {'execute': lambda self: type('D', (), {'data': 0})()})()
    return mod, client, cfg


def test_supersede_is_skipped_when_nothing_was_upserted(tmp_path, monkeypatch, capsys):
    """A cross-match that finds nothing must not wipe the field's other catalogs."""
    mod, client, cfg = _supersede_setup(tmp_path, monkeypatch, catalog_ra=200.0, n_old=3)
    result = mod.deploy_field_photometry(client, 'egs', cfg, {}, include_photoz=False, supersede=True)
    assert result['n_matched'] == 0 and result['n_superseded'] == 0
    assert len(client.t.rows) == 3 and client.t.deleted == []
    assert 'skipping --supersede' in capsys.readouterr().out


def test_supersede_refuses_a_collapse_unless_forced(tmp_path, monkeypatch, capsys):
    """One new match against ten old rows is a misconfiguration, not a release."""
    mod, client, cfg = _supersede_setup(tmp_path, monkeypatch, catalog_ra=214.9, n_old=10)
    assert SUPERSEDE_MIN_RATIO * 10 > 1
    result = mod.deploy_field_photometry(client, 'egs', cfg, {}, include_photoz=False, supersede=True)
    assert result['n_matched'] == 1 and result['n_superseded'] == 0
    assert len(client.t.rows) == 10 and client.t.deleted == []
    assert 'Pass --force' in capsys.readouterr().out

    result = mod.deploy_field_photometry(client, 'egs', cfg, {}, include_photoz=False,
                                         supersede=True, supersede_force=True)
    assert result['n_superseded'] == 10
    assert client.t.rows == [] and len(client.t.deleted) == 10


def test_supersede_runs_when_the_new_catalog_covers_the_old(tmp_path, monkeypatch):
    mod, client, cfg = _supersede_setup(tmp_path, monkeypatch, catalog_ra=214.9, n_old=1)
    result = mod.deploy_field_photometry(client, 'egs', cfg, {}, include_photoz=False, supersede=True)
    assert result['n_matched'] == 1 and result['n_superseded'] == 1
    assert client.t.rows == []


def test_dry_run_reports_supersede_ratio(tmp_path, monkeypatch, capsys):
    mod, client, cfg = _supersede_setup(tmp_path, monkeypatch, catalog_ra=214.9, n_old=10)
    result = mod.deploy_field_photometry(client, 'egs', cfg, {}, include_photoz=False,
                                         supersede=True, dry_run=True)
    out = capsys.readouterr().out
    assert result['n_superseded'] == 10 and client.t.deleted == []
    assert 'Would retire 10 rows' in out and 'refused without --force' in out
