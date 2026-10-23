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
PASS1_VERSION = '2'
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
        if os.path.exists(TO.trace_map_path(cache, name)):
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
        if key not in out and os.path.exists(path):
            out[key] = TO.TraceMap.read(path)
    return out


# ---------------------------------------------------------------------------
# Geometry: which other sources light which pixels
# ---------------------------------------------------------------------------
@dataclass
class Geometry:
    """Per exposure (index into the source's exposures): pixels any other source's
    source shutter lights (``mask``), those pixels per contaminating source id
    (``sources``), and pixels of failed-open shutters (``unmodelled``)."""
    mask: list
    sources: list
    unmodelled: list
    have_maps: bool = True


def geometry(exps, sid, maps):
    n = len(exps)
    out = Geometry(mask=[np.zeros(e.y.shape, bool) for e in exps], sources=[{} for _ in exps],
                   unmodelled=[np.zeros(e.y.shape, bool) for e in exps])
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
            out.mask[k][r.iy, r.ix] = True
            if r.kind == 'slitlet':
                px = out.sources[k].setdefault(r.source_id, np.zeros(exps[k].y.shape, bool))
                px[r.iy, r.ix] = True
            else:
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


def _pass1_valid(out, groups, n_maps, mode):
    if not os.path.exists(out):
        return None
    newest = max(os.path.getmtime(f) for g in groups for f in g)
    if os.path.getmtime(out) <= newest:
        return None
    try:
        z = np.load(out)
        if str(z['version']) == PASS1_VERSION and int(z['n_maps']) == n_maps and str(z['mode']) == mode:
            return z
    except (KeyError, OSError, ValueError):
        pass
    return None


