"""
Deployment orchestrator.

Each public function follows the same pattern:
  1. Load ECSV (primary metadata source)
  2. Filter by source IDs if requested
  3. Discover / generate content
  4. Upload to R2
  5. Upsert to Supabase
"""

import json
import re
import shutil
import sys
from pathlib import Path

from tqdm import tqdm

from campfire.deploy.config import load_observations, load_programs, resolve_field, resolve_imaging_config, resolve_obs_dir, resolve_reference_obs_dir, validate_program_slug
from campfire.deploy.discover import (
    discover_pointings_ecsv,
    discover_rate_files,
    discover_shutters_ecsv,
    discover_spectrum_exposures,
    discover_slits_json,
    load_pointings_ecsv,
    load_shutters_ecsv,
    load_slits_json,
)
from campfire.deploy.generate import (
    new_deploy_temp_dir,
    generate_spectrum_jsons,
    generate_spectrum_products,
    generate_thumbnails_from_fits,
    generate_zfit_json,
)
from campfire_layout import KeyScheme, Scope, storage_key
from campfire.deploy.lines import (
    get_lines_paths, line_fit_versions, lines_upload_tasks, publish_line_fits,
    split_auto_line_fits,
)
from campfire.deploy.deploy_plan import (
    FileGroup, NirspecDeployPlan, format_bytes, path_size, render_nirspec_plan,
)
from campfire.deploy.nirspec_flags import deployment_snapshot
from campfire.deploy.r2 import UploadTask, upload_files_parallel
from campfire.deploy.supabase import (
    batch_upsert_objects,
    batch_upsert_spectra,
    check_existing_objects,
    deploy_event_metadata,
    deploy_shutters as db_deploy_shutters,
    deploy_slits as db_deploy_slits,
    ScopeLease,
    get_lifecycle_status,
    get_supabase_client,
    get_user_id_from_token,
    insert_deployment,
    log_deploy_event,
    mark_deployment_completed,
    recompute_target_aggregates,
    refresh_filter_options,
    refresh_programs_overview,
    refresh_observations_overview,
    update_latest_deployment,
    update_observation_pointings,
    update_spectra_zfit_scalars,
    upsert_observation,
    upsert_programs,
)
from campfire.deploy.summary import (
    filter_by_source_ids,
    format_stale_summary,
    stale_summary_rows,
    get_field,
    get_program_slug,
    get_spec_paths,
    get_spectra_records,
    get_unique_objects,
    get_zfit_scalar_updates,
    get_zfit_paths,
    load_summary,
    spectrum_key,
    summary_row_filter,
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


_RELEASE_VERSION_RE = re.compile(r'^\d+\.\d+\.\d+$')


def _is_release_version(version: str | None) -> bool:
    """Return True iff *version* is a clean PEP 440 release like '0.4.0'.

    Anything else — ``.dev`` builds, ``+local`` segments, free-form override
    strings, or raw git SHAs from the legacy reduction_version scheme — is
    considered non-release and triggers the warn-and-confirm path in
    ``deploy_observation``.
    """
    return bool(version) and bool(_RELEASE_VERSION_RE.match(version))


def _collect_non_release_versions(summary, spectra, extra_versions=()) -> list[str]:
    """Return the unique non-release version strings present in *summary* or
    *spectra*. Inspects both ``summary.meta['cfpipe_version']`` and each
    spectrum's ``cfpipe_version`` (sourced verbatim from the FITS ``CMPFRVER``
    header), since heterogeneous reductions may carry different strings
    per row. *extra_versions* are further strings to gate on the same way
    (the ``CMPFRVER`` of the line-fit products, which may come from a later,
    untagged pipeline than the spectra).
    """
    versions: set[str] = set()
    meta_v = summary.meta.get('cfpipe_version')
    if meta_v:
        versions.add(meta_v)
    for s in spectra:
        v = s.get('cfpipe_version')
        if v:
            versions.add(v)
    versions.update(v for v in extra_versions if v)
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
    """How many of these spectra (``(target_id, grating, filter)``) are currently 'published'.

    Used by the `--in-prep` guard so a re-draft never silently hides live data.
    PostgREST can't filter on tuples, so over-fetch by target_id and match the
    keys Python-side (spectra per target are few).
    """
    wanted = {spectrum_key(s) for s in spectra}
    target_ids = sorted({k[0] for k in wanted})
    published = 0
    for i in range(0, len(target_ids), 200):
        batch = target_ids[i:i + 200]
        resp = (sb.table('spectra')
                .select('target_id, grating, filter, deploy_status')
                .in_('target_id', batch)
                .eq('deploy_status', 'published')
                .execute())
        for row in resp.data or []:
            if spectrum_key(row) in wanted:
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
    steal_lease: bool = False,
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
        config_section=obs_cfg or None,
    )

    scope = Scope(obs=obs_name)
    # Deploy lease (data releases P1): visible while this deploy runs; refuses
    # (DeployScopeBusy, surfaced by the CLI) if another deploy of this
    # observation is live. Also returns the B4 scope version for the finalize CAS.
    lease = ScopeLease(sb, 'observation', obs_name, actor=user_id, steal=steal_lease)
    lease.begin()
    upload_tasks = [
        UploadTask(p, storage_key('nirspec_spectrum_exposure', scope, p.name,
                                  scheme=KeyScheme.CANONICAL), 'application/fits')
        for p in exposure_files
    ] + [
        UploadTask(p, storage_key('nirspec_rate', scope, p.name,
                                  scheme=KeyScheme.CANONICAL), 'application/fits')
        for p in rate_files
    ]
    # Best-effort provenance from the first exposure header (Phase 3): the
    # intermediates carry whatever cards the reduction stamped. Absent → NULL.
    # For a rate-only deploy (stage 1 done, stage 2 not yet) there are no
    # spectrum-exposures, so fall back to a rate header for provenance.
    cfpipe_version, jwst_version, crds_context = _read_exposure_provenance(
        exposure_files or rate_files)

    # Record the deployment UP FRONT (parity with the main deploy paths): the
    # flusher registers landed objects directly against it as batches complete.
    # Draft semantics: only uploaded objects stage as draft; unchanged objects
    # stay on their prior (possibly published) deployment.
    deployment_id = insert_deployment(
        sb, observation=obs_name, deployed_by=user_id, status='draft',
        cfpipe_version=cfpipe_version, jwst_version=jwst_version,
        crds_context=crds_context, n_targets=0, n_spectra=0,
    )
    if deployment_id:
        print(f"Deployment #{deployment_id} recorded (draft — intermediates only)")

    # Push plan: dedup against the server registry (with the local mirror's
    # stat fast path), then upload only new/changed bytes, registering per
    # batch so an interrupted run resumes at file granularity.
    from campfire.deploy.push import (
        open_reducer_store, plan_remote_push, registration_flusher,
    )
    store = open_reducer_store()
    plan, _server_rows = plan_remote_push(sb, store, upload_tasks, backend='osn')
    print(f"Plan: {plan.summary()}")

    uploaded_keys: set[str] = set()
    success, failed, failed_msgs = 0, 0, []
    flusher = registration_flusher(
        sb, store, plan, backend='osn', deployment_id=deployment_id,
        uploaded_by=user_id, cfpipe_version=cfpipe_version,
        heartbeat=lease.heartbeat)
    if plan.to_upload:
        print(f"Uploading {len(plan.to_upload)} of {len(upload_tasks)} intermediates "
              f"({len(exposure_files)} spectrum-exposures + {len(rate_files)} rate files) to OSN...")
        success, failed, failed_msgs = upload_files_parallel(
            config, plan.to_upload, desc="OSN uploads", succeeded_out=uploaded_keys,
            backend='osn', on_success=flusher.add)
        flusher.flush()
        print(f"Uploaded {success}"
              + (f", failed {failed}" if failed else "")
              + f" ({len(plan.unchanged)} unchanged skipped)")
    else:
        print("Cloud already matches local intermediates — nothing to upload.")

    if deployment_id:
        update_latest_deployment(sb, obs_name, deployment_id)
        log_deploy_event(
            sb, action='upload', actor=user_id, deployment_id=deployment_id,
            observation=obs_name, affected_count=success,
            metadata=deploy_event_metadata(
                'nirspec', observation=obs_name, planned=len(upload_tasks),
                succeeded=success, failed=failed, skipped=len(plan.unchanged),
                items=len(upload_tasks), draft=True, intermediates_only=True))

    # Rate-mask triage rows (P2, design §3.2): one per (obs, exposure, detector),
    # split-ownership upsert so re-deploy never clobbers web review state.
    if rate_files:
        from campfire.deploy.rate_exposures import (
            build_rate_exposure_records, upsert_rate_exposures,
        )
        n_rate = upsert_rate_exposures(
            sb, build_rate_exposure_records(rate_files, observation=obs_name, scope=scope))
        print(f"Upserted {n_rate} rate-exposure triage rows")

    # Nods-renderer grid rows (P4, design §4.2): one per (obs, exposure_root, nod,
    # detector, source), exp_group from the CFEXPGRP header stamp. Split-ownership
    # upsert. exposure_files is already source-filtered — correct, the grid is
    # per-source.
    if exposure_files:
        from campfire.deploy.spectrum_grid import (
            build_spectrum_exposure_records, upsert_spectrum_exposures,
        )
        n_grid = upsert_spectrum_exposures(
            sb, build_spectrum_exposure_records(exposure_files, observation=obs_name, scope=scope))
        print(f"Upserted {n_grid} spectrum-exposure grid rows")

    # Completion marker: every write of this deploy has landed. A failed upload
    # leaves it NULL — the deploy told the operator to re-run, it did not finish.
    if not failed:
        mark_deployment_completed(sb, deployment_id)

    claim = lease.claim()
    if claim.get('conflict'):
        print(f"⚠  CONCURRENT DEPLOY DETECTED for {obs_name}: another deploy advanced "
              f"this observation while this one was running.")

    print(f"\nDeployed {len(exposure_files)} canonical spectrum-exposures "
          f"+ {len(rate_files)} detector rate files (draft — no finals yet). "
          f"Finish stage3 + re-deploy to publish.")
    return {'field': field, 'needs_reconcile': False}


