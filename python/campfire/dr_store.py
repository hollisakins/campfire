"""The local store of a data release (P5-1, docs/design-data-releases.md §4.7).

A release the reader pulled lives under ``$CAMPFIRE_ROOT/dr/<tag>/``::

    release.json                      the files route's answer + loaded_at
    exports/<stream>/<scope>.jsonl.gz the export files, kept as fetched
    meta/campfire.db                  a LocalStore over the same DDL as the
                                      live mirror, built from the kept files
    products/<relpath>                the bytes (``campfire pull --dr``)

:func:`load_release` fetches the files the caller may read from
``GET /api/v1/dr/<tag>/files`` (docs/dr-format-v1.md), verifies each against
the sha256 / size / row count the route gave, keeps it, and loads the set into
the store. The store is derived from the kept files and nothing else: it is
rebuilt from them whenever the file set changes, the client's schema moved
(``SchemaMismatchError``) or the loader learned to derive more from the files
(``LOADER_VERSION``), and the live mirror's purge, tombstone and
schema-mismatch-delete logic never touch it. The one piece of state that is
not derived, which products are on disk (``storage_objects.local_*``), is
carried across a rebuild.

Merging (dr-format-v1.md): a stream's files are one table. An object that
belongs to several of the reader's programs appears in each program's
``objects`` file with the aggregates scoped to that program; the loader keeps
one row per id, unions ``programs`` / ``evidence_programs`` /
``reviewed_spectrum_ids``, takes the non-null ``z_source_spectrum_id``, and
recomputes ``n_targets`` / ``n_spectra`` / ``gratings`` / ``observations`` /
``max_snr`` / ``max_exposure_time`` / ``member_target_ids`` from the spectra
rows it holds (every published spectrum of the reader's programs is in the
``spectra`` files, so the recomputed aggregates describe exactly what the
reader holds). ``photometry`` rows are deduplicated by id and keyed by the
object's name, as the live stream keys them. ``manifest`` rows become the
store's ``storage_objects`` (what ``get_spectrum`` / ``find_local_path`` join
on; ``content_hash`` is the hash of the bytes a reader of the release gets,
the stamped archive copy when there is one) and are kept whole in the
release-only ``release_manifest`` table (archive key, byte state, stamped
hash), which the byte pull reads.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .exceptions import DRLoadError

DR_DIRNAME = 'dr'
FORMAT_VERSIONS = (1,)
# Bump when the loader derives something new from the kept files (a store
# built by an older loader is rebuilt from them, like on a SCHEMA_VERSION
# bump): 2 = storage_objects.filter / observation / field off the key.
LOADER_VERSION = 2
STREAMS = ('objects', 'spectra', 'line_fits', 'photometry', 'manifest')
_KEY_SAFE = re.compile(r'^[A-Za-z0-9._-]+$')
_TAG_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$')

# release_manifest: the manifest rows as exported, beside the storage_objects
# mirror the live-shaped code reads. Created in the release store only.
_RELEASE_MANIFEST_SQL = """
CREATE TABLE IF NOT EXISTS release_manifest (
    storage_key TEXT PRIMARY KEY,
    component TEXT,
    program_slug TEXT,
    field TEXT,
    product_type TEXT,
    spectrum_id TEXT,
    backend TEXT,
    bucket TEXT,
    content_hash TEXT,
    science_hash TEXT,
    size_bytes INTEGER,
    archive_key TEXT,
    byte_state TEXT,
    stamped_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_rm_spectrum_id ON release_manifest(spectrum_id);
CREATE INDEX IF NOT EXISTS idx_rm_product_type ON release_manifest(product_type);
"""


def release_dir(data_dir: Path, tag: str) -> Path:
    """``<data_dir>/dr/<tag>`` (the tag is validated: it names a directory)."""
    if not _TAG_RE.match(tag or ''):
        raise DRLoadError(f"invalid release tag {tag!r}")
    return Path(data_dir) / DR_DIRNAME / tag


def export_filename(scope: str) -> str:
    """The local file name of a scope, as the server keys it
    (``dr_export_key``): key-safe names as they are, any other name sanitised
    to the key alphabet and suffixed with the first 8 hex of its md5, so two
    names that sanitise alike stay distinct."""
    if _KEY_SAFE.match(scope) and scope not in ('.', '..'):
        safe = scope
    else:
        safe = re.sub(r'[^A-Za-z0-9._-]', '_', scope) + '-' + hashlib.md5(scope.encode('utf-8')).hexdigest()[:8]
    return safe + '.jsonl.gz'


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8 << 20), b''):
            h.update(chunk)
    return 'sha256:' + h.hexdigest()


def _default_fetch(url: str, dest: Path) -> None:
    """Stream a presigned url to *dest* (a plain GET: the url carries its auth)."""
    import requests
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def read_rows(path: Path) -> Iterable[dict]:
    """The rows of one export file, in file order."""
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


@dataclass
class ReleaseLoadReport:
    tag: str
    fetched: list = field(default_factory=list)   # (stream, scope)
    kept: list = field(default_factory=list)      # already on disk and verified
    rebuilt: bool = False
    counts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {'tag': self.tag, 'fetched': self.fetched, 'kept': self.kept,
                'rebuilt': self.rebuilt, 'counts': self.counts}


def read_release_json(root: Path) -> Optional[dict]:
    p = root / 'release.json'
    if not p.exists():
        return None
    with open(p, encoding='utf-8') as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# fetching the files
# ---------------------------------------------------------------------------

def _file_entry(root: Path, f: dict) -> dict:
    stream, scope = f.get('stream'), f.get('scope')
    if stream not in STREAMS:
        raise DRLoadError(f"unknown export stream {stream!r} (expected one of {', '.join(STREAMS)})")
    if not isinstance(scope, str) or not scope:
        raise DRLoadError(f"file of stream {stream} has no scope")
    rel = Path('exports') / stream / export_filename(scope)
    dest = (root / rel).resolve()
    if not str(dest).startswith(str((root / 'exports').resolve()) + os.sep):
        raise DRLoadError(f"refusing to write outside the release tree for scope {scope!r}")
    return {'stream': stream, 'scope': scope, 'sha256': f.get('sha256'), 'size': f.get('size'),
            'rows': f.get('rows'), 'path': str(rel), 'url': f.get('url')}


def _verify(path: Path, entry: dict) -> None:
    size = os.path.getsize(path)
    if entry.get('size') is not None and size != int(entry['size']):
        raise DRLoadError(f"{entry['stream']}/{entry['scope']}: size {size} != {entry['size']} the files route gave")
    sha = _sha256_of(path)
    if entry.get('sha256') and sha != entry['sha256']:
        raise DRLoadError(f"{entry['stream']}/{entry['scope']}: sha256 {sha} != {entry['sha256']} the files route gave")


def _fetch_files(root: Path, entries: list[dict], fetch: Callable[[str, Path], None],
                 report: ReleaseLoadReport, log) -> None:
    for e in entries:
        dest = root / e['path']
        if dest.exists():
            try:
                _verify(dest, e)
            except DRLoadError:
                dest.unlink()
            else:
                report.kept.append((e['stream'], e['scope']))
                continue
        if not e.get('url'):
            raise DRLoadError(f"{e['stream']}/{e['scope']}: no url to fetch it from")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + '.part')
        try:
            fetch(e['url'], tmp)
            _verify(tmp, e)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, dest)
        report.fetched.append((e['stream'], e['scope']))
        if log:
            log(f"  fetched {e['stream']}/{e['scope']} ({e.get('rows')} rows)")


# ---------------------------------------------------------------------------
# merging the rows into the store
# ---------------------------------------------------------------------------

def _union(*lists) -> list:
    seen, out = set(), []
    for lst in lists:
        for v in lst or []:
            if v not in seen:
                seen.add(v)
                out.append(v)
    return out


def _merge_object(acc: Optional[dict], row: dict) -> dict:
    if acc is None:
        acc = dict(row)
        acc['programs'] = list(row.get('programs') or [])
        acc['evidence_programs'] = list(row.get('evidence_programs') or [])
        acc['reviewed_spectrum_ids'] = (list(row['reviewed_spectrum_ids'])
                                        if row.get('reviewed_spectrum_ids') is not None else None)
        return acc
    acc['programs'] = _union(acc['programs'], row.get('programs'))
    acc['evidence_programs'] = _union(acc['evidence_programs'], row.get('evidence_programs'))
    if row.get('reviewed_spectrum_ids') is not None:
        acc['reviewed_spectrum_ids'] = _union(acc.get('reviewed_spectrum_ids'), row['reviewed_spectrum_ids'])
    if acc.get('z_source_spectrum_id') is None and row.get('z_source_spectrum_id') is not None:
        acc['z_source_spectrum_id'] = row['z_source_spectrum_id']
    # the per-file aggregates are replaced by the recomputation below; keep
    # the larger as the fallback for an object with no spectra row loaded
    for k in ('n_targets', 'n_spectra', 'max_snr', 'max_exposure_time'):
        a, b = acc.get(k), row.get(k)
        if a is None or (b is not None and b > a):
            acc[k] = b
    for k in ('gratings', 'observations'):
        acc[k] = _union(acc.get(k), row.get(k))
    return acc


def _aggregate_from_spectra(obj: dict, spectra: list[dict]) -> None:
    if not spectra:
        return
    obj['n_spectra'] = len(spectra)
    targets = _union([s.get('target_id') for s in spectra if s.get('target_id')])
    obj['n_targets'] = len(targets)
    obj['member_target_ids'] = sorted(targets)
    obj['gratings'] = sorted(_union([s.get('grating') for s in spectra if s.get('grating')]))
    obj['observations'] = sorted(_union([s.get('observation') for s in spectra if s.get('observation')]))
    snr = [s['signal_to_noise'] for s in spectra if s.get('signal_to_noise') is not None]
    exp = [s['exposure_time'] for s in spectra if s.get('exposure_time') is not None]
    obj['max_snr'] = max(snr) if snr else obj.get('max_snr')
    obj['max_exposure_time'] = max(exp) if exp else obj.get('max_exposure_time')


def _key_scope(storage_key: str) -> dict:
    """What the layout reads off a key: the NIRCam filter (so ``pull --dr
    --filters`` narrows mosaics as a live pull does; the manifest carries no
    filter column), plus observation / field as a fallback. Empty for a key
    the layout does not know."""
    try:
        from campfire_layout import parse_key
        pk = parse_key(storage_key)
    except Exception:  # noqa: BLE001 - not a layout key: nothing to read
        return {}
    sc = pk.scope
    return {'filter': getattr(sc, 'filt', None), 'observation': getattr(sc, 'obs', None),
            'field': getattr(sc, 'field', None)}


def _instrument_of(product_type: Optional[str]) -> Optional[str]:
    if not product_type:
        return None
    if product_type.startswith('nirspec') or product_type.startswith('spectrum'):
        return 'nirspec'
    if product_type.startswith('nircam'):
        return 'nircam'
    return None


def _load_into_store(store, root: Path, entries: list[dict], counts: dict) -> None:
    """Read every kept file and write the merged tables (one transaction per table)."""
    by_stream: dict[str, list[dict]] = {s: [] for s in STREAMS}
    for e in entries:
        by_stream[e['stream']].append(e)

    # spectra first: the objects' aggregates and the manifest's observation come from them
    spectra: dict[str, dict] = {}
    for e in by_stream['spectra']:
        for r in read_rows(root / e['path']):
            spectra[r['spectrum_id']] = r
    spectra_rows = list(spectra.values())
    counts['spectra'] = store.upsert_spectra(spectra_rows)
    by_object_db_id: dict[int, list[dict]] = {}
    for r in spectra_rows:
        if r.get('object_db_id') is not None:
            by_object_db_id.setdefault(r['object_db_id'], []).append(r)
    obs_of_spectrum = {k: v.get('observation') for k, v in spectra.items()}
    field_of_spectrum = {k: v.get('field') for k, v in spectra.items()}

    objects: dict[int, dict] = {}
    for e in by_stream['objects']:
        for r in read_rows(root / e['path']):
            objects[r['id']] = _merge_object(objects.get(r['id']), r)
    for oid, obj in objects.items():
        _aggregate_from_spectra(obj, by_object_db_id.get(oid, []))
        if 'member_target_ids' not in obj:
            obj['member_target_ids'] = []
    counts['objects'] = store.upsert_objects(list(objects.values()))
    name_of_object = {oid: o.get('object_id') for oid, o in objects.items()}

    fits: dict = {}
    for e in by_stream['line_fits']:
        for r in read_rows(root / e['path']):
            r = dict(r)
            r.setdefault('field', field_of_spectrum.get(r.get('spectrum_name')))
            fits[r['spectrum_id']] = r
    counts['line_fits'] = store.upsert_line_fits(list(fits.values()))

    phot: dict = {}
    for e in by_stream['photometry']:
        for r in read_rows(root / e['path']):
            r = dict(r)
            # the export keys the row by objects.id; the store (as the live
            # stream) by the object's name
            oid = r.get('object_id')
            r['object_id'] = name_of_object.get(oid, oid if isinstance(oid, str) else None)
            r.pop('programs', None)
            phot[r['id']] = r
    counts['photometry'] = store.upsert_photometry(list(phot.values()))

    store._conn.executescript(_RELEASE_MANIFEST_SQL)
    store._conn.execute("DELETE FROM release_manifest")
    manifest: dict[str, dict] = {}
    for e in by_stream['manifest']:
        for r in read_rows(root / e['path']):
            manifest[r['storage_key']] = r
    storage_rows = []
    for key, m in manifest.items():
        store._conn.execute(
            """INSERT OR REPLACE INTO release_manifest
               (storage_key, component, program_slug, field, product_type, spectrum_id, backend, bucket,
                content_hash, science_hash, size_bytes, archive_key, byte_state, stamped_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, m.get('component'), m.get('program_slug'), m.get('field'), m.get('product_type'),
             m.get('spectrum_id'), m.get('backend'), m.get('bucket'), m.get('content_hash'),
             m.get('science_hash'), m.get('size_bytes'), m.get('archive_key'), m.get('byte_state'),
             m.get('stamped_hash')))
        stamped = m.get('byte_state') == 'stamped' and m.get('stamped_hash')
        ks = _key_scope(key)
        storage_rows.append({
            'storage_key': key, 'backend': m.get('backend'), 'bucket': m.get('bucket'),
            'content_hash': m.get('stamped_hash') if stamped else m.get('content_hash'),
            'size_bytes': m.get('size_bytes'), 'product_type': m.get('product_type'),
            'instrument': _instrument_of(m.get('product_type')), 'status': 'active',
            'observation': obs_of_spectrum.get(m.get('spectrum_id')) or ks.get('observation'),
            'field': m.get('field') or field_of_spectrum.get(m.get('spectrum_id')) or ks.get('field'),
            'filter': ks.get('filter'),
            'spectrum_id': m.get('spectrum_id'),
        })
    counts['manifest'] = store.upsert_storage_objects(storage_rows)
    store.commit()


