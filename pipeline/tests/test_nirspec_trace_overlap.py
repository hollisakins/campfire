"""Tests for the trace maps of nirspec/trace_overlap.py, on synthetic traces.

Two slitlets on a small detector: the target's trace and a contaminant's trace
share detector rows over half of the columns. Between nods both sources move one
shutter along their slitlets, so their source shutters move together on the
detector, as in a real nod pattern. The map stored in the rate file (``ShutterMap``)
is tested on a synthetic geometry below; on real data it is checked against p1.
"""

import numpy as np

from campfire_pipeline.nirspec import trace_overlap as TO
from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH

NY, NX = 40, 100
DY = 0.23                      # slit-frame units per detector row


def _trace(rec, row_src, row_lo, row_hi, col_lo, col_hi, centres):
    """Pixels of a slitlet whose source shutter centre sits on detector row ``row_src``."""
    y, x = np.mgrid[row_lo:row_hi, col_lo:col_hi]
    ys = (y - row_src) * DY
    lam = 1.0 + 0.01 * x + (0.5 if rec['source_id'] == 2 else 0.0)
    return rec, np.asarray(centres, float), ((y * NX + x).ravel(), ys.ravel(), lam.ravel())


def _rec(name, sid, state='1x1', kind='slitlet'):
    return dict(name=name, source_id=sid, kind=kind, shutter_state=state, ymin=-2.2, ymax=2.2,
                quadrant=1, xcen=1, ycen=1, src_ypos=0.0, centres='')


def _maps():
    """Three nods; the source shutter of both slitlets moves 5 rows per nod (one pitch)."""
    centres = [-MSA_SLIT_PITCH, 0.0, MSA_SLIT_PITCH]
    maps = []
    for shift in (0, 5, -5):
        t = _trace(_rec('1', 1), 20 + shift, 6, 34, 0, NX, centres)
        c = _trace(_rec('2', 2), 12 + shift, 0, 26, 50, NX, centres)     # one shutter-pitch-plus below the target
        maps.append(TO.TraceMap.from_traces([t, c], (NY, NX)))
    return maps


def test_map_counts_and_shared_table():
    m = _maps()[0]
    assert m.count.max() == 2
    shared = m.count == 2
    # a shared pixel carries both traces, one per layer
    assert m.index.shape[0] == 2
    assert set(np.unique(m.index[:, shared])) == {0, 1}
    assert np.all(m.index[1][~shared] == TO.NONE)
    # only the contaminant's columns are shared
    cols = np.nonzero(shared.any(axis=0))[0]
    assert cols.min() >= 50


def test_traces_in_returns_every_trace_on_a_region():
    m = _maps()[0]
    iy, ix, idx, flag = m.traces_in(60, 10, 20, 10)       # region inside the overlap
    both = set(zip(iy[idx == 0], ix[idx == 0])) & set(zip(iy[idx == 1], ix[idx == 1]))
    assert len(both) > 0
    assert iy.min() >= 0 and ix.min() >= 0 and iy.max() < 10 and ix.max() < 20


def test_nod_contamination_signs_and_shutters():
    maps = _maps()
    recs = TO.nod_contamination(maps, 1, (0, 0, NX, NY))
    assert recs and all(r.source_id == 2 for r in recs)
    for r in recs:
        assert r.sign == (1 if r.exposure == r.from_exposure else -1)
        assert r.ix.min() >= 50                                # only where the traces overlap
        # only the contaminant's source-shutter pixels count (its other shutters carry sky, which cancels)
        assert r.iy.size <= 2 * (TO.OPEN_HALF + TO.LIGHT_PAD) / DY * (NX - 50) + NX
    own = [r for r in recs if r.sign > 0]
    assert len(own) == 3
    # its own-nod light sits 8 rows below the target's source shutter: outside the target's shutter
    assert all(not r.in_target_shutter().any() for r in own)
    # a subtracted partner's light lands on the target's source shutter in some nod
    assert any(r.in_target_shutter().any() for r in recs if r.sign < 0)


