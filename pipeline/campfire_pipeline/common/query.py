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
                    obs_ids=None, filters=None, token=None):
    """Query MAST for level 1b filesets in a program.

    Returns a list of dicts with fileSetName and observation metadata.

    Parameters
    ----------
    obs_ids : list of int, optional
        Restrict to these observation numbers. Sent server-side as multiple
        equality conditions; an ``in`` operator isn't documented for the JWST
        search API, so we fan out one search per obs and merge.
    filters : list of str, optional
        Restrict to these filters (NIRCam only). Same fan-out reason.
    """
    obs_list = list(obs_ids) if obs_ids else [None]
    filt_list = list(filters) if filters else [None]

    headers = {"Authorization": f"token {token}"} if token else {}
    select_cols = [
        "fileSetName", "productLevel", "opticalElements",
        "filter", "date_obs", "duration", "observtn", "exposure",
    ]
    if instrument == "NIRCAM":
        select_cols += ["targ_ra", "targ_dec", "s_region"]

    label_extras = []
    if obs_ids:
        label_extras.append(f"obs {','.join(str(o) for o in obs_ids)}")
    if filters:
        label_extras.append(f"filters {','.join(filters)}")
    extra = (" " + ", ".join(label_extras)) if label_extras else ""
    print(f"Searching for {instrument} {exp_type} level 1b filesets in program {program_id}{extra}...")

    seen = set()
    merged = []
    for obs_id in obs_list:
        for filt in filt_list:
            conditions = [
                {"program": str(program_id)},
                {"instrume": instrument},
                {"exp_type": exp_type},
                {"productLevel": "1b"},
            ]
            if obs_id is not None:
                conditions.append({"observtn": str(obs_id)})
            if filt is not None:
                conditions.append({"filter": filt.upper()})

            resp = requests.post(
                f"{BASE_URL}/search",
                json={
                    "conditions": conditions,
                    "select_cols": select_cols,
                    "limit": 5000,
                },
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            for r in data["results"]:
                key = r["fileSetName"]
                if key in seen:
                    continue
                seen.add(key)
                merged.append(r)
            total = data["totalResults"]
            if total > len(data["results"]):
                print(f"  Warning: {total} filesets found but only {len(data['results'])} returned (limit 5000)")

    print(f"Found {len(merged)} filesets")
    return merged


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


def _download_one(file_info, token, pbar, pbar_lock):
    """Download a single file to ``file_info['_path']``, updating the shared bar.

    Returns ('downloaded' | 'error', filename, error_message_or_None).
    """
    filename = file_info["filename"]
    output_path = file_info["_path"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
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


def download_files(files, token=None, workers=4, desc="downloading"):
    """Download a list of files concurrently with a single aggregate progress bar.

    Each file_info must have ``_path`` precomputed (see ``_output_path_for``).

    Parameters
    ----------
    files : list of dict
        Each dict has 'filename', 'uri', 'size', and '_path' (target Path).
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
                ex.submit(_download_one, f, token, pbar, pbar_lock)
                for f in files
            ]
            try:
                for future in concurrent.futures.as_completed(futures):
                    status, name, err = future.result()
                    if status == "error":
                        errors += 1
                        tqdm.write(f"    ERROR {name}: {err}")
            except KeyboardInterrupt:
                ex.shutdown(wait=False, cancel_futures=True)
                raise

    return errors


def _split_existing(files):
    """Partition files into (to_download, to_skip) based on what's on disk.

    Each file_info must have ``_path`` precomputed.
    """
    to_download = []
    to_skip = []
    for f in files:
        path = f["_path"]
        if path.exists() and path.stat().st_size == f["size"]:
            to_skip.append(f)
        else:
            to_download.append(f)
    return to_download, to_skip


def _build_fileset_index(filesets):
    """Map fileSetName → fileset metadata dict for fast joins to products."""
    return {fs["fileSetName"]: fs for fs in filesets}


def _output_path_for(file_info, download_root, instrument):
    """Compute the on-disk path for a downloaded product.

    NIRSpec: ``{download_root}/{PID}/{filename}``  (flat per-PID).
    NIRCam:  ``{download_root}/nircam/{PID}/{filter}/{filename}``.
    """
    filename = file_info["filename"]
    pid = file_info["program_id"]
    if instrument == "NIRCAM":
        filt = (file_info.get("filter") or "unknown").lower()
        return Path(download_root) / "nircam" / pid / filt / filename
    return Path(download_root) / pid / filename


def _write_nircam_manifest(download_root, program_id, rows):
    """Upsert NIRCam manifest rows into ``raw/nircam/{PID}/manifest.ecsv``.

    `rows` is a list of dicts (one per uncal file). Existing rows with the same
    `filename` are replaced; new rows are appended; unrelated rows are kept.
    """
    if not rows:
        return
    from astropy.table import Table, vstack
    manifest_dir = Path(download_root) / "nircam" / program_id
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "manifest.ecsv"

    new_tbl = Table(rows=rows)
    if manifest_path.exists():
        try:
            old_tbl = Table.read(manifest_path, format="ascii.ecsv")
            keep = [name not in set(new_tbl["filename"]) for name in old_tbl["filename"]]
            old_tbl = old_tbl[keep]
            tbl = vstack([old_tbl, new_tbl], join_type="outer")
        except Exception as e:
            print(f"  Warning: could not read existing manifest ({e}); overwriting.")
            tbl = new_tbl
    else:
        tbl = new_tbl

    tbl.sort("filename")
    tbl.write(manifest_path, format="ascii.ecsv", overwrite=True)
    print(f"  Manifest updated: {manifest_path} ({len(tbl)} rows)")


def download_jwst_data(program_id, instrument="NIRSPEC", exp_type="NRS_MSASPEC",
                       download_dir="data", dry_run=False, obs_ids=None,
                       filters=None, token=None, workers=4):
    """Download JWST level 1b data for a program.

    Layout:
      NIRSpec → ``{download_dir}/{PID}/{filename}``
      NIRCam  → ``{download_dir}/nircam/{PID}/{filter}/{filename}`` plus a
                ``manifest.ecsv`` per PID directory.

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
        Base download directory (the ``raw/`` root).
    dry_run : bool
        If True, list files without downloading.
    obs_ids : iterable of int, optional
        Restrict to these observation numbers.
    filters : iterable of str, optional
        Restrict to these filters (NIRCam only).
    token : str or None
        MAST API token for accessing proprietary data.
    workers : int
        Number of parallel download streams (default 4).
    """
    if token:
        print("Using MAST API token for authentication.")

    # Step 1: Search for filesets
    filesets = search_filesets(
        program_id, instrument, exp_type,
        obs_ids=obs_ids, filters=filters, token=token,
    )
    if not filesets:
        print("No filesets found. Exiting.")
        return

    # Step 2: List products
    print()
    products = list_products_batched(filesets, token=token)
    print(f"  {len(products)} total products across {len(filesets)} filesets")

    # Step 3: Filter to uncal + per-instrument aux files
    uncal_files, aux_files = filter_products(products, instrument)

    # Annotate every file with the program_id and (NIRCam) the parent fileset's
    # filter, so the path computation has what it needs.
    pid_str = str(program_id).zfill(5)
    fileset_index = _build_fileset_index(filesets)
    for f in uncal_files + aux_files:
        f["program_id"] = pid_str
    if instrument == "NIRCAM":
        for f in uncal_files:
            # NIRCam uncal filenames look like '{fileSetName}_{detector}_uncal.fits'
            # where fileSetName has no detector token. Walk back tokens until we
            # match an entry in the fileset index.
            stem = f["filename"].rsplit("_uncal.fits", 1)[0]
            fs = {}
            parts = stem.split("_")
            while parts:
                candidate = "_".join(parts)
                if candidate in fileset_index:
                    fs = fileset_index[candidate]
                    break
                parts.pop()
            f["filter"] = (fs.get("filter") or "").lower() or None
            f["_fileset"] = fs

    # Aux first so reduction can start while uncal files stream in.
    all_files = aux_files + uncal_files
    total_size = sum(f["size"] for f in all_files)

    print()
    aux_label = f" and {len(aux_files)} unique MSA files" if aux_files else ""
    print(f"Selected {len(uncal_files)} uncal files{aux_label} ({format_size(total_size)} total)")

    if not all_files:
        print("No matching files found. Exiting.")
        return

    # Annotate every file with its target path, then split into existing-vs-new.
    download_root = Path(download_dir)
    for f in all_files:
        f["_path"] = _output_path_for(f, download_root, instrument)
    aux_to_dl, aux_skip = _split_existing(aux_files)
    uncal_to_dl, uncal_skip = _split_existing(uncal_files)
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
                rel = f["_path"].relative_to(download_root)
                print(f"  {str(rel):70s}  {format_size(f['size']):>10s}")
        else:
            print("Nothing to download — all files already exist.")
        if to_skip:
            print(f"\nAlready downloaded ({len(to_skip)}):")
            for f in to_skip:
                rel = f["_path"].relative_to(download_root)
                print(f"  {str(rel):70s}  {format_size(f['size']):>10s}  (exists)")
        if instrument == "NIRCAM":
            print(f"\nManifest would be written/updated at: "
                  f"{download_root / 'nircam' / pid_str / 'manifest.ecsv'}")
        return

    if not to_download:
        print("\nNothing to download — all files already exist.")
        # Even with nothing new, refresh manifest from current selection so
        # filter/s_region info backfills for previously-downloaded files.
        if instrument == "NIRCAM":
            _write_nircam_manifest(
                download_root, pid_str,
                [_manifest_row(f) for f in uncal_files if f["_path"].exists()],
            )
        return

    dl_size = sum(f["size"] for f in to_download)
    print()
    print(f"Downloading {len(to_download)} files ({format_size(dl_size)}) "
          f"with {workers} parallel stream{'s' if workers != 1 else ''}")

    errors = 0

    # Step 4a: Aux/MSA metafiles first so reduction can start early
    if aux_to_dl:
        print(f"\nFetching {len(aux_to_dl)} metafile(s) first...")
        errors += download_files(
            aux_to_dl, token=token, workers=workers, desc="metafiles",
        )

    # Step 4b: Uncal data files
    if uncal_to_dl:
        print(f"\nFetching {len(uncal_to_dl)} uncal file(s)...")
        errors += download_files(
            uncal_to_dl, token=token, workers=workers, desc="uncal    ",
        )

    print()
    downloaded = len(to_download) - errors
    print(f"Complete: {downloaded} downloaded, {len(to_skip)} skipped, {errors} errors")

    if instrument == "NIRCAM":
        # Manifest covers everything we know about (downloaded + previously present)
        _write_nircam_manifest(
            download_root, pid_str,
            [_manifest_row(f) for f in uncal_files if f["_path"].exists()],
        )


def _manifest_row(f):
    """Build a manifest row dict from a NIRCam uncal file_info."""
    from datetime import datetime, timezone
    fs = f.get("_fileset") or {}
    return {
        "filename":       f["filename"],
        "fileSetName":    fs.get("fileSetName", ""),
        "filter":         (f.get("filter") or "").lower(),
        "observtn":       str(fs.get("observtn", "")),
        "exposure":       str(fs.get("exposure", "")),
        "targ_ra":        float(fs["targ_ra"]) if fs.get("targ_ra") is not None else float("nan"),
        "targ_dec":       float(fs["targ_dec"]) if fs.get("targ_dec") is not None else float("nan"),
        "s_region":       fs.get("s_region", "") or "",
        "date_obs":       fs.get("date_obs", "") or "",
        "duration":       float(fs["duration"]) if fs.get("duration") is not None else float("nan"),
        "size_bytes":     int(f["size"]),
        "downloaded_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


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
