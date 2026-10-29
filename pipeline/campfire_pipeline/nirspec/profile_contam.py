"""
Other sources' light in a source's nod differences, predicted in one pass (prototype,
extraction v2).

Other slitlets of the MSA configuration disperse onto a source's trace
(``trace_overlap``). Their light is predicted from a quick first-pass spectrum of
every source and used to protect the source's shape fit:

The order of work, per disperser of an observation (``stage3.run_profile_fit_phase``):

1. **pass 1**: a quick spectrum of every source, its own trace's overlaps masked;
2. **pass 1b**: only for the sources whose trace others cover (pass 1 marked them
   unreliable): solved once more after subtracting the light of the contaminants
   whose pass 1 is reliable. Pass 1b never feeds another pass 1b;
3. **final**: every product's shape fit with the predicted light of the others
   subtracted or masked (``predict`` + ``apply``), then the 1-D extraction.

Nothing is iterated: each step reads only the output of the steps before it.

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
  hold others' light) only masks the pixels where it exceeds the noise threshold. Its
  spectrum was solved over the target's pixels too, so the target's own light (its
  lines included) is taken out of it first (``echo_corrected``), or the target would
  mask its own emission. Alternative
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
PASS1_VERSION = '10'
MIN_EXPOSURE_PIXELS = 100  # per-exposure clean fractions only for exposures with this many main-shutter pixels
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
def stuck_by_root(table):
    """The observation's stuck-closed shutters (``Observation.stuck_closed_shutters``:
    root, source_id, shutters) as {root: {source id: [shutter numbers]}}."""
    out = {}
    if table is None:
        return out
    for row in table:
        out.setdefault(str(row['root']), {})[int(row['source_id'])] = [int(n) for n in row['shutters']]
    return out


def map_tasks(workspace, canonical_paths, log=None, stuck=None):
    """One task per exposure of the canonicals: (rate or template rate, cache dir,
    full metafile, metadata id, dither position, exposure name, stuck-closed shutters).

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
        if TO.map_is_current(TO.trace_map_path(cache, name), stuck=st):
            continue
        h = fits.getheader(p)
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


def build_map(rate, cache, msa, msa_id, patt, name, stuck=None):
    from campfire_pipeline.common.io import log
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


PIXELS_PER_RESEL = 2.0     # NIRSpec resolving power is quoted for a 2-pixel resolution element
GUARD_REFS = ('stage3', 'boxcar_skip', 'box3', 'max')


def smooth_points(resel):
    """Running-mean length in spectrum-grid points (native pixel sampling) for a length
    in resolution elements: odd, 0 for no smoothing."""
    n = int(round(float(resel) * PIXELS_PER_RESEL))
    return 0 if n <= 1 else n + (1 - n % 2)


