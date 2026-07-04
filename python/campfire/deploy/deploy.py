"""
Deployment orchestrator.

Each public function follows the same pattern:
  1. Load ECSV (primary metadata source)
  2. Filter by source IDs if requested
  3. Discover / generate content
  4. Upload to R2
  5. Upsert to Supabase
"""

import re
import shutil
import sys
from pathlib import Path

from tqdm import tqdm

from campfire.deploy.config import load_observations, load_programs, resolve_field, resolve_imaging_config, resolve_obs_dir, resolve_reference_obs_dir, validate_program_slug
from campfire.deploy.discover import (
    discover_pointings_ecsv,
    discover_rgb_images,
    discover_sed_plots,
    discover_rate_files,
    discover_shutters_ecsv,
    discover_spectrum_exposures,
    discover_slits_json,
    extract_object_ids_from_files,
    filter_files_by_source_ids,
    load_pointings_ecsv,
    load_shutters_ecsv,
    load_slits_json,
)
from campfire.deploy.generate import (
    generate_spectrum_json,
    generate_thumbnails_from_fits,
    generate_zfit_json,
)
from campfire_layout import KeyScheme, Scope, storage_key
from campfire.deploy.r2 import UploadTask, upload_files_parallel
from campfire.deploy.supabase import (
    batch_upsert_objects,
    batch_upsert_spectra,
    check_existing_objects,
    deploy_event_metadata,
    deploy_shutters as db_deploy_shutters,
    deploy_slits as db_deploy_slits,
    fetch_deployment_config,
    claim_deploy_scope,
    get_deploy_scope_version,
    get_lifecycle_status,
    get_supabase_client,
    get_user_id_from_token,
    insert_deployment,
    log_deploy_event,
    recompute_target_aggregates,
    refresh_filter_options,
    refresh_programs_overview,
    update_has_sed_plot,
    update_latest_deployment,
    update_observation_pointings,
    upsert_observation,
    upsert_programs,
)
from campfire.deploy.summary import (
    filter_by_source_ids,
    get_field,
    get_program_slug,
    get_spec_paths,
    get_spectra_records,
    get_unique_objects,
    get_zfit_paths,
    load_summary,
)


def _load_config_snapshot(obs_dir: Path, obs_name: str) -> dict | None:
    """Load the effective config TOML written by the pipeline, if it exists."""
    config_path = obs_dir / f"{obs_name}_config.toml"
    if not config_path.exists():
        return None
    try:
        import tomllib
        with open(config_path, 'rb') as f:
            return tomllib.load(f)
    except Exception:
        return None


def _load_stuck_shutters(reference_obs_dir: Path) -> dict | None:
    """Load the stuck closed shutters TOML, if it exists.

    Issue #212 (PR-4): the reducer-decision TOML moved to
    ``reference/nirspec/<obs>/stuck_closed_shutters.toml`` (was
    ``products/<obs>/_<obs>_stuck_closed_shutters.toml``).
    """
    shutters_path = reference_obs_dir / "stuck_closed_shutters.toml"
    if not shutters_path.exists():
        return None
    try:
        import tomllib
        with open(shutters_path, 'rb') as f:
            return tomllib.load(f)
    except Exception:
        return None


_RELEASE_VERSION_RE = re.compile(r'^\d+\.\d+\.\d+$')


def _is_release_version(version: str | None) -> bool:
    """Return True iff *version* is a clean PEP 440 release like '0.4.0'.

    Anything else — ``.dev`` builds, ``+local`` segments, free-form override
    strings, or raw git SHAs from the legacy reduction_version scheme — is
    considered non-release and triggers the warn-and-confirm path in
    ``deploy_observation``.
    """
    return bool(version) and bool(_RELEASE_VERSION_RE.match(version))


def _collect_non_release_versions(summary, spectra) -> list[str]:
    """Return the unique non-release version strings present in *summary* or
    *spectra*. Inspects both ``summary.meta['cfpipe_version']`` and each
    spectrum's ``cfpipe_version`` (sourced verbatim from the FITS ``CMPFRVER``
    header), since heterogeneous reductions may carry different strings
    per row.
    """
    versions: set[str] = set()
    meta_v = summary.meta.get('cfpipe_version')
    if meta_v:
        versions.add(meta_v)
    for s in spectra:
        v = s.get('cfpipe_version')
        if v:
            versions.add(v)
    return sorted(v for v in versions if not _is_release_version(v))


def _collect_crds_contexts(spectra) -> list[str]:
    """Return the distinct CRDS contexts across *spectra* (deploy records).

    More than one means this single observation mixes calibration contexts —
    a silent intra-sample drift that can quietly corrupt a stacked measurement,
    so deploy surfaces it for a conscious decision.
    """
    contexts = {s.get('crds_context') for s in spectra if s.get('crds_context')}
    return sorted(contexts)


def _count_published_spectra(sb, spectra: list[dict]) -> int:
    """How many of these (target_id, grating) spectra are currently 'published'.

    Used by the `--in-prep` guard so a re-draft never silently hides live data.
    PostgREST can't filter on tuples, so over-fetch by target_id and filter the
    (target_id, grating) pairs Python-side (gratings per target are few).
    """
    wanted = {(s['target_id'], s['grating']) for s in spectra}
    target_ids = sorted({tid for (tid, _) in wanted})
    published = 0
    for i in range(0, len(target_ids), 200):
        batch = target_ids[i:i + 200]
        resp = (sb.table('spectra')
                .select('target_id, grating, deploy_status')
                .in_('target_id', batch)
                .eq('deploy_status', 'published')
                .execute())
        for row in resp.data or []:
            if (row['target_id'], row['grating']) in wanted:
                published += 1
    return published


def _filter_exposures_by_source_ids(files: list, source_ids: list[int]) -> list:
    """Keep only canonical exposures whose trailing ``_<source_id>.fits`` is selected."""
    allowed = {str(s) for s in source_ids}
    out = []
    for p in files:
        stem = p.name[:-len('.fits')] if p.name.endswith('.fits') else p.name
        if stem.rsplit('_', 1)[-1] in allowed:
            out.append(p)
    return out


