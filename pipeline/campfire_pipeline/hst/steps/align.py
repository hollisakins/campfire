"""align: tie the exposures of one HST visit to the field's reference catalog.

The solver is the NIRCam ``align`` solver (``nircam/align/solve.py``): clip the
reference catalog to the footprint, find the gross offset with the
offset-histogram matcher, fit one shift and rotation for a pool of chips. What
differs is which pools are fitted against what, and the WCS representation:

* **Exposure to exposure first.** The exposures of a visit share guide stars
  and a distortion model, so they differ by a few milliarcseconds at most, and
  they share hundreds of sources. Each exposure (its chips as one rigid pool)
  is fitted to the detections of the visit's richest exposure. This is the only
  stage that can measure offsets that small: the reference catalog usually has
  too few sources per exposure (a Gaia-only field gives 10-15).
* **Then the visit to the reference catalog.** All chips of all exposures, as
  one rigid pool, get one shift and rotation. The per-chip refinement NIRCam
  uses is off by default (``fine_fitgeom = "none"``): NIRCam detectors carry
  individual offsets, HST chips within a visit do not, and on the first real
  visit a per-chip fit from 5-8 Gaia stars made the exposures agree worse with
  each other (docs/hst/design.md, section 8b).
* **WCS.** An HST chip's WCS is an ``astropy.wcs.WCS`` (FITS keywords, SIP and
  lookup-table distortion). It goes to the solver without ``wcsinfo``, which
  selects the arcsec-plane FITS corrector
  (``imaging/align/fitswcs.py``). A solution changes ``CRVAL`` and the ``CD``
  matrix only; the distortion model is untouched.

Write-back. The corrected ``CRVAL`` / ``CD`` go into each chip's ``SCI`` header
and ``WCSNAME`` gains the suffix ``-CFALIGN``. The pre-alignment values are kept
in ``CFOCRV1/2`` and ``CFOCD11..22`` the first time a chip is aligned, and every
later solve starts from them, so re-running never compounds a correction (the
role ``ALGN_BAK`` plays for NIRCam). ``CFP_ALGN`` on the primary header carries
the solve summary, or ``NOT_ALIGNED`` when the pool or one of the file's chips
was rejected; the WCS is left as it was in that case.

Detection uses the adapter's arrays, so cosmic rays flagged by ``crreject`` and
bad pixels are already excluded. If ``crreject`` has not run yet on CCD data,
run it first: an ACS exposure's cosmic rays outnumber its sources.
"""

import dataclasses
import os
import shutil

import numpy as np
from astropy.io import fits
from astropy.time import Time

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.hst.exposure import open_exposure

_BACKUP = {'CRVAL1': 'CFOCRV1', 'CRVAL2': 'CFOCRV2', 'CD1_1': 'CFOCD11',
           'CD1_2': 'CFOCD12', 'CD2_1': 'CFOCD21', 'CD2_2': 'CFOCD22'}
ALIGN_SUFFIX = '-CFALIGN'

# Matched-filter width (native pixels) for source detection, per detector.
_DETECT_FWHM = {'acswfc': 2.2, 'wfc3ir': 1.3}
# DQ value of a saturated pixel in calibrated ACS and WFC3 files (the DQ flag
# tables of the ACS and WFC3 Data Handbooks give 256 for both).
SATURATED_BIT = 256


def _original_wcs(chip, header):
    """The chip's pre-alignment WCS: the current one with any backed-up
    ``CRVAL`` / ``CD`` restored."""
    w = chip._wcs.deepcopy()
    if all(k in header for k in _BACKUP.values()):
        w.wcs.crval = [header[_BACKUP['CRVAL1']], header[_BACKUP['CRVAL2']]]
        w.wcs.cd = [[header[_BACKUP['CD1_1']], header[_BACKUP['CD1_2']]],
                    [header[_BACKUP['CD2_1']], header[_BACKUP['CD2_2']]]]
        w.wcs.set()
    return w


