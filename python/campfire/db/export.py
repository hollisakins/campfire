"""CSV catalog export from SQLite database.

Generates targets.csv, spectra.csv, and objects.csv as human-readable export
artifacts from the LocalStore. These files are written atomically so they're
always in a consistent state.
"""

import csv
from pathlib import Path
from typing import Tuple

from .store import (
    LocalStore,
    TARGET_EXPORT_COLUMNS,
    SPECTRA_EXPORT_COLUMNS,
    OBJECT_EXPORT_COLUMNS,
)


def export_catalogs(store: LocalStore, output_dir: Path) -> Tuple[int, int, int]:
    """Export targets.csv, spectra.csv, and objects.csv from the local database.

    Parameters
    ----------
    store : LocalStore
        The local database to export from.
    output_dir : Path
        Directory to write CSV files into (typically meta/).

    Returns
    -------
    tuple of (target_count, spectra_count, object_count)
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Query all targets
    targets = store.query_targets()

    target_rows = []
    spectra_rows = []

    for obj in targets:
        # Build target row
        obj_row = {col: obj.get(col) for col in TARGET_EXPORT_COLUMNS}
        target_rows.append(obj_row)

        # Build spectra rows
        for spec in obj.get("spectra", []):
            spec_row = {}
            for col in SPECTRA_EXPORT_COLUMNS:
                if col == "spectra_id":
                    spec_row[col] = spec.get("id") or spec.get("spectra_id")
                elif col == "signal_to_noise":
                    spec_row[col] = spec.get("signal_to_noise")
                else:
                    spec_row[col] = spec.get(col)

            # Infer local_path if not set
            if not spec_row.get("local_path"):
                obs = obj.get("observation", "")
                filename = Path(spec.get("fits_path", "")).name
                spec_row["local_path"] = f"{obs}/{filename}" if obs else filename

            spectra_rows.append(spec_row)

    _atomic_csv_write(output_dir / "targets.csv", TARGET_EXPORT_COLUMNS, target_rows)
    _atomic_csv_write(output_dir / "spectra.csv", SPECTRA_EXPORT_COLUMNS, spectra_rows)

    # Export sky-objects
    sky_objects = store.query_sky_objects()
    object_rows = []
    for obj in sky_objects:
        row = {}
        for col in OBJECT_EXPORT_COLUMNS:
            val = obj.get(col)
            # List fields are already deserialized by query_sky_objects;
            # serialize back to semicolons for CSV
            if isinstance(val, list):
                val = ";".join(str(v) for v in val)
            row[col] = val
        object_rows.append(row)

    _atomic_csv_write(output_dir / "objects.csv", OBJECT_EXPORT_COLUMNS, object_rows)

    return len(target_rows), len(spectra_rows), len(object_rows)


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
