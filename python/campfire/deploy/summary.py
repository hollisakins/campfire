"""
ECSV summary reader — the primary metadata source for deployment.

Replaces all direct FITS metadata scanning. The pipeline writes a summary
ECSV per observation; this module reads it and builds the records needed
for Supabase upserts and R2 upload planning.
"""

import sys
from pathlib import Path

from astropy.table import Table


def load_summary(obs_dir: Path, obs_name: str) -> Table:
    """
    Load the observation summary ECSV.

    Raises SystemExit with a helpful message if the file is missing.
    """
    ecsv_path = obs_dir / f"{obs_name}_summary.ecsv"
    if not ecsv_path.exists():
        print(f"Error: Summary file not found: {ecsv_path}")
        print(f"Run `cfpipe nirspec summary --obs {obs_name}` first.")
        sys.exit(1)

    summary = Table.read(ecsv_path, format='ascii.ecsv')
    return summary


def filter_by_source_ids(summary: Table, source_ids: list[int]) -> Table:
    """Filter summary table to only rows matching the given source IDs."""
    if not source_ids:
        return summary

    str_ids = {str(sid) for sid in source_ids}
    mask = [str(row['source_id']) in str_ids for row in summary]
    return summary[mask]


def get_field(summary: Table) -> str:
    """Extract the field name from table metadata."""
    return summary.meta.get('field', '')


def get_obs_name(summary: Table) -> str:
    """Extract the observation name from table metadata."""
    return summary.meta.get('obs_name', '')


def get_unique_objects(summary: Table) -> list[dict]:
    """
    Deduplicate by object_id and return one record per unique target.

    Returns list of dicts with keys:
        object_id, source_id, program_slug, observation, ra, dec, redshift_best

    Note: ``object_id`` here is the ECSV column (the target identifier value).
    It maps to ``target_id`` in the database.
    """
    program_slug = summary.meta.get('program_slug', '')
    observation = summary.meta.get('obs_name', '')

    seen = set()
    objects = []

    for row in summary:
        oid = row['object_id']
        if oid in seen:
            continue
        seen.add(oid)
        objects.append({
            'object_id': oid,
            'source_id': str(row['source_id']),
            'program_slug': program_slug,
            'observation': observation,
            'ra': float(row['ra']),
            'dec': float(row['dec']),
            'redshift_best': float(row['redshift_best']) if row['redshift_best'] is not None else None,
        })

    return objects


def get_spectra_records(summary: Table, obs_name: str) -> list[dict]:
    """
    Build per-spectrum records for Supabase spectra upserts.

    Returns list of dicts with keys:
        target_id, grating, fits_path (R2 key), reduction_version,
        signal_to_noise, exposure_time, file_hash, file_size,
        date_obs, cfpipe_version, jwst_version, crds_context,
        redshift_auto (per-grating zfit; Phase B)

    `dq_flags` is intentionally absent: the pipeline does not produce
    per-spectrum DQ. New rows pick up the column default (0); existing rows
    keep whatever the inspection API has set (PostgREST upsert only updates
    columns present in the request body).
    """
    # cfpipe_version is a package version, same for all rows
    cfpipe_version = summary.meta.get('cfpipe_version')

    # Check which per-row provenance columns exist (backward compat with old ECSVs)
    has_jwst_version = 'jwst_version' in summary.colnames
    has_crds_context = 'crds_context' in summary.colnames
    has_date_obs = 'date_obs' in summary.colnames
    # Phase B: per-grating redshift_auto from zfit. Older ECSVs may not have it.
    has_redshift_auto = 'redshift_auto' in summary.colnames

    # Fallback to metadata for old ECSVs that lack per-row columns
    meta_jwst_version = summary.meta.get('jwst_version')
    meta_crds_context = summary.meta.get('crds_context')

    records = []
    for row in summary:
        r2_key = f"spectra/{obs_name}/{row['fits_filename']}"
        rec = {
            'target_id': row['object_id'],
            'grating': row['grating'],
            'fits_path': r2_key,
            'reduction_version': row['reduction_version'],
            'signal_to_noise': float(row['signal_to_noise']) if row['signal_to_noise'] is not None else None,
            'exposure_time': float(row['exposure_time']) if row['exposure_time'] is not None else None,
            'file_hash': row['file_hash'],
            'file_size': int(row['file_size']) if row['file_size'] is not None else None,
        }
        # Per-row provenance from FITS headers (preferred), falling back to metadata
        jwst_version = str(row['jwst_version']) if has_jwst_version and row['jwst_version'] else meta_jwst_version
        crds_context = str(row['crds_context']) if has_crds_context and row['crds_context'] else meta_crds_context
        date_obs = str(row['date_obs']) if has_date_obs and row['date_obs'] else None

        if cfpipe_version:
            rec['cfpipe_version'] = cfpipe_version
        if jwst_version:
            rec['jwst_version'] = jwst_version
        if crds_context:
            rec['crds_context'] = crds_context
        if date_obs:
            rec['date_obs'] = date_obs

        # Phase B: per-grating redshift_auto. Always include (even when null) so
        # the pipeline value is authoritative — a re-fit producing NULL clears
        # the previous value rather than silently keeping it.
        if has_redshift_auto:
            raw = row['redshift_auto']
            if raw is None:
                rec['redshift_auto'] = None
            else:
                val = float(raw)
                # zfit may emit NaN for failed fits; store as NULL.
                rec['redshift_auto'] = None if val != val else val

        records.append(rec)
    return records


def get_spec_paths(summary: Table, obs_dir: Path) -> list[Path]:
    """Resolve spec_file basenames to absolute paths."""
    paths = []
    for row in summary:
        spec_file = row['spec_file']
        if spec_file:
            paths.append(obs_dir / spec_file)
    return paths


def get_zfit_paths(summary: Table, obs_dir: Path) -> list[Path]:
    """Resolve zfit_file basenames to absolute paths (skip empty entries)."""
    paths = []
    seen = set()
    for row in summary:
        zfit_file = row['zfit_file']
        if zfit_file and zfit_file not in seen:
            seen.add(zfit_file)
            zfit_path = obs_dir / zfit_file
            if zfit_path.exists():
                paths.append(zfit_path)
    return paths


def get_program_slug(summary: Table) -> str:
    """Return the CAMPFIRE program slug from table metadata.

    Raises SystemExit if missing — all ECSVs must be regenerated
    with the updated pipeline before deploying.
    """
    slug = summary.meta.get('program_slug', '')
    if not slug:
        print("Error: ECSV missing 'program_slug' metadata.")
        print("Re-run: cfpipe nirspec summary --obs <name>")
        sys.exit(1)
    return slug
