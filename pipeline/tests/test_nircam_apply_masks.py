"""Tests for NIRCam ``apply_masks`` region rasterization.

The web mask editor is pixel-native, so ``campfire deploy pull-masks``
writes ``.reg`` files in DS9 ``image`` coordinates. ``Regions.read`` parses
those back as ``PixelRegion`` objects, which have **no** ``to_pixel`` method
— only sky regions do. ``apply_masks_step`` used to call ``reg.to_pixel(wcs)``
unconditionally, so every web-defined mask crashed the combine phase with

    AttributeError: 'PolygonPixelRegion' object has no attribute 'to_pixel'

These tests pin the fix in ``_rasterize_regions``: pixel regions rasterize
directly, sky regions still project through the WCS, and degenerate regions
(off-frame, unprojectable) are skipped instead of aborting the exposure.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("regions")

from regions import Regions

from campfire_pipeline.nircam.steps.apply_masks import _exposure_fits_wcs, _rasterize_regions


def _parse(text):
    return Regions.parse(text, format="ds9")


def _tan_wcs():
    """A small TAN WCS whose reference pixel sits mid-frame."""
    from astropy.wcs import WCS

    w = WCS(naxis=2)
    w.wcs.crpix = [5, 5]
    w.wcs.cdelt = [-1e-4, 1e-4]
    w.wcs.crval = [150.0, 2.0]
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    return w


class TestPixelRegions:
    def test_image_coord_polygon_does_not_crash(self):
        # This is the regression: a pull-masks-style image-coord polygon.
        # No WCS is needed (or used) for a pixel region — pass None to prove it.
        regs = _parse("image\npolygon(2,2,8,2,8,8,2,8)")
        mask = _rasterize_regions(regs, wcs=None, shape=(10, 10))
        assert mask.dtype == np.uint8
        assert mask[5, 5] == 1          # inside the square
        assert mask[0, 0] == 0          # outside
        assert mask.sum() > 0

    def test_union_of_two_polygons(self):
        regs = _parse(
            "image\n"
            "polygon(1,1,3,1,3,3,1,3)\n"
            "polygon(6,6,9,6,9,9,6,9)"
        )
        mask = _rasterize_regions(regs, wcs=None, shape=(10, 10))
        assert mask[1, 1] == 1          # interior of the first square
        assert mask[7, 7] == 1          # interior of the second square
        assert mask[4, 4] == 0          # gap between the two squares

    def test_offframe_region_skipped(self):
        # to_image() returns None when the footprint misses the frame — the
        # old code crashed on None.astype; now it's skipped, mask stays empty.
        regs = _parse("image\npolygon(100,100,110,100,110,110,100,110)")
        mask = _rasterize_regions(regs, wcs=None, shape=(10, 10))
        assert mask.sum() == 0

    def test_pixel_region_ignores_wcs(self):
        # Passing a real WCS must not change a pixel region's rasterization
        # (it must not be re-projected).
        regs = _parse("image\npolygon(2,2,8,2,8,8,2,8)")
        with_wcs = _rasterize_regions(_parse("image\npolygon(2,2,8,2,8,8,2,8)"),
                                      wcs=_tan_wcs(), shape=(10, 10))
        without_wcs = _rasterize_regions(regs, wcs=None, shape=(10, 10))
        np.testing.assert_array_equal(with_wcs, without_wcs)


class TestSkyRegions:
    def test_sky_polygon_projects_through_wcs(self):
        pytest.importorskip("astropy")
        wcs = _tan_wcs()
        # A small fk5 quad around the WCS reference coordinate.
        regs = _parse(
            "fk5\npolygon("
            "150.0003,1.9997,149.9997,1.9997,"
            "149.9997,2.0003,150.0003,2.0003)"
        )
        mask = _rasterize_regions(regs, wcs=wcs, shape=(10, 10))
        assert mask.sum() > 0

    def test_sky_region_without_wcs_is_skipped_not_crash(self):
        # A sky region needs a WCS to project; with wcs=None the projection
        # raises and the region is skipped rather than aborting the exposure.
        regs = _parse(
            "fk5\npolygon("
            "150.0003,1.9997,149.9997,1.9997,"
            "149.9997,2.0003,150.0003,2.0003)"
        )
        mask = _rasterize_regions(regs, wcs=None, shape=(10, 10))
        assert mask.sum() == 0


class TestMixed:
    def test_mixed_pixel_and_sky(self):
        pytest.importorskip("astropy")
        wcs = _tan_wcs()
        pix = _rasterize_regions(_parse("image\npolygon(1,1,3,1,3,3,1,3)"),
                                 wcs=wcs, shape=(10, 10))
        sky = _rasterize_regions(
            _parse("fk5\npolygon(150.0003,1.9997,149.9997,1.9997,"
                   "149.9997,2.0003,150.0003,2.0003)"),
            wcs=wcs, shape=(10, 10))
        # Both contribute independently; a file with both unions them.
        both = _rasterize_regions(
            _parse("image\npolygon(1,1,3,1,3,3,1,3)"
                   "\nfk5;polygon(150.0003,1.9997,149.9997,1.9997,"
                   "149.9997,2.0003,150.0003,2.0003)"),
            wcs=wcs, shape=(10, 10))
        np.testing.assert_array_equal(both, (pix | sky))


class TestExposureWcs:
    """The step's WCS comes from the SCI header (``get_fits_wcs`` left with jwst 3.0)."""

    def test_sky_region_rasterizes_through_sci_header_wcs(self, tmp_path):
        from astropy.io import fits

        hdr = _tan_wcs().to_header()
        f = tmp_path / "exp_nrcalong.fits"
        fits.HDUList([fits.PrimaryHDU(),
                      fits.ImageHDU(np.zeros((10, 10), np.float32), header=hdr, name="SCI")]).writeto(f)
        wcs = _exposure_fits_wcs(str(f))
        regs = _parse('fk5\ncircle(150.0,2.0,0.72")')
        mask = _rasterize_regions(regs, wcs, (10, 10))
        assert mask[4, 4] == 1 and mask[0, 0] == 0


