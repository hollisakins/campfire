"""Where an observation's MSA bars sit against jwst's slit frame, measured from the background in its
empty shutters.

The profile fit's shutters (``optics``) sit at fixed positions in jwst's slit frame. A star fitted
near a bar shows that the real bars of one observation can sit up to about 0.02 slit units (9 mas)
away from them, by a different amount in each observation, and that this moves the flux of a source
near a bar by several per cent (docs/design-nirspec-pipeline-v2.md, §6.4). The amount does not follow
the grating-wheel tilt readings or the date, so it is measured per observation, here, from light
every observation has: the background in the shutters of its slitlets that hold no source.

The background lights an empty shutter evenly, so the shutter's profile across the slit is the
open area blurred by the optics: flat in the middle, falling over the bars. The model's profile for
uniform light is its point images summed over every source position (``uniform_profile``). For
every empty open shutter of the observation's canonicals (``barshadow_calibration.pixel_samples``:
the source's shutter, other catalogued sources and pixels under two slitlets left out), each
pixel's surface brightness over its exposure's pooled background, divided by the shutter's own
centre level in each of three wavelength bands, is compared with the model's profile at the
pixel's slit y plus a trial offset. Only the half of each shutter that faces away from the source
is used (the source's wings brighten the near half) and only its steep part, 0.2 to 0.75 pitches
from the shutter centre. The two halves are fitted apart: an offset moves both the same way, a
profile wider or narrower than the model moves them opposite, so their mean is the offset whatever
the width. The score of a trial offset is the chi2 of binned medians (half, band, distance from
the centre); the minimum is a parabola through the best three.

The offset has the profile fit's sign: it is added to every pixel's slit y and to the source's
planned position (``profile_fit.load_group``), so the model's shutters sit where the background
says the bars are. Within an observation it is one number plus a small pattern by MSA quadrant
that is the same in every observation (``QUADRANT_PATTERN``).

The result is written per disperser to ``bar_offset.json`` in the observation's workspace and
measured again only when it is missing, from an older version of this module or optics model, or
from other canonicals (their number, or when stage 2a made them; ``ensure``).
"""
import json
import os
import warnings

import numpy as np
from scipy.ndimage import map_coordinates

from campfire_pipeline.nirspec import barshadow_calibration as B
from campfire_pipeline.nirspec import crds_refs as CR
from campfire_pipeline.nirspec import optics as O

MEASURE_VERSION = '5'                 # 2: shutters above SCALE_CUT (1.05 x the background) left out; 3: exact shutter edges (optics engine 2);
#                                       4: shutters holding a secondary source left out (pixel_samples);
#                                       5: the quadrant pattern counted once (pattern_mean), G140M / G235M on the widened
#                                          grid, no offset from a minimum on the edge of SHIFTS
OFFSET_FILE = 'bar_offset.json'
SHIFTS = np.round(np.arange(-0.10, 0.1001, 0.005), 4)  # trial offsets [slit units]
U_LO, U_HI = 0.2, 0.75       # the steep part of a shutter's edge [pitches from its centre]
U_BIN = 0.025
CENTRE = 0.15                # a shutter's own level from |u| below this [pitches] (B.CENTRE_FRAC)
N_BANDS = 3
MAX_K = 2                    # shutters up to this many from the source (the optics tables end at +-5.5 slit units)
SCALE_MAX = 1.5              # a shutter whose centre reads more than this x its exposure's background holds other light
# The cut ``measure`` applies (at most SCALE_MAX). A bright star's far wing raises its neighbouring empty shutters a
# little and slopes across their edges. Scanned on 1133, NGC 2506 v3b and v6 (bright stars) and two survey fields
# (2026-11-05, scale_cut_scan.py): from 1.5 down to 1.05 the bright fields' offsets settle (1133 +0.0105 -> +0.0096,
# v3b -0.019 -> -0.017, v6 -0.007 -> -0.0115) and 1133's lower-minus-upper falls from -0.026 to -0.0135, the survey
# value; the survey fields' offsets do not move (ember_cosmos_p14 -0.0012, uncover2 -0.0073) and keep 85-93 % of
# their pixels.
SCALE_CUT = 1.05
MIN_PIXELS = 3000            # per half, for an offset
MIN_EXPOSURE_PIXELS = 2000   # per half, for one exposure's own offset (the spread between exposures)
MIN_BIN = 30
PAD_PX = 2.0                 # detector rows past the source's shutter left out (``pixel_samples``)
MAX_CANONICALS = 800         # a random subset of sources above this many canonicals, every exposure of each
# Each quadrant's offset from its observation's, the same in every observation: the mean over eleven survey
# PRISM observations (uncover2, eight EMBER pointings, capers_uds_p8, venus_pg004; scatter 0.0006-0.0009 per
# quadrant, the same against the empirical bar-shadow table and against the optics model; 2026-11-05).
QUADRANT_PATTERN = {1: -0.0042, 2: 0.0003, 3: -0.0009, 4: 0.0047}


