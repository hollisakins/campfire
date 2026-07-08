"""Tests for the ``--tiles`` exposure pre-filter (Phase 8).

Covers the ``S_REGION``-based coarse overlap gate that lets ``process`` /
``align`` / ``combine`` reduce a single tile without touching the rest of the
field:

- ``geometry`` primitives: ``polygon_from_sregion``, ``read_sregion_polygon``,
  ``select_overlapping_by_sregion`` (per-file + exposure-union + fail-open),
  ``tiles_union_polygon`` (buffer + unknown-tile error), ``filter_exposures_to_tiles``.
- ``Field.get_exposure_files(tiles=)`` / ``Field.get_uncal_files(tiles=)`` and
  ``build_exposure_groups(tiles=)`` end-to-end on real FITS carrying ``S_REGION``.
- CLI wiring: ``--tiles`` on ``process``/``align`` and the relaxed ``run`` guard.

Everything here is pure (astropy FITS + shapely) — no CRDS / jwst / assign_wcs.
"""

import os

import numpy as np
import pytest
from astropy.io import fits
from shapely.geometry import Point, Polygon

from campfire_pipeline.nircam import geometry as g
from campfire_pipeline.nircam.association import build_exposure_groups, exposure_key
from campfire_pipeline.nircam.field import Field

# Tile A4 (test): a small square 150.0..150.2 / 2.0..2.2 (deg), explicit corners.
_A4_CORNERS = [[150.0, 2.0], [150.2, 2.0], [150.2, 2.2], [150.0, 2.2]]
# An S_REGION footprint fully inside A4, and one far away.
_SR_IN = 'POLYGON ICRS 150.05 2.05 150.06 2.05 150.06 2.06 150.05 2.06'
_SR_OUT = 'POLYGON ICRS 151.00 3.00 151.01 3.00 151.01 3.01 151.00 3.01'

_TOKEN = 'jw01727028001_04101_00003'


def _write_fits(path, s_region=None, on_primary=False):
    """A minimal 2-HDU FITS (Primary + SCI) with an optional S_REGION."""
    prim = fits.PrimaryHDU()
    sci = fits.ImageHDU(data=np.zeros((2, 2), dtype='f4'), name='SCI')
    if s_region is not None:
        (prim.header if on_primary else sci.header)['S_REGION'] = s_region
    fits.HDUList([prim, sci]).writeto(path, overwrite=True)
    return path


def _make_field(tmp_path, filters=('f200w', 'f444w'), tiles=None):
    f = Field(name='cosmos', filters=list(filters), files=['jw01727*'],
              tangent_point=(150.1, 2.1),
              tiles=tiles if tiles is not None else {'A4': {'corners': _A4_CORNERS}})
    f.setup_workspace(campfire_root=str(tmp_path))
    return f


# --- polygon_from_sregion ---------------------------------------------------

def test_polygon_from_sregion_valid():
    poly = g.polygon_from_sregion(_SR_IN)
    assert isinstance(poly, Polygon) and poly.area > 0


@pytest.mark.parametrize('s', ['', None, 'POLYGON ICRS 150.0',
                               'POLYGON ICRS 150.0 2.0 150.1 2.0',  # 2 verts
                               'garbage tokens here x y'])
def test_polygon_from_sregion_bad_returns_none(s):
    assert g.polygon_from_sregion(s) is None


# --- read_sregion_polygon ---------------------------------------------------

def test_read_sregion_polygon_sci_and_primary(tmp_path):
    p_sci = _write_fits(str(tmp_path / 'sci.fits'), _SR_IN)
    p_prim = _write_fits(str(tmp_path / 'prim.fits'), _SR_IN, on_primary=True)
    assert g.read_sregion_polygon(p_sci) is not None
    assert g.read_sregion_polygon(p_prim) is not None


def test_read_sregion_polygon_missing_and_unreadable(tmp_path):
    p_none = _write_fits(str(tmp_path / 'nosr.fits'), s_region=None)
    assert g.read_sregion_polygon(p_none) is None
    # An empty / non-FITS file → OSError → None (fail-open signal).
    bad = tmp_path / 'empty.fits'
    bad.write_text('')
    assert g.read_sregion_polygon(str(bad)) is None


# --- select_overlapping_by_sregion ------------------------------------------

def _polys_map(mapping):
    """Patch read_sregion_polygon to a dict keyed by basename."""
    def _read(path):
        return mapping[os.path.basename(path)]
    return _read


def test_select_overlapping_per_file(monkeypatch):
    tile = Polygon([(150.0, 2.0), (150.2, 2.0), (150.2, 2.2), (150.0, 2.2)])
    inside = g.polygon_from_sregion(_SR_IN)
    outside = g.polygon_from_sregion(_SR_OUT)
    monkeypatch.setattr(g, 'read_sregion_polygon', _polys_map({
        'in.fits': inside, 'out.fits': outside, 'noneknown.fits': None,
    }))
    files = ['in.fits', 'out.fits', 'noneknown.fits']
    # per-file: keep the overlapping one + the fail-open (None) one.
    assert g.select_overlapping_by_sregion(files, tile) == ['in.fits',
                                                            'noneknown.fits']


