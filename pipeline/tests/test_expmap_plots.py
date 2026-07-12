"""Tests for expmap plot outputs: the dark web PNG, the field layout plot,
per-filter ``AREA`` header, and the coverage/area JSON summary.

Pure numpy / matplotlib / astropy — no CRDS/jwst. Exercises the new plot and
survey-area code paths on synthetic exposure maps built from a couple of
overlapping S_REGION polygons, plus a ``Field`` loaded from an in-memory
``fields.toml`` for the tile-outline geometry.
"""

import json
import textwrap

import matplotlib

matplotlib.use('Agg')

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs.utils import proj_plane_pixel_area

from campfire_pipeline.nircam import expmap as em
from campfire_pipeline.nircam.field import Field


def _synthetic_expmap(pixel_scale=0.5, padding=15.0):
    """Real TAN WCS + expmap array from two overlapping polygons."""
    metas = [
        em._ExposureMeta(path='a.fits', rootname='a', xposure=1000.0,
                         ra=[150.00, 150.02, 150.02, 150.00],
                         dec=[2.00, 2.00, 2.02, 2.02]),
        em._ExposureMeta(path='b.fits', rootname='b', xposure=500.0,
                         ra=[150.01, 150.03, 150.03, 150.01],
                         dec=[2.005, 2.005, 2.025, 2.025]),
    ]
    wcs, shape = em._auto_wcs(metas, pixel_scale, padding)
    arr = em._accumulate_expmap(metas, wcs, shape)
    return metas, wcs, shape, arr


def test_expmap_paths_canonical_and_uncal():
    fits_p, pdf_p, png_p = em._expmap_paths('/base', 'cosmos', 'f444w',
                                            'canonical')
    # per-filter products live in the filter subdir <base>/<filter>/
    assert fits_p.endswith('/f444w/expmap_cosmos_f444w.fits')
    assert pdf_p.endswith('/f444w/expmap_cosmos_f444w.pdf')
    assert png_p.endswith('/f444w/expmap_cosmos_f444w.png')
    # uncal quick-look keeps the stage suffix so it never collides
    f2, _p2, png2 = em._expmap_paths('/base', 'cosmos', 'f444w', 'uncal')
    assert f2.endswith('expmap_cosmos_f444w_uncal.fits')
    assert png2.endswith('expmap_cosmos_f444w_uncal.png')


def test_layout_paths_canonical_and_uncal():
    png, js = em._layout_paths('/base', 'cosmos', 'canonical')
    assert png.endswith('/cosmos_layout.png')
    assert js.endswith('/cosmos_layout.json')
    png_u, _js_u = em._layout_paths('/base', 'cosmos', 'uncal')
    assert png_u.endswith('/cosmos_layout_uncal.png')


def test_write_fits_area_header(tmp_path):
    metas, wcs, _shape, arr = _synthetic_expmap()
    out = tmp_path / 'expmap.fits'
    em._write_fits(str(out), arr, wcs, field_name='cosmos',
                   filter_name='f444w', stage='canonical', metas=metas)
    with fits.open(out) as hdul:
        hdr = hdul['EXPMAP'].header
    assert hdr['BUNIT'] == 's'
    expect = int(np.count_nonzero(arr > 0)) * proj_plane_pixel_area(wcs) * 3600
    assert hdr['AREA'] == pytest.approx(expect, rel=1e-6)
    assert hdr['AREA'] > 0


@pytest.mark.parametrize('dark', [False, True])
def test_render_figure(tmp_path, dark):
    _metas, wcs, _shape, arr = _synthetic_expmap()
    out = tmp_path / ('dark.png' if dark else 'light.pdf')
    ok = em._render_expmap_figure(str(out), arr, wcs, title='t',
                                  cbar_label='Exposure time [s]', dark=dark)
    assert ok is True
    assert out.exists() and out.stat().st_size > 0


def test_render_figure_no_title(tmp_path):
    # Deployable PNGs (per-filter + layout) pass title=None — the web page
    # embedding them already labels the plot.
    _metas, wcs, _shape, arr = _synthetic_expmap()
    out = tmp_path / 'untitled.png'
    ok = em._render_expmap_figure(str(out), arr, wcs, title=None,
                                  cbar_label='Exposure time [s]', dark=True)
    assert ok is True
    assert out.exists() and out.stat().st_size > 0


