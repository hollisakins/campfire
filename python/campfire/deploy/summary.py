"""
ECSV summary reader — the primary metadata source for deployment.

Replaces all direct FITS metadata scanning. The pipeline writes a summary
ECSV per observation; this module reads it and builds the records needed
for Supabase upserts and R2 upload planning.
"""

import math
import sys
from pathlib import Path

from astropy.table import Table

from campfire_layout import Scope, storage_key


def _finite_or_none(value) -> float | None:
    """Coerce a scalar to a JSON-safe float, mapping non-finite to None.

    PostgREST/httpx serialize request bodies with ``allow_nan=False``, so any
    ``inf``, ``-inf``, or ``nan`` float raises
    ``ValueError: Out of range float values are not JSON compliant`` and aborts
    the whole upsert. Non-finite science values (e.g. signal_to_noise = inf
    from a zero-noise division, or NaN from a failed fit) carry no meaningful
    measurement, so they map to SQL NULL.

    Returns None for None input or any non-finite float; otherwise the value
    as a float.
    """
    if value is None:
        return None
    val = float(value)
    return val if math.isfinite(val) else None


def _clean_str(value) -> str | None:
    """Coerce a table cell to a clean string, mapping empty/sentinel to None.

    ECSV string columns surface absent cells as ``''`` or a masked value, and a
    Python ``None`` round-trips as the literal ``'None'``. None of those are
    real provenance, so they map to None (which deploy then omits from the
    upsert body, leaving the column NULL).
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ('none', 'nan', '--'):
        return None
    return s


def load_summary(obs_dir: Path, obs_name: str, required: bool = True) -> Table | None:
    """
    Load the observation summary ECSV (the stage3 finals manifest).

    When ``required`` (default), a missing file is a hard error. When
    ``required=False`` (epic #210, B5), a missing summary returns None instead —
    the caller then treats the deploy as intermediates-only (no stage3 finals yet),
    which is automatically a draft.
    """
    ecsv_path = obs_dir / f"{obs_name}_summary.ecsv"
    if not ecsv_path.exists():
        if not required:
            return None
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
            'redshift_best': _finite_or_none(row['redshift_best']),
        })

    return objects


def get_spectra_records(summary: Table, obs_name: str) -> list[dict]:
    """
    Build per-spectrum records for Supabase spectra upserts.

    Returns list of dicts with keys:
        target_id, grating, fits_path (R2 key), observation, program_slug,
        signal_to_noise, exposure_time, file_hash, file_size,
        cfpipe_version, crds_context, jwst_version, date_obs, reduced_at,
        redshift_auto (per-grating zfit; Phase B)

    All provenance fields are carried verbatim from the FITS primary header
    (via the summary ECSV) — never recomputed — so a flux value traces back to
    the exact pipeline version, CRDS context, and reduction time.

    `observation` / `program_slug` are the row-local RLS scope columns
    (perf T2-A, #504). The database owns them: the sync_spectra_target_scope
    trigger re-copies both from the parent targets row on every insert/upsert,
    so what is sent here is belt and braces (and must agree with the targets
    upsert built from the same summary, which it does by construction).
    `program_slug` is omitted for old ECSVs whose metadata lacks it.

    `dq_flags` is intentionally absent: the pipeline does not produce
    per-spectrum DQ. New rows pick up the column default (0); existing rows
    keep whatever the inspection API has set (PostgREST upsert only updates
    columns present in the request body).
    """
    meta_program_slug = _clean_str(summary.meta.get('program_slug'))
    # cfpipe_version is the single pipeline-version string. Prefer the per-row
    # value (read verbatim from each product's CMPFRVER header, so a
    # [pipeline].version override flows through); fall back to the observation-
    # level meta for old ECSVs that only carried it there.
    meta_cfpipe_version = _clean_str(summary.meta.get('cfpipe_version'))

    # Check which per-row provenance columns exist (backward compat with old ECSVs)
    has_cfpipe_version = 'cfpipe_version' in summary.colnames
    # Pre-collapse ECSVs carried the per-row pipeline version as 'reduction_version'.
    has_reduction_version = 'reduction_version' in summary.colnames
    has_jwst_version = 'jwst_version' in summary.colnames
    has_crds_context = 'crds_context' in summary.colnames
    has_date_obs = 'date_obs' in summary.colnames
    has_reduced_at = 'reduced_at' in summary.colnames
    # Phase B: per-grating redshift_auto from zfit. Older ECSVs may not have it.
    has_redshift_auto = 'redshift_auto' in summary.colnames

    # Fallback to metadata for old ECSVs that lack per-row columns
    meta_jwst_version = _clean_str(summary.meta.get('jwst_version'))
    meta_crds_context = _clean_str(summary.meta.get('crds_context'))
    meta_reduced_at = _clean_str(summary.meta.get('reduced_at'))

    records = []
    for row in summary:
        r2_key = storage_key('nirspec_spec', Scope(obs=obs_name), row['fits_filename'])
        rec = {
            'target_id': row['object_id'],
            'grating': row['grating'],
            'fits_path': r2_key,
            'observation': obs_name,
            'signal_to_noise': _finite_or_none(row['signal_to_noise']),
            'exposure_time': _finite_or_none(row['exposure_time']),
            'file_hash': row['file_hash'],
            'file_size': int(row['file_size']) if row['file_size'] is not None else None,
        }
        # Per-row provenance from FITS headers (preferred), falling back to metadata
        cfpipe_version = (
            (_clean_str(row['cfpipe_version']) if has_cfpipe_version else None)
            or (_clean_str(row['reduction_version']) if has_reduction_version else None)
            or meta_cfpipe_version
        )
        jwst_version = (_clean_str(row['jwst_version']) if has_jwst_version else None) or meta_jwst_version
        crds_context = (_clean_str(row['crds_context']) if has_crds_context else None) or meta_crds_context
        date_obs = _clean_str(row['date_obs']) if has_date_obs else None
        reduced_at = (_clean_str(row['reduced_at']) if has_reduced_at else None) or meta_reduced_at

        if meta_program_slug:
            rec['program_slug'] = meta_program_slug
        if cfpipe_version:
            rec['cfpipe_version'] = cfpipe_version
        if jwst_version:
            rec['jwst_version'] = jwst_version
        if crds_context:
            rec['crds_context'] = crds_context
        if date_obs:
            rec['date_obs'] = date_obs
        if reduced_at:
            rec['reduced_at'] = reduced_at

        # Phase B: per-grating redshift_auto. Always include (even when null) so
        # the pipeline value is authoritative — a re-fit producing NULL clears
        # the previous value rather than silently keeping it.
        if has_redshift_auto:
            raw = row['redshift_auto']
            # zfit may emit NaN for failed fits (or inf); store either as NULL.
            rec['redshift_auto'] = _finite_or_none(raw)

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
