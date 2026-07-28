"""Tests for the tolerant dither point-count header readers (issue #415).

Some Cycle 1 NIRSpec programs (e.g. 1210) omit PRIDTPTS/SUBPXPTS entirely, and
SDP wrote SUBPXPTS = 0 for some modes before Build 9.0. ``discover_files`` used
to index them directly and raised ``KeyError`` before stage2a's unit fixing.
"""

from astropy.io import fits

from campfire_pipeline.nirspec.observation import (
    DEFAULT_PRIMARY_DITHER_POINTS,
    DEFAULT_SUBPIXEL_DITHER_POINTS,
    read_primary_dither_points,
    read_subpixel_dither_points,
)


def _hdr(**cards):
    hdr = fits.Header()
    for key, value in cards.items():
        hdr[key] = value
    return hdr


def test_primary_points_read_when_present():
    assert read_primary_dither_points(_hdr(PRIDTPTS=2), 'a.fits') == 2


def test_primary_points_default_when_missing():
    assert (read_primary_dither_points(_hdr(), 'a.fits')
            == DEFAULT_PRIMARY_DITHER_POINTS)


def test_subpixel_points_read_when_present():
    assert read_subpixel_dither_points(_hdr(SUBPXPTS=2), 'a.fits') == 2


def test_subpixel_points_default_when_missing():
    assert (read_subpixel_dither_points(_hdr(), 'a.fits')
            == DEFAULT_SUBPIXEL_DITHER_POINTS)


def test_subpixel_points_falls_back_to_legacy_spelling():
    """Pre-jwst-0.18.2 data carries the real value under SUBPXPNS."""
    assert read_subpixel_dither_points(_hdr(SUBPXPNS=2), 'a.fits') == 2


def test_subpixel_points_prefers_current_spelling():
    hdr = _hdr(SUBPXPTS=2, SUBPXPNS=4)
    assert read_subpixel_dither_points(hdr, 'a.fits') == 2


def test_zero_is_rejected_not_trusted():
    """SDP wrote SUBPXPTS = 0 for some NIRSpec modes before Build 9.0."""
    assert (read_subpixel_dither_points(_hdr(SUBPXPTS=0), 'a.fits')
            == DEFAULT_SUBPIXEL_DITHER_POINTS)


def test_zero_falls_through_to_legacy_spelling():
    assert read_subpixel_dither_points(_hdr(SUBPXPTS=0, SUBPXPNS=2), 'a.fits') == 2


def test_non_numeric_value_is_rejected():
    assert (read_subpixel_dither_points(_hdr(SUBPXPTS='N/A'), 'a.fits')
            == DEFAULT_SUBPIXEL_DITHER_POINTS)


def test_string_integer_is_accepted():
    """SDP-populated cards occasionally arrive as strings."""
    assert read_subpixel_dither_points(_hdr(SUBPXPTS='2'), 'a.fits') == 2
