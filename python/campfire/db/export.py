"""CSV catalog export from SQLite database.

Generates targets.csv and spectra.csv as human-readable export artifacts
from the LocalStore. These files are written atomically so they're always
in a consistent state.
"""

import csv
from pathlib import Path
from typing import Tuple

from .store import LocalStore, OBJECT_EXPORT_COLUMNS, SPECTRA_EXPORT_COLUMNS


def export_catalogs(store: LocalStore, output_dir: Path) -> Tuple[int, int]:
    """Export objects.csv and spectra.csv from the local SQLite database.

    Parameters
    ----------
    store : LocalStore
        The local database to export from.
    output_dir : Path
        Directory to write CSV files into (typically meta/).

    Returns
    -------
    tuple of (object_count, spectra_count)
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Query all objects
    objects = store.query_objects()

    object_rows = []
    spectra_rows = []

    for obj in objects:
        # Build object row
        obj_row = {col: obj.get(col) for col in OBJECT_EXPORT_COLUMNS}
        object_rows.append(obj_row)

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

    _atomic_csv_write(output_dir / "targets.csv", OBJECT_EXPORT_COLUMNS, object_rows)
    _atomic_csv_write(output_dir / "spectra.csv", SPECTRA_EXPORT_COLUMNS, spectra_rows)

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
