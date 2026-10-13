"""The deploy/freeze contract, enforced mechanically (Fable review of #599).

A data-release freeze is one snapshot transaction, so it is internally
consistent by construction; the deploy lease exists so it never snapshots a
catalog write that spans several requests half-way. Every `campfire deploy`
command is classified here: LEASED (takes a scope lease around its
multi-request catalog writes), EXEMPT (writes nothing the freeze reads, or one
atomic request), or a KNOWN_GAP with the change that closes it. A new command
fails this test until it is classified -- that is the point.
"""
import inspect

import click

import campfire.deploy.cli as dcli

LEASE_MARKERS = ('_under_scope_lease', '_under_observation_lease', '_confirm_then_lease',
                 'ScopeLease(', '_run_deploy_flow(')

# command path -> the function whose source must take the lease
LEASED = {
    '': dcli.deploy_group,                 # the full deploy (deploy_observation / deploy_nircam)
    'json': dcli.json_cmd,
    'zfit': dcli.zfit,
    'lines': dcli.lines,
    'photometry': dcli.photometry,
    'publish': dcli._lifecycle_transition,  # --field loops; --obs is one RPC
    'revoke': dcli._lifecycle_transition,
    'objects reconcile': dcli.objects_reconcile,
    'objects split': dcli.objects_split,
    'objects merge': dcli.objects_merge,
    'objects rebuild': dcli.objects_rebuild,
}

EXEMPT = {
    'thumbnails': 'thumbnail products are not release members',
    'slits': 'slit_regions are not frozen',
    'shutters': 'shutters are not frozen',
    'pointings': 'pointings are not frozen',
    'delete-local': 'local files only',
    'sync-programs': 'config tables, not frozen',
    'sync-fields': 'config tables, not frozen',
    'fetch-config': 'read-only',
    'tiles': 'map tiles (R2) are not release members',
    'objects': 'group (defaults to reconcile)',
    'nircam': 'group', 'nirspec': 'group',
    'nircam pull': 'read-only (cloud -> local)', 'nircam pull-masks': 'read-only (cloud -> local)',
    'nirspec pull-redshifts': 'read-only', 'nirspec pull-rate-masks': 'read-only',
    'nirspec pull-stuck-shutters': 'read-only', 'nirspec pull-bkg-overrides': 'read-only',
}

KNOWN_GAP = {
    'remove': 'leased in #602 (P4d), which holds the observation lease through every delete batch',
}


def _commands(group, prefix=''):
    for name, cmd in group.commands.items():
        path = f'{prefix}{name}'
        yield path, cmd
        if isinstance(cmd, click.Group):
            yield from _commands(cmd, path + ' ')


def _source(obj):
    fn = getattr(obj, 'callback', None) or obj
    return inspect.getsource(inspect.unwrap(fn))


def test_every_deploy_command_is_classified():
    paths = {p for p, _ in _commands(dcli.deploy_group)}
    unclassified = paths - set(LEASED) - set(EXEMPT) - set(KNOWN_GAP)
    assert not unclassified, (
        f"classify {sorted(unclassified)} in this file: does the command write, over several "
        f"requests, anything a data-release freeze reads? Then it takes a scope lease.")
    stale = (set(LEASED) | set(EXEMPT) | set(KNOWN_GAP)) - paths - {''}
    assert not stale, f"classified commands that no longer exist: {sorted(stale)}"


def test_leased_commands_take_a_lease():
    for path, fn in LEASED.items():
        src = _source(fn)
        assert any(m in src for m in LEASE_MARKERS), f"`deploy {path}` no longer takes a scope lease"
