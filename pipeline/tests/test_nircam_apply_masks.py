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

from campfire_pipeline.nircam.steps.apply_masks import _rasterize_regions


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
