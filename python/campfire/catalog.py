"""Generate CSV catalogs from API data.

Produces objects.csv and spectra.csv in {data_dir}/.campfire_meta/ for easy
use with pandas, astropy, or any CSV-compatible tool.
"""

import csv
from pathlib import Path
from typing import Dict, List, Optional, Tuple


OBJECT_COLUMNS = [
    "object_id", "program_id", "program_name", "field", "observation",
    "ra", "dec", "redshift", "redshift_auto", "redshift_inspected",
    "redshift_quality", "spectral_features", "object_flags", "dq_flags",
    "max_snr", "last_inspected_at", "created_at", "updated_at",
]

SPECTRA_COLUMNS = [
    "spectra_id", "object_id", "grating", "fits_path", "file_hash",
    "file_size", "signal_to_noise", "reduction_version", "local_path",
]


def generate_catalogs(
    objects_data: List[dict],
    data_dir: Path,
    spectra_extra: Optional[Dict[int, dict]] = None,
) -> Tuple[int, int]:
    """Write objects.csv and spectra.csv atomically.

    Parameters
    ----------
    objects_data
        Objects from the /api/v1/objects endpoint (each with nested 'spectra' list).
    data_dir
        Base data directory containing .campfire_meta/.
    spectra_extra
        Optional dict of {spectra_id: {file_hash, file_size}} to merge into spectra rows.
        This allows enriching spectra data with fields from the manifest.

    Returns
    -------
    tuple of (object_count, spectra_count)
    """
    meta_dir = data_dir / ".campfire_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)

    spectra_extra = spectra_extra or {}

    object_rows = []
    spectra_rows = []

    for obj in objects_data:
        # Build object row
        obj_row = {}
        for col in OBJECT_COLUMNS:
            val = obj.get(col)
            # Handle nested program data
            if col == "program_name" and val is None:
                program = obj.get("program") or obj.get("programs") or {}
                if isinstance(program, dict):
                    val = program.get("program_name")
            obj_row[col] = val
        object_rows.append(obj_row)

        # Build spectra rows
        obs = obj.get("observation", "")
        for spec in obj.get("spectra", []):
            spec_id = spec.get("id")
            extra = spectra_extra.get(spec_id, {}) if spec_id else {}
            filename = Path(spec.get("fits_path", "")).name
            local_path = f"{obs}/{filename}" if obs else filename

            spec_row = {
                "spectra_id": spec_id,
                "object_id": spec.get("object_id") or obj.get("object_id"),
                "grating": spec.get("grating"),
                "fits_path": spec.get("fits_path"),
                "file_hash": extra.get("file_hash") or spec.get("file_hash"),
                "file_size": extra.get("file_size") or spec.get("file_size"),
                "signal_to_noise": spec.get("signal_to_noise"),
                "reduction_version": spec.get("reduction_version"),
                "local_path": local_path,
            }
            spectra_rows.append(spec_row)

    _atomic_csv_write(meta_dir / "objects.csv", OBJECT_COLUMNS, object_rows)
    _atomic_csv_write(meta_dir / "spectra.csv", SPECTRA_COLUMNS, spectra_rows)

    return len(object_rows), len(spectra_rows)


def _atomic_csv_write(path: Path, columns: list, rows: list) -> None:
    """Write CSV atomically via temp file + rename."""
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        tmp.rename(path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
