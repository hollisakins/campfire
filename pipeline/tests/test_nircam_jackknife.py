"""Tests for the jackknife ramp-fit zero-point correction step.

Covers the pure helpers (pattern extraction, lattice assignment, zone
geometry, paired delta measurement) and the cross-module registration
consistency the step depends on. The full step (RampFitStep rerun against
CRDS) is exercised by the on-branch validation harness, not here.
"""

import numpy as np
import pytest

from campfire_pipeline.nircam.steps.jackknife import (
    _class_map,
    _measure_deltas,
    _patterns_from_groupdq,
    _zone_map,
    _DNU, _SAT, _JUMP,
)


# ---------------------------------------------------------------------------
# pattern extraction
# ---------------------------------------------------------------------------

def test_patterns_jump_bits_only():
    gdq = np.zeros((5, 4, 4), dtype=np.uint8)
    gdq[2, 1, 1] = _JUMP
    gdq[3, 1, 1] = _JUMP
    gdq[1, 2, 2] = _SAT          # SAT alone: no pattern bit, but a carrier
    gdq[4, 3, 3] = _DNU
    pat, carries = _patterns_from_groupdq(gdq)
    assert pat[1, 1] == (1 << 2) | (1 << 3)
    assert pat[2, 2] == 0 and carries[2, 2]
    assert pat[3, 3] == 0 and carries[3, 3]
    assert not carries[1, 1]
    assert pat[0, 0] == 0 and not carries[0, 0]


def test_patterns_jump_and_sat_pixel_is_carrier():
    gdq = np.zeros((4, 2, 2), dtype=np.uint8)
    gdq[1, 0, 0] = _JUMP
    gdq[2, 0, 0] = _SAT
    pat, carries = _patterns_from_groupdq(gdq)
    # The JUMP bit is recorded, but the pixel is excluded from correction.
    assert pat[0, 0] == 1 << 1
    assert carries[0, 0]


# ---------------------------------------------------------------------------
# lattice assignment — the anti-comb property
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n_classes", [2, 7, 31, 61, 62, 64, 124, 214])
def test_class_map_no_column_or_row_comb(n_classes):
    """No class may alias a fixed column or row subset. Covers both comb
    failure modes: n dividing the axis length (`index % n` layouts) and n
    sharing a factor with a lattice stride (31 and its multiples collapsed
    every row to a single class before the strides were made runtime-coprime
    to the class count)."""
    cm = _class_map((256, 256), n_classes)
    # every column and every row must contain many distinct classes
    for axis in (0, 1):
        line_class_counts = [len(np.unique(np.take(cm, i, axis=axis)))
                             for i in range(0, 256, 37)]
        assert min(line_class_counts) >= min(n_classes, 8)


def test_class_map_balanced_and_deterministic():
    cm1 = _class_map((128, 128), 13)
    cm2 = _class_map((128, 128), 13)
    assert np.array_equal(cm1, cm2)
    counts = np.bincount(cm1.ravel(), minlength=13)
    assert counts.min() > 0.8 * counts.mean()


# ---------------------------------------------------------------------------
# zone geometry
# ---------------------------------------------------------------------------

def test_zone_map_full_frame_8():
    zm, n = _zone_map((2048, 2048), 8)
    assert n == 8
    # 4 amp columns of 512, split at row 1024
    assert zm[0, 0] == 0 and zm[0, 600] == 1 and zm[0, 2047] == 3
    assert zm[1500, 0] == 4 and zm[1500, 2047] == 7


def test_zone_map_single():
    zm, n = _zone_map((100, 300), 1)
    assert n == 1 and zm.max() == 0


# ---------------------------------------------------------------------------
# paired delta measurement
# ---------------------------------------------------------------------------

def test_measure_deltas_recovers_known_offsets():
    rng = np.random.default_rng(7)
    shape = (200, 200)
    cands = np.array([0, 3, 5], dtype=np.uint32)   # class 0 = reference
    cm = _class_map(shape, len(cands))
    zm, nz = _zone_map(shape, 1)
    clean = np.ones(shape, dtype=bool)
    truth = {3: -0.5, 5: +1.25}
    diff = rng.normal(0, 0.1, shape)
    for k, p in enumerate(cands):
        if p:
            diff[cm == k] += truth[int(p)]
    delta, sem, nulls = _measure_deltas(diff, cm, zm, nz, clean, cands,
                                        min_cell=200)
    for p, d in truth.items():
        assert delta[(-1, p)] == pytest.approx(d, abs=0.01)
        assert sem[(-1, p)] < 0.01
    assert nulls and max(abs(x) for x in nulls) < 0.02


