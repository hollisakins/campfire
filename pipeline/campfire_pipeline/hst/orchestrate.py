"""Orchestration of an HST instrument's reduction of a field.

The same two phases as NIRCam (``nircam/orchestrate.py``), one instrument and
one field at a time (``cfpipe acswfc ...`` / ``cfpipe wfc3ir ...``):

**process**, on the canonical exposures, in this order::

    calibrate   RAW -> <rootname>_flc.fits / _flt.fits at the pinned context
    wcs_init    distortion model into the headers
    sky         per-chip sky, ACS amplifier offsets, variance scale
    crreject    cosmic rays, per visit (two or more exposures)
    align       per visit, to the field's reference catalog

**combine**, on working copies::

    outlier     flags across everything that overlaps, per overlap group
    resample    drizzle onto the field's tiles; background, split files, plots

Every step is recorded by its ``CFP_*`` stamp (``common/cfp.py``, key set
``HST``) and is skipped where the stamp is present, so any command can be
re-run after an interruption. The settings of a step are
``[<instrument>.<step>]`` of the config with the field's
``[<field>.<instrument>.<step>]`` on top (:func:`step_config`).

Parallelism is across exposures (calibrate, wcs_init, sky), across visits
(crreject, align) and across overlap groups (outlier); the drizzle of a tile
is one process.
"""

import hashlib
import json
import os
import shutil

from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.common.parallel import dispatch
from campfire_pipeline.config import (
    crds_context_for, crds_server_for, get_hst_step_config,
)

PROCESS_STEPS = [
    ('calibrate', 'CFP_CAL'),
    ('wcs_init',  'CFP_WCS'),
    ('sky',       'CFP_SKY'),
    ('crreject',  'CFP_CR'),
    ('align',     'CFP_ALGN'),
]
COMBINE_STEPS = [
    ('outlier',  'CFP_OUT'),
    ('resample', None),
]
ALL_STEPS = PROCESS_STEPS + COMBINE_STEPS
STEP_NAMES = [name for name, _ in ALL_STEPS]

OUTLIER_MANIFEST = 'outlier_groups.json'


def step_config(step, field, config):
    """Effective settings of one step for this field: the config's
    ``[<instrument>.<step>]`` with the field's overrides on top."""
    return get_hst_step_config(step, field.instrument, config=config,
                               overrides=field.step_overrides.get(step))


def reference_settings(config, field):
    """Where HST reference files come from: the CRDS context and server of the
    instrument, and the reference cache.

    The cache is ``[environment].HST_CRDS_PATH`` when set, otherwise
    ``$CAMPFIRE_ROOT/cache/crds_hst``. It is never the JWST ``CRDS_PATH``:
    that one is often a shared, read-only directory, and the HST files are
    downloaded into theirs on demand.
    """
    from campfire_layout import cache_path
    from campfire_pipeline.hst.steps.calibrate import HST_CRDS_SERVER

    path = (config.get('environment', {}).get('HST_CRDS_PATH')
            or str(cache_path('crds_hst', root=field.campfire_root)))
    return {
        'crds_context': crds_context_for(config, field.instrument),
        'crds_server': crds_server_for(config, field.instrument) or HST_CRDS_SERVER,
        'crds_path': os.path.expanduser(str(path)),
    }


def _has(path, key):
    return os.path.exists(path) and cfp.has_step(path, key, keyset=cfp.HST)


def _resolve_filters(filters, field):
    return list(filters) if filters else list(field.filters)


# ---------------------------------------------------------------------------
# Observing modes
# ---------------------------------------------------------------------------

def unsupported_mode_reason(header, filtname):
    """Why a RAW exposure cannot be reduced as direct imaging, or ``None``.

    The download selects imaging at the archive; this is the second gate, on
    the file itself, before any calibration runs.
    """
    from campfire_pipeline.hst.instruments import filter_name

    filt = filter_name(header)
    if filt != filtname:
        return (f'its header says filter {filt}, but it is in the {filtname} '
                f'directory')
    if filt.startswith(('g', 'pr')):
        return f'dispersed element {filt}'
    if str(header.get('IMAGETYP', 'EXT')).strip().upper() != 'EXT':
        return f"IMAGETYP = {header.get('IMAGETYP')} (not a sky exposure)"
    if header.get('SUBARRAY') in (True, 'T'):
        return 'subarray readout'
    if str(header.get('SCAN_TYP', 'N')).strip().upper() not in ('N', ''):
        return f"spatial scan (SCAN_TYP = {header.get('SCAN_TYP')})"
    if header.get('MTFLAG') in (True, 'T'):
        return 'moving target'
    return None


