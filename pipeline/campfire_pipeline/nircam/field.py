"""
Field dataclass: NIRCam field configuration and workspace management.

Analogous to nirspec/observation.py but organized around sky fields
(file globs + filters) rather than MSA observations.

Raw uncal layout is PID-organized: ``$CAMPFIRE_ROOT/raw/nircam/{PID}/{filter}/``.
PIDs are derived from the leading ``jwNNNNN`` of each ``files`` glob, so a field
that spans multiple programs naturally pulls from multiple PID directories.
"""

import os
import re
from glob import glob
from dataclasses import dataclass, field
from typing import List, Optional

import toml
import numpy as np

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


@dataclass
class Field:
    name: str
    filters: List[str]
    files: List[str]           # glob patterns, e.g. ['jw01727*', 'jw05893*']
    tangent_point: tuple       # (RA, Dec)
    tiles: dict                # tile WCS definitions
    step_overrides: dict = field(default_factory=dict)
    skip: List[str] = field(default_factory=list)  # field-wide exclude globs

    # Populated by setup_workspace()
    campfire_root: Optional[str] = None
    raw_root: Optional[str] = None  # $CAMPFIRE_ROOT/raw/nircam (parent of PID dirs)
    products_dir: Optional[str] = None
    reference_dir: Optional[str] = None

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
            'detector1', 'persistence', 'wisp', 'striping',
            'image2', 'edge', 'sky', 'variance', 'jhat',
            'apply_mask', 'bad_pixel', 'outlier', 'resample',
        }

        # Parse tile WCS definitions. A tile is any non-reserved sub-table
        # that either declares explicit sky `corners` (legacy / override) or
        # provides at least one `<scale>mas` subsection with `crpix`+`naxis`
        # — in the latter case corners are derived on demand from the WCS.
        tiles = {}
        reserved_keys = ({'filters', 'files', 'skip', 'tangent_point'} | known_steps)
        for key, value in fc.items():
            if key in reserved_keys:
                continue
            if not isinstance(value, dict):
                continue
            if 'corners' in value or _tile_has_wcs_subsection(value):
                tiles[key] = value

        # Capture per-field step config overrides (flat layout)
        step_overrides = {}
        for key in known_steps:
            if key in fc and isinstance(fc[key], dict):
                step_overrides[key] = fc[key]

        return cls(
            name=name,
            filters=filters,
            files=file_patterns,
            tangent_point=tangent_point,
            tiles=tiles,
            step_overrides=step_overrides,
            skip=skip_patterns,
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

        self.campfire_root = campfire_root
        self.raw_root = os.path.join(campfire_root, 'raw', 'nircam')
        self.products_dir = os.path.join(campfire_root, 'products', 'nircam', self.name)
        self.reference_dir = os.path.join(campfire_root, 'reference', 'nircam', self.name)

        self.bad_pixel_dir = os.path.join(self.reference_dir, 'bad_pixels')
        self.refcat_dir = os.path.join(self.reference_dir, 'astrom_cats')
        self.wisp_dir = os.path.join(self.reference_dir, 'wisps')
        self.mask_dir = os.path.join(self.reference_dir, 'masks')
        self.flats_dir = os.path.join(self.reference_dir, 'flats')

        # One flat directory per filter holds everything for that filter:
        # canonical exposures, drizzled mosaic tiles, split extensions,
        # diagnostics PDFs, and outlier/mosaic manifests.
        for filt in self.filters:
            os.makedirs(os.path.join(self.products_dir, filt), exist_ok=True)
        for d in [self.bad_pixel_dir, self.refcat_dir,
                  self.wisp_dir, self.mask_dir, self.flats_dir]:
            os.makedirs(d, exist_ok=True)

        log(f"Workspace ready for field '{self.name}' at {self.products_dir}")

    def filter_dir(self, filter_name):
        """Return the flat per-filter products directory for this field.

        ``$CAMPFIRE_ROOT/products/nircam/<field>/<filter>/`` holds every
        output for the (field, filter) pair: per-exposure FITS files,
        drizzled mosaic tiles, split extension files, diagnostic PDFs,
        and outlier/mosaic manifest JSON.
        """
        if self.products_dir is None:
            raise RuntimeError("setup_workspace() must be called first")
        return os.path.join(self.products_dir, filter_name)

    def get_uncal_files(self, filter_name, skip=None):
        """Get uncal files from PID-organized raw directories.

        Globs across ``$CAMPFIRE_ROOT/raw/nircam/{PID}/{filter}/`` for every PID
        derived from this field's ``files`` patterns. The field-wide ``skip``
        list (from fields.toml) is always applied; caller-passed ``skip`` adds
        to it.
        """
        if self.raw_root is None:
            raise RuntimeError("setup_workspace() must be called first")
        result = []
        for pattern in self.files:
            pid = _extract_pid(pattern)
            stage_dir = os.path.join(self.raw_root, pid)
            full_pattern = os.path.join(stage_dir, filter_name, pattern + '*_uncal.fits')
            result.extend(glob(full_pattern))

        effective_skip = list(self.skip) + list(skip or [])
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

        return sorted(result)

    def get_exposure_files(self, filter_name, skip=None, with_step=None,
                           status=None):
        """Get canonical per-exposure files from the filter's flat dir.

        These are the files that the new pipeline mutates in place — one FITS
        file per exposure, named simply ``<rootname>.fits`` (no ``_rate`` /
        ``_cal`` / ``_jhat`` / ``_crf`` suffix).

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

        Returns
        -------
        list of str
            Sorted absolute paths.
        """
        filter_dir = self.filter_dir(filter_name)

        result = []
        for pattern in self.files:
            full_pattern = os.path.join(filter_dir, pattern + '*.fits')
            # Keep only canonical files — exclude transient sidecars and
            # diagnostic outputs that share the directory.
            for path in glob(full_pattern):
                base = os.path.basename(path)
                if base.endswith('.tmp'):
                    continue
                if base.endswith('_jump.fits'):
                    continue
                result.append(path)

        effective_skip = list(self.skip) + list(skip or [])
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

        return sorted(result)

    def get_exposure_path(self, rootname, filter_name):
        """Return the canonical path for a given exposure rootname.

        ``rootname`` is the JWST filename stem without any ``_<suffix>.fits``
        (e.g. ``'jw01727028001_04101_00003_nrcalong'``).
        """
        return os.path.join(self.filter_dir(filter_name), f'{rootname}.fits')

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

    def get_excluded_exposures(self):
        """Read excluded exposure names from the contract file if present.

        The contract file is written by ``campfire deploy nircam pull`` and lives at
        ``$CAMPFIRE_ROOT/reference/nircam/{field}/exposures.json``.

        Returns
        -------
        list of str
            Exposure basenames to skip (empty list if no contract file).
        """
        import json
        contract_path = os.path.join(self.reference_dir, 'exposures.json')
        if not os.path.exists(contract_path):
            return []
        try:
            with open(contract_path) as f:
                contract = json.load(f)
            return [
                name for name, info in contract.get('exposures', {}).items()
                if info.get('review_status') == 'excluded'
            ]
        except (json.JSONDecodeError, KeyError):
            return []

    def is_sw_filter(self, filter_name):
        """Check if a filter is short-wavelength."""
        return filter_name.lower() in SW_FILTERS

    def is_lw_filter(self, filter_name):
        """Check if a filter is long-wavelength."""
        return filter_name.lower() in LW_FILTERS