def _jwst_pid_from_obs_cfg(obs_cfg: dict) -> int:
    """Best-effort JWST program id for an intermediates-only deploy (no summary).

    Prefer the numeric ``data_subdir``; else parse ``jw<5 digits>`` from the files
    glob; else 0 (the observations upsert tolerates it; a later full deploy from the
    summary corrects it)."""
    ds = str(obs_cfg.get('data_subdir', '')).strip()
    if ds.isdigit():
        return int(ds)
    files = obs_cfg.get('files', '')
    files = files if isinstance(files, str) else (files[0] if files else '')
    m = re.search(r'jw(\d{5})', str(files))
    return int(m.group(1)) if m else 0


def _read_exposure_provenance(exposure_files):
    """Best-effort ``(cfpipe_version, jwst_version, crds_context)`` from the first
    spectrum-exposure header (Phase 3).

    Reads whatever provenance cards the reduction stamped
    (``CMPFRVER`` / ``CAL_VER`` / ``CRDS_CTX``); any absent card is None. Purely
    additive over the old ``cfpipe_version=None`` on the intermediates-draft
    path — a missing/unreadable header just yields all-None.
    """
    if not exposure_files:
        return None, None, None
    try:
        from astropy.io import fits
        with fits.open(exposure_files[0], memmap=False) as hdul:
            hdr = hdul[0].header
            return hdr.get('CMPFRVER'), hdr.get('CAL_VER'), hdr.get('CRDS_CTX')
    except Exception:
        return None, None, None


def _deploy_intermediates_only(
    obs_name: str, obs_dir, config: dict, *,
    dry_run: bool = False, auto_approve: bool = False,
    source_ids: list[int] | None = None,
) -> dict:
    """Deploy canonical spectrum-exposure intermediates for an observation with no
    stage3 finals yet (epic #210, B5) — the "reduced through stage2, review before
    stage3" flow. Uploads + registers the intermediates and records an auto-DRAFT
    deployment. There is no science to publish; finishing stage3 + a normal deploy
    publishes it. Scope (field/program/pid) comes from observations.toml since there
    is no summary to read it from.
    """
    exposure_files = discover_spectrum_exposures(obs_dir)
    if source_ids:
        total = len(exposure_files)
        exposure_files = _filter_exposures_by_source_ids(exposure_files, source_ids)
        print(f"Filtered {total} spectrum-exposures to {len(exposure_files)} "
              f"matching {len(source_ids)} source IDs")
    # Detector rate files (P1, design §3.1) — source-INDEPENDENT, never source-filtered.
    # The rate deploy runs after stage 1 / before stage 2, precisely when no
    # spectrum-exposures exist yet, so this path must handle rate-only deploys (see
    # the guard below).
    rate_files = discover_rate_files(obs_dir)
    obs_cfg = load_observations().get(obs_name, {})
    field = obs_cfg.get('field', '')
    program_slug = obs_cfg.get('program', '')
    programs_config = load_programs()
    if program_slug:
        validate_program_slug(program_slug, programs_config, obs_name)

    # The intermediates deployment row FKs observations(name), whose own
    # program_slug is NOT NULL with an FK to programs(slug). Without a program we
    # cannot create that FK target, so insert_deployment would crash *after* the R2
    # uploads and registry writes have already happened — orphaning cloud objects
    # with no deployment record. Fail fast before doing any work (#237).
    if not program_slug:
        print(f"ERROR: observation '{obs_name}' has no program defined in "
              f"observations.toml. A program is required to record the deployment "
              f"(observations.program_slug → programs.slug). Add a 'program' entry "
              f"for this observation and retry.")
        sys.exit(1)

    print(f"Observation: {obs_name}  (intermediates-only — no stage3 finals yet)")
    print(f"  Field: {field}")
    print(f"  Program: {program_slug}")
    print(f"  Canonical spectrum-exposures: {len(exposure_files)}")
    print(f"  Detector rate files: {len(rate_files)}")

    if not exposure_files and not rate_files:
        print("No canonical spectrum-exposures, rate files, or finals — nothing to deploy.")
        return {'field': field, 'needs_reconcile': False}

    if dry_run:
        print("\n=== DRY RUN (intermediates-only → auto-draft) ===")
        print(f"Would upload {len(exposure_files)} canonical spectrum-exposures "
              f"and {len(rate_files)} detector rate files, and record a draft "
              f"deployment for {obs_name}.")
        return {'field': field, 'needs_reconcile': False}

    sb = get_supabase_client(config)

    # A draft deploy requires the B1 lifecycle on the target DB (same gate as --draft).
    cap = get_lifecycle_status(sb)
    if not cap.get('enabled'):
        print("ERROR: deploying intermediates as a draft requires the B1 lifecycle "
              "enforcement (#217) on the target database, but it is not enabled.")
        if cap.get('error'):
            print(f"  {cap['error']}")
        sys.exit(1)

    user_id = get_user_id_from_token(config)
    jwst_program_id = _jwst_pid_from_obs_cfg(obs_cfg)

    # The deployment row FKs observations(name); ensure program + observation exist.
    # program_slug is guaranteed non-empty by the fail-fast guard above (#237).
    print("Upserting program + observation...")
    upsert_programs(sb, [program_slug], programs_config)
    file_globs_raw = obs_cfg.get('files', [])
    file_globs = [file_globs_raw] if isinstance(file_globs_raw, str) else list(file_globs_raw)
    upsert_observation(
        sb, obs_name, program_slug, jwst_program_id, field,
        file_globs=file_globs or None,
        gratings=obs_cfg.get('gratings') or None,
        data_subdir=obs_cfg.get('data_subdir'),
    )

    scope = Scope(obs=obs_name)
    scope_version_at_start = get_deploy_scope_version(sb, 'observation', obs_name)
    upload_tasks = [
        UploadTask(p, storage_key('nirspec_spectrum_exposure', scope, p.name,
                                  scheme=KeyScheme.CANONICAL), 'application/fits')
        for p in exposure_files
    ] + [
        UploadTask(p, storage_key('nirspec_rate', scope, p.name,
                                  scheme=KeyScheme.CANONICAL), 'application/fits')
        for p in rate_files
    ]
    uploaded_keys: set[str] = set()
    print(f"Uploading {len(upload_tasks)} intermediates "
          f"({len(exposure_files)} spectrum-exposures + {len(rate_files)} rate files) to OSN...")
    success, failed, failed_msgs = upload_files_parallel(
        config, upload_tasks, desc="OSN uploads", succeeded_out=uploaded_keys,
        backend='osn')
    print(f"Uploaded {success}/{len(upload_tasks)} files")

    # Best-effort provenance from the first exposure header (Phase 3): the
    # intermediates carry whatever cards the reduction stamped. Absent → NULL.
    # For a rate-only deploy (stage 1 done, stage 2 not yet) there are no
    # spectrum-exposures, so fall back to a rate header for provenance.
    cfpipe_version, jwst_version, crds_context = _read_exposure_provenance(
        exposure_files or rate_files)

    deployment_id = insert_deployment(
        sb, observation=obs_name, deployed_by=user_id, status='draft',
        cfpipe_version=cfpipe_version, jwst_version=jwst_version,
        crds_context=crds_context, n_targets=0, n_spectra=0,
    )
    if deployment_id:
        update_latest_deployment(sb, obs_name, deployment_id)
        print(f"\nDeployment #{deployment_id} recorded (draft — intermediates only)")
        log_deploy_event(
            sb, action='upload', actor=user_id, deployment_id=deployment_id,
            observation=obs_name, affected_count=success,
            metadata=deploy_event_metadata(
                'nirspec', observation=obs_name, planned=len(upload_tasks),
                succeeded=success, failed=failed, items=len(upload_tasks),
                draft=True, intermediates_only=True))

    if uploaded_keys:
        from campfire.deploy.registry import (
            build_registry_rows, upsert_storage_objects,
        )
        # NIRSpec products are homed on OSN under canonical keys (epic #210 / #216).
        reg_rows = build_registry_rows(
            upload_tasks, backend='osn',
            deployment_id=deployment_id, uploaded_by=user_id,
            cfpipe_version=cfpipe_version, succeeded_keys=uploaded_keys)
        n_reg = upsert_storage_objects(sb, reg_rows)
        print(f"Registered {n_reg} storage objects")

    claim = claim_deploy_scope(sb, 'observation', obs_name, scope_version_at_start, actor=user_id)
    if claim.get('conflict'):
        print(f"⚠  CONCURRENT DEPLOY DETECTED for {obs_name}: another deploy advanced "
              f"this observation while this one was running.")

    print(f"\nDeployed {len(exposure_files)} canonical spectrum-exposures "
          f"+ {len(rate_files)} detector rate files (draft — no finals yet). "
          f"Finish stage3 + re-deploy to publish.")
    return {'field': field, 'needs_reconcile': False}


