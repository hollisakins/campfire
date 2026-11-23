"""NIRCam review state (reviewer masks and exposure triage) on the config plane.

Two kinds of review decision drive ``cfpipe nircam combine`` for a field:

- **masks**: ``reference/nircam/<field>/masks/<filter>/<rootname>.reg``, the
  polygons ``apply_masks`` turns into CFMASK. In the cloud: ``nircam_exposures.mask_regions``.
- **triage status**: ``reference/nircam/<field>/exposures.json``, which lists the
  ``excluded`` exposures (dropped from combine and outlier detection) and the
  ``approved`` ones (reviewed and clean); every other exposure is pending. In the
  cloud: ``nircam_exposures.review_status``.

Both are set in the portal and by hand (DS9, editing the JSON), so they are synced,
not regenerated: ``campfire config push / pull / diff --masks`` reconcile them with
``nircam_exposures``, by the rule :mod:`campfire.deploy.nirspec_flags` applies to the
NIRSpec stuck-shutter lists.

**The sync unit is the entry**: one channel (``mask`` or ``status``) of one exposure
(filter, rootname). A field is only the scope named on the command line. Each entry
is reconciled three-way against the *base*, the value this machine and the cloud
last agreed on, kept per entry in ``$CAMPFIRE_ROOT/meta/config_sync_state.json``
under ``nircam_review`` (per backend host, as a digest for masks). "No entry" is no
mask, or the status ``pending``.

==================  ==================  =========================  ====================
local vs base       cloud vs base       pull                       push
==================  ==================  =========================  ====================
same                changed / added     takes the cloud            reports it
same                removed             asks (or ``--prune``)      reports it
changed / added     same                keeps local                sends local
removed             same                restores the entry         asks (or ``--prune``)
both, equal                             records the base           records the base
both, different                         keeps local, reports       refuses (``--force``)
                                        (``--theirs``)
==================  ==================  =========================  ====================

Rules that keep this safe (the NIRSpec lists' rules, where every entry is a
person's):

- **A removal travels only on an explicit yes.** A mask file or an exclusion missing
  locally is ambiguous (deleted on purpose, or never pulled), so neither side drops an
  entry because the other lacks it: push asks before clearing a cloud mask or
  resetting a status to pending, pull asks before deleting a local one (``--prune``
  answers yes for a script; ``--force`` / ``--theirs`` do not), and the
  non-interactive callers (``campfire deploy --field``, ``campfire pull im``) never
  remove anything. To un-exclude an exposure or drop a mask everywhere: change the
  file, then ``campfire config push --masks``, before the next pull.
- **A pull puts back what a file lost**: a cloud entry this machine once agreed on
  and the file no longer has is restored by every pull.
- **A missing file is not an empty list.** With no ``masks/<filter>/`` directory (or
  no ``exposures.json``) the base of those entries is ignored: a pull writes every
  cloud entry, a push sends nothing.
- **An unreadable entry is left alone.** A ``.reg`` the portal cannot hold (a circle,
  a sky-coordinate or exclude region) or a malformed cloud mask is reported and
  skipped, never read as "no mask"; the rest of the field syncs.
- Cloud writes go through ``nircam_review_apply``, a guarded write: a change lands
  only while the column still holds the value this client read, so a portal edit
  made in between is refused and reported, never overwritten.
- A file is rewritten only when its entries change (atomic, previous file kept as
  ``.prev``); a mask removed here is moved to ``<rootname>.reg.prev``.

Known limit: there are no tombstones. A machine that never synced an entry (no base
for it) and still has it sends it again after it was removed elsewhere.

``nircam_exposures`` is admin-only (RLS): a non-admin session reads zero rows, which
must never be mistaken for an empty cloud. ``can_sync`` is the positive check every
caller makes first.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path

from .nirspec_flags import (
    CLOUD_CHANGED, CLOUD_NEW, CLOUD_REMOVED, CONFLICT, IN_SYNC,
    LOCAL_CHANGED, LOCAL_NEW, LOCAL_REMOVED, _ask, _Report, can_sync, decide,
)

CHANNELS = ('mask', 'status')
STATE_KEY = 'nircam_review'
EXPOSURES_FILE = 'exposures.json'
EXPOSURES_VERSION = 2
LISTED = ('excluded', 'approved')      # what exposures.json lists; pending is the default
STATUSES = ('pending',) + LISTED
TABLE = 'nircam_exposures'
_APPLY_CHUNK = 200
_PAGE = 1000

__all__ = ['can_sync', 'sync_pull', 'sync_push', 'sync_diff', 'pull_on_pull',
           'push_on_deploy', 'ReviewError']


class ReviewError(Exception):
    """A file, an entry or a cloud answer that cannot be read. What it concerns is
    skipped and nothing is written over it: an unreadable side is never empty."""


# ---------------------------------------------------------------------------
# The entry model
# ---------------------------------------------------------------------------

def _coord(v) -> float:
    x = float(v)
    if not math.isfinite(x):
        raise ReviewError(f"non-finite vertex coordinate {v!r}")
    return round(x, 4) + 0.0     # 4 decimals: what a .reg file holds; -0.0 -> 0.0


def mask_entry(polygons) -> list | None:
    """``[{'vertices': [[x, y], ...], 'label': str | None}, ...]`` in DS9 image
    coordinates (1-indexed), rounded as a ``.reg`` file stores them; None for no
    polygons. Order is kept. Polygons with fewer than 3 vertices are dropped (the
    ``.reg`` writer drops them too)."""
    if not isinstance(polygons, (list, tuple)):
        raise ReviewError(f"expected a list of polygons, got {polygons!r:.80}")
    out = []
    for poly in polygons:
        if not isinstance(poly, dict):
            raise ReviewError(f"a polygon is not a table: {poly!r:.80}")
        verts = poly.get('vertices') or []
        try:
            verts = [[_coord(x), _coord(y)] for x, y in verts]
        except (TypeError, ValueError) as e:
            raise ReviewError(f"malformed vertices {poly.get('vertices')!r:.80} ({e})") from e
        if len(verts) < 3:
            continue
        out.append({'vertices': verts, 'label': poly.get('label') or None})
    return out or None


def digest(channel: str, value) -> str | None:
    """What the base records: a status as itself, a mask as a short hash."""
    if value is None:
        return None
    if channel == 'status':
        return value
    blob = json.dumps(value, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def fmt_entry(channel: str, value) -> str:
    if value is None:
        return 'no mask' if channel == 'mask' else 'pending'
    if channel == 'status':
        return value
    return f"{len(value)} polygon{'s' if len(value) != 1 else ''}"


def _key_name(key) -> str:
    return f"{key[0]}/{key[1]}"


# ---------------------------------------------------------------------------
# Local files
# ---------------------------------------------------------------------------

def campfire_root() -> Path:
    from campfire.deploy.nircam import _resolve_campfire_root
    return _resolve_campfire_root()


def ref_dir(field: str, root: Path | None = None) -> Path:
    """reference/nircam/<field>/ — equals ``Field.reference_dir``."""
    return Path(root or campfire_root()) / 'reference' / 'nircam' / field


def masks_dir(field: str, root: Path | None = None) -> Path:
    """reference/nircam/<field>/masks/ — equals ``Field.mask_dir``."""
    return ref_dir(field, root) / 'masks'


def local_fields(root: Path | None = None) -> list[str]:
    """Fields with masks or an ``exposures.json`` on disk."""
    parent = ref_dir('_', root).parent
    if not parent.is_dir():
        return []
    return sorted(d.name for d in parent.iterdir()
                  if d.is_dir() and ((d / 'masks').is_dir() or (d / EXPOSURES_FILE).exists()))


def read_reg(path) -> list | None:
    """The polygons of one ``.reg`` file as a mask entry (None: no polygons).

    Read with ``regions``, the reader ``apply_masks`` uses. Only include-polygons in
    image coordinates are representable in the portal; any other region raises."""
    from regions import PolygonPixelRegion, Regions
    try:
        regs = Regions.read(str(path), format='ds9')
    except Exception as e:
        raise ReviewError(f"cannot read it ({e})") from e
    polygons = []
    for reg in regs:
        if not isinstance(reg, PolygonPixelRegion):
            raise ReviewError(f"holds a {type(reg).__name__}; the portal stores "
                              f"image-coordinate polygons only")
        if not reg.meta.get('include', 1):
            raise ReviewError("holds an exclude (-) region; the portal stores include polygons only")
        # regions reads DS9 image coordinates 0-indexed; the portal stores them as DS9 does.
        polygons.append({'vertices': [[float(x) + 1, float(y) + 1]
                                      for x, y in zip(reg.vertices.x, reg.vertices.y)],
                         'label': reg.meta.get('text')})
    return mask_entry(polygons)


@dataclass
class Local:
    """This machine's side of one field."""
    mask: dict          # {(filter, root): entry}
    status: dict        # {(filter, root): 'excluded' | 'approved'}
    mask_filters: set   # filters with a masks/<filter>/ directory (a statement)
    has_status: bool    # exposures.json exists (a statement)
    bad: dict           # {(channel, key): why} entries that could not be read

    def states(self, channel: str, key) -> bool:
        """Whether this machine makes a statement about the entry."""
        return key[0] in self.mask_filters if channel == 'mask' else self.has_status


