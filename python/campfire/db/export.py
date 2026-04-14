"""CSV catalog export from SQLite database.

Generates targets.csv, spectra.csv, objects.csv, and photometry.csv as
human-readable export artifacts from the LocalStore. These files are written
atomically so they're always in a consistent state.
"""

import csv
import json
from pathlib import Path
from typing import Tuple

from .store import (
    LocalStore,
    TARGET_EXPORT_COLUMNS,
    SPECTRA_EXPORT_COLUMNS,
    OBJECT_EXPORT_COLUMNS,
    PHOTOMETRY_EXPORT_COLUMNS,
)


def export_catalogs(store: LocalStore, output_dir: Path) -> Tuple[int, int, int]:
    """Export targets.csv, spectra.csv, objects.csv, and photometry.csv.

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

    # Export wide-format photometry
    _export_photometry_csv(store, output_dir / "photometry.csv")

    return len(target_rows), len(spectra_rows), len(object_rows)


def _export_photometry_csv(store: LocalStore, path: Path) -> None:
    """Export photometry as a wide-format CSV with f_/e_ columns per band.

    One row per object-catalog combination. Bands are sorted by wavelength
    (using the ``wav`` field from the JSONB data). Missing bands get empty cells.
    """
    records = store.query_photometry()
    if not records:
        # Write empty file with just scalar headers
        _atomic_csv_write(path, PHOTOMETRY_EXPORT_COLUMNS, [])
        return

    # First pass: collect all band names with their wavelengths
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

    # Sort bands by wavelength, then alphabetically for ties
    sorted_bands = sorted(band_wavs.keys(), key=lambda b: (band_wavs[b], b))

    # Build columns: scalar fields + f_<band>, e_<band> for each band
    columns = list(PHOTOMETRY_EXPORT_COLUMNS) + [
        col for b in sorted_bands for col in (f"f_{b}", f"e_{b}")
    ]

    # Build rows
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
