"""
NIRCam mosaic tile generation for CAMPFIRE map viewer.

Reprojects input FITS mosaics to a North-up grid and generates
256x256 PNG tile pyramids in z/x/y structure for Leaflet consumption.

Key design: tiles are generated one at a time at max zoom by reprojecting
only the overlapping input files onto each 256x256 tile. Lower zoom levels
are built by combining 4 child tiles (FITSMap approach). This avoids ever
allocating the full mosaic in memory.

Usage:
    Called from campfire.deploy.tiles orchestration layer via ``campfire deploy tiles``.

Dependencies:
    astropy, numpy, reproject, Pillow
"""

import gc
import json
import logging
import math
import shutil
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.utils.exceptions import AstropyWarning
from astropy.wcs import WCS
from PIL import Image

# Silence noisy warnings from JWST FITS headers and reproject
warnings.filterwarnings('ignore', category=AstropyWarning)
logging.getLogger('reproject').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

try:
    import tomllib
except ImportError:
    import tomli as tomllib

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable


# ============================================
# Data Classes
# ============================================

@dataclass
class TileConfig:
    """Configuration for a single field/filter tile generation run."""
    field: str
    filter_name: str
    input_files: list[Path]
    output_dir: Path
    output_pixel_scale_arcsec: float = 0.06
    tile_size: int = 256
    stretch_type: str = "asinh"
    min_percentile: float = 1.0
    max_percentile: float = 99.5
    mask_wht: bool = False


@dataclass
class OutputGrid:
    """Defines the North-up output WCS grid for reprojection."""
    crpix1: float
    crpix2: float
    crval1: float   # RA center (degrees)
    crval2: float   # Dec center (degrees)
    cd1_1: float    # -pixel_scale_deg (RA direction, negative)
    cd2_2: float    # +pixel_scale_deg (Dec direction, positive)
    naxis1: int     # width in pixels
    naxis2: int     # height in pixels

    def to_wcs(self) -> WCS:
        """Convert to astropy WCS object."""
        w = WCS(naxis=2)
        w.wcs.crpix = [self.crpix1, self.crpix2]
        w.wcs.crval = [self.crval1, self.crval2]
        w.wcs.cd = [[self.cd1_1, 0.0], [0.0, self.cd2_2]]
        w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
        w.pixel_shape = (self.naxis2, self.naxis1)
        return w

    def to_header(self) -> fits.Header:
        """Convert to FITS header for reproject."""
        header = self.to_wcs().to_header()
        header['NAXIS'] = 2
        header['NAXIS1'] = self.naxis1
        header['NAXIS2'] = self.naxis2
        return header

    def to_json(self) -> dict:
        """Serialize for Supabase storage."""
        return {
            'crpix1': self.crpix1,
            'crpix2': self.crpix2,
            'crval1': self.crval1,
            'crval2': self.crval2,
            'cd1_1': self.cd1_1,
            'cd2_2': self.cd2_2,
            'naxis1': self.naxis1,
            'naxis2': self.naxis2,
        }

    def sub_header(self, x0: int, y0: int, nx: int, ny: int) -> fits.Header:
        """
        Create a FITS header for a sub-region of the output grid.

        The sub-region starts at pixel (x0, y0) and has size (nx, ny).
        CRPIX is adjusted so the WCS remains correct for the sub-region.
        """
        header = fits.Header()
        header['NAXIS'] = 2
        header['NAXIS1'] = nx
        header['NAXIS2'] = ny
        header['CRPIX1'] = self.crpix1 - x0
        header['CRPIX2'] = self.crpix2 - y0
        header['CRVAL1'] = self.crval1
        header['CRVAL2'] = self.crval2
        header['CD1_1'] = self.cd1_1
        header['CD1_2'] = 0.0
        header['CD2_1'] = 0.0
        header['CD2_2'] = self.cd2_2
        header['CTYPE1'] = 'RA---TAN'
        header['CTYPE2'] = 'DEC--TAN'
        return header


@dataclass
class TileStats:
    """Statistics from a tile generation run."""
    field: str
    filter_name: str
    num_input_files: int
    output_grid_size: tuple[int, int]  # (naxis1, naxis2)
    min_zoom: int
    max_zoom: int
    total_tiles: int
    total_size_bytes: int
    ra_min: float
    ra_max: float
    dec_min: float
    dec_max: float
    wcs_params: dict
    tile_base_url: str = ""


@dataclass
class InputFileInfo:
    """Cached info about an input FITS file for fast overlap checks."""
    path: Path
    # Bounding box in output grid pixel coordinates
    x_min: int
    x_max: int
    y_min: int
    y_max: int


@dataclass
class RGBConfig:
    """Configuration for an RGB composite tile generation run."""
    field: str
    output_dir: Path
    output_pixel_scale_arcsec: float
    tile_size: int
    mask_wht: bool
    filter_channels: dict[str, dict]  # {filter: {'files': [Path], 'color': [r,g,b]}}
    noisesig: float = 2.0
    noiselum: float = 0.12
    satpercent: float = 0.01


@dataclass
class RGBStretchParams:
    """Precomputed global RGB stretch parameters."""
    blackpoint: float
    whitepoint: float
    noiselum: float
    rgb_lum_sum: np.ndarray  # shape (3,), sum of all filter color weights


# ============================================
# Weight Map Helpers
# ============================================

def _find_wht_path(sci_path: Path) -> Path | None:
    """Find corresponding WHT file for a SCI FITS file (_sci.fits → _wht.fits)."""
    name = sci_path.name
    if '_sci.fits' not in name:
        return None
    wht_path = sci_path.parent / name.replace('_sci.fits', '_wht.fits')
    return wht_path if wht_path.exists() else None


def _get_ext(hdul, ext_names: list, expected_shape: tuple):
    """Find first matching 2D extension with the expected shape."""
    for ext in ext_names:
        try:
            hdu = hdul[ext]
            if hdu.data is not None and hdu.data.shape == expected_shape:
                return hdu
        except (KeyError, IndexError):
            continue
    return None


# ============================================
# Grid Computation
# ============================================

def _get_fits_wcs(fits_path: Path) -> WCS:
    """Read WCS from a FITS file, trying SCI extension first, then primary."""
    with fits.open(fits_path, memmap=True) as hdul:
        # Try SCI extension first (common for HST/JWST products)
        for ext_name in ['SCI', 0]:
            try:
                wcs = WCS(hdul[ext_name].header, naxis=2)
                if wcs.has_celestial:
                    return wcs
            except (KeyError, IndexError):
                continue
        raise ValueError(f"No valid celestial WCS found in {fits_path}")


def _get_fits_data_shape(fits_path: Path) -> tuple[int, int]:
    """Get the data shape from a FITS file without loading data."""
    with fits.open(fits_path, memmap=True) as hdul:
        for ext_name in ['SCI', 0]:
            try:
                shape = hdul[ext_name].shape
                if len(shape) == 2:
                    return shape
            except (KeyError, IndexError):
                continue
        raise ValueError(f"No 2D data found in {fits_path}")


def compute_output_grid(
    input_files: list[Path],
    pixel_scale_arcsec: float,
    padding_arcsec: float = 10.0,
) -> OutputGrid:
    """
    Compute the minimal North-up output grid that encompasses all input files.

    Reads WCS from each input FITS header, computes corner coordinates,
    finds the bounding box in RA/Dec, and constructs an output WCS.
    """
    all_ra = []
    all_dec = []

    for fits_path in input_files:
        wcs = _get_fits_wcs(fits_path)
        shape = _get_fits_data_shape(fits_path)
        ny, nx = shape

        corners_pix = np.array([
            [0, 0],
            [nx - 1, 0],
            [nx - 1, ny - 1],
            [0, ny - 1],
        ], dtype=float)

        corners_sky = wcs.pixel_to_world_values(corners_pix[:, 0], corners_pix[:, 1])
        all_ra.extend(corners_sky[0])
        all_dec.extend(corners_sky[1])

    all_ra = np.array(all_ra)
    all_dec = np.array(all_dec)

    # Handle RA wrap-around
    if np.ptp(all_ra) > 180:
        all_ra = np.where(all_ra > 180, all_ra - 360, all_ra)

    padding_deg = padding_arcsec / 3600.0
    ra_min = np.min(all_ra) - padding_deg
    ra_max = np.max(all_ra) + padding_deg
    dec_min = np.min(all_dec) - padding_deg
    dec_max = np.max(all_dec) + padding_deg

    ra_center = (ra_min + ra_max) / 2.0
    dec_center = (dec_min + dec_max) / 2.0

    pixel_scale_deg = pixel_scale_arcsec / 3600.0
    cos_dec = math.cos(math.radians(dec_center))
    naxis1 = int(math.ceil((ra_max - ra_min) * cos_dec / pixel_scale_deg))
    naxis2 = int(math.ceil((dec_max - dec_min) / pixel_scale_deg))

    crpix1 = naxis1 / 2.0
    crpix2 = naxis2 / 2.0

    # CD matrix: North-up, RA increases to the left (negative CD1_1)
    cd1_1 = -pixel_scale_deg
    cd2_2 = pixel_scale_deg

    logger.info(
        f"Output grid: {naxis1} x {naxis2} pixels, "
        f"center RA={ra_center:.4f} Dec={dec_center:.4f}, "
        f"scale={pixel_scale_arcsec:.3f}\"/px"
    )

    return OutputGrid(
        crpix1=crpix1,
        crpix2=crpix2,
        crval1=ra_center,
        crval2=dec_center,
        cd1_1=cd1_1,
        cd2_2=cd2_2,
        naxis1=naxis1,
        naxis2=naxis2,
    )