def test_past_range_pixels_are_the_contaminants_own():
    """A contaminant's trace continued past its nominal range (columns 80+) is part of
    its trace like any other: the geometry attributes those pixels to it (its pass 1 is
    extended there), with nothing set aside."""
    from campfire_pipeline.nirspec import profile_contam as CT
    maps = _maps()
    recs = TO.nod_contamination(maps, 1, (0, 0, NX, NY))
    assert any((r.ix >= 80).any() for r in recs)

    class E:                                    # the attributes geometry() reads
        def __init__(self, k):
            self.name = f'jw00000001001_03101_0000{k + 1}_nrs1_1.fits'
            self.group, self.y = 0, np.zeros((NY, NX))
            self.origin = (1, 1)
            self.partners = [j for j in range(3) if j != k]
    exps = [E(k) for k in range(3)]
    mp = {CT.exposure_key(e.name): m for e, m in zip(exps, maps)}
    g = CT.geometry(exps, 1, mp)
    for k in range(3):
        assert g.sources[k][2][:, 80:].any()
        assert np.array_equal(g.sources[k][2], g.mask[k])
        assert g.lit_from[k][2] <= {0, 1, 2} and g.lit_from[k][2]


def _maps_out_of_slit():
    """As ``_maps``, but in the third nod the target's source sits outside its slitlet
    (a short slitlet in a 3-point nod): the slitlet holds only sky there."""
    centres = [-MSA_SLIT_PITCH, 0.0, MSA_SLIT_PITCH]
    maps = []
    for i, shift in enumerate((0, 5, -5)):
        rec = _rec('33', 1)
        rec['lit'] = i < 2
        t = _trace(rec, 20 + shift, 6, 34, 0, NX, centres)
        c = _trace(_rec('2', 2), 12 + shift, 0, 26, 50, NX, centres)
        maps.append(TO.TraceMap.from_traces([t, c], (NY, NX)))
    return maps


def test_slitlet_without_its_source_keeps_its_id_and_lights_nothing():
    """ember_cosmos_p14 slitlet 33 (source 15944): at the third dither point jwst calls
    it background-only. The map keeps the source's id there, so the source never meets
    its own slitlet as another source, and marks no source shutter, so no other source
    sees light from it in that nod (its sky cancels)."""
    maps = _maps_out_of_slit()
    assert maps[2].rows_of(1).size == 1                                # still the target's slitlet
    assert not TO.TraceMap.source_shutter(maps[2].flag[maps[2].index == 0]).any()
    assert TO.TraceMap.source_shutter(maps[0].flag[maps[0].index == 0]).any()
    recs = TO.nod_contamination(maps, 1, (0, 0, NX, NY))
    assert recs and all(r.source_id == 2 for r in recs)
    # seen from the other source: the target's slitlet adds light from nods 1 and 2 only
    recs = TO.nod_contamination(maps, 2, (0, 0, NX, NY))
    assert recs and all(r.source_id == 1 and r.from_exposure != 2 for r in recs)


def test_sky_slitlet_is_not_a_contaminant():
    centres = [-MSA_SLIT_PITCH, 0.0, MSA_SLIT_PITCH]
    maps = []
    for shift in (0, 5, -5):
        t = _trace(_rec('1', 1), 20 + shift, 6, 34, 0, NX, centres)
        s = _trace(_rec('7', 0, kind='sky'), 12 + shift, 0, 26, 50, NX, centres)
        maps.append(TO.TraceMap.from_traces([t, s], (NY, NX)))
    assert maps[0].count.max() == 2                                    # in the map ...
    assert TO.nod_contamination(maps, 1, (0, 0, NX, NY)) == []         # ... but no light that stays


def _msa(path, rows, sources):
    """A metafile with SHUTTER_INFO rows (slitlet, column, dither, source, background,
    primary) of configuration 1 and SOURCE_INFO (source, ra, dec)."""
    from astropy.table import Table
    from astropy.io import fits
    t = Table(rows=[(s, 1, 1, 100, c, sid, bg, 'OPEN', np.nan, np.nan, d, pr, 'NONE')
                    for s, c, d, sid, bg, pr in rows],
              names=('slitlet_id', 'msa_metadata_id', 'shutter_quadrant', 'shutter_row', 'shutter_column',
                     'source_id', 'background', 'shutter_state', 'estimated_source_in_shutter_x',
                     'estimated_source_in_shutter_y', 'dither_point_index', 'primary_source', 'fixed_slit'))
    src = Table(rows=sources, names=('source_id', 'ra', 'dec'))
    sh, so = fits.table_to_hdu(t), fits.table_to_hdu(src)
    sh.name, so.name = 'SHUTTER_INFO', 'SOURCE_INFO'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((2, 2)), name='SCI'), sh, so]).writeto(path, overwrite=True)


