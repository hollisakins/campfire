"""Orchestration of an HST instrument's reduction of a field.

The same two phases as NIRCam (``nircam/orchestrate.py``), one instrument and
one field at a time (``cfpipe acswfc ...`` / ``cfpipe wfc3ir ...``):

**process**, on the canonical exposures, in this order::

    calibrate   RAW -> <rootname>_flc.fits / _flt.fits at the pinned context
    wcs_init    distortion model into the headers
    sky         per-chip sky, ACS amplifier offsets, variance scale
    trails      satellite trails -> the exposure's region file (ACS/WFC only)
    crreject    cosmic rays, per visit (two or more exposures)
    preview     the two review PNGs of each exposure
    align       per visit, to the field's reference catalog

**combine**::

    apply_masks hand-drawn region files -> CFMASK on the canonical exposures
    outlier     flags across everything that overlaps, per overlap group
    resample    drizzle onto the field's tiles; background, split files, plots

``outlier`` and ``resample`` work on copies, and ``outlier`` goes visit by
visit as for NIRCam. What a reviewer decides reaches a reduction as it does
for NIRCam: ``reference/<instrument>/<field>/`` holds
``masks/<filter>/<file>.reg`` and ``exposures.json`` (exposures left out
altogether). The satellite trails the ``trails`` step finds go into those same
region files as preliminary masks. There is no automatic quality gate
(Hollis, 2026-11-16: no heavier than NIRCam), and no exposure is kept out of
a mosaic without a banner that names it and says why
(:func:`_report_held_back`).

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
    ('trails',    'CFP_TRL'),
    ('crreject',  'CFP_CR'),
    ('preview',   'CFP_PREV'),
    ('align',     'CFP_ALGN'),
]
COMBINE_STEPS = [
    ('apply_masks', 'CFP_MASK'),
    ('outlier',  'CFP_OUT'),
    ('resample', None),
]
ALL_STEPS = PROCESS_STEPS + COMBINE_STEPS
STEP_NAMES = [name for name, _ in ALL_STEPS]

# Steps an instrument does not have (none at present: every HST instrument
# runs every step).
_NOT_FOR = {}


def steps_for(instrument, steps=None):
    """``(name, CFP key)`` of the steps of one instrument, in order."""
    skip = _NOT_FOR.get(instrument, ())
    return [(n, k) for n, k in (ALL_STEPS if steps is None else steps)
            if n not in skip]

def outlier_manifest_path(work_dir, visit):
    """The record of one visit's outlier pass, next to the working copies
    that hold its flags (NIRCam's name: ``outlier_<visit>_manifest.json``)."""
    return os.path.join(work_dir, f'outlier_{visit}_manifest.json')


def outlier_manifests(work_dir):
    """Every outlier record in a working directory (the per-visit files, and
    the single ``outlier_groups.json`` of the pass that preceded them)."""
    from glob import glob
    found = glob(os.path.join(work_dir, 'outlier_*_manifest.json'))
    old = os.path.join(work_dir, 'outlier_groups.json')
    return sorted(found) + ([old] if os.path.exists(old) else [])


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
                      overwrite, reduction_version=None):
    from campfire_pipeline.hst.steps.calibrate import calibrate_step
    return calibrate_step(raw_file, out_dir, nthreads=nthreads,
                          overwrite=overwrite,
                          keep_intermediates=keep_intermediates,
                          reduction_version=reduction_version, **refs)


def _wcs_init_worker(exposure_file, *, reference_env, overwrite):
    from campfire_pipeline.hst.steps.wcs_init import wcs_init_step
    return wcs_init_step(exposure_file, reference_env=reference_env,
                         overwrite=overwrite)


def _sky_worker(exposure_file, *, cfg, overwrite):
    from campfire_pipeline.hst.steps.sky import sky_step
    sky_step(exposure_file, step_config=cfg, overwrite=overwrite)
    return exposure_file


def _trails_worker(exposure_file, reg_file, *, cfg, overwrite, processes):
    from campfire_pipeline.hst.steps.trails import trails_step
    found = trails_step(exposure_file, reg_file, step_config=cfg,
                        overwrite=overwrite, processes=processes)
    masked = sum(1 for ts in (found or {}).values() for t in ts if t['accepted'])
    return exposure_file, masked


