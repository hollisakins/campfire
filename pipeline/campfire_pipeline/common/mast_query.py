"""Ask MAST which exposures exist, for JWST and HST alike, without downloading.

One query path for every instrument: the CAOM catalog (the TAP service
``cfpipe download`` already uses), selected by program, by a field's tiles or
by a position on the sky. The answer is a list of :class:`Exposure` records,
one per archive file set (a JWST detector exposure, an HST exposure), which
``cfpipe query`` prints as a summary, as one line per exposure, or as JSON.

What an exposure selection is *for*: a field's membership is always the file
globs written in ``fields.toml``. This module only tells the reducer what is
there, and prints the glob for each program so the lines can be pasted.

Selection on the sky is done in two steps. The server gets a box around the
region, padded, on the *target* position (the only indexed position there
is). The footprint of every exposure that comes back is then tested against
the region here. The padding has to cover the distance between a target and
its footprint, which for a parallel observation is the separation of two
instruments in the focal plane.
"""

import json
import time
from dataclasses import asdict, dataclass

import numpy as np


@dataclass(frozen=True)
class Instrument:
    token: str        # the name used in commands, config and product names
    telescope: str    # 'jwst' or 'hst'
    collection: str   # CAOM collection
    insname: str      # CAOM instrument name (an ADQL LIKE pattern)
    imaging: bool     # keep image data products only


INSTRUMENTS = {
    'nircam': Instrument('nircam', 'jwst', 'JWST', 'NIRCAM/IMAGE', True),
    'nirspec': Instrument('nirspec', 'jwst', 'JWST', 'NIRSPEC%', False),
    'acswfc': Instrument('acswfc', 'hst', 'HST', 'ACS/WFC', True),
    'wfc3ir': Instrument('wfc3ir', 'hst', 'HST', 'WFC3/IR', True),
}
TELESCOPES = ('jwst', 'hst')

# Padding of the server-side box around a region, degrees: the largest
# distance expected between an observation's target and its footprint.
DEFAULT_PAD_DEG = 0.3

_SELECT = (
    "o.observationid, o.prpid, o.prppi, o.trgname, o.intent, "
    "p.enrbandpassname, p.timexposure, p.timmin, p.datarights, "
    "p.dataproducttype, p.posboundsstcs"
)


def resolve_instruments(telescope=None, instruments=None):
    """The :class:`Instrument` records a query covers.

    ``instruments`` are tokens; with none, every instrument of ``telescope``.
    An instrument implies its telescope, so ``telescope`` may be left out;
    naming a telescope and an instrument of the other one is an error, as is
    naming neither.
    """
    telescope = telescope.lower() if telescope else None
    if telescope is not None and telescope not in TELESCOPES:
        raise ValueError(f'unknown telescope {telescope!r}; expected one of '
                         f'{", ".join(TELESCOPES)}')
    tokens = [t.lower() for t in (instruments or [])]
    unknown = [t for t in tokens if t not in INSTRUMENTS]
    if unknown:
        raise ValueError(f'unknown instrument(s) {unknown}; expected one of '
                         f'{", ".join(INSTRUMENTS)}')
    if not tokens:
        if telescope is None:
            raise ValueError('name a telescope or an instrument')
        return [i for i in INSTRUMENTS.values() if i.telescope == telescope]
    chosen = [INSTRUMENTS[t] for t in dict.fromkeys(tokens)]
    wrong = [i.token for i in chosen if telescope and i.telescope != telescope]
    if wrong:
        raise ValueError(f'{", ".join(wrong)}: not on {telescope.upper()}')
    return chosen