def _p14_like(tmp_path):
    """Slitlet 33: two shutters, source 15944 in shutter 28 at dither 1, 27 at dither 2,
    out of the slitlet at dither 3 (jwst: background-only, source id 0 in every row).
    Slitlet 40: three shutters, source 500 in the middle one at every dither, a
    duplicate catalogue entry 501 of it in the same slitlet. Slitlet 9: sky only."""
    rows = [(33, 27, 1, 15944, 'Y', 'N'), (33, 28, 1, 15944, 'N', 'Y'),
            (33, 27, 2, 15944, 'N', 'Y'), (33, 28, 2, 15944, 'Y', 'N'),
            (33, 27, 3, 0, 'Y', 'N'), (33, 28, 3, 0, 'Y', 'N')]
    for d in (1, 2, 3):
        rows += [(40, 60, d, 501, 'Y', 'N'), (40, 61, d, 500, 'N', 'Y'), (40, 62, d, 0, 'Y', 'N')]
        rows += [(9, 80, d, 0, 'Y', 'N')]
    path = str(tmp_path / 'jw00001001001_01_msa.fits')
    _msa(path, rows, [(15944, 150.0, 2.0), (500, 150.1, 2.1), (501, 150.1, 2.1 + 1e-6)])
    return path


def test_slitlet_sources_names_each_slitlet_by_its_source(tmp_path):
    path = _p14_like(tmp_path)
    for d, lit33 in ((1, True), (2, True), (3, False)):
        s = TO.slitlet_sources(path, 1, d)
        assert s[33] == (15944, lit33)
        assert s[40] == (500, True)                       # the duplicate entry 501 is never the name
        assert s[9] == (None, False)                      # sky only


def test_shared_signal_recovers_injected_light():
    maps = _maps()
    recs = TO.nod_contamination(maps, 1, (0, 0, NX, NY))
    r = [x for x in recs if x.sign < 0][0]
    rng = np.random.default_rng(1)
    data = rng.normal(0, 1.0, (NY, NX))
    data[r.iy, r.ix] -= 0.5                                     # a subtracted contaminant leaves a dip
    val, err, n = TO.shared_signal(data, np.ones((NY, NX)), r)
    assert n == r.iy.size and abs(val - 0.5) < 4 * err


# ---------------------------------------------------------------------------
# The stored map (ShutterMap): built from a rate file's geometry, stuck shutters removed on read
# ---------------------------------------------------------------------------
def _rate_geometry(slits, shape=(60, 80)):
    """A ``coverage.RateGeometry`` of slitlets ``(row0, state, name)`` whose traces run along
    x at detector row ``row0`` (the source shutter's centre), 4 rows per shutter pitch."""
    from campfire_pipeline.nirspec import coverage as C

    def transform(row0):
        def tr(x, y):
            x = np.asarray(x, float); y = np.asarray(y, float)
            ys = (y - row0) / 4.0 * MSA_SLIT_PITCH
            inside = (ys >= -4.0) & (ys <= 4.0)
            return np.where(inside, x, np.nan), np.where(inside, ys, np.nan), np.where(inside, (1.0 + 0.001 * x) * 1e-6, np.nan)
        return tr

    pix, lam, ys, meta = [], [], [], []
    for row0, state, name in slits:
        g = C.geometry_on_block(transform(row0), 0, 0, row0 - 16, row0 + 16, state, MSA_SLIT_PITCH,
                                ncols=shape[1], nrows=shape[0], with_segments=False)
        rr, cc = np.nonzero(g['footprint'])
        pix.append(((rr + g['row_lo']) * shape[1] + cc).astype(np.int32))
        lam.append(g['lam'][g['footprint']].astype(np.float32)); ys.append(g['ys'][g['footprint']].astype(np.float32))
        meta.append(dict(kind='msa', name=str(name), quadrant=2, ycen=float(row0), source_id=1000 + name, state=state))
    return C.RateGeometry(np.concatenate(pix), np.concatenate(lam), np.concatenate(ys),
                          np.cumsum([0] + [p.size for p in pix]), meta, shape)