def test_select_overlapping_exposure_union(monkeypatch):
    tile = Polygon([(150.0, 2.0), (150.2, 2.0), (150.2, 2.2), (150.0, 2.2)])
    inside = g.polygon_from_sregion(_SR_IN)
    outside = g.polygon_from_sregion(_SR_OUT)
    # jw1 has one in-tile detector + one out; jw2 fully out; jw3 fail-open.
    monkeypatch.setattr(g, 'read_sregion_polygon', _polys_map({
        'jw1_nrca1.fits': inside, 'jw1_nrca2.fits': outside,
        'jw2_nrca1.fits': outside, 'jw3_nrca1.fits': None,
    }))
    files = ['jw1_nrca1.fits', 'jw1_nrca2.fits', 'jw2_nrca1.fits',
             'jw3_nrca1.fits']
    sel = g.select_overlapping_by_sregion(files, tile, key_fn=exposure_key)
    # union: both jw1 detectors kept (dither straddles the edge), jw3 fail-open,
    # jw2 dropped.
    assert set(sel) == {'jw1_nrca1.fits', 'jw1_nrca2.fits', 'jw3_nrca1.fits'}


# --- tiles_union_polygon ----------------------------------------------------

def test_tiles_union_polygon_buffer(tmp_path):
    f = _make_field(tmp_path)
    raw = Polygon(f.get_tile_corners('A4'))
    buffered = g.tiles_union_polygon(f, ['A4'], buffer_deg=0.01)
    just_outside = Point(150.0 - 0.005, 2.1)  # 0.005 deg left of the edge
    assert not raw.contains(just_outside)
    assert buffered.contains(just_outside)


def test_tiles_union_polygon_unknown_tile(tmp_path):
    f = _make_field(tmp_path)
    with pytest.raises(ValueError):
        g.tiles_union_polygon(f, ['ZZ'])


def test_tiles_union_polygon_accepts_string(tmp_path):
    f = _make_field(tmp_path)
    assert g.tiles_union_polygon(f, 'A4').area > 0


# --- Field enumerators + build_exposure_groups ------------------------------

def test_get_exposure_files_tiles(tmp_path):
    f = _make_field(tmp_path)
    # Two SW detectors of one dither (one on-tile, one off) + a fully-off dither.
    on = _write_fits(os.path.join(f.filter_dir('f200w'),
                                  f'{_TOKEN}_nrca1.fits'), _SR_IN)
    edge = _write_fits(os.path.join(f.filter_dir('f200w'),
                                    f'{_TOKEN}_nrca2.fits'), _SR_OUT)
    _write_fits(os.path.join(f.filter_dir('f200w'),
                             'jw01727028001_04101_00099_nrca1.fits'), _SR_OUT)

    kept = f.get_exposure_files('f200w', tiles=['A4'])
    # Exposure-union: both detectors of the on-tile dither survive; the
    # fully-off dither is dropped.
    assert set(kept) == {on, edge}
    # No tiles → everything.
    assert len(f.get_exposure_files('f200w')) == 3


def test_get_uncal_files_tiles(tmp_path):
    f = _make_field(tmp_path)
    raw_dir = os.path.join(f.raw_root, '1727', 'f200w')
    os.makedirs(raw_dir, exist_ok=True)
    on = _write_fits(os.path.join(raw_dir, f'{_TOKEN}_nrca1_uncal.fits'), _SR_IN)
    _write_fits(os.path.join(raw_dir,
                             'jw01727028001_04101_00099_nrca1_uncal.fits'),
                _SR_OUT)
    kept = f.get_uncal_files('f200w', tiles=['A4'])
    assert kept == [on]
    assert len(f.get_uncal_files('f200w')) == 2


def test_build_exposure_groups_tiles(tmp_path):
    f = _make_field(tmp_path)
    for det in ('nrca1', 'nrca2', 'nrca3', 'nrca4'):
        _write_fits(os.path.join(f.filter_dir('f200w'),
                                 f'{_TOKEN}_{det}.fits'), _SR_IN)
    _write_fits(os.path.join(f.filter_dir('f444w'),
                             f'{_TOKEN}_nrcalong.fits'), _SR_IN)
    # A second dither entirely off-tile.
    _write_fits(os.path.join(f.filter_dir('f200w'),
                             'jw01727028001_04101_00099_nrca1.fits'), _SR_OUT)

    groups = build_exposure_groups(f, tiles=['A4'])
    assert [gr.key for gr in groups] == [_TOKEN]
    assert groups[0].n_members == 5


# --- CLI wiring -------------------------------------------------------------

def test_cli_process_align_have_tiles():
    from click.testing import CliRunner
    from campfire_pipeline.nircam.cli import main
    runner = CliRunner()
    for cmd in ('process', 'align'):
        out = runner.invoke(main, [cmd, '--help'])
        assert out.exit_code == 0
        assert '--tiles' in out.output


def test_cli_run_tiles_allowed_with_process(monkeypatch):
    from click.testing import CliRunner
    import campfire_pipeline.nircam.cli as cli

    calls = {}
    monkeypatch.setattr(cli, '_setup', lambda c, f: ({}, object()))
    monkeypatch.setattr(cli, '_resolve_filters', lambda flt, fo: ['f200w'])
    monkeypatch.setattr(cli, 'run_process',
                        lambda *a, **k: calls.setdefault('tiles', k.get('tiles')))
    monkeypatch.setattr(cli, 'run_step', lambda *a, **k: None)  # align is a step now
    monkeypatch.setattr(cli, 'run_combine', lambda *a, **k: None)

    out = CliRunner().invoke(
        cli.main, ['run', '--field', 'cosmos', '--process', '--tiles', 'A4'])
    # The old "only combine" guard is gone; --tiles reaches run_process.
    assert out.exit_code == 0, out.output
    assert calls['tiles'] == ['A4']