# ---------------------------------------------------------------------------
# Issue #478 item 1: the skip follows the .reg input, not the stamp's presence
# ---------------------------------------------------------------------------

from campfire_pipeline.nircam.steps import apply_masks as am  # noqa: E402

_SQUARE = "image\npolygon(2,2,8,2,8,8,2,8)\n"
_BIGGER = "image\npolygon(1,1,15,1,15,15,1,15)\n"


class TestStaleness:
    """``mask_staleness`` as a pure function of (stamp, .reg on disk)."""

    def test_not_yet_run(self, tmp_path):
        assert am.mask_staleness(None, str(tmp_path / "x.reg")) == "not yet run"

    def test_no_reg_stays_current(self, tmp_path):
        assert am.mask_staleness(am.NO_REG, str(tmp_path / "x.reg")) is None

    def test_reg_appearing_after_no_reg_stamp_is_stale(self, tmp_path):
        # The sequence the issue measured: combine stamps 'no .reg file', then
        # a pull materializes the reviewer's mask.
        reg = tmp_path / "x.reg"
        reg.write_text(_SQUARE)
        assert am.mask_staleness(am.NO_REG, str(reg)) is not None

    def test_hash_match_and_mismatch(self, tmp_path):
        reg = tmp_path / "x.reg"
        reg.write_text(_SQUARE)
        stamp = am.mask_stamp(am.reg_hash(str(reg)))
        assert am.mask_staleness(stamp, str(reg)) is None
        reg.write_text(_BIGGER)
        assert am.mask_staleness(stamp, str(reg)) == "its .reg file changed"

    def test_comment_lines_do_not_count(self, tmp_path):
        reg = tmp_path / "x.reg"
        reg.write_text("# Generated at 2026-01-01\n" + _SQUARE)
        stamp = am.mask_stamp(am.reg_hash(str(reg)))
        reg.write_text("# Generated at 2026-11-22\n\n" + _SQUARE)
        assert am.mask_staleness(stamp, str(reg)) is None

    def test_removed_reg_is_stale(self, tmp_path):
        assert am.mask_staleness("2026-01-01T00:00:00 reg=abc",
                                 str(tmp_path / "x.reg")) == "its .reg file was removed"

    def test_legacy_timestamp_judged_by_mtime(self, tmp_path):
        import os
        reg = tmp_path / "x.reg"
        reg.write_text(_SQUARE)
        t = 1_700_000_000
        os.utime(reg, (t, t))
        from datetime import datetime, timedelta
        at = datetime.fromtimestamp(t)
        older = (at - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        newer = (at + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        assert am.mask_staleness(newer, str(reg)) is None
        assert am.mask_staleness(older, str(reg)) is not None

    def test_unreadable_stamp_is_stale(self, tmp_path):
        reg = tmp_path / "x.reg"
        reg.write_text(_SQUARE)
        assert am.mask_staleness("garbage", str(reg)) is not None


@pytest.fixture
def exposure(tmp_path):
    """A canonical-like exposure ``<tmp>/products/f277w/<root>.fits`` plus a
    ``field`` stand-in whose ``mask_dir`` is ``<tmp>/masks``."""
    pytest.importorskip("jwst")
    from types import SimpleNamespace
    from jwst.datamodels import ImageModel

    prod = tmp_path / "products" / "f277w"
    prod.mkdir(parents=True)
    path = prod / "jw01176221001_08101_00003_nrcblong.fits"
    m = ImageModel(data=np.zeros((20, 20), np.float32),
                   dq=np.zeros((20, 20), np.uint32))
    m.save(str(path))
    m.close()
    (tmp_path / "masks" / "f277w").mkdir(parents=True)
    field = SimpleNamespace(mask_dir=str(tmp_path / "masks"))
    reg = tmp_path / "masks" / "f277w" / f"{path.stem}.reg"
    return str(path), field, reg


def _state(path):
    from astropy.io import fits
    with fits.open(path) as h:
        stamp = h[0].header.get("CFP_MASK")
        n = int(h["CFMASK"].data.sum()) if "CFMASK" in h else None
        spke = "CFP_SPKE" in h[0].header
    return stamp, n, spke


class TestStepRoundTrip:
    """``apply_masks_step`` end to end on a real datamodel file, no overwrite."""

    def test_mask_drawn_after_first_combine_is_applied(self, exposure):
        path, field, reg = exposure
        am.apply_masks_step(path, field, {})
        assert _state(path)[:2] == (am.NO_REG, None)

        reg.write_text(_SQUARE)                       # the reviewer's mask arrives
        am.apply_masks_step(path, field, {})          # combine re-run, no --overwrite
        stamp, n, _ = _state(path)
        assert n and n > 0
        assert am.stamp_reg_hash(stamp) == am.reg_hash(str(reg))

    def test_unchanged_mask_is_skipped(self, exposure):
        import os
        path, field, reg = exposure
        reg.write_text(_SQUARE)
        am.apply_masks_step(path, field, {})
        before = os.stat(path).st_mtime_ns
        am.apply_masks_step(path, field, {})
        assert os.stat(path).st_mtime_ns == before   # not rewritten

    def test_edited_mask_rebuilds_and_clears_spike_stamp(self, exposure):
        from astropy.io import fits
        path, field, reg = exposure
        reg.write_text(_SQUARE)
        am.apply_masks_step(path, field, {})
        n_small = _state(path)[1]
        with fits.open(path, mode="update") as h:     # as spike_mask stamps it
            h[0].header["CFP_SPKE"] = "x"
        reg.write_text(_BIGGER)
        am.apply_masks_step(path, field, {})
        stamp, n_big, spke = _state(path)
        assert n_big > n_small
        assert am.stamp_reg_hash(stamp) == am.reg_hash(str(reg))
        assert not spke

    def test_removed_mask_drops_cfmask_and_file_still_loads(self, exposure):
        from jwst.datamodels import ImageModel
        path, field, reg = exposure
        reg.write_text(_SQUARE)
        am.apply_masks_step(path, field, {})
        reg.unlink()
        am.apply_masks_step(path, field, {})
        assert _state(path)[:2] == (am.NO_REG, None)
        with ImageModel(path) as m:                   # no dangling ASDF reference
            assert m.data.shape == (20, 20)
        am.apply_masks_step(path, field, {})          # and a later save is clean
        assert _state(path)[1] is None


class TestDispatchFilter:
    """The orchestrator's pre-dispatch filter re-queues changed masks."""

    def test_pending_follows_reg_input(self, exposure):
        from campfire_pipeline.nircam.orchestrate import _filter_pending_masks
        path, field, reg = exposure
        assert _filter_pending_masks([path], field, False) == [path]
        am.apply_masks_step(path, field, {})
        assert _filter_pending_masks([path], field, False) == []
        reg.write_text(_SQUARE)
        assert _filter_pending_masks([path], field, False) == [path]
        am.apply_masks_step(path, field, {})
        assert _filter_pending_masks([path], field, False) == []
        assert _filter_pending_masks([path], field, True) == [path]

    def test_dispatch_refreshes_status_so_spike_mask_reruns(self, exposure):
        # Codex review on #712: clearing CFP_SPKE on disk must reach the
        # phase's StepStatus snapshot, or spike_mask skips in the same run.
        from types import SimpleNamespace
        from unittest import mock
        from astropy.io import fits
        from campfire_pipeline.nircam import orchestrate
        from campfire_pipeline.nircam.status import StepStatus

        path, field, reg = exposure
        reg.write_text(_SQUARE)
        am.apply_masks_step(path, field, {})
        with fits.open(path, mode="update") as h:
            h[0].header["CFP_SPKE"] = "x"
        reg.write_text(_BIGGER)
        status = StepStatus.scan([path])
        assert status.has(path, "CFP_SPKE")

        fld = SimpleNamespace(mask_dir=field.mask_dir,
                              get_exposure_files=lambda *a, **k: [path])
        with mock.patch.object(orchestrate, "get_nircam_step_config",
                               return_value={}), \
             mock.patch.object(orchestrate, "dispatch",
                               side_effect=lambda fn, items, **kw:
                               [fn(i, kw["field"], kw["step_config"]) for i in items]):
            orchestrate._run_per_exposure("apply_mask", fld, {}, "f277w", 1,
                                          False, status)
        assert status.has(path, "CFP_MASK")
        assert not status.has(path, "CFP_SPKE")
