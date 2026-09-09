"""``cfpipe nirspec linefit`` — the stage runner around :mod:`linefit`.

For every ``*_spec.fits`` in the observation workspace it

1. resolves the redshift to fit at: the inspected redshift from
   ``reference/nirspec/<obs>/redshifts.toml`` (materialized by ``campfire
   pull``), gated on ``min_quality``; or, only with ``allow_auto=True``, the
   spectrum's own ``_zfit.fits`` ``ZBEST`` (provenance ``ZSRC='auto'``) —
   a QA convenience that ``campfire deploy lines`` refuses by default;
2. builds the effective LSF from the grating's R-curve × the same
   ``f_LSF_<grating>`` the redshift fitter uses;
3. runs :func:`linefit.fit_lines` and writes ``<base>_lines.fits`` next to
   the spectrum (plus a QA PDF when ``plot`` is on).

A product is only rewritten when its inputs changed (redshift, quality, or
the spectrum bytes) or ``--overwrite`` is given, so re-running after a
re-pull refits exactly the spectra whose inspected redshift moved.

The ``_lines.fits`` product::

    PRIMARY   provenance header (ZUSED/ZSRC/ZQUAL/OBJID/OBJVER, ZFIT, global
              kinematics, counts, LFITVER, SPECHASH, CMPFRVER, CMPFRTIM, ...)
    LINES     one row per measured line (and per accepted broad component)
    MODEL     wave / model / cont (flam) on the spectrum grid, NaN outside windows
    COMPLEXES the fitted windows

:func:`read_lines_file` and :func:`lines_payload` are what ``campfire deploy
lines`` uses to turn the product into a ``spectrum_line_fits`` row.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.nirspec.linefit import (
    LINEFIT_VERSION, LineFitConfig, fit_lines, make_r_function,
)
from campfire_pipeline.nirspec.redshift_reference import (
    RedshiftEntry, load_redshifts, redshifts_path,
)

log = logging.getLogger('nirspec_linefit')

LINES_SUFFIX = '_lines.fits'

# Column order of the LINES table (and of every per-line JSON record).
LINE_COLUMNS = [
    'name', 'component', 'wave_rest', 'wave_obs',
    'flux', 'flux_err', 'snr', 'ew_rest', 'ew_rest_err', 'cont', 'cont_err',
    'dv', 'dv_err', 'sigma_v', 'sigma_v_err', 'sigma_lsf_kms',
    'complex', 'chi2', 'dof', 'npix', 'flags', 'blend_into', 'tied_to',
]
_STR_COLS = {'name', 'component', 'blend_into', 'tied_to'}
_INT_COLS = {'complex', 'dof', 'npix', 'flags'}


# ---------------------------------------------------------------------------
# Redshift resolution
# ---------------------------------------------------------------------------

def resolve_redshift(target_id, redshifts, min_quality, allow_auto, zfit_path=None):
    """Pick the redshift for one spectrum.

    Returns ``(z, source, entry)`` with ``source`` in ``{'inspected', 'auto'}``,
    or ``(None, reason, entry)`` when the spectrum must be skipped.
    """
    entry = redshifts.get(target_id)
    if entry is not None and entry.redshift is not None and entry.quality >= min_quality:
        return float(entry.redshift), 'inspected', entry
    if allow_auto:
        z_auto = _read_zfit_zbest(zfit_path) if zfit_path else None
        if z_auto is not None:
            return float(z_auto), 'auto', entry
        return None, 'no zfit result for auto fallback', entry
    if entry is None:
        return None, 'no inspected redshift', entry
    if entry.redshift is None:
        return None, 'inspected as impossible', entry
    return None, (f'quality {entry.quality} below min_quality {min_quality}'), entry


def _read_zfit_zbest(zfit_path):
    if not zfit_path or not os.path.exists(zfit_path):
        return None
    try:
        with fits.open(zfit_path) as h:
            return float(h[0].header['ZBEST'])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Product IO
# ---------------------------------------------------------------------------

def _sha256(path, chunk=1 << 20):
    """``sha256:<hex>`` — the same scheme-prefixed form the summary reader stores
    in ``spectra.file_hash`` (``metadata/reader.py``), so the catalog can compare
    ``SPECHASH`` with it directly for staleness."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return f"sha256:{h.hexdigest()}"


