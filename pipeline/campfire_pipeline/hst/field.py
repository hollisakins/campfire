"""HstField: one HST instrument's part of a field in ``fields.toml``.

A field is a patch of sky with tiles, a tangent point and one reference
catalog; those belong to the field and every instrument shares them. What an
HST instrument adds is its own table, named after the instrument::

    [pg004]
    tangent_point = [...]
    refcat = "pg004_f444w.ecsv"
    [pg004.A1] ...                      # tiles, shared with NIRCam

    [pg004.wfc3ir]
    filters = ["f105w", "f125w", "f140w", "f160w"]
    files = ["iczg*", "iepl*"]          # rootname globs: this is the selection
    skip = ["iepl72i1q"]
    [pg004.wfc3ir.outlier]              # step overrides for this instrument
    protect_radius = 0.5

``files`` is the whole statement of which exposures go into the reduction, as
for NIRCam (Hollis, 2026-11-15: "the config file tells you exactly what goes
into the reduction"). ``cfpipe query --field <name> --instrument wfc3ir``
prints the globs the archive holds for the field; nothing is selected at run
time by sky position.

An HST rootname is ``ipppssoot``: the instrument letter (``j`` ACS, ``i``
WFC3), a three-character program code, the visit, the exposure. It carries no
proposal number, so a glob is matched in every proposal directory of
``raw/<instrument>/``; ``iczg*`` selects one program, ``iczgo2*`` one visit.

Directories (the NIRCam shape, layout contract):

* raw            ``raw/<instrument>/<proposal>/<filter>/<rootname>_raw.fits``
* canonical      ``products/<instrument>/<field>/<filter>/<rootname>_flc.fits``
                 (ACS/WFC) or ``..._flt.fits`` (WFC3/IR); mosaics in the same
                 directory
* working copies ``products/<instrument>_work/<field>/<filter>/``

The process phase writes the canonical files; the combine phase works on
copies, so the field-level outlier flags (``CFOUT``) never reach a canonical
file.
"""

import os
import shutil
from dataclasses import dataclass, field as dc_field
from glob import glob
from typing import List, Optional

import toml

from campfire_layout import Scope, dir_for, imaging_work_dir, raw_dir
from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.hst.instruments import DETECTORS
from campfire_pipeline.nircam.field import Field, _expand_braces, parse_tiles

# First character of the rootnames of each instrument (HST Data Handbooks,
# "HST File Names").
ROOTNAME_LETTER = {'acswfc': 'j', 'wfc3ir': 'i'}

# The steps a field may override, [<field>.<instrument>.<step>].
STEP_TABLES = ('calibrate', 'sky', 'crreject', 'align', 'outlier', 'resample')

_KEYS = {'filters', 'files', 'skip'} | set(STEP_TABLES)


