"""Catalogued neighbours of a source's slitlet (``slitlet_neighbours``): positions from the
MSA plan, the distance rule, the brightness threshold and the layering of overrides. The
listed layout is p1 slitlet 35: 634522 (primary) and 634547 share shutter columns 26-28."""
import os

import numpy as np
from astropy.table import Table

from campfire_pipeline.nirspec import slitlet_neighbours as SN
from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH as P


def _table(rows, mid=192):
    # rows: (slitlet, dither, quadrant, row, column, source, background, x, y)
    n = len(rows)
    col = lambda i: [r[i] for r in rows]                 # noqa: E731
    return Table(dict(slitlet_id=col(0), msa_metadata_id=np.full(n, mid), dither_point_index=col(1),
                      shutter_quadrant=col(2), shutter_row=col(3), shutter_column=col(4), source_id=col(5),
                      background=col(6), estimated_source_in_shutter_x=np.array(col(7), float),
                      estimated_source_in_shutter_y=np.array(col(8), float), primary_source=['Y'] * n))


def _shutters():
    # (dither, column, source, background, y) of the open shutters, as SHUTTER_INFO lists them
    rows = [(1, 26, 634547, 'N', 0.02), (1, 27, 634522, 'N', 0.32), (1, 28, 634522, 'Y', np.nan),
            (2, 26, 634522, 'N', 0.34), (2, 27, 634522, 'Y', np.nan), (2, 28, 634522, 'Y', np.nan),
            (3, 26, 634547, 'Y', np.nan), (3, 27, 634547, 'N', 0.00), (3, 28, 634522, 'N', 0.31)]
    return _table([(35, d, 1, 112, c, s, b, 0.5, y) for d, c, s, b, y in rows])


NOD_DITHER = {1: 1, 2: 2, 3: 3}
PARTNERS = {1: [2, 3], 2: [1, 3], 3: [1, 2]}


def test_listed_positions_and_the_distance_rule():
    sl, nbs = SN.scene(_shutters(), None, 192, 634522)
    assert [n.source_id for n in nbs] == [634547]
    assert {d: round(u / P, 2) for d, u in sl.target_u.items()} == {1: 26.82, 2: 25.84, 3: 27.81}
    assert {d: round(u / P) for d, u in sl.ref_u.items()} == {1: 27, 2: 26, 3: 28}
    # nod 2's partner 3 puts the neighbour 0.76 away: clean at 0.75, dirty at 0.8
    assert SN.dirty_partners(sl, nbs, NOD_DITHER, PARTNERS) == {1: {3: [634547]}, 2: {1: [634547]}}
    assert SN.dirty_partners(sl, nbs, NOD_DITHER, PARTNERS, 0.8)[2] == {1: [634547], 3: [634547]}
    sl, nbs = SN.scene(_shutters(), None, 192, 634547)              # not in the slitlet at dither 2
    assert sorted(sl.target_u) == [1, 3]
    assert SN.dirty_partners(sl, nbs, NOD_DITHER, PARTNERS) == {1: {2: [634522]}, 3: {1: [634522]}}


def test_threshold_and_the_flag_without_a_clean_partner():
    sl, nbs = SN.scene(_shutters(), None, 192, 634522)
    nbs[0].sky_frac = 0.03
    dec = {d.nod: d for d in SN.decide('r', sl, nbs, NOD_DITHER, PARTNERS, min_sky_frac=0.02)}
    assert dec[1].keep == [2] and dec[1].why == 'dropped'
    assert dec[2].keep == [3] and dec[3].keep == [1, 2] and dec[3].why == ''
    dec = {d.nod: d for d in SN.decide('r', sl, nbs, NOD_DITHER, PARTNERS, min_sky_frac=0.05)}
    assert dec[1].keep == [2, 3] and dec[1].why == 'faint'
    dec = {d.nod: d for d in SN.decide('r', sl, nbs, NOD_DITHER, PARTNERS, 0.02, max_distance=0.8)}
    assert dec[2].keep == [1, 3] and dec[2].why == 'no clear background'


def test_unmeasured_neighbour_counts_as_bright():
    sl, nbs = SN.scene(_shutters(), None, 192, 634522)
    dec = {d.nod: d for d in SN.decide('r', sl, nbs, NOD_DITHER, PARTNERS, min_sky_frac=0.05)}
    assert dec[1].keep == [2]


