"""
Other sources' light in a source's nod differences, predicted in one pass (prototype,
extraction v2). Supersedes the iterative ``contam_fit`` prototype.

Other slitlets of the MSA configuration disperse onto a source's trace
(``trace_overlap``). Their light is predicted from a quick first-pass spectrum of
every source and used to protect the source's shape fit:

* **Trace maps** (phase 0): one per exposure, cached under ``{workspace}/tracemaps``.
* **Pass 1**: every source of the configuration gets a spectrum from a fixed shape
  (the default shape, or for a grating the PRISM shape of the same source when the
  PRISM product took its own fit), solved after masking every pixel any other
  source's source shutter lights (geometry only, from the maps). The masking keeps
  a bright target's light out of a faint neighbour's pass-1 spectrum, so the
  neighbour's prediction cannot hand the target its own flux back. A source whose
  trace is mostly covered by others cannot be masked; it is solved unmasked,
  marked unreliable, and solved once more after subtracting the contaminants whose
  own pass 1 is reliable (``run_pass1``). A contaminant whose pass 1 stays
  unreliable is never subtracted from a target; its prediction (an upper bound: it may
  hold others' light) only masks the pixels where it exceeds the noise threshold. Alternative
  (``contam_pass1 = "stage3"``): the source's existing stage-3 optimal spectrum,
  converted to a true spectrum with the default shape's main-shutter factor.
  Pass 1 stores each exposure's model frame (the source's light alone, path loss
  removed) and the pixel calibration of that exposure, sparse, in
  ``{workspace}/pass1/{obs}_{disperser}_{sid}_pass1.npz``.
* **Prediction** for a target T: for each contaminant C named by the maps in T's
  nod difference k, C's frames ``j in {k} + partners(k)`` are moved onto T's pixels
  with weights +1 and -1/n (the target's own partner list, so any background
  override of either source is respected), converted from C's calibration to T's
  (pre-subtraction frame over rate, per pixel and per frame), and summed. Every
  pixel of C's frame image counts (an extended C spills into its other shutters),
  not only the source-shutter pixels the maps list.
* **Use** (``contam_mode``): ``mask`` drops pixels where the predicted light exceeds
  ``kappa`` times the target's per-pixel noise; ``hybrid`` (default) subtracts the
  prediction everywhere and drops the pixels where the prediction's own uncertainty
  exceeds ``kappa`` times the noise. Pixels whose light cannot be predicted
  (failed-open shutters, contaminants with no pass 1, no calibration) are dropped
  in both modes. This only changes the shape fit; the product's ``fnu_pf`` is
  extracted from the s2d unchanged, and the predicted light is reported beside it
  (``CONTAM2D``, ``fnu_pf_contam``).
"""

import glob
import os
import re
from dataclasses import dataclass, field

import numpy as np

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import trace_overlap as TO

MAPS_SUBDIR = 'tracemaps'
PASS1_SUBDIR = 'pass1'
PASS1_VERSION = '4'
DPRED_FLOOR = 0.1          # prediction uncertainty floor, fraction of the prediction (ASSUMED, to be measured)
_NOD = re.compile(r'^(jw\d+_\d{5})_(\d{5})_(nrs[12])')


def exposure_key(path):
    """'jw07076014001_03101_00001_nrs1' of a canonical, rate or map file."""
    m = _NOD.match(os.path.basename(path))
    return f'{m.group(1)}_{m.group(2)}_{m.group(3)}' if m else None


def nod_key(path):
    """('00001', 'nrs1')."""
    m = _NOD.match(os.path.basename(path))
    return m.group(2), m.group(3)


# ---------------------------------------------------------------------------
# Phase 0: trace maps
# ---------------------------------------------------------------------------
def map_tasks(workspace, canonical_paths, log=None):
    """One task per exposure of the canonicals: (rate or template rate, cache dir,
    full metafile, metadata id, dither position, exposure name).

    The full metafile (every slitlet of the configuration) is the one the rate
    file names; the canonicals name their per-source copy. An exposure without its
    own rate file uses a rate file of the same detector, disperser and filter as a
    template (``trace_overlap.full_mask_model``) and the only full metafile of its
    visit; when there is none, or several, it gets no map (its contaminants are
    then not predicted)."""
    from astropy.io import fits

    log = log or (lambda *a: None)
    cache = os.path.join(workspace, MAPS_SUBDIR)
    rates = {exposure_key(r): r for r in glob.glob(os.path.join(workspace, 'jw*_rate.fits'))}
    rate_meta = {}
    for k, r in rates.items():
        h = fits.getheader(r)
        rate_meta[k] = (str(h.get('DETECTOR')).upper(), str(h.get('GRATING')).upper(), str(h.get('FILTER')).upper(),
                        str(h.get('MSAMETFL', '')), h.get('MSAMETID'), h.get('PATT_NUM'))
    full_msa = [f for f in glob.glob(os.path.join(workspace, 'jw*_msa.fits'))]
    seen, tasks = set(), []
    for p in canonical_paths:
        key = exposure_key(p)
        if key is None or key in seen:
            continue
        seen.add(key)
        name = f'{key}_rate.fits'
        if TO.map_is_current(TO.trace_map_path(cache, name)):
            continue
        h = fits.getheader(p)
        if key in rates:
            det, grat, filt, msa_name, msa_id, patt = rate_meta[key]
            tasks.append((rates[key], cache, os.path.join(workspace, msa_name), int(msa_id), int(patt), name))
            continue
        want = (str(h.get('DETECTOR')).upper(), str(h.get('GRATING')).upper(), str(h.get('FILTER')).upper())
        rate = next((r for k, r in sorted(rates.items()) if rate_meta[k][:3] == want), None)
        visit = os.path.basename(p)[:13]
        msas = [f for f in full_msa if os.path.basename(f).startswith(visit)]
        if rate is None or len(msas) != 1:
            log(f'trace map {key}: no rate template or no unique full metafile ({len(msas)}); no map')
            continue
        tasks.append((rate, cache, msas[0], int(h['MSAMETID']), int(h['PATT_NUM']), name))
    return tasks


