"""Deploy a FitsGL tile-pyramid dataset to the campfire-tiles bucket (epic #337, Phase 3).

`campfire fitsgl deploy` takes a dataset built by `campfire fitsgl build`, pushes it to
R2 under a per-field/tile prefix via FitsGL's ``deploy_dataset`` (called as a library),
and upserts a thin ``fitsgl_datasets`` row the web map reads. That row carries no
lifecycle column of its own — its public visibility DERIVES from the backing mosaics'
``nircam_images.deploy_status`` (RLS), so a deploy against still-draft mosaics never
surfaces a public manifest.

Credentials, per Supabase auth mode:

* ``login`` (default) — the admin machine holds no tiles keys, so the CLI fetches them
  from the admin-gated web endpoint (``/deploy/tiles-credentials``) and constructs
  FitsGL's boto3 ``R2Target`` itself. FitsGL's deploy GETs the prior ledger, DELETEs
  orphaned supertiles, and calls ``put_bucket_cors`` — operations a presigned PutObject
  URL cannot express, which is why this can't ride the ``/deploy/presign`` path. Bounded
  relaxation of #250: tiles bucket only (public, derived data), never the ``data`` bucket.
* ``service-role`` / ``local`` — read local ``CAMPFIRE_S3_TILES_*`` via ``resolve_backend``.

Split like ``build.py``: a **pure** layer (prefix / DeployConfig-kwargs / dataset-row /
source-hash assembly — no fitsgl, no network, unit-testable) and an **orchestration**
layer (``run_deploy``) that defers the ``import fitsgl`` behind the ``campfire[fitsgl]``
extra.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

# The tiles-bucket key namespace for FitsGL datasets. Disjoint from the PNG tile
# keyspace (``<field>/<filter>/<z>/<x>/<y>.png`` — campfire-layout's ``tile`` product)
# because everything here lives under a ``fitsgl/`` root that no field name collides
# with, so FitsGL's prefix-scoped ledger diff/delete never touches the PNG pyramid.
# Composite and per-tile datasets are sibling prefixes (neither an ancestor of the
# other), so a per-tile ledger delete can't reach the composite's objects.
_FITSGL_ROOT = 'fitsgl'
_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Pure layer (no fitsgl import, no network; unit-tested without real data)
# ---------------------------------------------------------------------------

def dataset_prefix(field, tile=None):
    """R2 key prefix for a field's fiducial composite (``tile=None``) or one tile."""
    if tile is not None:
        return f"{_FITSGL_ROOT}/{field}/tile/{tile}"
    return f"{_FITSGL_ROOT}/{field}/composite"


def dataset_name(field, tile=None):
    """Local dataset dir name ``campfire fitsgl build`` wrote (mirrors build_fitsgl_toml)."""
    return f"{field}__{tile}" if tile is not None else field


def build_deploy_config(creds, prefix, *, viewer_origin='*'):
    """DeployConfig kwargs for FitsGL, from resolved tiles creds + a dataset prefix.

    ``public_url`` is ``<public_url_base>/<prefix>`` — FitsGL uploads an object to key
    ``<prefix>/<path>`` and serves it at ``<public_url>/<path>`` (the workspace invariant
    ``public_url == base/prefix``), so the two must be composed this way or the edge-purge
    URLs drift from the uploaded keys. ``zone_id`` (optional) enables the Cloudflare edge
    purge on re-deploy; without it (and ``CLOUDFLARE_API_TOKEN``) the purge is skipped.
    Raises if the tiles backend has no ``public_url_base``.
    """
    base = creds.get('public_url_base')
    if not base:
        raise ValueError(
            "tiles storage has no public_url_base — set CAMPFIRE_S3_TILES_PUBLIC_URL_BASE "
            "(the CDN origin serving the tiles bucket) so the viewer can fetch the pyramid."
        )
    public_url = f"{base.rstrip('/')}/{prefix}"
    return {
        'bucket': creds['bucket'],
        'endpoint': creds['endpoint'],
        'public_url': public_url,
        'prefix': prefix,
        'viewer_origin': viewer_origin,
        'zone_id': creds.get('cf_zone_id') or os.environ.get('CLOUDFLARE_ZONE_ID'),
    }


def fitsgl_json_url(public_url):
    """The ``fitsgl.json`` URL the web viewer points ``<FitsViewer>`` at."""
    return f"{public_url.rstrip('/')}/fitsgl.json"


