"""CLI wiring tests for the storage-plane restructure.

The seven-verb surface: pull (alias download) / push / verify / drop-local /
status / sync, with the deploy group's migration-era commands deleted (#371). These
tests exercise registration and help rendering — the engine itself is covered
by test_storage_plan / test_registry / test_download_objects.
"""

import pytest
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
    assert 'push spec --obs' in res.output and 'push im --field' in res.output
    for args, flag in ((['push', 'spec'], '--obs'), (['push', 'im'], '--field')):
        res = CliRunner().invoke(cli, args)
        assert res.exit_code != 0 and flag in res.output, (args, res.output)


def test_modality_subcommands_and_aliases():
    cli = _cli()
    runner = CliRunner()
    for verb in ('pull', 'push'):
        group = cli.commands[verb]
        assert set(group.commands) == {'spectroscopy', 'imaging'}
        for args in ([verb, 'spec', '--help'], [verb, 'im', '--help'],
                     [verb, 'spectroscopy', '--help'], [verb, 'imaging', '--help']):
            res = runner.invoke(cli, args)
            assert res.exit_code == 0, (args, res.output)
        res = runner.invoke(cli, [verb, '--help'])
        assert 'spectroscopy (spec)' in res.output and 'imaging (im)' in res.output


def test_bare_pull_asks_for_a_modality():
    res = CliRunner().invoke(_cli(), ['pull'])
    assert res.exit_code == 2
    assert 'pull spec' in res.output and 'pull im' in res.output


@pytest.mark.parametrize('args, expect', [
    (['pull', '--obs', 'ember_uds_p4'], ['campfire pull spec --obs ember_uds_p4']),
    (['download', '--program', 'EMBER-UDS', '--grating', 'PRISM'],
     ['campfire download spec --program EMBER-UDS --grating PRISM']),
    # A field holds both modalities: the hint offers both, --intermediate as --products.
    (['pull', '--field', 'nep', '--intermediate'],
     ['campfire pull spec --field nep --products spectra,exposures',
      'campfire pull im --field nep --products mosaics,exposures,expmaps']),
    (['pull', '--field', 'egs', '--filters', 'f444w'], ['campfire pull im --field egs --filters f444w']),
    (['push', '--field', 'cosmos', '--filter', 'f444w'], ['campfire push im --field cosmos --filters f444w']),
    (['push', '--obs', 'x', '--source-ids', '1'], ['campfire push spec --obs x --source-ids 1']),
])
def test_old_syntax_answered_with_the_new_command(args, expect):
    res = CliRunner().invoke(_cli(), args)
    assert res.exit_code == 2, res.output
    for line in expect:
        assert line in res.output, res.output


def test_migration_era_commands_deleted():
    """A1/A2 completed → the one-time migration tools are gone (issue #371)."""
    from campfire.deploy.cli import deploy_group

    # The whole registry subgroup (backfill / reconcile / copy / prune) is deleted;
    # day-to-day verification is `campfire verify --cloud`.
    assert 'registry' not in deploy_group.commands

    nircam = deploy_group.commands['nircam']
    for name in ('import-masks', 'import-skip'):
        assert name not in nircam.commands, name
    # The NIRCam round-trips moved to the config plane (retired, hidden).
    for name in ('pull', 'pull-masks'):
        assert nircam.commands[name].hidden, name
    nirspec = deploy_group.commands['nirspec']
    assert nirspec.commands['pull-rate-masks'].hidden


def test_retired_commands_answer_with_their_replacement():
    # The stuck-shutter / background-override lists moved to the config plane, and
    # fetch-config with them: the old spellings name the new command (exit 2)
    # whatever arguments they are given, instead of "No such command".
    from campfire.deploy.cli import deploy_group
    runner = CliRunner()
    # (--local on the group: the admin gate runs before any subcommand.)
    for args in (['nirspec', 'pull-stuck-shutters', '--obs', 'x', '--dry-run'],
                 ['nirspec', 'pull-bkg-overrides', '--obs', 'x', '--local']):
        res = runner.invoke(deploy_group, ['--local', *args], obj={})
        assert res.exit_code == 2, res.output
        assert 'campfire config pull --obs <obs>' in res.output
        assert deploy_group.commands['nirspec'].commands[args[1]].hidden
    # The NIRCam masks and exclusions moved to the config plane too.
    for args in (['nircam', 'pull', '--field', 'x', '--dry-run'],
                 ['nircam', 'pull-masks', '--field', 'x']):
        res = runner.invoke(deploy_group, ['--local', *args], obj={})
        assert res.exit_code == 2, res.output
        assert 'campfire config pull --masks --field <field>' in res.output
    res = runner.invoke(deploy_group, ['--local', 'fetch-config', '--obs', 'x', '--output-dir', 'y'], obj={})
    assert res.exit_code == 2, res.output
    assert 'campfire config pull --obs <obs>' in res.output
    assert deploy_group.commands['fetch-config'].hidden


def test_status_scope_points_at_a_push_dry_run():
    res = CliRunner().invoke(_cli(), ['status', '--obs', 'a', 'b', '--field', 'nep'])
    assert res.exit_code == 2
    assert 'campfire push spec --obs a b --dry-run' in res.output
    assert 'campfire push im --field nep --dry-run' in res.output


