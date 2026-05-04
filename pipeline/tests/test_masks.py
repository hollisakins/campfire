"""Tests for the manual masking module (campfire_pipeline.nirspec.masks).

Targets the pure-Python pieces (parsing, hashing, TOML round-trip,
sentinel reads/writes, in-rate extension I/O) that don't require a
real JWST rate file. End-to-end DQ/bkg-sub behavior is verified in
the project README's manual workflow.
"""

import os
import tempfile

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nirspec import masks


class TestCanonicalizeAndHash:
    def test_empty_inputs_hash_to_empty(self):
        assert masks.canonicalize(None) == ""
        assert masks.canonicalize("") == ""
        assert masks.canonicalize("   \n  ") == ""
        assert masks.hash_mask(None) == ""
        assert masks.hash_mask("") == ""

    def test_canonicalize_strips_comments_and_blanks(self):
        raw = "# DS9 region file\nimage\n\npolygon(1,2,3,4,5,6)\n# trailing comment\n"
        assert masks.canonicalize(raw) == "image\npolygon(1,2,3,4,5,6)"

    def test_hash_is_stable(self):
        raw = "image\npolygon(10,20,30,40,50,60)\n"
        assert masks.hash_mask(raw) == masks.hash_mask(raw + "\n# comment\n")

    def test_hash_length_is_12(self):
        h = masks.hash_mask("image\npolygon(0,0,10,0,10,10)")
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)


class TestParseRegions:
    def test_polygon_rasterizes_correctly(self):
        # Square polygon (2,2)-(8,2)-(8,8)-(2,8) in image coords.
        reg = "image\npolygon(2,2,8,2,8,8,2,8)"
        mask = masks.parse_regions_to_mask(reg, (10, 10))
        assert mask.sum() > 0
        # Center of the square should be flagged.
        assert mask[5, 5]
        # Corner outside should not.
        assert not mask[0, 0]

    def test_empty_string_returns_empty_mask(self):
        mask = masks.parse_regions_to_mask("", (10, 10))
        assert mask.shape == (10, 10)
        assert mask.sum() == 0

    def test_implicit_image_system(self):
        # No 'image' header line — should still parse.
        reg = "polygon(2,2,8,2,8,8,2,8)"
        mask = masks.parse_regions_to_mask(reg, (10, 10))
        assert mask[5, 5]


class TestSentinelRoundTrip:
    def _make_rate(self, path):
        # Minimal FITS file with a primary HDU + SCI image extension.
        hdu_p = fits.PrimaryHDU()
        hdu_p.header["INSTRUME"] = "NIRSPEC"
        hdu_s = fits.ImageHDU(data=np.zeros((20, 20), dtype=np.float32), name="SCI")
        fits.HDUList([hdu_p, hdu_s]).writeto(path, overwrite=True)

    def test_stamp_and_read(self, tmp_path):
        rate = str(tmp_path / "jw00001001001_nrs1_rate.fits")
        self._make_rate(rate)
        assert masks.read_sentinel(rate) == ""
        masks.stamp_sentinel(rate, "image\npolygon(1,2,3,4,5,6)", n_pixels=42)
        sha = masks.read_sentinel(rate)
        assert sha == masks.hash_mask("image\npolygon(1,2,3,4,5,6)")

    def test_empty_clears_sentinel(self, tmp_path):
        rate = str(tmp_path / "rate.fits")
        self._make_rate(rate)
        masks.stamp_sentinel(rate, "image\npolygon(1,2,3,4,5,6)", n_pixels=42)
        assert masks.read_sentinel(rate)
        masks.stamp_sentinel(rate, None, n_pixels=0)
        assert masks.read_sentinel(rate) == ""

    def test_is_stale(self, tmp_path):
        rate = str(tmp_path / "rate.fits")
        self._make_rate(rate)
        reg = "image\npolygon(1,2,3,4,5,6)"
        assert masks.is_stale(rate, reg) is True  # nothing stamped yet
        masks.stamp_sentinel(rate, reg, n_pixels=10)
        assert masks.is_stale(rate, reg) is False
        assert masks.is_stale(rate, "image\npolygon(7,8,9,10,11,12)") is True


