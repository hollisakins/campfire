"""Provenance files (P5-3, docs/design-data-releases.md §4.8).

A provenance file (``paper.campfire.json``) is what an author writes from
live: the objects and files a piece of work drew on, with the hashes of the
bytes actually on this machine, the object version, redshift and quality at
that moment, and which data releases on this machine already hold each file.
It is built from the local ledgers only (the live mirror and the release
stores under ``$CAMPFIRE_ROOT``), never registered on the server, and resolves
later for as long as its files are current in live or in a release (D-P2-4:
the archive keeps release members only). ``check`` re-reads it: are the files
still those bytes, what does the archive say about them now, and does a
release hold the same science.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .identify import describe, local_matches, science_hash_of, sha256_of

FORMAT = 'campfire-provenance/1'

OBJECT_FIELDS = ('object_id', 'version', 'redshift', 'redshift_auto', 'redshift_inspected',
                 'redshift_quality', 'field', 'programs', 'last_inspected_at', 'last_data_change_at')


class ProvenanceError(Exception):
    pass


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------

def _selected_objects(store, *, lists: Iterable[str], observations: Iterable[str], programs: Iterable[str],
                      objects: Iterable[str]) -> list[dict]:
    lists, observations, programs, objects = map(list, (lists, observations, programs, objects))
    if not (lists or observations or programs or objects):
        raise ProvenanceError("select something: --list, --obs, --program or object names")
    found: dict[str, dict] = {}
    if lists or observations or programs:
        for row in store.query_objects(tags=lists or None, observations=observations or None,
                                       programs=programs or None, limit=None):
            found[row['object_id']] = row
    missing = []
    for name in objects:
        row = store.get_object(name)
        if row is None:
            missing.append(name)
        else:
            found[name] = row
    if missing:
        raise ProvenanceError("not in the local catalog: " + ", ".join(missing) + " (run `campfire sync`)")
    rows = [found[k] for k in sorted(found)]
    for row in rows:   # query_objects keeps the mirror's ';'-joined lists
        for col in ('programs', 'gratings', 'observations'):
            if isinstance(row.get(col), str):
                row[col] = [v for v in row[col].split(';') if v]
    return rows


def _spectra_of(store, object_names: list[str]) -> list[dict]:
    """Every spectrum of the objects with its final's registry row (what
    ``get_spectrum`` joins), ordered by name."""
    out: list[dict] = []
    for start in range(0, len(object_names), 500):
        chunk = object_names[start:start + 500]
        marks = ','.join('?' * len(chunk))
        rows = store._conn.execute(
            f"""SELECT s.spectrum_id, s.object_id, s.observation, s.program_slug, s.field, s.grating,
                       s.cfpipe_version, s.crds_context, s.science_hash, s.reduced_at,
                       so.storage_key, so.content_hash, so.size_bytes, so.local_path, so.local_file_hash
                FROM spectra s
                LEFT JOIN storage_objects so
                       ON so.spectrum_id = s.spectrum_id AND so.product_type = 'nirspec_spec' AND so.status = 'active'
                WHERE s.object_id IN ({marks})""", chunk).fetchall()
        out.extend(dict(r) for r in rows)
    out.sort(key=lambda r: r['spectrum_id'])
    return out


def build(store, data_dir: Path, *, lists: Iterable[str] = (), observations: Iterable[str] = (),
          programs: Iterable[str] = (), objects: Iterable[str] = (), base_url: Optional[str] = None,
          latest_dr: Optional[str] = None, log=None) -> dict:
    """The provenance document of a selection of the live mirror: each
    selected object as it is now, each of its spectra with the hash of the
    file on disk (``sha256``; null when the file is not here) beside the
    registry's (``server_hash``), and the releases on this machine holding
    those bytes or that science."""
    from ._version import __version__

    data_dir = Path(data_dir)
    products = data_dir / 'products'
    objs = _selected_objects(store, lists=lists, observations=observations, programs=programs, objects=objects)
    names = [o['object_id'] for o in objs]
    files = []
    on_disk = 0
    for s in _spectra_of(store, names):
        entry = {k: s.get(k) for k in ('storage_key', 'spectrum_id', 'object_id', 'observation', 'program_slug',
                                       'field', 'grating', 'cfpipe_version', 'crds_context', 'reduced_at')}
        entry['server_hash'] = s.get('content_hash')
        entry['science_hash'] = s.get('science_hash')
        entry['size_bytes'] = s.get('size_bytes')
        entry['local_path'] = s.get('local_path')
        entry['sha256'] = None
        entry['local_science_hash'] = None
        path = products / s['local_path'] if s.get('local_path') else None
        if path is not None and path.exists():
            entry['sha256'] = sha256_of(path)
            entry['local_science_hash'] = science_hash_of(path)
            on_disk += 1
            if log and entry['sha256'] != entry['server_hash']:
                log(f"  {s['spectrum_id']}: the file on disk is not the registry's bytes (recorded as opened)")
        else:
            entry['local_path'] = None
        sci = entry['local_science_hash'] or (entry['science_hash'] if entry['sha256'] == entry['server_hash']
                                              or entry['sha256'] is None else None)
        hits = local_matches(data_dir, entry['sha256'] or entry['server_hash'] or '', sci)
        entry['in_releases'] = sorted({h['tag'] for h in hits if h.get('source') == 'dr'})
        files.append(entry)
    doc = {
        'format': FORMAT,
        'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'client_version': __version__,
        'base_url': base_url,
        'channel': 'live',
        'latest_dr': latest_dr,
        'last_synced_at': store.get_last_synced_at(),
        'selection': {'lists': sorted(lists), 'observations': sorted(observations),
                      'programs': sorted(programs), 'objects': sorted(objects)},
        'objects': [{k: o.get(k) for k in OBJECT_FIELDS} for o in objs],
        'files': files,
        'summary': {'objects': len(objs), 'files': len(files), 'files_on_disk': on_disk},
    }
    return doc


# ---------------------------------------------------------------------------
# checking
# ---------------------------------------------------------------------------

def load(path: Path) -> dict:
    with open(path, encoding='utf-8') as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or doc.get('format') != FORMAT:
        raise ProvenanceError(f"{path}: not a {FORMAT} file")
    return doc


def _release_rows(data_dir: Path, tag: str) -> dict[str, dict]:
    from .dr_store import release_dir

    db = release_dir(data_dir, tag) / 'meta' / 'campfire.db'
    if not db.exists():
        raise ProvenanceError(f"release {tag} is not on this machine; run `campfire pull --dr {tag}` first")
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {r['spectrum_id']: dict(r) for r in conn.execute(
            "SELECT spectrum_id, science_hash, content_hash, stamped_hash FROM release_manifest "
            "WHERE product_type = 'nirspec_spec' AND spectrum_id IS NOT NULL")}
    finally:
        conn.close()


def check(doc: dict, data_dir: Path, *, store=None, api=None, dr: Optional[str] = None) -> dict:
    """Re-read a provenance document: per file, whether the bytes on disk are
    still the recorded ones (``local``: ok / modified / missing / not-recorded),
    what the archive says about the recorded bytes now (``archive``: a
    sentence from :func:`campfire.identify.describe`, from the server through
    *api* or from the stores on this machine; ``resolved`` is false, and a
    problem, when nothing holds those bytes or that science any more), and,
    with *dr*, whether that
    release holds the same science (``release``); per object, whether the
    live mirror still shows the recorded version and redshift."""
    data_dir = Path(data_dir)
    products = data_dir / 'products'
    release_rows = _release_rows(data_dir, dr) if dr else None
    results = []
    problems = 0
    for e in doc.get('files') or []:
        r = {'spectrum_id': e.get('spectrum_id'), 'storage_key': e.get('storage_key')}
        recorded = e.get('sha256')
        path = products / e['local_path'] if e.get('local_path') else None
        if not recorded:
            r['local'] = 'not-recorded'
        elif path is None or not path.exists():
            r['local'] = 'missing'
        elif sha256_of(path) != recorded:
            r['local'] = 'modified'
            problems += 1
        else:
            r['local'] = 'ok'
        sha = recorded or e.get('server_hash')
        # the registry's science hash describes the registry's bytes: reuse it
        # only when the recorded bytes are those (or no local bytes were
        # recorded), never for a file that differed when it was opened
        sci = e.get('local_science_hash') or (
            e.get('science_hash') if recorded in (None, e.get('server_hash')) else None)
        r['sha256'], r['science_hash'] = sha, sci
        results.append(r)
    asked = [r for r in results if r['sha256']]
    if api is not None and asked:
        answers = api.identify_files([{'sha256': r['sha256'], 'science_hash': r['science_hash']} for r in asked])
        for r, a in zip(asked, answers):
            r['server'] = a
    for r in results:
        if not r['sha256']:
            r['archive'] = 'no hash recorded'
            problems += 1
            continue
        hits = local_matches(data_dir, r['sha256'], r['science_hash'])
        r['archive'] = describe(r.get('server'), hits)
        # resolved = something still holds those bytes or that science: the
        # server's releases / live / collected facts, or, offline, a hit in a
        # store on this machine other than a superseded download
        server = r.get('server')
        if server is not None:
            r['resolved'] = any(server.get(k) for k in ('releases', 'live', 'collected'))
        else:
            r['resolved'] = any(h.get('match') != 'stale-download' for h in hits)
        if not r['resolved']:
            problems += 1
        if release_rows is not None:
            m = release_rows.get(r['spectrum_id'] or '')
            if m is None:
                r['release'] = f"not in {dr}"
            elif r['science_hash'] and m.get('science_hash') == r['science_hash']:
                r['release'] = f"in {dr}, same science"
            elif r['sha256'] in (m.get('content_hash'), m.get('stamped_hash')):
                r['release'] = f"in {dr}, same bytes"
            else:
                r['release'] = f"in {dr}, different science (re-reduced before the freeze)"
    objects = []
    if store is not None:
        for o in doc.get('objects') or []:
            now = store.get_object(o.get('object_id') or '')
            if now is None:
                objects.append({'object_id': o.get('object_id'), 'state': 'gone from the live catalog'})
                continue
            changed = [k for k in ('version', 'redshift', 'redshift_quality') if now.get(k) != o.get(k)]
            objects.append({'object_id': o.get('object_id'),
                            'state': 'unchanged' if not changed else
                            'changed: ' + ', '.join(f"{k} {o.get(k)} → {now.get(k)}" for k in changed)})
    return {'files': results, 'objects': objects, 'problems': problems,
            'summary': {'files': len(results),
                        'ok': sum(r['local'] == 'ok' for r in results),
                        'modified': sum(r['local'] == 'modified' for r in results),
                        'missing': sum(r['local'] == 'missing' for r in results),
                        'objects_changed': sum(o['state'] != 'unchanged' for o in objects)}}
