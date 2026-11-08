"""The joint first pass (joint_first_pass): neighbourhoods and the target's errors and
not-separable flags."""
import os

import numpy as np
import scipy.sparse as sp

from campfire_pipeline.nirspec import joint_first_pass as JF


def test_neighbourhood_is_symmetric_strongest_first_and_capped():
    coupled = {1: {2: 500, 3: 150}, 2: {4: 1000}, 3: {}, 4: {}, 5: {1: 50}}
    assert JF.neighbourhood(coupled, 1, depth=1) == [1, 2, 3]
    assert JF.neighbourhood(coupled, 1, depth=2) == [1, 2, 3, 4]
    assert JF.neighbourhood(coupled, 4, depth=1) == [4, 2]           # 2 lights 4: symmetric
    assert JF.neighbourhood(coupled, 1, depth=2, cap=2) == [1, 2]
    assert 5 not in JF.neighbourhood(coupled, 1, depth=2)            # below min_px


def test_target_order_walks_the_coupling():
    coupled = {1: {2: 500}, 2: {3: 400}, 3: {}, 7: {8: 300}, 8: {}}
    order = JF.target_order(coupled)
    assert sorted(order) == [1, 2, 3, 7, 8]
    assert order.index(2) == order.index(1) + 1 and order.index(8) == order.index(7) + 1


def _builds(coupled, order, depth, window):
    from collections import OrderedDict
    lru, n = OrderedDict(), 0
    for s in order:
        for t in JF.neighbourhood(coupled, s, depth=depth):
            if t in lru:
                lru.move_to_end(t)
                continue
            n += 1
            lru[t] = None
            if len(lru) > window:
                lru.popitem(last=False)
    return n


def test_target_order_keeps_neighbourhoods_in_the_cache():
    """A 20 x 20 grid of sources lit by their right and lower neighbours: the order
    rebuilds few members more than once with a cache far smaller than the grid."""
    k = 20
    sid = lambda i, j: i * k + j                                            # noqa: E731
    coupled = {sid(i, j): {} for i in range(k) for j in range(k)}
    for i in range(k):
        for j in range(k):
            if j + 1 < k:
                coupled[sid(i, j)][sid(i, j + 1)] = 500
            if i + 1 < k:
                coupled[sid(i, j)][sid(i + 1, j)] = 500
    order = JF.target_order(coupled, depth=1, window=30)
    assert sorted(order) == sorted(coupled)
    assert _builds(coupled, order, 1, 30) < 2.0 * len(coupled)


class _Model:
    def __init__(self, n):
        self.lam_grid = np.linspace(3.0, 5.0, n)


class _Member:
    def __init__(self, n):
        self.model = _Model(n)
        self.sid = 0
        self.ext =dict(n=0, grids=[np.zeros(0), np.zeros(0)], lo=3.0, model={}, rows=None)


