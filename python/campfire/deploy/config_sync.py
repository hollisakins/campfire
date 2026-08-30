"""Data-management config sync (issue #303): the three TOMLs <-> the cloud registry.

programs.toml / observations.toml / fields.toml define *which data gets reduced
in which groups*. The cloud tables (`programs` / `observations` / `fields`) are
the source of truth for their current state: every row carries the TOML section
mirrored losslessly in ``config`` (jsonb) plus ``config_hash`` — the sha256 of
the section's canonical JSON form — so two reducers (or an ephemeral container)
can regenerate and reconcile their local TOMLs from the cloud.

This module owns the shared contract:

- :func:`config_hash` — the divergence token. Computed identically from a
  freshly-parsed TOML section and from a ``config`` jsonb payload, so local and
  cloud state compare by hash alone.
- ``*_config_row`` mappers — section -> row, lifting the commonly-queried bits
  into typed columns next to the lossless ``config`` (mirrors
  :func:`campfire.deploy.fields.field_config_row`, which owns the fields shape).
- :func:`push_kind` — one push path for all three kinds, used by
  ``campfire config push`` and delegated to by the deploy-time upserts.
- The local sync-state file (``$CAMPFIRE_ROOT/meta/config_sync_state.json``)
  recording the last-synced hash per section — the *base* of the three-way
  pull/push logic. Deliberately not in campfire.db: config sync must not be
  coupled to the mirror's strict SCHEMA_VERSION.

Sections must be JSON-representable: bare TOML datetimes/dates are rejected at
push time (:func:`find_unjsonable`) because they would silently change type on
the jsonb round trip — use ISO strings in the TOML instead.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path

from .config import _load_toml
from .fields import _PID_RE, field_config_row

KINDS = ("programs", "observations", "fields")

# Typed columns each kind lifts out of the section. The section itself always
# rides in `config`; anything not listed here still round-trips through it.
_TABLES = {"programs": "programs", "observations": "observations", "fields": "fields"}
_PK = {"programs": "slug", "observations": "name", "fields": "name"}


# ---------------------------------------------------------------------------
# Canonical hash + JSON-representability
# ---------------------------------------------------------------------------

def canonical_json(section: dict) -> str:
    """Deterministic JSON form of a TOML section (sorted keys, no whitespace)."""
    return json.dumps(section, sort_keys=True, separators=(",", ":"))


def config_hash(section: dict) -> str:
    """sha256 of the canonical JSON form, scheme-prefixed like content_hash."""
    digest = hashlib.sha256(canonical_json(section).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def find_unjsonable(section, _path="") -> list[str]:
    """Key paths whose values would not survive the jsonb round trip.

    TOML has native datetime/date/time; JSON does not. A bare TOML date would
    come back from the cloud as a string, silently changing the parsed type —
    so push refuses them with the offending path (use quoted ISO strings).
    """
    bad = []
    if isinstance(section, dict):
        for k, v in section.items():
            bad += find_unjsonable(v, f"{_path}.{k}" if _path else str(k))
    elif isinstance(section, list):
        for i, v in enumerate(section):
            bad += find_unjsonable(v, f"{_path}[{i}]")
    elif isinstance(section, (_dt.datetime, _dt.date, _dt.time)):
        bad.append(_path)
    return bad


# ---------------------------------------------------------------------------
# TOML file access (raw sections — no injected keys)
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    root = os.environ.get("CAMPFIRE_ROOT")
    if not root:
        raise RuntimeError("$CAMPFIRE_ROOT is not set")
    return Path(root) / "config"


def toml_path(kind: str) -> Path:
    """``$CAMPFIRE_ROOT/config/<kind>.toml``."""
    if kind not in KINDS:
        raise ValueError(f"unknown config kind: {kind!r}")
    return _config_dir() / f"{kind}.toml"


def load_sections(kind: str) -> dict[str, dict]:
    """Raw ``{name: section}`` from the local TOML (``{}`` if absent).

    Unlike :func:`campfire.deploy.config.load_programs` this injects nothing —
    the section is the faithful mirror that rides in ``config`` jsonb.
    """
    path = toml_path(kind)
    return _load_toml(path) if path.exists() else {}


# ---------------------------------------------------------------------------
# Section -> row mappers
# ---------------------------------------------------------------------------

def _stamp(row: dict, section: dict) -> dict:
    row["config"] = section
    row["config_hash"] = config_hash(section)
    row["config_updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return row


def program_typed_row(slug: str, section: dict) -> dict:
    """The typed `programs` columns only — no config mirror. Deploy-time
    callers fall back to this when the section is not JSON-representable
    (deploy must never fail over a bare TOML date). Empty strings normalize
    to NULL (the pre-#303 upsert wrote ``''`` defaults; don't propagate them)."""
    return {
        "slug": slug,
        "program_name": section.get("program_name") or slug,
        "pi_name": section.get("pi_name") or None,
        "description": section.get("description") or None,
        "is_public": section.get("is_public", False),
        "cycle": section.get("cycle"),
    }


def program_config_row(slug: str, section: dict) -> dict:
    """programs.toml ``[<slug>]`` -> a full `programs` row: typed columns plus
    the lossless config mirror and hash/stamp. Raises TypeError on sections
    that cannot ride jsonb — callers guard with :func:`find_unjsonable`."""
    return _stamp(program_typed_row(slug, section), section)


def _jwst_pid(section: dict) -> int | None:
    """The 5-digit JWST program id: explicit ``program_id`` key, else parsed
    from the first ``jwPPPPP*`` file glob (same convention as the pipeline)."""
    if section.get("program_id") is not None:
        return int(section["program_id"])
    for g in _as_list(section.get("files")):
        m = _PID_RE.search(g)
        if m:
            return int(m.group(1))
    return None


def _as_list(value) -> list:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def observation_config_row(name: str, section: dict) -> dict:
    """observations.toml ``[<name>]`` -> an `observations` row.

    Requires ``program`` and ``field`` (NOT NULL columns with an FK to
    programs); raises ValueError otherwise so callers can skip-with-warning.
    The full section (stage overrides, config_groups, ...) rides in ``config``.
    """
    program = section.get("program")
    field = section.get("field")
    pid = _jwst_pid(section)
    missing = [k for k, v in
               (("program", program), ("field", field), ("program_id/files", pid))
               if not v]
    if missing:
        raise ValueError(f"observation '{name}' is missing {', '.join(missing)}")
    row = {
        "name": name,
        "program_slug": program,
        "jwst_program_id": pid,
        "field": field,
    }
    globs = _as_list(section.get("files"))
    if globs:
        row["file_globs"] = globs
    gratings = _as_list(section.get("gratings"))
    if gratings:
        row["gratings"] = gratings
    if section.get("data_subdir") is not None:
        row["data_subdir"] = section["data_subdir"]
    return _stamp(row, section)


def _field_row(name: str, section: dict) -> dict:
    """fields.toml section -> row: the existing fields mapper + the sync stamp."""
    row = field_config_row(name, section)
    return _stamp(row, section)


_ROW_FN = {
    "programs": program_config_row,
    "observations": observation_config_row,
    "fields": _field_row,
}


def config_row(kind: str, name: str, section: dict) -> dict:
    return _ROW_FN[kind](name, section)


# ---------------------------------------------------------------------------
# Cloud access
# ---------------------------------------------------------------------------

_CLOUD_COLS = "config, config_hash, config_updated_at, retired_at"


def fetch_cloud_rows(client, kind: str, names: list[str] | None = None) -> dict[str, dict]:
    """``{name: row}`` of config-relevant columns for *kind* (RLS-scoped —
    non-admins see what their program access allows). fields also carries
    ``programs`` so push can tell when only the slug backfill changed."""
    cols = f"{_PK[kind]}, {_CLOUD_COLS}"
    if kind == "fields":
        cols += ", programs"
    q = client.table(_TABLES[kind]).select(cols)
    if names:
        q = q.in_(_PK[kind], names)
    resp = q.execute()
    return {r[_PK[kind]]: r for r in (resp.data or [])}


def cloud_hash(row: dict | None) -> str | None:
    """The row's divergence token; recomputed from ``config`` for rows pushed
    before config_hash existed (or by an older client)."""
    if not row:
        return None
    if row.get("config_hash"):
        return row["config_hash"]
    if row.get("config") is not None:
        return config_hash(row["config"])
    return None


def push_kind(client, kind: str, sections: dict[str, dict],
              names: list[str] | None = None, *,
              programs_config: dict | None = None,
              base: dict[str, str] | None = None,
              force: bool = False,
              dry_run: bool = False) -> tuple[int, dict[str, str]]:
    """Upsert local sections of *kind* into the cloud.

    - ``names`` scopes the push: ``None`` means every local section, while an
      explicit list — INCLUDING an empty one — is honored verbatim. The
      distinction matters: a caller that computed "fields with deployed data"
      and found none must push nothing, not fall through to everything.
    - ``programs_config`` (observations only) validates the program slug so a
      name/slug mix-up cannot create a broken FK at push time.
    - ``base`` (``{name: hash}`` from the state file) is the divergence guard:
      if the cloud section changed since the caller's last sync AND differs
      from what is being pushed, the section is refused unless ``force``.

    Returns ``(pushed_count, {name: new_hash})`` for state recording.
    """
    todo = sorted(sections) if names is None else list(names)
    cloud = fetch_cloud_rows(client, kind, todo) if todo else {}
    pushed: dict[str, str] = {}
    n = 0
    for name in todo:
        section = sections.get(name)
        if section is None:
            print(f"  ! {name}: not in {kind}.toml — skipping")
            continue
        bad = find_unjsonable(section)
        if bad:
            print(f"  ! {name}: bare TOML datetime at {', '.join(bad)} — "
                  f"quote as ISO string to sync. Skipping.")
            continue
        if kind == "observations" and programs_config is not None:
            slug = section.get("program")
            if slug not in programs_config:
                known = ", ".join(sorted(programs_config)) or "(none)"
                print(f"  ! {name}: program '{slug}' is not a slug in "
                      f"programs.toml (known: {known}) — skipping")
                continue
        try:
            row = config_row(kind, name, section)
        except ValueError as e:
            print(f"  ! {e} — skipping")
            continue
        if kind == "fields":
            from .fields import resolve_field_programs
            row = resolve_field_programs(client, row)
        crow = cloud.get(name)
        if crow and crow.get("retired_at") and not force:
            print(f"  ! {name}: retired in cloud "
                  f"({crow['retired_at']}) — skipping (--force to re-push)")
            continue
        chash = cloud_hash(crow)
        if chash == row["config_hash"]:
            # Config is in sync, but the programs slug backfill (#454) rides
            # outside the config hash: an unchanged field whose resolution
            # improved still needs a write — a narrow one, so the config
            # mirror and its updated_at stamp stay untouched. This is the
            # MAIN backfill path: pre-existing fields rarely change their TOML.
            new_programs = row.get("programs")
            if (kind == "fields" and new_programs
                    and sorted(crow.get("programs") or []) != new_programs):
                if dry_run:
                    print(f"  would update programs for {name} "
                          f"-> {new_programs}")
                    continue
                client.table(_TABLES[kind]).upsert(
                    {_PK[kind]: name, "programs": new_programs},
                    on_conflict=_PK[kind]).execute()
                pushed[name] = row["config_hash"]
                n += 1
                print(f"  ~ {name} (programs -> {', '.join(new_programs)})")
                continue
            pushed[name] = row["config_hash"]
            print(f"  = {name} (in sync)")
            continue
        if (not force and crow is not None and base is not None
                and chash is not None and base.get(name) != chash):
            if base.get(name) is None:
                print(f"  ! {name}: cloud already has a version of this "
                      f"section that this machine never synced — run "
                      f"`campfire config pull` to reconcile, or --force to "
                      f"overwrite")
            else:
                print(f"  ! {name}: cloud section changed since your last "
                      f"sync (someone else pushed?) — run `campfire config "
                      f"pull` or `campfire config diff`, or --force to "
                      f"overwrite")
            continue
        if dry_run:
            print(f"  would push {name}")
            continue
        client.table(_TABLES[kind]).upsert(row, on_conflict=_PK[kind]).execute()
        pushed[name] = row["config_hash"]
        n += 1
        print(f"  + {name}")
    return n, pushed


# ---------------------------------------------------------------------------
# Local sync state (the "base" of the three-way merge)
# ---------------------------------------------------------------------------

_STATE_VERSION = 1


def _state_path() -> Path:
    root = os.environ.get("CAMPFIRE_ROOT")
    if not root:
        raise RuntimeError("$CAMPFIRE_ROOT is not set")
    return Path(root) / "meta" / "config_sync_state.json"


def load_state() -> dict:
    """``{"version": 1, "base": {kind: {name: hash}}}`` (empty if absent)."""
    path = _state_path()
    try:
        data = json.loads(path.read_text())
        if data.get("version") == _STATE_VERSION:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": _STATE_VERSION, "base": {k: {} for k in KINDS}}


def save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def record_base(state: dict, kind: str, hashes: dict[str, str]) -> None:
    state.setdefault("base", {}).setdefault(kind, {}).update(hashes)


def record_synced(kind: str, hashes: dict[str, str]) -> None:
    """Best-effort load/record/save for deploy-time upserts.

    A deploy that mirrors a section to the cloud has, by construction, just
    synced local and cloud — so the hash it wrote is the operator's new base.
    Without this, the operator's own deploy later reads as "someone else
    pushed" and `config push` refuses their next hand-edit indefinitely.
    Never raises: base recording is bookkeeping and must not fail a deploy
    (e.g. no $CAMPFIRE_ROOT in an exotic CI setup)."""
    if not hashes:
        return
    try:
        state = load_state()
        record_base(state, kind, hashes)
        save_state(state)
    except Exception as e:
        print(f"  Warning: could not record config sync base ({kind}): {e}")
