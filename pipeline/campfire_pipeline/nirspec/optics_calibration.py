"""
Calibration stars for the diffraction model of the MSA (``optics``).

Stars are loaded the way stage 3 loads a source (``profile_fit.load_source``: the nod
differences with the path loss removed, the partners stage 2b subtracted) and fitted per
wavelength range through ``profile_fit.ProfileModel`` with a model (``optics.Optics`` per
detector; the shipped one by default): the centre and its drift free with no centre law (the
centre is where the light is), a position offset per exposure, and the star's width free from
``W_FLOOR`` up (bright stars' cores are broader than faint ones'; design doc). Pixel weights
are the extraction's variances plus a floor of (column peak / ``SN_CAP``)^2, fixed per star and
range from a first fit: a star's misfit at the model's percent level would otherwise set the fit
by its brightest pixels, and the clipping would remove its core.

The stack test (``stack`` / ``score``): each star's own image per pixel as a share of its
light, against the distance from its fitted centre in detector rows, data and model, binned;
per detector and range the median over the clean stars of (data - model) in % of the peak. A
model passes a range when its largest |median| is under 1 %.

The kernel before the shutter (``kernel`` / ``solve``): the model is linear in the kernel, so
per clean star-range (at the fit the stack test makes, the width free only for stars at peak
S/N >= ``FREE_W_SNR``: the faint stars set the core) the model's pixels per unit kernel weight
at each node (``kernel_response``), for the nod differences and the single frames less the
master background; their normal equations summed per detector and range and solved for a
non-negative kernel with a penalty on the wing's curvature (``solve_kernel``). One pass is one
linearisation about the model it starts from; the shipped kernel took two (design doc).

The centre law and nod law (``laws``): from a stack run's clean star-ranges, each star's centre
per range against its 1.0-2.6 um centre, fitted per detector as a drift per MSA quadrant plus a
slope with the star's position (``profile_fit.CENTRE_LAW``), with the scatter held out by
observation; and the exposures' fitted offsets against their planned shifts
(``profile_fit.NOD_OFFSET_LAW``).

The stars are loaded with each observation's bar offset (``bars``: ``bar_offset.ensure`` on each
calibration workspace), as stage 3 loads a source. Every calibration observation enters every step;
none is held out (user, 2026-11-05).

Checks of a model: ``score --groups`` (the stack test per dataset), ``compare`` (two stack runs'
chi2 on the same stars), ``visits`` + ``repeat`` (the same star at several positions, fitted as
stage 3 fits it: is its flux the same? across configurations in y, check 1 on NGC 2506; along a
scan in x, 1133's slit grid or 12497 obs 4, which also refits the along-dispersion correction) and
``barshadow`` (the model's uniform light against the empirical bar-shadow tables).

    python -m campfire_pipeline.nirspec.optics_calibration bars --root R --obs a,b
    python -m campfire_pipeline.nirspec.optics_calibration stack --root R --obs a,b --out D
    python -m campfire_pipeline.nirspec.optics_calibration score --out D [--groups name=prefix,...]
    python -m campfire_pipeline.nirspec.optics_calibration compare --out D --against D2 [--groups ...]
    python -m campfire_pipeline.nirspec.optics_calibration kernel --root R --obs a,b --select D --out K
    python -m campfire_pipeline.nirspec.optics_calibration solve --out K --model-out new.json --version V
    python -m campfire_pipeline.nirspec.optics_calibration laws --out D
    python -m campfire_pipeline.nirspec.optics_calibration visits --root R --obs a,b --out v.json [--by-group]
    python -m campfire_pipeline.nirspec.optics_calibration repeat --out v.json [--along x]
    python -m campfire_pipeline.nirspec.optics_calibration barshadow [--optics M]
"""

import copy
import json
import os
from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares

from campfire_pipeline.nirspec import profile_fit as PF

RANGES = ((0.7, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 2.6), (2.6, 3.3), (3.3, 4.1), (4.1, 5.3))   # PRISM
SN_CAP = 30.0                      # per-pixel S/N cap of the weights (variance floor)
CLIP = 5.0                         # outlier clipping, sigma (the extraction's)
N_CLIP = 2
DRIFT_BOUND = 0.3                  # centre drift within a range [slit units per octave]
CENTRE_BOUND = 0.6                 # centre within this of the planned position [slit units]
OFFSET_BOUND = 0.1                 # per-exposure offset [slit units]
W_MAX = 0.15                       # a star's width [slit units]; it needs a few hundredths at most
GRID_MARGIN = 0.05                 # spectrum grid beyond the range [um]
SATURATED = 2                      # jwst DQ bit

# the clean star-ranges of the stack test
SNR_MIN = 20.0                     # peak-pixel S/N
EDGE_MAX = 0.35                    # |centre| [slit units]
SAT_MAX = 0.0                      # saturated share of the trace pixels
W_STAR_MAX = 0.06                  # a star's median width over its ranges [slit units]: wider is not a point source
CHI2_MAX = 2.0                     # chi2/dof: the variance floor keeps a star that matches the model at ~1 at any
                                   # brightness (bright ones far below), so the cut is absolute; one relative to
                                   # the range's median (bright stars) dropped faint ones that fit (1133 red: 60 %)


def restrict(exps, lam_range):
    """Shallow copies of the exposures with only the pixels in a wavelength range usable."""
    a, b = lam_range
    out = []
    for e in exps:
        x = copy.copy(e)
        with np.errstate(invalid='ignore'):
            x.ok = e.ok & (e.wav > a) & (e.wav < b)
        out.append(x)
    return out


def grating_ranges(exps, n=3):
    """``n`` wavelength ranges, equal in log wavelength, over a grating's coverage."""
    lam = np.concatenate([e.wav[np.isfinite(e.wav)] for e in exps if e.use_data])
    lo, hi = np.percentile(lam, [2, 98])
    edges = np.exp(np.linspace(np.log(lo), np.log(hi), n + 1))
    return tuple((float(a), float(b)) for a, b in zip(edges[:-1], edges[1:]))


# ---------------------------------------------------------------------------
# Stars on disk
# ---------------------------------------------------------------------------
@dataclass
class StarTask:
    """One star on one detector in one observation: its stage-2b background groups on
    that detector, their nod overrides and the canonicals stage 3 would combine
    (``stage3._profile_fit_task``)."""
    obs: str
    source_id: int
    detector: str
    groups: list
    overrides: list
    science: list
    quadrant: int = None
    srcy: float = np.nan
    disperser: str = 'prism_clear'
    bar_offsets: dict = None           # the observation's bar offsets (``bar_offset.table``), as stage 3 applies them
    group: str = ''                    # one stage-2b background group only (``star_tasks(by_group=True)``)
    srcx: float = np.nan
    config: int = -1                   # MSAMETID

    @property
    def key(self):
        base = f'{self.obs}_{self.source_id}_{self.detector}'
        base = base if self.disperser == 'prism_clear' else f'{base}_{self.disperser}'
        return f'{base}_g{self.group}' if self.group else base


def star_tasks(root, obs_name, disperser='prism_clear', observations_file=None, by_group=False):
    """Every source of a reduced observation with a stage-3 science exposure in the
    disperser, one task per detector (and per stage-2b background group with ``by_group``: a
    scan that steps the stars within one observation, ``visits``), with the observation's bar
    offsets (``bar_offset.json`` in its workspace, measured by the ``bars`` step; without it the
    stars are loaded with none, and a warning says so). Reads the root only (no workspace
    set-up)."""
    from astropy.io import fits
    from campfire_pipeline.nirspec import bar_offset as BO
    from campfire_pipeline.nirspec.observation import Observation
    from campfire_pipeline.nirspec import stage3 as S3

    obs = Observation.load(obs_name, observations_file=observations_file
                           or os.path.join(root, 'config', 'observations.toml'))
    obs.workspace_dir = os.path.join(root, 'products', 'nirspec', obs_name)
    obs.reference_dir = os.path.join(root, 'reference', 'nirspec', obs_name)
    every = Observation.group_files(obs.discover_files(ext='canonical', source_ids='all'))
    every = every[every['filter_grating'] == disperser]
    if len(every) == 0:
        return []
    bars = BO.table(obs.workspace_dir)
    if disperser.replace('_', '/').upper() not in bars:
        import warnings
        warnings.warn(f'{obs_name} {disperser}: no bar offset measured (optics_calibration bars); loaded without one')
    sci = S3._science_files(every)
    out = []
    for sid in np.unique(sci['source_id']):
        for det in np.unique(sci['detector'][sci['source_id'] == sid]):
            this = (sci['source_id'] == sid) & (sci['detector'] == det)
            for bg in (np.unique(sci['bkg_group'][this]) if by_group else [None]):
                tf = sci[this if bg is None else this & (sci['bkg_group'] == bg)]
                t = S3._profile_fit_task(obs, every, tf, '', sid)
                hs = [fits.getheader(p, 'SCI') for p in tf['path']]
                out.append(StarTask(obs=obs_name, source_id=int(sid), detector=str(det).upper(),
                                    groups=[[str(p) for p in g] for g in t['groups']], overrides=t['overrides'],
                                    science=[str(n) for n in t['science']], quadrant=int(hs[0].get('MSAQUAD', 0)),
                                    srcy=float(np.median([h.get('SRCYPOS', np.nan) for h in hs])),
                                    srcx=float(np.median([h.get('SRCXPOS', np.nan) for h in hs])),
                                    config=int(fits.getheader(tf['path'][0]).get('MSAMETID', -1)),
                                    disperser=disperser, bar_offsets=bars or None, group='' if bg is None else str(bg)))
    return out