class TestTomlRoundTrip:
    def test_write_and_delete(self, tmp_path):
        toml_path = tmp_path / "observations.toml"
        toml_path.write_text(
            "# top-of-file comment\n"
            "[demo]\n"
            "    field = 'cosmos'\n"
            "    program = 'p'\n"
            "    data_subdir = '0'\n"
            "    files = ['jw00000000001*']\n"
        )
        masks.write_masks_to_observations_toml(
            str(toml_path), "demo",
            {"jw00000000001_nrs1": "image\npolygon(1,2,3,4,5,6)"},
        )
        text = toml_path.read_text()
        assert "top-of-file comment" in text  # tomlkit preserved the comment
        assert "polygon(1,2,3,4,5,6)" in text
        assert "[demo.masks]" in text or "demo.masks" in text

        # Now delete it.
        masks.write_masks_to_observations_toml(
            str(toml_path), "demo", {"jw00000000001_nrs1": None},
        )
        text2 = toml_path.read_text()
        assert "polygon" not in text2

    def test_write_requires_existing_obs(self, tmp_path):
        toml_path = tmp_path / "observations.toml"
        toml_path.write_text("[other]\nfield = 'x'\n")
        with pytest.raises(ValueError):
            masks.write_masks_to_observations_toml(
                str(toml_path), "missing", {"foo": "image\npolygon(0,0,1,0,1,1)"},
            )


class TestApplyAndClearDQ:
    def _make_rate_with_dq(self, path, dq_init):
        # Build a 4-extension FITS that ImageModel will accept (SCI/ERR/DQ/VAR_RNOISE).
        # For the purposes of these tests, only DQ is exercised; we use small
        # arrays and let the JWST datamodel attach defaults to the rest.
        n = dq_init.shape[0]
        zeros = np.zeros((n, n), dtype=np.float32)
        hdr = fits.Header()
        hdr["TELESCOP"] = "JWST"
        hdr["INSTRUME"] = "NIRSPEC"
        hdr["DETECTOR"] = "NRS1"
        hdul = fits.HDUList([
            fits.PrimaryHDU(header=hdr),
            fits.ImageHDU(data=zeros, name="SCI"),
            fits.ImageHDU(data=zeros, name="ERR"),
            fits.ImageHDU(data=dq_init.astype(np.uint32), name="DQ"),
            fits.ImageHDU(data=zeros, name="VAR_POISSON"),
            fits.ImageHDU(data=zeros, name="VAR_RNOISE"),
        ])
        hdul.writeto(path, overwrite=True)

    def test_apply_then_clear_round_trip(self, tmp_path):
        # Skip if jwst is unavailable.
        pytest.importorskip("jwst")

        rate = str(tmp_path / "jw00000000001_nrs1_rate.fits")
        n = 32
        dq = np.zeros((n, n), dtype=np.uint32)
        dq[10, 10] = masks.DO_NOT_USE  # pre-existing flag we must not clear
        self._make_rate_with_dq(rate, dq)

        reg = "image\npolygon(5,5,15,5,15,15,5,15)"
        n_pixels = masks.apply_mask_dq(rate, reg)
        assert n_pixels > 0

        from jwst.datamodels import ImageModel
        with ImageModel(rate) as model:
            # Pre-existing DNU pixel still set.
            assert model.dq[10, 10] & masks.DO_NOT_USE
            # Pixel inside polygon also set.
            assert model.dq[10, 8] & masks.DO_NOT_USE

        # CFDQMASK extension records the pixels we flipped, and no on-disk
        # sidecar is created (the _manual_dq.fits file is gone for good).
        assert not os.path.exists(rate.replace("_rate.fits", "_manual_dq.fits"))
        with fits.open(rate) as hdul:
            assert "CFDQMASK" in hdul
            flipped = hdul["CFDQMASK"].data.astype(bool)
        # The pre-existing DNU pixel must NOT be in the flipped set, otherwise
        # clear_manual_mask_dq would erase a DNU bit it didn't put there.
        assert not flipped[10, 10]
        # A polygon-interior pixel that wasn't pre-flagged must be in flipped.
        assert flipped[10, 8]

        masks.clear_manual_mask_dq(rate)
        with fits.open(rate) as hdul:
            assert "CFDQMASK" not in hdul
        with ImageModel(rate) as model:
            # Pre-existing DNU pixel should still be set.
            assert model.dq[10, 10] & masks.DO_NOT_USE
            # Pixel inside polygon (that we flipped) should be cleared.
            assert not (model.dq[10, 8] & masks.DO_NOT_USE)


