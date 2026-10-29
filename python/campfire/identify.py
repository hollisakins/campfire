"""``campfire identify FILE...`` (P5-2, docs/design-data-releases.md §4.6):
what the archive knows about a file, from its bytes.

A file is hashed twice: the whole-file sha256 (the registry's ``content_hash``
/ ``spectra.file_hash``; for a stamped release member the ``stamped_hash``) and,
when the pipeline is installed and the file is FITS, the science hash
(``campfire_pipeline.metadata.science_hash``: headers minus the volatile cards
plus the data segments, the identity that survives a ``CMPFRDR`` stamp or a
no-op re-reduction). The hashes are looked up locally first, in the live
mirror and every release store on this machine, then on the server
(``POST /api/v1/identify`` → ``identify_files``, under the caller's program
scope). The server returns facts; :func:`describe` phrases them:

* ``in DR2026.1, still current``: a release member whose live spectrum still
  carries the frozen science hash;
* ``in DR2026.1, superseded in live on <date>``: the live bytes moved since;
* ``live only, deployed <date>, not in any DR; cite with a provenance file``;
* ``archived, collected on <date>``: the bytes were a release member once and
  the archive copy has been collected;
* ``unknown``: not a CAMPFIRE product the caller may see, or superseded before
  any release froze it (bytes not kept).
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

HASH_RE = re.compile(r'^sha256:[0-9a-f]{64}$')


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8 << 20), b''):
            h.update(chunk)
    return 'sha256:' + h.hexdigest()


def science_hash_of(path: Path) -> Optional[str]:
    """The pipeline's science hash, or None when the pipeline is not installed
    or the file is not a FITS file it can read (the sha256 still identifies a
    byte-identical copy; the science hash is what survives a stamp)."""
    try:
        from campfire_pipeline.metadata.science_hash import science_hash
    except ImportError:
        return None
    try:
        return science_hash(path)
    except Exception:  # noqa: BLE001 - not a FITS file, truncated, ...: no science identity
        return None


@dataclass
class FileIdentity:
    path: Path
    sha256: str
    science_hash: Optional[str] = None
    local: list = field(default_factory=list)      # hits in the stores on this machine
    server: Optional[dict] = None                  # identify_files' answer, when asked
    error: Optional[str] = None

    @property
    def sentence(self) -> str:
        return describe(self.server, self.local)

    def as_dict(self) -> dict:
        return {'path': str(self.path), 'sha256': self.sha256, 'science_hash': self.science_hash,
                'local': self.local, 'server': self.server, 'error': self.error, 'sentence': self.sentence}


def hash_file(path: Path) -> FileIdentity:
    return FileIdentity(path=Path(path), sha256=sha256_of(path), science_hash=science_hash_of(path))


# ---------------------------------------------------------------------------
# the stores on this machine
# ---------------------------------------------------------------------------

def _rows(db: Path, sql: str, params: tuple) -> list[dict]:
    """Read-only rows from a store, whatever its schema version (an older
    store still answers what it has; a missing table answers nothing)."""
    try:
        conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def local_matches(data_dir: Path, sha256: str, science_hash: Optional[str]) -> list[dict]:
    """Hits in the live mirror (``meta/campfire.db``) and every release store
    (``dr/<tag>/meta/campfire.db``) under *data_dir*."""
    from .dr_store import DR_DIRNAME

    hits: list[dict] = []
    live = Path(data_dir) / 'meta' / 'campfire.db'
    if live.exists():
        for r in _rows(live, "SELECT storage_key, spectrum_id, observation, field, product_type, local_path "
                             "FROM storage_objects WHERE content_hash = ? OR local_file_hash = ?", (sha256, sha256)):
            hits.append({'source': 'live', 'match': 'file', **r})
        if science_hash:
            for r in _rows(live, "SELECT spectrum_id, observation, program_slug FROM spectra WHERE science_hash = ?",
                           (science_hash,)):
                hits.append({'source': 'live', 'match': 'science', **r})
    dr_root = Path(data_dir) / DR_DIRNAME
    if dr_root.is_dir():
        for tag_dir in sorted(dr_root.iterdir()):
            db = tag_dir / 'meta' / 'campfire.db'
            if not db.exists():
                continue
            for r in _rows(db, "SELECT storage_key, spectrum_id, product_type, byte_state, archive_key, "
                               "CASE WHEN stamped_hash = ? THEN 'stamped' WHEN content_hash = ? THEN 'file' ELSE 'science' END AS match "
                               "FROM release_manifest WHERE stamped_hash = ? OR content_hash = ? OR (? IS NOT NULL AND science_hash = ?)",
                           (sha256, sha256, sha256, sha256, science_hash, science_hash)):
                hits.append({'source': 'dr', 'tag': tag_dir.name, **r})
    return hits


# ---------------------------------------------------------------------------
# phrasing
# ---------------------------------------------------------------------------

def _day(value: Optional[str]) -> str:
    if not value:
        return 'an unknown date'
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        return str(value)[:10]


def _tag_key(tag: str):
    # DR2026.1 > DR2026.0 > DR2025.3: numeric where it can be
    parts = re.findall(r'\d+|\D+', tag)
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def describe(server: Optional[dict], local: Iterable[dict] = ()) -> str:
    """One sentence for a file, from the server's facts (or, without them,
    from the local hits alone)."""
    local = list(local)
    if server is None:
        tags = sorted({h['tag'] for h in local if h.get('source') == 'dr'}, key=_tag_key, reverse=True)
        if tags:
            return f"in {' and '.join(tags)} (local store; the server was not asked)"
        if any(h.get('source') == 'live' for h in local):
            return "in the live catalog (local mirror; the server was not asked)"
        return "not in any store on this machine (the server was not asked)"

    releases = server.get('releases') or []
    live = server.get('live') or []
    collected = server.get('collected') or []
    if releases:
        by_tag: dict[str, list] = {}
        for r in releases:
            by_tag.setdefault(r['tag'], []).append(r)
        tags = sorted(by_tag, key=_tag_key, reverse=True)
        newest = by_tag[tags[0]]
        current = [r.get('current') for r in newest]
        head = f"in {' and '.join(tags)}"
        if all(c is None for c in current):
            tail = ""                      # a field product: no live spectrum to compare
        elif any(c is True for c in current):
            tail = ", still current"
        else:
            when = next((r.get('live_deployed_at') for r in newest if r.get('live_deployed_at')), None)
            tail = f", superseded in live on {_day(when)}"
        archived = any(r.get('byte_state') in ('copied', 'stamped') for r in newest)
        return head + tail + ("" if archived else " (archive copy not yet made)")
    if live:
        when = next((r.get('deployed_at') for r in live if r.get('deployed_at')), None)
        how = "" if any(r.get('match') == 'file' for r in live) else " (same science, different bytes)"
        return f"live only, deployed {_day(when)}, not in any DR; cite with a provenance file{how}"
    if collected:
        return f"archived, collected on {_day(collected[0].get('collected_at'))}"
    return "unknown to the archive: not a CAMPFIRE product you can see, or superseded before any release froze it (bytes not kept)"


def subject(identity: FileIdentity) -> Optional[str]:
    """What the file is, when something named it: the spectrum or the key."""
    cands = []
    if identity.server:
        for r in identity.server.get('releases') or []:
            cands.append(r.get('spectrum_id') or r.get('storage_key'))
        for r in identity.server.get('live') or []:
            cands.append(r.get('spectrum_id') or r.get('storage_key'))
    for h in identity.local:
        cands.append(h.get('spectrum_id') or h.get('storage_key'))
    return next((c for c in cands if c), None)


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def identify_paths(paths: Iterable[Path], *, data_dir: Path, api=None) -> list[FileIdentity]:
    """Hash every path, look the hashes up locally and, with *api*, on the
    server (one batched request). A path that cannot be read is reported
    with its error and no hashes looked up."""
    out: list[FileIdentity] = []
    for p in paths:
        p = Path(p)
        try:
            ident = hash_file(p)
        except OSError as e:
            out.append(FileIdentity(path=p, sha256='', error=str(e)))
            continue
        ident.local = local_matches(data_dir, ident.sha256, ident.science_hash)
        out.append(ident)
    if api is not None:
        asked = [i for i in out if i.sha256]
        if asked:
            answers = api.identify_files([{'sha256': i.sha256, 'science_hash': i.science_hash} for i in asked])
            for i, a in zip(asked, answers):
                i.server = a
    return out