def build_map(rate, cache, msa, msa_id, patt, name):
    from campfire_pipeline.common.io import log
    work = os.path.join(cache, f'work_{name[:-10]}')
    try:
        TO.get_trace_map(rate, cache, workdir=work, exposure_name=name,
                         msa_file=msa, msa_metadata_id=msa_id, dither_position=patt)
        try:
            os.rmdir(work)                      # the build removes its staged copies; drop the empty dir
        except OSError:
            pass
        return name
    except Exception as e:                       # noqa: BLE001 - no map means no prediction, not a failed stage
        log(f'trace map {name}: failed ({type(e).__name__}: {e})')
        return None


def maps_for_exposures(workspace, exps):
    """Trace maps of the exposures' configurations, keyed by exposure key."""
    cache = os.path.join(workspace, MAPS_SUBDIR)
    out = {}
    for e in exps:
        key = exposure_key(e.name)
        path = TO.trace_map_path(cache, f'{key}_rate.fits')
        if key not in out and TO.map_is_current(path):
            out[key] = TO.TraceMap.read(path)
    return out


# ---------------------------------------------------------------------------
# Geometry: which other sources light which pixels
# ---------------------------------------------------------------------------
@dataclass
class Geometry:
    """Per exposure (index into the source's exposures): pixels any other source's
    source shutter lights (``mask``), those pixels per contaminating source id
    (``sources``, within the contaminant's nominal wavelength range), pixels whose
    other-source light cannot be predicted (``unmodelled``: other traces past their
    nominal range; failed-open shutters too with ``failed_open = 'mask'``), and
    pixels of failed-open shutters (``failed_open``)."""
    mask: list
    sources: list
    unmodelled: list
    have_maps: bool = True
    failed_open: list = None
    beyond: list = None


def geometry(exps, sid, maps, failed_open='flag', beyond='mask'):
    """Other sources' pixels on a source's nod differences (see ``Geometry``).

    failed_open : 'flag' (default) records failed-open shutters only: their sky is the
        same in every nod and cancels in the difference, and a sky object in one
        would be the only light left. 'mask' treats them as unmodelled (the
        behaviour before 2026-10-23).
    beyond : 'mask' (default): other traces past their nominal wavelength range are
        unmodelled (their light is there, pass 1 has no spectrum for it). 'ignore':
        those pixels are left out, as before 2026-10-23 (maps did not have them)."""
    n = len(exps)
    out = Geometry(mask=[np.zeros(e.y.shape, bool) for e in exps], sources=[{} for _ in exps],
                   unmodelled=[np.zeros(e.y.shape, bool) for e in exps],
                   failed_open=[np.zeros(e.y.shape, bool) for e in exps],
                   beyond=[np.zeros(e.y.shape, bool) for e in exps])
    for g in sorted({e.group for e in exps}):
        ks = [k for k in range(n) if exps[k].group == g]
        try:
            mlist = [maps[exposure_key(exps[k].name)] for k in ks]
        except KeyError:
            out.have_maps = False
            continue
        e0 = exps[ks[0]]
        ny, nx = e0.y.shape
        region = (e0.origin[1] - 1, e0.origin[0] - 1, nx, ny)
        partners = [[ks.index(j) for j in exps[k].partners] if exps[k].partners is not None else [] for k in ks]
        for r in TO.nod_contamination(mlist, int(sid), region, partners):
            k = ks[r.exposure]
            if r.kind == 'slitlet':
                bey = r.beyond if r.beyond is not None else np.zeros(r.iy.size, bool)
                if beyond == 'ignore':
                    r.iy, r.ix, bey = r.iy[~bey], r.ix[~bey], bey[~bey]
                out.mask[k][r.iy, r.ix] = True
                out.beyond[k][r.iy[bey], r.ix[bey]] = True
                out.unmodelled[k][r.iy[bey], r.ix[bey]] = True
                if (~bey).any():
                    px = out.sources[k].setdefault(r.source_id, np.zeros(exps[k].y.shape, bool))
                    px[r.iy[~bey], r.ix[~bey]] = True
            else:
                out.failed_open[k][r.iy, r.ix] = True
                if failed_open == 'mask':
                    out.mask[k][r.iy, r.ix] = True
                    out.unmodelled[k][r.iy, r.ix] = True
    return out


