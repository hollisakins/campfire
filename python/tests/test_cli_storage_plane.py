"""CLI wiring tests for the storage-plane restructure.

The seven-verb surface: pull (alias download) / push / verify / drop-local /
status / sync, with the deploy group's migration-era commands deleted (#371). These
tests exercise registration and help rendering — the engine itself is covered
by test_storage_plan / test_registry / test_download_objects.
"""

from click.testing import CliRunner

import campfire.cli as main_cli


def _cli():
    main_cli._register_deploy_group()
    main_cli._register_fitsgl_group()
    return main_cli.cli


def test_top_level_commands_registered():
    cli = _cli()
    for name in ('pull', 'download', 'push', 'verify', 'drop-local',
                 'status', 'sync', 'deploy'):
        assert name in cli.commands, name


def test_download_is_alias_of_pull():
    cli = _cli()
    assert cli.commands['download'] is cli.commands['pull']


def test_help_screens_render():
    cli = _cli()
    runner = CliRunner()
    for args in (['--help'], ['pull', '--help'], ['push', '--help'],
                 ['verify', '--help'], ['drop-local', '--help'],
                 ['status', '--help']):
        res = runner.invoke(cli, args)
        assert res.exit_code == 0, (args, res.output)


def test_push_requires_scope():
    cli = _cli()
    res = CliRunner().invoke(cli, ['push'])
    assert res.exit_code != 0
    assert '--obs' in res.output and '--field' in res.output


def test_migration_era_commands_deleted():
    """A1/A2 completed → the one-time migration tools are gone (issue #371)."""
    from campfire.deploy.cli import deploy_group

    # The whole registry subgroup (backfill / reconcile / copy / prune) is deleted;
    # day-to-day verification is `campfire verify --cloud`.
    assert 'registry' not in deploy_group.commands

    nircam = deploy_group.commands['nircam']
    for name in ('import-masks', 'import-skip'):
        assert name not in nircam.commands, name
    # The annotation round-trips survive (hidden; folded into `campfire pull`).
    for name in ('pull', 'pull-masks'):
        assert nircam.commands[name].hidden, name
    nirspec = deploy_group.commands['nirspec']
    for name in ('pull-rate-masks', 'pull-stuck-shutters', 'pull-bkg-overrides'):
        assert nirspec.commands[name].hidden, name


def test_status_accepts_scope_flags():
    cli = _cli()
    params = {p.name for p in cli.commands['status'].params}
    assert {'obs_scope', 'field_scope'} <= params


def test_verify_has_cloud_and_json_flags():
    cli = _cli()
    params = {p.name for p in cli.commands['verify'].params}
    assert {'cloud', 'as_json', 'obs_filter'} <= params


# --- deploy --no-mosaics ----------------------------------------------------
# The exposures-only field deploy: a reviewer needs the exposures in the portal
# to draw masks, but mosaics built *before* those masks exist are superseded by
# the post-mask re-combine, so shipping them is wasted transfer.

def test_deploy_has_no_mosaics_flag():
    cli = _cli()
    params = {p.name for p in cli.commands['deploy'].params}
    assert 'no_mosaics' in params


def test_no_mosaics_rejected_without_field(monkeypatch):
    """It scopes the NIRCam mosaic stage, so it is meaningless on a NIRSpec
    --obs deploy and must not silently no-op there.

    load_config/_gate_admin are stubbed because deploy_group runs
    `_gate_admin(load_config(...))` *before* this validation: unstubbed, a
    sandbox with no `campfire login` session exits 1 on "Not logged in" and the
    assertion below would pass for entirely the wrong reason.
    """
    import campfire.deploy.cli as dcli

    monkeypatch.setattr(dcli, 'load_config', lambda *a, **k: {})
    monkeypatch.setattr(dcli, '_announce_auth_mode', lambda *a, **k: None)
    monkeypatch.setattr(dcli, '_gate_admin', lambda *a, **k: None)

    cli = _cli()
    res = CliRunner().invoke(cli, ['deploy', '--obs', 'ember_uds_p4',
                                   '--no-mosaics', '--dry-run'])
    assert res.exit_code != 0
    assert '--no-mosaics' in res.output


def test_no_mosaics_threads_through_to_deploy_nircam(monkeypatch):
    """The flag must reach deploy_nircam as skip_mosaics; a dropped kwarg would
    ship ~60 GB of superseded mosaics without any visible error."""
    import campfire.deploy.cli as dcli
    import campfire.deploy.nircam as dn

    seen = {}

    def _fake_deploy_nircam(field, config, **kw):
        seen['field'] = field
        seen.update(kw)

    monkeypatch.setattr(dn, 'deploy_nircam', _fake_deploy_nircam)
    monkeypatch.setattr(dcli, 'load_config', lambda *a, **k: {})
    monkeypatch.setattr(dcli, '_announce_auth_mode', lambda *a, **k: None)
    monkeypatch.setattr(dcli, '_gate_admin', lambda *a, **k: None)

    cli = _cli()
    res = CliRunner().invoke(cli, ['deploy', '--field', 'egs',
                                   '--filter', 'f070w', '--no-mosaics'])
    assert res.exit_code == 0, res.output
    assert seen['field'] == 'egs'
    assert seen['skip_mosaics'] is True
    assert seen['filters'] == ['f070w']


def test_mosaics_deployed_by_default(monkeypatch):
    """Absent the flag, skip_mosaics must be False — the default path is
    unchanged."""
    import campfire.deploy.cli as dcli
    import campfire.deploy.nircam as dn

    seen = {}
    monkeypatch.setattr(dn, 'deploy_nircam',
                        lambda field, config, **kw: seen.update(kw))
    monkeypatch.setattr(dcli, 'load_config', lambda *a, **k: {})
    monkeypatch.setattr(dcli, '_announce_auth_mode', lambda *a, **k: None)
    monkeypatch.setattr(dcli, '_gate_admin', lambda *a, **k: None)

    cli = _cli()
    res = CliRunner().invoke(cli, ['deploy', '--field', 'egs'])
    assert res.exit_code == 0, res.output
    assert seen['skip_mosaics'] is False