def _shutter_info(rows):
    """SHUTTER_INFO rows ``(dither, slitlet, column, source, background, primary)``, metadata id 1, MSA row 100."""
    from astropy.table import Table
    return Table(rows=[(1, d, s, 2, 100 + s, c, src, bg, pr) for d, s, c, src, bg, pr in rows],
                 names=('msa_metadata_id', 'dither_point_index', 'slitlet_id', 'shutter_quadrant', 'shutter_row',
                        'shutter_column', 'source_id', 'background', 'primary_source'))


def _two_slitlets():
    """Slitlet 7 ('1x1', source 1007 in its middle shutter, a second source 2001 in its first
    shutter at dither 2) and slitlet 8 ('11x', source 1008) two rows below, so they overlap."""
    geo = _rate_geometry(((20, '1x1', 7), (22, '11x', 8)))
    rows = []
    for d in (1, 2):
        rows += [(d, 7, 10, 2001 if d == 2 else 1007, 'N' if d == 2 else 'Y', 'N'),
                 (d, 7, 11, 1007, 'N', 'Y'), (d, 7, 12, 1007, 'Y', 'N'),
                 (d, 8, 30, 1008, 'Y', 'N'), (d, 8, 31, 1008, 'Y', 'N'), (d, 8, 32, 1008, 'N', 'Y')]
    sm = TO.ShutterMap.from_geometry(geo, _shutter_info(rows), 1, 1, sources={7: (1007, True), 8: (1008, True)})
    return geo, sm


def _direct(geo, name, centres_j, pad=TO.LIGHT_PAD, half=None):
    """Pixels of slitlet ``name`` within ``half`` (default OPEN_HALF + pad) slit units of the
    given shutters' centres, from the geometry itself."""
    half = TO.OPEN_HALF + pad if half is None else half
    for b in geo.blocks(with_segments=False):
        if b['name'] == str(name):
            c = np.asarray(centres_j, float) - b['kx']
            near = np.min(np.abs(b['ys'][:, None] - c[None, :]), axis=1) * MSA_SLIT_PITCH < half
            return set(b['pix'][near].tolist())


def _pixels(tm, name, bit=None):
    i = list(np.asarray(tm.slits['name']).astype(str)).index(str(name))
    sel = tm.index == i
    if bit is not None:
        sel &= (tm.flag & bit) > 0
    l, y, x = np.nonzero(sel)
    return set((y * tm.index.shape[2] + x).tolist())


def test_shutter_map_lights_the_padded_open_shutters_and_flags_the_unpadded_core():
    geo, sm = _two_slitlets()
    tm = sm.trace_map()
    assert _pixels(tm, 7) == _direct(geo, 7, [0, 1, 2]) and _pixels(tm, 8) == _direct(geo, 8, [0, 1, 2])
    assert _pixels(tm, 7, TO.IN_SOURCE) == _direct(geo, 7, [1])          # the 'x' shutter, padded
    assert _pixels(tm, 8, TO.IN_SOURCE) == _direct(geo, 8, [2])
    assert _pixels(tm, 7, TO.CORE) == _direct(geo, 7, [0, 1, 2], half=TO.OPEN_HALF)
    assert tm.count.max() == 2 and tm.index.shape[0] == 2                 # the traces overlap
    sh = sm.shutters
    assert list(sh['col'][sh['slit'] == 0]) == [10, 11, 12] and list(sh['is_source'][sh['slit'] == 0]) == [False, True, False]


