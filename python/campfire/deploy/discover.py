"""
Non-ECSV file discovery — globs for RGB, SED, and slit files.

These files aren't tracked in the summary ECSV, so we discover them
by globbing the observation products directory.
"""

import json
from pathlib import Path


def discover_rgb_images(obs_dir: Path) -> list[Path]:
    """Find all *_rgb.png files in the observation directory."""
    return sorted(obs_dir.glob('*_rgb.png'))


def discover_sed_plots(obs_dir: Path) -> list[Path]:
    """Find all *_sed.pdf files in the observation directory."""
    return sorted(obs_dir.glob('*_sed.pdf'))


def discover_slits_json(obs_dir: Path, obs_name: str) -> Path | None:
    """Find the slit geometry JSON file, or None if absent."""
    slits_path = obs_dir / f'{obs_name}_slits.json'
    return slits_path if slits_path.exists() else None


def load_slits_json(slits_path: Path) -> list[dict]:
    """Load and return slit geometry records from JSON."""
    with open(slits_path) as f:
        return json.load(f)


def discover_shutters_ecsv(obs_dir: Path, obs_name: str) -> Path | None:
    """Find the shutters ECSV file, or None if absent."""
    ecsv_path = obs_dir / f'{obs_name}_shutters.ecsv'
    return ecsv_path if ecsv_path.exists() else None


def load_shutters_ecsv(ecsv_path: Path) -> list[dict]:
    """Load shutters ECSV, deduplicate, and return dicts for Supabase insertion.

    The ECSV contains one row per open shutter per exposure (including
    duplicates from different gratings at the same nod). This function
    deduplicates by (object_id, shutter_idx, position, v3pa) so that
    same-nod/different-grating entries collapse, while different nods
    (different shutter positions on sky) are preserved.

    After dedup, assigns sequential dither_ids per (object_id, shutter_idx)
    and drops ECSV-only columns (grating, v3pa) before returning.
    """
    from astropy.table import Table

    table = Table.read(ecsv_path, format='ascii.ecsv')

    # Deduplicate: same physical shutter at same sky position from different
    # gratings or repeated exposures. Uses tolerance-based merging (0.05")
    # rather than fixed binning to avoid boundary artifacts. The tolerance
    # is well above measurement noise (~10 mas) and well below the shutter
    # pitch (0.53").
    TOLERANCE_DEG = 0.05 / 3600  # 0.05 arcsec in degrees

    # Group rows by (object_id, shutter_idx, v3pa) then merge nearby positions
    from collections import defaultdict
    groups_raw: dict[tuple, list[dict]] = defaultdict(list)
    for row in table:
        gkey = (
            str(row['object_id']),
            int(row['shutter_idx']),
            round(float(row['v3pa']), 2),
        )
        groups_raw[gkey].append(dict(row))

    deduped = []
    for entries in groups_raw.values():
        # Greedily merge: for each entry, check if it's within tolerance
        # of an already-accepted entry. If so, skip it.
        accepted = []
        for entry in entries:
            ra = float(entry['center_ra'])
            dec = float(entry['center_dec'])
            is_dup = False
            for acc in accepted:
                if (abs(ra - float(acc['center_ra'])) < TOLERANCE_DEG and
                        abs(dec - float(acc['center_dec'])) < TOLERANCE_DEG):
                    is_dup = True
                    break
            if not is_dup:
                accepted.append(entry)
        deduped.extend(accepted)

    # Assign sequential dither_ids per (object_id, shutter_idx) group
    dither_groups: dict[tuple, int] = {}
    for row in deduped:
        gkey = (row['object_id'], row['shutter_idx'])
        if gkey not in dither_groups:
            dither_groups[gkey] = 0
        row['dither_id'] = dither_groups[gkey]
        dither_groups[gkey] += 1

    # Convert to database-ready dicts (drop ECSV-only columns)
    records = []
    for row in deduped:
        records.append({
            'field': str(row['field']),
            'observation': str(row['observation']),
            'object_id': str(row['object_id']),
            'source_id': int(row['source_id']),
            'center_ra': float(row['center_ra']),
            'center_dec': float(row['center_dec']),
            'position_angle': float(row['position_angle']),
            'shutter_idx': int(row['shutter_idx']),
            'dither_id': int(row['dither_id']),
            'shutter_state': str(row['shutter_state']),
        })
    return records