# ---------------------------------------------------------------------------
# The model's profile for uniform light
# ---------------------------------------------------------------------------
_UNIFORM = {}


def uniform_profile(detector, mask):
    """(LAM, Y) light per detector row of a uniform background of unit surface brightness (per slit
    unit) through the open shutters ``mask`` (offsets from the source's shutter): the model's point
    images summed over every source position. The kernel before the shutter is left out: it moves
    light between source positions, which uniform light does not feel."""
    key = (str(detector).upper(), tuple(int(k) for k in mask))
    if key not in _UNIFORM:
        tab = O.optics_for(key[0]).raw_image_table(key[1])
        _UNIFORM[key] = np.asarray(tab, float).sum(axis=1) * (O.C[1] - O.C[0])
    return _UNIFORM[key]


def uniform_at(prof, y, lam):
    """``uniform_profile`` at slit positions ``y`` and wavelengths ``lam`` [um]."""
    li = O.Optics._li(np.asarray(lam, float))
    yi = (np.asarray(y, float) - O.Y[0]) / (O.Y[1] - O.Y[0])
    return map_coordinates(prof, np.vstack([li, yi]), order=1, mode='nearest')


def open_mask(state):
    """The open shutters of a shutter-state string as offsets from its 'x' shutter."""
    state = str(state or '')
    kx = max(state.find('x'), 0)
    return tuple(j - kx for j, ch in enumerate(state) if ch != '0')


def model_ratio(prof, y, lam, kk, pitch, shifts=SHIFTS):
    """(n_shift, n) the model's profile at each pixel's slit y (``y``, slit units) plus each trial
    offset, over its value at the centre of the pixel's shutter ``kk`` moved the same way (the data
    are divided by that shutter's own centre level)."""
    cu = np.linspace(-CENTRE, CENTRE, 7) * pitch
    out = np.empty((len(shifts), y.size), np.float32)
    for i, d in enumerate(shifts):
        num = uniform_at(prof, y + d, lam)
        den = np.mean([uniform_at(prof, kk * pitch + c + d, lam) for c in cu], axis=0)
        with np.errstate(invalid='ignore', divide='ignore'):
            out[i] = num / den
    return out


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------
def _band_edges(grid):
    lam = grid.wavelengths_um
    lo, hi = max(float(lam.min()), O.LAM[0]), min(float(lam.max()), O.LAM[-1])
    return np.geomspace(lo, hi, N_BANDS + 1)


def _worker(args):
    c, counter_path, grid, sky, scale_k, edges = args
    counter = np.load(counter_path) if counter_path else None
    try:
        p = B.pixel_samples(c, counter, PAD_PX)
    except Exception as e:                    # noqa: BLE001 - one slit's failure leaves the rest
        return None, f'{os.path.basename(c.path)}: {type(e).__name__}: {e}'
    col = grid.column_of(p['lam'])
    sel = B.profile_selection(p, col, set(scale_k)) & (p['lam'] > edges[0]) & (p['lam'] < edges[-1])
    kk = np.asarray(p['k'], int).copy()
    j = np.asarray(p['j'], int)
    n_sh = len(p['state'])
    kk[j == -1] += 1                          # a margin pixel measures the open shutter it borders
    kk[j == n_sh] -= 1
    u = p['y'] - kk
    with np.errstate(invalid='ignore', divide='ignore'):
        ratio = p['sb'] / np.interp(p['lam'], grid.wavelengths_um, sky)
    band = np.digitize(p['lam'], edges) - 1
    norm = np.full(u.size, np.nan)
    for k in np.unique(kk[sel]):
        for b in range(N_BANDS):
            m = (kk == k) & (band == b) & (np.abs(u) < CENTRE) & np.isfinite(ratio)
            if m.sum() >= 20:
                norm[(kk == k) & (band == b)] = np.median(ratio[m])
    scale = np.array([scale_k.get(int(k), np.nan) for k in kk])
    sel &= (np.sign(u) == np.sign(kk)) & (kk != 0) & (np.abs(kk) <= MAX_K)
    sel &= (np.abs(u) > U_LO) & (np.abs(u) < U_HI) & np.isfinite(norm) & (norm > 0) & (scale < SCALE_MAX)
    if not sel.any():
        return None, None
    mask = open_mask(p['state'])
    prof = uniform_profile(c.detector, mask)
    pitch = float(p['pitch'])
    T = model_ratio(prof, p['y'][sel] * pitch, p['lam'][sel], kk[sel], pitch)
    n = int(sel.sum())
    # the edge each pixel looks at: inner when the shutter beyond it is open too (a bar between two open
    # shutters), outer when it is closed (the slitlet's end)
    beyond = kk[sel] + np.sign(u[sel]).astype(int)
    inner = np.isin(beyond, np.array(mask))
    return dict(u=u[sel].astype(np.float32), band=band[sel].astype(np.int8),
                r=(ratio[sel] / norm[sel]).astype(np.float32), T=T,
                quadrant=np.full(n, p['quadrant'], np.int8), inner=inner, scale=scale[sel].astype(np.float32),
                exposure=np.full(n, f'{c.root}_{c.nod}')), None