def deploy_observation(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
    supabase_only: bool = False,
    force_overwrite: bool = False,
    include_rgb: bool = False,
    include_sed: bool = True,
    include_shutters: bool = True,
    include_photometry: bool = True,
    skip_astrometry: bool = False,
    source_ids: list[int] | None = None,
    auto_approve: bool = False,
    defer_rebuild: bool = False,
    draft: bool = False,
) -> dict:
    """Full deployment: ECSV -> generate content -> R2 upload -> Supabase upsert.

    When *defer_rebuild* is True, the per-field objects reconciliation
    (and materialized-view refresh) is skipped so the caller can batch
    them after processing multiple observations of the same field.

    When *draft* is True (epic #210, B2), the deployed spectra land
    ``deploy_status='draft'`` (admin-only, invisible to users) and the
    deployment record is marked ``draft`` — a draft an admin reviews and
    publishes via the admin UI. Requires the B1 (#217) lifecycle to be applied
    to the target database; checked up front via ``get_lifecycle_status`` so the
    deploy aborts cleanly rather than silently shipping unguarded drafts.

    Returns a dict with ``field`` and ``needs_reconcile`` (always True
    for completed deploys; the no-op return at the dry-run / nothing-
    to-do branch above sets it False).
    """
    obs_dir = resolve_obs_dir(obs_name)
    summary = load_summary(obs_dir, obs_name, required=False)

    # Intermediates-only deploy (epic #210, B5): reduced through stage2 (canonical
    # spectrum-exposures exist) but no stage3 finals/summary yet. Upload + register
    # the intermediates and record an auto-draft deployment for portal review, then
    # return — the finals path below is summary-driven. An incomplete deployment is
    # always a draft.
    if summary is None or len(summary) == 0:
        return _deploy_intermediates_only(
            obs_name, obs_dir, config, dry_run=dry_run, auto_approve=auto_approve,
            source_ids=source_ids)

    if source_ids:
        total = len(summary)
        summary = filter_by_source_ids(summary, source_ids)
        print(f"Filtered {total} spectra to {len(summary)} matching {len(source_ids)} source IDs")

    field = get_field(summary)
    program_slug = get_program_slug(summary)

    # Validate the program slug resolves to a known program before doing any
    # work (including dry runs). A slug/name mix-up in observations.toml is
    # otherwise baked into the ECSV and only surfaces downstream as a cryptic
    # Supabase RLS error.
    programs_config = load_programs()
    validate_program_slug(program_slug, programs_config, obs_name)

    objects = get_unique_objects(summary)
    spectra = get_spectra_records(summary, obs_name)
    spec_paths = get_spec_paths(summary, obs_dir)
    zfit_paths = get_zfit_paths(summary, obs_dir)

    # Get JWST PID from first row (all rows share the same PID per observation)
    jwst_program_id = int(summary['program_id'][0]) if len(summary) > 0 else 0

    print(f"Observation: {obs_name}")
    print(f"  Field: {field}")
    print(f"  Program: {program_slug}")
    print(f"  Objects: {len(objects)}")
    print(f"  Spectra: {len(spectra)}")
    print(f"  Zfit files: {len(zfit_paths)}")

    # Warn if any spectrum was reduced with a non-release pipeline version.
    # The dev/override string is preserved verbatim in spectra.cfpipe_version
    # for downstream traceability — this prompt exists so deployers consciously
    # choose to ship unreleased data, not to block it.
    non_release_versions = _collect_non_release_versions(summary, spectra)
    if non_release_versions:
        print()
        print("WARNING: non-release pipeline version detected")
        print("  cfpipe_version strings present in this deployment:")
        for v in non_release_versions:
            print(f"    - {v}")
        print("  These will be preserved verbatim in spectra.cfpipe_version.")
        print("  Prefer deploying from a tagged release (see /pipeline-release).")
        if not dry_run and not auto_approve:
            resp = input("  Continue? [y/N]: ")
            if resp.lower() != 'y':
                print("Aborted.")
                return {'field': field, 'needs_reconcile': False}

    # Warn if this observation mixes CRDS contexts. A single observation should
    # share one calibration context; >1 means part of the sample was reduced
    # against a different CRDS pmap, which can silently bias a stacked measurement.
    crds_contexts = _collect_crds_contexts(spectra)
    if len(crds_contexts) > 1:
        print()
        print("WARNING: this observation mixes CRDS contexts")
        for c in crds_contexts:
            print(f"    - {c}")
        print("  Spectra reduced against different CRDS pmaps are not calibration-")
        print("  homogeneous; prefer re-reducing the whole observation against one.")
        if not dry_run and not auto_approve:
            resp = input("  Continue? [y/N]: ")
            if resp.lower() != 'y':
                print("Aborted.")
                return {'field': field, 'needs_reconcile': False}

    # Generate RGB/SED if requested (skips existing files)
    if not dry_run:
        if include_rgb:
            from campfire.deploy.generate_rgb import generate_rgb_images
            n = generate_rgb_images(obs_name, obs_dir, field,
                                    source_ids=source_ids)
            if n:
                print(f"  Generated {n} RGB images")
        if include_sed:
            from campfire.deploy.generate_sed import generate_sed_plots
            n = generate_sed_plots(obs_name, obs_dir, field,
                                   source_ids=source_ids)
            if n:
                print(f"  Generated {n} SED plots")

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
            print(f"Would upload to OSN (NIRSpec, canonical keys):")
            print(f"  {len(spec_paths)} FITS files")
            print(f"  {len(spec_paths)} spectrum JSON files")
            if zfit_paths:
                print(f"  {len(zfit_paths)} zfit JSON files")
            if rgb_files or sed_files:
                print(f"Would upload to R2 (dead rgb/sed, legacy keys):")
                if rgb_files:
                    print(f"  {len(rgb_files)} RGB images")
                if sed_files:
                    print(f"  {len(sed_files)} SED plots")
        print(f"Would upsert to Supabase:")
        print(f"  Program: {program_slug}")
        print(f"  {len(objects)} object(s)")
        print(f"  {len(spectra)} spectrum record(s)")
        if include_shutters:
            ecsv_path = discover_shutters_ecsv(obs_dir, obs_name)
            if ecsv_path:
                shutters_data = load_shutters_ecsv(ecsv_path)
                print(f"  {len(shutters_data)} shutter record(s)")
            else:
                print("  No shutters ECSV found, would skip")
        else:
            print("  Shutters: skipped (--no-shutters)")
        pointings_path = discover_pointings_ecsv(obs_dir, obs_name)
        if pointings_path:
            pointings_data = load_pointings_ecsv(pointings_path)
            print(f"  {len(pointings_data)} pointing(s)")
        else:
            print("  No pointings ECSV found, would skip")
        if include_photometry:
            from campfire.deploy.config import resolve_photometry_config
            phot_path = resolve_photometry_config(None)
            if phot_path is not None:
                print(f"  Photometry: would deploy for changed objects after reconcile (count TBD)")
            else:
                print("  Photometry: no photometry.toml found, would skip")
        else:
            print("  Photometry: skipped (--no-photometry)")
        if not force_overwrite:
            print("  (existing objects: pipeline fields only, inspection data preserved)")
        print()
        print("Sample object IDs:")
        for obj in objects[:5]:
            print(f"  - {obj['object_id']}")
        if len(objects) > 5:
            print(f"  ... and {len(objects) - 5} more")
        return {'field': field, 'needs_reconcile': False}

    # --- Live deployment ---
    sb = get_supabase_client(config)

    # B2 capability gate: `--in-prep` requires the B1 (#217) lifecycle to be live
    # on the TARGET database, or the drafts would not actually be hidden. Check the
    # marker up front and abort cleanly if absent (or if the RPC itself is missing,
    # i.e. this deploy code is newer than the DB).
    if draft:
        cap = get_lifecycle_status(sb)
        if not cap.get('enabled'):
            print("ERROR: --draft requires the B1 lifecycle enforcement (#217) on "
                  "the target database, but it is not enabled.")
            if cap.get('checks'):
                print(f"  capability checks: {cap['checks']}")
            if cap.get('error'):
                print(f"  {cap['error']}")
            print("  Deploy the B1 migration first, or omit --draft to publish directly.")
            sys.exit(1)
        print("Deploying as draft (admin-only until published).")

    # B4 multi-reducer safety: snapshot this observation's deploy-scope version
    # now; we compare-and-set it at finalize to detect a concurrent deploy of the
    # same observation (advisory — never blocks).
    scope_version_at_start = get_deploy_scope_version(sb, 'observation', obs_name)

    # Check for existing targets and confirm
    target_ids = [o['object_id'] for o in objects]
    existing = check_existing_objects(sb, target_ids)
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

    # Upsert program
    print("Upserting program...")
    upsert_programs(sb, [program_slug], programs_config)

    # Upsert observation record with definition from observations.toml
    obs_config = load_observations().get(obs_name, {})
    file_globs_raw = obs_config.get('files', [])
    file_globs = [file_globs_raw] if isinstance(file_globs_raw, str) else list(file_globs_raw)
    obs_gratings = obs_config.get('gratings', [])
    obs_data_subdir = obs_config.get('data_subdir')
    upsert_observation(
        sb, obs_name, program_slug, jwst_program_id, field,
        file_globs=file_globs if file_globs else None,
        gratings=obs_gratings if obs_gratings else None,
        data_subdir=obs_data_subdir,
    )
    print()

    # Generate content and upload
    temp_dir = obs_dir / '.deploy_temp'
    temp_dir.mkdir(exist_ok=True)

    try:
        upload_tasks: list[UploadTask] = []
        uploaded_keys: set[str] = set()  # r2_keys that actually landed (for the registry)
        scope = Scope(obs=obs_name)

        # NIRSpec science products are homed on OSN under CANONICAL keys (epic #210 /
        # #216 — deploy → OSN); rgb/sed are dead products that stay on R2 under legacy
        # keys (unregistered, 404, not migrated). They upload in separate batches.
        legacy_tasks: list[UploadTask] = []
        if not supabase_only:
            print("Generating content...")
            for spec_path in tqdm(spec_paths, desc="Processing", unit="file"):
                # FITS file
                upload_tasks.append(UploadTask(spec_path, storage_key('nirspec_spec', scope, spec_path.name, scheme=KeyScheme.CANONICAL), 'application/fits'))

                # Spectrum JSON
                json_path = generate_spectrum_json(spec_path, temp_dir)
                upload_tasks.append(UploadTask(json_path, storage_key('spectrum_json', scope, json_path.name, scheme=KeyScheme.CANONICAL), 'application/json'))

            # Zfit JSONs
            for zfit_path in zfit_paths:
                zfit_json = generate_zfit_json(zfit_path, temp_dir)
                upload_tasks.append(UploadTask(zfit_json, storage_key('zfit', scope, zfit_json.name, scheme=KeyScheme.CANONICAL), 'application/json'))

            # RGB images / SED plots — dead products (kept on R2/legacy, unchanged).
            for rgb_path in rgb_files:
                legacy_tasks.append(UploadTask(rgb_path, storage_key('rgb', scope, rgb_path.name), 'image/png'))
            for sed_path in sed_files:
                legacy_tasks.append(UploadTask(sed_path, storage_key('sed', scope, sed_path.name), 'application/pdf'))

            # Canonical spectrum-exposure intermediates (epic #210, B5): uploaded on
            # EVERY deploy (cloud-as-source-of-truth + delete-local→restore), filtered
            # to the deployed source_ids when --source-ids is set. Registered as
            # product_type='nirspec_spectrum_exposure' with a per-exposure exposure_ref.
            exposure_files = discover_spectrum_exposures(obs_dir)
            if source_ids:
                exposure_files = _filter_exposures_by_source_ids(exposure_files, source_ids)
            for exp_path in exposure_files:
                upload_tasks.append(UploadTask(
                    exp_path, storage_key('nirspec_spectrum_exposure', scope, exp_path.name, scheme=KeyScheme.CANONICAL),
                    'application/fits'))
            if exposure_files:
                print(f"  + {len(exposure_files)} canonical spectrum-exposure intermediates")

            # Detector rate-file intermediates (P1, design §3.1). SOURCE-INDEPENDENT:
            # uploaded on every deploy regardless of --source-ids (rate-level masks are
            # per-detector, not per-source), so we deliberately do NOT run these
            # through _filter_exposures_by_source_ids. Registered
            # product_type='nirspec_rate' with a per-(exposure,detector) exposure_ref;
            # uploaded uncompressed (the web SCI decoder rejects fpack ZIMAGE).
            rate_files = discover_rate_files(obs_dir)
            for rate_path in rate_files:
                upload_tasks.append(UploadTask(
                    rate_path, storage_key('nirspec_rate', scope, rate_path.name, scheme=KeyScheme.CANONICAL),
                    'application/fits'))
            if rate_files:
                print(f"  + {len(rate_files)} detector rate files")

            total_tasks = len(upload_tasks) + len(legacy_tasks)
            print(f"Uploading {total_tasks} files ({len(upload_tasks)} NIRSpec → OSN)...")
            success, failed, failed_msgs = upload_files_parallel(
                config, upload_tasks, desc="OSN uploads", succeeded_out=uploaded_keys,
                backend='osn',
            )
            if legacy_tasks:
                s2, f2, m2 = upload_files_parallel(
                    config, legacy_tasks, desc="R2 uploads (rgb/sed)",
                    succeeded_out=uploaded_keys, backend='r2',
                )
                success += s2
                failed += f2
                failed_msgs = failed_msgs + m2

            if failed_msgs:
                print(f"\n  {failed} uploads failed:")
                for msg in failed_msgs[:10]:
                    print(f"    - {msg}")
                if len(failed_msgs) > 10:
                    print(f"    ... and {len(failed_msgs) - 10} more")
            print(f"Uploaded {success}/{total_tasks} files")
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
        n_obj, new_object_ids, _n_quality_reset = batch_upsert_objects(sb, objects, field, force_overwrite, objects_with_sed)
        print(f"  {n_obj} objects")

        # B2: an draft deploy forces deploy_status='draft' on every spectrum it
        # writes — the whole observation lands as an admin-only draft. Guard first
        # against silently hiding data that is already live: warn (and require
        # confirmation) if any of these (target_id, grating) rows are currently
        # 'published', since re-drafting them would pull them from users until an
        # explicit publish. A single spectra row per (target,grating) can't hold a
        # live + draft version at once, so this is "take the observation back to
        # draft", not zero-downtime staging of a re-reduction.
        if draft:
            already_published = _count_published_spectra(sb, spectra)
            if already_published and not auto_approve:
                print()
                print(f"WARNING: --in-prep will hide {already_published} currently-PUBLISHED "
                      f"spectrum row(s) until you re-publish them.")
                resp = input("  Take these back to draft (draft)? [y/N]: ")
                if resp.lower() != 'y':
                    print("Aborted.")
                    return {'field': field, 'needs_reconcile': False}
            for rec in spectra:
                rec['deploy_status'] = 'draft'

        print("Upserting spectra...")
        n_spec, changed_hashes = batch_upsert_spectra(sb, spectra)
        print(f"  {n_spec} spectra ({len(changed_hashes)} with hash changes)")

        # Recompute aggregate columns (max_snr, max_exposure_time) in bulk
        target_ids = [o['object_id'] for o in objects]
        n_agg = recompute_target_aggregates(sb, target_ids)
        print(f"  Recomputed aggregates for {n_agg} targets")

        # Phase C: incremental object reconciliation. Preserves inspection
        # state on existing objects, inserts only when new clusters appear,
        # soft-deletes orphans (is_active=false), and aborts hard if a
        # split/merge is detected (operator must resolve interactively via
        # `campfire deploy objects reconcile`). `propagate_crossmatches` is
        # gone — its job is now done by object-level inspection ownership.
        # `defer_rebuild` defers reconciliation to the multi-obs caller so
        # the same field isn't reconciled N times for N observations.
        if defer_rebuild:
            print(f"\nDeferred objects reconciliation (--defer-rebuild set)")
        else:
            from campfire.deploy.reconcile import reconcile_field_objects
            print("\nReconciling objects...")
            n_clusters, _stats, changed_ids = reconcile_field_objects(
                sb, field,
                changed_hashes=changed_hashes,
                abort_on_split_merge=True,
            )
            print(f"  {n_clusters} clusters")

            print()
            refresh_filter_options(sb)
            refresh_programs_overview(sb)

            # Photometry: cross-match the field's catalog and upsert rows
            # for objects touched by reconcile. Skipped silently if no
            # photometry config exists for the field, or the change set is
            # empty (early-exit inside deploy_field_photometry).
            if include_photometry and changed_ids:
                from campfire.deploy.config import resolve_photometry_config
                from campfire.deploy.photometry import deploy_field_photometry
                phot_path = resolve_photometry_config(None)
                if phot_path is not None:
                    print(f"\nDeploying photometry for {len(changed_ids)} "
                          f"changed objects...")
                    deploy_field_photometry(
                        sb, field, phot_path, config,
                        restrict_to_object_db_ids=changed_ids,
                    )

        # Deploy pointings (JSONB on observations)
        pointings_ecsv = discover_pointings_ecsv(obs_dir, obs_name)
        if pointings_ecsv:
            pointings_data = load_pointings_ecsv(pointings_ecsv)
            update_observation_pointings(sb, obs_name, pointings_data)
            print(f"\nDeployed {len(pointings_data)} pointing(s) for {obs_name}")
        else:
            print(f"\nNo pointings ECSV found for {obs_name}, skipping pointings deployment")

        # Deploy shutters
        n_shutters = 0
        if include_shutters:
            ecsv_path = discover_shutters_ecsv(obs_dir, obs_name)
            if ecsv_path:
                shutters_data = load_shutters_ecsv(ecsv_path)
                n_src = len(set(r['object_id'] for r in shutters_data))
                print(f"\nDeploying shutters ({len(shutters_data)} records, {n_src} sources)...")

                if not skip_astrometry:
                    import tomllib
                    from campfire.deploy.astrometry import correct_shutter_positions

                    imaging_path = resolve_imaging_config()
                    if imaging_path is None:
                        print("  No imaging.toml found, skipping astrometry correction")
                    else:
                        with open(imaging_path, 'rb') as f:
                            imaging_config = tomllib.load(f)
                        n_corrected, n_matches = correct_shutter_positions(
                            shutters_data, obs_dir, obs_name, field, imaging_config,
                        )
                        if n_corrected:
                            print(f"  Astrometry: corrected {n_corrected} shutters "
                                  f"({n_matches} catalog cross-matches)")

                n_shutters = db_deploy_shutters(sb, obs_name, shutters_data)
                print(f"  Deployed {n_shutters} shutter records")
            else:
                print(f"\nNo shutters ECSV found for {obs_name}, skipping shutter deployment")

        # Record deployment provenance
        config_snapshot = _load_config_snapshot(obs_dir, obs_name)
        stuck_shutters = _load_stuck_shutters(resolve_reference_obs_dir(obs_name))
        user_id = get_user_id_from_token(config)

        deployment_id = insert_deployment(
            sb,
            observation=obs_name,
            deployed_by=user_id,
            cfpipe_version=summary.meta.get('cfpipe_version'),
            jwst_version=summary.meta.get('jwst_version'),
            crds_context=summary.meta.get('crds_context'),
            config_snapshot=config_snapshot,
            stuck_shutters=stuck_shutters,
            # Real reduction time (earliest CMPFRTIM across products), not the
            # summary-build wall-clock — so re-running `summary` on unchanged
            # pixels no longer advances reduced_at.
            reduced_at=summary.meta.get('reduced_at'),
            n_targets=len(objects),
            n_spectra=len(spectra),
            n_new_targets=len(new_object_ids),
            force_overwrite=force_overwrite,
            source_ids_filter=source_ids,
            supabase_only=supabase_only,
            status='draft' if draft else 'published',
        )
        if deployment_id:
            update_latest_deployment(sb, obs_name, deployment_id)
            print(f"\nDeployment #{deployment_id} recorded"
                  f"{' (draft)' if draft else ''}")
            # B2: record the deploy in the lifecycle audit log (normalized
            # envelope, Phase 3). `success`/`failed` are the actual OSN+R2 upload
            # tallies; `affected_count` stays the spectra count for continuity.
            log_deploy_event(
                sb,
                action='upload',
                actor=user_id,
                deployment_id=deployment_id,
                observation=obs_name,
                affected_count=len(spectra),
                metadata=deploy_event_metadata(
                    'nirspec', observation=obs_name, planned=total_tasks,
                    succeeded=success, failed=failed, items=len(spectra),
                    draft=draft, n_objects=len(objects)),
            )

        # B4 multi-reducer safety: compare-and-set the scope version. A conflict
        # means another reducer deployed this same observation while we were
        # running — surface it so the operator can re-check (the writes are
        # idempotent by key, but a concurrent re-reduction may have interleaved).
        claim = claim_deploy_scope(sb, 'observation', obs_name,
                                   scope_version_at_start, actor=user_id)
        if claim.get('conflict'):
            print()
            print(f"⚠  CONCURRENT DEPLOY DETECTED for {obs_name}: another deploy "
                  f"advanced this observation (version {scope_version_at_start} -> "
                  f"{claim.get('current')}) while this one was running.")
            print("   Both deploys' writes are present; re-check the result and "
                  "re-deploy if a re-reduction was clobbered.")

        # Storage registry (#214): index the objects that actually landed in this
        # deploy. Shadow index — additive, nothing reads it as authoritative yet.
        if uploaded_keys:
            from campfire.deploy.registry import (
                build_registry_rows, upsert_storage_objects,
            )
            # upload_tasks are the NIRSpec products, homed on OSN under canonical
            # keys (rgb/sed are in legacy_tasks and never register). backend='osn'.
            reg_rows = build_registry_rows(
                upload_tasks,
                backend='osn',
                deployment_id=deployment_id,
                uploaded_by=user_id,
                cfpipe_version=summary.meta.get('cfpipe_version'),
                succeeded_keys=uploaded_keys,
            )
            n_reg = upsert_storage_objects(sb, reg_rows)
            print(f"Registered {n_reg} storage objects")

        print()
        msg = f"Deployed {len(spectra)} spectra from {len(objects)} objects"
        if zfit_paths:
            msg += f" + {len(zfit_paths)} zfit files"
        if rgb_files:
            msg += f" + {len(rgb_files)} RGB images"
        if sed_files:
            msg += f" + {len(sed_files)} SED plots"
        if n_shutters:
            msg += f" + {n_shutters} shutters"
        print(msg)

        # Phase C: any successful deploy potentially affected member spectra
        # or membership; the deferred multi-obs path always wants to reconcile.
        return {'field': field, 'needs_reconcile': True}

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
    overwrite: bool = False,
) -> None:
    """Generate and deploy RGB images to R2."""
    obs_dir = resolve_obs_dir(obs_name)

    # Generate RGB images (skips existing unless overwrite=True)
    if not dry_run:
        field = resolve_field(obs_name)
        if field:
            from campfire.deploy.generate_rgb import generate_rgb_images
            n = generate_rgb_images(obs_name, obs_dir, field,
                                    overwrite=overwrite, source_ids=source_ids)
            if n:
                print(f"Generated {n} RGB images")

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
            print(f"  {path.name} -> {storage_key('rgb', Scope(obs=obs_name), path.name)}")
        if len(rgb_files) > 5:
            print(f"  ... and {len(rgb_files) - 5} more")
        return

    tasks = [UploadTask(p, storage_key('rgb', Scope(obs=obs_name), p.name), 'image/png') for p in rgb_files]
    success, failed, failed_msgs = upload_files_parallel(config, tasks, desc="RGB images")

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
    overwrite: bool = False,
) -> None:
    """Generate and deploy SED plots to R2 and update has_sed_plot in Supabase."""
    obs_dir = resolve_obs_dir(obs_name)

    # Generate SED plots (skips existing unless overwrite=True)
    if not dry_run:
        field = resolve_field(obs_name)
        if field:
            from campfire.deploy.generate_sed import generate_sed_plots
            n = generate_sed_plots(obs_name, obs_dir, field,
                                   overwrite=overwrite, source_ids=source_ids)
            if n:
                print(f"Generated {n} SED plots")

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
            print(f"  {path.name} -> {storage_key('sed', Scope(obs=obs_name), path.name)}")
        if len(sed_files) > 5:
            print(f"  ... and {len(sed_files) - 5} more")
        print(f"Would set has_sed_plot=true for {len(objects_with_sed)} objects")
        return

    tasks = [UploadTask(p, storage_key('sed', Scope(obs=obs_name), p.name), 'application/pdf') for p in sed_files]
    success, failed, failed_msgs = upload_files_parallel(config, tasks, desc="SED plots")

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
            print(f"  {path.name} -> {storage_key('spectrum_json', Scope(obs=obs_name), f'{path.stem}.json', scheme=KeyScheme.CANONICAL)}")
        if len(spec_paths) > 5:
            print(f"  ... and {len(spec_paths) - 5} more")
        return

    temp_dir = obs_dir / '.deploy_temp'
    temp_dir.mkdir(exist_ok=True)

    try:
        print("Generating JSON files...")
        scope = Scope(obs=obs_name)
        tasks = []
        for path in tqdm(spec_paths, desc="Generating", unit="file"):
            json_path = generate_spectrum_json(path, temp_dir)
            tasks.append(UploadTask(json_path, storage_key('spectrum_json', scope, json_path.name, scheme=KeyScheme.CANONICAL), 'application/json'))

        print("Uploading to OSN...")
        uploaded_keys: set[str] = set()
        success, failed, failed_msgs = upload_files_parallel(
            config, tasks, desc="JSON files", succeeded_out=uploaded_keys, backend='osn')

        if failed_msgs:
            print(f"\n  {failed} failed:")
            for msg in failed_msgs[:5]:
                print(f"    - {msg}")

        print(f"Uploaded {success}/{len(spec_paths)} JSON files")

        # Refresh registry rows for the re-uploaded canonical objects so the served
        # OSN bytes and the recorded sha256 stay consistent (epic #210 / #216).
        # deployment_id stays NULL — a content-only refresh records no deployment;
        # the next full `deploy --obs` re-links provenance.
        if uploaded_keys:
            from campfire.deploy.registry import build_registry_rows, upsert_storage_objects
            sb = get_supabase_client(config)
            reg_rows = build_registry_rows(tasks, backend='osn', succeeded_keys=uploaded_keys)
            n_reg = upsert_storage_objects(sb, reg_rows)
            print(f"Registered {n_reg} storage objects")
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
    temp_dir = obs_dir / '.deploy_temp'
    temp_dir.mkdir(exist_ok=True)

    try:
        print("Generating zfit JSON files...")
        scope = Scope(obs=obs_name)
        tasks = []
        for path in zfit_paths:
            json_path = generate_zfit_json(path, temp_dir)
            tasks.append(UploadTask(json_path, storage_key('zfit', scope, json_path.name, scheme=KeyScheme.CANONICAL), 'application/json'))

        print("Uploading to OSN...")
        uploaded_keys: set[str] = set()
        success, failed, failed_msgs = upload_files_parallel(
            config, tasks, desc="Zfit JSON", succeeded_out=uploaded_keys, backend='osn')

        if failed_msgs:
            print(f"\n  {failed} failed:")
            for msg in failed_msgs[:5]:
                print(f"    - {msg}")

        print(f"Uploaded {success}/{len(zfit_paths)} zfit JSON files")

        # Refresh registry rows for the re-uploaded canonical objects so the served
        # OSN bytes and the recorded sha256 stay consistent (epic #210 / #216).
        # Must run before `finally` removes temp_dir (build_registry_rows hashes the
        # local files). deployment_id stays NULL (content-only refresh).
        if uploaded_keys:
            from campfire.deploy.registry import build_registry_rows, upsert_storage_objects
            reg_rows = build_registry_rows(tasks, backend='osn', succeeded_keys=uploaded_keys)
            n_reg = upsert_storage_objects(get_supabase_client(config), reg_rows)
            print(f"Registered {n_reg} storage objects")
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
    n, _, _ = batch_upsert_objects(sb, objects, field, force_overwrite)
    print(f"  Updated {n} objects")

    refresh_filter_options(sb)
    refresh_programs_overview(sb)
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
                        'target_id', row['object_id']
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


