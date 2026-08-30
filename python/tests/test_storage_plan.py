"""Unit tests for the push planner (campfire.storage.plan).

Pure classification: no Supabase, no network. Covers the new/changed/unchanged
split, the pushed_* stat fast path (skip without reading), identity-kind
selection (sci_dq for NIRCam exposures vs whole-file), provisional-etag
handling, the confirmed-unchanged bookkeeping that arms the fast path, and the
two-component exposure identity (science + astrometry) with its legacy-row
reconciliation.
"""

import os
from pathlib import Path

from campfire_layout import KeyScheme, Scope, storage_key

from campfire.deploy.r2 import UploadTask
from campfire.storage.hashing import (
    compute_file_hash, exposure_identity, sci_dq_hash, wcs_hash,
)
from campfire.storage.plan import (
    compose_identity,
    identity_kind_for_key,
    plan_push,
    server_identity_for,
)


def _spec_task(tmp_path, name="a_spec.fits", content=b"spec-bytes"):
    p = tmp_path / name
    p.write_bytes(content)
    key = storage_key('nirspec_spec', Scope(obs='obs1'), name,
                      scheme=KeyScheme.CANONICAL)
    return UploadTask(p, key, 'application/fits')


def _row(key, content_hash=None, sci_dq=None, status='active', **extra):
    row = {'storage_key': key, 'status': status,
           'content_hash': content_hash, 'sci_dq_hash': sci_dq}
    row.update(extra)
    return row


# --- identity selection -------------------------------------------------------

def test_identity_kind_selection():
    k_exp = storage_key('nircam_exposure', Scope(field='cosmos', filt='f444w'),
                        'jw1_nrca1.fits', scheme=KeyScheme.CANONICAL)
    k_spec = storage_key('nirspec_spec', Scope(obs='o'), 'x_spec.fits',
                         scheme=KeyScheme.CANONICAL)
    assert identity_kind_for_key(k_exp) == 'sci_dq'
    assert identity_kind_for_key(k_spec) == 'whole_file'
    assert identity_kind_for_key('garbage/not/a/key.bin') == 'whole_file'


def test_server_identity_requires_active_and_sha256():
    key = 'data/products/nirspec/o/x_spec.fits'
    assert server_identity_for(None, 'whole_file') is None
    assert server_identity_for(_row(key, 'sha256:' + 'a' * 64, status='superseded'),
                               'whole_file') is None
    # Provisional etag hashes are not comparable → always re-upload.
    assert server_identity_for(_row(key, 'etag:abc'), 'whole_file') is None
    assert server_identity_for(_row(key, 'sha256:' + 'a' * 64),
                               'whole_file') == 'sha256:' + 'a' * 64
    # sci_dq kind reads the science digest, not the whole-file hash.
    assert server_identity_for(
        _row(key, 'sha256:' + 'a' * 64, sci_dq='sha256:' + 'b' * 64),
        'sci_dq') == 'sha256:' + 'b' * 64
    assert server_identity_for(_row(key, 'sha256:' + 'a' * 64), 'sci_dq') is None


# --- classification -----------------------------------------------------------

def test_new_file_uploads_without_plan_time_hash(tmp_path):
    task = _spec_task(tmp_path)
    plan = plan_push([task], server_rows={}, progress=False)
    assert plan.new == [task]
    assert plan.to_upload == [task]
    # New files are not hashed at plan time (registration hashes per batch).
    assert task.r2_key not in plan.identities


def test_unchanged_file_skips_and_confirms(tmp_path):
    task = _spec_task(tmp_path)
    h = compute_file_hash(task.local_path)
    rows = {task.r2_key: _row(task.r2_key, h)}
    plan = plan_push([task], rows, progress=False)
    assert plan.unchanged == [task]
    assert plan.to_upload == []
    # Identity-confirmed → bookkeeping entry arms the stat fast path.
    (key, identity, mtime, size) = plan.confirmed[0]
    assert key == task.r2_key and identity == h
    st = os.stat(task.local_path)
    assert size == st.st_size


def test_changed_file_uploads_and_reuses_hash(tmp_path):
    task = _spec_task(tmp_path, content=b"new-bytes")
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + '0' * 64)}
    plan = plan_push([task], rows, progress=False)
    assert plan.changed == [task]
    assert plan.to_upload == [task]
    # Whole-file hash computed for the decision is exposed for registration.
    h, size = plan.whole_file[task.r2_key]
    assert h == compute_file_hash(task.local_path)
    assert size == len(b"new-bytes")


