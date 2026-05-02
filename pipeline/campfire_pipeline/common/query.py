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
import concurrent.futures
import sys
import threading
from pathlib import Path

import requests
from tqdm import tqdm

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


def _download_one(file_info, output_dir, token, pbar, pbar_lock):
    """Download a single file, updating the shared byte-progress bar.

    Returns ('downloaded' | 'error', filename, error_message_or_None).
    """
    filename = file_info["filename"]
    output_path = output_dir / filename
    tmp_path = output_path.with_suffix(".tmp")
    headers = {"Authorization": f"token {token}"} if token else {}

    try:
        resp = requests.get(
            f"{BASE_URL}/retrieve_product",
            params={"product_name": file_info["uri"]},
            stream=True,
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()

        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                with pbar_lock:
                    pbar.update(len(chunk))

        tmp_path.rename(output_path)
        return "downloaded", filename, None

    except (requests.RequestException, OSError) as e:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return "error", filename, str(e)


def download_files(files, output_dir, token=None, workers=4, desc="downloading"):
    """Download a list of files concurrently with a single aggregate progress bar.

    Parameters
    ----------
    files : list of dict
        Each dict has 'filename', 'uri', and 'size'.
    output_dir : Path
        Destination directory (must already exist).
    token : str or None
        MAST API token for authentication.
    workers : int
        Number of parallel download streams.
    desc : str
        Label shown in the progress bar.

    Returns the number of files that errored.
    """
    if not files:
        return 0

    total_size = sum(f["size"] for f in files)
    pbar_lock = threading.Lock()
    errors = 0

    workers = max(1, min(workers, len(files)))

    with tqdm(
        total=total_size,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=f"  {desc}",
        dynamic_ncols=True,
    ) as pbar:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(_download_one, f, output_dir, token, pbar, pbar_lock)
                for f in files
            ]
            try:
                for future in concurrent.futures.as_completed(futures):
                    status, name, err = future.result()
                    if status == "error":
                        errors += 1
                        tqdm.write(f"    ERROR {name}: {err}")
            except KeyboardInterrupt:
                for fut in futures:
                    fut.cancel()
                raise

    return errors


def _split_existing(files, output_dir):
    """Partition files into (to_download, to_skip) based on what's on disk."""
    to_download = []
    to_skip = []
    for f in files:
        path = output_dir / f["filename"]
        if path.exists() and path.stat().st_size == f["size"]:
            to_skip.append(f)
        else:
            to_download.append(f)
    return to_download, to_skip


def download_jwst_data(program_id, instrument="NIRSPEC", exp_type="NRS_MSASPEC",
                       download_dir="data", dry_run=False, obs_id=None, token=None,
                       workers=4):
    """Download JWST level 1b data for a program.

    Auxiliary metadata files (e.g. NIRSpec MSA metafiles) are downloaded
    first, so reduction can begin while the larger uncal files are still
    being fetched.

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
    workers : int
        Number of parallel download streams (default 4).
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
    all_files = aux_files + uncal_files
    total_size = sum(f["size"] for f in all_files)

    print()
    aux_label = f" and {len(aux_files)} unique MSA files" if aux_files else ""
    print(f"Selected {len(uncal_files)} uncal files{aux_label} ({format_size(total_size)} total)")

    if not all_files:
        print("No matching files found. Exiting.")
        return

    # Check which files already exist
    output_dir = Path(download_dir) / str(program_id)
    aux_to_dl, aux_skip = _split_existing(aux_files, output_dir)
    uncal_to_dl, uncal_skip = _split_existing(uncal_files, output_dir)

    to_skip = aux_skip + uncal_skip
    to_download = aux_to_dl + uncal_to_dl

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

    if not to_download:
        print("\nNothing to download — all files already exist.")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    dl_size = sum(f["size"] for f in to_download)

    print()
    print(f"Downloading {len(to_download)} files ({format_size(dl_size)}) to {output_dir}/ "
          f"with {workers} parallel stream{'s' if workers != 1 else ''}")

    errors = 0

    # Step 4a: Aux/MSA metafiles first so reduction can start early
    if aux_to_dl:
        print(f"\nFetching {len(aux_to_dl)} metafile(s) first...")
        errors += download_files(
            aux_to_dl, output_dir, token=token, workers=workers, desc="metafiles",
        )

    # Step 4b: Uncal data files
    if uncal_to_dl:
        print(f"\nFetching {len(uncal_to_dl)} uncal file(s)...")
        errors += download_files(
            uncal_to_dl, output_dir, token=token, workers=workers, desc="uncal    ",
        )

    print()
    downloaded = len(to_download) - errors
    print(f"Complete: {downloaded} downloaded, {len(to_skip)} skipped, {errors} errors")
    print(f"Location: {output_dir}/")


def download_nirspec_uncal(program_id, download_dir="data", exp_type="NRS_MSASPEC",
                           dry_run=False, workers=4):
    """Backwards-compatible wrapper for download_jwst_data()."""
    download_jwst_data(
        program_id=program_id,
        instrument="NIRSPEC",
        exp_type=exp_type,
        download_dir=download_dir,
        dry_run=dry_run,
        workers=workers,
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
    parser.add_argument("-p", "--processes", type=int, default=4, help="Parallel download streams (default: 4)")

    args = parser.parse_args()

    try:
        download_jwst_data(
            program_id=args.program,
            instrument="NIRSPEC",
            exp_type=args.exp_type,
            download_dir=args.download_dir,
            dry_run=args.dry_run,
            workers=args.processes,
        )
    except requests.HTTPError as e:
        print(f"\nAPI error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nInterrupted. Re-run to resume (existing files will be skipped).")
        sys.exit(130)


if __name__ == "__main__":
    main()