def _build_nirspec_plan(
    obs_name: str,
    obs_dir: Path,
    summary,
    temp_dir: Path,
    *,
    supabase_only: bool,
    force_overwrite: bool,
    include_shutters: bool,
    include_photometry: bool,
    skip_astrometry: bool,
    source_ids: list[int] | None,
    draft: bool,
    defer_rebuild: bool,
) -> NirspecDeployPlan:
    """The local half of an observation's deploy plan.

    Discovers every product, generates the sidecars that get uploaded (the
    spectrum JSON pair, the zfit and line-fit JSON) into *temp_dir*, embeds
    the SVG thumbnails in the spectra records, and loads the pointings and
    shutters. Touches no network: the remote half is :func:`_check_plan_remote`.
    """
    field = get_field(summary)
    program_slug = get_program_slug(summary)
    targets = get_unique_objects(summary)
    spectra = get_spectra_records(summary, obs_name)
    spec_paths = get_spec_paths(summary, obs_dir)
    zfit_paths = get_zfit_paths(summary, obs_dir)
    lines_paths = get_lines_paths(summary, obs_dir)
    scope = Scope(obs=obs_name)

    def task(path: Path, kind: str, content_type: str) -> UploadTask:
        return UploadTask(path, storage_key(kind, scope, path.name, scheme=KeyScheme.CANONICAL),
                          content_type)

    # One read per final yields every derivative: the spectrum JSON and its
    # 1-D sidecar (uploaded) and the SVG thumbnails (embedded in the spectra
    # rows, so they are made on --supabase-only too).
    finals: list[UploadTask] = []
    sidecars: list[UploadTask] = []
    thumb_map: dict[str, dict] = {}
    for spec_path in tqdm(spec_paths, desc="Generating spectrum JSON", unit="file"):
        if supabase_only:
            try:
                thumb_map[spec_path.name] = generate_thumbnails_from_fits(spec_path)
            except Exception as e:
                print(f"  Warning: {spec_path.name}: {e}")
            continue
        finals.append(task(spec_path, 'nirspec_spec', 'application/fits'))
        json_path, json_1d_path, thumbs = generate_spectrum_products(spec_path, temp_dir)
        thumb_map[spec_path.name] = thumbs
        sidecars.append(task(json_path, 'spectrum_json', 'application/json'))
        sidecars.append(task(json_1d_path, 'spectrum_1d_json', 'application/json'))
    for rec in spectra:
        fits_name = rec['fits_path'].split('/')[-1]
        if fits_name in thumb_map:
            rec.update(thumb_map[fits_name])

    file_groups: list[FileGroup] = []
    exposure_files: list = []
    rate_files: list = []
    if not supabase_only:
        zfits = [task(generate_zfit_json(p, temp_dir), 'zfit', 'application/json')
                 for p in zfit_paths]
        line_tasks = lines_upload_tasks(obs_name, lines_paths, temp_dir)
        # Canonical spectrum-exposures (epic #210, B5) go up on every deploy,
        # narrowed to --source-ids. Detector rate files (P1) are per detector,
        # not per source, so they are never narrowed.
        exposure_files = discover_spectrum_exposures(obs_dir)
        if source_ids:
            exposure_files = _filter_exposures_by_source_ids(exposure_files, source_ids)
        rate_files = discover_rate_files(obs_dir)
        file_groups = [
            FileGroup('Final spectra (FITS)', finals),
            FileGroup('Spectrum JSON (full + 1-D)', sidecars),
            FileGroup('Redshift fits (zfit JSON)', zfits),
            FileGroup('Line fits (FITS + JSON)', line_tasks),
            FileGroup('Spectrum-exposure intermediates',
                      [task(p, 'nirspec_spectrum_exposure', 'application/fits')
                       for p in exposure_files]),
            FileGroup('Detector rate files',
                      [task(p, 'nirspec_rate', 'application/fits') for p in rate_files]),
        ]

    pointings_path = discover_pointings_ecsv(obs_dir, obs_name)
    shutters = None
    astrometry = ''
    if include_shutters:
        shutters_path = discover_shutters_ecsv(obs_dir, obs_name)
        if shutters_path:
            shutters = load_shutters_ecsv(shutters_path)
            if skip_astrometry:
                astrometry = 'raw MSA positions (--skip-astrometry)'
            elif resolve_imaging_config() is None:
                astrometry = 'raw MSA positions (no imaging.toml)'
            else:
                astrometry = "positions corrected to the field's reference catalog"

    if not include_photometry:
        photometry = 'skipped (--no-photometry)'
    else:
        from campfire.deploy.config import resolve_photometry_config
        if resolve_photometry_config(None) is None:
            photometry = 'skipped (no photometry.toml)'
        else:
            photometry = 'cross-match the objects the reconcile changes'

    warnings: list[str] = []
    non_release = _collect_non_release_versions(
        summary, spectra, extra_versions=line_fit_versions(lines_paths))
    if non_release:
        warnings.append(
            "Non-release pipeline version: " + ', '.join(non_release) + "\n"
            "Kept verbatim in spectra.cfpipe_version / line_fits.cfpipe_version;\n"
            "prefer deploying from a tagged release (see /pipeline-release).")
    crds = _collect_crds_contexts(spectra)
    if len(crds) > 1:
        warnings.append(
            "Mixed CRDS contexts: " + ', '.join(crds) + "\n"
            "Spectra reduced against different pmaps are not calibration-homogeneous;\n"
            "prefer re-reducing the whole observation against one.")
    if force_overwrite:
        warnings.append("--force-overwrite: inspection data on existing targets will be RESET")

    n_line_fits = len(split_auto_line_fits(lines_paths)[0]) if lines_paths else 0

    return NirspecDeployPlan(
        obs_name=obs_name,
        field=field,
        program_slug=program_slug,
        targets=targets,
        spectra=spectra,
        file_groups=file_groups,
        cfpipe_version=summary.meta.get('cfpipe_version'),
        crds_context=crds[0] if len(crds) == 1 else summary.meta.get('crds_context'),
        pointings=load_pointings_ecsv(pointings_path) if pointings_path else None,
        shutters=shutters,
        shutters_skipped=not include_shutters,
        astrometry=astrometry,
        n_line_fits=n_line_fits,
        n_rate_rows=len(rate_files),
        n_grid_rows=len(exposure_files),
        photometry=photometry,
        supabase_only=supabase_only,
        force_overwrite=force_overwrite,
        draft=draft,
        defer_rebuild=defer_rebuild,
        warnings=warnings,
        lines_paths=lines_paths,
        exposure_files=exposure_files,
        rate_files=rate_files,
    )


