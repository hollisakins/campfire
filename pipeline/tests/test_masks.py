"""Tests for the manual masking module (campfire_pipeline.nirspec.masks).

Targets the pure-Python pieces (parsing, hashing, TOML round-trip,
sentinel reads/writes, sidecar logic) that don't require a real JWST
rate file. End-to-end DQ/bkg-sub behavior is verified in the project
README's manual workflow.
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

        # Sidecar exists.
        sidecar = rate.replace("_rate.fits", "_manual_dq.fits")
        assert os.path.exists(sidecar)

        masks.clear_manual_mask_dq(rate)
        assert not os.path.exists(sidecar)
        with ImageModel(rate) as model:
            # Pre-existing DNU pixel should still be set.
            assert model.dq[10, 10] & masks.DO_NOT_USE
            # Pixel inside polygon (that we flipped) should be cleared.
            assert not (model.dq[10, 8] & masks.DO_NOT_USE)