@pytest.mark.parametrize("n_classes", [3, 208])
def test_measure_deltas_null_halves_populated(n_classes):
    """Regression: with an EVEN class count the diagonal lattice's parity is
    constant within a class, so a checkerboard split-half left one half empty
    and the null diagnostic silently vanished. The 2x2-block split must
    populate both halves for every class count."""
    shape = (256, 256)
    cands = np.arange(n_classes, dtype=np.uint32)
    cm = _class_map(shape, n_classes)
    zm, nz = _zone_map(shape, 1)
    clean = np.ones(shape, dtype=bool)
    diff = np.random.default_rng(0).normal(0, 0.1, shape)
    _, _, nulls = _measure_deltas(diff, cm, zm, nz, clean, cands, min_cell=100)
    assert len(nulls) == n_classes - 1
    assert all(np.isfinite(nulls))


def test_measure_deltas_nonfinite_diff_excluded():
    """A pattern whose calibration fits come back NaN must yield no delta
    (and therefore no correction), not a NaN that poisons SCI."""
    shape = (128, 128)
    cands = np.array([0, 9], dtype=np.uint32)
    cm = _class_map(shape, 2)
    zm, nz = _zone_map(shape, 1)
    clean = np.ones(shape, dtype=bool)
    diff = np.where(cm == 1, np.nan, 0.0)
    delta, _, _ = _measure_deltas(diff, cm, zm, nz, clean, cands, min_cell=50)
    assert (-1, 9) not in delta


def test_measure_deltas_zone_fallback():
    """A (zone, pattern) cell below the floor yields no zonal entry, but the
    frame-global delta is still available for fallback."""
    shape = (64, 64)
    cands = np.array([0, 1], dtype=np.uint32)
    cm = _class_map(shape, 2)
    zm, nz = _zone_map(shape, 8)
    clean = np.ones(shape, dtype=bool)
    diff = np.where(cm == 1, 0.7, 0.0)
    delta, _, _ = _measure_deltas(diff, cm, zm, nz, clean, cands,
                                  min_cell=3000)
    assert (-1, 1) not in delta          # even global cell too small here
    delta, _, _ = _measure_deltas(diff, cm, zm, nz, clean, cands,
                                  min_cell=500)
    assert delta[(-1, 1)] == pytest.approx(0.7, abs=1e-6)
    assert not any(z >= 0 for (z, _p) in delta)   # zonal cells all under floor


# ---------------------------------------------------------------------------
# registration consistency — one commit must carry all surfaces
# ---------------------------------------------------------------------------

def test_step_registration_consistent():
    from campfire_pipeline.nircam import orchestrate as orch
    from campfire_pipeline.nircam.cli import _STEP_LABELS, _SCI_MUTATING_STEPS
    from campfire_pipeline.common import cfp

    names = set(orch.STEP_NAMES)
    assert 'jackknife' in names

    # every stamped step's CFP key is in the keyset
    for name, key in orch.CFP_STEPS:
        if key is not None:
            assert key in cfp.NIRCAM.keys, (name, key)

    # status labels cover every stamped step (resample has no CFP stamp)
    for name, key in orch.ALL_STEPS:
        if key is not None:
            assert name in _STEP_LABELS, name

    # every runner exists for every process step
    for name, _ in orch.PROCESS_STEPS:
        assert name in orch._RUNNERS, name

    # per-exposure registry rows agree with the CFP table
    cfp_by_name = dict(orch.CFP_STEPS)
    for name, (_mod, _fn, key) in orch._PER_EXPOSURE_STEPS.items():
        assert cfp_by_name[name] == key, name

    assert _SCI_MUTATING_STEPS <= names
    assert 'jackknife' in _SCI_MUTATING_STEPS
    assert 'jackknife' in orch._CRDS_STEPS


def test_jackknife_ordering_and_default_config():
    from campfire_pipeline.nircam.orchestrate import PROCESS_STEPS
    names = [n for n, _ in PROCESS_STEPS]
    # must sit inside the sidecar lifetime window
    assert names.index('detector1') < names.index('jackknife') \
        < names.index('persistence')

    import tomllib
    from importlib.resources import files
    cfg = tomllib.loads(
        files('campfire_pipeline.data').joinpath('config_default.toml')
        .read_text())
    jk = cfg['nircam']['jackknife']
    assert jk['enabled'] is True
    assert jk['zones'] == 8