def test_verify_has_cloud_and_json_flags_and_modality_scopes():
    cli = _cli()
    verify = cli.commands['verify']
    assert {'cloud', 'as_json', 'deep', 'local'} <= {p.name for p in verify.params}
    assert set(verify.commands) == {'spectroscopy', 'imaging'}
    runner = CliRunner()
    for args in (['verify', 'spec', '--help'], ['verify', 'im', '--help']):
        assert runner.invoke(cli, args).exit_code == 0, args
    # --cloud is catalog-wide: no modality
    res = runner.invoke(cli, ['verify', '--cloud', 'im', '--field', 'nep'])
    assert res.exit_code == 2 and 'whole catalog' in res.output
    # the old flag is answered with the new command
    res = runner.invoke(cli, ['verify', '--obs', 'ember_uds_p4'])
    assert res.exit_code == 2 and 'campfire verify spec --obs ember_uds_p4' in res.output


def test_verify_scopes_the_local_scan_by_modality(tmp_path, monkeypatch):
    calls = []

    class FakeStore:
        def verify_local_objects(self, root, **kw):
            calls.append(kw)
            return {'cleared': 0, 'rehashed': 0, 'discovered': 0}

        def close(self):
            pass

    monkeypatch.setattr(main_cli, '_open_store', lambda: FakeStore())
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    runner = CliRunner()
    assert runner.invoke(_cli(), ['verify', 'im', '--field', 'nep']).exit_code == 0
    assert runner.invoke(_cli(), ['verify', '--deep', 'spec', '--obs', 'o1']).exit_code == 0
    assert runner.invoke(_cli(), ['verify']).exit_code == 0
    im, spec, whole = calls
    assert im['fields'] == ['nep'] and im['observations'] is None
    assert set(im['product_types']) == {'nircam_mosaic', 'nircam_exposure', 'nircam_expmap'}
    assert spec['observations'] == ['o1'] and spec['deep'] is True
    assert set(spec['product_types']) == {'nirspec_spec', 'nirspec_spectrum_exposure'}
    assert whole['product_types'] is None and whole['observations'] is None and whole['fields'] is None


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


def _field_deploy(monkeypatch, *args, tiles=None):
    """Invoke `campfire deploy` with deploy_nircam stubbed; returns (result, kwargs)."""
    import campfire.deploy.cli as dcli
    import campfire.deploy.nircam as dn
    import campfire.deploy.push as push

    seen = {}

    def _fake_deploy_nircam(field, config, **kw):
        seen['field'] = field
        seen.update(kw)

    monkeypatch.setattr(dn, 'deploy_nircam', _fake_deploy_nircam)
    monkeypatch.setattr(dcli, 'load_config', lambda *a, **k: {})
    monkeypatch.setattr(dcli, '_announce_auth_mode', lambda *a, **k: None)
    monkeypatch.setattr(dcli, '_gate_admin', lambda *a, **k: None)
    monkeypatch.setattr(push, 'field_mosaic_tiles', lambda f, filters=None: set(tiles or ()))
    res = CliRunner().invoke(_cli(), ['deploy', *args])
    return res, seen


def test_no_mosaics_threads_through_to_deploy_nircam(monkeypatch):
    """--no-mosaics must drop mosaics from the products deploy_nircam gets; a
    dropped flag would ship ~60 GB of superseded mosaics without any error."""
    res, seen = _field_deploy(monkeypatch, '--field', 'egs', '--filter', 'f070w',
                              '--no-mosaics')
    assert res.exit_code == 0, res.output
    assert seen['field'] == 'egs'
    assert seen['products'] == ['exposures', 'expmaps']
    assert seen['explicit_products'] is False
    assert seen['filters'] == ['f070w']


def test_mosaics_deployed_by_default(monkeypatch):
    """With no selection every product deploys — the default path is unchanged."""
    res, seen = _field_deploy(monkeypatch, '--field', 'egs')
    assert res.exit_code == 0, res.output
    assert seen['products'] == ['mosaics', 'exposures', 'expmaps']
    assert seen['tiles'] is None


def test_field_deploy_products_use_the_push_vocabulary(monkeypatch):
    res, seen = _field_deploy(monkeypatch, '--field', 'egs', '--products', 'mosaics',
                              '--tile', 'core', '--filters', 'F444W,f277w', tiles={'core'})
    assert res.exit_code == 0, res.output
    assert seen['products'] == ['mosaics']
    assert seen['explicit_products'] is True
    assert seen['tiles'] == ['core']
    assert seen['filters'] == ['f444w', 'f277w']


@pytest.mark.parametrize('args, message', [
    (['--field', 'egs', '--products', 'spectra'], 'imaging has no product spectra'),
    (['--field', 'egs', '--products', 'exposures', '--tile', 'core'], '--tile narrows mosaics'),
    (['--field', 'egs', '--tile', 'nope'], 'No mosaics for tile(s) nope'),
    (['--field', 'egs', '--products', 'mosaics', '--no-mosaics'], 'use one or the other'),
    (['--obs', 'ember_uds_p4', '--products', 'mosaics'], 'use them with --field'),
])
def test_field_deploy_selection_errors(monkeypatch, args, message):
    res, seen = _field_deploy(monkeypatch, *args, tiles={'core'})
    assert res.exit_code != 0
    assert message in res.output
    assert 'field' not in seen  # refused before deploy_nircam ran