def calibration(e, rate_dir):
    """Per-pixel calibration of exposure e on its common frame: the calibrated frame
    (path loss removed) over the rate, NaN where either is unusable or missing."""
    from astropy.io import fits

    cal = np.full(e.y.shape, np.nan)
    key = exposure_key(e.name)
    rate_path = os.path.join(rate_dir, f'{key}_rate.fits')
    if not os.path.exists(rate_path) or not e.path:
        return cal
    rate = fits.getdata(rate_path, 'SCI')
    with fits.open(e.path) as h:
        if 'PRE_BKGSUB_SCI' not in h:
            return cal
        pre = np.asarray(h['PRE_BKGSUB_SCI'].data, float)
    r0, c0, ny, nx = e.cutout
    ys, xs = e.origin[0] + r0, e.origin[1] + c0
    raw = rate[ys - 1:ys - 1 + ny, xs - 1:xs - 1 + nx]
    if raw.shape != pre.shape:
        return cal
    with np.errstate(divide='ignore', invalid='ignore'):
        cal[r0:r0 + ny, c0:c0 + nx] = np.where(raw != 0, pre * e.pl_applied[r0:r0 + ny, c0:c0 + nx] / raw, np.nan)
    return cal


def diff_sigma(exps, k):
    """Per-pixel noise of exposure k's nod difference (the profile fit's weighting)."""
    e = exps[k]
    n = len(e.partners)
    s2 = (e.var + sum(exps[j].var for j in e.partners)) / (n + 1)
    return np.sqrt(((n + 1) / n) ** 2 * s2)


def main_shutter(e, c_plan):
    return np.isfinite(e.y) & (np.abs(e.y - c_plan) < PF.OPEN_HALF)


# ---------------------------------------------------------------------------
# Pass 1
# ---------------------------------------------------------------------------
def pass1_path(workspace, product):
    return os.path.join(workspace, PASS1_SUBDIR, f'{product}_pass1.npz')


def default_msf(R):
    return PF.main_shutter_factor(R, R.lam)


def pass1_settings(stage_config):
    """The pass-1 knobs of ``[nirspec.stage3]`` (also the cache signature)."""
    g = stage_config.get
    return dict(mode=str(g('contam_pass1', 'fixed')).lower(),
                smooth=int(g('contam_pass1_smooth', 31)),
                guard=float(g('contam_pass1_guard', 3.0)),
                shape=str(g('contam_pass1_shape', 'chain')).lower(),
                failed_open=str(g('contam_failed_open', 'flag')).lower(),
                beyond=str(g('contam_beyond_range', 'mask')).lower(),
                gate_prism=float(g('gate_snr_prism', 50.0)), gate_grating=float(g('gate_snr_grating', 70.0)),
                gate_noise=str(g('gate_noise', 'mean_square')).lower())


def _signature(s):
    return f"{PASS1_VERSION}|{TO.MAP_VERSION}|" + '|'.join(f'{k}={s[k]}' for k in sorted(s))


def _pass1_valid(out, groups, n_maps, sig):
    if not os.path.exists(out):
        return None
    newest = max(os.path.getmtime(f) for g in groups for f in g)
    if os.path.getmtime(out) <= newest:
        return None
    try:
        z = np.load(out)
        if str(z['sig']) == sig and int(z['n_maps']) == n_maps:
            return z
    except (KeyError, OSError, ValueError):
        pass
    return None


def smooth_spectrum(flux, err, n=31, clip=5.0, n_iter=3):
    """Pass-1 spectrum for predicting light: outliers clipped, then a running mean.

    A point is clipped when it is more than ``clip`` sigma from the inverse-variance
    mean of the ``n`` points around it (itself and clipped points left out). The
    first reference is the running median, so one wild point does not drag its
    neighbours out with it; then up to ``n_iter`` rounds against the mean. The
    result is the inverse-variance running mean of the remaining points over ``n``
    grid points, its error the error of that mean. A full-resolution pass-1 solve
    is mostly noise for faint sources, and a few points of an ill-conditioned solve
    can be wildly off; the prediction only needs the continuum level. Strong
    emission lines of the contaminant are clipped too (smeared out of the
    prediction). Returns (flux, err, clipped)."""
    from scipy.ndimage import median_filter

    f = np.asarray(flux, float)
    e = np.asarray(err, float)
    ok = np.isfinite(f) & np.isfinite(e) & (e > 0)
    if n is None or n <= 1 or ok.sum() < 3:
        return f.copy(), e.copy(), np.zeros(f.size, bool)
    k = np.ones(int(n))
    med = np.full(f.size, np.nan)
    med[ok] = median_filter(f[ok], size=int(n), mode='nearest')
    with np.errstate(invalid='ignore'):
        keep = ok & ~(np.abs(f - med) > clip * e)
    for _ in range(n_iter):
        w = np.where(keep, 1.0 / np.where(ok, e, 1.0) ** 2, 0.0)
        fw = np.where(keep, f * w, 0.0)
        sw = np.convolve(w, k, 'same') - w                  # leave the point itself out
        sfw = np.convolve(fw, k, 'same') - fw
        with np.errstate(invalid='ignore', divide='ignore'):
            mean = sfw / sw
            z = np.abs(f - mean) / np.sqrt(e ** 2 + 1.0 / sw)
        new = np.where(sw > 0, ok & ~(z > clip), keep)     # no reference left: keep the state
        if np.array_equal(new, keep):
            break
        keep = new
    w = np.where(keep, 1.0 / np.where(ok, e, 1.0) ** 2, 0.0)
    sw = np.convolve(w, k, 'same')
    sfw = np.convolve(np.where(keep, f * w, 0.0), k, 'same')
    with np.errstate(invalid='ignore', divide='ignore'):
        fs = np.where(sw > 0, sfw / sw, np.nan)
        es = np.where(sw > 0, 1.0 / np.sqrt(sw), np.nan)
    fs = np.where(np.isfinite(f), fs, np.nan)
    es = np.where(np.isfinite(f), es, np.nan)
    return fs, es, ok & ~keep


