"""Pipeline consumer for reviewer exclusions (epic #261, N6, D10).

`Field.setup_workspace` reads `reference/<field>/exposures.json` (materialized by
`campfire deploy nircam pull`) and `get_exposure_files` drops the listed
rootnames — so they leave both combine and outlier detection. Reversible: an
empty list re-includes; a missing file is today's behavior.
"""
import json
import os

from campfire_pipeline.nircam.field import Field

FIELDS_TOML = """
[cosmos]
filters = ["f444w"]
files = ["jw01727"]
tangent_point = [150.0, 2.0]
"""


def _make_field(tmp_path):
    fields_file = tmp_path / 'fields.toml'
    fields_file.write_text(FIELDS_TOML)
    field = Field.load('cosmos', fields_file=str(fields_file))
    field.setup_workspace(campfire_root=str(tmp_path))
    # Two canonical exposures the field's `files` glob (jw01727*) will pick up.
    filt_dir = field.filter_dir('f444w')
    os.makedirs(filt_dir, exist_ok=True)
    for root in ('jw01727_a', 'jw01727_b'):
        open(os.path.join(filt_dir, f'{root}.fits'), 'w').close()
    return field


def _write_exposures_json(field, excluded):
    path = os.path.join(field.reference_dir, 'exposures.json')
    os.makedirs(field.reference_dir, exist_ok=True)
    with open(path, 'w') as f:
        json.dump({'version': 1, 'field': 'cosmos', 'excluded': excluded}, f)
    return path


def _roots(paths):
    return sorted(os.path.basename(p).removesuffix('.fits') for p in paths)


def test_excluded_exposure_dropped(tmp_path):
    field = _make_field(tmp_path)
    _write_exposures_json(field, {'f444w': ['jw01727_a']})
    field.excluded_exposures = field._load_excluded_exposures()
    assert _roots(field.get_exposure_files('f444w')) == ['jw01727_b']


def test_reversible_empty_reincludes(tmp_path):
    field = _make_field(tmp_path)
    _write_exposures_json(field, {})  # un-excluded everything
    field.excluded_exposures = field._load_excluded_exposures()
    assert _roots(field.get_exposure_files('f444w')) == ['jw01727_a', 'jw01727_b']


def test_missing_file_backward_compatible(tmp_path):
    field = _make_field(tmp_path)
    # No exposures.json at all → today's behavior (nothing excluded).
    field.excluded_exposures = field._load_excluded_exposures()
    assert field.excluded_exposures == {}
    assert _roots(field.get_exposure_files('f444w')) == ['jw01727_a', 'jw01727_b']


def test_malformed_file_ignored(tmp_path):
    field = _make_field(tmp_path)
    path = os.path.join(field.reference_dir, 'exposures.json')
    os.makedirs(field.reference_dir, exist_ok=True)
    with open(path, 'w') as f:
        f.write('{ not valid json')
    field.excluded_exposures = field._load_excluded_exposures()  # must not raise
    assert field.excluded_exposures == {}
    assert _roots(field.get_exposure_files('f444w')) == ['jw01727_a', 'jw01727_b']
