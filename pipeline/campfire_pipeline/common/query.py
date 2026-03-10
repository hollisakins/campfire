#!/usr/bin/env python
"""
Download JWST NIRSpec raw data from MAST using the JWST Search API.

Retrieves level 1b uncalibrated (*_uncal.fits) and MSA metadata (*_msa.fits)
files for a given program ID, filtering server-side to avoid downloading
product lists for all calibration levels.

Usage:
    python query.py --program 6585
    python query.py --program 6585 --dry-run
    python query.py --program 2561 --exp-type NRS_MSASPEC
"""

import argparse
import sys
import time
from pathlib import Path

import requests

BASE_URL = "https://mast.stsci.edu/search/jwst/api/v0.1"


def search_filesets(program_id, instrument="NIRSPEC", exp_type="NRS_MSASPEC",
                    obs_id=None, token=None):
    """Query MAST for level 1b filesets in a program.

    Returns a list of dicts with fileSetName and observation metadata.
    """
    obs_label = f" obs {obs_id}" if obs_id else ""
    print(f"Searching for {instrument} {exp_type} level 1b filesets in program {program_id}{obs_label}...")

    conditions = [
        {"program": str(program_id)},
        {"instrume": instrument},
        {"exp_type": exp_type},
        {"productLevel": "1b"},
    ]
    if obs_id is not None:
        conditions.append({"observtn": str(obs_id)})

    headers = {"Authorization": f"token {token}"} if token else {}
    resp = requests.post(
        f"{BASE_URL}/search",
        json={
            "conditions": conditions,
            "select_cols": [
                "fileSetName", "productLevel", "opticalElements",
                "filter", "date_obs", "duration", "observtn", "exposure",
            ],
            "limit": 5000,
        },
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()

    results = data["results"]
    total = data["totalResults"]

    if total > len(results):
        print(f"  Warning: {total} filesets found but only {len(results)} returned (limit 5000)")

    print(f"Found {total} filesets")
    return results


def list_products_batched(filesets, batch_size=25, token=None):
    """Retrieve product lists for filesets in batches.

    Returns a flat list of all product dicts across all filesets.
    """
    fileset_names = [f["fileSetName"] for f in filesets]
    all_products = []
    n_batches = (len(fileset_names) + batch_size - 1) // batch_size
    headers = {"Authorization": f"token {token}"} if token else {}

    for i in range(0, len(fileset_names), batch_size):
        batch = fileset_names[i : i + batch_size]
        batch_num = i // batch_size + 1
        print(f"  Retrieving product lists... (batch {batch_num}/{n_batches})")

        resp = requests.get(
            f"{BASE_URL}/list_products",
            params={"dataset_ids": ",".join(batch)},
            headers=headers,
        )
        resp.raise_for_status()
        all_products.extend(resp.json()["products"])

    return all_products


def filter_products(products, instrument="NIRSPEC"):
    """Filter products to uncal FITS and instrument-specific auxiliary files.

    For NIRSPEC, also returns deduplicated MSA metadata files.

    Returns (uncal_files, aux_files) where each is a list of
    dicts with 'filename', 'uri', and 'size' keys.
    """
    uncal_files = [
        p for p in products
        if p["filename"].endswith("_uncal.fits")
    ]

    aux_files = []
    if instrument == "NIRSPEC":
        # MSA files are duplicated across filesets in the same nod group;
        # deduplicate by filename, keeping the first occurrence
        seen_msa = set()
        for p in products:
            if p["filename"].endswith("_msa.fits") and p["filename"] not in seen_msa:
                seen_msa.add(p["filename"])
                aux_files.append(p)

    return uncal_files, aux_files


def format_size(size_bytes):
    """Format byte count as human-readable string."""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / 1024 ** 3:.2f} GB"
    if size_bytes >= 1024 ** 2:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def progress_bar(fraction, width=30):
    """Render a progress bar string like [████████░░░░░░░░░░]."""
    filled = int(width * fraction)
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def format_speed(bytes_per_sec):
    """Format download speed as human-readable string."""
    if bytes_per_sec >= 1024 ** 2:
        return f"{bytes_per_sec / 1024 ** 2:.1f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.0f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"


def download_file(uri, output_path, size, index, total, token=None):
    """Download a single file with progress bar and speed.

    Returns 'downloaded' or 'error'.
    """
    label = f"  [{index}/{total}]"
    name = output_path.name
    size_str = format_size(size)
    headers = {"Authorization": f"token {token}"} if token else {}

    try:
        resp = requests.get(
            f"{BASE_URL}/retrieve_product",
            params={"product_name": uri},
            stream=True,
            headers=headers,
        )
        resp.raise_for_status()

        tmp_path = output_path.with_suffix(".tmp")
        downloaded = 0
        t_start = time.monotonic()

        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                elapsed = time.monotonic() - t_start
                speed = downloaded / elapsed if elapsed > 0 else 0
                frac = downloaded / size if size > 0 else 0
                bar = progress_bar(frac)
                line = f"\r{label} {name}  {bar} {format_size(downloaded)}/{size_str}  {format_speed(speed)}"
                print(line, end="", flush=True)

        tmp_path.rename(output_path)
        elapsed = time.monotonic() - t_start
        speed = size / elapsed if elapsed > 0 else 0
        print(f"\r{label} {name}  {size_str}  done in {elapsed:.0f}s ({format_speed(speed)})" + " " * 20)
        return "downloaded"

    except (requests.RequestException, OSError) as e:
        print(f"\r{label} {name}  ERROR: {e}" + " " * 40)
        tmp_path = output_path.with_suffix(".tmp")
        if tmp_path.exists():
            tmp_path.unlink()
        return "error"