def test_render_figure_square_inticks_and_zorder(tmp_path, monkeypatch):
    # Change set: the main panel is forced square (predictable web layout),
    # its ticks point inward, and the imshow sits behind the axes so the grid,
    # ticks, and tile outlines render on top of the map. Capture the figure
    # before it is closed and introspect it.
    import warnings

    import matplotlib.pyplot as plt

    captured = {}
    real_close = plt.close
    monkeypatch.setattr(plt, 'close', lambda fig: captured.setdefault('fig', fig))

    _metas, wcs, _shape, arr = _synthetic_expmap()
    out = tmp_path / 'sq.png'
    ok = em._render_expmap_figure(str(out), arr, wcs, title=None,
                                  cbar_label='Exposure time [s]', dark=True)
    assert ok is True

    fig = captured['fig']
    ax = fig.axes[0]                       # main panel; colorbar is a later axes
    assert ax.get_box_aspect() == 1.0      # forced square
    images = ax.get_images()
    assert images and all(im.get_zorder() < 0 for im in images)  # behind axes
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')    # .ticks accessor is deprecated
        # get_tick_out() is False for direction='in'; both RA and Dec.
        assert ax.coords[0].ticks.get_tick_out() is False
        assert ax.coords[1].ticks.get_tick_out() is False
    real_close(fig)


def test_render_filter_plots_forward_tiles(tmp_path, monkeypatch):
    # Per-filter maps now carry the tile outlines too (both the light PDF and
    # the dark PNG), matching the layout plot.
    metas, wcs, _shape, arr = _synthetic_expmap()
    fdir = tmp_path / 'f444w'
    fdir.mkdir()
    fp = fdir / 'expmap_cosmos_f444w.fits'
    em._write_fits(str(fp), arr, wcs, field_name='cosmos',
                   filter_name='f444w', stage='canonical', metas=metas)

    calls = []

    def _fake_render(path, data, wcs_, *, title, cbar_label, vmin=None,
                     vmax=None, dark=False, tile_outlines=None):
        calls.append((dark, tile_outlines))
        return True

    monkeypatch.setattr(em, '_render_expmap_figure', _fake_render)
    tiles = [('A1', [[150.0, 2.0], [150.01, 2.0],
                     [150.01, 2.01], [150.0, 2.01]])]
    em._render_filter_plots('cosmos', 'f444w', 'canonical', metas, str(fp),
                            str(tmp_path), wcs, 1.0, 10.0, tile_outlines=tiles)

    # both renders (light PDF dark=False, dark PNG dark=True) get the tiles
    assert len(calls) == 2
    assert sorted(dark for dark, _ in calls) == [False, True]
    assert all(passed == tiles for _, passed in calls)


def test_run_expmap_passes_tiles_to_filter_plots(tmp_path, monkeypatch):
    # End-to-end: the field's tile (A1) flows through _collect_tile_outlines
    # into every per-filter render, even though this stubs out the heavy work.
    metas, wcs, shape, _arr = _synthetic_expmap()

    ff = tmp_path / 'fields.toml'
    ff.write_text(textwrap.dedent(_FIELDS_TOML))    # cosmos has tile A1
    field = Field.load('cosmos', fields_file=str(ff))

    monkeypatch.setattr(em, '_load_metadata_cache', lambda *a, **k: {})
    monkeypatch.setattr(em, '_save_metadata_cache', lambda *a, **k: None)
    monkeypatch.setattr(em, '_collect_metas',
                        lambda field, filt, stage, cache=None: [metas[0]])
    monkeypatch.setattr(em, '_auto_wcs', lambda ms, ps, pad: (wcs, shape))
    monkeypatch.setattr(em, '_accumulate_filter',
                        lambda w: (w[1], w[5], f'/fake/{w[1]}.fits', 1.0, 10.0))
    monkeypatch.setattr(em, '_render_layout', lambda *a, **k: None)
    monkeypatch.setattr(em, '_write_region_file', lambda *a, **k: None)

    captured = []
    monkeypatch.setattr(em, '_render_filter_plots',
                        lambda *a, **k: captured.append(k.get('tile_outlines')))

    em.run_expmap(field, filters=['f200w', 'f444w'], out_dir=str(tmp_path))

    assert captured                                 # per-filter plots rendered
    for tiles in captured:
        assert [name for name, _ in tiles] == ['A1']


def test_render_figure_all_zero_writes_nothing(tmp_path):
    _metas, wcs, shape, _arr = _synthetic_expmap()
    out = tmp_path / 'zero.png'
    ok = em._render_expmap_figure(str(out), np.zeros(shape, np.float32), wcs,
                                  title='t', cbar_label='c', dark=True)
    assert ok is False
    assert not out.exists()


_FIELDS_TOML = """
    [cosmos]
    filters = ["f200w", "f444w"]
    files = ["jw01727*"]
    tangent_point = [150.02, 2.01]

    [cosmos.A1]
    "30mas" = { crpix = [500, 500], naxis = [1000, 1000] }
"""


