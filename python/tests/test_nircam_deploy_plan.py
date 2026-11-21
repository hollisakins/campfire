"""The NIRCam field deploy plan: one plan drives ``--dry-run`` and the live deploy.

Same contract as the NIRSpec plan (test_nirspec_deploy_plan.py): the builder
lists every product, the dry run writes nothing, and the live run uploads, in
one pass, exactly what the plan's registry check said it would. Mosaics used
to be planned and uploaded in a second pass with its own progress bars; they
are now one more file group.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from campfire.deploy import nircam as nc
from campfire.deploy.deploy_plan import render_nircam_plan
from campfire.deploy.r2 import UploadTask
from campfire.storage.plan import PushPlan


def _touch(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * size)
    return path


@pytest.fixture
def fake_field(tmp_path, monkeypatch):
    """Field 'demo', two filters, two exposures each, one tile of mosaics."""
    field = 'demo'
    prod = tmp_path / 'products' / 'nircam' / field
    exposures = {}
    for filt in ('f115w', 'f444w'):
        for i in range(2):
            base = f'jw01_{i}_nrca1_{filt}'
            exposures[(filt, base)] = {
                'path': _touch(prod / filt / f'{base}_cal.fits', 4000),
                'detector': 'nrca1', 'visit': '001', 'date_obs': '2025-01-01',
                'ra_center': 150.0, 'dec_center': 2.0,
                'stage': 'bkg' if i else 'image2',
            }
    mosaics = []
    for filt in ('f115w', 'f444w'):
        for ext in ('sci', 'err'):
            p = _touch(prod / filt / f'mosaic_{filt}_{ext}.fits', 50000)
            mosaics.append({'path': p, 'storage_key': f'nircam/{field}/{filt}/{p.name}.gz',
                            'content_type': 'application/gzip', 'tile': 't1',
                            'filter': filt, 'pixel_scale': '30mas', 'extension': ext,
                            'epoch': '', 'mosaic_name': f'mosaic_{filt}'})

    def key(p):
        return f'nircam/{field}/{p.parent.name}/{p.name}'

    def png_tasks(dirs, fld, filters):
        out = []
        for (filt, base) in exposures:
            for kind in ('thumb', 'full'):
                p = _touch(prod / filt / f'{base}_{kind}.png', 200)
                out.append((UploadTask(p, key(p), 'image/png'), base, filt, kind))
        return out

    expmap = _touch(prod / 'expmap_f444w.fits', 1000)
    layout = _touch(prod / 'demo_layout.png', 300)
    stubs = {
        '_resolve_nircam_dirs': lambda f: {'products': prod},
        '_discover_filters': lambda d: ['f115w', 'f444w'],
        'discover_exposures': lambda d, f: {k: v for k, v in exposures.items() if k[0] in f},
        'discover_expmap_tasks': lambda d, f, fl: [UploadTask(expmap, key(expmap), 'application/fits')],
        'discover_layout_tasks': lambda d, f: [UploadTask(layout, key(layout), 'image/png')],
        'discover_mosaics': lambda d, f, fl: [m for m in mosaics if m['filter'] in fl],
        'scope_mosaics_to_fields_toml': lambda ms, f: ms,
        'build_upload_tasks': png_tasks,
        '_read_full_png_dimensions': lambda p: (2048, 2048),
        'build_fits_upload_tasks': lambda f, exps: [
            UploadTask(v['path'], key(v['path']), 'application/fits') for v in exps.values()],
        'discover_mosaic_thumbnail_tasks': lambda ms, f: [
            UploadTask(_touch(prod / m['filter'] / f"{m['mosaic_name']}_thumb.png"),
                       f"nircam/{f}/{m['filter']}/{m['mosaic_name']}_thumb.png", 'image/png')
            for m in ms if m['extension'] == 'sci'],
        '_read_field_provenance': lambda *a, **k: ('1.0.0', '1.18.0', 'jwst_1614.pmap'),
    }
    for name, fn in stubs.items():
        monkeypatch.setattr(nc, name, fn)
    import campfire.deploy.fields as fields
    monkeypatch.setattr(fields, 'load_fields_toml', lambda: {field: {}})
    return SimpleNamespace(field=field, mosaics=mosaics, exposures=exposures)


def _push(plan, unchanged=lambda k: False):
    p = PushPlan()
    for t in plan.upload_tasks:
        if unchanged(t.r2_key):
            p.unchanged.append(t)
        else:
            p.new.append(t)
            p.to_upload.append(t)
            p.stats[t.r2_key] = (0.0, 123)
    return p


# ---------------------------------------------------------------------------
# Builder + renderer
# ---------------------------------------------------------------------------

def test_plan_lists_every_product_including_mosaics(fake_field):
    plan = nc._build_nircam_plan(fake_field.field, None, draft=False, skip_mosaics=False)
    counts = {g.label: len(g.tasks) for g in plan.file_groups}
    assert counts == {
        'Exposures (FITS)': 4,
        'Exposure previews (PNG)': 8,
        'Exposure maps (FITS)': 1,
        'Layout plots': 1,
        'Mosaics (FITS)': 4,
        'Mosaic thumbnails (PNG)': 2,
    }
    assert len(plan.records) == 4
    assert all(r['png_path'] and r['full_png_path'] for r in plan.records)
    assert plan.cfpipe_version == '1.0.0'


def test_no_mosaics_drops_the_mosaic_groups_and_says_so(fake_field):
    plan = nc._build_nircam_plan(fake_field.field, None, draft=False, skip_mosaics=True)
    assert plan.mosaics == []
    text = render_nircam_plan(plan, dry_run=True)
    assert 'Mosaics: skipped (--no-mosaics)' in text
    assert 'Mosaic index' not in text


def test_unknown_filter_is_a_plan_warning(fake_field):
    plan = nc._build_nircam_plan(fake_field.field, ['f115w', 'f999w'],
                                 draft=False, skip_mosaics=False)
    assert plan.filters == ['f115w']
    assert '! No products for filter(s): f999w' in render_nircam_plan(plan, dry_run=True)


def test_render_names_each_count(fake_field):
    plan = nc._build_nircam_plan(fake_field.field, None, draft=False, skip_mosaics=False)
    plan.existing_exposures = 3
    plan.push = _push(plan, unchanged=lambda k: 'f115w' in k and k.endswith('.gz'))
    text = render_nircam_plan(plan, dry_run=True)
    assert 'Deploy plan: field demo  (dry run: nothing will be written)' in text
    assert '1 new, 3 updated; review state kept' in text
    assert 'latest stage: ' in text and 'bkg 2' in text and 'image2 2' in text
    # published: every present mosaic is (re-)indexed, unchanged ones included
    mos_row = next(line for line in text.splitlines() if 'Mosaic index' in line)
    assert mos_row.split()[2] == '4'
    mos_files = next(line for line in text.splitlines() if 'Mosaics (FITS)' in line)
    assert mos_files.split()[-2:] == ['2', '2']
    assert 'Field registry' in text and 'fields.toml config + survey area' in text


def test_draft_indexes_only_new_or_changed_mosaics(fake_field):
    plan = nc._build_nircam_plan(fake_field.field, None, draft=True, skip_mosaics=False)
    plan.push = _push(plan, unchanged=lambda k: 'f115w' in k and k.endswith('.gz'))
    assert plan.n_mosaic_rows() == 2
    assert 'new/changed only (draft)' in render_nircam_plan(plan, dry_run=True)


# ---------------------------------------------------------------------------
# deploy_nircam: dry run writes nothing; live run executes the plan
# ---------------------------------------------------------------------------

@pytest.fixture
def remote_stubs(monkeypatch):
    import campfire.deploy.push as push
    seen = {'calls': []}

    def fake_plan_remote_push(client, store, tasks, *, backend, progress=True,
                              apply_backfill=False, **kw):
        seen['calls'].append({'n': len(tasks), 'apply_backfill': apply_backfill})
        p = PushPlan()
        for t in tasks:
            if 'layout' in t.r2_key or ('f115w' in t.r2_key and t.r2_key.endswith('.gz')):
                p.unchanged.append(t)
            else:
                p.new.append(t)
                p.to_upload.append(t)
            p.stats[t.r2_key] = (0.0, 999)
        return p, {}
    monkeypatch.setattr(push, 'plan_remote_push', fake_plan_remote_push)
    monkeypatch.setattr(push, 'open_reducer_store', lambda: None)
    monkeypatch.setattr(nc, '_existing_exposure_keys', lambda client, recs: {('x',)})
    return seen


class _NoWrites:
    def table(self, name):
        raise AssertionError(f"unexpected table access: {name}")


def test_dry_run_prints_the_plan_and_writes_nothing(fake_field, remote_stubs, monkeypatch, capsys):
    monkeypatch.setattr(nc, 'get_supabase_client', lambda c: _NoWrites())
    monkeypatch.setattr(nc, 'upload_files_parallel', lambda *a, **k: pytest.fail('uploaded'))
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('prompted'))

    nc.deploy_nircam(fake_field.field, {}, dry_run=True)

    out = capsys.readouterr().out
    assert 'Deploy plan: field demo' in out and 'Mosaics (FITS)' in out
    assert remote_stubs['calls'] == [{'n': 20, 'apply_backfill': False}]


def test_declining_the_prompt_writes_nothing(fake_field, remote_stubs, monkeypatch, capsys):
    monkeypatch.setattr(nc, 'get_supabase_client', lambda c: _NoWrites())
    monkeypatch.setattr('builtins.input', lambda *a: 'n')
    nc.deploy_nircam(fake_field.field, {})
    assert 'Aborted.' in capsys.readouterr().out


@pytest.mark.parametrize('draft', [False, True])
def test_live_run_is_one_upload_pass(fake_field, remote_stubs, monkeypatch, capsys, draft):
    import campfire.deploy.fields as fields
    import campfire.deploy.push as push
    import campfire.deploy.registry as reg

    calls, uploads, images, repointed = [], [], [], []
    flushed = {'tree': [], 'mosaic': []}

    class Lease:
        def __init__(self, *a, **k):
            pass

        def begin(self):
            calls.append('lease')

        def heartbeat(self):
            pass

        def claim(self):
            return {}

    def flusher(*a, stored_sizes=None, **k):
        bucket = flushed['mosaic' if stored_sizes is not None else 'tree']
        return SimpleNamespace(add=bucket.append, flush=lambda: None)

    def upload(config, tasks, *, succeeded_out, on_success, **k):
        calls.append('upload')
        uploads.append(list(tasks))
        for t in tasks:
            succeeded_out.add(t.r2_key)
            on_success(t)
        return len(tasks), 0, []

    def note(name, ret=None):
        def fn(*a, **k):
            calls.append(name)
            return ret
        return fn

    monkeypatch.setattr(nc, 'get_supabase_client', lambda c: object())
    monkeypatch.setattr('builtins.input', lambda *a: 'y')
    monkeypatch.setattr(nc, 'get_user_id_from_token', lambda c: 'u1')
    monkeypatch.setattr(nc, 'ScopeLease', Lease)
    monkeypatch.setattr(nc, 'insert_deployment', note('deployment', 700))
    monkeypatch.setattr(push, 'registration_flusher', flusher)
    monkeypatch.setattr(nc, 'upload_files_parallel', upload)
    monkeypatch.setattr(nc, '_upsert_exposures', note('exposures', 1))
    monkeypatch.setattr(nc, '_upsert_nircam_images',
                        lambda client, rows: (calls.append('images'), images.extend(rows),
                                              len(rows))[-1])
    monkeypatch.setattr(reg, 'set_active_deployment',
                        lambda client, keys, dep: repointed.extend(keys))
    monkeypatch.setattr(fields, 'upsert_field_on_deploy', note('field_row', True))
    monkeypatch.setattr(nc, 'log_deploy_event', note('audit'))

    nc.deploy_nircam(fake_field.field, {}, draft=draft)

    assert remote_stubs['calls'] == [{'n': 20, 'apply_backfill': False},
                                     {'n': 20, 'apply_backfill': True}]
    # one upload pass carrying exposures AND mosaics, minus the unchanged
    assert len(uploads) == 1 and len(uploads[0]) == 17
    assert len(flushed['mosaic']) == 2 and len(flushed['tree']) == 15
    assert (calls.index('lease') < calls.index('deployment') < calls.index('upload')
            < calls.index('exposures') < calls.index('images') < calls.index('field_row'))
    mosaic_rows = {r['file_path'] for r in images}
    if draft:
        # staging: only the new/changed mosaics, and nothing is re-pointed
        assert mosaic_rows == {m['storage_key'] for m in fake_field.mosaics
                               if m['filter'] == 'f444w'}
        assert all(r['deploy_status'] == 'draft' for r in images)
        assert repointed == []
    else:
        assert len(mosaic_rows) == 4
        assert len(repointed) == 3  # 2 unchanged mosaics + the layout plot
    out = capsys.readouterr().out
    assert 'Deployment #700 opened' in out
    assert 'files:    17 uploaded' in out and '3 already in the cloud' in out
    assert f"{len(images)} mosaic index rows" in out