def test_solve_target_flags_only_the_inseparable_wavelengths(monkeypatch):
    """Two sources whose spectra share pixels: on the first half of the target's grid each
    target point appears only together with one neighbour point, in the same proportions on
    every pixel (inseparable); on the second half the target also has pixels of its own."""
    rng = np.random.default_rng(0)
    n = 40
    rows, cols, vals = [], [], []
    r = 0
    for i in range(n):
        for _ in range(6):                                      # shared pixels: target i + neighbour i
            rows += [r, r]
            cols += [i, n + i]
            vals += [1.0, 0.8]
            r += 1
        if i >= n // 2:
            for _ in range(6):                                  # own pixels of the target
                rows.append(r)
                cols.append(i)
                vals.append(1.0)
                r += 1
        for _ in range(6):                                      # own pixels of the neighbour
            rows.append(r)
            cols.append(n + i)
            vals.append(1.0 if i >= n // 2 else 0.0)
            r += 1
    A = sp.csr_matrix((vals, (rows, cols)), shape=(r, 2 * n))
    v = np.full(r, 0.01)
    d = A @ np.r_[np.ones(n), 2 * np.ones(n)] + rng.normal(0, 0.1, r)
    members = [_Member(n), _Member(n)]
    monkeypatch.setattr(JF, 'design_full', lambda ms: (A, d, v, r))
    sol = JF.solve_target(members, n_clip=0)
    assert sol['nosep'][: n // 2].all()
    assert not sol['nosep'][n // 2:].any()
    sep = slice(n // 2, n)
    assert np.allclose(sol['flux'][sep], 1.0, atol=5 * np.nanmax(sol['err'][sep]))
    assert np.all(sol['err'][: n // 2] > 3 * sol['err'][sep].max())     # inseparable: large errors


def test_solve_target_recovers_unknowns_of_very_different_scales(monkeypatch):
    """A target with in-range unknowns in calibrated units (design ~1e10) and past-cutout
    unknowns in count rate (design ~1): both are recovered (an absolute ridge set by the
    in-range columns used to pin the count-rate ones to zero)."""
    rng = np.random.default_rng(1)
    n_in, n_ext = 30, 10
    m = _Member(n_in)
    m.ext = dict(n=n_ext, grids=[np.zeros(0), np.linspace(5.2, 5.7, n_ext)], lo=3.0, model={}, rows=None)
    rows, cols, vals = [], [], []
    r = 0
    for i in range(n_in + n_ext):
        scale = 1e10 if i < n_in else 1.0
        for _ in range(8):
            rows.append(r)
            cols.append(i)
            vals.append(scale)
            r += 1
    A = sp.csr_matrix((vals, (rows, cols)), shape=(r, n_in + n_ext))
    truth = np.r_[np.full(n_in, 3e-12), np.full(n_ext, 0.05)]
    v = np.r_[np.full(8 * n_in, 1e-4), np.full(8 * n_ext, 1e-4)]
    d = A @ truth + rng.normal(0, 1e-2, r)
    monkeypatch.setattr(JF, 'design_full', lambda ms: (A, d, v, 8 * n_in))
    sol = JF.solve_target([m], n_clip=0)
    assert np.allclose(sol['flux'], 3e-12, rtol=0.01)
    red = sol['ext'][1]
    assert np.allclose(red['flux'], 0.05, atol=5 * red['err'].max())
    assert np.all(red['err'] < 0.01)


def test_ext_columns_pick_the_end_by_wavelength():
    m = _Member(5)
    m.ext = dict(n=6, grids=[np.array([2.6, 2.7, 2.8]), np.array([5.2, 5.3, 5.4])], lo=2.88)
    ok, i, t = JF._ext_cols(m, np.array([2.65, 5.35, 4.0, 5.5]))
    assert ok.tolist() == [True, True, False, False]
    assert i[:2].tolist() == [0, 4] and np.allclose(t[:2], 0.5)


def test_terms_are_the_exposure_and_its_partners():
    class E:
        def __init__(self, p):
            self.partners = p
    exps = [E([1, 2]), E([0, 2]), E([])]
    assert JF.PF.nod_terms(exps, 0) == [(0, 1.0), (1, -0.5), (2, -0.5)]
    assert JF.PF.nod_terms(exps, 2) == [(2, 1.0)]


def test_rate_nod_diff_subtracts_the_partners_mean(tmp_path):
    from astropy.io import fits
    vals = {'a': 5.0, 'b': 1.0, 'c': 3.0}
    for k, v in vals.items():
        dq = np.zeros((4, 6), np.uint32)
        if k == 'c':
            dq[1, 2] = 1                                   # DO_NOT_USE in a partner
        fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.full((4, 6), v, np.float32), name='SCI'),
                      fits.ImageHDU(np.full((4, 6), 0.1, np.float32), name='VAR_POISSON'),
                      fits.ImageHDU(np.full((4, 6), 0.1, np.float32), name='VAR_RNOISE'),
                      fits.ImageHDU(dq, name='DQ')]).writeto(tmp_path / f'{k}_rate.fits')
    d, v, bad = JF._rate_nod_diff(str(tmp_path), 'a', ['b', 'c'], np.arange(4), np.arange(6))
    assert np.allclose(d, 5.0 - 2.0)
    # weighted like the cutout's differences (profile_fit.ProfileModel.diff_variance): with N = 3
    # frames of variance 0.2, (N / (N - 1))**2 * 0.2, not the single difference's 0.2 + 2 * 0.2 / 4
    assert np.allclose(v, (3 / 2) ** 2 * 0.2)
    assert bad[1, 2] and bad.sum() == 1
    assert JF._rate_nod_diff(str(tmp_path), 'a', [], np.arange(4), np.arange(6)) is None


def test_others_light_on_target_is_their_columns_times_their_spectra():
    t, c = _Member(3), _Member(2)
    t.sid, c.sid = 1, 2
    t.owned = np.ones(4, bool)
    t.col0, t.xcol0 = 0, 3
    c.col0, c.xcol0 = 3, 5
    A = sp.csr_matrix(np.array([[1, 0, 0, 0.5, 0], [0, 1, 0, 0, 0], [0, 0, 1, 0, 2.0], [0, 0, 0, 1, 1],
                                [0, 0, 0, 1, 0]]))
    f = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    gi = np.arange(5)
    s = np.full(5, 0.1)
    out = JF._others_on_target([t, c], A, f, gi, s)
    assert len(out) == 1 and out[0]['sid'] == 2
    assert out[0]['rows'].tolist() == [0, 2, 3]                 # rows 0-3 are the target's; row 1 has none of c
    assert np.allclose(out[0]['val'], [2.0, 10.0, 9.0])
    assert np.allclose(out[0]['err'], [0.05, 0.2, 0.2])


class _Exp:
    def __init__(self, name, partners, use_data=True):
        self.name = name
        self.partners = partners
        self.use_data = use_data
        self.origin = (10, 20)
        self.y = np.zeros((5, 8))
        self.ok = np.ones((5, 8), bool)


class _Target:
    def __init__(self, exps):
        self.sid = 7
        self.exps = exps
        self.pix_k = np.array([0, 0, 1, 1])
        self.pix_iy = np.array([1, 2, 1, 3])
        self.pix_ix = np.array([4, 4, 5, 6])


def _exps():
    return [_Exp('jw00001001001_01101_00001_nrs1_cal.fits', [1]), _Exp('jw00001001001_01101_00002_nrs1_cal.fits', [0])]


def test_joint_light_round_trip_and_placement(tmp_path, monkeypatch):
    exps = _exps()
    t = _Target(exps)
    others = [dict(sid=3, rows=np.array([0, 2]), val=np.array([1.5, -0.5]), err=np.array([0.1, 0.2])),
              dict(sid=4, rows=np.array([3]), val=np.array([2.0]), err=np.array([0.3]))]
    path = str(tmp_path / 'p_jointlight.npz')
    JF.write_joint_light(path, t, others, 'sigA')
    assert JF.read_joint_light(path, 'sigB') is None
    jl = JF.read_joint_light(path, 'sigA')
    monkeypatch.setattr(JF.PF, 'planned_position', lambda exps: (0.0, 0))
    monkeypatch.setattr(JF.CT, 'main_shutter', lambda e, c: np.ones(e.y.shape, bool))
    monkeypatch.setattr(JF.CT, 'diff_sigma', lambda exps, k: np.ones(exps[k].y.shape))
    P = JF.CT.Prediction(pred=[np.zeros((5, 8)) for _ in exps], dpred=[np.zeros((5, 8)) for _ in exps])
    JF.add_joint_light(P, exps, jl)
    assert P.pred[0][1, 4] == 1.5 and P.pred[1][1, 5] == -0.5 and P.pred[1][3, 6] == 2.0
    assert np.allclose(P.pred[0], P.pred_joint[0]) and np.isclose(P.dpred_joint[1][3, 6], 0.3)
    assert P.joint == {3, 4} and np.isclose(P.contributors[3], 1.5) and np.isclose(P.contributors[4], 2.0)


def test_joint_light_refuses_other_nod_differences(tmp_path):
    exps = _exps()
    path = str(tmp_path / 'p_jointlight.npz')
    JF.write_joint_light(path, _Target(exps), [], 's')
    other = _exps()
    other[0].partners = []
    P = JF.CT.Prediction(pred=[np.zeros((5, 8)) for _ in other], dpred=[np.zeros((5, 8)) for _ in other])
    import pytest
    with pytest.raises(ValueError):
        JF.add_joint_light(P, other, JF.read_joint_light(path))


class _Cache:
    """A member cache whose source 2 cannot be built and whose source 9 (the target of
    the boxcar test) cannot either."""

    def __init__(self, tmp_path, bad):
        f = tmp_path / 'a_nrs1_cal.fits'
        f.write_text('x')
        self.workspace = str(tmp_path)
        self.is_prism = False
        self.bad = set(bad)
        self.tasks = {s: dict(product=f'p_{s}', groups=[[str(f)]], overrides=None, science=[f.name],
                              prism_product='') for s in (1, 2, 3, 9)}

    def get(self, sid):
        if sid in self.bad:
            raise JF.PF.NoUsablePixels(f'source {sid}')
        return sid

    def prism_sig(self, sid):
        return 'none'


def _stub_write(monkeypatch, written):
    monkeypatch.setattr(JF.CT, 'write_pass1', lambda out, P1, *a: written.append((out, P1)))
    monkeypatch.setattr(JF, 'write_joint_light', lambda path, target, others, sig: None)


def test_run_target_leaves_out_a_neighbour_that_cannot_be_built(tmp_path, monkeypatch):
    solved, written = [], []
    monkeypatch.setattr(JF, 'solve_target', lambda members, degenerate: solved.append(members) or {})
    monkeypatch.setattr(JF, 'pass1_of', lambda target, sol, maps, sc: ('joint', target))
    _stub_write(monkeypatch, written)
    out = JF.run_target(1, [1, 2, 3], _Cache(tmp_path, bad=[2]), {}, {}, pass1_dir=str(tmp_path / 'p1'))
    assert solved == [[1, 3]]
    assert out.endswith('p_1_pass1.npz') and written[0][1] == ('joint', 1)


def test_run_target_takes_the_boxcar_when_the_target_cannot_be_built(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(JF, '_boxcar', lambda sid, t, cache, maps, sc, err: (('boxcar', sid), 's2d'))
    _stub_write(monkeypatch, written)
    out = JF.run_target(9, [9, 1], _Cache(tmp_path, bad=[9]), {}, {}, pass1_dir=str(tmp_path / 'p1'))
    assert out.endswith('p_9_pass1.npz') and written[0][1] == ('boxcar', 9)


def test_run_target_without_a_boxcar_writes_the_reason(tmp_path, monkeypatch):
    def no_box(sid, t, cache, maps, sc, err):
        raise type(err)(f'{err}; no boxcar') from err
    monkeypatch.setattr(JF, '_boxcar', no_box)
    (tmp_path / 'p1').mkdir()
    (tmp_path / 'p1' / 'p_9_pass1.npz').write_text('an earlier pass 1')
    out = JF.run_target(9, [9], _Cache(tmp_path, bad=[9]), {}, {}, pass1_dir=str(tmp_path / 'p1'),
                        overwrite=True)
    assert out is None
    assert 'no_usable_pixels' in (tmp_path / 'p1' / 'p_9_pass1_missing.txt').read_text()
    assert not (tmp_path / 'p1' / 'p_9_pass1.npz').exists()     # neighbours must not read it as current


def test_run_target_checks_the_neighbours_files_too(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(JF.CT, '_pass1_valid', lambda out, groups, n, sig: seen.append(groups) or None)
    monkeypatch.setattr(JF, 'solve_target', lambda members, degenerate: {})
    monkeypatch.setattr(JF, 'pass1_of', lambda target, sol, maps, sc: ('joint', target))
    _stub_write(monkeypatch, [])
    cache = _Cache(tmp_path, bad=[])
    nb = tmp_path / 'b_nrs1_cal.fits'
    nb.write_text('x')
    cache.tasks[3]['groups'] = [[str(nb)]]
    JF.run_target(1, [1, 3], cache, {}, {}, pass1_dir=str(tmp_path / 'p1'))
    assert str(nb) in [f for g in seen[0] for f in g]


def test_own_pass_files_sit_beside_the_joint_pass(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(JF, 'solve_target', lambda members, degenerate: {})
    monkeypatch.setattr(JF, 'pass1_of', lambda target, sol, maps, sc: ('joint', target))
    _stub_write(monkeypatch, written)
    out = JF.run_target(1, [1], _Cache(tmp_path, bad=[]), {}, {}, own=True)
    assert out.endswith('p_1_own_pass1.npz')
    joint = JF.CT.pass1_path(str(tmp_path), 'p_1')
    assert JF.CT.pass1_current(str(tmp_path), 'p_1') == joint     # nothing written yet (stubbed)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, 'w').close()
    assert JF.CT.pass1_current(str(tmp_path), 'p_1') == out
    JF.drop_own(str(tmp_path), ['p_1'])
    assert not os.path.exists(out) and JF.CT.pass1_current(str(tmp_path), 'p_1') == joint
