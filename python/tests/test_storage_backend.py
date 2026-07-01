"""Tests for the per-purpose S3 storage backend factory and its env resolution.

Covers ``campfire.deploy.backend`` (pure resolution + boto3 client build) and
the ``CAMPFIRE_S3_*`` / ``CAMPFIRE_R2_*`` environment resolution in
``campfire.deploy.config``. Together these prove the PR-1 acceptance gate:
endpoint / region / path-style are config-only concerns, with the data->OSN /
tiles->R2 split selected by the factory.
"""
import pytest

from campfire.deploy import backend
from campfire.deploy.config import load_config


# ---------------------------------------------------------------------------
# resolve_backend — endpoint / region / backend / path-style resolution
# ---------------------------------------------------------------------------

def _data(**overrides) -> dict:
    section = {
        'access_key_id': 'k', 'secret_access_key': 's', 'bucket_name': 'data',
    }
    section.update(overrides)
    return {'r2': section}


def test_r2_default_endpoint_from_account_id():
    b = backend.resolve_backend(_data(account_id='acct'), 'data')
    assert b.endpoint == 'https://acct.r2.cloudflarestorage.com'
    assert b.region == 'auto'
    assert b.backend == 'r2'
    assert b.force_path_style is False
    assert b.public_url_base is None
    assert b.bucket == 'data'


def test_explicit_endpoint_overrides_account_and_strips_slash():
    b = backend.resolve_backend(
        _data(account_id='acct', endpoint='https://osn.example.org/'), 'data'
    )
    # explicit endpoint wins over the derived R2 host, trailing slash stripped
    assert b.endpoint == 'https://osn.example.org'
    assert b.backend == 'osn'   # inferred from non-R2 host


def test_region_and_backend_label_honored():
    b = backend.resolve_backend(
        _data(endpoint='https://s3.example', region='us-east-1', backend='osn'),
        'data',
    )
    assert b.region == 'us-east-1'
    assert b.backend == 'osn'


@pytest.mark.parametrize('value,expected', [
    ('true', True), ('1', True), ('yes', True), ('on', True), (True, True),
    ('false', False), ('0', False), ('', False), (None, False), (False, False),
])
def test_force_path_style_coercion(value, expected):
    section = _data(account_id='acct')
    if value is not None:
        section['r2']['force_path_style'] = value
    b = backend.resolve_backend(section, 'data')
    assert b.force_path_style is expected


def test_public_url_base_passthrough():
    b = backend.resolve_backend(
        _data(account_id='acct', public_url_base='https://cdn.example'), 'data'
    )
    assert b.public_url_base == 'https://cdn.example'


def test_tiles_purpose_uses_r2_tiles_section():
    config = {'r2_tiles': {
        'account_id': 'tacct', 'access_key_id': 'k',
        'secret_access_key': 's', 'bucket_name': 'tiles',
        'public_url_base': 'https://tiles.example',
    }}
    b = backend.resolve_backend(config, 'tiles')
    assert b.purpose == 'tiles'
    assert b.bucket == 'tiles'
    assert b.endpoint == 'https://tacct.r2.cloudflarestorage.com'
    assert b.public_url_base == 'https://tiles.example'


def test_unknown_purpose_raises():
    with pytest.raises(ValueError, match='Unknown storage purpose'):
        backend.resolve_backend(_data(account_id='a'), 'bogus')


def test_missing_section_raises():
    with pytest.raises(ValueError, match='No \\[r2_tiles\\]'):
        backend.resolve_backend(_data(account_id='a'), 'tiles')


def test_missing_credentials_raises_with_names():
    with pytest.raises(ValueError, match='missing'):
        backend.resolve_backend({'r2': {'account_id': 'a'}}, 'data')


def test_no_endpoint_or_account_raises():
    with pytest.raises(ValueError, match='Cannot resolve S3 endpoint'):
        backend.resolve_backend(
            {'r2': {'access_key_id': 'k', 'secret_access_key': 's',
                    'bucket_name': 'b'}},
            'data',
        )