def _plan(seed=1):
    """A synthetic MSA plan: 40 sources, each alone in a 3-shutter slitlet of quadrant 1,
    nodded by one shutter (dithers 1, 2, 3 at columns c, c+1, c-1), placed by a known
    quadratic map; plus source 9001 in no shutter, 0.3 shutter below source 1 on the sky."""
    rng = np.random.default_rng(seed)
    E, N = rng.uniform(-60, 60, 40), rng.uniform(-60, 60, 40)
    E = np.r_[E, E[0]]
    N = np.r_[N, N[0] - 0.3 / 1.87]
    ids = np.r_[np.arange(1, 41), 9001]

    def where(e, n, d):
        r = 180 + 3.72 * e - 0.1 * n + 1e-3 * e * e
        c = 85 + 0.05 * e + 1.87 * n + 5e-4 * n * n + {1: 0, 2: 1, 3: -1}[d]
        return r, c

    rows = []
    for k, s in enumerate(ids[:40]):
        for d in (1, 2, 3):
            r, c = where(E[k], N[k], d)
            ri, ci = int(np.round(r)), int(np.round(c))         # a shutter is centred on its index
            for j in (-1, 0, 1):
                main = j == 0
                rows.append((k + 1, d, 1, ri, ci + j, int(s) if main else 0, 'N' if main else 'Y',
                             r - ri + 0.5 if main else np.nan, c - ci + 0.5 if main else np.nan))
    src = Table(dict(source_id=ids, ra=E / 3600.0, dec=N / 3600.0))
    return _table(rows), src, where, E, N


def test_sky_to_msa_reproduces_the_plan_and_places_unlisted_sources():
    shut, src, where, E, N = _plan()
    sky = SN.SkyToMsa.fit(shut, src, 192)
    for d in (1, 2, 3):
        r, c = sky.place(1, d)
        rt, ct = where(E, N, d)
        assert np.allclose(r, rt, atol=2e-3) and np.allclose(c, ct, atol=2e-3)
    assert sky.place(2, 1) is None
    sl, nbs = SN.scene(shut, sky, 192, 1)
    assert [n.source_id for n in nbs] == [9001]                     # in no shutter, but on source 1's slitlet
    far = {d: abs(nbs[0].where[d] - sl.target_u[d]) / P for d in nbs[0].where}
    assert all(abs(v - 0.3) < 0.01 for v in far.values())           # it nods with source 1


def test_layer_hand_then_catalogue():
    hand = {'1': [3], '3': []}                          # nods 1 and 3 by hand (3 excluded)
    slitlet = {'1': ([2], 'neighbor 9 at 0.030 of background'), '2': ([1], 'neighbor 9 at 0.030 of background')}
    out, auto, why = SN.layer(hand, slitlet)
    assert out['1'] == [3] and why['1'] == 'manual' and '1' not in auto
    assert out['2'] == [1] and why['2'] == 'neighbor 9 at 0.030 of background' and '2' in auto
    assert out['3'] == [] and why['3'] == 'manual' and '3' not in auto


def test_light_over_sky_reads_the_neighbours_shutter():
    ny, nx = 30, 200
    ys = np.repeat(np.linspace(-1.8, 3.0, ny)[:, None], nx, axis=1)
    sky = np.ones((ny, nx))
    sci = sky.copy()
    sci[np.abs(ys - 1.15) < 0.5] += 0.04               # a neighbour one shutter up, 4 % of the sky
    med, hi = SN.light_over_sky(sci, sky, ys, 1.15)
    assert abs(med - 0.04) < 1e-9 and abs(hi - 0.04) < 1e-9
    assert abs(SN.light_over_sky(sci, sky, ys, 0.0)[0]) < 1e-9
    # past the last open shutter the background is near zero and stray light reads huge
    sky[ys > 1.75] = 1e-3
    sci[ys > 1.75] = 0.5
    assert SN.light_over_sky(sci, sky, ys, 2.0)[0] > 0.5
    use = SN.usable_pixels(ys, [-1.15, 0.0, 1.15])
    assert abs(SN.light_over_sky(sci, sky, ys, 2.0, use=use)[0] - 0.04) < 1e-9


def test_model_light_places_the_pass1_frame_on_the_cutout():
    from types import SimpleNamespace
    img = np.array([1.0, 2.0, 4.0])
    fr = dict(origin=np.array([11, 21]), shape=np.array([4, 5]), idx=np.array([0, 6, 19]), img=img,
              ext=np.array([False, False, True]))
    P1 = SimpleNamespace(frames={'k': fr})
    pl = np.full((3, 3), 0.5)
    # the frame's 1-based origin (11, 21) is detector (10, 20); the cutout's 0-based corner is (9, 20)
    out = SN.model_light(P1, 'k', 9, 20, (3, 3), pl)
    assert np.isclose(out[1, 0], 1.0 / 0.5)                   # frame (0, 0) -> cutout (1, 0), path loss put back
    assert np.isclose(out[2, 1], 2.0 / 0.5)                   # frame (1, 1) -> cutout (2, 1)
    assert np.isclose(out.sum(), 6.0)                         # frame (3, 4) is off the cutout and 'ext' anyway
    assert SN.model_light(P1, 'other', 10, 21, (3, 3), pl) is None