def run_pass1(product, workspace, groups, overrides, science, sid, is_prism, stage_config,
              prism_product=None, overwrite=False, pass1_products=None, second=False):
    """Pass-1 spectrum and per-exposure model frames of one source (see module doc).
    Returns the output path, or None (logged) on failure.

    First round (``second=False``): every pixel another source's source shutter
    lights is masked before the solve. When that leaves less than 30 % of the
    source's main-shutter pixels (its trace is covered by others) the solve is
    done unmasked and the result is marked unreliable: it may contain the
    others' light.

    Second round (``second=True``, only for unreliable results): the light of the
    contaminants whose own pass 1 is reliable is predicted and subtracted, pixels
    of the remaining ones are masked, and the source is solved again; it becomes
    reliable when enough of its trace is left. Still one pass: nothing is
    iterated, a second-round result is never used to redo another source."""
    from campfire_pipeline.common.io import log
    from campfire_pipeline.nirspec import profile_chain as PC

    out = pass1_path(workspace, product)
    try:
        mode = str(stage_config.get('contam_pass1', 'fixed')).lower()
        keys = {exposure_key(f) for g in groups for f in g}
        n_maps = sum(os.path.exists(TO.trace_map_path(os.path.join(workspace, MAPS_SUBDIR), f'{k}_rate.fits'))
                     for k in keys)
        prev = _pass1_valid(out, groups, n_maps, mode)
        if second:
            if prev is None or bool(prev['reliable']) or str(prev['round']) == '2':
                return out
        elif prev is not None and not overwrite:
            return out
        cfg = PC.ChainConfig.from_stage_config(stage_config)
        exps = PF.load_source(groups, bkg_overrides=overrides, science=set(science))
        maps = maps_for_exposures(workspace, exps)
        geo = geometry(exps, sid, maps)
        c_plan = PF.planned_position(exps)[0]
        saved = [e.ok.copy() for e in exps]
        before = sum(int((e.ok & main_shutter(e, c_plan)).sum()) for e in exps if e.use_data)
        if second:
            names = pass1_products or {}

            def reliable_for(csid):
                name = names.get(int(csid))
                path = pass1_path(workspace, name) if name else None
                if not path or not os.path.exists(path):
                    return None
                P1 = Pass1.read(path)
                return P1 if P1.reliable else None
            pred = predict(exps, sid, maps, reliable_for, workspace)
            apply(exps, pred, mode='hybrid', kappa=np.inf)           # subtract; mask only the unpredictable
        else:
            for e, m in zip(exps, geo.mask):
                e.ok = e.ok & ~m
        after = sum(int((e.ok & main_shutter(e, c_plan)).sum()) for e in exps if e.use_data)
        reliable = True
        if before == 0 or after < 0.3 * before:          # trace covered by others: solve without the mask
            for e, ok in zip(exps, saved):
                e.ok = ok.copy()
            reliable = False
        shape, shape_src = None, 'default'
        M = PF.ProfileModel(exps, chromatic=True, priors=cfg.priors, bounds=cfg.bounds)
        if not is_prism and prism_product:
            ps = read_prism_shape(workspace, prism_product)
            if ps is not None and ps.shape == 'own':
                shape, shape_src = PF.map_shape(ps.params, M.pivot), 'prism'
        R = PF.fit_fixed(model=M, shape=shape)
        snr, _ = PF.detection_snr(M)
        src = 'solve'
        if mode == 'stage3':
            spec = os.path.join(workspace, f'{product}_spec.fits')
            if os.path.exists(spec):
                from astropy.io import fits
                with fits.open(spec) as h:
                    d = h['SPEC1D'].data
                    lam, fnu, err = (np.asarray(d['wave'], float), np.asarray(d['fnu'], float),
                                     np.asarray(d['fnu_err'], float))
                msf = default_msf(R)
                ok = np.isfinite(lam) & np.isfinite(fnu)
                o = np.argsort(lam[ok])
                f = np.interp(R.lam, lam[ok][o], fnu[ok][o], left=np.nan, right=np.nan) / 1e12
                fe = np.interp(R.lam, lam[ok][o], np.nan_to_num(err[ok][o]), left=np.nan, right=np.nan) / 1e12
                R.flux = np.where(msf > 0, f / np.maximum(msf, 1e-6), np.nan)
                R.flux_err = np.where(msf > 0, fe / np.maximum(msf, 1e-6), np.nan)
                src = 'stage3'
                reliable = True       # stage-3 spectra are not decontaminated either way; taken as they are
            else:
                src = 'solve(no_stage3)'
        for e, ok in zip(exps, saved):
            e.ok = ok
        frames = {}
        for j, e in enumerate(exps):
            if not e.has_source:
                continue
            img = PF.frame_image(R, j)
            err = PF.frame_image(R, j, spectrum=np.abs(np.nan_to_num(R.flux_err)))
            good = np.isfinite(img) & (np.abs(img) > 0)
            if not good.any():
                continue
            cal = calibration(e, workspace)
            idx = np.flatnonzero(good)
            frames[exposure_key(e.name)] = dict(
                origin=np.array(e.origin), shape=np.array(e.y.shape), idx=idx.astype(np.int32),
                img=img.ravel()[idx].astype(np.float32), err=err.ravel()[idx].astype(np.float32),
                cal=cal.ravel()[idx].astype(np.float32))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        arrays = dict(version=PASS1_VERSION, n_maps=n_maps, mode=mode, sid=int(sid), lam=R.lam, flux=R.flux,
                      flux_err=R.flux_err, snr_det=snr, shape_src=shape_src, spec_src=src, reliable=reliable,
                      round='2' if second else '1', clean=after / before if before else 0.0,
                      keys=np.array(sorted(frames)))
        for key, fr in frames.items():
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

    @classmethod
    def read(cls, path):
        z = np.load(path, allow_pickle=False)
        frames = {}
        for key in z['keys']:
            key = str(key)
            frames[key] = {n: z[f'{key}__{n}'] for n in ('origin', 'shape', 'idx', 'img', 'err', 'cal')}
        return cls(sid=int(z['sid']), lam=z['lam'], flux=z['flux'], flux_err=z['flux_err'], frames=frames,
                   reliable=bool(z['reliable']) if 'reliable' in z else True)


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
    have_maps: bool = True


def _place_sparse(fr, origin_t, shape_t):
    """Target-frame (iy, ix) of a pass-1 frame's sparse pixels and a validity mask."""
    ny_c, nx_c = (int(x) for x in fr['shape'])
    iy, ix = np.divmod(fr['idx'].astype(np.int64), nx_c)
    ty = iy + int(fr['origin'][0]) - int(origin_t[0])
    tx = ix + int(fr['origin'][1]) - int(origin_t[1])
    ok = (ty >= 0) & (ty < shape_t[0]) & (tx >= 0) & (tx < shape_t[1])
    return ty, tx, ok