def stage3_p95(workspace, product):
    """95th percentile of |flux| [uJy] of the source's standard stage-3 spectrum
    (``_spec.fits`` fnu, else Spec3's ``_x1d.fits``), or None."""
    from astropy.io import fits
    spec = os.path.join(workspace, f'{product}_spec.fits')
    x1d = os.path.join(workspace, f'{product}_x1d.fits')
    try:
        if os.path.exists(spec):
            f = np.asarray(fits.getdata(spec, 'SPEC1D')['fnu'], float)
        elif os.path.exists(x1d):
            f = np.asarray(fits.getdata(x1d, 'EXTRACT1D')['FLUX'], float) * 1e6      # Jy -> uJy
        else:
            return None
    except (KeyError, OSError, ValueError):
        return None
    f = f[np.isfinite(f)]
    return float(np.percentile(np.abs(f), 95)) if f.size > 10 else None


def compute_pass1(exps, sid, maps, workspace, is_prism, stage_config, prism_shape=None, second=False,
                  pass1_for=None, stage3_ref=None, stage3_spec=None):
    """Pass 1 of one source on its loaded exposures (in memory; ``run_pass1`` writes it).

    Shape: with ``contam_pass1_shape = 'chain'`` (default) each source is predicted
    with the shape the chain would give it: for a grating the PRISM shape of the
    same source if the PRISM product took its own fit (``prism_shape``); else the
    source's own fit with the gated priors when it is above its gate and the fit is
    sane (no bound, planned positions agree); else the default shape.
    ``'default'``: the default (or PRISM) shape only, as before 2026-10-23. A
    default-shape prediction of an offset or extended contaminant carries only part
    of its light (B's injection: 55-60 %).

    Spectrum: the fixed-shape solve on the masked data, then ``smooth_spectrum``
    (``contam_pass1_smooth`` points; 0 = full resolution). Sanity guard: a spectrum
    whose 95th percentile of |flux| (as reported, uJy) exceeds ``contam_pass1_guard``
    times that of the source's stage-3 spectrum (``stage3_ref``) is marked
    unreliable (an ill-conditioned solve); it then only masks.

    Returns a ``Pass1`` with extra attributes (clean, why, shape_src, snr_det,
    flux_raw, err_raw)."""
    from campfire_pipeline.nirspec import profile_chain as PC

    st = pass1_settings(stage_config)
    cfg = PC.ChainConfig.from_stage_config(stage_config)
    geo = geometry(exps, sid, maps, failed_open=st['failed_open'], beyond=st['beyond'])
    c_plan = PF.planned_position(exps)[0]
    saved = [e.ok.copy() for e in exps]
    saved_diff = [e.diff for e in exps]
    before = sum(int((e.ok & main_shutter(e, c_plan)).sum()) for e in exps if e.use_data)
    if second:
        pred = predict(exps, sid, maps, pass1_for, workspace, failed_open=st['failed_open'], beyond=st['beyond'])
        apply(exps, pred, mode='hybrid', kappa=np.inf)           # subtract; mask only the unpredictable
    else:
        for e, m in zip(exps, geo.mask):
            e.ok = e.ok & ~m
    after = sum(int((e.ok & main_shutter(e, c_plan)).sum()) for e in exps if e.use_data)
    reliable, why = True, ''
    if before == 0 or after < 0.3 * before:          # trace covered by others: solve without the mask
        for e, ok in zip(exps, saved):
            e.ok = ok.copy()
        reliable, why = False, 'covered'
    M = PF.ProfileModel(exps, chromatic=True, priors=cfg.priors, bounds=cfg.bounds)
    snr, _ = PF.detection_snr(M, scale=st['gate_noise'])
    shape, shape_src = None, 'default'
    if not is_prism and prism_shape is not None and prism_shape.shape == 'own':
        shape, shape_src = PF.map_shape(prism_shape.params, M.pivot), 'prism'
    elif st['shape'] == 'chain' and reliable and snr >= (cfg.gate_prism if is_prism else cfg.gate_grating):
        try:
            Ro = PF.fit(exps, model=M)
            if not Ro.bound_bits and PF.planned_position(exps, cfg.srcypos_tolerance)[1] != 2:
                shape = dict(centre_offset=Ro.params['centre'] - Ro.params['centre_prior'], drift=Ro.params['drift'],
                             width=Ro.params['width'], width_change=Ro.params['width_change'])
                shape_src = 'own'
        except Exception:                        # noqa: BLE001 - default shape then
            pass
    R = PF.fit_fixed(model=M, shape=shape)
    raw, raw_err = R.flux.copy(), R.flux_err.copy()
    src = 'solve'
    if st['mode'] == 'stage3':
        if stage3_spec is not None:
            lam, fnu, err = stage3_spec
            msf = default_msf(R)
            ok = np.isfinite(lam) & np.isfinite(fnu)
            o = np.argsort(lam[ok])
            f = np.interp(R.lam, lam[ok][o], fnu[ok][o], left=np.nan, right=np.nan) / 1e12
            fe = np.interp(R.lam, lam[ok][o], np.nan_to_num(err[ok][o]), left=np.nan, right=np.nan) / 1e12
            R.flux = np.where(msf > 0, f / np.maximum(msf, 1e-6), np.nan)
            R.flux_err = np.where(msf > 0, fe / np.maximum(msf, 1e-6), np.nan)
            src = 'stage3'
            reliable, why = True, ''   # stage-3 spectra are not decontaminated either way; taken as they are
        else:
            src = 'solve(no_stage3)'
    if src != 'stage3' and st['smooth'] > 1:
        R.flux, R.flux_err, _ = smooth_spectrum(R.flux, R.flux_err, n=st['smooth'])
    if src != 'stage3' and st['guard'] > 0 and stage3_ref:
        rep = np.abs(R.flux * PF.main_shutter_factor(R, R.lam)) * 1e12
        rep = rep[np.isfinite(rep)]
        if rep.size > 10 and np.percentile(rep, 95) > st['guard'] * stage3_ref:
            reliable, why = False, 'guard'
    for e, ok, d in zip(exps, saved, saved_diff):
        e.ok, e.diff = ok, d
    frames, nosrc = {}, []
    for j, e in enumerate(exps):
        if not e.has_source:
            nosrc.append(exposure_key(e.name))
            continue
        img = PF.frame_image(R, j)
        err = PF.frame_image(R, j, spectrum=np.abs(np.nan_to_num(R.flux_err)))
        good = np.isfinite(img) & (np.abs(img) > 0)
        if not good.any():
            nosrc.append(exposure_key(e.name))
            continue
        cal = calibration(e, workspace)
        idx = np.flatnonzero(good)
        frames[exposure_key(e.name)] = dict(
            origin=np.array(e.origin), shape=np.array(e.y.shape), idx=idx.astype(np.int32),
            img=img.ravel()[idx].astype(np.float32), err=err.ravel()[idx].astype(np.float32),
            cal=cal.ravel()[idx].astype(np.float32))
    P1 = Pass1(sid=int(sid), lam=R.lam, flux=R.flux, flux_err=R.flux_err, frames=frames, reliable=reliable,
               nosrc=set(nosrc))
    P1.clean = after / before if before else 0.0
    P1.why, P1.shape_src, P1.spec_src, P1.snr_det = why, shape_src, src, snr
    P1.flux_raw, P1.err_raw = raw, raw_err
    P1.result = R
    return P1