def sample_grid(canonical, cutoffs):
    """The wavelength grid the samples are binned on: the empirical bar-shadow table's (widened
    to the light on the detector), else CRDS's widened to the disperser's red cutoff. The CRDS
    G140M / G235M references stop at 1.495 / 2.49 um, and a pixel off the grid is dropped."""
    from campfire_pipeline.config import get_empirical_barshadow_path
    emp = get_empirical_barshadow_path(canonical.grating)
    if emp:
        return B.reference_grid(emp)
    return B.widened_grid(B.reference_grid(CR.barshadow_path(canonical.path)), cutoffs[1])


def samples(canonicals, workspace, processes=1, cache_dir=None, log=None):
    """Every usable pixel of the empty shutters of ``canonicals`` (one disperser of one
    observation): dict of flat arrays ``u`` (pitches from the shutter centre), ``band``, ``r``
    (surface brightness over the exposure's background and the shutter's centre level),
    ``T`` (n_shift, n: the model's at each of ``SHIFTS``), ``quadrant``, ``exposure``; None
    when there is none."""
    from multiprocessing import Pool
    from campfire_pipeline.nirspec.coverage import cache_path, coverage_from_wcs, cutoffs_for, default_cache_dir
    log = log or (lambda *a: None)
    cache_dir = cache_dir or default_cache_dir()
    cut = cutoffs_for(canonicals[0].grating, canonicals[0].filter)
    grid = sample_grid(canonicals[0], cut)
    edges = _band_edges(grid)
    counters = {}
    for rate in sorted({c.rate_path for c in canonicals}):
        if os.path.exists(rate):
            coverage_from_wcs(rate, cache_dir=cache_dir, cutoffs=cut)
            counters[rate] = cache_path(rate, cache_dir, cut)
    shutters, fails = B.centre_spectra(canonicals, counters, grid, PAD_PX, processes)
    skies, _, scales = B.pool_sky_per_observation(shutters, grid)
    scale_by = {}
    for key, (a, _) in scales.items():
        if np.isfinite(a):
            scale_by.setdefault(key[:-1], {})[key[-1]] = a
    args = [(c, counters.get(c.rate_path), grid, skies[(c.obs, c.root, c.nod)], scale_by[c.key], edges)
            for c in canonicals if scale_by.get(c.key) and (c.obs, c.root, c.nod) in skies]
    res, errs = [], list(fails)
    with Pool(max(int(processes), 1)) as pool:
        for r, e in pool.imap_unordered(_worker, args, chunksize=2):
            if e:
                errs.append(e)
            if r is not None:
                res.append(r)
    if errs:
        log(f'bar offset: {len(errs)} slits could not be sampled (first: {errs[0]})')
    if not res:
        return None
    return {k: np.concatenate([r[k] for r in res], axis=-1) for k in res[0]}


# ---------------------------------------------------------------------------
# The fit
# ---------------------------------------------------------------------------
def _best(r, T, key):
    """Best trial offset for pixels ``r`` with model ``T``: chi2 of the binned medians (bins
    ``key``) against the model's, minimum of a parabola through the best three; NaN with fewer
    than five bins, or when the minimum is on the edge of the trial offsets (a half dominated by
    other light, not an offset the grid brackets)."""
    chi = np.zeros(SHIFTS.size)
    nb = 0
    order = np.argsort(key, kind='stable')
    ks, r, T = key[order], r[order], T[:, order]
    starts = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]])
    for a, b in zip(starts, np.r_[starts[1:], ks.size]):
        if b - a < MIN_BIN:
            continue
        med = np.median(r[a:b])
        var = 0.5 * np.pi * (1.4826 * np.median(np.abs(r[a:b] - med))) ** 2 / (b - a)
        if not var > 0:
            continue
        chi += (med - np.median(T[:, a:b], axis=1)) ** 2 / var
        nb += 1
    if nb < 5:
        return np.nan
    i = int(np.argmin(chi))
    if i in (0, SHIFTS.size - 1):
        return np.nan
    a, b, _ = np.polyfit(SHIFTS[i - 1:i + 2], chi[i - 1:i + 2], 2)
    return float(np.clip(-b / (2 * a), SHIFTS[i - 1], SHIFTS[i + 1])) if a > 0 else float(SHIFTS[i])