def predict(exps, sid, maps, pass1_for, rate_dir, own_pass1=None, pair_ratio=3.0):
    """Predicted other sources' light in each science nod difference of a target.

    pass1_for : callable sid -> ``Pass1`` or None."""
    geo = geometry(exps, sid, maps)
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

    cache, missing, contrib, unreliable = {}, set(), {}, set()
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
            for j, w in terms:
                fr = P1.frames.get(exposure_key(exps[j].name))
                if fr is None:
                    continue
                ty, tx, ok = _place_sparse(fr, e.origin, e.y.shape)
                ct = cal_of(j)[ty[ok], tx[ok]]
                with np.errstate(divide='ignore', invalid='ignore'):
                    conv = ct / fr['cal'][ok].astype(float)
                good = np.isfinite(conv)
                img = fr['img'][ok].astype(float)
                bad = ~good & (np.abs(img) > 0)
                unmod[k][ty[ok][bad], tx[ok][bad]] = True
                np.add.at(add, (ty[ok][good], tx[ok][good]), w * img[good] * conv[good])
                np.add.at(dadd, (ty[ok][good], tx[ok][good]), abs(w) * fr['err'][ok][good].astype(float) * conv[good])
            if not P1.reliable:
                pred_u[k] += add           # an upper bound, for masking only
                continue
            pred[k] += add
            dpred[k] += dadd
            with np.errstate(invalid='ignore', divide='ignore'):
                s = np.abs(add) / sig
            v = float(np.nanmax(np.where(ms, s, np.nan))) if ms.any() else 0.0
            contrib[csid] = max(contrib.get(csid, 0.0), v if np.isfinite(v) else 0.0)
    for k in range(n):
        dpred[k] = np.maximum(dpred[k], DPRED_FLOOR * np.abs(pred[k]))
    out = Prediction(pred=pred, dpred=dpred, unmodelled=unmod, contributors=contrib, missing=missing,
                     unreliable=unreliable, pred_unreliable=pred_u, have_maps=geo.have_maps)
    # similar-brightness pair: a contaminant above the noise on the target's main shutter
    # whose pass-1 flux is within a factor pair_ratio of the target's
    if own_pass1 is not None:
        best = None
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
                if best is None or abs(np.log(ratio)) < abs(np.log(best[1])):
                    best = (csid, ratio)
        if best is not None:
            out.pair, out.pair_id, out.pair_ratio = 1, int(best[0]), float(best[1])
    return out


def apply(exps, P, mode='hybrid', kappa=1.0):
    """Apply a prediction to the exposures before the shape fit (in place).

    Returns dict(overall, per_exposure, masked) with the fraction of the target's
    main-shutter pixels kept. ``mode``: mask | hybrid (see module doc)."""
    if mode not in ('mask', 'hybrid'):
        raise ValueError(f"contam_mode must be 'mask' or 'hybrid', got {mode!r}")
    c_plan = PF.planned_position(exps)[0]
    per, tot_b, tot_a = [], 0, 0
    for k, e in enumerate(exps):
        if not e.use_data or e.partners is None:
            continue
        ms = main_shutter(e, c_plan)
        b = int((e.ok & ms).sum())
        sig = diff_sigma(exps, k)
        drop = P.unmodelled[k].copy()
        with np.errstate(invalid='ignore'):
            if P.pred_unreliable is not None:
                drop |= np.abs(P.pred_unreliable[k]) > kappa * sig
            if mode == 'mask':
                drop |= np.abs(P.pred[k]) > kappa * sig
            else:
                e.diff = np.where(np.isfinite(P.pred[k]), e.diff - P.pred[k], e.diff)
                drop |= P.dpred[k] > kappa * sig
        e.ok = e.ok & ~drop
        a = int((e.ok & ms).sum())
        tot_b += b
        tot_a += a
        per.append(a / b if b else 1.0)
    overall = tot_a / tot_b if tot_b else 1.0
    return dict(overall=overall, per_exposure=per, masked=1.0 - overall)


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