def read_exposures_json(path) -> dict | None:
    """``{(filter, root): status}`` from ``exposures.json`` (v1: ``excluded`` only;
    v2: also ``approved``); None when the file is absent."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
        out = {}
        for status in LISTED:
            table = doc.get(status) or {}
            if not isinstance(table, dict):
                raise ReviewError(f"'{status}' is not a {{filter: [rootnames]}} table")
            for filt, names in table.items():
                if not isinstance(names, list):
                    raise ReviewError(f"'{status}.{filt}' is not a list")
                for name in names:
                    key = (str(filt), str(name))
                    if out.get(key, status) != status:
                        raise ReviewError(f"{filt}/{name} is listed as both {out[key]} and {status}")
                    out[key] = status
        return out
    except ReviewError as e:
        raise ReviewError(f"{path}: {e}") from e
    except (ValueError, AttributeError, TypeError) as e:
        raise ReviewError(f"{path}: cannot read it ({e})") from e


def read_local(field: str, root: Path | None = None) -> Local:
    """Every local entry of ``field``. An unreadable ``.reg`` is recorded in ``bad``
    (the rest still reads); an unreadable ``exposures.json`` raises."""
    status = read_exposures_json(ref_dir(field, root) / EXPOSURES_FILE)
    mdir = masks_dir(field, root)
    masks, filters, bad = {}, set(), {}
    if mdir.is_dir():
        for fdir in sorted(mdir.iterdir()):
            if not fdir.is_dir() or fdir.name.startswith('_'):
                continue     # _imported/: the archive of the retired import-masks
            filters.add(fdir.name)
            for reg in sorted(fdir.glob('*.reg')):
                key = (fdir.name, reg.stem)
                try:
                    entry = read_reg(reg)
                except ReviewError as e:
                    bad[('mask', key)] = f"{reg}: {e}"
                    continue
                if entry is not None:
                    masks[key] = entry
    return Local(masks, status or {}, filters, status is not None, bad)


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _replace(path: Path, text: str) -> None:
    """Write atomically, keeping the previous file beside it as ``.prev``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text)
    if path.exists():
        path.with_name(path.name + '.prev').write_bytes(path.read_bytes())
    tmp.replace(path)