def compute_field_grid(
    all_filter_files: dict[str, list[Path]],
    pixel_scale_arcsec: float,
    padding_arcsec: float = 10.0,
) -> OutputGrid:
    """
    Compute a unified output grid for a field by taking the union
    of all filter coverages. All filters will share this grid so
    tiles align when switching filters in the map viewer.
    """
    all_files = []
    for files in all_filter_files.values():
        all_files.extend(files)

    logger.info(
        f"Computing unified field grid from {len(all_files)} files "
        f"across {len(all_filter_files)} filter(s)"
    )
    return compute_output_grid(all_files, pixel_scale_arcsec, padding_arcsec)


def save_field_grid(output_dir: Path, field: str, grid: OutputGrid) -> Path:
    """Save field grid to JSON for reuse when regenerating individual filters."""
    path = output_dir / field / 'field_grid.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(grid.to_json(), f, indent=2)
    logger.info(f"Saved field grid to {path}")
    return path


def load_field_grid(output_dir: Path, field: str) -> OutputGrid | None:
    """Load a previously saved field grid."""
    path = output_dir / field / 'field_grid.json'
    if not path.exists():
        return None
    with open(path) as f:
        data = json.load(f)
    logger.info(f"Loaded field grid from {path}")
    return OutputGrid(**data)


# ============================================
# Input File Overlap Detection
# ============================================

def precompute_input_bboxes(
    input_files: list[Path],
    output_grid: OutputGrid,
) -> list[InputFileInfo]:
    """
    Precompute bounding boxes of each input file in output pixel coordinates.

    For each input FITS, project its corners into output pixel space and store
    the axis-aligned bounding box. This allows O(1) overlap checks per tile.
    """
    output_wcs = output_grid.to_wcs()
    infos = []

    for fits_path in input_files:
        input_wcs = _get_fits_wcs(fits_path)
        shape = _get_fits_data_shape(fits_path)
        ny, nx = shape

        # Input corners in pixel coords
        corners_pix = np.array([
            [0, 0],
            [nx - 1, 0],
            [nx - 1, ny - 1],
            [0, ny - 1],
        ], dtype=float)

        # Input corners -> sky -> output pixel coords
        sky_ra, sky_dec = input_wcs.pixel_to_world_values(
            corners_pix[:, 0], corners_pix[:, 1]
        )
        out_x, out_y = output_wcs.world_to_pixel_values(sky_ra, sky_dec)

        # Bounding box in output pixels (with generous padding for rotation)
        padding = 50  # pixels of padding for interpolation edge effects
        x_min = max(0, int(np.floor(np.min(out_x))) - padding)
        x_max = min(output_grid.naxis1, int(np.ceil(np.max(out_x))) + padding)
        y_min = max(0, int(np.floor(np.min(out_y))) - padding)
        y_max = min(output_grid.naxis2, int(np.ceil(np.max(out_y))) + padding)

        infos.append(InputFileInfo(
            path=fits_path,
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
        ))

        logger.debug(
            f"  {fits_path.name}: output bbox "
            f"x=[{x_min}, {x_max}] y=[{y_min}, {y_max}]"
        )

    return infos


def find_overlapping_inputs(
    input_infos: list[InputFileInfo],
    tile_x0: int,
    tile_y0: int,
    tile_size: int,
) -> list[InputFileInfo]:
    """Find input files whose bounding box overlaps this tile region."""
    tile_x1 = tile_x0 + tile_size
    tile_y1 = tile_y0 + tile_size

    overlapping = []
    for info in input_infos:
        # Check AABB overlap
        if (info.x_min < tile_x1 and info.x_max > tile_x0 and
                info.y_min < tile_y1 and info.y_max > tile_y0):
            overlapping.append(info)

    return overlapping


# ============================================
# Stretch / Normalization
# ============================================

def compute_stretch_params(
    input_files: list[Path],
    stretch_type: str = "asinh",
    min_percentile: float = 1.0,
    max_percentile: float = 99.5,
    n_rows_per_file: int = 50,
    mask_wht: bool = False,
) -> dict:
    """
    Precompute global stretch parameters by sampling pixels from all inputs.

    Reads random contiguous rows (fast sequential reads on memory-mapped data)
    from each input file, computes percentiles, and returns parameters for
    consistent normalization across all tiles.

    Returns dict with keys: vmin, vmax, stretch_type
    """
    logger.info("Computing global stretch parameters from input samples...")

    all_samples = []
    rng = np.random.default_rng(42)

    for fits_path in input_files:
        wht_path = _find_wht_path(fits_path) if mask_wht else None
        with fits.open(fits_path, memmap=True) as hdul:
            for ext_name in ['SCI', 0]:
                try:
                    data = hdul[ext_name].data
                    if data is not None and len(data.shape) == 2:
                        ny, nx = data.shape
                        # Sample random complete rows (contiguous reads)
                        n_rows = min(n_rows_per_file, ny)
                        row_indices = rng.choice(ny, size=n_rows, replace=False)
                        row_indices.sort()  # sequential access pattern
                        samples = data[row_indices, :].ravel().astype(np.float32)
                        # Mask zero-weight pixels using WHT map
                        if wht_path is not None:
                            with fits.open(wht_path, memmap=True) as wht_hdul:
                                wht_ext = _get_ext(wht_hdul, ['WHT', 0], data.shape)
                                if wht_ext is not None:
                                    wht_samples = wht_ext.data[row_indices, :].ravel()
                                    samples[wht_samples == 0] = np.nan
                        # Keep only finite values
                        samples = samples[np.isfinite(samples)]
                        all_samples.append(samples)
                        logger.info(
                            f"  Sampled {n_rows} rows ({len(samples):,} pixels) "
                            f"from {fits_path.name}"
                        )
                        break
                except (KeyError, IndexError):
                    continue

    if not all_samples:
        raise ValueError("No valid pixel data found in input files")

    combined = np.concatenate(all_samples)
    vmin = float(np.percentile(combined, min_percentile))
    vmax = float(np.percentile(combined, max_percentile))

    logger.info(
        f"Stretch params: {stretch_type}, "
        f"vmin={vmin:.4e} ({min_percentile}th pct), "
        f"vmax={vmax:.4e} ({max_percentile}th pct), "
        f"from {len(combined):,} samples"
    )

    return {
        'stretch_type': stretch_type,
        'vmin': vmin,
        'vmax': vmax,
    }


