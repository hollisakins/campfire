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


def test_update_batches_never_mix_column_sets():
    # postgrest-py sends one column list per batch (the union of its rows' keys) and
    # NULL where a row lacks one, which would blank a card the other file has: rows
    # with different columns must go in different batches.
    from campfire.deploy.rate_exposures import upsert_rate_exposures
    from .fakes import FakePostgrest
    base = dict(observation="obs", detector="nrs1", stage="rate")
    existing = [dict(base, exposure_root=f"jw0707602000{i}_04101_00001",
                     filename=f"jw0707602000{i}_04101_00001_nrs1_rate.fits") for i in (1, 2)]
    sb = FakePostgrest({"nirspec_rate_exposures": [dict(r) for r in existing]})
    records = [dict(existing[0], dither_position=1), dict(existing[1])]   # file 2 lacks the card
    upsert_rate_exposures(sb, records)
    batches = [rec.data for rec in sb.upserts]
    assert len(batches) == 2
    assert all(len({tuple(sorted(r)) for r in batch}) == 1 for batch in batches)


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


def test_partition_new_row_seeds_pending():
    new, upd = partition_rate_records([_record()], existing_keys=set(), now="T")
    assert upd == []
    assert len(new) == 1
    assert new[0]["review_status"] == "pending"
    assert new[0]["created_at"] == "T" and new[0]["updated_at"] == "T"


def test_partition_existing_row_omits_web_owned_columns():
    key = {("ember_egs_p1", "jw07076020001_04101_00001", "nrs1")}
    new, upd = partition_rate_records([_record()], existing_keys=key, now="T")
    assert new == []
    assert len(upd) == 1
    update = upd[0]
    # split ownership: the UPDATE must NOT carry any web-owned triage column
    for web_col in ("review_status", "mask_regions", "notes", "created_at"):
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


def _write_rate(path, rate_source="pool"):
    import numpy as np
    from astropy.io import fits
    from astropy.table import Table
    t = fits.BinTableHDU(Table({"wavelength": np.arange(3.0)}), name="MBKG1DR")
    t.header["MBKGPSRC"] = rate_source
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((4, 5), np.float32), name="SCI"), t]
                 ).writeto(path)


def test_records_carry_what_was_observed(tmp_path):
    import numpy as np
    from astropy.io import fits
    rate = tmp_path / "jw09214009001_03105_00001_nrs1_rate.fits"
    h = fits.Header()
    for k, v in dict(GRATING="G140M", FILTER="F100LP", EFFEXPTM=8753.334, **{"DATE-OBS": "2025-11-06"},
                     READPATT="NRSIRS2", NGROUPS=20, NINTS=6, PATT_NUM=1, NUMDTHPT=3).items():
        h[k] = v
    fits.HDUList([fits.PrimaryHDU(header=h), fits.ImageHDU(np.zeros((4, 5), np.float32), name="SCI")]
                 ).writeto(rate)
    r = build_rate_exposure_records([rate], observation="spurs_a2744", scope=Scope(obs="spurs_a2744"))[0]
    assert (r["grating"], r["filter"], r["date_obs"], r["readout_pattern"]) == (
        "G140M", "F100LP", "2025-11-06", "NRSIRS2")
    assert r["exposure_time"] == pytest.approx(8753.334)
    assert (r["n_groups"], r["n_integrations"], r["dither_position"], r["n_dither_positions"]) == (20, 6, 1, 3)
    # a re-deploy carries them in the UPDATE; a file without the cards leaves them out
    key = {("spurs_a2744", "jw09214009001_03105_00001", "nrs1")}
    _, upd = partition_rate_records([r], existing_keys=key, now="T")
    assert upd[0]["filter"] == "F100LP" and upd[0]["n_dither_positions"] == 3
    bare = tmp_path / "jw09214009001_03105_00002_nrs1_rate.fits"
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((4, 5), np.float32), name="SCI")]).writeto(bare)
    rb = build_rate_exposure_records([bare], observation="spurs_a2744", scope=Scope(obs="spurs_a2744"))[0]
    assert "filter" not in rb and "exposure_time" not in rb


