"""
Observation summary generation.

Produces an ECSV file per observation that serves as the contract between
the pipeline and the deploy script.  Deploy reads the ECSV instead of
re-scanning individual FITS files.
"""

import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from astropy.table import Table

from campfire_pipeline.common.io import log
from campfire_pipeline.metadata.reader import (
    discover_fits_files,
    discover_zfit_files,
    parse_fits_filename,
    read_fits_metadata,
    read_zfit_chi2,
    read_zfit_data,
)

# Speed of light in km/s
_C_KMS = 299792.458

# Grating tiebreak priority: lower value = higher priority
# PRISM ranks first (most reliable, least likely to lack detections)
GRATING_PRIORITY = {
    'PRISM': 0,
    'G395H': 1, 'G395M': 2,
    'G235H': 3, 'G235M': 4,
    'G140H': 5, 'G140M': 6,
}


def _grating_sort_key(name: str, data: dict) -> tuple:
    """Sort key for ranking gratings: highest SNR > longest exposure > wavelength priority."""
    snr = -(data.get('signal_to_noise') or 0)
    exposure = -(data.get('exposure_time', 0))
    wavelength = GRATING_PRIORITY.get(name, 99)
    return (snr, exposure, wavelength)


# ---------------------------------------------------------------------------
# Consensus helpers
# ---------------------------------------------------------------------------

def _find_candidate_peaks(z, chi2, threshold):
    """
    Find candidate redshift peaks as local minima in chi2(z).

    A point is a local minimum if chi2[i] < chi2[i-1] and chi2[i] < chi2[i+1].
    Only peaks within `threshold` of the global minimum are returned.
    The global minimum is always included.

    Returns list of (z_cand, chi2_val) tuples, sorted by chi2.
    """
    chi2_min = np.min(chi2)
    min_idx = np.argmin(chi2)

    candidates = [(float(z[min_idx]), float(chi2[min_idx]))]

    # Find local minima (skip endpoints)
    for i in range(1, len(chi2) - 1):
        if i == min_idx:
            continue
        if chi2[i] < chi2[i - 1] and chi2[i] < chi2[i + 1]:
            if chi2[i] < chi2_min + threshold:
                candidates.append((float(z[i]), float(chi2[i])))

    candidates.sort(key=lambda x: x[1])
    return candidates


def _lookup_chi2_at_z(z_grid, chi2_grid, z_cand, dv_tolerance):
    """
    Look up chi2 value at the nearest grid point to z_cand.

    Uses np.searchsorted to find the nearest point. Returns the chi2 value
    if the nearest point is within dv_tolerance (km/s), else None.
    """
    idx = np.searchsorted(z_grid, z_cand)

    # Check the two bracketing points and pick the closer one
    best_idx = None
    best_dv = np.inf
    for candidate_idx in [idx - 1, idx]:
        if candidate_idx < 0 or candidate_idx >= len(z_grid):
            continue
        z_grid_pt = z_grid[candidate_idx]
        # Velocity offset: dv = c * |z1 - z2| / (1 + z_mid)
        z_mid = 0.5 * (z_cand + z_grid_pt)
        if z_mid <= -1:
            continue
        dv = _C_KMS * abs(z_cand - z_grid_pt) / (1 + z_mid)
        if dv < best_dv:
            best_dv = dv
            best_idx = candidate_idx

    if best_idx is not None and best_dv <= dv_tolerance:
        return float(chi2_grid[best_idx])

    return None


def _filter_informative(chi2_by_grating, min_chi2_range_per_pix):
    """
    Exclude chi2 curves that are too flat to be informative.

    A curve is informative if (chi2_max - chi2_min) / n_pix >= threshold.
    Flat chi2 surfaces have no constraining power and generate spurious
    candidate peaks that pollute the consensus.
    """
    result = {}
    for grating, data in chi2_by_grating.items():
        n_pix = data.get('n_pix', 0)
        if n_pix <= 0:
            continue
        chi2_range = float(np.max(data['chi2']) - np.min(data['chi2']))
        if chi2_range / n_pix >= min_chi2_range_per_pix:
            result[grating] = data
    return result


def _scalar_fallback(scalar_by_grating):
    """Pick best-ranked grating's redshift from scalar metadata."""
    if not scalar_by_grating:
        return None
    best = min(scalar_by_grating,
               key=lambda g: _grating_sort_key(g, scalar_by_grating[g]))
    return scalar_by_grating[best].get('redshift')


