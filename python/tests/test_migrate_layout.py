"""Tests for the one-time #212 layout migration (issue #244).

Covers the shared, zero-dependency core in ``campfire_layout.migrate`` (the same
code the operator CLI and ``campfire sync`` run) and the client hook in
``campfire.migrate``:

  * detection is dry-run by default and never touches disk;
  * ``apply`` relocates products + raw + reference + NIRCam-shared and is
    idempotent (a second plan is not pending);
  * a download-only client (``obs_cfg=None``) migrates the products tree only
    and leaves ``raw/`` untouched;
  * the no-clobber invariant: an existing destination is never overwritten;
  * the ``_meta`` layout marker round-trips through the LocalStore.
"""

import pytest

from campfire_layout.migrate import LayoutMigrator, apply_migration, plan_migration


OBS_CFG = {"ember_egs_p1": {"data_subdir": "7076"}}


def _build_old_layout(root):
    """Materialize a representative pre-#212 tree under ``root``."""
    obs = root / "products" / "ember_egs_p1"
    obs.mkdir(parents=True)
    (obs / "ember_egs_p1_prism_clear_15182_spec.fits").write_text("spec")
    (obs / "ember_egs_p1_summary.ecsv").write_text("summary")
    (obs / "_ember_egs_p1_stuck_closed_shutters.toml").write_text("reducer")
    # a non-NIRSpec stray dir: must be left in place + warned, never misfiled
    (root / "products" / "junk").mkdir()
    (root / "products" / "junk" / "notes.txt").write_text("n")
    # OLD-layout NIRSpec raw dir (referenced by observations.toml)
    raw = root / "raw" / "7076"
    raw.mkdir(parents=True)
    (raw / "jw07076020001_04101_00003_nrs1_uncal.fits").write_text("raw")
    # NIRCam per-field shared flats -> hoisted to reference/nircam/shared
    fl = root / "reference" / "nircam" / "cosmos" / "flats"
    fl.mkdir(parents=True)
    (fl / "flat_f444w.fits").write_text("flat")


# --- detection / dry-run ----------------------------------------------------

def test_plan_detects_old_layout_without_touching_disk(tmp_path):
    _build_old_layout(tmp_path)
    plan = plan_migration(tmp_path, obs_cfg=OBS_CFG)
    assert plan["pending"] is True
    assert plan["counts"].get("MOVE", 0) >= 3
    # dry-run must not have moved anything
    assert (tmp_path / "products" / "ember_egs_p1"
            / "ember_egs_p1_prism_clear_15182_spec.fits").exists()
    assert not (tmp_path / "products" / "nirspec").exists()


def test_fresh_new_layout_is_not_pending(tmp_path):
    obs = tmp_path / "products" / "nirspec" / "ember_egs_p1"
    obs.mkdir(parents=True)
    (obs / "ember_egs_p1_prism_clear_15182_spec.fits").write_text("spec")
    assert plan_migration(tmp_path, obs_cfg=OBS_CFG)["pending"] is False


# --- full-tree apply (reducer: observations.toml present) -------------------

def test_apply_full_tree_and_idempotent(tmp_path):
    _build_old_layout(tmp_path)
    result = apply_migration(tmp_path, obs_cfg=OBS_CFG, echo=lambda *a, **k: None)

    # products -> products/nirspec/<obs>/
    assert (tmp_path / "products" / "nirspec" / "ember_egs_p1"
            / "ember_egs_p1_prism_clear_15182_spec.fits").exists()
    assert not (tmp_path / "products" / "ember_egs_p1").exists()
    # raw -> raw/nirspec/<subdir>/
    assert (tmp_path / "raw" / "nirspec" / "7076"
            / "jw07076020001_04101_00003_nrs1_uncal.fits").exists()
    # reducer TOML -> reference/nirspec/<obs>/
    assert (tmp_path / "reference" / "nirspec" / "ember_egs_p1"
            / "stuck_closed_shutters.toml").exists()
    # NIRCam flat -> reference/nircam/shared/flats/
    assert (tmp_path / "reference" / "nircam" / "shared" / "flats"
            / "flat_f444w.fits").exists()
    # stray dir untouched
    assert (tmp_path / "products" / "junk" / "notes.txt").exists()
    # audit manifest written
    assert result["manifest_path"] is not None

    # idempotent: nothing left to do
    assert plan_migration(tmp_path, obs_cfg=OBS_CFG)["pending"] is False