def _detect(chip, config):
    """Source catalog of one chip (0-indexed pixels), from the adapter arrays.

    Sources on or next to saturated pixels are dropped. The adapter masks a
    saturated core, so what is left of such a star is detected as one or more
    pieces of its wings with centroids off by tens of milliarcseconds; on the
    first real visit two saturated stars supplied 14 of 40 matches to Gaia and
    pulled the fit by about 10 mas (docs/hst/design.md, section 8b).
    """
    from scipy.ndimage import maximum_filter

    from campfire_pipeline.imaging.align.detect import detect_sources

    a = chip.load()
    # Background noise only (the adapter's source-independent variance). The
    # shared extraction measures positions on sci / err; with the source's own
    # Poisson noise in err that image goes as the square root of the flux for a
    # bright star, its wings outweigh its core, and the position moved by a
    # quarter of an ACS pixel (12 mas) on real stars.
    with np.errstate(invalid='ignore'):
        err = np.sqrt(a.var_rnoise.astype(np.float64))
    cat = detect_sources(
        a.sci.astype(np.float64), err, mask=~a.good,
        fwhm=float(config.get('detect_fwhm', _DETECT_FWHM[chip.instrument])),
        snr_thresh=float(config.get('detect_snr_thresh', 3.0)),
        minarea=int(config.get('detect_minarea', 9)),
        snr_min=config.get('snr_min', 10.0),
    )
    grow = int(config.get('saturation_grow', 5))
    if len(cat) and grow >= 0:
        dq = fits.getdata(chip.path, ('DQ', chip.extver))
        near = maximum_filter((dq & SATURATED_BIT) != 0, size=2 * grow + 1)
        ny, nx = near.shape
        i = np.clip(np.rint(np.asarray(cat['x'], float)).astype(int), 0, nx - 1)
        j = np.clip(np.rint(np.asarray(cat['y'], float)).astype(int), 0, ny - 1)
        cat = cat[~near[j, i]]
    return cat


def _mid_mjd(path):
    with fits.open(path, memmap=False) as hdul:
        h = hdul[0].header
        if 'EXPSTART' in h and 'EXPEND' in h:
            return 0.5 * (float(h['EXPSTART']) + float(h['EXPEND']))
        if 'EXPSTART' in h:
            return float(h['EXPSTART'])
        if h.get('DATE-OBS'):
            return float(Time(str(h['DATE-OBS'])).mjd)
    return None


def _write_back(path, solutions, cfp_value):
    """Write corrected WCS keywords for the chips in ``solutions`` (extver ->
    corrected ``astropy.wcs.WCS`` or ``None`` to leave it) and stamp CFP_ALGN."""
    tmp = f'{path}.algn.tmp'
    try:
        shutil.copyfile(path, tmp)
        with fits.open(tmp, mode='update', memmap=False) as hdul:
            for extver, wcs in solutions.items():
                if wcs is None:
                    continue
                h = hdul['SCI', extver].header
                new = {'CRVAL1': wcs.wcs.crval[0], 'CRVAL2': wcs.wcs.crval[1],
                       'CD1_1': wcs.wcs.cd[0, 0], 'CD1_2': wcs.wcs.cd[0, 1],
                       'CD2_1': wcs.wcs.cd[1, 0], 'CD2_2': wcs.wcs.cd[1, 1]}
                for key, backup in _BACKUP.items():
                    if backup not in h:
                        h[backup] = (h[key], f'campfire: pre-align {key}')
                for key, value in new.items():
                    h[key] = float(value)
                name = str(h.get('WCSNAME', '') or '')
                if not name.endswith(ALIGN_SUFFIX):
                    h['WCSNAME'] = name + ALIGN_SUFFIX
            for key, val in cfp.format(keyset=cfp.HST, CFP_ALGN=cfp_value).items():
                hdul[0].header[key] = val
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def align_visit(exposure_files, refcat, *, key, config=None, refcat_hash=None,
                overwrite=False):
    """Align the exposures of one visit and filter; return the ``GroupSolution``
    (or ``None`` when every file is already aligned and ``overwrite`` is unset).

    ``refcat`` is a campfire reference catalog table (``RA``, ``DEC``, optional
    proper-motion columns). ``config`` keys: ``relative`` (default true) and
    ``relative_min_matched`` (default 20) control the exposure-to-exposure
    stage; the ``detect_*`` keys and ``snr_min`` the detection; the rest are
    passed to ``solve_exposure_group`` (``coarse_searchrad``, ``match_radius``,
    ``min_matched``, ``max_residual_arcsec``, ``fine_fitgeom``, ...).
    """
    from campfire_pipeline.imaging.refcat.motion import propagate_to_epoch
    from campfire_pipeline.nircam.align.solve import (
        DetectorInput, solve_exposure_group,
    )

    files = sorted(exposure_files)
    cfg = dict(config or {})
    if not overwrite:
        values = [cfp.step_value(f, 'CFP_ALGN', keyset=cfp.HST) for f in files]
        if all(v is not None and v != cfp.NOT_ALIGNED for v in values):
            log(f'Skipping align on visit {key}: CFP_ALGN already set')
            return None

    detect_keys = ('detect_fwhm', 'detect_snr_thresh', 'detect_minarea', 'snr_min',
                   'saturation_grow')
    detect_cfg = {k: cfg.pop(k) for k in detect_keys if k in cfg}
    relative = bool(cfg.pop('relative', True))
    relative_min_matched = int(cfg.pop('relative_min_matched', 20))
    cfg.setdefault('fine_fitgeom', 'none')

    mjd = _mid_mjd(files[0])
    if mjd is not None:
        refcat = propagate_to_epoch(refcat, mjd)

    by_exposure, where = {}, {}
    for f in files:
        with fits.open(f, memmap=False) as hdul:
            headers = {c.extver: hdul['SCI', c.extver].header.copy()
                       for c in open_exposure(f)}
        by_exposure[f] = []
        for chip in open_exposure(f):
            catalog = _detect(chip, detect_cfg)
            by_exposure[f].append(DetectorInput(
                detector=chip.key, wcs=_original_wcs(chip, headers[chip.extver]),
                wcsinfo=None, catalog=catalog))
            where[chip.key] = (f, chip.extver)
            log(f'align[{key}]: {chip.key} {len(catalog)} sources')

    rel = {f: '0' for f in files}
    if relative and len(files) > 1:
        rel = _relative(by_exposure, key=key, min_matched=relative_min_matched,
                        solve=solve_exposure_group, config=cfg)

    detectors = [d for f in files for d in by_exposure[f]]
    sol = solve_exposure_group(detectors, refcat, key=key, dva_repivot=False, **cfg)

    by_file = {f: {} for f in files}
    summary = {f: [] for f in files}
    for d in sol.detectors:
        f, extver = where[d.detector]
        ok = sol.status == 'SOLVED' and d.aligned
        by_file[f][extver] = d.wcs if ok else None
        summary[f].append((ok, d))
    rc = f' rc={refcat_hash}' if refcat_hash else ''
    for f in files:
        if sol.status != 'SOLVED' or not all(ok for ok, _ in summary[f]):
            _write_back(f, {v: None for v in by_file[f]}, cfp.NOT_ALIGNED)
            log(f'align[{key}]: {os.path.basename(f)} NOT_ALIGNED (WCS preserved)')
            continue
        res = max(d.residual_arcsec for _, d in summary[f])
        n = sum(d.n_matched for _, d in summary[f])
        dofs = ','.join(d.dof for _, d in summary[f])
        value = f'dof={dofs} res={res * 1000:.1f}mas n={n} rel={rel[f]}{rc}'
        _write_back(f, by_file[f], value)
        log(f'align[{key}]: {os.path.basename(f)} {value}')
    return sol