def pass1_settings(stage_config):
    """The pass-1 knobs of ``[nirspec.stage3]`` (also the cache signature).

    Smoothing length per disperser, in resolution elements
    (``contam_pass1_smooth_prism`` / ``_grating``); ``contam_pass1_smooth`` (grid points)
    overrides both when set (the round-2 knob)."""
    g = stage_config.get
    legacy = g('contam_pass1_smooth', None)
    if legacy is not None:
        sm_p = sm_g = int(legacy)
    else:
        sm_p = smooth_points(g('contam_pass1_smooth_prism', 7.5))
        sm_g = smooth_points(g('contam_pass1_smooth_grating', 15.0))
    ref = str(g('contam_pass1_guard_ref', 'max')).lower()
    if ref not in GUARD_REFS:
        raise ValueError(f'contam_pass1_guard_ref must be one of {GUARD_REFS}, got {ref!r}')
    return dict(mode=str(g('contam_pass1', 'fixed')).lower(),
                smooth_prism=sm_p, smooth_grating=sm_g,
                lines=bool(g('contam_pass1_lines', True)),
                guard=float(g('contam_pass1_guard', 3.0)), guard_ref=ref,
                shape=str(g('contam_pass1_shape', 'chain')).lower(),
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


def _p95(f):
    f = np.asarray(f, float)
    f = f[np.isfinite(f)]
    return float(np.percentile(np.abs(f), 95)) if f.size > 10 else None


def stage3_p95(workspace, product, ref='stage3'):
    """95th percentile of |flux| [uJy] of the source's standard stage-3 spectrum, the
    pass-1 guard's reference, or None.

    ref : 'stage3' the optimal extraction ``fnu_opt`` (``fnu`` in products written before
        extraction v2; ``_spec.fits``, else Spec3's ``_x1d.fits``); when the optimal
        extraction fell back to a boxcar (``CMPFROPT``) that spectrum holds a fraction of the flux and a correct pass 1
        reads as several times brighter;
      'boxcar_skip' as 'stage3', but None (no guard) for a boxcar fallback;
      'box3' the 3-pixel boxcar ``fnu_3px`` (same aperture for every product);
      'max' the larger of the two (default)."""
    from astropy.io import fits
    spec = os.path.join(workspace, f'{product}_spec.fits')
    x1d = os.path.join(workspace, f'{product}_x1d.fits')
    try:
        if os.path.exists(spec):
            with fits.open(spec) as h:
                d = h['SPEC1D'].data
                # fnu_opt: the optimal extraction (fnu is the best spectrum since extraction v2)
                opt = _p95(d['fnu_opt'] if 'fnu_opt' in d.columns.names else d['fnu'])
                box = _p95(d['fnu_3px']) if 'fnu_3px' in d.columns.names else None
                boxcar = 'box' in str(h[0].header.get('CMPFROPT', '')).lower()
        elif os.path.exists(x1d):
            opt = _p95(np.asarray(fits.getdata(x1d, 'EXTRACT1D')['FLUX'], float) * 1e6)      # Jy -> uJy
            box, boxcar = None, False
        else:
            return None
    except (KeyError, OSError, ValueError):
        return None
    if ref == 'stage3':
        return opt
    if ref == 'boxcar_skip':
        return None if boxcar else opt
    if ref == 'box3':
        return box if box is not None else opt
    vals = [v for v in (opt, box) if v is not None]
    return max(vals) if vals else None


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
    (``contam_pass1_smooth_prism`` / ``_grating`` resolution elements; coherent
    emission lines kept with ``contam_pass1_lines``). Sanity guard: a spectrum
    whose 95th percentile of |flux| (as reported, uJy) exceeds ``contam_pass1_guard``
    times that of the source's stage-3 spectrum (``stage3_ref``, chosen by
    ``contam_pass1_guard_ref``, see ``stage3_p95``) is marked unreliable; it then
    only masks. In the population the guard fires on sources whose trace others
    cover (pass 1b): their pass 1 still holds part of the others' light.

    Returns a ``Pass1`` with extra attributes (clean, why, shape_src, snr_det,
    flux_raw, err_raw)."""
    from campfire_pipeline.nirspec import profile_chain as PC

    st = pass1_settings(stage_config)
    cfg = PC.ChainConfig.from_stage_config(stage_config)
    geo = geometry(exps, sid, maps)
    c_plan = PF.planned_position(exps)[0]
    saved = [e.ok.copy() for e in exps]
    saved_diff = [e.diff for e in exps]
    before = sum(int((e.ok & main_shutter(e, c_plan)).sum()) for e in exps if e.use_data)
    if second:
        # the contaminants with a reliable first-round pass 1 are subtracted; the others'
        # pixels are masked (``pass1_for`` gives None for them)
        others = sorted({c for s in geo.sources for c in s})
        got = {c: pass1_for(c) for c in others}
        skip = {c for c, P in got.items() if P is None}
        pred = predict(exps, sid, maps, lambda c: got.get(int(c)), workspace, exclude=skip)
        apply(exps, pred, mode='hybrid', kappa=np.inf)           # subtract, mask nothing
        for k, e in enumerate(exps):
            for c in skip & set(geo.sources[k]):
                e.ok = e.ok & ~geo.sources[k][c]
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
    solved_unmasked = why == 'covered'          # others' pixels are in the solve (``echo_corrected``)
    nband = normal_band(R) if solved_unmasked else None
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
    n_smooth = st['smooth_prism'] if is_prism else st['smooth_grating']
    n_lines = 0
    if src != 'stage3' and n_smooth > 1:
        R.flux, R.flux_err, clipped = smooth_spectrum(R.flux, R.flux_err, n=n_smooth, keep_lines=st['lines'])
        n_lines = int(np.sum(np.isfinite(raw) & (R.flux == raw)))
    rep = np.abs(R.flux * PF.main_shutter_factor(R, R.lam)) * 1e12
    rep = rep[np.isfinite(rep)]
    rep_p95 = float(np.percentile(rep, 95)) if rep.size > 10 else np.nan
    if src != 'stage3' and st['guard'] > 0 and stage3_ref:
        if np.isfinite(rep_p95) and rep_p95 > st['guard'] * stage3_ref:
            reliable, why = False, 'guard'
    for e, ok, d in zip(exps, saved, saved_diff):
        e.ok, e.diff = ok, d
    frames, nosrc, ext = _frames(R, exps, sid, maps, workspace, n_smooth, st['lines'],
                                 design=solved_unmasked and src == 'solve')
    P1 = Pass1(sid=int(sid), lam=R.lam, flux=R.flux, flux_err=R.flux_err, frames=frames, reliable=reliable,
               nosrc=set(nosrc))
    P1.ext = ext
    P1.n_smooth, P1.keep_lines = (n_smooth if src != 'stage3' else 0), bool(st['lines'])
    P1.nband = nband if src == 'solve' else None
    P1.clean = after / before if before else 0.0
    P1.why, P1.shape_src, P1.spec_src, P1.snr_det = why, shape_src, src, snr
    P1.flux_raw, P1.err_raw = raw, raw_err
    P1.rep_p95, P1.ref_p95, P1.n_lines = rep_p95, (stage3_ref if stage3_ref else np.nan), n_lines
    P1.result = R
    return P1


def _frames(R, exps, sid, maps, workspace, n_smooth, keep_lines, design=False):
    """Each exposure's pass-1 frame of the spectrum ``R`` at its shape (sparse, with the
    exposure's pixel calibration), the frames past the cutouts (``profile_extend``) and
    the exposures without the source. ``design``: also store the frame per unit spectrum
    and each pixel's wavelength (``echo_corrected``). Returns (frames, nosrc, ext)."""
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
        if design:
            # the frame per unit spectrum and each pixel's wavelength: the solve's design,
            # for removing a target's own light from this spectrum (``echo_corrected``)
            prof = PF.frame_image(R, j, spectrum=np.ones(R.lam.size))
            fr.update(prof=prof.ravel()[idx].astype(np.float32), plam=e.wav.ravel()[idx].astype(np.float32))
        frames[exposure_key(e.name)] = fr
    ext = None
    if maps:
        # the light past the cutouts' red end, in count-rate units (``profile_extend``): the
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
    source's own trace, which the failed shape fit does not touch; like a stage-3 pass 1
    (``contam_pass1 = 'stage3'``) it is not decontaminated, and like one it is taken as
    it is. Smoothed as a solved pass 1 is (``smooth_spectrum``), no sanity guard (its
    reference would be the boxcar itself)."""
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
    rep = np.abs(R.flux * PF.main_shutter_factor(R, R.lam)) * 1e12
    rep = rep[np.isfinite(rep)]
    frames, nosrc, ext = _frames(R, exps, sid, maps, workspace, n_smooth, st['lines'])
    P1 = Pass1(sid=int(sid), lam=R.lam, flux=R.flux, flux_err=R.flux_err, frames=frames, reliable=True,
               nosrc=set(nosrc))
    P1.ext = ext
    P1.n_smooth, P1.keep_lines, P1.nband, P1.clean = n_smooth, bool(st['lines']), None, np.nan
    P1.why, P1.shape_src, P1.spec_src, P1.snr_det = 'boxcar', 'default', 'boxcar', np.nan
    P1.flux_raw, P1.err_raw = raw, raw_err
    P1.rep_p95 = float(np.percentile(rep, 95)) if rep.size > 10 else np.nan
    P1.ref_p95, P1.n_lines = np.nan, n_lines
    P1.result = R
    return P1


def first_round_reliable(workspace, name):
    """The pass 1 a second round may subtract: a reliable first-round result, else None.

    The second rounds run in parallel and rewrite their sources' files; one already
    rewritten (71 of the p14 G395M sources come out of pass 1b reliable) must not feed
    another, or pass 1b would feed pass 1b, depending on the order the workers finish
    in."""
    path = pass1_path(workspace, name) if name else None
    if not path or not os.path.exists(path):
        return None
    P1 = Pass1.read(path)
    return P1 if P1.reliable and getattr(P1, 'round', '1') == '1' else None


def run_pass1(product, workspace, groups, overrides, science, sid, is_prism, stage_config,
              prism_product=None, overwrite=False, pass1_products=None, second=False):
    """Pass-1 spectrum and per-exposure model frames of one source (see module doc and
    ``compute_pass1``). Returns the output path. On a named condition
    (``profile_fit.ProfileFitFallback``) in the first round the pass 1 is taken from
    the source's stage-3 3-pixel boxcar (``boxcar_pass1``); without one (no s2d, or
    the exposures cannot be loaded) it returns None, logged, with the reason in
    ``missing_path`` for ``MissingContaminant``. Any other exception propagates.

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
        prism = None
        if not is_prism and prism_product:
            prism = read_prism_shape(workspace, prism_product)
        # the PRISM shape a grating pass 1 is predicted with is part of its identity: a
        # PRISM rerun that changes it invalidates the grating pass 1
        pshape = 'none' if prism is None else (
            f"{prism.shape}:" + ','.join(f'{k}={prism.params[k]:.4f}' for k in sorted(prism.params)))
        sig = _signature(dict(st, prism_shape=pshape))
        keys = {exposure_key(f) for g in groups for f in g}
        mpaths = [TO.trace_map_path(os.path.join(workspace, MAPS_SUBDIR), f'{k}_rate.fits') for k in sorted(keys)]
        mpaths = [p for p in mpaths if os.path.exists(p)]
        # a rebuilt map (a new stuck-closed list) redoes the pass 1 that masked with it
        prev = _pass1_valid(out, list(groups) + [mpaths], len(mpaths), sig)
        n_maps = len(mpaths)
        if second:
            if prev is None or bool(prev['reliable']) or str(prev['round']) == '2':
                return out
        elif prev is not None and not overwrite and not _boxcar_stale(prev, out, workspace, product):
            return out
        exps = PF.load_source(groups, bkg_overrides=overrides, science=set(science))
        maps = maps_for_exposures(workspace, exps)
        names = pass1_products or {}

        def reliable_for(csid):
            return first_round_reliable(workspace, names.get(int(csid)))
        oks = [e.ok.copy() for e in exps]
        spec3 = None
        if st['mode'] == 'stage3':
            spec = os.path.join(workspace, f'{product}_spec.fits')
            if os.path.exists(spec):
                from astropy.io import fits
                with fits.open(spec) as h:
                    d = h['SPEC1D'].data
                    sfx = '_opt' if 'fnu_opt' in d.columns.names else ''     # the optimal extraction
                    spec3 = (np.asarray(d['wave'], float), np.asarray(d[f'fnu{sfx}'], float),
                             np.asarray(d[f'fnu{sfx}_err'], float))
        try:
            P1 = compute_pass1(exps, sid, maps, workspace, is_prism, stage_config, prism_shape=prism,
                               second=second, pass1_for=reliable_for,
                               stage3_ref=stage3_p95(workspace, product, st['guard_ref']), stage3_spec=spec3)
        except PF.ProfileFitFallback as e:
            if second:                   # the first-round result stays
                raise
            # the source's own fit cannot run, but its light still reaches the targets its
            # trace crosses: predict it from stage 3's 3-pixel boxcar (``boxcar_pass1``)
            from campfire_pipeline.nirspec.stage3 import spec3_boxcar3
            box = spec3_boxcar3(workspace, product)
            if box is None:
                raise type(e)(f'{e}; no stage-3 s2d or spectrum to take its 3-pixel boxcar from '
                              f'(no science exposure reached stage 3)') from e
            for x, ok in zip(exps, oks):         # compute_pass1 stopped with others' pixels masked
                x.ok = ok
            P1 = boxcar_pass1(exps, sid, maps, workspace, is_prism, stage_config, box[:3])
            log(f'{product}: pass 1 from the stage-3 3-pixel boxcar ({box[3]}), its own fell back '
                f'({e.reason}: {e})')
        os.makedirs(os.path.dirname(out), exist_ok=True)
        arrays = dict(version=PASS1_VERSION, sig=sig, n_maps=n_maps, mode=st['mode'], sid=int(sid), lam=P1.lam,
                      flux=P1.flux, flux_err=P1.flux_err, flux_raw=P1.flux_raw, err_raw=P1.err_raw,
                      snr_det=P1.snr_det, shape_src=P1.shape_src, spec_src=P1.spec_src, reliable=P1.reliable,
                      why=P1.why, round='2' if second else '1', clean=P1.clean, rep_p95=P1.rep_p95,
                      ref_p95=P1.ref_p95, n_lines=P1.n_lines, n_smooth=P1.n_smooth, keep_lines=P1.keep_lines,
                      keys=np.array(sorted(P1.frames)), nosrc=np.array(sorted(P1.nosrc)))
        if getattr(P1, 'nband', None) is not None:
            arrays['nband'] = P1.nband
        if getattr(P1, 'ext', None) is not None:          # the spectrum past the cutouts, count-rate units
            arrays.update(ext_lam=P1.ext['lam'], ext_flux=P1.ext['flux'], ext_err=P1.ext['err'],
                          ext_raw=P1.ext['flux_raw'])
        for key, fr in P1.frames.items():
            for name, v in fr.items():
                arrays[f'{key}__{name}'] = v
        tmp = out + '.tmp.npz'
        np.savez_compressed(tmp, **arrays)
        os.replace(tmp, out)
        if os.path.exists(missing_path(workspace, product)):
            os.remove(missing_path(workspace, product))
        return out
    except PF.ProfileFitFallback as e:
        # a named condition and no boxcar either: the source has no pass 1, and a target
        # its trace covers fails on it (``MissingContaminant``, which quotes this reason)
        log(f'{product}: no pass 1 ({e.reason}: {e})')
        if not second:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(missing_path(workspace, product), 'w') as f:
                f.write(f'{e.reason}: {e}\n')
        return None