@dataclass(frozen=True)
class Exposure:
    """One archive file set: a JWST detector exposure or an HST exposure."""

    instrument: str
    telescope: str
    obs_id: str        # 'jw06882060001_06101_00003_nrcb4', 'iczgo2dlq'
    program: str       # proposal number as the archive writes it
    pi: str
    target: str
    filter: str        # lower case; several elements joined with ';'
    exptime: float     # seconds
    mjd: float         # start, or nan for an exposure not yet taken
    rights: str        # 'PUBLIC' or the archive's other values
    footprint: tuple   # polygons, each a tuple of (ra, dec) vertices

    @property
    def exposure(self):
        """What the detector files of one exposure share: the JWST name
        without the detector, the HST rootname."""
        if self.telescope == 'jwst':
            return '_'.join(self.obs_id.split('_')[:3])
        return self.obs_id

    @property
    def glob(self):
        """The file glob that selects this exposure's program in
        ``fields.toml``. JWST names carry the program number. HST rootnames
        carry a three-character program code after the instrument letter
        instead, which the archive does not translate back."""
        if self.telescope == 'jwst':
            return f'jw{int(self.program):05d}*'
        return f'{self.obs_id[:4]}*'

    @property
    def date(self):
        if not np.isfinite(self.mjd):
            return ''
        from astropy.time import Time
        return Time(self.mjd, format='mjd').strftime('%Y-%m-%d')

    def to_dict(self):
        out = asdict(self)
        out['mjd'] = self.mjd if np.isfinite(self.mjd) else None
        out['footprint'] = [[list(v) for v in poly] for poly in self.footprint]
        out['exposure'] = self.exposure
        out['glob'] = self.glob
        return out

    @classmethod
    def from_dict(cls, d):
        """Back from :meth:`to_dict` (the JSON ``cfpipe query`` writes)."""
        return cls(
            instrument=str(d['instrument']), telescope=str(d['telescope']),
            obs_id=str(d['obs_id']), program=str(d.get('program') or ''),
            pi=str(d.get('pi') or ''), target=str(d.get('target') or ''),
            filter=str(d.get('filter') or ''),
            exptime=_float(d.get('exptime'), 0.0), mjd=_float(d.get('mjd')),
            rights=str(d.get('rights') or ''),
            footprint=tuple(tuple((float(r), float(c)) for r, c in poly)
                            for poly in d.get('footprint') or ()))

    @property
    def filter_dir(self):
        """The filter as a directory name: the bandpass elements that are not
        an open position, joined by '-' ('f606w', 'f606w-pol0v')."""
        parts = [p for p in self.filter.split(';')
                 if p and not p.startswith('clear')]
        return '-'.join(parts) or 'unknown'


# --- regions on the sky -------------------------------------------------------

def cone_polygon(ra, dec, radius_deg, n=36):
    """A circle on the sky as a polygon of ``n`` vertices (degrees)."""
    theta = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    cosd = max(np.cos(np.radians(dec)), 1e-6)
    return np.column_stack([(ra + radius_deg * np.cos(theta) / cosd) % 360.0,
                            dec + radius_deg * np.sin(theta)])


def field_polygons(field, tiles=None):
    """The sky polygons of a field's tiles (all of them, or those named)."""
    names = list(tiles) if tiles else list(field.tiles)
    missing = [t for t in names if t not in field.tiles]
    if missing:
        raise ValueError(f"field '{field.name}' has no tile(s) {missing}; "
                         f"declared: {list(field.tiles)}")
    if not names:
        raise ValueError(f"field '{field.name}' declares no tiles, so it has "
                         f"no footprint to search")
    return [np.asarray(field.get_tile_corners(t), dtype=float) for t in names]


def parse_stcs(text):
    """Polygons of a CAOM ``posboundsstcs`` string, as ``(n, 2)`` arrays.

    ``POLYGON [frame] ra dec ra dec ...``, possibly several in one string.
    Anything else (a circle, an empty string) gives no polygon.
    """
    polygons = []
    for part in str(text or '').upper().split('POLYGON')[1:]:
        values = []
        for token in part.split():
            try:
                values.append(float(token))
            except ValueError:
                continue                       # frame token, UNION, brackets
        if len(values) >= 6 and len(values) % 2 == 0:
            polygons.append(np.asarray(values, dtype=float).reshape(-1, 2))
    return polygons


