"""The exposure-map grid is the field's.

Every exposure map of a field at one scale is on the same pixel lattice (a
north-up TAN projection with the field's tangent point at a pixel center), and
a run's window encloses the exposures of every filter of every imaging
instrument of the field found on the machine. So maps of different filters and
instruments, from different runs, are registered, and those built from the
same exposures on disk are the same array shape. Pure numpy / astropy.
"""

import json
import textwrap

import matplotlib

matplotlib.use('Agg')

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.imaging import expmap as em
from campfire_pipeline.nircam.field import Field


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
    two_chips = em._ExposureMeta(
        path='j.fits', rootname='j', xposure=400.0,
        ra=[150.000, 150.010, 150.010, 150.000], dec=[2.00, 2.00, 2.02, 2.02],
        more=[([150.012, 150.022, 150.022, 150.012], [2.00, 2.00, 2.02, 2.02])])
    w, shape = em._field_wcs([two_chips], 0.5, 30.0, (150.0, 2.0))
    for ra, dec in two_chips.polygons:
        x, y = w.all_world2pix(ra, dec, 0)
        assert x.min() >= 59 and y.min() >= 59            # 30" = 60 pixels
        assert x.max() <= shape[1] - 60 and y.max() <= shape[0] - 60


def test_field_grid_refuses_a_footprint_off_the_tangent_plane():
    with pytest.raises(ValueError, match='does not project'):
        em._field_wcs([_meta('a', 330.0, -2.0)], 0.5, 30.0, (150.0, 2.0))


class _GridField:
    """A field of one instrument whose exposures are given directly."""

    def __init__(self, name, instrument, per_filter, tangent_point=(150.02, 2.01)):
        self.name = name
        self.per_filter = per_filter
        self.filters = list(per_filter)
        self.tangent_point = tangent_point
        self.tiles = {}
        if instrument != 'nircam':
            self.instrument = instrument


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


def test_every_instrument_and_filter_of_a_field_shares_the_grid(tmp_path, monkeypatch):
    """NIRCam and ACS/WFC maps of one field, built by separate runs, are the
    same array on the same pixels: each run's window encloses the other
    instrument's exposures too."""
    _stub_collect(monkeypatch)
    nircam = _GridField('f', 'nircam', {'f444w': [_meta('n1', 150.00, 2.00)],
                                        'f200w': [_meta('n2', 150.03, 2.02)]})
    acs = _GridField('f', 'acswfc', {'f814w': [_meta('j1', 150.10, 1.95, t=500.0)]})
    monkeypatch.setattr(em, '_sibling_fields',
                        lambda field: [acs] if field is nircam else [nircam])

    em.run_expmap(nircam, out_dir=str(tmp_path / 'nircam'))
    em.run_expmap(acs, out_dir=str(tmp_path / 'acswfc'))
    files = [tmp_path / 'nircam' / 'f444w' / 'expmap_f_f444w.fits',
             tmp_path / 'nircam' / 'f200w' / 'expmap_f_f200w.fits',
             tmp_path / 'acswfc' / 'f814w' / 'expmap_f_f814w.fits']
    assert len({_grid_of(p) for p in files}) == 1
    # so they stack directly
    total = fits.getdata(files[0]) + fits.getdata(files[2])
    assert set(np.unique(total)) == {0.0, 100.0, 500.0}
    # and the layout summaries record the grid
    a = json.loads((tmp_path / 'nircam' / 'f_layout.json').read_text())
    b = json.loads((tmp_path / 'acswfc' / 'f_layout.json').read_text())
    assert a['grid'] == b['grid']
    assert a['instrument'] == 'nircam' and b['instrument'] == 'acswfc'