def _globs(field_name, instrument, key, value):
    """Normalize a ``files`` / ``skip`` entry to a list of rootname globs."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise ValueError(
            f"Field '{field_name}': [{field_name}.{instrument}].{key} must be "
            f"a list of rootname globs")
    patterns = sorted({p.strip().lower() for raw in value
                       for p in _expand_braces(raw)})
    letter = ROOTNAME_LETTER[instrument]
    bad = [p for p in patterns
           if not p.startswith(letter) or len(p.rstrip('*')) < 4]
    if bad:
        raise ValueError(
            f"Field '{field_name}': [{field_name}.{instrument}].{key} entries "
            f"are HST rootname globs. A rootname of this instrument starts "
            f"with '{letter}' followed by the three-character program code "
            f"(e.g. '{letter}czg*' for a program, '{letter}czgo2*' for one "
            f"visit), so that the entry names what it selects. Offending: "
            f"{bad}. `cfpipe query --field {field_name} --instrument "
            f"{instrument}` prints the globs the archive holds.")
    return patterns


@dataclass
class HstField:
    """One HST instrument's exposures and settings for a field."""

    name: str
    instrument: str                     # 'acswfc' or 'wfc3ir'
    filters: List[str]
    files: List[str]                    # rootname globs
    grid: Field                         # tiles and tangent point of the field
    skip: List[str] = dc_field(default_factory=list)
    step_overrides: dict = dc_field(default_factory=dict)
    refcat: Optional[str] = None        # field-level reference catalog name

    # set by setup_workspace()
    campfire_root: Optional[str] = None
    raw_root: Optional[str] = None      # raw/<instrument>
    refcat_dir: Optional[str] = None

    @classmethod
    def load(cls, name, instrument, fields_file=None):
        """Read ``[<name>]`` and ``[<name>.<instrument>]`` from fields.toml."""
        from campfire_pipeline.config import resolve_fields_file

        if instrument not in DETECTORS:
            raise ValueError(
                f'{instrument!r} is not an HST instrument; expected one of '
                f'{", ".join(sorted(DETECTORS))}')
        fields_file = resolve_fields_file(fields_file)
        with open(fields_file, 'r') as f:
            fields_config = toml.load(f)
        if name not in fields_config:
            raise ValueError(
                f"Field '{name}' not found in {fields_file}. "
                f"Available: {list(fields_config)}")
        fc = fields_config[name]

        block = fc.get(instrument)
        if not isinstance(block, dict):
            raise ValueError(
                f"Field '{name}' has no [{name}.{instrument}] table in "
                f"{fields_file}. It names the filters and the exposures "
                f"(rootname globs) of this instrument:\n\n"
                f"    [{name}.{instrument}]\n"
                f"    filters = [\"...\"]\n"
                f"    files = [\"{ROOTNAME_LETTER[instrument]}...*\"]\n\n"
                f"`cfpipe query --field {name} --instrument {instrument}` "
                f"prints the globs the archive holds for the field.")
        unknown = sorted(set(block) - _KEYS)
        if unknown:
            raise ValueError(
                f"Field '{name}': unknown key(s) {unknown} in "
                f"[{name}.{instrument}]. Allowed: {sorted(_KEYS)}")

        filters = block.get('filters')
        if isinstance(filters, str):
            filters = [filters]
        if not filters or not all(isinstance(f, str) for f in filters):
            raise ValueError(
                f"Field '{name}': [{name}.{instrument}].filters must list at "
                f"least one filter")
        filters = [f.strip().lower() for f in filters]
        files = _globs(name, instrument, 'files', block.get('files'))
        if not files:
            raise ValueError(
                f"Field '{name}': [{name}.{instrument}].files is empty; it is "
                f"the list of rootname globs that go into the reduction")
        skip = _globs(name, instrument, 'skip', block.get('skip'))

        overrides = {}
        for step in STEP_TABLES:
            if step in block:
                if not isinstance(block[step], dict):
                    raise ValueError(
                        f"Field '{name}': [{name}.{instrument}.{step}] must "
                        f"be a table")
                overrides[step] = block[step]

        if 'tangent_point' not in fc:
            raise ValueError(f"Field '{name}' has no tangent_point")
        # The tiles and the tangent point are the field's. The Field object
        # that holds them needs no NIRCam exposures; it is used for its tile
        # geometry only.
        grid = Field(name=name, filters=[], files=[],
                     tangent_point=tuple(fc['tangent_point']),
                     tiles=parse_tiles(fc))

        refcat = fc.get('refcat')
        old = (fc.get('align') or {}).get('refcat') \
            if isinstance(fc.get('align'), dict) else None
        if refcat and old and refcat != old:
            raise ValueError(
                f"field '{name}' names two reference catalogs: refcat = "
                f"{refcat!r} under [{name}] and {old!r} under [{name}.align]. "
                f"Keep the first.")
        return cls(name=name, instrument=instrument, filters=filters,
                   files=files, grid=grid, skip=skip, step_overrides=overrides,
                   refcat=refcat or old or None)

    # -- geometry: the field's, shared with NIRCam ---------------------------

    @property
    def tiles(self):
        return self.grid.tiles

    def get_tile_wcs(self, tile_name, pixel_scale='30mas'):
        return self.grid.get_tile_wcs(tile_name, pixel_scale=pixel_scale)

    def get_tile_corners(self, tile_name):
        return self.grid.get_tile_corners(tile_name)

    # -- workspace -----------------------------------------------------------

    @property
    def detector(self):
        return DETECTORS[self.instrument]

    @property
    def canonical_suffix(self):
        """``_flc.fits`` (ACS/WFC) or ``_flt.fits`` (WFC3/IR)."""
        return f'_{self.detector.calibrated_suffix}.fits'

    def setup_workspace(self, campfire_root=None):
        if campfire_root is None:
            from campfire_pipeline.config import _get_campfire_root
            campfire_root = _get_campfire_root()
        self.campfire_root = str(campfire_root)
        self.raw_root = str(raw_dir(self.instrument, root=campfire_root))
        # One reference catalog for every instrument of the field: it lives
        # where the NIRCam one does.
        self.refcat_dir = str(dir_for('nircam_astrom_cat', Scope(field=self.name),
                                      root=campfire_root))
        for filt in self.filters:
            os.makedirs(self.filter_dir(filt), exist_ok=True)
        log(f"Workspace ready for field '{self.name}' ({self.instrument})")

    def filter_dir(self, filter_name, work=False):
        if self.campfire_root is None:
            raise RuntimeError('setup_workspace() must be called first')
        scope = Scope(field=self.name, filt=filter_name)
        if work:
            return str(imaging_work_dir(self.instrument, scope,
                                        root=self.campfire_root))
        return str(dir_for(f'{self.instrument}_exposure', scope,
                           root=self.campfire_root))

    def refcat_path(self):
        """Path of the field's reference catalog, or ``None`` when the field
        names none."""
        if not self.refcat:
            return None
        return os.path.join(self.refcat_dir, self.refcat)

    # -- exposures -----------------------------------------------------------

    def _select(self, directories, suffix, skip=None):
        """Files matching the field's globs, less the skip globs, in the given
        directories. A glob is a rootname prefix: ``iczg*`` and ``iczg`` select
        the same files."""
        def matches(patterns):
            found = set()
            for d in directories:
                for p in patterns:
                    found.update(glob(os.path.join(d, p.rstrip('*') + '*' + suffix)))
            return found

        selected = matches(self.files)
        skip_patterns = list(self.skip) + [
            p.strip().lower() for raw in (skip or []) for p in _expand_braces(raw)]
        if skip_patterns:
            selected -= matches(skip_patterns)
        return sorted(selected)

    def get_raw_files(self, filter_name, skip=None):
        """RAW files of this filter that the field's globs select, in every
        proposal directory of ``raw/<instrument>/``."""
        if self.raw_root is None:
            raise RuntimeError('setup_workspace() must be called first')
        dirs = sorted(glob(os.path.join(self.raw_root, '*', filter_name)))
        return self._select(dirs, '_raw.fits', skip=skip)

    def get_exposure_files(self, filter_name, skip=None, work=False):
        """Canonical calibrated exposures of this filter (or, with ``work``,
        their combine working copies)."""
        return self._select([self.filter_dir(filter_name, work=work)],
                            self.canonical_suffix, skip=skip)

    def canonical_path(self, raw_file, filter_name):
        """Where the canonical exposure of a RAW file goes."""
        rootname = os.path.basename(raw_file).split('_')[0]
        return os.path.join(self.filter_dir(filter_name),
                            rootname + self.canonical_suffix)

    def combine_ready(self, exposure_file, include_unaligned=False):
        """Whether a canonical exposure may go into a mosaic, and why not.

        Two things keep an exposure out: no solved alignment (it would be
        drizzled where it is not), and, on ACS/WFC, no cosmic-ray flags
        (``crreject`` has not run on it). Returns ``(ready, reason)``.
        """
        steps = cfp.get_steps(exposure_file, keyset=cfp.HST)
        algn = steps.get('CFP_ALGN')
        if not include_unaligned:
            if algn is None:
                return False, 'not aligned yet'
            if algn == cfp.NOT_ALIGNED:
                return False, 'alignment failed (NOT_ALIGNED)'
        if self.instrument == 'acswfc' and 'CFP_CR' not in steps:
            return False, 'no cosmic-ray flags yet (crreject has not run)'
        return True, ''

    def materialize_work(self, filter_name, overwrite=False,
                         include_unaligned=False):
        """Bring the combine working copies of a filter up to date.

        A canonical exposure that is ready for a mosaic (:meth:`combine_ready`)
        is copied when its working copy is missing or older than it, or with
        ``overwrite``. A fresh copy carries no field-level outlier flags, so
        the outlier pass redoes whatever group it belongs to. Working copies
        of exposures that are no longer selected, or no longer ready, are
        removed. Returns ``(working paths, held back)`` with ``held back`` a
        list of ``(canonical path, reason)``.
        """
        work_dir = self.filter_dir(filter_name, work=True)
        os.makedirs(work_dir, exist_ok=True)
        paths, held = [], []
        for src in self.get_exposure_files(filter_name):
            ready, reason = self.combine_ready(src, include_unaligned)
            dst = os.path.join(work_dir, os.path.basename(src))
            if not ready:
                held.append((src, reason))
                continue
            paths.append(dst)
            if (overwrite or not os.path.exists(dst)
                    or os.path.getmtime(src) > os.path.getmtime(dst)):
                tmp = dst + '.tmp'
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
        keep = set(paths)
        for stale in glob(os.path.join(work_dir, '*' + self.canonical_suffix)):
            if stale not in keep:
                os.remove(stale)
        return sorted(paths), held
