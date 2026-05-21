"""
Generic phase skeleton for imaging-arm pipelines (NIRCam, MIRI, ...).

Each instrument's ``<instrument>/orchestrate.py`` owns its own ``PROCESS_STEPS``,
``COMBINE_STEPS``, and ``_RUNNERS`` dict. The phase entry points here
(``run_process`` / ``run_combine`` / ``run_step``) drive that dispatch table
in a uniform way — pre-scan CFP_* status, resolve the filter list, iterate
``(filter, step)``, call the runner.

All runners share the same signature
``(field, config, filtname, n_processes, overwrite, status)``. Steps that
need extra state (e.g. resample needs the reduction version stamped into
CMPFRVER) resolve it internally rather than having the orchestrator
special-case them.

Helpers (``filter_pending`` / ``run_per_exposure`` / ``scan_status`` /
``group_by_visit`` / ``read_sregions``) are usable by per-instrument
runners.
"""

import os
import warnings

from astropy.io import fits

from campfire_pipeline.common.io import log
from campfire_pipeline.common.imaging.status import StepStatus


# ---------------------------------------------------------------------------
# Helpers (used by per-instrument runners + the phase entry points below)
# ---------------------------------------------------------------------------

def group_by_visit(exposure_files):
    """Return ``{visit: [paths...]}`` keyed on the leading ``jw...`` token."""
    visits = {}
    for f in exposure_files:
        visit = os.path.basename(f).split('_')[0]
        visits.setdefault(visit, []).append(f)
    return visits


def read_sregions(exposure_files):
    """Return S_REGION header strings parallel to ``exposure_files``."""
    sregions = []
    for f in exposure_files:
        with fits.open(f) as hdul:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                sregions.append(hdul[1].header['S_REGION'])
    return sregions


def filter_pending(step_name, exposures, cfp_key, status, overwrite):
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


def run_per_exposure(step_name, fn, cfp_key, field, config, filtname,
                     n_processes, overwrite, status, get_step_config):
    """Generic per-exposure parallel dispatch.

    Filters out already-stamped exposures *before* spinning up the worker
    pool — a no-op pass on a finished field skips the Pool entirely.

    ``get_step_config`` is an instrument-aware callable
    ``(step_name, config, field) -> dict`` (e.g.
    ``get_nircam_step_config`` / ``get_miri_step_config``) that resolves
    the per-step config merge.
    """
    from campfire_pipeline.common.parallel import dispatch

    exposures = field.get_exposure_files(filtname)
    if not exposures:
        log(f"{step_name}: no exposures for {filtname}")
        return
    pending, _ = filter_pending(step_name, exposures, cfp_key, status,
                                overwrite)
    if not pending:
        return
    cfg = get_step_config(step_name, config, field)
    log(f"{step_name}: dispatching {len(pending)} exposures for {filtname}")
    dispatch(fn, pending, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite,
             status=status)
    status.mark_all(pending, cfp_key)


# ---------------------------------------------------------------------------
# Phase orchestration internals
# ---------------------------------------------------------------------------

def resolve_filters(filters, field):
    if filters is None:
        return list(field.filters)
    return list(filters)


def resolve_reduction_version(config):
    from campfire_pipeline.common.version import get_reduction_version
    return get_reduction_version(config)


def scan_status(field, filters, overwrite=False):
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


# ---------------------------------------------------------------------------
# Phase entry points — drive an instrument's runners + step lists
# ---------------------------------------------------------------------------

def run_process(field, config, process_steps, runners, prefetch_fn,
                filters=None, n_processes=1, overwrite=False):
    """Run all process-phase steps in order across each filter.

    ``process_steps`` is the instrument's ordered list of
    ``(step_name, cfp_key)`` tuples. ``runners`` is the instrument's
    ``_RUNNERS`` dispatch dict. ``prefetch_fn(field, filters, n_processes)``
    pre-fetches CRDS references; pass a no-op if the instrument has no
    references to prefetch.
    """
    filters = resolve_filters(filters, field)
    status = scan_status(field, filters, overwrite=overwrite)
    log(f"=== Process phase: field={field.name}, filters={filters} ===")
    prefetch_fn(field, filters, n_processes)
    for filt in filters:
        log(f"--- Process: {filt} ---")
        for step_name, _ in process_steps:
            runners[step_name](field, config, filt, n_processes, overwrite,
                               status)


def run_combine(field, config, combine_steps, runners,
                filters=None, n_processes=1, overwrite=False):
    """Run all combine-phase steps in order across each filter."""
    filters = resolve_filters(filters, field)
    status = scan_status(field, filters, overwrite=overwrite)

    log(f"=== Combine phase: field={field.name}, filters={filters} ===")
    for filt in filters:
        log(f"--- Combine: {filt} ---")
        for step_name, _ in combine_steps:
            runners[step_name](field, config, filt, n_processes, overwrite,
                               status)


def run_step(step_name, field, config, all_step_names, runners, crds_steps,
             prefetch_fn, filters=None, n_processes=1, overwrite=False):
    """Run a single named step across the field's filters.

    Used by the per-step CLI commands (``cfpipe <instrument> <step>``).
    """
    if step_name not in all_step_names:
        raise ValueError(
            f"Unknown step '{step_name}'. Known: {all_step_names}"
        )

    filters = resolve_filters(filters, field)
    status = scan_status(field, filters, overwrite=overwrite)
    log(f"=== Step '{step_name}': field={field.name}, filters={filters} ===")
    if step_name in crds_steps:
        prefetch_fn(field, filters, n_processes)

    for filt in filters:
        runners[step_name](field, config, filt, n_processes, overwrite, status)
