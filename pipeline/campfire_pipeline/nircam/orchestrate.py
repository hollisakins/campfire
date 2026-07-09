"""
Orchestrators for the canonical-exposure NIRCam pipeline.

Two phase-level entry points (``run_process``, ``run_combine``) and one
single-step dispatcher (``run_step``). Both phases iterate over the
field's filters; within a filter, the per-exposure steps run via
``common.parallel.dispatch`` (with ``n_processes`` workers), the
per-filter ensemble steps (persistence, build_bad_pixel_masks) run
serially, and the per-visit ensemble step (outlier) dispatches one
visit per worker via the same ``dispatch`` helper.

The legacy ``stage1.py`` / ``stage2.py`` / ``stage3.py`` orchestrators
remain in place for now but are not invoked from the new CLI.
"""

import functools
import os
import warnings
import hashlib
from importlib import import_module

from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.common.parallel import dispatch
from campfire_pipeline.config import get_nircam_step_config

from campfire_pipeline.nircam.status import StepStatus

# Step worker functions are imported lazily (see ``_load_step`` and the
# per-runner local imports below), NOT at module top. Each step module pulls
# in heavy scientific deps — photutils.segmentation via wisp/striping
# (~140s to import on cluster NFS), matplotlib via outlier, the jwst/crds
# stack via detector1/image2/jhat. Importing ``orchestrate`` is what every
# ``cfpipe`` invocation does (the NIRCam CLI imports it at module top), so an
# eager step import made *every* command — including ``--help`` and a
# ``combine`` run that never touches the process-phase steps — pay for all of
# them. Deferring the import to the moment a phase actually dispatches a step
# keeps startup proportional to the work being done.


# Step ordering — also used by the CLI to validate ``cfpipe nircam <step>``
# names. Each entry is (step_name, cfp_key_or_None). ``cfp_key`` is None for
# resample (mosaic outputs are stamped with CMPFRVER, not CFP_*).
PROCESS_STEPS = [
    ('detector1',   'CFP_DET1'),
    ('persistence', 'CFP_PERS'),
    ('wisp',        'CFP_WISP'),
    ('image2',      'CFP_IMG2'),
    ('edge',        'CFP_EDGE'),
    # bkg replaces the former striping (CFP_1F) + sky (CFP_SKY) + variance
    # (CFP_VAR) steps; edge now runs before it so edge DQ feeds the mask.
    ('bkg',         'CFP_BKG'),
    ('diag_striping', 'CFP_DIAG'),
    ('wcs_shift',   'CFP_SHFT'),
    ('preview',     'CFP_PREV'),
    ('jhat',        'CFP_JHAT'),
]

COMBINE_STEPS = [
    ('apply_mask', 'CFP_MASK'),
    ('bad_pixel',  'CFP_BPIX'),
    ('outlier',    'CFP_OUT'),
    ('resample',   None),
]

ALL_STEPS = PROCESS_STEPS + COMBINE_STEPS
# 'align' is not a static PROCESS_STEP (it is substituted for 'jhat' at runtime
# by _active_process_steps for align-enabled fields), but it is a valid target
# for `run_step` / the `align` CLI command, so it joins the known step names.
STEP_NAMES = [name for name, _ in ALL_STEPS] + ['align']
# Ordered (step, cfp_key) list that includes BOTH `jhat` and `align` (mutually
# exclusive per field) at the same process position, for the CLI `status`
# columns and `reset --from <step>` key lookup. Clearing an absent key is a
# no-op, so listing both is safe; align sits right after jhat (its runtime slot).
CFP_STEPS = PROCESS_STEPS + [('align', 'CFP_ALGN')] + COMBINE_STEPS

# Combine steps that read/write the disposable working copies rather than the
# frozen canonical (apply_mask is excluded — it writes the canonical's CFMASK).
# Running any of these standalone via ``run_step`` must materialize the work
# tree first.
_COMBINE_WORK_STEPS = {'bad_pixel', 'outlier', 'resample'}

# Steps that hit CRDS — used by run_step() to decide when to pre-fetch
# reference files before parallel dispatch. striping now runs *after* image2
# (on flat-fielded, flux-calibrated cal-stage data) so it no longer resolves a
# flat itself; wisp still runs in the rate frame and resolves its own flat.
_CRDS_STEPS = {'detector1', 'wisp', 'image2'}


def _detector_sorted(paths):
    """Order exposure paths detector-major (stable within detector).

    Tasks are independent, so dispatch order is free to choose — but
    ``Pool.map`` hands each worker a contiguous chunk, so detector-major
    order makes a worker's chunk mostly single-detector. Per-worker
    reference caches (wisp templates, flats, bad-pixel masks — all keyed
    per detector) then hit instead of thrash.
    """
    def key(p):
        base = os.path.basename(p)
        parts = base.removesuffix('.fits').split('_')
        # Detector is the 4th underscore field in both canonical and uncal
        # names (jw..._<visitgrp>_<expnum>_<detector>[_uncal].fits) — same
        # token the step modules themselves parse.
        det = parts[3] if len(parts) > 3 else ''
        return (det, base)
    return sorted(paths, key=key)

# Per-exposure steps dispatched through the generic ``_run_per_exposure``
# helper: step_name -> (step module basename, worker callable, CFP key). The
# module is imported lazily inside the runner so ``orchestrate`` import never
# drags in a step the current phase won't run.
_PER_EXPOSURE_STEPS = {
    'wisp':       ('wisp',        'wisp_step',        'CFP_WISP'),
    'image2':     ('image2',      'image2_step',      'CFP_IMG2'),
    'edge':       ('edge',        'edge_step',        'CFP_EDGE'),
    'bkg':        ('bkg',         'bkg_step',         'CFP_BKG'),
    'preview':    ('preview',     'preview_step',     'CFP_PREV'),
    'jhat':       ('jhat',        'jhat_step',        'CFP_JHAT'),
    'apply_mask': ('apply_masks', 'apply_masks_step', 'CFP_MASK'),
}


