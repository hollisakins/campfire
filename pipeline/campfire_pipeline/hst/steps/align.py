"""align: tie the exposures of one HST visit to the field's reference catalog.

The solver is the NIRCam ``align`` solver (``nircam/align/solve.py``): clip the
reference catalog to the footprint, find the gross offset with the
offset-histogram matcher, fit a pool of chips. The recipe is HST's own and is
set with HST's own settings (:data:`DEFAULTS`); it differs from the NIRCam
recipe on purpose, because the two instruments are wrong in different ways.
NIRCam's ten detectors each sit at an offset of their own, so the NIRCam step
fits each detector. The two ACS/WFC chips are one focal plane and do not stand
apart; what a shift and rotation leaves there is a linear term across the whole
field (measurements in docs/hst/design.md, section 8b).

* **Exposure to exposure first** (``pool = "visit"``, the default). Each
  exposure, its chips as one body, is fitted to the detections of the visit's
  richest exposure on their common sources (hundreds to thousands). The dither
  offsets in the headers are not relied on: they are measured here, to better
  than a milliarcsecond, which a reference catalog with tens of sources per
  exposure cannot do. An exposure that cannot be fitted to the others is fitted
  to the reference catalog on its own. ``pool = "exposure"`` fits every
  exposure to the catalog on its own, as the NIRCam step does.
* **Then the pool to the reference catalog** (``fit``). ``"rigid"`` is one shift
  and rotation; ``"linear"`` adds scale and skew, six numbers for the whole
  pool; ``"auto"`` (the default) uses the linear fit when the matches can hold
  a linear term to ``linear_max_error`` (3 mas per 100 arcsec) and the rigid
  fit otherwise. A dense NIRCam-derived catalog or a rich Gaia field passes;
  HSC or Legacy Surveys used directly do not.
* **Then the chips, the way the NIRCam step treats its detectors**
  (``chip_fits``, ``exposure_fits``). One fit per chip for the visit, shared
  by that chip in every tied exposure and accepted only when significant; if
  one is accepted the exposures are tied a second time and the pool is fitted
  again. On top, a fit of every chip in every exposure, each accepted when its
  shift is significant against its own standard error (all its matches). With
  the linear fit in place these rarely have anything left to correct, and the
  evidence decides.
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
bad pixels are already excluded. CCD exposures need those flags before this
step: a 400 s ACS exposure gave about 10,000 detections per chip without them
against 140-400 with them, the exposure-to-exposure stage then failed, and a
fit to a dense catalog was rejected (design.md, section 8b). WFC3/IR ramps
reject cosmic rays in ``calwf3`` and do not depend on the order.
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

# The recipe of the HST align step and its defaults. These settings are HST's
# own ([hst.align] once the configuration is wired); the NIRCam step
# ([nircam.align]) has a different recipe and different settings.
DEFAULTS = {
    # Which exposures are fitted to the reference catalog together.
    #   'visit'    : the exposures of a visit are first fitted to each other on
    #                their common sources, then fitted to the catalog as one body.
    #   'exposure' : every exposure is fitted to the catalog on its own (what
    #                the NIRCam step does).
    'pool': 'visit',
    # What is fitted for a pool against the reference catalog.
    #   'rigid'  : one shift and one rotation.
    #   'linear' : shift, rotation, scale and skew (six numbers), for the whole
    #              pool. ACS/WFC needs this: its two chips do not stand apart,
    #              the field as a whole carries a skew of 3 to 7 mas per 100".
    #   'auto'   : linear when the matches can hold a linear term to
    #              `linear_max_error`, rigid otherwise.
    'fit': 'auto',
    # mas per 100 arcsec. The term to be corrected is 3 to 7; a fit that cannot
    # do better than about 3 would add as much error as it removes.
    'linear_max_error': 3.0,
    # After the pool's fit, a shift and rotation for each chip: one per chip for
    # the pool, shared by that chip in every tied exposure and accepted only
    # when significant (the NIRCam rule, counted on distinct reference sources).
    # When one is accepted the exposures are tied a second time.
    'chip_fits': True,
    # On top of that, a fit of every chip in every exposure, each accepted when
    # its shift is significant against the shift's own standard error (all of
    # the chip's matches; the solver's `fine_min_significance` /
    # `exposure_min_significance`). Same setting and rule as [nircam.align].
    'exposure_fits': True,
    # Common sources needed to fit one exposure to another.
    'tie_min_matched': 20,
}

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
    proper-motion columns). ``config`` holds the recipe settings of
    :data:`DEFAULTS` (``pool``, ``fit``, ``linear_max_error``, ``per_chip_fit``,
    ``tie_min_matched``), the detection settings (``detect_*``, ``snr_min``,
    ``saturation_grow``) and matching settings passed to the solver
    (``coarse_searchrad``, ``match_radius``, ``min_matched``,
    ``max_residual_arcsec``, ...). The NIRCam names for the fit geometry
    (``fine_fitgeom``, ``coarse_fitgeom``) are refused here.

    The returned solution lists every chip; its ``diagnostics`` give the fit
    used by each pool and the number of exposures in it.
    """
    from campfire_pipeline.imaging.refcat.motion import propagate_to_epoch
    from campfire_pipeline.nircam.align.solve import (
        DetectorInput, GroupSolution, solve_exposure_group,
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
    borrowed = sorted(set(cfg) & {'relative', 'fine_fitgeom', 'coarse_fitgeom'})
    if borrowed:
        raise ValueError(
            f'{", ".join(borrowed)}: not a setting of the HST align step. Its recipe '
            f'is set with pool, fit, chip_fits and exposure_fits '
            f'(see hst/steps/align.py).')
    opt = {k: cfg.pop(k, v) for k, v in DEFAULTS.items()}
    if opt['pool'] not in ('visit', 'exposure'):
        raise ValueError(f"pool = {opt['pool']!r}: expected 'visit' or 'exposure'")
    if opt['fit'] not in ('auto', 'linear', 'rigid'):
        raise ValueError(f"fit = {opt['fit']!r}: expected 'auto', 'linear' or 'rigid'")
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

    # Which exposures are fitted to the reference catalog together. No pool
    # ever relies on the dither offsets in the headers: exposures share a pool
    # only after they have been fitted to each other, and an exposure that
    # could not be fitted to the others is fitted to the catalog on its own.
    rel = {f: '0' for f in files}
    pools = [[f] for f in files]
    if opt['pool'] == 'visit' and len(files) > 1:
        rel = _tie(by_exposure, key=key, min_matched=int(opt['tie_min_matched']),
                   solve=solve_exposure_group, config=cfg)
        pools = ([[f for f in files if rel[f] != '0']]
                 + [[f] for f in files if rel[f] == '0'])

    rc = f' rc={refcat_hash}' if refcat_hash else ''
    solved, all_detectors, n_matched, first, fits_used = False, [], 0, None, []
    chip_marks = []

    def fit_pool(members, inputs, pool_key):
        dets = [d for f in members for d in inputs[f]]
        # the same chip in every exposure of the pool shares one fit
        groups = ({d.detector: d.detector.rsplit('_', 1)[1] for d in dets}
                  if len(members) > 1 else None)
        return _fit_pool(
            dets, refcat, key=pool_key, fit=opt['fit'],
            linear_max_error=float(opt['linear_max_error']),
            chip_fits=bool(opt['chip_fits']), exposure_fits=opt['exposure_fits'],
            groups=groups, solve=solve_exposure_group, config=cfg)

    for members in pools:
        together = len(members) > 1
        pool_key = key if together else f'{key}:{os.path.basename(members[0])[:9]}'
        sol, used, lin_err = fit_pool(members, by_exposure, pool_key)
        carried = False
        if (together and sol.status == 'SOLVED'
                and (sol.diagnostics or {}).get('shared_fits')):
            # A chip was corrected in its own frame after the exposures were
            # tied; its rotation moves a source by a different amount in each
            # dither, so the tie is slightly off. Tie once more with the
            # corrected chips (the shared fits alone) and fit again from there,
            # as the NIRCam step does.
            fitted = {d.detector: d.wcs for d in sol.shared_detectors}
            corrected = {f: [dataclasses.replace(d, wcs=fitted[d.detector])
                             for d in by_exposure[f]] for f in members}
            again = _tie(corrected, key=key, min_matched=int(opt['tie_min_matched']),
                         solve=solve_exposure_group, config=cfg)
            if all(t != '0' for t in again.values()):
                second = fit_pool(members, corrected, pool_key)
                if second[0].status == 'SOLVED':
                    sol, used, lin_err = second
                    carried = True
        # chip: 'x' some chip of this pool took a fit of its own in one
        # exposure, 'v' a fit shared by the visit only, '-' no chip fit.
        if any(getattr(d, 'own_fit', False) for d in sol.detectors):
            mark = 'x'
        elif carried or (sol.diagnostics or {}).get('shared_fits'):
            mark = 'v'
        else:
            mark = '-'
        chip_marks.append(mark)
        first = first or sol
        fits_used.append(used)
        all_detectors.extend(sol.detectors)
        by_file = {f: {} for f in members}
        summary = {f: [] for f in members}
        for d in sol.detectors:
            f, extver = where[d.detector]
            ok = sol.status == 'SOLVED' and d.aligned
            by_file[f][extver] = d.wcs if ok else None
            summary[f].append((ok, d))
        for f in members:
            if sol.status != 'SOLVED' or not all(ok for ok, _ in summary[f]):
                _write_back(f, {v: None for v in by_file[f]}, cfp.NOT_ALIGNED)
                log(f'align[{key}]: {os.path.basename(f)} NOT_ALIGNED (WCS preserved)')
                continue
            solved = True
            res = max(d.residual_arcsec for _, d in summary[f])
            n = sum(d.n_matched for _, d in summary[f])
            n_matched += n
            # chip: '-' no chip fit, 'v' one fit per chip for the visit,
            #       'x' a fit per chip and exposure
            value = (f'fit={used} pool={"visit" if together else "exposure"} '
                     f'chip={mark} res={res * 1000:.1f}mas n={n} rel={rel[f]}{rc}')
            _write_back(f, by_file[f], value)
            log(f'align[{key}]: {os.path.basename(f)} {value}')
    return GroupSolution(
        key=key, status='SOLVED' if solved else 'NOT_ALIGNED', shift=first.shift,
        rot_deg=first.rot_deg, rmse_arcsec=first.rmse_arcsec, n_matched=n_matched,
        detectors=all_detectors,
        diagnostics={'fit': fits_used, 'pools': [len(m) for m in pools],
                     'chip': chip_marks})


def _linear_term_error(sol, detectors):
    """Expected error of a linear term fitted to this solution's matches, in
    mas per 100 arcsec, or ``inf`` when it cannot be estimated.

    For a slope fitted to N independent points with scatter s per axis over
    positions of spread d, the error is ``s / (sqrt(N) * d)``. N counts
    distinct reference sources (a source matched in four exposures carries the
    same catalog error four times). s is the scatter of the matches about a
    linear term (fitted here to the residuals with clipping, so that a real
    term is not counted as noise), and d is the spread of the matched sources
    along the direction in which they are most narrowly distributed.
    """
    catalogs = {d.detector: d.catalog for d in detectors}
    rows = []
    for d in sol.detectors:
        if d.matched is None or not d.aligned:
            continue
        m = np.asarray(d.matched, bool)
        cat = catalogs[d.detector]
        ra, dec = d.wcs.all_pix2world(np.asarray(cat['x'], float)[m],
                                      np.asarray(cat['y'], float)[m], 0)
        rows.append(np.column_stack([np.asarray(d.ref_ra, float)[m],
                                     np.asarray(d.ref_dec, float)[m], ra, dec]))
    if not rows:
        return float('inf')
    a = np.vstack(rows)
    sources = np.unique(a[:, :2], axis=0)
    if len(sources) < 6:
        return float('inf')
    cosd = np.cos(np.radians(sources[:, 1].mean()))
    xi = (a[:, 0] - sources[:, 0].mean()) * cosd * 3600.0
    eta = (a[:, 1] - sources[:, 1].mean()) * 3600.0
    design = np.column_stack([np.ones(len(a)), xi, eta])
    scatter = []
    for resid in ((a[:, 2] - a[:, 0]) * cosd * 3.6e6, (a[:, 3] - a[:, 1]) * 3.6e6):
        keep = np.ones(len(a), bool)
        for _ in range(4):
            coef, *_ = np.linalg.lstsq(design[keep], resid[keep], rcond=None)
            r = resid - design @ coef
            mad = 1.4826 * np.median(np.abs(r[keep] - np.median(r[keep])))
            keep = np.abs(r - np.median(r[keep])) <= 3 * max(mad, 1e-6)
        scatter.append(mad)
    sx = (sources[:, 0] - sources[:, 0].mean()) * cosd * 3600.0
    sy = (sources[:, 1] - sources[:, 1].mean()) * 3600.0
    spread = np.sqrt(max(np.linalg.eigvalsh(np.cov(np.vstack([sx, sy])))[0], 0.0))
    if not spread > 0:
        return float('inf')
    return float(np.mean(scatter) / (np.sqrt(len(sources)) * spread) * 100.0)


def _fit_pool(detectors, refcat, *, key, fit, linear_max_error, chip_fits,
              exposure_fits, groups, solve, config):
    """Fit one pool of chips to the reference catalog; return
    ``(solution, 'rigid' | 'linear', expected linear-term error)``.

    ``rigid`` is one shift and rotation for the pool. ``linear`` adds scale and
    skew (six numbers). With ``fit = 'auto'`` the rigid fit runs first and the
    linear fit replaces it when the matches can hold a linear term to
    ``linear_max_error`` (mas per 100 arcsec).

    With ``chip_fits`` each chip then gets a fit of its own: one for the pool,
    shared by that chip in every exposure (``groups``), and the solution also
    carries the alternative with a fit per chip and exposure plus the solver's
    verdict on which is closer to held-out reference sources. A pool of one
    exposure has no ``groups``; its chip fits are simply its own.
    """
    if not chip_fits:
        chips = {'fine_fitgeom': 'none'}
    elif groups:
        chips = {'fine_groups': groups, 'fine_individual': bool(exposure_fits)}
    else:
        chips = {}
    if fit == 'linear':
        sol = solve(detectors, refcat, key=key, dva_repivot=False,
                    coarse_fitgeom='general', **chips, **config)
        return sol, 'linear', float('nan')
    if fit == 'rigid':
        sol = solve(detectors, refcat, key=key, dva_repivot=False,
                    coarse_fitgeom='rshift', **chips, **config)
        return sol, 'rigid', float('nan')
    # 'auto': can the matches hold a linear term? Judged on the pooled shift
    # and rotation alone. Chip fits would absorb part of a field-wide term chip
    # by chip and leave residuals that no single linear term describes.
    probe = solve(detectors, refcat, key=key, dva_repivot=False,
                  coarse_fitgeom='rshift', fine_fitgeom='none', **config)
    err = (_linear_term_error(probe, detectors) if probe.status == 'SOLVED'
           else float('inf'))
    if err <= linear_max_error:
        lin = solve(detectors, refcat, key=key, dva_repivot=False,
                    coarse_fitgeom='general', **chips, **config)
        if lin.status == 'SOLVED' and all(d.aligned for d in lin.detectors):
            log(f'align[{key}]: linear fit (a linear term can be held to '
                f'{err:.1f} mas per 100 arcsec; limit {linear_max_error:g})')
            return lin, 'linear', err
    sol = (solve(detectors, refcat, key=key, dva_repivot=False,
                 coarse_fitgeom='rshift', **chips, **config)
           if chip_fits and probe.status == 'SOLVED' else probe)
    if sol.status == 'SOLVED':
        log(f'align[{key}]: shift and rotation only (a linear term would be '
            f'uncertain by {err:.1f} mas per 100 arcsec; limit {linear_max_error:g})')
    return sol, 'rigid', err


def _tie(by_exposure, *, key, min_matched, solve, config):
    """Fit each exposure to the visit's richest one on their common sources, in
    place. The offsets between exposures are measured here, not read from the
    headers.

    ``by_exposure`` maps file -> its chips' ``DetectorInput``; the WCS of every
    exposure that solves is replaced by the corrected one. Returns file -> the
    ``rel=`` token for the step stamp: ``ref`` for the reference exposure, the
    number of matched sources for a fitted one, ``0`` for one that could not be
    fitted (the caller then fits it to the reference catalog on its own).
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

    # Always one shift and rotation per exposure, both chips together.
    cfg = {k: v for k, v in config.items() if k != 'min_matched'}
    token = {reference: 'ref'}
    for f, dets in by_exposure.items():
        if f == reference:
            continue
        name = os.path.basename(f)
        sol = solve(dets, relcat, key=f'{key}:rel', dva_repivot=False,
                    fine_fitgeom='none', min_matched=min_matched, **cfg)
        if sol.status != 'SOLVED' or not all(d.aligned for d in sol.detectors):
            token[f] = '0'
            log(f'align[{key}]: {name} could not be fitted to '
                f'{os.path.basename(reference)} (too few common sources); it is '
                f'fitted to the reference catalog on its own')
            continue
        fitted = {d.detector: d.wcs for d in sol.detectors}
        by_exposure[f] = [dataclasses.replace(d, wcs=fitted[d.detector])
                          for d in dets]
        token[f] = str(sol.n_matched)
        log(f'align[{key}]: {name} tied to {os.path.basename(reference)}: '
            f'shift ({sol.shift[0] * 1000:+.1f}, {sol.shift[1] * 1000:+.1f}) mas, '
            f'{sol.n_matched} sources, rms {sol.rmse_arcsec * 1000:.1f} mas')
    return token
