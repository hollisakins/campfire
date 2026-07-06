"""Tests for the NIRCam triangle/asterism catalog matcher (nircam/align/matcher.py).

The matcher wraps ``tristars`` behind a ``tweakwcs`` ``MatchCatalogs`` interface.
Fixtures are synthetic tangent-plane point sets (no WCS/corrector needed): a
reference set, and an "image" set that is the reference rotated + shifted +
jittered, with spurious extras appended. Because ``im[k]`` is built from
``ref[k]``, the geometrically-correct match for reference row ``k`` is image row
``k`` — so a returned pair ``(ri, ii)`` is correct iff ``ri == ii``.
"""

import numpy as np
import pytest
from astropy.table import Table

from campfire_pipeline.nircam.align import TriangleMatch


def _synthetic(n=40, theta_deg=0.4, shift=(2.5, -1.0), jitter=0.02,
               n_spurious=10, seed=0):
    """Return (ref[n,2], im[n+n_spurious,2], R, shift, n_true)."""
    rng = np.random.default_rng(seed)
    ref = rng.uniform(0.0, 100.0, size=(n, 2))
    th = np.deg2rad(theta_deg)
    R = np.array([[np.cos(th), -np.sin(th)],
                  [np.sin(th), np.cos(th)]])
    shift = np.asarray(shift, dtype=float)
    im_true = ref @ R.T + shift + rng.normal(0.0, jitter, ref.shape)
    spurious = rng.uniform(0.0, 100.0, size=(n_spurious, 2))
    im = np.vstack([im_true, spurious])
    return ref, im, R, shift, n


def _tables(ref, im, mag_ref=None, mag_im=None):
    rt = Table({'TPx': ref[:, 0], 'TPy': ref[:, 1]})
    it = Table({'TPx': im[:, 0], 'TPy': im[:, 1]})
    if mag_ref is not None:
        rt['mag'] = np.asarray(mag_ref, dtype=float)
    if mag_im is not None:
        it['mag'] = np.asarray(mag_im, dtype=float)
    return rt, it


def _residuals(ref, im, R, shift, ri, ii):
    pred = ref[ri] @ R.T + shift
    return np.hypot(*(pred - im[ii]).T)


# --- core recovery ----------------------------------------------------------

@pytest.mark.parametrize('shift', [(2.5, -1.0), (30.0, -25.0)])
def test_recovers_shift_and_rotation(shift):
    # Triangle sides are translation-invariant, so even a large offset (the
    # regime that breaks nearest-neighbour matching) is recovered.
    ref, im, R, shift_v, n_true = _synthetic(shift=shift)
    rt, it = _tables(ref, im)

    ri, ii = TriangleMatch()(rt, it)
    assert len(ri) == len(ii) >= 6

    n_correct = int(np.sum(ri == ii))
    assert n_correct >= 6
    assert n_correct / len(ri) >= 0.9                      # high precision
    assert np.all(_residuals(ref, im, R, shift_v, ri, ii)[ri == ii] < 0.2)


def test_return_order_is_reference_first():
    # imcat is larger than refcat (spurious extras), so a swapped return would
    # put out-of-range / wrong indices in the first array.
    ref, im, R, shift, n_true = _synthetic()
    rt, it = _tables(ref, im)

    ri, ii = TriangleMatch()(rt, it)
    assert ri.max() < len(rt)          # ref indices index refcat
    assert ii.max() < len(it)          # im indices index imcat
    # ref rows transform onto the matched image rows (not the reverse).
    assert np.all(_residuals(ref, im, R, shift, ri, ii)[ri == ii] < 0.2)


def test_spurious_image_sources_excluded():
    ref, im, R, shift, n_true = _synthetic(n_spurious=15)
    rt, it = _tables(ref, im)
    ri, ii = TriangleMatch()(rt, it)
    assert np.all(ii < n_true)         # no match points at a spurious extra


