"""
Deployment orchestrator.

Each public function follows the same pattern:
  1. Load ECSV (primary metadata source)
  2. Filter by source IDs if requested
  3. Discover / generate content
  4. Upload to R2
  5. Upsert to Supabase
"""

import shutil
from pathlib import Path

from tqdm import tqdm

from campfire_deploy.config import load_programs, resolve_obs_dir
from campfire_deploy.discover import (
    discover_rgb_images,
    discover_sed_plots,
    discover_slits_json,
    extract_object_ids_from_files,
    filter_files_by_source_ids,
    load_slits_json,
)
from campfire_deploy.generate import (
    generate_spectrum_json,
    generate_thumbnails_from_fits,
    generate_zfit_json,
)
from campfire_deploy.r2 import UploadTask, get_r2_client, upload_files_parallel
from campfire_deploy.supabase import (
    batch_upsert_objects,
    batch_upsert_spectra,
    check_existing_objects,
    deploy_slits as db_deploy_slits,
    get_supabase_client,
    refresh_filter_options,
    update_has_sed_plot,
    upsert_programs,
)
from campfire_deploy.summary import (
    filter_by_source_ids,
    get_field,
    get_spec_paths,
    get_spectra_records,
    get_unique_objects,
    get_unique_program_ids,
    get_zfit_paths,
    load_summary,
)


