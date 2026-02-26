"""
FITS metadata extraction and file discovery.

Functions extracted from deploy.py for use by both the pipeline (summary
generation) and deploy (cloud upload).  SVG thumbnail generation is NOT
included here — that stays in deploy.py as a web-specific concern.
"""

import hashlib
from pathlib import Path

import numpy as np
from astropy.io import fits

from campfire_pipeline.common.io import log


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

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

    # Known gratings and filters
    gratings = {'prism', 'g140m', 'g235m', 'g395m', 'g140h', 'g235h', 'g395h'}

    # Find grating position by scanning from left
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


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_fits_files(obs_dir: Path) -> list[Path]:
    """Find all *_spec.fits files in an observation directory."""
    if not obs_dir.exists():
        log(f"Observation directory not found: {obs_dir}")
        return []

    return sorted(obs_dir.glob('*_spec.fits'))


def discover_zfit_files(obs_dir: Path) -> dict[str, Path]:
    """
    Find all *_zfit.fits files in an observation directory.

    Returns a dict mapping from spec filename base to zfit Path.
    Example: 'ember_uds_p1_prism_clear_18509' -> Path to zfit file
    """
    if not obs_dir.exists():
        return {}

    zfit_files = sorted(obs_dir.glob('*_zfit.fits'))

    zfit_map = {}
    for zfit_path in zfit_files:
        base = zfit_path.stem.replace('_zfit', '')
        zfit_map[base] = zfit_path

    return zfit_map


# ---------------------------------------------------------------------------
# FITS reading
# ---------------------------------------------------------------------------

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

            chi2_data = hdul['CHI2'].data
            chi2_values = chi2_data['chi2']
            z_grid = chi2_data['z']

            min_idx = np.argmin(chi2_values)
            z_best = float(z_grid[min_idx])
            chi2_min = float(chi2_values[min_idx])

            confidence = float(primary.get('ZCONF', 0.0))

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
        log(f"Warning: Failed to read zfit file {zfit_path.name}: {e}")
        return None


def _compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file, returned as 'sha256:<hex>'."""
    hasher = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def read_fits_metadata(fits_path: Path, obs_name: str) -> dict:
    """
    Read metadata from a *_spec.fits file.

    Returns a dict with metadata suitable for summary ECSV or database
    insertion.  Does NOT include SVG thumbnails (deploy handles those).
    """
    with fits.open(fits_path) as hdul:
        primary = hdul['PRIMARY'].header
        sci = hdul['SCI'].header
        spec1d = hdul['SPEC1D'].data

        parsed = parse_fits_filename(fits_path.name)

        # Calculate max S/N
        fnu = spec1d['fnu']
        fnu_err = spec1d['fnu_err']
        valid = ~np.isnan(fnu) & ~np.isnan(fnu_err) & (fnu_err > 0)
        if valid.sum() > 0:
            sn = fnu[valid] / fnu_err[valid]
            max_sn = float(np.nanmax(sn))
        else:
            max_sn = None

        object_id = f"{obs_name}_{parsed['source_id']}"

        # Try to get redshift from header
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

        reduction_version = primary.get('CMPFRVER', 'v0.1')

        return {
            'object_id': object_id,
            'source_id': parsed['source_id'],
            'grating': parsed['grating'],
            'filter': parsed['filter'],

            'program_id': int(primary.get('PROGRAM', 0)),
            'pi_name': primary.get('PI_NAME', ''),
            'date_obs': primary.get('DATE-OBS', ''),
            'exposure_time': float(primary.get('EFFEXPTM', 0)),
            'cal_ver': primary.get('CAL_VER', ''),
            'reduction_version': reduction_version,

            'ra': float(sci.get('SRCRA', 0)),
            'dec': float(sci.get('SRCDEC', 0)),

            'redshift_auto': redshift_auto,

            'signal_to_noise': max_sn,

            'fits_filename': fits_path.name,
            'file_size': fits_path.stat().st_size,
            'file_hash': _compute_file_hash(fits_path),
        }
