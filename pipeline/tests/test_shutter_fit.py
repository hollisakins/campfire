"""Stuck-closed shutters from the count-rate frame (shutter_fit)."""
import numpy as np
import scipy.sparse as sp
from astropy.table import Table

from campfire_pipeline.nirspec import shutter_fit as SF


def _overlapping_templates(rng, n_pix=4000, n_tpl=6):
    """Templates that overlap pairwise on part of their pixels, as traces do on a grating."""
    rows, cols, vals = [], [], []
    for k in range(n_tpl):
        lo = k * 500
        pix = np.arange(lo, lo + 900)                      # each overlaps the next by 400 pixels
        rows.append(pix)
        cols.append(np.full(pix.size, k))
        vals.append(0.02 * (1 + 0.3 * np.sin(pix / 50.0 + k)))
    return sp.csc_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(n_pix, n_tpl))


def test_solve_separates_a_stuck_shutter_under_overlap():
    rng = np.random.default_rng(0)
    A = _overlapping_templates(rng)
    a_true = np.array([1.0, 0.0, 1.1, 0.95, 1.0, 1.05])   # shutter 1 stuck closed
    sig = 0.002
    y = A @ a_true + 0.001 + rng.normal(0, sig, A.shape[0])
    a, err, nfit, chi2, const = SF.solve(A.tocsr(), y, np.full(y.size, 1 / sig ** 2))
    assert abs(a[1]) < 5 * err[1] and abs(a[1]) < 0.05
    assert np.allclose(a[[0, 2, 3, 4, 5]], a_true[[0, 2, 3, 4, 5]], atol=5 * err[[0, 2, 3, 4, 5]].max())
    assert 0.8 < chi2 < 1.2 and abs(const - 0.001) < 1e-3
    assert np.all(nfit == 900)


def test_solve_survives_an_outlier_and_an_empty_column():
    rng = np.random.default_rng(1)
    A = sp.hstack([_overlapping_templates(rng), sp.csc_matrix((4000, 1))]).tocsc()   # last column has no pixel
    y = A @ np.r_[np.ones(6), 0.0] + rng.normal(0, 0.002, 4000)
    y[100:110] += 1.0                                       # a cosmic ray
    a, err, nfit, chi2, const = SF.solve(A.tocsr(), y, np.full(4000, 1 / 0.002 ** 2))
    assert np.allclose(a[:6], 1.0, atol=0.05) and np.isnan(err[6])


def _table(rows):
    return Table(rows=rows, names=SF.COLUMNS)


def _row(q, r, c, det, a, n=500):
    return (q, r, c, 10, 0, 1, 'sky', det, n, 3.0, 5.0, a, 0.02)


def test_call_rule():
    t1 = _table([_row(2, 195, 64, 'nrs1', -0.02), _row(2, 195, 64, 'nrs2', 0.03),        # stuck
                 _row(2, 195, 65, 'nrs1', 1.0), _row(2, 195, 65, 'nrs2', 0.97),          # open
                 _row(2, 300, 149, 'nrs1', -0.7), _row(2, 300, 149, 'nrs2', 0.92),       # detectors disagree
                 _row(1, 35, 15, 'nrs2', 0.65), _row(1, 35, 15, 'nrs2', 0.7),            # partly open
                 _row(3, 9, 19, 'nrs2', 0.01),                                            # one measurement only
                 _row(4, 1, 1, 'nrs2', 0.0, n=50)])                                       # too few pixels
    calls = SF.call_shutters([t1])
    assert calls[(2, 195, 64)]['stuck'] and calls[(2, 195, 64)]['n'] == 2
    assert not calls[(2, 195, 65)]['stuck'] and calls[(2, 195, 65)]['note'] == ''
    assert not calls[(2, 300, 149)]['stuck'] and calls[(2, 300, 149)]['note'].startswith('guard')
    assert not calls[(1, 35, 15)]['stuck'] and calls[(1, 35, 15)]['note'].startswith('review')
    assert not calls[(3, 9, 19)]['stuck'] and calls[(3, 9, 19)]['note'] == 'n<2'
    assert (4, 1, 1) not in calls


def test_call_combines_tables_of_a_root():
    nods = [_table([_row(2, 195, 64, 'nrs2', a)]) for a in (0.02, -0.01, 0.6)]   # one nod lifted by stray light
    c = SF.call_shutters(nods)[(2, 195, 64)]
    assert c['stuck'] and c['n'] == 3 and abs(c['a'] - 0.02) < 1e-9