def _fits_safe(v):
    """Header-safe scalar: NaN → None-skip marker."""
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def write_lines_file(path, result, *, z_source, entry: RedshiftEntry | None, spec_path,
                     spec_hash, grating, filt, source_id, target_id, f_lsf, cfpipe_version):
    """Write the ``_lines.fits`` product."""
    s = result['summary']
    hdr = fits.Header()
    hdr['EXTEND'] = True
    hdr['LFITVER'] = (LINEFIT_VERSION, 'linefit algorithm version')
    hdr['ZUSED'] = (round(float(s['z_used']), 6), 'Redshift the lines were fit at')
    hdr['ZSRC'] = (z_source, "Redshift source: inspected (portal) | auto (zfit)")
    hdr['ZQUAL'] = (int(entry.quality) if entry is not None else 0,
                    'Inspection quality at fit time (0-4)')
    if entry is not None and entry.object_id:
        hdr['OBJID'] = (entry.object_id, 'Portal object_id the redshift came from')
    if entry is not None and entry.version is not None:
        hdr['OBJVER'] = (int(entry.version), 'objects.version at pull time')
    if entry is not None and entry.inspected_at:
        hdr['ZINSPAT'] = (entry.inspected_at, 'Inspection timestamp at pull time')
    for key, name, comment in [
        ('ZFIT', 'z_fit', 'Redshift from the global line velocity'),
        ('ZFITERR', 'z_fit_err', 'Error on ZFIT'),
        ('DVGLOB', 'dv', 'Global velocity offset vs ZUSED (km/s)'),
        ('DVGLOBE', 'dv_err', 'Error on DVGLOB'),
        ('SIGGLOB', 'sigma_v', 'Global intrinsic sigma_v (km/s)'),
        ('SIGGLOBE', 'sigma_v_err', 'Error on SIGGLOB'),
        ('CHI2', 'chi2', 'Summed chi2 over fitted windows'),
    ]:
        v = _fits_safe(s.get(name))
        if v is not None:
            hdr[key] = (float(v), comment)
    hdr['DOF'] = (int(s.get('dof', 0)), 'Summed degrees of freedom')
    hdr['KINSRC'] = (s.get('kin_source', 'none'), "Kinematics: 'anchor' | 'default' | 'none'")
    hdr['NANCHOR'] = (int(s.get('n_anchors', 0)), 'Complexes anchoring the kinematics')
    hdr['NLINES'] = (int(s['n_lines']), 'Lines with a flux measurement')
    hdr['NDETECT'] = (int(s['n_detected']), 'Lines above detect_snr')
    hdr['NCOMPLEX'] = (int(s['n_complexes']), 'Fitted line complexes')
    hdr['NBROAD'] = (int(s.get('n_broad', 0)), 'Accepted broad components')
    hdr['FLSF'] = (float(f_lsf), 'LSF scale applied to the R-curve')
    hdr['GRATING'] = (str(grating).upper(), 'Grating')
    hdr['FILTER'] = (str(filt).upper(), 'Filter')
    hdr['SRCID'] = (str(source_id), 'Source id')
    hdr['TARGETID'] = (str(target_id), 'Catalog target_id')
    hdr['SPECFILE'] = (os.path.basename(spec_path), 'Input spectrum')
    hdr['SPECHASH'] = (spec_hash, 'sha256:<hex> of the input spectrum file')
    hdr['CMPFRVER'] = (cfpipe_version, 'campfire-pipeline version (PEP 440)')
    hdr['CMPFRTIM'] = (datetime.now(timezone.utc).isoformat(),
                       'UTC date/time of the line fit (ISO 8601)')
    primary = fits.PrimaryHDU(header=hdr)

    # LINES table
    recs = result['lines']
    order = sorted(recs, key=lambda n: (recs[n]['wave_rest'], recs[n]['component'] == 'broad'))
    cols = {c: [] for c in LINE_COLUMNS}
    for name in order:
        r = recs[name]
        for c in LINE_COLUMNS:
            if c == 'name':
                cols[c].append(name)
            elif c in _STR_COLS:
                cols[c].append(r.get(c) or '')
            elif c in _INT_COLS:
                cols[c].append(int(r.get(c, 0)))
            else:
                v = r.get(c)
                cols[c].append(float(v) if v is not None else float('nan'))
    t = Table()
    for c in LINE_COLUMNS:
        if c in _STR_COLS:
            t[c] = np.asarray(cols[c], dtype=str) if cols[c] else np.zeros(0, dtype='U1')
        elif c in _INT_COLS:
            t[c] = np.asarray(cols[c], dtype=np.int32)
        else:
            t[c] = np.asarray(cols[c], dtype=np.float64)
    _set_units(t)
    lines_hdu = fits.BinTableHDU(t, name='LINES')

    model, cont = result['model']
    wave = result.get('wave')
    mt = Table()
    mt['wave'] = np.asarray(wave, dtype=np.float64) if wave is not None else np.zeros(0)
    mt['model'] = np.asarray(model, dtype=np.float64)
    mt['cont'] = np.asarray(cont, dtype=np.float64)
    mt['wave'].unit = 'um'
    mt['model'].unit = mt['cont'].unit = 'erg / (s cm2 Angstrom)'
    model_hdu = fits.BinTableHDU(mt, name='MODEL')

    ct = Table()
    cxs = result['complexes']
    ct['index'] = np.asarray([c['index'] for c in cxs], dtype=np.int32)
    ct['lines'] = np.asarray([','.join(c['lines']) for c in cxs], dtype=str) if cxs else np.zeros(0, dtype='U1')
    ct['lo_idx'] = np.asarray([c['lo_idx'] for c in cxs], dtype=np.int32)
    ct['hi_idx'] = np.asarray([c['hi_idx'] for c in cxs], dtype=np.int32)
    ct['chi2'] = np.asarray([c['chi2'] for c in cxs], dtype=np.float64)
    ct['dof'] = np.asarray([c['dof'] for c in cxs], dtype=np.int32)
    ct['anchor'] = np.asarray([c['anchor'] for c in cxs], dtype=bool)
    ct['broad'] = np.asarray([c['broad'] for c in cxs], dtype=bool)
    cx_hdu = fits.BinTableHDU(ct, name='COMPLEXES')

    tmp = str(path) + '.tmp'
    fits.HDUList([primary, lines_hdu, model_hdu, cx_hdu]).writeto(tmp, overwrite=True)
    os.replace(tmp, path)