def step_bars(root, observations, processes=1, log=print):
    """Measure the bar offset of every disperser of the calibration observations
    (``bar_offset.ensure``: kept in each workspace's ``bar_offset.json``, measured again only
    when stale), so the stars are loaded as stage 3 loads a source."""
    from campfire_pipeline.nirspec import bar_offset as BO
    for name in observations:
        bars = BO.ensure(os.path.join(root, 'products', 'nirspec', name), processes=processes, log=log)
        log(f'{name}: ' + '; '.join(f'{d} ' + ' '.join(f'q{q} {v:+.4f}' for q, v in sorted(qs.items()))
                                    for d, qs in sorted(bars.items())))


def tasks_for(root, observations, dispersers=('prism_clear',), by_group=False):
    """Star tasks of the observations."""
    return [t for name in observations for d in dispersers for t in star_tasks(root, name, disperser=d,
                                                                                 by_group=by_group)]


def load_star(task):
    return PF.load_source(task.groups, bkg_overrides=task.overrides, science=set(task.science),
                          bar_offsets=task.bar_offsets)


def saturated_flags(exps):
    """Per exposure: the DQ SATURATED bit of its canonical's live DQ (the frame or a
    subtracted partner) on its cutout, or None."""
    from campfire_pipeline.nirspec import canonical as C
    out = []
    for e in exps:
        dq = C.read_slit_arrays(e.path, exts=('DQ',)).get('DQ') if (e.use_data and e.has_source and e.path) else None
        out.append(None if dq is None else (np.asarray(dq).astype(np.int64) & SATURATED) > 0)
    return out


def saturated_fraction(exps, flags, centre, lam_range, halfwidth=0.25):
    """Share of a star's science pixels in a wavelength range within ``halfwidth`` (slit
    units) of the trace that are flagged SATURATED (``saturated_flags``)."""
    n, nsat = 0, 0
    for e, sat in zip(exps, flags):
        if sat is None:
            continue
        r0, c0, ny, nx = e.cutout
        y, w = e.y[r0:r0 + ny, c0:c0 + nx], e.wav[r0:r0 + ny, c0:c0 + nx]
        with np.errstate(invalid='ignore'):
            near = (np.abs(y - centre) < halfwidth) & (w > lam_range[0]) & (w < lam_range[1])
        n += int(near.sum())
        nsat += int(np.sum(near & sat))
    return nsat / n if n else 0.0


# ---------------------------------------------------------------------------
# One star in one wavelength range
# ---------------------------------------------------------------------------
class RangeFit:
    """A star in one wavelength range through the profile fit's model with ``optics``
    ({detector: Optics}; None for the shipped model). Shape: centre ``c0`` and drift (per
    octave about the model's pivot), the width (``logw``), no centre law; a position offset
    per exposure (``eoff``)."""

    def __init__(self, exps, lam_range, optics=None, nod_law=None):
        self.lam_range = tuple(lam_range)
        ex = restrict(exps, lam_range)
        opt = None if optics is None else [optics[e.detector] for e in ex]
        law = PF.NO_CENTRE_LAW
        g = PF.ProfileModel(ex, centre_law_tables=law, nod_law=nod_law, optics=opt).lam_grid
        g = g[(g > lam_range[0] - GRID_MARGIN) & (g < lam_range[1] + GRID_MARGIN)]
        if g.size < 3:
            raise PF.TooFewWavelengths('optics calibration: too few wavelengths in range')
        self.M = M = PF.ProfileModel(ex, grid=g, centre_law_tables=law, nod_law=nod_law, optics=opt)
        self.eoff = np.zeros(len(ex))
        self.logw = np.log(PF.W_FLOOR)
        self.v_raw = M.pix['v'].copy()
        # (exposure, detector column) of every fitted pixel, for the per-column peak
        self.colkey = np.concatenate([k * 100000 + np.nonzero(m)[1] for k, m in M.pix_masks])
        self.own = np.array([M.exps[k].has_source for k in M.pix['e']])
        self.lam_c = 0.5 * (lam_range[0] + lam_range[1])

    def shape(self, c0, drift):
        return np.array([c0, drift, self.logw, 0.0])

    def solve(self, c0, drift):
        f, _, good, A = self.M.solve(self.shape(c0, drift), self.eoff)
        return f, good, A

    def set_floor(self, model):
        """Variance floor (column peak / SN_CAP)^2 from a model of the differences."""
        u, inv = np.unique(self.colkey, return_inverse=True)
        mx = np.zeros(u.size)
        np.maximum.at(mx, inv, np.abs(model))
        self.M.pix['v'] = self.v_raw + (mx[inv] / SN_CAP) ** 2

    def peak_snr(self, model):
        """Median over the range's detector columns (science frames with the source) of
        the brightest pixel's S/N in the nod difference: model value over its noise."""
        s = model / np.sqrt(self.v_raw)
        sel = self.own & np.isfinite(s)
        if not sel.any():
            return 0.0
        u, inv = np.unique(self.colkey[sel], return_inverse=True)
        mx = np.full(u.size, -np.inf)
        np.maximum.at(mx, inv, s[sel])
        return float(np.median(mx))

    def centre_at(self, c0, drift, lam):
        return c0 + drift * self.M._x(np.asarray(lam, float))

    def fit(self, x0=None, use=None, clip_rounds=N_CLIP, offsets=True, width=True):
        """Least-squares fit of the centre and drift, with the width (``width``) and an offset
        per exposure that contains the star (``offsets``; the first held at zero) free.
        ``x0``: (c0, drift), else the best of a scan over the planned position +- 0.2.
        ``use``: a fixed pixel mask (no clipping); else clipping at CLIP sigma, ``clip_rounds``
        times. Leaves the shape in ``eoff`` / ``logw``; returns a dict."""
        M = self.M
        free = [k for k, e in enumerate(M.exps) if e.has_source][1:] if offsets else []
        nw = 1 if width else 0
        lo = np.array([M.c_prior - CENTRE_BOUND + 1.0, 1.0 - DRIFT_BOUND] + [np.log(PF.W_FLOOR)] * nw
                      + [-OFFSET_BOUND] * len(free))
        hi = np.array([M.c_prior + CENTRE_BOUND + 1.0, 1.0 + DRIFT_BOUND] + [np.log(W_MAX)] * nw
                      + [OFFSET_BOUND] * len(free))

        def unpack(x):
            e = np.zeros(len(M.exps))
            e[free] = x[2 + nw:]
            return x[0] - 1.0, x[1] - 1.0, (x[2] if width else np.log(PF.W_FLOOR)), e

        def resid(x):
            c0, dr, self.logw, self.eoff = unpack(x)
            f, _, A = self.solve(c0, dr)
            return np.where(M.use, (M.pix['d'] - A @ f) / np.sqrt(M.pix['v']), 0.0)

        tail = [np.log(0.01)] * nw + [0.0] * len(free)
        M.use = np.ones(M.npix, bool) if use is None else use.copy()
        if x0 is None:
            starts = [np.array([c + 1.0, 1.0] + tail) for c in M.c_prior + np.arange(-0.2, 0.201, 0.05)]
            x = min(starts, key=lambda s: float(np.sum(resid(np.clip(s, lo + 1e-6, hi - 1e-6)) ** 2)))
        else:
            x = np.array([x0[0] + 1.0, x0[1] + 1.0] + tail)
        x = np.clip(x, lo + 1e-6, hi - 1e-6)
        nround = 0 if use is not None else clip_rounds
        for it in range(nround + 1):
            res = least_squares(resid, x, bounds=(lo, hi), x_scale='jac', ftol=1e-6, xtol=1e-6, gtol=1e-6)
            x = res.x
            if it == nround:
                break
            saved, M.use = M.use, np.ones(M.npix, bool)
            new = np.abs(resid(x)) < CLIP
            M.use = saved
            if np.array_equal(new, M.use):
                break
            M.use = new
        c0, dr, self.logw, self.eoff = unpack(x)
        f, good, A = self.solve(c0, dr)
        model = A @ f
        r = (M.pix['d'] - model) / np.sqrt(M.pix['v'])
        rr = (M.pix['d'] - model) / np.sqrt(self.v_raw)
        ndof = max(float(np.sum(M.dof_weight()[M.use])) - int(good.sum()) - x.size, 1.0)
        chi2 = float(np.sum(r[M.use] ** 2))
        # the centre's error at the range's centre, from the last solve's Jacobian (chi2/dof >= 1)
        grad = np.zeros(x.size)
        grad[0], grad[1] = 1.0, float(M._x(np.array([self.lam_c]))[0])
        try:
            cov = np.linalg.pinv(res.jac.T @ res.jac) * max(chi2 / ndof, 1.0)
            e_c = float(np.sqrt(max(grad @ cov @ grad, 0.0)))
        except np.linalg.LinAlgError:
            e_c = np.nan
        return dict(c0=float(c0), drift=float(dr), c_node=float(self.centre_at(c0, dr, self.lam_c)), e_c=e_c,
                    w=float(np.exp(self.logw)), offsets=self.eoff.tolist(), chi2=chi2,
                    chi2_raw=float(np.sum(rr[M.use] ** 2)), ndof=ndof, chi2r=chi2 / ndof,
                    peak_snr=self.peak_snr(model), _x=[float(c0), float(dr)], _use=M.use.copy(), _model=model, _f=f)

    def start(self):
        """The variance floor from a first fit (centre and drift only, no clipping), then a
        clipped fit with the width free. Returns that fit."""
        self.set_floor(np.zeros(self.M.npix))
        ref = self.fit(clip_rounds=0, offsets=False, width=False)
        self.set_floor(ref['_model'])
        return self.fit(x0=ref['_x'], offsets=False)

    def samples(self, fitres):
        """The star's own image per pixel as a share of its light, for stacking: (offset
        from the trace centre [rows], data, model, noise, wavelength), over the science
        frames that contain the star, at the fit ``fitres`` (the last one made). The data are
        the nod differences less the model's partner terms (the negative copies), divided by
        the column's light (spectrum x throughput); the model is the same pixel's own term."""
        M = self.M
        c0, drift = fitres['_x']
        f, good, A = self.solve(c0, drift)
        model = A @ f
        p = self.shape(c0, drift)
        fvals = np.where(good, f, np.nan)
        out = dict(u=[], d=[], m=[], e=[], lam=[])
        n = 0
        for k, msk in M.pix_masks:
            sl = slice(n, n + int(msk.sum()))
            n += int(msk.sum())
            e = M.exps[k]
            if not e.has_source:
                continue
            y, lam, dy = e.y[msk], e.wav[msk], e.dy[msk]
            own = M.image(p, self.eoff, k, y, lam, dy)
            norm = np.interp(lam, M.lam_grid, fvals, left=np.nan, right=np.nan) * M.throughput(p, self.eoff, k, lam, dy)
            data_own = M.pix['d'][sl] - (model[sl] - norm * own)
            keep = M.use[sl] & np.isfinite(norm) & (norm > 0)
            out['u'].append(((y - M.centre(p, self.eoff, k, lam)) / dy)[keep])
            out['d'].append((data_own / norm)[keep])
            out['m'].append(own[keep])
            out['e'].append((np.sqrt(self.v_raw[sl]) / norm)[keep])
            out['lam'].append(lam[keep])
        return {k: (np.concatenate(v).astype('f4') if v else np.zeros(0, 'f4')) for k, v in out.items()}


