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
        # resolved = something still holds those bytes or that science: a
        # release member or a live row on the server (a `collected` entry is
        # a tombstone of a copy that is gone, kept in the sentence only), or,
        # offline, a hit in a store on this machine other than a superseded
        # download
        server = r.get('server')
        if server is not None:
            r['resolved'] = bool(server.get('releases') or server.get('live'))
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


# ---------------------------------------------------------------------------
# pulling (``campfire pull --provenance FILE``, design §4.8)
# ---------------------------------------------------------------------------

def _served_hash(member: dict) -> Optional[str]:
    """The hash of the bytes the release presign serves for a member: the
    stamped copy when there is one, else the archived copy of the live
    bytes; None while the archive copy is not made (nothing to fetch)."""
    if member.get('byte_state') not in ('copied', 'stamped'):
        return None
    if member.get('byte_state') == 'stamped' and member.get('stamped_hash'):
        return member['stamped_hash']
    return member.get('content_hash')


def _newest_first(members: list[dict]) -> list[dict]:
    from .identify import _tag_key
    return sorted(members, key=lambda m: _tag_key(m.get('tag') or ''), reverse=True)


def plan_pull(doc: dict, *, api) -> list[dict]:
    """Where each recorded file's bytes can be fetched from now, from the
    server's facts about the recorded hash (``identify_files``). Per file,
    in order of preference: the live key while it still carries exactly those
    bytes (``source: live``, the live tree); a release whose archived copy is
    exactly those bytes (``source: dr``, that release's tree); a release
    whose copy is the same science under other bytes (a stamped copy, or a
    re-reduction that changed nothing scientific); the live key when its
    bytes are the same science. ``source`` is None, with a ``reason``, when
    nothing the caller may see holds those bytes or that science any more
    (D-P2-4: the archive keeps release members only)."""
    entries = []
    for e in doc.get('files') or []:
        recorded = e.get('sha256') or e.get('server_hash')
        sci = e.get('local_science_hash') or (
            e.get('science_hash') if e.get('sha256') in (None, e.get('server_hash')) else None)
        entries.append({'spectrum_id': e.get('spectrum_id'), 'storage_key': e.get('storage_key'),
                        'recorded': recorded, 'science_hash': sci, 'source': None, 'tag': None,
                        'expected': None, 'match': None, 'reason': None})
    asked = [p for p in entries if p['recorded']]
    answers = api.identify_files([{'sha256': p['recorded'], 'science_hash': p['science_hash']} for p in asked]) \
        if asked else []
    for p, a in zip(asked, answers):
        live = a.get('live') or []
        releases = [r for r in (a.get('releases') or []) if _served_hash(r)]
        exact_live = [l for l in live if l.get('match') == 'file' and (l.get('storage_key') or p['storage_key'])]
        exact_dr = [r for r in releases if _served_hash(r) == p['recorded']]
        sci_dr = [r for r in releases if _served_hash(r) != p['recorded']]
        sci_live = [l for l in live if l.get('match') == 'science' and l.get('file_hash') and l.get('storage_key')]
        if exact_live:
            l = exact_live[0]
            p.update(source='live', storage_key=l.get('storage_key') or p['storage_key'],
                     expected=p['recorded'], match='exact bytes')
        elif exact_dr:
            r = _newest_first(exact_dr)[0]
            p.update(source='dr', tag=r['tag'], storage_key=r.get('storage_key') or p['storage_key'],
                     expected=_served_hash(r), match='exact bytes')
        elif sci_dr:
            r = _newest_first(sci_dr)[0]
            how = 'stamped copy' if r.get('byte_state') == 'stamped' else 'other bytes'
            p.update(source='dr', tag=r['tag'], storage_key=r.get('storage_key') or p['storage_key'],
                     expected=_served_hash(r), match=f'same science ({how})')
        elif sci_live:
            l = sci_live[0]
            p.update(source='live', storage_key=l['storage_key'], expected=l['file_hash'],
                     match='same science (re-reduced, other bytes)')
        else:
            p['reason'] = describe(a)
    for p in entries:
        if not p['recorded']:
            p['reason'] = 'no hash recorded'
    return entries


def _set_aside(path: Path) -> Path:
    """Move a file that is not the bytes about to land to
    ``.superseded/<name>.<12 hex of its hash>`` beside it (design §4.8: the
    evidence survives a refresh); returns the new path."""
    aside = path.parent / '.superseded' / f"{path.name}.{sha256_of(path)[7:19]}"
    aside.parent.mkdir(exist_ok=True)
    if aside.exists():
        path.unlink()
    else:
        path.replace(aside)
    return aside


