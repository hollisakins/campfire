"""
Observation summary generation.

Produces an ECSV file per observation that serves as the contract between
the pipeline and the deploy script.  Deploy reads the ECSV instead of
re-scanning individual FITS files.
"""

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from astropy.table import Table

from campfire_pipeline.common.io import log
from campfire_pipeline.metadata.reader import (
    discover_fits_files,
    discover_zfit_files,
    parse_fits_filename,
    read_fits_metadata,
    read_zfit_data,
)


# Grating wavelength priority for tiebreaking: lower value = higher priority
GRATING_PRIORITY = {
    'G395M': 0, 'G395H': 1,
    'G235M': 2, 'G235H': 3,
    'G140M': 4, 'G140H': 5,
}


def _grating_sort_key(name: str, data: dict) -> tuple:
    """Sort key for ranking gratings: highest SNR > longest exposure > wavelength priority."""
    snr = -(data.get('signal_to_noise') or 0)
    exposure = -(data.get('exposure_time', 0))
    wavelength = GRATING_PRIORITY.get(name, 99)
    return (snr, exposure, wavelength)


def determine_best_redshift(zfit_data_by_grating: dict[str, dict]) -> float | None:
    """
    Apply decision tree to choose the best redshift for an object from multiple spectra.

    Decision logic:
    1. If PRISM available and no gratings: use PRISM
    2. If gratings available and no PRISM: use best grating
    3. If both PRISM and gratings available:
       - Check if they agree (|z_prism - z_grating| < 0.1)
       - If agree: use grating (more precise)
       - If disagree: use PRISM (more robust)

    Best grating ranking: highest max SNR > longest exposure > wavelength
    priority (G395 > G235 > G140).

    Args:
        zfit_data_by_grating: Dict mapping grating names to zfit data dicts.
            Each dict should contain 'redshift', and optionally
            'exposure_time' and 'signal_to_noise' for ranking.

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
        best = min(grating_data, key=lambda g: _grating_sort_key(g, grating_data[g]))
        return grating_data[best]['redshift']

    # Case 3: Both PRISM and gratings
    if prism_data and grating_data:
        z_prism = prism_data['redshift']

        best = min(grating_data, key=lambda g: _grating_sort_key(g, grating_data[g]))
        z_grating = grating_data[best]['redshift']

        # Check agreement
        if abs(z_prism - z_grating) < 0.1:
            return z_grating  # Agree: use grating (more precise)
        else:
            return z_prism    # Disagree: use PRISM (more robust)

    return None


def generate_observation_summary(obs_name: str, obs_dir: Path,
                                  reduction_version: str = 'unknown') -> Table:
    """
    Discover all spec/zfit files for an observation, read their metadata,
    apply the redshift decision tree, and return an astropy Table.

    Parameters
    ----------
    obs_name : str
        Observation name (e.g. 'ember_uds_p4')
    obs_dir : Path
        Path to the observation products directory
    reduction_version : str
        Pipeline version string to embed

    Returns
    -------
    summary : astropy.table.Table
        One row per unique (source_id, grating) combination.
    """
    obs_dir = Path(obs_dir)
    spec_files = discover_fits_files(obs_dir)
    if not spec_files:
        log(f"No spec files found in {obs_dir}")
        return Table()

    zfit_map = discover_zfit_files(obs_dir)

    # ------------------------------------------------------------------
    # Phase 1: read per-grating metadata + zfit data
    # ------------------------------------------------------------------
    rows = []
    # Also collect zfit data grouped by source_id for the decision tree
    zfit_by_source: dict[str, dict[str, dict]] = defaultdict(dict)

    for spec_path in spec_files:
        try:
            meta = read_fits_metadata(spec_path, obs_name)
        except Exception as e:
            log(f"Warning: skipping {spec_path.name}: {e}")
            continue

        # Look up corresponding zfit
        base = spec_path.stem.replace('_spec', '')
        zfit_path = zfit_map.get(base)
        zfit_data = read_zfit_data(zfit_path) if zfit_path else None

        if zfit_data:
            meta['redshift_auto'] = zfit_data['redshift']
            meta['redshift_confidence'] = zfit_data['confidence']
            meta['chi2_min'] = zfit_data['chi2_min']

            # Feed into the per-source decision tree input
            zfit_by_source[meta['source_id']][meta['grating']] = {
                'redshift': zfit_data['redshift'],
                'exposure_time': meta.get('exposure_time', 0),
                'signal_to_noise': meta.get('signal_to_noise', 0),
            }
        else:
            meta['redshift_auto'] = None
            meta['redshift_confidence'] = None
            meta['chi2_min'] = None

        # Relative path within products dir (for portability)
        meta['spec_file'] = spec_path.name
        meta['zfit_file'] = zfit_path.name if zfit_path else ''

        rows.append(meta)

    if not rows:
        return Table()

    # ------------------------------------------------------------------
    # Phase 2: apply redshift decision tree per source
    # ------------------------------------------------------------------
    best_z_by_source = {}
    for source_id, grating_data in zfit_by_source.items():
        best_z_by_source[source_id] = determine_best_redshift(grating_data)

    # Inject best redshift into each row
    for row in rows:
        row['redshift_best'] = best_z_by_source.get(row['source_id'])

    # ------------------------------------------------------------------
    # Phase 3: build astropy Table
    # ------------------------------------------------------------------
    columns = [
        'object_id', 'source_id', 'grating', 'filter',
        'ra', 'dec',
        'redshift_auto', 'redshift_confidence', 'chi2_min',
        'redshift_best',
        'exposure_time', 'signal_to_noise',
        'program_id', 'pi_name', 'date_obs',
        'spec_file', 'zfit_file',
        'reduction_version', 'cal_ver',
        'fits_filename', 'file_size', 'file_hash',
    ]

    table_data = {col: [] for col in columns}
    for row in rows:
        for col in columns:
            table_data[col].append(row.get(col))

    summary = Table(table_data)

    # Add metadata
    summary.meta['obs_name'] = obs_name
    summary.meta['reduction_version'] = reduction_version
    summary.meta['generated_at'] = datetime.utcnow().isoformat()
    summary.meta['n_sources'] = len(set(summary['source_id']))
    summary.meta['n_spectra'] = len(summary)

    return summary


def write_summary_ecsv(summary: Table, obs_dir: Path, obs_name: str) -> Path:
    """
    Write the summary Table to an ECSV file.

    Parameters
    ----------
    summary : Table
        The observation summary table
    obs_dir : Path
        Observation products directory
    obs_name : str
        Observation name (used in filename)

    Returns
    -------
    output_path : Path
        Path to the written ECSV file
    """
    output_path = Path(obs_dir) / f"{obs_name}_summary.ecsv"
    summary.write(output_path, format='ascii.ecsv', overwrite=True)
    log(f"Wrote summary: {output_path} ({len(summary)} spectra)")
    return output_path