def determine_best_redshift(
    chi2_by_grating: dict[str, dict] | None = None,
    scalar_by_grating: dict[str, dict] | None = None,
    delta_chi2_peak: float = 30.0,
    dv_tolerance: float = 1000.0,
    dz_prism_confirm: float = 0.1,
    min_chi2_range_per_pix: float = 0.05,
) -> float | None:
    """
    Choose the best redshift using a PRISM-priority hierarchy.

    PRISM covers 3x the wavelength range of any single grating and is the
    most reliable for redshift identification. Gratings provide precision
    refinement when they agree with PRISM.

    Hierarchy:
    1. PRISM only → PRISM zbest
    2. PRISM + 1 grating → grating zbest if within dz_prism_confirm, else PRISM
    3. PRISM + N gratings → grating consensus if within dz_prism_confirm, else PRISM
    4. No PRISM, 2+ gratings → grating consensus (with flat-curve gate)
    5. Single grating → that grating's zbest
    6. No chi2 data → scalar fallback

    Parameters
    ----------
    chi2_by_grating : dict, optional
        Mapping grating name -> dict with 'z', 'chi2' (ndarrays),
        'chi2_min', 'zbest', 'n_pix' keys.
    scalar_by_grating : dict, optional
        Mapping grating name -> dict with 'redshift', 'exposure_time',
        'signal_to_noise' keys (legacy scalar data for fallback).
    delta_chi2_peak : float
        Maximum delta-chi2 above global minimum for candidate peaks.
    dv_tolerance : float
        Maximum velocity offset (km/s) for nearest grid-point lookup.
    dz_prism_confirm : float
        Maximum |dz| for a grating result to confirm PRISM.
    min_chi2_range_per_pix : float
        Minimum (chi2_max - chi2_min) / n_pix for a curve to participate
        in grating-only consensus. Filters flat/uninformative curves.

    Returns
    -------
    float or None
        Best consensus redshift, or None if no valid data.
    """
    if not chi2_by_grating:
        return _scalar_fallback(scalar_by_grating)

    # Partition into PRISM vs gratings
    prism_data = chi2_by_grating.get('PRISM')
    grating_chi2 = {k: v for k, v in chi2_by_grating.items() if k != 'PRISM'}
    has_prism = prism_data is not None
    n_gratings = len(grating_chi2)

    # Case 1: PRISM only
    if has_prism and n_gratings == 0:
        return prism_data['zbest']

    # Case 5: single grating, no PRISM
    if not has_prism and n_gratings == 1:
        return next(iter(grating_chi2.values()))['zbest']

    # Case 2: PRISM + 1 grating
    if has_prism and n_gratings == 1:
        grating_zbest = next(iter(grating_chi2.values()))['zbest']
        if abs(grating_zbest - prism_data['zbest']) <= dz_prism_confirm:
            return grating_zbest
        return prism_data['zbest']

    # Case 3: PRISM + N gratings
    if has_prism and n_gratings >= 2:
        consensus_z = _consensus_redshift(grating_chi2, delta_chi2_peak, dv_tolerance)
        if consensus_z is not None and abs(consensus_z - prism_data['zbest']) <= dz_prism_confirm:
            return consensus_z
        return prism_data['zbest']

    # Case 4: no PRISM, multiple gratings
    if n_gratings >= 2:
        informative = _filter_informative(grating_chi2, min_chi2_range_per_pix)
        if len(informative) >= 2:
            result = _consensus_redshift(informative, delta_chi2_peak, dv_tolerance)
            if result is not None:
                return result
        # Fall back to best single grating
        pool = informative if informative else grating_chi2
        best_g = min(pool, key=lambda g: GRATING_PRIORITY.get(g, 99))
        return pool[best_g]['zbest']

    # Case 6: scalar fallback
    return _scalar_fallback(scalar_by_grating)


def _consensus_redshift(chi2_by_grating, delta_chi2_peak, dv_tolerance):
    """
    Core consensus algorithm: score candidate peaks across gratings.

    For each grating, find candidate peaks. For each candidate, look up
    the chi2 value at the nearest grid point in every *other* grating.
    Score = sum of delta-chi2 values across all constraining gratings
    (lower is better). Tiebreak by number of constraining gratings
    (more is better), then by grating precision priority.
    """
    # Collect all candidates from all gratings
    all_candidates = []  # (z_cand, source_grating, chi2_at_peak)

    for grating, data in chi2_by_grating.items():
        z = data['z']
        chi2 = data['chi2']
        peaks = _find_candidate_peaks(z, chi2, delta_chi2_peak)
        for z_cand, chi2_val in peaks:
            all_candidates.append((z_cand, grating, chi2_val))

    if not all_candidates:
        return None

    # Score each candidate across all gratings.
    # Normalize delta-chi2 by n_pix so that gratings contribute per-pixel
    # constraining power rather than raw pixel count.
    scored = []  # (total_delta_chi2_per_pix, -n_constraining, grating_priority, z_cand)

    for z_cand, source_grating, _ in all_candidates:
        total_delta_chi2 = 0.0
        n_constraining = 0

        for grating, data in chi2_by_grating.items():
            chi2_val = _lookup_chi2_at_z(data['z'], data['chi2'],
                                         z_cand, dv_tolerance)
            if chi2_val is not None:
                delta = chi2_val - data['chi2_min']
                n_pix = data.get('n_pix', 0)
                if n_pix > 0:
                    delta /= n_pix
                total_delta_chi2 += delta
                n_constraining += 1

        grating_prio = GRATING_PRIORITY.get(source_grating, 99)
        scored.append((total_delta_chi2, -n_constraining, grating_prio, z_cand))

    # Sort: lowest total delta-chi2, most constraining gratings, best precision
    scored.sort()
    return scored[0][3]


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------