def apply_stretch(
    data: np.ndarray,
    stretch_params: dict,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Apply pre-computed stretch to convert float data to 0-255 uint8.

    Uses the global vmin/vmax from compute_stretch_params for consistent
    normalization across all tiles.

    Returns:
        (uint8_data, alpha) where alpha is 255 for valid pixels, 0 for NaN.
    """
    from astropy.visualization import AsinhStretch, LinearStretch, LogStretch, SqrtStretch
    from astropy.visualization import ManualInterval, ImageNormalize

    stretch_type = stretch_params['stretch_type']
    vmin = stretch_params['vmin']
    vmax = stretch_params['vmax']

    stretch_map = {
        'asinh': AsinhStretch,
        'linear': LinearStretch,
        'log': LogStretch,
        'sqrt': SqrtStretch,
    }
    stretch_cls = stretch_map.get(stretch_type, AsinhStretch)

    interval = ManualInterval(vmin=vmin, vmax=vmax)
    norm = ImageNormalize(interval=interval, stretch=stretch_cls())

    valid = np.isfinite(data)
    if not np.any(valid):
        h, w = data.shape
        return np.zeros((h, w), dtype=np.uint8), np.zeros((h, w), dtype=np.uint8)

    normalized = norm(data)
    result = np.zeros(data.shape, dtype=np.uint8)
    result[valid] = (np.clip(normalized[valid], 0, 1) * 255).astype(np.uint8)

    alpha = np.where(valid, np.uint8(255), np.uint8(0))

    return result, alpha


# ============================================
# Tile Pyramid Generation (per-tile reprojection)
# ============================================

def compute_zoom_range(
    naxis1: int,
    naxis2: int,
    tile_size: int = 256,
) -> tuple[int, int]:
    """
    Compute min and max zoom levels for the tile pyramid.

    At max_zoom, each tile covers tile_size pixels of the source image.
    At min_zoom, the entire image fits in 1-2 tiles per dimension.
    """
    long_side = max(naxis1, naxis2)
    max_zoom = max(0, int(math.ceil(math.log2(long_side / tile_size))))
    min_zoom = 0
    return min_zoom, max_zoom


def _reproject_tile(
    overlapping_hdus: list,
    tile_header: fits.Header,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Reproject pre-opened input HDUs onto a single tile.

    Returns (data, footprint) arrays.
    """
    from reproject import reproject_interp
    from reproject.mosaicking import reproject_and_coadd

    tile_shape = (tile_header['NAXIS2'], tile_header['NAXIS1'])

    if not overlapping_hdus:
        return (
            np.full(tile_shape, np.nan, dtype=np.float32),
            np.zeros(tile_shape, dtype=np.float32),
        )

    if len(overlapping_hdus) == 1:
        data, footprint = reproject_interp(
            overlapping_hdus[0],
            tile_header,
            shape_out=tile_shape,
        )
    else:
        data, footprint = reproject_and_coadd(
            overlapping_hdus,
            tile_header,
            shape_out=tile_shape,
            reproject_function=reproject_interp,
            combine_function='mean',
        )

    return data.astype(np.float32), footprint.astype(np.float32)


def _process_supertile(
    sx: int, sy: int,
    supertile_size: int,
    output_grid: OutputGrid,
    input_infos: list[InputFileInfo],
    path_to_hdu: dict[str, object],
    stretch_params: dict,
    tile_dir: Path,
    tile_size: int,
    max_zoom: int,
    n_tiles_y: int,
    overwrite: bool = False,
) -> tuple[int, int]:
    """
    Reproject one supertile and slice it into 256x256 PNG tiles.

    A supertile is a larger region (e.g., 2048x2048) that gets reprojected
    in a single reproject call, then sliced into tile_size chunks. This
    amortizes the per-call overhead of reproject across many tiles.

    Uses pre-opened HDUs from ``path_to_hdu`` (shared across threads).

    Returns (n_tiles_written, total_bytes).
    """
    # Supertile bounds in output pixel coords
    x0 = sx * supertile_size
    y0 = sy * supertile_size
    nx = min(supertile_size, output_grid.naxis1 - x0)
    ny = min(supertile_size, output_grid.naxis2 - y0)

    # How many 256-tiles fit in this supertile
    tiles_per_st = supertile_size // tile_size
    base_tx = sx * tiles_per_st
    base_ty = sy * tiles_per_st

    # Sentinel file marks a completed supertile (for resume)
    sentinel = tile_dir / str(max_zoom) / f".st_{sx}_{sy}.done"
    if not overwrite and sentinel.exists():
        # Count existing tiles for stats
        n_existing = 0
        existing_bytes = 0
        for lty in range(tiles_per_st):
            for ltx in range(tiles_per_st):
                tx = base_tx + ltx
                ty = base_ty + lty
                leaflet_y = n_tiles_y - 1 - ty
                tile_path = tile_dir / str(max_zoom) / str(tx) / f"{leaflet_y}.png"
                if tile_path.exists():
                    n_existing += 1
                    existing_bytes += tile_path.stat().st_size
        return (n_existing, existing_bytes)

    # Find overlapping inputs for the full supertile region
    overlapping = find_overlapping_inputs(input_infos, x0, y0, supertile_size)
    if not overlapping:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return (0, 0)

    hdus = [path_to_hdu[str(info.path)] for info in overlapping
            if str(info.path) in path_to_hdu]
    if not hdus:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return (0, 0)

    # Single reproject call for the full supertile
    st_header = output_grid.sub_header(x0, y0, nx, ny)
    data, footprint = _reproject_tile(hdus, st_header)

    if not np.any(footprint > 0):
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return (0, 0)

    # Mask poorly-covered pixels so they become transparent after stretch.
    # reproject_interp can return small non-zero footprint values at
    # interpolation boundaries where the kernel barely overlaps valid input.
    # These pixels have unreliable data (often ~0) that stretches to grey.
    data[footprint < 0.5] = np.nan

    # Apply stretch to full supertile at once
    stretched, alpha = apply_stretch(data[:ny, :nx], stretch_params)

    # Slice into 256x256 tiles
    n_tiles = 0
    total_bytes = 0

    for lty in range(tiles_per_st):
        for ltx in range(tiles_per_st):
            tx = base_tx + ltx
            ty = base_ty + lty

            # Pixel range within the supertile
            px0 = ltx * tile_size
            py0 = lty * tile_size
            px1 = min(px0 + tile_size, nx)
            py1 = min(py0 + tile_size, ny)

            if px0 >= nx or py0 >= ny:
                continue

            tile_alpha = alpha[py0:py1, px0:px1]
            if not np.any(tile_alpha > 0):
                # Remove stale tile from previous run if overwriting
                if overwrite:
                    leaflet_y = n_tiles_y - 1 - ty
                    stale = tile_dir / str(max_zoom) / str(tx) / f"{leaflet_y}.png"
                    stale.unlink(missing_ok=True)
                continue

            leaflet_y = n_tiles_y - 1 - ty
            tile_path = tile_dir / str(max_zoom) / str(tx) / f"{leaflet_y}.png"

            # Skip existing in resume mode
            if not overwrite and tile_path.exists():
                n_tiles += 1
                total_bytes += tile_path.stat().st_size
                continue

            tile_data = stretched[py0:py1, px0:px1]

            # Build RGBA (pad to tile_size if at edge)
            rgba = np.zeros((tile_size, tile_size, 4), dtype=np.uint8)
            h, w = tile_data.shape
            rgba[:h, :w, 0] = tile_data
            rgba[:h, :w, 1] = tile_data
            rgba[:h, :w, 2] = tile_data
            rgba[:h, :w, 3] = tile_alpha

            tile_path.parent.mkdir(parents=True, exist_ok=True)
            img = Image.fromarray(np.flipud(rgba), 'RGBA')
            img.save(tile_path, 'PNG', compress_level=PNG_COMPRESS_LEVEL)

            n_tiles += 1
            total_bytes += tile_path.stat().st_size

    # Mark supertile as complete for resume
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    return (n_tiles, total_bytes)


# Default supertile size: 8x8 tiles = 2048x2048 pixels (~16MB float32)
SUPERTILE_SIZE = 2048

# PNG compression level (0-9). Lower = faster writes, larger files.
# Level 1 is ~3x faster than default (6) with ~20% larger tiles.
PNG_COMPRESS_LEVEL = 3

# Intermediate directory name for two-pass RGB tile generation.
# Pass 1 writes per-filter .npy files here; pass 2 reads and deletes them.
RGB_INTERMEDIATE_DIR = '.rgb_intermediate'


def generate_max_zoom_tiles(
    output_grid: OutputGrid,
    input_infos: list[InputFileInfo],
    stretch_params: dict,
    tile_dir: Path,
    tile_size: int,
    max_zoom: int,
    n_workers: int = 1,
    overwrite: bool = False,
    mask_wht: bool = False,
) -> tuple[int, int]:
    """
    Generate tiles at max zoom using supertile reprojection.

    Instead of calling reproject once per 256x256 tile (~37k calls),
    reprojects larger supertiles (2048x2048) and slices into PNG tiles.
    This reduces reproject calls by ~64x, amortizing the per-call overhead.

    Uses ThreadPoolExecutor for parallelism across supertiles. FITS files
    are pre-opened once (memory-mapped) and shared across all threads in
    the same process, avoiding per-worker duplication and page cache
    thrashing that occurs with ProcessPoolExecutor.

    Supertiles are processed row-by-row so threads in the same batch access
    similar y-ranges of the input FITS files, improving page cache locality.

    Returns (total_tiles, total_bytes).
    """
    n_tiles_x = int(math.ceil(output_grid.naxis1 / tile_size))
    n_tiles_y = int(math.ceil(output_grid.naxis2 / tile_size))
    n_st_x = int(math.ceil(output_grid.naxis1 / SUPERTILE_SIZE))
    n_st_y = int(math.ceil(output_grid.naxis2 / SUPERTILE_SIZE))
    total_supertiles = n_st_x * n_st_y

    logger.info(
        f"Max zoom {max_zoom}: {n_tiles_x}x{n_tiles_y} tiles via "
        f"{n_st_x}x{n_st_y} supertiles ({SUPERTILE_SIZE}px), "
        f"{n_workers} thread(s)"
        f"{'' if overwrite else ', skipping existing'}"
    )

    # Pre-open all FITS files once (memory-mapped, thread-safe for reads).
    # When mask_wht is enabled and a corresponding WHT file exists, load SCI
    # data into memory and mask zero-weight pixels as NaN.
    path_to_hdu: dict[str, object] = {}
    open_hduls = []
    for info in input_infos:
        path_str = str(info.path)
        hdul = fits.open(info.path, memmap=True)
        open_hduls.append(hdul)
        for ext_name in ['SCI', 0]:
            try:
                hdu = hdul[ext_name]
                if hdu.data is not None and len(hdu.data.shape) == 2:
                    if mask_wht:
                        wht_path = _find_wht_path(info.path)
                        if wht_path is not None:
                            data = hdu.data.astype(np.float32)
                            with fits.open(wht_path, memmap=True) as wht_hdul:
                                wht_ext = _get_ext(
                                    wht_hdul, ['WHT', 0], data.shape
                                )
                                if wht_ext is not None:
                                    data[wht_ext.data == 0] = np.nan
                            path_to_hdu[path_str] = (
                                data, WCS(hdu.header, naxis=2)
                            )
                        else:
                            path_to_hdu[path_str] = hdu
                    else:
                        path_to_hdu[path_str] = hdu
                    break
            except (KeyError, IndexError):
                continue

    # Build supertile positions grouped by row (for page cache locality).
    # Supertiles in the same row access similar y-ranges of input files.
    st_rows: list[list[tuple[int, int]]] = [[] for _ in range(n_st_y)]
    total_with_overlap = 0
    for sy in range(n_st_y):
        for sx in range(n_st_x):
            x0 = sx * SUPERTILE_SIZE
            y0 = sy * SUPERTILE_SIZE
            if find_overlapping_inputs(input_infos, x0, y0, SUPERTILE_SIZE):
                st_rows[sy].append((sx, sy))
                total_with_overlap += 1

    logger.info(
        f"  {total_with_overlap}/{total_supertiles} supertiles have "
        f"overlapping inputs"
    )

    total_tiles = 0
    total_bytes = 0

    def process(pos):
        return _process_supertile(
            pos[0], pos[1], SUPERTILE_SIZE,
            output_grid, input_infos, path_to_hdu,
            stretch_params, tile_dir, tile_size, max_zoom, n_tiles_y,
            overwrite=overwrite,
        )

    # Process row by row to keep memory-mapped page cache footprint small.
    # Threads within a row access similar y-ranges of the input FITS files.
    pbar = tqdm(total=total_with_overlap, desc=f"Zoom {max_zoom}", unit="st", smoothing=0.05)

    if n_workers <= 1:
        for row in st_rows:
            for pos in row:
                n, nbytes = process(pos)
                total_tiles += n
                total_bytes += nbytes
                pbar.update(1)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            for row in st_rows:
                if not row:
                    continue
                # Submit one row at a time, wait for completion
                futures = {executor.submit(process, pos): pos for pos in row}
                for future in as_completed(futures):
                    n, nbytes = future.result()
                    total_tiles += n
                    total_bytes += nbytes
                    pbar.update(1)

    pbar.close()

    # Close files
    for hdul in open_hduls:
        hdul.close()

    logger.info(
        f"  Max zoom {max_zoom}: {total_tiles} non-empty tiles "
        f"({total_bytes / (1024 * 1024):.1f} MB)"
    )

    return total_tiles, total_bytes


def _build_one_lower_tile(
    tile_dir: Path,
    zoom: int,
    tx: int,
    ty: int,
    n_tiles_y: int,
    tile_size: int,
    overwrite: bool,
) -> tuple[int, int]:
    """
    Build one lower-zoom tile by combining its 4 children.

    Top-level function (also usable from ThreadPoolExecutor).
    Returns (n_tiles, n_bytes) — (1, size) if written/exists, (0, 0) if empty.
    """
    leaflet_y = n_tiles_y - 1 - ty
    tile_path = tile_dir / str(zoom) / str(tx) / f"{leaflet_y}.png"

    # Skip if already exists
    if not overwrite and tile_path.exists():
        return (1, tile_path.stat().st_size)

    # Combine 4 children from zoom+1
    child_zoom = zoom + 1
    combined = np.zeros((tile_size * 2, tile_size * 2, 4), dtype=np.uint8)
    has_any = False

    for dx in range(2):
        for dy in range(2):
            child_tx = 2 * tx + dx
            child_leaflet_y = 2 * leaflet_y + dy

            child_path = (
                tile_dir / str(child_zoom) / str(child_tx)
                / f"{child_leaflet_y}.png"
            )
            if not child_path.exists():
                continue

            child_img = Image.open(child_path)
            child_arr = np.array(child_img)

            # Place in correct quadrant (Leaflet convention: y=0 at top)
            qx = dx * tile_size
            qy = dy * tile_size
            h = min(tile_size, child_arr.shape[0])
            w = min(tile_size, child_arr.shape[1])
            channels = min(child_arr.shape[2], 4) if len(child_arr.shape) == 3 else 1
            combined[qy:qy + h, qx:qx + w, :channels] = child_arr[:h, :w, :channels]
            has_any = True

    if not has_any:
        if overwrite:
            tile_path.unlink(missing_ok=True)
        return (0, 0)

    # Downsample 2x using Pillow (high quality)
    combined_img = Image.fromarray(combined, 'RGBA')
    downsampled = combined_img.resize((tile_size, tile_size), Image.LANCZOS)

    # Check if tile has any non-transparent content
    ds_arr = np.array(downsampled)
    if not np.any(ds_arr[:, :, 3] > 0):
        if overwrite:
            tile_path.unlink(missing_ok=True)
        return (0, 0)

    tile_path.parent.mkdir(parents=True, exist_ok=True)
    downsampled.save(tile_path, 'PNG', compress_level=PNG_COMPRESS_LEVEL)

    return (1, tile_path.stat().st_size)


def build_lower_zoom_levels(
    tile_dir: Path,
    min_zoom: int,
    max_zoom: int,
    tile_size: int,
    naxis1: int,
    naxis2: int,
    overwrite: bool = False,
    n_workers: int = 1,
) -> tuple[int, int]:
    """
    Build lower zoom levels by combining 4 child tiles into 1 parent.

    At each zoom level z, tile (tx, ty) is built from zoom z+1 tiles:
        (2*tx, 2*ty), (2*tx+1, 2*ty), (2*tx, 2*ty+1), (2*tx+1, 2*ty+1)

    Each child is placed in its quadrant of a 2*tile_size image, then
    downsampled to tile_size. This is exactly the FITSMap approach.

    Uses ThreadPoolExecutor when n_workers > 1 for parallelism.
    When overwrite is False, existing tiles are skipped (counted in totals).

    Returns (total_tiles, total_bytes).
    """
    total_tiles = 0
    total_bytes = 0

    for zoom in range(max_zoom - 1, min_zoom - 1, -1):
        # At this zoom, each pixel covers 2^(max_zoom - zoom) output pixels
        scale = 2 ** (max_zoom - zoom)
        n_tiles_x = int(math.ceil(naxis1 / scale / tile_size))
        n_tiles_y = int(math.ceil(naxis2 / scale / tile_size))
        zoom_tiles = 0
        positions = [(tx, ty) for tx in range(n_tiles_x) for ty in range(n_tiles_y)]

        if n_workers <= 1 or len(positions) < 16:
            for tx, ty in tqdm(positions, desc=f"Zoom {zoom}", unit="tile", smoothing=0.05):
                n, nbytes = _build_one_lower_tile(
                    tile_dir, zoom, tx, ty, n_tiles_y, tile_size, overwrite,
                )
                total_tiles += n
                total_bytes += nbytes
                zoom_tiles += n
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=n_workers) as executor:
                futures = {
                    executor.submit(
                        _build_one_lower_tile,
                        tile_dir, zoom, tx, ty, n_tiles_y, tile_size, overwrite,
                    ): (tx, ty)
                    for tx, ty in positions
                }
                for future in tqdm(
                    as_completed(futures),
                    total=len(futures),
                    desc=f"Zoom {zoom}",
                    unit="tile",
                    smoothing=0.05,
                ):
                    n, nbytes = future.result()
                    total_tiles += n
                    total_bytes += nbytes
                    zoom_tiles += n

        logger.info(
            f"  Zoom {zoom}: {n_tiles_x}x{n_tiles_y} grid, "
            f"{zoom_tiles} non-empty tiles"
        )

    return total_tiles, total_bytes


