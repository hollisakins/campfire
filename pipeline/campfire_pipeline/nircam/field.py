"""
Field dataclass: NIRCam field configuration and workspace management.

Analogous to nirspec/observation.py but organized around sky fields
(file globs + filters) rather than MSA observations.

Raw uncal layout is PID-organized: ``$CAMPFIRE_ROOT/raw/nircam/{PID}/{filter}/``.
PIDs are derived from the leading ``jwNNNNN`` of each ``files`` glob, so a field
that spans multiple programs naturally pulls from multiple PID directories.
"""

import json
import os
import re
from glob import glob
from datetime import date
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import List, Optional

import toml
import numpy as np
from astropy.io import fits

from campfire_layout import (
    Scope,
    dir_for,
    nircam_work_dir as layout_nircam_work_dir,
    raw_dir as layout_raw_dir,
    reference_dir as layout_reference_dir,
    roots,
    shared_reference_dir as layout_shared_reference_dir,
)
from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.constants import SW_FILTERS, LW_FILTERS


_PID_RE = re.compile(r'jw(\d{5})')
_PIXEL_SCALE_RE = re.compile(r'^(\d+)mas$')
_BRACE_RE = re.compile(r'\{([^{}]*)\}')


def _extract_pid(pattern):
    """Pull the PID out of a JWST file glob (e.g. 'jw01727*' → '1727').

    JWST filenames carry the PID zero-padded to 5 digits, but the on-disk raw
    layout under ``$CAMPFIRE_ROOT/raw/nircam/`` uses the unpadded integer form,
    so leading zeros are stripped.
    """
    m = _PID_RE.match(pattern)
    return str(int(m.group(1))) if m else None


def _expand_braces(pattern):
    """Expand bash-style brace lists in a glob pattern.

    ``'jw01234{001,002,003}*'`` → ``['jw01234001*', 'jw01234002*', 'jw01234003*']``.
    Multiple/nested braces produce the cartesian product:
    ``'a{1,2}{x,y}'`` → ``['a1x', 'a1y', 'a2x', 'a2y']``. Patterns without
    braces pass through unchanged (as a single-element list). Python's stdlib
    ``glob`` doesn't understand braces, so we expand them up-front into the
    list of patterns the rest of the pipeline already handles.
    """
    m = _BRACE_RE.search(pattern)
    if m is None:
        return [pattern]
    prefix = pattern[:m.start()]
    suffix = pattern[m.end():]
    options = m.group(1).split(',')
    result = []
    for opt in options:
        result.extend(_expand_braces(prefix + opt + suffix))
    return result


# Cache of path → observation date (or None) so a date-range epoch doesn't
# reopen the same FITS header on every combine step. Mirrors the S_REGION cache
# in geometry.py; a miss (unreadable / missing keyword) is cached as None so a
# later readable pass can still resolve it — matching the S_REGION fail-open.
_DATE_OBS_CACHE = {}


def _read_date_obs(path):
    """Observation date of an exposure as a ``datetime.date``, or ``None``.

    Reads ``DATE-OBS`` (``YYYY-MM-DD``) from the primary header, falling back to
    the date part of ``DATE-BEG`` (``YYYY-MM-DDThh:mm:ss``). Returns ``None`` for
    an unreadable file or a missing/malformed keyword; callers fail open (keep
    the file) so a bad header never silently drops an exposure.
    """
    if path in _DATE_OBS_CACHE:
        return _DATE_OBS_CACHE[path]
    d = None
    try:
        with fits.open(path, memmap=False) as hdul:
            hdr = hdul[0].header
            raw = hdr.get('DATE-OBS') or hdr.get('DATE-BEG')
        if raw:
            try:
                d = date.fromisoformat(str(raw)[:10])
            except ValueError:
                d = None
    except (OSError, KeyError):
        # Unreadable / transient — fail open (kept by callers) but do NOT cache,
        # so a later readable pass on the same path can still resolve it.
        return None
    _DATE_OBS_CACHE[path] = d
    return d


# DO_NOT_USE == 1 (jwst.datamodels.dqflags.pixel['DO_NOT_USE']). Hardcoded so
# field.py stays free of the jwst/CRDS import chain (which, imported at module
# top, would lock CRDS into serverless mode before setup_environment runs).
_DO_NOT_USE = np.uint32(1)


def _prime_work_copy(path):
    """Prime a freshly-copied combine working copy for the ensemble steps.

    Fuses the canonical's CFMASK extension into the working copy's DQ as
    ``DO_NOT_USE`` (so ``good_bits='~DO_NOT_USE'`` excludes the user-masked
    pixels in outlier detection and resample), and clears any ``CFP_BPIX`` /
    ``CFP_OUT`` provenance so those ensemble steps re-derive their flags on the
    fresh copy. Operates on the copy only — the canonical is never touched.
    """
    with fits.open(path, mode='update', memmap=False) as hdul:
        if 'CFMASK' in hdul and 'DQ' in hdul:
            masked = np.asarray(hdul['CFMASK'].data).astype(bool)
            dq = np.asarray(hdul['DQ'].data)
            dq[masked] |= _DO_NOT_USE
            hdul['DQ'].data = dq          # reassign so astropy flushes the change
        hdr = hdul[0].header
        for key in ('CFP_BPIX', 'CFP_OUT'):
            if key in hdr:
                del hdr[key]
        hdul.flush()


def _is_pixel_scale_section(value):
    """True for a dict like ``{'crpix': [...], 'naxis': [...]}``."""
    return isinstance(value, dict) and 'crpix' in value and 'naxis' in value


