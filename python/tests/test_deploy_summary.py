"""Unit tests for deploy.summary record building — focused on JSON-safety of
non-finite floats. PostgREST/httpx serialize with allow_nan=False, so inf/nan
leaking into a record aborts the entire spectra/targets upsert."""

from __future__ import annotations

import math

from astropy.table import Table

from campfire.deploy.summary import (
    _finite_or_none,
    get_spectra_records,
    get_unique_objects,
)


def test_finite_or_none():
    assert _finite_or_none(None) is None
    assert _finite_or_none(math.inf) is None
    assert _finite_or_none(-math.inf) is None
    assert _finite_or_none(math.nan) is None
    assert _finite_or_none(0.0) == 0.0
    assert _finite_or_none(3.5) == 3.5


def _spectra_table() -> Table:
    t = Table()
    t['object_id'] = ['t1', 't2']
    t['grating'] = ['prism', 'prism']
    t['fits_filename'] = ['a_spec.fits', 'b_spec.fits']
    t['reduction_version'] = ['v1', 'v1']
    # t1 has a zero-noise division -> inf SNR; t2 is finite
    t['signal_to_noise'] = [math.inf, 12.0]
    t['exposure_time'] = [1000.0, math.nan]
    t['file_hash'] = ['h1', 'h2']
    t['file_size'] = [100, 200]
    t['redshift_auto'] = [math.nan, 3.1]
    t.meta['cfpipe_version'] = '0.5.0'
    return t


def test_get_spectra_records_sanitizes_non_finite():
    records = get_spectra_records(_spectra_table(), 'obs1')

    r1, r2 = records
    # inf SNR and nan exposure_time / redshift become NULL, not a crash
    assert r1['signal_to_noise'] is None
    assert r1['redshift_auto'] is None
    assert r2['exposure_time'] is None
    # finite values pass through untouched
    assert r2['signal_to_noise'] == 12.0
    assert r2['redshift_auto'] == 3.1
    assert r1['exposure_time'] == 1000.0


def test_get_unique_objects_sanitizes_redshift():
    t = Table()
    t['object_id'] = ['t1']
    t['source_id'] = [1]
    t['ra'] = [150.0]
    t['dec'] = [2.0]
    t['redshift_best'] = [math.inf]
    t.meta['program_slug'] = 'prog'
    t.meta['obs_name'] = 'obs1'

    objects = get_unique_objects(t)
    assert objects[0]['redshift_best'] is None