def fit(s, sel=None, min_pixels=MIN_PIXELS):
    """(offset, lower, upper) [slit units] from samples ``s`` (``samples``), restricted to
    ``sel``: each half fitted alone, the offset their mean; NaNs when a half has fewer than
    ``min_pixels``."""
    ok = np.isfinite(s['r']) & np.all(np.isfinite(s['T']), axis=0)
    if sel is not None:
        ok &= sel
    up = s['u'] > 0
    ub = np.digitize(np.abs(s['u']), np.arange(U_LO, U_HI + 1e-6, U_BIN)) - 1
    key = s['band'].astype(int) * 1000 + ub
    out = []
    for half in (ok & ~up, ok & up):
        out.append(_best(s['r'][half], s['T'][:, half], key[half]) if half.sum() >= min_pixels else np.nan)
    lo, hi = out
    return 0.5 * (lo + hi), lo, hi


def subset(s, keep):
    """The samples ``s`` (``samples``) restricted to ``keep``."""
    return {k: (v[:, keep] if k == 'T' else v[keep]) for k, v in s.items()}


def measure(canonicals, workspace, processes=1, cache_dir=None, max_canonicals=MAX_CANONICALS, log=None,
            scale_cut=None):
    """The bar offset of one disperser of one observation from the canonicals' empty shutters:
    a dict with ``offset`` (slit units; NaN when too few pixels), the two halves (``lower`` /
    ``upper``), ``exposures`` (each exposure's own offset where it has enough pixels) and their
    ``spread``, the offset measured in each quadrant (``quadrants``, a check), the offsets the
    profile fit applies per quadrant (``applied``: the offset less ``pattern_mean``, the pattern
    the all-quadrant fit already holds, plus ``QUADRANT_PATTERN``), and the counts. Only shutters whose centre reads below ``scale_cut`` x the exposure's background
    (default ``SCALE_CUT``) enter: a bright neighbour's far wing raises a shutter's level and
    slopes across its edges."""
    scale_cut = SCALE_CUT if scale_cut is None else scale_cut
    log = log or (lambda *a: None)
    cans = list(canonicals)
    n_all = len(cans)
    if max_canonicals and len(cans) > max_canonicals:
        sids = sorted({c.source_id for c in cans})
        keep = set(np.random.default_rng(1).choice(sids, size=max(1, int(len(sids) * max_canonicals / len(cans))),
                                                   replace=False).tolist())
        cans = [c for c in cans if c.source_id in keep]
    out = dict(version=MEASURE_VERSION, optics=O.version(), n_canonicals=n_all, n_sampled=len(cans),
               offset=np.nan, lower=np.nan, upper=np.nan, n_pixels=0, exposures={}, spread=np.nan,
               quadrants={}, applied={})
    s = samples(cans, workspace, processes=processes, cache_dir=cache_dir, log=log) if cans else None
    if s is None:
        return out
    s = subset(s, s['scale'] < scale_cut)
    out['scale_cut'] = scale_cut
    out['n_pixels'] = int(s['r'].size)
    out['offset'], out['lower'], out['upper'] = fit(s)
    for ex in np.unique(s['exposure']):
        v = fit(s, s['exposure'] == ex, MIN_EXPOSURE_PIXELS)[0]
        if np.isfinite(v):
            out['exposures'][str(ex)] = v
    vals = list(out['exposures'].values())
    out['spread'] = float(np.std(vals)) if len(vals) > 2 else np.nan
    for q in (1, 2, 3, 4):
        v = fit(s, s['quadrant'] == q)[0]
        if np.isfinite(v):
            out['quadrants'][str(q)] = v
    # the two halves per wavelength band (the band edges of ``_band_edges``): where the lower and the upper edges
    # sit against the model at each wavelength, for the calibration of the shutter edges (optics_calibration)
    # and apart for the edges between two open shutters (inner) and the slitlet's ends (outer)
    out['bands'] = {}
    for b in range(N_BANDS):
        rec = {}
        for lab, sel in (('all', s['band'] == b), ('inner', (s['band'] == b) & s['inner']),
                         ('outer', (s['band'] == b) & ~s['inner'])):
            _, lo, hi = fit(s, sel, MIN_PIXELS // N_BANDS)
            rec[lab] = dict(lower=lo, upper=hi, n=int(sel.sum()))
        out['bands'][str(b)] = rec
    if np.isfinite(out['offset']):
        # the fit over every quadrant's pixels already holds the pattern, weighted by where the pixels
        # sit; it is taken out before each quadrant's own is added (zero for a field spread evenly)
        out['pattern_mean'] = pattern_mean(s['quadrant'][np.isfinite(s['r'])])
        out['applied'] = {str(q): out['offset'] - out['pattern_mean'] + QUADRANT_PATTERN[q] for q in (1, 2, 3, 4)}
    return out


def pattern_mean(quadrants):
    """``QUADRANT_PATTERN`` averaged over pixels in these quadrants."""
    n = np.bincount(np.clip(np.asarray(quadrants, int), 0, 4), minlength=5)
    if n.sum() == 0:
        return 0.0
    return float(sum(n[q] * QUADRANT_PATTERN[q] for q in (1, 2, 3, 4)) / n.sum())


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------
def disperser_key(grating, filter_name):
    """'PRISM/CLEAR', the key of ``bar_offset.json`` and ``profile_fit.Exposure.grating``."""
    return f'{grating}/{filter_name}'.upper()


def offset_path(workspace):
    return os.path.join(workspace, OFFSET_FILE)


def _clean(v):
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (float, np.floating)):
        return None if not np.isfinite(v) else round(float(v), 5)
    if isinstance(v, np.integer):
        return int(v)
    return v


