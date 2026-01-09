#!/usr/bin/env python3
"""
CAMPFIRE Deployment Script

Deploys reduced NIRSpec spectra, RGB images, and SED plots to Supabase (metadata) and Cloudflare R2 (files).

Usage:
    # Full deployment (spectra + RGB images)
    # Version is read from CMPFRVER header in FITS files (defaults to v0.1 if missing)
    python scripts/deploy.py --obs cosmos_ddt

    # Deploy specific source IDs only
    python scripts/deploy.py --obs cosmos_ddt --source-ids 12345 67890

    # RGB images only
    python scripts/deploy.py --obs ember_uds_p4 --rgb-only

    # RGB images for specific source IDs
    python scripts/deploy.py --obs ember_uds_p4 --rgb-only --source-ids 12345

    # SED plots only
    python scripts/deploy.py --obs ember_uds_p4 --sed-only

    # SED plots for specific source IDs
    python scripts/deploy.py --obs ember_uds_p4 --sed-only --source-ids 12345 67890

    # Spectra only (no RGB)
    python scripts/deploy.py --obs cosmos_ddt --no-rgb

    # Other options
    python scripts/deploy.py --obs cosmos_ddt --dry-run
    python scripts/deploy.py --obs cosmos_ddt --force-overwrite  # Reset inspection data

Behavior:
    - New objects: Inserted with default values for all fields
    - Existing objects (default): Only pipeline fields (ra, dec, redshift_auto) are updated;
      user inspection data (redshift_inspected, quality, flags) is preserved
    - Existing objects (--force-overwrite): ALL fields reset including user inspection data
    - RGB images: Uploaded to rgb/{obs_name}/{object_id}_rgb.png
    - SED plots: Uploaded to sed/{obs_name}/{object_id}_sed.pdf
"""

import argparse
import json
import sys
from pathlib import Path
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NamedTuple

import numpy as np
from astropy.io import fits

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    # Fallback: simple progress indicator
    def tqdm(iterable=None, total=None, desc=None, unit=None):
        if iterable is not None:
            return iterable
        # For context manager usage, return a dummy class
        class DummyProgress:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def update(self, n=1): pass
        return DummyProgress()

# Third-party imports for cloud services
try:
    import tomllib
except ImportError:
    import tomli as tomllib

try:
    from supabase import create_client, Client
    HAS_SUPABASE = True
except ImportError:
    HAS_SUPABASE = False
    print("Warning: supabase-py not installed. Install with: pip install supabase")

try:
    import boto3
    from botocore.config import Config
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False
    print("Warning: boto3 not installed. Install with: pip install boto3")


# === Configuration Loading ===

def load_toml(path: Path) -> dict:
    """Load a TOML file."""
    with open(path, 'rb') as f:
        return tomllib.load(f)


def load_config(scripts_dir: Path) -> dict:
    """Load deployment configuration from config.toml."""
    config_path = scripts_dir / 'config.toml'
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        print(f"Copy config.example.toml to config.toml and fill in your credentials.")
        sys.exit(1)
    return load_toml(config_path)


def load_programs(scripts_dir: Path) -> dict:
    """Load program definitions from programs.toml."""
    programs_path = scripts_dir / 'programs.toml'
    if not programs_path.exists():
        print(f"Error: Programs file not found: {programs_path}")
        sys.exit(1)
    data = load_toml(programs_path)
    # Convert list to dict keyed by program_id
    return {p['program_id']: p for p in data.get('programs', [])}


def load_observations(pipeline_dir: Path) -> dict:
    """Load observation definitions from observations.toml."""
    obs_path = pipeline_dir / 'observations.toml'
    if not obs_path.exists():
        print(f"Error: Observations file not found: {obs_path}")
        sys.exit(1)
    return load_toml(obs_path)


# === FITS File Discovery and Reading ===

def discover_fits_files(products_dir: Path, obs_name: str) -> list[Path]:
    """Find all *_spec.fits files for an observation."""
    obs_dir = products_dir / obs_name
    if not obs_dir.exists():
        print(f"Error: Observation directory not found: {obs_dir}")
        sys.exit(1)

    fits_files = sorted(obs_dir.glob('*_spec.fits'))
    if not fits_files:
        print(f"Error: No *_spec.fits files found in {obs_dir}")
        sys.exit(1)

    return fits_files


def discover_rgb_images(products_dir: Path, obs_name: str) -> list[Path]:
    """Find all *_rgb.png files for an observation."""
    obs_dir = products_dir / obs_name
    if not obs_dir.exists():
        print(f"Error: Observation directory not found: {obs_dir}")
        sys.exit(1)

    rgb_files = sorted(obs_dir.glob('*_rgb.png'))
    return rgb_files


def discover_sed_plots(products_dir: Path, obs_name: str) -> list[Path]:
    """Find all *_sed.pdf files for an observation."""
    obs_dir = products_dir / obs_name
    if not obs_dir.exists():
        print(f"Error: Observation directory not found: {obs_dir}")
        sys.exit(1)

    sed_files = sorted(obs_dir.glob('*_sed.pdf'))
    return sed_files


def discover_zfit_files(products_dir: Path, obs_name: str) -> dict[str, Path]:
    """
    Find all *_zfit.fits files for an observation.

    Returns a dict mapping from spec filename base to zfit Path.
    Example: 'ember_uds_p1_prism_clear_18509' -> Path to zfit file
    """
    obs_dir = products_dir / obs_name
    if not obs_dir.exists():
        return {}

    zfit_files = sorted(obs_dir.glob('*_zfit.fits'))

    # Create mapping from base filename to zfit path
    zfit_map = {}
    for zfit_path in zfit_files:
        # Remove _zfit.fits suffix to get base
        base = zfit_path.stem.replace('_zfit', '')
        zfit_map[base] = zfit_path

    return zfit_map


def filter_files_by_source_ids(files: list[Path], source_ids: list[str], obs_name: str) -> list[Path]:
    """
    Filter file list to only include files matching the specified source IDs.

    Handles multiple filename patterns:
    - Spectra: {obs_name}_{grating}_{filter}_{source_id}_spec.fits
    - RGB: {obs_name}_{source_id}_rgb.png
    - SED: {obs_name}_{source_id}_sed.pdf
    - Zfit: {obs_name}_{grating}_{filter}_{source_id}_zfit.fits

    Args:
        files: List of file paths to filter
        source_ids: List of allowed source IDs (as strings)
        obs_name: Observation name (needed to extract source_id correctly)

    Returns:
        Filtered list of files matching the allowed source IDs
    """
    if not source_ids:
        return files

    # Convert source_ids to set for faster lookup
    allowed_ids = set(source_ids)
    filtered = []

    for file_path in files:
        filename = file_path.name
        extracted_id = None

        # Determine file type and extract source_id accordingly
        if filename.endswith('_spec.fits') or filename.endswith('_zfit.fits'):
            # FITS files: use parse_fits_filename logic
            try:
                parsed = parse_fits_filename(filename)
                extracted_id = parsed['source_id']
            except ValueError:
                # If parsing fails, skip this file
                continue

        elif filename.endswith('_rgb.png'):
            # RGB: {obs_name}_{source_id}_rgb.png
            base = filename.replace('_rgb.png', '')
            # Remove obs_name prefix
            if base.startswith(obs_name + '_'):
                extracted_id = base[len(obs_name) + 1:]

        elif filename.endswith('_sed.pdf'):
            # SED: {obs_name}_{source_id}_sed.pdf
            base = filename.replace('_sed.pdf', '')
            # Remove obs_name prefix
            if base.startswith(obs_name + '_'):
                extracted_id = base[len(obs_name) + 1:]

        # Include file if source_id matches
        if extracted_id and extracted_id in allowed_ids:
            filtered.append(file_path)

    return filtered