def fit_star(task, ranges=None, optics=None):
    """A star in every range (``RANGES``; a grating's own, ``grating_ranges``, when the task is
    not PRISM): per range the start fit, then the fit with the width and the exposure offsets
    free on its pixels. Returns (record, {range: samples})."""
    exps = load_star(task)
    if ranges is None:
        ranges = RANGES if task.disperser == 'prism_clear' else grating_ranges(exps)
    flags = saturated_flags(exps)
    rec = dict(key=task.key, obs=task.obs, detector=task.detector, quadrant=task.quadrant, srcy=task.srcy,
               exposures=[e.name for e in exps], has_source=[bool(e.has_source) for e in exps], ranges=[])
    samp = {}
    for r, rng in enumerate(ranges):
        try:
            RF = RangeFit(exps, rng, optics=optics)
            s = RF.start()
            g = RF.fit(x0=s['_x'], use=s['_use'])
        except PF.ProfileFitFallback:
            continue
        shifts = RF.M.nod_shift if RF.M.nod_shift is not None else np.zeros(len(exps))
        rec['ranges'].append(dict(range=r, lam=list(rng), peak_snr=g['peak_snr'], c_node=g['c_node'], e_c=g['e_c'],
                                  w=g['w'], chi2=g['chi2'], ndof=g['ndof'], offsets=g['offsets'],
                                  shifts=[float(v) for v in shifts],
                                  sat=saturated_fraction(exps, flags, g['c_node'], rng)))
        samp[r] = RF.samples(g)
    return rec, samp


# ---------------------------------------------------------------------------
# The stack test
# ---------------------------------------------------------------------------
def clean_keys(records, snr_min=SNR_MIN, edge_max=EDGE_MAX, sat_max=SAT_MAX, w_max=W_STAR_MAX,
               chi2_max=CHI2_MAX):
    """{(detector, range): [star keys]} of the star-ranges the stack test uses: peak S/N at
    least ``snr_min``, the centre within ``edge_max`` of the shutter centre, no saturated pixel
    on the trace; of stars whose median width over their ranges is at most ``w_max`` (a
    galaxy, a pair or a contaminated star fits wider), and with chi2/dof at most ``chi2_max``."""
    out = {}
    for rec in records:
        if not rec['ranges'] or np.median([row['w'] for row in rec['ranges']]) > w_max:
            continue
        for row in rec['ranges']:
            if (row['peak_snr'] >= snr_min and abs(row['c_node']) < edge_max and row['sat'] <= sat_max
                    and row['chi2'] <= chi2_max * row['ndof']):
                out.setdefault((rec['detector'], row['range']), []).append(rec['key'])
    return out


def _star_curves(samples_dir, keys, r, bins):
    """Per star: binned mean data and model (share of the star's light per pixel) against
    the distance from the trace centre. Returns (D, M) arrays (stars x bins) or None."""
    D, M = [], []
    for k in keys:
        fn = os.path.join(samples_dir, f'{k}.npz')
        if not os.path.exists(fn):
            continue
        z = np.load(fn)
        if f'r{r}_u' not in z:
            continue
        u, d, m = z[f'r{r}_u'], z[f'r{r}_d'], z[f'r{r}_m']
        idx = np.digitize(u, bins) - 1
        ok = (idx >= 0) & (idx < bins.size - 1) & np.isfinite(d) & np.isfinite(m)
        n = np.bincount(idx[ok], minlength=bins.size - 1)
        with np.errstate(invalid='ignore', divide='ignore'):
            D.append(np.where(n >= 3, np.bincount(idx[ok], d[ok], minlength=bins.size - 1) / n, np.nan))
            M.append(np.where(n >= 3, np.bincount(idx[ok], m[ok], minlength=bins.size - 1) / n, np.nan))
    return (np.array(D), np.array(M)) if D else None


def stack_metric(samples_dir, keys_by, half=4.0, step=0.25):
    """Per (detector, range): the median over stars of (data - model) in % of the peak, binned
    in distance from the trace centre [rows]; its largest |value| and rms over |u| <= ``half``
    and the uncertainty of the median. Returns {(det, r): dict}."""
    bins = np.arange(-half, half + 1e-9, step)
    out = {}
    for (det, r), keys in keys_by.items():
        c = _star_curves(samples_dir, keys, r, bins)
        if c is None or c[0].shape[0] < 3:
            continue
        D, Mm = c
        with np.errstate(invalid='ignore'):
            pk = np.nanmax(np.nanmedian(Mm, axis=0))
            R = 100 * (D - Mm) / pk
            med = np.nanmedian(R, axis=0)
            n = np.sum(np.isfinite(R), axis=0)
            err = 1.2533 * np.nanstd(R, axis=0) / np.sqrt(np.maximum(n, 1))
        ok = np.isfinite(med)
        out[(det, r)] = dict(max=float(np.nanmax(np.abs(med))), rms=float(np.sqrt(np.nanmean(med[ok] ** 2))),
                             n=int(D.shape[0]), centre=(0.5 * (bins[:-1] + bins[1:])).tolist(), curve=med.tolist(),
                             err=err.tolist(), model=np.nanmedian(Mm, axis=0).tolist())
    return out


def _stack_one(args):
    task, out_dir, optics_path = args
    optics = None
    if optics_path:
        from campfire_pipeline.nirspec import optics as O
        optics = {d: o for d, o in O.load_optics(optics_path).items() if d != 'version'}
    try:
        rec, samp = fit_star(task, optics=optics)
    except Exception as err:          # one star's failure is reported, the others go on
        return task.key, f'{type(err).__name__}: {err}'
    with open(os.path.join(out_dir, 'records', f'{task.key}.json'), 'w') as fh:
        json.dump(rec, fh)
    if samp:
        np.savez_compressed(os.path.join(out_dir, 'samples', f'{task.key}.npz'),
                            **{f'r{r}_{k}': v for r, s in samp.items() for k, v in s.items()})
    return task.key, len(rec['ranges'])


def step_stack(root, observations, out_dir, dispersers=('prism_clear',), optics_path=None, processes=1, log=print):
    """Every star of the observations through ``fit_star``: records under ``out_dir``/records,
    stacking samples under ``out_dir``/samples."""
    from multiprocessing import Pool
    os.makedirs(os.path.join(out_dir, 'records'), exist_ok=True)
    os.makedirs(os.path.join(out_dir, 'samples'), exist_ok=True)
    tasks = tasks_for(root, observations, dispersers)
    log(f'{len(tasks)} stars')
    args = [(t, out_dir, optics_path) for t in tasks]
    if processes <= 1:
        for key, res in map(_stack_one, args):
            log(f'{key} {res}')
        return
    with Pool(processes) as pool:
        for key, res in pool.imap_unordered(_stack_one, args):
            log(f'{key} {res}')


def read_records(out_dir):
    d = os.path.join(out_dir, 'records')
    return [json.load(open(os.path.join(d, f))) for f in sorted(os.listdir(d)) if f.endswith('.json')]


