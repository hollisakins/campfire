"""Same-slitlet neighbours from the MSA metadata (``slitlet_neighbours``), on the layout of
p1 slitlet 35: 634522 (primary) and 634547 share one slitlet, shutter columns 26-28."""
import numpy as np
from astropy.table import Table

from campfire_pipeline.nirspec import slitlet_neighbours as SN


def _shutters():
    # (dither, column, source, background, y) of the open shutters, as SHUTTER_INFO lists them
    rows = [(1, 26, 634547, 'N', 0.02), (1, 27, 634522, 'N', 0.32), (1, 28, 634522, 'Y', np.nan),
            (2, 26, 634522, 'N', 0.34), (2, 27, 634522, 'Y', np.nan), (2, 28, 634522, 'Y', np.nan),
            (3, 26, 634547, 'Y', np.nan), (3, 27, 634547, 'N', 0.00), (3, 28, 634522, 'N', 0.31)]
    n = len(rows)
    return Table(dict(slitlet_id=np.full(n, 35), msa_metadata_id=np.full(n, 192), shutter_quadrant=np.ones(n, int),
                      shutter_row=np.full(n, 112), shutter_column=[r[1] for r in rows], source_id=[r[2] for r in rows],
                      background=[r[3] for r in rows], estimated_source_in_shutter_y=[r[4] for r in rows],
                      dither_point_index=[r[0] for r in rows], primary_source=['Y'] * n))


NOD_DITHER = {1: 1, 2: 2, 3: 3}
PARTNERS = {1: [2, 3], 2: [1, 3], 3: [1, 2]}


def test_layout_and_dirty_partners_follow_the_shutters():
    lay = SN.slitlet_layout(_shutters(), 192)
    t, nbs = SN.neighbours_of(lay, 634522)
    assert [n.source_id for n in nbs] == [634547]
    assert {d: p.column for d, p in t.items()} == {1: 27, 2: 26, 3: 28}
    assert SN.dirty_partners(t, nbs, NOD_DITHER, PARTNERS) == {1: {3: [634547]}, 2: {1: [634547]}}
    t, nbs = SN.neighbours_of(lay, 634547)            # not in the slitlet at dither 2
    assert SN.dirty_partners(t, nbs, NOD_DITHER, PARTNERS) == {1: {2: [634522]}, 3: {1: [634522]}}


def test_threshold_decides_the_drop():
    lay = SN.slitlet_layout(_shutters(), 192)
    t, nbs = SN.neighbours_of(lay, 634522)
    nbs[0].sky_frac = 0.03
    dec = {d.nod: d for d in SN.decide('r', t, nbs, NOD_DITHER, PARTNERS, min_sky_frac=0.02)}
    assert dec[1].keep == [2] and dec[1].why == 'dropped'
    assert dec[2].keep == [3] and dec[3].keep == [1, 2] and dec[3].why == ''
    dec = {d.nod: d for d in SN.decide('r', t, nbs, NOD_DITHER, PARTNERS, min_sky_frac=0.05)}
    assert dec[1].keep == [2, 3] and dec[1].why == 'faint'


def test_unmeasured_neighbour_counts_as_bright():
    lay = SN.slitlet_layout(_shutters(), 192)
    t, nbs = SN.neighbours_of(lay, 634522)
    dec = {d.nod: d for d in SN.decide('r', t, nbs, NOD_DITHER, PARTNERS, min_sky_frac=0.05)}
    assert dec[1].keep == [2]


def test_layer_hand_then_catalogue_then_scan():
    effective = {'1': [3], '2': [3], '3': [2]}          # nod 1 by hand; nods 2, 3 from the scan's file
    scan = {'2', '3'}
    slitlet = {'1': ([2], 'slitlet 9 at 0.030 of sky'), '2': ([1], 'slitlet 9 at 0.030 of sky')}
    out, auto, why = SN.layer(effective, scan, slitlet)
    assert out['1'] == [3] and why['1'] == 'hand' and '1' not in auto
    assert out['2'] == [1] and why['2'] == 'slitlet 9 at 0.030 of sky' and '2' in auto   # nothing both keep
    assert out['3'] == [2] and why['3'] == 'scan' and '3' in auto
    out, _, why = SN.layer({'2': [1, 3]}, {'2'}, {'2': ([1], 'slitlet 9 at 0.030 of sky')})
    assert out['2'] == [1] and why['2'].endswith('; scan')


def test_light_over_sky_reads_the_neighbours_shutter():
    ny, nx = 30, 200
    ys = np.repeat(np.linspace(-1.8, 3.0, ny)[:, None], nx, axis=1)
    sky = np.ones((ny, nx))
    sci = sky.copy()
    sci[np.abs(ys - 1.15) < 0.5] += 0.04               # a neighbour one shutter up, 4 % of the sky
    med, hi = SN.light_over_sky(sci, sky, ys, 1.15)
    assert abs(med - 0.04) < 1e-9 and abs(hi - 0.04) < 1e-9
    assert abs(SN.light_over_sky(sci, sky, ys, 0.0)[0]) < 1e-9
