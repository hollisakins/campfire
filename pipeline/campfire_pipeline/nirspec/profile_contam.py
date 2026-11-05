"""
Other sources' light in a source's nod differences, predicted in one pass (prototype,
extraction v2).

Other slitlets of the MSA configuration disperse onto a source's trace
(``trace_overlap``). Their light is predicted from a first-pass spectrum of every
source and used to protect the source's shape fit:

The order of work, per disperser of an observation (``stage3.run_profile_fit_phase``):

1. **pass 1**: every source solved with its neighbours on its nod differences
   (``joint_first_pass``; on the override files' nod differences when the same-slitlet
   decisions are redone from it, then again on their own for the sources whose
   background exposures those decisions change);
2. **final**: Spec3, then every product's shape fit with the predicted light of the
   others subtracted or masked (``predict`` + ``apply``), then the 1-D extraction.

Nothing is iterated: each step reads only the output of the steps before it.

* **Trace maps** (phase 0): one per exposure, cached under ``{workspace}/tracemaps``.
* **Pass 1**: each exposure's model frame of the source (its light alone, path loss
  removed) and the pixel calibration of that exposure, sparse, in
  ``{workspace}/pass1/{obs}_{disperser}_{sid}_pass1.npz`` (``Pass1``, ``write_pass1``).
  A contaminant whose pass 1 is marked unreliable is never subtracted from a target;
  its prediction only masks the pixels where it exceeds the noise threshold (the
  joint first pass and the boxcar fallback write reliable ones only).
* **Prediction** for a target T: for each contaminant C named by the maps in T's
  nod difference k, C's frames ``j in {k} + partners(k)`` are moved onto T's pixels
  with weights +1 and -1/n (the target's own partner list, so any background
  override of either source is respected), converted from C's calibration to T's
  (pre-subtraction frame over rate, per pixel and per frame), and summed. Every
  pixel of C's frame image counts (an extended C spills into its other shutters),
  not only the source-shutter pixels the maps list. Every trace is followed past its
  nominal wavelength range, and every pass 1 with it (``profile_extend``, in
  count-rate units). Every source the maps name has a pass 1: one without is an
  error (``MissingContaminant``), not a pixel to mask. A source whose own pass 1
  hits a named fallback (no usable pixel, too few wavelengths) gets one from its
  stage-3 3-pixel boxcar placed with the default shape (``boxcar_pass1``), which
  subtracts like any reliable pass 1; only a source with no stage-3 s2d has none.
* **Use** (``contam_mode``): ``mask`` drops pixels where the predicted light exceeds
  ``kappa`` times the target's per-pixel noise; ``hybrid`` (default) subtracts the
  prediction everywhere and drops the pixels where the prediction's own uncertainty
  exceeds ``kappa`` times the noise. Failed-open shutters are recorded only (their sky
  cancels in a nod difference). This only changes the shape fit. The predicted light
  is drizzled like the data (``CONTAM2D``) and extracted with the same weights as
  ``fnu_prof`` (``fnu_contam``). Whether the best spectrum ``fnu`` has the prediction
  subtracted is the ``contam_1d`` knob (``stage3._profile_fit_columns``).
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
PASS1_VERSION = '11'
MIN_EXPOSURE_PIXELS = 100  # per-exposure clean fractions only for exposures with this many main-shutter pixels
DPRED_FLOOR = 0.1          # prediction uncertainty floor, fraction of the prediction (ASSUMED, to be measured)
_NOD = re.compile(r'^(jw\d+_[0-9a-z]{5})_(\d{5})_(nrs[12])')     # the sequence's activity is base 36 (0410f)


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
def stuck_by_root(table):
    """The observation's stuck-closed shutters (``Observation.stuck_closed_shutters``:
    root, source_id, shutters) as {root: {source id: [shutter numbers]}}."""
    out = {}
    if table is None:
        return out
    for row in table:
        out.setdefault(str(row['root']), {})[int(row['source_id'])] = [int(n) for n in row['shutters']]
    return out


def stuck_keys_by_root(obs):
    """The observation's stuck-closed physical shutters, {root: {(q, row, col)}}
    (``stuck_list``; what a map read from a rate file leaves out)."""
    from campfire_pipeline.nirspec import stuck_list
    resolved = stuck_list.load_obs(obs)
    return {root: stuck_list.stuck_set(resolved, root) for root in resolved}


def map_tasks(workspace, canonical_paths, log=None, stuck=None, stuck_keys=None):
    """One task per exposure of the canonicals: (rate or template rate, cache dir,
    full metafile, metadata id, dither position, exposure name, stuck-closed shutters).

    An exposure whose rate file holds the map master background step 1 stored there
    (``TO.ShutterMap``) takes it from there (``build_map`` with the root's stuck physical
    shutters, ``stuck_keys``: {root: {(q, row, col)}}, ``stuck_keys_by_root``), with no
    jwst run; its cached copy is redone when the rate file is newer.

    The full metafile (every slitlet of the configuration) is the one the rate
    file names; the canonicals name their per-source copy. An exposure without its
    own rate file uses a rate file of the same detector, disperser and filter as a
    template (``trace_overlap.full_mask_model``) and the only full metafile of its
    visit; when there is none, or several, it gets no map (its contaminants are
    then not predicted). ``stuck`` ({root: {source id: [shutter numbers]}},
    ``stuck_by_root``): those shutters are left out of the map of every exposure of
    the root (``jw07076009001_03101``); a map built with another list is rebuilt."""
    from astropy.io import fits

    log = log or (lambda *a: None)
    stuck = stuck or {}
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
        st = stuck.get(key[:19], {})
        stored = key in rates and TO.ShutterMap.present(rates[key])
        if TO.map_is_current(TO.trace_map_path(cache, name), stuck=st, rate_file=rates[key] if stored else None):
            continue
        h = fits.getheader(p)
        if stored:
            tasks.append((rates[key], cache, None, None, None, name, st, sorted((stuck_keys or {}).get(key[:19], ()))))
            continue
        if key in rates:
            det, grat, filt, msa_name, msa_id, patt = rate_meta[key]
            tasks.append((rates[key], cache, os.path.join(workspace, msa_name), int(msa_id), int(patt), name, st))
            continue
        want = (str(h.get('DETECTOR')).upper(), str(h.get('GRATING')).upper(), str(h.get('FILTER')).upper())
        rate = next((r for k, r in sorted(rates.items()) if rate_meta[k][:3] == want), None)
        visit = os.path.basename(p)[:13]
        msas = [f for f in full_msa if os.path.basename(f).startswith(visit)]
        if rate is None or len(msas) != 1:
            log(f'trace map {key}: no rate template or no unique full metafile ({len(msas)}); no map')
            continue
        tasks.append((rate, cache, msas[0], int(h['MSAMETID']), int(h['PATT_NUM']), name, st))
    return tasks


def build_map(rate, cache, msa, msa_id, patt, name, stuck=None, stuck_keys=None):
    """Write one exposure's map to the cache: the map master background step 1 stored in
    its rate file, with ``stuck_keys`` (the root's stuck physical shutters) removed, when
    ``stuck_keys`` is given; else built with jwst (``TO.get_trace_map``)."""
    from campfire_pipeline.common.io import log
    if stuck_keys is not None:
        try:
            tm = TO.trace_map_from_rate(rate, {tuple(int(v) for v in k) for k in stuck_keys})
            os.makedirs(cache, exist_ok=True)
            tm.meta.update(MAPVER=TO.MAP_VERSION, STUCK=TO.stuck_signature(stuck), MAPSRC='rate')
            tm.write(TO.trace_map_path(cache, name))
            return name
        except Exception as e:                   # noqa: BLE001 - as below
            log(f'trace map {name}: not read from the rate file ({type(e).__name__}: {e})')
            return None
    work = os.path.join(cache, f'work_{name[:-10]}')
    try:
        TO.get_trace_map(rate, cache, workdir=work, exposure_name=name,
                         msa_file=msa, msa_metadata_id=msa_id, dither_position=patt, stuck=stuck or {})
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
    (``sources``; past the contaminant's nominal wavelength range too, where its pass 1
    is extended), the exposures whose frames put that light there (``lit_from``:
    source id -> exposure indices, k itself or its partners), and pixels of failed-open
    shutters (``failed_open``: recorded only, their sky is the same in every nod and
    cancels in the difference; a sky object in one would be the only light left)."""
    mask: list
    sources: list
    have_maps: bool = True
    failed_open: list = None
    lit_from: list = None


def geometry(exps, sid, maps):
    """Other sources' pixels on a source's nod differences (see ``Geometry``)."""
    n = len(exps)
    out = Geometry(mask=[np.zeros(e.y.shape, bool) for e in exps], sources=[{} for _ in exps],
                   failed_open=[np.zeros(e.y.shape, bool) for e in exps], lit_from=[{} for _ in exps])
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
                out.mask[k][r.iy, r.ix] = True
                px = out.sources[k].setdefault(r.source_id, np.zeros(exps[k].y.shape, bool))
                px[r.iy, r.ix] = True
                out.lit_from[k].setdefault(r.source_id, set()).add(ks[r.from_exposure])
            else:
                out.failed_open[k][r.iy, r.ix] = True
    return out


def calibration(e, rate_dir):
    """Per-pixel calibration of exposure e on its common frame: the calibrated frame
    (path loss removed) over the rate, NaN where either is unusable or missing. The
    calibrated frame is ``PRE_BKGSUB_SCI`` once stage 2b has run, else the live SCI of
    a canonical stage 2b has not touched (the first pass on single frames)."""
    from astropy.io import fits

    cal = np.full(e.y.shape, np.nan)
    key = exposure_key(e.name)
    rate_path = os.path.join(rate_dir, f'{key}_rate.fits')
    if not os.path.exists(rate_path) or not e.path:
        return cal
    rate = fits.getdata(rate_path, 'SCI')
    with fits.open(e.path) as h:
        if 'PRE_BKGSUB_SCI' in h:
            pre = np.asarray(h['PRE_BKGSUB_SCI'].data, float)
        elif 'COMPLETE' not in str(h[0].header.get('S_BKDSUB', '')).upper():
            pre = np.asarray(h['SCI'].data, float)
        else:
            return cal
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
    if n == 0:                            # a single frame (no partners subtracted)
        return np.sqrt(e.var)
    s2 = (e.var + sum(exps[j].var for j in e.partners)) / (n + 1)
    return np.sqrt(((n + 1) / n) ** 2 * s2)


def main_shutter(e, c_plan):
    return np.isfinite(e.y) & (np.abs(e.y - c_plan) < PF.OPEN_HALF)


# ---------------------------------------------------------------------------
# Pass 1
# ---------------------------------------------------------------------------
def pass1_path(workspace, product):
    return os.path.join(workspace, PASS1_SUBDIR, f'{product}_pass1.npz')


PIXELS_PER_RESEL = 2.0     # NIRSpec resolving power is quoted for a 2-pixel resolution element


def smooth_points(resel):
    """Running-mean length in spectrum-grid points (native pixel sampling) for a length
    in resolution elements: odd, 0 for no smoothing."""
    n = int(round(float(resel) * PIXELS_PER_RESEL))
    return 0 if n <= 1 else n + (1 - n % 2)


def pass1_settings(stage_config):
    """The pass-1 knobs of ``[nirspec.stage3]`` (also the cache signature).

    Smoothing length per disperser, in resolution elements
    (``contam_pass1_smooth_prism`` / ``_grating``; a pass 1 from the 3-pixel boxcar,
    ``boxcar_pass1``); ``contam_pass1_smooth`` (grid points) overrides both when set;
    ``contam_pass1_lines`` keeps emission lines; the shape gates (the joint first
    pass's member shapes)."""
    g = stage_config.get
    legacy = g('contam_pass1_smooth', None)
    if legacy is not None:
        sm_p = sm_g = int(legacy)
    else:
        sm_p = smooth_points(g('contam_pass1_smooth_prism', 7.5))
        sm_g = smooth_points(g('contam_pass1_smooth_grating', 15.0))
    out = dict(smooth_prism=sm_p, smooth_grating=sm_g,
               lines=bool(g('contam_pass1_lines', True)),
               gate_prism=float(g('gate_snr_prism', 50.0)), gate_grating=float(g('gate_snr_grating', 70.0)),
               gate_noise=str(g('gate_noise', 'mean_square')).lower())
    bars = g('bar_offsets', None)
    if bars:
        # the measured bar offsets (``bar_offset.table``): a new measurement redoes pass 1
        out['bars'] = ';'.join(f'{d}:' + ','.join(f'{q}={v:.4f}' for q, v in sorted(qs.items()))
                               for d, qs in sorted(bars.items()))
    return out


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


def smooth_spectrum(flux, err, n=31, clip=5.0, n_iter=3, keep_lines=False, min_run=2):
    """Pass-1 spectrum for predicting light: outliers clipped, then a running mean.

    A point is clipped when it is more than ``clip`` sigma from the inverse-variance
    mean of the ``n`` points around it (itself and clipped points left out). The
    first reference is the running median, so one wild point does not drag its
    neighbours out with it; then up to ``n_iter`` rounds against the mean. The
    result is the inverse-variance running mean of the remaining points over ``n``
    grid points, its error the error of that mean. A full-resolution pass-1 solve
    is mostly noise for faint sources, and a few points of an ill-conditioned solve
    can be wildly off; the prediction only needs the continuum level.

    ``keep_lines``: a contaminant's emission line is real light that a running mean
    would smear out of the prediction. Clipped points that lie *above* the reference
    in a run of at least ``min_run`` adjacent grid points (a line is resolved over
    two or more samples; a wild point of the solve is usually alone) are put back at
    full resolution, with one neighbour on each side, with their own errors (in
    hybrid mode an uncertain line is then masked rather than subtracted).
    Returns (flux, err, clipped); ``clipped`` excludes the restored points."""
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
    clipped = ok & ~keep
    if keep_lines and clipped.any():
        with np.errstate(invalid='ignore'):
            up = clipped & (f - fs > clip * e)
        restore = np.zeros(f.size, bool)
        i = 0
        while i < f.size:
            if up[i]:
                j = i
                while j + 1 < f.size and up[j + 1]:
                    j += 1
                if j - i + 1 >= min_run:
                    restore[max(i - 1, 0):min(j + 2, f.size)] = True
                i = j + 1
            else:
                i += 1
        restore &= ok
        fs = np.where(restore, f, fs)
        es = np.where(restore, e, es)
        clipped &= ~restore
    return fs, es, clipped


def _frames(R, exps, sid, maps, workspace, n_smooth, keep_lines):
    """Each exposure's pass-1 frame of the spectrum ``R`` at its shape (sparse, with the
    exposure's pixel calibration), the frames past the cutouts (``profile_extend``) and
    the exposures without the source. Returns (frames, nosrc, ext)."""
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
        fr = dict(origin=np.array(e.origin), shape=np.array(e.y.shape), idx=idx.astype(np.int32),
                  img=img.ravel()[idx].astype(np.float32), err=err.ravel()[idx].astype(np.float32),
                  cal=cal.ravel()[idx].astype(np.float32))
        frames[exposure_key(e.name)] = fr
    ext = None
    if maps:
        # the light past both ends of the cutouts, in count-rate units (``profile_extend``): the
        # maps follow every trace there
        from campfire_pipeline.nirspec import profile_extend as PE
        ext = PE.solve_extension(R, sid, maps, workspace, n_smooth=n_smooth, keep_lines=keep_lines)
        if ext is not None and ext['frames']:
            frames = PE.merge_frames(frames, ext)
            nosrc = [k for k in nosrc if k not in ext['frames']]
    return frames, nosrc, ext


def boxcar_pass1(exps, sid, maps, workspace, is_prism, stage_config, box):
    """Pass 1 of a source whose own pass 1 hit a named fallback
    (``profile_fit.ProfileFitFallback``: no usable pixel, too few wavelengths): its
    stage-3 3-pixel boxcar (``box`` = (wave [um], fnu_3px, fnu_3px_err [uJy]), from
    ``stage3.spec3_boxcar3``), turned into a true spectrum for the default shape
    (``profile_fit.boxcar_factor``: the part of that shape's light the three rows hold)
    and placed on every exposure with that shape, so a target its trace crosses is
    predicted from it like from any other pass 1.

    Marked ``why = 'boxcar'`` and reliable: it subtracts. Its light was measured on the
    source's own trace, which the failed shape fit does not touch; it is not
    decontaminated, and it is taken as it is. Smoothed (``smooth_spectrum``,
    ``contam_pass1_smooth_prism`` / ``_grating``)."""
    from campfire_pipeline.nirspec import profile_chain as PC

    st = pass1_settings(stage_config)
    cfg = PC.ChainConfig.from_stage_config(stage_config)
    wave, fnu, err = (np.asarray(a, float) for a in box)
    ok = np.isfinite(wave) & (wave > 0)
    o = np.argsort(wave[ok])
    lam, fnu, err = wave[ok][o], fnu[ok][o], err[ok][o]
    keep = np.concatenate([[True], np.diff(lam) > 0])
    lam, fnu, err = lam[keep], fnu[keep], err[keep]
    if lam.size < 3:
        raise PF.TooFewWavelengths(f'source {sid}: boxcar pass 1: {lam.size} wavelength samples')
    M = PF.ProfileModel.for_placement(exps, lam, priors=cfg.priors, bounds=cfg.bounds)
    p = PF.shape_vector(None, M.c_prior, M.priors.width_median)           # the default shape
    R = PF.FitResult(params=dict(centre=float(p[0]), drift=float(p[1]), width=float(np.exp(p[2])),
                                 width_change=float(p[3]), pivot=M.pivot, centre_prior=M.c_prior),
                     eoff=np.zeros(len(exps)), lam=lam, flux=np.full(lam.size, np.nan),
                     flux_err=np.full(lam.size, np.nan), chi2=np.nan, npix=0, nfree=0, chromatic=True, model=M)
    R.bound_bits = 0
    fac = PF.boxcar_factor(R, lam)
    with np.errstate(invalid='ignore', divide='ignore'):
        good = np.isfinite(fac) & (fac > 0) & np.isfinite(fnu)
        R.flux = np.where(good, fnu / 1e12 / np.maximum(fac, 1e-6), np.nan)        # uJy -> MJy, true spectrum
        R.flux_err = np.where(good, err / 1e12 / np.maximum(fac, 1e-6), np.nan)
    raw, raw_err = R.flux.copy(), R.flux_err.copy()
    n_smooth = st['smooth_prism'] if is_prism else st['smooth_grating']
    n_lines = 0
    if n_smooth > 1:
        R.flux, R.flux_err, _ = smooth_spectrum(R.flux, R.flux_err, n=n_smooth, keep_lines=st['lines'])
        n_lines = int(np.sum(np.isfinite(raw) & (R.flux == raw)))
    rep = np.abs(R.flux * PF.reported_factor(R, R.lam)) * 1e12
    rep = rep[np.isfinite(rep)]
    frames, nosrc, ext = _frames(R, exps, sid, maps, workspace, n_smooth, st['lines'])
    P1 = Pass1(sid=int(sid), lam=R.lam, flux=R.flux, flux_err=R.flux_err, frames=frames, reliable=True,
               nosrc=set(nosrc))
    P1.ext = ext
    P1.n_smooth, P1.keep_lines, P1.clean = n_smooth, bool(st['lines']), np.nan
    P1.why, P1.shape_src, P1.spec_src, P1.snr_det = 'boxcar', 'default', 'boxcar', np.nan
    P1.flux_raw, P1.err_raw = raw, raw_err
    P1.rep_p95 = float(np.percentile(rep, 95)) if rep.size > 10 else np.nan
    P1.ref_p95, P1.n_lines = np.nan, n_lines
    P1.result = R
    return P1


def write_pass1(out, P1, sig, n_maps, mode):
    """Write a ``Pass1`` (with the attributes ``joint_first_pass.pass1_of`` and
    ``boxcar_pass1`` add) atomically."""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    arrays = dict(version=PASS1_VERSION, sig=sig, n_maps=n_maps, mode=mode, sid=int(P1.sid), lam=P1.lam,
                  flux=P1.flux, flux_err=P1.flux_err, flux_raw=P1.flux_raw, err_raw=P1.err_raw,
                  snr_det=P1.snr_det, shape_src=P1.shape_src, spec_src=P1.spec_src, reliable=P1.reliable,
                  why=P1.why, clean=P1.clean, rep_p95=P1.rep_p95,
                  ref_p95=P1.ref_p95, n_lines=P1.n_lines, n_smooth=P1.n_smooth, keep_lines=P1.keep_lines,
                  keys=np.array(sorted(P1.frames)), nosrc=np.array(sorted(P1.nosrc)))
    if getattr(P1, 'nosep', None) is not None:         # joint first pass: wavelengths not separable
        arrays['nosep'] = np.asarray(P1.nosep, bool)
    if getattr(P1, 'ext', None) is not None:          # the spectrum past the cutouts, count-rate units
        arrays.update(ext_lam=P1.ext['lam'], ext_flux=P1.ext['flux'], ext_err=P1.ext['err'],
                      ext_raw=P1.ext['flux_raw'])
    for key, fr in P1.frames.items():
        for name, v in fr.items():
            arrays[f'{key}__{name}'] = v
    tmp = out + '.tmp.npz'
    np.savez_compressed(tmp, **arrays)
    os.replace(tmp, out)


def missing_path(workspace, product):
    """Why a source has no pass 1 (written by ``joint_first_pass.run_target``, quoted by
    ``MissingContaminant``)."""
    return os.path.join(workspace, PASS1_SUBDIR, f'{product}_pass1_missing.txt')


def missing_reason(workspace, product):
    """The reason the first pass recorded for a source without a pass 1, or None."""
    path = missing_path(workspace, product) if product else None
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read().strip() or None


def _boxcar_stale(prev, out, workspace, product):
    """A pass 1 from the 3-pixel boxcar is redone when Spec3 rewrote the s2d it was taken
    from (or, without one, stage 3 the spectrum; ``stage3.spec3_boxcar3``)."""
    if 'why' not in prev or str(prev['why']) != 'boxcar':
        return False
    t = os.path.getmtime(out)
    srcs = [os.path.join(workspace, f'{product}{s}') for s in ('_s2d.fits', '_x1d.fits')]
    if not all(os.path.exists(p) for p in srcs):
        srcs = [os.path.join(workspace, f'{product}_spec.fits')]
    return any(os.path.exists(p) and os.path.getmtime(p) > t for p in srcs)


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
            if f'{key}__ext' in z:
                frames[key]['ext'] = z[f'{key}__ext']
        P = cls(sid=int(z['sid']), lam=z['lam'], flux=z['flux'], flux_err=z['flux_err'], frames=frames,
                reliable=bool(z['reliable']) if 'reliable' in z else True,
                nosrc={str(x) for x in z['nosrc']} if 'nosrc' in z else None)
        P.why = str(z['why']) if 'why' in z else ''
        P.shape_src = str(z['shape_src']) if 'shape_src' in z else ''
        P.flux_raw = z['flux_raw'] if 'flux_raw' in z else None
        P.err_raw = z['err_raw'] if 'err_raw' in z else None
        P.rep_p95 = float(z['rep_p95']) if 'rep_p95' in z else np.nan
        P.ref_p95 = float(z['ref_p95']) if 'ref_p95' in z else np.nan
        P.n_smooth = int(z['n_smooth']) if 'n_smooth' in z else 0
        P.keep_lines = bool(z['keep_lines']) if 'keep_lines' in z else True
        P.nosep = z['nosep'] if 'nosep' in z else None
        P.ext = (dict(lam=z['ext_lam'], flux=z['ext_flux'], err=z['ext_err'], flux_raw=z['ext_raw'])
                 if 'ext_lam' in z else None)
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
class MissingContaminant(RuntimeError):
    """The trace maps put another source's light on a target, and that source has no
    pass 1 (or no pass-1 frame of an exposure its light comes from). Every source of a
    configuration gets a pass 1 (``stage3.run_profile_fit_phase``; from its 3-pixel
    boxcar when its own fit falls back), so this is a bug or a broken run (a source
    with no stage-3 s2d, e.g. every nod excluded, a source the maps name that stage 2
    did not reduce), never a condition to extract around."""


@dataclass
class Prediction:
    pred: list                       # per exposure: predicted other-source light in the nod difference (diff units)
    dpred: list                      # its uncertainty
    contributors: dict = field(default_factory=dict)   # sid -> max |pred| / sigma on the target's main shutter
    unreliable: set = field(default_factory=set)       # contaminants whose pass 1 may hold the target's light
    pred_unreliable: list = None     # per exposure: their predicted light (masking only, never subtracted)
    pair: int = 0
    pair_id: int = -1
    pair_ratio: float = np.nan
    pair_frac: float = np.nan        # the pair contaminant's light in the target's extraction / the target's
    have_maps: bool = True
    by_source: dict = field(default_factory=dict, repr=False)   # sid -> per exposure predicted light (pair flag)
    pair_candidates: list = field(default_factory=list)       # [(sid, pass-1 flux ratio)] within the ratio window
    failed_open: list = None         # per exposure: pixels of failed-open shutters (recorded only)
    dpred_joint: list = None         # per exposure: statistical uncertainty of the jointly solved light (report only)
    pred_joint: list = None          # per exposure: the jointly solved part of ``pred``
    joint: set = field(default_factory=set)             # contaminants whose light comes from the joint solve
    excluded: set = field(default_factory=set)          # contaminants left out of pred (modelled elsewhere)
    boxcar: set = field(default_factory=set)            # contaminants predicted from their 3-pixel boxcar


def _place_sparse(fr, origin_t, shape_t):
    """Target-frame (iy, ix) of a pass-1 frame's sparse pixels and a validity mask."""
    ny_c, nx_c = (int(x) for x in fr['shape'])
    iy, ix = np.divmod(fr['idx'].astype(np.int64), nx_c)
    ty = iy + int(fr['origin'][0]) - int(origin_t[0])
    tx = ix + int(fr['origin'][1]) - int(origin_t[1])
    ok = (ty >= 0) & (ty < shape_t[0]) & (tx >= 0) & (tx < shape_t[1])
    return ty, tx, ok


def predict(exps, sid, maps, pass1_for, rate_dir, own_pass1=None, pair_ratio=3.0, exclude=(),
            why_missing=None, include=()):
    """Predicted other sources' light in each science nod difference of a target.

    Every source the trace maps put on the target's trace is predicted from its pass
    1, past its nominal wavelength range too (pass 1 is extended there in count-rate
    units, ``profile_extend``). A frame pixel is converted with the target's calibration
    over the contaminant's (both per unit count rate); where either has none the pixel
    gets no light. In the stored pass-1 frames that is a detector pixel flagged
    DO_NOT_USE in the rate file, which the target does not fit either, or one of the
    last ~10 columns of a trace, where the contaminant's flats stop (0.1 % of a
    contaminant's light in G395M, 0.3 % in PRISM; ember_cosmos_p14 pass 1, 2026-10-29).

    pass1_for : callable sid -> ``Pass1``. A ``Pass1`` with ``joint = True`` holds a
        jointly solved spectrum: its light is subtracted and never masked (its
        statistical uncertainty goes to ``dpred_joint``, for reporting only, without
        the ``DPRED_FLOOR``).
    own_pass1 : the target's own pass 1 (pair candidates).
    exclude : contaminants whose light is modelled elsewhere, left out entirely, their
        light past their cutouts too (the members of the target's joint first pass,
        whose light on it comes from that solve, ``joint_first_pass.add_joint_light``).
    why_missing : callable sid -> why that source has no pass 1 (``missing_reason``), for
        the error message.
    include : contaminants added to every exposure whatever the maps say: catalogued
        neighbors in the target's own slitlet, which the maps count as the slitlet's
        own light (``TraceMap.rows_of``). Placed from their pass-1 frames where they
        have one; one without a pass 1 (not reduced in this disperser) is skipped.
        Used on single frames (the master-background spectrum, ``mbkg_extraction``).

    A pass 1 taken from the contaminant's 3-pixel boxcar (``boxcar_pass1``) is used like
    any reliable one; such contaminants are listed in ``Prediction.boxcar``.

    Raises ``MissingContaminant`` for a contaminant (not in ``exclude``) without a
    pass 1, or without the pass-1 frame of an exposure whose light the maps put on the
    target (a frame the source is not in, ``Pass1.nosrc``, holds no light)."""
    geo = geometry(exps, sid, maps)
    n = len(exps)
    pred = [np.zeros(e.y.shape) for e in exps]
    dpred = [np.zeros(e.y.shape) for e in exps]
    pred_u = [np.zeros(e.y.shape) for e in exps]
    exclude = {int(x) for x in exclude}
    include = {int(x) for x in include} - {int(sid)} - exclude
    c_plan = PF.planned_position(exps)[0]
    cals = {}

    def cal_of(j):
        if j not in cals:
            cals[j] = calibration(exps[j], rate_dir)
        return cals[j]

    cache, contrib, unreliable, by_source = {}, {}, set(), {}
    boxcar = set()
    for k in range(n):
        e = exps[k]
        if not e.use_data or e.partners is None or not (geo.sources[k] or include):
            continue
        sig = diff_sigma(exps, k)
        ms = main_shutter(e, c_plan) & e.ok
        terms = [(k, 1.0)] + [(j, -1.0 / len(e.partners)) for j in e.partners]
        for csid in sorted(set(geo.sources[k]) | include):
            if int(csid) in exclude:
                continue
            if csid not in cache:
                cache[csid] = pass1_for(csid)
            P1 = cache[csid]
            if P1 is None and int(csid) in include and csid not in geo.sources[k]:
                continue
            if P1 is None:
                why = why_missing(csid) if why_missing is not None else None
                raise MissingContaminant(f'source {sid}: the trace maps put source {csid} on its trace in '
                                         f'{exposure_key(e.name)}, and source {csid} has no pass 1'
                                         + (f' ({why})' if why else ''))
            if not P1.reliable:            # its pass 1 may carry the target's light: do not hand it back
                unreliable.add(csid)
            if getattr(P1, 'why', '') == 'boxcar':
                boxcar.add(csid)
            lit = geo.lit_from[k].get(csid, set()) if geo.lit_from is not None else set()
            add = np.zeros(e.y.shape)
            dadd = np.zeros(e.y.shape)
            for j, w in terms:
                key = exposure_key(exps[j].name)
                fr = P1.frames.get(key)
                if fr is None:
                    if j in lit and P1.nosrc is not None and key not in P1.nosrc:
                        raise MissingContaminant(f'source {sid}: the trace maps put the light of source {csid} '
                                                 f'in {key} on its trace, and its pass 1 has no frame there')
                    continue
                ty, tx, ok = _place_sparse(fr, e.origin, e.y.shape)
                with np.errstate(divide='ignore', invalid='ignore'):
                    conv = cal_of(j)[ty[ok], tx[ok]] / fr['cal'][ok].astype(float)
                good = np.isfinite(conv)
                img = fr['img'][ok].astype(float)
                np.add.at(add, (ty[ok][good], tx[ok][good]), w * img[good] * conv[good])
                np.add.at(dadd, (ty[ok][good], tx[ok][good]), abs(w) * fr['err'][ok][good].astype(float) * conv[good])
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
    # jointly solved light (``joint_first_pass.add_joint_light``) is added to pred_joint later
    out = Prediction(pred=pred, dpred=dpred, contributors=contrib, unreliable=unreliable, pred_unreliable=pred_u,
                     have_maps=geo.have_maps, by_source=by_source, failed_open=geo.failed_open,
                     dpred_joint=[np.zeros(e.y.shape) for e in exps], excluded=exclude,
                     pred_joint=[np.zeros(e.y.shape) for e in exps], boxcar=boxcar)
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
    fraction of main-shutter pixels where the predicted other-source light stays below
    ``dominance_kappa`` times the noise, whatever the mode did with it (in hybrid mode a well-predicted contaminant is subtracted,
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
        drop = np.zeros(e.y.shape, bool)
        # jointly solved light (``joint_first_pass.add_joint_light``) is subtracted in both modes, never masked
        pj = P.pred_joint[k] if getattr(P, 'pred_joint', None) is not None else 0.0
        with np.errstate(invalid='ignore'):
            tot = np.abs(P.pred[k] + (P.pred_unreliable[k] if P.pred_unreliable is not None else 0.0))
            dirty = tot > dominance_kappa * sig
            if P.pred_unreliable is not None:
                drop |= np.abs(P.pred_unreliable[k]) > kappa * sig
            if mode == 'mask':
                e.diff = np.where(np.isfinite(pj), e.diff - pj, e.diff)
                drop |= np.abs(P.pred[k] - pj) > kappa * sig
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
        if b >= MIN_EXPOSURE_PIXELS:         # a sliver of trace on the other detector says nothing
            per.append(a / b)
            per_c.append(c / b)
    overall = tot_a / tot_b if tot_b else 1.0
    return dict(overall=overall, per_exposure=per, masked=1.0 - overall,
                clean_pred=tot_c / tot_b if tot_b else 1.0, clean_pred_exposure=per_c,
                failed_open=tot_fo / tot_b if tot_b else 0.0)


def live_frames(exps, P, what='pred'):
    """Predicted light per science exposure in the live canonical units (path loss
    re-applied) on its cutout, keyed by file name, for drizzling (``CONTAM2D``).
    ``what = 'dpred'``: its uncertainty (``CONTERR2D``; for jointly solved light only
    the systematic part: its statistical uncertainty is ``dpred_joint``, reported only)."""
    out = {}
    for k, e in enumerate(exps):
        if not e.use_data or e.partners is None:
            continue
        r0, c0, ny, nx = e.cutout
        v = P.pred[k]
        if what == 'dpred':
            v = P.dpred[k]
            if getattr(P, 'pred_joint', None) is not None:
                v = v + DPRED_FLOOR * np.abs(P.pred_joint[k])
        with np.errstate(invalid='ignore', divide='ignore'):
            a = v / e.pl_applied
        out[e.name] = np.nan_to_num(a[r0:r0 + ny, c0:c0 + nx])
    return out