def write_mask(field: str, key, entry, root: Path | None = None) -> Path:
    """Write (or, for None, retire to ``.reg.prev``) one exposure's ``.reg``."""
    from .nircam_masks import _serialize_regfile
    path = masks_dir(field, root) / key[0] / f"{key[1]}.reg"
    if entry is None:
        if path.exists():
            path.replace(path.with_name(path.name + '.prev'))
        return path
    _replace(path, _serialize_regfile(
        entry, generated_at=_utcnow_iso(), written_by='campfire config pull --masks',
        note='# Synced with the portal by `campfire config push / pull --masks` (edit freely).'))
    return path


def write_exposures_json(field: str, entries: dict, root: Path | None = None) -> Path:
    """Rewrite ``exposures.json`` from ``{(filter, root): status}``. ``excluded`` is
    what ``Field._load_excluded_exposures`` reads; ``approved`` records the
    exposures reviewed and found clean."""
    doc = {'version': EXPOSURES_VERSION, 'field': field, 'generated_at': _utcnow_iso(),
           'generated_by': 'campfire config pull --masks'}
    for status in LISTED:
        table: dict[str, list] = {}
        for (filt, name), value in entries.items():
            if value == status:
                table.setdefault(filt, []).append(name)
        doc[status] = {f: sorted(v) for f, v in sorted(table.items())}
    path = ref_dir(field, root) / EXPOSURES_FILE
    _replace(path, json.dumps(doc, indent=2) + '\n')
    return path


# ---------------------------------------------------------------------------
# Cloud rows
# ---------------------------------------------------------------------------

def fetch_cloud_rows(sb, fields: list[str] | None = None) -> dict[str, list[dict]]:
    """``{field: [rows]}`` of the exposures that hold a review decision (a mask, or
    a status other than pending), every field when ``fields`` is None. Paged on the
    primary key: a field holds tens of thousands of exposures, past the row cap."""
    cols = 'id, field, filter, filename, mask_regions, review_status'
    out: dict[str, list[dict]] = {f: [] for f in (fields or [])}
    if fields is not None and not fields:
        return out
    seen = set()
    for narrow in (lambda q: q.not_.is_('mask_regions', 'null'),
                   lambda q: q.neq('review_status', 'pending')):
        last = 0
        while True:
            q = narrow(sb.table(TABLE).select(cols)).gt('id', last).order('id').limit(_PAGE)
            if fields is not None:
                q = q.in_('field', list(fields))
            rows = q.execute().data or []
            for row in rows:
                if row['id'] not in seen:
                    seen.add(row['id'])
                    out.setdefault(row['field'], []).append(row)
            if len(rows) < _PAGE:
                break
            last = rows[-1]['id']
    return out