def deploy_shutters(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
    skip_astrometry: bool = False,
) -> None:
    """Deploy shutters ECSV data to Supabase."""
    obs_dir = resolve_obs_dir(obs_name)
    ecsv_path = discover_shutters_ecsv(obs_dir, obs_name)

    if not ecsv_path:
        print(f"Error: Shutters file not found: {obs_dir / f'{obs_name}_shutters.ecsv'}")
        print(f"Generate it first with: cfpipe nirspec summary --obs {obs_name}")
        return

    shutters_data = load_shutters_ecsv(ecsv_path)
    n_sources = len(set(r['object_id'] for r in shutters_data))
    print(f"Found {len(shutters_data)} shutter records ({n_sources} sources)")

    # Astrometric correction: align MSA coordinates to imaging reference frame
    if skip_astrometry:
        print("Skipping astrometry correction (--skip-astrometry)")
    else:
        import tomllib
        from campfire.deploy.astrometry import correct_shutter_positions

        field = resolve_field(obs_name)
        if not field:
            print("  Could not determine field, skipping astrometry")
        else:
            imaging_path = resolve_imaging_config()
            if imaging_path is None:
                print("  No imaging.toml found, skipping astrometry correction")
            else:
                with open(imaging_path, 'rb') as f:
                    imaging_config = tomllib.load(f)
                n_corrected, n_matches = correct_shutter_positions(
                    shutters_data, obs_dir, obs_name, field, imaging_config,
                )
                if n_corrected:
                    print(f"  Astrometry: corrected {n_corrected} shutters "
                          f"({n_matches} catalog cross-matches)")

    if dry_run:
        print("=== DRY RUN ===")
        print(f"Would delete existing shutters for '{obs_name}'")
        print(f"Would insert {len(shutters_data)} rows")
        return

    sb = get_supabase_client(config)

    print("Deploying shutters...")
    n = db_deploy_shutters(sb, obs_name, shutters_data)
    print(f"Deployed {n} shutter records for {obs_name}")


