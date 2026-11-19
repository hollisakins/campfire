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
* reviewer input ``reference/<instrument>/<field>/``: ``masks/<filter>/<file
                 name>.reg`` (hand-drawn regions, ``steps/apply_masks.py``)
                 and ``exposures.json`` (exposures a reviewer excluded, dropped
                 like ``skip``). The NIRCam files, per instrument.

The process phase writes the canonical files; the outlier pass and the drizzle
work on copies, so the field-level outlier flags (``CFOUT``) never reach a
canonical file.
"""

import json
import os
import shutil
from dataclasses import dataclass, field as dc_field
from glob import glob
from typing import List, Optional

import toml

from campfire_layout import Scope, dir_for, imaging_work_dir, raw_dir, reference_dir
from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.hst.instruments import DETECTORS
from campfire_pipeline.nircam.field import Field, _expand_braces, parse_tiles

# First character of the rootnames of each instrument (HST Data Handbooks,
# "HST File Names").
ROOTNAME_LETTER = {'acswfc': 'j', 'wfc3ir': 'i'}

# The steps a field may override, [<field>.<instrument>.<step>].
STEP_TABLES = ('calibrate', 'trails', 'crreject', 'bkg', 'persist', 'preview',
               'align', 'outlier', 'resample')

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
    fields_file: Optional[str] = None   # the fields.toml this was loaded from

    # set by setup_workspace()
    campfire_root: Optional[str] = None
    raw_root: Optional[str] = None      # raw/<instrument>
    refcat_dir: Optional[str] = None
    reference_dir: Optional[str] = None  # reference/<instrument>/<field>
    mask_dir: Optional[str] = None       # .../masks: hand-drawn region files
    # Rootnames a reviewer excluded, per filter, from
    # reference/<instrument>/<field>/exposures.json; dropped like `skip`.
    excluded_exposures: dict = dc_field(default_factory=dict)

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
                   refcat=refcat or old or None, fields_file=str(fields_file))

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

    @property
    def tangent_point(self):
        """The field's tangent point (shared by its instruments)."""
        return self.grid.tangent_point if self.grid is not None else None

    def setup_workspace(self, campfire_root=None, create=True):
        """Resolve the directories of this instrument's part of the field.
        ``create=False`` creates none (a reader that only lists files)."""
        if campfire_root is None:
            from campfire_pipeline.config import _get_campfire_root
            campfire_root = _get_campfire_root()
        self.campfire_root = str(campfire_root)
        self.raw_root = str(raw_dir(self.instrument, root=campfire_root))
        # One reference catalog for every instrument of the field: it lives
        # where the NIRCam one does.
        self.refcat_dir = str(dir_for('nircam_astrom_cat', Scope(field=self.name),
                                      root=campfire_root))
        # What a reviewer decides about this instrument's exposures has the
        # NIRCam shape: reference/<instrument>/<field>/masks/<filter>/ holds
        # one region file per exposure, exposures.json the exclusions.
        scope = Scope(field=self.name)
        self.reference_dir = str(reference_dir(self.instrument, scope,
                                               root=campfire_root))
        self.mask_dir = str(dir_for(f'{self.instrument}_mask', scope,
                                    root=campfire_root))
        self.excluded_exposures = self._load_excluded_exposures()
        if not create:
            return
        for filt in self.filters:
            os.makedirs(self.filter_dir(filt), exist_ok=True)
        log(f"Workspace ready for field '{self.name}' ({self.instrument})")

    def _load_excluded_exposures(self):
        """Reviewer exclusions from ``reference/<instrument>/<field>/exposures.json``.

        The NIRCam file and meaning (``nircam/field.py``)::

            {"excluded": {"f814w": ["j8pu6eicq_flc", ...]}}

        A name is an exposure's file name without ``.fits``, or its bare
        rootname. Returns ``{filter: [rootname]}``. A missing file is no
        exclusions; an unreadable one is a warning and no exclusions, since a
        reduction must not fail over an advisory list.
        """
        path = os.path.join(self.reference_dir, 'exposures.json')
        if not os.path.exists(path):
            return {}
        try:
            with open(path) as f:
                doc = json.load(f)
            out = {str(filt).lower(): sorted({str(n).split('_')[0].strip().lower()
                                              for n in (names or [])})
                   for filt, names in (doc.get('excluded') or {}).items()}
        except (ValueError, OSError, AttributeError) as e:
            log(f'Warning: could not read {path}: {e}; ignoring exclusions.')
            return {}
        n = sum(len(v) for v in out.values())
        if n:
            log(f'Excluding {n} reviewer-flagged exposure(s) per {path}')
        return out

    def mask_file(self, exposure_file, filter_name):
        """Where the hand-drawn region file of an exposure is looked for:
        ``<mask_dir>/<filter>/<file name without .fits>.reg``."""
        base = os.path.basename(exposure_file).removesuffix('.fits')
        return os.path.join(self.mask_dir, filter_name, base + '.reg')

    @property
    def products_dir(self):
        """``products/<instrument>/<field>/``: the filter directories, and the
        field layout plot beside them (the NIRCam shape)."""
        if self.campfire_root is None:
            raise RuntimeError('setup_workspace() must be called first')
        return os.path.dirname(self.filter_dir(self.filters[0]))

    def expmap_metadata(self, path):
        """Exposure time and footprint of one calibrated exposure, for the
        field exposure maps (``imaging/expmap.py``).

        An HST file has no footprint keyword, so the outline of each chip is
        taken through the chip's own WCS, the same outline that decides which
        exposures overlap a tile (``steps/resample.chip_polygon``). The
        exposure time is ``EXPTIME``, the quantity the mosaics' exposure-time
        planes sum. An ACS/WFC exposure gives two outlines, one per chip.
        """
        from campfire_pipeline.hst.exposure import open_exposure
        from campfire_pipeline.hst.steps.resample import chip_polygon
        from campfire_pipeline.imaging.expmap import _ExposureMeta

        chips = open_exposure(path)
        outlines = []
        for chip in chips:
            ra, dec = chip_polygon(chip).exterior.coords.xy
            # shapely closes the ring; the region code wants it open
            outlines.append(([float(v) for v in ra[:-1]],
                             [float(v) for v in dec[:-1]]))
        if not outlines:
            return None
        return _ExposureMeta(path=str(path), rootname=chips[0].rootname,
                             xposure=float(chips[0].exptime),
                             ra=outlines[0][0], dec=outlines[0][1],
                             more=outlines[1:])

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

    def _select(self, directories, suffix, skip=None, filter_name=None):
        """Files matching the field's globs, less the skip globs and the
        reviewer exclusions of ``filter_name``, in the given directories. A
        glob is a rootname prefix: ``iczg*`` and ``iczg`` select the same
        files."""
        def matches(patterns):
            found = set()
            for d in directories:
                for p in patterns:
                    found.update(glob(os.path.join(d, p.rstrip('*') + '*' + suffix)))
            return found

        selected = matches(self.files)
        skip_patterns = list(self.skip) + [
            p.strip().lower() for raw in (skip or []) for p in _expand_braces(raw)]
        skip_patterns += list(self.excluded_exposures.get(filter_name, []))
        if skip_patterns:
            selected -= matches(skip_patterns)
        return sorted(selected)

    def get_raw_files(self, filter_name, skip=None):
        """RAW files of this filter that the field's globs select, in every
        proposal directory of ``raw/<instrument>/``."""
        if self.raw_root is None:
            raise RuntimeError('setup_workspace() must be called first')
        dirs = sorted(glob(os.path.join(self.raw_root, '*', filter_name)))
        return self._select(dirs, '_raw.fits', skip=skip, filter_name=filter_name)

    def get_exposure_files(self, filter_name, skip=None, work=False):
        """Canonical calibrated exposures of this filter (or, with ``work``,
        their combine working copies)."""
        return self._select([self.filter_dir(filter_name, work=work)],
                            self.canonical_suffix, skip=skip,
                            filter_name=filter_name)

    def canonical_path(self, raw_file, filter_name):
        """Where the canonical exposure of a RAW file goes."""
        rootname = os.path.basename(raw_file).split('_')[0]
        return os.path.join(self.filter_dir(filter_name),
                            rootname + self.canonical_suffix)

    def combine_ready(self, exposure_file, include_unaligned=False):
        """Whether a canonical exposure may go into a mosaic, and why not.

        What keeps an exposure out: no solved alignment (it would be drizzled
        where it is not, NIRCam's rule), and, on ACS/WFC, no cosmic-ray flags
        (``crreject`` has not run on it; a CCD exposure without them is
        covered in hits), and no background subtraction (``bkg`` has not run
        on it). Returns ``(ready, reason)``. The caller says so
        loudly: nothing is left out of a mosaic without a message.
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
        if 'CFP_BKG' not in steps:
            # its background is still in the pixels: drizzled as it is, it
            # would put its own level and pattern into the mosaic
            return False, 'background not subtracted yet (bkg has not run)'
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