def _check_plan_remote(sb, plan: NirspecDeployPlan, store, *, progress: bool = True) -> None:
    """The remote half of the plan, read-only: which targets already exist,
    which files the cloud registry already holds, and (for --draft) how many
    published spectra the deploy would take back to draft.

    ``plan_remote_push`` runs without its wcs_hash backfill here, so a dry run
    writes nothing to the server (it refreshes the local mirror's cache only,
    as ``campfire push --dry-run`` does).
    """
    plan.existing_targets = len(check_existing_objects(sb, [t['object_id'] for t in plan.targets]))
    if plan.draft:
        plan.n_published_redraft = _count_published_spectra(sb, plan.spectra)
        if plan.n_published_redraft:
            plan.warnings.append(
                f"--draft hides {plan.n_published_redraft:,} currently-PUBLISHED spectrum "
                f"row(s) until they are re-published")
    if not plan.supabase_only:
        from campfire.deploy.push import plan_remote_push
        plan.push, _ = plan_remote_push(sb, store, plan.upload_tasks, backend='osn',
                                        progress=progress)


def _finish_nirspec_deployment(sb, deployment_id, push_plan, *, draft: bool,
                               failed: int) -> None:
    """The last writes of a NIRSpec finals deploy: re-point, then completion.

    Re-points unchanged dedup-skipped objects at this deployment, only on a
    PUBLISHED deploy, so deployment-gated intermediates ride the current
    deployment's visibility. Uploaded objects were already registered with this
    deployment_id as batches landed (registration_flusher). On --draft,
    unchanged objects stay on their prior (possibly published) deployment:
    staging must not take live data dark.

    Then stamps ``deployments.completed_at``. The row was opened up front, so
    until this lands a crashed deploy is indistinguishable from a finished one.
    A failed upload leaves it NULL: the deploy asked for a re-run, it did not
    finish.
    """
    if not deployment_id:
        return
    if push_plan is not None and not draft:
        skipped = {t.r2_key for t in push_plan.unchanged}
        if skipped:
            from campfire.deploy.registry import set_active_deployment
            set_active_deployment(sb, sorted(skipped), deployment_id)
    if not failed:
        mark_deployment_completed(sb, deployment_id)