def _tile_has_wcs_subsection(value):
    """True if ``value`` defines at least one pixel-scale WCS subsection."""
    if not isinstance(value, dict):
        return False
    return any(
        _PIXEL_SCALE_RE.match(k) and _is_pixel_scale_section(v)
        for k, v in value.items()
    )


_WCS_SHIFT_REQUIRED = {'files', 'delta_ra', 'delta_dec'}
_WCS_SHIFT_ALLOWED = (
    _WCS_SHIFT_REQUIRED | {'filters', 'delta_roll', 'scale'}
)


def _parse_wcs_shift_rules(field_name, raw, field_filters):
    """Normalize a list of [[<field>.wcs_shift]] entries.

    Each entry must declare ``files`` (string or list of rootname globs) and
    ``delta_ra``/``delta_dec`` (degrees). Optional: ``filters`` (defaults to
    every field filter), ``delta_roll`` (default 0.0), ``scale`` (default 1.0).
    Brace-style globs in ``files`` are expanded up-front.

    Returns
    -------
    list of dict
        Empty list when ``raw`` is None or empty.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"Field '{field_name}': [<field>.wcs_shift] must be an array of "
            f"tables ([[ ]]), got {type(raw).__name__}"
        )

    rules = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(
                f"Field '{field_name}': wcs_shift entry #{i} must be a table"
            )
        unknown = set(entry) - _WCS_SHIFT_ALLOWED
        if unknown:
            raise ValueError(
                f"Field '{field_name}': wcs_shift entry #{i} has unknown "
                f"keys {sorted(unknown)}. Allowed: {sorted(_WCS_SHIFT_ALLOWED)}"
            )
        missing = _WCS_SHIFT_REQUIRED - set(entry)
        if missing:
            raise ValueError(
                f"Field '{field_name}': wcs_shift entry #{i} missing required "
                f"keys {sorted(missing)}"
            )

        files = entry['files']
        if isinstance(files, str):
            files = [files]
        files = [p for raw_p in files for p in _expand_braces(raw_p)]
        files = list(np.unique(files))
        bad = [p for p in files if _extract_pid(p) is None]
        if bad:
            raise ValueError(
                f"Field '{field_name}': wcs_shift entry #{i} `files` patterns "
                f"must start with 'jwNNNNN' (5-digit PID). Offending: {bad}"
            )

        filters = entry.get('filters')
        if filters is None:
            filters_norm = list(field_filters)
        else:
            if isinstance(filters, str):
                filters = [filters]
            unknown_f = [f for f in filters if f not in field_filters]
            if unknown_f:
                raise ValueError(
                    f"Field '{field_name}': wcs_shift entry #{i} `filters` "
                    f"contains values not in field.filters: {unknown_f}"
                )
            filters_norm = list(filters)

        rules.append({
            'files': files,
            'filters': filters_norm,
            'delta_ra': float(entry['delta_ra']),
            'delta_dec': float(entry['delta_dec']),
            'delta_roll': float(entry.get('delta_roll', 0.0)),
            'scale': float(entry.get('scale', 1.0)),
        })
    return rules


_EPOCH_ALLOWED = {'files', 'date_range'}


def _parse_epochs(field_name, raw):
    """Normalize the ``[<field>.epochs.<name>]`` tables into a dict.

    Each epoch names a *subset* of the field's exposures, defined by an optional
    ``files`` glob list (same ``jwNNNNN`` validation + brace expansion as the
    field's ``files``/``skip``) and/or an optional ``date_range = [start, end]``
    (inclusive ISO dates matched against each exposure's ``DATE-OBS``). At least
    one of the two must be present. The epoch name becomes an extra segment on
    the mosaic filename (see :func:`manifest.build_mosaic_name`).

    Returns
    -------
    dict
        ``{name: {'files': [globs] | None, 'date_range': (start, end) | None}}``.
        Empty dict when ``raw`` is None or empty.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Field '{field_name}': [{field_name}.epochs] must be a table of "
            f"named epochs, got {type(raw).__name__}"
        )

    epochs = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"Field '{field_name}': epoch '{name}' must be a table"
            )
        unknown = set(entry) - _EPOCH_ALLOWED
        if unknown:
            raise ValueError(
                f"Field '{field_name}': epoch '{name}' has unknown keys "
                f"{sorted(unknown)}. Allowed: {sorted(_EPOCH_ALLOWED)}"
            )

        files = entry.get('files')
        if files is not None:
            if isinstance(files, str):
                files = [files]
            files = [p for raw_p in files for p in _expand_braces(raw_p)]
            files = list(np.unique(files))
            bad = [p for p in files if _extract_pid(p) is None]
            if bad:
                raise ValueError(
                    f"Field '{field_name}': epoch '{name}' `files` patterns "
                    f"must start with 'jwNNNNN' (5-digit PID). Offending: {bad}"
                )

        date_range = entry.get('date_range')
        if date_range is not None:
            if (not isinstance(date_range, (list, tuple))
                    or len(date_range) != 2):
                raise ValueError(
                    f"Field '{field_name}': epoch '{name}' `date_range` must be "
                    f"a two-element [start, end] list of ISO dates"
                )
            try:
                start = date.fromisoformat(str(date_range[0]))
                end = date.fromisoformat(str(date_range[1]))
            except ValueError as e:
                raise ValueError(
                    f"Field '{field_name}': epoch '{name}' `date_range` values "
                    f"must be ISO dates (YYYY-MM-DD): {e}"
                )
            if start > end:
                raise ValueError(
                    f"Field '{field_name}': epoch '{name}' `date_range` start "
                    f"{start} is after end {end}"
                )
            date_range = (start, end)

        if files is None and date_range is None:
            raise ValueError(
                f"Field '{field_name}': epoch '{name}' must define at least one "
                f"of `files` or `date_range`"
            )

        epochs[name] = {'files': files, 'date_range': date_range}
    return epochs