# ============================================
# Full Pipeline
# ============================================

def generate_tiles_for_filter(
    config: TileConfig,
    n_workers: int = 1,
    overwrite: bool = False,
    output_grid: OutputGrid | None = None,
) -> TileStats:
    """
    Full tile generation pipeline for one field/filter combination.

    1. Compute output grid from input WCS headers (or use provided grid)
    2. Precompute input file bounding boxes in output pixel coords
    3. Compute global stretch parameters by sampling input pixels
    4. Generate max zoom tiles by per-tile reprojection
    5. Build lower zoom levels from children
    6. Return stats for registration

    Args:
        config: TileConfig with all parameters.
        n_workers: Number of parallel workers for max-zoom tile generation.
        overwrite: If False, skip tiles that already exist on disk.
        output_grid: Optional pre-computed grid (e.g. unified field grid).
            If None, computes per-filter grid from config.input_files.
    """
    logger.info(
        f"=== Generating tiles: {config.field}/{config.filter_name} "
        f"({len(config.input_files)} input files) ==="
    )

    # Step 1: Use provided grid or compute per-filter grid
    if output_grid is None:
        output_grid = compute_output_grid(
            config.input_files,
            config.output_pixel_scale_arcsec,
        )
    else:
        logger.info("Using provided unified field grid")

    min_zoom, max_zoom = compute_zoom_range(
        output_grid.naxis1, output_grid.naxis2, config.tile_size
    )

    # Step 2: Precompute input bounding boxes
    logger.info("Precomputing input file bounding boxes...")
    input_infos = precompute_input_bboxes(config.input_files, output_grid)

    # Step 3: Compute global stretch parameters
    stretch_params = compute_stretch_params(
        config.input_files,
        stretch_type=config.stretch_type,
        min_percentile=config.min_percentile,
        max_percentile=config.max_percentile,
        mask_wht=config.mask_wht,
    )

    tile_dir = config.output_dir / config.field / config.filter_name

    # Step 4: Generate max zoom tiles (per-tile reprojection)
    max_tiles, max_bytes = generate_max_zoom_tiles(
        output_grid=output_grid,
        input_infos=input_infos,
        stretch_params=stretch_params,
        tile_dir=tile_dir,
        tile_size=config.tile_size,
        max_zoom=max_zoom,
        n_workers=n_workers,
        overwrite=overwrite,
        mask_wht=config.mask_wht,
    )

    # Step 5: Build lower zoom levels from children
    lower_tiles, lower_bytes = build_lower_zoom_levels(
        tile_dir=tile_dir,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        tile_size=config.tile_size,
        naxis1=output_grid.naxis1,
        naxis2=output_grid.naxis2,
        overwrite=overwrite,
        n_workers=n_workers,
    )

    total_tiles = max_tiles + lower_tiles
    total_bytes = max_bytes + lower_bytes

    # Compute sky bounds
    wcs = output_grid.to_wcs()
    corners = np.array([
        [0, 0],
        [output_grid.naxis1 - 1, 0],
        [output_grid.naxis1 - 1, output_grid.naxis2 - 1],
        [0, output_grid.naxis2 - 1],
    ], dtype=float)
    corner_sky = wcs.pixel_to_world_values(corners[:, 0], corners[:, 1])

    stats = TileStats(
        field=config.field,
        filter_name=config.filter_name,
        num_input_files=len(config.input_files),
        output_grid_size=(output_grid.naxis1, output_grid.naxis2),
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        total_tiles=total_tiles,
        total_size_bytes=total_bytes,
        ra_min=float(np.min(corner_sky[0])),
        ra_max=float(np.max(corner_sky[0])),
        dec_min=float(np.min(corner_sky[1])),
        dec_max=float(np.max(corner_sky[1])),
        wcs_params=output_grid.to_json(),
    )

    logger.info(
        f"=== Done: {total_tiles} tiles, "
        f"{total_bytes / (1024 * 1024):.1f} MB ==="
    )

    return stats


def estimate_tiles_for_filter(
    config: TileConfig,
    output_grid: OutputGrid | None = None,
) -> dict:
    """
    Dry-run estimation: compute grid and tile counts without generating.
    """
    if output_grid is None:
        output_grid = compute_output_grid(
            config.input_files,
            config.output_pixel_scale_arcsec,
        )

    min_zoom, max_zoom = compute_zoom_range(
        output_grid.naxis1, output_grid.naxis2, config.tile_size
    )

    total_tiles = 0
    for zoom in range(min_zoom, max_zoom + 1):
        scale = 2 ** (max_zoom - zoom)
        nx_tiles = int(math.ceil(output_grid.naxis1 / scale / config.tile_size))
        ny_tiles = int(math.ceil(output_grid.naxis2 / scale / config.tile_size))
        # Assume ~60% coverage
        total_tiles += int(nx_tiles * ny_tiles * 0.6)

    avg_tile_bytes = 40 * 1024  # ~40KB
    estimated_bytes = total_tiles * avg_tile_bytes

    return {
        'field': config.field,
        'filter': config.filter_name,
        'input_files': len(config.input_files),
        'output_width': output_grid.naxis1,
        'output_height': output_grid.naxis2,
        'pixel_scale_arcsec': config.output_pixel_scale_arcsec,
        'min_zoom': min_zoom,
        'max_zoom': max_zoom,
        'estimated_tiles': total_tiles,
        'estimated_size_mb': estimated_bytes / (1024 * 1024),
        'estimated_size_gb': estimated_bytes / (1024 * 1024 * 1024),
    }