def read(workspace):
    """{disperser key: measurement} from the workspace's file ({} when there is none)."""
    try:
        with open(offset_path(workspace)) as fh:
            return json.load(fh).get('dispersers', {})
    except (OSError, ValueError):
        return {}


def write(workspace, dispersers):
    path = offset_path(workspace)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w') as fh:
        json.dump(dict(dispersers={k: _clean(v) for k, v in sorted(dispersers.items())}), fh, indent=1)
    os.replace(tmp, path)


def table(workspace):
    """{disperser key: {quadrant: offset}} of the measured dispersers, for ``profile_fit.load_group``."""
    out = {}
    for key, m in read(workspace).items():
        ap = m.get('applied') or {}
        if ap:
            out[key] = {int(q): float(v) for q, v in ap.items() if v is not None}
    return out


def ensure(workspace, processes=1, cache_dir=None, log=None, overwrite=False):
    """Measure every disperser of the observation in ``workspace`` whose entry in
    ``bar_offset.json`` is missing, from another ``MEASURE_VERSION`` or optics model, or from
    other canonicals (another number, or made at another time by stage 2a); returns ``table``."""
    log = log or (lambda *a: None)
    workspace = os.path.abspath(workspace)
    obs_name = os.path.basename(workspace)
    cans = B.discover_workspace(workspace)
    by = {}
    for c in cans:
        by.setdefault(disperser_key(c.grating, c.filter), []).append(c)
    from campfire_pipeline.nirspec.master_bkg import inputs_stamp
    have = read(workspace)
    changed = False
    for key, cs in sorted(by.items()):
        old = have.get(key) or {}
        # the canonicals as stage 2a made them (CFS2ATIM): a stage 2a rerun, e.g. on a new CRDS
        # context that moves jwst's slit frame, is measured again
        stamp = inputs_stamp(cs)
        if (not overwrite and old.get('version') == MEASURE_VERSION and old.get('optics') == O.version()
                and old.get('n_canonicals') == len(cs) and abs(float(old.get('inputs') or -1) - stamp) < 1e-3):
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', RuntimeWarning)
                m = measure(cs, workspace, processes=processes, cache_dir=cache_dir, log=log)
        except Exception as e:                 # noqa: BLE001 - no offset is the model as it was; tried again next run
            log(f'bar offset {obs_name} {key}: not measured ({type(e).__name__}: {e}); none applied')
            have.pop(key, None)
            changed = True
            continue
        if np.isfinite(m['offset']):
            log(f"bar offset {obs_name} {key}: {m['offset']:+.4f} slit units (halves {m['lower']:+.4f} / "
                f"{m['upper']:+.4f}, {m['n_pixels']} pixels, {len(m['exposures'])} exposures, spread "
                f"{m['spread']:.4f})")
        else:
            log(f'bar offset {obs_name} {key}: too few empty-shutter pixels ({m["n_pixels"]}); none applied')
        m['inputs'] = stamp
        have[key] = m
        changed = True
    if changed:
        write(workspace, have)
    return table(workspace)