def missing_path(workspace, product):
    """Why a source has no pass 1 (written by ``run_pass1``, quoted by ``MissingContaminant``)."""
    return os.path.join(workspace, PASS1_SUBDIR, f'{product}_pass1_missing.txt')


def missing_reason(workspace, product):
    """The reason ``run_pass1`` recorded for a source without a pass 1, or None."""
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
            for n in ('prof', 'plam', 'ext'):
                if f'{key}__{n}' in z:
                    frames[key][n] = z[f'{key}__{n}']
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
        P.round = str(z['round']) if 'round' in z else '1'
        P.nband = z['nband'] if 'nband' in z else None
        P.ext = (dict(lam=z['ext_lam'], flux=z['ext_flux'], err=z['ext_err'], flux_raw=z['ext_raw'])
                 if 'ext_lam' in z else None)
        return P

    def has_design(self):
        """The frames carry the solve's design (``prof``, ``plam``): the source was solved
        over other sources' pixels and its spectrum can be corrected for a target's echo."""
        return bool(self.frames) and all('prof' in fr and 'plam' in fr for fr in self.frames.values()) \
            and getattr(self, 'flux_raw', None) is not None and getattr(self, 'err_raw', None) is not None


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
    echoed: set = field(default_factory=set)            # unreliable contaminants with the target's echo removed
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


