"""CSV catalog export from SQLite database.

Generates objects.csv, spectra.csv, photometry.csv and lines.csv as
human-readable export artifacts from the LocalStore. These files are written
atomically so they're always in a consistent state.
"""

import csv
from pathlib import Path
from typing import Tuple

from .store import (
    LINE_FIT_EXPORT_COLUMNS,
    LocalStore,
    OBJECT_EXPORT_COLUMNS,
    PHOTOMETRY_EXPORT_COLUMNS,
    SPECTRA_EXPORT_COLUMNS,
)

# Per-line quantities pivoted into lines.csv as <prefix>_<line> columns.
LINE_EXPORT_QUANTITIES = (
    ("f", "flux"),          # erg/s/cm2
    ("e", "flux_err"),
    ("ew", "ew_rest"),      # rest-frame Angstrom
    ("ewe", "ew_rest_err"),
    ("flag", "flags"),      # bitmask, see campfire.flags.LineFlags
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

    # Wide-format emission-line catalog
    export_lines_csv(store.query_line_fits(), output_dir / "lines.csv")

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


def line_columns_for(records: list) -> list:
    """Line names present in any record, ordered by rest wavelength then name
    (broad components sort with their line)."""
    waves: dict = {}
    for rec in records:
        lines = rec.get("lines")
        if not isinstance(lines, dict):
            continue
        for name, data in lines.items():
            if name not in waves:
                w = (data or {}).get("wave_rest")
                waves[name] = float("inf") if w is None else float(w)
    return sorted(waves, key=lambda n: (waves[n], n))


def pivot_line_fit(rec: dict, line_names: list) -> dict:
    """Wide row: the per-spectrum scalars plus <prefix>_<line> columns."""
    row = {col: rec.get(col) for col in LINE_FIT_EXPORT_COLUMNS}
    lines = rec.get("lines") if isinstance(rec.get("lines"), dict) else {}
    for name in line_names:
        data = lines.get(name)
        for prefix, key in LINE_EXPORT_QUANTITIES:
            row[f"{prefix}_{name}"] = (data or {}).get(key) if data else None
    return row


def export_lines_csv(records: list, path: Path) -> list:
    """Write ``lines.csv`` (one row per spectrum, columns per line); returns the columns."""
    line_names = line_columns_for(records)
    columns = list(LINE_FIT_EXPORT_COLUMNS) + [
        f"{prefix}_{name}" for name in line_names for prefix, _key in LINE_EXPORT_QUANTITIES
    ]
    rows = [pivot_line_fit(rec, line_names) for rec in records]
    _atomic_csv_write(path, columns, rows)
    return columns


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
