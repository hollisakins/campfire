"""Unit tests for the P2 rate-exposure triage upsert (design §3.2).

Pure functions, no DB/cloud: the rate-filename → (exposure_root, detector) parse,
the record builder's key columns, and the split-ownership partition (which columns
a re-deploy UPDATE is allowed to touch). The full insert/upsert round-trip against
Postgres is exercised live by `campfire deploy … --local` in CI.
"""
import pytest

from campfire_layout import Scope
from campfire.deploy.rate_exposures import (
    build_rate_exposure_records,
    parse_rate_filename,
    partition_rate_records,
)


def test_parse_rate_filename():
    assert parse_rate_filename("jw07076020001_04101_00001_nrs1_rate.fits") == (
        "jw07076020001_04101_00001", "nrs1")
    assert parse_rate_filename("jw07076020001_04101_00002_nrs2_rate.fits") == (
        "jw07076020001_04101_00002", "nrs2")


def test_parse_rate_filename_rejects_non_rate_and_bad_detector():
    with pytest.raises(ValueError):
        parse_rate_filename("jw07076020001_04101_00001_nrs1_117757.fits")  # spectrum-exposure
    with pytest.raises(ValueError):
        parse_rate_filename("jw07076020001_04101_00001_nrca_rate.fits")    # not nrs1/nrs2


def test_exposure_root_excludes_detector_unlike_registry_ref():
    # P1's registry exposure_ref keeps the full stem (…_nrs1_rate); this table's
    # exposure_root drops both the detector and the _rate suffix.
    root, det = parse_rate_filename("jw07076020001_04101_00001_nrs1_rate.fits")
    assert root == "jw07076020001_04101_00001"
    assert det == "nrs1"
    assert "nrs1" not in root and "rate" not in root


def test_build_rate_exposure_records_shape(tmp_path):
    # touch a real file so the (best-effort) header read runs and degrades to None
    p = tmp_path / "jw07076020001_04101_00001_nrs1_rate.fits"
    p.write_bytes(b"\x00")
    recs = build_rate_exposure_records(
        [p], observation="ember_egs_p1", scope=Scope(obs="ember_egs_p1"))
    assert len(recs) == 1
    r = recs[0]
    assert r["observation"] == "ember_egs_p1"
    assert r["exposure_root"] == "jw07076020001_04101_00001"
    assert r["detector"] == "nrs1"
    assert r["filename"] == p.name
    assert r["stage"] == "rate"
    assert r["storage_key"] == (
        "data/products/nirspec/ember_egs_p1/jw07076020001_04101_00001_nrs1_rate.fits")
    # unreadable stub → metadata degrades to None, never raises
    assert r["grating"] is None and r["image_width"] is None and r["image_height"] is None


def _record(**over):
    base = {
        "observation": "ember_egs_p1",
        "exposure_root": "jw07076020001_04101_00001",
        "detector": "nrs1",
        "filename": "jw07076020001_04101_00001_nrs1_rate.fits",
        "grating": "PRISM",
        "image_width": 2048,
        "image_height": 2048,
        "storage_key": "data/products/nirspec/ember_egs_p1/x_rate.fits",
        "stage": "rate",
    }
    base.update(over)
    return base


def test_partition_new_row_seeds_pending_and_none():
    new, upd = partition_rate_records([_record()], existing_keys=set(), now="T")
    assert upd == []
    assert len(new) == 1
    assert new[0]["review_status"] == "pending"
    assert new[0]["masking"] == "none"
    assert new[0]["created_at"] == "T" and new[0]["updated_at"] == "T"


def test_partition_existing_row_omits_web_owned_columns():
    key = {("ember_egs_p1", "jw07076020001_04101_00001", "nrs1")}
    new, upd = partition_rate_records([_record()], existing_keys=key, now="T")
    assert new == []
    assert len(upd) == 1
    update = upd[0]
    # split ownership: the UPDATE must NOT carry any web-owned triage column
    for web_col in ("review_status", "masking", "mask_regions", "notes",
                    "created_at"):
        assert web_col not in update, web_col
    # it DOES carry identity + render + updated_at
    assert update["updated_at"] == "T"
    for col in ("observation", "exposure_root", "detector", "filename", "stage",
                "grating", "image_width", "image_height", "storage_key"):
        assert col in update


def test_partition_existing_row_skips_null_render_columns():
    # a re-deploy where the header read failed must not overwrite good render
    # columns with NULLs — null render columns are simply omitted from the UPDATE.
    rec = _record(grating=None, image_width=None, image_height=None)
    key = {("ember_egs_p1", "jw07076020001_04101_00001", "nrs1")}
    _, upd = partition_rate_records([rec], existing_keys=key, now="T")
    update = upd[0]
    assert "grating" not in update
    assert "image_width" not in update
    assert "image_height" not in update
    assert update["storage_key"] == rec["storage_key"]  # non-null render kept
