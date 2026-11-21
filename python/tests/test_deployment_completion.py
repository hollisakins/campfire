"""``deployments.completed_at``: the completion marker every deploy path stamps.

The deploy CLI opens its ``deployments`` row before uploading (the registration
flusher tags storage rows with the id as batches land), so ``status`` alone
cannot tell a deploy that died part-way from a finished one. Each path stamps
``completed_at`` as its last write, and only when no upload failed. These tests
drive the paths against stubs and a :class:`FakePostgrest`, recording the order
of the writes that matter.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import campfire.deploy.deploy as dep
import campfire.deploy.fields as fields_mod
import campfire.deploy.nircam as nircam
import campfire.deploy.push as push_mod
import campfire.deploy.rate_exposures as rate_mod
import campfire.deploy.registry as registry_mod
from campfire.deploy.r2 import UploadTask
from campfire.storage.plan import PushPlan
from campfire.deploy.supabase import mark_deployment_completed
from tests.fakes import FakePostgrest


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------

def _completion_stamps(client: FakePostgrest) -> list[int]:
    """Deployment ids whose completed_at the client was asked to set."""
    out = []
    for u in client.updates:
        if u.table == 'deployments' and 'completed_at' in u.patch:
            out.extend(f.value for f in u.filters if f.column == 'id')
    return out


class _Log(list):
    """Ordered record of the writes whose sequence the marker depends on."""


class _FakeLease:
    def __init__(self, log, *a, **k):
        self._log = log

    def begin(self):
        return {}

    def heartbeat(self):
        pass

    def claim(self):
        self._log.append('claim')
        return {}


class _FakeFlusher:
    flushed = 0

    def add(self, *a, **k):
        pass

    def flush(self):
        pass


def _install_push(monkeypatch, log, *, to_upload, unchanged):
    plan = PushPlan(to_upload=list(to_upload), new=list(to_upload),
                    unchanged=list(unchanged))
    monkeypatch.setattr(push_mod, 'open_reducer_store', lambda: object())
    monkeypatch.setattr(push_mod, 'plan_remote_push', lambda *a, **k: (plan, {}))
    monkeypatch.setattr(push_mod, 'registration_flusher',
                        lambda *a, **k: _FakeFlusher())

    def _set_active(client, keys, deployment_id):
        log.append(('repoint', tuple(keys), deployment_id))
        return len(keys)
    monkeypatch.setattr(registry_mod, 'set_active_deployment', _set_active)
    return plan


def _uploader(log, *, failed):
    def _upload(config, tasks, **kw):
        log.append('upload')
        return len(tasks) - failed, failed, ['boom'] * failed
    return _upload


def _track_completion(monkeypatch, module, log):
    """Wrap the module's mark_deployment_completed to log its position."""
    real = module.mark_deployment_completed

    def _mark(client, deployment_id):
        log.append(('complete', deployment_id))
        return real(client, deployment_id)
    monkeypatch.setattr(module, 'mark_deployment_completed', _mark)


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

def test_mark_deployment_completed_stamps_the_row():
    client = FakePostgrest(tables={'deployments': [{'id': 7, 'completed_at': None}]})
    mark_deployment_completed(client, 7)
    assert _completion_stamps(client) == [7]
    assert client.rows('deployments')[0]['completed_at']


def test_mark_deployment_completed_without_a_row_is_a_no_op():
    client = FakePostgrest()
    mark_deployment_completed(client, None)
    assert client.updates == []


# ---------------------------------------------------------------------------
# NIRSpec finals deploy: the tail of deploy_observation
# ---------------------------------------------------------------------------

def _plan(unchanged_keys):
    return SimpleNamespace(unchanged=[SimpleNamespace(r2_key=k) for k in unchanged_keys])


def test_nirspec_finish_repoints_then_completes(monkeypatch):
    log = _Log()
    _install_push(monkeypatch, log, to_upload=[], unchanged=[])
    _track_completion(monkeypatch, dep, log)
    client = FakePostgrest(tables={'deployments': [{'id': 5}]})

    dep._finish_nirspec_deployment(client, 5, _plan(['b', 'a']), draft=False, failed=0)

    # The marker is the last write: after the unchanged objects moved over.
    assert log == [('repoint', ('a', 'b'), 5), ('complete', 5)]
    assert _completion_stamps(client) == [5]


