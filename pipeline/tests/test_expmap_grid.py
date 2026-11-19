"""The exposure-map grid is the field's.

Every exposure map of a field at one scale is on the same pixel lattice (a
north-up TAN projection with the field's tangent point at a pixel center), and
a run's window encloses the exposures of every filter of the field found on
the machine. So maps of different filters, from different runs, are
registered, and those built from the same exposures on disk are the same
array shape. Pure numpy / astropy.
"""

import json

import matplotlib

matplotlib.use('Agg')

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nircam import expmap as em


def _square(ra0, dec0, size=0.01):
    return ([ra0, ra0 + size, ra0 + size, ra0], [dec0, dec0, dec0 + size, dec0 + size])


def _meta(name, ra0, dec0, t=100.0):
    ra, dec = _square(ra0, dec0)
    return em._ExposureMeta(path=f'{name}.fits', rootname=name, xposure=t, ra=ra, dec=dec)


def test_field_grid_is_one_lattice_whatever_it_encloses():
    """Two windows of the same field and scale are the same pixels where they
    overlap: integer CRPIX, the tangent point at a pixel center, and the same
    sky position at pixel coordinates that differ by the CRPIX difference."""
    tp = (150.02, 2.01)
    a, shape_a = em._field_wcs([_meta('a', 150.00, 2.00)], 0.5, 30.0, tp)
    b, shape_b = em._field_wcs([_meta('a', 150.00, 2.00), _meta('b', 150.20, 2.15)],
                               0.5, 30.0, tp)
    assert shape_b[0] > shape_a[0] and shape_b[1] > shape_a[1]
    for w in (a, b):
        assert list(w.wcs.crval) == [150.02, 2.01]
        assert all(float(v).is_integer() for v in w.wcs.crpix)
        # the tangent point is the center of a pixel
        x, y = w.all_world2pix(tp[0], tp[1], 0)
        assert abs(float(x) - round(float(x))) < 1e-6
        assert abs(float(y) - round(float(y))) < 1e-6
    xa, ya = a.all_world2pix(150.005, 2.004, 0)
    xb, yb = b.all_world2pix(150.005, 2.004, 0)
    shift = np.array(b.wcs.crpix) - np.array(a.wcs.crpix)
    assert float(xb - xa) == pytest.approx(shift[0], abs=1e-6)
    assert float(yb - ya) == pytest.approx(shift[1], abs=1e-6)
    # a map built on the small window is the matching block of one built on
    # the large window
    small = em._accumulate_expmap([_meta('a', 150.00, 2.00)], a, shape_a)
    large = em._accumulate_expmap([_meta('a', 150.00, 2.00)], b, shape_b)
    i, j = int(round(shift[1])), int(round(shift[0]))
    assert np.array_equal(large[i:i + shape_a[0], j:j + shape_a[1]], small)


def test_field_grid_encloses_every_polygon_with_padding():
    m = _meta('a', 150.00, 2.00)
    w, shape = em._field_wcs([m], 0.5, 30.0, (150.0, 2.0))
    x, y = w.all_world2pix(m.ra, m.dec, 0)
    assert x.min() >= 59 and y.min() >= 59            # 30" = 60 pixels
    assert x.max() <= shape[1] - 60 and y.max() <= shape[0] - 60


def test_field_grid_refuses_a_footprint_off_the_tangent_plane():
    with pytest.raises(ValueError, match='does not project'):
        em._field_wcs([_meta('a', 330.0, -2.0)], 0.5, 30.0, (150.0, 2.0))


class _GridField:
    """A field whose exposures are given directly."""

    def __init__(self, name, per_filter, tangent_point=(150.02, 2.01)):
        self.name = name
        self.per_filter = per_filter
        self.filters = list(per_filter)
        self.tangent_point = tangent_point
        self.tiles = {}


def _stub_collect(monkeypatch):
    monkeypatch.setattr(em, '_collect_metas',
                        lambda field, filt, stage, cache=None: list(field.per_filter[filt]))
    monkeypatch.setattr(em, '_load_metadata_cache', lambda *a, **k: {})
    monkeypatch.setattr(em, '_save_metadata_cache', lambda *a, **k: None)


def _grid_of(path):
    with fits.open(path) as hdul:
        h = hdul['EXPMAP'].header
        return (h['CRVAL1'], h['CRVAL2'], h['CRPIX1'], h['CRPIX2'],
                h['NAXIS1'], h['NAXIS2'])


def test_a_run_of_one_filter_lands_on_the_grid_of_the_full_run(tmp_path, monkeypatch):
    _stub_collect(monkeypatch)
    field = _GridField('f', {'f444w': [_meta('n1', 150.00, 2.00)],
                             'f200w': [_meta('n2', 150.03, 2.02)]})
    em.run_expmap(field, out_dir=str(tmp_path))
    full = _grid_of(tmp_path / 'f444w' / 'expmap_f_f444w.fits')
    em.run_expmap(field, filters=['f444w'], out_dir=str(tmp_path), overwrite=True)
    assert _grid_of(tmp_path / 'f444w' / 'expmap_f_f444w.fits') == full
    assert _grid_of(tmp_path / 'f200w' / 'expmap_f_f200w.fits') == full
    # so the two stack directly, and the layout summary records the grid
    total = (fits.getdata(tmp_path / 'f444w' / 'expmap_f_f444w.fits')
             + fits.getdata(tmp_path / 'f200w' / 'expmap_f_f200w.fits'))
    assert set(np.unique(total)) <= {0.0, 100.0, 200.0}
    grid = json.loads((tmp_path / 'f_layout.json').read_text())['grid']
    assert grid['crval'] == [full[0], full[1]]
    assert grid['crpix'] == [full[2], full[3]]
    assert grid['naxis'] == [full[4], full[5]]


def test_a_map_on_disk_is_rebuilt_when_its_grid_or_its_exposures_changed(
        tmp_path, monkeypatch, capsys):
    _stub_collect(monkeypatch)
    field = _GridField('f', {'f444w': [_meta('n1', 150.00, 2.00)]})
    path = tmp_path / 'f444w' / 'expmap_f_f444w.fits'

    em.run_expmap(field, out_dir=str(tmp_path))
    first = _grid_of(path)
    capsys.readouterr()
    em.run_expmap(field, out_dir=str(tmp_path))                 # nothing changed
    assert 'FITS up to date' in capsys.readouterr().out

    # an exposure is added inside the same window: same grid, new content
    field.per_filter['f444w'].append(_meta('n3', 150.00, 2.00, t=50.0))
    em.run_expmap(field, out_dir=str(tmp_path))
    assert 'holds 1 exposure(s), 2 are selected now' in capsys.readouterr().out
    assert _grid_of(path) == first and float(fits.getdata(path).max()) == 150.0

    # another filter's exposures arrive and widen the window
    field.per_filter['f200w'] = [_meta('n4', 150.10, 1.95)]
    field.filters = list(field.per_filter)
    em.run_expmap(field, out_dir=str(tmp_path))
    assert 'it is on another grid' in capsys.readouterr().out
    wider = _grid_of(path)
    assert wider[4] > first[4] and wider[:2] == first[:2]


def test_the_uncal_quick_look_keeps_a_grid_centered_on_its_run(tmp_path, monkeypatch):
    _stub_collect(monkeypatch)
    field = _GridField('f', {'f444w': [_meta('n1', 150.00, 2.00)]})
    em.run_expmap(field, stage='uncal', out_dir=str(tmp_path))
    grid = _grid_of(tmp_path / 'f444w' / 'expmap_f_f444w_uncal.fits')
    assert grid[:2] != (150.02, 2.01)