def _preview_worker(exposure_file, *, cfg, overwrite):
    from campfire_pipeline.hst.steps.preview import preview_step
    preview_step(exposure_file, step_config=cfg, overwrite=overwrite)
    return exposure_file


def _crreject_worker(key, files, *, cfg, overwrite):
    from campfire_pipeline.hst.steps.crreject import crreject_step
    crreject_step(list(files), step_config=cfg, overwrite=overwrite)
    return key


def _crreject_single_worker(exposure_file, *, cfg, overwrite):
    from campfire_pipeline.hst.steps.crreject import crreject_single_step
    crreject_single_step(exposure_file, step_config=cfg, overwrite=overwrite)
    return exposure_file


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


def _outlier_worker(visit_files, context_files, *, cfg, refcat):
    from campfire_pipeline.hst.steps.outlier import outlier_step
    counts = outlier_step(list(visit_files), list(context_files),
                          step_config=cfg, overwrite=True, refcat=refcat)
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
    from campfire_pipeline.common.version import get_reduction_version
    dispatch(_calibrate_worker, pending, n_processes=n_processes,
             out_dir=field.filter_dir(filt), refs=refs,
             nthreads=cfg.get('nthreads'),
             keep_intermediates=bool(cfg.get('keep_intermediates', False)),
             overwrite=overwrite,
             reduction_version=get_reduction_version(config))


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


def _run_trails(field, config, filt, n_processes, overwrite):
    """Satellite trails, ACS/WFC only, into the exposures' region files."""
    if 'trails' in _NOT_FOR.get(field.instrument, ()):
        return
    files = [f for f in field.get_exposure_files(filt) if _has(f, 'CFP_SKY')]
    pending = [f for f in files if overwrite or not _has(f, 'CFP_TRL')]
    log(f'trails: {len(files)} {filt} exposures, {len(pending)} to search')
    if not pending:
        return
    # One exposure at a time, the finder on all the processes: it must run on
    # at least two to give its documented result (steps/trails.py), and a
    # worker of a process pool cannot start processes of its own.
    cfg = step_config('trails', field, config)
    results = [_trails_worker(f, field.mask_file(f, filt), cfg=cfg,
                              overwrite=overwrite,
                              processes=max(2, n_processes))
               for f in pending]
    hit = sorted(os.path.basename(f).split('_')[0] for f, n in results if n)
    if hit:
        log(f'trails: satellite trails found on {len(hit)} of {len(pending)} '
            f'{filt} exposure(s): {", ".join(hit)}. Their masks are region '
            f'files in {os.path.join(field.mask_dir, filt)}.')


def _run_preview(field, config, filt, n_processes, overwrite):
    """Review PNGs, once the flags they blank are in: after crreject on
    ACS/WFC (every exposure gets cosmic-ray flags there), after sky on
    WFC3/IR."""
    from campfire_pipeline.hst.steps.preview import PREVIEW_KIND, preview_paths

    need = 'CFP_CR' if field.instrument == 'acswfc' else 'CFP_SKY'
    files = [f for f in field.get_exposure_files(filt) if _has(f, need)]

    def done(f):
        return (all(os.path.exists(p) for p in preview_paths(f))
                and str(cfp.step_value(f, 'CFP_PREV', keyset=cfp.HST)
                        ).startswith(PREVIEW_KIND))

    pending = [f for f in files if overwrite or not done(f)]
    log(f'preview: {len(files)} {filt} exposures, {len(pending)} to render')
    if not pending:
        return
    dispatch(_preview_worker, pending,
             n_processes=max(1, min(n_processes, len(pending))),
             cfg=step_config('preview', field, config), overwrite=True)


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
            lone = [v.files[0] for v in single
                    if overwrite or not _has(v.files[0], 'CFP_CR')]
            log(f'crreject: {len(single)} visit(s) of a single exposure ({names}) '
                f'are cleaned from their own pixels, {len(lone)} to do. That '
                f'removes most cosmic rays, not all (about 0.1 to 0.2 percent of '
                f'pixels keep one above 5 sigma); where such an exposure overlaps '
                f'others the field-level outlier pass removes the rest.')
            if lone:
                dispatch(_crreject_single_worker, lone, n_processes=n_processes,
                         cfg=step_config('crreject', field, config),
                         overwrite=overwrite)
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