@dataclass
class Cloud:
    """The cloud's side of one field."""
    mask: dict      # {key: {'value': entry | None, 'raw': mask_regions as stored}}
    status: dict    # {key: {'value': status | None, 'raw': review_status}}
    bad: dict       # {(channel, key): why}

    def raw(self, channel: str, key):
        """The stored value a write must expect (a row not fetched holds the default)."""
        rec = getattr(self, channel).get(key)
        if rec is not None:
            return rec['raw']
        return None if channel == 'mask' else 'pending'


def cloud_entries(rows: list[dict]) -> Cloud:
    out = Cloud({}, {}, {})
    for row in rows:
        key = (row['filter'], row['filename'])
        raw_mask = row.get('mask_regions')
        if raw_mask is not None:
            try:
                if not isinstance(raw_mask, dict):
                    raise ReviewError(f"mask_regions is not a table: {raw_mask!r:.60}")
                value = mask_entry(raw_mask.get('polygons') or [])
            except ReviewError as e:
                out.bad[('mask', key)] = f"cloud mask {_key_name(key)}: {e}"
            else:
                out.mask[key] = {'value': value, 'raw': raw_mask}
        raw_status = row.get('review_status') or 'pending'
        if raw_status not in STATUSES:
            out.bad[('status', key)] = f"cloud status {_key_name(key)}: unknown {raw_status!r}"
        elif raw_status != 'pending':
            out.status[key] = {'value': raw_status, 'raw': raw_status}
    return out


def apply_changes(sb, field: str, changes: list[dict]) -> dict:
    """Send guarded writes through ``nircam_review_apply`` (one transaction per
    call). Returns ``{'applied', 'unchanged', 'refused': [...], 'missing': [...]}``."""
    total = {'applied': 0, 'unchanged': 0, 'refused': [], 'missing': []}
    for i in range(0, len(changes), _APPLY_CHUNK):
        chunk = changes[i:i + _APPLY_CHUNK]
        res = sb.rpc('nircam_review_apply', {'p_field': field, 'p_changes': chunk}).execute().data
        # Every change is accounted for, or the answer is not trusted: recording a
        # base for a write that may not have landed is how an edit gets lost later.
        if (not isinstance(res, dict) or not isinstance(res.get('refused'), list)
                or not isinstance(res.get('missing'), list)
                or (int(res.get('applied') or 0) + int(res.get('unchanged') or 0)
                    + len(res['refused']) + len(res['missing'])) != len(chunk)):
            raise ReviewError(f"unexpected answer from nircam_review_apply ({res!r:.120}); "
                              f"run `campfire config diff --masks --field {field}` to see what landed")
        total['applied'] += int(res.get('applied') or 0)
        total['unchanged'] += int(res.get('unchanged') or 0)
        total['refused'] += res['refused']
        total['missing'] += res['missing']
    return total


# ---------------------------------------------------------------------------
# The three-way rule
# ---------------------------------------------------------------------------

@dataclass
class Item:
    """One entry's reconciliation: its values, the base digest and the verdict."""
    channel: str
    key: tuple
    local: object
    cloud: object
    base: str | None
    verdict: str

    @property
    def where(self) -> str:
        return _key_name(self.key)


def plan_channel(channel: str, local: Local, cloud: Cloud, base: dict) -> list[Item]:
    """Reconcile every readable entry of one channel of one field. Pure.

    ``base``: ``{key: digest}``. An entry about which this machine makes no
    statement (its file is absent) is compared with its base ignored."""
    mine_all = getattr(local, channel)
    theirs_all = getattr(cloud, channel)
    items = []
    for key in sorted(set(mine_all) | set(theirs_all) | set(base)):
        if (channel, key) in local.bad or (channel, key) in cloud.bad:
            continue
        stated = local.states(channel, key)
        mine = mine_all.get(key) if stated else None
        was = base.get(key) if stated else None
        rec = theirs_all.get(key)
        theirs = rec['value'] if rec else None
        if mine is None and theirs is None and was is None:
            continue
        verdict = decide(digest(channel, mine), digest(channel, theirs), was)
        items.append(Item(channel, key, mine, theirs, was, verdict))
    return items


def _missing_here(item: Item) -> bool:
    """The cloud holds an entry the file lacks, and this machine once had it."""
    return (item.local is None and item.cloud is not None
            and item.verdict in (LOCAL_REMOVED, CONFLICT))


def _gone_there(item: Item) -> bool:
    """A conflict whose cloud side is a removal: edited here, removed there."""
    return item.verdict == CONFLICT and item.cloud is None