def _relative(by_exposure, *, key, min_matched, solve, config):
    """Tie each exposure to the visit's richest one, in place.

    ``by_exposure`` maps file -> its chips' ``DetectorInput``; the WCS of every
    exposure that solves is replaced by the corrected one. Returns file -> the
    ``rel=`` token for the step stamp: ``ref`` for the reference exposure, the
    number of matched sources for a corrected one, ``0`` for one left alone.
    """
    from astropy.table import Table

    reference = max(by_exposure,
                    key=lambda f: sum(len(d.catalog) for d in by_exposure[f]))
    ra, dec = [], []
    for d in by_exposure[reference]:
        r, c = d.wcs.all_pix2world(np.asarray(d.catalog['x'], float),
                                   np.asarray(d.catalog['y'], float), 0)
        ra.append(r)
        dec.append(c)
    relcat = Table({'RA': np.concatenate(ra), 'DEC': np.concatenate(dec)})

    cfg = {k: v for k, v in config.items() if k not in ('min_matched', 'fine_fitgeom')}
    token = {reference: 'ref'}
    for f, dets in by_exposure.items():
        if f == reference:
            continue
        name = os.path.basename(f)
        sol = solve(dets, relcat, key=f'{key}:rel', dva_repivot=False,
                    fine_fitgeom='none', min_matched=min_matched, **cfg)
        if sol.status != 'SOLVED' or not all(d.aligned for d in sol.detectors):
            token[f] = '0'
            log(f'align[{key}]: {name} not tied to {os.path.basename(reference)} '
                f'(too few common sources); left as it is')
            continue
        fitted = {d.detector: d.wcs for d in sol.detectors}
        by_exposure[f] = [dataclasses.replace(d, wcs=fitted[d.detector])
                          for d in dets]
        token[f] = str(sol.n_matched)
        log(f'align[{key}]: {name} tied to {os.path.basename(reference)}: '
            f'shift ({sol.shift[0] * 1000:+.1f}, {sol.shift[1] * 1000:+.1f}) mas, '
            f'{sol.n_matched} sources, rms {sol.rmse_arcsec * 1000:.1f} mas')
    return token