def _load_step(module_basename, func_name):
    """Import and return a step's worker callable on demand.

    Kept tiny and explicit so the lazy-import intent is obvious at each call
    site — see the module docstring for why step modules are not imported at
    the top of ``orchestrate``.
    """
    module = import_module(f'campfire_pipeline.nircam.steps.{module_basename}')
    return getattr(module, func_name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _group_by_visit(exposure_files):
    """Return ``{visit: [paths...]}`` keyed on the leading ``jw...`` token."""
    visits = {}
    for f in exposure_files:
        visit = os.path.basename(f).split('_')[0]
        visits.setdefault(visit, []).append(f)
    return visits


def _visit_membership_matches(manifest, visit, visit_files):
    """True iff an outlier *manifest* recorded exactly the current *visit_files*
    as this visit's own inputs.

    Catches a member dropped from the visit (a NOT_ALIGNED quarantine, a new
    skip/exclusion, a tile re-scope) that the per-file hash check alone would
    miss — the survivors' hashes still match the larger manifest, so outlier
    would be skipped and resample would reuse CR masks computed with the
    now-absent exposure still pooled. A manifest input belongs to this visit iff
    its filename's leading ``jw...`` token is the visit; cross-visit overlap
    inputs (a different token) are excluded here and validated by
    ``outlier_step``'s full input-set check on the slow path.
    """
    manifest_this_visit = {inp['filename'] for inp in manifest.get('inputs', [])
                           if inp['filename'].split('_')[0] == visit}
    return manifest_this_visit == {os.path.basename(f) for f in visit_files}


def _read_sregions(exposure_files):
    """Return S_REGION header strings parallel to ``exposure_files``."""
    sregions = []
    for f in exposure_files:
        with fits.open(f, memmap=False) as hdul:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                sregions.append(hdul[1].header['S_REGION'])
    return sregions


# ---------------------------------------------------------------------------
# Per-step runners (used by run_process / run_combine / run_step)
# ---------------------------------------------------------------------------

def _filter_pending(step_name, exposures, cfp_key, status, overwrite):
    """Drop exposures that already carry ``cfp_key`` per the cache.

    Returns ``(pending, skipped_count)``. Logs a single summary line if
    anything was filtered out.
    """
    if overwrite:
        return list(exposures), 0
    pending = [f for f in exposures if not status.has(f, cfp_key)]
    skipped = len(exposures) - len(pending)
    if skipped:
        log(f"{step_name}: {skipped}/{len(exposures)} already have "
            f"{cfp_key}; skipping those")
    return pending, skipped


_GRISM_EXP_TYPES = ('NRC_WFSS', 'NRC_TSGRISM')


def _filter_imaging_uncals(uncals, step_name):
    """Drop NIRCam grism uncals before they enter the imaging pipeline.

    Image2Pipeline's photom step matches imaging on (filter, pupil), but the
    NIRCam phot_table has multiple rows per (filter, pupil) for WFSS — one
    per spectral order — so a grism exposure routed through the imaging
    branch raises ``MatchFitsTableRowError``. Defense in depth: the download
    filter is the primary gate; this catches anything that slips through.
    """
    keep = []
    skipped = 0
    for u in uncals:
        try:
            exp_type = fits.getval(u, 'EXP_TYPE', ext=0)
        except (OSError, KeyError):
            # Unreadable / missing keyword: keep, let the step fail loudly
            keep.append(u)
            continue
        if exp_type in _GRISM_EXP_TYPES:
            skipped += 1
        else:
            keep.append(u)
    if skipped:
        log(f"{step_name}: skipping {skipped} grism exposure(s) "
            f"(EXP_TYPE in {_GRISM_EXP_TYPES}); imaging pipeline only")
    return keep


def _run_detector1(field, config, filtname, n_processes, overwrite, status,
                   tiles=None):
    uncals = field.get_uncal_files(filtname, tiles=tiles)
    if not uncals:
        log(f"detector1: no uncal files for {filtname}")
        return

    uncals = _filter_imaging_uncals(uncals, 'detector1')
    if not uncals:
        log(f"detector1: no imaging uncals for {filtname} after grism filter")
        return

    if not overwrite:
        pending = []
        for u in uncals:
            rootname = os.path.basename(u).removesuffix('_uncal.fits')
            canonical = field.get_exposure_path(rootname, filtname)
            if os.path.exists(canonical) and status.has(canonical, 'CFP_DET1'):
                continue
            pending.append(u)
        skipped = len(uncals) - len(pending)
        if skipped:
            log(f"detector1: {skipped}/{len(uncals)} canonicals already have "
                f"CFP_DET1; skipping those")
    else:
        pending = list(uncals)

    if not pending:
        return

    from campfire_pipeline.nircam.steps.detector1 import detector1_step
    cfg = get_nircam_step_config('detector1', config, field)
    pending = _detector_sorted(pending)
    log(f"detector1: dispatching {len(pending)} files for {filtname}")
    dispatch(detector1_step, pending, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite,
             status=status, reduction_version=_resolve_reduction_version(config))
    new_canonical = [
        field.get_exposure_path(
            os.path.basename(u).removesuffix('_uncal.fits'), filtname,
        )
        for u in pending
    ]
    status.add_paths(new_canonical)
    status.mark_all(
        [c for c in new_canonical if os.path.exists(c)], 'CFP_DET1',
    )


def _run_persistence(field, config, filtname, n_processes, overwrite, status,
                     tiles=None):
    exposures = field.get_exposure_files(filtname, tiles=tiles)
    if not exposures:
        log(f"persistence: no exposures for {filtname}")
        return
    # Persistence is an ensemble step — it re-runs the whole batch unless every
    # member is already done. So we only short-circuit the "all done" case.
    if not overwrite and all(status.has(f, 'CFP_PERS') for f in exposures):
        log(f"persistence: CFP_PERS already set on all {len(exposures)} "
            f"exposures for {filtname}; skipping")
        return
    from campfire_pipeline.nircam.steps.persistence import persistence_step
    cfg = get_nircam_step_config('persistence', config, field)
    persistence_step(exposures, field, cfg, overwrite=overwrite, status=status)
    status.mark_all(exposures, 'CFP_PERS')


def _run_per_exposure(step_name, field, config, filtname,
                      n_processes, overwrite, status, tiles=None, epoch=None):
    """Generic per-exposure parallel dispatch.

    Filters out already-stamped exposures *before* spinning up the worker
    pool — a no-op pass on a finished field skips the Pool entirely. The
    step's worker callable is imported lazily here (after the early-out
    checks), so a no-op pass doesn't import the step's heavy deps at all.

    ``epoch`` restricts to the named epoch's exposure subset (used by the
    combine-phase ``apply_mask`` step); process-phase steps pass ``None``.
    """
    module_basename, func_name, cfp_key = _PER_EXPOSURE_STEPS[step_name]
    exposures = field.get_exposure_files(filtname, tiles=tiles, epoch=epoch)
    if not exposures:
        log(f"{step_name}: no exposures for {filtname}")
        return
    pending, _ = _filter_pending(step_name, exposures, cfp_key, status,
                                 overwrite)
    if not pending:
        return
    fn = _load_step(module_basename, func_name)
    cfg = get_nircam_step_config(step_name, config, field)
    pending = _detector_sorted(pending)
    log(f"{step_name}: dispatching {len(pending)} exposures for {filtname}")
    dispatch(fn, pending, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite,
             status=status)
    status.mark_all(pending, cfp_key)


def _run_diag_striping(field, config, filtname, n_processes, overwrite, status,
                       tiles=None):
    """Opt-in scattered-light diagonal striping. Disabled unless a field
    sets ``[field.diag_striping].enabled = true``."""
    cfg = get_nircam_step_config('diag_striping', config, field)
    if not cfg.get('enabled', False):
        log(f"diag_striping: disabled by config; skipping {filtname}")
        return
    exposures = field.get_exposure_files(filtname, tiles=tiles)
    if not exposures:
        log(f"diag_striping: no exposures for {filtname}")
        return
    pending, _ = _filter_pending('diag_striping', exposures, 'CFP_DIAG',
                                 status, overwrite)
    if not pending:
        return
    from campfire_pipeline.nircam.steps.diag_striping import diag_striping_step
    log(f"diag_striping: dispatching {len(pending)} exposures for {filtname}")
    dispatch(diag_striping_step, pending, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite,
             status=status)
    status.mark_all(pending, 'CFP_DIAG')


def _run_wcs_shift(field, config, filtname, n_processes, overwrite, status,
                   tiles=None):
    """Opt-in pre-JHAT astrometric shift. No-op unless ``[[<field>.wcs_shift]]``
    rules are defined in fields.toml."""
    rules = field.wcs_shift_rules
    if not rules:
        log(f"wcs_shift: no rules; skipping {filtname}")
        return
    exposures = field.get_exposure_files(filtname, tiles=tiles)
    if not exposures:
        log(f"wcs_shift: no exposures for {filtname}")
        return

    from campfire_pipeline.nircam.steps.wcs_shift import (
        wcs_shift_step, _match_rule,
    )
    # Pre-filter to exposures actually matched by some rule. Saves I/O on
    # the (typical) majority of files that no rule touches — they're never
    # stamped, so _filter_pending wouldn't catch them.
    matched = []
    for f in exposures:
        rootname = os.path.basename(f).removesuffix('.fits')
        if _match_rule(rootname, filtname, rules) is not None:
            matched.append(f)
    if not matched:
        log(f"wcs_shift: no exposures match any rule for {filtname}")
        return

    pending, _ = _filter_pending('wcs_shift', matched, 'CFP_SHFT', status,
                                 overwrite)
    if not pending:
        return
    cfg = dict(get_nircam_step_config('wcs_shift', config, field))
    cfg['rules'] = rules
    log(f"wcs_shift: dispatching {len(pending)} exposures for {filtname}")
    dispatch(wcs_shift_step, pending, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite,
             status=status)
    # Rescan (not mark_all): the workers stamped CFP_SHFT but also SCRUBBED any
    # stale CFP_JHAT/CFP_ALGN (the WCS they described no longer exists), and
    # mark_all can only add keys — a same-run jhat/align skip check must see
    # the scrub, not the phase-start snapshot.
    status.rescan(pending)


def _run_bad_pixel(field, config, filtname, n_processes, overwrite, status,
                   tiles=None, epoch=None):
    # Combine phase: operate on the working copies, never the frozen canonical.
    exposures = field.get_exposure_files(filtname, work=True, tiles=tiles,
                                         epoch=epoch)
    if not exposures:
        log(f"bad_pixel: no exposures for {filtname}")
        return
    cfg = get_nircam_step_config('bad_pixel', config, field)
    # Opt-in step: only useful in the many-exposure regime where the empirical
    # DO_NOT_USE rate beats the CRDS prior. Disabled by default; enable per
    # field via [nircam.bad_pixel].enabled = true.
    if not cfg.get('enabled', False):
        log(f"bad_pixel: disabled by config; skipping {filtname}")
        return
    from campfire_pipeline.nircam.steps.bad_pixel import (
        build_bad_pixel_masks, bad_pixel_step,
    )
    # Ensemble: build per-detector masks once (no CFP key — it's a reference
    # product, not a per-exposure mutation). Cheap to call when up-to-date,
    # but we still skip when --overwrite is off and all reference products
    # exist (handled inside build_bad_pixel_masks).
    build_bad_pixel_masks(filtname, exposures, field, cfg, overwrite=overwrite)
    # Per-exposure: OR the masks into each exposure's DQ
    pending, _ = _filter_pending('bad_pixel', exposures, 'CFP_BPIX', status,
                                 overwrite)
    if not pending:
        return
    pending = _detector_sorted(pending)
    log(f"bad_pixel: dispatching {len(pending)} exposures for {filtname}")
    dispatch(bad_pixel_step, pending, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite,
             status=status)
    status.mark_all(pending, 'CFP_BPIX')


def _run_outlier(field, config, filtname, n_processes, overwrite, status,
                 tiles=None, epoch=None):
    cfg = get_nircam_step_config('outlier', config, field)
    implementation = cfg.get('implementation', 'jwst')
    if implementation not in ('jwst', 'campfire'):
        raise ValueError(
            f"Unknown outlier.implementation {implementation!r}; "
            f"expected 'jwst' or 'campfire'"
        )
    _run_outlier_per_visit(field, cfg, filtname, n_processes, overwrite, status,
                           implementation=implementation, tiles=tiles,
                           epoch=epoch)


def _run_outlier_per_visit(field, cfg, filtname, n_processes, overwrite, status,
                           implementation='jwst', tiles=None, epoch=None):
    """Per-visit outlier dispatcher.

    Both implementations share the same orchestration (visit grouping,
    manifest staleness pre-scan, CFP_OUT stamping). They differ only in
    the per-visit drizzle/median/blot routine:

    - ``implementation='jwst'`` → ``outlier_step``: ``Image3Pipeline``
      with stcal Resample, classic per-visit ASN flow.
    - ``implementation='campfire'`` → ``outlier_step_campfire``: builds
      a per-visit intermediate WCS via ``wcs_from_sregions`` and runs
      campfire's bbox-sliced drizzle primitive + ``MedianComputer``.

    Parallelization
    ---------------
    Visits are dispatched in parallel across ``n_processes`` workers.
    Each visit writes only to its own working copies (via ``atomic_save``)
    while reading other visits' working copies as cross-visit overlap padding.
    The frozen canonical exposures are never touched (see
    ``Field.materialize_work``). Because reads/writes are atomic and
    outlier_detection only ADDS DQ bits (SCI is unchanged), parallel runs
    cannot crash; the only
    observable difference vs. serial is that a worker may read an overlap
    file's DQ before the visit owning that file has stamped its new
    outlier bits, producing a small median bias in those overlap pixels.
    Intra-program scoping (the default) keeps overlap small. Set
    ``--processes 1`` for a strictly sequential, ordering-stable run.
    """
    from campfire_pipeline.nircam.steps.outlier import (
        outlier_step, outlier_step_campfire,
    )
    visit_step = (
        outlier_step_campfire if implementation == 'campfire' else outlier_step
    )

    # Combine phase: operate on the working copies, never the frozen canonical.
    exposures = field.get_exposure_files(filtname, work=True, tiles=tiles,
                                         epoch=epoch)
    if not exposures:
        log(f"outlier: no exposures for {filtname}")
        return
    visits = _group_by_visit(exposures)

    # Pre-filter visits whose members all carry CFP_OUT *AND* whose manifest
    # is unchanged (cheap check); fall back to outlier_step for the rest.
    # The CFP_OUT-only short-circuit avoids the polygon-overlap setup work
    # done at the top of outlier_step on no-op runs.
    from campfire_pipeline.nircam.manifest import (
        compute_file_hash, load_manifest,
    )

    def _visit_up_to_date(visit, visit_files):
        if not all(status.has(f, 'CFP_OUT') for f in visit_files):
            return False
        manifest_path = os.path.join(
            field.filter_dir(filtname), f'outlier_{visit}_manifest.json',
        )
        manifest = load_manifest(manifest_path)
        if manifest is None:
            return False
        # A member dropped from this visit (e.g. a NOT_ALIGNED quarantine) must
        # force outlier to re-run, else resample reuses CR masks computed with
        # the now-absent exposure still pooled (see _visit_membership_matches).
        if not _visit_membership_matches(manifest, visit, visit_files):
            return False
        # Check that visit_files (a subset of all_inputs) hashes still match.
        old_hashes = {
            inp['filename']: inp['file_hash']
            for inp in manifest['inputs']
        }
        for f in visit_files:
            bn = os.path.basename(f)
            if bn not in old_hashes:
                return False
            if compute_file_hash(f) != old_hashes[bn]:
                return False
        return True

    pending_visits = {}
    for visit, visit_files in visits.items():
        if not overwrite and _visit_up_to_date(visit, visit_files):
            continue
        pending_visits[visit] = visit_files
    skipped = len(visits) - len(pending_visits)
    if skipped:
        log(f"outlier: {skipped}/{len(visits)} visits already up-to-date "
            f"for {filtname}; skipping those")
    if not pending_visits:
        return

    sregions = _read_sregions(exposures)
    log(f"outlier: {len(pending_visits)} visits for {filtname} "
        f"({implementation})")

    tasks = [(visit, visit_files)
             for visit, visit_files in sorted(pending_visits.items())]
    dispatch(visit_step, tasks, n_processes=n_processes, use_starmap=True,
             filter_files=exposures, sregions=sregions,
             field=field, step_config=cfg,
             overwrite=overwrite, status=status)
    # Each worker writes CFP_OUT on-disk via atomic_save; sync the parent's
    # in-memory cache so the resample step later in the combine phase sees
    # freshly-stamped exposures.
    for _, visit_files in pending_visits.items():
        status.mark_all(visit_files, 'CFP_OUT')


def _run_resample(field, config, filtname, n_processes, overwrite, status,
                  reduction_version, tiles=None, epoch=None):
    # Read the outlier-finished working copies; the mosaic itself is written to
    # the canonical filter dir (resample_step derives it from field.filter_dir).
    # ``tiles`` both coarse-filters the input here and tells resample_step which
    # tiles to drizzle (its own precise per-tile SCI-WCS selection narrows
    # further within this set). ``epoch`` subsets the drizzle inputs to the
    # named epoch and labels the mosaics with the epoch name.
    exposures = field.get_exposure_files(filtname, with_step='CFP_OUT',
                                         status=status, work=True, tiles=tiles,
                                         epoch=epoch)
    if not exposures:
        log(f"resample: no CFP_OUT-stamped exposures for {filtname}")
        return
    from campfire_pipeline.nircam.steps.resample import resample_step
    cfg = get_nircam_step_config('resample', config, field)
    resample_step(filtname, exposures, field, cfg, reduction_version,
                  overwrite=overwrite, tiles=tiles, epoch=epoch)


# Dispatch table: step name → callable that takes (field, config, filtname,
# n_processes, overwrite, status). Resample needs reduction_version, so it's
# handled specially in run_combine / run_step. Per-exposure steps bind their
# name onto ``_run_per_exposure`` via ``functools.partial``; that runner looks
# up the (module, worker, cfp_key) triple in ``_PER_EXPOSURE_STEPS`` and
# imports the worker lazily when it actually dispatches.
_RUNNERS = {
    'detector1':   _run_detector1,
    'persistence': _run_persistence,
    'wisp':        functools.partial(_run_per_exposure, 'wisp'),
    'image2':      functools.partial(_run_per_exposure, 'image2'),
    'edge':        functools.partial(_run_per_exposure, 'edge'),
    'bkg':         functools.partial(_run_per_exposure, 'bkg'),
    'diag_striping': _run_diag_striping,
    'wcs_shift':   _run_wcs_shift,
    'preview':     functools.partial(_run_per_exposure, 'preview'),
    'jhat':        functools.partial(_run_per_exposure, 'jhat'),
    'apply_mask':  functools.partial(_run_per_exposure, 'apply_mask'),
    'bad_pixel':   _run_bad_pixel,
    'outlier':     _run_outlier,
    # 'resample' handled in run_combine/run_step (needs reduction_version)
}


# ---------------------------------------------------------------------------
# Phase orchestrators
# ---------------------------------------------------------------------------

def _resolve_filters(filters, field):
    if filters is None:
        return list(field.filters)
    return list(filters)


def _resolve_reduction_version(config):
    from campfire_pipeline.common.version import get_reduction_version
    return get_reduction_version(config)


def _scan_status(field, filters, overwrite=False):
    """Pre-scan canonical exposures for CFP_* keys once per phase.

    Builds a single ``StepStatus`` covering every canonical exposure across
    the requested filters. Detector1's output may not exist yet — the scan
    records empty key sets for missing paths so the skip check naturally
    reports "not done".

    With ``overwrite=True`` we skip the scan and return an empty cache:
    every step is going to run regardless of prior state, and a pre-scanned
    snapshot would go stale mid-phase (fresh-model steps like image2 and
    detector1 strip prior CFP_* keys and non-schema extensions like
    WCS_BAK from disk, but ``StepStatus.mark_all`` only adds keys to the
    cache — it never removes — so the snapshot would falsely report
    already-cleared keys as "still present"). With an empty cache,
    ``StepStatus.has`` falls back to a live ``cfp.has_step`` read for any
    path not yet seen, keeping the in-step check in sync with disk.
    """
    if overwrite:
        return StepStatus()
    paths = []
    for filt in filters:
        try:
            paths.extend(field.get_exposure_files(filt))
        except RuntimeError:
            # Workspace not set up for this filter directory — skip silently;
            # detector1 will create it.
            continue
    log(f"Pre-scanning CFP_* status for {len(paths)} canonical exposures")
    return StepStatus.scan(paths)


def _active_process_steps(config, field):
    """``PROCESS_STEPS`` with the ``jhat`` step swapped for ``align`` when the
    field has opted into the astrometric ``align`` step.

    A field that runs ``align`` (``[<field>.align].enabled = true``) uses exactly
    one alignment engine, so ``jhat`` is replaced by ``align`` in place (it runs
    where jhat did — last, on post-``image2`` cal data). ``wcs_shift`` is
    **kept**: it applies manual per-exposure offsets for corrupted-metadata
    exposures and feeds a good input WCS into align. When align is off (the
    default), the full ``PROCESS_STEPS`` list runs unchanged.
    """
    align_mode = get_nircam_step_config('align', config, field).get(
        'enabled', False)
    if not align_mode:
        return PROCESS_STEPS
    return [('align', 'CFP_ALGN') if n == 'jhat' else (n, k)
            for n, k in PROCESS_STEPS]


def _prefetch_wisp_templates(field, filters):
    """Fetch every wisp template the pending work needs, before the fan-out.

    A single-process warm-up so the parallel per-exposure workers read templates
    from ``$CAMPFIRE_ROOT/cache/wisps/`` instead of racing to download the same
    ~16 MB files. The ``(detector, filter)`` set is derived straight from uncal
    filenames (no header reads), matching the ``rootname.split('_')[3]`` detector
    convention the wisp step itself uses.

    A template the manifest says should exist but that can't be fetched raises
    ``WispTemplateError`` here and aborts the phase — 'wisp enabled + template
    missing' must never degrade to a silently unsubtracted mosaic, which is the
    exact failure this whole cache path exists to kill.
    """
    from campfire_pipeline.nircam import wisp_cache
    pairs = set()
    for filt in filters:
        try:
            uncals = field.get_uncal_files(filt)
        except RuntimeError:
            # Workspace not set up for this filter — nothing to warm.
            continue
        for f in uncals:
            parts = os.path.basename(f).removesuffix('_uncal.fits').split('_')
            if len(parts) > 3 and parts[3].lower() in wisp_cache.WISP_DETECTORS:
                pairs.add((parts[3], filt))
    if not pairs:
        return
    n = wisp_cache.ensure_for_pairs(pairs, legacy_dir=field.wisp_dir)
    if n:
        log(f"Fetched {n} wisp template(s) into the cache")


def run_process(field, config, filters=None, n_processes=1, overwrite=False,
                tiles=None):
    """Run all process-phase steps in order across each filter.

    Per-exposure steps run in parallel via ``dispatch``; the per-filter
    persistence step runs serially since it operates over the whole filter
    set at once.

    ``tiles`` restricts the phase to exposures overlapping the named tile(s) —
    ``detector1`` gates on the uncal ``S_REGION`` footprint, and every later
    step inherits the resulting canonical subset — so a single tile can be
    reduced without processing the whole field. ``None`` processes everything.
    """
    from campfire_pipeline.nircam.prefetch import prefetch_process_references
    filters = _resolve_filters(filters, field)
    if tiles:
        # Every step below re-derives the same tile-overlap set via
        # ``get_exposure_files(tiles=)``; the per-path footprint memo makes that
        # scan happen once for the whole phase instead of once per step.
        from campfire_pipeline.nircam.geometry import reset_sregion_cache
        reset_sregion_cache()
    status = _scan_status(field, filters, overwrite=overwrite)
    log(f"=== Process phase: field={field.name}, filters={filters} ===")
    prefetch_process_references(field, filters, status=status,
                               overwrite=overwrite)
    active_steps = _active_process_steps(config, field)
    if any(name == 'wisp' for name, _ in active_steps):
        _prefetch_wisp_templates(field, filters)
    for filt in filters:
        log(f"--- Process: {filt} ---")
        for step_name, _ in active_steps:
            _RUNNERS[step_name](field, config, filt, n_processes, overwrite,
                                status, tiles=tiles)


def _resolve_align_refcat(align_cfg, refcat_dir):
    """Resolve the single per-field align reference-catalog path.

    Align ties a whole multi-filter exposure to ONE refcat, so — unlike jhat —
    there is no per-filter mapping; just ``[<field>.align].refcat``.
    """
    name = align_cfg.get('refcat')
    if not name:
        raise ValueError(
            "align config missing 'refcat'. Set [<field>.align].refcat = "
            '"<file>" in fields.toml (a Gaia-tied campfire-refcat-v1 ECSV in '
            "the field's astrometric-catalog directory).")
    return os.path.join(refcat_dir, name)


def _refcat_hash(path):
    """Short content hash of the refcat file, for align-solution provenance and
    staleness detection. Hashing the bytes (not the loaded table) is cheap and
    lets the skip check run before the refcat is even loaded.
    """
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()[:8]


def _algn_refcat_hash(value):
    """The ``rc=`` refcat-hash token from a ``CFP_ALGN`` value string, or None
    (older solutions, or a NOT_ALIGNED sentinel, carry no ``rc=``)."""
    if not value:
        return None
    for tok in str(value).split():
        if tok.startswith('rc='):
            return tok[3:]
    return None


def _align_group_worker(key, members, *, refcat, config, overwrite, status):
    """``dispatch`` worker: align one pool. Module-level for pickling."""
    from campfire_pipeline.nircam.align.apply import align_exposure_group
    return align_exposure_group(members, refcat, key=key, config=config,
                                overwrite=overwrite, status=status)


def _warn_not_aligned(field, failed_groups):
    """Loudly report exposures that failed alignment, and how to resolve them.

    Excluding data from the mosaic is never an automatic decision — a NOT_ALIGNED
    exposure is quarantined from every future combine (its raw WCS would double
    sources). Surface the full list at the end of the command and hand the user
    the two levers: retune + re-run (re-attempted automatically), or force-include.
    """
    bar = '!' * 72
    log('')
    log(bar)
    log(f"align: WARNING — {len(failed_groups)} exposure(s) FAILED alignment "
        f"(CFP_ALGN = {cfp.NOT_ALIGNED}).")
    log("These are EXCLUDED from all future mosaics by default (the combine")
    log("quarantine). Omitting data is not automatic — review and resolve first:")
    log('')
    for g in failed_groups:
        log(f"  {g.key}  ({g.n_members} detector(s)):")
        for m in g.members:
            log(f"      {m.path}")
    log('')
    log("To retry: retune [<field>.align] in fields.toml (e.g. widen "
        "ref_border_arcmin,")
    log("  lower snr_min, raise match_radius / coarse_searchrad, lower "
        "min_matched / min_coverage_arcsec), then re-run")
    log("  `cfpipe nircam align` — NOT_ALIGNED exposures are re-attempted "
        "automatically")
    log("  (no --overwrite needed; already-aligned exposures are left alone).")
    log("To include them as-is (raw WCS, not recommended): "
        "`cfpipe nircam combine --include-unaligned`.")
    log(bar)
    log('')


def _pending_pools(pools, status, overwrite, refcat_hash):
    """Return ``(pending, retry_count)`` — the pools still needing a solve.

    A pool is skipped only when every member carries a *completed, non-rejected*
    ``CFP_ALGN`` **produced by the current refcat** (its stamped ``rc=`` hash
    matches *refcat_hash*). Re-solved on a normal re-run (no ``--overwrite``):
    NOT_ALIGNED pools (the user may have retuned ``[<field>.align]``) and pools
    solved against a now-changed refcat (or by an older, ``rc=``-less
    generation). *retry_count* is how many pending pools are such re-attempts of
    already-stamped work.
    """
    if overwrite:
        return list(pools), 0
    pending, retry = [], 0
    for p in pools:
        stamped = all(status.has(m.path, 'CFP_ALGN') for m in p.members)
        value = (cfp.step_value(p.members[0].path, 'CFP_ALGN')
                 if stamped else None)
        done = (stamped and value != cfp.NOT_ALIGNED
                and _algn_refcat_hash(value) == refcat_hash)
        if done:
            continue
        pending.append(p)
        if stamped:
            retry += 1              # NOT_ALIGNED, stale refcat, or old generation
    return pending, retry


def _run_align(field, config, filtname, n_processes, overwrite, status,
               tiles=None):
    """Per-filter astrometric align step (replaces ``jhat`` for align-enabled
    fields; runs inside the process loop).

    Because filter directories are channel-segregated (SW filters hold SW
    detectors, LW filters LW), grouping this filter's exposures already yields
    one channel. Each exposure is split into module **pools** (unless
    ``pool_modules``), and each pool is tied to the field's Gaia-tied reference
    catalog with a coarse ``rshift`` + gated per-detector fine fit, its corrected
    gwcs written back with a ``CFP_ALGN`` stamp. A no-op when align is disabled.

    ``tiles`` restricts to exposures overlapping the named tile(s) — the
    per-filter exposure-union gate keeps a dither's full detector complement in
    this channel, so pools stay complete at tile edges.
    """
    align_cfg = get_nircam_step_config('align', config, field)
    if not align_cfg.get('enabled', False):
        return

    from campfire_pipeline.nircam.association import (
        build_exposure_groups, split_pools, unsupported_mode_reason,
    )
    from campfire_pipeline.nircam.refcat.io import read_refcat

    groups = build_exposure_groups(field, [filtname], status=status, tiles=tiles)
    if not groups:
        log(f"align: no exposures for {filtname}")
        return

    # Observing-mode gating: stop before solving if any exposure in scope is in
    # a mode align does not support (subarray / coronagraph / TSO / WFSS).
    unsupported = [(g, r) for g in groups
                   for r in (unsupported_mode_reason(g.members[0].path),) if r]
    if unsupported:
        listing = "\n".join(f"  {g.key}: {reason}" for g, reason in unsupported)
        raise RuntimeError(
            f"align: {len(unsupported)} exposure(s) in {filtname} are in an "
            f"observing mode align does not support:\n{listing}\n\nExclude them "
            f"(fields.toml [{field.name}].skip, or the reviewer "
            f"excluded_exposures list) and re-run, or select a supported subset "
            f"with --filters / --tiles.")

    pools = split_pools(groups, pool_modules=align_cfg.get('pool_modules', False))
    refcat_path = _resolve_align_refcat(align_cfg, field.refcat_dir)
    refcat_hash = _refcat_hash(refcat_path)   # cheap; drives the staleness skip
    pending, retry = _pending_pools(pools, status, overwrite, refcat_hash)
    skipped = len(pools) - len(pending)
    if skipped:
        log(f"align: {skipped}/{len(pools)} pool(s) already aligned for "
            f"{filtname}; skipping (--overwrite to re-solve)")
    if retry:
        log(f"align[{filtname}]: re-attempting {retry} previously-stamped "
            f"pool(s) (NOT_ALIGNED or changed refcat)")
    if not pending:
        return

    refcat = read_refcat(refcat_path)
    log(f"align[{filtname}]: refcat={os.path.basename(refcat_path)} "
        f"({len(refcat)} sources, rc={refcat_hash}); solving {len(pending)} "
        f"pool(s)")
    worker_cfg = {**align_cfg, '_refcat_hash': refcat_hash}
    tasks = [(p.key, list(p.members)) for p in pending]
    results = dispatch(_align_group_worker, tasks, n_processes=n_processes,
                       use_starmap=True, refcat=refcat, config=worker_cfg,
                       overwrite=overwrite, status=status)

    # Workers stamped CFP_ALGN on disk in child processes; sync the parent cache
    # so a later step/phase in the same run sees the fresh stamps.
    for p in pending:
        status.mark_all([m.path for m in p.members], 'CFP_ALGN')

    # A NOT_ALIGNED pool is quarantined from every future mosaic (the combine
    # quarantine). That must never be silent — surface it loudly with the levers
    # to resolve it.
    by_key = {p.key: p for p in pending}
    failed = [by_key[r.key] for r in results
              if r is not None and getattr(r, 'status', None) == 'NOT_ALIGNED'
              and getattr(r, 'key', None) in by_key]
    if failed:
        _warn_not_aligned(field, failed)


_RUNNERS['align'] = _run_align


def run_combine(field, config, filters=None, n_processes=1, overwrite=False,
                tiles=None, epoch=None, include_unaligned=False):
    """Run all combine-phase steps in order across each filter.

    ``tiles`` scopes the *whole* combine phase to exposures overlapping the
    named tile(s): apply_mask, bad_pixel, outlier, and resample all see only
    the overlapping subset. This is what lets a single tile be combined without
    touching the rest of the field (e.g. an A/B reduction), and it pairs with a
    tile-scoped ``run_process`` (whose align step only ever produced the subset
    canonicals). **Caveat:** restricting the ensemble steps truncates outlier's
    cross-visit median pool and bad_pixel's per-detector stacks, so a
    tile-scoped mosaic may differ at tile boundaries from the same tile built
    with the whole field — a tile-scoped run is a distinct input set by design.
    ``None`` (the default) runs the full field and is unchanged.

    ``epoch`` scopes the whole combine phase to one named epoch's exposure
    subset (fields.toml ``[<field>.epochs.<name>]``) in the same way ``tiles``
    does, and the resulting mosaics carry the epoch name as a trailing filename
    segment. The same ensemble-truncation caveat applies: an epoch mosaic is
    reduced from only the epoch's exposures by design. ``None`` (the default)
    uses the full field with no epoch segment.
    """
    filters = _resolve_filters(filters, field)
    reduction_version = _resolve_reduction_version(config)
    status = _scan_status(field, filters, overwrite=overwrite)

    # For an align-enabled field, quarantine NOT_ALIGNED exposures from the
    # ensemble (they'd drizzle with a raw WCS). --include-unaligned overrides.
    align_enabled = get_nircam_step_config('align', config, field).get(
        'enabled', False)
    exclude_not_aligned = align_enabled and not include_unaligned

    log(f"=== Combine phase: field={field.name}, filters={filters}"
        f"{f', epoch={epoch}' if epoch else ''} ===")
    for filt in filters:
        log(f"--- Combine: {filt} ---")
        for step_name, _ in COMBINE_STEPS:
            if step_name == 'apply_mask':
                # apply_mask writes the canonical (CFMASK extension only) — the
                # last thing to touch the frozen canonical this phase. Then
                # refresh the disposable working copies the ensemble steps
                # mutate (copy canonical -> work where stale, fuse CFMASK ->
                # DO_NOT_USE) and rescan them into the status cache.
                _RUNNERS[step_name](field, config, filt, n_processes,
                                    overwrite, status, tiles=tiles, epoch=epoch)
                field.materialize_work(filt, status=status, overwrite=overwrite,
                                       exclude_not_aligned=exclude_not_aligned)
            elif step_name == 'resample':
                _run_resample(field, config, filt, n_processes, overwrite,
                              status, reduction_version, tiles=tiles,
                              epoch=epoch)
            else:
                _RUNNERS[step_name](field, config, filt, n_processes, overwrite,
                                    status, tiles=tiles, epoch=epoch)


def run_step(step_name, field, config, filters=None, n_processes=1,
             overwrite=False, tiles=None, epoch=None):
    """Run a single named step across the field's filters.

    Used by the per-step CLI commands (``cfpipe nircam <step>``). ``tiles``
    restricts the step to exposures overlapping the named tile(s); for
    ``resample`` it additionally scopes which mosaics are built. The per-step
    CLI commands other than ``resample`` don't expose ``--tiles``, so they pass
    ``None`` and run over the full field. ``epoch`` is only meaningful for
    ``resample`` (the only per-step command exposing ``--epoch``): it subsets
    the drizzle inputs and labels the mosaics.
    """
    if step_name not in STEP_NAMES:
        raise ValueError(
            f"Unknown step '{step_name}'. Known: {STEP_NAMES}"
        )

    filters = _resolve_filters(filters, field)
    status = _scan_status(field, filters, overwrite=overwrite)
    log(f"=== Step '{step_name}': field={field.name}, filters={filters} ===")
    if step_name in _CRDS_STEPS:
        from campfire_pipeline.nircam.prefetch import prefetch_process_references
        prefetch_process_references(field, filters, status=status,
                                    overwrite=overwrite)

    # A standalone ensemble step honours the same NOT_ALIGNED quarantine as the
    # full combine phase for align-enabled fields (no --include-unaligned escape
    # hatch on the per-step CLI — the full `combine` command is where you opt in).
    exclude_not_aligned = get_nircam_step_config('align', config, field).get(
        'enabled', False)

    for filt in filters:
        # A standalone combine ensemble step needs the working copies present
        # and primed (canonical -> work, CFMASK -> DO_NOT_USE) before it runs.
        if step_name in _COMBINE_WORK_STEPS:
            field.materialize_work(filt, status=status, overwrite=overwrite,
                                   exclude_not_aligned=exclude_not_aligned)
        if step_name == 'resample':
            reduction_version = _resolve_reduction_version(config)
            _run_resample(field, config, filt, n_processes, overwrite,
                          status, reduction_version, tiles=tiles, epoch=epoch)
        else:
            _RUNNERS[step_name](field, config, filt, n_processes, overwrite,
                                status, tiles=tiles)
