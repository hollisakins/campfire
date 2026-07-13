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