def step_score(out_dir, log=print, **cuts):
    """The stack test of ``out_dir`` (``step_stack``): per detector and range the clean stars
    and the largest |median residual| in % of the peak."""
    recs = read_records(out_dir)
    keys = clean_keys(recs, **cuts)
    met = stack_metric(os.path.join(out_dir, 'samples'), keys)
    lam = {(rec['detector'], row['range']): row['lam'] for rec in recs for row in rec['ranges']}
    for k in sorted(met):
        a, b = lam[k]
        log(f'{k[0]} {a:.2f}-{b:.2f} um: {met[k]["n"]} stars, worst |median residual| {met[k]["max"]:.2f} % of the '
            f'peak, rms {met[k]["rms"]:.2f} %')
    return met


def parse_groups(text):
    """'name=prefix,name=prefix' -> {name: prefix} (star keys start with the observation name)."""
    return dict(item.split('=', 1) for item in text.split(',') if item) if text else {}


def step_score_groups(out_dir, groups, log=print, **cuts):
    """The stack test per dataset (``groups``: {name: key prefix}): per detector and range,
    each group's clean stars, its largest |median residual| and the offset [rows] where it
    falls; a model can pass on all stars while one dataset does not."""
    keys = clean_keys(read_records(out_dir), **cuts)
    met = {g: stack_metric(os.path.join(out_dir, 'samples'),
                           {k: [s for s in v if s.startswith(p)] for k, v in keys.items()}) for g, p in groups.items()}
    for det in ('NRS1', 'NRS2'):
        for r, (a, b) in enumerate(RANGES):
            cells = []
            for g in groups:
                m = met[g].get((det, r))
                if m is None:
                    cells.append(f'{g} -')
                    continue
                c = np.array(m['curve'])
                i = int(np.nanargmax(np.abs(c)))
                cells.append(f'{g} {m["n"]:3d} {c[i]:+.2f} % at {m["centre"][i]:+.2f}')
            log(f'{det} {a:.1f}-{b:.1f} um: ' + '; '.join(cells))
    return met


def _chi2_rows(out_dir, edge_max=EDGE_MAX):
    out = {}
    for rec in read_records(out_dir):
        for rg in rec['ranges']:
            if rg.get('chi2') is None or not np.isfinite(rg['chi2']) or rg.get('sat', 0) > 0 \
                    or abs(rg.get('c_node', np.inf)) > edge_max:
                continue
            out[(rec['key'], rg['range'])] = (rec['detector'], rg['range'], rg['chi2'])
    return out


def step_compare(dir_a, dir_b, groups=None, log=print):
    """Two stack runs of the same stars (two models): per group of stars (``groups``, {name:
    key prefix}; all stars when None), detector and range, the summed chi2 over the star-ranges
    both fitted (|centre| <= EDGE_MAX, unsaturated) and the median of each star-range's ratio
    b / a. Returns {(group, det, range): (n, chi2 a, chi2 b, median ratio)}."""
    a, b = _chi2_rows(dir_a), _chi2_rows(dir_b)
    common = sorted(set(a) & set(b))
    out = {}
    for g, prefix in (groups or {'all': ''}).items():
        for det in ('NRS1', 'NRS2'):
            for r, (lo, hi) in enumerate(RANGES):
                ks = [k for k in common if k[0].startswith(prefix) and a[k][0] == det and a[k][1] == r]
                if len(ks) < 3:
                    continue
                ca, cb = sum(a[k][2] for k in ks), sum(b[k][2] for k in ks)
                ratio = float(np.median([b[k][2] / a[k][2] for k in ks]))
                out[(g, det, r)] = (len(ks), ca, cb, ratio)
                log(f'{g} {det} {lo:.1f}-{hi:.1f} um: {len(ks):4d} star-ranges, chi2 {ca:10.0f} -> {cb:10.0f} '
                    f'({100 * (cb / ca - 1):+5.1f} %), median ratio {ratio:.3f}')
    return out


# ---------------------------------------------------------------------------
# The kernel before the shutter
# ---------------------------------------------------------------------------
_HALF = (0.0, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.65, 0.8, 1.0, 1.25, 1.5, 2.0)
DELTA = np.array(sorted({-d for d in _HALF} | set(_HALF)))     # kernel nodes [slit units]
FINE = np.round(np.arange(-2.0, 2.0001, 0.0125), 5)          # offsets the point images are taken at (the tables' step)
FREE_W_SNR = 63.0                  # star-ranges at this peak S/N or above fit their own width; fainter ones are points
S_DELTA = 1e-2                     # the solve's penalty on the wing's second differences (relative to the data)
WING = 0.3                         # the penalty applies beyond this offset [slit units]; the core is free
NOD_EXPOSURES = 3                  # a star's weight in the solve (per_star): its normals x this / its exposures


def _hat():
    """FINE x DELTA: a kernel linear between its nodes, as weights on FINE, times the step."""
    H = np.zeros((FINE.size, DELTA.size))
    for i, x in enumerate(FINE):
        j = int(np.clip(np.searchsorted(DELTA, x) - 1, 0, DELTA.size - 2))
        t = (x - DELTA[j]) / (DELTA[j + 1] - DELTA[j])
        H[i, j] += 1 - t
        H[i, j + 1] += t
    return H * (FINE[1] - FINE[0])


HAT = _hat()


def gauss_matrix(grid, w):
    """(n x n): a Gaussian source of width ``w`` centred at each point of the evenly spaced
    ``grid``, as weights on the grid, exact for values linear between the points (the
    profile fit's compact source, ``optics.Optics._compact_weights``)."""
    from scipy.special import ndtr
    h = grid[1] - grid[0]
    u = grid[:, None] - grid[None, :]

    def R(x):
        z = x / w
        return x * ndtr(z) + w * np.exp(-0.5 * z * z) / np.sqrt(2.0 * np.pi)
    return (R(u + h) - 2.0 * R(u) + R(u - h)) / h


def kernel_response(M, p, f, eoff, width=None):
    """(npix x DELTA) the model's pixels per unit kernel weight at each node: the model ``M``
    at shape ``p``, offsets ``eoff`` and spectrum ``f`` (on M.lam_grid), with each exposure's
    point images taken from the diffraction alone (``Optics.raw_image_table``) at the source
    centre plus FINE, spread by the source's own width (``width``; None or the floor: a point
    source), through the nod-difference terms and M's throughput. The model is linear in the
    kernel, so R k is the model with kernel k."""
    from scipy.ndimage import map_coordinates
    from campfire_pipeline.nirspec import optics as O
    H = HAT if width is None or width <= 1.5 * PF.W_FLOOR else gauss_matrix(FINE, width) @ HAT
    G = {}
    for j, q in M.image_pix.items():
        e = M.exps[j]
        tab = M.optics[j].raw_image_table(e.open_k)
        c = M.centre(p, eoff, j, q['lam'])
        tau = M.throughput(p, eoff, j, q['lam'], q['dy'])
        li, yi = O.Optics._li(q['lam']), (q['y'] - O.Y[0]) / (O.Y[1] - O.Y[0])
        img = np.empty((q['lam'].size, FINE.size))
        for m, dd in enumerate(FINE):
            img[:, m] = map_coordinates(tab, np.vstack([li, O.Optics._ci(c + dd), yi]), order=1, mode='constant',
                                        cval=0.0)
        G[j] = (tau * q['dy'] / O.DY_ROW)[:, None] * (img @ H)
    R = np.zeros((M.npix, DELTA.size))
    for T in M.terms:
        g = T['w'] * G[T['j']][T['upos']]
        fv = f[T['idx']] * (1 - T['t']) + f[T['idx'] + 1] * T['t']
        np.add.at(R, T['rows'], np.where(T['inside'][:, None], g * fv[:, None], 0.0))
    return R


def _add_normal(out, key, R, d, v, use):
    ok = use & np.isfinite(d) & np.isfinite(v) & (v > 0) & np.all(np.isfinite(R), axis=1)
    if ok.sum() < 20:
        return
    w = 1.0 / v[ok]
    A = R[ok]
    AtA, Atd, n = out.get(key, (0.0, 0.0, 0))
    out[key] = (AtA + A.T @ (A * w[:, None]), Atd + A.T @ (w * d[ok]), n + int(ok.sum()))