class TestBkgsubExtensionsAndRestore:
    """Round-trip CFBKG/CFBKGMASK/CFBKGSUB/CFBKGRMS: simulate a bkgsub by
    writing the extensions and header keys directly, then verify
    restore_pre_bkgsub correctly inverts the SCI offset and the variance
    rescale, and tears down all bkgsub state."""

    def _make_rate(self, path, sci, var, dq=None):
        from jwst.datamodels import ImageModel
        m = ImageModel(sci.shape)
        m.data = sci.astype(np.float32)
        m.err = np.ones_like(sci, dtype=np.float32)
        m.var_rnoise = var.astype(np.float32)
        m.var_poisson = np.ones_like(sci, dtype=np.float32)
        m.dq = (dq if dq is not None else np.zeros_like(sci, dtype=np.uint32))
        m.meta.instrument.grating = "PRISM"
        m.save(path)
        m.close()

    def _stamp_post_bkgsub(self, path, bkg, mask, var_rescale):
        with fits.open(path, mode="update") as hdul:
            hdr = hdul[0].header
            hdr["CFBKGSUB"] = (True, "Background subtraction applied to SCI")
            hdr["CFBKGRMS"] = (var_rescale, "VAR_RNOISE rescale factor from bkg sub")
            hdr["CFBKGDT"] = ("2026-05-02T00:00:00", "Timestamp")
            hdul.append(fits.ImageHDU(bkg.astype(np.float32), name="CFBKG"))
            hdul.append(fits.ImageHDU(mask.astype(np.uint8), name="CFBKGMASK"))

    def test_bkgsub_done_reads_header(self, tmp_path):
        pytest.importorskip("jwst")
        from campfire_pipeline.nirspec.stage1 import bkgsub_done

        rate = str(tmp_path / "rate.fits")
        sci = np.full((8, 8), 5.0, dtype=np.float32)
        var = np.ones((8, 8), dtype=np.float32)
        self._make_rate(rate, sci, var)
        assert bkgsub_done(rate) is False

        with fits.open(rate, mode="update") as hdul:
            hdul[0].header["CFBKGSUB"] = True
        assert bkgsub_done(rate) is True

    def test_restore_pre_bkgsub_round_trip(self, tmp_path):
        pytest.importorskip("jwst")
        from jwst.datamodels import ImageModel

        rate = str(tmp_path / "rate.fits")
        # Pre-bkgsub state: SCI = original, var = original.
        original_sci = np.full((8, 8), 10.0, dtype=np.float32)
        original_var = np.full((8, 8), 2.0, dtype=np.float32)
        bkg = np.full((8, 8), 3.0, dtype=np.float32)
        var_rescale = 4.0

        # Write the *post*-bkgsub state directly: SCI = original - bkg,
        # var = original * var_rescale, plus extensions + header.
        post_sci = original_sci - bkg
        post_var = original_var * var_rescale
        bkg_mask = np.ones((8, 8), dtype=bool)
        self._make_rate(rate, post_sci, post_var)
        self._stamp_post_bkgsub(rate, bkg, bkg_mask, var_rescale)

        # Sanity: extensions and header are present.
        with fits.open(rate) as hdul:
            assert "CFBKG" in hdul and "CFBKGMASK" in hdul
            assert hdul[0].header["CFBKGSUB"] is True

        masks.restore_pre_bkgsub(rate)

        # SCI is restored: post + bkg == original.
        # var_rnoise is un-rescaled: post / var_rescale == original.
        with ImageModel(rate) as model:
            np.testing.assert_allclose(model.data, original_sci, rtol=1e-6)
            np.testing.assert_allclose(model.var_rnoise, original_var, rtol=1e-6)

        # All bkgsub state is torn down.
        with fits.open(rate) as hdul:
            hdr = hdul[0].header
            for k in ("CFBKGSUB", "CFBKGRMS", "CFBKGDT"):
                assert k not in hdr, f"{k} should be cleared after restore"
            assert "CFBKG" not in hdul
            assert "CFBKGMASK" not in hdul

    def test_restore_raises_when_cfbkgsub_false(self, tmp_path):
        pytest.importorskip("jwst")

        rate = str(tmp_path / "rate.fits")
        sci = np.full((8, 8), 1.0, dtype=np.float32)
        var = np.full((8, 8), 1.0, dtype=np.float32)
        self._make_rate(rate, sci, var)
        # No CFBKGSUB stamped — restore should refuse.
        with pytest.raises(RuntimeError, match="CFBKGSUB"):
            masks.restore_pre_bkgsub(rate)

    def test_restore_raises_when_cfbkg_extension_missing(self, tmp_path):
        pytest.importorskip("jwst")

        rate = str(tmp_path / "rate.fits")
        sci = np.full((8, 8), 1.0, dtype=np.float32)
        var = np.full((8, 8), 1.0, dtype=np.float32)
        self._make_rate(rate, sci, var)
        with fits.open(rate, mode="update") as hdul:
            hdul[0].header["CFBKGSUB"] = True
            hdul[0].header["CFBKGRMS"] = 2.0
        with pytest.raises(RuntimeError, match="CFBKG extension missing"):
            masks.restore_pre_bkgsub(rate)
