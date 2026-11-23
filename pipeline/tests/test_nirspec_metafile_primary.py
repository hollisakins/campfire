"""Which per-source shutter rows hold the source (stage 2a, metafile.py).

1. ``filter_by_source_id`` makes a row primary at intra-shutter x == 0.0 (the
   shutter's edge), never at NaN or a masked x.
2. After stuck-shutter removal, a source with no primary row left at any dither
   is nodata (``AllShuttersStuck``) even when its slitlet keeps other shutters;
   a source open at one dither is still extracted.

Synthetic tables only; no JWST stack.
"""

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import MaskedColumn, Table

from campfire_pipeline.nirspec import nodata
from campfire_pipeline.nirspec.metafile import (
    MetaFile, _source_in_shutter, has_primary_source, remove_stuck_shutters,
)

MSAMETID = 1
SLIT = 99
PRIMARY = 6979      # the slitlet's planned primary
NEIGHBOUR = 20040588  # in shutter column 76 only, at dither 2 only


def _row(col, dither, sid, x, y=0.5, background='N'):
    return dict(slitlet_id=SLIT, msa_metadata_id=MSAMETID, shutter_quadrant=3,
                shutter_row=100, shutter_column=col, source_id=sid,
                background=background, shutter_state='OPEN',
                estimated_source_in_shutter_x=x, estimated_source_in_shutter_y=y,
                dither_point_index=dither, primary_source='N', fixed_slit='NONE')


def _metafile(rows, sources):
    shutters = Table(rows=rows)
    for name in ('estimated_source_in_shutter_x', 'estimated_source_in_shutter_y'):
        shutters[name] = shutters[name].astype(np.float32)
    source_table = Table(rows=[dict(source_id=s, ra=150.0, dec=2.0, stellarity=0.0)
                               for s in sources])
    hdul = fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(name='IMAGE'),
                         fits.BinTableHDU(shutters.as_array(), name='SHUTTER_INFO'),
                         fits.BinTableHDU(source_table.as_array(), name='SOURCE_INFO')])
    return MetaFile(hdul=hdul, filename='jw00001001001_01_msa.fits', msametid=MSAMETID,
                    shutter_table=shutters, source_table=source_table)


def _three_shutter_slitlet():
    """Three-shutter slitlet nodded over 3 dithers, zenith_p3 slitlet 99 style.

    The primary sits in the central shutter of each nod (columns 74 / 75 / 76
    at dithers 1 / 2 / 3); the neighbour falls in column 76 at dither 2 only.
    Column 76 is shutter 3 in the stuck list's 1-indexed convention.
    """
    rows = []
    for dither, centre in ((1, 74), (2, 75), (3, 76)):
        for col in (74, 75, 76):
            if col == centre:
                rows.append(_row(col, dither, PRIMARY, 0.5))
            elif dither == 2 and col == 76:
                rows.append(_row(col, dither, NEIGHBOUR, 0.3))
            else:
                rows.append(_row(col, dither, 0, np.nan, np.nan, background='Y'))
    return _metafile(rows, [PRIMARY, NEIGHBOUR])


def _stage2a_extracts(mf, source_id, stuck):
    """Stage 2a's decision: filter, drop stuck columns, extract only with a primary row."""
    st = mf.filter_by_source_id(source_id).shutter_table
    if stuck:
        st = remove_stuck_shutters(st, stuck)
    return has_primary_source(st), st


# --- Bug 2: x == 0.0 is a position ---------------------------------------------

def test_edge_x_zero_is_primary():
    mf = _metafile([_row(139, 1, 0, np.nan, np.nan, 'Y'),
                    _row(139, 3, 148172, 0.0, 0.744),
                    _row(140, 3, 0, np.nan, np.nan, 'Y')], [148172])
    st = mf.filter_by_source_id(148172).shutter_table
    primary = st[st['primary_source'] == 'Y']
    assert len(primary) == 1
    assert primary['dither_point_index'][0] == 3
    assert primary['source_id'][0] == 148172
    assert primary['estimated_source_in_shutter_x'][0] == 0.0
    assert primary['background'][0] == 'N'
    assert has_primary_source(st)


