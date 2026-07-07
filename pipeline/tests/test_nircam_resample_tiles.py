"""Tests for the runtime ``--tiles`` selection on the resample step.

Tile selection migrated from a ``[nircam.resample].tile`` config key to a CLI
flag (``--tiles``), since it is a runtime parameter, not a config parameter.
The flag scopes the resample step only; it is exposed on ``resample``,
``combine``, ``run``, and ``check``, and is absent from the exposure-level
per-step commands.
"""

import tempfile
import types

import pytest
from click.testing import CliRunner

from campfire_pipeline.nircam import cli as nircam_cli
import campfire_pipeline.nircam.steps.resample as resample_mod


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def _help(runner, command):
    return runner.invoke(nircam_cli.main, [command, '--help']).output


def test_resample_registered_exactly_once():
    # Skipped in the auto-registration loop and added explicitly with --tiles;
    # a double-registration would silently clobber one definition.
    assert list(nircam_cli.main.commands.keys()).count('resample') == 1


@pytest.mark.parametrize('command', ['resample', 'combine', 'run', 'check'])
def test_tiles_flag_present(command):
    assert '--tiles' in _help(CliRunner(), command)


@pytest.mark.parametrize('command', ['sky', 'outlier', 'jhat', 'apply_mask'])
def test_tiles_flag_absent_on_exposure_level_steps(command):
    # Only resample is per-tile; the exposure/visit-level steps must not
    # advertise a flag they ignore.
    assert '--tiles' not in _help(CliRunner(), command)


# ---------------------------------------------------------------------------
# resample_step tile resolution
# ---------------------------------------------------------------------------

def _visited_tiles(tiles_arg, monkeypatch):
    """Return the tiles ``resample_step`` iterates for a given ``tiles`` arg.

    Overlap selection is stubbed to return no files, so each tile logs its
    header line and short-circuits before any WCS/drizzle work.
    """
    captured = []
    monkeypatch.setattr(resample_mod, 'select_overlapping_files',
                        lambda files, poly: [])
    monkeypatch.setattr(resample_mod, 'log', captured.append)

    field = types.SimpleNamespace(
        name='cosmos',
        tiles={'A1': None, 'A2': None, 'B3': None},
        filter_dir=lambda f: tempfile.gettempdir(),
        get_tile_corners=lambda t: [(0, 0), (0, 1), (1, 1), (1, 0)],
    )
    resample_mod.resample_step(
        'f444w', ['exp1.fits'], field, {'pixel_scale': '30mas'},
        'v1.2.3', overwrite=False, tiles=tiles_arg,
    )
    return [m.split(',')[0].removeprefix('resample: tile ')
            for m in captured if m.startswith('resample: tile ')]


def test_tiles_none_resamples_all(monkeypatch):
    assert _visited_tiles(None, monkeypatch) == ['A1', 'A2', 'B3']


def test_tiles_subset(monkeypatch):
    assert _visited_tiles(['A1', 'B3'], monkeypatch) == ['A1', 'B3']


def test_tiles_string_is_normalized(monkeypatch):
    assert _visited_tiles('A2', monkeypatch) == ['A2']