def _wrap(delta):
    return (np.asarray(delta, dtype=float) + 180.0) % 360.0 - 180.0


def polygons_overlap(a, b):
    """True when two sky polygons share any area (small polygons; a flat
    projection about the first one's center)."""
    from matplotlib.path import Path

    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    ra0, dec0 = a[0, 0], float(np.mean(a[:, 1]))
    cosd = np.cos(np.radians(dec0))

    def flat(p):
        return np.column_stack([_wrap(p[:, 0] - ra0) * cosd, p[:, 1] - dec0])

    return bool(Path(flat(a)).intersects_path(Path(flat(b)), filled=True))


def _box_clause(regions, pad_deg):
    """ADQL condition on the target position: a box around ``regions``."""
    points = np.vstack(regions)
    dec_lo = max(float(points[:, 1].min()) - pad_deg, -90.0)
    dec_hi = min(float(points[:, 1].max()) + pad_deg, 90.0)
    cosd = max(np.cos(np.radians(max(abs(dec_lo), abs(dec_hi)))), 1e-3)
    ra0 = float(points[0, 0])
    offsets = _wrap(points[:, 0] - ra0)
    lo = ra0 + float(offsets.min()) - pad_deg / cosd
    hi = ra0 + float(offsets.max()) + pad_deg / cosd
    dec = f'o.trgposdec BETWEEN {dec_lo:.6f} AND {dec_hi:.6f}'
    if hi - lo >= 360.0:
        return dec
    if lo < 0.0 or hi >= 360.0:                 # the box crosses RA = 0
        return (f'{dec} AND (o.trgposra >= {lo % 360.0:.6f} '
                f'OR o.trgposra <= {hi % 360.0:.6f})')
    return f'{dec} AND o.trgposra BETWEEN {lo:.6f} AND {hi:.6f}'


# --- the query ----------------------------------------------------------------

def build_adql(instrument, *, programs=None, regions=None, science_only=True,
               pad_deg=DEFAULT_PAD_DEG):
    """The ADQL for one instrument. Raw planes only (calibration level 1):
    one row per file set that was actually taken."""
    where = [
        f"o.collection = '{instrument.collection}'",
        f"o.insname LIKE '{instrument.insname}'",
        "o.obstype = 'S'",
        "p.calibrationlevel = 1",
    ]
    if science_only:
        where.append("o.intent = 'science'")
    if programs:
        ids = ', '.join(f"'{int(p)}'" for p in programs)
        where.append(f'o.prpid IN ({ids})')
    if regions:
        where.append(_box_clause(regions, pad_deg))
    return (f'SELECT {_SELECT} FROM dbo.caomobservation o '
            'JOIN dbo.caomplane p ON p.observationtid = o.observationtid '
            'WHERE ' + ' AND '.join(where))


def _float(value, default=float('nan')):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _exposure_from_row(instrument, row):
    return Exposure(
        instrument=instrument.token, telescope=instrument.telescope,
        obs_id=str(row.get('observationid') or '').strip(),
        program=str(row.get('prpid') or '').strip(),
        pi=str(row.get('prppi') or '').strip(),
        target=str(row.get('trgname') or '').strip(),
        filter=str(row.get('enrbandpassname') or '').strip().lower(),
        exptime=_float(row.get('timexposure'), 0.0),
        mjd=_float(row.get('timmin')),
        rights=str(row.get('datarights') or '').strip(),
        footprint=tuple(tuple((float(r), float(d)) for r, d in poly)
                        for poly in parse_stcs(row.get('posboundsstcs'))),
    )