def _mask_payload(entry, raw, filename: str) -> dict | None:
    """The ``mask_regions`` to store for a local mask entry. A polygon the cloud
    already holds (same vertices and label) keeps its stored record (id, source,
    timestamps); a new one is recorded as imported from the ``.reg`` file."""
    if entry is None:
        return None
    held: dict[str, list] = {}
    for poly in (raw or {}).get('polygons') or []:
        try:
            norm = mask_entry([poly])
        except ReviewError:
            continue
        if norm:
            held.setdefault(json.dumps(norm[0], sort_keys=True), []).append(poly)
    now = _utcnow_iso()
    polygons = []
    for poly in entry:
        same = held.get(json.dumps(poly, sort_keys=True))
        if same:
            polygons.append(same.pop(0))
            continue
        rec = {'id': str(uuid.uuid4()), 'vertices': poly['vertices'], 'source': 'imported',
               'original_frame': 'image', 'imported_from': f"{filename}.reg", 'imported_at': now}
        if poly['label']:
            rec['label'] = poly['label']
        polygons.append(rec)
    return {'version': 1, 'polygons': polygons}


def _change(item: Item, cloud: Cloud) -> dict:
    """The guarded write that makes the cloud hold this item's local value."""
    filt, name = item.key
    raw = cloud.raw(item.channel, item.key)
    if item.channel == 'mask':
        new = _mask_payload(item.local, raw, name)
    else:
        new = item.local or 'pending'
    return {'channel': item.channel, 'filter': filt, 'filename': name,
            'expect': {'value': raw}, 'set': {'value': new}}


# ---------------------------------------------------------------------------
# Base state ($CAMPFIRE_ROOT/meta/config_sync_state.json, key `nircam_review`)
# ---------------------------------------------------------------------------

def backend_state(state: dict, config: dict) -> dict:
    """The part of the sync state that belongs to the backend ``config`` talks to
    (per Supabase host, as for the NIRSpec lists). A view sharing storage with
    ``state``: pass it wherever a ``state`` is taken, then save ``state``."""
    from urllib.parse import urlparse
    url = (config.get('supabase') or {}).get('url') or ''
    backend = urlparse(url).netloc or url or 'default'
    store = state.setdefault(STATE_KEY, {}).setdefault(backend, {})
    return {STATE_KEY: store}


def _base_of(state: dict, field: str, channel: str) -> dict:
    stored = state.get(STATE_KEY, {}).get(field, {}).get(channel, {})
    out = {}
    for name, value in stored.items():
        filt, _, root = name.partition('/')
        out[(filt, root)] = value
    return out


def _set_base(state: dict, field: str, channel: str, base: dict) -> None:
    per_field = state.setdefault(STATE_KEY, {}).setdefault(field, {})
    per_field[channel] = {_key_name(k): base[k] for k in sorted(base)}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_NOUN = {'mask': ('mask', 'masks'), 'status': ('status', 'statuses')}


def _n(items, channel: str) -> str:
    one, many = _NOUN[channel]
    return f"{len(items)} {one if len(items) == 1 else many}"


def _listing(items, channel: str, fmt, limit: int = 10) -> list[str]:
    lines = [f"      {it.where}: {fmt(it)}" for it in items[:limit]]
    if len(items) > limit:
        lines.append(f"      ... and {len(items) - limit} more")
    return lines


def _both(channel):
    return lambda it: f"local {fmt_entry(channel, it.local)}, cloud {fmt_entry(channel, it.cloud)}"


def _report_bad(say, bad: dict) -> None:
    for (channel, _key), why in sorted(bad.items())[:10]:
        say(f"    ! {channel} {why} — skipped, left as it is")
    if len(bad) > 10:
        say(f"    ! ... and {len(bad) - 10} more unreadable entries skipped")


# ---------------------------------------------------------------------------
# pull / push / diff for one field
# ---------------------------------------------------------------------------

def _plan(field, rows, state, root):
    local = read_local(field, root)
    cloud = cloud_entries(rows)
    plan = {ch: plan_channel(ch, local, cloud, _base_of(state, field, ch)) for ch in CHANNELS}
    return local, cloud, plan


def _kept_base(state, field, channel, local, cloud) -> dict:
    """Base entries this run does not decide (unreadable, or not stated here) stand."""
    old = _base_of(state, field, channel)
    return {k: v for k, v in old.items()
            if (channel, k) in local.bad or (channel, k) in cloud.bad
            or not local.states(channel, k)}