def run_pass1(product, workspace, groups, overrides, science, sid, is_prism, stage_config,
              prism_product=None, overwrite=False, pass1_products=None, second=False):
    """Pass-1 spectrum and per-exposure model frames of one source (see module doc and
    ``compute_pass1``). Returns the output path, or None (logged) on failure.

    First round (``second=False``): every pixel another source's source shutter
    lights is masked before the solve. When that leaves less than 30 % of the
    source's main-shutter pixels (its trace is covered by others) the solve is
    done unmasked and the result is marked unreliable: it may contain the
    others' light.

    Second round (``second=True``, only for unreliable results): the light of the
    contaminants whose own pass 1 is reliable is predicted and subtracted, pixels
    of the remaining ones are masked, and the source is solved again; it becomes
    reliable when enough of its trace is left (and it passes the sanity guard).
    Still one pass: nothing is iterated, a second-round result is never used to
    redo another source."""
    from campfire_pipeline.common.io import log

    out = pass1_path(workspace, product)
    try:
        st = pass1_settings(stage_config)
        sig = _signature(st)
        keys = {exposure_key(f) for g in groups for f in g}
        n_maps = sum(os.path.exists(TO.trace_map_path(os.path.join(workspace, MAPS_SUBDIR), f'{k}_rate.fits'))
                     for k in keys)
        prev = _pass1_valid(out, groups, n_maps, sig)
        if second:
            if prev is None or bool(prev['reliable']) or str(prev['round']) == '2':
                return out
        elif prev is not None and not overwrite:
            return out
        exps = PF.load_source(groups, bkg_overrides=overrides, science=set(science))
        maps = maps_for_exposures(workspace, exps)
        prism = None
        if not is_prism and prism_product:
            prism = read_prism_shape(workspace, prism_product)
        names = pass1_products or {}

        def reliable_for(csid):
            name = names.get(int(csid))
            path = pass1_path(workspace, name) if name else None
            if not path or not os.path.exists(path):
                return None
            P1 = Pass1.read(path)
            return P1 if P1.reliable else None
        spec3 = None
        if st['mode'] == 'stage3':
            spec = os.path.join(workspace, f'{product}_spec.fits')
            if os.path.exists(spec):
                from astropy.io import fits
                with fits.open(spec) as h:
                    d = h['SPEC1D'].data
                    spec3 = (np.asarray(d['wave'], float), np.asarray(d['fnu'], float), np.asarray(d['fnu_err'], float))
        P1 = compute_pass1(exps, sid, maps, workspace, is_prism, stage_config, prism_shape=prism, second=second,
                           pass1_for=reliable_for, stage3_ref=stage3_p95(workspace, product), stage3_spec=spec3)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        arrays = dict(version=PASS1_VERSION, sig=sig, n_maps=n_maps, mode=st['mode'], sid=int(sid), lam=P1.lam,
                      flux=P1.flux, flux_err=P1.flux_err, flux_raw=P1.flux_raw, err_raw=P1.err_raw,
                      snr_det=P1.snr_det, shape_src=P1.shape_src, spec_src=P1.spec_src, reliable=P1.reliable,
                      why=P1.why, round='2' if second else '1', clean=P1.clean,
                      keys=np.array(sorted(P1.frames)), nosrc=np.array(sorted(P1.nosrc)))
        for key, fr in P1.frames.items():
            for name, v in fr.items():
                arrays[f'{key}__{name}'] = v
        tmp = out + '.tmp.npz'
        np.savez_compressed(tmp, **arrays)
        os.replace(tmp, out)
        return out
    except Exception as e:                       # noqa: BLE001
        log(f'{product}: pass 1 failed ({type(e).__name__}: {e})')
        return None