def deploy_pointings(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
) -> None:
    """Deploy pointings ECSV data to observations.pointings (JSONB).

    Useful for backfilling existing observations without rerunning a
    full `campfire deploy`. The observations row must already exist —
    run `campfire deploy --obs <name>` first if it doesn't.
    """
    obs_dir = resolve_obs_dir(obs_name)
    ecsv_path = discover_pointings_ecsv(obs_dir, obs_name)

    if not ecsv_path:
        print(f"Error: Pointings file not found: {obs_dir / f'{obs_name}_pointings.ecsv'}")
        print(f"Generate it first by rerunning the pipeline summary for {obs_name}.")
        return

    pointings_data = load_pointings_ecsv(ecsv_path)
    print(f"Found {len(pointings_data)} pointing(s) for {obs_name}")

    if dry_run:
        print("=== DRY RUN ===")
        print(f"Would update observations.pointings for '{obs_name}' "
              f"with {len(pointings_data)} entries")
        return

    sb = get_supabase_client(config)
    n_updated = update_observation_pointings(sb, obs_name, pointings_data)
    if n_updated == 0:
        print(f"Error: observation '{obs_name}' not found in database.")
        print(f"  Run 'campfire deploy --obs {obs_name}' first to create the row.")
        return
    print(f"Deployed {len(pointings_data)} pointing(s) for {obs_name}")