def parse_fits_filename(filename: str) -> dict:
    """
    Parse a FITS filename to extract components.

    Expected pattern: {obs_name}_{grating}_{filter}_{source_id}_spec.fits
    Example: cosmos_ddt_prism_clear_66964_spec.fits
    """
    # Remove _spec.fits suffix
    base = filename.replace('_spec.fits', '')

    # Split from the right to handle obs_name with underscores
    parts = base.rsplit('_', 3)
    if len(parts) < 4:
        raise ValueError(f"Cannot parse filename: {filename}")

    # parts = [obs_name_prefix, grating, filter, source_id]
    # But obs_name might have multiple underscores, so we need to be smarter
    # Pattern: everything before _{grating}_{filter}_{source_id}

    # Known gratings and filters
    gratings = {'prism', 'g140m', 'g235m', 'g395m', 'g140h', 'g235h', 'g395h'}
    filters = {'clear', 'f070lp', 'f100lp', 'f170lp', 'f290lp'}

    # Find grating position by scanning from right
    parts = base.split('_')
    grating_idx = None
    for i, part in enumerate(parts):
        if part.lower() in gratings:
            grating_idx = i
            break

    if grating_idx is None or grating_idx + 2 >= len(parts):
        raise ValueError(f"Cannot parse filename: {filename}")

    obs_name = '_'.join(parts[:grating_idx])
    grating = parts[grating_idx].upper()
    filter_name = parts[grating_idx + 1].upper()
    source_id = parts[grating_idx + 2]

    return {
        'obs_name': obs_name,
        'grating': grating,
        'filter': filter_name,
        'source_id': source_id,
    }


def read_zfit_data(zfit_path: Path) -> dict | None:
    """
    Read redshift fitting results from a zfit FITS file.

    Returns dict with:
        - redshift: best-fit redshift (at minimum chi2)
        - chi2_min: minimum chi-squared value
        - confidence: confidence value (0-100 from ZCONF header)
        - model_wave: wavelength array of best-fit model
        - model_fnu: flux array of best-fit model
    Returns None if file doesn't exist or can't be read.
    """
    if not zfit_path or not zfit_path.exists():
        return None

    try:
        with fits.open(zfit_path) as hdul:
            primary = hdul['PRIMARY'].header

            # Read CHI2 extension to find best redshift
            chi2_data = hdul['CHI2'].data
            chi2_values = chi2_data['chi2']
            z_grid = chi2_data['z']

            # Find minimum chi2 and corresponding redshift
            min_idx = np.argmin(chi2_values)
            z_best = float(z_grid[min_idx])
            chi2_min = float(chi2_values[min_idx])

            # Read confidence from header
            confidence = float(primary.get('ZCONF', 0.0))

            # Read MODEL extension for best-fit spectrum
            model_data = hdul['MODEL'].data
            model_wave = model_data['wav'].tolist()
            model_fnu = model_data['fnu'].tolist()

            return {
                'redshift': z_best,
                'chi2_min': chi2_min,
                'confidence': confidence,
                'model_wave': model_wave,
                'model_fnu': model_fnu,
            }
    except Exception as e:
        print(f"  Warning: Failed to read zfit file {zfit_path.name}: {e}")
        return None


def determine_best_redshift(zfit_data_by_grating: dict[str, dict]) -> float | None:
    """
    Apply decision tree to choose the best redshift for an object from multiple spectra.

    Decision logic:
    1. If PRISM available and no gratings: use PRISM
    2. If gratings available and no PRISM: use grating with lowest chi2
    3. If both PRISM and gratings available:
       - Check if they agree (|z_prism - z_grating| < 0.1)
       - If agree: use grating (more precise)
       - If disagree: use PRISM (more robust)

    Args:
        zfit_data_by_grating: Dict mapping grating names to zfit data dicts
                              Example: {'PRISM': {...}, 'G140M': {...}}

    Returns:
        Best redshift value, or None if no valid data
    """
    if not zfit_data_by_grating:
        return None

    # Separate PRISM from gratings
    prism_data = zfit_data_by_grating.get('PRISM')
    grating_data = {g: d for g, d in zfit_data_by_grating.items() if g != 'PRISM'}

    # Case 1: Only PRISM
    if prism_data and not grating_data:
        return prism_data['redshift']

    # Case 2: Only gratings (no PRISM)
    if grating_data and not prism_data:
        # Choose grating with lowest chi2
        best_grating = min(grating_data.items(), key=lambda x: x[1]['chi2_min'])
        return best_grating[1]['redshift']

    # Case 3: Both PRISM and gratings
    if prism_data and grating_data:
        z_prism = prism_data['redshift']

        # Find best grating (lowest chi2)
        best_grating_name, best_grating_data = min(grating_data.items(), key=lambda x: x[1]['chi2_min'])
        z_grating = best_grating_data['redshift']

        # Check agreement
        dz = abs(z_prism - z_grating)
        if dz < 0.1:
            # Agree: use grating (more precise)
            return z_grating
        else:
            # Disagree: use PRISM (more robust)
            return z_prism

    return None


def read_fits_metadata(fits_path: Path, obs_name: str) -> dict:
    """
    Read metadata from a FITS file.

    Returns a dict with all relevant metadata for database insertion.
    """
    with fits.open(fits_path) as hdul:
        primary = hdul['PRIMARY'].header
        sci = hdul['SCI'].header
        spec1d = hdul['SPEC1D'].data

        # Parse filename for grating/filter/source_id
        parsed = parse_fits_filename(fits_path.name)

        # Calculate S/N
        fnu = spec1d['fnu']
        fnu_err = spec1d['fnu_err']
        valid = ~np.isnan(fnu) & ~np.isnan(fnu_err) & (fnu_err > 0)
        if valid.sum() > 0:
            sn = fnu[valid] / fnu_err[valid]
            max_sn = float(np.nanmax(sn))
        else:
            max_sn = None

        # Build object_id string
        object_id = f"{obs_name}_{parsed['source_id']}"

        # Try to get redshift from header (may be set by fitting step)
        redshift_auto = None
        for key in ['REDSHIFT', 'Z', 'Z_SPEC', 'Z_BEST']:
            if key in primary:
                val = primary[key]
                if val is not None and not isinstance(val, str):
                    redshift_auto = float(val)
                    break
            if key in sci:
                val = sci[key]
                if val is not None and not isinstance(val, str):
                    redshift_auto = float(val)
                    break

        # Get reduction version from FITS header
        reduction_version = primary.get('CMPFRVER', 'v0.1')

        return {
            # From filename parsing
            'object_id': object_id,
            'source_id': parsed['source_id'],
            'grating': parsed['grating'],
            'filter': parsed['filter'],

            # From PRIMARY header
            'program_id': int(primary.get('PROGRAM', 0)),
            'pi_name': primary.get('PI_NAME', ''),
            'date_obs': primary.get('DATE-OBS', ''),
            'exposure_time': float(primary.get('EFFEXPTM', 0)),
            'cal_ver': primary.get('CAL_VER', ''),
            'reduction_version': reduction_version,

            # From SCI header
            'ra': float(sci.get('SRCRA', 0)),
            'dec': float(sci.get('SRCDEC', 0)),

            # Redshift from fitting (if available)
            'redshift_auto': redshift_auto,

            # Calculated
            'signal_to_noise': max_sn,

            # File reference
            'fits_filename': fits_path.name,
        }