def test_nirspec_finish_draft_completes_without_repointing(monkeypatch):
    log = _Log()
    _install_push(monkeypatch, log, to_upload=[], unchanged=[])
    _track_completion(monkeypatch, dep, log)
    client = FakePostgrest(tables={'deployments': [{'id': 5}]})

    dep._finish_nirspec_deployment(client, 5, _plan(['a']), draft=True, failed=0)

    assert log == [('complete', 5)]


def test_nirspec_finish_supabase_only_completes(monkeypatch):
    # --supabase-only has no push plan; the catalog writes are the whole deploy.
    log = _Log()
    _track_completion(monkeypatch, dep, log)
    client = FakePostgrest(tables={'deployments': [{'id': 5}]})

    dep._finish_nirspec_deployment(client, 5, None, draft=False, failed=0)

    assert _completion_stamps(client) == [5]


def test_nirspec_finish_with_failed_uploads_stays_incomplete(monkeypatch):
    log = _Log()
    _install_push(monkeypatch, log, to_upload=[], unchanged=[])
    client = FakePostgrest(tables={'deployments': [{'id': 5}]})

    dep._finish_nirspec_deployment(client, 5, _plan(['a']), draft=False, failed=2)

    assert log == [('repoint', ('a',), 5)]
    assert _completion_stamps(client) == []


def test_nirspec_finish_without_a_deployment_row_writes_nothing(monkeypatch):
    log = _Log()
    _install_push(monkeypatch, log, to_upload=[], unchanged=[])
    client = FakePostgrest()

    dep._finish_nirspec_deployment(client, None, _plan(['a']), draft=False, failed=0)

    assert log == [] and client.updates == []


def test_deploy_observation_finishes_through_the_helper():
    # The tail of the finals deploy (_execute_nirspec_plan, which carries out the
    # plan deploy_observation built) is _finish_nirspec_deployment, called after
    # the latest-deployment pointer and before the lease claim releases the
    # scope. A cheap structural check here; the stubbed end-to-end run in
    # test_nirspec_deploy_plan.py checks the stamp is the last write.
    import inspect
    src = inspect.getsource(dep._execute_nirspec_plan)
    i_latest = src.index('update_latest_deployment(sb, obs_name, deployment_id)')
    i_finish = src.index('_finish_nirspec_deployment(')
    i_claim = src.index('claim = lease.claim()')
    assert i_latest < i_finish < i_claim
    assert 'mark_deployment_completed' not in src  # one place stamps it


# ---------------------------------------------------------------------------
# NIRSpec intermediates-only deploy (no stage3 finals yet)
# ---------------------------------------------------------------------------

_RATE = 'jw01234001001_02101_00001_nrs1_rate.fits'


@pytest.fixture
def intermediates(monkeypatch, tmp_path):
    log = _Log()
    rate = tmp_path / _RATE
    rate.write_bytes(b'x')
    client = FakePostgrest(next_id=11)

    monkeypatch.setattr(dep, 'discover_spectrum_exposures', lambda d: [])
    monkeypatch.setattr(dep, 'discover_rate_files', lambda d: [rate])
    monkeypatch.setattr(dep, 'load_observations',
                        lambda: {'obs': {'field': 'cosmos', 'program': 'prog'}})
    monkeypatch.setattr(dep, 'load_programs', lambda: {})
    monkeypatch.setattr(dep, 'validate_program_slug', lambda *a, **k: None)
    monkeypatch.setattr(dep, 'get_supabase_client', lambda config: client)
    monkeypatch.setattr(dep, 'get_lifecycle_status', lambda sb: {'enabled': True})
    monkeypatch.setattr(dep, 'get_user_id_from_token', lambda config: 'u1')
    monkeypatch.setattr(dep, 'upsert_programs', lambda *a, **k: None)
    monkeypatch.setattr(dep, 'upsert_observation', lambda *a, **k: None)
    monkeypatch.setattr(dep, 'ScopeLease', lambda *a, **k: _FakeLease(log))
    monkeypatch.setattr(dep, '_read_exposure_provenance', lambda files: (None, None, None))
    monkeypatch.setattr(rate_mod, 'build_rate_exposure_records', lambda *a, **k: [])
    monkeypatch.setattr(rate_mod, 'upsert_rate_exposures',
                        lambda *a, **k: log.append('rate_rows') or 0)
    _track_completion(monkeypatch, dep, log)

    def run(*, failed=0):
        task = UploadTask(rate, 'nirspec/obs/rate/' + _RATE, 'application/fits')
        _install_push(monkeypatch, log, to_upload=[task], unchanged=[])
        monkeypatch.setattr(dep, 'upload_files_parallel', _uploader(log, failed=failed))
        dep._deploy_intermediates_only('obs', tmp_path, {})
        return client, log
    return run