def _set_units(t: Table):
    for c in ('wave_rest',):
        t[c].unit = 'Angstrom'
    t['wave_obs'].unit = 'um'
    for c in ('flux', 'flux_err'):
        t[c].unit = 'erg / (s cm2)'
    for c in ('cont', 'cont_err'):
        t[c].unit = 'erg / (s cm2 Angstrom)'
    for c in ('ew_rest', 'ew_rest_err'):
        t[c].unit = 'Angstrom'
    for c in ('dv', 'dv_err', 'sigma_v', 'sigma_v_err', 'sigma_lsf_kms'):
        t[c].unit = 'km / s'


def read_lines_file(path) -> dict:
    """Read a ``_lines.fits`` product back into ``{'header', 'lines', 'model', 'complexes'}``.

    ``lines`` is ``{name: record}`` with the LINES columns; NaN stays NaN
    (use :func:`lines_payload` for a JSON-safe form).
    """
    with fits.open(path) as h:
        hdr = dict(h[0].header)
        lt = Table(h['LINES'].data)
        mt = Table(h['MODEL'].data)
        ct = Table(h['COMPLEXES'].data)
    lines = {}
    for row in lt:
        rec = {}
        for c in LINE_COLUMNS:
            v = row[c]
            if c in _STR_COLS:
                v = str(v).strip() or None
            elif c in _INT_COLS:
                v = int(v)
            else:
                v = float(v)
            rec[c] = v
        name = rec.pop('name')
        rec['label'] = _label_for(name)
        lines[name] = rec
    model = dict(wave=np.asarray(mt['wave']), model=np.asarray(mt['model']), cont=np.asarray(mt['cont']))
    complexes = [dict(index=int(r['index']), lines=str(r['lines']).split(','), lo_idx=int(r['lo_idx']),
                      hi_idx=int(r['hi_idx']), chi2=float(r['chi2']), dof=int(r['dof']),
                      anchor=bool(r['anchor']), broad=bool(r['broad'])) for r in ct]
    return dict(header=hdr, lines=lines, model=model, complexes=complexes)