def test_active_row_with_etag_hash_always_uploads(tmp_path):
    task = _spec_task(tmp_path)
    rows = {task.r2_key: _row(task.r2_key, 'etag:abc123')}
    plan = plan_push([task], rows, progress=False)
    assert plan.changed == [task]
    # No comparable identity → not hashed at plan time either.
    assert task.r2_key not in plan.identities


def test_inactive_row_treated_as_new(tmp_path):
    task = _spec_task(tmp_path)
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + 'a' * 64,
                              status='superseded')}
    plan = plan_push([task], rows, progress=False)
    assert plan.new == [task]


def test_missing_local_file_reported(tmp_path):
    key = storage_key('nirspec_spec', Scope(obs='obs1'), 'gone_spec.fits',
                      scheme=KeyScheme.CANONICAL)
    task = UploadTask(tmp_path / 'gone_spec.fits', key, 'application/fits')
    plan = plan_push([task], server_rows={}, progress=False)
    assert plan.missing == [task]
    assert plan.to_upload == []


# --- stat fast path -----------------------------------------------------------

def test_stat_fast_path_skips_without_reading(tmp_path, monkeypatch):
    task = _spec_task(tmp_path)
    h = compute_file_hash(task.local_path)
    st = os.stat(task.local_path)
    rows = {task.r2_key: _row(task.r2_key, h)}
    local = {task.r2_key: {'pushed_identity': h,
                           'pushed_mtime': st.st_mtime,
                           'pushed_size': st.st_size}}

    # Any attempt to hash would blow up — the fast path must not read files.
    import campfire.storage.plan as plan_mod

    def _boom(*a, **k):
        raise AssertionError("fast path must not hash")
    monkeypatch.setattr(plan_mod, 'hash_files_parallel', _boom)
    monkeypatch.setattr(plan_mod, 'exposure_identities_parallel', _boom)

    plan = plan_push([task], rows, local_rows=local, progress=False)
    assert plan.unchanged == [task]
    assert plan.fast_skipped == 1
    assert plan.confirmed == []  # already armed; nothing to record


def test_stat_mismatch_falls_back_to_hash(tmp_path):
    task = _spec_task(tmp_path)
    h = compute_file_hash(task.local_path)
    rows = {task.r2_key: _row(task.r2_key, h)}
    # Recorded stat is stale (different size) → must re-hash; identity still
    # matches → unchanged, and confirmed refreshes the stat for next time.
    local = {task.r2_key: {'pushed_identity': h,
                           'pushed_mtime': 1.0, 'pushed_size': 1}}
    plan = plan_push([task], rows, local_rows=local, progress=False)
    assert plan.unchanged == [task]
    assert plan.fast_skipped == 0
    assert len(plan.confirmed) == 1


def test_fast_path_ignored_when_server_identity_moved(tmp_path):
    # Another machine re-uploaded different bytes: server identity no longer
    # matches our pushed_identity → the stale stat record must NOT skip.
    task = _spec_task(tmp_path)
    st = os.stat(task.local_path)
    local_h = compute_file_hash(task.local_path)
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + 'f' * 64)}
    local = {task.r2_key: {'pushed_identity': local_h,
                           'pushed_mtime': st.st_mtime,
                           'pushed_size': st.st_size}}
    plan = plan_push([task], rows, local_rows=local, progress=False)
    assert plan.changed == [task]


# --- sci_dq identity end-to-end ------------------------------------------------

def _exposure_task(tmp_path, name='jw001_nrca1.fits', seed=1.0, crval=None):
    import numpy as np
    from astropy.io import fits as afits
    sci = afits.ImageHDU(np.full((4, 4), seed, dtype='f4'), name='SCI')
    if crval is not None:
        # A minimal but real celestial WCS on the SCI header, as the datamodel
        # writes it — this is what a re-alignment moves.
        sci.header['CTYPE1'] = 'RA---TAN'
        sci.header['CTYPE2'] = 'DEC--TAN'
        sci.header['CRPIX1'] = 2.0
        sci.header['CRPIX2'] = 2.0
        sci.header['CRVAL1'] = crval[0]
        sci.header['CRVAL2'] = crval[1]
        sci.header['CD1_1'] = -8.6e-6
        sci.header['CD2_2'] = 8.6e-6
    dq = afits.ImageHDU(np.zeros((4, 4), dtype='i4'), name='DQ')
    hdul = afits.HDUList([afits.PrimaryHDU(), sci, dq])
    p = tmp_path / name
    hdul.writeto(p)
    key = storage_key('nircam_exposure', Scope(field='cosmos', filt='f444w'),
                      name, scheme=KeyScheme.CANONICAL)
    return UploadTask(p, key, 'application/fits')