NBAND = 3      # stored diagonals of a pass-1 solve's normal matrix (main + 2 above; each pixel touches 2 grid points)


def normal_band(R):
    """The upper band of the normal matrix A^T W A of a fixed-shape solve (its shape,
    offsets and clipped pixels), shape (NBAND, n grid): row d holds diagonal d,
    left-aligned. ``echo_corrected`` solves with it."""
    M = R.model
    p, eoff = PF._qvec(R)
    saved = M.use
    try:
        if getattr(R, 'use', None) is not None:
            M.use = R.use
        A = M.design(p, eoff)
        w = np.where(M.use, 1.0 / M.pix['v'], 0.0)
        N = (A.T.multiply(w).tocsr() @ A).tocsr()
    finally:
        M.use = saved
    n = N.shape[0]
    out = np.zeros((NBAND, n))
    for d in range(NBAND):
        out[d, :n - d] = N.diagonal(d)
    return out


def _solve_band(nband, b):
    """N x = b for the stored band of a symmetric normal matrix; grid points without
    weight get x = 0."""
    from scipy.linalg import solveh_banded
    n = b.size
    diag = nband[0]
    pos = diag[diag > 0]
    if pos.size == 0:
        return np.zeros(n)
    good = diag > 1e-3 * np.median(pos)
    ab = np.zeros((NBAND, n))                  # upper form: ab[u - d, d:] = diagonal d
    u = NBAND - 1
    for d in range(NBAND):
        v = nband[d, :n - d].copy()
        if d == 0:
            v = np.where(good, v + 1e-9 * np.median(pos), 1.0)
        else:
            v = np.where(good[:n - d] & good[d:], v, 0.0)
        ab[u - d, d:] = v
    try:
        x = solveh_banded(ab, np.where(good, b, 0.0))
    except np.linalg.LinAlgError:
        x = np.where(good, b / np.where(good, diag, 1.0), 0.0)
    return np.where(good, x, 0.0)


