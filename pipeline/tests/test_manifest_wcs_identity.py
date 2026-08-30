"""Manifest change detection must see a re-ALIGNMENT, not just new pixels.

`compute_file_hash` digests SCI+DQ only, so an exposure that `align` (or
`wcs_shift`) re-solved — identical science, corrected WCS — hashed the same as
before and every tile that consumed it was judged up to date. `compute_wcs_hash`
is the missing half; these tests pin both halves and the legacy tolerance that
keeps existing manifests from all invalidating at once.
"""
import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nircam.manifest import (
    compute_file_hash, compute_wcs_hash, file_unchanged, input_entry,
)

WCS_CARDS = {
    'CTYPE1': 'RA---TAN', 'CTYPE2': 'DEC--TAN',
    'CRPIX1': 1024.0, 'CRPIX2': 1024.0,
    'CRVAL1': 150.0, 'CRVAL2': 2.0,
    'CD1_1': -8.6e-6, 'CD1_2': 0.0, 'CD2_1': 0.0, 'CD2_2': 8.6e-6,
}


@pytest.fixture
def exposure(tmp_path):
    sci = fits.ImageHDU(np.arange(16, dtype='f4').reshape(4, 4), name='SCI')
    for key, value in WCS_CARDS.items():
        sci.header[key] = value
    dq = fits.ImageHDU(np.zeros((4, 4), dtype='i4'), name='DQ')
    path = str(tmp_path / 'jw001_nrca1.fits')
    fits.HDUList([fits.PrimaryHDU(), sci, dq]).writeto(path)
    return path


def _realign(path, crval1):
    """Move the WCS and nothing else — what an align re-solve writes."""
    with fits.open(path, mode='update') as hdul:
        hdul['SCI'].header['CRVAL1'] = crval1


def test_wcs_hash_moves_on_realignment_science_hash_does_not(exposure):
    before_sci = compute_file_hash(exposure)
    before_wcs = compute_wcs_hash(exposure)
    _realign(exposure, 150.0001)
    assert compute_file_hash(exposure) == before_sci   # not one pixel moved
    assert compute_wcs_hash(exposure) != before_wcs


def test_wcs_hash_survives_a_plain_resave(exposure):
    before = compute_wcs_hash(exposure)
    with fits.open(exposure, mode='update') as hdul:
        hdul[0].header['DATE'] = '2026-07-27T12:00:00'
        hdul[0].header['HISTORY'] = 'resaved'
    assert compute_wcs_hash(exposure) == before


def test_wcs_hash_is_none_without_wcs_cards(tmp_path):
    path = str(tmp_path / 'bare.fits')
    fits.HDUList([fits.PrimaryHDU()]).writeto(path)
    assert compute_wcs_hash(path) is None


def test_file_unchanged_detects_realignment(exposure):
    entry = input_entry(exposure)
    assert entry['wcs_hash'] is not None
    _realign(exposure, 150.0001)
    # The stat fast path must not mask this: the re-solve rewrote the file, so
    # size/mtime_ns no longer match and the digests are consulted.
    assert file_unchanged(exposure, entry) is False


def test_legacy_entry_without_wcs_hash_stays_tolerant(exposure):
    # Manifests written before this digest existed keep their verdicts — a
    # recipe change that invalidated all of them at once would re-drizzle every
    # tile of every field on the next run.
    entry = input_entry(exposure)
    legacy = {k: v for k, v in entry.items() if k != 'wcs_hash'}
    _realign(exposure, 150.0001)
    assert file_unchanged(exposure, legacy) is True
    # ...but a science change is still caught, exactly as before.
    with fits.open(exposure, mode='update') as hdul:
        hdul['SCI'].data = np.ones((4, 4), dtype='f4')
    assert file_unchanged(exposure, legacy) is False


def test_stat_fast_path_still_short_circuits(exposure):
    entry = input_entry(exposure)
    assert file_unchanged(exposure, entry) is True
