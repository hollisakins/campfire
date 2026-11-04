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

    python -m campfire_pipeline.nirspec.optics_calibration stack --root R --obs a,b --out D
    python -m campfire_pipeline.nirspec.optics_calibration score --out D
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
CHI2_OUTLIER = 5.0                 # chi2/dof over the detector-range's median


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

    @property
    def key(self):
        base = f'{self.obs}_{self.source_id}_{self.detector}'
        return base if self.disperser == 'prism_clear' else f'{base}_{self.disperser}'


def star_tasks(root, obs_name, disperser='prism_clear', observations_file=None):
    """Every source of a reduced observation with a stage-3 science exposure in the
    disperser, one task per detector. Reads the root only (no workspace set-up)."""
    from astropy.io import fits
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
    sci = S3._science_files(every)
    out = []
    for sid in np.unique(sci['source_id']):
        for det in np.unique(sci['detector'][sci['source_id'] == sid]):
            tf = sci[(sci['source_id'] == sid) & (sci['detector'] == det)]
            t = S3._profile_fit_task(obs, every, tf, '', sid)
            h = fits.getheader(tf['path'][0], 'SCI')
            out.append(StarTask(obs=obs_name, source_id=int(sid), detector=str(det).upper(),
                                groups=[[str(p) for p in g] for g in t['groups']], overrides=t['overrides'],
                                science=[str(n) for n in t['science']], quadrant=int(h.get('MSAQUAD', 0)),
                                srcy=float(h.get('SRCYPOS', np.nan)), disperser=disperser))
    return out


def tasks_for(root, observations, dispersers=('prism_clear',)):
    """Star tasks of the observations."""
    return [t for name in observations for d in dispersers for t in star_tasks(root, name, disperser=d)]


def load_star(task):
    return PF.load_source(task.groups, bkg_overrides=task.overrides, science=set(task.science))


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
        return dict(c0=float(c0), drift=float(dr), c_node=float(self.centre_at(c0, dr, self.lam_c)),
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
    rec, samp = dict(key=task.key, detector=task.detector, ranges=[]), {}
    for r, rng in enumerate(ranges):
        try:
            RF = RangeFit(exps, rng, optics=optics)
            s = RF.start()
            g = RF.fit(x0=s['_x'], use=s['_use'])
        except PF.ProfileFitFallback:
            continue
        rec['ranges'].append(dict(range=r, lam=list(rng), peak_snr=g['peak_snr'], c_node=g['c_node'], w=g['w'],
                                  chi2=g['chi2'], ndof=g['ndof'],
                                  sat=saturated_fraction(exps, flags, g['c_node'], rng)))
        samp[r] = RF.samples(g)
    return rec, samp


# ---------------------------------------------------------------------------
# The stack test
# ---------------------------------------------------------------------------
def clean_keys(records, snr_min=SNR_MIN, edge_max=EDGE_MAX, sat_max=SAT_MAX, w_max=W_STAR_MAX,
               chi2_outlier=CHI2_OUTLIER):
    """{(detector, range): [star keys]} of the star-ranges the stack test uses: peak S/N at
    least ``snr_min``, the centre within ``edge_max`` of the shutter centre, no saturated pixel
    on the trace; of stars whose median width over their ranges is at most ``w_max`` (a
    galaxy, a pair or a contaminated star fits wider), and with chi2/dof at most
    ``chi2_outlier`` x the median of the detector-range's candidates."""
    cand = {}
    for rec in records:
        if not rec['ranges'] or np.median([row['w'] for row in rec['ranges']]) > w_max:
            continue
        for row in rec['ranges']:
            if row['peak_snr'] >= snr_min and abs(row['c_node']) < edge_max and row['sat'] <= sat_max:
                cand.setdefault((rec['detector'], row['range']), []).append((rec['key'], row['chi2'] / row['ndof']))
    out = {}
    for k, rows in cand.items():
        med = np.median([c for _, c in rows])
        keep = [key for key, c in rows if c <= chi2_outlier * med]
        if keep:
            out[k] = keep
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


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('step', choices=('stack', 'score'))
    ap.add_argument('--root', help='CAMPFIRE root of the reduced calibration observations')
    ap.add_argument('--obs', help='comma-separated observations')
    ap.add_argument('--out', required=True, help='output directory')
    ap.add_argument('--dispersers', default='prism_clear', help='comma-separated, e.g. prism_clear,g395m_f290lp')
    ap.add_argument('--optics', default=None, help='a model file in place of the shipped data/nirspec_optics.json')
    ap.add_argument('--processes', type=int, default=1)
    a = ap.parse_args(argv)
    if a.step == 'stack':
        step_stack(a.root, a.obs.split(','), a.out, dispersers=tuple(a.dispersers.split(',')), optics_path=a.optics,
                   processes=a.processes)
    else:
        step_score(a.out)


if __name__ == '__main__':
    main()