def generate_observation_summary(obs_name: str, obs_dir: Path,
                                  reduction_version: str = 'unknown',
                                  field: str = '',
                                  program_slug: str = '',
                                  consensus_config: dict | None = None) -> Table:
    """
    Discover all spec/zfit files for an observation, read their metadata,
    apply chi2-informed redshift consensus, and return an astropy Table.

    Parameters
    ----------
    obs_name : str
        Observation name (e.g. 'ember_uds_p4')
    obs_dir : Path
        Path to the observation products directory
    reduction_version : str
        Pipeline version string to embed
    field : str
        Field name (e.g. 'uds') to store in table metadata
    program_slug : str
        CAMPFIRE program slug (e.g. 'ember') to store in table metadata
    consensus_config : dict, optional
        Redshift consensus parameters (delta_chi2_peak, dv_tolerance).
        Uses defaults if not provided.

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

    # Consensus parameters
    cc = consensus_config or {}
    delta_chi2_peak = cc.get('delta_chi2_peak', 30.0)
    dv_tolerance = cc.get('dv_tolerance', 1000.0)
    dz_prism_confirm = cc.get('dz_prism_confirm', 0.1)
    min_chi2_range_per_pix = cc.get('min_chi2_range_per_pix', 0.05)

    # ------------------------------------------------------------------
    # Phase 1: read per-grating metadata + zfit data
    # ------------------------------------------------------------------
    rows = []
    # Scalar zfit data grouped by source_id for fallback
    scalar_by_source: dict[str, dict[str, dict]] = defaultdict(dict)
    # Full chi2 curves grouped by source_id for consensus
    chi2_by_source: dict[str, dict[str, dict]] = defaultdict(dict)

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

            # Scalar data for fallback
            scalar_by_source[meta['source_id']][meta['grating']] = {
                'redshift': zfit_data['redshift'],
                'exposure_time': meta.get('exposure_time', 0),
                'signal_to_noise': meta.get('signal_to_noise', 0),
            }

            # Full chi2 curve for consensus
            chi2_curve = read_zfit_chi2(zfit_path)
            if chi2_curve:
                chi2_curve['n_pix'] = meta.get('n_pix', 0)
                chi2_by_source[meta['source_id']][meta['grating']] = chi2_curve
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
    # Phase 2: apply redshift consensus per source
    # ------------------------------------------------------------------
    best_z_by_source = {}
    for source_id in set(list(scalar_by_source.keys()) + list(chi2_by_source.keys())):
        best_z_by_source[source_id] = determine_best_redshift(
            chi2_by_grating=chi2_by_source.get(source_id),
            scalar_by_grating=scalar_by_source.get(source_id),
            delta_chi2_peak=delta_chi2_peak,
            dv_tolerance=dv_tolerance,
            dz_prism_confirm=dz_prism_confirm,
            min_chi2_range_per_pix=min_chi2_range_per_pix,
        )

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
        'n_pix', 'exposure_time', 'signal_to_noise',
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
    summary.meta['field'] = field
    summary.meta['program_slug'] = program_slug
    summary.meta['reduction_version'] = reduction_version
    summary.meta['generated_at'] = datetime.utcnow().isoformat()
    summary.meta['n_sources'] = len(set(summary['source_id']))
    summary.meta['n_spectra'] = len(summary)

    # Provenance: capture package versions and environment for reproducibility
    import campfire_pipeline
    summary.meta['cfpipe_version'] = campfire_pipeline.__version__
    try:
        import jwst
        summary.meta['jwst_version'] = jwst.__version__
    except ImportError:
        summary.meta['jwst_version'] = 'unknown'
    summary.meta['crds_context'] = os.environ.get('CRDS_CONTEXT', 'unknown')

    return summary


def write_effective_config(config: dict, obs_dir: Path, obs_name: str) -> Path:
    """
    Write the effective pipeline config to a TOML file in the products directory.

    This captures the fully-resolved config (package defaults + user overrides
    + per-observation overrides) used for this specific reduction, for
    reproducibility and provenance tracking. The deploy process reads this
    file and stores it as JSONB in the deployments table.

    Parameters
    ----------
    config : dict
        Effective pipeline configuration dictionary
    obs_dir : Path
        Observation products directory
    obs_name : str
        Observation name (used in filename)

    Returns
    -------
    output_path : Path
        Path to the written TOML file
    """
    import toml

    output_path = Path(obs_dir) / f"{obs_name}_config.toml"

    # Add provenance header
    provenance = {
        'generated_at': datetime.utcnow().isoformat(),
        'obs_name': obs_name,
    }
    import campfire_pipeline
    provenance['cfpipe_version'] = campfire_pipeline.__version__
    try:
        import jwst
        provenance['jwst_version'] = jwst.__version__
    except ImportError:
        provenance['jwst_version'] = 'unknown'
    provenance['crds_context'] = os.environ.get('CRDS_CONTEXT', 'unknown')

    output = {**config, '_provenance': provenance}
    with open(output_path, 'w') as f:
        toml.dump(output, f)

    log(f"Wrote effective config: {output_path}")
    return output_path


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
