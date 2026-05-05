"""
Orchestrators for the canonical-exposure NIRCam pipeline.

Two phase-level entry points (``run_process``, ``run_combine``) and one
single-step dispatcher (``run_step``). Both phases iterate over the
field's filters; within a filter, the per-exposure steps run via
``common.parallel.dispatch`` (with ``n_processes`` workers), the
per-filter ensemble steps (persistence, build_bad_pixel_masks) run
serially, and the per-visit ensemble steps (skymatch, outlier) iterate
over visits sequentially.

The legacy ``stage1.py`` / ``stage2.py`` / ``stage3.py`` orchestrators
remain in place for now but are not invoked from the new CLI.
"""

import os
import warnings

from astropy.io import fits

from campfire_pipeline.common.io import log
from campfire_pipeline.common.parallel import dispatch
from campfire_pipeline.config import get_nircam_step_config

from campfire_pipeline.nircam.steps.detector1 import detector1_step
from campfire_pipeline.nircam.steps.persistence import persistence_step
from campfire_pipeline.nircam.steps.wisp import wisp_step
from campfire_pipeline.nircam.steps.striping import striping_step
from campfire_pipeline.nircam.steps.image2 import image2_step
from campfire_pipeline.nircam.steps.edge import edge_step
from campfire_pipeline.nircam.steps.sky import sky_step
from campfire_pipeline.nircam.steps.variance import variance_step
from campfire_pipeline.nircam.steps.jhat import jhat_step
from campfire_pipeline.nircam.steps.apply_masks import apply_masks_step
from campfire_pipeline.nircam.steps.bad_pixel import (
    build_bad_pixel_masks, bad_pixel_step,
)
from campfire_pipeline.nircam.steps.skymatch import skymatch_step
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
    ('variance',    'CFP_VAR'),
    ('jhat',        'CFP_JHAT'),
]

COMBINE_STEPS = [
    ('apply_mask', 'CFP_MASK'),
    ('bad_pixel',  'CFP_BPIX'),
    ('skymatch',   'CFP_SMAT'),
    ('outlier',    'CFP_OUT'),
    ('resample',   None),
]

ALL_STEPS = PROCESS_STEPS + COMBINE_STEPS
STEP_NAMES = [name for name, _ in ALL_STEPS]


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

def _run_detector1(field, config, filtname, n_processes, overwrite):
    uncals = field.get_uncal_files(filtname)
    if not uncals:
        log(f"detector1: no uncal files for {filtname}")
        return
    cfg = get_nircam_step_config('detector1', config, field)
    log(f"detector1: {len(uncals)} files for {filtname}")
    dispatch(detector1_step, uncals, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite)


def _run_persistence(field, config, filtname, n_processes, overwrite):
    exposures = field.get_exposure_files(filtname)
    if not exposures:
        log(f"persistence: no exposures for {filtname}")
        return
    cfg = get_nircam_step_config('persistence', config, field)
    persistence_step(exposures, field, cfg, overwrite=overwrite)


def _run_per_exposure(step_name, fn, field, config, filtname,
                      n_processes, overwrite):
    """Generic per-exposure parallel dispatch."""
    exposures = field.get_exposure_files(filtname)
    if not exposures:
        log(f"{step_name}: no exposures for {filtname}")
        return
    cfg = get_nircam_step_config(step_name, config, field)
    log(f"{step_name}: {len(exposures)} exposures for {filtname}")
    dispatch(fn, exposures, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite)


def _run_bad_pixel(field, config, filtname, n_processes, overwrite):
    exposures = field.get_exposure_files(filtname)
    if not exposures:
        log(f"bad_pixel: no exposures for {filtname}")
        return
    cfg = get_nircam_step_config('bad_pixel', config, field)
    # Ensemble: build per-detector masks once
    build_bad_pixel_masks(filtname, exposures, field, cfg, overwrite=overwrite)
    # Per-exposure: OR the masks into each exposure's DQ
    log(f"bad_pixel: applying to {len(exposures)} exposures for {filtname}")
    dispatch(bad_pixel_step, exposures, n_processes=n_processes,
             field=field, step_config=cfg, overwrite=overwrite)


