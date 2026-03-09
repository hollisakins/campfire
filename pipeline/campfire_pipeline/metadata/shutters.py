"""
Generate shutters ECSV with per-shutter state for MSA shutter overlay.

Produces an ECSV file per observation with one row per open shutter per
exposure. Each shutter is either 'source' (contains the target) or 'open'
(background shutter), determined by parsing the shutter_state string from
the exposure table.

No deduplication is performed here — the ECSV is a faithful record of all
open shutters across all exposures and gratings. Deduplication is done at
deployment time.
"""

from collections import defaultdict
from pathlib import Path

from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.common.io import log
from campfire_pipeline.nirspec.slits import compute_slit_centers, get_source_pos


def generate_shutters_table(obs_name, obs_dir, field):
    """Generate a shutters table for all sources in an observation.

    Processes each spec file individually to preserve grating information.
    Returns one row per open shutter per exposure row (no deduplication).

    Parameters
    ----------
    obs_name : str
        Observation name (e.g. 'ember_uds_p4').
    obs_dir : Path or str
        Path to the observation products directory.
    field : str
        Field name (e.g. 'uds').

    Returns
    -------
    astropy.table.Table
        Shutters table with columns: object_id, source_id, observation, field,
        grating, center_ra, center_dec, position_angle, shutter_idx,
        shutter_state, v3pa.
    """
    obs_dir = Path(obs_dir)

    # Discover all spec files
    spec_files = sorted(obs_dir.glob('*_spec.fits'))
    if not spec_files:
        log(f"No spec files found in {obs_dir}")
        return Table()

    # Group spec files by source_id
    source_files = defaultdict(list)
    for spec_path in spec_files:
        stem = spec_path.stem.replace('_spec', '')
        parts = stem.split('_')
        try:
            source_id = int(parts[-1])
        except (ValueError, IndexError):
            log(f"Warning: cannot extract source_id from {spec_path.name}, skipping")
            continue
        source_files[source_id].append(str(spec_path))

    rows = []

    for source_id, files in sorted(source_files.items()):
        object_id = f"{obs_name}_{source_id}"

        # Compute source position once from first file
        try:
            source_ra, source_dec = get_source_pos(files[0])
        except Exception as e:
            log(f"Warning: failed to get source position for {source_id}: {e}")
            continue

        # Process each spec file individually to preserve grating info
        for spec_file in files:
            grating = _get_grating(spec_file)

            try:
                slit_data = compute_slit_centers(
                    [spec_file],
                    corrected_pos=(source_ra, source_dec),
                )
            except Exception as e:
                log(f"Warning: failed to compute slit centers for "
                    f"source {source_id} ({grating}): {e}")
                continue

            for sd in slit_data:
                rows.append({
                    'object_id': object_id,
                    'source_id': source_id,
                    'observation': obs_name,
                    'field': field,
                    'grating': grating,
                    'center_ra': sd['center_ra'],
                    'center_dec': sd['center_dec'],
                    'position_angle': sd['position_angle'],
                    'shutter_idx': sd['shutter_idx'],
                    'shutter_state': sd['shutter_state'],
                    'v3pa': sd['v3pa'],
                })

    if not rows:
        return Table()

    # Build table with explicit column ordering
    col_order = [
        'object_id', 'source_id', 'observation', 'field', 'grating',
        'center_ra', 'center_dec', 'position_angle',
        'shutter_idx', 'shutter_state', 'v3pa',
    ]
    table_data = {col: [r[col] for r in rows] for col in col_order}
    table = Table(table_data)

    table.meta['obs_name'] = obs_name
    table.meta['field'] = field

    n_sources = len(set(table['source_id']))
    log(f"Generated shutters table: {len(table)} entries for {n_sources} sources")

    return table


def write_shutters_ecsv(table, obs_dir, obs_name):
    """Write the shutters table to an ECSV file.

    Parameters
    ----------
    table : astropy.table.Table
    obs_dir : Path or str
    obs_name : str

    Returns
    -------
    Path
        Path to the written ECSV file.
    """
    obs_dir = Path(obs_dir)
    output_path = obs_dir / f"{obs_name}_shutters.ecsv"
    table.write(output_path, format='ascii.ecsv', overwrite=True)
    log(f"Wrote shutters ECSV: {output_path} ({len(table)} rows)")
    return output_path


def _get_grating(spec_file):
    """Read the GRATING keyword from a FITS file's primary header."""
    try:
        with fits.open(spec_file) as hdul:
            return hdul[0].header.get('GRATING', 'UNKNOWN')
    except Exception:
        return 'UNKNOWN'