# ============================================
# Config Loading
# ============================================

def load_imaging_config(config_path: Path) -> dict:
    """Load and validate imaging.toml configuration."""
    if not config_path.exists():
        raise FileNotFoundError(f"Imaging config not found: {config_path}")

    with open(config_path, 'rb') as f:
        config = tomllib.load(f)

    if 'defaults' not in config:
        raise ValueError("imaging.toml must contain a [defaults] section")

    return config


def get_tile_configs(
    imaging_config: dict,
    fields: list[str] | None = None,
    filters: list[str] | None = None,
) -> list[TileConfig]:
    """
    Parse imaging.toml into TileConfig objects.

    Resolves glob patterns to actual file lists.
    """
    defaults = imaging_config.get('defaults', {})
    default_pixel_scale = defaults.get('output_pixel_scale_arcsec', 0.06)
    default_tile_size = defaults.get('tile_size', 256)
    default_output_dir = Path(defaults.get('output_dir', 'pipeline/tiles'))
    default_stretch = defaults.get('stretch', {})
    default_stretch_type = default_stretch.get('type', 'asinh')
    default_min_pct = default_stretch.get('min_percentile', 1.0)
    default_max_pct = default_stretch.get('max_percentile', 99.5)

    configs = []

    for section_name, section in imaging_config.items():
        if section_name == 'defaults' or not isinstance(section, dict):
            continue

        field_name = section.get('field', section_name)

        if fields and field_name not in fields:
            continue

        data_dir = Path(section.get('data_dir', '.'))
        mask_wht = section.get('mask_wht', False)
        field_filters = section.get('filters', {})

        for filter_key, filter_config in field_filters.items():
            if filters and filter_key not in filters:
                continue

            file_pattern = filter_config.get('files', '')
            if isinstance(file_pattern, str):
                input_files = sorted(data_dir.glob(file_pattern))
            elif isinstance(file_pattern, list):
                input_files = []
                for pattern in file_pattern:
                    input_files.extend(sorted(data_dir.glob(pattern)))
            else:
                logger.warning(
                    f"Skipping {field_name}/{filter_key}: "
                    f"invalid files specification"
                )
                continue

            if not input_files:
                logger.warning(
                    f"No files found for {field_name}/{filter_key}: "
                    f"pattern '{file_pattern}' in {data_dir}"
                )
                continue

            configs.append(TileConfig(
                field=field_name,
                filter_name=filter_key,
                input_files=input_files,
                output_dir=default_output_dir,
                output_pixel_scale_arcsec=default_pixel_scale,
                tile_size=default_tile_size,
                stretch_type=default_stretch_type,
                min_percentile=default_min_pct,
                max_percentile=default_max_pct,
                mask_wht=mask_wht,
            ))

    return configs


# ============================================
# RGB Composite Pipeline
# ============================================

def get_rgb_configs(
    imaging_config: dict,
    fields: list[str] | None = None,
) -> list[RGBConfig]:
    """
    Parse [field.rgb] sections from imaging.toml into RGBConfig objects.

    For each filter in rgb.channels, looks up file globs from [field.filters].
    Skips filters not found (with warning).
    """
    defaults = imaging_config.get('defaults', {})
    default_pixel_scale = defaults.get('output_pixel_scale_arcsec', 0.06)
    default_tile_size = defaults.get('tile_size', 256)
    default_output_dir = Path(defaults.get('output_dir', 'pipeline/tiles'))

    configs = []

    for section_name, section in imaging_config.items():
        if section_name == 'defaults' or not isinstance(section, dict):
            continue

        field_name = section.get('field', section_name)
        if fields and field_name not in fields:
            continue

        rgb_section = section.get('rgb')
        if not rgb_section:
            continue

        data_dir = Path(section.get('data_dir', '.'))
        mask_wht = section.get('mask_wht', False)
        field_filters = section.get('filters', {})

        channels = rgb_section.get('channels', {})
        if not channels:
            logger.warning(
                f"No channels defined in [{section_name}.rgb], skipping"
            )
            continue

        filter_channels = {}
        for filter_name, color_weights in channels.items():
            if filter_name not in field_filters:
                logger.warning(
                    f"Filter '{filter_name}' in [{section_name}.rgb.channels] "
                    f"not found in [{section_name}.filters], skipping"
                )
                continue

            # Resolve file globs
            file_pattern = field_filters[filter_name].get('files', '')
            if isinstance(file_pattern, str):
                input_files = sorted(data_dir.glob(file_pattern))
            elif isinstance(file_pattern, list):
                input_files = []
                for pattern in file_pattern:
                    input_files.extend(sorted(data_dir.glob(pattern)))
            else:
                continue

            if not input_files:
                logger.warning(
                    f"No files found for {field_name}/{filter_name} "
                    f"(used in RGB), skipping"
                )
                continue

            filter_channels[filter_name] = {
                'files': input_files,
                'color': np.array(color_weights, dtype=np.float32),
            }

        if not filter_channels:
            logger.warning(
                f"No valid filters for [{section_name}.rgb], skipping"
            )
            continue

        configs.append(RGBConfig(
            field=field_name,
            output_dir=default_output_dir,
            output_pixel_scale_arcsec=default_pixel_scale,
            tile_size=default_tile_size,
            mask_wht=mask_wht,
            filter_channels=filter_channels,
            noisesig=rgb_section.get('noisesig', 2.0),
            noiselum=rgb_section.get('noiselum', 0.12),
            satpercent=rgb_section.get('satpercent', 0.01),
        ))

    return configs


def compute_rgb_stretch_params(
    rgb_config: RGBConfig,
    n_rows_per_file: int = 200,
) -> RGBStretchParams:
    """
    Precompute global RGB stretch parameters by sampling pixels.

    Mirrors gen_rgb_image's stretch computation but via sampling:
    1. Compute rgb_lum_sum = sum(color_weight for each filter)
    2. Sample random rows from each filter's input files
    3. Compute per-channel contributions and normalize
    4. blackpoint = noisesig * max(sigma_clipped_std per R,G,B)
    5. whitepoint = nanpercentile(all_channels, 100 * (1 - 0.01 * satpercent))
    """
    from astropy.stats import sigma_clipped_stats

    logger.info("Computing global RGB stretch parameters from input samples...")

    # Compute rgb_lum_sum: sum of color weights across all filters
    rgb_lum_sum = np.zeros(3, dtype=np.float64)
    for filt_info in rgb_config.filter_channels.values():
        rgb_lum_sum += filt_info['color']

    # Sample from each filter and compute per-channel contributions
    ch_samples = [[], [], []]  # R, G, B
    rng = np.random.default_rng(42)

    for filt_name, filt_info in rgb_config.filter_channels.items():
        color = filt_info['color']
        filt_samples = []

        for fits_path in filt_info['files']:
            wht_path = _find_wht_path(fits_path) if rgb_config.mask_wht else None
            with fits.open(fits_path, memmap=True) as hdul:
                for ext_name in ['SCI', 0]:
                    try:
                        data = hdul[ext_name].data
                        if data is not None and len(data.shape) == 2:
                            ny, nx = data.shape
                            n_rows = min(n_rows_per_file, ny)
                            row_indices = rng.choice(ny, size=n_rows, replace=False)
                            row_indices.sort()
                            samples = data[row_indices, :].ravel().astype(np.float32)
                            if wht_path is not None:
                                with fits.open(wht_path, memmap=True) as wht_hdul:
                                    wht_ext = _get_ext(
                                        wht_hdul, ['WHT', 0], data.shape
                                    )
                                    if wht_ext is not None:
                                        wht_samples = wht_ext.data[
                                            row_indices, :
                                        ].ravel()
                                        samples[wht_samples == 0] = np.nan
                            samples = samples[np.isfinite(samples)]
                            filt_samples.append(samples)
                            logger.info(
                                f"  Sampled {n_rows} rows "
                                f"({len(samples):,} pixels) "
                                f"from {fits_path.name} [{filt_name}]"
                            )
                            break
                    except (KeyError, IndexError):
                        continue

        if not filt_samples:
            logger.warning(f"  No samples for {filt_name}")
            continue

        combined = np.concatenate(filt_samples)
        # Compute per-channel contributions (color * samples / lum_sum)
        for ch in range(3):
            if color[ch] > 0 and rgb_lum_sum[ch] > 0:
                ch_contrib = color[ch] * combined / rgb_lum_sum[ch]
                ch_samples[ch].append(ch_contrib)

    # Concatenate all samples per channel
    ch_arrays = []
    for ch in range(3):
        if ch_samples[ch]:
            ch_arrays.append(np.concatenate(ch_samples[ch]))
        else:
            ch_arrays.append(np.array([]))

    if all(len(a) == 0 for a in ch_arrays):
        raise ValueError("No valid pixel data found for RGB stretch computation")

    # Blackpoint: noisesig * max(sigma_clipped_std per channel)
    stds = []
    for ch, ch_name in enumerate(['R', 'G', 'B']):
        if len(ch_arrays[ch]) > 0:
            _, _, std = sigma_clipped_stats(ch_arrays[ch])
            stds.append(std)
            logger.info(f"  {ch_name} sigma-clipped std: {std:.4e}")

    blackpoint = float(rgb_config.noisesig * max(stds))

    # Whitepoint: nanpercentile across all channels
    all_channels = np.concatenate([a for a in ch_arrays if len(a) > 0])
    unsatpercent = 1 - 0.01 * rgb_config.satpercent
    whitepoint = float(np.nanpercentile(all_channels, 100 * unsatpercent))

    logger.info(
        f"RGB stretch params: blackpoint={blackpoint:.4e}, "
        f"whitepoint={whitepoint:.4e}, noiselum={rgb_config.noiselum}"
    )

    return RGBStretchParams(
        blackpoint=blackpoint,
        whitepoint=whitepoint,
        noiselum=rgb_config.noiselum,
        rgb_lum_sum=rgb_lum_sum,
    )