def _warn_no_motions(refcat, refcat_path):
    """Say so when a reference catalog carries no proper motion at all.

    HST exposures are often a decade or two from the imaging a catalog was
    made from. A catalog built by ``cfpipe nircam refcat extract`` gives Gaia
    stars their proper motions and drops compact sources that have none; one
    without any proper motion has skipped that, and whatever stars it holds
    are at the catalog's epoch only. Measured on COSMOS ACS F814W of 2004
    against a plain cut of a 2023 catalog: the stars sat 70 mas from their
    catalog positions and pulled the alignment 6 mas off the galaxies
    (docs/hst/design.md, section 8b).
    """
    import numpy as np

    names = set(refcat.colnames)
    has = False
    if {'pmra', 'pmdec'} <= names:
        pm = np.asarray(refcat['pmra'], dtype=float)
        has = bool(np.isfinite(pm).any())
    if not has:
        log(f'align: WARNING: {os.path.basename(refcat_path)} has no proper '
            f'motions. Stars in it are at the positions of the catalog\'s own '
            f'epoch and pull the fit of exposures from another epoch (6 mas '
            f'measured over 19 years). `cfpipe nircam refcat add-gaia` gives '
            f'an existing catalog Gaia\'s stars with their motions and drops '
            f'the other point sources; `refcat extract` does the same to a '
            f'catalog it makes.')


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
    # fit fails. A visit that has not been through crreject yet is not
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
    _warn_no_motions(refcat, refcat_path)
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
    'trails': _run_trails,
    'crreject': _run_crreject,
    'preview': _run_preview,
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
        for name, _ in steps_for(field.instrument, PROCESS_STEPS):
            _PROCESS_RUNNERS[name](field, config, filt, n_processes, overwrite)


# ---------------------------------------------------------------------------
# Combine steps
# ---------------------------------------------------------------------------

def program_code(path):
    """Program of an HST exposure, from its file name: the ``ppp`` of the
    rootname ``ipppssoot`` (one code per program and instrument)."""
    return os.path.basename(path)[1:4]


def overlap_inputs(visit_files, filter_files, polygons, cross_program):
    """Exposures of other visits that overlap a visit's exposures.

    The rule is the one NIRCam uses, one function for every imaging
    instrument (``imaging/outlier_visits.py``): only exposures of the visit's
    own program by default, of any program with ``cross_program``. HST
    supplies its footprints (``polygons`` maps each file to its footprint on
    the sky) and its program (:func:`program_code`).
    """
    from campfire_pipeline.imaging.outlier_visits import overlap_inputs as rule

    return rule(visit_files, filter_files,
                lambda a, b: polygons[a].intersects(polygons[b]),
                program_code, cross_program)