def test_intermediates_only_completes_after_its_last_write(intermediates):
    client, log = intermediates()

    # Recorded as a draft up front, stamped complete after the triage rows.
    dep_row = client.rows('deployments')[0]
    assert dep_row['status'] == 'draft' and dep_row['completed_at']
    assert log == ['upload', 'rate_rows', ('complete', 11), 'claim']
    assert _completion_stamps(client) == [11]


def test_intermediates_only_with_a_failed_upload_stays_incomplete(intermediates):
    client, log = intermediates(failed=1)

    assert 'completed_at' not in client.rows('deployments')[0]
    assert _completion_stamps(client) == []
    assert log[-1] == 'claim'


# ---------------------------------------------------------------------------
# NIRCam field deploy
# ---------------------------------------------------------------------------

@pytest.fixture
def field_deploy(monkeypatch, tmp_path):
    log = _Log()
    client = FakePostgrest(next_id=21)
    layout = tmp_path / 'cosmos_layout.png'
    layout.write_bytes(b'png')
    layout_task = UploadTask(layout, 'nircam/cosmos/cosmos_layout.png', 'image/png')

    monkeypatch.setattr(nircam, '_resolve_nircam_dirs', lambda f: {'products': tmp_path})
    monkeypatch.setattr(nircam, '_discover_filters', lambda d: ['f444w'])
    monkeypatch.setattr(nircam, 'discover_exposures', lambda d, f: {})
    monkeypatch.setattr(nircam, 'discover_expmap_tasks', lambda *a: [])
    monkeypatch.setattr(nircam, 'discover_layout_tasks', lambda *a: [layout_task])
    monkeypatch.setattr(nircam, 'build_upload_tasks', lambda *a: [])
    monkeypatch.setattr(nircam, 'build_fits_upload_tasks', lambda *a: [])
    monkeypatch.setattr(nircam, 'discover_mosaics', lambda *a: [])
    monkeypatch.setattr(nircam, 'deployable_mosaics', lambda m: m)
    monkeypatch.setattr(nircam, 'scope_mosaics_to_fields_toml', lambda m, f: m)
    monkeypatch.setattr(nircam, '_read_field_provenance', lambda *a, **k: (None, None, None))
    monkeypatch.setattr(nircam, 'get_supabase_client', lambda config: client)
    monkeypatch.setattr(nircam, 'get_user_id_from_token', lambda config: 'u1')
    monkeypatch.setattr(nircam, 'ScopeLease', lambda *a, **k: _FakeLease(log))
    monkeypatch.setattr(nircam, '_upsert_exposures', lambda *a, **k: 0)  # new rows
    monkeypatch.setattr(fields_mod, 'upsert_field_on_deploy',
                        lambda *a, **k: log.append('field_row'))
    _track_completion(monkeypatch, nircam, log)

    def run(*, failed=0, draft=False):
        _install_push(monkeypatch, log, to_upload=[layout_task],
                      unchanged=[UploadTask(Path('old.png'), 'nircam/cosmos/old.png',
                                            'image/png')])
        monkeypatch.setattr(nircam, 'upload_files_parallel', _uploader(log, failed=failed))
        nircam.deploy_nircam('cosmos', {}, draft=draft, auto_approve=True)
        return client, log
    return run


def test_field_deploy_completes_after_its_last_write(field_deploy):
    client, log = field_deploy()

    assert client.rows('deployments')[0]['completed_at']
    assert log == ['upload', ('repoint', ('nircam/cosmos/old.png',), 21),
                   'field_row', ('complete', 21), 'claim']


def test_field_deploy_draft_completes(field_deploy):
    client, log = field_deploy(draft=True)

    assert client.rows('deployments')[0]['status'] == 'draft'
    assert _completion_stamps(client) == [21]
    assert not any(isinstance(e, tuple) and e[0] == 'repoint' for e in log)


def test_field_deploy_with_failed_exposure_upload_stays_incomplete(field_deploy):
    client, _log = field_deploy(failed=1)
    assert _completion_stamps(client) == []


# Mosaics share the field deploy's one upload pass, so a failed mosaic upload is
# the failed-upload case above; test_nircam_deploy_plan.py runs it with mosaics.