def query_exposures(instruments, *, programs=None, regions=None, filters=None,
                    science_only=True, pad_deg=DEFAULT_PAD_DEG, tap=None,
                    log=print):
    """Exposures MAST holds for ``instruments`` (:class:`Instrument` records).

    ``programs``: proposal numbers. ``regions``: sky polygons (``(n, 2)``
    arrays in degrees; :func:`cone_polygon`, :func:`field_polygons`); an
    exposure is kept when its footprint touches any of them. ``filters``:
    keep exposures whose bandpass has one of these elements. At least one of
    ``programs`` and ``regions`` is required: the archive is not listed whole.
    ``tap`` runs the ADQL and returns rows as dicts (default: the CAOM TAP
    helper of ``common/query.py``); tests pass their own.

    Returns the exposures sorted by instrument, program, date and name.
    """
    if not programs and not regions:
        raise ValueError('a query needs a program, a field or a position')
    if tap is None:
        from campfire_pipeline.common.query import _tap_query as tap
    regions = [np.asarray(r, dtype=float) for r in (regions or [])]
    wanted = {f.lower() for f in (filters or [])}

    found = {}
    for instrument in instruments:
        adql = build_adql(instrument, programs=programs, regions=regions,
                          science_only=science_only, pad_deg=pad_deg)
        t0 = time.time()
        rows = tap(adql)
        kept = no_footprint = 0
        for row in rows:
            if instrument.imaging and str(row.get('dataproducttype') or
                                          '').strip().lower() != 'image':
                continue
            exp = _exposure_from_row(instrument, row)
            if not exp.obs_id or (instrument.token, exp.obs_id) in found:
                continue
            if wanted and not wanted & set(exp.filter.split(';')):
                continue
            if regions:
                if not exp.footprint:
                    no_footprint += 1
                    continue
                if not any(polygons_overlap(region, np.asarray(poly))
                           for region in regions for poly in exp.footprint):
                    continue
            found[(instrument.token, exp.obs_id)] = exp
            kept += 1
        note = (f'; {no_footprint} without a footprint in the archive were '
                f'left out' if no_footprint else '')
        log(f'{instrument.token}: {len(rows)} archive rows, {kept} file sets '
            f'selected in {time.time() - t0:.1f}s{note}')
    return sorted(found.values(), key=lambda e: (
        e.instrument, _float(e.program, 0.0),
        e.mjd if np.isfinite(e.mjd) else float('inf'), e.obs_id))


# --- output -------------------------------------------------------------------

def summarize(exposures):
    """One row per instrument, program and filter.

    ``n_exposures`` counts exposures (the detector files of a JWST exposure
    count once), ``n_files`` the archive file sets, ``exptime`` the exposure
    time summed over exposures.
    """
    groups = {}
    for e in exposures:
        groups.setdefault((e.instrument, e.program, e.filter), []).append(e)
    rows = []
    for (instrument, program, filt), members in groups.items():
        per_exposure = {}
        for e in members:
            per_exposure[e.exposure] = max(per_exposure.get(e.exposure, 0.0),
                                           e.exptime)
        dates = sorted(e.date for e in members if e.date)
        rows.append({
            'instrument': instrument, 'program': program, 'filter': filt,
            'pi': members[0].pi,
            'targets': sorted({e.target for e in members if e.target}),
            'n_exposures': len(per_exposure), 'n_files': len(members),
            'exptime': float(sum(per_exposure.values())),
            'first': dates[0] if dates else '', 'last': dates[-1] if dates else '',
            'rights': sorted({e.rights for e in members if e.rights}),
            'globs': sorted({e.glob for e in members}),
        })
    rows.sort(key=lambda r: (r['instrument'], _float(r['program'], 0.0),
                             r['filter']))
    return rows


