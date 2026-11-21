"""The NIRSpec deploy plan: one plan drives both ``--dry-run`` and the live deploy.

The dry run used to be a hand-written list of print statements that fell
behind the live path (the 1-D spectrum sidecars, the spectrum-exposure
intermediates and the detector rate files were uploaded but never shown).
These tests pin the contract that replaced it: the builder lists every
product, the dry run writes nothing, and the live run uploads exactly what
the plan's registry check said it would.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from campfire.deploy import deploy as dmod
from campfire.deploy.deploy_plan import format_bytes, render_nirspec_plan as render_plan
from campfire.deploy.r2 import UploadTask
from campfire.storage.plan import PushPlan


# ---------------------------------------------------------------------------
# Fixtures: a fake observation on disk + stubbed discovery/generation
# ---------------------------------------------------------------------------

def _touch(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * size)
    return path


class _FakeSummary:
    """Just enough of the summary Table: ``len``, ``.meta`` and a column."""

    def __init__(self, meta):
        self.meta = meta

    def __len__(self):
        return 2

    def __getitem__(self, col):
        return {'program_id': [1234, 1234]}[col]


@pytest.fixture
def fake_obs(tmp_path, monkeypatch):
    """Two sources, one grating each; every product kind present on disk."""
    obs = 'demo_p1'
    obs_dir = tmp_path / obs
    finals = [_touch(obs_dir / f'{obs}_{sid}_prism_spec.fits', 1000) for sid in (1, 2)]
    zfits = [_touch(obs_dir / f'{obs}_{sid}_prism_zfit.fits') for sid in (1, 2)]
    lines = [_touch(obs_dir / f'{obs}_1_prism_lines.fits')]
    exposures = [_touch(obs_dir / 'exp' / f'jw01_{i}_{sid}.fits', 5000)
                 for i in range(3) for sid in (1, 2)]
    rates = [_touch(obs_dir / 'rate' / f'jw01_{i}_nrs1_rate.fits', 20000) for i in range(2)]
    targets = [{'object_id': f'{obs}_{sid}', 'source_id': str(sid)} for sid in (1, 2)]
    spectra = [{'target_id': f'{obs}_{sid}', 'grating': 'prism', 'filter': 'clear',
                'fits_path': f'nirspec/{obs}/{p.name}'} for sid, p in zip((1, 2), finals)]

    def gen_products(spec_path, out_dir):
        full = _touch(out_dir / f'{spec_path.stem}.json', 300)
        one_d = _touch(out_dir / f'{spec_path.stem}_spec_1d.json', 30)
        return full, one_d, {'thumbnail_svg_fnu': '<svg/>', 'thumbnail_svg_flambda': '<svg/>'}

    def gen_zfit(path, out_dir):
        return _touch(out_dir / f'{path.stem}.json')

    def lines_tasks(obs_name, paths, out_dir):
        out = []
        for p in paths:
            out.append(UploadTask(p, f'nirspec/{obs_name}/{p.name}', 'application/fits'))
            j = _touch(out_dir / f'{p.stem}.json')
            out.append(UploadTask(j, f'nirspec/{obs_name}/{j.name}', 'application/json'))
        return out

    stubs = {
        'get_field': lambda s: 'cosmos',
        'get_program_slug': lambda s: 'demo',
        'get_unique_objects': lambda s: [dict(t) for t in targets],
        'get_spectra_records': lambda s, o: [dict(r) for r in spectra],
        'get_spec_paths': lambda s, d: finals,
        'get_zfit_paths': lambda s, d: zfits,
        'get_lines_paths': lambda s, d: lines,
        'generate_spectrum_products': gen_products,
        'generate_thumbnails_from_fits': lambda p: {'thumbnail_svg_fnu': '<svg/>'},
        'generate_zfit_json': gen_zfit,
        'lines_upload_tasks': lines_tasks,
        'split_auto_line_fits': lambda paths: (list(paths), []),
        'line_fit_versions': lambda paths: [],
        'discover_spectrum_exposures': lambda d: exposures,
        'discover_rate_files': lambda d: rates,
        'discover_pointings_ecsv': lambda d, o: d / 'pointings.ecsv',
        'load_pointings_ecsv': lambda p: [{'msametid': 1}, {'msametid': 2}],
        'discover_shutters_ecsv': lambda d, o: d / 'shutters.ecsv',
        'load_shutters_ecsv': lambda p: [{'object_id': f'{obs}_1'}] * 5,
        'resolve_imaging_config': lambda: None,
    }
    for name, fn in stubs.items():
        monkeypatch.setattr(dmod, name, fn)
    import campfire.deploy.config as cfg
    monkeypatch.setattr(cfg, 'resolve_photometry_config', lambda _: None)

    summary = _FakeSummary({'cfpipe_version': '1.0.0', 'crds_context': 'jwst_1614.pmap'})
    return SimpleNamespace(obs=obs, obs_dir=obs_dir, summary=summary, finals=finals,
                           exposures=exposures, rates=rates, targets=targets)


def _build(fake, tmp_path, **kw):
    temp = tmp_path / 'deploy_temp'
    temp.mkdir(exist_ok=True)
    opts = dict(supabase_only=False, force_overwrite=False, include_shutters=True,
                include_photometry=True, skip_astrometry=False, source_ids=None,
                draft=False, defer_rebuild=False)
    opts.update(kw)
    return dmod._build_nirspec_plan(fake.obs, fake.obs_dir, fake.summary, temp, **opts)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def test_plan_lists_every_uploaded_product_kind(fake_obs, tmp_path):
    plan = _build(fake_obs, tmp_path)
    counts = {g.label: len(g.tasks) for g in plan.file_groups}
    assert counts == {
        'Final spectra (FITS)': 2,
        'Spectrum JSON (full + 1-D)': 4,     # the 1-D sidecar the old dry run omitted
        'Redshift fits (zfit JSON)': 2,
        'Line fits (FITS + JSON)': 2,
        'Spectrum-exposure intermediates': 6,
        'Detector rate files': 2,
    }
    kinds = {t.r2_key.rsplit('/', 1)[-1] for t in plan.upload_tasks}
    assert any(k.endswith('_spec_1d.json') for k in kinds)
    assert plan.n_grid_rows == 6 and plan.n_rate_rows == 2
    assert len(plan.pointings) == 2 and len(plan.shutters) == 5
    # thumbnails are embedded in the spectra records the catalog upsert sends
    assert all('thumbnail_svg_fnu' in r for r in plan.spectra)


def test_source_ids_narrow_exposures_but_never_rate_files(fake_obs, tmp_path):
    plan = _build(fake_obs, tmp_path, source_ids=[1])
    counts = {g.label: len(g.tasks) for g in plan.file_groups}
    assert counts['Spectrum-exposure intermediates'] == 3
    assert counts['Detector rate files'] == 2


def test_supabase_only_plans_no_files_but_keeps_thumbnails(fake_obs, tmp_path):
    plan = _build(fake_obs, tmp_path, supabase_only=True)
    assert plan.upload_tasks == []
    assert plan.n_grid_rows == plan.n_rate_rows == 0
    assert all('thumbnail_svg_fnu' in r for r in plan.spectra)


def test_no_shutters_flag_is_shown_not_silently_dropped(fake_obs, tmp_path):
    plan = _build(fake_obs, tmp_path, include_shutters=False)
    assert plan.shutters is None and plan.shutters_skipped
    assert 'skipped (--no-shutters)' in render_plan(plan, dry_run=True)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _push_for(plan, unchanged_labels=()):
    push = PushPlan()
    for g in plan.file_groups:
        for t in g.tasks:
            if g.label in unchanged_labels:
                push.unchanged.append(t)
            else:
                push.new.append(t)
                push.to_upload.append(t)
    return push


def test_render_names_what_each_count_is(fake_obs, tmp_path):
    plan = _build(fake_obs, tmp_path)
    plan.existing_targets = 1
    plan.push = _push_for(plan, unchanged_labels=('Detector rate files',))
    text = render_plan(plan, dry_run=True)

    assert 'dry run: nothing will be written' in text
    assert 'Targets (source × observation)' in text and '1 new, 1 updated' in text
    assert 'Spectra (target × grating)' in text
    assert 'MSA pointings' in text and 'stored on the observation row' in text
    assert 'MSA shutters' in text and "replace this observation's rows" in text
    assert 'raw MSA positions (no imaging.toml)' in text
    assert 'Review rows' in text and '2 rate-mask triage, 6 exposure grid' in text
    assert 'Photometry: skipped (no photometry.toml)' in text
    # per-group upload / already-in-cloud split; total row
    rate_row = next(line for line in text.splitlines() if 'Detector rate files' in line)
    assert rate_row.split()[-2:] == ['0', '2']
    total_row = next(line for line in text.splitlines() if line.strip().startswith('Total'))
    assert total_row.split()[1] == '18'
    assert '16 file(s) to upload' in text


def test_render_without_registry_check_says_so(fake_obs, tmp_path):
    plan = _build(fake_obs, tmp_path)
    text = render_plan(plan, dry_run=False)
    assert 'not compared with the cloud registry' in text
    assert 'dry run' not in text


def test_render_warnings_lead_the_plan(fake_obs, tmp_path):
    fake_obs.summary.meta['cfpipe_version'] = '1.0.1.dev3+gabc'
    plan = _build(fake_obs, tmp_path, force_overwrite=True)
    text = render_plan(plan, dry_run=True)
    assert '! Non-release pipeline version: 1.0.1.dev3+gabc' in text
    assert '! --force-overwrite' in text
    assert text.index('! Non-release') < text.index('Files → OSN')


def test_format_bytes():
    assert format_bytes(512) == '512 B'
    assert format_bytes(1536) == '1.5 KB'
    assert format_bytes(3 * 1024 ** 3) == '3.0 GB'


# ---------------------------------------------------------------------------
# deploy_observation: dry run writes nothing; live run executes the plan
# ---------------------------------------------------------------------------

class _RecordingClient:
    """Stands in for the Supabase client; any table write fails the test."""

    def table(self, name):
        raise AssertionError(f"unexpected table access: {name}")

    def rpc(self, name, *a, **k):
        raise AssertionError(f"unexpected rpc: {name}")


@pytest.fixture
def entry_stubs(fake_obs, monkeypatch):
    """Stub what deploy_observation reads before it builds the plan."""
    monkeypatch.setattr(dmod, 'resolve_obs_dir', lambda o: fake_obs.obs_dir)
    monkeypatch.setattr(dmod, 'load_summary', lambda d, o, required=False: fake_obs.summary)
    monkeypatch.setattr(dmod, 'stale_summary_rows', lambda s, d: [])
    monkeypatch.setattr(dmod, 'load_programs', lambda: {'demo': {}})
    monkeypatch.setattr(dmod, 'validate_program_slug', lambda *a: None)
    monkeypatch.setattr(dmod, 'check_existing_objects',
                        lambda sb, ids: {ids[0]: {'target_id': ids[0]}})
    import campfire.deploy.push as push
    seen = {}

    def fake_plan_remote_push(sb, store, tasks, *, backend, progress=True,
                              apply_backfill=False, **kw):
        seen.setdefault('calls', []).append(
            {'n': len(tasks), 'apply_backfill': apply_backfill})
        p = PushPlan()
        for t in tasks:
            (p.unchanged if 'rate' in t.r2_key else p.new).append(t)
        p.to_upload = list(p.new)
        return p, {}
    monkeypatch.setattr(push, 'plan_remote_push', fake_plan_remote_push)
    monkeypatch.setattr(push, 'open_reducer_store', lambda: None)
    return seen


def test_dry_run_prints_the_plan_and_writes_nothing(fake_obs, entry_stubs, monkeypatch, capsys):
    monkeypatch.setattr(dmod, 'get_supabase_client', lambda c: _RecordingClient())
    monkeypatch.setattr(dmod, 'upload_files_parallel',
                        lambda *a, **k: pytest.fail('dry run uploaded'))
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('dry run prompted'))

    result = dmod.deploy_observation(fake_obs.obs, {}, dry_run=True)

    assert result == {'field': 'cosmos', 'needs_reconcile': False}
    out = capsys.readouterr().out
    assert 'Deploy plan: demo_p1' in out
    assert 'Spectrum-exposure intermediates' in out
    # the registry check never asks for the wcs_hash backfill write
    assert entry_stubs['calls'] == [{'n': 18, 'apply_backfill': False}]
    assert not (fake_obs.obs_dir / '.deploy_temp').exists()


def test_declining_the_prompt_writes_nothing(fake_obs, entry_stubs, monkeypatch, capsys):
    monkeypatch.setattr(dmod, 'get_supabase_client', lambda c: _RecordingClient())
    monkeypatch.setattr('builtins.input', lambda *a: 'n')
    result = dmod.deploy_observation(fake_obs.obs, {})
    assert result['needs_reconcile'] is False
    assert 'Aborted.' in capsys.readouterr().out


def test_live_run_uploads_exactly_the_planned_files(fake_obs, entry_stubs, monkeypatch, capsys):
    import campfire.deploy.nirspec_flags as flags
    import campfire.deploy.push as push
    import campfire.deploy.rate_exposures as rate_mod
    import campfire.deploy.reconcile as rec
    import campfire.deploy.registry as reg
    import campfire.deploy.spectrum_grid as grid

    calls: list[str] = []
    uploaded: list = []

    class Lease:
        version_at_start = 1

        def __init__(self, *a, **k):
            pass

        def begin(self):
            calls.append('lease')

        def heartbeat(self):
            pass

        def claim(self):
            return {}

    class Flusher:
        def add(self, t):
            pass

        def flush(self):
            pass

    def upload(config, tasks, **kw):
        calls.append('upload')
        uploaded.extend(tasks)
        return len(tasks), 0, []

    def note(name, ret=None):
        def fn(*a, **k):
            calls.append(name)
            return ret
        return fn

    monkeypatch.setattr(dmod, 'get_supabase_client', lambda c: object())
    monkeypatch.setattr('builtins.input', lambda *a: 'y')
    monkeypatch.setattr(dmod, 'upsert_programs', note('program'))
    monkeypatch.setattr(dmod, 'load_observations', lambda: {})
    monkeypatch.setattr(dmod, 'upsert_observation', note('observation'))
    monkeypatch.setattr(dmod, 'get_user_id_from_token', lambda c: 'u1')
    monkeypatch.setattr(dmod, 'ScopeLease', Lease)
    monkeypatch.setattr(dmod, 'insert_deployment', note('deployment', 616))
    monkeypatch.setattr(dmod, '_load_config_snapshot', lambda *a: None)
    monkeypatch.setattr(dmod, '_load_stuck_shutters', lambda *a: None)
    monkeypatch.setattr(dmod, 'resolve_reference_obs_dir', lambda o: fake_obs.obs_dir)
    monkeypatch.setattr(push, 'registration_flusher', lambda *a, **k: Flusher())
    monkeypatch.setattr(dmod, 'upload_files_parallel', upload)
    monkeypatch.setattr(dmod, 'batch_upsert_objects', note('targets', (2, [7], 0)))
    monkeypatch.setattr(dmod, 'batch_upsert_spectra', note('spectra', (2, set())))
    monkeypatch.setattr(dmod, 'publish_line_fits', note('line_fits'))
    monkeypatch.setattr(dmod, 'recompute_target_aggregates', note('aggregates', 2))
    monkeypatch.setattr(dmod, 'update_observation_pointings', note('pointings'))
    monkeypatch.setattr(dmod, 'db_deploy_shutters', note('shutters', 5))
    monkeypatch.setattr(rate_mod, 'upsert_rate_exposures', note('rate_rows', 2))
    monkeypatch.setattr(rate_mod, 'build_rate_exposure_records', lambda *a, **k: [])
    monkeypatch.setattr(grid, 'upsert_spectrum_exposures', note('grid_rows', 6))
    monkeypatch.setattr(grid, 'build_spectrum_exposure_records', lambda *a, **k: [])
    monkeypatch.setattr(flags, 'push_review_flags', note('review_flags', 0))
    monkeypatch.setattr(rec, 'reconcile_field_objects',
                        note('reconcile', (10, {'updated': 2, 'inserted': 0}, set())))
    monkeypatch.setattr(dmod, 'refresh_filter_options', note('refresh', True))
    monkeypatch.setattr(dmod, 'refresh_programs_overview', note('refresh', True))
    monkeypatch.setattr(dmod, 'refresh_observations_overview', note('refresh', True))
    monkeypatch.setattr(dmod, 'update_latest_deployment', note('finalize'))
    monkeypatch.setattr(dmod, 'log_deploy_event', note('audit'))
    monkeypatch.setattr(reg, 'set_active_deployment', note('repoint', 2))

    result = dmod.deploy_observation(fake_obs.obs, {})

    assert result == {'field': 'cosmos', 'needs_reconcile': True}
    # the plan is re-checked under the lease, with the backfill write this time
    assert entry_stubs['calls'] == [{'n': 18, 'apply_backfill': False},
                                    {'n': 18, 'apply_backfill': True}]
    assert len(uploaded) == 16 and not any('rate' in t.r2_key for t in uploaded)
    # pointings and shutters are part of the catalog phase, before the reconcile
    assert calls.index('lease') < calls.index('deployment') < calls.index('upload')
    assert (calls.index('targets') < calls.index('pointings') < calls.index('shutters')
            < calls.index('reconcile') < calls.index('finalize'))
    out = capsys.readouterr().out
    assert 'Deployment #616 opened (published)' in out
    assert 'Deploy complete: demo_p1 → deployment #616 (published)' in out
    assert 'files:    16 uploaded' in out and '2 already in the cloud' in out
    assert 'objects:  2 updated, 0 new (of 10 in cosmos)' in out
    assert not (fake_obs.obs_dir / '.deploy_temp').exists()
