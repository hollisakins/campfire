"""fields.toml -> the cloud `fields` table (issue #303).

The NIRCam field registry: the third leg of the cloud-as-source-of-truth config
loop (programs / observations / fields). The full fields.toml section is mirrored
losslessly into the `config` jsonb column; the commonly-queried bits are lifted
into typed columns.

`coverage_area_*` and `latest_deployment_id` are **deploy-owned** — written by
`deploy_nircam` from `<field>_layout.json` — so `sync_fields` never sends them and
the upsert leaves them untouched on existing rows (PostgREST merge-duplicates only
updates the columns present in the payload).

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
    Program *slugs* are left empty — programs.toml carries no program-id map, so
    slug resolution is deferred (issue #303).
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
        "programs": [],  # slug resolution deferred (no program-id map in programs.toml)
        "jwst_program_ids": pids,
        "file_globs": globs,
        "center_ra": tp[0] if len(tp) > 0 else None,
        "center_dec": tp[1] if len(tp) > 1 else None,
        "config": section,
    }


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
    (coverage_area_*, latest_deployment_id) untouched on existing rows."""
    client.table("fields").upsert(row, on_conflict="name").execute()


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