@dataclass
class Field:
    name: str
    filters: List[str]
    files: List[str]           # glob patterns, e.g. ['jw01727*', 'jw05893*']
    tangent_point: tuple       # (RA, Dec)
    tiles: dict                # tile WCS definitions
    step_overrides: dict = field(default_factory=dict)
    skip: List[str] = field(default_factory=list)  # field-wide exclude globs
    rgb: Optional[dict] = None  # optional [field.rgb] block, consumed by `cfpipe nircam rgb`
    # Parsed [[<field>.wcs_shift]] array-of-tables (pre-JHAT shift rules).
    # Each entry: {files: [globs], filters: [filtnames] | None,
    #              delta_ra, delta_dec, delta_roll, scale}.
    wcs_shift_rules: List[dict] = field(default_factory=list)
    # Parsed [<field>.epochs.<name>] tables (combine-time exposure subsets).
    # {name: {files: [globs] | None, date_range: (start, end) | None}}.
    epochs: dict = field(default_factory=dict)
    # Field-level `fiducial_tiles = [...]`: the subset of tiles (sharing a tangent
    # point + rotation, spanning the field) that forms the field-composite map view
    # (FitsGL, epic #337). Empty when undeclared; `fiducial_tile_set()` also honors
    # a per-tile `fiducial = true` fallback.
    fiducial_tiles: List[str] = field(default_factory=list)

    # Populated by setup_workspace()
    campfire_root: Optional[str] = None
    raw_root: Optional[str] = None  # $CAMPFIRE_ROOT/raw/nircam (parent of PID dirs)
    products_dir: Optional[str] = None
    reference_dir: Optional[str] = None
    # Reviewer-flagged exclusions from reference/<field>/exposures.json (epic
    # #261, N6). {filter: [rootname]} — materialized by `campfire deploy nircam
    # pull`; folded into the skip list so they drop from combine + outlier.
    excluded_exposures: dict = field(default_factory=dict)

    # Reference subdirectories
    bad_pixel_dir: Optional[str] = None
    refcat_dir: Optional[str] = None
    wisp_dir: Optional[str] = None
    mask_dir: Optional[str] = None
    flats_dir: Optional[str] = None

    @classmethod
    def load(cls, name, fields_file=None):
        """Load a field definition from fields.toml.

        Parameters
        ----------
        name : str
            Field name (top-level key in fields.toml).
        fields_file : str, optional
            Explicit path to fields.toml. If None, uses
            resolve_fields_file() search order.

        Returns
        -------
        Field
        """
        from campfire_pipeline.config import resolve_fields_file

        fields_file = resolve_fields_file(fields_file)
        with open(fields_file, 'r') as f:
            fields_config = toml.load(f)

        if name not in fields_config:
            available = [k for k in fields_config.keys()]
            raise ValueError(
                f"Field '{name}' not found in {fields_file}. "
                f"Available: {available}"
            )

        fc = fields_config[name]

        filters = fc['filters']
        if isinstance(filters, str):
            filters = [filters]

        file_patterns = fc['files']
        if isinstance(file_patterns, str):
            file_patterns = [file_patterns]
        # Expand bash-style brace lists, e.g. 'jw01234{001,002}*' → two patterns.
        file_patterns = [p for raw in file_patterns for p in _expand_braces(raw)]
        file_patterns = list(np.unique(file_patterns))

        # Validate: every pattern must start with jwNNNNN so we can locate
        # the PID-organized raw directory.
        bad = [p for p in file_patterns if _extract_pid(p) is None]
        if bad:
            raise ValueError(
                f"Field '{name}': every entry in `files` must start with "
                f"'jwNNNNN' (5-digit PID). Offending patterns: {bad}"
            )

        # Field-level skip list: applied to every step that resolves files via
        # get_uncal_files / get_exposure_files. Stacks with caller-passed skip.
        skip_patterns = fc.get('skip', [])
        if isinstance(skip_patterns, str):
            skip_patterns = [skip_patterns]
        skip_patterns = [p for raw in skip_patterns for p in _expand_braces(raw)]
        skip_patterns = list(np.unique(skip_patterns))
        bad_skip = [p for p in skip_patterns if _extract_pid(p) is None]
        if bad_skip:
            raise ValueError(
                f"Field '{name}': every entry in `skip` must start with "
                f"'jwNNNNN' (5-digit PID). Offending patterns: {bad_skip}"
            )

        tangent_point = tuple(fc['tangent_point'])

        # Known per-step keys for the canonical-exposure pipeline. Used both
        # to recognize per-field step overrides and to exclude them from the
        # tile-detection loop below.
        known_steps = {
            'detector1', 'jackknife', 'persistence', 'wisp', 'image2',
            'edge', 'bkg', 'diag_striping',
            'wcs_shift', 'jhat', 'align',
            'apply_mask', 'bad_pixel', 'outlier', 'resample',
        }

        # Parse tile WCS definitions. A tile is any non-reserved sub-table
        # that either declares explicit sky `corners` (legacy / override) or
        # provides at least one `<scale>mas` subsection with `crpix`+`naxis`
        # — in the latter case corners are derived on demand from the WCS.
        tiles = {}
        reserved_keys = ({'filters', 'files', 'skip', 'tangent_point', 'rgb',
                          'epochs', 'fiducial_tiles'} | known_steps)
        for key, value in fc.items():
            if key in reserved_keys:
                continue
            if not isinstance(value, dict):
                continue
            if 'corners' in value or _tile_has_wcs_subsection(value):
                tiles[key] = value

        # Optional [<field>.rgb] block, consumed only by `cfpipe nircam rgb`.
        # Validation of channel filters against `filters` happens at use-site
        # so a stale RGB block doesn't block other commands.
        rgb_cfg = fc.get('rgb')
        if rgb_cfg is not None and not isinstance(rgb_cfg, dict):
            raise ValueError(
                f"Field '{name}': [{name}.rgb] must be a table, got {type(rgb_cfg).__name__}"
            )

        # Capture per-field step config overrides (flat layout)
        step_overrides = {}
        for key in known_steps:
            if key in fc and isinstance(fc[key], dict):
                step_overrides[key] = fc[key]

        # Parse [[<field>.wcs_shift]] array-of-tables (TOML parses these as
        # a list under fc['wcs_shift']) into a normalized rule list.
        wcs_shift_rules = _parse_wcs_shift_rules(name, fc.get('wcs_shift'),
                                                 filters)

        # Parse [<field>.epochs.<name>] tables (combine-time exposure subsets).
        epochs = _parse_epochs(name, fc.get('epochs'))

        # Field-level fiducial tile set (FitsGL field-composite view, epic #337).
        # A list of already-declared tile names; validated against `tiles` so a
        # typo surfaces here rather than as an empty composite downstream.
        raw_fiducial = fc.get('fiducial_tiles', [])
        if isinstance(raw_fiducial, str):
            raw_fiducial = [raw_fiducial]
        if not (isinstance(raw_fiducial, list)
                and all(isinstance(t, str) for t in raw_fiducial)):
            raise ValueError(
                f"Field '{name}': `fiducial_tiles` must be a list of tile-name "
                f"strings, got {raw_fiducial!r}"
            )
        unknown = [t for t in raw_fiducial if t not in tiles]
        if unknown:
            raise ValueError(
                f"Field '{name}': `fiducial_tiles` references undeclared tile(s) "
                f"{unknown}. Declared tiles: {list(tiles.keys())}"
            )

        return cls(
            name=name,
            filters=filters,
            files=file_patterns,
            tangent_point=tangent_point,
            tiles=tiles,
            step_overrides=step_overrides,
            skip=skip_patterns,
            rgb=rgb_cfg,
            wcs_shift_rules=wcs_shift_rules,
            epochs=epochs,
            fiducial_tiles=list(raw_fiducial),
        )

    @property
    def pids(self):
        """Unique JWST program IDs referenced by this field's `files` patterns."""
        return sorted({_extract_pid(p) for p in self.files if _extract_pid(p)})

    def setup_workspace(self, campfire_root=None):
        """Create the directory tree for this field.

        Parameters
        ----------
        campfire_root : str, optional
            Override $CAMPFIRE_ROOT. If None, reads from environment.
        """
        if campfire_root is None:
            from campfire_pipeline.config import _get_campfire_root
            campfire_root = _get_campfire_root()

        # Issue #213 (PR-2): resolve the workspace tree through the shared layout
        # contract (campfire_layout), the single authority shared with deploy.
        self.campfire_root = campfire_root
        scope = Scope(field=self.name)
        self.raw_root = str(layout_raw_dir('nircam', root=campfire_root))
        self.products_dir = str(roots(campfire_root).products / 'nircam' / self.name)
        self.reference_dir = str(layout_reference_dir('nircam', scope, root=campfire_root))
        self.excluded_exposures = self._load_excluded_exposures()

        # Reducer-decision reference state is per-field.
        self.bad_pixel_dir = str(dir_for('nircam_bad_pixel', scope, root=campfire_root))
        self.refcat_dir = str(dir_for('nircam_astrom_cat', scope, root=campfire_root))
        self.mask_dir = str(dir_for('nircam_mask', scope, root=campfire_root))

        # Shared calibration references are detector/filter-scoped, NOT per-field
        # (issue #212 PR-4): custom flats (flat_nircam_<FILT>_<DET>_CLEAR.fits) and
        # wisp templates (WISP_<DET>_<FILT>_CLEAR_*.fits) key purely on
        # (detector, filter), so two fields with the same (detector, filter)
        # resolve byte-identical references. Hoisting them to a shared dir dedups
        # storage. Lookups go through self.flats_dir / self.wisp_dir, so this move
        # is transparent to resolve_flat / image2_step / wisp_step.
        # NOTE (audit): a missing wisp template makes wisp_step skip the exposure
        # without stamping CFP_WISP; with a shared dir, a field that previously
        # had an empty wisp_dir (intentionally skipping wisp) would start running
        # wisp on shared templates. Preserve that opt-out per-field if templates
        # are ever populated in shared/.
        self.shared_reference_dir = str(layout_shared_reference_dir('nircam', root=campfire_root))
        self.wisp_dir = str(dir_for('nircam_wisp', Scope(), root=campfire_root))
        self.flats_dir = str(dir_for('nircam_flat', Scope(), root=campfire_root))

        # One flat directory per filter holds everything for that filter:
        # canonical exposures, drizzled mosaic tiles, split extensions,
        # diagnostics PDFs, and outlier/mosaic manifests.
        for filt in self.filters:
            os.makedirs(os.path.join(self.products_dir, filt), exist_ok=True)
        for d in [self.bad_pixel_dir, self.refcat_dir,
                  self.wisp_dir, self.mask_dir, self.flats_dir]:
            os.makedirs(d, exist_ok=True)

        log(f"Workspace ready for field '{self.name}' at {self.products_dir}")

    def filter_dir(self, filter_name, work=False):
        """Return the flat per-filter products directory for this field.

        ``$CAMPFIRE_ROOT/products/nircam/<field>/<filter>/`` holds every
        output for the (field, filter) pair: per-exposure FITS files,
        drizzled mosaic tiles, split extension files, diagnostic PDFs,
        and outlier/mosaic manifest JSON.

        With ``work=True`` return the combine *working-copy* directory
        ``products/nircam_work/<field>/<filter>/`` instead — the disposable,
        never-deployed tree the combine phase (bad_pixel / outlier / resample)
        mutates so the canonical per-exposure FITS stay frozen as the
        process-phase output. See :meth:`materialize_work`.
        """
        if self.products_dir is None:
            raise RuntimeError("setup_workspace() must be called first")
        scope = Scope(field=self.name, filt=filter_name)
        if work:
            return str(layout_nircam_work_dir(scope, root=self.campfire_root))
        return str(dir_for('nircam_exposure', scope, root=self.campfire_root))

    def _load_excluded_exposures(self):
        """Load reviewer exclusions from ``reference/<field>/exposures.json``.

        Materialized by ``campfire deploy nircam pull`` from the portal's
        ``review_status='excluded'`` flags (epic #261, N6). Returns
        ``{filter: [rootname]}``; a missing/blank file → ``{}`` (today's
        behavior); a malformed file → ``{}`` + a warning (never hard-fail a
        reduction over an advisory list).
        """
        if self.reference_dir is None:
            return {}
        path = os.path.join(self.reference_dir, 'exposures.json')
        if not os.path.exists(path):
            return {}
        try:
            with open(path) as f:
                doc = json.load(f)
            out = {filt: list(names or [])
                   for filt, names in (doc.get('excluded') or {}).items()}
            n = sum(len(v) for v in out.values())
            if n:
                log(f"Excluding {n} reviewer-flagged exposure(s) per {path}")
            return out
        except (ValueError, OSError) as e:
            log(f"Warning: could not read {path}: {e}; ignoring exclusions.")
            return {}

    def filter_files_to_epoch(self, files, epoch):
        """Restrict *files* to the named epoch's exposure subset.

        A no-op when *epoch* is falsy. An epoch is defined in fields.toml by an
        optional ``files`` glob list and/or an optional ``date_range`` — both are
        applied as an AND. File globs are matched against each path's basename
        (so this works on both the canonical and combine working-copy trees);
        the date range is matched against ``DATE-OBS`` (fail-open: a file whose
        date can't be read is kept). Raises ``ValueError`` for an unknown epoch.
        """
        if not epoch:
            return files
        if epoch not in self.epochs:
            raise ValueError(
                f"Field '{self.name}': unknown epoch '{epoch}'. "
                f"Available: {sorted(self.epochs)}"
            )
        edef = self.epochs[epoch]
        result = list(files)

        globs = edef.get('files')
        if globs:
            result = [f for f in result
                      if any(fnmatch(os.path.basename(f), pat + '*')
                             for pat in globs)]

        date_range = edef.get('date_range')
        if date_range:
            start, end = date_range
            kept = []
            for f in result:
                d = _read_date_obs(f)
                if d is None or start <= d <= end:  # fail open on unknown date
                    kept.append(f)
            result = kept

        return result

    def get_uncal_files(self, filter_name, skip=None, tiles=None, epoch=None):
        """Get uncal files from PID-organized raw directories.

        Globs across ``$CAMPFIRE_ROOT/raw/nircam/{PID}/{filter}/`` for every PID
        derived from this field's ``files`` patterns. The field-wide ``skip``
        list (from fields.toml) is always applied; caller-passed ``skip`` adds
        to it.

        ``tiles`` (optional) restricts the result to exposures overlapping the
        named tile(s), gated on each uncal's ``S_REGION`` footprint (raw uncals
        carry it before any WCS is assigned). This is what lets ``--tiles`` skip
        ``detector1`` on off-tile exposures. See
        :func:`geometry.filter_exposures_to_tiles`.
        """
        if self.raw_root is None:
            raise RuntimeError("setup_workspace() must be called first")
        result = []
        for pattern in self.files:
            pid = _extract_pid(pattern)
            stage_dir = os.path.join(self.raw_root, pid)
            full_pattern = os.path.join(stage_dir, filter_name, pattern + '*_uncal.fits')
            result.extend(glob(full_pattern))

        effective_skip = (list(self.skip)
                          + list(self.excluded_exposures.get(filter_name, []))
                          + list(skip or []))
        # Brace-expand any caller-passed skip patterns (field-level skip is
        # already expanded at load time).
        effective_skip = [p for raw in effective_skip for p in _expand_braces(raw)]
        if effective_skip:
            excluded = set()
            for exc_pattern in effective_skip:
                pid = _extract_pid(exc_pattern)
                if pid is None:
                    continue
                stage_dir = os.path.join(self.raw_root, pid)
                full_exc = os.path.join(stage_dir, filter_name, exc_pattern + '*_uncal.fits')
                excluded.update(glob(full_exc))
            result = [f for f in result if f not in excluded]

        result = sorted(result)
        if tiles:
            from campfire_pipeline.nircam.geometry import filter_exposures_to_tiles
            result = filter_exposures_to_tiles(self, result, tiles)
        if epoch:
            result = self.filter_files_to_epoch(result, epoch)
        return result

    def get_exposure_files(self, filter_name, skip=None, with_step=None,
                           status=None, work=False, tiles=None, epoch=None):
        """Get canonical per-exposure files from the filter's flat dir.

        These are the files that the process phase writes — one FITS file per
        exposure, named simply ``<rootname>.fits`` (no ``_rate`` / ``_cal`` /
        ``_jhat`` / ``_crf`` suffix).

        With ``work=True`` enumerate the combine working-copy tree
        (``products/nircam_work/…``) instead of the canonical tree — used by the
        combine steps, which operate on working copies (see
        :meth:`materialize_work`). The glob, skip list, and ``with_step`` CFP
        filtering are identical; only the directory differs.

        Mosaic outputs share the same directory but are named ``mosaic_*``,
        which the ``jw*`` field globs naturally exclude.

        Parameters
        ----------
        filter_name : str
            Filter (e.g. ``'f444w'``).
        skip : list of str, optional
            Glob patterns to exclude (matched against the same naming root
            used by the field's ``files`` patterns). Stacks on top of the
            field-wide ``skip`` list from fields.toml.
        with_step : str, optional
            If given, restrict the results to exposures whose primary header
            already records this CFP step keyword (e.g. ``'CFP_OUT'`` to
            select only outlier-detection-finished exposures for resample).
        status : StepStatus, optional
            If given, ``with_step`` filtering consults the cached status
            instead of reopening each FITS. Required for correctness only
            in the orchestrator (which marks fresh CFP_OUT keys onto the
            cache as outlier finishes); other callers can omit it and pay
            the per-file fits.open.
        work : bool, optional
            Enumerate the working-copy tree instead of the canonical tree.
        tiles : str or list of str, optional
            Restrict the result to exposures overlapping the named tile(s),
            gated on each file's ``S_REGION`` footprint via
            :func:`geometry.filter_exposures_to_tiles` (exposure-union: a whole
            dither is kept when any of its detectors overlaps). ``None`` (the
            default) applies no tile restriction.
        epoch : str, optional
            Restrict the result to the named epoch's exposure subset (see
            :meth:`filter_files_to_epoch`). ``None`` (the default) applies no
            epoch restriction. Composes with ``tiles`` as an AND.

        Returns
        -------
        list of str
            Sorted absolute paths.
        """
        filter_dir = self.filter_dir(filter_name, work=work)

        result = []
        for pattern in self.files:
            full_pattern = os.path.join(filter_dir, pattern + '*.fits')
            # Keep only canonical files — exclude transient sidecars and
            # diagnostic outputs that share the directory.
            for path in glob(full_pattern):
                base = os.path.basename(path)
                # Skip transient staging sidecars sharing the directory:
                # materialize_work stages as '<name>.fits.tmp'; atomic_save
                # stages as '<name>.tmp.fits' ('.tmp' before the extension so
                # the datamodel format dispatch still sees '.fits'). An
                # interrupted atomic_save leaves a truncated, WCS-less
                # '.tmp.fits' that the plain '*.fits' glob would otherwise pull
                # in as a phantom input (missing meta.wcs blows up outlier).
                if base.endswith('.tmp') or base.removesuffix('.fits').endswith('.tmp'):
                    continue
                if base.endswith('_jump.fits'):
                    continue
                result.append(path)

        effective_skip = (list(self.skip)
                          + list(self.excluded_exposures.get(filter_name, []))
                          + list(skip or []))
        effective_skip = [p for raw in effective_skip for p in _expand_braces(raw)]
        if effective_skip:
            excluded = set()
            for exc_pattern in effective_skip:
                full_exc = os.path.join(filter_dir, exc_pattern + '*.fits')
                excluded.update(glob(full_exc))
            result = [f for f in result if f not in excluded]

        if with_step is not None:
            if status is not None:
                result = [f for f in result if status.has(f, with_step)]
            else:
                from campfire_pipeline.common import cfp
                result = [f for f in result if cfp.has_step(f, with_step)]

        result = sorted(result)
        if tiles:
            from campfire_pipeline.nircam.geometry import filter_exposures_to_tiles
            result = filter_exposures_to_tiles(self, result, tiles)
        if epoch:
            result = self.filter_files_to_epoch(result, epoch)
        return result

    def get_exposure_path(self, rootname, filter_name, work=False):
        """Return the canonical path for a given exposure rootname.

        ``rootname`` is the JWST filename stem without any ``_<suffix>.fits``
        (e.g. ``'jw01727028001_04101_00003_nrcalong'``). With ``work=True``
        return the working-copy path under ``products/nircam_work/…``.
        """
        return os.path.join(self.filter_dir(filter_name, work=work),
                            f'{rootname}.fits')

    def materialize_work(self, filter_name, status=None, overwrite=False,
                         exclude_not_aligned=False):
        """Refresh the combine working copies for a filter; return their paths.

        The combine phase must not mutate the canonical per-exposure FITS (those
        are the frozen process-phase output that gets deployed to OSN). Instead
        it works on disposable copies under ``products/nircam_work/…``. This
        method brings that tree up to date:

        * A canonical exposure is (re)copied to its working path when the working
          copy is missing, ``overwrite`` is set, or the canonical is newer than
          the working copy (i.e. it was re-processed, or its manual mask changed
          — both rewrite the canonical). Up-to-date working copies are left
          untouched so their ``CFP_BPIX`` / ``CFP_OUT`` stamps survive and
          bad_pixel / outlier skip them — this is what keeps combine incremental.
        * Each freshly-copied working copy is *primed*: the canonical's CFMASK
          extension is fused into its DQ as ``DO_NOT_USE`` (so downstream steps'
          ``good_bits='~DO_NOT_USE'`` exclude masked pixels), and any stale
          ``CFP_BPIX`` / ``CFP_OUT`` provenance is cleared so combine re-derives
          those flags on the fresh copy. The canonical is never modified.

        The freshness test is an mtime comparison, which is reliable here because
        the canonical and its working copy live on the same filesystem (one
        clock). ``--overwrite`` forces a full re-copy.

        If a ``StepStatus`` is passed, the working paths are rescanned into it so
        the combine steps' skip checks reflect the working tree's current state.

        With ``exclude_not_aligned`` (set by the combine phase for align-enabled
        fields), exposures without a completed alignment — both ``CFP_ALGN =
        NOT_ALIGNED`` rejects and exposures with no ``CFP_ALGN`` at all — are
        quarantined from the working tree so they never drizzle or pollute the
        ensemble with a raw WCS. This is the single gate into the tree every
        combine step reads (see :meth:`_quarantine_not_aligned`).
        """
        import shutil

        canon = self.get_exposure_files(filter_name)          # frozen process outputs
        work_dir = self.filter_dir(filter_name, work=True)
        os.makedirs(work_dir, exist_ok=True)

        if exclude_not_aligned:
            canon = self._quarantine_not_aligned(canon, work_dir)

        work_paths = []
        for src in canon:
            dst = os.path.join(work_dir, os.path.basename(src))
            work_paths.append(dst)
            stale = (
                overwrite
                or not os.path.exists(dst)
                or os.path.getmtime(src) > os.path.getmtime(dst)
            )
            if not stale:
                continue
            tmp = dst + '.tmp'
            shutil.copyfile(src, tmp)      # content only → work copy gets a fresh mtime
            _prime_work_copy(tmp)          # fuse mask + clear combine stamps on the tmp
            os.replace(tmp, dst)           # atomic reveal of the primed copy

        if status is not None:
            status.rescan(work_paths)
        return sorted(work_paths)

    def _quarantine_not_aligned(self, canon, work_dir):
        """Drop exposures without a completed alignment from the combine input.

        For an align-enabled field, only exposures carrying a real align
        solution (``CFP_ALGN`` set to a ``dof=…`` value) may enter the ensemble.
        Two failure modes are excluded — both would otherwise drizzle with a raw
        (unaligned) WCS, doubling sources and defeating CR rejection — and each
        is surfaced with its own fix, because silently omitting data is never
        acceptable:

        * ``CFP_ALGN = NOT_ALIGNED`` — the align phase tried and could not tie
          the exposure to the reference. Retune ``[<field>.align]`` and re-run
          ``cfpipe nircam align`` (NOT_ALIGNED exposures are re-attempted), or
          force it in with ``combine --include-unaligned``.
        * **no ``CFP_ALGN`` at all** — align has not solved this exposure (it was
          never run, was run over a different filter/tile subset, or its worker
          died). Since an align-enabled field replaces ``jhat``, this exposure
          has no automatic astrometric correction (at most a manual
          ``wcs_shift`` offset). Run ``cfpipe nircam align``.

        This is the one gate into the working tree; we also delete any stale
        working copy so a lingering copy can't sneak into the work-tree glob the
        ensemble steps enumerate. Returns the kept canonical paths.
        """
        from campfire_pipeline.common import cfp

        kept, rejected, unstamped = [], [], []
        for src in canon:
            value = cfp.step_value(src, 'CFP_ALGN')
            if value is not None and value != cfp.NOT_ALIGNED:
                kept.append(src)                      # a real align solution
                continue
            (rejected if value == cfp.NOT_ALIGNED else unstamped).append(src)
            dst = os.path.join(work_dir, os.path.basename(src))
            if os.path.exists(dst):
                os.remove(dst)

        if rejected:
            log(f"combine: quarantined {len(rejected)} NOT_ALIGNED exposure(s) "
                f"from the ensemble — failed alignment. Retune [<field>.align] "
                f"and re-run `cfpipe nircam align`, or `combine "
                f"--include-unaligned`.")
        if unstamped:
            log(f"combine: quarantined {len(unstamped)} exposure(s) with NO "
                f"alignment stamp — align is enabled but has not solved them "
                f"(raw WCS). Run `cfpipe nircam align`, or `combine "
                f"--include-unaligned`.")
        return kept

    def get_tile_wcs(self, tile_name, pixel_scale='30mas'):
        """Get WCS parameters for a tile at the requested pixel scale.

        Tiles only need to declare their WCS at one pixel scale; other
        pixel scales are derived by rescaling ``naxis`` and ``crpix`` so
        the tile covers the same sky region. Explicit ``[<scale>mas]``
        subsections always override the derived values.

        ``fields.toml`` declares ``crpix`` in the natural FITS 1-indexed
        convention (where ``CRPIX = (NAXIS+1)/2`` lands at the array
        centre). The returned ``crpix`` is converted to **0-indexed**
        pixel coordinates so it can be passed straight to
        ``stcal.alignment.util.wcs_from_sregions`` /
        ``jwst.resample.resample_step`` (both document 0-based crpix).
        ``ResampleImage.update_fits_wcsinfo`` then adds 1 back when it
        writes the FITS-WCS keywords, restoring the user's intended
        1-indexed value in the output header.

        Parameters
        ----------
        tile_name : str
            Tile name (e.g. 'A1', 'B3').
        pixel_scale : str
            Pixel scale key (e.g. ``'30mas'`` or ``'60mas'``).

        Returns
        -------
        tuple
            (crpix, crval, shape, rotation). ``crpix`` is 0-indexed; all
            other values match the tile config directly.
        """
        if tile_name not in self.tiles:
            raise ValueError(
                f"Tile '{tile_name}' not found for field '{self.name}'. "
                f"Available: {list(self.tiles.keys())}"
            )
        tile = self.tiles[tile_name]

        m = _PIXEL_SCALE_RE.match(pixel_scale)
        if not m:
            raise ValueError(
                f"pixel_scale must look like 'NNmas' (got {pixel_scale!r})"
            )
        target_mas = int(m.group(1))

        if pixel_scale in tile and _is_pixel_scale_section(tile[pixel_scale]):
            crpix = list(tile[pixel_scale]['crpix'])
            shape = list(tile[pixel_scale]['naxis'])
        else:
            ref_mas, ref_section = self._reference_pixel_scale(tile_name, tile)
            ratio = ref_mas / target_mas
            ref_crpix = ref_section['crpix']
            ref_naxis = ref_section['naxis']
            crpix = [(c - 0.5) * ratio + 0.5 for c in ref_crpix]
            shape = [int(round(n * ratio)) for n in ref_naxis]

        # Convert FITS 1-indexed crpix (as written in fields.toml and used
        # by _compute_corners_from_wcs / astropy.wcs) to 0-indexed crpix
        # (as expected by stcal.wcs_from_sregions and jwst.resample).
        crpix = [c - 1.0 for c in crpix]

        rotation = tile.get('rotation', 0)
        crval = list(tile.get('tangent_point', list(self.tangent_point)))
        return crpix, crval, shape, rotation

    def _reference_pixel_scale(self, tile_name, tile):
        """Return ``(scale_mas, section_dict)`` for the first WCS subsection."""
        for key, value in tile.items():
            m = _PIXEL_SCALE_RE.match(key)
            if m and _is_pixel_scale_section(value):
                return int(m.group(1)), value
        raise ValueError(
            f"Tile '{tile_name}' on field '{self.name}' has no "
            f"`<scale>mas` subsection with `crpix` and `naxis`."
        )

    def fiducial_tile_set(self, pixel_scale='30mas'):
        """Ordered tile names forming the field-composite fiducial set (epic #337).

        The field-level ``fiducial_tiles = [...]`` declaration wins; absent that,
        any tile carrying a per-tile ``fiducial = true`` flag is included (in
        declaration order). Returns ``[]`` when neither is declared — the caller
        (``campfire fitsgl build``) decides whether that is an error.

        The set must co-grid to composite in FitsGL, so a set of two or more tiles
        is validated to share one tangent point (``crval``) and rotation; a
        mismatch (e.g. an off-grid PRIMER tile mistakenly included) raises
        ``ValueError``. Only the pipeline can see tile WCS, so the check lives
        here rather than in the deploy client.
        """
        if self.fiducial_tiles:
            names = list(self.fiducial_tiles)
        else:
            names = [name for name, t in self.tiles.items()
                     if isinstance(t, dict) and t.get('fiducial') is True]
        if len(names) <= 1:
            return names

        ref_crval = ref_rot = None
        for tname in names:
            _crpix, crval, _shape, rotation = self.get_tile_wcs(tname, pixel_scale)
            if ref_crval is None:
                ref_crval, ref_rot = crval, rotation
                continue
            if not (np.allclose(crval, ref_crval, atol=1e-6)
                    and np.isclose(rotation, ref_rot, atol=1e-6)):
                raise ValueError(
                    f"Field '{self.name}': fiducial tiles must share a tangent "
                    f"point and rotation to co-grid, but '{tname}' has "
                    f"crval={crval}, rotation={rotation} vs {ref_crval}, {ref_rot}. "
                    f"Off-grid tiles (e.g. PRIMER) need a standalone per-tile "
                    f"dataset, not the composite."
                )
        return names

    def get_tile_corners(self, tile_name):
        """Get sky corners for a tile.

        If the tile config provides explicit ``corners``, those are returned
        verbatim. Otherwise corners are derived from the tile's WCS
        (``crpix``/``naxis`` of the first pixel-scale subsection plus the
        tile or field tangent point and rotation).

        Parameters
        ----------
        tile_name : str
            Tile name (e.g. 'A1').

        Returns
        -------
        list
            List of [RA, Dec] corners (4 entries, FITS pixel order:
            (1,1), (nx,1), (nx,ny), (1,ny)).
        """
        if tile_name not in self.tiles:
            raise ValueError(
                f"Tile '{tile_name}' not found for field '{self.name}'. "
                f"Available: {list(self.tiles.keys())}"
            )
        tile = self.tiles[tile_name]
        if 'corners' in tile:
            return tile['corners']
        return self._compute_corners_from_wcs(tile_name, tile)

    def _compute_corners_from_wcs(self, tile_name, tile):
        """Build a TAN WCS from the tile config and return its sky corners.

        Picks the first ``<scale>mas`` subsection that defines ``crpix`` and
        ``naxis``. The polygon is sky-equivalent across pixel scales as
        long as ``naxis`` and ``crpix`` are defined consistently.
        """
        from astropy.wcs import WCS

        ref_mas, ref_section = self._reference_pixel_scale(tile_name, tile)
        pixel_scale_arcsec = ref_mas / 1000.0
        crpix = list(ref_section['crpix'])
        naxis = list(ref_section['naxis'])

        rotation = tile.get('rotation', 0)
        crval = list(tile.get('tangent_point', list(self.tangent_point)))

        scale_deg = pixel_scale_arcsec / 3600.0
        theta = np.radians(rotation)
        cos_t = np.cos(theta)
        sin_t = np.sin(theta)

        w = WCS(naxis=2)
        w.wcs.crpix = crpix
        w.wcs.crval = crval
        w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
        # North-up east-left at rotation=0; rotation interpreted as PA of
        # output +y measured east-of-north (CCW on sky), matching the
        # `rotation` argument passed to JWST resample.
        w.wcs.cd = np.array([
            [-scale_deg * cos_t,  scale_deg * sin_t],
            [ scale_deg * sin_t,  scale_deg * cos_t],
        ])

        nx, ny = naxis
        pix = np.array(
            [[1, 1], [nx, 1], [nx, ny], [1, ny]], dtype=float,
        )
        sky = w.all_pix2world(pix, 1)
        return sky.tolist()

    def is_sw_filter(self, filter_name):
        """Check if a filter is short-wavelength."""
        return filter_name.lower() in SW_FILTERS

    def is_lw_filter(self, filter_name):
        """Check if a filter is long-wavelength."""
        return filter_name.lower() in LW_FILTERS