def read_spectrum_data(fits_path: Path) -> dict:
    """
    Read spectrum data for JSON export (Plotly).

    Returns 1D data (wave, fnu, fnu_err), 2D S/N data for heatmap display,
    and cross-dispersion profile data.
    Uses compact precision to minimize file size:
    - 6 decimal places for wavelength and flux values
    - 2 decimal places for S/N and profile values
    """
    with fits.open(fits_path) as hdul:
        spec1d = hdul['SPEC1D'].data
        sci = hdul['SCI'].data
        err = hdul['ERR'].data
        wave_2d = hdul['WAVELENGTH'].data
        prof1d = hdul['PROF1D'].data

        # 1D spectrum data with compact precision
        wave = [round(x, 6) for x in spec1d['wave'].tolist()]
        fnu = [None if np.isnan(x) else round(float(x), 6) for x in spec1d['fnu']]
        fnu_err = [None if np.isnan(x) or np.isinf(x) else round(float(x), 6) for x in spec1d['fnu_err']]

        # 2D S/N calculation
        with np.errstate(divide='ignore', invalid='ignore'):
            snr_2d = sci / err
            # Replace non-finite values with 0
            snr_2d = np.where(np.isfinite(snr_2d), snr_2d, 0)

        # Convert to compact format (2 decimal places)
        snr_2d_list = [[round(x, 2) for x in row] for row in snr_2d.tolist()]

        # Cross-dispersion profile data
        # Collapsed spatial profile (median along wavelength axis)
        with np.errstate(divide='ignore', invalid='ignore'):
            collapsed = np.nanmedian(sci, axis=1)

        # Find center as weighted centroid of positive flux
        ypos = prof1d['ypos']
        opt_weight = prof1d['opt']

        # Compute center from optimal extraction weight
        valid_opt = opt_weight > 0
        if np.any(valid_opt):
            cen = np.average(ypos[valid_opt], weights=opt_weight[valid_opt])
        else:
            cen = np.median(ypos)

        # Center the pixel positions
        pix_centered = ypos - cen

        # Normalize profiles for display
        with np.errstate(divide='ignore', invalid='ignore'):
            # Normalize collapsed profile to peak of 1
            collapsed_norm = collapsed / np.nanmax(np.abs(collapsed[valid_opt])) if np.any(valid_opt) else collapsed
            collapsed_norm = np.where(np.isfinite(collapsed_norm), collapsed_norm, 0)

            # Normalize optimal weight to peak of 1
            opt_norm = opt_weight / np.nanmax(opt_weight) if np.nanmax(opt_weight) > 0 else opt_weight

        return {
            'wave': wave,
            'fnu': fnu,
            'fnu_err': fnu_err,
            'snr_2d': snr_2d_list,
            'n_spatial': sci.shape[0],
            'n_wave': sci.shape[1],
            # Cross-dispersion profile data
            'profile': [round(float(x), 3) for x in collapsed_norm.tolist()],
            'profile_fit': [round(float(x), 3) for x in opt_norm.tolist()],
            'profile_pix': [round(float(x), 2) for x in pix_centered.tolist()],
        }


# === File Generation ===

def generate_spectrum_json(fits_path: Path, output_dir: Path) -> Path:
    """Generate JSON file with spectrum data for Plotly (1D + 2D S/N)."""
    data = read_spectrum_data(fits_path)

    json_filename = fits_path.stem + '.json'
    json_path = output_dir / json_filename

    with open(json_path, 'w') as f:
        json.dump(data, f)

    return json_path


def generate_zfit_json(zfit_path: Path, output_dir: Path) -> Path:
    """
    Generate JSON file with redshift fitting results.

    Includes:
    - Best-fit redshift, chi2, and confidence
    - Full chi² vs redshift curve (z_grid, chi2_grid)
    - Best-fit model spectrum (model_wave, model_fnu)
    """
    with fits.open(zfit_path) as hdul:
        primary = hdul['PRIMARY'].header

        # Read CHI2 extension for full curve
        chi2_data = hdul['CHI2'].data
        z_grid = [round(float(z), 4) for z in chi2_data['z'].tolist()]
        chi2_grid = [round(float(c), 2) for c in chi2_data['chi2'].tolist()]

        # Find best redshift
        min_idx = np.argmin(chi2_data['chi2'])
        z_best = round(float(chi2_data['z'][min_idx]), 4)
        chi2_min = round(float(chi2_data['chi2'][min_idx]), 2)

        # Read confidence
        confidence = round(float(primary.get('ZCONF', 0.0)), 1)

        # Read MODEL extension
        model_data = hdul['MODEL'].data
        model_wave = [round(x, 6) for x in model_data['wav'].tolist()]
        model_fnu = [round(x, 6) for x in model_data['fnu'].tolist()]

        data = {
            'redshift': z_best,
            'chi2_min': chi2_min,
            'confidence': confidence,
            'z_grid': z_grid,
            'chi2_grid': chi2_grid,
            'model_wave': model_wave,
            'model_fnu': model_fnu,
        }

    json_filename = zfit_path.stem + '.json'
    json_path = output_dir / json_filename

    with open(json_path, 'w') as f:
        json.dump(data, f)

    return json_path


# === Supabase Integration ===

def get_supabase_client(config: dict) -> 'Client':
    """Create Supabase client from config."""
    if not HAS_SUPABASE:
        raise RuntimeError("supabase-py not installed")

    return create_client(
        config['supabase']['url'],
        config['supabase']['service_role_key']
    )


def check_existing_objects(supabase: 'Client', object_ids: list[str]) -> list[str]:
    """Check which object_ids already exist in the database."""
    if not object_ids:
        return []

    response = supabase.table('objects').select('object_id').in_('object_id', object_ids).execute()
    return [row['object_id'] for row in response.data]


def upsert_program(supabase: 'Client', program_id: int, programs_config: dict) -> None:
    """Upsert a program record."""
    program_info = programs_config.get(program_id, {})

    data = {
        'program_id': program_id,
        'program_name': program_info.get('program_name', f'Program {program_id}'),
        'pi_name': program_info.get('pi_name', ''),
        'description': program_info.get('description', ''),
        'is_public': program_info.get('is_public', False),
    }

    supabase.table('programs').upsert(data, on_conflict='program_id').execute()


def refresh_filter_options(supabase: 'Client') -> None:
    """
    Refresh the filter options materialized view after deployment.

    This updates the cached list of fields and observations shown in the web UI filter dropdowns.
    """
    print("  Refreshing filter options cache...")
    try:
        supabase.rpc('refresh_filter_options').execute()
        print("  ✓ Filter options cache refreshed")
    except Exception as e:
        print(f"  ⚠ Warning: Failed to refresh filter options cache: {e}")
        print("    Run manually in Supabase: SELECT refresh_filter_options();")


def upsert_object(supabase: 'Client', metadata: dict, obs_config: dict, force_overwrite: bool = False) -> None:
    """
    Upsert an object record.

    For NEW objects: insert with default values for inspection fields.
    For EXISTING objects (normal mode): only update pipeline fields (ra, dec, redshift_auto),
    preserving user-set inspection data.
    For EXISTING objects (force_overwrite): reset ALL fields including inspection data.
    """
    object_id = metadata['object_id']

    # Check if object already exists
    existing = supabase.table('objects').select('id').eq('object_id', object_id).execute()

    if existing.data and not force_overwrite:
        # UPDATE existing object - only pipeline fields, preserve user inspection data
        pipeline_data = {
            'program_id': metadata['program_id'],
            'field': obs_config.get('field', ''),
            'ra': metadata['ra'],
            'dec': metadata['dec'],
            'redshift_auto': metadata.get('redshift_auto'),
        }
        supabase.table('objects').update(pipeline_data).eq('object_id', object_id).execute()
    elif existing.data and force_overwrite:
        # FORCE UPDATE - reset all fields including inspection data
        full_data = {
            'program_id': metadata['program_id'],
            'field': obs_config.get('field', ''),
            'ra': metadata['ra'],
            'dec': metadata['dec'],
            'redshift_auto': metadata.get('redshift_auto'),
            'redshift_inspected': None,  # Reset user override
            'redshift_quality': 0,  # Reset to not inspected
            'spectral_features': 0,
            'object_flags': 0,
            'dq_flags': 0,
            'last_inspected_at': None,
            'last_inspected_by': None,
        }
        supabase.table('objects').update(full_data).eq('object_id', object_id).execute()
    else:
        # INSERT new object - include default values for inspection fields
        new_data = {
            'object_id': object_id,
            'program_id': metadata['program_id'],
            'field': obs_config.get('field', ''),
            'ra': metadata['ra'],
            'dec': metadata['dec'],
            'redshift_auto': metadata.get('redshift_auto'),
            'redshift_inspected': None,  # User override
            'redshift_quality': 0,  # Not inspected
            'spectral_features': 0,
            'object_flags': 0,
            'dq_flags': 0,
            'last_inspected_at': None,
            'last_inspected_by': None,
        }
        supabase.table('objects').insert(new_data).execute()


