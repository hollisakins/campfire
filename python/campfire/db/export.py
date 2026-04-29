"""CSV catalog export from SQLite database.

Generates objects.csv, spectra.csv, and photometry.csv as human-readable
export artifacts from the LocalStore. These files are written atomically so
they're always in a consistent state.
"""

import csv
from pathlib import Path
from typing import Tuple

from .store import (
    LocalStore,
    OBJECT_EXPORT_COLUMNS,
    PHOTOMETRY_EXPORT_COLUMNS,
    SPECTRA_EXPORT_COLUMNS,
)


def export_catalogs(store: LocalStore, output_dir: Path) -> Tuple[int, int]:
    """Export objects.csv, spectra.csv, and photometry.csv.

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

    # Objects
    objects = store.query_objects()
    object_rows = []
    for obj in objects:
        row = {}
        for col in OBJECT_EXPORT_COLUMNS:
            val = obj.get(col)
            if isinstance(val, list):
                val = ";".join(str(v) for v in val)
            row[col] = val
        object_rows.append(row)

    _atomic_csv_write(output_dir / "objects.csv", OBJECT_EXPORT_COLUMNS, object_rows)

    # Spectra (flat, one row per spectrum)
    spectra = store.query_spectra()
    spectra_rows = []
    for spec in spectra:
        row = {col: spec.get(col) for col in SPECTRA_EXPORT_COLUMNS}
        if not row.get("local_path"):
            obs = spec.get("observation") or ""
            filename = Path(spec.get("fits_path", "")).name
            row["local_path"] = f"{obs}/{filename}" if obs else filename
        spectra_rows.append(row)

    _atomic_csv_write(output_dir / "spectra.csv", SPECTRA_EXPORT_COLUMNS, spectra_rows)

    # Wide-format photometry
    _export_photometry_csv(store, output_dir / "photometry.csv")

    return len(object_rows), len(spectra_rows)


def _export_photometry_csv(store: LocalStore, path: Path) -> None:
    """Export photometry as a wide-format CSV with f_/e_ columns per band."""
    records = store.query_photometry()
    if not records:
        _atomic_csv_write(path, PHOTOMETRY_EXPORT_COLUMNS, [])
        return

    band_wavs: dict = {}
    for rec in records:
        phot = rec.get("photometry")
        if not isinstance(phot, dict):
            continue
        bands = phot.get("bands", {})
        for band_name, band_data in bands.items():
            if band_name not in band_wavs:
                wav = band_data.get("wav", float("inf"))
                band_wavs[band_name] = wav

    sorted_bands = sorted(band_wavs.keys(), key=lambda b: (band_wavs[b], b))

    columns = list(PHOTOMETRY_EXPORT_COLUMNS) + [
        col for b in sorted_bands for col in (f"f_{b}", f"e_{b}")
    ]

    rows = []
    for rec in records:
        row = {col: rec.get(col) for col in PHOTOMETRY_EXPORT_COLUMNS}
        phot = rec.get("photometry")
        bands = phot.get("bands", {}) if isinstance(phot, dict) else {}
        for band_name in sorted_bands:
            data = bands.get(band_name)
            if data:
                row[f"f_{band_name}"] = data.get("flux")
                row[f"e_{band_name}"] = data.get("flux_err")
            else:
                row[f"f_{band_name}"] = None
                row[f"e_{band_name}"] = None
        rows.append(row)

    _atomic_csv_write(path, columns, rows)


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