def kernel_star(task, ranges, optics=None, free_w_snr=FREE_W_SNR):
    """One star's normal equations for the kernel in each of its ``ranges`` (indices into
    RANGES): the range fitted as the stack test fits it (``RangeFit.start``, then centre,
    drift and exposure offsets free, the width free only at peak S/N >= ``free_w_snr``), then
    ``kernel_response`` at that state for the nod differences and for the single frames less
    the master background (which keep the light nodding removes; same shape, offsets and
    spectrum, the same variance floor, pixels beyond CLIP sigma of the model dropped).
    Returns {range: (AtA, Atd, npix)}, {range: width}, the nod differences' part alone and the
    number of exposures that hold the star."""
    from campfire_pipeline.nirspec import mbkg_extraction as MB
    exps = load_star(task)
    sci = [e for e in exps if e.use_data]
    try:
        frames = MB.frames_for([e.path for e in sci])
    except MB.NoMasterBackground:
        frames = None
    out, nod, widths = {}, {}, {}
    for r in ranges:
        rng = RANGES[r]
        try:
            RF = RangeFit(exps, rng, optics=optics)
            s = RF.start()
            g = RF.fit(x0=s['_x'], use=s['_use'], width=s['peak_snr'] >= free_w_snr)
        except PF.ProfileFitFallback:
            continue
        M = RF.M
        p, eoff, f = RF.shape(g['c0'], g['drift']), RF.eoff.copy(), np.nan_to_num(g['_f'])
        widths[r] = g['w']
        Rn = kernel_response(M, p, f, eoff, g['w'])
        _add_normal(out, r, Rn, M.pix['d'], M.pix['v'], g['_use'])
        _add_normal(nod, r, Rn, M.pix['d'], M.pix['v'], g['_use'])
        if frames is None:
            continue
        mexps = restrict(MB.single_frame_exposures(sci, frames), rng)
        opt = None if optics is None else [optics[e.detector] for e in mexps]
        Mm = PF.ProfileModel(mexps, grid=M.lam_grid, centre_law_tables=PF.NO_CENTRE_LAW, c_prior=M.c_prior, optics=opt)
        Mm.pivot = M.pivot
        off = {e.name: eoff[k] for k, e in enumerate(M.exps)}
        em = np.array([off.get(e.name, 0.0) for e in Mm.exps])
        mm = Mm.design(p, em) @ f
        colkey = np.concatenate([k * 100000 + np.nonzero(msk)[1] for k, msk in Mm.pix_masks])
        u, inv = np.unique(colkey, return_inverse=True)
        mx = np.zeros(u.size)
        np.maximum.at(mx, inv, np.abs(mm))
        vf = Mm.pix['v'] + (mx[inv] / SN_CAP) ** 2
        use = np.abs(Mm.pix['d'] - mm) < CLIP * np.sqrt(vf)
        _add_normal(out, r, kernel_response(Mm, p, f, em, g['w']), Mm.pix['d'], vf, use)
    return out, widths, nod, int(sum(bool(e.has_source) for e in exps))


def _kernel_one(args):
    task, ranges, out_dir, optics_path = args
    optics = None
    if optics_path:
        from campfire_pipeline.nirspec import optics as O
        optics = {d: o for d, o in O.load_optics(optics_path).items() if d != 'version'}
    try:
        out, widths, nod, nexp = kernel_star(task, ranges, optics=optics)
    except Exception as err:          # one star's failure is reported, the others go on
        return task.key, f'{type(err).__name__}: {err}'
    parts = {'': out, 'nod_': nod}    # every term, and the nod differences alone (what the stack test measures)
    np.savez(os.path.join(out_dir, 'kernel', f'{task.key}.npz'), widths=json.dumps(widths), detector=task.detector,
             nexp=nexp,
             **{f'r{r}_{p}AtA': a for p, o in parts.items() for r, (a, b, n) in o.items()},
             **{f'r{r}_{p}Atd': b for p, o in parts.items() for r, (a, b, n) in o.items()},
             **{f'r{r}_{p}n': n for p, o in parts.items() for r, (a, b, n) in o.items()})
    return task.key, len(out)


def step_kernel(root, observations, select_dir, out_dir, optics_path=None, processes=1, log=print):
    """The kernel's normal equations of every clean star-range of a stack run (``select_dir``,
    ``clean_keys``), at the model ``optics_path`` (the shipped one by default): one file per
    star under ``out_dir``/kernel."""
    from multiprocessing import Pool
    from campfire_pipeline.nirspec import optics as O
    os.makedirs(os.path.join(out_dir, 'kernel'), exist_ok=True)
    ranges_of = {}
    for (det, r), keys in clean_keys(read_records(select_dir)).items():
        for k in keys:
            ranges_of.setdefault(k, []).append(r)
    tasks = [t for t in tasks_for(root, observations) if t.key in ranges_of]
    log(f'{len(tasks)} stars, {sum(len(ranges_of[t.key]) for t in tasks)} star-ranges')
    model = O.load_optics(optics_path) if optics_path else O.models()
    for det in ('NRS1', 'NRS2'):      # the shared tables once, before the workers
        for mask in ((-1, 0, 1), (0, 1, 2), (-2, -1, 0)):
            model[det].raw_image_table(mask)
            model[det].image_table(mask)
            model[det].transmission_table(PF.REF_APERTURE)
    args = [(t, sorted(ranges_of[t.key]), out_dir, optics_path) for t in sorted(tasks, key=lambda t: t.key)]
    if processes <= 1:
        for key, res in map(_kernel_one, args):
            log(f'{key} {res}')
        return
    with Pool(processes) as pool:
        for key, res in pool.imap_unordered(_kernel_one, args):
            log(f'{key} {res}')


def sum_normals(kernel_dir, keys=None, part='', per_star=True):
    """{(detector, range): (AtA, Atd, npix)} summed over the stars' files (``step_kernel``),
    or over ``keys`` only; every term (``part`` '') or the nod differences alone ('nod_').
    ``per_star``: each star's normals times NOD_EXPOSURES / its number of exposures, so a star
    counts once however often it was observed (1133's slit grid: 60 exposures a star; weighted
    by pixels, a few 1133 stars with a neighbor set the kernel's wing for every star); False
    weights every pixel alike."""
    tot = {}
    for fn in sorted(os.listdir(kernel_dir)):
        key = fn[:-4]
        if not fn.endswith('.npz') or (keys is not None and key not in keys):
            continue
        z = np.load(os.path.join(kernel_dir, fn))
        det = str(z['detector'])
        w = NOD_EXPOSURES / max(int(z['nexp']), 1) if per_star else 1.0
        for name in z.files:
            head, _, tail = name.partition('_')
            if tail != f'{part}AtA':
                continue
            r = int(head[1:])
            AtA, Atd, n = tot.get((det, r), (0.0, 0.0, 0))
            tot[(det, r)] = (AtA + w * z[name], Atd + w * z[f'r{r}_{part}Atd'], n + int(z[f'r{r}_{part}n']))
    return tot


def solve_kernel(AtA, Atd, s_delta=S_DELTA):
    """The non-negative kernel minimising chi2 + s_delta x (the data's scale) x |second
    differences beyond WING|^2."""
    from scipy.linalg import cholesky, solve_triangular
    from scipy.optimize import lsq_linear
    n = DELTA.size
    D2 = np.diff(np.eye(n), n=2, axis=0)[np.abs(DELTA[1:-1]) > WING]
    scale = np.trace(AtA) / n
    U = cholesky(AtA + s_delta * scale * D2.T @ D2 + 1e-9 * scale * np.eye(n), lower=False)
    return lsq_linear(U, solve_triangular(U, Atd, trans='T', lower=False), bounds=(0.0, np.inf), method='bvls').x


def step_solve(kernel_dir, optics_path, out_path, version, keys=None, s_delta=S_DELTA, part='', per_star=True,
               log=print):
    """A model file from the stars' normal equations (``part``, ``per_star``: see
    ``sum_normals``): the kernel per detector and range (at the range's centre, ln-mean), with
    everything else (diffraction numbers, ``x_loss``, shutter edges) from the model the normals
    were built at (``optics_path``; the shipped file by default)."""
    from campfire_pipeline.nirspec import optics as O
    with open(optics_path or O.OPTICS_FILE) as fh:
        base = json.load(fh)
    tot = sum_normals(kernel_dir, keys, part, per_star)
    out = {'meta': dict(base['meta'], version=version,
                        source=f'kernel fitted by optics_calibration from {kernel_dir} at the model '
                               f'{base["meta"]["version"]}')}
    out.update({k: v for k, v in base.items() if k not in ('meta', 'NRS1', 'NRS2')})
    for det in ('NRS1', 'NRS2'):
        nodes = []
        for r in sorted(r for d, r in tot if d == det):
            AtA, Atd, n = tot[(det, r)]
            k = solve_kernel(AtA, Atd, s_delta)
            wk = HAT.sum(axis=0) * k
            lam = float(np.sqrt(RANGES[r][0] * RANGES[r][1]))
            nodes.append(dict(lam=lam, delta=DELTA.tolist(), k=k.tolist()))
            log(f'{det} {lam:.2f} um: integral {wk.sum():.4f}, beyond 0.3 {wk[np.abs(DELTA) > 0.3].sum() / wk.sum():.4f}, '
                f'beyond 0.8 {wk[np.abs(DELTA) > 0.8].sum() / wk.sum():.4f}, {n} pixels')
        out[det] = nodes
    with open(out_path, 'w') as fh:
        json.dump(out, fh, indent=1)
    log(f'wrote {out_path}')
    return out


# ---------------------------------------------------------------------------
# The centre law and the nod law (profile_fit.CENTRE_LAW, NOD_OFFSET_LAW)
# ---------------------------------------------------------------------------
REF_RANGES = (1, 2, 3)             # 1.0-2.6 um: a star's reference centre
CENTRE_MIN_STARS = 5               # per MSA quadrant for its own drift column


def centre_rows(records, keys_by):
    """Per star with a clean reference range: detector, quadrant, observation, its reference
    centre (inverse-variance mean of the clean ranges in REF_RANGES) and each clean range's
    centre change from it, with errors (NaN where not clean)."""
    clean = {(k, r) for (det, r), keys in keys_by.items() for k in keys}
    out = []
    for rec in records:
        rows = {row['range']: row for row in rec['ranges'] if (rec['key'], row['range']) in clean
                and np.isfinite(row.get('e_c', np.nan)) and row['e_c'] > 0}
        ref = [rows[r] for r in REF_RANGES if r in rows]
        if not ref:
            continue
        w = np.array([1.0 / max(x['e_c'], 1e-3) ** 2 for x in ref])
        cref = float(np.sum(w * [x['c_node'] for x in ref]) / w.sum())
        dc, err = np.full(len(RANGES), np.nan), np.full(len(RANGES), np.nan)
        for r, x in rows.items():
            dc[r], err[r] = x['c_node'] - cref, x['e_c']
        out.append(dict(key=rec['key'], obs=rec['obs'], det=rec['detector'], quad=rec['quadrant'], cref=cref, dc=dc,
                        err=err))
    return out