def test_shutter_map_removes_stuck_shutters_on_read():
    geo, sm = _two_slitlets()
    tm = sm.trace_map(stuck={(2, 107, 11)})                              # slitlet 7's source shutter
    assert _pixels(tm, 7) == _direct(geo, 7, [0, 2])                    # its light is gone, the others' stays
    assert not _pixels(tm, 7, TO.IN_SOURCE)
    i7 = list(tm.slits['name']).index('7')
    assert tm.slits['kind'][i7] == 'sky' and tm.slits['source_id'][i7] == 0 and not tm.slits['lit'][i7]
    assert _pixels(tm, 8, TO.IN_SOURCE) == _direct(geo, 8, [2])         # the other slitlet untouched
    tm = sm.trace_map(stuck={(2, 107, 12)})                              # a sky shutter: source still named and lit
    i7 = list(tm.slits['name']).index('7')
    assert tm.slits['kind'][i7] == 'slitlet' and tm.slits['lit'][i7] and _pixels(tm, 7) == _direct(geo, 7, [0, 1])


def test_rows_of_finds_a_second_source_in_any_shutter_of_the_slitlet():
    _, sm = _two_slitlets()
    tm = sm.trace_map()
    i7 = list(tm.slits['name']).index('7')
    assert list(tm.rows_of(1007)) == [i7] and list(tm.rows_of(2001)) == [i7]
    assert list(sm.trace_map(stuck={(2, 107, 10)}).rows_of(2001)) == []   # its shutter stuck: no longer there


def test_shutter_map_round_trips_through_the_rate_file(tmp_path):
    from astropy.io import fits
    _, sm = _two_slitlets()
    path = str(tmp_path / 'x_rate.fits')
    fits.HDUList([fits.PrimaryHDU()] + sm.hdus()).writeto(path)
    back = TO.trace_map_from_rate(path, stuck={(2, 108, 32)})
    ref = sm.trace_map(stuck={(2, 108, 32)})
    assert np.array_equal(back.index, ref.index) and np.array_equal(back.flag, ref.flag)
    assert list(back.slits['kind']) == list(ref.slits['kind'])
    empty = str(tmp_path / 'y_rate.fits')
    fits.HDUList([fits.PrimaryHDU()] + TO.ShutterMap.empty_hdus()).writeto(empty)
    assert TO.trace_map_from_rate(empty) is None


def test_stage3_reads_the_map_stored_in_the_rate_file(tmp_path):
    import os
    from astropy.io import fits
    from campfire_pipeline.nirspec import profile_contam as CT
    _, sm = _two_slitlets()
    key = 'jw07076001001_03101_00001_nrs1'
    rate = CT.rate_path(str(tmp_path), key)
    fits.HDUList([fits.PrimaryHDU()] + sm.hdus()).writeto(rate)
    stuck = {'jw07076001001_03101': {(2, 107, 11)}}
    maps = CT.trace_maps(str(tmp_path), [key, None], stuck)
    ref = sm.trace_map(stuck={(2, 107, 11)})
    assert set(maps) == {key}
    assert np.array_equal(maps[key].index, ref.index) and np.array_equal(maps[key].flag, ref.flag)
    assert list(maps[key].rows_of(2001)) == list(ref.rows_of(2001))
    assert CT.trace_maps(str(tmp_path), [key], stuck)[key] is maps[key]           # kept in memory
    other = CT.trace_maps(str(tmp_path), [key], {})[key]                          # another stuck list: read again
    assert not np.array_equal(other.index, ref.index)
    os.utime(rate, (os.path.getmtime(rate) + 10,) * 2)                            # step 1 rewrote the rate file
    assert CT.trace_maps(str(tmp_path), [key], stuck)[key] is not maps[key]
    assert CT.maps_signature([key], stuck) != CT.maps_signature([key], {}) and CT.maps_signature([key], {}) == ''
    # a rate file without a stored map (reduced before step 1 stored one): no map, reported
    old = 'jw07076001001_03101_00002_nrs1'
    fits.HDUList([fits.PrimaryHDU()] + TO.ShutterMap.empty_hdus()).writeto(CT.rate_path(str(tmp_path), old))
    assert CT.trace_maps(str(tmp_path), [old]) == {}
    assert CT.exposures_without_maps(str(tmp_path), [f'{old}_s000001_cal.fits', f'{key}_s000001_cal.fits']) == [old]