def upsert_spectrum(
    supabase: 'Client',
    object_id: str,
    metadata: dict,
    fits_r2_path: str
) -> None:
    """Upsert a spectrum record."""
    data = {
        'object_id': object_id,
        'grating': metadata['grating'],
        'fits_path': fits_r2_path,
        'reduction_version': metadata['reduction_version'],
        'signal_to_noise': metadata['signal_to_noise'],
    }

    # Check if spectrum exists for this object+grating
    existing = supabase.table('spectra').select('id').eq('object_id', object_id).eq('grating', metadata['grating']).execute()

    if existing.data:
        # Update existing
        supabase.table('spectra').update(data).eq('id', existing.data[0]['id']).execute()
    else:
        # Insert new
        supabase.table('spectra').insert(data).execute()


def batch_upsert_objects(
    supabase: 'Client',
    metadata_list: list[dict],
    obs_config: dict,
    force_overwrite: bool,
    batch_size: int = 500
) -> int:
    """
    Upsert objects in batches for improved performance.

    Args:
        supabase: Supabase client
        metadata_list: List of metadata dicts from FITS files
        obs_config: Observation configuration
        force_overwrite: If True, reset inspection data
        batch_size: Records per batch (default: 500)

    Returns:
        Number of objects upserted
    """
    # Deduplicate by object_id (only one record per object)
    seen = set()
    unique_metadata = []
    for m in metadata_list:
        if m['object_id'] not in seen:
            seen.add(m['object_id'])
            unique_metadata.append(m)

    if not unique_metadata:
        return 0

    # Get existing object IDs
    object_ids = [m['object_id'] for m in unique_metadata]
    existing = set(check_existing_objects(supabase, object_ids))

    # Prepare records for insert vs update
    new_records = []
    update_records = []

    for m in unique_metadata:
        object_id = m['object_id']
        is_existing = object_id in existing

        if is_existing and not force_overwrite:
            # UPDATE existing object - only pipeline fields
            data = {
                'object_id': object_id,  # Needed for on_conflict
                'program_id': m['program_id'],
                'field': obs_config.get('field', ''),
                'ra': m['ra'],
                'dec': m['dec'],
                'redshift_auto': m.get('redshift_auto'),
            }
            update_records.append(data)
        elif is_existing and force_overwrite:
            # FORCE UPDATE - reset all fields
            data = {
                'object_id': object_id,
                'program_id': m['program_id'],
                'field': obs_config.get('field', ''),
                'ra': m['ra'],
                'dec': m['dec'],
                'redshift_auto': m.get('redshift_auto'),
                'redshift_inspected': None,
                'redshift_quality': 0,
                'spectral_features': 0,
                'object_flags': 0,
                'dq_flags': 0,
                'last_inspected_at': None,
                'last_inspected_by': None,
            }
            update_records.append(data)
        else:
            # INSERT new object
            data = {
                'object_id': object_id,
                'program_id': m['program_id'],
                'field': obs_config.get('field', ''),
                'ra': m['ra'],
                'dec': m['dec'],
                'redshift_auto': m.get('redshift_auto'),
                'redshift_inspected': None,
                'redshift_quality': 0,
                'spectral_features': 0,
                'object_flags': 0,
                'dq_flags': 0,
                'last_inspected_at': None,
                'last_inspected_by': None,
            }
            new_records.append(data)

    # Batch insert new records
    for i in range(0, len(new_records), batch_size):
        batch = new_records[i:i + batch_size]
        supabase.table('objects').insert(batch).execute()

    # Batch upsert updates (uses on_conflict to update existing)
    for i in range(0, len(update_records), batch_size):
        batch = update_records[i:i + batch_size]
        supabase.table('objects').upsert(batch, on_conflict='object_id').execute()

    return len(unique_metadata)


def batch_upsert_spectra(
    supabase: 'Client',
    metadata_list: list[dict],
    obs_name: str,
    batch_size: int = 500
) -> int:
    """
    Upsert spectra in batches for improved performance.

    Args:
        supabase: Supabase client
        metadata_list: List of metadata dicts from FITS files
        obs_name: Observation name (for building R2 paths)
        batch_size: Records per batch (default: 500)

    Returns:
        Number of spectra upserted
    """
    if not metadata_list:
        return 0

    # Build spectrum records with composite keys for lookup
    spectrum_records = []
    for m in metadata_list:
        fits_r2_key = f"spectra/{obs_name}/{m['fits_filename']}"
        data = {
            'object_id': m['object_id'],
            'grating': m['grating'],
            'fits_path': fits_r2_key,
            'reduction_version': m['reduction_version'],
            'signal_to_noise': m['signal_to_noise'],
        }
        spectrum_records.append(data)

    # Get all existing spectra for these object_ids to determine insert vs update
    object_ids = list(set(r['object_id'] for r in spectrum_records))

    # Fetch existing spectra in batches (to handle large queries)
    existing_map = {}  # (object_id, grating) -> id
    for i in range(0, len(object_ids), batch_size):
        batch_ids = object_ids[i:i + batch_size]
        existing = supabase.table('spectra').select('id,object_id,grating').in_('object_id', batch_ids).execute()
        for row in existing.data:
            key = (row['object_id'], row['grating'])
            existing_map[key] = row['id']

    # Split into new vs existing records
    new_records = []
    update_records = []

    for record in spectrum_records:
        key = (record['object_id'], record['grating'])
        if key in existing_map:
            # Add id for update
            record_with_id = {**record, 'id': existing_map[key]}
            update_records.append(record_with_id)
        else:
            new_records.append(record)

    # Batch insert new records
    for i in range(0, len(new_records), batch_size):
        batch = new_records[i:i + batch_size]
        supabase.table('spectra').insert(batch).execute()

    # Batch update existing records (upsert with id as conflict key)
    for i in range(0, len(update_records), batch_size):
        batch = update_records[i:i + batch_size]
        supabase.table('spectra').upsert(batch, on_conflict='id').execute()

    return len(spectrum_records)


# === R2 Integration ===

def get_r2_client(config: dict):
    """Create boto3 S3 client configured for Cloudflare R2."""
    if not HAS_BOTO3:
        raise RuntimeError("boto3 not installed")

    r2_config = config['r2']

    return boto3.client(
        's3',
        endpoint_url=f"https://{r2_config['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=r2_config['access_key_id'],
        aws_secret_access_key=r2_config['secret_access_key'],
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )


def upload_to_r2(r2_client, bucket: str, local_path: Path, r2_key: str, content_type: str = None) -> None:
    """Upload a file to R2."""
    extra_args = {}
    if content_type:
        extra_args['ContentType'] = content_type

    r2_client.upload_file(
        str(local_path),
        bucket,
        r2_key,
        ExtraArgs=extra_args if extra_args else None
    )


class UploadTask(NamedTuple):
    """Represents a file to be uploaded to R2."""
    local_path: Path
    r2_key: str
    content_type: str


def upload_files_parallel(
    r2_client,
    bucket: str,
    tasks: list[UploadTask],
    max_workers: int = 12,
    desc: str = "Uploading"
) -> tuple[int, int, list[str]]:
    """
    Upload multiple files to R2 in parallel with progress bar.

    Args:
        r2_client: boto3 S3 client configured for R2
        bucket: R2 bucket name
        tasks: List of UploadTask namedtuples
        max_workers: Maximum parallel upload threads (default: 12)
        desc: Description for progress bar

    Returns:
        Tuple of (success_count, failure_count, failed_file_messages)
    """
    if not tasks:
        return 0, 0, []

    success, failed = 0, 0
    failed_files = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {
            executor.submit(
                upload_to_r2, r2_client, bucket,
                task.local_path, task.r2_key, task.content_type
            ): task
            for task in tasks
        }

        with tqdm(total=len(tasks), desc=desc, unit="file") as pbar:
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    future.result()
                    success += 1
                except Exception as e:
                    failed += 1
                    failed_files.append(f"{task.local_path.name}: {e}")
                pbar.update(1)

    return success, failed, failed_files