def _robust_lsq(X, y, e, nit=4):
    """Weighted least squares with 4-sigma clipping (errors floored at 0.003): (parameters,
    errors, robust scatter, number used)."""
    ok = np.isfinite(y) & np.isfinite(e)
    p = np.full(X.shape[1], np.nan)
    A = b = None
    for _ in range(nit):
        if ok.sum() <= X.shape[1]:
            break
        w = 1 / np.maximum(e[ok], 0.003) ** 2
        A, b = X[ok] * np.sqrt(w)[:, None], y[ok] * np.sqrt(w)
        p, *_ = np.linalg.lstsq(A, b, rcond=None)
        res = y - X @ p
        s = 1.4826 * np.nanmedian(np.abs(res[ok]))
        ok = ok & (np.abs(res) < 4 * max(s, 0.004))
    if A is None:
        return p, np.full(X.shape[1], np.nan), np.nan, 0
    cov = np.linalg.pinv(A.T @ A) * max(np.sum((A @ p - b) ** 2) / max(ok.sum() - len(p), 1), 1)
    res = y - X @ p
    return p, np.sqrt(np.diag(cov)), float(1.4826 * np.nanmedian(np.abs(res[ok]))), int(ok.sum())


def centre_law_fit(rows):
    """``CENTRE_LAW`` from star centres (``centre_rows``): per detector and range, the centre
    change dc = D_quadrant + alpha x the star's reference centre (one drift column per MSA
    quadrant with at least CENTRE_MIN_STARS stars, alpha shared), and the detector's own D from
    every star (the fallback). Values at the ranges' centres; unmeasured entries NaN."""
    lam = [0.5 * (a + b) for a, b in RANGES]
    out = {}
    for det in ('NRS1', 'NRS2'):
        g = [x for x in rows if x['det'] == det]
        quads = [q for q in (1, 2, 3, 4) if sum(x['quad'] == q for x in g) >= CENTRE_MIN_STARS]
        out[det] = dict(lam=lam, D=[], alpha=[], eD=[], ealpha=[], scatter=[], n=[])
        for q in quads:
            out[f'Q{q}_{det}'] = dict(lam=lam, D=[], eD=[])
        for r in range(len(RANGES)):
            pos = np.array([x['cref'] for x in g])
            y, e = np.array([x['dc'][r] for x in g]), np.array([x['err'][r] for x in g])
            p0, pe0, sc, n = _robust_lsq(np.column_stack([np.ones_like(pos), pos]), y, e)
            out[det]['D'].append(float(p0[0]))
            out[det]['eD'].append(float(pe0[0]))
            out[det]['scatter'].append(sc)
            out[det]['n'].append(n)
            have = [q for q in quads if np.sum([(x['quad'] == q) and np.isfinite(v) for x, v in zip(g, y)])
                    >= CENTRE_MIN_STARS]
            if not have:
                out[det]['alpha'].append(float(p0[1]))
                out[det]['ealpha'].append(float(pe0[1]))
                for q in quads:
                    out[f'Q{q}_{det}']['D'].append(np.nan)
                    out[f'Q{q}_{det}']['eD'].append(np.nan)
                continue
            sub = np.array([x['quad'] in have for x in g])
            X = np.column_stack([np.array([x['quad'] == q for x in g], float) for q in have] + [pos])
            p, pe, _, _ = _robust_lsq(X[sub], y[sub], e[sub])
            out[det]['alpha'].append(float(p[-1]))
            out[det]['ealpha'].append(float(pe[-1]))
            for q in quads:
                i = have.index(q) if q in have else None
                out[f'Q{q}_{det}']['D'].append(float(p[i]) if i is not None else np.nan)
                out[f'Q{q}_{det}']['eD'].append(float(pe[i]) if i is not None else np.nan)
    return out


def law_table(fit):
    """A ``centre_law_fit`` result as a ``profile_fit.centre_law`` table: unmeasured quadrant
    values take the detector's D."""
    out = {}
    for name, v in fit.items():
        det = name.split('_')[-1]
        D = np.array(v['D'], float)
        D = np.where(np.isfinite(D), D, np.array(fit[det]['D'], float))
        out[name] = dict(lam=np.array(v['lam']), D=D)
        if 'alpha' in v:
            out[name]['alpha'] = np.array(v['alpha'], float)
    return out


def centre_law_residuals(rows, law):
    """Per row and range: dc minus the law's prediction (``profile_fit.centre_law`` with
    ``law``; None for no law) relative to its value at the reference centre, as the fit
    applies it."""
    lam = np.array([0.5 * (a + b) for a, b in RANGES])
    res = np.full((len(rows), len(RANGES)), np.nan)
    for i, x in enumerate(rows):
        if law is None:
            pred = np.zeros(lam.size)
        else:
            D, a = PF.centre_law(lam, x['det'], x['quad'], law=law)
            pred = D + a * x['cref']
            ref = [r for r in REF_RANGES if np.isfinite(x['dc'][r])]
            pred = pred - np.mean(pred[ref])
        res[i] = x['dc'] - pred
    return res


def nod_rows(records, keys_by):
    """Per star and exposure with the source (but the first, whose offset is held at zero):
    its observation, planned shift from the first (``nod_shifts`` difference) and fitted extra
    offset, medians over the star's clean ranges."""
    clean = {(k, r) for (det, r), keys in keys_by.items() for k in keys}
    out = []
    for rec in records:
        rows = [row for row in rec['ranges'] if (rec['key'], row['range']) in clean and 'offsets' in row]
        src = [k for k, h in enumerate(rec.get('has_source', [])) if h]
        if not rows or len(src) < 2:
            continue
        for k in src[1:]:
            plan = np.median([row['shifts'][k] - row['shifts'][src[0]] for row in rows])
            off = np.median([row['offsets'][k] for row in rows])
            out.append(dict(key=rec['key'], obs=rec['obs'], plan=float(plan), off=float(off)))
    return out


def step_laws(out_dir, log=print):
    """The centre law and the nod law from a stack run's clean star-ranges (``step_stack``):
    the new law, the scatter of the centre changes with no law / the shipped law / the new
    law fitted on the other observations (held out by observation), and the nod offsets with
    and without the planned shifts, with the fitted scale of the planned shifts per
    observation. Writes ``out_dir``/laws.json."""
    recs = read_records(out_dir)
    keys = clean_keys(recs)
    rows = centre_rows(recs, keys)
    fit = centre_law_fit(rows)
    obs = sorted({x['obs'] for x in rows})
    held = np.full((len(rows), len(RANGES)), np.nan)
    for o in obs:
        law_o = law_table(centre_law_fit([x for x in rows if x['obs'] != o]))
        idx = [i for i, x in enumerate(rows) if x['obs'] == o]
        held[idx] = centre_law_residuals([rows[i] for i in idx], law_o)
    cases = dict(none=centre_law_residuals(rows, None), shipped=centre_law_residuals(rows, PF.CENTRE_LAW),
                 new=centre_law_residuals(rows, law_table(fit)), new_held_out=held)
    det = np.array([x['det'] for x in rows])
    scat = {}
    for name, res in cases.items():
        for d in ('NRS1', 'NRS2'):
            s = res[det == d]
            scat[f'{name}:{d}'] = [float(1.4826 * np.nanmedian(np.abs(c[np.isfinite(c)]))) if np.isfinite(c).sum() > 5
                                   else np.nan for c in s.T]
    for d in ('NRS1', 'NRS2'):
        log(f'centre change scatter, {d} (slit units, per range 0.7-5.3 um; {int(np.sum(det == d))} stars):')
        for name in cases:
            log(f'  {name:13s} ' + ' '.join(f'{v:.4f}' for v in scat[f'{name}:{d}']))
        log('  new D  ' + ' '.join(f'{v:+.4f}' for v in fit[d]['D']))
        log('  new a  ' + ' '.join(f'{v:+.4f}' for v in fit[d]['alpha']))
    nods = nod_rows(recs, keys)
    plan, off = np.array([x['plan'] for x in nods]), np.array([x['off'] for x in nods])
    rms = lambda v: float(np.sqrt(np.mean(v ** 2)))      # noqa: E731
    nod = dict(n=len(nods), rms_with_plan=rms(off), rms_without=rms(off + plan), per_obs={})
    log(f'nod offsets ({len(nods)} star-exposures): rms {nod["rms_with_plan"]:.4f} with the planned shifts, '
        f'{nod["rms_without"]:.4f} without')
    for o in sorted({x['obs'] for x in nods}):
        s = np.array([x['obs'] == o for x in nods])
        if np.sum(s) < 5 or np.ptp(plan[s]) < 1e-3:
            continue
        slope = float(np.sum(plan[s] * off[s]) / np.sum(plan[s] ** 2))     # off = (scale - 1) x plan
        nod['per_obs'][o] = dict(n=int(s.sum()), scale=1.0 + slope, rms=rms(off[s]))
        log(f'  {o}: {int(s.sum())} star-exposures, scale measured / planned {1.0 + slope:.3f}, rms {rms(off[s]):.4f}')
    with open(os.path.join(out_dir, 'laws.json'), 'w') as fh:
        json.dump(dict(centre_law=fit, scatter=scat, nod=nod), fh, indent=1)
    return fit, scat, nod