def test_x_one_is_primary_and_nan_is_not():
    mf = _metafile([_row(10, 1, 7, 1.0), _row(10, 2, 7, np.nan),
                    _row(11, 1, 0, np.nan, np.nan, 'Y')], [7])
    st = mf.filter_by_source_id(7).shutter_table
    assert list(st['primary_source']) == ['Y', 'N', 'N']
    # a non-primary row is background with no source and no position
    assert st['source_id'][1] == 0 and st['background'][1] == 'Y'


def test_masked_and_nan_x_never_primary():
    t = Table({'estimated_source_in_shutter_x': MaskedColumn(
        [0.0, 0.5, np.nan, 0.2, -0.1, np.inf],
        mask=[False, False, False, True, False, False])})
    assert list(_source_in_shutter(t)) == [True, True, False, False, False, False]
    assert _source_in_shutter(t).dtype == bool
    assert not isinstance(_source_in_shutter(t), np.ma.MaskedArray)


def test_source_with_only_nan_x_has_no_primary():
    mf = _metafile([_row(10, 1, 7, np.nan), _row(11, 1, 0, np.nan, np.nan, 'Y')], [7])
    assert not has_primary_source(mf.filter_by_source_id(7).shutter_table)


# --- Bug 1: the source's own shutters all stuck -----------------------------------

def test_source_whose_only_column_is_stuck_is_not_extracted():
    mf = _three_shutter_slitlet()
    extract, st = _stage2a_extracts(mf, NEIGHBOUR, [3])
    # the slitlet keeps shutters (columns 74 / 75), but none of them is the source's
    assert len(st) == 6
    assert set(st['shutter_column']) == {74, 75}
    assert not extract


def test_source_with_one_open_dither_is_extracted():
    # the neighbour's only shutter (column 76, dither 2) is open: stuck is column 74
    extract, st = _stage2a_extracts(_three_shutter_slitlet(), NEIGHBOUR, [1])
    assert extract
    primary = st[st['primary_source'] == 'Y']
    assert list(primary['dither_point_index']) == [2]
    assert list(primary['shutter_column']) == [76]


def test_primary_keeps_extracting_with_one_column_stuck():
    extract, st = _stage2a_extracts(_three_shutter_slitlet(), PRIMARY, [3])
    assert extract
    assert sorted(st['dither_point_index'][st['primary_source'] == 'Y']) == [1, 2]


def test_every_shutter_stuck_is_not_extracted():
    extract, st = _stage2a_extracts(_three_shutter_slitlet(), PRIMARY, [1, 2, 3])
    assert len(st) == 0
    assert not extract


def test_remove_stuck_shutters_indexes_sorted_columns():
    st = _three_shutter_slitlet().shutter_table
    assert set(remove_stuck_shutters(st, [1, 3])['shutter_column']) == {75}
    assert set(remove_stuck_shutters(st, [3, 1])['shutter_column']) == {75}


def test_no_primary_takes_the_all_stuck_nodata_path(tmp_path):
    """The marker stage 2a writes for it is the all-stuck one, retiring a stale canonical."""
    prod = 'jw07417003001_03101_00002_nrs1_20040588'
    (tmp_path / f'{prod}.fits').write_bytes(b'')
    extract, _ = _stage2a_extracts(_three_shutter_slitlet(), NEIGHBOUR, [3])
    assert not extract
    removed = nodata.mark_all_shutters_stuck(prod, directory=str(tmp_path))
    assert [p.split('/')[-1] for p in removed] == [f'{prod}.fits']
    assert (tmp_path / f'{prod}_nodata').read_text() == f'{nodata.REASON_ALL_SHUTTERS_STUCK}\n'


@pytest.mark.parametrize('value, expected', [('Y', True), ('N', False), (' Y ', True)])
def test_has_primary_source_reads_the_flag(value, expected):
    assert has_primary_source(Table({'primary_source': [value, 'N']})) is expected