def deploy_rgb_images(
    obs_name: str,
    dry_run: bool,
    project_root: Path,
    source_ids: list[str] | None = None
) -> None:
    """
    Deploy RGB images for an observation to R2.

    Images are uploaded to: rgb/{obs_name}/{object_id}_rgb.png

    Args:
        source_ids: Optional list of source IDs to filter deployment to specific objects
    """
    scripts_dir = project_root / 'scripts'
    pipeline_dir = project_root / 'pipeline'
    products_dir = pipeline_dir / 'products'

    # Load configuration
    print(f"Loading configuration...")
    config = load_config(scripts_dir)
    observations = load_observations(pipeline_dir)

    # Validate observation exists
    if obs_name not in observations:
        print(f"Error: Observation '{obs_name}' not found in observations.toml")
        print(f"Available observations: {list(observations.keys())}")
        sys.exit(1)

    obs_config = observations[obs_name]
    print(f"Deploying RGB images for observation: {obs_name}")
    print(f"  Field: {obs_config.get('field', 'unknown')}")
    print()

    # Discover RGB images
    rgb_files = discover_rgb_images(products_dir, obs_name)

    # Filter by source IDs if specified
    if source_ids:
        original_count = len(rgb_files)
        rgb_files = filter_files_by_source_ids(rgb_files, source_ids, obs_name)
        print(f"Found {original_count} RGB images, filtered to {len(rgb_files)} matching source IDs: {', '.join(source_ids)}")

        if not rgb_files:
            print(f"Error: No RGB images found matching source IDs: {', '.join(source_ids)}")
            print("Nothing to deploy.")
            return
    else:
        if not rgb_files:
            print(f"No RGB images (*_rgb.png) found in {products_dir / obs_name}")
            print("Nothing to deploy.")
            return
        print(f"Found {len(rgb_files)} RGB images")

    print()

    if dry_run:
        print("=== DRY RUN MODE ===")
        print("Would upload to R2:")
        for rgb_path in rgb_files[:5]:
            # Extract object_id from filename (remove _rgb.png suffix)
            object_id = rgb_path.stem.replace('_rgb', '')
            r2_key = f"rgb/{obs_name}/{rgb_path.name}"
            print(f"  - {rgb_path.name} → {r2_key}")
        if len(rgb_files) > 5:
            print(f"  ... and {len(rgb_files) - 5} more")
        return

    # Check dependencies
    if not HAS_BOTO3:
        print("Error: boto3 required. Install with: pip install boto3")
        sys.exit(1)

    # Initialize R2 client
    print("Connecting to R2...")
    r2_client = get_r2_client(config)
    bucket = config['r2']['bucket_name']

    # Build upload tasks
    upload_tasks = [
        UploadTask(
            local_path=rgb_path,
            r2_key=f"rgb/{obs_name}/{rgb_path.name}",
            content_type='image/png'
        )
        for rgb_path in rgb_files
    ]

    # Upload RGB images in parallel
    print("Uploading RGB images...")
    success, failed, failed_files = upload_files_parallel(
        r2_client, bucket, upload_tasks,
        max_workers=12, desc="RGB images"
    )

    # Report failures
    if failed_files:
        print(f"\n⚠️  {failed} uploads failed:")
        for msg in failed_files[:5]:
            print(f"    - {msg}")
        if len(failed_files) > 5:
            print(f"    ... and {len(failed_files) - 5} more")

    print()
    print(f"✓ Successfully uploaded {success}/{len(rgb_files)} RGB images to rgb/{obs_name}/")


def deploy_sed_plots(
    obs_name: str,
    dry_run: bool,
    project_root: Path,
    source_ids: list[str] | None = None
) -> None:
    """
    Deploy SED plot PDFs for an observation to R2.

    PDFs are uploaded to: sed/{obs_name}/{object_id}_sed.pdf

    Args:
        source_ids: Optional list of source IDs to filter deployment to specific objects
    """
    scripts_dir = project_root / 'scripts'
    pipeline_dir = project_root / 'pipeline'
    products_dir = pipeline_dir / 'products'

    # Load configuration
    print(f"Loading configuration...")
    config = load_config(scripts_dir)
    observations = load_observations(pipeline_dir)

    # Validate observation exists
    if obs_name not in observations:
        print(f"Error: Observation '{obs_name}' not found in observations.toml")
        print(f"Available observations: {list(observations.keys())}")
        sys.exit(1)

    obs_config = observations[obs_name]
    print(f"Deploying SED plots for observation: {obs_name}")
    print(f"  Field: {obs_config.get('field', 'unknown')}")
    print()

    # Discover SED plot PDFs
    sed_files = discover_sed_plots(products_dir, obs_name)

    # Filter by source IDs if specified
    if source_ids:
        original_count = len(sed_files)
        sed_files = filter_files_by_source_ids(sed_files, source_ids, obs_name)
        print(f"Found {original_count} SED plot PDFs, filtered to {len(sed_files)} matching source IDs: {', '.join(source_ids)}")

        if not sed_files:
            print(f"Error: No SED plots found matching source IDs: {', '.join(source_ids)}")
            print("Nothing to deploy.")
            return
    else:
        if not sed_files:
            print(f"No SED plots (*_sed.pdf) found in {products_dir / obs_name}")
            print("Nothing to deploy.")
            return
        print(f"Found {len(sed_files)} SED plot PDFs")

    print()

    if dry_run:
        print("=== DRY RUN MODE ===")
        print("Would upload to R2:")
        for sed_path in sed_files[:5]:
            # Extract object_id from filename (remove _sed.pdf suffix)
            object_id = sed_path.stem.replace('_sed', '')
            r2_key = f"sed/{obs_name}/{sed_path.name}"
            print(f"  - {sed_path.name} → {r2_key}")
        if len(sed_files) > 5:
            print(f"  ... and {len(sed_files) - 5} more")
        return

    # Check dependencies
    if not HAS_BOTO3:
        print("Error: boto3 required. Install with: pip install boto3")
        sys.exit(1)

    # Initialize R2 client
    print("Connecting to R2...")
    r2_client = get_r2_client(config)
    bucket = config['r2']['bucket_name']

    # Build upload tasks
    upload_tasks = [
        UploadTask(
            local_path=sed_path,
            r2_key=f"sed/{obs_name}/{sed_path.name}",
            content_type='application/pdf'
        )
        for sed_path in sed_files
    ]

    # Upload SED plots in parallel
    print("Uploading SED plots...")
    success, failed, failed_files = upload_files_parallel(
        r2_client, bucket, upload_tasks,
        max_workers=12, desc="SED plots"
    )

    # Report failures
    if failed_files:
        print(f"\n⚠️  {failed} uploads failed:")
        for msg in failed_files[:5]:
            print(f"    - {msg}")
        if len(failed_files) > 5:
            print(f"    ... and {len(failed_files) - 5} more")

    print()
    print(f"✓ Successfully uploaded {success}/{len(sed_files)} SED plots to sed/{obs_name}/")