def _write_mbkg(path, **cards):
    from astropy.io import fits
    h = fits.Header()
    for k, v in cards.items():
        h[k] = v
    fits.HDUList([fits.PrimaryHDU(header=h)]).writeto(path)


def test_records_carry_the_master_background(tmp_path):
    rate = tmp_path / "jw07076020001_04101_00001_nrs1_rate.fits"
    _write_rate(rate, "pool")
    _write_mbkg(tmp_path / "jw07076020001_04101_00001_nrs1_mbkg.fits",
                CFMBKGV="1.5", MBKGSRC="pool", MBKGPDEC="consistent",
                MBKGPMEM="1/1 1/2 2/1", MBKGNSH=164)
    (r,) = build_rate_exposure_records([rate], observation="ember_egs_p1",
                                       scope=Scope(obs="ember_egs_p1"))
    assert r["mbkg_storage_key"] == (
        "data/products/nirspec/ember_egs_p1/jw07076020001_04101_00001_nrs1_mbkg.fits")
    assert r["mbkg_source"] == "combined"
    assert r["mbkg_rate_source"] == "combined"
    assert r["mbkg_test"] == "consistent"
    assert r["mbkg_exposures"] == ["1/1", "1/2", "2/1"]
    assert r["mbkg_n_empty_shutters"] == 164
    assert r["mbkg_version"] == "1.5"
    assert (r["grating"], r["image_width"], r["image_height"]) == (None, 5, 4)


def test_master_background_labels(tmp_path):
    # an exposure that differs from the others keeps its own; one with too few empty
    # shutters of its own takes the combined one, and the test says why
    rate1 = tmp_path / "jw07076020001_04101_00001_nrs1_rate.fits"
    rate2 = tmp_path / "jw07076020001_04101_00002_nrs1_rate.fits"
    _write_rate(rate1, "exposure")
    _write_rate(rate2, "pool")
    _write_mbkg(tmp_path / "jw07076020001_04101_00001_nrs1_mbkg.fits", MBKGSRC="exposure",
                MBKGPDEC="differs 3.10-3.40 um +4.2 % (5.1 sig)", MBKGPMEM="none")
    _write_mbkg(tmp_path / "jw07076020001_04101_00002_nrs1_mbkg.fits", MBKGSRC="group",
                MBKGPDEC="consistent", MBKGPMEM="1/1")
    r1, r2 = build_rate_exposure_records([rate1, rate2], observation="o", scope=Scope(obs="o"))
    assert (r1["mbkg_source"], r1["mbkg_rate_source"]) == ("exposure", "exposure")
    assert r1["mbkg_test"].startswith("differs") and r1["mbkg_exposures"] == []
    assert (r2["mbkg_source"], r2["mbkg_test"]) == ("combined", "too few empty shutters")


def test_without_a_master_background_the_columns_are_left_out(tmp_path):
    rate = tmp_path / "jw07076020001_04101_00001_nrs1_rate.fits"
    _write_rate(rate, "pool")
    (r,) = build_rate_exposure_records([rate], observation="o", scope=Scope(obs="o"))
    assert "mbkg_storage_key" not in r and "mbkg_source" not in r
    assert r["mbkg_rate_source"] == "combined"         # step 1's tables are in the rate file
    # and a re-deploy's update never writes absent master-background columns
    _, (upd,) = partition_rate_records([r], existing_keys={("o", r["exposure_root"], "nrs1")},
                                       now="T")
    assert "mbkg_storage_key" not in upd and upd["mbkg_rate_source"] == "combined"


def test_update_carries_the_master_background(tmp_path):
    rec = _record(mbkg_storage_key="k", mbkg_source="combined", mbkg_n_empty_shutters=3)
    _, (upd,) = partition_rate_records(
        [rec], existing_keys={(rec["observation"], rec["exposure_root"], rec["detector"])}, now="T")
    assert upd["mbkg_storage_key"] == "k" and upd["mbkg_source"] == "combined"
    assert upd["mbkg_n_empty_shutters"] == 3
    assert "review_status" not in upd
