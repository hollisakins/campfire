"""
The stuck-closed shutter list: physical MSA shutters per exposure root.

``reference/nirspec/<obs>/stuck_closed_shutters.toml`` holds, per root (obs / visit /
config, e.g. ``jw07076001001_05101``), a ``[<root>.shutters]`` table keyed by the
physical shutter, ``"q{quadrant}r{row}c{column}"`` with the metafile's
``shutter_quadrant`` / ``shutter_row`` (along the dispersion) / ``shutter_column``
(along the slitlet)::

    [jw07076001001_05101.shutters]
    "q2r195c64" = {origin = "manual", stuck = true}
    "q2r349c66" = {origin = "auto", stuck = true, a = 0.061}
    "q2r300c149" = {origin = "manual", stuck = false, note = "NRS1 blue stub"}

``origin`` is ``manual`` / ``web`` / ``auto``, in that order of precedence; a ``manual``
or ``web`` entry with ``stuck = false`` vetoes an ``auto`` call. ``a`` (the fitted sky
amplitude) and ``note`` are optional. Stuck shutters are transient: the MSA is
reconfigured between roots, so an entry never carries over to another root.

Legacy entries, ``<source_id> = [ordinals]  # hand|web|auto`` directly under
``[<root>]`` (the 1-indexed positions among the source's slitlet columns), are still
read: each ordinal is mapped through the root's metafile onto its physical shutters,
the trailing tag giving the origin (``hand`` or no tag -> ``manual``). At equal origin
a physical entry beats a legacy one.
"""

import math
import os
import re
from dataclasses import dataclass

import numpy as np

from campfire_pipeline.common.io import log

ORIGINS = ('manual', 'web', 'auto')
KEY_RE = re.compile(r'^q([1-4])r(\d+)c(\d+)$')
SHUTTERS = 'shutters'


@dataclass(frozen=True)
class Entry:
    origin: str
    stuck: bool = True
    a: float | None = None     # the fit's sky amplitude, when an auto call wrote one
    note: str = ''


def shutter_key(q, r, c):
    return f'q{int(q)}r{int(r)}c{int(c)}'


def parse_key(key):
    """``"q2r195c64"`` -> ``(2, 195, 64)``; ``ValueError`` otherwise."""
    m = KEY_RE.match(str(key).strip())
    if not m:
        raise ValueError(f'not a shutter key: {key!r} (expected "q<quadrant>r<row>c<column>")')
    return tuple(int(g) for g in m.groups())


def root_of(rate_base):
    """``jw07076001001_05101_00001_nrs1`` -> ``jw07076001001_05101``."""
    return '_'.join(os.path.basename(rate_base).split('_')[:2])


def _entry(value, where):
    if not isinstance(value, dict):
        raise ValueError(f'{where}: expected an inline table like {{origin = "manual", stuck = true}}, got {value!r}')
    origin = str(value.get('origin', '')).strip().lower()
    if origin not in ORIGINS:
        raise ValueError(f'{where}: origin must be one of {ORIGINS}, got {value.get("origin")!r}')
    stuck = value.get('stuck', True)
    if not isinstance(stuck, bool):
        raise ValueError(f'{where}: stuck must be true or false, got {stuck!r}')
    a = value.get('a')
    return Entry(origin=origin, stuck=stuck, a=float(a) if a is not None else None, note=str(value.get('note', '')))


def _legacy_origin(tag):
    return 'manual' if tag == 'hand' else tag


def read_file(path):
    """Parse the TOML into ``(physical, legacy)``:
    ``physical = {root: {(q, r, c): Entry}}`` and
    ``legacy = {root: {source_id: ([ordinals], origin)}}``. A missing file is empty."""
    import toml
    from campfire_pipeline.nirspec.stuck_shutters import load_stuck_shutters_tagged
    physical, legacy = {}, {}
    if not os.path.exists(path):
        return physical, legacy
    data = toml.load(path)
    tags = load_stuck_shutters_tagged(path)
    for root, table in data.items():
        if not isinstance(table, dict):
            raise ValueError(f'{path}: [{root}] is not a table')
        for key, value in table.items():
            if key == SHUTTERS:
                for skey, sval in value.items():
                    physical.setdefault(root, {})[parse_key(skey)] = _entry(sval, f'{path} [{root}.shutters] {skey}')
                continue
            try:
                sid = int(key)
            except ValueError:
                raise ValueError(f'{path}: [{root}] {key!r} is neither a source id nor the shutters table')
            tag = tags.get((root, str(key)), (None, 'hand'))[1]
            legacy.setdefault(root, {})[sid] = ([int(k) for k in value], _legacy_origin(tag))
    return physical, legacy


def source_columns(rows):
    """The sorted shutter columns of a source's edited rows (``MetaFile.rows_for_source``):
    the frame the legacy ordinals count in."""
    return np.sort(np.unique(np.asarray(rows['shutter_column'], int)))