def deploy_observation(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
    supabase_only: bool = False,
    force_overwrite: bool = False,
    include_shutters: bool = True,
    include_photometry: bool = True,
    skip_astrometry: bool = False,
    source_ids: list[int] | None = None,
    auto_approve: bool = False,
    defer_rebuild: bool = False,
    draft: bool = False,
    steal_lease: bool = False,
) -> dict:
    """Full deployment of one NIRSpec observation: plan, confirm, execute.

    The plan (:class:`~campfire.deploy.deploy_plan.NirspecDeployPlan`) is
    built first, from the summary ECSV and the products on disk plus a
    read-only check against the database and the storage registry, and
    printed. ``dry_run`` stops there; otherwise one confirmation (skipped by
    *auto_approve*) and the plan is executed as printed.

    *steal_lease* (data releases P1) takes the deploy lease over from a live
    deploy of the same observation instead of refusing (``--steal-lease``).

    When *defer_rebuild* is True, the per-field objects reconciliation
    (and materialized-view refresh) is skipped so the caller can batch
    them after processing multiple observations of the same field.

    When *draft* is True (epic #210, B2), the deployed spectra land
    ``deploy_status='draft'`` (admin-only, invisible to users) and the
    deployment record is marked ``draft`` — a draft an admin reviews and
    publishes via the admin UI. Requires the B1 (#217) lifecycle to be applied
    to the target database; checked up front via ``get_lifecycle_status`` so the
    deploy aborts cleanly rather than silently shipping unguarded drafts.

    Returns a dict with ``field`` and ``needs_reconcile`` (True for a completed
    deploy; False for a dry run or an aborted one).
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
            source_ids=source_ids, steal_lease=steal_lease)

    if source_ids:
        total = len(summary)
        summary = filter_by_source_ids(summary, source_ids)
        print(f"Filtered {total} spectra to {len(summary)} matching {len(source_ids)} source IDs")

    # The summary is the catalog's source for file_hash / science_hash; a final
    # reprocessed in place since it was written would deploy a catalog row the
    # freeze later refuses (#665). Checked before any work, dry run included.
    stale = stale_summary_rows(summary, obs_dir)
    if stale:
        print(format_stale_summary(obs_name, stale))
        sys.exit(1)

    # Validate the program slug resolves to a known program before doing any
    # work (including dry runs). A slug/name mix-up in observations.toml is
    # otherwise baked into the ECSV and only surfaces downstream as a cryptic
    # Supabase RLS error.
    field = get_field(summary)
    programs_config = load_programs()
    validate_program_slug(get_program_slug(summary), programs_config, obs_name)

    sb = get_supabase_client(config)

    # B2 capability gate: `--draft` requires the B1 (#217) lifecycle to be live
    # on the TARGET database, or the drafts would not actually be hidden. A
    # read, so a dry run checks it too.
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

    temp_dir = new_deploy_temp_dir(obs_dir)
    try:
        plan = _build_nirspec_plan(
            obs_name, obs_dir, summary, temp_dir,
            supabase_only=supabase_only, force_overwrite=force_overwrite,
            include_shutters=include_shutters, include_photometry=include_photometry,
            skip_astrometry=skip_astrometry, source_ids=source_ids, draft=draft,
            defer_rebuild=defer_rebuild)
        store = None
        if not supabase_only:
            from campfire.deploy.push import open_reducer_store
            store = open_reducer_store()
        _check_plan_remote(sb, plan, store)

        print()
        print(render_nirspec_plan(plan, dry_run=dry_run))
        print()
        if dry_run:
            return {'field': field, 'needs_reconcile': False}
        # The one interactive gate: every prompt happens before the deployment
        # row exists and before any byte moves or the lease is taken.
        if not auto_approve:
            resp = input("Proceed? [y/N]: ")
            if resp.strip().lower() != 'y':
                print("Aborted.")
                return {'field': field, 'needs_reconcile': False}
        return _execute_nirspec_plan(
            plan, sb, store, config, summary, obs_dir, programs_config,
            source_ids=source_ids, skip_astrometry=skip_astrometry,
            steal_lease=steal_lease)
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


def _execute_nirspec_plan(
    plan: NirspecDeployPlan,
    sb,
    store,
    config: dict,
    summary,
    obs_dir: Path,
    programs_config: dict,
    *,
    source_ids: list[int] | None,
    skip_astrometry: bool,
    steal_lease: bool,
) -> dict:
    """Carry out a confirmed plan. Output is one short block per phase:
    deployment, upload, catalog, objects, then the closing summary."""
    obs_name, field = plan.obs_name, plan.field
    if plan.draft:
        # A draft deploy lands the whole observation admin-only: one spectra row
        # per (target, grating) cannot hold a live and a draft version at once.
        for rec in plan.spectra:
            rec['deploy_status'] = 'draft'

    # Program + observation rows first: the lease and the deployment reference them.
    upsert_programs(sb, [plan.program_slug], programs_config, quiet=True)
    obs_config = load_observations().get(obs_name, {})
    file_globs_raw = obs_config.get('files', [])
    file_globs = [file_globs_raw] if isinstance(file_globs_raw, str) else list(file_globs_raw)
    upsert_observation(
        sb, obs_name, plan.program_slug, int(summary['program_id'][0]), field,
        file_globs=file_globs or None,
        gratings=obs_config.get('gratings') or None,
        data_subdir=obs_config.get('data_subdir'),
        config_section=obs_config or None,
    )

    # Deploy lease (data releases P1): from here until the finalize CAS this
    # deploy is visible in deploy_scope_state (`campfire dr status`). Refuses
    # with DeployScopeBusy — surfaced by the CLI with --steal-lease as the way
    # out — when another deploy of this observation is live; a lease with no
    # heartbeat for 30 minutes is taken over as stale. begin() also returns the
    # B4 scope version, compare-and-set at finalize (lease.claim()). The
    # registration flusher heartbeats it; the CLI releases it if we exit
    # abnormally before the claim.
    user_id = get_user_id_from_token(config)
    lease = ScopeLease(sb, 'observation', obs_name, actor=user_id, steal=steal_lease)
    lease.begin()

    # The plan's registry check ran before the prompt and outside the lease;
    # re-run it under the lease so the upload set (and the expected hashes the
    # retain gate checks) is what the cloud holds now. Cheap: files the first
    # pass confirmed take the local mirror's stat fast path. This pass also
    # writes the legacy wcs_hash backfill a dry run must not.
    push_plan = None
    if not plan.supabase_only:
        from campfire.deploy.push import plan_remote_push
        push_plan, _ = plan_remote_push(sb, store, plan.upload_tasks, backend='osn',
                                        progress=False, apply_backfill=True)
        if plan.push is not None and (
                len(push_plan.to_upload), len(push_plan.unchanged)) != (
                len(plan.push.to_upload), len(plan.push.unchanged)):
            print(f"Note: the cloud registry changed since the plan was made; "
                  f"now {push_plan.summary()}")

    # The deployment row is opened before any upload, not after: it is the
    # provenance anchor every registered object hangs off, and the flusher tags
    # rows with it as each batch lands (so an interrupted deploy resumes with
    # its uploads already attached). It is finalized (latest_deployment_id, the
    # audit event) only once the deploy completes.
    deployment_id = insert_deployment(
        sb,
        observation=obs_name,
        deployed_by=user_id,
        cfpipe_version=summary.meta.get('cfpipe_version'),
        jwst_version=summary.meta.get('jwst_version'),
        crds_context=summary.meta.get('crds_context'),
        config_snapshot=_load_config_snapshot(obs_dir, obs_name),
        # What this reduction used (stuck list + tags, background overrides),
        # frozen on the deployment row; the live lists are synced below.
        **deployment_snapshot(resolve_reference_obs_dir(obs_name)),
        # Real reduction time (earliest CMPFRTIM across products), not the
        # summary-build wall-clock — so re-running `summary` on unchanged
        # pixels no longer advances reduced_at.
        reduced_at=summary.meta.get('reduced_at'),
        n_targets=len(plan.targets),
        n_spectra=len(plan.spectra),
        n_new_targets=plan.n_new_targets or 0,
        force_overwrite=plan.force_overwrite,
        source_ids_filter=source_ids,
        supabase_only=plan.supabase_only,
        status='draft' if plan.draft else 'published',
    )
    status_word = 'draft, admin-only' if plan.draft else 'published'
    if deployment_id:
        print(f"Deployment #{deployment_id} opened ({status_word})")

    # ---- Upload -------------------------------------------------------------
    success, failed = 0, 0
    up_bytes = 0
    if push_plan is not None:
        from campfire.deploy.push import registration_flusher
        flusher = registration_flusher(
            sb, store, push_plan, backend='osn', deployment_id=deployment_id,
            uploaded_by=user_id,
            cfpipe_version=summary.meta.get('cfpipe_version'),
            heartbeat=lease.heartbeat)
        if push_plan.to_upload:
            up_bytes = sum(path_size(t.local_path) for t in push_plan.to_upload)
            print(f"\nUploading {len(push_plan.to_upload):,} file(s), "
                  f"{format_bytes(up_bytes)} → OSN")
            success, failed, failed_msgs = upload_files_parallel(
                config, push_plan.to_upload, desc="OSN upload",
                backend='osn', on_success=flusher.add,
            )
            flusher.flush()
            if failed_msgs:
                print(f"  {failed} upload(s) failed:")
                for msg in failed_msgs[:10]:
                    print(f"    - {msg}")
                if len(failed_msgs) > 10:
                    print(f"    ... and {len(failed_msgs) - 10} more")
        else:
            print("\nNothing to upload: the cloud already holds every file")

    # ---- Catalog ------------------------------------------------------------
    print("\nWriting catalog...")
    n_obj, new_object_ids, _n_quality_reset = batch_upsert_objects(
        sb, plan.targets, field, plan.force_overwrite)
    if deployment_id and len(new_object_ids) != (plan.n_new_targets or 0):
        sb.table('deployments').update(
            {'n_new_targets': len(new_object_ids)}).eq('id', deployment_id).execute()
    n_spec, changed_hashes = batch_upsert_spectra(sb, plan.spectra)
    print(f"  {n_obj:,} targets ({len(new_object_ids):,} new), "
          f"{n_spec:,} spectra ({len(changed_hashes):,} with new content)")

    # Emission-line catalog rows for the spectra that carry a _lines.fits
    # product. Inspected-redshift fits only; fits made at the auto redshift are
    # refused here (campfire deploy lines --allow-auto-z is the explicit path).
    if plan.lines_paths:
        publish_line_fits(sb, obs_name, plan.lines_paths, program_slug=plan.program_slug)

    recompute_target_aggregates(sb, [t['object_id'] for t in plan.targets])

    parts = []
    if plan.pointings is not None:
        update_observation_pointings(sb, obs_name, plan.pointings)
        parts.append(f"{len(plan.pointings):,} MSA pointings")
    n_shutters = 0
    if plan.shutters is not None:
        astrometry_note = plan.astrometry
        if not skip_astrometry:
            imaging_path = resolve_imaging_config()
            if imaging_path is not None:
                import tomllib
                from campfire.deploy.astrometry import correct_shutter_positions
                with open(imaging_path, 'rb') as f:
                    imaging_config = tomllib.load(f)
                n_corrected, n_matches = correct_shutter_positions(
                    plan.shutters, obs_dir, obs_name, field, imaging_config)
                astrometry_note = (
                    f"{n_corrected:,} positions corrected ({n_matches:,} catalog matches)"
                    if n_corrected else 'raw MSA positions (astrometry fit skipped)')
        n_shutters = db_deploy_shutters(sb, obs_name, plan.shutters)
        parts.append(f"{n_shutters:,} MSA shutters, {astrometry_note}")
    if parts:
        print(f"  {', '.join(parts)}")

    # Review-surface rows (they feed the admin review views). Split-ownership
    # upserts, so a re-deploy refreshes render columns without clobbering web
    # review state.
    review = []
    if plan.rate_files:
        from campfire.deploy.rate_exposures import (
            build_rate_exposure_records, upsert_rate_exposures,
        )
        n_rate = upsert_rate_exposures(sb, build_rate_exposure_records(
            plan.rate_files, observation=obs_name, scope=Scope(obs=obs_name)))
        review.append(f"{n_rate:,} rate-mask triage")
    if plan.exposure_files:
        from campfire.deploy.spectrum_grid import (
            build_spectrum_exposure_records, upsert_spectrum_exposures,
        )
        n_grid = upsert_spectrum_exposures(sb, build_spectrum_exposure_records(
            plan.exposure_files, observation=obs_name, scope=Scope(obs=obs_name)))
        review.append(f"{n_grid:,} exposure grid")
    # The stuck-shutter / background-override lists this reduction used go to the
    # cloud (nirspec_source_review) with the deploy, under the config plane's
    # three-way rule: additions and edits are sent; an entry the cloud changed, and
    # any removal, is left for an explicit `campfire config push / pull`.
    flag_notes = []
    try:
        from campfire.deploy.nirspec_flags import push_on_deploy
        flags = push_on_deploy(sb, obs_name, config)
        if flags['pushed']:
            review.append(f"{flags['pushed']:,} stuck-shutter / bkg-override "
                          f"entr{'y' if flags['pushed'] == 1 else 'ies'} pushed")
        flag_notes = flags['lines']
    except Exception as e:   # never fail a deploy over the lists
        flag_notes = [f"  ! stuck-shutter / bkg-override lists not synced: {e}",
                      f"    run `campfire config push --obs {obs_name}`"]
    if review:
        print(f"  review rows: {', '.join(review)}")
    if flag_notes:
        print("  stuck-shutter / bkg-override lists differ from the cloud:")
        for line in flag_notes:
            print(line)

    # ---- Objects ------------------------------------------------------------
    # Incremental object reconciliation: preserves inspection state on existing
    # objects, inserts only when new clusters appear, soft-deletes orphans, and
    # aborts hard on a split/merge (resolve with `campfire deploy objects
    # reconcile`). Deferred to the multi-obs caller under --defer-rebuild so a
    # field is not reconciled once per observation.
    objects_line = None
    caches_ok = True
    if plan.defer_rebuild:
        print(f"\nObjects: field {field} is reconciled once, after the last observation")
    else:
        from campfire.deploy.reconcile import reconcile_field_objects
        print(f"\nReconciling objects in field {field}...")
        n_clusters, rstats, changed_ids = reconcile_field_objects(
            sb, field, changed_hashes=changed_hashes, abort_on_split_merge=True,
            verbose=False)
        counts = [f"{rstats.get('updated', 0):,} updated",
                  f"{rstats.get('inserted', 0):,} new"]
        for key, word in (('revived', 'revived'), ('soft_deleted', 'orphaned')):
            if rstats.get(key):
                counts.append(f"{rstats[key]:,} {word}")
        objects_line = f"{', '.join(counts)} (of {n_clusters:,} in {field})"
        print(f"  {objects_line}")
        if rstats.get('redshift_auto_set'):
            print(f"  redshift_auto recomputed for {rstats['redshift_auto_set']:,}")

        # Photometry for the objects the reconcile touched; silent when the
        # field has no photometry config or nothing changed.
        if plan.photometry.startswith('cross-match') and changed_ids:
            from campfire.deploy.config import resolve_photometry_config
            from campfire.deploy.photometry import deploy_field_photometry
            print(f"\nDeploying photometry for {len(changed_ids)} changed objects...")
            deploy_field_photometry(
                sb, field, resolve_photometry_config(None), config,
                restrict_to_object_db_ids=changed_ids,
            )

        # AFTER the photometry upsert, not before: mv_filter_options carries
        # photometry_bands, so a refresh that runs first publishes a band list
        # that predates the rows just written.
        caches_ok &= refresh_filter_options(sb, quiet=True)
        caches_ok &= refresh_programs_overview(sb, quiet=True)

    # ---- Finalize -----------------------------------------------------------
    if deployment_id:
        update_latest_deployment(sb, obs_name, deployment_id)
        # B2: record the deploy in the lifecycle audit log. `success`/`failed`
        # are the actual upload tallies; `affected_count` stays the spectra
        # count for continuity.
        log_deploy_event(
            sb,
            action='upload',
            actor=user_id,
            deployment_id=deployment_id,
            observation=obs_name,
            affected_count=len(plan.spectra),
            metadata=deploy_event_metadata(
                'nirspec', observation=obs_name, planned=len(plan.upload_tasks),
                succeeded=success, failed=failed, items=len(plan.spectra),
                draft=plan.draft, n_objects=len(plan.targets)),
        )

    # Observation overview snapshot (perf T1-5 / #501): after the observation
    # row (pointings), targets/spectra and the deployment record are final.
    # Batch deploys refresh once at the end instead.
    if not plan.defer_rebuild:
        caches_ok &= refresh_observations_overview(sb, quiet=True)
        if caches_ok:
            print("  portal caches refreshed")

    # Re-point unchanged dedup-skipped objects at this deployment — only on a
    # PUBLISHED deploy, so deployment-gated intermediates ride the current
    # deployment's visibility. Uploaded objects were already registered with
    # this deployment_id as batches landed. On --draft, unchanged objects stay
    # on their prior (possibly published) deployment — staging must not take
    # live data dark.
    #
    # Then the completion marker (deployments.completed_at), the last write of
    # the deploy: skipped when an upload failed, since the deploy asks for a re-run.
    _finish_nirspec_deployment(sb, deployment_id, push_plan,
                               draft=plan.draft, failed=failed)

    # B4 multi-reducer safety: compare-and-set the scope version. A conflict
    # means another reducer deployed this same observation while we were
    # running — surface it so the operator can re-check (the writes are
    # idempotent by key, but a concurrent re-reduction may have interleaved).
    claim = lease.claim()
    if claim.get('conflict'):
        print()
        print(f"⚠  CONCURRENT DEPLOY DETECTED for {obs_name}: another deploy "
              f"advanced this observation (version {lease.version_at_start} -> "
              f"{claim.get('current')}) while this one was running.")
        print("   Both deploys' writes are present; re-check the result and "
              "re-deploy if a re-reduction was clobbered.")

    # Closing summary: the stage output above scrolls away; this block states
    # what actually happened, in one place.
    print()
    print(f"Deploy complete: {obs_name}"
          + (f" → deployment #{deployment_id} ({status_word})" if deployment_id else ''))
    if plan.supabase_only:
        print("  files:    none (--supabase-only)")
    elif push_plan is not None:
        print(f"  files:    {success:,} uploaded ({format_bytes(up_bytes)}), "
              f"{len(push_plan.unchanged):,} already in the cloud"
              + (f", {failed:,} FAILED (re-run to retry)" if failed else ''))
    catalog = [f"{len(plan.targets):,} targets ({len(new_object_ids):,} new)",
               f"{len(plan.spectra):,} spectra"]
    if plan.pointings is not None:
        catalog.append(f"{len(plan.pointings):,} pointings")
    if n_shutters:
        catalog.append(f"{n_shutters:,} shutters")
    print(f"  catalog:  {', '.join(catalog)}")
    if objects_line:
        print(f"  objects:  {objects_line}")

    # Any completed deploy potentially affected member spectra or membership;
    # the deferred multi-obs path always wants to reconcile.
    return {'field': field, 'needs_reconcile': True}


# ---------------------------------------------------------------------------
# Standalone subcommand handlers
# ---------------------------------------------------------------------------

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
            print(f"  {path.name} -> {storage_key('spectrum_json', Scope(obs=obs_name), f'{path.stem}.json', scheme=KeyScheme.CANONICAL)} (+ _1d sidecar)")
        if len(spec_paths) > 5:
            print(f"  ... and {len(spec_paths) - 5} more")
        return

    temp_dir = new_deploy_temp_dir(obs_dir)

    try:
        print("Generating JSON files...")
        scope = Scope(obs=obs_name)
        tasks = []
        for path in tqdm(spec_paths, desc="Generating", unit="file"):
            json_path, json_1d_path = generate_spectrum_jsons(path, temp_dir)
            tasks.append(UploadTask(json_path, storage_key('spectrum_json', scope, json_path.name, scheme=KeyScheme.CANONICAL), 'application/json'))
            tasks.append(UploadTask(json_1d_path, storage_key('spectrum_1d_json', scope, json_1d_path.name, scheme=KeyScheme.CANONICAL), 'application/json'))

        print("Uploading to OSN...")
        uploaded_keys: set[str] = set()
        success, failed, failed_msgs = upload_files_parallel(
            config, tasks, desc="JSON files", succeeded_out=uploaded_keys, backend='osn')

        if failed_msgs:
            print(f"\n  {failed} failed:")
            for msg in failed_msgs[:5]:
                print(f"    - {msg}")

        print(f"Uploaded {success}/{len(tasks)} JSON files ({len(spec_paths)} spectra, full + 1-D sidecar)")

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

    # Confirm the destructive part before anything lands: once the sidecars
    # are uploaded and the spectra scalars rewritten, "Aborted" would be a
    # lie (the fit is already live; only the object reset would be skipped).
    sb = get_supabase_client(config)
    if force_overwrite:
        existing = check_existing_objects(sb, [o['object_id'] for o in objects])
        if existing and not auto_approve:
            print(f"  {len(existing)} objects exist. FORCE OVERWRITE will reset inspection data!")
            resp = input("  Are you sure? [y/N]: ")
            if resp.lower() != 'y':
                print("Aborted.")
                return

    # Upload zfit JSONs
    temp_dir = new_deploy_temp_dir(obs_dir)

    try:
        print("Generating zfit JSON files...")
        scope = Scope(obs=obs_name)
        tasks = []
        zfit_results_by_key: dict[str, tuple[str, dict]] = {}
        for path in zfit_paths:
            json_path = generate_zfit_json(path, temp_dir)
            key = storage_key('zfit', scope, json_path.name, scheme=KeyScheme.CANONICAL)
            tasks.append(UploadTask(json_path, key, 'application/json'))
            with open(json_path) as f:
                zfit_results_by_key[key] = (path.name, json.load(f))

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
            n_reg = upsert_storage_objects(sb, reg_rows)
            print(f"Registered {n_reg} storage objects")
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

    # Update redshift_auto in Supabase.
    # Keep the row-backed fit values in lockstep with the exact JSON artifacts
    # that landed. RedshiftFitSummary trusts these columns when present, so a
    # standalone re-fit must replace them too. Failed uploads are deliberately
    # excluded: their old artifact and old scalar row remain a coherent pair.
    uploaded_results = {
        filename: result
        for key, (filename, result) in zfit_results_by_key.items()
        if key in uploaded_keys
    }
    scalar_updates = get_zfit_scalar_updates(summary, uploaded_results)
    n_scalars = update_spectra_zfit_scalars(sb, scalar_updates)
    print(f"  Updated zfit scalars for {n_scalars} spectra")

    print("Updating objects...")
    n, _, _ = batch_upsert_objects(sb, objects, field, force_overwrite)
    print(f"  Updated {n} objects")

    refresh_filter_options(sb)
    refresh_programs_overview(sb)
    refresh_observations_overview(sb)
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

            # The summary row for this file names the spectrum: (target_id,
            # grating, filter), the filter from the file name
            # ({obs_name}_{grating}_{filter}_{source_id}_spec.fits) when the
            # summary predates the column.
            for row in summary:
                if row['spec_file'] == spec_path.name:
                    filt = summary_row_filter(summary, row)
                    if not filt:
                        raise ValueError('no NIRSpec filter in the summary or the file name')
                    sb.table('spectra').update(thumbs).eq(
                        'target_id', row['object_id']
                    ).eq('grating', row['grating']).eq(
                        'filter', filt
                    ).execute()
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
