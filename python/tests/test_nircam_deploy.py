"""Unit tests for the NIRCam canonical-exposure deploy (epic #261, N1).

Pure/local: the science-only content hash (SCI+DQ, stable across a header-only
re-save), canonical FITS/expmap upload-task keys, the registry row identity for a
NIRCam exposure (field-scoped, admin-only, carrying a sci_dq_hash + exposure_ref),
and sci_dq_hash threading through build_registry_rows. The full upload + registry
round-trip is exercised live against local Supabase in CI.
"""
import numpy as np
import pytest
from astropy.io import fits

from campfire.deploy import nircam as nc
from campfire.deploy import registry as reg
from campfire.deploy.r2 import UploadTask
from campfire_layout import KeyScheme, Scope, storage_key

ROOT = "jw01727028001_04101_00003_nrcalong"
EXP_KEY = f"data/products/nircam/cosmos/f444w/{ROOT}.fits"


def _make_exposure(path, sci, dq, *, extra_header=None, with_sci=True, with_dq=True):
    hdus = [fits.PrimaryHDU()]
    for k, v in (extra_header or {}).items():
        hdus[0].header[k] = v
    if with_sci:
        hdus.append(fits.ImageHDU(data=sci.astype("float32"), name="SCI"))
    if with_dq:
        hdus.append(fits.ImageHDU(data=dq.astype("int32"), name="DQ"))
    fits.HDUList(hdus).writeto(path, overwrite=True)
    return path


# --- _sci_dq_hash -----------------------------------------------------------

def test_sci_dq_hash_prefixed_and_stable_over_header_change(tmp_path):
    sci = np.arange(16, dtype="float32").reshape(4, 4)
    dq = np.zeros((4, 4), dtype="int32")
    a = _make_exposure(tmp_path / "a.fits", sci, dq)
    # Same SCI+DQ, different primary header (mimics a pipeline re-save that only
    # bumps timestamps) → identical digest.
    b = _make_exposure(tmp_path / "b.fits", sci, dq, extra_header={"DATE": "2026-07-01"})
    ha, hb = nc._sci_dq_hash(a), nc._sci_dq_hash(b)
    assert ha.startswith("sha256:") and len(ha) == len("sha256:") + 64
    assert ha == hb


def test_sci_dq_hash_changes_when_science_changes(tmp_path):
    dq = np.zeros((4, 4), dtype="int32")
    a = _make_exposure(tmp_path / "a.fits", np.zeros((4, 4), "float32"), dq)
    c = _make_exposure(tmp_path / "c.fits", np.ones((4, 4), "float32"), dq)
    assert nc._sci_dq_hash(a) != nc._sci_dq_hash(c)
    # A DQ change also flips it.
    d = _make_exposure(tmp_path / "d.fits", np.zeros((4, 4), "float32"),
                       np.ones((4, 4), "int32"))
    assert nc._sci_dq_hash(a) != nc._sci_dq_hash(d)


def test_sci_dq_hash_none_without_sci_or_dq(tmp_path):
    p = tmp_path / "e.fits"
    fits.HDUList([fits.PrimaryHDU()]).writeto(p, overwrite=True)
    assert nc._sci_dq_hash(p) is None


# --- upload-task builders ---------------------------------------------------

def test_build_fits_upload_tasks_uses_canonical_keys(tmp_path):
    path = tmp_path / f"{ROOT}.fits"
    path.write_bytes(b"\x00")
    exposures = {("f444w", ROOT): {"path": path, "filter": "f444w", "basename": ROOT}}
    tasks = nc.build_fits_upload_tasks("cosmos", exposures)
    assert len(tasks) == 1
    assert tasks[0].r2_key == EXP_KEY
    assert tasks[0].content_type == "application/fits"


def test_discover_expmap_tasks_canonical_keys(tmp_path):
    expdir = tmp_path / "products" / "nircam" / "cosmos" / "expmaps"
    expdir.mkdir(parents=True)
    (expdir / "cosmos_f444w_expmap.fits").write_bytes(b"\x00")
    dirs = {"products": tmp_path / "products" / "nircam" / "cosmos"}
    tasks = nc.discover_expmap_tasks(dirs, "cosmos")
    assert len(tasks) == 1
    assert tasks[0].r2_key == "data/products/nircam/cosmos/expmaps/cosmos_f444w_expmap.fits"


def test_discover_expmap_tasks_empty_when_absent(tmp_path):
    dirs = {"products": tmp_path / "nope"}
    assert nc.discover_expmap_tasks(dirs, "cosmos") == []


# --- registry identity ------------------------------------------------------

def test_exposure_ref_for_nircam_exposure():
    assert reg._exposure_ref_for("nircam_exposure", f"{ROOT}.fits") == ROOT
    # A preview PNG is not an exposure-level object → no ref.
    assert reg._exposure_ref_for("nircam_exposure_preview", f"{ROOT}_preview.png") is None


def test_row_for_nircam_exposure_identity_and_sci_dq():
    # Deploy tags the row with a field deployment_id (visibility rides
    # deployment.status); row_for_key defaults it to None when not supplied.
    row = reg.row_for_key(
        EXP_KEY, backend="osn", content_hash="sha256:" + "a" * 64,
        size_bytes=100, content_type="application/fits",
        deployment_id=42, sci_dq_hash="sha256:" + "b" * 64)
    assert row["product_type"] == "nircam_exposure"
    assert row["instrument"] == "nircam"
    assert row["field"] == "cosmos"
    assert row["observation"] is None
    assert row["spectrum_id"] is None       # not a spectrum
    assert row["deployment_id"] == 42        # tagged with the field deployment
    assert row["exposure_ref"] == ROOT       # one active row per exposure
    assert row["sci_dq_hash"] == "sha256:" + "b" * 64


def test_row_sci_dq_hash_none_by_default():
    # An expmap (and every non-exposure product) carries no science digest.
    row = reg.row_for_key(
        "data/products/nircam/cosmos/expmaps/cosmos_f444w_expmap.fits",
        backend="osn", content_hash="sha256:" + "a" * 64, size_bytes=1,
        content_type="application/fits")
    assert row["product_type"] == "nircam_expmap"
    assert row["sci_dq_hash"] is None


def test_build_registry_rows_threads_sci_dq_hashes(tmp_path):
    p = tmp_path / f"{ROOT}.fits"
    p.write_bytes(b"\x00" * 512)
    tasks = [UploadTask(p, EXP_KEY, "application/fits")]
    rows = reg.build_registry_rows(
        tasks, backend="osn", succeeded_keys={EXP_KEY},
        sci_dq_hashes={EXP_KEY: "sha256:" + "c" * 64})
    assert len(rows) == 1
    assert rows[0]["content_hash"].startswith("sha256:")   # whole-file digest
    assert rows[0]["content_hash"] != "sha256:" + "c" * 64  # not the sci_dq one
    assert rows[0]["sci_dq_hash"] == "sha256:" + "c" * 64
