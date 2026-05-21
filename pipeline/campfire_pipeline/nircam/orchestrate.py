"""
NIRCam-specific orchestrator: step ordering, step runners, dispatch table.

Phase entry points (``run_process`` / ``run_combine`` / ``run_step``) are
thin wrappers around the generic skeleton in
``common.imaging.orchestrate`` — this module owns only the NIRCam-specific
constants (step lists, CRDS-touching steps) and the per-step runner
functions that implement NIRCam's pipeline semantics (persistence ensemble,
diag_striping opt-in, JHAT WCS rules, per-visit outlier dispatch, etc.).

Within a filter the per-exposure steps run via
``common.parallel.dispatch`` (with ``n_processes`` workers), the per-filter
ensemble steps (persistence, build_bad_pixel_masks) run serially, and the
per-visit ensemble step (outlier) dispatches one visit per worker via the
same ``dispatch`` helper.
"""

import os

from astropy.io import fits

from campfire_pipeline.common.io import log
from campfire_pipeline.common.parallel import dispatch
from campfire_pipeline.common.imaging import orchestrate as _orch
from campfire_pipeline.common.imaging.prefetch import prefetch_process_references
from campfire_pipeline.config import get_nircam_step_config

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
# NIRCam-specific helpers
# ---------------------------------------------------------------------------

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


def _per_exposure(step_name, fn, cfp_key, field, config, filtname,
                  n_processes, overwrite, status):
    """NIRCam-side wrapper that injects ``get_nircam_step_config``.

    Lets the entries in ``_RUNNERS`` stay short — they pass the step name,
    callable, and CFP key, and this helper closes over the config-getter.
    """
    return _orch.run_per_exposure(
        step_name, fn, cfp_key, field, config, filtname,
        n_processes, overwrite, status,
        get_step_config=get_nircam_step_config,
    )


# ---------------------------------------------------------------------------
# Per-step runners (used by run_process / run_combine / run_step)
# ---------------------------------------------------------------------------

def _run_detector1(field, config, filtname, n_processes, overwrite, status):
    uncals = field.get_uncal_files(filtname)
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

    cfg = get_nircam_step_config('detector1', config, field)
    log(f"detector1: dispatching {len(pending)} files for {filtname}")
    dispatch(detector1_step, pending, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite,
             status=status)
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
    pending, _ = _orch.filter_pending('diag_striping', exposures, 'CFP_DIAG',
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
    # stamped, so filter_pending wouldn't catch them.
    matched = []
    for f in exposures:
        rootname = os.path.basename(f).removesuffix('.fits')
        if _match_rule(rootname, filtname, rules) is not None:
            matched.append(f)
    if not matched:
        log(f"wcs_shift: no exposures match any rule for {filtname}")
        return

    pending, _ = _orch.filter_pending('wcs_shift', matched, 'CFP_SHFT', status,
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
    pending, _ = _orch.filter_pending('bad_pixel', exposures, 'CFP_BPIX', status,
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
    visits = _orch.group_by_visit(exposures)

    # Pre-filter visits whose members all carry CFP_OUT *AND* whose manifest
    # is unchanged (cheap check); fall back to outlier_step for the rest.
    # The CFP_OUT-only short-circuit avoids the polygon-overlap setup work
    # done at the top of outlier_step on no-op runs.
    from campfire_pipeline.common.imaging.manifest import (
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

    sregions = _orch.read_sregions(exposures)
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


def _run_resample(field, config, filtname, n_processes, overwrite, status):
    """Drizzle CFP_OUT-stamped exposures into per-tile mosaics.

    Resolves the reduction version internally so the runner has the same
    6-arg signature as every other entry in ``_RUNNERS``.
    """
    exposures = field.get_exposure_files(filtname, with_step='CFP_OUT',
                                         status=status)
    if not exposures:
        log(f"resample: no CFP_OUT-stamped exposures for {filtname}")
        return
    cfg = get_nircam_step_config('resample', config, field)
    reduction_version = _orch.resolve_reduction_version(config)
    resample_step(filtname, exposures, field, cfg, reduction_version,
                  overwrite=overwrite)


# Dispatch table: step name → callable that takes (field, config, filtname,
# n_processes, overwrite, status). The lambda-wrapped entries adapt the
# (step_name, fn, cfp_key) triple onto ``_per_exposure``'s signature.
_RUNNERS = {
    'detector1':   _run_detector1,
    'persistence': _run_persistence,
    'wisp':        lambda f, c, fl, n, ow, st: _per_exposure(
                       'wisp', wisp_step, 'CFP_WISP',
                       f, c, fl, n, ow, st),
    'striping':    lambda f, c, fl, n, ow, st: _per_exposure(
                       'striping', striping_step, 'CFP_1F',
                       f, c, fl, n, ow, st),
    'image2':      lambda f, c, fl, n, ow, st: _per_exposure(
                       'image2', image2_step, 'CFP_IMG2',
                       f, c, fl, n, ow, st),
    'diag_striping': _run_diag_striping,
    'edge':        lambda f, c, fl, n, ow, st: _per_exposure(
                       'edge', edge_step, 'CFP_EDGE',
                       f, c, fl, n, ow, st),
    'sky':         lambda f, c, fl, n, ow, st: _per_exposure(
                       'sky', sky_step, 'CFP_SKY',
                       f, c, fl, n, ow, st),
    'variance':    lambda f, c, fl, n, ow, st: _per_exposure(
                       'variance', variance_step, 'CFP_VAR',
                       f, c, fl, n, ow, st),
    'wcs_shift':   _run_wcs_shift,
    'preview':     lambda f, c, fl, n, ow, st: _per_exposure(
                       'preview', preview_step, 'CFP_PREV',
                       f, c, fl, n, ow, st),
    'jhat':        lambda f, c, fl, n, ow, st: _per_exposure(
                       'jhat', jhat_step, 'CFP_JHAT',
                       f, c, fl, n, ow, st),
    'apply_mask':  lambda f, c, fl, n, ow, st: _per_exposure(
                       'apply_mask', apply_masks_step, 'CFP_MASK',
                       f, c, fl, n, ow, st),
    'bad_pixel':   _run_bad_pixel,
    'outlier':     _run_outlier,
    'resample':    _run_resample,
}


# ---------------------------------------------------------------------------
# Phase entry points — thin delegates to the common skeleton
# ---------------------------------------------------------------------------

def run_process(field, config, filters=None, n_processes=1, overwrite=False):
    """Run all process-phase steps in order across each filter."""
    return _orch.run_process(
        field, config, PROCESS_STEPS, _RUNNERS, prefetch_process_references,
        filters=filters, n_processes=n_processes, overwrite=overwrite,
    )


def run_combine(field, config, filters=None, n_processes=1, overwrite=False):
    """Run all combine-phase steps in order across each filter."""
    return _orch.run_combine(
        field, config, COMBINE_STEPS, _RUNNERS,
        filters=filters, n_processes=n_processes, overwrite=overwrite,
    )


def run_step(step_name, field, config, filters=None, n_processes=1,
             overwrite=False):
    """Run a single named step across the field's filters."""
    return _orch.run_step(
        step_name, field, config, STEP_NAMES, _RUNNERS, _CRDS_STEPS,
        prefetch_process_references,
        filters=filters, n_processes=n_processes, overwrite=overwrite,
    )
