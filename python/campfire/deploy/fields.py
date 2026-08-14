"""fields.toml -> the cloud `fields` table (issue #303).

The NIRCam field registry: the third leg of the cloud-as-source-of-truth config
loop (programs / observations / fields). The full fields.toml section is mirrored
losslessly into the `config` jsonb column; the commonly-queried bits are lifted
into typed columns.

`coverage_area_*`, `expmap_pixel_scale_arcsec` and `latest_deployment_id` are
**deploy-owned** — written by `deploy_nircam` from `<field>_layout.json` — so
`sync_fields` never sends them and the upsert leaves them untouched on existing rows
(PostgREST merge-duplicates only updates the columns present in the payload).

Scoped to fields that already have deployed NIRCam data, so the cloud table
reflects what is actually reduced, not every field defined locally.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .config import _load_toml

# JWST rootname program id: jwPPPPP... (the leading 5-digit program number).
_PID_RE = re.compile(r"jw(\d{5})")

# A pixel-scale WCS subsection key, e.g. ``30mas``.
_PIXEL_SCALE_RE = re.compile(r"^\d+mas$")


def _is_tile_section(value: dict) -> bool:
    """True if a ``[<field>.<key>]`` sub-table is a tile definition.

    Mirrors the pipeline's tile gate (``campfire_pipeline.nircam.field``): a tile
    declares explicit sky ``corners`` or at least one ``<N>mas`` subsection with
    ``crpix``+``naxis``. Deploy must not import the pipeline, so the gate is
    reproduced here. This is what distinguishes a real tile from a step-override
    table (``[<field>.jhat]``, ``[<field>.align]``, …), which are also dicts but
    carry no WCS — so those are *not* tiles.
    """
    if not isinstance(value, dict):
        return False
    if "corners" in value:
        return True
    return any(
        _PIXEL_SCALE_RE.match(k) and isinstance(v, dict)
        and "crpix" in v and "naxis" in v
        for k, v in value.items()
    )


def declared_tiles_and_epochs(section: dict) -> tuple[set[str], set[str]]:
    """The tile + epoch names a fields.toml ``[<field>]`` section declares.

    The single authority for "what did the *current* config ask the pipeline to
    produce" — used both to lift the `fields` registry columns and to scope
    mosaic discovery at deploy time (drop stray on-disk products from a former
    config). Tiles pass :func:`_is_tile_section` (so step-override tables are
    excluded); epochs are the ``[<field>.epochs.<name>]`` sub-tables.
    """
    tiles = {k for k, v in section.items()
             if k != "epochs" and _is_tile_section(v)}
    epochs = set((section.get("epochs") or {}).keys())
    return tiles, epochs


def _fields_toml_path() -> Path:
    root = os.environ.get("CAMPFIRE_ROOT")
    if not root:
        raise RuntimeError("$CAMPFIRE_ROOT is not set")
    return Path(root) / "config" / "fields.toml"


def load_fields_toml() -> dict:
    """Full parsed fields.toml (``{}`` if the file is absent)."""
    path = _fields_toml_path()
    return _load_toml(path) if path.exists() else {}


def _as_list(value) -> list:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def field_config_row(field: str, section: dict) -> dict:
    """Map a fields.toml ``[<field>]`` section to the `fields` config columns.

    Lossless: the whole section rides in ``config`` (jsonb); the rest are lifted
    for querying. Tile names come from :func:`declared_tiles_and_epochs` (WCS-bearing
    sub-tables only — step-override tables like ``jhat``/``align`` are excluded).
    Program ids are the 5-digit ids embedded in the ``files`` globs (``jwPPPPP*``).
    Program *slugs* are resolved separately at write time by
    :func:`resolve_field_programs` (programs.toml carries no program-id map, so
    the mapping comes from the cloud `observations` rows); this row deliberately
    omits the ``programs`` key so an unresolved write never clobbers a
    previously resolved value (PostgREST only updates columns present in the
    payload — same contract as the deploy-owned columns).
    """
    tp = _as_list(section.get("tangent_point"))
    globs = _as_list(section.get("files"))
    pids = sorted({int(m.group(1)) for g in globs
                   for m in [_PID_RE.search(g)] if m})
    tiles, epochs = declared_tiles_and_epochs(section)
    return {
        "name": field,
        "display_name": section.get("display_name"),  # None -> RPC derives upper()
        "filters": _as_list(section.get("filters")),
        "tiles": sorted(tiles),
        "fiducial_tiles": _as_list(section.get("fiducial_tiles")),
        "epochs": sorted(epochs),
        "jwst_program_ids": pids,
        "file_globs": globs,
        "center_ra": tp[0] if len(tp) > 0 else None,
        "center_dec": tp[1] if len(tp) > 1 else None,
        "config": section,
    }


def resolve_field_programs(client, row: dict) -> dict:
    """Best-effort ``fields.programs`` slug backfill (issue #454).

    Maps the field's ``jwst_program_ids`` (lifted from its file globs) through
    the cloud `observations` rows (``jwst_program_id -> program_slug``) — the
    one place both identifiers coexist. Ids with no matching observation row
    stay unresolved; when nothing resolves the ``programs`` key is left absent
    so the upsert keeps whatever the column already holds. Never raises: this
    runs inside deploys.
    """
    pids = row.get("jwst_program_ids") or []
    if not pids:
        return row
    try:
        resp = (client.table("observations")
                .select("jwst_program_id, program_slug")
                .in_("jwst_program_id", pids).execute())
        slugs = sorted({r["program_slug"] for r in (resp.data or [])
                        if r.get("program_slug")})
        if slugs:
            row["programs"] = slugs
    except Exception as e:
        print(f"  Warning: could not resolve field programs: {e}")
    return row


def read_layout_coverage(products_dir, field: str) -> dict | None:
    """Parse ``<field>_layout.json`` (the deploy-computed survey area), or None."""
    path = Path(products_dir) / f"{field}_layout.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def upsert_field(client, row: dict) -> None:
    """Upsert one `fields` row (on conflict = name). Only the keys present in
    ``row`` are written, so a config-only sync leaves the deploy-owned columns
    (coverage_area_*, latest_deployment_id) untouched on existing rows.

    The single write path for fields rows, so the config-sync stamp (#303 —
    config_hash + config_updated_at) is applied here for every producer
    (sync_fields, upsert_field_on_deploy, `campfire config push`). Lazy import:
    config_sync imports this module at top level."""
    if "programs" not in row:
        row = resolve_field_programs(client, row)
    if row.get("config") is not None and "config_hash" not in row:
        import datetime as _dt
        from .config_sync import config_hash, find_unjsonable
        if find_unjsonable(row["config"]):
            # Bare TOML datetimes can't ride jsonb faithfully; push validates
            # loudly, but deploy-time upserts must never fail the deploy — drop
            # only the config mirror and keep the typed columns.
            row = {k: v for k, v in row.items() if k != "config"}
        else:
            row["config_hash"] = config_hash(row["config"])
            row["config_updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    client.table("fields").upsert(row, on_conflict="name").execute()
    if "config_hash" in row:
        # The hash just written IS the operator's new sync base — without it,
        # their own deploy reads as "someone else pushed" on the next
        # `config push` (see config_sync.record_synced).
        from .config_sync import record_synced
        record_synced("fields", {row["name"]: row["config_hash"]})


def upsert_field_on_deploy(client, products_dir, field: str,
                           deployment_id: int | None) -> bool:
    """Ride ``deploy_nircam``: sync the field's fields.toml config AND write the
    deploy-owned columns (coverage area from ``<field>_layout.json``,
    latest_deployment_id). Returns True if a row was upserted."""
    section = load_fields_toml().get(field)
    if section is None:
        print(f"  fields: '{field}' not in fields.toml — skipping registry upsert")
        return False
    row = field_config_row(field, section)
    if deployment_id is not None:
        row["latest_deployment_id"] = deployment_id
    coverage = read_layout_coverage(products_dir, field)
    area_msg = ""
    if coverage and coverage.get("coverage_area_arcmin2") is not None:
        row["coverage_area_arcmin2"] = coverage["coverage_area_arcmin2"]
        row["coverage_area_deg2"] = coverage.get("coverage_area_deg2")
        area_msg = f", area {coverage['coverage_area_arcmin2']:.1f} arcmin2"
    # The expmap grid's pixel scale, so the web product table can show a Scale
    # for expmap rows (an expmap FITS carries it only in CDELT1/2, and the
    # registry row has no scale axis). Lifted separately from the area: it is
    # present whenever the layout JSON is, independent of coverage.
    if coverage and coverage.get("pixel_scale_arcsec") is not None:
        row["expmap_pixel_scale_arcsec"] = coverage["pixel_scale_arcsec"]
    upsert_field(client, row)
    print(f"  fields: upserted '{field}' "
          f"({len(row['filters'])} filters, {len(row['tiles'])} tiles{area_msg})")
    return True


def deployed_field_names(client) -> list[str]:
    """Field names that already have a deployment — the scope for sync-fields."""
    resp = client.table("deployments").select("field").execute()
    return sorted({r["field"] for r in (resp.data or []) if r.get("field")})


def sync_fields(client, field_names=None, *, dry_run=False) -> int:
    """Upsert the fields.toml **config** (only) for the given fields — or, when
    ``field_names`` is None, for every field that already has deployed data.
    Never touches the deploy-owned coverage/deployment columns. Returns the count
    upserted."""
    toml = load_fields_toml()
    if field_names is None:
        field_names = deployed_field_names(client)
    n = 0
    for name in field_names:
        section = toml.get(name)
        if section is None:
            print(f"  ! {name}: not in fields.toml — skipping")
            continue
        row = field_config_row(name, section)
        if dry_run:
            print(f"  would sync {name} "
                  f"({len(row['filters'])} filters, {len(row['tiles'])} tiles, "
                  f"pids {row['jwst_program_ids']})")
            continue
        upsert_field(client, row)
        print(f"  + {name}")
        n += 1
    return n