def download_jwst_data(program_id, instrument="NIRSPEC", exp_type="NRS_MSASPEC",
                       download_dir="data", dry_run=False, obs_id=None, token=None):
    """Download JWST level 1b data for a program.

    Parameters
    ----------
    program_id : int
        JWST program ID.
    instrument : str
        Instrument name ('NIRSPEC' or 'NIRCAM').
    exp_type : str
        Exposure type (e.g. 'NRS_MSASPEC', 'NRC_IMAGE').
    download_dir : str
        Base download directory. Files go into download_dir/program_id/.
    dry_run : bool
        If True, list files without downloading.
    obs_id : int or None
        JWST observation number to filter by (e.g. 1, 2, 3).
    token : str or None
        MAST API token for accessing proprietary data.
    """
    if token:
        print("Using MAST API token for authentication.")

    # Step 1: Search for filesets
    filesets = search_filesets(program_id, instrument, exp_type, obs_id=obs_id, token=token)
    if not filesets:
        print("No filesets found. Exiting.")
        return

    # Step 2: List products
    print()
    products = list_products_batched(filesets, token=token)
    print(f"  {len(products)} total products across {len(filesets)} filesets")

    # Step 3: Filter
    uncal_files, aux_files = filter_products(products, instrument)
    all_files = uncal_files + aux_files
    total_size = sum(f["size"] for f in all_files)

    print()
    aux_label = f" and {len(aux_files)} unique MSA files" if aux_files else ""
    print(f"Selected {len(uncal_files)} uncal files{aux_label} ({format_size(total_size)} total)")

    if not all_files:
        print("No matching files found. Exiting.")
        return

    # Check which files already exist
    output_dir = Path(download_dir) / str(program_id)
    to_download = []
    to_skip = []
    for f in all_files:
        path = output_dir / f["filename"]
        if path.exists() and path.stat().st_size == f["size"]:
            to_skip.append(f)
        else:
            to_download.append(f)

    if to_skip:
        skip_size = sum(f["size"] for f in to_skip)
        print(f"  {len(to_skip)} files already exist ({format_size(skip_size)}), will be skipped")

    if dry_run:
        print()
        if to_download:
            dl_size = sum(f["size"] for f in to_download)
            print(f"Files to download ({len(to_download)}, {format_size(dl_size)}):")
            for f in to_download:
                print(f"  {f['filename']:55s}  {format_size(f['size']):>10s}")
        else:
            print("Nothing to download — all files already exist.")
        if to_skip:
            print(f"\nAlready downloaded ({len(to_skip)}):")
            for f in to_skip:
                print(f"  {f['filename']:55s}  {format_size(f['size']):>10s}  (exists)")
        return

    # Step 4: Download
    if not to_download:
        print("\nNothing to download — all files already exist.")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    dl_size = sum(f["size"] for f in to_download)

    print()
    print(f"Downloading {len(to_download)} files ({format_size(dl_size)}) to {output_dir}/")

    errors = 0
    for i, f in enumerate(to_download, 1):
        output_path = output_dir / f["filename"]
        result = download_file(f["uri"], output_path, f["size"], i, len(to_download), token=token)
        if result == "error":
            errors += 1

    print()
    downloaded = len(to_download) - errors
    print(f"Complete: {downloaded} downloaded, {len(to_skip)} skipped, {errors} errors")
    print(f"Location: {output_dir}/")


def download_nirspec_uncal(program_id, download_dir="data", exp_type="NRS_MSASPEC", dry_run=False):
    """Backwards-compatible wrapper for download_jwst_data()."""
    download_jwst_data(
        program_id=program_id,
        instrument="NIRSPEC",
        exp_type=exp_type,
        download_dir=download_dir,
        dry_run=dry_run,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Download JWST NIRSpec raw (uncal + MSA) data from MAST",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python query.py --program 6585
  python query.py --program 6585 --dry-run
  python query.py --program 2561 --download-dir /data/jwst
  python query.py --program 1210 --exp-type NRS_FIXEDSLIT
        """,
    )

    parser.add_argument("--program", type=int, required=True, help="JWST program ID")
    parser.add_argument("--download-dir", default="data", help="Base directory for downloads (default: data)")
    parser.add_argument("--dry-run", action="store_true", help="List files without downloading")
    parser.add_argument("--exp-type", default="NRS_MSASPEC", help="NIRSpec exposure type (default: NRS_MSASPEC)")

    args = parser.parse_args()

    try:
        download_nirspec_uncal(
            program_id=args.program,
            download_dir=args.download_dir,
            exp_type=args.exp_type,
            dry_run=args.dry_run,
        )
    except requests.HTTPError as e:
        print(f"\nAPI error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nInterrupted. Re-run to resume (existing files will be skipped).")
        sys.exit(130)


if __name__ == "__main__":
    main()
