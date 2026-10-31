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
    monkeypatch.setattr(JF.PJ, 'owned_pixels', lambda ms: None)
    monkeypatch.setattr(JF.PJ, 'design', lambda ms: (A, d, v))
    sol = JF.solve_target(members, n_clip=0)
    assert sol['nosep'][: n // 2].all()
    assert not sol['nosep'][n // 2:].any()
    sep = slice(n // 2, n)
    assert np.allclose(sol['flux'][sep], 1.0, atol=5 * np.nanmax(sol['err'][sep]))
    assert np.all(sol['err'][: n // 2] > 3 * sol['err'][sep].max())     # inseparable: large errors