def fetch_config(
    obs_name: str,
    config: dict,
    output_dir: Path | None = None,
) -> None:
    """
    Reconstitute reduction config files from the database.

    Fetches the latest deployment for the observation and writes:
    - {obs}_config.toml from deployments.config_snapshot
    - {obs}_stuck_closed_shutters.toml from deployments.stuck_shutters
      (place at reference/nirspec/<obs>/stuck_closed_shutters.toml to re-reduce)
    - observations.toml fragment from observations metadata

    Parameters
    ----------
    obs_name : str
        Observation name
    config : dict
        Deploy config (for Supabase credentials)
    output_dir : Path, optional
        Output directory (default: current directory)
    """
    import toml

    sb = get_supabase_client(config)
    result = fetch_deployment_config(sb, obs_name)

    if result is None:
        print(f"Error: Observation '{obs_name}' not found or has no deployment record.")
        return

    obs_data = result['observation']
    dep_data = result['deployment']
    out = output_dir or Path('.')
    out.mkdir(parents=True, exist_ok=True)

    files_written = []

    # 1. Pipeline config snapshot
    if dep_data.get('config_snapshot'):
        config_path = out / f"{obs_name}_config.toml"
        with open(config_path, 'w') as f:
            toml.dump(dep_data['config_snapshot'], f)
        files_written.append(config_path)

    # 2. Stuck shutters. Issue #212 (PR-4): the pipeline reads this from
    # reference/nirspec/<obs>/stuck_closed_shutters.toml; reconstitute under that
    # bare basename (obs-prefixed here so a flat output dir stays unambiguous).
    if dep_data.get('stuck_shutters'):
        shutters_path = out / f"{obs_name}_stuck_closed_shutters.toml"
        with open(shutters_path, 'w') as f:
            toml.dump(dep_data['stuck_shutters'], f)
        files_written.append(shutters_path)

    # 3. Observations.toml fragment
    obs_fragment = {obs_name: {}}
    if obs_data.get('field'):
        obs_fragment[obs_name]['field'] = obs_data['field']
    if obs_data.get('program_slug'):
        obs_fragment[obs_name]['program'] = obs_data['program_slug']
    if obs_data.get('data_subdir'):
        obs_fragment[obs_name]['data_subdir'] = obs_data['data_subdir']
    if obs_data.get('file_globs'):
        globs = obs_data['file_globs']
        obs_fragment[obs_name]['files'] = globs[0] if len(globs) == 1 else globs
    if obs_data.get('gratings'):
        obs_fragment[obs_name]['gratings'] = obs_data['gratings']

    obs_path = out / 'observations.toml'
    with open(obs_path, 'w') as f:
        toml.dump(obs_fragment, f)
    files_written.append(obs_path)

    # Summary
    print(f"Fetched config for '{obs_name}':")
    for p in files_written:
        print(f"  {p}")

    print(f"\nDeployment #{dep_data.get('id')}:")
    print(f"  Deployed by: {dep_data.get('deployed_by', 'unknown')}")
    print(f"  Deployed at: {dep_data.get('deployed_at', 'unknown')}")
    print(f"  Reduced at:  {dep_data.get('reduced_at', 'unknown')}")
    print(f"  cfpipe:      {dep_data.get('cfpipe_version', 'unknown')}")
    print(f"  jwst:        {dep_data.get('jwst_version', 'unknown')}")
    print(f"  CRDS:        {dep_data.get('crds_context', 'unknown')}")