def deploy_observation(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
    supabase_only: bool = False,
    force_overwrite: bool = False,
    include_rgb: bool = True,
    include_sed: bool = True,
    source_ids: list[int] | None = None,
    auto_approve: bool = False,
) -> None:
    """Full deployment: ECSV -> generate content -> R2 upload -> Supabase upsert."""
    obs_dir = resolve_obs_dir(obs_name)
    summary = load_summary(obs_dir, obs_name)

    if source_ids:
        total = len(summary)
        summary = filter_by_source_ids(summary, source_ids)
        print(f"Filtered {total} spectra to {len(summary)} matching {len(source_ids)} source IDs")

    field = get_field(summary)
    objects = get_unique_objects(summary)
    spectra = get_spectra_records(summary, obs_name)
    program_ids = get_unique_program_ids(summary)
    spec_paths = get_spec_paths(summary, obs_dir)
    zfit_paths = get_zfit_paths(summary, obs_dir)

    print(f"Observation: {obs_name}")
    print(f"  Field: {field}")
    print(f"  Objects: {len(objects)}")
    print(f"  Spectra: {len(spectra)}")
    print(f"  Programs: {program_ids}")
    print(f"  Zfit files: {len(zfit_paths)}")

    # Discover optional file types
    rgb_files = discover_rgb_images(obs_dir) if include_rgb else []
    sed_files = discover_sed_plots(obs_dir) if include_sed else []

    if source_ids:
        rgb_files = filter_files_by_source_ids(rgb_files, source_ids, obs_name)
        sed_files = filter_files_by_source_ids(sed_files, source_ids, obs_name)

    if rgb_files:
        print(f"  RGB images: {len(rgb_files)}")
    if sed_files:
        print(f"  SED plots: {len(sed_files)}")
    print()

    # Build set of object_ids with SED plots
    objects_with_sed = extract_object_ids_from_files(sed_files, '_sed.pdf') if sed_files else set()

    # --- Dry run ---
    if dry_run:
        print("=== DRY RUN ===")
        if force_overwrite:
            print("!! FORCE OVERWRITE: inspection data will be reset")
        if not supabase_only:
            print(f"Would upload to R2:")
            print(f"  {len(spec_paths)} FITS files")
            print(f"  {len(spec_paths)} spectrum JSON files")
            if zfit_paths:
                print(f"  {len(zfit_paths)} zfit JSON files")
            if rgb_files:
                print(f"  {len(rgb_files)} RGB images")
            if sed_files:
                print(f"  {len(sed_files)} SED plots")
        print(f"Would upsert to Supabase:")
        print(f"  {len(program_ids)} program(s)")
        print(f"  {len(objects)} object(s)")
        print(f"  {len(spectra)} spectrum record(s)")
        if not force_overwrite:
            print("  (existing objects: pipeline fields only, inspection data preserved)")
        print()
        print("Sample object IDs:")
        for obj in objects[:5]:
            print(f"  - {obj['object_id']}")
        if len(objects) > 5:
            print(f"  ... and {len(objects) - 5} more")
        return

    # --- Live deployment ---
    programs_config = load_programs()
    sb = get_supabase_client(config)

    # Check for existing objects and confirm
    object_ids = [o['object_id'] for o in objects]
    existing = check_existing_objects(sb, object_ids)
    if existing:
        print(f"Found {len(existing)} existing objects")
        if force_overwrite:
            print("  FORCE OVERWRITE: inspection data will be RESET!")
            if not auto_approve:
                resp = input("  Are you sure? [y/N]: ")
                if resp.lower() != 'y':
                    print("Aborted.")
                    return
        else:
            print("  (inspection data preserved)")
            if not auto_approve:
                resp = input("  Update pipeline data for existing objects? [y/N]: ")
                if resp.lower() != 'y':
                    print("Aborted.")
                    return
    print()

    # Upsert programs
    print("Upserting programs...")
    upsert_programs(sb, program_ids, programs_config)
    print()

    # Generate content and upload
    temp_dir = obs_dir / '.deploy_temp'
    temp_dir.mkdir(exist_ok=True)

    try:
        upload_tasks: list[UploadTask] = []
        r2_prefix = f"spectra/{obs_name}"

        if not supabase_only:
            r2_client = get_r2_client(config)
            bucket = config['r2']['bucket_name']

            print("Generating content...")
            for spec_path in tqdm(spec_paths, desc="Processing", unit="file"):
                # FITS file
                upload_tasks.append(UploadTask(spec_path, f"{r2_prefix}/{spec_path.name}", 'application/fits'))

                # Spectrum JSON
                json_path = generate_spectrum_json(spec_path, temp_dir)
                upload_tasks.append(UploadTask(json_path, f"{r2_prefix}/{json_path.name}", 'application/json'))

            # Zfit JSONs
            for zfit_path in zfit_paths:
                zfit_json = generate_zfit_json(zfit_path, temp_dir)
                upload_tasks.append(UploadTask(zfit_json, f"{r2_prefix}/{zfit_json.name}", 'application/json'))

            # RGB images
            for rgb_path in rgb_files:
                upload_tasks.append(UploadTask(rgb_path, f"rgb/{obs_name}/{rgb_path.name}", 'image/png'))

            # SED plots
            for sed_path in sed_files:
                upload_tasks.append(UploadTask(sed_path, f"sed/{obs_name}/{sed_path.name}", 'application/pdf'))

            print(f"Uploading {len(upload_tasks)} files...")
            success, failed, failed_msgs = upload_files_parallel(r2_client, bucket, upload_tasks, desc="R2 uploads")

            if failed_msgs:
                print(f"\n  {failed} uploads failed:")
                for msg in failed_msgs[:10]:
                    print(f"    - {msg}")
                if len(failed_msgs) > 10:
                    print(f"    ... and {len(failed_msgs) - 10} more")
            print(f"Uploaded {success}/{len(upload_tasks)} files")
            print()

        # Generate thumbnails and enrich spectra records
        print("Generating thumbnails...")
        thumb_map = {}
        for spec_path in tqdm(spec_paths, desc="Thumbnails", unit="file"):
            try:
                thumbs = generate_thumbnails_from_fits(spec_path)
                thumb_map[spec_path.name] = thumbs
            except Exception as e:
                print(f"  Warning: {spec_path.name}: {e}")

        # Enrich spectra records with thumbnails
        for rec in spectra:
            fits_name = rec['fits_path'].split('/')[-1]
            if fits_name in thumb_map:
                rec.update(thumb_map[fits_name])

        # Supabase upserts
        print("Upserting objects...")
        n_obj = batch_upsert_objects(sb, objects, field, force_overwrite, objects_with_sed)
        print(f"  {n_obj} objects")

        print("Upserting spectra...")
        n_spec = batch_upsert_spectra(sb, spectra)
        print(f"  {n_spec} spectra")

        print()
        refresh_filter_options(sb)

        print()
        msg = f"Deployed {len(spectra)} spectra from {len(objects)} objects"
        if zfit_paths:
            msg += f" + {len(zfit_paths)} zfit files"
        if rgb_files:
            msg += f" + {len(rgb_files)} RGB images"
        if sed_files:
            msg += f" + {len(sed_files)} SED plots"
        print(msg)

    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


# ---------------------------------------------------------------------------
# Standalone subcommand handlers
# ---------------------------------------------------------------------------