def apply_rgb_stretch(
    per_filter_data: dict[str, np.ndarray],
    rgb_config: RGBConfig,
    stretch_params: RGBStretchParams,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Apply RGB stretch to per-filter data arrays.

    Ports gen_rgb_image lines 81-103 exactly, with per-pixel
    handling of partial filter coverage.

    Args:
        per_filter_data: dict mapping filter name -> (H, W) float32 array
        rgb_config: RGB configuration with filter color weights
        stretch_params: Precomputed blackpoint/whitepoint

    Returns:
        (rgb_uint8[H,W,3], alpha[H,W]) where alpha is 255 for valid, 0 for NaN
    """
    # Get dimensions from first filter
    first_data = next(iter(per_filter_data.values()))
    H, W = first_data.shape

    # Sum weighted contributions per channel, tracking per-pixel validity
    rgb_total = np.zeros((3, H, W), dtype=np.float64)
    lum_sum_2d = np.zeros((3, H, W), dtype=np.float64)
    any_valid = np.zeros((H, W), dtype=bool)

    for filt_name, data in per_filter_data.items():
        color = rgb_config.filter_channels[filt_name]['color']
        valid = np.isfinite(data)
        any_valid |= valid

        for ch in range(3):
            if color[ch] > 0:
                rgb_total[ch] += np.where(valid, color[ch] * data, 0)
                lum_sum_2d[ch] += np.where(valid, color[ch], 0)

    # Per-pixel normalization (handles partial coverage)
    lum_sum_2d = np.where(lum_sum_2d > 0, lum_sum_2d, np.nan)
    rgb_avg = rgb_total / lum_sum_2d

    # Log stretch (mirrors gen_rgb_image)
    bp = stretch_params.blackpoint
    wp = stretch_params.whitepoint
    noiselum = stretch_params.noiselum

    log_bp = np.log10(bp)
    log_wp = np.log10(wp)
    log_range = log_wp - log_bp

    result = np.zeros((H, W, 3), dtype=np.uint8)

    for ch in range(3):
        ch_data = rgb_avg[ch]
        with np.errstate(invalid='ignore', divide='ignore'):
            stretched = (np.log10(ch_data) - log_bp) / log_range
        stretched = stretched * (255 * (1 - noiselum)) + 255 * noiselum
        stretched = np.where(stretched > 255, 255, stretched)
        stretched = np.where(np.isnan(stretched) | (stretched < 0), 0, stretched)
        result[:, :, ch] = stretched.astype(np.uint8)

    alpha = np.where(any_valid, np.uint8(255), np.uint8(0))

    return result, alpha


def _open_filter_hdus(
    overlapping: list[InputFileInfo],
    mask_wht: bool,
) -> tuple[list, list]:
    """
    Open FITS files for overlapping inputs, returning (hdus, open_hduls).

    Each hdu is either an HDU object (memmap) or a (data, WCS) tuple
    when WHT masking is applied. Caller must close open_hduls when done.
    """
    hdus = []
    open_hduls = []
    for info in overlapping:
        hdul = fits.open(info.path, memmap=True)
        open_hduls.append(hdul)
        for ext_name in ['SCI', 0]:
            try:
                hdu = hdul[ext_name]
                if hdu.data is not None and len(hdu.data.shape) == 2:
                    if mask_wht:
                        wht_path = _find_wht_path(info.path)
                        if wht_path is not None:
                            data_arr = hdu.data.astype(np.float32)
                            with fits.open(
                                wht_path, memmap=True
                            ) as wht_hdul:
                                wht_ext = _get_ext(
                                    wht_hdul, ['WHT', 0], data_arr.shape
                                )
                                if wht_ext is not None:
                                    data_arr[wht_ext.data == 0] = np.nan
                            hdus.append(
                                (data_arr, WCS(hdu.header, naxis=2))
                            )
                        else:
                            hdus.append(hdu)
                    else:
                        hdus.append(hdu)
                    break
            except (KeyError, IndexError):
                continue
    return hdus, open_hduls


def _reproject_filter_supertile(
    sx: int, sy: int,
    supertile_size: int,
    output_grid: OutputGrid,
    input_infos: list[InputFileInfo],
    path_to_hdu: dict[str, object],
    intermediate_dir: Path,
    filter_name: str,
) -> bool:
    """
    Pass 1 worker: reproject one filter onto one supertile, save as .npy.

    Uses pre-opened HDUs from ``path_to_hdu`` (shared across threads).

    Returns True if a .npy file was written (or already existed), False if
    the filter has no overlap with this supertile.
    """
    npy_path = intermediate_dir / f"{filter_name}_{sx}_{sy}.npy"

    # Resume: skip if already reprojected
    if npy_path.exists():
        return True

    x0 = sx * supertile_size
    y0 = sy * supertile_size
    nx = min(supertile_size, output_grid.naxis1 - x0)
    ny = min(supertile_size, output_grid.naxis2 - y0)

    overlapping = find_overlapping_inputs(input_infos, x0, y0, supertile_size)
    if not overlapping:
        return False

    hdus = [path_to_hdu[str(info.path)] for info in overlapping
            if str(info.path) in path_to_hdu]
    if not hdus:
        return False

    st_header = output_grid.sub_header(x0, y0, nx, ny)
    data, footprint = _reproject_tile(hdus, st_header)

    data[footprint < 0.5] = np.nan
    data = data[:ny, :nx]

    # Check if there's any valid data worth saving
    if not np.any(np.isfinite(data)):
        return False

    npy_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(npy_path, data.astype(np.float32))
    return True


def _combine_rgb_supertile(
    sx: int, sy: int,
    supertile_size: int,
    output_grid: OutputGrid,
    intermediate_dir: Path,
    filter_names: list[str],
    rgb_config: RGBConfig,
    stretch_params: RGBStretchParams,
    tile_dir: Path,
    tile_size: int,
    max_zoom: int,
    n_tiles_y: int,
    overwrite: bool = False,
) -> tuple[int, int]:
    """
    Pass 2 worker: load per-filter .npy intermediates, apply RGB stretch,
    slice into PNG tiles, and clean up .npy files.

    Returns (n_tiles_written, total_bytes).
    """
    x0 = sx * supertile_size
    y0 = sy * supertile_size
    nx = min(supertile_size, output_grid.naxis1 - x0)
    ny = min(supertile_size, output_grid.naxis2 - y0)

    tiles_per_st = supertile_size // tile_size
    base_tx = sx * tiles_per_st
    base_ty = sy * tiles_per_st

    # Sentinel file for resume (same pattern as single-filter tiles)
    sentinel = tile_dir / str(max_zoom) / f".st_{sx}_{sy}.done"
    if not overwrite and sentinel.exists():
        n_existing = 0
        existing_bytes = 0
        for lty in range(tiles_per_st):
            for ltx in range(tiles_per_st):
                tx = base_tx + ltx
                ty = base_ty + lty
                leaflet_y = n_tiles_y - 1 - ty
                tile_path = (
                    tile_dir / str(max_zoom) / str(tx) / f"{leaflet_y}.png"
                )
                if tile_path.exists():
                    n_existing += 1
                    existing_bytes += tile_path.stat().st_size
        return (n_existing, existing_bytes)

    # Load per-filter intermediate data
    per_filter_data = {}
    for filt in filter_names:
        npy_path = intermediate_dir / f"{filt}_{sx}_{sy}.npy"
        if npy_path.exists():
            per_filter_data[filt] = np.load(npy_path)

    if not per_filter_data:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return (0, 0)

    # Apply RGB stretch to full supertile at once
    rgb_data, alpha = apply_rgb_stretch(
        per_filter_data, rgb_config, stretch_params
    )

    # Slice into 256x256 tiles
    n_tiles = 0
    total_bytes = 0

    for lty in range(tiles_per_st):
        for ltx in range(tiles_per_st):
            tx = base_tx + ltx
            ty = base_ty + lty

            px0 = ltx * tile_size
            py0 = lty * tile_size
            px1 = min(px0 + tile_size, nx)
            py1 = min(py0 + tile_size, ny)

            if px0 >= nx or py0 >= ny:
                continue

            tile_alpha = alpha[py0:py1, px0:px1]
            if not np.any(tile_alpha > 0):
                if overwrite:
                    leaflet_y = n_tiles_y - 1 - ty
                    stale = (
                        tile_dir / str(max_zoom) / str(tx) / f"{leaflet_y}.png"
                    )
                    stale.unlink(missing_ok=True)
                continue

            leaflet_y = n_tiles_y - 1 - ty
            tile_path = (
                tile_dir / str(max_zoom) / str(tx) / f"{leaflet_y}.png"
            )

            if not overwrite and tile_path.exists():
                n_tiles += 1
                total_bytes += tile_path.stat().st_size
                continue

            tile_rgb = rgb_data[py0:py1, px0:px1]  # (h, w, 3)

            # Build RGBA (pad to tile_size if at edge)
            rgba = np.zeros((tile_size, tile_size, 4), dtype=np.uint8)
            h, w = tile_rgb.shape[:2]
            rgba[:h, :w, 0:3] = tile_rgb
            rgba[:h, :w, 3] = tile_alpha

            tile_path.parent.mkdir(parents=True, exist_ok=True)
            img = Image.fromarray(np.flipud(rgba), 'RGBA')
            img.save(tile_path, 'PNG', compress_level=PNG_COMPRESS_LEVEL)

            n_tiles += 1
            total_bytes += tile_path.stat().st_size

    # Mark supertile as complete
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    # Clean up intermediate .npy files for this supertile
    for filt in filter_names:
        npy_path = intermediate_dir / f"{filt}_{sx}_{sy}.npy"
        npy_path.unlink(missing_ok=True)

    return (n_tiles, total_bytes)


def generate_rgb_max_zoom_tiles(
    output_grid: OutputGrid,
    per_filter_input_infos: dict[str, list[InputFileInfo]],
    rgb_config: RGBConfig,
    stretch_params: RGBStretchParams,
    tile_dir: Path,
    tile_size: int,
    max_zoom: int,
    n_workers: int = 1,
    overwrite: bool = False,
) -> tuple[int, int]:
    """
    Generate RGB tiles at max zoom using two-pass filter-major reprojection.

    Pass 1 (filter-major): For each filter sequentially, reproject ALL
    supertiles in parallel and save intermediate .npy arrays. This keeps
    each filter's FITS files hot in the OS page cache instead of thrashing
    across all filters per supertile.

    Pass 2 (RGB combine): For each supertile in parallel, load the per-filter
    .npy intermediates, apply RGB stretch, and slice into PNG tiles. Clean up
    .npy files after each supertile.

    Returns (total_tiles, total_bytes).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    n_tiles_x = int(math.ceil(output_grid.naxis1 / tile_size))
    n_tiles_y = int(math.ceil(output_grid.naxis2 / tile_size))
    n_st_x = int(math.ceil(output_grid.naxis1 / SUPERTILE_SIZE))
    n_st_y = int(math.ceil(output_grid.naxis2 / SUPERTILE_SIZE))
    total_supertiles = n_st_x * n_st_y

    n_filters = len(rgb_config.filter_channels)
    logger.info(
        f"RGB max zoom {max_zoom}: {n_tiles_x}x{n_tiles_y} tiles via "
        f"{n_st_x}x{n_st_y} supertiles ({SUPERTILE_SIZE}px), "
        f"{n_filters} filters, {n_workers} thread(s)"
        f"{'' if overwrite else ', skipping existing'}"
    )

    # Build supertile positions: overlap if ANY filter overlaps
    all_positions = []
    for sy in range(n_st_y):
        for sx in range(n_st_x):
            x0 = sx * SUPERTILE_SIZE
            y0 = sy * SUPERTILE_SIZE
            for infos in per_filter_input_infos.values():
                if find_overlapping_inputs(infos, x0, y0, SUPERTILE_SIZE):
                    all_positions.append((sx, sy))
                    break

    total_with_overlap = len(all_positions)
    logger.info(
        f"  {total_with_overlap}/{total_supertiles} supertiles have "
        f"overlapping inputs"
    )

    # Intermediate directory for per-filter .npy files
    intermediate_dir = tile_dir / RGB_INTERMEDIATE_DIR
    intermediate_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Pass 1: Filter-major reprojection
    # ------------------------------------------------------------------
    # Process one filter at a time across ALL supertiles so each filter's
    # FITS files stay hot in the OS page cache. Uses ThreadPoolExecutor
    # so all threads share the same pre-opened memmap'd files (single
    # process = single page table, no duplication).

    for filt in rgb_config.filter_channels:
        # Skip entire filter if already done (resume support)
        filter_sentinel = intermediate_dir / f".filter_{filt}.done"
        if not overwrite and filter_sentinel.exists():
            logger.info(f"  Skipping {filt}: already reprojected")
            continue

        # Pre-open this filter's FITS files (shared across threads)
        infos = per_filter_input_infos[filt]
        path_to_hdu: dict[str, object] = {}
        open_hduls = []
        for info in infos:
            path_str = str(info.path)
            hdul = fits.open(info.path, memmap=True)
            open_hduls.append(hdul)
            for ext_name in ['SCI', 0]:
                try:
                    hdu = hdul[ext_name]
                    if hdu.data is not None and len(hdu.data.shape) == 2:
                        if rgb_config.mask_wht:
                            wht_path = _find_wht_path(info.path)
                            if wht_path is not None:
                                data = hdu.data.astype(np.float32)
                                with fits.open(
                                    wht_path, memmap=True
                                ) as wht_hdul:
                                    wht_ext = _get_ext(
                                        wht_hdul, ['WHT', 0], data.shape
                                    )
                                    if wht_ext is not None:
                                        data[wht_ext.data == 0] = np.nan
                                path_to_hdu[path_str] = (
                                    data, WCS(hdu.header, naxis=2)
                                )
                            else:
                                path_to_hdu[path_str] = hdu
                        else:
                            path_to_hdu[path_str] = hdu
                        break
                except (KeyError, IndexError):
                    continue

        # Build per-filter work list (only supertiles with overlap)
        filter_positions = []
        for sx, sy in all_positions:
            if overwrite:
                npy_path = intermediate_dir / f"{filt}_{sx}_{sy}.npy"
                npy_path.unlink(missing_ok=True)
            x0 = sx * SUPERTILE_SIZE
            y0 = sy * SUPERTILE_SIZE
            if find_overlapping_inputs(infos, x0, y0, SUPERTILE_SIZE):
                # Skip if .npy already exists (intra-filter resume)
                if not overwrite:
                    npy_path = intermediate_dir / f"{filt}_{sx}_{sy}.npy"
                    if npy_path.exists():
                        continue
                filter_positions.append((sx, sy))

        if not filter_positions:
            logger.info(f"  {filt}: nothing to reproject")
            for hdul in open_hduls:
                hdul.close()
            del path_to_hdu, open_hduls
            gc.collect()
            filter_sentinel.touch()
            continue

        def reproject_one(pos, _infos=infos, _p2h=path_to_hdu, _filt=filt):
            return _reproject_filter_supertile(
                pos[0], pos[1], SUPERTILE_SIZE,
                output_grid, _infos, _p2h,
                intermediate_dir, _filt,
            )

        pbar = tqdm(
            total=len(filter_positions),
            desc=f"Reproject {filt}", unit="st", smoothing=0.05,
        )

        if n_workers <= 1:
            for pos in filter_positions:
                reproject_one(pos)
                pbar.update(1)
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as executor:
                futures = {
                    executor.submit(reproject_one, pos): pos
                    for pos in filter_positions
                }
                for future in as_completed(futures):
                    future.result()  # propagate exceptions
                    pbar.update(1)

        pbar.close()

        # Release this filter's memmap'd data before moving to the next.
        # hdul.close() closes the file descriptor but numpy memmap arrays
        # survive if referenced. Explicitly delete all references and force
        # GC to avoid accumulating ~5 GB per filter across 6 passes.
        for hdul in open_hduls:
            hdul.close()
        del path_to_hdu, open_hduls
        gc.collect()

        filter_sentinel.touch()
        logger.info(f"  {filt}: reprojected {len(filter_positions)} supertiles")

    # ------------------------------------------------------------------
    # Pass 2: RGB combine + PNG slicing
    # ------------------------------------------------------------------
    # Reads .npy intermediates (small, sequential) — no FITS I/O,
    # so ThreadPoolExecutor is ideal (shared memory, no GIL contention
    # for numpy/PIL C code).

    # Pre-filter sentinels so the progress bar only tracks real work
    tiles_per_st = SUPERTILE_SIZE // tile_size
    work_positions = []
    skipped_tiles = 0
    skipped_bytes = 0

    if overwrite:
        work_positions = list(all_positions)
    else:
        for sx, sy in all_positions:
            sentinel = tile_dir / str(max_zoom) / f".st_{sx}_{sy}.done"
            if sentinel.exists():
                base_tx = sx * tiles_per_st
                base_ty = sy * tiles_per_st
                for lty in range(tiles_per_st):
                    for ltx in range(tiles_per_st):
                        tx = base_tx + ltx
                        ty = base_ty + lty
                        leaflet_y = n_tiles_y - 1 - ty
                        tile_path = (
                            tile_dir / str(max_zoom) / str(tx)
                            / f"{leaflet_y}.png"
                        )
                        if tile_path.exists():
                            skipped_tiles += 1
                            skipped_bytes += tile_path.stat().st_size
            else:
                work_positions.append((sx, sy))

    total_tiles = skipped_tiles
    total_bytes = skipped_bytes

    if skipped_tiles or len(work_positions) < len(all_positions):
        logger.info(
            f"  {len(all_positions) - len(work_positions)} supertiles "
            f"already combined"
        )

    if not work_positions:
        logger.info(
            f"  RGB max zoom {max_zoom}: all supertiles already done, "
            f"{total_tiles} tiles ({total_bytes / (1024 * 1024):.1f} MB)"
        )
    else:
        filter_names = list(rgb_config.filter_channels.keys())

        def combine_one(pos):
            return _combine_rgb_supertile(
                pos[0], pos[1], SUPERTILE_SIZE,
                output_grid, intermediate_dir, filter_names,
                rgb_config, stretch_params, tile_dir,
                tile_size, max_zoom, n_tiles_y, overwrite,
            )

        pbar = tqdm(
            total=len(work_positions),
            desc="RGB combine", unit="st", smoothing=0.05,
        )

        if n_workers <= 1:
            for pos in work_positions:
                n, nbytes = combine_one(pos)
                total_tiles += n
                total_bytes += nbytes
                pbar.update(1)
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as executor:
                futures = {
                    executor.submit(combine_one, pos): pos
                    for pos in work_positions
                }
                for future in as_completed(futures):
                    n, nbytes = future.result()
                    total_tiles += n
                    total_bytes += nbytes
                    pbar.update(1)

        pbar.close()

    # Clean up intermediate directory
    if intermediate_dir.exists():
        shutil.rmtree(intermediate_dir)
        logger.info("Cleaned up intermediate directory")

    logger.info(
        f"  RGB max zoom {max_zoom}: {total_tiles} non-empty tiles "
        f"({total_bytes / (1024 * 1024):.1f} MB)"
    )

    return total_tiles, total_bytes


def generate_tiles_for_rgb(
    rgb_config: RGBConfig,
    n_workers: int = 1,
    overwrite: bool = False,
    output_grid: OutputGrid | None = None,
) -> TileStats:
    """
    Full RGB tile generation pipeline.

    1. Use provided grid or compute from all filter files
    2. Precompute per-filter input bounding boxes
    3. Compute global RGB stretch parameters
    4. Generate max zoom RGB tiles
    5. Build lower zoom levels (unchanged, handles RGBA)
    6. Return TileStats with filter_name='rgb'
    """
    n_filters = len(rgb_config.filter_channels)
    n_files = sum(len(f['files']) for f in rgb_config.filter_channels.values())
    logger.info(
        f"=== Generating RGB tiles: {rgb_config.field} "
        f"({n_filters} filters, {n_files} input files) ==="
    )

    # Step 1: Use provided grid or compute from all filter files
    if output_grid is None:
        all_files = {
            filt: info['files']
            for filt, info in rgb_config.filter_channels.items()
        }
        output_grid = compute_field_grid(
            all_files, rgb_config.output_pixel_scale_arcsec
        )
    else:
        logger.info("Using provided unified field grid")

    min_zoom, max_zoom = compute_zoom_range(
        output_grid.naxis1, output_grid.naxis2, rgb_config.tile_size
    )

    # Step 2: Precompute per-filter input bounding boxes
    logger.info("Precomputing per-filter input file bounding boxes...")
    per_filter_input_infos = {}
    for filt, info in rgb_config.filter_channels.items():
        per_filter_input_infos[filt] = precompute_input_bboxes(
            info['files'], output_grid
        )

    # Step 3: Compute global RGB stretch parameters
    import time as _time
    _t0 = _time.monotonic()
    stretch_params = compute_rgb_stretch_params(rgb_config)
    print(f"  Stretch params computed in {_time.monotonic() - _t0:.1f}s")

    tile_dir = rgb_config.output_dir / rgb_config.field / 'rgb'

    # Step 4: Generate max zoom tiles
    max_tiles, max_bytes = generate_rgb_max_zoom_tiles(
        output_grid=output_grid,
        per_filter_input_infos=per_filter_input_infos,
        rgb_config=rgb_config,
        stretch_params=stretch_params,
        tile_dir=tile_dir,
        tile_size=rgb_config.tile_size,
        max_zoom=max_zoom,
        n_workers=n_workers,
        overwrite=overwrite,
    )

    # Step 5: Build lower zoom levels (already handles RGBA)
    lower_tiles, lower_bytes = build_lower_zoom_levels(
        tile_dir=tile_dir,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        tile_size=rgb_config.tile_size,
        naxis1=output_grid.naxis1,
        naxis2=output_grid.naxis2,
        overwrite=overwrite,
        n_workers=n_workers,
    )

    total_tiles = max_tiles + lower_tiles
    total_bytes = max_bytes + lower_bytes

    # Compute sky bounds
    wcs = output_grid.to_wcs()
    corners = np.array([
        [0, 0],
        [output_grid.naxis1 - 1, 0],
        [output_grid.naxis1 - 1, output_grid.naxis2 - 1],
        [0, output_grid.naxis2 - 1],
    ], dtype=float)
    corner_sky = wcs.pixel_to_world_values(corners[:, 0], corners[:, 1])

    stats = TileStats(
        field=rgb_config.field,
        filter_name='rgb',
        num_input_files=n_files,
        output_grid_size=(output_grid.naxis1, output_grid.naxis2),
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        total_tiles=total_tiles,
        total_size_bytes=total_bytes,
        ra_min=float(np.min(corner_sky[0])),
        ra_max=float(np.max(corner_sky[0])),
        dec_min=float(np.min(corner_sky[1])),
        dec_max=float(np.max(corner_sky[1])),
        wcs_params=output_grid.to_json(),
    )

    logger.info(
        f"=== Done: {total_tiles} RGB tiles, "
        f"{total_bytes / (1024 * 1024):.1f} MB ==="
    )

    return stats


def estimate_tiles_for_rgb(
    rgb_config: RGBConfig,
    output_grid: OutputGrid | None = None,
) -> dict:
    """Dry-run estimation for RGB tiles (same math as single-filter)."""
    if output_grid is None:
        all_files = {
            filt: info['files']
            for filt, info in rgb_config.filter_channels.items()
        }
        output_grid = compute_field_grid(
            all_files, rgb_config.output_pixel_scale_arcsec
        )

    min_zoom, max_zoom = compute_zoom_range(
        output_grid.naxis1, output_grid.naxis2, rgb_config.tile_size
    )

    total_tiles = 0
    for zoom in range(min_zoom, max_zoom + 1):
        scale = 2 ** (max_zoom - zoom)
        nx_tiles = int(math.ceil(
            output_grid.naxis1 / scale / rgb_config.tile_size
        ))
        ny_tiles = int(math.ceil(
            output_grid.naxis2 / scale / rgb_config.tile_size
        ))
        total_tiles += int(nx_tiles * ny_tiles * 0.6)

    # RGB tiles are larger than grayscale (~80KB vs ~40KB)
    avg_tile_bytes = 80 * 1024
    estimated_bytes = total_tiles * avg_tile_bytes

    n_files = sum(len(f['files']) for f in rgb_config.filter_channels.values())

    return {
        'field': rgb_config.field,
        'filter': 'rgb',
        'input_files': n_files,
        'num_filters': len(rgb_config.filter_channels),
        'output_width': output_grid.naxis1,
        'output_height': output_grid.naxis2,
        'pixel_scale_arcsec': rgb_config.output_pixel_scale_arcsec,
        'min_zoom': min_zoom,
        'max_zoom': max_zoom,
        'estimated_tiles': total_tiles,
        'estimated_size_mb': estimated_bytes / (1024 * 1024),
        'estimated_size_gb': estimated_bytes / (1024 * 1024 * 1024),
    }


def generate_rgb_preview(
    rgb_config: RGBConfig,
    output_grid: OutputGrid,
    output_path: Path | None = None,
    center_ra: float | None = None,
    center_dec: float | None = None,
    preview_size: int = 2048,
) -> Path:
    """
    Generate an RGB preview image from a single supertile-sized region.

    If no center coordinates given, picks the supertile with the most
    filter coverage (best representative region).
    """
    logger.info("Generating RGB preview...")

    # Precompute per-filter bboxes
    per_filter_input_infos = {}
    for filt, info in rgb_config.filter_channels.items():
        per_filter_input_infos[filt] = precompute_input_bboxes(
            info['files'], output_grid
        )

    if center_ra is not None and center_dec is not None:
        # Convert sky coords to pixel coords
        wcs = output_grid.to_wcs()
        cx, cy = wcs.world_to_pixel_values(center_ra, center_dec)
        x0 = max(0, int(cx) - preview_size // 2)
        y0 = max(0, int(cy) - preview_size // 2)
    else:
        # Find supertile with most filter overlap
        n_st_x = int(math.ceil(output_grid.naxis1 / SUPERTILE_SIZE))
        n_st_y = int(math.ceil(output_grid.naxis2 / SUPERTILE_SIZE))

        best_sx, best_sy = 0, 0
        best_count = 0

        for sy in range(n_st_y):
            for sx in range(n_st_x):
                st_x0 = sx * SUPERTILE_SIZE
                st_y0 = sy * SUPERTILE_SIZE
                count = 0
                for filt, infos in per_filter_input_infos.items():
                    if find_overlapping_inputs(
                        infos, st_x0, st_y0, SUPERTILE_SIZE
                    ):
                        count += 1
                if count > best_count:
                    best_count = count
                    best_sx, best_sy = sx, sy

        x0 = best_sx * SUPERTILE_SIZE
        y0 = best_sy * SUPERTILE_SIZE
        logger.info(
            f"  Best supertile ({best_sx}, {best_sy}): "
            f"{best_count}/{len(per_filter_input_infos)} filters"
        )

    nx = min(preview_size, output_grid.naxis1 - x0)
    ny = min(preview_size, output_grid.naxis2 - y0)

    # Compute global stretch params
    stretch_params = compute_rgb_stretch_params(rgb_config)

    # Reproject each filter onto the preview region
    st_header = output_grid.sub_header(x0, y0, nx, ny)
    per_filter_data = {}

    for filt, infos in per_filter_input_infos.items():
        overlapping = find_overlapping_inputs(infos, x0, y0, preview_size)
        if not overlapping:
            continue

        hdus, open_hduls = _open_filter_hdus(overlapping, rgb_config.mask_wht)
        try:
            if hdus:
                data, footprint = _reproject_tile(hdus, st_header)
                data[footprint < 0.5] = np.nan
                per_filter_data[filt] = data[:ny, :nx]
        finally:
            for hdul in open_hduls:
                hdul.close()

    if not per_filter_data:
        raise ValueError("No filter data found in preview region")

    # Apply RGB stretch
    rgb_data, alpha = apply_rgb_stretch(
        per_filter_data, rgb_config, stretch_params
    )

    # Build RGBA and save
    rgba = np.zeros((ny, nx, 4), dtype=np.uint8)
    rgba[:, :, 0:3] = rgb_data
    rgba[:, :, 3] = alpha

    if output_path is None:
        output_path = rgb_config.output_dir / rgb_config.field / 'rgb_preview.png'
    output_path.parent.mkdir(parents=True, exist_ok=True)

    img = Image.fromarray(np.flipud(rgba), 'RGBA')
    img.save(output_path, 'PNG')

    logger.info(f"  Preview saved to {output_path} ({nx}x{ny} px)")
    logger.info(
        f"  Stretch params: blackpoint={stretch_params.blackpoint:.4e}, "
        f"whitepoint={stretch_params.whitepoint:.4e}, "
        f"noiselum={stretch_params.noiselum}"
    )

    return output_path