def _realign(path, crval):
    """Rewrite only the WCS — exactly what `cfpipe nircam align` does."""
    from astropy.io import fits as afits
    with afits.open(path, mode='update') as hdul:
        hdul['SCI'].header['CRVAL1'] = crval[0]
        hdul['SCI'].header['CRVAL2'] = crval[1]


def test_sci_dq_resave_with_header_change_skips(tmp_path):
    from astropy.io import fits as afits
    task = _exposure_task(tmp_path)
    digest = sci_dq_hash(task.local_path)
    # Server row: matching science digest, but a DIFFERENT whole-file hash
    # (as after a pipeline re-save).
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + '9' * 64, sci_dq=digest)}
    # Touch a header keyword → whole file changes, science unchanged.
    with afits.open(task.local_path, mode='update') as hdul:
        hdul[0].header['HISTORY'] = 'resaved'
    plan = plan_push([task], rows, progress=False)
    assert plan.unchanged == [task]
    assert plan.to_upload == []


def test_sci_dq_science_change_uploads(tmp_path):
    task = _exposure_task(tmp_path, seed=1.0)
    other = _exposure_task(tmp_path, name='jw002_nrca1.fits', seed=2.0)
    digest_other = sci_dq_hash(other.local_path)
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + '9' * 64,
                              sci_dq=digest_other)}
    plan = plan_push([task], rows, progress=False)
    assert plan.changed == [task]
    # sci_dq identity exposed for registration reuse; whole-file NOT computed
    # at plan time for sci_dq products.
    assert plan.identities[task.r2_key] == sci_dq_hash(task.local_path)
    assert task.r2_key not in plan.whole_file


def test_legacy_exposure_row_without_sci_dq_uploads(tmp_path):
    task = _exposure_task(tmp_path)
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + '9' * 64, sci_dq=None)}
    plan = plan_push([task], rows, progress=False)
    assert plan.changed == [task]


# --- astrometric identity: the re-alignment case ------------------------------

def test_realignment_uploads_despite_identical_science(tmp_path):
    # The regression this component exists for: align rewrites the WCS and does
    # not touch one SCI or DQ pixel, so a science-only identity said "unchanged"
    # and the cloud copy kept its pre-alignment astrometry forever.
    task = _exposure_task(tmp_path, crval=(150.0, 2.0))
    sci, wcs_before = exposure_identity(task.local_path)
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + '9' * 64,
                              sci_dq=sci, wcs_hash=wcs_before)}

    _realign(task.local_path, (150.0001, 2.0001))
    plan = plan_push([task], rows, progress=False)

    assert plan.changed == [task]
    assert plan.to_upload == [task]
    # Science digest is genuinely unchanged — only the WCS moved.
    assert plan.identities[task.r2_key] == sci
    assert plan.wcs_hashes[task.r2_key] != wcs_before


def test_resave_without_wcs_change_still_skips(tmp_path):
    # The property that made the partial digest worth having must survive: a
    # re-save that shifts the whole-file hash but moves neither pixels nor WCS
    # is still an upload we skip.
    from astropy.io import fits as afits
    task = _exposure_task(tmp_path, crval=(150.0, 2.0))
    sci, wcs = exposure_identity(task.local_path)
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + '9' * 64,
                              sci_dq=sci, wcs_hash=wcs)}
    with afits.open(task.local_path, mode='update') as hdul:
        hdul[0].header['DATE'] = '2026-07-27T00:00:00'
    plan = plan_push([task], rows, progress=False)
    assert plan.unchanged == [task]
    assert plan.to_upload == []


def test_both_components_must_match_to_skip(tmp_path):
    task = _exposure_task(tmp_path, crval=(150.0, 2.0))
    sci, wcs = exposure_identity(task.local_path)
    # Right WCS, wrong science → upload.
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + '9' * 64,
                              sci_dq='sha256:' + '0' * 64, wcs_hash=wcs)}
    assert plan_push([task], rows, progress=False).changed == [task]
    # Right science, wrong WCS → upload.
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + '9' * 64,
                              sci_dq=sci, wcs_hash='sha256:' + '0' * 64)}
    assert plan_push([task], rows, progress=False).changed == [task]