def _config_hash(cfg):
    text = json.dumps(cfg, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _visit_is_up_to_date(visit, visit_files, canonical_inputs, manifest_path,
                         refcat_hash, cfg_hash):
    """Whether a visit can skip the outlier pass: every one of its working
    copies carries ``CFP_OUT``, and the record of the pass that wrote those
    flags names the same inputs, unchanged, the same reference catalog and
    the same settings. Logs the reason when it cannot.

    The inputs are identified by their canonical files: the working copies
    change when their own visit's flags are written, the exposures those
    flags were derived from do not.
    """
    from campfire_pipeline.imaging.manifest import file_unchanged, load_manifest

    if not all(_has(f, 'CFP_OUT') for f in visit_files):
        return False
    manifest = load_manifest(manifest_path)
    if manifest is None:
        log(f'  outlier: no record for visit {visit}; running')
        return False
    old = {e['filename']: e for e in manifest.get('inputs', [])}
    if sorted(old) != sorted(os.path.basename(f) for f in canonical_inputs):
        log(f'  outlier: the exposures compared with visit {visit} changed')
        return False
    changed = [os.path.basename(f) for f in canonical_inputs
               if not file_unchanged(f, old[os.path.basename(f)])]
    if changed:
        log(f"  outlier: inputs of visit {visit} changed: {', '.join(changed)}")
        return False
    if manifest.get('refcat') != refcat_hash or manifest.get('config') != cfg_hash:
        log(f'  outlier: the reference catalog or the settings changed since '
            f'visit {visit} was flagged')
        return False
    return True


def _run_apply_masks(field, filt, overwrite=False):
    """Bring ``CFMASK`` on the canonical exposures of a filter in line with
    the hand-drawn region files. Cheap when nothing changed (one header read
    and one small hash per exposure), so every combine does it first: an
    edited region file takes effect on the next combine, with no reset."""
    from campfire_pipeline.hst.steps.apply_masks import (
        apply_masks_step, is_current,
    )

    files = field.get_exposure_files(filt)
    pending = [f for f in files
               if overwrite or not is_current(f, field.mask_file(f, filt))]
    with_reg = sum(1 for f in files if os.path.exists(field.mask_file(f, filt)))
    log(f'apply_masks: {len(files)} {filt} exposures, {with_reg} with a region '
        f'file in {os.path.join(field.mask_dir, filt)}, {len(pending)} to update')
    for f in pending:
        apply_masks_step(f, field.mask_file(f, filt), overwrite=True)


_NOT_CALIBRATED = 'not calibrated yet (RAW file selected, no calibrated exposure)'
_NO_OUTLIER_PASS = 'not through the outlier pass (its working copy has no CFP_OUT)'
_HELD_BACK_FIX = {
    _NOT_CALIBRATED: 'run `cfpipe {inst} process --field {field}`',
    _NO_OUTLIER_PASS:
        'run `cfpipe {inst} combine --field {field}` (or `outlier`, then '
        '`resample`)',
    'not aligned yet': 'run `cfpipe {inst} align --field {field}`',
    'alignment failed (NOT_ALIGNED)':
        'change [{field}.{inst}.align] and run `cfpipe {inst} align` again, '
        'or drizzle them as they are with `combine --include-unaligned`',
    'no cosmic-ray flags yet (crreject has not run)':
        'run `cfpipe {inst} process --field {field}`',
}


def uncalibrated(field, filt):
    """Rootnames of the RAW files the field selects for a filter that have no
    calibrated exposure yet. A combine cannot see those at all, so without
    this check a mosaic made halfway through a reduction would be short of
    exposures and say nothing."""
    have = {os.path.basename(f).split('_')[0]
            for f in field.get_exposure_files(filt)}
    return sorted({os.path.basename(f).split('_')[0]
                   for f in field.get_raw_files(filt)} - have)


def held_back(field, filt, include_unaligned=False):
    """``{reason: [rootname, ...]}`` for the selected exposures of a filter
    that a combine would keep out of the mosaic. Reads headers only."""
    reasons = {}
    missing = uncalibrated(field, filt)
    if missing:
        reasons[_NOT_CALIBRATED] = missing
    for src in field.get_exposure_files(filt):
        ready, reason = field.combine_ready(src, include_unaligned)
        if not ready:
            reasons.setdefault(reason, []).append(
                os.path.basename(src).split('_')[0])
    return reasons


def _report_held_back(field, filt, reasons):
    """A banner for exposures that are selected and do not go into the mosaic.

    Leaving data out is sometimes right (an exposure drizzled where it is not
    doubles every source) and never something to do quietly: the banner names
    every exposure, the reason and what to run."""
    if not reasons:
        return
    n = sum(len(v) for v in reasons.values())
    bar = '!' * 72
    log('')
    log(bar)
    log(f'combine: WARNING: {n} selected {field.instrument} {filt} exposure(s) '
        f'are NOT IN THE MOSAIC:')
    for reason, names in sorted(reasons.items()):
        fix = _HELD_BACK_FIX.get(reason, '').format(inst=field.instrument,
                                                    field=field.name)
        log(f"  {reason} ({len(names)}): {', '.join(names)}")
        if fix:
            log(f'    to bring them in: {fix}')
    log(bar)
    log('')


def _materialize(field, filt, overwrite, include_unaligned):
    paths, held = field.materialize_work(filt, overwrite=overwrite,
                                         include_unaligned=include_unaligned)
    reasons = {}
    for src, reason in held:
        reasons.setdefault(reason, []).append(os.path.basename(src).split('_')[0])
    _report_held_back(field, filt, reasons)
    return paths, reasons


def _run_outlier(field, config, filt, n_processes, overwrite, paths):
    """Outlier flags for the working copies of one filter, visit by visit.

    The NIRCam scheme (``nircam/orchestrate.py`` and ``nircam/steps/outlier.py``):
    each visit's exposures are flagged against themselves and the exposures
    of other visits that overlap them, of the same program by default and of
    any program with ``[<instrument>.outlier].cross_program_overlap = true``.
    A visit is redone when one of its working copies has no flags, or when
    the exposures it was compared with, the reference catalog or the settings
    differ from the record of the pass that flagged it
    (``outlier_<visit>_manifest.json`` next to the working copies).
    """
    from campfire_pipeline.hst.exposure import open_exposure
    from campfire_pipeline.hst.steps.resample import exposure_polygon
    from campfire_pipeline.hst.visit import group_visits
    from campfire_pipeline.imaging.manifest import whole_file_entry, write_manifest
    from campfire_pipeline.nircam.orchestrate import _refcat_hash

    work_dir = field.filter_dir(filt, work=True)
    canon_dir = field.filter_dir(filt)
    cfg = step_config('outlier', field, config)
    cross_program = bool(cfg.get('cross_program_overlap', False))
    refcat_path = field.refcat_path()
    refcat, refcat_hash = None, None
    if refcat_path and os.path.exists(refcat_path):
        refcat_hash = _refcat_hash(refcat_path)
    else:
        log('outlier: the field has no reference catalog on disk, so no '
            'moving star is protected')
    cfg_hash = _config_hash(cfg)

    def canonical(path):
        return os.path.join(canon_dir, os.path.basename(path))

    polygons = {f: exposure_polygon(open_exposure(f)) for f in paths}
    visits = group_visits(paths)
    tasks, records, alone = [], [], 0
    for v in visits:
        files = list(v.files)
        context = overlap_inputs(files, paths, polygons, cross_program)
        inputs = [canonical(f) for f in files + context]
        manifest_path = outlier_manifest_path(work_dir, v.visit)
        if len(files) + len(context) < 2:
            alone += 1
        if not overwrite and _visit_is_up_to_date(
                v.visit, files, inputs, manifest_path, refcat_hash, cfg_hash):
            continue
        tasks.append((files, context))
        records.append((v.visit, inputs, manifest_path))
    known = {outlier_manifest_path(work_dir, v.visit) for v in visits}
    for stale in outlier_manifests(work_dir):
        if stale not in known:
            os.remove(stale)          # a visit that left, or the older format

    scope = 'any program' if cross_program else 'same program'
    log(f'outlier: {len(paths)} {filt} exposures in {len(visits)} visit(s), '
        f'each against what overlaps it ({scope}): {len(tasks)} to do, '
        f'{len(visits) - len(tasks)} up to date'
        + (f'; {alone} visit(s) of one exposure that nothing overlaps have '
           f'nothing to be compared with' if alone else ''))
    if not tasks:
        return
    if refcat_hash is not None:
        from campfire_pipeline.imaging.refcat.io import read_refcat
        refcat = read_refcat(refcat_path)
    dispatch(_outlier_worker, tasks, n_processes=min(n_processes, len(tasks)),
             use_starmap=True, cfg=cfg, refcat=refcat)
    entries = {}
    for visit, inputs, manifest_path in records:
        for f in inputs:
            if f not in entries:
                entries[f] = whole_file_entry(f)
        write_manifest({
            'visit': visit, 'field': field.name, 'filter': filt,
            'inputs': [entries[f] for f in sorted(inputs)],
            'refcat': refcat_hash, 'config': cfg_hash,
            'cross_program_overlap': cross_program,
        }, manifest_path)


def _run_resample(field, config, filt, overwrite, reduction_version, tiles, paths):
    from campfire_pipeline.hst.steps.resample import resample_step

    return resample_step(filt, paths, field, step_config('resample', field, config),
                         reduction_version, overwrite=overwrite, tiles=tiles)


def run_combine(field, config, filters=None, n_processes=1, overwrite=False,
                tiles=None, include_unaligned=False,
                steps=('apply_masks', 'outlier', 'resample')):
    """Run the combine phase over each filter: apply the hand-drawn masks,
    refresh the working copies, flag outliers across overlapping exposures,
    drizzle the tiles.

    The masks are brought up to date whichever steps are asked for: a working
    copy is made from the canonical file, and one made before its mask was
    applied would put masked pixels into a mosaic without a word.

    ``tiles`` limits which mosaics are built; the outlier pass always sees
    every exposure of the filter, so a tile's mosaic does not depend on which
    tiles were asked for.
    """
    from campfire_pipeline.common.version import get_reduction_version

    filters = _resolve_filters(filters, field)
    version = get_reduction_version(config)
    log(f'=== Combine phase: field={field.name}, instrument={field.instrument}, '
        f'filters={filters} ===')
    left_out = {}
    for filt in filters:
        log(f'--- Combine: {field.instrument} {filt} ---')
        _run_apply_masks(field, filt,
                         overwrite=overwrite and 'apply_masks' in steps)
        if not set(steps) & {'outlier', 'resample'}:
            continue
        # A working copy is replaced by a fresh one (which has no outlier
        # flags) only in a run that goes on to flag it. `resample --overwrite`
        # on its own re-drizzles from the flagged copies that are there.
        paths, reasons = _materialize(field, filt,
                                      overwrite and 'outlier' in steps,
                                      include_unaligned)
        missing = uncalibrated(field, filt)
        if missing:
            reasons = dict(reasons)
            reasons[_NOT_CALIBRATED] = missing
            _report_held_back(field, filt, {_NOT_CALIBRATED: missing})
        if reasons:
            left_out[filt] = reasons
        if not paths:
            log(f'combine: no {filt} exposure is ready for a mosaic')
            continue
        if 'outlier' in steps:
            _run_outlier(field, config, filt, n_processes, overwrite, paths)
        if 'resample' in steps:
            # Only exposures the outlier pass has stamped go into a mosaic, as
            # for NIRCam (the pass stamps every exposure it handles, one that
            # nothing overlaps included). A working copy without the stamp is
            # fresh: made after its canonical file changed, or never flagged.
            # That happens only when resample runs without the outlier step.
            # Drizzling it would put its uncompared pixels in the mosaic, and
            # drizzling the rest would rebuild the mosaic short of it; the
            # filter is left as it is and the run says what to do.
            unflagged = sorted(os.path.basename(p).split('_')[0]
                               for p in paths if not _has(p, 'CFP_OUT'))
            if unflagged:
                reasons = dict(reasons)
                reasons[_NO_OUTLIER_PASS] = unflagged
                left_out[filt] = reasons
                _report_held_back(field, filt, {_NO_OUTLIER_PASS: unflagged})
                log(f'resample: {filt} not drizzled: {len(unflagged)} of '
                    f'{len(paths)} exposure(s) have not been through the '
                    f'outlier pass, and a mosaic is made of all of them or '
                    f'not at all. Run `cfpipe {field.instrument} combine '
                    f'--field {field.name}`.')
                continue
            _run_resample(field, config, filt, overwrite, version, tiles, paths)
    # Say it again where a long log ends: the mosaics above are missing these.
    for filt, reasons in left_out.items():
        _report_held_back(field, filt, reasons)
    return left_out


def run_step(step_name, field, config, filters=None, n_processes=1,
             overwrite=False, tiles=None):
    """Run one named step over the field's filters."""
    names = [n for n, _ in steps_for(field.instrument)]
    if step_name not in names:
        raise ValueError(
            f"Unknown {field.instrument} step '{step_name}'. Known: {names}")
    if step_name in ('apply_masks', 'outlier', 'resample'):
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