def deploy_rgb(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
    source_ids: list[int] | None = None,
) -> None:
    """Deploy RGB images to R2."""
    obs_dir = resolve_obs_dir(obs_name)
    rgb_files = discover_rgb_images(obs_dir)

    if source_ids:
        rgb_files = filter_files_by_source_ids(rgb_files, source_ids, obs_name)

    if not rgb_files:
        print("No RGB images found.")
        return

    print(f"Found {len(rgb_files)} RGB images")

    if dry_run:
        print("=== DRY RUN ===")
        for path in rgb_files[:5]:
            print(f"  {path.name} -> rgb/{obs_name}/{path.name}")
        if len(rgb_files) > 5:
            print(f"  ... and {len(rgb_files) - 5} more")
        return

    r2_client = get_r2_client(config)
    bucket = config['r2']['bucket_name']

    tasks = [UploadTask(p, f"rgb/{obs_name}/{p.name}", 'image/png') for p in rgb_files]
    success, failed, failed_msgs = upload_files_parallel(r2_client, bucket, tasks, desc="RGB images")

    if failed_msgs:
        print(f"\n  {failed} failed:")
        for msg in failed_msgs[:5]:
            print(f"    - {msg}")

    print(f"Uploaded {success}/{len(rgb_files)} RGB images")


def deploy_sed(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
    source_ids: list[int] | None = None,
) -> None:
    """Deploy SED plots to R2 and update has_sed_plot in Supabase."""
    obs_dir = resolve_obs_dir(obs_name)
    sed_files = discover_sed_plots(obs_dir)

    if source_ids:
        sed_files = filter_files_by_source_ids(sed_files, source_ids, obs_name)

    if not sed_files:
        print("No SED plots found.")
        return

    objects_with_sed = extract_object_ids_from_files(sed_files, '_sed.pdf')
    print(f"Found {len(sed_files)} SED plots ({len(objects_with_sed)} objects)")

    if dry_run:
        print("=== DRY RUN ===")
        for path in sed_files[:5]:
            print(f"  {path.name} -> sed/{obs_name}/{path.name}")
        if len(sed_files) > 5:
            print(f"  ... and {len(sed_files) - 5} more")
        print(f"Would set has_sed_plot=true for {len(objects_with_sed)} objects")
        return

    r2_client = get_r2_client(config)
    bucket = config['r2']['bucket_name']

    tasks = [UploadTask(p, f"sed/{obs_name}/{p.name}", 'application/pdf') for p in sed_files]
    success, failed, failed_msgs = upload_files_parallel(r2_client, bucket, tasks, desc="SED plots")

    if failed_msgs:
        print(f"\n  {failed} failed:")
        for msg in failed_msgs[:5]:
            print(f"    - {msg}")

    print(f"Uploaded {success}/{len(sed_files)} SED plots")

    # Update database
    sb = get_supabase_client(config)
    n = update_has_sed_plot(sb, objects_with_sed)
    print(f"Updated has_sed_plot for {n} objects")


def deploy_json(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
    source_ids: list[int] | None = None,
) -> None:
    """Regenerate and upload spectrum JSON files."""
    obs_dir = resolve_obs_dir(obs_name)
    summary = load_summary(obs_dir, obs_name)

    if source_ids:
        summary = filter_by_source_ids(summary, source_ids)

    spec_paths = get_spec_paths(summary, obs_dir)

    if not spec_paths:
        print("No spectrum files found.")
        return

    print(f"Found {len(spec_paths)} spectrum files")

    if dry_run:
        print("=== DRY RUN ===")
        for path in spec_paths[:5]:
            print(f"  {path.name} -> spectra/{obs_name}/{path.stem}.json")
        if len(spec_paths) > 5:
            print(f"  ... and {len(spec_paths) - 5} more")
        return

    r2_client = get_r2_client(config)
    bucket = config['r2']['bucket_name']

    temp_dir = obs_dir / '.deploy_temp'
    temp_dir.mkdir(exist_ok=True)

    try:
        print("Generating JSON files...")
        tasks = []
        for path in tqdm(spec_paths, desc="Generating", unit="file"):
            json_path = generate_spectrum_json(path, temp_dir)
            tasks.append(UploadTask(json_path, f"spectra/{obs_name}/{json_path.name}", 'application/json'))

        print("Uploading...")
        success, failed, failed_msgs = upload_files_parallel(r2_client, bucket, tasks, desc="JSON files")

        if failed_msgs:
            print(f"\n  {failed} failed:")
            for msg in failed_msgs[:5]:
                print(f"    - {msg}")

        print(f"Uploaded {success}/{len(spec_paths)} JSON files")
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