def legacy_to_physical(metafile, source_id, ordinals):
    """The physical shutters a legacy ``source_id = [ordinals]`` entry names: every
    (quadrant, row, column) of the source's slitlet rows at the ordinal's column, the
    columns counted exactly as stage 2a always removed them."""
    rows = metafile.rows_for_source(source_id)
    cols = source_columns(rows)
    out = set()
    for k in ordinals:
        k = int(k)
        if not 1 <= k <= len(cols):
            raise IndexError(f'stuck shutter ordinal {k} is out of range for a {len(cols)}-shutter slitlet')
        sel = np.asarray(rows['shutter_column'], int) == cols[k - 1]
        for q, r, c in zip(rows['shutter_quadrant'][sel], rows['shutter_row'][sel], rows['shutter_column'][sel]):
            out.add((int(q), int(r), int(c)))
    return out


def resolve(physical, legacy, metafiles):
    """One entry per physical shutter and root: origin precedence manual > web > auto,
    a physical entry before a legacy one at equal origin. ``metafiles`` maps a root to
    its ``MetaFile``; a legacy entry of a root without one, or of a source the metafile
    does not hold, is reported and left out."""
    rank = {o: i for i, o in enumerate(ORIGINS)}
    best = {}
    for root, entries in physical.items():
        for key, e in entries.items():
            best[(root, key)] = ((rank[e.origin], 0), e)
    for root, entries in legacy.items():
        mf = metafiles.get(root)
        if mf is None:
            log(f'stuck shutters: [{root}] has legacy entries but no metafile to map them; ignored')
            continue
        for sid, (ordinals, origin) in entries.items():
            try:
                keys = legacy_to_physical(mf, sid, ordinals)
            except (RuntimeError, IndexError) as e:
                log(f'stuck shutters: [{root}] {sid} = {ordinals} could not be mapped ({e}); ignored')
                continue
            for key in keys:
                cand = ((rank[origin], 1), Entry(origin=origin, stuck=True, note=f'legacy {sid} = {ordinals}'))
                if (root, key) not in best or cand[0] < best[(root, key)][0]:
                    best[(root, key)] = cand
    out = {}
    for (root, key), (_, e) in best.items():
        out.setdefault(root, {})[key] = e
    return out


def root_metafiles(obs, roots=None):
    """``{root: MetaFile}`` from the first rate file of each root (its original
    metafile, ``OGMETFL`` when stage 2a has repointed ``MSAMETFL``)."""
    from astropy.io import fits
    from campfire_pipeline.nirspec.metafile import MetaFile
    out = {}
    for rate in sorted(obs.rate_files):
        root = root_of(os.path.basename(rate)[:-len('_rate.fits')])
        if root in out or (roots is not None and root not in roots):
            continue
        try:
            hdr = fits.getheader(rate)
            name = hdr.get('OGMETFL') or hdr.get('MSAMETFL')
            if not name or str(hdr.get('EXP_TYPE', '')).upper() == 'NRS_FIXEDSLIT':
                continue
            path = name if os.path.isabs(name) else os.path.join(os.path.dirname(rate), name)
            if not os.path.exists(path):
                path = os.path.join(obs.workspace_dir, os.path.basename(name))
            with fits.open(path) as h:
                from copy import deepcopy
                out[root] = MetaFile(deepcopy(h), os.path.basename(path), int(hdr['MSAMETID']))
        except Exception as e:
            log(f'stuck shutters: no metafile for {root} ({type(e).__name__}: {e})')
    return out


def load(path, metafiles):
    """The resolved list ``{root: {(q, r, c): Entry}}`` of the TOML at ``path``."""
    physical, legacy = read_file(path)
    return resolve(physical, legacy, metafiles)


def load_root(path, root, metafile):
    """``load`` restricted to one root, with that root's ``MetaFile`` (what stage 2a
    has at hand for a rate file)."""
    physical, legacy = read_file(path)
    return resolve({root: physical.get(root, {})}, {root: legacy.get(root, {})}, {root: metafile})


def load_obs(obs):
    """``load`` for an observation, with the metafiles of the roots that need one."""
    physical, legacy = read_file(obs.stuck_closed_shutters_file)
    return resolve(physical, legacy, root_metafiles(obs, roots=set(legacy)) if legacy else {})


def stuck_set(resolved, root):
    """The physical shutters of ``root`` that are stuck closed."""
    return {k for k, e in resolved.get(root, {}).items() if e.stuck}


def row_keys(rows):
    """``(q, r, c)`` of every SHUTTER_INFO row."""
    return list(zip(np.asarray(rows['shutter_quadrant'], int), np.asarray(rows['shutter_row'], int),
                    np.asarray(rows['shutter_column'], int)))


def remove_stuck(rows, stuck):
    """The SHUTTER_INFO rows without the stuck shutters (every dither point)."""
    if not stuck or len(rows) == 0:
        return rows
    keep = np.array([k not in stuck for k in row_keys(rows)], bool)
    return rows if keep.all() else rows[keep]