def _check_modes(raw_files, filtname, field):
    bad = []
    for f in raw_files:
        reason = unsupported_mode_reason(fits.getheader(f, 0), filtname)
        if reason:
            bad.append((os.path.basename(f).split('_')[0], reason))
    if bad:
        listing = '\n'.join(f'  {root}: {reason}' for root, reason in bad)
        raise RuntimeError(
            f'{len(bad)} selected {field.instrument} {filtname} exposure(s) '
            f'cannot be reduced as direct imaging:\n{listing}\n\nAdd them to '
            f'[{field.name}.{field.instrument}].skip in fields.toml, or '
            f'narrow the `files` globs.')


# ---------------------------------------------------------------------------
# Workers (module level, so a process pool can pickle them)
# ---------------------------------------------------------------------------

def _calibrate_worker(raw_file, *, out_dir, refs, nthreads, keep_intermediates,
                      overwrite):
    from campfire_pipeline.hst.steps.calibrate import calibrate_step
    return calibrate_step(raw_file, out_dir, nthreads=nthreads,
                          overwrite=overwrite,
                          keep_intermediates=keep_intermediates, **refs)


def _wcs_init_worker(exposure_file, *, reference_env, overwrite):
    from campfire_pipeline.hst.steps.wcs_init import wcs_init_step
    return wcs_init_step(exposure_file, reference_env=reference_env,
                         overwrite=overwrite)


def _sky_worker(exposure_file, *, cfg, overwrite):
    from campfire_pipeline.hst.steps.sky import sky_step
    sky_step(exposure_file, step_config=cfg, overwrite=overwrite)
    return exposure_file


def _crreject_worker(key, files, *, cfg, overwrite):
    from campfire_pipeline.hst.steps.crreject import crreject_step
    crreject_step(list(files), step_config=cfg, overwrite=overwrite)
    return key


def _align_worker(key, files, *, refcat, cfg, refcat_hash):
    from campfire_pipeline.hst.steps.align import align_visit
    sol = align_visit(list(files), refcat, key=key, config=cfg,
                      refcat_hash=refcat_hash, overwrite=True)
    failed = []
    if sol is not None:
        if sol.status != 'SOLVED':
            failed = [os.path.basename(f) for f in files]
        else:
            failed = [d.detector for d in (sol.detectors or [])
                      if not getattr(d, 'aligned', True)]
    return key, (None if sol is None else sol.status), failed


def _outlier_worker(files, *, cfg, refcat):
    from campfire_pipeline.hst.steps.outlier import outlier_step
    counts = outlier_step(list(files), step_config=cfg, overwrite=True,
                          refcat=refcat)
    return sum(counts.values()) if counts else 0


# ---------------------------------------------------------------------------
# Process steps
# ---------------------------------------------------------------------------

def _run_calibrate(field, config, filt, n_processes, overwrite):
    from campfire_pipeline.hst.steps.calibrate import prefetch_references

    raws = field.get_raw_files(filt)
    if not raws:
        log(f'calibrate: no RAW files for {filt} match the globs of '
            f'[{field.name}.{field.instrument}] under '
            f'{os.path.join(field.raw_root, "*", filt)}. Download them with '
            f'`cfpipe download --telescope hst --instrument {field.instrument} '
            f'--field {field.name}`.')
        return
    _check_modes(raws, filt, field)
    pending = [r for r in raws
               if overwrite or not _has(field.canonical_path(r, filt), 'CFP_CAL')]
    log(f'calibrate: {len(raws)} RAW {filt} exposures, {len(pending)} to calibrate')
    if not pending:
        return
    refs = reference_settings(config, field)
    prefetch_references(pending, **refs)
    cfg = step_config('calibrate', field, config)
    dispatch(_calibrate_worker, pending, n_processes=n_processes,
             out_dir=field.filter_dir(filt), refs=refs,
             nthreads=cfg.get('nthreads'),
             keep_intermediates=bool(cfg.get('keep_intermediates', False)),
             overwrite=overwrite)


def _run_wcs_init(field, config, filt, n_processes, overwrite):
    from campfire_pipeline.hst.steps.calibrate import reference_dir

    files = field.get_exposure_files(filt)
    pending = [f for f in files if overwrite or not _has(f, 'CFP_WCS')]
    log(f'wcs_init: {len(files)} {filt} exposures, {len(pending)} to do')
    if not pending:
        return
    var, refdir = reference_dir(reference_settings(config, field)['crds_path'],
                                field.instrument)
    dispatch(_wcs_init_worker, pending, n_processes=n_processes,
             reference_env={var: refdir}, overwrite=overwrite)


