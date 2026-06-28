"""Tests for the issue #212 PR-4 instrument-parity layout.

Validates the path-construction contract (no reduction needed): NIRSpec products
gain a ``nirspec/`` segment, raw gains one, the reducer-decision TOMLs move under
``reference/nirspec/<obs>/``, the deploy package resolves the same NIRSpec path,
and NIRCam custom flats / wisp templates hoist to the shared (de-fielded)
``reference/nircam/shared/`` while per-field reference state stays per-field.
"""

import os

from campfire_pipeline.nirspec.observation import Observation


def _obs():
    return Observation(name='test_obs', field='egs', program='ember',
                       program_id=7076, data_subdir='7076', files=['jw07076020001*'],
                       gratings=['PRISM'])


# --- NIRSpec: products/nirspec/<obs>, raw/nirspec/<subdir>, reference/nirspec/<obs> ---

def test_nirspec_workspace_has_nirspec_segment(tmp_path):
    obs = _obs()
    data_dir = str(tmp_path / 'raw')
    product_dir = str(tmp_path / 'products')
    obs.setup_workspace_directory(data_dir, product_dir)
    assert obs.workspace_dir == os.path.join(product_dir, 'nirspec', 'test_obs')
    assert obs.raw_dir == os.path.join(data_dir, 'nirspec', '7076')
    # reference root is the sibling of the products root
    assert obs.reference_dir == os.path.join(
        str(tmp_path), 'reference', 'nirspec', 'test_obs')
    assert os.path.isdir(obs.workspace_dir)
    assert os.path.isdir(obs.reference_dir)


def test_nirspec_reducer_tomls_under_reference(tmp_path):
    obs = _obs()
    obs.setup_workspace_directory(str(tmp_path / 'raw'), str(tmp_path / 'products'))
    assert obs.stuck_closed_shutters_file == os.path.join(
        obs.reference_dir, 'stuck_closed_shutters.toml')
    assert obs.bkg_override_file == os.path.join(
        obs.reference_dir, 'nodded_background_overrides.toml')
    # No longer embedded in the products workspace with the obs name in the filename.
    assert 'products' not in obs.stuck_closed_shutters_file
    assert '_test_obs_' not in obs.stuck_closed_shutters_file


# --- deploy package resolves the same NIRSpec path (lockstep) ---

def test_deploy_resolve_obs_dir_has_nirspec_segment(tmp_path, monkeypatch):
    from campfire.deploy import config as dconfig
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    obs_dir = tmp_path / 'products' / 'nirspec' / 'test_obs'
    obs_dir.mkdir(parents=True)
    assert dconfig.resolve_obs_dir('test_obs') == obs_dir


# --- NIRCam: flats/wisps hoisted to shared/, per-field state stays per-field ---

def test_nircam_flats_wisps_shared(tmp_path):
    from campfire_pipeline.nircam.field import Field
    f = Field(name='rj0911', filters=['f444w'], files=['jw01727*'],
              tangent_point=(150.0, 2.0), tiles={})
    f.setup_workspace(campfire_root=str(tmp_path))
    shared = os.path.join(str(tmp_path), 'reference', 'nircam', 'shared')
    assert f.flats_dir == os.path.join(shared, 'flats')
    assert f.wisp_dir == os.path.join(shared, 'wisps')
    # Two different fields share the same flats/wisps dir (de-fielded).
    g = Field(name='cosmos', filters=['f444w'], files=['jw01727*'],
              tangent_point=(150.1, 2.3), tiles={})
    g.setup_workspace(campfire_root=str(tmp_path))
    assert g.flats_dir == f.flats_dir and g.wisp_dir == f.wisp_dir
    # Per-field reducer state stays per-field.
    assert 'rj0911' in f.mask_dir and 'rj0911' in f.bad_pixel_dir
    assert f.mask_dir != g.mask_dir
