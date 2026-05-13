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

import os
import warnings

from astropy.io import fits

from campfire_pipeline.common.io import log
from campfire_pipeline.common.parallel import dispatch
from campfire_pipeline.config import get_nircam_step_config

from campfire_pipeline.nircam.status import StepStatus
from campfire_pipeline.nircam.steps.detector1 import detector1_step
from campfire_pipeline.nircam.steps.persistence import persistence_step
from campfire_pipeline.nircam.steps.wisp import wisp_step
from campfire_pipeline.nircam.steps.striping import striping_step
from campfire_pipeline.nircam.steps.image2 import image2_step
from campfire_pipeline.nircam.steps.diag_striping import diag_striping_step
from campfire_pipeline.nircam.steps.edge import edge_step
from campfire_pipeline.nircam.steps.sky import sky_step
from campfire_pipeline.nircam.steps.variance import variance_step
from campfire_pipeline.nircam.steps.wcs_shift import wcs_shift_step, _match_rule
from campfire_pipeline.nircam.steps.preview import preview_step
from campfire_pipeline.nircam.steps.jhat import jhat_step
from campfire_pipeline.nircam.steps.apply_masks import apply_masks_step
from campfire_pipeline.nircam.steps.bad_pixel import (
    build_bad_pixel_masks, bad_pixel_step,
)
from campfire_pipeline.nircam.steps.outlier import outlier_step
from campfire_pipeline.nircam.steps.resample import resample_step
from campfire_pipeline.nircam.prefetch import prefetch_process_references


