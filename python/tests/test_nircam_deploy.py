"""Unit tests for the NIRCam canonical-exposure deploy (epic #261, N1).

Pure/local: the science-only content hash (SCI+DQ, stable across a header-only
re-save), the astrometric wcs_hash that goes with it (so a re-alignment is not
mistaken for a re-save), canonical FITS/expmap upload-task keys, the registry row
identity for a NIRCam exposure (field-scoped, admin-only, carrying both digests +
an exposure_ref), and digest threading through build_registry_rows. The full
upload + registry round-trip is exercised live against local Supabase in CI.
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


def test_sci_dq_hash_includes_cfmask(tmp_path):
    # A manual-mask edit (N7 records it as a CFMASK extension on the canonical,
    # without touching SCI/DQ) must still change the digest so the exposure
    # re-uploads; an un-masked exposure (no CFMASK) hashes as before.
    sci = np.arange(16, dtype="float32").reshape(4, 4)
    dq = np.zeros((4, 4), dtype="int32")
    plain = _make_exposure(tmp_path / "p.fits", sci, dq)
    masked = _make_exposure(tmp_path / "m.fits", sci, dq)
    cf = np.zeros((4, 4), dtype="uint8")
    cf[1, 1] = 1
    with fits.open(masked, mode="update") as hdul:
        hdul.append(fits.ImageHDU(data=cf, name="CFMASK"))
        hdul.flush()
    assert nc._sci_dq_hash(masked) != nc._sci_dq_hash(plain)


# --- wcs_hash (the astrometric half of the exposure identity) ----------------

def _wcs_cards(crval1=150.0, crval2=2.0):
    return {
        "CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN",
        "CRPIX1": 1024.0, "CRPIX2": 1024.0,
        "CRVAL1": crval1, "CRVAL2": crval2,
        "CD1_1": -8.6e-6, "CD1_2": 0.0, "CD2_1": 0.0, "CD2_2": 8.6e-6,
    }


def _make_wcs_exposure(path, sci, dq, wcs_cards, *, extra_header=None):
    p = _make_exposure(path, sci, dq, extra_header=extra_header)
    with fits.open(p, mode="update") as hdul:
        for k, v in wcs_cards.items():
            hdul["SCI"].header[k] = v
        hdul.flush()
    return p


def test_wcs_hash_moves_when_the_solution_moves(tmp_path):
    from campfire.storage.hashing import wcs_hash
    sci = np.arange(16, dtype="float32").reshape(4, 4)
    dq = np.zeros((4, 4), dtype="int32")
    a = _make_wcs_exposure(tmp_path / "a.fits", sci, dq, _wcs_cards())
    # A re-alignment: same pixels, corrected reference position.
    b = _make_wcs_exposure(tmp_path / "b.fits", sci, dq,
                           _wcs_cards(crval1=150.0001))
    assert nc._sci_dq_hash(a) == nc._sci_dq_hash(b)   # science is identical...
    assert wcs_hash(a) != wcs_hash(b)                 # ...astrometry is not.
    assert wcs_hash(a).startswith("sha256:")


def test_wcs_hash_stable_across_a_plain_resave(tmp_path):
    # The property that makes a partial digest usable at all: nothing a re-save
    # touches (timestamps, HISTORY, card order) may move it.
    from campfire.storage.hashing import wcs_hash
    sci = np.arange(16, dtype="float32").reshape(4, 4)
    dq = np.zeros((4, 4), dtype="int32")
    p = _make_wcs_exposure(tmp_path / "a.fits", sci, dq, _wcs_cards())
    before = wcs_hash(p)
    with fits.open(p, mode="update") as hdul:
        hdul[0].header["DATE"] = "2026-07-27T12:00:00"
        hdul[0].header["HISTORY"] = "resaved"
        hdul["SCI"].header["BUNIT"] = "MJy/sr"
        hdul.flush()
    assert wcs_hash(p) == before


def test_wcs_hash_covers_sip_distortion(tmp_path):
    # update_fits_wcsinfo re-fits the SIP approximation from the corrected gwcs
    # on every solve, so a distortion-only change is a real astrometric change
    # that must not slip past the digest.
    from campfire.storage.hashing import wcs_hash
    sci = np.arange(16, dtype="float32").reshape(4, 4)
    dq = np.zeros((4, 4), dtype="int32")
    cards = dict(_wcs_cards(), A_ORDER=2, A_0_2=1e-7, B_ORDER=2, B_2_0=1e-7)
    a = _make_wcs_exposure(tmp_path / "a.fits", sci, dq, cards)
    b = _make_wcs_exposure(tmp_path / "b.fits", sci, dq,
                           dict(cards, A_0_2=2e-7))
    assert wcs_hash(a) != wcs_hash(b)


def test_wcs_hash_none_without_wcs_cards(tmp_path):
    # No WCS → no astrometric component. Must be None, never a digest over an
    # empty card set, so it can't compare equal to some other WCS-less file.
    from campfire.storage.hashing import wcs_hash
    p = _make_exposure(tmp_path / "e.fits", np.zeros((4, 4), "float32"),
                       np.zeros((4, 4), "int32"))
    assert wcs_hash(p) is None


def test_exposure_identity_matches_the_separate_digests(tmp_path):
    # exposure_identity() reads both from one open; it must agree exactly with
    # the two standalone readers.
    from campfire.storage.hashing import exposure_identity, wcs_hash
    sci = np.arange(16, dtype="float32").reshape(4, 4)
    dq = np.zeros((4, 4), dtype="int32")
    p = _make_wcs_exposure(tmp_path / "a.fits", sci, dq, _wcs_cards())
    assert exposure_identity(p) == (nc._sci_dq_hash(p), wcs_hash(p))


def test_exposure_identity_matches_pipeline_recipe(tmp_path):
    # The client re-implements the pipeline's digests (it must not import the
    # pipeline). The two copies drifting apart would silently churn uploads, so
    # pin them together here.
    pipeline_manifest = pytest.importorskip(
        "campfire_pipeline.nircam.manifest",
        reason="campfire-pipeline not installed")
    from campfire.storage.hashing import exposure_identity
    sci = np.arange(16, dtype="float32").reshape(4, 4)
    dq = np.zeros((4, 4), dtype="int32")
    p = _make_wcs_exposure(tmp_path / "a.fits", sci, dq, _wcs_cards())
    assert exposure_identity(str(p)) == (
        pipeline_manifest.compute_file_hash(str(p)),
        pipeline_manifest.compute_wcs_hash(str(p)),
    )


# --- upload-task builders ---------------------------------------------------

def test_build_fits_upload_tasks_uses_canonical_keys(tmp_path):
    path = tmp_path / f"{ROOT}.fits"
    path.write_bytes(b"\x00")
    exposures = {("f444w", ROOT): {"path": path, "filter": "f444w", "basename": ROOT}}
    tasks = nc.build_fits_upload_tasks("cosmos", exposures)
    assert len(tasks) == 1
    assert tasks[0].r2_key == EXP_KEY
    assert tasks[0].content_type == "application/fits"


def test_read_metadata_flags_only_combine_stamps(tmp_path):
    # CFP_MASK (apply_mask on the canonical) is allowed; CFP_BPIX / CFP_OUT
    # (ensemble steps) mark a mutated canonical the freeze guard must reject.
    sci = np.zeros((4, 4), "float32")
    dq = np.zeros((4, 4), "int32")
    clean = _make_exposure(tmp_path / "clean.fits", sci, dq,
                           extra_header={"CFP_JHAT": "t", "CFP_MASK": "t"})
    stamped = _make_exposure(tmp_path / "stamped.fits", sci, dq,
                             extra_header={"CFP_JHAT": "t", "CFP_OUT": "t"})
    assert nc._read_exposure_metadata(clean)["combine_stamped"] is False
    assert nc._read_exposure_metadata(stamped)["combine_stamped"] is True


def test_build_fits_upload_tasks_refuses_combine_stamped(tmp_path):
    path = tmp_path / f"{ROOT}.fits"
    path.write_bytes(b"\x00")
    exposures = {
        ("f444w", ROOT): {"path": path, "filter": "f444w", "basename": ROOT,
                          "combine_stamped": True},
    }
    with pytest.raises(RuntimeError, match="combine-mutated"):
        nc.build_fits_upload_tasks("cosmos", exposures)


def test_discover_expmap_tasks_deploys_only_fiducial(tmp_path):
    # Expmaps live in the canonical per-filter dir (alongside mosaics), keyed off an
    # ``expmap_`` filename prefix so they carry a real filter. Only the fiducial map
    # (undecorated ``expmap_<field>_<filter>.fits``) deploys.
    products = tmp_path / "products" / "nircam" / "cosmos"
    (products / "f444w").mkdir(parents=True)
    (products / "f444w" / "expmap_cosmos_f444w.fits").write_bytes(b"\x00")
    # A non-expmap FITS in the same dir must not be picked up by the expmap glob.
    (products / "f444w" / "jw01_00001_nrcalong.fits").write_bytes(b"\x00")
    dirs = {"products": products}
    tasks = nc.discover_expmap_tasks(dirs, "cosmos", ["f444w"])
    assert len(tasks) == 1
    assert tasks[0].r2_key == \
        "data/products/nircam/cosmos/f444w/expmap_cosmos_f444w.fits"


def test_discover_expmap_tasks_excludes_nonfiducial_stages(tmp_path):
    # The reducer-only ``_uncal`` quick-look and any pre-rename ``_canonical``
    # leftover must NOT deploy — only the undecorated fiducial map ships.
    products = tmp_path / "products" / "nircam" / "cosmos"
    (products / "f444w").mkdir(parents=True)
    (products / "f444w" / "expmap_cosmos_f444w.fits").write_bytes(b"\x00")
    (products / "f444w" / "expmap_cosmos_f444w_uncal.fits").write_bytes(b"\x00")
    (products / "f444w" / "expmap_cosmos_f444w_canonical.fits").write_bytes(b"\x00")
    dirs = {"products": products}
    tasks = nc.discover_expmap_tasks(dirs, "cosmos", ["f444w"])
    assert [t.r2_key for t in tasks] == \
        ["data/products/nircam/cosmos/f444w/expmap_cosmos_f444w.fits"]


def test_discover_expmap_tasks_empty_when_absent(tmp_path):
    dirs = {"products": tmp_path / "nope"}
    assert nc.discover_expmap_tasks(dirs, "cosmos", ["f444w"]) == []


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
        deployment_id=42, sci_dq_hash="sha256:" + "b" * 64,
        wcs_hash="sha256:" + "d" * 64)
    assert row["product_type"] == "nircam_exposure"
    assert row["instrument"] == "nircam"
    assert row["field"] == "cosmos"
    assert row["filter"] == "f444w"
    assert row["observation"] is None
    assert row["spectrum_id"] is None       # not a spectrum
    assert row["deployment_id"] == 42        # tagged with the field deployment
    assert row["exposure_ref"] == ROOT       # one active row per exposure
    assert row["sci_dq_hash"] == "sha256:" + "b" * 64
    assert row["wcs_hash"] == "sha256:" + "d" * 64


def test_row_sci_dq_hash_none_by_default():
    # An expmap (and every non-exposure product) carries no science digest, but
    # does carry its per-filter scope column now that it lives in the filter dir.
    row = reg.row_for_key(
        "data/products/nircam/cosmos/f444w/expmap_cosmos_f444w.fits",
        backend="osn", content_hash="sha256:" + "a" * 64, size_bytes=1,
        content_type="application/fits")
    assert row["product_type"] == "nircam_expmap"
    assert row["field"] == "cosmos"
    assert row["filter"] == "f444w"
    assert row["sci_dq_hash"] is None
    assert row["wcs_hash"] is None


def test_build_registry_rows_threads_sci_dq_hashes(tmp_path):
    p = tmp_path / f"{ROOT}.fits"
    p.write_bytes(b"\x00" * 512)
    tasks = [UploadTask(p, EXP_KEY, "application/fits")]
    rows = reg.build_registry_rows(
        tasks, backend="osn", succeeded_keys={EXP_KEY},
        sci_dq_hashes={EXP_KEY: "sha256:" + "c" * 64},
        wcs_hashes={EXP_KEY: "sha256:" + "d" * 64})
    assert len(rows) == 1
    assert rows[0]["content_hash"].startswith("sha256:")   # whole-file digest
    assert rows[0]["content_hash"] != "sha256:" + "c" * 64  # not the sci_dq one
    assert rows[0]["sci_dq_hash"] == "sha256:" + "c" * 64
    # Both identity components ride the same row; a row registered without the
    # astrometric one would dedup as unchanged after a re-alignment.
    assert rows[0]["wcs_hash"] == "sha256:" + "d" * 64


# --- mosaic deploy (N2) -----------------------------------------------------

def _write_manifest(filter_dir, base, *, field, filt, tile, scale):
    import json
    (filter_dir / f"{base}_manifest.json").write_text(json.dumps({
        "mosaic_name": base, "field": field, "filter": filt,
        "tile": tile, "pixel_scale": scale,
    }))


def test_discover_mosaics_manifest_based_version_free_keys(tmp_path):
    # A version-free mosaic slot with i2d + split extensions, discovered via its
    # manifest and keyed under canonical nircam_mosaic keys.
    fdir = tmp_path / "products" / "nircam" / "cosmos" / "f444w"
    fdir.mkdir(parents=True)
    base = "mosaic_nircam_f444w_cosmos_30mas_A1"
    _write_manifest(fdir, base, field="cosmos", filt="f444w", tile="A1", scale="30mas")
    for suffix in ("_i2d.fits", "_sci.fits", "_err.fits", "_wht.fits"):
        (fdir / f"{base}{suffix}").write_bytes(b"\x00")
    # a stray split with no i2d/manifest twin must be ignored
    (fdir / "orphan_sci.fits").write_bytes(b"\x00")

    dirs = {"products": tmp_path / "products" / "nircam" / "cosmos"}
    found = nc.discover_mosaics(dirs, "cosmos", ["f444w"])
    exts = sorted(m["extension"] for m in found)
    assert exts == ["err", "i2d", "sci", "wht"]
    i2d = next(m for m in found if m["extension"] == "i2d")
    assert i2d["tile"] == "A1" and i2d["pixel_scale"] == "30mas"
    # Mosaic FITS are stored gzipped: the cloud key gains '.gz' and the task
    # carries application/gzip (the local .fits path is unchanged).
    assert i2d["storage_key"] == \
        "data/products/nircam/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_A1_i2d.fits.gz"
    assert i2d["content_type"] == "application/gzip"
    # every discovered key registers as nircam_mosaic (no version segment); the
    # registry parses the .fits.gz cloud key back to the mosaic product.
    row = reg.row_for_key(i2d["storage_key"], backend="osn",
                          content_hash="sha256:" + "a" * 64, size_bytes=1,
                          content_type="application/gzip")
    assert row["product_type"] == "nircam_mosaic"
    assert row["field"] == "cosmos"


def test_deployable_mosaics_drops_i2d(tmp_path):
    # i2d stays *discoverable* — provenance stamping and the FitsGL pyramid read
    # it locally — but it must never reach the cloud upload / nircam_images index
    # set. deployable_mosaics is the filter every cloud-bound path applies.
    fdir = tmp_path / "products" / "nircam" / "cosmos" / "f444w"
    fdir.mkdir(parents=True)
    base = "mosaic_nircam_f444w_cosmos_30mas_A1"
    _write_manifest(fdir, base, field="cosmos", filt="f444w", tile="A1", scale="30mas")
    for suffix in ("_i2d.fits", "_sci.fits", "_err.fits", "_wht.fits"):
        (fdir / f"{base}{suffix}").write_bytes(b"\x00")

    dirs = {"products": tmp_path / "products" / "nircam" / "cosmos"}
    found = nc.discover_mosaics(dirs, "cosmos", ["f444w"])
    # discovery still surfaces i2d (local consumers depend on it)...
    assert "i2d" in {m["extension"] for m in found}
    # ...but the deploy set excludes it, keeping the split extensions.
    deployable = nc.deployable_mosaics(found)
    assert sorted(m["extension"] for m in deployable) == ["err", "sci", "wht"]
    assert all(m["extension"] != "i2d" for m in deployable)
    # a mosaic with only an i2d on disk deploys nothing (no split science product)
    assert nc.deployable_mosaics(
        [{"extension": "i2d", "path": "x", "storage_key": "k"}]) == []


def test_discover_mosaics_skips_stale_versioned_manifest(tmp_path):
    # A field re-reduced after N2 keeps its pre-N2 `..._v0_1_..._manifest.json`
    # on disk next to the new version-free one. discover_mosaics must accept ONLY
    # the canonical version-free slot — else it emits a duplicate (field, tile,
    # filter, scale, extension) row that crashes the batch upsert and re-uploads
    # a version-bearing key to OSN.
    fdir = tmp_path / "products" / "nircam" / "cosmos" / "f444w"
    fdir.mkdir(parents=True)
    canon = "mosaic_nircam_f444w_cosmos_30mas_A1"
    stale = "mosaic_nircam_f444w_cosmos_30mas_v0_1_A1"
    for base in (canon, stale):
        _write_manifest(fdir, base, field="cosmos", filt="f444w", tile="A1", scale="30mas")
        (fdir / f"{base}_i2d.fits").write_bytes(b"\x00")

    dirs = {"products": tmp_path / "products" / "nircam" / "cosmos"}
    found = nc.discover_mosaics(dirs, "cosmos", ["f444w"])
    assert len(found) == 1
    assert found[0]["storage_key"].endswith(f"{canon}_i2d.fits.gz")
    # no version segment leaked into any discovered key
    assert all("_v0_1_" not in m["storage_key"] for m in found)


def test_discover_mosaics_multiunderscore_field(tmp_path):
    # Field name with underscores survives (manifest-driven, not positional).
    fdir = tmp_path / "products" / "nircam" / "ember_egs_p1" / "f356w"
    fdir.mkdir(parents=True)
    base = "mosaic_nircam_f356w_ember_egs_p1_30mas_t2"
    _write_manifest(fdir, base, field="ember_egs_p1", filt="f356w", tile="t2", scale="30mas")
    (fdir / f"{base}_i2d.fits").write_bytes(b"\x00")
    dirs = {"products": tmp_path / "products" / "nircam" / "ember_egs_p1"}
    found = nc.discover_mosaics(dirs, "ember_egs_p1", ["f356w"])
    assert len(found) == 1
    assert found[0]["tile"] == "t2"
    assert found[0]["storage_key"].endswith(
        "ember_egs_p1/f356w/mosaic_nircam_f356w_ember_egs_p1_30mas_t2_i2d.fits.gz")


# --- parallel sci_dq hashing + upload-worker tuning (deploy speed) -----------

def test_compute_sci_dq_hashes_matches_serial(tmp_path):
    sci = np.arange(16, dtype="float32").reshape(4, 4)
    dq = np.zeros((4, 4), dtype="int32")
    tasks = []
    for i in range(5):
        p = _make_exposure(tmp_path / f"jw_{i}.fits", sci + i, dq)
        tasks.append(UploadTask(local_path=p, r2_key=f"k{i}", content_type="application/fits"))
    serial = {t.r2_key: nc._sci_dq_hash(t.local_path) for t in tasks}
    assert nc._compute_sci_dq_hashes(tasks) == serial
    assert nc._compute_sci_dq_hashes([]) == {}


def test_upload_workers_env_override(monkeypatch):
    monkeypatch.delenv("CAMPFIRE_DEPLOY_UPLOAD_WORKERS", raising=False)
    assert nc._upload_workers() == 16
    monkeypatch.setenv("CAMPFIRE_DEPLOY_UPLOAD_WORKERS", "24")
    assert nc._upload_workers() == 24
    monkeypatch.setenv("CAMPFIRE_DEPLOY_UPLOAD_WORKERS", "0")
    assert nc._upload_workers() == 1            # clamped to >= 1
    monkeypatch.setenv("CAMPFIRE_DEPLOY_UPLOAD_WORKERS", "not-a-number")
    assert nc._upload_workers() == 16           # falls back to default