def deploy_spectrum_json(
    obs_name: str,
    dry_run: bool,
    project_root: Path,
    source_ids: list[str] | None = None
) -> None:
    """
    Deploy only spectrum JSON files for an observation to R2.

    This regenerates and uploads JSON files (with 1D spectrum, 2D S/N, and
    cross-dispersion profile data) without re-uploading FITS files or
    updating Supabase.

    JSON files are uploaded to: spectra/{obs_name}/{filename}.json

    Args:
        source_ids: Optional list of source IDs to filter deployment to specific objects
    """
    scripts_dir = project_root / 'scripts'
    pipeline_dir = project_root / 'pipeline'
    products_dir = pipeline_dir / 'products'

    # Load configuration
    print(f"Loading configuration...")
    config = load_config(scripts_dir)
    observations = load_observations(pipeline_dir)

    # Validate observation exists
    if obs_name not in observations:
        print(f"Error: Observation '{obs_name}' not found in observations.toml")
        print(f"Available observations: {list(observations.keys())}")
        sys.exit(1)

    obs_config = observations[obs_name]
    print(f"Deploying spectrum JSON files for observation: {obs_name}")
    print(f"  Field: {obs_config.get('field', 'unknown')}")
    print()

    # Discover FITS files (we need these to generate JSON)
    fits_files = discover_fits_files(products_dir, obs_name)

    # Filter by source IDs if specified
    if source_ids:
        original_count = len(fits_files)
        fits_files = filter_files_by_source_ids(fits_files, source_ids, obs_name)
        print(f"Found {original_count} spectrum files, filtered to {len(fits_files)} matching source IDs: {', '.join(source_ids)}")

        if not fits_files:
            print(f"Error: No spectrum files found matching source IDs: {', '.join(source_ids)}")
            print("Nothing to deploy.")
            return
    else:
        print(f"Found {len(fits_files)} spectrum files")

    print()

    if dry_run:
        print("=== DRY RUN MODE ===")
        print("Would generate and upload JSON files to R2:")
        for fits_path in fits_files[:5]:
            json_name = fits_path.stem + '.json'
            r2_key = f"spectra/{obs_name}/{json_name}"
            print(f"  - {fits_path.name} → {r2_key}")
        if len(fits_files) > 5:
            print(f"  ... and {len(fits_files) - 5} more")
        return

    # Check dependencies
    if not HAS_BOTO3:
        print("Error: boto3 required. Install with: pip install boto3")
        sys.exit(1)

    # Initialize R2 client
    print("Connecting to R2...")
    r2_client = get_r2_client(config)
    bucket = config['r2']['bucket_name']

    # Create temp directory for generated files
    temp_dir = products_dir / obs_name / '.deploy_temp'
    temp_dir.mkdir(exist_ok=True)

    try:
        # Generate JSON files
        print("Generating spectrum JSON files...")
        upload_tasks = []
        r2_prefix = f"spectra/{obs_name}"

        for fits_path in tqdm(fits_files, desc="Generating", unit="file"):
            json_path = generate_spectrum_json(fits_path, temp_dir)
            upload_tasks.append(UploadTask(
                local_path=json_path,
                r2_key=f"{r2_prefix}/{json_path.name}",
                content_type='application/json'
            ))

        print(f"Prepared {len(upload_tasks)} JSON files for upload")
        print()

        # Upload JSON files in parallel
        print("Uploading JSON files...")
        success, failed, failed_files = upload_files_parallel(
            r2_client, bucket, upload_tasks,
            max_workers=12, desc="JSON files"
        )

        # Report failures
        if failed_files:
            print(f"\n⚠️  {failed} uploads failed:")
            for msg in failed_files[:5]:
                print(f"    - {msg}")
            if len(failed_files) > 5:
                print(f"    ... and {len(failed_files) - 5} more")

        print()
        print(f"✓ Successfully uploaded {success}/{len(fits_files)} spectrum JSON files to spectra/{obs_name}/")

    finally:
        # Cleanup temp files
        import shutil
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


# === Main Deployment Logic ===