def _label_for(name: str) -> str:
    """Display label from the catalog (labels are non-ASCII, so they are not
    stored in the FITS table; ``Halpha_broad`` → ``Hα (broad)``)."""
    from campfire_pipeline.nirspec.linelist import LINES_BY_NAME
    base, _, suffix = name.partition('_')
    line = LINES_BY_NAME.get(base)
    label = line.label if line else base
    return f"{label} ({suffix})" if suffix else label


def lines_payload(lines: dict) -> dict:
    """JSON-safe ``{name: record}`` (NaN/inf → null) — the ``spectrum_line_fits.lines`` value."""
    out = {}
    for name, rec in lines.items():
        clean = {}
        for k, v in rec.items():
            if k in ('label',):
                clean[k] = v
            elif isinstance(v, float):
                clean[k] = v if math.isfinite(v) else None
            elif isinstance(v, (np.floating,)):
                fv = float(v)
                clean[k] = fv if math.isfinite(fv) else None
            elif isinstance(v, (np.integer,)):
                clean[k] = int(v)
            elif isinstance(v, list):
                clean[k] = v
            else:
                clean[k] = v
        out[name] = clean
    return out


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------

def _needs_refit(lines_path, z, quality, spec_hash, overwrite):
    if overwrite or not os.path.exists(lines_path):
        return True, 'new'
    try:
        with fits.open(lines_path) as h:
            hdr = h[0].header
        if hdr.get('LFITVER') != LINEFIT_VERSION:
            return True, 'algorithm version changed'
        if abs(float(hdr.get('ZUSED', np.nan)) - z) > 1e-6:
            return True, f"redshift changed ({hdr.get('ZUSED')} -> {z:.6f})"
        if int(hdr.get('ZQUAL', -1)) != int(quality):
            return True, 'quality changed'
        if hdr.get('SPECHASH') != spec_hash:
            return True, 'spectrum changed'
    except Exception as e:
        return True, f'unreadable product ({e})'
    return False, 'up to date'


def _fit_one(args):
    """Worker: fit one spectrum. Returns (spec_file, status, message)."""
    (spec_file, z, z_source, entry, grating, filt, source_id, target_id, r_wav, r_val,
     f_lsf, options, plot, cfpipe_version, spec_hash) = args
    try:
        tab = Table.read(spec_file, hdu='SPEC1D')
        wave = np.asarray(tab['wave'], dtype=float)
        fnu = np.asarray(tab['fnu'], dtype=float)
        fnu_err = np.asarray(tab['fnu_err'], dtype=float)
        cfg = LineFitConfig.from_options(options, grating)
        r_of = make_r_function(r_wav, r_val, f_lsf)
        result = fit_lines(wave, fnu, fnu_err, z, r_of, cfg, grating=grating)
        result['wave'] = wave
        lines_path = spec_file.replace('_spec.fits', LINES_SUFFIX)
        write_lines_file(lines_path, result, z_source=z_source, entry=entry, spec_path=spec_file,
                         spec_hash=spec_hash, grating=grating, filt=filt, source_id=source_id,
                         target_id=target_id, f_lsf=f_lsf, cfpipe_version=cfpipe_version)
        if plot:
            from campfire_pipeline.nirspec.plots import plot_linefit_results
            try:
                plot_linefit_results(lines_path, spec_file=spec_file)
            except Exception as e:  # a QA plot must never fail the product
                log.warning(f"QA plot failed for {os.path.basename(lines_path)}: {e}")
        s = result['summary']
        return spec_file, True, (f"z={z:.4f} ({z_source}) lines={s['n_lines']} "
                                 f"detected={s['n_detected']} broad={s.get('n_broad', 0)}")
    except Exception as e:
        return spec_file, False, str(e)