# ---------------------------------------------------------------------------
# make_s3_client — boto3 client carries the resolved endpoint/region/style
# ---------------------------------------------------------------------------

def test_make_s3_client_applies_endpoint_region_pathstyle():
    boto3 = pytest.importorskip('boto3')  # noqa: F841
    b = backend.resolve_backend(
        _data(endpoint='https://osn.example.org', region='us-east-1',
              force_path_style='true'),
        'data',
    )
    client = backend.make_s3_client(b, max_pool_connections=7)
    assert client.meta.endpoint_url == 'https://osn.example.org'
    assert client.meta.region_name == 'us-east-1'
    assert client.meta.config.s3.get('addressing_style') == 'path'
    assert client.meta.config.max_pool_connections == 7


def test_make_s3_client_r2_defaults():
    pytest.importorskip('boto3')
    b = backend.resolve_backend(_data(account_id='acct'), 'data')
    client = backend.make_s3_client(b)
    assert client.meta.endpoint_url == 'https://acct.r2.cloudflarestorage.com'
    assert client.meta.region_name == 'auto'


# ---------------------------------------------------------------------------
# Environment resolution (config.py) — neutral S3 names + R2 aliases
# ---------------------------------------------------------------------------

_ALL_STORAGE_ENV = [
    # supabase
    'CAMPFIRE_SUPABASE_URL', 'CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY',
    # data (neutral + alias) and tuning
    'CAMPFIRE_S3_ACCOUNT_ID', 'CAMPFIRE_S3_ACCESS_KEY_ID',
    'CAMPFIRE_S3_SECRET_ACCESS_KEY', 'CAMPFIRE_S3_BUCKET_NAME',
    'CAMPFIRE_S3_ENDPOINT', 'CAMPFIRE_S3_REGION',
    'CAMPFIRE_S3_FORCE_PATH_STYLE', 'CAMPFIRE_S3_BACKEND',
    'CAMPFIRE_S3_PUBLIC_URL_BASE',
    'CAMPFIRE_R2_ACCOUNT_ID', 'CAMPFIRE_R2_ACCESS_KEY_ID',
    'CAMPFIRE_R2_SECRET_ACCESS_KEY', 'CAMPFIRE_R2_BUCKET_NAME',
    'CAMPFIRE_R2_ENDPOINT', 'CAMPFIRE_R2_PUBLIC_URL',
    # tiles
    'CAMPFIRE_S3_TILES_ACCOUNT_ID', 'CAMPFIRE_S3_TILES_ACCESS_KEY_ID',
    'CAMPFIRE_S3_TILES_SECRET_ACCESS_KEY', 'CAMPFIRE_S3_TILES_BUCKET_NAME',
    'CAMPFIRE_S3_TILES_PUBLIC_URL_BASE',
    'CAMPFIRE_R2_TILES_ACCOUNT_ID', 'CAMPFIRE_R2_TILES_ACCESS_KEY_ID',
    'CAMPFIRE_R2_TILES_SECRET_ACCESS_KEY', 'CAMPFIRE_R2_TILES_BUCKET_NAME',
    # osn (data-bucket migration destination, #215)
    'CAMPFIRE_S3_OSN_ACCESS_KEY_ID', 'CAMPFIRE_S3_OSN_SECRET_ACCESS_KEY',
    'CAMPFIRE_S3_OSN_BUCKET_NAME', 'CAMPFIRE_S3_OSN_ENDPOINT',
    'CAMPFIRE_S3_OSN_REGION', 'CAMPFIRE_S3_OSN_FORCE_PATH_STYLE',
    'CAMPFIRE_S3_OSN_BACKEND',
]


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv('CAMPFIRE_ROOT', raising=False)
    monkeypatch.delenv('CAMPFIRE_DEPLOY_MODE', raising=False)
    for var in _ALL_STORAGE_ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _set_supabase(mp):
    # These tests exercise the direct-storage path, which lives behind the
    # explicit service-role opt-in (issue #250) — a service-role key alone no
    # longer selects that mode.
    mp.setenv('CAMPFIRE_SUPABASE_URL', 'https://prod.supabase.co')
    mp.setenv('CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY', 'svc-key')
    mp.setenv('CAMPFIRE_DEPLOY_MODE', 'service-role')