def _carry_local_state(db_path: Path) -> list[dict]:
    """The ``storage_objects.local_*`` rows of an existing store (what is on
    disk), read without the schema check so a store of an older client still
    yields them."""
    import sqlite3
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT storage_key, local_path, local_file_hash, local_file_size, local_file_mtime "
            "FROM storage_objects WHERE local_path IS NOT NULL").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _file_set_signature(entries: list[dict]) -> str:
    parts = sorted(f"{e['stream']}/{e['scope']}/{e.get('sha256')}" for e in entries)
    return hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()


def open_store(root: Path):
    """The release's LocalStore (the caller checks the tree exists)."""
    from .db.store import LocalStore
    return LocalStore(root / 'meta' / 'campfire.db')


def load_release(session, tag: str, *, data_dir: Optional[Path] = None,
                 fetch: Optional[Callable[[str, Path], None]] = None,
                 log: Optional[Callable[[str], None]] = None) -> ReleaseLoadReport:
    """Fetch (or verify) the export files of *tag* this caller may read and
    build the release store from them. Idempotent: a file already on disk
    that verifies is kept, and the store is rebuilt only when the file set or
    the client's schema changed. Returns what was done."""
    from .config import resolve_data_dir
    from .db.store import SCHEMA_VERSION, LocalStore, SchemaMismatchError
    from .dr import fetch_files

    data_dir = Path(data_dir) if data_dir else resolve_data_dir()
    root = release_dir(data_dir, tag)
    info = fetch_files(session, tag)
    fmt = info.get('format_version')
    if fmt not in FORMAT_VERSIONS:
        raise DRLoadError(f"release {tag} is export format version {fmt!r}; this client reads "
                          f"{', '.join(str(v) for v in FORMAT_VERSIONS)} (update campfire)")
    entries = [_file_entry(root, f) for f in info.get('files') or []]
    if not entries:
        raise DRLoadError(f"release {tag} has no export file you may read"
                          + ('' if info.get('complete', True) else ' (its export is not complete yet)'))

    report = ReleaseLoadReport(tag=tag)
    root.mkdir(parents=True, exist_ok=True)
    (root / 'products').mkdir(exist_ok=True)
    _fetch_files(root, entries, fetch or _default_fetch, report, log)

    signature = _file_set_signature(entries)
    db_path = root / 'meta' / 'campfire.db'
    store = None
    if db_path.exists():
        try:
            store = LocalStore(db_path)
        except SchemaMismatchError:
            store = None
        else:
            if (store.get_meta('release_files') != signature or store.get_meta('release_tag') != tag
                    or store.get_meta('release_loader') != str(LOADER_VERSION)):
                store.close()
                store = None
    if store is None:
        local_state = _carry_local_state(db_path)
        # the store is derived from the kept files: build it fresh beside the
        # old one and swap, so a failed load leaves the previous store usable
        (root / 'meta').mkdir(exist_ok=True)
        tmpdir = Path(tempfile.mkdtemp(prefix='campfire-db-', dir=root / 'meta'))
        try:
            fresh = LocalStore(tmpdir / 'campfire.db')
            counts: dict = {}
            _load_into_store(fresh, root, entries, counts)
            for r in local_state:
                fresh.mark_object_synced(r['storage_key'], r['local_path'], r.get('local_file_hash'),
                                         r.get('local_file_size'), r.get('local_file_mtime'))
            fresh.set_meta('release_tag', tag)
            fresh.set_meta('release_files', signature)
            fresh.set_meta('release_loader', str(LOADER_VERSION))
            fresh.set_meta('release_loaded_at', datetime.now(timezone.utc).isoformat())
            fresh.close()
            for suffix in ('', '-wal', '-shm'):
                (db_path.parent / (db_path.name + suffix)).unlink(missing_ok=True)
            os.replace(tmpdir / 'campfire.db', db_path)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        report.rebuilt = True
        report.counts = counts
    else:
        store.close()
        report.counts = dict((read_release_json(root) or {}).get('counts_loaded') or {})

    meta = {k: info.get(k) for k in ('tag', 'state', 'format_version', 'components', 'counts', 'frozen_at',
                                     'published_at', 'doi', 'notes', 'known_issues', 'complete')}
    meta['loaded_at'] = datetime.now(timezone.utc).isoformat()
    meta['schema_version'] = SCHEMA_VERSION
    meta['counts_loaded'] = report.counts
    meta['files'] = [{k: e[k] for k in ('stream', 'scope', 'sha256', 'size', 'rows', 'path')} for e in entries]
    tmp = root / 'release.json.part'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, sort_keys=True)
    os.replace(tmp, root / 'release.json')
    return report