def _run_skymatch(field, config, filtname, n_processes, overwrite):
    exposures = field.get_exposure_files(filtname)
    if not exposures:
        log(f"skymatch: no exposures for {filtname}")
        return
    cfg = get_nircam_step_config('skymatch', config, field)
    visits = _group_by_visit(exposures)
    log(f"skymatch: {len(visits)} visits for {filtname}")
    for visit, visit_files in sorted(visits.items()):
        skymatch_step(visit_files, field, cfg, overwrite=overwrite)


def _run_outlier(field, config, filtname, n_processes, overwrite):
    exposures = field.get_exposure_files(filtname)
    if not exposures:
        log(f"outlier: no exposures for {filtname}")
        return
    cfg = get_nircam_step_config('outlier', config, field)
    sregions = _read_sregions(exposures)
    visits = _group_by_visit(exposures)
    log(f"outlier: {len(visits)} visits for {filtname}")
    for visit, visit_files in sorted(visits.items()):
        outlier_step(visit, visit_files, exposures, sregions,
                     field, cfg, overwrite=overwrite)


def _run_resample(field, config, filtname, n_processes, overwrite,
                  reduction_version):
    exposures = field.get_exposure_files(filtname, with_step='CFP_OUT')
    if not exposures:
        log(f"resample: no CFP_OUT-stamped exposures for {filtname}")
        return
    cfg = get_nircam_step_config('resample', config, field)
    resample_step(filtname, exposures, field, cfg, reduction_version,
                  overwrite=overwrite)


# Dispatch table: step name → callable that takes (field, config, filtname,
# n_processes, overwrite). Resample needs reduction_version, so it's handled
# specially in run_combine / run_step.
_RUNNERS = {
    'detector1':   _run_detector1,
    'persistence': _run_persistence,
    'wisp':        lambda f, c, fl, n, ow: _run_per_exposure(
                       'wisp', wisp_step, f, c, fl, n, ow),
    'striping':    lambda f, c, fl, n, ow: _run_per_exposure(
                       'striping', striping_step, f, c, fl, n, ow),
    'image2':      lambda f, c, fl, n, ow: _run_per_exposure(
                       'image2', image2_step, f, c, fl, n, ow),
    'edge':        lambda f, c, fl, n, ow: _run_per_exposure(
                       'edge', edge_step, f, c, fl, n, ow),
    'sky':         lambda f, c, fl, n, ow: _run_per_exposure(
                       'sky', sky_step, f, c, fl, n, ow),
    'variance':    lambda f, c, fl, n, ow: _run_per_exposure(
                       'variance', variance_step, f, c, fl, n, ow),
    'jhat':        lambda f, c, fl, n, ow: _run_per_exposure(
                       'jhat', jhat_step, f, c, fl, n, ow),
    'apply_mask':  lambda f, c, fl, n, ow: _run_per_exposure(
                       'apply_mask', apply_masks_step, f, c, fl, n, ow),
    'bad_pixel':   _run_bad_pixel,
    'skymatch':    _run_skymatch,
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


def run_process(field, config, filters=None, n_processes=1, overwrite=False):
    """Run all process-phase steps in order across each filter.

    Per-exposure steps run in parallel via ``dispatch``; the per-filter
    persistence step runs serially since it operates over the whole filter
    set at once.
    """
    filters = _resolve_filters(filters, field)
    log(f"=== Process phase: field={field.name}, filters={filters} ===")
    for filt in filters:
        log(f"--- Process: {filt} ---")
        for step_name, _ in PROCESS_STEPS:
            _RUNNERS[step_name](field, config, filt, n_processes, overwrite)


def run_combine(field, config, filters=None, n_processes=1, overwrite=False):
    """Run all combine-phase steps in order across each filter."""
    filters = _resolve_filters(filters, field)
    reduction_version = _resolve_reduction_version(config)

    log(f"=== Combine phase: field={field.name}, filters={filters} ===")
    for filt in filters:
        log(f"--- Combine: {filt} ---")
        for step_name, _ in COMBINE_STEPS:
            if step_name == 'resample':
                _run_resample(field, config, filt, n_processes, overwrite,
                              reduction_version)
            else:
                _RUNNERS[step_name](field, config, filt, n_processes, overwrite)


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
    log(f"=== Step '{step_name}': field={field.name}, filters={filters} ===")

    for filt in filters:
        if step_name == 'resample':
            reduction_version = _resolve_reduction_version(config)
            _run_resample(field, config, filt, n_processes, overwrite,
                          reduction_version)
        else:
            _RUNNERS[step_name](field, config, filt, n_processes, overwrite)