def deploy_zfit(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
    force_overwrite: bool = False,
    source_ids: list[int] | None = None,
    auto_approve: bool = False,
) -> None:
    """Deploy zfit JSON files and update redshift_auto."""
    obs_dir = resolve_obs_dir(obs_name)
    summary = load_summary(obs_dir, obs_name)

    if source_ids:
        summary = filter_by_source_ids(summary, source_ids)

    zfit_paths = get_zfit_paths(summary, obs_dir)
    objects = get_unique_objects(summary)
    field = get_field(summary)

    print(f"Found {len(zfit_paths)} zfit files for {len(objects)} objects")

    if not zfit_paths:
        print("No zfit files to deploy.")
        return

    if dry_run:
        print("=== DRY RUN ===")
        if force_overwrite:
            print("!! FORCE OVERWRITE: inspection data will be reset")
        print(f"Would upload {len(zfit_paths)} zfit JSON files")
        print(f"Would update redshift_auto for {len(objects)} objects")
        return

    # Upload zfit JSONs
    r2_client = get_r2_client(config)
    bucket = config['r2']['bucket_name']

    temp_dir = obs_dir / '.deploy_temp'
    temp_dir.mkdir(exist_ok=True)

    try:
        print("Generating zfit JSON files...")
        tasks = []
        for path in zfit_paths:
            json_path = generate_zfit_json(path, temp_dir)
            tasks.append(UploadTask(json_path, f"spectra/{obs_name}/{json_path.name}", 'application/json'))

        print("Uploading...")
        success, failed, failed_msgs = upload_files_parallel(r2_client, bucket, tasks, desc="Zfit JSON")

        if failed_msgs:
            print(f"\n  {failed} failed:")
            for msg in failed_msgs[:5]:
                print(f"    - {msg}")

        print(f"Uploaded {success}/{len(zfit_paths)} zfit JSON files")
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

    # Update redshift_auto in Supabase
    sb = get_supabase_client(config)

    if force_overwrite:
        existing = check_existing_objects(sb, [o['object_id'] for o in objects])
        if existing and not auto_approve:
            print(f"  {len(existing)} objects exist. FORCE OVERWRITE will reset inspection data!")
            resp = input("  Are you sure? [y/N]: ")
            if resp.lower() != 'y':
                print("Aborted.")
                return

    print("Updating objects...")
    n = batch_upsert_objects(sb, objects, field, force_overwrite)
    print(f"  Updated {n} objects")

    refresh_filter_options(sb)
    print(f"Deployed {len(zfit_paths)} zfit files, updated {n} objects")


def deploy_thumbnails(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
    source_ids: list[int] | None = None,
) -> None:
    """Regenerate spectrum thumbnail SVGs and update Supabase."""
    obs_dir = resolve_obs_dir(obs_name)
    summary = load_summary(obs_dir, obs_name)

    if source_ids:
        summary = filter_by_source_ids(summary, source_ids)

    spec_paths = get_spec_paths(summary, obs_dir)

    if not spec_paths:
        print("No spectrum files found.")
        return

    print(f"Found {len(spec_paths)} spectrum files")

    if dry_run:
        print("=== DRY RUN ===")
        print(f"Would regenerate thumbnails for {len(spec_paths)} spectra")
        return

    sb = get_supabase_client(config)

    print("Generating and updating thumbnails...")
    updated = 0
    errors = []

    for spec_path in tqdm(spec_paths, desc="Processing", unit="file"):
        try:
            thumbs = generate_thumbnails_from_fits(spec_path)

            # Extract object_id and grating from the ECSV-referenced filename
            # Filename pattern: {obs_name}_{grating}_{filter}_{source_id}_spec.fits
            stem = spec_path.stem  # e.g. ember_uds_p4_prism_clear_12345_spec
            # Find the corresponding row in summary
            for row in summary:
                if row['spec_file'] == spec_path.name:
                    sb.table('spectra').update(thumbs).eq(
                        'object_id', row['object_id']
                    ).eq('grating', row['grating']).execute()
                    updated += 1
                    break
        except Exception as e:
            errors.append(f"{spec_path.name}: {e}")

    if errors:
        print(f"\n  {len(errors)} errors:")
        for msg in errors[:5]:
            print(f"    - {msg}")
        if len(errors) > 5:
            print(f"    ... and {len(errors) - 5} more")

    print(f"Updated thumbnails for {updated}/{len(spec_paths)} spectra")


def deploy_slits(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
) -> None:
    """Deploy slit geometry data to Supabase."""
    obs_dir = resolve_obs_dir(obs_name)
    slits_path = discover_slits_json(obs_dir, obs_name)

    if not slits_path:
        print(f"Error: Slit file not found: {obs_dir / f'{obs_name}_slits.json'}")
        print(f"Generate it first with: cfpipe nirspec slits --obs {obs_name}")
        return

    slits_data = load_slits_json(slits_path)
    print(f"Found {len(slits_data)} slit records")

    if dry_run:
        print("=== DRY RUN ===")
        print(f"Would delete existing slit_regions for '{obs_name}'")
        print(f"Would insert {len(slits_data)} rows")
        return

    sb = get_supabase_client(config)

    print("Deploying slits...")
    n = db_deploy_slits(sb, obs_name, slits_data)
    print(f"Deployed {n} slit records for {obs_name}")