# ---------------------------------------------------------------------------
# The along-dispersion correction (optics.Optics.x_correction)
# ---------------------------------------------------------------------------
X_LAM0 = 2.0                       # um: below it jwst's x dependence stands (no trend in the stars)


def fit_x_loss(pairs, lam0=X_LAM0, nit=4):
    """b of ``Optics.x_correction`` from stars seen at several positions: ``pairs`` of a
    star's flux in a band in one visit over its reference visit (dict lnr = ln ratio, lam =
    the band's centre [um], xv / xr = SRCXPOS of the visit and of the reference, band = an
    id), fluxes with jwst's x dependence. The corrected ratio is lnr + b (lam - lam0)+ (xv^2 -
    xr^2); b by least squares, clipped at 4 sigma per band. Returns b."""
    lnr = np.array([p['lnr'] for p in pairs], float)
    band = np.array([p['band'] for p in pairs])
    g = np.array([max(p['lam'] - lam0, 0.0) * (p['xv'] ** 2 - p['xr'] ** 2) for p in pairs], float)
    keep = np.ones(lnr.size, bool)
    b = 0.0
    for _ in range(nit):
        b = -np.sum(lnr[keep] * g[keep]) / np.sum(g[keep] ** 2)
        v = lnr + b * g
        keep = np.zeros(lnr.size, bool)
        for bi in np.unique(band):
            s = band == bi
            mad = 1.4826 * np.median(np.abs(v[s] - np.median(v[s])))
            keep[s] = np.abs(v[s] - np.median(v[s])) < 4 * max(mad, 1e-3)
    return float(b)


# ---------------------------------------------------------------------------
# The same star at several positions (visits / repeat) and the bar shadow (barshadow)
# ---------------------------------------------------------------------------
VISIT_BANDS = ((0.7, 1.0), (1.0, 1.5), (1.5, 2.5), (2.5, 3.5), (3.5, 5.0))   # PRISM; a grating: four of its own
REPEAT_SNR = 20.0                  # a band enters when its S/N (flux over error) is at least this
REPEAT_SAT = 0.01                  # ... and at most this share of its trace pixels is saturated
REPEAT_EDGE = 0.25                 # |SRCYPOS| of an edge visit
X_BINS = (-0.5, -0.35, -0.2, -0.07, 0.07, 0.2, 0.35, 0.5)


def band_fluxes(lam, flux, err, bands, min_points=5):
    """Per band: (inverse-variance mean flux, its error, median per-point S/N), NaN with fewer
    than ``min_points`` good points."""
    lam, f, e = np.asarray(lam, float), np.asarray(flux, float), np.asarray(err, float)
    ok = np.isfinite(f) & np.isfinite(e) & (e > 0)
    out = []
    for a, b in bands:
        m = ok & (lam >= a) & (lam < b)
        if m.sum() < min_points:
            out.append((np.nan, np.nan, np.nan))
            continue
        w = 1.0 / e[m] ** 2
        out.append((float(np.sum(w * f[m]) / np.sum(w)), float(1.0 / np.sqrt(np.sum(w))), float(np.median(f[m] / e[m]))))
    return out


def fit_visit(task):
    """One visit of a star (``StarTask``: one detector of one configuration, or one background
    group of a scan) through the profile fit as stage 3 runs it (``PF.fit``, the model in use:
    the shipped one or the file ``CAMPFIRE_NIRSPEC_OPTICS`` names): band fluxes, the fitted
    centre and width, chi2/dof, the planned position (median SRCXPOS / SRCYPOS of its science
    exposures) and the saturated share of each band. Errors are returned, not raised."""
    head = dict(key=task.key, obs=task.obs, sid=task.source_id, det=task.detector, group=task.group,
                config=task.config, srcx=task.srcx, srcy=task.srcy)
    try:
        exps = load_star(task)
        bands = VISIT_BANDS if task.disperser == 'prism_clear' else grating_ranges(exps, n=4)
        R = PF.fit(exps)
        flags = saturated_flags(exps)
        return dict(head, bands=[list(b) for b in bands], flux=band_fluxes(R.lam, R.flux, R.flux_err, bands),
                    sat=[saturated_fraction(exps, flags, R.params['centre'], b) for b in bands],
                    centre=float(R.params['centre']), width=float(R.params['width']), chi2r=float(R.chi2_red))
    except Exception as err:                       # noqa: BLE001 - one visit's failure is reported, the others go on
        return dict(head, error=f'{type(err).__name__}: {err}')


def step_visits(root, observations, out_path, disperser='prism_clear', by_group=False, optics_path=None,
                min_visits=2, processes=1, log=print):
    """Every visit of every star seen in ``min_visits`` or more (``fit_visit``), written to
    ``out_path`` (JSON: optics version, rows). A visit is one detector of one observation; with
    ``by_group`` one background group (a scan inside one observation: 1133's slit grid, 12497 obs
    4). ``optics_path``: a candidate model in place of the shipped one."""
    from multiprocessing import Pool
    from campfire_pipeline.nirspec import optics as O
    if optics_path:
        os.environ['CAMPFIRE_NIRSPEC_OPTICS'] = os.path.abspath(optics_path)    # the workers' PF.fit reads it
        O._MODELS.clear()
    tasks = tasks_for(root, observations, (disperser,), by_group=by_group)
    seen = {}
    for t in tasks:
        seen.setdefault(t.source_id, set()).add((t.obs, t.group, t.detector))
    tasks = [t for t in tasks if len(seen[t.source_id]) >= min_visits]
    log(f'{len(tasks)} visits of {len({t.source_id for t in tasks})} stars')
    rows = []
    with Pool(max(1, processes)) as pool:
        for r in pool.imap_unordered(fit_visit, tasks):
            rows.append(r)
            log(f"{r['key']} {r.get('error', 'ok')}")
    with open(out_path, 'w') as fh:
        json.dump(dict(optics=O.version(), x_loss=O.models()['NRS1'].x_loss, disperser=disperser, rows=rows), fh,
                  indent=1)
    return rows


def repeat_pairs(rows, along='y', snr_min=REPEAT_SNR, sat_max=REPEAT_SAT, exclude=()):
    """Per band: each visit of a star against the same star's most central visit along
    ``along`` ('y': SRCYPOS, the slit direction; 'x': SRCXPOS, along the dispersion) on the
    same detector (bands over two detectors cover different wavelengths), among the visits
    where the band has S/N >= ``snr_min`` and at most ``sat_max`` saturated. Returns
    [[dict(lnr, pos, ref_pos, sid)]] per band."""
    pos = 'srcy' if along == 'y' else 'srcx'
    by = {}
    for r in rows:
        if 'error' not in r and r['sid'] not in set(exclude) and np.isfinite(r[pos]):
            by.setdefault((r['sid'], r['det']), []).append(r)
    nb = max((len(r['bands']) for r in rows if 'bands' in r), default=0)
    out = [[] for _ in range(nb)]
    for vs in by.values():
        for bi in range(nb):
            ok = [v for v in vs if len(v['flux']) > bi and np.isfinite(v['flux'][bi][0]) and v['flux'][bi][0] > 0
                  and v['flux'][bi][0] / v['flux'][bi][1] >= snr_min and v['sat'][bi] <= sat_max]
            if len(ok) < 2:
                continue
            ref = min(ok, key=lambda v: abs(v[pos]))
            out[bi] += [dict(lnr=float(np.log(v['flux'][bi][0] / ref['flux'][bi][0])), pos=v[pos], ref_pos=ref[pos],
                             sid=v['sid'], lam=float(np.sqrt(v['bands'][bi][0] * v['bands'][bi][1])), band=bi)
                        for v in ok if v is not ref]
    return out


