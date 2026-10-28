"""Unit tests for deploy.summary record building — focused on JSON-safety of
non-finite floats. PostgREST/httpx serialize with allow_nan=False, so inf/nan
leaking into a record aborts the entire spectra/targets upsert."""

from __future__ import annotations

import math

from astropy.table import Table

from campfire.deploy.summary import (
    _finite_or_none,
    format_stale_summary,
    stale_summary_rows,
    get_spectra_records,
    get_zfit_scalar_updates,
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
    t['cfpipe_version'] = ['0.5.0', '0.5.0']
    t['crds_context'] = ['jwst_1210.pmap', 'jwst_1322.pmap']
    t['jwst_version'] = ['1.14.0', '1.14.0']
    t['date_obs'] = ['2024-01-01', '2024-01-02']
    t['reduced_at'] = ['2026-06-01T00:00:00+00:00', '2026-06-02T00:00:00+00:00']
    # t1 has a zero-noise division -> inf SNR; t2 is finite
    t['signal_to_noise'] = [math.inf, 12.0]
    t['exposure_time'] = [1000.0, math.nan]
    t['file_hash'] = ['h1', 'h2']
    t['file_size'] = [100, 200]
    t['redshift_auto'] = [math.nan, 3.1]
    t.meta['cfpipe_version'] = '0.5.0'
    return t


SCI_HASH = 'sha256:' + 'ab' * 32


def test_get_spectra_records_carries_science_hash_when_present():
    """P0b: the summary's science_hash rides into the spectra upsert body on
    every deploy; an empty cell (product the hasher could not read) is sent as
    NULL rather than dropped, so it cannot keep a stale hash."""
    t = _spectra_table()
    t['science_hash'] = [SCI_HASH, '']
    r1, r2 = get_spectra_records(t, 'obs1')
    assert r1['science_hash'] == SCI_HASH
    assert 'science_hash' in r2 and r2['science_hash'] is None


def test_get_spectra_records_omits_science_hash_for_old_ecsv():
    """A pre-P0b summary has no column: the key is absent so the upsert
    leaves whatever the backfill wrote on the row."""
    r1, _ = get_spectra_records(_spectra_table(), 'obs1')
    assert 'science_hash' not in r1


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


def test_get_spectra_records_carries_provenance():
    """Every provenance field rides verbatim from the summary into the record,
    and the collapsed reduction_version key never appears."""
    r1, r2 = get_spectra_records(_spectra_table(), 'obs1')
    assert r1['cfpipe_version'] == '0.5.0'
    assert r1['crds_context'] == 'jwst_1210.pmap'
    assert r2['crds_context'] == 'jwst_1322.pmap'
    assert r1['jwst_version'] == '1.14.0'
    assert r1['date_obs'] == '2024-01-01'
    assert r1['reduced_at'] == '2026-06-01T00:00:00+00:00'
    assert 'reduction_version' not in r1


def test_get_spectra_records_reads_legacy_reduction_version():
    """A pre-collapse ECSV (per-row reduction_version, no cfpipe_version column)
    still populates cfpipe_version from the old column."""
    t = _spectra_table()
    t.remove_column('cfpipe_version')
    t['reduction_version'] = ['v0.3-legacy', 'v0.3-legacy']
    del t.meta['cfpipe_version']
    r1, _ = get_spectra_records(t, 'obs1')
    assert r1['cfpipe_version'] == 'v0.3-legacy'
    assert 'reduction_version' not in r1


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


def test_get_spectra_records_carries_rls_scope_columns():
    """Perf T2-A (#504): observation always, program_slug when the ECSV meta has it.

    The database trigger owns both columns; the deploy sends them as belt and
    braces, so they must agree with the targets upsert built from the same
    summary (program_slug from meta, observation from the obs name).
    """
    t = _spectra_table()
    r1, r2 = get_spectra_records(t, 'obs1')
    assert r1['observation'] == 'obs1' and r2['observation'] == 'obs1'
    assert 'program_slug' not in r1  # old ECSV: no meta, let the trigger fill it

    t.meta['program_slug'] = 'capers'
    r1, r2 = get_spectra_records(t, 'obs1')
    assert r1['program_slug'] == 'capers' and r2['program_slug'] == 'capers'


def test_get_spectra_records_carries_zfit_scalars_when_present():
    t = _spectra_table()
    t['chi2_min'] = [math.nan, 41.25]
    t['redshift_confidence'] = [math.inf, 87.5]
    r1, r2 = get_spectra_records(t, 'obs1')
    # Present columns are always sent (null clears a stale value on re-fit).
    assert r1['chi2_min'] is None and r1['confidence'] is None
    assert r2['chi2_min'] == 41.25 and r2['confidence'] == 87.5


def test_get_spectra_records_omits_zfit_scalars_for_old_ecsv():
    r1, _ = get_spectra_records(_spectra_table(), 'obs1')
    assert 'chi2_min' not in r1 and 'confidence' not in r1


def test_get_zfit_scalar_updates_uses_uploaded_json_not_stale_summary():
    t = _spectra_table()
    t['zfit_file'] = ['a_zfit.fits', 'b_zfit.fits']
    t['chi2_min'] = [999.0, 888.0]
    t['redshift_confidence'] = [1.0, 2.0]
    updates = get_zfit_scalar_updates(t, {
        'a_zfit.fits': {'redshift': 2.75, 'chi2_min': 41.25, 'confidence': 87.5},
    })
    assert updates == [{
        'target_id': 't1',
        'grating': 'prism',
        'redshift_auto': 2.75,
        'chi2_min': 41.25,
        'confidence': 87.5,
    }]


def test_get_zfit_scalar_updates_clears_degenerate_fit_values():
    t = _spectra_table()[:1]
    t['zfit_file'] = ['a_zfit.fits']
    updates = get_zfit_scalar_updates(t, {
        'a_zfit.fits': {'redshift': None, 'chi2_min': None, 'confidence': None},
    })
    assert updates[0]['redshift_auto'] is None
    assert updates[0]['chi2_min'] is None
    assert updates[0]['confidence'] is None


def _summary_on_disk(tmp_path, files: dict[str, bytes]) -> Table:
    import hashlib
    t = Table()
    t['spec_file'] = list(files)
    t['file_hash'] = ['sha256:' + hashlib.sha256(b).hexdigest() for b in files.values()]
    for name, body in files.items():
        (tmp_path / name).write_bytes(body)
    return t


def test_stale_summary_rows_accepts_a_fresh_summary(tmp_path):
    t = _summary_on_disk(tmp_path, {'a_spec.fits': b'aaa', 'b_spec.fits': b'bbb'})
    assert stale_summary_rows(t, tmp_path) == []


def test_stale_summary_rows_names_a_final_rewritten_after_the_summary(tmp_path):
    """The rehearsal case (#665): one FITS reprocessed in place, summary not regenerated."""
    import hashlib
    t = _summary_on_disk(tmp_path, {'a_spec.fits': b'aaa', 'b_spec.fits': b'bbb'})
    (tmp_path / 'b_spec.fits').write_bytes(b'reprocessed')
    stale = stale_summary_rows(t, tmp_path)
    assert [r['spec_file'] for r in stale] == ['b_spec.fits']
    assert stale[0]['file_hash'] == hashlib.sha256(b'reprocessed').hexdigest()
    assert stale[0]['summary_hash'] == hashlib.sha256(b'bbb').hexdigest() and stale[0]['mtime']
    msg = format_stale_summary('obs_x', stale)
    assert 'b_spec.fits' in msg and 'cfpipe nirspec summary --obs obs_x' in msg and '1 final(s)' in msg


def test_stale_summary_rows_reports_a_missing_final(tmp_path):
    t = _summary_on_disk(tmp_path, {'a_spec.fits': b'aaa'})
    (tmp_path / 'a_spec.fits').unlink()
    stale = stale_summary_rows(t, tmp_path)
    assert stale == [{'spec_file': 'a_spec.fits', 'summary_hash': stale[0]['summary_hash'], 'file_hash': None, 'mtime': None}]
    assert 'missing on disk' in format_stale_summary('obs_x', stale)


def test_stale_summary_rows_compares_without_the_prefix(tmp_path):
    t = _summary_on_disk(tmp_path, {'a_spec.fits': b'aaa'})
    t['file_hash'] = [h.split(':', 1)[1] for h in t['file_hash']]      # an old ECSV with bare hex
    assert stale_summary_rows(t, tmp_path) == []
