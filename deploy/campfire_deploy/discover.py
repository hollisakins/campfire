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