def pull_field(field: str, rows: list[dict], state: dict, *, theirs: bool = False,
               prune: bool = False, interactive: bool = False, dry_run: bool = False,
               root: Path | None = None, out=print) -> dict:
    """Bring the cloud's review state of ``field`` into the local files.

    Applies cloud additions and changes to entries this machine has not touched
    since the base, puts back entries the files lost, and keeps everything else,
    reporting it. ``theirs`` takes the cloud's side of a conflict; ``prune`` deletes
    local entries the cloud removed; an interactive session is asked instead.
    Returns counts: ``applied``, ``kept``."""
    local, cloud, plan = _plan(field, rows, state, root)
    say = _Report(field, out, dry_run)
    ask = interactive and not dry_run
    counts = {'applied': 0, 'kept': 0}
    _report_bad(say, {**local.bad, **cloud.bad})
    for channel in CHANNELS:
        items = plan[channel]
        base = _kept_base(state, field, channel, local, cloud)
        if not items:
            if not dry_run:
                _set_base(state, field, channel, base)
            continue
        take = [it for it in items if it.verdict in (CLOUD_NEW, CLOUD_CHANGED)]
        lost = [it for it in items if _missing_here(it)]
        conflicts = [it for it in items if it.verdict == CONFLICT and not _missing_here(it)]
        ahead = [it for it in items if it.verdict in (LOCAL_NEW, LOCAL_CHANGED)]

        take_conflicts = take_removals = False
        if conflicts:
            say(f"    ! {_n(conflicts, channel)} changed here AND in the cloud since your last sync:")
            for line in _listing(conflicts, channel, _both(channel)):
                say(line)
            take_conflicts = theirs or _ask("    Take the cloud's version of these?", ask)
        # Deleting a local entry always needs its own yes; --theirs alone never deletes.
        removed = [it for it in items if it.verdict == CLOUD_REMOVED
                   or (take_conflicts and _gone_there(it))]
        if removed:
            say(f"    - {_n(removed, channel)} removed in the cloud:")
            for line in _listing(removed, channel, lambda it: fmt_entry(channel, it.local)):
                say(line)
            take_removals = prune or _ask("    Remove them here too?", ask)
        if lost:
            say(f"    + {_n(lost, channel)} in the cloud that your files no longer have, restored:")
            for line in _listing(lost, channel, lambda it: fmt_entry(channel, it.cloud)):
                say(line)
            say("      (to remove one everywhere: change the file, then "
                "`campfire config push --masks`)")

        changed = {}      # key -> new local value (None: remove)
        for it in items:
            v = it.verdict
            if v == IN_SYNC:
                if it.local is not None:
                    base[it.key] = digest(channel, it.local)
                continue
            if _missing_here(it):
                apply = True
            elif v == CONFLICT:
                apply = take_conflicts and (it.cloud is not None or take_removals)
            else:
                apply = v in (CLOUD_NEW, CLOUD_CHANGED) or (v == CLOUD_REMOVED and take_removals)
            if apply:
                changed[it.key] = it.cloud
                if it.cloud is not None:
                    base[it.key] = digest(channel, it.cloud)
                counts['applied'] += 1
            else:
                if it.base is not None:
                    base[it.key] = it.base     # still pending: the base stands
                counts['kept'] += 1

        parts = []
        if take:
            parts.append(f"{len(take)} from the cloud")
        if lost:
            parts.append(f"{len(lost)} restored")
        if conflicts:
            parts.append(f"{len(conflicts)} conflict(s) "
                         + ('taken from the cloud' if take_conflicts
                            else 'kept local (--theirs takes the cloud)'))
        if removed:
            parts.append(f"{len(removed)} cloud removal(s) "
                         + ('applied' if take_removals else 'not applied (--prune applies them)'))
        if ahead:
            parts.append(f"{len(ahead)} local-only or edited (`campfire config push --masks`)")
        if parts:
            say(f"    {channel}: " + '; '.join(parts))

        if dry_run:
            continue
        if channel == 'mask':
            for key, value in sorted(changed.items()):
                write_mask(field, key, value, root)
            if changed:
                say(f"    wrote {len(changed)} mask file(s) under {masks_dir(field, root)}")
        elif changed:
            new = dict(local.status)
            for key, value in changed.items():
                if value is None:
                    new.pop(key, None)
                else:
                    new[key] = value
            if new != local.status or not local.has_status:
                say(f"    wrote {write_exposures_json(field, new, root)}")
        _set_base(state, field, channel, base)
    return counts