# --- graceful degradation ---------------------------------------------------

def test_too_few_sources_returns_empty():
    rt = Table({'TPx': [1.0, 2.0], 'TPy': [1.0, 3.0]})          # only 2
    it = Table({'TPx': [1.0, 2.0, 3.0], 'TPy': [1.0, 2.0, 3.5]})
    ri, ii = TriangleMatch()(rt, it)
    assert len(ri) == 0 and len(ii) == 0


def test_empty_table_returns_empty():
    rt = Table({'TPx': [], 'TPy': []})
    it = Table({'TPx': [1.0, 2.0, 3.0], 'TPy': [1.0, 2.0, 3.5]})
    ri, ii = TriangleMatch()(rt, it)
    assert len(ri) == 0 and len(ii) == 0


def test_missing_tp_columns_raises():
    rt = Table({'x': [1.0, 2.0, 3.0], 'y': [1.0, 2.0, 3.0]})
    it = Table({'TPx': [1.0, 2.0, 3.0], 'TPy': [1.0, 2.0, 3.0]})
    with pytest.raises(KeyError):
        TriangleMatch()(rt, it)


# --- brightest-N cap + index remapping --------------------------------------

def test_brightest_cap_remaps_to_original_rows():
    # n_true=160 > brightest=150. Rank magnitude so the SURVIVING rows are the
    # LAST 150 (mag[k] = 159-k => row 159 brightest), not the first — proving
    # both that the cap fires and that returned indices are original rows.
    ref, im, R, shift, n_true = _synthetic(n=160, n_spurious=0)
    mag = (n_true - 1) - np.arange(n_true)
    rt, it = _tables(ref, im, mag_ref=mag, mag_im=mag)

    ri, ii = TriangleMatch(brightest=150)(rt, it)
    assert len(ri) >= 6
    # Only rows [10, 159] survive the brightest-150 cut on both sides.
    assert ri.min() >= 10 and ri.max() <= 159
    # Remapping correct: original ref rows transform onto original image rows.
    assert np.all(_residuals(ref, im, R, shift, ri, ii)[ri == ii] < 0.2)
    assert int(np.sum(ri == ii)) >= 6


def test_brightest_cap_without_mag_col_spreads_and_matches(capsys):
    # tweakwcs' create_group_catalog strips every brightness column from the
    # pooled image catalog, so the cap can't rank by magnitude. It must then
    # spread the surviving vertices EVENLY across the whole catalog rather than
    # slice a contiguous head: a multi-detector pooled catalog is vstacked
    # head-to-tail, so a head-slice would keep only the first detector's block
    # and starve the rest (collapsing the pooled geometry to one detector).
    import campfire_pipeline.nircam.align.matcher as _m
    _m._UNRANKED_WARNED = False                     # warn-once is per-process
    ref, im, R, shift, n_true = _synthetic(n=200, n_spurious=0)
    rt, it = _tables(ref, im)                       # no 'mag' column either side
    ri, ii = TriangleMatch(brightest=150)(rt, it)
    assert len(ri) >= 6
    # Vertices are drawn from across the full [0, 200) range, not confined to
    # the first 150 — the old head-slice would cap both maxima below 150.
    assert ri.max() >= 150 and ii.max() >= 150
    # Remapping still lands original ref rows on their original image rows.
    assert int(np.sum(ri == ii)) >= 6
    assert np.all(_residuals(ref, im, R, shift, ri, ii)[ri == ii] < 0.2)
    assert "no 'mag' column" in capsys.readouterr().out


# --- determinism ------------------------------------------------------------

def test_deterministic():
    ref, im, R, shift, n_true = _synthetic(seed=7)
    rt, it = _tables(ref, im)
    m = TriangleMatch()
    r1, i1 = m(rt, it)
    r2, i2 = m(rt, it)
    assert np.array_equal(r1, r2) and np.array_equal(i1, i2)
