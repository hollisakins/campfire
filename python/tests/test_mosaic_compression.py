"""Mosaic gzip compression: layout key policy + upload-side gzip contract.

Mosaic FITS are stored gzipped in the bucket ('.fits.gz' key) but plain on disk;
the pull decompress + hash-in-decompressed-space half is covered by
test_download_objects.test_downloads_compressed_mosaic_decompresses.
"""
import gzip
import hashlib
import zlib
from pathlib import Path

from campfire_layout import Scope, is_compressed_key, key_to_relpath, storage_key
from campfire_layout import KeyScheme as KS
from campfire.deploy.r2 import _upload_body

FIELD, FILT = "cosmos", "f444w"
SC = Scope(field=FIELD, filt=FILT)


def _key(fname, product="nircam_mosaic"):
    return storage_key(product, SC, fname, scheme=KS.CANONICAL)


def test_layout_only_mosaic_fits_are_compressed():
    # FITS extensions gain '.gz'; the JSON manifest and PNG thumbnail do not.
    for fn in ("mosaic_x_sci.fits", "mosaic_x_i2d.fits", "mosaic_x_err.fits"):
        k = _key(fn)
        assert k.endswith(".fits.gz"), k
        assert is_compressed_key(k)
    manifest = _key("mosaic_x_manifest.json")
    assert manifest.endswith("_manifest.json") and not is_compressed_key(manifest)
    thumb = _key("mosaic_x_thumb.png", product="nircam_mosaic_thumbnail")
    assert thumb.endswith("_thumb.png") and not is_compressed_key(thumb)


def test_layout_gz_key_maps_back_to_plain_local_path():
    k = _key("mosaic_x_sci.fits")  # -> ...sci.fits.gz
    # the local tree stays plain: key_to_relpath strips the .gz
    assert key_to_relpath(k).endswith("mosaic_x_sci.fits")
    assert not key_to_relpath(k).endswith(".gz")


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_upload_body_passthrough_when_not_compressed(tmp_path):
    p = _write(tmp_path / "x.fits", b"hello world" * 100)
    with _upload_body(p, compress=False) as body:
        assert body == p  # no temp file, original path yielded


def test_upload_body_gzips_deterministically_and_inflates(tmp_path):
    # 50%-NaN-ish payload: a run of zeros (compresses) + random tail (does not).
    payload = b"\x00" * 4096 + hashlib.sha256(b"seed").digest() * 128
    p = _write(tmp_path / "mosaic_sci.fits", payload)

    with _upload_body(p, compress=True) as body:
        assert body != p and body.exists()
        gz1 = body.read_bytes()
        # deterministic: a second pass is byte-identical (mtime=0)
        with _upload_body(p, compress=True) as body2:
            assert body2.read_bytes() == gz1
    # temp removed on context exit
    assert not body.exists()

    # what we PUT inflates (via the pull-side gzip inflate) back to the original,
    # and its sha256 matches the plain file's — identity lives in decompressed space
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    inflated = d.decompress(gz1) + d.flush()
    assert inflated == payload
    assert hashlib.sha256(inflated).hexdigest() == hashlib.sha256(payload).hexdigest()
    # and it is a standard gzip stream (any gunzip / astropy reads it)
    assert gzip.decompress(gz1) == payload


def test_upload_body_cleans_up_temp_on_error(tmp_path):
    p = _write(tmp_path / "mosaic_sci.fits", b"data" * 256)
    leaked = None
    try:
        with _upload_body(p, compress=True) as body:
            leaked = body
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert leaked is not None and not leaked.exists()  # finally-unlinked
    # no stray .gz.tmp left in the dir
    assert not any(f.name.endswith(".gz.tmp") for f in tmp_path.iterdir())


# --- stored_size_bytes capture (registry transport-size column) -------------

class _FakeS3:
    """Captures upload_file calls; records the staged body's size."""
    def __init__(self):
        self.calls = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.calls.append((Path(filename).stat().st_size, bucket, key))


def test_upload_to_r2_returns_stored_gz_size(tmp_path):
    from campfire.deploy.r2 import upload_to_r2
    src = tmp_path / "mosaic_x_sci.fits"
    src.write_bytes(b"\x00" * 100_000)  # highly compressible

    fake = _FakeS3()
    stored = upload_to_r2(fake, "bucket", src, _key("mosaic_x_sci.fits"),
                          "application/gzip", compress=True)
    # returned value == the gz body actually PUT, and much smaller than source
    assert stored == fake.calls[0][0]
    assert 0 < stored < 100_000

    # verbatim upload: no stored size (stored bytes == logical size_bytes)
    assert upload_to_r2(fake, "bucket", src, "data/x.fits",
                        "application/fits", compress=False) is None


def test_direct_wrapper_collects_stored_sizes(tmp_path):
    from campfire.deploy.r2 import UploadTask, upload_files_direct
    fits = tmp_path / "mosaic_x_sci.fits"
    fits.write_bytes(b"\x00" * 50_000)
    png = tmp_path / "mosaic_x_thumb.png"
    png.write_bytes(b"\x01" * 500)

    gz_key = _key("mosaic_x_sci.fits")  # .fits.gz -> compressed
    png_key = _key("mosaic_x_thumb.png", product="nircam_mosaic_thumbnail")

    stored: dict[str, int] = {}
    ok, failed, _ = upload_files_direct(
        _FakeS3(), "bucket",
        [UploadTask(fits, gz_key, "application/gzip"),
         UploadTask(png, png_key, "image/png")],
        max_workers=2, stored_sizes_out=stored)
    assert (ok, failed) == (2, 0)
    assert set(stored) == {gz_key}          # only the compressed product
    assert 0 < stored[gz_key] < 50_000


def test_registry_row_carries_stored_size_bytes(tmp_path):
    from campfire.deploy.r2 import UploadTask
    from campfire.deploy.registry import build_registry_rows, row_for_key

    row = row_for_key(_key("mosaic_x_sci.fits"), backend="osn",
                      content_hash="sha256:0", size_bytes=100,
                      content_type="application/gzip", stored_size_bytes=42)
    assert row["stored_size_bytes"] == 42
    assert row["size_bytes"] == 100  # logical size untouched

    # default: NULL (stored verbatim)
    row = row_for_key(_key("mosaic_x_sci.fits"), backend="osn",
                      content_hash="sha256:0", size_bytes=100,
                      content_type="application/gzip")
    assert row["stored_size_bytes"] is None

    # build_registry_rows threads the per-key map through
    fits = tmp_path / "mosaic_x_sci.fits"
    fits.write_bytes(b"\x00" * 10)
    gz_key = _key("mosaic_x_sci.fits")
    rows = build_registry_rows(
        [UploadTask(fits, gz_key, "application/gzip")],
        backend="osn", stored_sizes={gz_key: 7})
    assert rows[0]["stored_size_bytes"] == 7
