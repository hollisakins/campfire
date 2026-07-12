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
