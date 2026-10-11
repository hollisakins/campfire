"""Science-content hash: pinned definition (P0b).

The three golden hashes below pin SCIENCE_HASH_VERSION = 1 for synthetic files
built deterministically in-test. If one of them changes, the identity every
stored ``science_hash`` relies on has changed: bump SCIENCE_HASH_VERSION, plan
a backfill, and only then update the constant.
"""
import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.metadata import science_hash as sh

GOLDEN_IMAGE = "sha256:4f4127e03ca55c4e616622cb72429b17f0255981cbc2f5041ffab94ed2a4cec7"
GOLDEN_MULTI = "sha256:ebe11a5d80df7067a54a791f2aa1036313f1f368cad22cc20e46da8821701a00"
GOLDEN_SCALED = "sha256:29fc111d9e602e1491f8cb701bbbdcde97de0ec330962ee074feea75e3f298ed"


def _image(path, seed=1, extra=None):
    rng = np.random.default_rng(seed)
    hdu = fits.PrimaryHDU(rng.normal(size=(8, 16)).astype(">f4"))
    hdu.header["CMPFRVER"] = "0.7.0"
    hdu.header["GRATING"] = "PRISM"
    hdu.header["EXPTIME"] = 1234.5
    hdu.header["CMPFRTIM"] = "2026-10-11T00:00:00+00:00"
    hdu.header["HISTORY"] = "made by test"
    hdu.header["COMMENT"] = "a comment"
    for k, v in (extra or {}).items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)
    return path


def _multi(path, seed=2):
    rng = np.random.default_rng(seed)
    primary = fits.PrimaryHDU()
    primary.header["CMPFRVER"] = "0.7.0"
    primary.header["CMPFRTIM"] = "2026-10-11T00:00:00+00:00"
    sci = fits.ImageHDU(rng.normal(size=(4, 32)).astype(">f8"), name="SCI")
    sci.header["BUNIT"] = "MJy/sr"
    wave = np.linspace(0.6, 5.3, 32)
    flux = rng.normal(size=32)
    tab = fits.BinTableHDU.from_columns([
        fits.Column(name="WAVE", format="D", array=wave),
        fits.Column(name="FLUX", format="D", array=flux),
        fits.Column(name="NAME", format="8A", array=np.array(["a", "bb", "ccc"] + [""] * 29)),
    ], name="SPEC1D")
    fits.HDUList([primary, sci, tab]).writeto(path, overwrite=True)
    return path


def _scaled(path, seed=3):
    rng = np.random.default_rng(seed)
    data = rng.integers(0, 4000, size=(6, 6)).astype(">i2")
    hdu = fits.PrimaryHDU(data)
    hdu.header["BSCALE"] = 0.25
    hdu.header["BZERO"] = 100.0
    hdu.header["CMPFRTIM"] = "2026-10-11T00:00:00+00:00"
    hdu.writeto(path, overwrite=True)
    return path


def _rewrite_with(path, **cards):
    """Rewrite the primary header with extra/changed cards, data untouched."""
    with fits.open(path, mode="update", do_not_scale_image_data=True) as h:
        for k, v in cards.items():
            if v is None:
                del h[0].header[k]
            else:
                h[0].header[k] = v
        h.flush()


@pytest.mark.parametrize("builder,golden", [
    (_image, GOLDEN_IMAGE), (_multi, GOLDEN_MULTI), (_scaled, GOLDEN_SCALED),
])
def test_golden(tmp_path, builder, golden):
    p = builder(tmp_path / "f.fits")
    assert sh.science_hash(p) == golden


def test_volatile_cards_do_not_change_it(tmp_path):
    p = _image(tmp_path / "f.fits")
    before = sh.science_hash(p)
    whole_before = sh.file_hash(p)
    _rewrite_with(p, CMPFRTIM="2027-01-01T00:00:00+00:00", CMPFRDR="DR2026.1",
                  DATE="2027-01-01")
    with fits.open(p, mode="update") as h:          # add CHECKSUM/DATASUM too
        h[0].add_checksum()
        h.flush()
    assert sh.science_hash(p) == before
    assert sh.file_hash(p) != whole_before


def test_commentary_and_card_order_do_not_change_it(tmp_path):
    a = _image(tmp_path / "a.fits")
    b = _image(tmp_path / "b.fits")
    with fits.open(b, mode="update", do_not_scale_image_data=True) as h:
        hdr = h[0].header
        hdr["HISTORY"] = "another line"
        hdr["COMMENT"] = "another comment"
        # reorder two non-volatile cards
        g, e = hdr["GRATING"], hdr["EXPTIME"]
        del hdr["GRATING"], hdr["EXPTIME"]
        hdr["EXPTIME"] = e
        hdr["GRATING"] = g
        h.flush()
    assert sh.science_hash(a) == sh.science_hash(b)


def test_one_data_byte_changes_it(tmp_path):
    p = _image(tmp_path / "f.fits")
    before = sh.science_hash(p)
    with fits.open(p, mode="update", do_not_scale_image_data=True) as h:
        h[0].data[3, 5] += np.float32(1e-3)
        h.flush()
    assert sh.science_hash(p) != before


def test_one_science_card_changes_it(tmp_path):
    p = _image(tmp_path / "f.fits")
    before = sh.science_hash(p)
    _rewrite_with(p, EXPTIME=1234.6)
    assert sh.science_hash(p) != before
    _rewrite_with(p, EXPTIME=1234.5)
    assert sh.science_hash(p) == before


def test_scaling_cards_are_science(tmp_path):
    p = _scaled(tmp_path / "f.fits")
    before = sh.science_hash(p)
    _rewrite_with(p, BZERO=101.0)
    assert sh.science_hash(p) != before


def test_table_heap_and_strings_are_covered(tmp_path):
    p = _multi(tmp_path / "f.fits")
    before = sh.science_hash(p)
    with fits.open(p, mode="update") as h:
        h["SPEC1D"].data["NAME"][1] = "bx"
        h.flush()
    assert sh.science_hash(p) != before


def test_version_constant():
    assert sh.SCIENCE_HASH_VERSION == 1
    assert {"CMPFRTIM", "CMPFRDR", "DATE", "CHECKSUM", "DATASUM"} <= set(sh.VOLATILE_CARDS)