def deploy_observation(
    obs_name: str,
    dry_run: bool,
    supabase_only: bool,
    force_overwrite: bool,
    include_rgb: bool,
    include_sed: bool,
    zfit_only: bool,
    project_root: Path,
    source_ids: list[str] | None = None,
    auto_approve: bool = False
) -> None:
    """
    Deploy an observation to Supabase and R2.

    Version is read from CMPFRVER header in each FITS file.

    Args:
        include_rgb: If True, include RGB images in deployment
        include_sed: If True, include SED plot PDFs in deployment
        zfit_only: If True, only deploy zfit JSON files and update redshift_auto
                   (skip FITS, spectrum JSON, RGB, and SED uploads)
        source_ids: Optional list of source IDs to filter deployment to specific objects
        auto_approve: If True, skip confirmation prompts (useful for scripting)
    """
    scripts_dir = project_root / 'scripts'
    pipeline_dir = project_root / 'pipeline'
    products_dir = pipeline_dir / 'products'

    # Load configurations
    print(f"Loading configuration...")
    config = load_config(scripts_dir)
    programs_config = load_programs(scripts_dir)
    observations = load_observations(pipeline_dir)

    # Validate observation exists
    if obs_name not in observations:
        print(f"Error: Observation '{obs_name}' not found in observations.toml")
        print(f"Available observations: {list(observations.keys())}")
        sys.exit(1)

    obs_config = observations[obs_name]
    print(f"Deploying observation: {obs_name}")
    print(f"  Field: {obs_config.get('field', 'unknown')}")
    print(f"  Gratings: {obs_config.get('gratings', [])}")
    print()

    # Discover FITS files
    fits_files = discover_fits_files(products_dir, obs_name)

    # Filter by source IDs if specified
    if source_ids:
        original_count = len(fits_files)
        fits_files = filter_files_by_source_ids(fits_files, source_ids, obs_name)
        print(f"Found {original_count} spectrum files, filtered to {len(fits_files)} matching source IDs: {', '.join(source_ids)}")

        if not fits_files:
            print(f"Error: No files found matching source IDs: {', '.join(source_ids)}")
            sys.exit(1)
    else:
        print(f"Found {len(fits_files)} spectrum files")

    # Discover RGB images (if including RGB)
    rgb_files = []
    if include_rgb:
        rgb_files = discover_rgb_images(products_dir, obs_name)

        # Filter by source IDs if specified
        if source_ids and rgb_files:
            original_count = len(rgb_files)
            rgb_files = filter_files_by_source_ids(rgb_files, source_ids, obs_name)
            if rgb_files:
                print(f"Found {original_count} RGB images, filtered to {len(rgb_files)}")
            else:
                print(f"No RGB images found matching specified source IDs")
        elif rgb_files:
            print(f"Found {len(rgb_files)} RGB images")
        else:
            print(f"No RGB images found (skipping)")

    # Discover SED plots (if including SED)
    sed_files = []
    if include_sed:
        sed_files = discover_sed_plots(products_dir, obs_name)

        # Filter by source IDs if specified
        if source_ids and sed_files:
            original_count = len(sed_files)
            sed_files = filter_files_by_source_ids(sed_files, source_ids, obs_name)
            if sed_files:
                print(f"Found {original_count} SED plots, filtered to {len(sed_files)}")
            else:
                print(f"No SED plots found matching specified source IDs")
        elif sed_files:
            print(f"Found {len(sed_files)} SED plots")
        else:
            print(f"No SED plots found (skipping)")

    # Read metadata from all files
    print(f"Reading FITS metadata...")
    all_metadata = []
    for fits_path in fits_files:
        try:
            metadata = read_fits_metadata(fits_path, obs_name)
            metadata['fits_path'] = fits_path
            all_metadata.append(metadata)
        except Exception as e:
            print(f"  Warning: Failed to read {fits_path.name}: {e}")

    print(f"Successfully read {len(all_metadata)} files")

    # Discover and process zfit files for redshift determination
    print(f"Discovering zfit files...")
    zfit_map = discover_zfit_files(products_dir, obs_name)

    # Filter zfit_map by source IDs if specified
    if source_ids and zfit_map:
        original_count = len(zfit_map)
        # Filter zfit files (need to convert dict values to list, filter, then rebuild dict)
        zfit_files = list(zfit_map.values())
        filtered_zfit_files = filter_files_by_source_ids(zfit_files, source_ids, obs_name)
        # Rebuild map with filtered files
        zfit_map = {}
        for zfit_path in filtered_zfit_files:
            base = zfit_path.stem.replace('_zfit', '')
            zfit_map[base] = zfit_path
        print(f"Found {original_count} zfit files, filtered to {len(zfit_map)}")

    zfit_data_map = {}  # Maps spec filename base -> zfit data dict (initialized for later use)

    if not source_ids or zfit_map:
        if not source_ids:
            print(f"Found {len(zfit_map)} zfit files")
        # If source_ids specified, count was already printed above

    # Read zfit data and associate with spectra
    for metadata in all_metadata:
        fits_path = metadata['fits_path']
        spec_base = fits_path.stem.replace('_spec', '')

        if spec_base in zfit_map:
            zfit_data = read_zfit_data(zfit_map[spec_base])
            if zfit_data:
                zfit_data_map[spec_base] = zfit_data

    if zfit_data_map:
        print(f"Successfully read {len(zfit_data_map)} zfit files")

        # Group zfit data by object_id to determine best redshift
        print(f"Determining best redshift for each object...")
        object_zfit_by_grating = {}  # {object_id: {grating: zfit_data}}

        for metadata in all_metadata:
            object_id = metadata['object_id']
            grating = metadata['grating']
            spec_base = metadata['fits_path'].stem.replace('_spec', '')

            if spec_base in zfit_data_map:
                if object_id not in object_zfit_by_grating:
                    object_zfit_by_grating[object_id] = {}
                object_zfit_by_grating[object_id][grating] = zfit_data_map[spec_base]

        # Apply decision tree to determine best redshift for each object
        object_redshifts = {}  # {object_id: best_redshift}
        for object_id, zfit_by_grating in object_zfit_by_grating.items():
            best_z = determine_best_redshift(zfit_by_grating)
            if best_z is not None:
                object_redshifts[object_id] = best_z

        # Update metadata with best redshift
        for metadata in all_metadata:
            object_id = metadata['object_id']
            if object_id in object_redshifts:
                metadata['redshift_auto'] = object_redshifts[object_id]

        print(f"Assigned redshift_auto for {len(object_redshifts)} objects")
    else:
        print("No zfit files found - redshift_auto will not be populated")
    print()

    # Get unique objects and program
    object_ids = list(set(m['object_id'] for m in all_metadata))
    program_ids = list(set(m['program_id'] for m in all_metadata))

    print(f"  Unique objects: {len(object_ids)}")
    print(f"  Programs: {program_ids}")
    print()

    if dry_run:
        print("=== DRY RUN MODE ===")
        if force_overwrite:
            print("⚠️  FORCE OVERWRITE enabled - will reset all inspection data!")
            print()
        if not supabase_only:
            print("Would upload to R2:")
            if zfit_only:
                if zfit_data_map:
                    print(f"  - {len(zfit_data_map)} zfit JSON files (redshift fits + chi² curves)")
                else:
                    print("  - No zfit files found")
            else:
                print(f"  - {len(all_metadata)} FITS files")
                print(f"  - {len(all_metadata)} spectrum JSON files (1D spectra + 2D S/N)")
                if zfit_data_map:
                    print(f"  - {len(zfit_data_map)} zfit JSON files (redshift fits + chi² curves)")
                if rgb_files:
                    print(f"  - {len(rgb_files)} RGB images")
                if sed_files:
                    print(f"  - {len(sed_files)} SED plots")
            print()
        print("Would upsert to Supabase:")
        if not zfit_only:
            print(f"  - {len(program_ids)} program(s)")
            print(f"  - {len(all_metadata)} spectrum records")
        print(f"  - {len(object_ids)} object(s) (update redshift_auto)")
        if not force_overwrite:
            print("  (existing objects: only pipeline fields updated, inspection data preserved)")
        print()
        print("Sample object_ids:")
        for oid in object_ids[:5]:
            print(f"  - {oid}")
        if len(object_ids) > 5:
            print(f"  ... and {len(object_ids) - 5} more")
        return

    # Check dependencies
    if not HAS_SUPABASE:
        print("Error: supabase-py required. Install with: pip install supabase")
        sys.exit(1)
    if not supabase_only and not HAS_BOTO3:
        print("Error: boto3 required. Install with: pip install boto3")
        sys.exit(1)

    # Initialize clients
    print("Connecting to Supabase...")
    supabase = get_supabase_client(config)

    r2_client = None
    bucket = None
    if not supabase_only:
        print("Connecting to R2...")
        r2_client = get_r2_client(config)
        bucket = config['r2']['bucket_name']

    # Check for existing objects
    print("Checking for existing data...")
    existing = check_existing_objects(supabase, object_ids)
    if existing:
        print(f"  Found {len(existing)} existing objects")
        if force_overwrite:
            print("  ⚠️  FORCE OVERWRITE mode: inspection data will be RESET!")
            if not auto_approve:
                response = input(f"  Are you sure you want to reset all inspection data? [y/N]: ")
                if response.lower() != 'y':
                    print("Aborted.")
                    sys.exit(0)
            else:
                print("  (auto-approved)")
        else:
            print("  (inspection data will be preserved)")
            if not auto_approve:
                response = input(f"  Update pipeline data for existing objects? [y/N]: ")
                if response.lower() != 'y':
                    print("Aborted.")
                    sys.exit(0)
            else:
                print("  (auto-approved)")
    else:
        print("  No existing objects found")
    print()

    # Create temp directory for generated files
    temp_dir = products_dir / obs_name / '.deploy_temp'
    temp_dir.mkdir(exist_ok=True)

    try:
        # Upsert programs
        print("Upserting programs...")
        for pid in program_ids:
            upsert_program(supabase, pid, programs_config)
            program_name = programs_config.get(pid, {}).get('program_name', f'Program {pid}')
            print(f"  ✓ {pid} ({program_name})")
        print()

        # === PHASE 1: Generate all JSON files ===
        upload_tasks = []  # Collect all upload tasks
        r2_prefix = f"spectra/{obs_name}"

        if not supabase_only:
            print("Generating JSON files...")
            for metadata in tqdm(all_metadata, desc="Generating", unit="file"):
                fits_path = metadata['fits_path']
                spec_base = fits_path.stem.replace('_spec', '')
                zfit_path = zfit_map.get(spec_base)

                if zfit_only:
                    # Zfit-only mode: only generate zfit JSON
                    if zfit_path:
                        zfit_json_path = generate_zfit_json(zfit_path, temp_dir)
                        upload_tasks.append(UploadTask(
                            local_path=zfit_json_path,
                            r2_key=f"{r2_prefix}/{zfit_json_path.name}",
                            content_type='application/json'
                        ))
                else:
                    # Normal mode: FITS, spectrum JSON, and zfit JSON
                    # Add FITS file
                    upload_tasks.append(UploadTask(
                        local_path=fits_path,
                        r2_key=f"{r2_prefix}/{fits_path.name}",
                        content_type='application/fits'
                    ))

                    # Generate and add spectrum JSON
                    json_path = generate_spectrum_json(fits_path, temp_dir)
                    upload_tasks.append(UploadTask(
                        local_path=json_path,
                        r2_key=f"{r2_prefix}/{json_path.name}",
                        content_type='application/json'
                    ))

                    # Generate and add zfit JSON if available
                    if zfit_path:
                        zfit_json_path = generate_zfit_json(zfit_path, temp_dir)
                        upload_tasks.append(UploadTask(
                            local_path=zfit_json_path,
                            r2_key=f"{r2_prefix}/{zfit_json_path.name}",
                            content_type='application/json'
                        ))

            # Add RGB images to upload tasks
            if rgb_files and not zfit_only:
                for rgb_path in rgb_files:
                    upload_tasks.append(UploadTask(
                        local_path=rgb_path,
                        r2_key=f"rgb/{obs_name}/{rgb_path.name}",
                        content_type='image/png'
                    ))

            # Add SED plots to upload tasks
            if sed_files and not zfit_only:
                for sed_path in sed_files:
                    upload_tasks.append(UploadTask(
                        local_path=sed_path,
                        r2_key=f"sed/{obs_name}/{sed_path.name}",
                        content_type='application/pdf'
                    ))

            print(f"Prepared {len(upload_tasks)} files for upload")
            print()

        # === PHASE 2: Parallel R2 uploads ===
        if not supabase_only and upload_tasks:
            print("Uploading files to R2...")
            success, failed, failed_files = upload_files_parallel(
                r2_client, bucket, upload_tasks,
                max_workers=12, desc="R2 uploads"
            )

            # Report failures
            if failed_files:
                print(f"\n⚠️  {failed} uploads failed:")
                for msg in failed_files[:10]:
                    print(f"    - {msg}")
                if len(failed_files) > 10:
                    print(f"    ... and {len(failed_files) - 10} more")

            print(f"\n✓ Uploaded {success}/{len(upload_tasks)} files to R2")
            print()

        # === PHASE 3: Batch Supabase upserts ===
        print("Upserting objects to Supabase...")
        num_objects = batch_upsert_objects(supabase, all_metadata, obs_config, force_overwrite)
        print(f"  ✓ Upserted {num_objects} objects")

        print("Upserting spectra to Supabase...")
        num_spectra = batch_upsert_spectra(supabase, all_metadata, obs_name)
        print(f"  ✓ Upserted {num_spectra} spectra")

        # Refresh filter options cache (updates field/observation dropdowns in web UI)
        print()
        refresh_filter_options(supabase)

        print()
        if zfit_only:
            print(f"✓ Successfully deployed {len(zfit_data_map)} zfit JSONs and updated redshift_auto for {len(object_ids)} objects")
        elif supabase_only:
            print(f"✓ Successfully updated {len(all_metadata)} spectra from {len(object_ids)} objects (Supabase only)")
        else:
            msg = f"✓ Successfully deployed {len(all_metadata)} spectra from {len(object_ids)} objects"
            if zfit_data_map:
                msg += f" + {len(zfit_data_map)} zfit JSONs"
            if rgb_files:
                msg += f" + {len(rgb_files)} RGB images"
            print(msg)

    finally:
        # Cleanup temp files
        import shutil
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