def compute_source_hashes(mosaics, hash_by_key):
    """Nested ``{tile: {filter: 'sha256:..'}}`` of the mosaics a dataset was built from.

    ``mosaics`` are ``select_mosaics()`` dicts (``tile`` / ``filter`` / ``storage_key``);
    ``hash_by_key`` maps ``storage_key -> content_hash``. Mosaics with no known hash are
    skipped (the caller decides whether that is fatal). Deterministic given its inputs.
    """
    out: dict[str, dict[str, str]] = {}
    for m in mosaics:
        h = hash_by_key.get(m['storage_key'])
        if h:
            out.setdefault(m['tile'], {})[m['filter']] = h
    return out


def dataset_row(*, field, tile, prefix, pixel_scale, fitsgl_json, bands, tiles,
                source_hashes, is_default):
    """Assemble the ``fitsgl_datasets`` upsert row (conflict target = ``prefix``)."""
    return {
        'prefix': prefix,
        'field': field,
        'kind': 'tile' if tile is not None else 'field',
        'tile': tile,
        'tiles': sorted(tiles),
        'pixel_scale': pixel_scale,
        'fitsgl_json_url': fitsgl_json,
        'bands': list(bands),
        'source_hashes': source_hashes,
        'is_default': is_default,
        'schema_version': _SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# Credentials + source hashes (touch the registry / web API)
# ---------------------------------------------------------------------------

def resolve_tiles_creds(config):
    """Resolve tiles-bucket creds as a plain dict, per the resolved auth mode.

    ``login`` (default): fetch from the admin-gated web endpoint (no local keys assumed).
    ``service_role`` / ``local``: read local ``CAMPFIRE_S3_TILES_*`` via ``resolve_backend``.
    Returns ``{endpoint, region, bucket, access_key_id, secret_access_key,
    force_path_style, public_url_base}``.
    """
    mode = config.get('supabase', {}).get('_auth_mode', 'login')
    if mode == 'login':
        return fetch_tiles_credentials(config)
    from campfire.deploy.backend import resolve_backend
    b = resolve_backend(config, 'tiles')
    return {
        'endpoint': b.endpoint, 'region': b.region, 'bucket': b.bucket,
        'access_key_id': b.access_key_id, 'secret_access_key': b.secret_access_key,
        'force_path_style': b.force_path_style, 'public_url_base': b.public_url_base,
    }


def fetch_tiles_credentials(config):
    """GET the R2 tiles creds from the admin-gated web endpoint (login mode)."""
    import requests
    from campfire.api.session import resolve_base_url
    from campfire.auth.tokens import TokenManager

    base_url = resolve_base_url()
    tm = TokenManager(base_url=base_url)
    if not tm.is_oauth():
        raise RuntimeError(
            "not logged in — run `campfire login` (or use --service-role with "
            "CAMPFIRE_S3_TILES_* for unattended deploys)."
        )
    token = tm.get_valid_token()
    if not token:
        raise RuntimeError("login session expired — run `campfire login`.")
    resp = requests.get(
        f"{base_url}/deploy/tiles-credentials",
        headers={'Authorization': f'Bearer {token}'}, timeout=30,
    )
    if resp.status_code == 403:
        raise RuntimeError("tiles credentials require admin access.")
    if resp.status_code == 404:
        raise RuntimeError(
            "tiles-credentials endpoint not found — the web app may predate Phase 3; "
            "use --service-role with CAMPFIRE_S3_TILES_* instead."
        )
    resp.raise_for_status()
    return resp.json()


def resolve_source_hashes(client, mosaics, *, local_fallback=True):
    """Content-hash the mosaics a dataset is built from (registry first).

    Prefers ``storage_objects.content_hash`` (what the last mosaic deploy recorded, and
    what the deploy hook compares against) so multi-GB files aren't re-hashed. With
    ``local_fallback=True`` (the deploy path) it hashes any not-yet-registered mosaic
    locally so the recorded hashes are complete; with ``local_fallback=False`` (the
    deploy-staleness hook, which must stay cheap on the hot path) it returns only the
    registry-known hashes and lets the caller detect the gap. Returns
    ``compute_source_hashes(...)``.
    """
    from campfire.deploy.registry import fetch_active_content_hashes, hash_files_parallel
    keys = [m['storage_key'] for m in mosaics]
    by_key = dict(fetch_active_content_hashes(client, keys)) if keys else {}
    if local_fallback:
        missing = [m for m in mosaics if m['storage_key'] not in by_key]
        if missing:
            local = hash_files_parallel([m['path'] for m in missing])
            for m in missing:
                by_key[m['storage_key']] = local[m['path']][0]
    return compute_source_hashes(mosaics, by_key)


def _hash_count(source_hashes):
    """Number of ``(tile, filter)`` leaf hashes in a nested source_hashes dict."""
    return sum(len(v) for v in source_hashes.values())


def _select_dataset_mosaics(field, *, tile, pixel_scale):
    """The mosaics backing a dataset — the same selection ``build`` fed to FitsGL.

    Returns ``(mosaics, tiles, bands)``: ``select_mosaics`` dicts (each carrying a
    ``storage_key``), the tile set, and the filter (band) list in build's blue→red
    order. Empty ``mosaics`` means the source mosaics aren't on this machine, the
    fiducial set is undeclared, or the fiducial/scale changed since build — the caller
    decides whether that's fatal (``run_deploy``) or a skip (the staleness hook). Tile
    resolution is guarded so a broken/removed fields.toml declaration degrades to "no
    mosaics" rather than a traceback.
    """
    from campfire.deploy.nircam import _discover_filters, _resolve_nircam_dirs
    from campfire.fitsgl.build import (
        group_bands, resolve_fiducial_tiles, select_mosaics,
    )
    dirs = _resolve_nircam_dirs(field)
    filters = _discover_filters(dirs) if dirs['products'].exists() else []
    if tile is not None:
        tiles = [tile]
    else:
        try:
            tiles = resolve_fiducial_tiles(field, pixel_scale=pixel_scale)
        except Exception:
            tiles = []
    mosaics = (select_mosaics(dirs, field, filters, pixel_scale=pixel_scale, tiles=tiles)
               if filters and tiles else [])
    bands = list(group_bands(mosaics, single_tile=(tile is not None)))
    return mosaics, list(tiles), bands


# ---------------------------------------------------------------------------
# Orchestration (imports the registry + defers `import fitsgl`)
# ---------------------------------------------------------------------------

def run_deploy(field, *, config, client, tile=None, pixel_scale='30mas',
               viewer_origin='*', dry_run=False, out_dir=None):
    """Deploy one built FitsGL dataset to R2 and upsert its ``fitsgl_datasets`` row.

    Requires the dataset to have been built (``campfire fitsgl build``) and — to record
    ``source_hashes`` for the deploy-staleness hook — the backing mosaics to be present on
    this machine (the build+deploy-on-one-machine workflow). ``dry_run`` prints FitsGL's
    upload/delete/purge plan and writes nothing (no row).
    """
    from campfire.fitsgl.build import resolve_fitsgl_dir

    out_root = resolve_fitsgl_dir(out_dir)
    name = dataset_name(field, tile)
    dataset_dir = out_root / name
    if not (dataset_dir / 'fitsgl.json').is_file():
        scope = f" --tile {tile}" if tile is not None else ""
        click.echo(
            f"Error: no built dataset at {dataset_dir}. Run "
            f"`campfire fitsgl build --field {field}{scope}` first."
        )
        sys.exit(1)

    mosaics, tiles, bands = _select_dataset_mosaics(field, tile=tile, pixel_scale=pixel_scale)
    if not mosaics:
        scope = f"tile {tile}" if tile is not None else f"fiducial tiles of '{field}'"
        click.echo(
            f"Error: no {pixel_scale} mosaics found for {scope} on this machine. Deploy "
            f"from the same machine that built the dataset (source_hashes are read from "
            f"the mosaics), or rebuild after fixing the fiducial/pixel-scale."
        )
        sys.exit(1)

    source_hashes = resolve_source_hashes(client, mosaics)
    prefix = dataset_prefix(field, tile)

    # One dataset per prefix (a field has one composite at one chosen scale — design
    # §5). Re-deploying at a different scale is a deliberate replace, but warn so it's
    # never a silent surprise: the row is overwritten and FitsGL re-tiles the pyramid.
    existing = (client.table('fitsgl_datasets').select('pixel_scale')
                .eq('prefix', prefix).limit(1).execute())
    if existing.data and existing.data[0]['pixel_scale'] != pixel_scale:
        click.echo(f"  ⚠  replacing the existing {existing.data[0]['pixel_scale']} dataset "
                   f"at this prefix with {pixel_scale} (its pyramid will be re-tiled).")

    creds = resolve_tiles_creds(config)
    dc_kwargs = build_deploy_config(creds, prefix, viewer_origin=viewer_origin)
    json_url = fitsgl_json_url(dc_kwargs['public_url'])

    scope = f"tile {tile}" if tile is not None else "fiducial composite"
    click.echo(
        f"Deploying FitsGL {scope} for '{field}' ({len(bands)} band(s)) "
        f"→ {creds['bucket']}/{prefix}"
    )

    try:
        from fitsgl.deploy import CloudflarePurge, DeployConfig, R2Target, deploy_dataset
    except ImportError:
        click.echo("FitsGL producer not installed. Run: pip install campfire[fitsgl]")
        sys.exit(1)

    deploy_cfg = DeployConfig(**dc_kwargs)
    target = R2Target(
        bucket=creds['bucket'], endpoint=creds['endpoint'],
        access_key=creds['access_key_id'], secret_key=creds['secret_access_key'],
        region=creds.get('region', 'auto'),
    )
    # Optional edge purge: supertile filenames are geometry-derived (not content-hashed),
    # so a changed mosaic re-uses a filename and its edge copy must be evicted. Needs
    # CLOUDFLARE_API_TOKEN + a zone id; absent them the purge is skipped (changed tiles
    # serve stale until max-age) — first deploys are unaffected (nothing cached yet).
    purger = CloudflarePurge.from_config(deploy_cfg)

    result = deploy_dataset(
        dataset_dir, deploy_cfg, target, purger=purger, dry_run=dry_run,
        on_progress=lambda msg: click.echo(f"  {msg}"),
    )

    if dry_run:
        click.echo("Dry run — no changes made, no fitsgl_datasets row written.")
        return result

    row = dataset_row(
        field=field, tile=tile, prefix=prefix, pixel_scale=pixel_scale,
        fitsgl_json=json_url, bands=bands, tiles=tiles,
        source_hashes=source_hashes, is_default=(tile is None),
    )
    client.table('fitsgl_datasets').upsert(row, on_conflict='prefix').execute()
    click.echo(f"\n✓ Deployed {name}: {len(result.uploaded)} uploaded, "
               f"{len(result.deleted)} removed")
    click.echo(f"  Viewer manifest: {json_url}")
    return result


# ---------------------------------------------------------------------------
# Deploy-staleness hook (called from campfire deploy after a mosaic deploy)
# ---------------------------------------------------------------------------

def suggest_fitsgl_rebuild(client, field, *, default_pixel_scale='30mas'):
    """Print a suggested ``campfire fitsgl`` command when the field's composite is stale.

    Best-effort and side-effect-free: compares the field's fiducial mosaics' current
    content hashes to the stored ``fitsgl_datasets`` composite row and prints a hint if
    the dataset is missing or its inputs changed. Never raises into the deploy — any
    error (no fiducial declaration, no dataset table, etc.) just skips the suggestion.
    Suggests; never auto-builds (pyramid builds are expensive).

    Stays cheap on the deploy hot path: it uses the stored composite's ``pixel_scale``
    (so a non-30mas composite isn't perpetually reported stale) and reads hashes
    registry-only (no multi-GB re-hash) — if any fiducial mosaic isn't registered yet
    it can't confirm freshness and simply skips rather than false-suggesting.
    """
    try:
        resp = (client.table('fitsgl_datasets')
                .select('source_hashes,pixel_scale')
                .eq('field', field).eq('kind', 'field').limit(1).execute())
        stored_row = resp.data[0] if resp.data else None
        pixel_scale = stored_row['pixel_scale'] if stored_row else default_pixel_scale

        mosaics, _tiles, _bands = _select_dataset_mosaics(field, tile=None, pixel_scale=pixel_scale)
        if not mosaics:
            return  # no fiducial declaration / no mosaics → nothing to suggest
        current = resolve_source_hashes(client, mosaics, local_fallback=False)
        if _hash_count(current) < len(mosaics):
            return  # some fiducial mosaic unregistered → can't confirm; don't nag

        stored = stored_row['source_hashes'] if stored_row else None
        if stored == current:
            return  # up to date
        if stored is None:
            click.echo(f"\n💡 No FitsGL map dataset for '{field}'. To enable the FitsGL map:")
        else:
            click.echo(f"\n💡 FitsGL map dataset for '{field}' is stale (mosaics changed). Rebuild:")
        click.echo(f"     campfire fitsgl build --field {field}")
        click.echo(f"     campfire fitsgl deploy --field {field}")
    except Exception:
        return