# Step ordering — also used by the CLI to validate ``cfpipe nircam <step>``
# names. Each entry is (step_name, cfp_key_or_None). ``cfp_key`` is None for
# resample (mosaic outputs are stamped with CMPFRVER, not CFP_*).
PROCESS_STEPS = [
    ('detector1',   'CFP_DET1'),
    ('persistence', 'CFP_PERS'),
    ('wisp',        'CFP_WISP'),
    ('striping',    'CFP_1F'),
    ('image2',      'CFP_IMG2'),
    ('edge',        'CFP_EDGE'),
    ('sky',         'CFP_SKY'),
    ('diag_striping', 'CFP_DIAG'),
    ('variance',    'CFP_VAR'),
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
STEP_NAMES = [name for name, _ in ALL_STEPS]

# Steps that hit CRDS — used by run_step() to decide when to pre-fetch
# reference files before parallel dispatch.
_CRDS_STEPS = {'detector1', 'wisp', 'striping', 'image2'}


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


def _read_sregions(exposure_files):
    """Return S_REGION header strings parallel to ``exposure_files``."""
    sregions = []
    for f in exposure_files:
        with fits.open(f) as hdul:
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


def _run_detector1(field, config, filtname, n_processes, overwrite, status):
    uncals = field.get_uncal_files(filtname)
    if not uncals:
        log(f"detector1: no uncal files for {filtname}")
        return

    uncals = _filter_imaging_uncals(uncals, 'detector1')
    if not uncals:
        log(f"detector1: no imaging uncals for {filtname} after grism filter")
        return

    # Skip uncals whose canonical output already has CFP_DET1.
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

    cfg = get_nircam_step_config('detector1', config, field)
    log(f"detector1: dispatching {len(pending)} files for {filtname}")
    dispatch(detector1_step, pending, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite,
             status=status)
    # Mark CFP_DET1 on the newly produced canonicals so later phases see them
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


def _run_persistence(field, config, filtname, n_processes, overwrite, status):
    exposures = field.get_exposure_files(filtname)
    if not exposures:
        log(f"persistence: no exposures for {filtname}")
        return
    # Persistence is an ensemble step — it re-runs the whole batch unless every
    # member is already done. So we only short-circuit the "all done" case.
    if not overwrite and all(status.has(f, 'CFP_PERS') for f in exposures):
        log(f"persistence: CFP_PERS already set on all {len(exposures)} "
            f"exposures for {filtname}; skipping")
        return
    cfg = get_nircam_step_config('persistence', config, field)
    persistence_step(exposures, field, cfg, overwrite=overwrite, status=status)
    status.mark_all(exposures, 'CFP_PERS')


def _run_per_exposure(step_name, fn, cfp_key, field, config, filtname,
                      n_processes, overwrite, status):
    """Generic per-exposure parallel dispatch.

    Filters out already-stamped exposures *before* spinning up the worker
    pool — a no-op pass on a finished field skips the Pool entirely.
    """
    exposures = field.get_exposure_files(filtname)
    if not exposures:
        log(f"{step_name}: no exposures for {filtname}")
        return
    pending, _ = _filter_pending(step_name, exposures, cfp_key, status,
                                 overwrite)
    if not pending:
        return
    cfg = get_nircam_step_config(step_name, config, field)
    log(f"{step_name}: dispatching {len(pending)} exposures for {filtname}")
    dispatch(fn, pending, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite,
             status=status)
    status.mark_all(pending, cfp_key)


def _run_diag_striping(field, config, filtname, n_processes, overwrite, status):
    """Opt-in scattered-light diagonal striping. Disabled unless a field
    sets ``[field.diag_striping].enabled = true``."""
    cfg = get_nircam_step_config('diag_striping', config, field)
    if not cfg.get('enabled', False):
        log(f"diag_striping: disabled by config; skipping {filtname}")
        return
    exposures = field.get_exposure_files(filtname)
    if not exposures:
        log(f"diag_striping: no exposures for {filtname}")
        return
    pending, _ = _filter_pending('diag_striping', exposures, 'CFP_DIAG',
                                 status, overwrite)
    if not pending:
        return
    log(f"diag_striping: dispatching {len(pending)} exposures for {filtname}")
    dispatch(diag_striping_step, pending, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite,
             status=status)
    status.mark_all(pending, 'CFP_DIAG')


def _run_wcs_shift(field, config, filtname, n_processes, overwrite, status):
    """Opt-in pre-JHAT astrometric shift. No-op unless ``[[<field>.wcs_shift]]``
    rules are defined in fields.toml."""
    rules = field.wcs_shift_rules
    if not rules:
        log(f"wcs_shift: no rules; skipping {filtname}")
        return
    exposures = field.get_exposure_files(filtname)
    if not exposures:
        log(f"wcs_shift: no exposures for {filtname}")
        return

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
    status.mark_all(pending, 'CFP_SHFT')


def _run_bad_pixel(field, config, filtname, n_processes, overwrite, status):
    exposures = field.get_exposure_files(filtname)
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
    log(f"bad_pixel: dispatching {len(pending)} exposures for {filtname}")
    dispatch(bad_pixel_step, pending, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite,
             status=status)
    status.mark_all(pending, 'CFP_BPIX')


def _run_outlier(field, config, filtname, n_processes, overwrite, status):
    cfg = get_nircam_step_config('outlier', config, field)
    implementation = cfg.get('implementation', 'jwst')
    if implementation not in ('jwst', 'campfire'):
        raise ValueError(
            f"Unknown outlier.implementation {implementation!r}; "
            f"expected 'jwst' or 'campfire'"
        )
    _run_outlier_per_visit(field, cfg, filtname, n_processes, overwrite, status,
                           implementation=implementation)


def _run_outlier_per_visit(field, cfg, filtname, n_processes, overwrite, status,
                           implementation='jwst'):
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
    Each visit writes only to its own canonical files (via ``atomic_save``)
    while reading other visits' files as cross-visit overlap padding.
    Because reads/writes are atomic and outlier_detection only ADDS DQ
    bits (SCI is unchanged), parallel runs cannot crash; the only
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

    exposures = field.get_exposure_files(filtname)
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
        # Check that visit_files (a subset of all_inputs) hashes still match.
        # Cross-visit overlaps are validated inside outlier_step on the slow
        # path; here we only confirm the visit's own files are unchanged so
        # we can cheaply skip the obvious no-op case.
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

    # Read S_REGION only when there's actual outlier work to do
    sregions = _read_sregions(exposures)
    log(f"outlier: {len(pending_visits)} visits for {filtname} "
        f"({implementation})")

    tasks = [(visit, visit_files)
             for visit, visit_files in sorted(pending_visits.items())]
    dispatch(visit_step, tasks, n_processes=n_processes, use_starmap=True,
             filter_files=exposures, sregions=sregions,
             field=field, step_config=cfg,
             overwrite=overwrite, status=status)
    # Stamp CFP_OUT in the parent's status cache after all workers finish.
    # The on-disk CFP_OUT key is written by each worker via atomic_save;
    # this just keeps the in-memory cache consistent so the resample step
    # later in the same combine phase sees freshly-stamped exposures.
    for _, visit_files in pending_visits.items():
        status.mark_all(visit_files, 'CFP_OUT')


def _run_resample(field, config, filtname, n_processes, overwrite, status,
                  reduction_version):
    exposures = field.get_exposure_files(filtname, with_step='CFP_OUT',
                                         status=status)
    if not exposures:
        log(f"resample: no CFP_OUT-stamped exposures for {filtname}")
        return
    cfg = get_nircam_step_config('resample', config, field)
    resample_step(filtname, exposures, field, cfg, reduction_version,
                  overwrite=overwrite)


# Dispatch table: step name → callable that takes (field, config, filtname,
# n_processes, overwrite, status). Resample needs reduction_version, so it's
# handled specially in run_combine / run_step.
_RUNNERS = {
    'detector1':   _run_detector1,
    'persistence': _run_persistence,
    'wisp':        lambda f, c, fl, n, ow, st: _run_per_exposure(
                       'wisp', wisp_step, 'CFP_WISP',
                       f, c, fl, n, ow, st),
    'striping':    lambda f, c, fl, n, ow, st: _run_per_exposure(
                       'striping', striping_step, 'CFP_1F',
                       f, c, fl, n, ow, st),
    'image2':      lambda f, c, fl, n, ow, st: _run_per_exposure(
                       'image2', image2_step, 'CFP_IMG2',
                       f, c, fl, n, ow, st),
    'diag_striping': _run_diag_striping,
    'edge':        lambda f, c, fl, n, ow, st: _run_per_exposure(
                       'edge', edge_step, 'CFP_EDGE',
                       f, c, fl, n, ow, st),
    'sky':         lambda f, c, fl, n, ow, st: _run_per_exposure(
                       'sky', sky_step, 'CFP_SKY',
                       f, c, fl, n, ow, st),
    'variance':    lambda f, c, fl, n, ow, st: _run_per_exposure(
                       'variance', variance_step, 'CFP_VAR',
                       f, c, fl, n, ow, st),
    'wcs_shift':   _run_wcs_shift,
    'preview':     lambda f, c, fl, n, ow, st: _run_per_exposure(
                       'preview', preview_step, 'CFP_PREV',
                       f, c, fl, n, ow, st),
    'jhat':        lambda f, c, fl, n, ow, st: _run_per_exposure(
                       'jhat', jhat_step, 'CFP_JHAT',
                       f, c, fl, n, ow, st),
    'apply_mask':  lambda f, c, fl, n, ow, st: _run_per_exposure(
                       'apply_mask', apply_masks_step, 'CFP_MASK',
                       f, c, fl, n, ow, st),
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


def run_process(field, config, filters=None, n_processes=1, overwrite=False):
    """Run all process-phase steps in order across each filter.

    Per-exposure steps run in parallel via ``dispatch``; the per-filter
    persistence step runs serially since it operates over the whole filter
    set at once.
    """
    filters = _resolve_filters(filters, field)
    status = _scan_status(field, filters, overwrite=overwrite)
    log(f"=== Process phase: field={field.name}, filters={filters} ===")
    prefetch_process_references(field, filters, n_processes)
    for filt in filters:
        log(f"--- Process: {filt} ---")
        for step_name, _ in PROCESS_STEPS:
            _RUNNERS[step_name](field, config, filt, n_processes, overwrite,
                                status)


def run_combine(field, config, filters=None, n_processes=1, overwrite=False):
    """Run all combine-phase steps in order across each filter."""
    filters = _resolve_filters(filters, field)
    reduction_version = _resolve_reduction_version(config)
    status = _scan_status(field, filters, overwrite=overwrite)

    log(f"=== Combine phase: field={field.name}, filters={filters} ===")
    for filt in filters:
        log(f"--- Combine: {filt} ---")
        for step_name, _ in COMBINE_STEPS:
            if step_name == 'resample':
                _run_resample(field, config, filt, n_processes, overwrite,
                              status, reduction_version)
            else:
                _RUNNERS[step_name](field, config, filt, n_processes, overwrite,
                                    status)


def run_step(step_name, field, config, filters=None, n_processes=1,
             overwrite=False):
    """Run a single named step across the field's filters.

    Used by the per-step CLI commands (``cfpipe nircam <step>``).
    """
    if step_name not in STEP_NAMES:
        raise ValueError(
            f"Unknown step '{step_name}'. Known: {STEP_NAMES}"
        )

    filters = _resolve_filters(filters, field)
    status = _scan_status(field, filters, overwrite=overwrite)
    log(f"=== Step '{step_name}': field={field.name}, filters={filters} ===")
    if step_name in _CRDS_STEPS:
        prefetch_process_references(field, filters, n_processes)

    for filt in filters:
        if step_name == 'resample':
            reduction_version = _resolve_reduction_version(config)
            _run_resample(field, config, filt, n_processes, overwrite,
                          status, reduction_version)
        else:
            _RUNNERS[step_name](field, config, filt, n_processes, overwrite,
                                status)