def main():
    parser = argparse.ArgumentParser(
        description='Deploy CAMPFIRE spectra, RGB images, and SED plots to Supabase and R2',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Deploy spectra and RGB images
    python scripts/deploy.py --obs cosmos_ddt

    # Deploy specific source IDs only
    python scripts/deploy.py --obs cosmos_ddt --source-ids 12345 67890

    # Deploy with dry-run
    python scripts/deploy.py --obs cosmos_ddt --dry-run

    # Deploy only zfit JSONs and update redshift_auto (after re-running fitting)
    python scripts/deploy.py --obs cosmos_ddt --zfit-only

    # Deploy only spectrum JSON files (regenerate with updated profile data)
    python scripts/deploy.py --obs cosmos_ddt --json-only

    # Deploy only RGB images (skip FITS/JSON and Supabase)
    python scripts/deploy.py --obs ember_uds_p4 --rgb-only

    # Deploy RGB images for specific source IDs
    python scripts/deploy.py --obs ember_uds_p4 --rgb-only --source-ids 12345

    # Deploy only SED plots (skip FITS/JSON and Supabase)
    python scripts/deploy.py --obs ember_uds_p4 --sed-only

    # Deploy spectra only (no RGB images)
    python scripts/deploy.py --obs cosmos_ddt --no-rgb

    # Deploy spectra only (no SED plots)
    python scripts/deploy.py --obs cosmos_ddt --no-sed

    # Deploy spectra only (no RGB or SED)
    python scripts/deploy.py --obs cosmos_ddt --no-rgb --no-sed

    # Other options
    python scripts/deploy.py --obs cosmos_ddt --supabase-only
    python scripts/deploy.py --obs ember_uds_p4 --version v0.2
    python scripts/deploy.py --obs cosmos_ddt --force-overwrite  # Reset all inspection data
        """
    )

    parser.add_argument(
        '--obs',
        required=True,
        help='Observation name (must match key in observations.toml)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be deployed without making changes'
    )
    parser.add_argument(
        '--supabase-only',
        action='store_true',
        help='Only update Supabase metadata, skip R2 file uploads'
    )
    parser.add_argument(
        '--force-overwrite',
        action='store_true',
        help='Force overwrite ALL data including user inspection data (use with caution!)'
    )
    parser.add_argument(
        '--rgb-only',
        action='store_true',
        help='Only deploy RGB images (skip FITS files and Supabase updates)'
    )
    parser.add_argument(
        '--no-rgb',
        action='store_true',
        help='Skip RGB image deployment (only deploy spectra)'
    )
    parser.add_argument(
        '--zfit-only',
        action='store_true',
        help='Only deploy zfit JSON files and update redshift_auto (skip FITS/spectrum JSON/RGB)'
    )
    parser.add_argument(
        '--json-only',
        action='store_true',
        help='Only deploy spectrum JSON files (skip FITS uploads and Supabase updates)'
    )
    parser.add_argument(
        '--sed-only',
        action='store_true',
        help='Only deploy SED plot PDFs (skip FITS files and Supabase updates)'
    )
    parser.add_argument(
        '--no-sed',
        action='store_true',
        help='Skip SED plot deployment (only deploy spectra and optionally RGB)'
    )
    parser.add_argument(
        '--auto-approve',
        action='store_true',
        help='Skip confirmation prompts (useful for scripting deployments)'
    )
    parser.add_argument(
        '--source-ids',
        nargs='+',
        metavar='ID',
        help='Deploy only specific source IDs (e.g., --source-ids 12345 67890)'
    )

    args = parser.parse_args()

    # Determine project root
    project_root = Path(__file__).parent.parent

    # Validate flag combinations
    if args.rgb_only:
        if args.supabase_only or args.force_overwrite or args.no_rgb or args.zfit_only or args.json_only or args.sed_only or args.no_sed:
            print("Error: --rgb-only cannot be combined with --supabase-only, --force-overwrite, --no-rgb, --zfit-only, --json-only, --sed-only, or --no-sed")
            sys.exit(1)

        deploy_rgb_images(
            obs_name=args.obs,
            dry_run=args.dry_run,
            project_root=project_root,
            source_ids=args.source_ids
        )
    elif args.sed_only:
        if args.supabase_only or args.force_overwrite or args.no_rgb or args.zfit_only or args.json_only or args.rgb_only or args.no_sed:
            print("Error: --sed-only cannot be combined with --supabase-only, --force-overwrite, --no-rgb, --zfit-only, --json-only, --rgb-only, or --no-sed")
            sys.exit(1)

        deploy_sed_plots(
            obs_name=args.obs,
            dry_run=args.dry_run,
            project_root=project_root,
            source_ids=args.source_ids
        )
    elif args.zfit_only:
        if args.supabase_only or args.no_rgb or args.rgb_only or args.json_only or args.sed_only or args.no_sed:
            print("Error: --zfit-only cannot be combined with --supabase-only, --no-rgb, --rgb-only, --json-only, --sed-only, or --no-sed")
            sys.exit(1)

        # Zfit-only deployment
        deploy_observation(
            obs_name=args.obs,
            dry_run=args.dry_run,
            supabase_only=False,  # Need R2 for zfit JSON uploads
            force_overwrite=args.force_overwrite,
            include_rgb=False,  # Skip RGB in zfit-only mode
            include_sed=False,  # Skip SED in zfit-only mode
            zfit_only=True,
            project_root=project_root,
            source_ids=args.source_ids,
            auto_approve=args.auto_approve
        )
    elif args.json_only:
        if args.supabase_only or args.force_overwrite or args.no_rgb or args.rgb_only or args.zfit_only or args.sed_only or args.no_sed:
            print("Error: --json-only cannot be combined with --supabase-only, --force-overwrite, --no-rgb, --rgb-only, --zfit-only, --sed-only, or --no-sed")
            sys.exit(1)

        deploy_spectrum_json(
            obs_name=args.obs,
            dry_run=args.dry_run,
            project_root=project_root,
            source_ids=args.source_ids
        )
    else:
        # Normal deployment (with or without RGB/SED)
        include_rgb = not args.no_rgb
        include_sed = not args.no_sed

        deploy_observation(
            obs_name=args.obs,
            dry_run=args.dry_run,
            supabase_only=args.supabase_only,
            force_overwrite=args.force_overwrite,
            include_rgb=include_rgb,
            include_sed=include_sed,
            zfit_only=False,
            project_root=project_root,
            source_ids=args.source_ids,
            auto_approve=args.auto_approve
        )


if __name__ == '__main__':
    main()