def test_neutral_s3_env_resolves_to_backend(clean_env):
    mp = clean_env
    _set_supabase(mp)
    mp.setenv('CAMPFIRE_S3_ACCESS_KEY_ID', 'ak')
    mp.setenv('CAMPFIRE_S3_SECRET_ACCESS_KEY', 'sk')
    mp.setenv('CAMPFIRE_S3_BUCKET_NAME', 'data')
    mp.setenv('CAMPFIRE_S3_ENDPOINT', 'https://osn.example.org')
    mp.setenv('CAMPFIRE_S3_REGION', 'us-east-1')
    mp.setenv('CAMPFIRE_S3_FORCE_PATH_STYLE', 'true')

    config = load_config()
    assert config['supabase']['_auth_mode'] == 'service_role'

    b = backend.resolve_backend(config, 'data')
    assert b.endpoint == 'https://osn.example.org'
    assert b.region == 'us-east-1'
    assert b.force_path_style is True
    assert b.backend == 'osn'
    assert b.bucket == 'data'


def test_legacy_r2_env_aliases_still_work(clean_env):
    mp = clean_env
    _set_supabase(mp)
    mp.setenv('CAMPFIRE_R2_ACCOUNT_ID', 'acct')
    mp.setenv('CAMPFIRE_R2_ACCESS_KEY_ID', 'ak')
    mp.setenv('CAMPFIRE_R2_SECRET_ACCESS_KEY', 'sk')
    mp.setenv('CAMPFIRE_R2_BUCKET_NAME', 'data')

    config = load_config()
    b = backend.resolve_backend(config, 'data')
    assert b.endpoint == 'https://acct.r2.cloudflarestorage.com'
    assert b.backend == 'r2'


def test_endpoint_without_account_id_is_usable(clean_env):
    mp = clean_env
    _set_supabase(mp)
    mp.setenv('CAMPFIRE_S3_ACCESS_KEY_ID', 'ak')
    mp.setenv('CAMPFIRE_S3_SECRET_ACCESS_KEY', 'sk')
    mp.setenv('CAMPFIRE_S3_BUCKET_NAME', 'data')
    mp.setenv('CAMPFIRE_S3_ENDPOINT', 'https://osn.example.org')

    config = load_config()  # must NOT fall through to "no credentials"
    assert config['supabase']['_auth_mode'] == 'service_role'
    assert 'account_id' not in config['r2']
    assert backend.resolve_backend(config, 'data').endpoint == 'https://osn.example.org'


def test_tiles_section_included_when_complete(clean_env):
    mp = clean_env
    _set_supabase(mp)
    mp.setenv('CAMPFIRE_S3_ACCOUNT_ID', 'acct')
    mp.setenv('CAMPFIRE_S3_ACCESS_KEY_ID', 'ak')
    mp.setenv('CAMPFIRE_S3_SECRET_ACCESS_KEY', 'sk')
    mp.setenv('CAMPFIRE_S3_BUCKET_NAME', 'data')
    # tiles via R2 aliases
    mp.setenv('CAMPFIRE_R2_TILES_ACCOUNT_ID', 'tacct')
    mp.setenv('CAMPFIRE_R2_TILES_ACCESS_KEY_ID', 'tak')
    mp.setenv('CAMPFIRE_R2_TILES_SECRET_ACCESS_KEY', 'tsk')
    mp.setenv('CAMPFIRE_R2_TILES_BUCKET_NAME', 'tiles')

    config = load_config()
    assert 'r2_tiles' in config
    assert backend.resolve_backend(config, 'tiles').bucket == 'tiles'