def format_summary(exposures):
    """The summary as text: per instrument a line per program and filter, then
    the glob lines for ``fields.toml``."""
    rows = summarize(exposures)
    if not rows:
        return 'no exposures found'
    lines = []
    for instrument in dict.fromkeys(r['instrument'] for r in rows):
        mine = [r for r in rows if r['instrument'] == instrument]
        n_exp = sum(r['n_exposures'] for r in mine)
        lines.append(f'{instrument}: {n_exp} exposures in '
                     f'{len({r["program"] for r in mine})} programs')
        for r in mine:
            span = r['first'] if r['first'] == r['last'] else \
                f"{r['first']} to {r['last']}"
            rights = '' if r['rights'] == ['PUBLIC'] else \
                f"  [{', '.join(r['rights'])}]"
            lines.append(
                f"  {r['program']:>6s}  {r['filter']:<14s} "
                f"{r['n_exposures']:4d} exposures {r['exptime']:9.0f} s  "
                f"{span}  {r['pi']}{rights}")
        lines.append('')
        lines.append(f'  globs for fields.toml ({instrument}):')
        by_glob = {}
        for r in mine:
            for g in r['globs']:
                by_glob.setdefault(g, []).append(r)
        for g, members in sorted(by_glob.items()):
            programs = ', '.join(dict.fromkeys(m['program'] for m in members))
            filters = ' '.join(dict.fromkeys(m['filter'] for m in members))
            n = sum(m['n_exposures'] for m in members)
            lines.append(f"    '{g}',   # {programs}  {members[0]['pi']}  "
                         f"{filters}  {n} exposures")
        lines.append('')
    return '\n'.join(lines).rstrip()


def format_exposures(exposures):
    """One line per archive file set."""
    if not exposures:
        return 'no exposures found'
    return '\n'.join(
        f'{e.instrument:8s} {e.program:>6s} {e.obs_id:36s} {e.filter:<14s} '
        f'{e.exptime:8.0f} s  {e.date or "not taken":10s}  {e.rights}'
        for e in exposures)


def to_json(exposures):
    """The exposures as a JSON list (the form ``cfpipe download --from`` reads)."""
    return json.dumps([e.to_dict() for e in exposures], indent=1)


def from_json(text):
    """Exposures from the JSON ``cfpipe query --format json`` writes."""
    try:
        listed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f'not the JSON of `cfpipe query --format json`: {e}')
    if not isinstance(listed, list):
        raise ValueError('expected a JSON list of exposures')
    return [Exposure.from_dict(d) for d in listed]


# --- HST download -------------------------------------------------------------
#
# JWST downloads stay with `common/query.py::download_jwst_data` (uncal files,
# NIRSpec MSA metadata, the NIRCam manifest). HST is new and goes through the
# exposures of this module: one `<rootname>_raw.fits` per exposure, written to
# raw/hst/<detector>/<proposal>/<filter>/ (layout kind `raw_hst`).

def raw_products(exposures, tap=None, batch=100):
    """The archive's RAW file of each HST exposure.

    Returns ``{obs_id: {'filename', 'uri', 'size', 'checksum'}}``; an exposure
    the archive lists no ``<rootname>_raw.fits`` for is absent.
    """
    if tap is None:
        from campfire_pipeline.common.query import _tap_query as tap
    ids = sorted({e.obs_id for e in exposures})
    bad = [i for i in ids if not i.isalnum()]
    if bad:
        raise ValueError(f'not HST rootnames: {bad[:3]}')
    found = {}
    for k in range(0, len(ids), batch):
        chunk = ids[k:k + batch]
        listed = ', '.join(f"'{i}'" for i in chunk)
        rows = tap(
            'SELECT o.observationid, a.productfilename, a.datauri, '
            'a.contentlength, a.contentchecksum FROM dbo.caomobservation o '
            'JOIN dbo.caomplane p ON p.observationtid = o.observationtid '
            'JOIN dbo.caomartifact a ON a.planetid = p.planetid '
            f"WHERE o.collection = 'HST' AND o.observationid IN ({listed}) "
            "AND a.productfilename LIKE '%raw.fits'")
        for row in rows:
            obs_id = str(row.get('observationid') or '').strip()
            name = str(row.get('productfilename') or '').strip()
            if name != f'{obs_id}_raw.fits' or obs_id in found:
                continue
            found[obs_id] = {'filename': name, 'uri': row['datauri'],
                             'size': int(_float(row.get('contentlength'), 0)),
                             'checksum': row.get('contentchecksum') or ''}
    return found