def push_field(field: str, rows: list[dict], state: dict, sb, *, force: bool = False,
               prune: bool = False, interactive: bool = False, dry_run: bool = False,
               root: Path | None = None, out=print) -> dict:
    """Send this machine's review state of ``field`` to the cloud.

    Sends additions and edits. An entry the cloud also changed is refused unless
    ``force``. Entries the files no longer have are cleared in the cloud only on a
    yes: ``prune``, or the answer of an ``interactive`` session. Returns counts:
    ``pushed`` and ``pending``."""
    local, cloud, plan = _plan(field, rows, state, root)
    say = _Report(field, out, dry_run)
    counts = {'pushed': 0, 'pending': 0}
    _report_bad(say, {**local.bad, **cloud.bad})
    changes, sent = [], []
    bases = {}
    for channel in CHANNELS:
        items = plan[channel]
        base = bases[channel] = _kept_base(state, field, channel, local, cloud)
        if not items:
            continue
        conflicts = [it for it in items if it.verdict == CONFLICT]
        removals = [it for it in items if it.verdict == LOCAL_REMOVED
                    or (force and _missing_here(it))]
        ahead = [it for it in items if it.verdict in (CLOUD_NEW, CLOUD_CHANGED, CLOUD_REMOVED)]

        remove = False
        if removals and (prune or interactive):
            say(f"    - {_n(removals, channel)} in the cloud but no longer in your files:")
            for line in _listing(removals, channel, lambda it: fmt_entry(channel, it.cloud)):
                say(line)
            remove = prune or _ask("    Remove them from the cloud?", not dry_run)
        if conflicts and not force:
            say(f"    ! {_n(conflicts, channel)} changed here AND in the cloud since your last sync:")
            for line in _listing(conflicts, channel, _both(channel)):
                say(line)

        n_send = 0
        for it in items:
            v = it.verdict
            if v == IN_SYNC:
                if it.local is not None:
                    base[it.key] = digest(channel, it.local)
                continue
            if _missing_here(it):
                send = remove and (v == LOCAL_REMOVED or force)
            else:
                send = v in (LOCAL_NEW, LOCAL_CHANGED) or (v == CONFLICT and force)
            if send:
                changes.append(_change(it, cloud))
                sent.append(it)
                n_send += 1
            else:
                if it.base is not None:
                    base[it.key] = it.base
                counts['pending'] += 1

        verb = 'would send' if dry_run else 'sent'
        parts = []
        if n_send:
            parts.append(f"{verb} {n_send}"
                         + (f" ({len(removals)} removal(s))" if remove else '')
                         + (" (over the cloud's changes)" if conflicts and force else ''))
        if removals and not remove:
            parts.append(f"{_n(removals, channel)} in the cloud but not in your files: not removed "
                         "(`campfire config pull --masks` restores them here; "
                         "`campfire config push --masks` asks before removing, --prune removes)")
        if conflicts and not force:
            parts.append(f"{len(conflicts)} conflict(s) not sent "
                         "(`campfire config pull --masks`, or --force)")
        if ahead:
            parts.append(f"{len(ahead)} newer in the cloud (`campfire config pull --masks`)")
        if parts:
            say(f"    {channel}: " + '; '.join(parts))

    if dry_run:
        counts['pushed'] = len(sent)
        return counts
    if changes:
        result = apply_changes(sb, field, changes)
        refused = {(r['channel'], r['filter'], r['filename']) for r in result['refused']}
        missing = {(r['channel'], r['filter'], r['filename']) for r in result['missing']}
        n_missing = 0
        for it in sent:
            ident = (it.channel, *it.key)
            if ident in refused or ident in missing:
                # Not written: the base stands, so the entry is reported again.
                if it.base is not None:
                    bases[it.channel][it.key] = it.base
                else:
                    bases[it.channel].pop(it.key, None)
                counts['pending'] += 1
                if ident in refused:
                    say(f"    ! {it.channel} {it.where}: changed in the cloud while pushing, "
                        f"not sent (`campfire config pull --masks`)")
                else:
                    n_missing += 1
                continue
            if it.local is not None:
                bases[it.channel][it.key] = digest(it.channel, it.local)
            else:
                bases[it.channel].pop(it.key, None)
            counts['pushed'] += 1
        if n_missing:
            say(f"    ! {n_missing} entr{'y' if n_missing == 1 else 'ies'} for exposures the "
                f"cloud does not have (deploy the field first): not sent")
    for channel in CHANNELS:
        _set_base(state, field, channel, bases[channel])
    return counts


_DIFF_LABEL = {
    CLOUD_NEW: '< cloud-only', CLOUD_CHANGED: '< cloud-modified', CLOUD_REMOVED: '? cloud-removed',
    LOCAL_NEW: '> local-only', LOCAL_CHANGED: '> local-modified', LOCAL_REMOVED: '? not in file',
    CONFLICT: '! diverged', IN_SYNC: '=',
}


