"""`campfire archive gc --dry-run` (data releases P2): candidate selection,
the report, the renderer and the CLI wiring. The real collection lands with P4."""
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

import campfire.cli as main_cli
from campfire.archive import (
    COLLECTABLE_PRODUCT_TYPES, DEFAULT_RETENTION_DAYS, format_gc_report, gc_candidates, gc_report,
)

from .fakes import FakePostgrest

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def _row(i, *, days_ago, status='archived', product_type='nircam_mosaic', root='archive',
         field='cosmos', stored=None, size=1000):
    return {
        'id': i, 'root': root, 'status': status, 'product_type': product_type,
        'storage_key': f"archive/{i:064x}/products/nircam/{field}/{field}_f444w_{i}.fits.gz",
        'content_hash': f"sha256:{i:064x}", 'size_bytes': size, 'stored_size_bytes': stored,
        'field': field, 'filter': 'f444w', 'observation': None,
        'archived_from_key': f"data/products/nircam/{field}/{field}_f444w.fits.gz",
        'archived_at': (NOW - timedelta(days=days_ago)).isoformat(), 'archived_by_deployment': None,
    }


def _client(rows):
    return FakePostgrest(tables={'storage_objects': rows})


def test_gc_candidates_selects_old_archived_mosaics_only():
    rows = [
        _row(1, days_ago=400),                                  # candidate
        _row(2, days_ago=10),                                   # too young
        _row(3, days_ago=400, status='collected'),              # already collected
        _row(4, days_ago=400, product_type='nirspec_spec'),     # never (NIRSpec)
        _row(5, days_ago=400, root='live', status='active'),    # live pointer
        _row(6, days_ago=800, stored=400, field='egs'),         # candidate, compressed
    ]
    got = gc_candidates(_client(rows), older_than_days=365, now=NOW)
    assert [r['id'] for r in got] == [6, 1]                     # oldest first


def test_gc_candidates_refuses_nirspec_and_negative_window():
    with pytest.raises(ValueError):
        gc_candidates(_client([]), product_types=('nirspec_spec',), now=NOW)
    with pytest.raises(ValueError):
        gc_candidates(_client([]), older_than_days=-1, now=NOW)
    assert COLLECTABLE_PRODUCT_TYPES == ('nircam_mosaic',)


def test_gc_candidates_pages_on_id(monkeypatch):
    import campfire.archive as mod
    monkeypatch.setattr(mod, '_PAGE', 2)
    rows = [_row(i, days_ago=400 + i) for i in range(1, 6)]
    client = _client(rows)
    got = gc_candidates(client, older_than_days=365, now=NOW)
    assert sorted(r['id'] for r in got) == [1, 2, 3, 4, 5]
    assert len([c for c in client.calls if c.table == 'storage_objects']) == 3  # 2 + 2 + 1


def test_gc_report_totals_bytes_at_rest_by_field():
    rows = [_row(1, days_ago=400), _row(6, days_ago=800, stored=400, field='egs')]
    rep = gc_report(rows, older_than_days=365, now=NOW)
    assert rep['dry_run'] is True and rep['dr_membership_checked'] is False
    assert rep['candidates'] == 2 and rep['bytes_at_rest'] == 1400
    assert rep['by_field'] == {'cosmos': {'objects': 1, 'bytes': 1000}, 'egs': {'objects': 1, 'bytes': 400}}
    text = format_gc_report(rep)
    assert 'dry run' in text and 'Nothing is collected' in text and 'dr_members lands with P4' in text
    assert 'Candidates: 2 object(s)' in text and 'cosmos' in text and 'egs' in text


def test_cli_gc_requires_dry_run():
    res = CliRunner().invoke(main_cli.cli, ['archive', 'gc'])
    assert res.exit_code == 2 and 'only --dry-run' in res.output


def test_cli_gc_dry_run_renders(monkeypatch):
    rows = [_row(1, days_ago=400)]
    import campfire.deploy.config as cfg
    import campfire.deploy.supabase as sb
    monkeypatch.setattr(cfg, 'load_config', lambda *_a, **_k: {})
    monkeypatch.setattr(sb, 'get_supabase_client', lambda _c: _client(rows))
    res = CliRunner().invoke(main_cli.cli, ['archive', 'gc', '--dry-run', '--older-than', '365'])
    assert res.exit_code == 0, res.output
    assert 'Candidates: 1 object(s)' in res.output
    res = CliRunner().invoke(main_cli.cli, ['archive', 'gc', '--dry-run', '--json'])
    assert res.exit_code == 0 and f'"older_than_days": {DEFAULT_RETENTION_DAYS}' in res.output