def step_repeat(visits_path, along='y', exclude=(), snr_min=REPEAT_SNR, log=print):
    """The repeat test on a ``visits`` file: is a star's flux the same wherever it sits? Per
    band, ln(flux / the star's most central visit) (``repeat_pairs``).

    along 'y' (stars in several configurations, check 1): the rms over every visit and over the
    edge visits (|SRCYPOS| >= REPEAT_EDGE), and the edge visits' median.
    along 'x' (a scan along the dispersion): the clipped rms, the medians in bins of SRCXPOS, the
    slope against x^2 - x_ref^2 (the model predicts none), and b of the along-dispersion
    correction refitted (``fit_x_loss``) on the fluxes with the file's correction undone."""
    with open(visits_path) as fh:
        d = json.load(fh)
    rows = d['rows']
    bad = [r for r in rows if 'error' in r]
    log(f'{len(rows) - len(bad)} visits fitted, {len(bad)} failed (optics {d.get("optics")})')
    pairs = repeat_pairs(rows, along=along, snr_min=snr_min, exclude=exclude)
    bands = next(r['bands'] for r in rows if 'bands' in r)
    out = []
    for bi, ps in enumerate(pairs):
        a, b = bands[bi]
        lnr = np.array([p['lnr'] for p in ps])
        if lnr.size < 5:
            log(f'{a}-{b} um: too few pairs ({lnr.size})')
            out.append(None)
            continue
        x = np.array([p['pos'] for p in ps])
        res = dict(band=(a, b), n=int(lnr.size))
        if along == 'y':
            edge = np.abs(x) >= REPEAT_EDGE
            res.update(rms=float(np.sqrt(np.mean(lnr ** 2))),
                       rms_edge=float(np.sqrt(np.mean(lnr[edge] ** 2))) if edge.any() else np.nan,
                       median_edge=float(np.median(lnr[edge])) if edge.any() else np.nan, n_edge=int(edge.sum()))
            log(f'{a}-{b} um: {lnr.size} pairs, rms {100 * res["rms"]:.1f} %; edge visits ({res["n_edge"]}) rms '
                f'{100 * res["rms_edge"]:.1f} %, median {100 * res["median_edge"]:+.1f} %')
        else:
            med = np.median(lnr)
            mad = 1.4826 * np.median(np.abs(lnr - med))
            keep = np.abs(lnr - med) < 4 * max(mad, 1e-3)
            g = x ** 2 - np.array([p['ref_pos'] for p in ps]) ** 2
            slope = float(np.sum(lnr[keep] * g[keep]) / np.sum(g[keep] ** 2))
            se = float(np.std(lnr[keep] - slope * g[keep]) / np.sqrt(np.sum(g[keep] ** 2)))
            binned = [(0.5 * (lo + hi), float(np.median(lnr[keep & (x >= lo) & (x < hi)]))
                       if np.sum(keep & (x >= lo) & (x < hi)) >= 3 else np.nan) for lo, hi in zip(X_BINS[:-1], X_BINS[1:])]
            res.update(rms=float(np.std(lnr[keep])), slope=slope, slope_err=se, binned=binned)
            log(f'{a}-{b} um: {lnr.size} pairs ({keep.sum()} kept), clipped rms {100 * res["rms"]:.1f} %, slope against '
                f'x^2 - x_ref^2 {slope:+.2f} +- {se:.2f}; median % by x: '
                + ', '.join(f'{c:+.2f} {100 * m:+.1f}' for c, m in binned))
        out.append(res)
    if along == 'x':
        xl = d.get('x_loss') or {}
        b0, lam0 = float(xl.get('b', 0.0)), float(xl.get('lam0', X_LAM0))
        # the fit divided the flux by the correction c = exp(-b0 (lam - lam0)+ x^2); times c again is jwst's alone
        raw = [dict(p, xv=p['pos'], xr=p['ref_pos'],
                    lnr=p['lnr'] - b0 * max(p['lam'] - lam0, 0.0) * (p['pos'] ** 2 - p['ref_pos'] ** 2))
               for ps in pairs for p in ps]
        if any(max(p['lam'] - lam0, 0.0) > 0 for p in raw):
            b = fit_x_loss(raw, lam0=lam0)
            log(f'along-dispersion correction refitted on these pairs: b = {b:.3f} (the file used {b0:.3f})')
            out.append(dict(b=b, b_used=b0))
    return out


def barshadow_misfit(grating, optics=None, n_lam=10, y_max=0.95):
    """The model's uniform light against the empirical bar-shadow table of ``grating``
    (``barshadow_calibration``; the light of every source position across the open shutters,
    equal weights, normalised at the centre, against the table for one open shutter and for a
    slitlet of three): the rms over |y| < ``y_max`` pitch of both, at ``n_lam`` of the table's
    wavelengths. The kernel only moves light between source positions, so for uniform light it
    cancels: this tests the diffraction numbers (``raw_image_table``), the same for both
    detectors. ``optics``: an ``Optics`` (default the shipped NRS1 one). Returns (lams, rms)."""
    from campfire_pipeline.config import get_empirical_barshadow_path
    from campfire_pipeline.nirspec import barshadow_calibration as B
    from campfire_pipeline.nirspec import optics as O
    tab = B.load_table(get_empirical_barshadow_path(grating))
    g = tab['grid']
    y_t = g.y0 + g.dy * np.arange(g.n_y)                    # pitches
    lam_t = (g.w0 + g.dw * np.arange(g.n_w)) * 1e6
    good = [j for j in range(g.n_w) if np.isfinite(tab['data1x1'][:, j]).all() and np.isfinite(tab['data1x3'][:, j]).all()]
    cols = good[::max(1, len(good) // n_lam)]
    sel = np.abs(y_t) < y_max
    yy = y_t[sel] * O.PITCH_AS / O.SLIT                     # slit units
    opt = optics or O.optics_for('NRS1')
    uniform = {mask: np.asarray(opt.raw_image_table(mask), float).sum(axis=1) for mask in ((0,), (-1, 0, 1))}
    rms = []
    for j in cols:
        li = np.interp(np.log(lam_t[j]), np.log(O.LAM), np.arange(O.LAM.size))
        i0, t = int(np.floor(li)), li - np.floor(li)
        res = []
        for data, mask in ((tab['data1x1'], (0,)), (tab['data1x3'], (-1, 0, 1))):
            U = uniform[mask]
            prof = (1 - t) * U[i0] + t * U[min(i0 + 1, O.LAM.size - 1)]
            prof = prof / np.interp(0.0, O.Y, prof)
            res.append(np.interp(yy, O.Y, prof) - data[sel, j])
        rms.append(float(np.sqrt(np.mean(np.concatenate(res) ** 2))))
    return lam_t[cols].tolist(), rms


def step_barshadow(gratings=('prism', 'g395m'), optics_path=None, log=print):
    """``barshadow_misfit`` for each grating's empirical table, printed."""
    from campfire_pipeline.nirspec import optics as O
    opt = O.load_optics(optics_path)['NRS1'] if optics_path else None
    out = {}
    for gr in gratings:
        out[gr] = lams, rms = barshadow_misfit(gr, optics=opt)
        log(f'{gr}: median rms {np.median(rms):.4f}; at ' + ', '.join(f'{L:.2f} um {v:.4f}' for L, v in zip(lams, rms)))
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('step', choices=('bars', 'stack', 'score', 'compare', 'kernel', 'solve', 'laws', 'visits', 'repeat',
                                     'barshadow'))
    ap.add_argument('--root', help='CAMPFIRE root of the reduced calibration observations')
    ap.add_argument('--obs', help='comma-separated observations')
    ap.add_argument('--out', help='output directory (every step but bars)')
    ap.add_argument('--dispersers', default='prism_clear', help='comma-separated, e.g. prism_clear,g395m_f290lp')
    ap.add_argument('--optics', default=None, help='a model file in place of the shipped data/nirspec_optics.json')
    ap.add_argument('--select', help='kernel: the stack run whose clean star-ranges are fitted')
    ap.add_argument('--model-out', help='solve: the model file to write')
    ap.add_argument('--version', help='solve: the new model\'s version')
    ap.add_argument('--nod-only', action='store_true',
                    help='solve: the nod differences\' normal equations alone (not the single frames less the master '
                         'background)')
    ap.add_argument('--per-pixel', action='store_true',
                    help='solve: every pixel weighted alike, not each star once (sum_normals)')
    ap.add_argument('--groups', default='',
                    help='score, compare: datasets apart, name=key prefix,... (e.g. '
                         '1133=nrscal_slitgrid_1133,ngc=nrscal_ngc2506,wavecal=nrscal_wavecal)')
    ap.add_argument('--against', help='compare: the second stack run (--out is the first)')
    ap.add_argument('--by-group', action='store_true',
                    help='visits: one visit per background group (a scan inside one observation)')
    ap.add_argument('--along', choices=('y', 'x'), default='y',
                    help='repeat: the position the visits differ in (y: across configurations; x: a scan along the '
                         'dispersion)')
    ap.add_argument('--exclude', default='', help='repeat: comma-separated source ids left out (not the same object)')
    ap.add_argument('--gratings', default='prism,g395m', help='barshadow: the empirical tables compared')
    ap.add_argument('--processes', type=int, default=1)
    a = ap.parse_args(argv)
    if a.step not in ('bars', 'barshadow') and not a.out:
        ap.error(f'{a.step} needs --out')
    groups = parse_groups(a.groups)
    if a.step == 'bars':
        step_bars(a.root, a.obs.split(','), processes=a.processes)
    elif a.step == 'stack':
        step_stack(a.root, a.obs.split(','), a.out, dispersers=tuple(a.dispersers.split(',')), optics_path=a.optics,
                   processes=a.processes)
    elif a.step == 'score':
        step_score(a.out)
        if groups:
            step_score_groups(a.out, groups)
    elif a.step == 'compare':
        step_compare(a.out, a.against, groups or None)
    elif a.step == 'visits':
        step_visits(a.root, a.obs.split(','), a.out, disperser=a.dispersers.split(',')[0], by_group=a.by_group,
                    optics_path=a.optics, processes=a.processes)
    elif a.step == 'repeat':
        step_repeat(a.out, along=a.along, exclude=[int(s) for s in a.exclude.split(',') if s])
    elif a.step == 'barshadow':
        step_barshadow(tuple(a.gratings.split(',')), optics_path=a.optics)
    elif a.step == 'kernel':
        step_kernel(a.root, a.obs.split(','), a.select, a.out, optics_path=a.optics, processes=a.processes)
    elif a.step == 'laws':
        step_laws(a.out)
    else:
        step_solve(os.path.join(a.out, 'kernel'), a.optics, a.model_out, a.version, part='nod_' if a.nod_only else '',
                   per_star=not a.per_pixel)


if __name__ == '__main__':
    main()