@dataclass
class Pass1:
    sid: int
    lam: np.ndarray
    flux: np.ndarray
    flux_err: np.ndarray
    frames: dict
    reliable: bool = True
    nosrc: set = None                # exposure keys whose frame holds no light of the source (not missing)

    @classmethod
    def read(cls, path):
        z = np.load(path, allow_pickle=False)
        frames = {}
        for key in z['keys']:
            key = str(key)
            frames[key] = {n: z[f'{key}__{n}'] for n in ('origin', 'shape', 'idx', 'img', 'err', 'cal')}
        P = cls(sid=int(z['sid']), lam=z['lam'], flux=z['flux'], flux_err=z['flux_err'], frames=frames,
                reliable=bool(z['reliable']) if 'reliable' in z else True,
                nosrc={str(x) for x in z['nosrc']} if 'nosrc' in z else None)
        P.why = str(z['why']) if 'why' in z else ''
        P.shape_src = str(z['shape_src']) if 'shape_src' in z else ''
        return P


def read_prism_shape(workspace, prism_product):
    """``PrismShape`` of a PRISM product: from its ``_pf.fits`` (this run) or its
    ``_spec.fits`` (an earlier run)."""
    from astropy.io import fits
    from campfire_pipeline.nirspec import profile_chain as PC

    cands = [os.path.join(workspace, f'{prism_product}{s}') for s in ('_pf.fits', '_spec.fits')]
    cands += sorted(glob.glob(os.path.join(workspace, f'{prism_product}_g*_spec.fits')))
    for p in cands:
        if os.path.exists(p):
            try:
                return PC.PrismShape.from_header(prism_product, fits.getheader(p))
            except (KeyError, OSError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# Prediction and use
# ---------------------------------------------------------------------------
@dataclass
class Prediction:
    pred: list                       # per exposure: predicted other-source light in the nod difference (diff units)
    dpred: list                      # its uncertainty
    unmodelled: list                 # per exposure: pixels whose foreign light cannot be predicted
    contributors: dict = field(default_factory=dict)   # sid -> max |pred| / sigma on the target's main shutter
    missing: set = field(default_factory=set)          # contaminants without a pass 1
    unreliable: set = field(default_factory=set)       # contaminants whose pass 1 may hold the target's light
    pred_unreliable: list = None     # per exposure: their predicted light (masking only, never subtracted)
    pair: int = 0
    pair_id: int = -1
    pair_ratio: float = np.nan
    pair_frac: float = np.nan        # the pair contaminant's light in the target's extraction / the target's
    have_maps: bool = True
    by_source: dict = field(default_factory=dict, repr=False)   # sid -> per exposure predicted light (pair flag)
    pair_candidates: list = field(default_factory=list)       # [(sid, pass-1 flux ratio)] within the ratio window
    failed_open: list = None         # per exposure: pixels of failed-open shutters (flag only by default)
    beyond: list = None              # per exposure: pixels of other traces past their nominal range (unmodelled)
    missing_frame: set = field(default_factory=set)     # contaminants with a pass-1 frame missing (masked)


def _place_sparse(fr, origin_t, shape_t):
    """Target-frame (iy, ix) of a pass-1 frame's sparse pixels and a validity mask."""
    ny_c, nx_c = (int(x) for x in fr['shape'])
    iy, ix = np.divmod(fr['idx'].astype(np.int64), nx_c)
    ty = iy + int(fr['origin'][0]) - int(origin_t[0])
    tx = ix + int(fr['origin'][1]) - int(origin_t[1])
    ok = (ty >= 0) & (ty < shape_t[0]) & (tx >= 0) & (tx < shape_t[1])
    return ty, tx, ok


def predict(exps, sid, maps, pass1_for, rate_dir, own_pass1=None, pair_ratio=3.0, failed_open='flag',
            beyond='mask'):
    """Predicted other sources' light in each science nod difference of a target.

    pass1_for : callable sid -> ``Pass1`` or None.
    A contaminant whose pass 1 lacks a frame of an exposure that holds its light
    (the frame is neither empty nor without the source) cannot be predicted in the
    nod differences that use that exposure: its pixels there are unmodelled."""
    geo = geometry(exps, sid, maps, failed_open=failed_open, beyond=beyond)
    n = len(exps)
    pred = [np.zeros(e.y.shape) for e in exps]
    dpred = [np.zeros(e.y.shape) for e in exps]
    pred_u = [np.zeros(e.y.shape) for e in exps]
    unmod = [u.copy() for u in geo.unmodelled]
    c_plan = PF.planned_position(exps)[0]
    cals = {}

    def cal_of(j):
        if j not in cals:
            cals[j] = calibration(exps[j], rate_dir)
        return cals[j]

    cache, missing, contrib, unreliable, missing_frame, by_source = {}, set(), {}, set(), set(), {}
    for k in range(n):
        e = exps[k]
        if not e.use_data or e.partners is None or not geo.sources[k]:
            continue
        sig = diff_sigma(exps, k)
        ms = main_shutter(e, c_plan) & e.ok
        terms = [(k, 1.0)] + [(j, -1.0 / len(e.partners)) for j in e.partners]
        for csid, px in geo.sources[k].items():
            if csid not in cache:
                cache[csid] = pass1_for(csid)
            P1 = cache[csid]
            if P1 is None:
                missing.add(csid)
                unmod[k] |= px
                continue
            if not P1.reliable:            # its pass 1 may carry the target's light: do not hand it back
                unreliable.add(csid)
            add = np.zeros(e.y.shape)
            dadd = np.zeros(e.y.shape)
            covered = np.zeros(e.y.shape, bool)
            for j, w in terms:
                key = exposure_key(exps[j].name)
                fr = P1.frames.get(key)
                if fr is None:
                    if P1.nosrc is not None and key not in P1.nosrc:
                        unmod[k] |= px                 # light there, but no frame to predict it from
                        missing_frame.add(csid)
                    continue
                ty, tx, ok = _place_sparse(fr, e.origin, e.y.shape)
                ct = cal_of(j)[ty[ok], tx[ok]]
                with np.errstate(divide='ignore', invalid='ignore'):
                    conv = ct / fr['cal'][ok].astype(float)
                good = np.isfinite(conv)
                img = fr['img'][ok].astype(float)
                bad = ~good & (np.abs(img) > 0)
                unmod[k][ty[ok][bad], tx[ok][bad]] = True
                covered[ty[ok], tx[ok]] = True
                np.add.at(add, (ty[ok][good], tx[ok][good]), w * img[good] * conv[good])
                np.add.at(dadd, (ty[ok][good], tx[ok][good]), abs(w) * fr['err'][ok][good].astype(float) * conv[good])
            if P1.nosrc is not None and not any(exposure_key(exps[j].name) in P1.nosrc for j, _ in terms):
                # mapped pixels of the contaminant its pass-1 frames do not reach (its cutout or its
                # spectrum ends short of its nominal range): light there, no prediction
                unmod[k] |= px & ~covered
            if not P1.reliable:
                pred_u[k] += add           # an upper bound, for masking only
            else:
                pred[k] += add
                dpred[k] += dadd
                with np.errstate(invalid='ignore', divide='ignore'):
                    s = np.abs(add) / sig
                v = float(np.nanmax(np.where(ms, s, np.nan))) if ms.any() else 0.0
                contrib[csid] = max(contrib.get(csid, 0.0), v if np.isfinite(v) else 0.0)
            by_source.setdefault(csid, [None] * n)
            by_source[csid][k] = add if by_source[csid][k] is None else by_source[csid][k] + add
    for k in range(n):
        dpred[k] = np.maximum(dpred[k], DPRED_FLOOR * np.abs(pred[k]))
    out = Prediction(pred=pred, dpred=dpred, unmodelled=unmod, contributors=contrib, missing=missing,
                     unreliable=unreliable, pred_unreliable=pred_u, have_maps=geo.have_maps,
                     by_source=by_source, failed_open=geo.failed_open, beyond=geo.beyond,
                     missing_frame=missing_frame)
    # similar-brightness pair candidates: a contaminant above the noise on the target's
    # main shutter whose pass-1 flux is within a factor pair_ratio of the target's. The
    # flag itself (``pair_flag``) also needs the contaminant's light to matter in the
    # target's extraction, which needs the target's fitted shape.
    if own_pass1 is not None:
        for csid in set(contrib) | unreliable:
            v = contrib.get(csid, np.inf)            # unreliable ones: no prediction, compare the spectra anyway
            P1 = cache.get(csid)
            if v <= 1.0 or P1 is None:
                continue
            fc = np.interp(own_pass1.lam, P1.lam, np.nan_to_num(P1.flux), left=np.nan, right=np.nan)
            ft = own_pass1.flux
            ok = np.isfinite(fc) & np.isfinite(ft)
            if ok.sum() < 5:
                continue
            ratio = np.nansum(fc[ok]) / np.nansum(ft[ok]) if np.nansum(ft[ok]) > 0 else np.inf
            if 1.0 / pair_ratio < ratio < pair_ratio:
                out.pair_candidates.append((int(csid), float(ratio)))
        out.pair_candidates.sort(key=lambda t: abs(np.log(t[1])))
        if out.pair_candidates:                      # provisional (ratio only) until pair_flag runs
            out.pair, out.pair_id, out.pair_ratio = 1, out.pair_candidates[0][0], out.pair_candidates[0][1]
    for csid in list(by_source):                     # keep per-source light only where the pair flag needs it
        if csid not in {c for c, _ in out.pair_candidates}:
            del by_source[csid]
    return out


def _target_light(R, exps, k, c_plan):
    """The fitted target's nod-difference model on exposure k and its main-shutter
    pixels (diff units, common frame)."""
    m = PF.model_frame(R, k, applied_pathloss=False, common=True)
    ms = main_shutter(exps[k], c_plan) & np.isfinite(m)
    return m, ms


def pair_flag(P, R, exps, min_frac=0.3):
    """Final similar-brightness pair flag: a ratio candidate (``predict``) whose light in
    the target's profile-weighted extraction is at least ``min_frac`` of the target's
    own (both with the target's fitted profile ``R``, main shutter; the target's own
    light is its model, not the data). Sets ``P.pair``, ``pair_id``, ``pair_ratio``,
    ``pair_frac`` (the candidates' best fraction is kept even when it does not fire)."""
    P.pair, P.pair_id, P.pair_ratio, P.pair_frac = 0, -1, np.nan, np.nan
    if not P.pair_candidates:
        return P
    c_plan = R.model.c_prior
    M = R.model
    tgt, rows = 0.0, {}
    for k, e in enumerate(exps):
        if not e.use_data or e.partners is None:
            continue
        m, ms = _target_light(R, exps, k, c_plan)
        V = M.diff_variance(k)
        ok = ms & np.isfinite(V) & (V > 0)
        rows[k] = (m, ok, V)
        tgt += float(np.sum((m * m / V)[ok]))
    best = None
    for csid, ratio in P.pair_candidates:
        arrs = P.by_source.get(csid)
        if arrs is None:
            continue
        num = 0.0
        for k, (m, ok, V) in rows.items():
            if arrs[k] is not None:
                num += float(np.nansum((m * arrs[k] / V)[ok]))
        frac = abs(num) / tgt if tgt > 0 else np.inf
        if best is None or frac > best[2]:
            best = (csid, ratio, frac)
    if best is not None:
        P.pair_frac = float(best[2])
        if best[2] >= min_frac:
            P.pair, P.pair_id, P.pair_ratio = 1, int(best[0]), float(best[1])
    return P


def contam_light_ratio(P, R, exps):
    """Other sources' predicted light on the target's main shutter over the target's
    own fitted light there: sum |prediction| / sum |model| over the main-shutter pixels
    of every science nod difference (predictions of unreliable contaminants
    included). Above ~1 the contaminants dominate the pixels the shape is fitted on."""
    c_plan = R.model.c_prior
    num = den = 0.0
    for k, e in enumerate(exps):
        if not e.use_data or e.partners is None:
            continue
        m, ms = _target_light(R, exps, k, c_plan)
        pr = P.pred[k] + (P.pred_unreliable[k] if P.pred_unreliable is not None else 0.0)
        ok = ms & np.isfinite(pr)
        num += float(np.sum(np.abs(pr[ok])))
        den += float(np.sum(np.abs(m[ok])))
    return num / den if den > 0 else np.inf


def apply(exps, P, mode='hybrid', kappa=1.0, dominance_kappa=1.0):
    """Apply a prediction to the exposures before the shape fit (in place).

    Returns dict(overall, per_exposure, masked) with the fraction of the target's
    main-shutter pixels kept, and ``clean_pred`` / ``clean_pred_exposure``: the
    fraction of main-shutter pixels where the predicted other-source light (and the
    unmodelled pixels) stays below ``dominance_kappa`` times the noise, whatever the
    mode did with them (in hybrid mode a well-predicted contaminant is subtracted,
    not masked, so ``overall`` does not say how much of the trace it covers).
    Also ``failed_open``: the fraction of main-shutter pixels in failed-open shutters.
    ``mode``: mask | hybrid (see module doc)."""
    if mode not in ('mask', 'hybrid'):
        raise ValueError(f"contam_mode must be 'mask' or 'hybrid', got {mode!r}")
    c_plan = PF.planned_position(exps)[0]
    per, tot_b, tot_a, tot_c, tot_fo, per_c = [], 0, 0, 0, 0, []
    for k, e in enumerate(exps):
        if not e.use_data or e.partners is None:
            continue
        ms = main_shutter(e, c_plan)
        b = int((e.ok & ms).sum())
        sig = diff_sigma(exps, k)
        drop = P.unmodelled[k].copy()
        with np.errstate(invalid='ignore'):
            tot = np.abs(P.pred[k] + (P.pred_unreliable[k] if P.pred_unreliable is not None else 0.0))
            dirty = P.unmodelled[k] | (tot > dominance_kappa * sig)
            if P.pred_unreliable is not None:
                drop |= np.abs(P.pred_unreliable[k]) > kappa * sig
            if mode == 'mask':
                drop |= np.abs(P.pred[k]) > kappa * sig
            else:
                e.diff = np.where(np.isfinite(P.pred[k]), e.diff - P.pred[k], e.diff)
                drop |= P.dpred[k] > kappa * sig
        c = int((e.ok & ms & ~dirty).sum())
        if P.failed_open is not None:
            tot_fo += int((e.ok & ms & P.failed_open[k]).sum())
        e.ok = e.ok & ~drop
        a = int((e.ok & ms).sum())
        tot_b += b
        tot_a += a
        tot_c += c
        per.append(a / b if b else 1.0)
        per_c.append(c / b if b else 1.0)
    overall = tot_a / tot_b if tot_b else 1.0
    return dict(overall=overall, per_exposure=per, masked=1.0 - overall,
                clean_pred=tot_c / tot_b if tot_b else 1.0, clean_pred_exposure=per_c,
                failed_open=tot_fo / tot_b if tot_b else 0.0)


def live_frames(exps, P):
    """Predicted light per science exposure in the live canonical units (path loss
    re-applied) on its cutout, keyed by file name, for drizzling (``CONTAM2D``)."""
    out = {}
    for k, e in enumerate(exps):
        if not e.use_data or e.partners is None:
            continue
        r0, c0, ny, nx = e.cutout
        with np.errstate(invalid='ignore', divide='ignore'):
            a = P.pred[k] / e.pl_applied
        out[e.name] = np.nan_to_num(a[r0:r0 + ny, c0:c0 + nx])
    return out