def _placed_light(exps, k, P1, cal_of):
    """A pass 1's light in exposure k's nod difference of the target, target units."""
    e = exps[k]
    out = np.zeros(e.y.shape)
    for j, w in [(k, 1.0)] + [(j, -1.0 / len(e.partners)) for j in e.partners]:
        fr = P1.frames.get(exposure_key(exps[j].name))
        if fr is None:
            continue
        ty, tx, ok = _place_sparse(fr, e.origin, e.y.shape)
        with np.errstate(divide='ignore', invalid='ignore'):
            conv = cal_of(j)[ty[ok], tx[ok]] / fr['cal'][ok].astype(float)
        good = np.isfinite(conv)
        np.add.at(out, (ty[ok][good], tx[ok][good]), w * fr['img'][ok][good].astype(float) * conv[good])
    return out


def echo_corrected(P1, exps, ks, own_light, cal_of):
    """A contaminant's pass 1 with the target's own light taken out.

    A contaminant whose trace others cover is solved over their pixels
    (``compute_pass1``, unreliable), the target's among them, so its spectrum holds part
    of the target's light: at the wavelengths the contaminant has on the shared pixels,
    the target's lines and continuum come back as the contaminant's. Predicted onto
    the target, that part (the echo) lands exactly on the target's own light, and
    the target masks its own lines as contamination (5301: 8427's pass 1 is 5301's
    echo to within its noise).

    The echo is the contaminant's fixed-shape solve applied to the target's light. The
    target's light on its own nod differences comes from its own pass 1
    (``own_light(k)``); the contaminant's design on those pixels from its frames per
    unit spectrum (``prof``, ``plam``), converted to target units as the prediction is;
    the normal matrix is taken diagonal, 1 / ``err_raw`` ** 2 (the solve's banded
    coupling of neighbouring grid points is left out; the result is smoothed). The
    echo is subtracted from the full-resolution spectrum, which is then smoothed as
    pass 1 smoothed it (``n_smooth``, lines kept), and the frames are rebuilt. Linear,
    no iteration. Where the target's own pass 1 holds the contaminant's light too (it
    is covered as well) the echo also removes that share of the contaminant (second
    order: product of the two absorbed fractions).

    ks : the target's nod differences that hold the contaminant's light.
    Returns a copy of ``P1`` (``echo`` = the subtracted full-resolution echo)."""
    import copy
    lam = np.asarray(P1.lam, float)
    b = np.zeros(lam.size)
    for k in ks:
        e = exps[k]
        L = own_light(k)
        sig = diff_sigma(exps, k)
        for j, w in [(k, 1.0)] + [(j, -1.0 / len(e.partners)) for j in e.partners]:
            fr = P1.frames.get(exposure_key(exps[j].name))
            if fr is None:
                continue
            ty, tx, ok = _place_sparse(fr, e.origin, e.y.shape)
            ty, tx = ty[ok], tx[ok]
            with np.errstate(divide='ignore', invalid='ignore'):
                conv = cal_of(j)[ty, tx] / fr['cal'][ok].astype(float)
                v = w * fr['prof'][ok].astype(float) * conv * L[ty, tx] / sig[ty, tx] ** 2
            lp = fr['plam'][ok].astype(float)
            use = np.isfinite(v) & e.ok[ty, tx] & (lp >= lam[0]) & (lp <= lam[-1])
            if not use.any():
                continue
            v, lp = v[use], lp[use]
            i = np.clip(np.searchsorted(lam, lp) - 1, 0, lam.size - 2)
            t = np.clip((lp - lam[i]) / (lam[i + 1] - lam[i]), 0.0, 1.0)
            np.add.at(b, i, v * (1 - t))
            np.add.at(b, i + 1, v * t)
    er = np.asarray(P1.err_raw, float)
    nb = getattr(P1, 'nband', None)
    if nb is not None and np.shape(nb) == (NBAND, lam.size):
        echo = np.where(np.isfinite(er), _solve_band(np.asarray(nb, float), b), 0.0)
    else:                                       # no band stored: diagonal (overestimates by ~1.4)
        echo = np.where(np.isfinite(er), er ** 2 * b, 0.0)
    f = np.asarray(P1.flux_raw, float) - echo
    if getattr(P1, 'n_smooth', 0) and P1.n_smooth > 1:
        fs, es, _ = smooth_spectrum(f, er, n=P1.n_smooth, keep_lines=getattr(P1, 'keep_lines', True))
    else:
        fs, es = f, er
    keep = ~np.isfinite(fs) & np.isfinite(P1.flux)
    fs, es = np.where(keep, P1.flux, fs), np.where(keep, P1.flux_err, es)
    fz, ez = np.nan_to_num(fs), np.abs(np.nan_to_num(es))
    frames = {}
    for key, fr in P1.frames.items():
        lp = fr['plam'].astype(float)
        prof = fr['prof'].astype(float)
        img = prof * np.interp(lp, lam, fz, left=0.0, right=0.0)
        err = prof * np.interp(lp, lam, ez, left=0.0, right=0.0)
        if 'ext' in fr:                         # light past the cutout (``profile_extend``): no echo there
            img = np.where(fr['ext'], fr['img'], img)
            err = np.where(fr['ext'], fr['err'], err)
        frames[key] = dict(fr, img=img.astype(np.float32), err=err.astype(np.float32))
    out = copy.copy(P1)
    out.frames, out.flux, out.flux_err, out.echo = frames, fs, es, echo
    return out