def test_render_layout_stack_and_area(tmp_path):
    metas, wcs, _shape, arr = _synthetic_expmap()
    # two per-filter FITS on the shared WCS (as run_expmap writes them)
    results = []
    for filt in ('f200w', 'f444w'):
        fdir = tmp_path / filt
        fdir.mkdir()
        fp = fdir / f'expmap_cosmos_{filt}.fits'
        em._write_fits(str(fp), arr, wcs, field_name='cosmos',
                       filter_name=filt, stage='canonical', metas=metas)
        results.append((filt, metas, str(fp), None, None))

    ff = tmp_path / 'fields.toml'
    ff.write_text(textwrap.dedent(_FIELDS_TOML))
    field = Field.load('cosmos', fields_file=str(ff))
    tile_outlines = [(t, field.get_tile_corners(t)) for t in field.tiles]
    assert len(tile_outlines) == 1  # A1

    em._render_layout(str(tmp_path), field, 'canonical', results, wcs,
                      0.5, tile_outlines, texp_total=3000.0)

    png = tmp_path / 'cosmos_layout.png'
    js = tmp_path / 'cosmos_layout.json'
    assert png.exists() and png.stat().st_size > 0

    summary = json.loads(js.read_text())
    assert summary['field'] == 'cosmos'
    assert summary['stage'] == 'canonical'
    assert summary['n_filters'] == 2
    assert sorted(summary['filters']) == ['f200w', 'f444w']
    assert summary['n_tiles'] == 1
    assert summary['texp_total_s'] == 3000.0
    # Footprint (non-zero area) of the summed stack == one filter's footprint,
    # since both filters share the same polygons.
    single = int(np.count_nonzero(arr > 0))
    expect = single * proj_plane_pixel_area(wcs) * 3600
    assert summary['coverage_area_arcmin2'] == pytest.approx(expect, rel=1e-6)


def test_render_layout_backfills_area_when_header_missing(tmp_path):
    # A cached FITS from before the AREA card existed must not report a
    # spurious zero per-filter area — it's recomputed from the array.
    metas, wcs, _shape, arr = _synthetic_expmap()
    results = []
    for filt in ('f200w', 'f444w'):
        fdir = tmp_path / filt
        fdir.mkdir()
        fp = fdir / f'expmap_cosmos_{filt}.fits'
        em._write_fits(str(fp), arr, wcs, field_name='cosmos',
                       filter_name=filt, stage='canonical', metas=metas)
        with fits.open(fp, mode='update') as hdul:
            del hdul['EXPMAP'].header['AREA']       # simulate a pre-AREA FITS
        results.append((filt, metas, str(fp), None, None))

    ff = tmp_path / 'fields.toml'
    ff.write_text(textwrap.dedent(_FIELDS_TOML))
    field = Field.load('cosmos', fields_file=str(ff))
    tile_outlines = [(t, field.get_tile_corners(t)) for t in field.tiles]

    em._render_layout(str(tmp_path), field, 'canonical', results, wcs,
                      0.5, tile_outlines, texp_total=3000.0)

    summary = json.loads((tmp_path / 'cosmos_layout.json').read_text())
    single = int(np.count_nonzero(arr > 0))
    expect = single * proj_plane_pixel_area(wcs) * 3600
    for filt in ('f200w', 'f444w'):
        area = summary['per_filter_area_arcmin2'][filt]
        assert area > 0
        assert area == pytest.approx(expect, rel=1e-6)


def test_run_expmap_skips_layout_on_partial_filter_run(tmp_path, monkeypatch):
    # A filtered rerun must not overwrite the full-field layout/coverage; a
    # full run (covering every field filter) still writes it.
    metas, wcs, shape, _arr = _synthetic_expmap()

    ff = tmp_path / 'fields.toml'
    ff.write_text(textwrap.dedent(_FIELDS_TOML))    # cosmos: [f200w, f444w]
    field = Field.load('cosmos', fields_file=str(ff))

    monkeypatch.setattr(em, '_load_metadata_cache', lambda *a, **k: {})
    monkeypatch.setattr(em, '_save_metadata_cache', lambda *a, **k: None)
    monkeypatch.setattr(em, '_collect_metas',
                        lambda field, filt, stage, cache=None: [metas[0]])
    monkeypatch.setattr(em, '_auto_wcs', lambda ms, ps, pad: (wcs, shape))
    monkeypatch.setattr(em, '_accumulate_filter',
                        lambda w: (w[1], w[5], f'/fake/{w[1]}.fits', 1.0, 10.0))
    monkeypatch.setattr(em, '_render_filter_plots', lambda *a, **k: None)
    monkeypatch.setattr(em, '_write_region_file', lambda *a, **k: None)

    calls = []
    monkeypatch.setattr(em, '_render_layout',
                        lambda *a, **k: calls.append(a))

    em.run_expmap(field, filters=['f444w'], out_dir=str(tmp_path))
    assert calls == []                              # partial → skipped

    em.run_expmap(field, filters=['f200w', 'f444w'], out_dir=str(tmp_path))
    assert len(calls) == 1                          # full → rendered


def test_render_layout_no_built_results_noop(tmp_path):
    _metas, wcs, _shape, _arr = _synthetic_expmap()
    # results with empty metas / no FITS path → nothing written
    em._render_layout(str(tmp_path), _DummyField(), 'canonical',
                      [('f444w', [], None, None, None)], wcs, 0.5, [], 0.0)
    assert not list(tmp_path.glob('*_layout.*'))


class _DummyField:
    name = 'cosmos'