def test_measure_from_the_model_ignores_other_light_and_the_separation_limit():
    ny, nx = 30, 200
    ys = np.repeat(np.linspace(-1.8, 3.0, ny)[:, None], nx, axis=1)
    sky = np.ones((ny, nx))
    sci = sky.copy()
    sci[np.abs(ys - 0.6) < 0.5] += 0.5                       # another slitlet's light where the neighbor is
    model = np.where(np.abs(ys - 0.6) < 0.5, 0.03, 0.0)       # the neighbor's own modeled light
    slit = SN.Slitlet(slitlet_id=1, quadrant=1, row=1, open_u={1: np.array([-1.15, 0.0, 1.15])},
                      target_u={1: 0.0}, ref_u={1: 0.0})
    frames = {1: (sci, sky, ys, True)}
    nb = SN.measure(SN.Neighbour(9, {1: 0.6}), slit, frames)  # data: too close (0.6 < 1) to measure
    assert not np.isfinite(nb.sky_frac)
    nb = SN.measure(SN.Neighbour(9, {1: 0.6}), slit, frames, model_of=lambda d: model)
    assert abs(nb.sky_frac - 0.03) < 1e-9 and nb.measured_in == 'dither 1 (model)'


def test_bkg_cards_match_reads_bkgnods_and_bkgwhy(tmp_path):
    from astropy.io import fits
    from campfire_pipeline.nirspec import canonical as C
    from campfire_pipeline.nirspec.stage2 import _bkg_cards_match
    paths = []
    for nod, partners, why in ((1, [2], 'neighbor 9 at 0.030 of background'), (2, [1, 3], ''), (3, [1, 2], '')):
        h = fits.Header()
        for k, (v, c) in C.bkg_nods_cards(partners, overridden=nod == 1, auto=nod == 1, why=why).items():
            h[k] = (v, c)
        p = str(tmp_path / f'jw00001001001_03101_{nod:05d}_nrs1_7.fits')
        fits.PrimaryHDU(header=h).writeto(p)
        paths.append(p)
    reasons = {'1': 'neighbor 9 at 0.030 of background'}
    assert _bkg_cards_match(paths, {'1': [2]}, ['1'], reasons) == 'same'
    assert _bkg_cards_match(paths, None, [], {}) == 'subtraction'         # stage 2b would now keep both
    assert _bkg_cards_match(paths, {'1': [3]}, ['1'], reasons) == 'subtraction'   # another partner
    assert _bkg_cards_match(paths, {'1': [2]}, ['1'], {'1': 'neighbor 9 at 0.050 of background'}) == 'cards'


def test_an_exposure_without_an_msa_configuration_has_no_scene(tmp_path):
    """A standalone fixed-slit canonical names no metafile: no slitlet, no neighbours
    (stage 2b's same-slitlet check then leaves it alone)."""
    from astropy.io import fits
    p = tmp_path / 'jw00001001001_03101_00001_nrs1_7.fits'
    fits.PrimaryHDU().writeto(p)
    assert SN.scene_for(str(p), str(tmp_path), 7, {}) == (None, [])
    fits.setval(p, 'MSAMETFL', value='N/A')
    fits.setval(p, 'MSAMETID', value=1)
    assert SN.scene_for(str(p), str(tmp_path), 7, {}) == (None, [])


def test_an_override_naming_only_other_detectors_nods_stops_with_its_name():
    import pytest
    from campfire_pipeline.nirspec.stage2 import nod_background_partners
    files = [f'jw00001001001_03101_{n:05d}_nrs2_7.fits' for n in (1, 2)]
    assert nod_background_partners(files, {'1': [2, 3]}) == [[1], [0]]       # one of them here: used
    assert nod_background_partners(files, {'1': []}) == [None, [0]]          # empty: excluded
    with pytest.raises(ValueError, match='none of which is in this group'):
        nod_background_partners(files, {'1': [3]})


def test_the_rectified_view_is_redone_when_its_master_background_is_rewritten(tmp_path, monkeypatch):
    import pytest
    from astropy.io import fits
    from campfire_pipeline.nirspec import stage2 as S2
    canon = tmp_path / 'jw00001001001_03101_00001_nrs1_7.fits'
    mbkg = tmp_path / 'jw00001001001_03101_00001_nrs1_mbkg.fits'
    mbkg.write_text('x')
    os.utime(mbkg, (1.7e9, 1.7e9))
    s2d = fits.ImageHDU(name='S2D_SCI')
    s2d.header['MBKGSUB'] = True
    s2d.header['MBKGTIME'] = S2._mbkg_stamp(str(canon))
    fits.HDUList([fits.PrimaryHDU(), s2d]).writeto(canon)

    def rebuilt(path):
        raise RuntimeError('rebuilt')
    monkeypatch.setattr(S2, 'master_background_on', rebuilt)
    S2.resample_single_exposure({'path': str(canon)})            # the same master background: kept
    os.utime(mbkg, (1.8e9, 1.8e9))                               # step 2 wrote it again
    with pytest.raises(RuntimeError, match='rebuilt'):
        S2.resample_single_exposure({'path': str(canon)})