def discover_pointings_ecsv(obs_dir: Path, obs_name: str) -> Path | None:
    """Find the pointings ECSV file, or None if absent."""
    ecsv_path = obs_dir / f'{obs_name}_pointings.ecsv'
    return ecsv_path if ecsv_path.exists() else None


def load_pointings_ecsv(ecsv_path: Path) -> list[dict]:
    """Load pointings ECSV and return JSONB-ready dicts.

    Each dict represents one MSA pointing with geometry, exposure
    aggregates, and a 4-quadrant footprint. Semicolon-joined string
    columns (gratings, filters, jwst_obs_ids) are split back into lists.
    The footprint column (4 x 4 x 2 numpy array) becomes a nested list
    of [ra, dec] corners.
    """
    from astropy.table import Table

    table = Table.read(ecsv_path, format='ascii.ecsv')

    records = []
    for row in table:
        gratings_s = str(row['gratings']) if row['gratings'] else ''
        filters_s = str(row['filters']) if row['filters'] else ''
        obs_ids_s = str(row['jwst_obs_ids']) if row['jwst_obs_ids'] else ''
        footprint = row['footprint']
        records.append({
            'msametid': int(row['msametid']),
            'msametfl': str(row['msametfl']),
            'ra_center': float(row['ra_center']),
            'dec_center': float(row['dec_center']),
            'pa_aper': float(row['pa_aper']),
            'gratings': [g for g in gratings_s.split(';') if g],
            'filters': [f for f in filters_s.split(';') if f],
            'jwst_program': int(row['jwst_program']),
            'jwst_obs_ids': [o for o in obs_ids_s.split(';') if o],
            'n_exposures': int(row['n_exposures']),
            'n_dithers': int(row['n_dithers']),
            'exptime_total': float(row['exptime_total']),
            'date_obs_start': str(row['date_obs_start']),
            'date_obs_end': str(row['date_obs_end']),
            'footprint': [
                [[float(footprint[i, j, 0]), float(footprint[i, j, 1])]
                 for j in range(footprint.shape[1])]
                for i in range(footprint.shape[0])
            ],
        })
    return records


def filter_files_by_source_ids(
    files: list[Path],
    source_ids: list[int],
    obs_name: str,
) -> list[Path]:
    """
    Filter a file list to only include files matching the given source IDs.

    Handles filename patterns:
      - RGB: {obs_name}_{source_id}_rgb.png
      - SED: {obs_name}_{source_id}_sed.pdf
    """
    if not source_ids:
        return files

    allowed = {str(sid) for sid in source_ids}
    filtered = []

    for path in files:
        filename = path.name
        # Strip known suffixes to get base
        for suffix in ('_rgb.png', '_sed.pdf'):
            if filename.endswith(suffix):
                base = filename[:-len(suffix)]
                # Remove obs_name prefix
                prefix = obs_name + '_'
                if base.startswith(prefix):
                    extracted_id = base[len(prefix):]
                    if extracted_id in allowed:
                        filtered.append(path)
                break

    return filtered


def extract_object_ids_from_files(
    files: list[Path],
    suffix: str,
) -> set[str]:
    """
    Extract object_ids from filenames by stripping a known suffix.

    Example: ember_uds_p4_12345_sed.pdf -> ember_uds_p4_12345
    """
    object_ids = set()
    for path in files:
        if path.name.endswith(suffix):
            object_id = path.name[:-len(suffix)]
            object_ids.add(object_id)
    return object_ids
