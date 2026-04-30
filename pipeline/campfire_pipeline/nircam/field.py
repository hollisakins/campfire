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


def _extract_pid(pattern):
    """Pull the 5-digit PID out of a JWST file glob (e.g. 'jw01727*' → '01727')."""
    m = _PID_RE.match(pattern)
    return m.group(1) if m else None


@dataclass
class Field:
    name: str
    filters: List[str]
    files: List[str]           # glob patterns, e.g. ['jw01727*', 'jw05893*']
    tangent_point: tuple       # (RA, Dec)
    tiles: dict                # tile WCS definitions
    stage_overrides: dict = field(default_factory=dict)

    # Populated by setup_workspace()
    campfire_root: Optional[str] = None
    raw_root: Optional[str] = None  # $CAMPFIRE_ROOT/raw/nircam (parent of PID dirs)
    products_dir: Optional[str] = None
    reference_dir: Optional[str] = None
    stage1_dir: Optional[str] = None
    stage2_dir: Optional[str] = None
    stage3_dir: Optional[str] = None
    mosaic_dir: Optional[str] = None

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
        file_patterns = list(np.unique(file_patterns))

        # Validate: every pattern must start with jwNNNNN so we can locate
        # the PID-organized raw directory.
        bad = [p for p in file_patterns if _extract_pid(p) is None]
        if bad:
            raise ValueError(
                f"Field '{name}': every entry in `files` must start with "
                f"'jwNNNNN' (5-digit PID). Offending patterns: {bad}"
            )

        tangent_point = tuple(fc['tangent_point'])

        # Parse tile WCS definitions
        tiles = {}
        reserved_keys = {'filters', 'files', 'tangent_point',
                         'stage1', 'stage2', 'stage3'}
        for key, value in fc.items():
            if key in reserved_keys:
                continue
            if isinstance(value, dict) and 'corners' in value:
                tiles[key] = value

        # Capture per-field stage config overrides
        stage_overrides = {}
        for key in ['stage1', 'stage2', 'stage3']:
            if key in fc and isinstance(fc[key], dict):
                stage_overrides[key] = fc[key]

        return cls(
            name=name,
            filters=filters,
            files=file_patterns,
            tangent_point=tangent_point,
            tiles=tiles,
            stage_overrides=stage_overrides,
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

        self.stage1_dir = os.path.join(self.products_dir, 'stage1')
        self.stage2_dir = os.path.join(self.products_dir, 'stage2')
        self.stage3_dir = os.path.join(self.products_dir, 'stage3')
        self.mosaic_dir = os.path.join(self.products_dir, 'mosaics')

        self.bad_pixel_dir = os.path.join(self.reference_dir, 'bad_pixels')
        self.refcat_dir = os.path.join(self.reference_dir, 'astrom_cats')
        self.wisp_dir = os.path.join(self.reference_dir, 'wisps')
        self.mask_dir = os.path.join(self.reference_dir, 'masks')
        self.flats_dir = os.path.join(self.reference_dir, 'flats')

        # Create directories
        for d in [self.stage1_dir, self.stage2_dir, self.stage3_dir,
                  self.mosaic_dir, self.bad_pixel_dir, self.refcat_dir,
                  self.wisp_dir, self.mask_dir, self.flats_dir]:
            os.makedirs(d, exist_ok=True)

        log(f"Workspace ready for field '{self.name}' at {self.products_dir}")

    def get_files(self, stage_dir, filter_name, suffix, skip=None):
        """Glob for files matching this field's patterns in a stage/filter directory.

        Parameters
        ----------
        stage_dir : str
            Base stage directory (e.g. self.stage1_dir).
        filter_name : str
            Filter subdirectory name (e.g. 'f444w').
        suffix : str
            Glob suffix (e.g. '*_rate.fits').
        skip : list of str, optional
            Glob patterns to exclude from results.

        Returns
        -------
        list of str
            Sorted list of matching file paths.
        """
        result = []
        for pattern in self.files:
            full_pattern = os.path.join(stage_dir, filter_name, pattern + suffix)
            result.extend(glob(full_pattern))

        if skip:
            excluded = set()
            for exc_pattern in skip:
                full_exc = os.path.join(stage_dir, filter_name, exc_pattern + suffix)
                excluded.update(glob(full_exc))
            result = [f for f in result if f not in excluded]

        return sorted(result)

    def get_uncal_files(self, filter_name, skip=None):
        """Get uncal files from PID-organized raw directories.

        Globs across ``$CAMPFIRE_ROOT/raw/nircam/{PID}/{filter}/`` for every PID
        derived from this field's ``files`` patterns.
        """
        if self.raw_root is None:
            raise RuntimeError("setup_workspace() must be called first")
        result = []
        for pattern in self.files:
            pid = _extract_pid(pattern)
            stage_dir = os.path.join(self.raw_root, pid)
            full_pattern = os.path.join(stage_dir, filter_name, pattern + '*_uncal.fits')
            result.extend(glob(full_pattern))

        if skip:
            excluded = set()
            for exc_pattern in skip:
                pid = _extract_pid(exc_pattern)
                if pid is None:
                    continue
                stage_dir = os.path.join(self.raw_root, pid)
                full_exc = os.path.join(stage_dir, filter_name, exc_pattern + '*_uncal.fits')
                excluded.update(glob(full_exc))
            result = [f for f in result if f not in excluded]

        return sorted(result)

    def get_rate_files(self, filter_name, skip=None):
        """Get rate files from stage1 products."""
        return self.get_files(self.stage1_dir, filter_name, '*_rate.fits', skip=skip)

    def get_cal_files(self, filter_name, skip=None):
        """Get cal files from stage2 products."""
        return self.get_files(self.stage2_dir, filter_name, '*_cal.fits', skip=skip)

    def get_jhat_files(self, filter_name, skip=None):
        """Get jhat files from stage3 products."""
        return self.get_files(self.stage3_dir, filter_name, '*_jhat.fits', skip=skip)

    def get_all_jhat_files(self, filter_name, skip=None):
        """Get all jhat files (any prefix) from stage3 products."""
        result = []
        full_pattern = os.path.join(self.stage3_dir, filter_name, '*_jhat.fits')
        result.extend(glob(full_pattern))
        if skip:
            excluded = set()
            for exc_pattern in skip:
                full_exc = os.path.join(self.stage3_dir, filter_name,
                                        exc_pattern + '*_jhat.fits')
                excluded.update(glob(full_exc))
            result = [f for f in result if f not in excluded]
        return sorted(result)

    def get_crf_files(self, filter_name, skip=None):
        """Get crf files from stage3 products."""
        return self.get_files(self.stage3_dir, filter_name, '*_crf.fits', skip=skip)

    def get_tile_wcs(self, tile_name, pixel_scale='30mas'):
        """Get WCS parameters for a tile.

        Parameters
        ----------
        tile_name : str
            Tile name (e.g. 'A1', 'B3').
        pixel_scale : str
            Pixel scale key ('30mas' or '60mas').

        Returns
        -------
        tuple
            (crpix, crval, shape, rotation) where crval uses tile-specific
            tangent point if available, otherwise the field tangent point.
        """
        if tile_name not in self.tiles:
            raise ValueError(
                f"Tile '{tile_name}' not found for field '{self.name}'. "
                f"Available: {list(self.tiles.keys())}"
            )
        tile = self.tiles[tile_name]
        crpix = tile[pixel_scale]['crpix']
        shape = tile[pixel_scale]['naxis']
        rotation = tile['rotation']
        crval = tile.get('tangent_point', list(self.tangent_point))
        return crpix, crval, shape, rotation

    def get_tile_corners(self, tile_name):
        """Get sky corners for a tile.

        Parameters
        ----------
        tile_name : str
            Tile name (e.g. 'A1').

        Returns
        -------
        list
            List of [RA, Dec] corners.
        """
        if tile_name not in self.tiles:
            raise ValueError(
                f"Tile '{tile_name}' not found for field '{self.name}'. "
                f"Available: {list(self.tiles.keys())}"
            )
        return self.tiles[tile_name]['corners']

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
