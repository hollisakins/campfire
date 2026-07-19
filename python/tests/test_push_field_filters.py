"""Regression: an explicit --filter selection must stay a selection.

A typo'd filter (`campfire push --field X --filter f999w`) previously fell
back to `available` — planning/uploading the ENTIRE field instead of nothing
(Codex review, PR #364). The guard drops unknown filters with a warning and
returns before any Supabase/network work when none remain.
"""

import pytest

from campfire.deploy.push import push_field


@pytest.fixture()
def nircam_root(tmp_path, monkeypatch):
    (tmp_path / 'products' / 'nircam' / 'cosmos' / 'f444w').mkdir(parents=True)
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    return tmp_path


def test_unknown_filter_pushes_nothing(nircam_root, monkeypatch, capsys):
    import campfire.deploy.supabase as sup

    def _boom(cfg):
        raise AssertionError("a typo'd --filter must return before touching Supabase")
    monkeypatch.setattr(sup, 'get_supabase_client', _boom)

    stats = push_field('cosmos', {}, filters=['f999w'])
    assert stats == {'planned': 0, 'pushed': 0, 'failed': 0, 'skipped': 0}
    out = capsys.readouterr().out
    assert 'f999w' in out and 'nothing to push' in out.lower()


def test_known_plus_unknown_filter_keeps_only_known(nircam_root, monkeypatch, capsys):
    # One valid + one typo'd filter: warn about the typo, proceed with the
    # valid one only. The empty valid filter dir has no products, so push
    # stops at "Nothing to push" — before any Supabase call.
    import campfire.deploy.supabase as sup
    monkeypatch.setattr(sup, 'get_supabase_client',
                        lambda cfg: (_ for _ in ()).throw(AssertionError('no supabase')))
    stats = push_field('cosmos', {}, filters=['f444w', 'f999w'])
    assert stats['planned'] == 0
    out = capsys.readouterr().out
    assert 'f999w' in out  # warned