# --- download-only client (obs_cfg=None): products only, raw untouched ------

def test_client_obs_cfg_none_skips_raw(tmp_path):
    _build_old_layout(tmp_path)
    apply_migration(tmp_path, obs_cfg=None, echo=lambda *a, **k: None)
    # products migrated
    assert (tmp_path / "products" / "nirspec" / "ember_egs_p1").exists()
    # raw left flat (raw migration skipped entirely without an observations table)
    assert (tmp_path / "raw" / "7076").exists()
    assert not (tmp_path / "raw" / "nirspec" / "7076").exists()


# --- safety: never clobber an existing destination -------------------------

def test_no_clobber_when_both_old_and_new_exist(tmp_path):
    _build_old_layout(tmp_path)
    # simulate a partially-migrated tree: the new dir already holds a file
    new = tmp_path / "products" / "nirspec" / "ember_egs_p1"
    new.mkdir(parents=True)
    (new / "keep.fits").write_text("authoritative")

    plan = plan_migration(tmp_path, obs_cfg=OBS_CFG)
    # a "BOTH exist" situation is a warning, not a material move of the obs dir
    assert any("BOTH" in w for w in plan["warnings"])

    apply_migration(tmp_path, obs_cfg=OBS_CFG, echo=lambda *a, **k: None)
    # old dir left in place, new file preserved (not overwritten)
    assert (tmp_path / "products" / "ember_egs_p1").exists()
    assert (new / "keep.fits").read_text() == "authoritative"


def test_dir_without_nirspec_markers_is_left_alone(tmp_path):
    d = tmp_path / "products" / "mystery"
    d.mkdir(parents=True)
    (d / "random.dat").write_text("x")
    plan = plan_migration(tmp_path, obs_cfg=OBS_CFG)
    assert plan["pending"] is False
    assert any("no NIRSpec markers" in w for w in plan["warnings"])
    assert (tmp_path / "products" / "mystery").exists()
    assert not (tmp_path / "products" / "nirspec" / "mystery").exists()


# --- client helper: observations.toml discovery ----------------------------

def test_client_load_obs_cfg(tmp_path):
    from campfire import migrate as cmig

    assert cmig.load_obs_cfg(tmp_path) is None  # no config/observations.toml
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "observations.toml").write_text('[ember_egs_p1]\ndata_subdir = "7076"\n')
    assert cmig.load_obs_cfg(tmp_path) == {"ember_egs_p1": {"data_subdir": "7076"}}


def test_client_plan_and_apply(tmp_path):
    from campfire import migrate as cmig

    _build_old_layout(tmp_path)
    assert cmig.plan(tmp_path)["pending"] is True  # obs_cfg=None -> products still pending
    cmig.apply(tmp_path, echo=lambda *a, **k: None)
    assert (tmp_path / "products" / "nirspec" / "ember_egs_p1").exists()


# --- store marker round-trip ------------------------------------------------

def test_layout_marker_roundtrips_through_store(tmp_path):
    from campfire.db.store import LocalStore
    from campfire.migrate import LAYOUT_MARKER_KEY, LAYOUT_MARKER_VALUE

    s = LocalStore(tmp_path / "meta" / "campfire.db")
    try:
        assert s.get_meta(LAYOUT_MARKER_KEY) is None
        s.set_meta(LAYOUT_MARKER_KEY, LAYOUT_MARKER_VALUE)
        assert s.get_meta(LAYOUT_MARKER_KEY) == LAYOUT_MARKER_VALUE
    finally:
        s.close()