def run_linefit(obs, config, source_ids=None, overwrite=False, n_processes=1,
                allow_auto=False, gratings=None) -> dict:
    """Fit emission lines for every spectrum in *obs* at its inspected redshift.

    Parameters
    ----------
    obs : Observation
        Loaded observation (``workspace_dir`` and ``reference_dir`` set).
    config : dict
        Full pipeline config (reads ``[nirspec.line_fitting]`` and the
        ``f_LSF_<grating>`` keys of ``[nirspec.redshift_fitting]``).
    source_ids : list of str/int, optional
    overwrite : bool
        Refit even when the product is up to date with its inputs.
    n_processes : int
    allow_auto : bool
        Fall back to the zfit ``ZBEST`` for spectra without a usable inspected
        redshift (QA only — flagged ``ZSRC='auto'`` in the product).
    gratings : list of str, optional

    Returns
    -------
    dict
        Counts: ``fit``, ``skipped_uptodate``, ``skipped_no_z``, ``failed``.
    """
    from campfire_pipeline.common.spectral import load_r_curve
    from campfire_pipeline.common.version import get_reduction_version
    from campfire_pipeline.config import get_r_curve_path
    from campfire_pipeline.metadata.reader import parse_fits_filename

    options = dict(config.get('nirspec', {}).get('line_fitting', {}))
    options.update(getattr(obs, 'stage_overrides', {}).get('line_fitting', {}))
    zfit_opts = config.get('nirspec', {}).get('redshift_fitting', {})
    min_quality = int(options.get('min_quality', 3))
    plot = bool(options.get('plot', True))
    cfpipe_version = get_reduction_version(config)

    workspace = obs.workspace_dir
    ref_file = redshifts_path(obs.reference_dir)
    redshifts = load_redshifts(ref_file)
    if not redshifts:
        log.warning(f"No inspected redshifts at {ref_file} — run `campfire pull --obs {obs.name}` "
                    f"first{' (falling back to zfit redshifts)' if allow_auto else ''}")

    spec_files = sorted(glob.glob(os.path.join(workspace, '*_spec.fits')))
    wanted_sids = {str(s) for s in source_ids} if source_ids else None
    wanted_gr = {g.lower() for g in gratings} if gratings else None

    tasks = []
    counts = dict(fit=0, skipped_uptodate=0, skipped_no_z=0, failed=0)
    r_cache = {}
    for spec_file in spec_files:
        parsed = parse_fits_filename(os.path.basename(spec_file))
        grating = parsed['grating'].lower()
        source_id = parsed['source_id']
        if wanted_sids is not None and source_id not in wanted_sids:
            continue
        if wanted_gr is not None and grating not in wanted_gr:
            continue
        target_id = f"{obs.name}_{source_id}"
        zfit_path = spec_file.replace('_spec.fits', '_zfit.fits')
        z, z_source, entry = resolve_redshift(target_id, redshifts, min_quality, allow_auto, zfit_path)
        if z is None:
            log.info(f"  skip {os.path.basename(spec_file)}: {z_source}")
            counts['skipped_no_z'] += 1
            continue
        spec_hash = _sha256(spec_file)
        quality = entry.quality if entry is not None else 0
        lines_path = spec_file.replace('_spec.fits', LINES_SUFFIX)
        refit, why = _needs_refit(lines_path, z, quality, spec_hash, overwrite)
        if not refit:
            counts['skipped_uptodate'] += 1
            continue
        if grating not in r_cache:
            r_cache[grating] = load_r_curve(get_r_curve_path(grating))
        r_wav, r_val = r_cache[grating]
        f_lsf = float(zfit_opts.get(f'f_LSF_{grating}', zfit_opts.get('f_LSF', 1.0)))
        tasks.append((spec_file, z, z_source, entry, grating, parsed['filter'].lower(), source_id,
                      target_id, r_wav, r_val, f_lsf, options, plot, cfpipe_version, spec_hash))

    log.info(f"linefit {obs.name}: {len(tasks)} to fit, {counts['skipped_uptodate']} up to date, "
             f"{counts['skipped_no_z']} without a usable redshift (min_quality={min_quality})")
    if not tasks:
        return counts

    if n_processes > 1 and len(tasks) > 1:
        from multiprocessing import Pool
        with Pool(n_processes) as pool:
            results = list(pool.imap_unordered(_fit_one, tasks))
    else:
        results = [_fit_one(t) for t in tasks]

    for spec_file, ok, msg in sorted(results):
        base = os.path.basename(spec_file).replace('_spec.fits', '')
        if ok:
            counts['fit'] += 1
            log.info(f"  {base}: {msg}")
        else:
            counts['failed'] += 1
            log.error(f"  {base}: FAILED {msg}")
    return counts


def discover_lines_files(workspace_dir) -> list[Path]:
    return sorted(Path(p) for p in glob.glob(os.path.join(str(workspace_dir), f'*{LINES_SUFFIX}')))