def pull(doc: dict, data_dir: Path, *, api, release_loader=None, fetch=None, dry_run: bool = False,
         log=None) -> dict:
    """Fetch the bytes a provenance document records (:func:`plan_pull`):
    live-current files into the live tree, release copies into that
    release's tree (``dr/<tag>/products/``, the release store loaded first
    through *release_loader(tag)* so the tree reads as a pulled release). A
    destination already holding the expected bytes is kept (``present``);
    one holding other bytes is set aside under ``.superseded/`` before the
    fetch. Every fetched file is verified against the expected hash and
    recorded on the store it belongs to (a present file too, so a store
    loaded after it landed learns of it). The catalog of every release the
    plan draws on is loaded on every run, present files included, so a tree
    whose load once failed is repaired by the next run. ``problems`` counts
    the files that did not resolve, were refused by the server, failed, or
    sit in a release tree whose catalog did not load."""
    from .config import products_relpath
    from .dr_store import release_dir

    data_dir = Path(data_dir)
    say = log or (lambda m: None)
    plan = plan_pull(doc, api=api)
    for p in plan:
        if p['source'] is None:
            p['status'] = 'unresolved'
            continue
        root = data_dir / 'products' if p['source'] == 'live' else release_dir(data_dir, p['tag']) / 'products'
        p['root'] = root
        p['dest'] = root / products_relpath(p['storage_key'])
        if p['dest'].exists() and sha256_of(p['dest']) == p['expected']:
            p['status'] = 'present'
        else:
            p['status'] = 'to-fetch'
    todo = [p for p in plan if p['status'] == 'to-fetch']
    catalog_failed: set[str] = set()
    if not dry_run and release_loader is not None:
        # the release catalog is loaded for every release the plan draws on,
        # present files included, so a tree whose catalog failed to load once
        # (a transient error) is repaired by the next run, and a release tree
        # is never bytes without the store that makes it a pulled release
        for tag in sorted({p['tag'] for p in plan if p['source'] == 'dr'}):
            try:
                release_loader(tag)
            except Exception as e:  # noqa: BLE001 - the bytes are still fetchable; reported as a problem below
                catalog_failed.add(tag)
                say(f"  (release {tag}: catalog not loaded: {e}; fetching its bytes anyway, run again later)")
    if not dry_run and todo:
        groups: dict[Optional[str], list[dict]] = {}
        for p in todo:
            groups.setdefault(p['tag'], []).append(p)
        fetch = fetch or _default_fetch
        for tag, items in groups.items():
            keys = sorted({p['storage_key'] for p in items})
            urls = api.presign_keys(keys, dr=tag) if tag else api.presign_keys(keys)
            for p in items:
                url = urls.get(p['storage_key'])
                if not url:
                    p['status'] = 'refused'
                    continue
                if p['dest'].exists():
                    p['set_aside'] = str(_set_aside(p['dest']))
                try:
                    result = fetch({'storage_key': p['storage_key'], 'content_hash': p['expected']}, url, p['root'])
                except Exception as e:  # noqa: BLE001 - one failure never stops the run
                    p['status'] = 'failed'
                    p['error'] = str(e)
                    continue
                p['status'] = 'fetched'
                _record(data_dir, p, result)
    if not dry_run:
        for p in plan:
            if p['source'] == 'dr' and p['tag'] in catalog_failed:
                p['catalog'] = 'not loaded'
            if p['status'] == 'present':
                # a store built after the file landed (this run's load, or a
                # later one) learns of it here
                _record(data_dir, p, _present_result(p))
    for p in plan:
        for k in ('root', 'dest'):
            if k in p:
                p[k] = str(p[k])
    counts = {s: sum(p['status'] == s for p in plan)
              for s in ('fetched', 'present', 'to-fetch', 'unresolved', 'refused', 'failed')}
    counts['catalog_not_loaded'] = sum(p.get('catalog') == 'not loaded' for p in plan)
    counts['problems'] = counts['unresolved'] + counts['refused'] + counts['failed'] + counts['catalog_not_loaded']
    return {'files': plan, 'summary': counts, 'problems': counts['problems']}


def _present_result(p: dict) -> dict:
    st = p['dest'].stat()
    return {'storage_key': p['storage_key'], 'local_path': str(p['dest'].relative_to(p['root'])),
            'local_file_hash': p['expected'], 'local_file_size': st.st_size, 'local_file_mtime': st.st_mtime}


def _default_fetch(obj: dict, url: str, root: Path) -> dict:
    from .api.session import create_download_session
    from .sync import _download_and_verify_key
    return _download_and_verify_key(obj, url, Path(root), create_download_session())


def _record(data_dir: Path, p: dict, result: dict) -> None:
    """Record the download on the store the file belongs to, when that store
    exists and knows the key (the live mirror, or the release store)."""
    from .db.store import LocalStore, SchemaMismatchError
    from .dr_store import release_dir

    db = (data_dir / 'meta' / 'campfire.db' if p['source'] == 'live'
          else release_dir(data_dir, p['tag']) / 'meta' / 'campfire.db')
    if not db.exists():
        return
    try:
        store = LocalStore(db)
    except SchemaMismatchError:
        return
    try:
        store.mark_object_synced(result['storage_key'], result['local_path'], result['local_file_hash'],
                                 result['local_file_size'], result['local_file_mtime'])
    finally:
        store.close()