def source_ordinals(rows, stuck):
    """The legacy 1-indexed ordinals (among the source's sorted columns) of the stuck
    shutters in a source's rows: what the ``STKSHTRS`` card and the nods plots count."""
    cols = list(source_columns(rows))
    hit = sorted({int(c) for (q, r, c) in row_keys(rows) if (q, r, c) in stuck})
    return [cols.index(c) + 1 for c in hit]


def as_source_table(resolved, metafiles):
    """The resolved list as the legacy ``(root, source_id, shutters)`` table the nods
    plots read: every source whose slitlet holds a stuck shutter, with its ordinals."""
    from astropy.table import Table
    roots, sids, shutters = [], [], []
    for root in sorted(resolved):
        stuck = stuck_set(resolved, root)
        mf = metafiles.get(root)
        if not stuck or mf is None:
            continue
        for sid in mf.unique_source_ids:
            try:
                rows = mf.rows_for_source(int(sid))
            except RuntimeError:
                continue
            ords = source_ordinals(rows, stuck)
            if ords:
                roots.append(root); sids.append(int(sid)); shutters.append(ords)
    return Table({'root': roots, 'source_id': sids, 'shutters': shutters},
                 dtype=[str, int, object])


# ---------------------------------------------------------------------------
# The auto writer
# ---------------------------------------------------------------------------

HEADER = """\
# stuck closed shutter list for {obs}
# one table per root (obs/visit/config, e.g. "jw06368001001_03101"), keyed by the
# physical shutter "q<quadrant>r<row>c<column>" (the metafile's shutter_quadrant,
# shutter_row, shutter_column); origin manual > web > auto; stuck = false vetoes:
# [jw06368001001_03101.shutters]
#     "q2r195c64" = {{origin = "manual", stuck = true}}
"""


def _inline(entry_dict):
    import tomlkit
    t = tomlkit.inline_table()
    for k in ('origin', 'stuck', 'a', 'note'):
        if k in entry_dict and entry_dict[k] is not None and entry_dict[k] != '':
            t[k] = entry_dict[k]
    return t


def write_auto(path, root, calls, metafiles=None, obs_name=''):
    """Merge one root's fit calls into the TOML as ``auto`` entries; returns True when
    the file changed (it is not rewritten otherwise: its mtime is stage 2a's skip key).

    ``calls`` is ``shutter_fit.call_shutters`` output, ``{(q, r, c): {'stuck', 'a',
    'n', 'note'}}``. A stuck call is written unless a manual or web entry (physical or
    legacy) already decides the shutter; an existing auto entry whose shutter is now
    called open is removed, or, when a legacy auto entry still names the shutter, set
    to ``stuck = false`` so it outranks that entry. Manual and web entries and shutters
    the calls do not cover are left alone. Comments and layout are kept (tomlkit).
    """
    import tomlkit
    old = open(path).read() if os.path.exists(path) else ''
    doc = tomlkit.parse(old) if old.strip() else tomlkit.parse(HEADER.format(obs=obs_name))
    physical, legacy = read_file(path) if old.strip() else ({}, {})
    legacy_keys = {}
    if legacy.get(root):
        resolved_legacy = resolve({}, {root: legacy[root]}, metafiles or {})
        legacy_keys = resolved_legacy.get(root, {})
    phys = physical.get(root, {})

    if root not in doc:
        doc.add(root, tomlkit.table(is_super_table=True))
    rt = doc[root]
    if SHUTTERS not in rt:
        rt.add(SHUTTERS, tomlkit.table())
    st = rt[SHUTTERS]

    for key, call in sorted(calls.items()):
        skey = shutter_key(*key)
        cur = phys.get(key)
        decided = (cur is not None and cur.origin != 'auto') or (key in legacy_keys and legacy_keys[key].origin != 'auto')
        if decided:
            continue
        a = call.get('a', math.nan)
        a = round(float(a), 3) if a is not None and np.isfinite(a) else None
        if call.get('stuck'):
            new = {'origin': 'auto', 'stuck': True, 'a': a}
            if cur is None or cur.origin != 'auto' or cur.stuck is not True or cur.a != a:
                st[skey] = _inline(new)
        elif key in legacy_keys and legacy_keys[key].origin == 'auto':
            if cur is None or cur.stuck is not False:
                st[skey] = _inline({'origin': 'auto', 'stuck': False, 'a': a, 'note': 'open; outranks a legacy auto entry'})
        elif cur is not None:
            del st[skey]

    if len(st) == 0:
        del rt[SHUTTERS]
    if len(rt) == 0:
        del doc[root]
    text = tomlkit.dumps(doc)
    if text == old:
        return False
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, path)
    log(f'stuck shutters: wrote auto calls for {root} to {path}')
    return True