def test_composed_identity_arms_the_stat_fast_path(tmp_path, monkeypatch):
    task = _exposure_task(tmp_path, crval=(150.0, 2.0))
    sci, wcs = exposure_identity(task.local_path)
    st = os.stat(task.local_path)
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + '9' * 64,
                              sci_dq=sci, wcs_hash=wcs)}
    local = {task.r2_key: {'pushed_identity': compose_identity(sci, wcs),
                           'pushed_mtime': st.st_mtime,
                           'pushed_size': st.st_size}}

    import campfire.storage.plan as plan_mod

    def _boom(*a, **k):
        raise AssertionError("fast path must not hash")
    monkeypatch.setattr(plan_mod, 'hash_files_parallel', _boom)
    monkeypatch.setattr(plan_mod, 'exposure_identities_parallel', _boom)

    plan = plan_push([task], rows, local_rows=local, progress=False)
    assert plan.unchanged == [task]
    assert plan.fast_skipped == 1


def test_wcsless_exposure_identity_is_bare_science_digest(tmp_path):
    # A file with no WCS cards has no astrometric component; its identity is
    # the science digest alone, so pre-existing pushed_identity records and
    # registry rows keep working untouched.
    task = _exposure_task(tmp_path)
    assert wcs_hash(task.local_path) is None
    assert compose_identity(sci_dq_hash(task.local_path), None) == \
        sci_dq_hash(task.local_path)


# --- legacy-row reconciliation (no stampede on the first upgraded push) --------

def test_legacy_row_reconciles_without_uploading(tmp_path):
    # Row predates wcs_hash. The science digest matches and the cloud object is
    # byte-identical to this file, which proves the cloud copy carries this WCS
    # — so: no upload, and the row learns its digest.
    task = _exposure_task(tmp_path, crval=(150.0, 2.0))
    sci, wcs = exposure_identity(task.local_path)
    whole = compute_file_hash(task.local_path)
    rows = {task.r2_key: _row(task.r2_key, whole, sci_dq=sci, wcs_hash=None)}

    plan = plan_push([task], rows, progress=False)

    assert plan.unchanged == [task]
    assert plan.to_upload == []
    assert plan.backfill == [(task.r2_key, wcs)]
    # The confirmed identity is the COMPOSED token, so the next run fast-paths
    # against a row that now carries both components.
    assert plan.confirmed[0][1] == compose_identity(sci, wcs)


def test_legacy_row_with_differing_bytes_uploads(tmp_path):
    # Same science, no recorded astrometry, and the cloud bytes are NOT ours —
    # unprovable, so it uploads. This is the re-aligned exposure the old
    # identity silently skipped.
    task = _exposure_task(tmp_path, crval=(150.0, 2.0))
    sci, wcs = exposure_identity(task.local_path)
    rows = {task.r2_key: _row(task.r2_key, 'sha256:' + '9' * 64,
                              sci_dq=sci, wcs_hash=None)}

    plan = plan_push([task], rows, progress=False)

    assert plan.changed == [task]
    assert plan.backfill == []
    # The whole-file hash the reconciliation computed is handed to registration
    # rather than thrown away.
    assert plan.whole_file[task.r2_key][0] == compute_file_hash(task.local_path)


def test_upload_order_preserved_across_reconciliation(tmp_path):
    # Reconciliation runs as a second pass; to_upload must still follow the
    # input task order.
    a = _exposure_task(tmp_path, name='jw_a_nrca1.fits', crval=(150.0, 2.0))
    b = _exposure_task(tmp_path, name='jw_b_nrca1.fits', seed=2.0)
    c = _exposure_task(tmp_path, name='jw_c_nrca1.fits', seed=3.0,
                       crval=(151.0, 3.0))
    rows = {
        # a: legacy row, bytes differ → reconciliation says upload.
        a.r2_key: _row(a.r2_key, 'sha256:' + '9' * 64,
                       sci_dq=sci_dq_hash(a.local_path), wcs_hash=None),
        # b: no row at all → new.
        # c: identity mismatch → changed in the first pass.
        c.r2_key: _row(c.r2_key, 'sha256:' + '9' * 64,
                       sci_dq='sha256:' + '0' * 64, wcs_hash='sha256:' + '1' * 64),
    }
    plan = plan_push([a, b, c], rows, progress=False)
    assert plan.to_upload == [a, b, c]