def _run_sky(field, config, filt, n_processes, overwrite):
    files = [f for f in field.get_exposure_files(filt) if _has(f, 'CFP_WCS')]
    pending = [f for f in files if overwrite or not _has(f, 'CFP_SKY')]
    log(f'sky: {len(files)} {filt} exposures, {len(pending)} to do')
    if not pending:
        return
    dispatch(_sky_worker, pending, n_processes=n_processes,
             cfg=step_config('sky', field, config), overwrite=overwrite)


def _visits(field, filt, need):
    """Visits of a filter, from the canonical exposures that carry ``need``."""
    from campfire_pipeline.hst.visit import group_visits

    files = [f for f in field.get_exposure_files(filt) if _has(f, need)]
    return group_visits(files)


def _run_crreject(field, config, filt, n_processes, overwrite):
    visits = _visits(field, filt, 'CFP_SKY')
    tasks, single = [], []
    for v in visits:
        if len(v.files) < 2:
            single.append(v)
        elif overwrite or not all(_has(f, 'CFP_CR') for f in v.files):
            tasks.append((v.key, v.files))
    log(f'crreject: {len(visits)} {filt} visits, {len(tasks)} to do')
    if single:
        names = ', '.join(os.path.basename(v.files[0]).split('_')[0] for v in single)
        if field.instrument == 'wfc3ir':
            log(f'crreject: {len(single)} visit(s) of a single exposure ({names}); '
                f'the ramp fit of calwf3 has removed most cosmic rays, and the '
                f'field-level outlier pass sees them where they overlap others')
        else:
            log(f'crreject: {len(single)} visit(s) of a single exposure ({names}) '
                f'get no cosmic-ray flags and are HELD BACK from the mosaic. '
                f'Cleaning a lone CCD exposure from its own pixels is not built.')
    if tasks:
        dispatch(_crreject_worker, tasks, n_processes=n_processes,
                 use_starmap=True, cfg=step_config('crreject', field, config),
                 overwrite=overwrite)


def check_align_config(field):
    """Stop early when the field has no usable reference catalog."""
    path = field.refcat_path()
    hint = (f'Set refcat = "<file>.ecsv" under [{field.name}] in fields.toml: '
            f'the one reference catalog every instrument of the field is '
            f'aligned to, a file in {field.refcat_dir}.')
    if path is None:
        raise ValueError(
            f"field '{field.name}' names no reference catalog, which the "
            f"{field.instrument} align step needs. {hint}")
    if not os.path.exists(path):
        raise ValueError(f'reference catalog not found: {path}. {hint}')
    return path


def _algn_pending(files, refcat_hash, overwrite):
    """Whether a visit still needs a solve: any exposure without a stamp, left
    ``NOT_ALIGNED`` (the settings may have been retuned), or solved against a
    reference catalog that has changed since."""
    from campfire_pipeline.nircam.orchestrate import _algn_refcat_hash

    if overwrite:
        return True
    for f in files:
        value = cfp.step_value(f, 'CFP_ALGN', keyset=cfp.HST)
        if (value is None or value == cfp.NOT_ALIGNED
                or _algn_refcat_hash(value) != refcat_hash):
            return True
    return False


def _run_align(field, config, filt, n_processes, overwrite):
    from campfire_pipeline.imaging.refcat.io import read_refcat
    from campfire_pipeline.nircam.orchestrate import _refcat_hash

    refcat_path = check_align_config(field)
    refcat_hash = _refcat_hash(refcat_path)
    # A CCD exposure is aligned on its cosmic-ray-cleaned pixels: without the
    # flags a 400 s ACS exposure gives thousands of false detections and the
    # fit fails. A visit that has no flags (not through crreject yet, or a
    # single exposure, which is held back from the mosaic anyway) is not
    # solved. WFC3/IR does not depend on it.
    visits = _visits(field, filt, 'CFP_SKY')
    if field.instrument == 'acswfc':
        visits = [v for v in visits if all(_has(f, 'CFP_CR') for f in v.files)]
    tasks = [(v.key, v.files) for v in visits
             if _algn_pending(v.files, refcat_hash, overwrite)]
    log(f'align: {len(visits)} {filt} visits, {len(tasks)} to solve '
        f'(refcat {os.path.basename(refcat_path)}, rc={refcat_hash})')
    if not tasks:
        return
    refcat = read_refcat(refcat_path)
    results = dispatch(_align_worker, tasks, n_processes=n_processes,
                       use_starmap=True, refcat=refcat,
                       cfg=step_config('align', field, config),
                       refcat_hash=refcat_hash)
    failed = [(key, names) for key, _status, names in results if names]
    if failed:
        bar = '!' * 72
        log('')
        log(bar)
        log(f'align: WARNING: {len(failed)} {filt} visit(s) have exposures that '
            f'FAILED alignment (CFP_ALGN = {cfp.NOT_ALIGNED}).')
        log('They are kept out of every mosaic until they are aligned:')
        for key, names in failed:
            log(f"  {key}: {', '.join(names)}")
        log(f'To retry: change [{field.name}.{field.instrument}.align] in '
            f'fields.toml and run `cfpipe {field.instrument} align` again '
            f'(failed visits are re-attempted without --overwrite).')
        log(f'To drizzle them as they are: `cfpipe {field.instrument} combine '
            f'--include-unaligned`.')
        log(bar)
        log('')