def test_tiles_section_omitted_when_incomplete(clean_env):
    mp = clean_env
    _set_supabase(mp)
    mp.setenv('CAMPFIRE_S3_ACCOUNT_ID', 'acct')
    mp.setenv('CAMPFIRE_S3_ACCESS_KEY_ID', 'ak')
    mp.setenv('CAMPFIRE_S3_SECRET_ACCESS_KEY', 'sk')
    mp.setenv('CAMPFIRE_S3_BUCKET_NAME', 'data')
    # only a partial tiles section -> dropped, never blocks loading
    mp.setenv('CAMPFIRE_R2_TILES_ACCOUNT_ID', 'tacct')

    config = load_config()
    assert 'r2_tiles' not in config


# ---------------------------------------------------------------------------
# OSN purpose (data-bucket migration destination, epic #210 / #215)
# ---------------------------------------------------------------------------

def test_osn_section_included_and_resolves(clean_env):
    mp = clean_env
    _set_supabase(mp)
    mp.setenv('CAMPFIRE_S3_ACCOUNT_ID', 'acct')
    mp.setenv('CAMPFIRE_S3_ACCESS_KEY_ID', 'ak')
    mp.setenv('CAMPFIRE_S3_SECRET_ACCESS_KEY', 'sk')
    mp.setenv('CAMPFIRE_S3_BUCKET_NAME', 'data')
    # OSN destination
    mp.setenv('CAMPFIRE_S3_OSN_ACCESS_KEY_ID', 'oak')
    mp.setenv('CAMPFIRE_S3_OSN_SECRET_ACCESS_KEY', 'osk')
    mp.setenv('CAMPFIRE_S3_OSN_BUCKET_NAME', 'campfire-jwst')
    mp.setenv('CAMPFIRE_S3_OSN_ENDPOINT', 'https://uaz1.osn.mghpcc.org')
    mp.setenv('CAMPFIRE_S3_OSN_REGION', 'us-east-1')
    mp.setenv('CAMPFIRE_S3_OSN_FORCE_PATH_STYLE', 'true')

    config = load_config()
    assert 'r2_osn' in config

    # data (R2) and osn (OSN) resolve independently and simultaneously.
    data = backend.resolve_backend(config, 'data')
    assert data.endpoint == 'https://acct.r2.cloudflarestorage.com'
    osn = backend.resolve_backend(config, 'osn')
    assert osn.endpoint == 'https://uaz1.osn.mghpcc.org'
    assert osn.region == 'us-east-1'
    assert osn.force_path_style is True
    assert osn.backend == 'osn'           # inferred from non-R2 host
    assert osn.bucket == 'campfire-jwst'


def test_osn_section_omitted_when_incomplete(clean_env):
    mp = clean_env
    _set_supabase(mp)
    mp.setenv('CAMPFIRE_S3_ACCESS_KEY_ID', 'ak')
    mp.setenv('CAMPFIRE_S3_SECRET_ACCESS_KEY', 'sk')
    mp.setenv('CAMPFIRE_S3_BUCKET_NAME', 'data')
    mp.setenv('CAMPFIRE_S3_ENDPOINT', 'https://acct.r2.cloudflarestorage.com')
    # partial OSN (no bucket / endpoint) -> dropped, never blocks loading
    mp.setenv('CAMPFIRE_S3_OSN_ACCESS_KEY_ID', 'oak')

    config = load_config()
    assert 'r2_osn' not in config


def test_osn_missing_section_raises_with_hint():
    with pytest.raises(ValueError, match='CAMPFIRE_S3_OSN'):
        backend.resolve_backend(_data(account_id='a'), 'osn')