def predict(exps, sid, maps, pass1_for, rate_dir, own_pass1=None, pair_ratio=3.0, echo=True, exclude=(),
            keep_sources=False, why_missing=None):
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
        spectrum from the joint solve of an overlap group (``profile_joint``): its
        light is subtracted and never masked (its statistical uncertainty goes to
        ``dpred_joint``, for reporting only, without the ``DPRED_FLOOR``), except its
        light past its cutout when that comes from an unreliable pass 1
        (``ext_mask_only``), which masks as an unreliable contaminant's does.
    own_pass1 : the target's own pass 1 (pair candidates; the echo correction).
    exclude : contaminants whose light is modelled elsewhere (the other members of
        the target's joint-solve group while their light is being solved for, and the
        contaminants pass 1b masks): their light is not added, except a member's
        light past its cutout (not among the joint unknowns), from its pass 1.
    keep_sources : keep every contaminant's predicted light per exposure in
        ``by_source`` (default: only the pair candidates').
    echo : an unreliable contaminant solved over the target's pixels is predicted from
        its spectrum with the target's own light taken out (``echo_corrected``).
    why_missing : callable sid -> why that source has no pass 1 (``missing_reason``), for
        the error message.

    A pass 1 taken from the contaminant's 3-pixel boxcar (``boxcar_pass1``) is used like
    any reliable one; such contaminants are listed in ``Prediction.boxcar``.

    Raises ``MissingContaminant`` for a contaminant (not in ``exclude``) without a
    pass 1, or without the pass-1 frame of an exposure whose light the maps put on the
    target (a frame the source is not in, ``Pass1.nosrc``, holds no light)."""
    geo = geometry(exps, sid, maps)
    n = len(exps)
    pred = [np.zeros(e.y.shape) for e in exps]
    dpred = [np.zeros(e.y.shape) for e in exps]
    dpred_j = [np.zeros(e.y.shape) for e in exps]
    pred_j = [np.zeros(e.y.shape) for e in exps]
    pred_u = [np.zeros(e.y.shape) for e in exps]
    exclude = {int(x) for x in exclude}
    joint = set()
    c_plan = PF.planned_position(exps)[0]
    cals = {}

    def cal_of(j):
        if j not in cals:
            cals[j] = calibration(exps[j], rate_dir)
        return cals[j]

    own_L = {}
    # the target's own pass 1 is its light for the echo; not when the sanity guard says its
    # level is wrong (several times the stage-3 spectrum): the echo would be over-subtracted
    # and the contaminant's prediction turn strongly negative (1736, 853847: masked 50-60 %)
    own_ok = own_pass1 is not None and getattr(own_pass1, 'why', '') != 'guard'

    def own_light(k):
        if k not in own_L:
            own_L[k] = _placed_light(exps, k, own_pass1, cal_of)
        return own_L[k]

    cache, contrib, unreliable, by_source = {}, {}, set(), {}
    echoed, boxcar = set(), set()
    for k in range(n):
        e = exps[k]
        if not e.use_data or e.partners is None or not geo.sources[k]:
            continue
        sig = diff_sigma(exps, k)
        ms = main_shutter(e, c_plan) & e.ok
        terms = [(k, 1.0)] + [(j, -1.0 / len(e.partners)) for j in e.partners]
        for csid in sorted(geo.sources[k]):
            if csid not in cache:
                P1 = pass1_for(csid)
                if (echo and own_ok and P1 is not None and not P1.reliable and int(csid) not in exclude
                        and hasattr(P1, 'has_design') and P1.has_design()):
                    ks = [q for q in range(n) if exps[q].use_data and exps[q].partners is not None
                          and csid in geo.sources[q]]
                    P1 = echo_corrected(P1, exps, ks, own_light, cal_of)
                    echoed.add(csid)
                cache[csid] = P1
            P1 = cache[csid]
            if P1 is None:
                if int(csid) in exclude:
                    continue
                why = why_missing(csid) if why_missing is not None else None
                raise MissingContaminant(f'source {sid}: the trace maps put source {csid} on its trace in '
                                         f'{exposure_key(e.name)}, and source {csid} has no pass 1'
                                         + (f' ({why})' if why else ''))
            if not P1.reliable:            # its pass 1 may carry the target's light: do not hand it back
                unreliable.add(csid)
            if getattr(P1, 'why', '') == 'boxcar' and int(csid) not in exclude:
                boxcar.add(csid)
            lit = geo.lit_from[k].get(csid, set()) if geo.lit_from is not None else set()
            add = np.zeros(e.y.shape)
            dadd = np.zeros(e.y.shape)
            add_ext = np.zeros(e.y.shape)          # the part of ``add`` past the contaminant's cutout
            dadd_ext = np.zeros(e.y.shape)
            for j, w in terms:
                key = exposure_key(exps[j].name)
                fr = P1.frames.get(key)
                if fr is None:
                    if (j in lit and int(csid) not in exclude and P1.nosrc is not None
                            and key not in P1.nosrc):
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
                if 'ext' in fr:
                    xg = good & np.asarray(fr['ext'], bool)[ok]
                    np.add.at(add_ext, (ty[ok][xg], tx[ok][xg]), w * img[xg] * conv[xg])
                    np.add.at(dadd_ext, (ty[ok][xg], tx[ok][xg]),
                              abs(w) * fr['err'][ok][xg].astype(float) * conv[xg])
            if int(csid) in exclude:       # modelled elsewhere (joint solve): only its light past its cutout
                if add_ext.any():          # (not among the joint unknowns) is predicted, from pass 1
                    if P1.reliable:
                        pred[k] += add_ext
                        dpred[k] += dadd_ext
                    else:
                        pred_u[k] += add_ext
                continue
            if getattr(P1, 'joint', False):
                if getattr(P1, 'ext_mask_only', False) and add_ext.any():
                    # past its cutout from an unreliable pass 1: masking only, as in one pass
                    pred_u[k] += add_ext
                    add, dadd = add - add_ext, dadd - dadd_ext
                pred_j[k] += add           # jointly solved: subtracted, never masked
                dpred_j[k] += dadd
                joint.add(csid)
                with np.errstate(invalid='ignore', divide='ignore'):
                    s = np.abs(add) / sig
                v = float(np.nanmax(np.where(ms, s, np.nan))) if ms.any() else 0.0
                contrib[csid] = max(contrib.get(csid, 0.0), v if np.isfinite(v) else 0.0)
            elif not P1.reliable:
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
        pred[k] = pred[k] + pred_j[k]
    out = Prediction(pred=pred, dpred=dpred, contributors=contrib, unreliable=unreliable, pred_unreliable=pred_u,
                     have_maps=geo.have_maps, by_source=by_source, failed_open=geo.failed_open, echoed=echoed,
                     dpred_joint=dpred_j, joint=joint, excluded=exclude, pred_joint=pred_j, boxcar=boxcar)
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
    if not keep_sources:
        for csid in list(by_source):                 # keep per-source light only where the pair flag needs it
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
        # jointly solved light (``profile_joint``) is subtracted in both modes, never masked
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
    the systematic part: its statistical uncertainty is in the joint solve's error
    inflation, ``profile_joint``)."""
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