def _hst_manifest_row(exposure, product):
    from datetime import datetime, timezone
    polygons = ' '.join(
        'POLYGON ICRS ' + ' '.join(f'{r:.9f} {d:.9f}' for r, d in poly)
        for poly in exposure.footprint)
    return {
        'filename': product['filename'], 'rootname': exposure.obs_id,
        'detector': exposure.instrument, 'program': exposure.program,
        'filter': exposure.filter_dir, 'target': exposure.target,
        'pi': exposure.pi, 'date_obs': exposure.date,
        'mjd': exposure.mjd, 'exptime': exposure.exptime,
        's_region': polygons, 'size_bytes': int(product['size']),
        'downloaded_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }


def _write_hst_manifest(directory, rows):
    """Upsert rows into ``<directory>/manifest.ecsv`` (one per detector and
    proposal), keyed on the file name, as the NIRCam download does."""
    from astropy.table import Table, vstack

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'manifest.ecsv'
    table = Table(rows=rows)
    if path.exists():
        try:
            old = Table.read(path, format='ascii.ecsv')
            keep = [name not in set(table['filename']) for name in old['filename']]
            table = vstack([old[keep], table], join_type='outer')
        except Exception as e:
            print(f'  Warning: could not read {path} ({e}); overwriting.')
    table.sort('filename')
    table.write(path, format='ascii.ecsv', overwrite=True)
    return path


def download_hst(exposures, campfire_root, *, token=None, workers=4,
                 dry_run=False, tap=None, log=print):
    """Download the RAW file of every HST exposure given.

    Files go to ``raw/hst/<detector>/<proposal>/<filter>/<rootname>_raw.fits``
    under ``campfire_root``; a file already there at the archive's size is
    skipped, so a re-run resumes. Each ``<detector>/<proposal>`` directory
    gets a ``manifest.ecsv`` with what the archive said about its exposures
    (filter, date, exposure time, footprint). Returns
    ``{'selected', 'missing', 'present', 'downloaded', 'errors'}`` counts.
    """
    from pathlib import Path

    from campfire_layout import Scope, raw_dir, raw_path

    from campfire_pipeline.common.query import (_split_existing, download_files,
                                                format_size)

    exposures = list(exposures)
    foreign = sorted({e.telescope for e in exposures} - {'hst'})
    if foreign:
        raise ValueError(
            f'download_hst got {", ".join(foreign).upper()} exposures; JWST '
            f'data are downloaded by program (cfpipe download --program)')
    root = Path(campfire_root)
    products = raw_products(exposures, tap=tap)
    files, rows, missing = [], {}, []
    for e in exposures:
        product = products.get(e.obs_id)
        if product is None:
            missing.append(e.obs_id)
            continue
        scope = Scope(detector=e.instrument, pid=e.program, filt=e.filter_dir)
        files.append(dict(product, _path=raw_path('hst', scope,
                                                  product['filename'], root=root)))
        manifest_dir = raw_dir('hst', Scope(detector=e.instrument, pid=e.program),
                               root=root)
        rows.setdefault(manifest_dir, []).append(_hst_manifest_row(e, product))

    todo, present = _split_existing(files)
    log(f'{len(exposures)} exposures selected: {len(todo)} to download '
        f'({format_size(sum(f["size"] for f in todo))}), {len(present)} already '
        f'on disk' + (f', {len(missing)} with no RAW file in the archive: '
                      f'{", ".join(missing[:5])}' if missing else ''))
    counts = {'selected': len(exposures), 'missing': len(missing),
              'present': len(present), 'downloaded': 0, 'errors': 0}
    if dry_run:
        for f in todo:
            log(f'  would download {f["_path"].relative_to(root)}')
        return counts
    counts['errors'] = download_files(todo, token=token, workers=workers,
                                      desc='HST raw')
    counts['downloaded'] = len(todo) - counts['errors']
    for directory, members in rows.items():
        path = _write_hst_manifest(directory, members)
        log(f'  manifest: {path.relative_to(root)} ({len(members)} rows written)')
    return counts