def test_a_run_of_one_filter_lands_on_the_grid_of_the_full_run(tmp_path, monkeypatch):
    _stub_collect(monkeypatch)
    field = _GridField('f', 'nircam', {'f444w': [_meta('n1', 150.00, 2.00)],
                                       'f200w': [_meta('n2', 150.03, 2.02)]})
    monkeypatch.setattr(em, '_sibling_fields', lambda field: [])
    em.run_expmap(field, out_dir=str(tmp_path))
    full = _grid_of(tmp_path / 'f444w' / 'expmap_f_f444w.fits')
    em.run_expmap(field, filters=['f444w'], out_dir=str(tmp_path), overwrite=True)
    assert _grid_of(tmp_path / 'f444w' / 'expmap_f_f444w.fits') == full
    assert _grid_of(tmp_path / 'f200w' / 'expmap_f_f200w.fits') == full


def test_a_map_on_disk_is_rebuilt_when_its_grid_or_its_exposures_changed(
        tmp_path, monkeypatch, capsys):
    _stub_collect(monkeypatch)
    field = _GridField('f', 'nircam', {'f444w': [_meta('n1', 150.00, 2.00)]})
    monkeypatch.setattr(em, '_sibling_fields', lambda field: [])
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

    # another instrument's exposures arrive and widen the window
    acs = _GridField('f', 'acswfc', {'f814w': [_meta('j1', 150.10, 1.95)]})
    monkeypatch.setattr(em, '_sibling_fields', lambda field: [acs])
    em.run_expmap(field, out_dir=str(tmp_path))
    assert 'it is on another grid' in capsys.readouterr().out
    wider = _grid_of(path)
    assert wider[4] > first[4] and wider[:2] == first[:2]


def test_an_instrument_declared_but_absent_is_said_so(tmp_path, monkeypatch, capsys):
    _stub_collect(monkeypatch)
    field = _GridField('f', 'nircam', {'f444w': [_meta('n1', 150.00, 2.00)]})
    absent = _GridField('f', 'wfc3ir', {'f160w': []})
    monkeypatch.setattr(em, '_sibling_fields', lambda field: [absent])
    em.run_expmap(field, out_dir=str(tmp_path))
    out = capsys.readouterr().out
    assert 'wfc3ir 0' in out and 'no calibrated exposures on this machine' in out


_FIELDS_TOML = """
    [cosmos]
    filters = ["f200w", "f444w"]
    files = ["jw01727*"]
    tangent_point = [150.02, 2.01]

    [cosmos.A1]
    "30mas" = { crpix = [500, 500], naxis = [1000, 1000] }

    [cosmos.acswfc]
    filters = ["f814w"]
    files = ["j8pu*"]

    [hstonly]
    tangent_point = [34.4, -5.2]

    [hstonly.wfc3ir]
    filters = ["f160w"]
    files = ["iczg*"]
"""


def test_siblings_are_the_fields_other_instruments_and_create_nothing(tmp_path):
    """From a real fields.toml: a NIRCam field with an ACS/WFC table sees the
    ACS/WFC part (and the reverse); nothing is created for the sibling."""
    from campfire_pipeline.hst.field import HstField

    ff = tmp_path / 'config' / 'fields.toml'
    ff.parent.mkdir()
    ff.write_text(textwrap.dedent(_FIELDS_TOML))
    root = tmp_path
    nircam = Field.load('cosmos', fields_file=str(ff))
    assert em._sibling_fields(nircam) == []                    # no workspace yet
    nircam.setup_workspace(str(root))
    (sib,) = em._sibling_fields(nircam)
    assert sib.instrument == 'acswfc' and sib.filters == ['f814w']
    assert not (root / 'products' / 'acswfc').exists()

    acs = HstField.load('cosmos', 'acswfc', fields_file=str(ff))
    acs.setup_workspace(str(root))
    (back,) = em._sibling_fields(acs)
    assert not hasattr(back, 'instrument') and back.filters == ['f200w', 'f444w']
    assert tuple(acs.tangent_point) == tuple(nircam.tangent_point)

    only = HstField.load('hstonly', 'wfc3ir', fields_file=str(ff))
    only.setup_workspace(str(root))
    assert em._sibling_fields(only) == []                      # no NIRCam, no ACS/WFC
