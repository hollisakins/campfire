"""The joint first pass (joint_first_pass): neighbourhoods and the target's errors and
not-separable flags."""
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
        self.ext = dict(n=0, grids=[np.zeros(0), np.zeros(0)], lo=3.0, model={}, rows=None)


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
    out = JF.run_target(9, [9], _Cache(tmp_path, bad=[9]), {}, {}, pass1_dir=str(tmp_path / 'p1'))
    assert out is None
    assert 'no_usable_pixels' in (tmp_path / 'p1' / 'p_9_pass1_missing.txt').read_text()