_PROCESS_RUNNERS = {
    'calibrate': _run_calibrate,
    'wcs_init': _run_wcs_init,
    'sky': _run_sky,
    'crreject': _run_crreject,
    'align': _run_align,
}


def run_process(field, config, filters=None, n_processes=1, overwrite=False):
    """Run the process phase, step by step, over each filter."""
    filters = _resolve_filters(filters, field)
    # align is the last step; stop now if it could not run
    check_align_config(field)
    log(f'=== Process phase: field={field.name}, instrument={field.instrument}, '
        f'filters={filters} ===')
    for filt in filters:
        log(f'--- Process: {field.instrument} {filt} ---')
        for name, _ in PROCESS_STEPS:
            _PROCESS_RUNNERS[name](field, config, filt, n_processes, overwrite)


# ---------------------------------------------------------------------------
# Combine steps
# ---------------------------------------------------------------------------

def overlap_groups(files):
    """Split exposures into groups that overlap on the sky, directly or
    through other exposures. Returns a list of sorted lists, largest group
    first."""
    from campfire_pipeline.hst.exposure import open_exposure
    from campfire_pipeline.hst.steps.resample import exposure_polygon

    files = sorted(files)
    polys = [exposure_polygon(open_exposure(f)) for f in files]
    parent = list(range(len(files)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            if find(i) != find(j) and polys[i].intersects(polys[j]):
                parent[find(j)] = find(i)
    groups = {}
    for i, f in enumerate(files):
        groups.setdefault(find(i), []).append(f)
    return sorted(groups.values(), key=lambda g: (-len(g), g[0]))


def _config_hash(cfg):
    text = json.dumps(cfg, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _load_outlier_manifest(work_dir):
    path = os.path.join(work_dir, OUTLIER_MANIFEST)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return list(json.load(f).get('groups', []))
    except (ValueError, OSError):
        return []


def _materialize(field, filt, overwrite, include_unaligned):
    paths, held = field.materialize_work(filt, overwrite=overwrite,
                                         include_unaligned=include_unaligned)
    if held:
        reasons = {}
        for src, reason in held:
            reasons.setdefault(reason, []).append(
                os.path.basename(src).split('_')[0])
        log(f'combine: {len(held)} {filt} exposure(s) are HELD BACK from the '
            f'mosaic:')
        for reason, names in sorted(reasons.items()):
            log(f"  {reason}: {', '.join(names)}")
    return paths


def _run_outlier(field, config, filt, n_processes, overwrite, paths):
    """Field-level outlier flags for the working copies of one filter.

    The exposures are split into overlap groups; each group of two or more is
    one outlier pass. A group is redone when its membership, the reference
    catalog or the settings changed since the pass that flagged it (recorded
    in ``outlier_groups.json`` next to the working copies), or when a member
    has no flags yet.
    """
    from campfire_pipeline.nircam.orchestrate import _refcat_hash

    work_dir = field.filter_dir(filt, work=True)
    cfg = step_config('outlier', field, config)
    refcat_path = field.refcat_path()
    refcat, refcat_hash = None, None
    if refcat_path and os.path.exists(refcat_path):
        refcat_hash = _refcat_hash(refcat_path)
    else:
        log('outlier: the field has no reference catalog on disk, so no '
            'moving star is protected')
    cfg_hash = _config_hash(cfg)

    def record(group):
        return {'members': [os.path.basename(f) for f in group],
                'refcat': refcat_hash, 'config': cfg_hash}

    groups = overlap_groups(paths)
    done_before = _load_outlier_manifest(work_dir)
    todo, current, alone = [], [], []
    for group in groups:
        if len(group) < 2:
            alone.append(group[0])
            continue
        fresh = (not overwrite and record(group) in done_before
                 and all(_has(f, 'CFP_OUT') for f in group))
        if fresh:
            current.append(record(group))
        else:
            todo.append(group)

    # An exposure that overlaps nothing is not compared with anything. If it
    # carries flags from an earlier grouping, they are dropped with a new copy.
    for path in alone:
        if _has(path, 'CFP_OUT'):
            src = os.path.join(field.filter_dir(filt), os.path.basename(path))
            shutil.copyfile(src, path)
    log(f'outlier: {len(paths)} {filt} exposures in {len(groups)} overlap '
        f'group(s): {len(todo)} to do, {len(current)} up to date, '
        f'{len(alone)} exposure(s) overlapping nothing')
    if todo:
        if refcat_hash is not None:
            from campfire_pipeline.imaging.refcat.io import read_refcat
            refcat = read_refcat(refcat_path)
        dispatch(_outlier_worker, todo, n_processes=min(n_processes, len(todo)),
                 cfg=cfg, refcat=refcat)
        current += [record(g) for g in todo]
    with open(os.path.join(work_dir, OUTLIER_MANIFEST), 'w') as f:
        json.dump({'groups': current}, f, indent=2)


def _run_resample(field, config, filt, overwrite, reduction_version, tiles, paths):
    from campfire_pipeline.hst.steps.resample import resample_step

    return resample_step(filt, paths, field, step_config('resample', field, config),
                         reduction_version, overwrite=overwrite, tiles=tiles)


def run_combine(field, config, filters=None, n_processes=1, overwrite=False,
                tiles=None, include_unaligned=False, steps=('outlier', 'resample')):
    """Run the combine phase over each filter: refresh the working copies,
    flag outliers across overlapping exposures, drizzle the tiles.

    ``tiles`` limits which mosaics are built; the outlier pass always sees
    every exposure of the filter, so a tile's mosaic does not depend on which
    tiles were asked for.
    """
    from campfire_pipeline.common.version import get_reduction_version

    filters = _resolve_filters(filters, field)
    version = get_reduction_version(config)
    log(f'=== Combine phase: field={field.name}, instrument={field.instrument}, '
        f'filters={filters} ===')
    for filt in filters:
        log(f'--- Combine: {field.instrument} {filt} ---')
        paths = _materialize(field, filt, overwrite, include_unaligned)
        if not paths:
            log(f'combine: no {filt} exposure is ready for a mosaic')
            continue
        if 'outlier' in steps:
            _run_outlier(field, config, filt, n_processes, overwrite, paths)
        if 'resample' in steps:
            _run_resample(field, config, filt, overwrite, version, tiles, paths)


def run_step(step_name, field, config, filters=None, n_processes=1,
             overwrite=False, tiles=None):
    """Run one named step over the field's filters."""
    if step_name not in STEP_NAMES:
        raise ValueError(f"Unknown step '{step_name}'. Known: {STEP_NAMES}")
    if step_name in ('outlier', 'resample'):
        return run_combine(field, config, filters=filters,
                           n_processes=n_processes, overwrite=overwrite,
                           tiles=tiles, steps=(step_name,))
    filters = _resolve_filters(filters, field)
    log(f"=== Step '{step_name}': field={field.name}, "
        f'instrument={field.instrument}, filters={filters} ===')
    for filt in filters:
        _PROCESS_RUNNERS[step_name](field, config, filt, n_processes, overwrite)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def exposure_status(field, filt):
    """``{rootname: {CFP key: value}}`` for the canonical exposures of a
    filter, with ``CFP_OUT`` taken from the working copy where one exists."""
    out = {}
    work_dir = field.filter_dir(filt, work=True)
    for f in field.get_exposure_files(filt):
        steps = cfp.get_steps(f, keyset=cfp.HST)
        work = os.path.join(work_dir, os.path.basename(f))
        if os.path.exists(work):
            value = cfp.step_value(work, 'CFP_OUT', keyset=cfp.HST)
            if value is not None:
                steps['CFP_OUT'] = value
        out[os.path.basename(f).split('_')[0]] = steps
    return out


def stale_tiles(field, config, filt, tiles=None):
    """Which mosaics of a filter would be rebuilt by ``resample`` now."""
    from campfire_pipeline.hst.steps.resample import tile_status

    paths = field.get_exposure_files(filt, work=True)
    return tile_status(filt, paths, field,
                       step_config('resample', field, config), tiles=tiles)
