"""
Per-purpose S3 storage backend resolution.

Centralizes S3 client construction so the **endpoint, region, and addressing
style are config concerns**, not hardcoded Cloudflare R2 details. This is the
foundation for the ``data → OSN`` / ``tiles → R2`` split: each logical
*purpose* (``data`` or ``tiles``) resolves to its own backend block, and
nothing else in the deploy code hand-builds an endpoint URL or a boto3 client.

A *purpose* maps to a config-dict section:

* ``data``  → ``config['r2']``        (spectra, rgb, sed, photometry, …)
* ``tiles`` → ``config['r2_tiles']``  (map tiles; kept on R2 for the CDN edge)

Each section may carry, in addition to the credentials
(``access_key_id`` / ``secret_access_key`` / ``bucket_name``):

* ``endpoint``         — explicit S3 endpoint URL. When absent it is derived
                         from ``account_id`` as the Cloudflare R2 host, so
                         existing R2 configs keep working unchanged.
* ``region``           — SigV4 signing region (default ``'auto'``, the R2
                         convention). OSN-style endpoints must set a real
                         region, since presigned URLs bind to the signing
                         host+region.
* ``force_path_style`` — path-style addressing (``endpoint/bucket/key``).
                         Required by some non-AWS S3 implementations.
* ``backend``          — informational label (``'r2'`` | ``'osn'``). Inferred
                         from the endpoint host when not given.
* ``public_url_base``  — base URL for publicly-served objects (tiles).

These keys are populated from TOML or the ``CAMPFIRE_S3_*`` / ``CAMPFIRE_R2_*``
environment variables (see ``config.py``).
"""

from dataclasses import dataclass
from typing import Optional


# Logical purpose -> config-dict section holding its backend config.
PURPOSE_SECTIONS = {
    'data': 'r2',
    'tiles': 'r2_tiles',
    # OSN data-bucket destination for the R2->OSN migration (epic #210 / #215).
    # Resolved alongside 'data' so the copy tool can hold both clients at once.
    'osn': 'r2_osn',
}

# Per-purpose env-var prefix used only to build a helpful "missing section" hint.
_PURPOSE_ENV_HINT = {
    'data': 'CAMPFIRE_S3',
    'tiles': 'CAMPFIRE_S3_TILES',
    'osn': 'CAMPFIRE_S3_OSN',
}

# Default endpoint host for Cloudflare R2 when only an account id is supplied.
R2_ENDPOINT_TEMPLATE = 'https://{account_id}.r2.cloudflarestorage.com'

# Default signing region for R2 (Cloudflare-ism; non-R2 backends should set one).
DEFAULT_REGION = 'auto'


@dataclass(frozen=True)
class BackendConfig:
    """Resolved S3 backend configuration for a single logical purpose."""

    purpose: str                       # 'data' | 'tiles'
    backend: str                       # 'r2' | 'osn'
    endpoint: str                      # full endpoint URL
    region: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    force_path_style: bool = False
    public_url_base: Optional[str] = None


def _coerce_bool(value) -> bool:
    """Interpret a TOML/env value (str or bool) as a boolean flag."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _infer_backend(endpoint: str) -> str:
    """Best-effort backend label from the endpoint host."""
    return 'r2' if 'r2.cloudflarestorage.com' in endpoint else 'osn'


def _resolve_endpoint(section: dict, purpose: str) -> str:
    """Resolve the endpoint URL: explicit ``endpoint`` wins, else R2-from-account."""
    endpoint = section.get('endpoint')
    if endpoint:
        return str(endpoint).rstrip('/')

    account_id = section.get('account_id')
    if account_id:
        return R2_ENDPOINT_TEMPLATE.format(account_id=account_id)

    raise ValueError(
        f"Cannot resolve S3 endpoint for '{purpose}' storage: set either "
        f"'endpoint' (e.g. CAMPFIRE_S3_ENDPOINT) or 'account_id' "
        f"(e.g. CAMPFIRE_R2_ACCOUNT_ID) in the config."
    )


def resolve_backend(config: dict, purpose: str = 'data') -> BackendConfig:
    """
    Resolve the :class:`BackendConfig` for a logical *purpose*.

    Parameters
    ----------
    config : dict
        Deploy config dict (as produced by ``config.load_config``).
    purpose : str
        ``'data'`` or ``'tiles'``.

    Raises
    ------
    ValueError
        If the purpose is unknown, its config section is missing, the
        endpoint cannot be resolved, or credentials are absent.
    """
    section_key = PURPOSE_SECTIONS.get(purpose)
    if section_key is None:
        known = ', '.join(sorted(PURPOSE_SECTIONS))
        raise ValueError(f"Unknown storage purpose '{purpose}'. Known: {known}.")

    section = config.get(section_key)
    if not section:
        env_hint = _PURPOSE_ENV_HINT.get(purpose, 'CAMPFIRE_S3')
        raise ValueError(
            f"No [{section_key}] storage config found for '{purpose}'. "
            f"Set {env_hint}_* env vars or add a [{section_key}] block to deploy.toml."
        )

    endpoint = _resolve_endpoint(section, purpose)

    access_key_id = section.get('access_key_id')
    secret_access_key = section.get('secret_access_key')
    bucket = section.get('bucket_name')
    missing = [
        name for name, val in (
            ('access_key_id', access_key_id),
            ('secret_access_key', secret_access_key),
            ('bucket_name', bucket),
        ) if not val
    ]
    if missing:
        raise ValueError(
            f"Incomplete [{section_key}] storage config for '{purpose}': "
            f"missing {', '.join(missing)}."
        )

    backend = section.get('backend') or _infer_backend(endpoint)
    region = section.get('region') or DEFAULT_REGION
    public_url_base = section.get('public_url_base')

    return BackendConfig(
        purpose=purpose,
        backend=str(backend),
        endpoint=endpoint,
        region=str(region),
        bucket=str(bucket),
        access_key_id=str(access_key_id),
        secret_access_key=str(secret_access_key),
        force_path_style=_coerce_bool(section.get('force_path_style')),
        public_url_base=str(public_url_base) if public_url_base else None,
    )


def make_s3_client(bcfg: BackendConfig, *, max_pool_connections: Optional[int] = None):
    """
    Build a boto3 S3 client from a resolved :class:`BackendConfig`.

    boto3/botocore are imported lazily so this module (and ``resolve_backend``)
    stay importable without the heavy AWS SDK installed.
    """
    import boto3
    from botocore.config import Config

    config_kwargs: dict = {'signature_version': 's3v4'}
    if bcfg.force_path_style:
        config_kwargs['s3'] = {'addressing_style': 'path'}
    if max_pool_connections is not None:
        config_kwargs['max_pool_connections'] = max_pool_connections

    return boto3.client(
        's3',
        endpoint_url=bcfg.endpoint,
        aws_access_key_id=bcfg.access_key_id,
        aws_secret_access_key=bcfg.secret_access_key,
        config=Config(**config_kwargs),
        region_name=bcfg.region,
    )


def make_client_for(config: dict, purpose: str = 'data', **kwargs):
    """Convenience: resolve *purpose* and build its boto3 client in one call."""
    return make_s3_client(resolve_backend(config, purpose), **kwargs)
