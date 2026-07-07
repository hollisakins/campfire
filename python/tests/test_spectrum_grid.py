"""Unit tests for the P4 nods-grid population (design §4.2).

Pure functions, no DB/cloud: the canonical-filename parse, the record builder's
key columns, and the split-ownership partition. The full insert/upsert round-trip
runs live against local Supabase in CI (`campfire deploy … --local`).
"""
import pytest

from campfire_layout import Scope
from campfire.deploy.spectrum_grid import (
    build_spectrum_exposure_records,
    parse_spectrum_exposure_filename,
    partition_spectrum_records,
)


def test_parse_spectrum_exposure_filename():
    assert parse_spectrum_exposure_filename(
        "jw07076020001_04101_00001_nrs1_117757.fits"
    ) == ("jw07076020001_04101", "00001", "nrs1", 117757)
    assert parse_spectrum_exposure_filename(
        "jw07076020001_04101_00002_nrs2_120383.fits"
    ) == ("jw07076020001_04101", "00002", "nrs2", 120383)


def test_parse_rejects_rate_and_final_and_bad():
    with pytest.raises(ValueError):
        parse_spectrum_exposure_filename("jw07076020001_04101_00001_nrs1_rate.fits")
    with pytest.raises(ValueError):
        parse_spectrum_exposure_filename("ember_egs_p1_prism_clear_117757_spec.fits")
    with pytest.raises(ValueError):
        parse_spectrum_exposure_filename("jw07076020001_04101_00001_nrs1_notanint.fits")


def test_exposure_root_divergence_from_rate_table():
    # spectrum_exposures.exposure_root = 2 tokens (exposure token split out as nod);
    # nirspec_rate_exposures.exposure_root = 3 tokens (detector-stripped). Deliberate.
    root, nod, det, sid = parse_spectrum_exposure_filename(
        "jw07076020001_04101_00001_nrs1_117757.fits")
    assert root == "jw07076020001_04101"   # 2 tokens, NOT jw..._04101_00001
    assert nod == "00001"
    assert det == "nrs1" and sid == 117757


def test_build_records_shape(tmp_path):
    p = tmp_path / "jw07076020001_04101_00001_nrs1_117757.fits"
    p.write_bytes(b"\x00")
    recs = build_spectrum_exposure_records(
        [p], observation="ember_egs_p1", scope=Scope(obs="ember_egs_p1"))
    assert len(recs) == 1
    r = recs[0]
    assert r["observation"] == "ember_egs_p1"
    assert r["exposure_root"] == "jw07076020001_04101"
    assert r["nod"] == "00001"
    assert r["detector"] == "nrs1"
    assert r["source_id"] == 117757
    assert r["stage"] == "cal"
    assert r["storage_key"] == (
        "data/products/nirspec/ember_egs_p1/jw07076020001_04101_00001_nrs1_117757.fits")
    # unreadable stub → metadata degrades to None, never raises
    assert r["exp_group"] is None and r["grating"] is None
    assert r["image_width"] is None and r["image_height"] is None


def _record(**over):
    base = {
        "observation": "ember_egs_p1",
        "exposure_root": "jw07076020001_04101",
        "nod": "00001",
        "detector": "nrs1",
        "source_id": 117757,
        "exp_group": 3,
        "grating": "PRISM",
        "filename": "jw07076020001_04101_00001_nrs1_117757.fits",
        "storage_key": "data/products/nirspec/ember_egs_p1/x.fits",
        "image_width": 40,
        "image_height": 400,
        "stage": "cal",
    }
    base.update(over)
    return base


def test_partition_new_row_seeds_pending_and_none():
    new, upd = partition_spectrum_records([_record()], existing_keys=set(), now="T")
    assert upd == []
    assert len(new) == 1
    assert new[0]["review_status"] == "pending"
    assert new[0]["masking"] == "none"
    assert new[0]["created_at"] == "T" and new[0]["updated_at"] == "T"


def test_partition_existing_row_omits_web_owned_columns():
    key = {("ember_egs_p1", "jw07076020001_04101", "00001", "nrs1", 117757)}
    new, upd = partition_spectrum_records([_record()], existing_keys=key, now="T")
    assert new == []
    assert len(upd) == 1
    update = upd[0]
    for web_col in ("review_status", "masking", "notes", "created_at"):
        assert web_col not in update, web_col
    assert update["updated_at"] == "T"
    for col in ("observation", "exposure_root", "nod", "detector", "source_id",
                "filename", "stage", "exp_group", "grating", "image_width",
                "image_height", "storage_key"):
        assert col in update


def test_partition_existing_row_skips_null_render_columns():
    rec = _record(exp_group=None, grating=None, image_width=None, image_height=None)
    key = {("ember_egs_p1", "jw07076020001_04101", "00001", "nrs1", 117757)}
    _, upd = partition_spectrum_records([rec], existing_keys=key, now="T")
    update = upd[0]
    assert "exp_group" not in update
    assert "grating" not in update
    assert "image_width" not in update
    assert update["storage_key"] == rec["storage_key"]  # non-null render kept
