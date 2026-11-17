"""calibrate: RAW exposure to canonical calibrated exposure at a pinned context.

The HST counterpart of the NIRCam ``detector1`` + ``image2`` steps. For one RAW
file it

1. selects reference files with ``crds bestrefs`` under an explicit HST CRDS
   context, writing their names into the RAW header and downloading any that
   are missing from the reference cache;
2. runs ``calacs.e`` (ACS/WFC) or ``calwf3.e`` (WFC3/IR) from the ``hstcal``
   package on it;
3. moves the product into place as the canonical exposure
   (``<rootname>_flc.fits`` for ACS/WFC, ``<rootname>_flt.fits`` for WFC3/IR)
   with a ``CFP_CAL`` stamp recording the calibration software version and the
   context.

Why from RAW: the calibrated files MAST serves depend on when MAST last
reprocessed them (monthly ACS darks, CTE model generations, WFC3/IR zeropoints
and flats). Starting from RAW under a pinned context makes the pixels a function
of the input, this pipeline's version and one context string.

Isolation: the JWST CRDS variables of the pipeline process are never touched.
``CRDS_SERVER_URL``, ``CRDS_PATH``, ``CRDS_CONTEXT`` and the ``jref`` / ``iref``
reference directories are set only in the environment of the two subprocesses.

Everything happens in a private working directory next to the destination and
is removed afterwards; an interrupted run leaves no partial canonical file.

Threads: ``calacs.e`` runs its CTE correction on every core unless told
otherwise, and it ignores ``OMP_NUM_THREADS``. Pass ``nthreads`` whenever
several exposures are calibrated in parallel.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.hst.instruments import detector_for

HST_CRDS_SERVER = 'https://hst-crds.stsci.edu'
_REF_VARS = {'acswfc': ('jref', 'acs'), 'wfc3ir': ('iref', 'wfc3')}
_EXECUTABLE = {'acswfc': 'calacs.e', 'wfc3ir': 'calwf3.e'}


def reference_dir(crds_path, token):
    """Directory the detector's reference files live in, inside a CRDS cache.

    CRDS keeps HST references either under the IRAF-style name (``jref``,
    ``iref``) or under the instrument name; use whichever this cache has.
    """
    var, instrument = _REF_VARS[token]
    base = os.path.join(str(crds_path), 'references', 'hst')
    for name in (var, instrument):
        if os.path.isdir(os.path.join(base, name)):
            return var, os.path.join(base, name)
    return var, os.path.join(base, var)


def _run(cmd, *, cwd, env, what):
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stdout + '\n' + proc.stderr)[-2000:]
        raise RuntimeError(f'{what} failed (exit {proc.returncode}):\n{tail}')
    return proc


# What a failure of the CRDS server (not of the request) looks like in the
# output of `crds bestrefs`. The server is asked for the context's mappings on
# every call, also when every reference file is already in the cache.
_SERVER_TROUBLE = ('ServiceError', 'HTTP Error 5', 'Gateway Time-out',
                   'timed out', 'Connection reset', 'Temporary failure',
                   'Service Unavailable')


def _run_crds(cmd, *, cwd, env, what, attempts=4, wait=30.0):
    """``_run`` for a CRDS call, repeated when the server fails.

    One 504 from the server stopped a run of twenty exposures after forty
    minutes (2026-11-16); the same call succeeded a minute later. A failure
    that is not the server's (a bad context, a file CRDS cannot classify) is
    raised at once.
    """
    for attempt in range(1, attempts + 1):
        try:
            return _run(cmd, cwd=cwd, env=env, what=what)
        except RuntimeError as e:
            transient = any(s in str(e) for s in _SERVER_TROUBLE)
            if not transient or attempt == attempts:
                raise
            log(f'{what}: the CRDS server failed (attempt {attempt} of '
                f'{attempts}); trying again in {wait * attempt:.0f} s')
            time.sleep(wait * attempt)


def prefetch_references(raw_files, *, crds_context, crds_path,
                        crds_server=HST_CRDS_SERVER, batch=100):
    """Download every reference file the RAW files need, in one process.

    ``calibrate_step`` fetches what it is missing, but several of them running
    at once would download the same file into the same cache at the same
    time. This runs ``crds bestrefs`` over all the files first, without
    touching them (no ``--update-bestrefs``), so the parallel calibrations
    find the cache complete.
    """
    from campfire_pipeline.config import check_crds_context, check_crds_server
    check_crds_context(crds_context, 'hst', 'HST calibrate: crds_context')
    check_crds_server(crds_server, 'hst', 'HST calibrate: crds_server')
    files = [str(f) for f in raw_files]
    if not files:
        return
    os.makedirs(str(crds_path), exist_ok=True)
    env = dict(os.environ, CRDS_SERVER_URL=crds_server,
               CRDS_PATH=str(crds_path), CRDS_CONTEXT=crds_context)
    env.pop('CRDS_OBSERVATORY', None)
    log(f'calibrate: checking reference files for {len(files)} RAW files '
        f'under {crds_context} (cache: {crds_path})')
    for i in range(0, len(files), batch):
        _run_crds([sys.executable, '-m', 'crds.bestrefs', '--files',
              *files[i:i + batch], '--new-context', crds_context,
              '--sync-references=1'],
             cwd=str(crds_path), env=env, what='crds bestrefs (reference prefetch)')


def calibrate_step(raw_file, out_dir, *, crds_context, crds_path,
                   crds_server=HST_CRDS_SERVER, nthreads=None, overwrite=False,
                   keep_intermediates=False, reduction_version=None):
    """Calibrate one RAW exposure; return the canonical file's path.

    ``crds_context`` (e.g. ``'hst_1372.pmap'``) and ``crds_path`` (the HST
    reference cache) are required: there is no silent default for either.
    """
    if not crds_context or not crds_path:
        raise ValueError('calibrate needs an explicit HST crds_context and crds_path')
    # a JWST context or the JWST server here stops before anything runs
    from campfire_pipeline.config import check_crds_context, check_crds_server
    check_crds_context(crds_context, 'hst', 'HST calibrate: crds_context')
    check_crds_server(crds_server, 'hst', 'HST calibrate: crds_server')

    with fits.open(raw_file, memmap=False) as hdul:
        primary = hdul[0].header
        det = detector_for(primary.get('INSTRUME'), primary.get('DETECTOR'))
        rootname = str(primary.get('ROOTNAME') or
                       os.path.basename(raw_file).split('_')[0]).strip().lower()

    os.makedirs(out_dir, exist_ok=True)
    canonical = os.path.join(out_dir, f'{rootname}_{det.calibrated_suffix}.fits')
    if (not overwrite and os.path.exists(canonical)
            and cfp.has_step(canonical, 'CFP_CAL', keyset=cfp.HST)):
        log(f'Skipping calibrate on {rootname}: CFP_CAL already set')
        return canonical

    exe = shutil.which(_EXECUTABLE[det.token])
    if exe is None:
        raise RuntimeError(
            f"{_EXECUTABLE[det.token]} not found on PATH. The HST calibration "
            f"programs come from the conda-forge package 'hstcal'.")

    work = tempfile.mkdtemp(prefix=f'.cal_{rootname}_', dir=out_dir)
    try:
        local_raw = os.path.join(work, f'{rootname}_raw.fits')
        shutil.copyfile(raw_file, local_raw)

        crds_env = dict(os.environ, CRDS_SERVER_URL=crds_server,
                        CRDS_PATH=str(crds_path), CRDS_CONTEXT=crds_context)
        crds_env.pop('CRDS_OBSERVATORY', None)
        _run_crds([sys.executable, '-m', 'crds.bestrefs', '--files', local_raw,
              '--new-context', crds_context, '--update-bestrefs',
              '--sync-references=1'],
             cwd=work, env=crds_env, what=f'crds bestrefs ({rootname})')

        var, refdir = reference_dir(crds_path, det.token)
        cal_env = dict(os.environ)
        cal_env[var] = os.path.join(refdir, '')
        cmd = [exe]
        if det.token == 'acswfc' and nthreads:
            cmd += ['--nthreads', str(int(nthreads))]
        cmd.append(os.path.basename(local_raw))
        _run(cmd, cwd=work, env=cal_env, what=f'{os.path.basename(exe)} ({rootname})')

        product = os.path.join(work, f'{rootname}_{det.calibrated_suffix}.fits')
        if not os.path.exists(product):
            raise RuntimeError(
                f'{os.path.basename(exe)} produced no '
                f'{os.path.basename(product)} for {rootname}')
        with fits.open(product, mode='update', memmap=False) as hdul:
            cal_ver = str(hdul[0].header.get('CAL_VER', '')).strip()
            stamp = f'raw {cal_ver.split()[0] if cal_ver else "?"} {crds_context}'
            for key, val in cfp.format(keyset=cfp.HST, CFP_CAL=stamp).items():
                hdul[0].header[key] = val
            # The provenance cards a NIRCam canonical exposure carries, under
            # the same names, so a deployment of either instrument records the
            # pipeline version and the CRDS context from the exposure header
            # (CAL_VER, the calibration software's own version, is already
            # there). An HST context name says it is HST's ('hst_1372.pmap').
            hdul[0].header['CRDS_CTX'] = (crds_context, 'CRDS context used')
            if reduction_version:
                hdul[0].header['CMPFRVER'] = (
                    reduction_version, 'CAMPFIRE git commit (or pinned version)')
                hdul[0].header['CMPFRTIM'] = (
                    cfp.iso_now(), 'UTC date/time of CAMPFIRE reduction (ISO 8601)')
        os.replace(product, canonical)

        if keep_intermediates:
            for name in os.listdir(work):
                if name.endswith('.fits') and not name.endswith('_raw.fits'):
                    os.replace(os.path.join(work, name), os.path.join(out_dir, name))
        log(f'calibrate: {rootname} -> {os.path.basename(canonical)} ({stamp})')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return canonical
