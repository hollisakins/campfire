"""Tests for the trace maps of nirspec/trace_overlap.py, on synthetic traces.

Two slitlets on a small detector: the target's trace and a contaminant's trace
share detector rows over half of the columns. Between nods both sources move one
shutter along their slitlets, so their source shutters move together on the
detector, as in a real nod pattern. The geometry (jwst WCS) is validated on real
data separately.
"""

import numpy as np
import pytest

from campfire_pipeline.nirspec import trace_overlap as TO
from campfire_pipeline.nirspec.constants import MSA_SLIT_MARGIN, MSA_SLIT_PITCH

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


@pytest.mark.parametrize('state,ymin,ymax,expect', [
    ('1x1', -2.2, 2.2, (-1, 0, 1)),
    ('x11', -1.05, 3.35, (0, 1, 2)),
    ('11x', -3.35, 1.05, (-2, -1, 0)),
    ('10x11', -3.35, 3.35, (-2, 0, 1, 2)),
    ('111', -2.2, 2.2, (-1, 0, 1)),            # no source shutter: reference from ymin
])
def test_open_shutter_centres(state, ymin, ymax, expect):
    c = TO.open_shutter_centres(state, ymin, ymax)
    assert np.allclose(c, np.array(expect) * MSA_SLIT_PITCH)


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


def test_write_read_roundtrip(tmp_path):
    m = _maps()[0]
    m.meta = dict(MAPVER=TO.MAP_VERSION, EXPOSURE='x_rate.fits', DETECTOR='NRS1')
    p = tmp_path / 'x_tracemap.fits'
    m.write(str(p))
    r = TO.TraceMap.read(str(p))
    assert np.array_equal(r.index, m.index) and np.array_equal(r.flag, m.flag)
    assert list(r.slits['source_id']) == [1, 2]


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


def test_beyond_range_pixels_are_flagged_and_unmodelled():
    """A contaminant's trace continued past its nominal range (columns 80+) keeps its
    pixels in the map with the BEYOND_RANGE flag; per source they are unmodelled
    (masked for the shape fit), not attributed to the contaminant's pass 1."""
    from campfire_pipeline.nirspec import profile_contam as CT
    centres = [-MSA_SLIT_PITCH, 0.0, MSA_SLIT_PITCH]
    maps = []
    for shift in (0, 5, -5):
        t = _trace(_rec('1', 1), 20 + shift, 6, 34, 0, NX, centres)
        rec, cen, (p, y, l) = _trace(_rec('2', 2), 12 + shift, 0, 26, 50, NX, centres)
        beyond = (p % NX) >= 80
        maps.append(TO.TraceMap.from_traces([t, (rec, cen, (p, y, l, beyond))], (NY, NX)))
    recs = TO.nod_contamination(maps, 1, (0, 0, NX, NY))
    assert all(np.array_equal(r.beyond, r.ix >= 80) for r in recs)
    assert any(r.beyond.any() for r in recs) and any((~r.beyond).any() for r in recs)

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
        assert not g.sources[k][2][:, 80:].any()             # never predicted from pass 1 there
        assert g.unmodelled[k][:, 80:].any() and not g.unmodelled[k][:, :80].any()
        assert g.mask[k][:, 80:].any()
    old = CT.geometry(exps, 1, mp, beyond='ignore')          # as before: those pixels unknown
    assert not any(u.any() for u in old.unmodelled) and not old.mask[0][:, 80:].any()


def test_shared_signal_recovers_injected_light():
    maps = _maps()
    recs = TO.nod_contamination(maps, 1, (0, 0, NX, NY))
    r = [x for x in recs if x.sign < 0][0]
    rng = np.random.default_rng(1)
    data = rng.normal(0, 1.0, (NY, NX))
    data[r.iy, r.ix] -= 0.5                                     # a subtracted contaminant leaves a dip
    val, err, n = TO.shared_signal(data, np.ones((NY, NX)), r)
    assert n == r.iy.size and abs(val - 0.5) < 4 * err