def diff_field(field: str, rows: list[dict], state: dict, *, show_all: bool = False,
               root: Path | None = None) -> list[str]:
    """Report lines for the entries of ``field`` that differ (read-only)."""
    local, cloud, plan = _plan(field, rows, state, root)
    lines = [f"  ! {field} {channel} {why}" for (channel, _k), why in sorted({**local.bad, **cloud.bad}.items())]
    for channel in CHANNELS:
        for it in plan[channel]:
            if it.verdict == IN_SYNC and not show_all:
                continue
            label = _DIFF_LABEL[it.verdict]
            f = lambda v: fmt_entry(channel, v)  # noqa: E731
            detail = {
                CLOUD_NEW: f(it.cloud),
                CLOUD_CHANGED: f"{f(it.local)} -> {f(it.cloud)}",
                CLOUD_REMOVED: f"{f(it.local)} (pull asks before removing it here)",
                LOCAL_NEW: f(it.local),
                LOCAL_CHANGED: f"{f(it.cloud)} -> {f(it.local)}",
                LOCAL_REMOVED: f(it.cloud),
                CONFLICT: f"local {f(it.local)}, cloud {f(it.cloud)}",
                IN_SYNC: f(it.local),
            }[it.verdict]
            if _missing_here(it):
                label = _DIFF_LABEL[LOCAL_REMOVED]
                detail = f"{f(it.cloud)} (pull restores it; push asks before removing it)"
            lines.append(f"  {label:<18} {field} {channel} {it.where}: {detail}")
    return lines


# ---------------------------------------------------------------------------
# Drivers over a scope of fields
# ---------------------------------------------------------------------------

def _scope(fields, cloud, state, root, *, local: bool, remote: bool) -> list[str]:
    if fields is not None:
        return list(fields)
    names = set()
    if local:
        names |= set(local_fields(root))
    if remote:
        names |= {f for f, rows in cloud.items() if rows}
        # ...and fields this machine synced before: an entry removed in the cloud
        # since then is only visible against the base.
        names |= {f for f, chans in state.get(STATE_KEY, {}).items()
                  if any(chans.get(ch) for ch in CHANNELS)}
    return sorted(names)


def sync_pull(sb, fields: list[str] | None, state: dict, **kw) -> dict:
    """Pull every field in scope (None: every field with cloud entries)."""
    out = kw.get('out', print)
    cloud = fetch_cloud_rows(sb, fields)
    totals = {'applied': 0, 'kept': 0, 'fields': 0, 'errors': 0}
    for field in _scope(fields, cloud, state, kw.get('root'), local=False, remote=True):
        try:
            c = pull_field(field, cloud.get(field, []), state, **kw)
        except ReviewError as e:
            out(f"  ! {field}: {e} — skipped, nothing written")
            totals['errors'] += 1
            continue
        totals['fields'] += 1
        totals['applied'] += c['applied']
        totals['kept'] += c['kept']
    return totals


def sync_push(sb, fields: list[str] | None, state: dict, **kw) -> dict:
    """Push every field in scope (None: every field with local review files)."""
    out = kw.get('out', print)
    names = fields if fields is not None else local_fields(kw.get('root'))
    cloud = fetch_cloud_rows(sb, list(names)) if names else {}
    totals = {'pushed': 0, 'pending': 0, 'fields': 0, 'errors': 0}
    for field in names:
        try:
            c = push_field(field, cloud.get(field, []), state, sb, **kw)
        except ReviewError as e:
            out(f"  ! {field}: {e} — skipped, nothing sent")
            totals['errors'] += 1
            continue
        totals['fields'] += 1
        totals['pushed'] += c['pushed']
        totals['pending'] += c['pending']
    return totals


def sync_diff(sb, fields: list[str] | None, state: dict, *, show_all: bool = False,
              root: Path | None = None) -> list[str]:
    cloud = fetch_cloud_rows(sb, fields)
    lines = []
    for field in _scope(fields, cloud, state, root, local=True, remote=True):
        try:
            lines += diff_field(field, cloud.get(field, []), state, show_all=show_all, root=root)
        except ReviewError as e:
            lines.append(f"  ! {field}: {e}")
    return lines


# ---------------------------------------------------------------------------
# The non-interactive callers: `campfire deploy --field` and `campfire pull im`
# ---------------------------------------------------------------------------

def push_on_deploy(sb, field: str, config: dict) -> dict:
    """Send the review state a field deploy just used. Additions and edits only: a
    deploy never removes a cloud entry and never overrides one the cloud changed
    (both reported for an explicit ``campfire config push / pull --masks``)."""
    from . import config_sync as cs
    state = cs.load_state()
    lines: list[str] = []
    counts = push_field(field, fetch_cloud_rows(sb, [field]).get(field, []),
                        backend_state(state, config), sb, out=lines.append)
    cs.save_state(state)
    counts['lines'] = lines if counts['pending'] else []
    return counts


def pull_on_pull(sb, field: str, config: dict, *, dry_run: bool = False, out=print) -> dict:
    """The review part of ``campfire pull im --field``: cloud additions and changes to
    entries this machine has not touched, and entries the files lost. Never removes
    and never resolves a conflict (``campfire config pull --masks`` does)."""
    from . import config_sync as cs
    state = cs.load_state()
    counts = pull_field(field, fetch_cloud_rows(sb, [field]).get(field, []),
                        backend_state(state, config), dry_run=dry_run, out=out)
    if not dry_run:
        cs.save_state(state)
    return counts
