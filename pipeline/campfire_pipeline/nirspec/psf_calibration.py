"""
Cross-dispersion PSF calibration for the profile fit, from calibration-field stars.

``profile_fit`` models a source as a Gaussian galaxy convolved with a star-measured
PSF (``profile_fit.PSF_TABLES``: per detector, a core Gaussian of sigma ``S_rows``
native rows plus a wing of fraction ``f`` and sigma ``PSF_K`` x core, both split
evenly before and after the shutter) and moves its trace centre with wavelength by
``profile_fit.CENTRE_LAW``. This module measures both on stars, in the profile fit's
own terms: every star is fitted through ``profile_fit.ProfileModel`` on the frames
stage 3 reads (``profile_fit.load_source``: the nod differences with the path-loss
correction removed, the partners stage 2b subtracted), with the extraction's shutter
clipping, pixel integration, point-source throughput and nod-partner terms. A star
is a point source, so the galaxy width is held at ``W_FLOOR`` and the PSF is fitted
instead (``ProfileModel``'s ``psf_tables`` / ``centre_law_tables``).

Per detector (NRS1, NRS2) and wavelength range (``RANGES``, centred on
``profile_fit.PSF_LAM``):

1. every star and range (``fit_star``): the core sigma, the centre and its drift
   within the range free, the wing fraction held (the current table's); the spectrum
   solved linearly as in the extraction. Then the core sigma and centre again at
   each wing fraction of ``F_GRID`` (the profile of the fit in f);
2. the star selection (``select``): peak S/N, position in the shutter, outliers;
3. the wing fraction shared by the clean stars (``joint_wing_fraction``): the minimum
   of the summed profiles, each star's chi2 scaled by its own reduced chi2 so a bright
   star counts as one star;
4. every selected star refitted at the joint wing fraction (``fit_star`` with
   ``wing``); the table (``build_table``): core sigma = 25th percentile of the clean
   stars' widths, wing fraction = the joint one, wing sigma = ``PSF_K`` x core. The
   lower quartile errs on the narrow side (a narrower PSF lets the galaxy width take
   up the difference; a broader one cannot be undone);
5. the trace centre law (``centre_law_refit``): each star's centre per range with no
   law applied (``profile_fit.NO_CENTRE_LAW``), relative to its 1.0-2.6 um mean, fitted
   per detector as a drift per MSA quadrant plus a slope with the star's position in
   the shutter (the ``CENTRE_LAW`` layout).

Pixel weights are the extraction's variances (``ProfileModel.diff_variance``) plus a
floor of (column peak / ``SN_CAP``)^2, fixed per star and range from a first fit with
the current table: a star's misfit at the PSF's few-percent level would otherwise set
the fit by its brightest pixels, and the outlier clipping would remove its core. The
floor keeps every pixel of the profile in play and caps what one bright star weighs.

``run`` drives the whole calibration on a reduced root and writes the tables and
the per-star fits; ``plot_*`` draw the figures.
"""

import copy
import json
import os
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import least_squares

from campfire_pipeline.nirspec import profile_fit as PF

RANGES = ((0.7, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 2.6), (2.6, 3.3), (3.3, 4.1), (4.1, 5.3))
REF_RANGES = (1, 2, 3)             # 1.0-2.6 um: the centre law's zero point
F_GRID = np.round(np.arange(0.0, 0.6001, 0.05), 3)
SN_CAP = 30.0                      # per-pixel S/N cap of the weights (variance floor)
CLIP = 5.0                         # outlier clipping, sigma (the extraction's)
N_CLIP = 2
S_BOUNDS = (0.12, 1.5)             # core sigma [native rows]
DRIFT_BOUND = 0.3                  # centre drift within a range [slit units per octave]
CENTRE_BOUND = 0.6                 # centre within this of the planned position [slit units]
GRID_MARGIN = 0.05                 # spectrum grid beyond the range [um]

# selection
SNR_MIN = 6.0                      # peak-pixel S/N per range
WIDTH_ERR_MAX = 0.1                # core-sigma uncertainty per range [rows]
EDGE_MAX = 0.3                     # |centre| [shutter pitch] for the table's stars
OUTLIER_RANGES = (1, 4)            # 1.0-1.5 and 2.6-3.3 um decide star-level outliers
OUTLIER_MAD = 3.0
OUTLIER_MAD_MIN = 0.03             # rows
OUTLIER_CHI2 = 5.0                 # x the detector's median reduced chi2
CENTRE_MIN_STARS = 5               # per MSA quadrant for its own drift column
SATURATED = 2                      # jwst DQ bit


def range_centres():
    return np.array([0.5 * (a + b) for a, b in RANGES])


def constant_tables(S_rows, f):
    """A PSF table (``profile_fit.PSF_TABLES`` layout) with one core sigma and wing
    fraction at every wavelength, on both detectors."""
    one = dict(lam=np.array([0.0, 100.0]), S_rows=np.array([S_rows, S_rows], float), f=np.array([f, f], float))
    return {'NRS1': one, 'NRS2': one}


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
    x_start: int = None
    y_start: int = None
    srcy: float = np.nan

    @property
    def key(self):
        return f'{self.obs}_{self.source_id}_{self.detector}'


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
                                x_start=int(h.get('SLTSTRT1', -1)), y_start=int(h.get('SLTSTRT2', -1)),
                                srcy=float(h.get('SRCYPOS', np.nan))))
    return out


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
    """A star in one wavelength range, through the profile fit's model.

    Shape: the galaxy width at ``W_FLOOR``, centre ``c0`` and drift (per octave about
    the model's pivot) free, no centre law (the centre is where the light is). PSF:
    a constant core sigma and wing fraction over the range (``constant_tables``), or a
    given table."""

    def __init__(self, exps, lam_range, centre_law_tables=None):
        self.lam_range = tuple(lam_range)
        law = PF.NO_CENTRE_LAW if centre_law_tables is None else centre_law_tables
        ex = restrict(exps, lam_range)
        M0 = PF.ProfileModel(ex, centre_law_tables=law)
        g = M0.lam_grid
        g = g[(g > lam_range[0] - GRID_MARGIN) & (g < lam_range[1] + GRID_MARGIN)]
        if g.size < 3:
            raise PF.TooFewWavelengths('psf calibration: too few wavelengths in range')
        self.M = PF.ProfileModel(ex, grid=g, centre_law_tables=law)
        M = self.M
        self.eoff = np.zeros(len(ex))
        self.v_raw = M.pix['v'].copy()
        # (exposure, detector column) of every fitted pixel, for the per-column peak
        keys = []
        for k, m in M.pix_masks:
            keys.append(k * 100000 + np.nonzero(m)[1])
        self.colkey = np.concatenate(keys)
        self.own = np.array([M.exps[k].has_source for k in M.pix['e']])
        self.lam_c = 0.5 * (lam_range[0] + lam_range[1])

    # -- model ---------------------------------------------------------------
    def shape(self, c0, drift):
        return np.array([c0, drift, np.log(PF.W_FLOOR), 0.0])

    def solve(self, c0, drift, tables):
        self.M.psf_tables = tables
        f, _, good, A = self.M.solve(self.shape(c0, drift), self.eoff)
        return f, good, A

    def set_floor(self, model):
        """Variance floor (column peak / SN_CAP)^2 from a model of the differences."""
        peak = np.zeros(self.colkey.size)
        u, inv = np.unique(self.colkey, return_inverse=True)
        mx = np.zeros(u.size)
        np.maximum.at(mx, inv, np.abs(model))
        peak = mx[inv]
        self.M.pix['v'] = self.v_raw + (peak / SN_CAP) ** 2

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

    # -- fitting -------------------------------------------------------------
    def fit(self, f_wing=None, tables=None, x0=None, use=None, clip_rounds=N_CLIP):
        """Least-squares fit of (c0, drift[, S]): with ``tables`` the PSF is that
        table (S fixed); otherwise a constant core sigma S is free at wing fraction
        ``f_wing``. ``use``: a fixed pixel mask (no clipping); else clipping at CLIP
        sigma, ``clip_rounds`` times, from all pixels. Returns a dict."""
        M = self.M
        free_S = tables is None
        c_lo, c_hi = M.c_prior - CENTRE_BOUND, M.c_prior + CENTRE_BOUND
        lo = [c_lo + 1.0, -DRIFT_BOUND + 1.0] + ([S_BOUNDS[0]] if free_S else [])
        hi = [c_hi + 1.0, DRIFT_BOUND + 1.0] + ([S_BOUNDS[1]] if free_S else [])
        lo, hi = np.array(lo), np.array(hi)

        def tab(x):
            return constant_tables(x[2], f_wing) if free_S else tables

        def resid(x):
            f, _, A = self.solve(x[0] - 1.0, x[1] - 1.0, tab(x))
            r = (M.pix['d'] - A @ f) / np.sqrt(M.pix['v'])
            return np.where(M.use, r, 0.0)

        if x0 is None:
            best = None
            S0 = [0.45] if free_S else []
            for c in M.c_prior + np.arange(-0.2, 0.201, 0.05):
                x = np.array([c + 1.0, 1.0] + S0)
                M.use = np.ones(M.npix, bool) if use is None else use.copy()
                s = float(np.sum(resid(x) ** 2))
                if best is None or s < best[0]:
                    best = (s, x)
            x = best[1]
        else:
            x = np.array([x0[0] + 1.0, x0[1] + 1.0] + ([x0[2]] if free_S else []))
        x = np.clip(x, lo + 1e-6, hi - 1e-6)
        M.use = np.ones(M.npix, bool) if use is None else use.copy()
        nround = 0 if use is not None else clip_rounds
        for it in range(nround + 1):
            res = least_squares(resid, x, bounds=(lo, hi), x_scale='jac', ftol=1e-6, xtol=1e-6, gtol=1e-6)
            x = res.x
            if it == nround:
                break
            f, _, A = self.solve(x[0] - 1.0, x[1] - 1.0, tab(x))
            r = (M.pix['d'] - A @ f) / np.sqrt(M.pix['v'])
            new = np.abs(r) < CLIP
            if np.array_equal(new, M.use):
                break
            M.use = new
        f, good, A = self.solve(x[0] - 1.0, x[1] - 1.0, tab(x))
        model = A @ f
        r = (M.pix['d'] - model) / np.sqrt(M.pix['v'])
        rr = (M.pix['d'] - model) / np.sqrt(self.v_raw)
        dofw = M.dof_weight()[M.use]
        ndof = max(float(np.sum(dofw)) - int(good.sum()) - x.size, 1.0)
        chi2 = float(np.sum(r[M.use] ** 2))
        J = res.jac[np.flatnonzero(M.use)] if res.jac.shape[0] == M.npix else res.jac
        try:
            cov = np.linalg.pinv(J.T @ J) * max(chi2 / ndof, 1.0)
            err = np.sqrt(np.abs(np.diag(cov)))
        except np.linalg.LinAlgError:
            err = np.full(x.size, np.nan)
        c0, drift = x[0] - 1.0, x[1] - 1.0
        out = dict(c0=float(c0), drift=float(drift), c_node=float(self.centre_at(c0, drift, self.lam_c)),
                   e_c=float(err[0]), chi2=chi2, chi2_raw=float(np.sum(rr[M.use] ** 2)), ndof=ndof,
                   chi2r=chi2 / ndof, npix=int(M.npix), nuse=int(M.use.sum()), ngrid=int(good.sum()),
                   success=bool(res.success), peak_snr=self.peak_snr(model))
        if free_S:
            S = float(x[2])
            out.update(S=S, e_S=float(err[2]), f=float(f_wing),
                       S_at_bound=bool(S <= S_BOUNDS[0] * 1.001 or S >= S_BOUNDS[1] * 0.999))
        out['_x'] = [float(c0), float(drift)] + ([float(x[2])] if free_S else [])
        out['_use'] = M.use.copy()
        out['_f'] = f
        out['_model'] = model
        return out

    def samples(self, fitres, tables):
        """The star's own image per pixel as a fraction of its light, for stacking:
        (offset from the trace centre [rows], data, model, noise, wavelength), over the
        science frames that contain the star. The data are the nod differences less the
        model's partner terms (the negative copies), divided by the column's light
        (spectrum x throughput); the model is the same pixel's own term."""
        M = self.M
        c0, drift = fitres['_x'][0], fitres['_x'][1]
        if tables is None:
            tables = constant_tables(fitres['S'], fitres['f'])
        f, good, A = self.solve(c0, drift, tables)
        model = A @ f
        p = self.shape(c0, drift)
        fvals = np.where(good, f, np.nan)
        out = dict(u=[], d=[], m=[], e=[], lam=[])
        n = 0
        for k, msk in M.pix_masks:
            nk = int(msk.sum())
            sl = slice(n, n + nk)
            n += nk
            e = M.exps[k]
            if not e.has_source:
                continue
            y, lam, dy = e.y[msk], e.wav[msk], e.dy[msk]
            own = M.image(p, self.eoff, k, y, lam, dy)
            tau = M.throughput(p, self.eoff, k, lam, dy)
            spec = np.interp(lam, M.lam_grid, fvals, left=np.nan, right=np.nan)
            norm = spec * tau
            data_own = M.pix['d'][sl] - (model[sl] - norm * own)
            keep = M.use[sl] & np.isfinite(norm) & (norm > 0)
            cen = M.centre(p, self.eoff, k, lam)
            out['u'].append(((y - cen) / dy)[keep])
            out['d'].append((data_own / norm)[keep])
            out['m'].append(own[keep])
            out['e'].append((np.sqrt(self.v_raw[sl]) / norm)[keep])
            out['lam'].append(lam[keep])
        return {k: (np.concatenate(v).astype('f4') if v else np.zeros(0, 'f4')) for k, v in out.items()}


# ---------------------------------------------------------------------------
# Per star
# ---------------------------------------------------------------------------
def _public(d):
    return {k: v for k, v in d.items() if not k.startswith('_')}


def current_wing(det):
    return dict(zip(range(len(RANGES)), np.asarray(PF.PSF_TABLES[det]['f'], float)))


def fit_star(task, exps=None, wing=None, f_grid=F_GRID, sample_tables=None, snr_floor=5.0, log=None):
    """Fit one star in every range.

    Per range: a reference fit with the current table (centre and drift free), which
    sets the variance floor; then the core sigma free at wing fraction ``wing[r]``
    (default: the current table's at the range centre), with clipping; then, when
    ``f_grid`` is given, the range's peak S/N reaches ``snr_floor`` and the star is not
    clearly off-centre (``EDGE_MAX`` + 0.05: no use to the joint wing fit), the core
    sigma and centre refitted at each wing fraction of the grid on the same pixels.

    sample_tables : {name: PSF tables or None (the current table)}: also return the
        stacking samples (``RangeFit.samples``) of a fit at each of these tables.
    Returns (record, samples): record = dict(star info, ranges=[per-range dicts]).
    """
    log = log or (lambda *a: None)
    exps = load_star(task) if exps is None else exps
    det = task.detector
    f_cur = np.interp(range_centres(), PF.PSF_LAM, PF.PSF_TABLES[det]['f'])
    rec = dict(key=task.key, obs=task.obs, source_id=task.source_id, detector=det, quadrant=task.quadrant,
               x_start=task.x_start, y_start=task.y_start, srcy=task.srcy, nexp=len(exps),
               nsci=int(sum(e.use_data for e in exps)), ranges=[])
    samples = {}
    sat = None
    for r, (a, b) in enumerate(RANGES):
        row = dict(range=r, lam=[a, b])
        try:
            RF = RangeFit(exps, (a, b))
        except (PF.ProfileFitFallback, ValueError) as err:
            row['error'] = str(err)
            rec['ranges'].append(row)
            continue
        # reference: the current table; first the floor from a fit without it
        RF.set_floor(np.zeros(RF.M.npix))
        ref0 = RF.fit(tables=PF.PSF_TABLES, clip_rounds=0)
        RF.set_floor(ref0['_model'])
        ref = RF.fit(tables=PF.PSF_TABLES)
        RF.set_floor(ref['_model'])
        row['ref'] = _public(ref)
        fw = float(f_cur[r] if wing is None else wing[r])
        fr = RF.fit(f_wing=fw, x0=ref['_x'] + [0.45])
        row['free'] = _public(fr)
        if f_grid is not None and fr['peak_snr'] >= snr_floor and abs(fr['c_node']) < EDGE_MAX + 0.05:
            prof = []
            x = fr['_x']
            order = np.argsort(np.abs(np.asarray(f_grid) - fw))       # outward from the start
            res = {}
            for i in order:
                g = RF.fit(f_wing=float(f_grid[i]), x0=x, use=fr['_use'])
                res[i] = g
            for i in range(len(f_grid)):
                g = res[i]
                prof.append(dict(f=float(f_grid[i]), chi2=g['chi2'], S=g.get('S'), c_node=g['c_node']))
            row['profile'] = prof
        if sample_tables:
            for name, tab in sample_tables.items():
                if tab is None:
                    g = fr
                    samp = RF.samples(g, constant_tables(g['S'], g['f']))
                else:
                    g = RF.fit(tables=tab, x0=fr['_x'][:2], use=fr['_use'])
                    row[f'fit_{name}'] = _public(g)
                    samp = RF.samples(g, tab)
                samples[(r, name)] = samp
        if sat is None:
            try:
                sat = saturated_flags(exps)
            except Exception as err:        # diagnostics only
                sat = [None] * len(exps)
                log(f'{task.key}: saturation check failed: {err}')
        row['sat_frac'] = saturated_fraction(exps, sat, fr['c_node'], (a, b))
        rec['ranges'].append(row)
    return rec, samples


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------
def star_centre(rec, stage='free', fallback=True, snr_min=SNR_MIN):
    """A star's centre in the reference ranges (1.0-2.6 um): inverse-variance mean of
    the centres of the ranges with peak S/N >= ``snr_min``. With ``fallback``, a star
    with no reference range on this detector (its spectrum crosses the detector gap)
    takes the mean over the ranges it has (the trace drifts by < 0.05 shutter pitch).
    NaN when none is measured."""
    def mean(ranges):
        c, w = [], []
        for row in rec['ranges']:
            if row['range'] in ranges and stage in row:
                x = row[stage]
                e = x.get('e_c')
                if x.get('success') and x.get('peak_snr', 0) >= snr_min and e and np.isfinite(e) and e > 0:
                    c.append(x['c_node'])
                    w.append(1.0 / max(e, 1e-3) ** 2)
        return float(np.sum(np.multiply(c, w)) / np.sum(w)) if w else np.nan
    c = mean(REF_RANGES)
    if not np.isfinite(c) and fallback:
        c = mean(range(len(RANGES)))
    return c


def range_ok(x, snr_min=SNR_MIN):
    return (x.get('success', False) and x.get('peak_snr', 0) >= snr_min and np.isfinite(x.get('e_S', np.nan))
            and x['e_S'] < WIDTH_ERR_MAX and not x.get('S_at_bound', False))


def select(records, snr_min=SNR_MIN, edge_max=EDGE_MAX, stage='free', log=None):
    """Star selection per detector and range.

    A range of a star is usable when its peak S/N reaches ``snr_min``, the core width
    is measured to better than ``WIDTH_ERR_MAX`` rows and is not at a bound. A star is
    an outlier when, in either of the ranges ``OUTLIER_RANGES``, its width is more than
    ``OUTLIER_MAD`` MADs (at least ``OUTLIER_MAD_MIN`` rows) from its detector's median
    or its reduced chi2 exceeds ``OUTLIER_CHI2`` times the median. A star is clean when
    it is no outlier and its centre in 1.0-2.6 um is within ``edge_max`` of the shutter
    centre; edge stars are kept for the centre law.

    Returns dict(clean={(det, r): [keys]}, usable={(det, r): [keys]}, outliers={key: reason},
    edge=set of keys, centre={key: c}) and writes the reasons through ``log``."""
    log = log or (lambda *a: None)
    centre = {rec['key']: star_centre(rec, stage, snr_min=snr_min) for rec in records}
    by_key = {rec['key']: rec for rec in records}
    outliers = {}
    for det in ('NRS1', 'NRS2'):
        for r in OUTLIER_RANGES:
            rows = [(rec['key'], rec['ranges'][r][stage]) for rec in records
                    if rec['detector'] == det and r < len(rec['ranges']) and stage in rec['ranges'][r]
                    and range_ok(rec['ranges'][r][stage], snr_min)]
            if len(rows) < 5:
                continue
            S = np.array([x['S'] for _, x in rows])
            chi = np.array([x['chi2r'] for _, x in rows])
            med, mad = np.median(S), 1.4826 * np.median(np.abs(S - np.median(S)))
            cmed = np.median(chi)
            for (key, x), s, c in zip(rows, S, chi):
                if abs(s - med) > OUTLIER_MAD * max(mad, OUTLIER_MAD_MIN):
                    outliers.setdefault(key, []).append(
                        f'{RANGES[r][0]}-{RANGES[r][1]} um width {s:.3f} vs median {med:.3f} (MAD {mad:.3f})')
                if c > OUTLIER_CHI2 * cmed:
                    outliers.setdefault(key, []).append(
                        f'{RANGES[r][0]}-{RANGES[r][1]} um reduced chi2 {c:.2f} vs median {cmed:.2f}')
    edge = {k for k, c in centre.items() if not (np.isfinite(c) and abs(c) < edge_max)}
    # range-level outliers: the same width test in every range, among the stars left
    range_out = {}
    for det in ('NRS1', 'NRS2'):
        for r in range(len(RANGES)):
            rows = [(rec['key'], rec['ranges'][r][stage]) for rec in records
                    if rec['detector'] == det and r < len(rec['ranges']) and stage in rec['ranges'][r]
                    and rec['key'] not in outliers and range_ok(rec['ranges'][r][stage], snr_min)]
            if len(rows) < 5:
                continue
            S = np.array([x['S'] for _, x in rows])
            med, mad = np.median(S), 1.4826 * np.median(np.abs(S - np.median(S)))
            for (key, x), s in zip(rows, S):
                if abs(s - med) > OUTLIER_MAD * max(mad, OUTLIER_MAD_MIN):
                    range_out[(key, r)] = f'width {s:.3f} vs median {med:.3f} (MAD {mad:.3f})'
    clean, usable = {}, {}
    for det in ('NRS1', 'NRS2'):
        for r in range(len(RANGES)):
            for rec in records:
                if rec['detector'] != det or r >= len(rec['ranges']) or stage not in rec['ranges'][r]:
                    continue
                if not range_ok(rec['ranges'][r][stage], snr_min) or rec['key'] in outliers:
                    continue
                if (rec['key'], r) in range_out:
                    continue
                usable.setdefault((det, r), []).append(rec['key'])
                if rec['key'] not in edge:
                    clean.setdefault((det, r), []).append(rec['key'])
    for key, why in sorted(outliers.items()):
        log(f'outlier {key}: ' + '; '.join(why))
    for (key, r), why in sorted(range_out.items()):
        log(f'outlier in {RANGES[r][0]}-{RANGES[r][1]} um only, {key}: {why}')
    for key in sorted(edge):
        if any(key in v for v in usable.values()):
            log(f'edge {key}: centre {centre[key]:+.3f} (|c| >= {edge_max})')
    for det in ('NRS1', 'NRS2'):
        n = [len(clean.get((det, r), [])) for r in range(len(RANGES))]
        stars = {k for r in range(len(RANGES)) for k in clean.get((det, r), [])}
        log(f'{det}: {len(stars)} clean stars; per range ' + ', '.join(
            f'{RANGES[r][0]}-{RANGES[r][1]}: {n[r]}' for r in range(len(RANGES))))
    return dict(clean=clean, usable=usable, outliers={k: '; '.join(v) for k, v in outliers.items()}, edge=edge,
                centre=centre, by_key=by_key, range_outliers={f'{k}:{r}': v for (k, r), v in range_out.items()})


def _profile_curve(row):
    p = row.get('profile')
    if not p:
        return None
    f = np.array([q['f'] for q in p])
    c = np.array([q['chi2'] for q in p])
    S = np.array([np.nan if q['S'] is None else q['S'] for q in p])
    return f, c, S


def _parabola_min(f, c):
    i = int(np.argmin(c))
    if i == 0 or i == len(f) - 1:
        return float(f[i])
    x, y = f[i - 1:i + 2], c[i - 1:i + 2]
    A = np.polyfit(x, y, 2)
    return float(-A[1] / (2 * A[0])) if A[0] > 0 else float(f[i])


def joint_wing_fraction(records, clean, stage='free', rescale=False):
    """Per (detector, range): the wing fraction minimising the clean stars' summed
    chi2 profiles (``fit_star``'s ``profile``: each star's core width, centre and
    spectrum refitted at every trial value), i.e. one least-squares fit of a shared
    wing fraction with the weights of the per-star fits (the ``SN_CAP`` floor already
    caps what one bright star weighs). ``rescale``: divide each star's chi2 by its
    reduced chi2 first (gives the bright stars back their full weight; not the
    default). Uncertainty: jackknife over the stars. Also the median of the stars'
    own minima (``f_median``), as a cross-check that no few stars set the value.
    Returns {(det, r): dict(f, f_err, f_median, n, curve=(f, chi2))}."""
    by_key = {rec['key']: rec for rec in records}
    out = {}
    for (det, r), keys in clean.items():
        curves, mins = [], []
        for k in keys:
            row = by_key[k]['ranges'][r]
            cv = _profile_curve(row)
            if cv is None:
                continue
            f, c, _ = cv
            scale = max(np.min(c) / row[stage]['ndof'], 1e-6) if rescale else 1.0
            curves.append(c / scale)
            mins.append(_parabola_min(f, c))
        if not curves:
            continue
        C = np.array(curves)
        tot = C.sum(axis=0)
        fj = _parabola_min(f, tot)
        jk = np.array([_parabola_min(f, tot - C[i]) for i in range(len(C))]) if len(C) > 2 else np.array([fj])
        n = len(C)
        err = float(np.sqrt((n - 1) / n * np.sum((jk - jk.mean()) ** 2))) if n > 2 else np.nan
        out[(det, r)] = dict(f=fj, f_err=err, f_median=float(np.median(mins)), n=n,
                             curve=(f.tolist(), (tot - tot.min()).tolist()))
    return out


def build_table(records, clean, stage='joint', percentile=25.0, wing=None):
    """The table: per detector, core sigma = ``percentile`` of the clean stars' core
    widths at each range (fits at the joint wing fraction, ``stage``), wing fraction =
    the joint one (``wing``: {(det, r): f}). Also the median and quartiles.
    Returns {det: dict(lam, S_rows, f, n, S_p16, S_p50, S_p75)} (PSF_TABLES layout plus
    statistics)."""
    by_key = {rec['key']: rec for rec in records}
    out = {}
    for det in ('NRS1', 'NRS2'):
        lam, S, F, N, P16, P50, P75 = [], [], [], [], [], [], []
        for r in range(len(RANGES)):
            v = np.array([by_key[k]['ranges'][r][stage]['S'] for k in clean.get((det, r), [])
                          if stage in by_key[k]['ranges'][r]])
            lam.append(float(range_centres()[r]))
            N.append(int(v.size))
            if v.size:
                p16, p25, p50, p75 = np.percentile(v, [16, percentile, 50, 75])
            else:
                p16 = p25 = p50 = p75 = np.nan
            S.append(float(p25)); P16.append(float(p16)); P50.append(float(p50)); P75.append(float(p75))
            F.append(float(wing[(det, r)]) if wing and (det, r) in wing else np.nan)
        out[det] = dict(lam=lam, S_rows=S, f=F, k=PF.PSF_K, n=N, S_p16=P16, S_p50=P50, S_p75=P75,
                        percentile=percentile)
    return out


def fwhm_rows(S, f, k=None):
    """FWHM [rows] of the core + wing profile (pre-shutter, no clipping, no pixel)."""
    from scipy.optimize import brentq
    k = PF.PSF_K if k is None else k

    def p(y):
        return (1 - f) / S * np.exp(-0.5 * (y / S) ** 2) + f / (k * S) * np.exp(-0.5 * (y / (k * S)) ** 2)
    half = 0.5 * p(0.0)
    return 2 * brentq(lambda y: p(y) - half, 0, 6 * S * k)


def side_light(out_dir, name, edge=1.5):
    """From the stacking samples of the clean stars: per (detector, range), the light
    beyond ``edge`` rows on each side of the trace and within it, data and model (``name``:
    'old' or 'new' table), each as a share of the model's light over all sampled pixels;
    median over stars. Returns {(det, r): dict(neg_d, neg_m, pos_d, pos_m, core_d, core_m, n)}."""
    sel, _ = read_selection(out_dir)
    rec2 = [r for r in read_records(os.path.join(out_dir, 'pass2')) if r['ranges']]
    clean = clean_in_pass2(rec2, sel)
    out = {}
    for (det, r), keys in clean.items():
        vals = []
        for k in keys:
            fn = os.path.join(out_dir, 'samples', f'{k}.npz')
            if not os.path.exists(fn):
                continue
            z = np.load(fn)
            pre = f'r{r}_{name}_'
            if pre + 'u' not in z:
                continue
            u, d, m = z[pre + 'u'], z[pre + 'd'], z[pre + 'm']
            ok = np.isfinite(d) & np.isfinite(m)
            tot = np.sum(m[ok])
            if not tot > 0:
                continue
            row = []
            for sel_ in (u < -edge, u > edge, np.abs(u) <= edge):
                s = ok & sel_
                row += [np.sum(d[s]) / tot, np.sum(m[s]) / tot]
            vals.append(row)
        if vals:
            v = np.median(np.array(vals), axis=0)
            out[(det, r)] = dict(neg_d=v[0], neg_m=v[1], pos_d=v[2], pos_m=v[3], core_d=v[4], core_m=v[5],
                                 n=len(vals))
    return out


def tables_for_model(table):
    """``PSF_TABLES`` layout (numpy arrays at ``PSF_LAM``) from ``build_table``'s output."""
    return {det: dict(S_rows=np.asarray(t['S_rows'], float), f=np.asarray(t['f'], float)) for det, t in table.items()}


def centre_rows(records, keys, stage='joint'):
    """Per star: detector, quadrant, position (centre in 1.0-2.6 um, slit units) and the
    centre change of each range from it (NaN where unmeasured), with errors."""
    by_key = {rec['key']: rec for rec in records}
    out = []
    for k in keys:
        rec = by_key[k]
        cref = star_centre(rec, stage, fallback=False)
        if not np.isfinite(cref):
            continue
        dc, err = np.full(len(RANGES), np.nan), np.full(len(RANGES), np.nan)
        for row in rec['ranges']:
            x = row.get(stage)
            if x and x.get('success') and x.get('peak_snr', 0) >= SNR_MIN and np.isfinite(x.get('e_c', np.nan)):
                dc[row['range']] = x['c_node'] - cref
                err[row['range']] = x['e_c']
        out.append(dict(key=k, det=rec['detector'], quad=rec['quadrant'], cref=cref, srcy=rec['srcy'], dc=dc, err=err))
    return out


def _robust_lsq(X, y, e, nit=4):
    ok = np.isfinite(y) & np.isfinite(e)
    p = np.zeros(X.shape[1])
    A = b = None
    for _ in range(nit):
        if ok.sum() <= X.shape[1]:
            break
        w = 1 / np.maximum(e[ok], 0.003) ** 2
        A = X[ok] * np.sqrt(w)[:, None]
        b = y[ok] * np.sqrt(w)
        p, *_ = np.linalg.lstsq(A, b, rcond=None)
        res = y - X @ p
        s = 1.4826 * np.nanmedian(np.abs(res[ok]))
        ok = ok & (np.abs(res) < 4 * max(s, 0.004))
    if A is None:
        return np.full(X.shape[1], np.nan), np.full(X.shape[1], np.nan), np.nan, 0
    cov = np.linalg.pinv(A.T @ A) * max(np.sum((A @ p - b) ** 2) / max(ok.sum() - len(p), 1), 1)
    res = y - X @ p
    return p, np.sqrt(np.diag(cov)), float(1.4826 * np.nanmedian(np.abs(res[ok]))), int(ok.sum())


def centre_law_refit(rows, regressor='cref'):
    """``CENTRE_LAW`` from star centres (``centre_rows``): per detector and range,
    dc = D_quadrant + alpha * position (robust weighted least squares, one drift column
    per MSA quadrant with at least ``CENTRE_MIN_STARS`` stars, alpha shared), and the
    detector-level D from all quadrants pooled (the fallback). ``regressor``: the
    star's measured centre ('cref', as the model applies alpha to the fitted centre)
    or its planned position ('srcy', as in October). Returns the CENTRE_LAW layout
    with errors (eD, ealpha), scatter and counts."""
    out = {}
    for det in ('NRS1', 'NRS2'):
        g = [x for x in rows if x['det'] == det]
        quads = [q for q in (1, 2, 3, 4) if sum(x['quad'] == q for x in g) >= CENTRE_MIN_STARS]
        out[det] = dict(D=[], eD=[], alpha=[], ealpha=[], scatter=[], n=[])
        for q in quads:
            out[f'Q{q}_{det}'] = dict(D=[], eD=[], n=int(sum(x['quad'] == q for x in g)))
        gq = [x for x in g if x['quad'] in quads]
        for r in range(len(RANGES)):
            pos = np.array([x[regressor] for x in g])
            y = np.array([x['dc'][r] for x in g])
            e = np.array([x['err'][r] for x in g])
            p0, pe0, sc, n = _robust_lsq(np.column_stack([np.ones_like(pos), pos]), y, e)
            out[det]['D'].append(float(p0[0])); out[det]['eD'].append(float(pe0[0]))
            out[det]['scatter'].append(sc); out[det]['n'].append(n)
            if quads:
                yq = np.array([x['dc'][r] for x in gq])
                eq = np.array([x['err'][r] for x in gq])
                # quadrants with fewer than CENTRE_MIN_STARS measured stars in this range get no column
                # (least squares would return 0 for an empty one): unmeasured, NaN
                have = [q for q in quads if np.sum([(x['quad'] == q) and np.isfinite(v) and np.isfinite(e)
                                                    for x, v, e in zip(gq, yq, eq)]) >= CENTRE_MIN_STARS]
                posq = np.array([x[regressor] for x in gq])
                X = np.column_stack([np.array([x['quad'] == q for x in gq], float) for q in have] + [posq])
                sub = np.array([x['quad'] in have for x in gq])
                p, pe, _, _ = _robust_lsq(X[sub], yq[sub], eq[sub])
                out[det]['alpha'].append(float(p[-1])); out[det]['ealpha'].append(float(pe[-1]))
                for q in quads:
                    i = have.index(q) if q in have else None
                    out[f'Q{q}_{det}']['D'].append(float(p[i]) if i is not None else np.nan)
                    out[f'Q{q}_{det}']['eD'].append(float(pe[i]) if i is not None else np.nan)
            else:
                out[det]['alpha'].append(float(p0[1])); out[det]['ealpha'].append(float(pe0[1]))
    return out


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------
def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return _jsonable(x.tolist())
    if isinstance(x, (np.floating,)):
        return float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, float) and not np.isfinite(x):
        return None
    return x


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w') as fh:
        json.dump(_jsonable(obj), fh, indent=1)


def read_records(directory):
    out = []
    for fn in sorted(os.listdir(directory)):
        if fn.endswith('.json'):
            with open(os.path.join(directory, fn)) as fh:
                out.append(json.load(fh))
    for rec in out:                      # JSON nulls back to NaN in the numbers used
        for row in rec['ranges']:
            for st in ('ref', 'free', 'joint', 'fit_old', 'fit_new'):
                x = row.get(st)
                if x:
                    for k in ('e_S', 'e_c', 'S', 'c_node', 'chi2r', 'peak_snr'):
                        if k in x and x[k] is None:
                            x[k] = np.nan
    return out


def fit_table(records, stages=('ref', 'free', 'joint', 'fit_old', 'fit_new')):
    """Per-(star, range) table of the fits (astropy Table)."""
    from astropy.table import Table
    rows = []
    for rec in records:
        for row in rec['ranges']:
            base = dict(key=rec['key'], obs=rec['obs'], source_id=rec['source_id'], detector=rec['detector'],
                        quadrant=rec['quadrant'], x_start=rec['x_start'], srcy=rec['srcy'], range=row['range'],
                        lam_lo=row['lam'][0], lam_hi=row['lam'][1],
                        sat_frac=np.nan if row.get('sat_frac') is None else row['sat_frac'])
            for st in stages:
                x = row.get(st) or {}
                for k in ('S', 'e_S', 'f', 'c_node', 'e_c', 'drift', 'chi2r', 'peak_snr', 'nuse', 'npix'):
                    v = x.get(k, np.nan)
                    base[f'{st}_{k}'] = np.nan if v is None else v
            rows.append(base)
    return Table(rows=rows)


def run_stars(tasks, out_dir, wing_for=None, f_grid=F_GRID, sample_tables=None, sample_dir=None, overwrite=False,
              log=print):
    """``fit_star`` over tasks, one JSON per star in ``out_dir`` (skipped when present
    unless ``overwrite``); samples (``sample_tables``) to ``sample_dir`` as npz.
    ``wing_for(det)`` gives the per-range wing fractions (default: the current table)."""
    os.makedirs(out_dir, exist_ok=True)
    for i, t in enumerate(tasks):
        path = os.path.join(out_dir, f'{t.key}.json')
        if os.path.exists(path) and not overwrite:
            continue
        try:
            rec, samp = fit_star(t, wing=None if wing_for is None else wing_for(t.detector), f_grid=f_grid,
                                 sample_tables=sample_tables, log=log)
        except Exception as err:
            log(f'{t.key}: failed: {type(err).__name__}: {err}')
            rec, samp = dict(key=t.key, obs=t.obs, source_id=t.source_id, detector=t.detector, quadrant=t.quadrant,
                             x_start=t.x_start, y_start=t.y_start, srcy=t.srcy, error=str(err), ranges=[]), {}
        write_json(path, rec)
        if samp and sample_dir:
            os.makedirs(sample_dir, exist_ok=True)
            np.savez_compressed(os.path.join(sample_dir, f'{t.key}.npz'),
                                **{f'r{r}_{name}_{k}': v for (r, name), s in samp.items() for k, v in s.items()})
        snr = [x.get('free', {}).get('peak_snr', 0) for x in rec['ranges']]
        log(f'[{i + 1}/{len(tasks)}] {t.key}: peak S/N ' + ' '.join(f'{s:.0f}' for s in snr))


# ---------------------------------------------------------------------------
# Steps: pass 1 (fit), selection and joint wing fraction, pass 2 (refit at the
# joint wing fraction), the tables, pass 3 (stacking samples), figures
# ---------------------------------------------------------------------------
def tasks_for(root, observations, keys=None, part=None):
    """Star tasks of the observations (only ``keys`` when given); ``part`` = (i, n) keeps
    every n-th task from the i-th, to split a step over n processes."""
    out = []
    for name in observations:
        out += [t for t in star_tasks(root, name) if keys is None or t.key in keys]
    if part is not None:
        i, n = part
        out = out[i::n]
    return out


def _kv(d):
    return {f'{det}:{r}': v for (det, r), v in d.items()}


def _vk(d):
    return {(k.split(':')[0], int(k.split(':')[1])): v for k, v in d.items()}


def step_select(out_dir, snr_min=SNR_MIN, edge_max=EDGE_MAX, log=print):
    """Selection and joint wing fraction from pass 1; writes selection.json, joint_wing.json."""
    recs = [r for r in read_records(os.path.join(out_dir, 'pass1')) if r['ranges']]
    lines = []

    def both(*a):
        lines.append(' '.join(str(x) for x in a))
        log(*a)
    both(f'{len(recs)} star groups (star x detector) in pass 1; peak S/N >= {snr_min} per range, '
         f'core-width error < {WIDTH_ERR_MAX} rows, |centre| < {edge_max} shutter for the table')
    sel = select(recs, snr_min=snr_min, edge_max=edge_max, log=both)
    joint = joint_wing_fraction(recs, sel['clean'])
    for (det, r), j in sorted(joint.items()):
        both(f'joint wing fraction {det} {RANGES[r][0]}-{RANGES[r][1]} um: {j["f"]:.3f} +- {j["f_err"]:.3f} '
             f'({j["n"]} stars; median of the stars\' own minima {j["f_median"]:.3f}; current '
             f'{np.interp(range_centres()[r], PF.PSF_LAM, PF.PSF_TABLES[det]["f"]):.3f})')
    write_json(os.path.join(out_dir, 'selection.json'),
               dict(clean=_kv(sel['clean']), usable=_kv(sel['usable']), outliers=sel['outliers'],
                    range_outliers=sel['range_outliers'], edge=sorted(sel['edge']), centre=sel['centre'],
                    snr_min=snr_min, edge_max=edge_max))
    write_json(os.path.join(out_dir, 'joint_wing.json'), _kv(joint))
    with open(os.path.join(out_dir, 'selection.log'), 'w') as fh:
        fh.write('\n'.join(lines) + '\n')
    return sel, joint


def read_selection(out_dir):
    with open(os.path.join(out_dir, 'selection.json')) as fh:
        sel = json.load(fh)
    with open(os.path.join(out_dir, 'joint_wing.json')) as fh:
        joint = _vk(json.load(fh))
    for k in ('clean', 'usable'):
        sel[k] = _vk(sel[k])
    sel['edge'] = set(sel['edge'])
    return sel, joint


def wing_for_joint(joint):
    """det -> per-range wing fractions: the joint value, else the current table's."""
    def f(det):
        cur = np.interp(range_centres(), PF.PSF_LAM, PF.PSF_TABLES[det]['f'])
        return [joint[(det, r)]['f'] if (det, r) in joint else float(cur[r]) for r in range(len(RANGES))]
    return f


def step_refit(root, out_dir, observations, log=print, part=None):
    """Pass 2: every usable star (clean and edge) refitted at the joint wing fraction."""
    sel, joint = read_selection(out_dir)
    keys = {k for v in sel['usable'].values() for k in v}
    tasks = tasks_for(root, observations, keys, part=part)
    run_stars(tasks, os.path.join(out_dir, 'pass2'), wing_for=wing_for_joint(joint), f_grid=None, log=log)


def clean_in_pass2(records2, sel, snr_min=None):
    """The pass-1 clean lists, each range kept only where pass 2 passes the range cuts."""
    by_key = {r['key']: r for r in records2}
    snr_min = sel.get('snr_min', SNR_MIN) if snr_min is None else snr_min
    out = {}
    for (det, r), keys in sel['clean'].items():
        out[(det, r)] = [k for k in keys if k in by_key and r < len(by_key[k]['ranges'])
                         and 'free' in by_key[k]['ranges'][r] and range_ok(by_key[k]['ranges'][r]['free'], snr_min)]
    return out


def step_table(out_dir, percentile=25.0, log=print):
    """The new PSF table and centre law from pass 2; writes psf_table_new.json,
    centre_law_new.json, centre_law_new_srcy.json."""
    sel, joint = read_selection(out_dir)
    rec2 = [r for r in read_records(os.path.join(out_dir, 'pass2')) if r['ranges']]
    clean = clean_in_pass2(rec2, sel)
    table = build_table(rec2, clean, stage='free', percentile=percentile,
                        wing={k: v['f'] for k, v in joint.items()})
    for det in ('NRS1', 'NRS2'):
        for r in range(len(RANGES)):
            if not np.isfinite(table[det]['f'][r]):
                table[det]['f'][r] = float(np.interp(range_centres()[r], PF.PSF_LAM, PF.PSF_TABLES[det]['f']))
        table[det]['f_err'] = [joint[(det, r)]['f_err'] if (det, r) in joint else None for r in range(len(RANGES))]
    write_json(os.path.join(out_dir, 'psf_table_new.json'), table)
    usable = sorted({k for v in sel['usable'].values() for k in v} & {r['key'] for r in rec2})
    rows = centre_rows(rec2, usable, stage='free')
    for reg, name in (('cref', 'centre_law_new.json'), ('srcy', 'centre_law_new_srcy.json')):
        write_json(os.path.join(out_dir, name), centre_law_refit(rows, regressor=reg))
    write_json(os.path.join(out_dir, 'centre_rows.json'), rows)
    for det in ('NRS1', 'NRS2'):
        t = table[det]
        log(f'{det} new  S_rows ' + ' '.join(f'{x:.3f}' for x in t['S_rows']) + ' | f ' +
            ' '.join(f'{x:.3f}' for x in t['f']) + ' | n ' + ' '.join(str(x) for x in t['n']))
        log(f'{det} old  S_rows ' + ' '.join(f'{x:.3f}' for x in PF.PSF_TABLES[det]['S_rows']) + ' | f ' +
            ' '.join(f'{x:.3f}' for x in PF.PSF_TABLES[det]['f']))
    return table


def read_table(out_dir):
    with open(os.path.join(out_dir, 'psf_table_new.json')) as fh:
        return json.load(fh)


def step_samples(root, out_dir, observations, log=print, part=None):
    """Pass 3: every clean star fitted with the current and the new table, with the
    stacking samples (``RangeFit.samples``) for the profile figure."""
    sel, joint = read_selection(out_dir)
    rec2 = [r for r in read_records(os.path.join(out_dir, 'pass2')) if r['ranges']]
    clean = clean_in_pass2(rec2, sel)
    keys = {k for v in clean.values() for k in v}
    new = tables_for_model(read_table(out_dir))
    run_stars(tasks_for(root, observations, keys, part=part), os.path.join(out_dir, 'pass3'),
              wing_for=wing_for_joint(joint),
              f_grid=None, sample_tables={'old': PF.PSF_TABLES, 'new': new},
              sample_dir=os.path.join(out_dir, 'samples'), log=log)


def merged_records(out_dir):
    """Pass 1 records with pass 2's fits as 'joint' and pass 3's table fits added."""
    recs = {r['key']: r for r in read_records(os.path.join(out_dir, 'pass1'))}
    for sub, pairs in (('pass2', (('free', 'joint'),)), ('pass3', (('fit_old', 'fit_old'), ('fit_new', 'fit_new')))):
        d = os.path.join(out_dir, sub)
        if not os.path.isdir(d):
            continue
        for r2 in read_records(d):
            r1 = recs.get(r2['key'])
            if r1 is None:
                continue
            for row1, row2 in zip(r1['ranges'], r2['ranges']):
                for a, b in pairs:
                    if a in row2:
                        row1[b] = row2[a]
    return list(recs.values())


def write_fit_table(out_dir):
    t = fit_table(merged_records(out_dir))
    t.write(os.path.join(out_dir, 'star_fits.ecsv'), overwrite=True)
    return t


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
NEW_C, OLD_C, STAR_C, EDGE_C, INK, MUTED = '#2a78d6', '#eb6834', '#9a9893', '#1baf7a', '#0b0b0b', '#52514e'
QUAD_C = {1: '#2a78d6', 2: '#eb6834', 3: '#1baf7a', 4: '#6250d6'}


def _style():
    import matplotlib as mpl
    mpl.rcParams.update({'font.size': 10, 'axes.titlesize': 10.5, 'axes.labelsize': 10, 'legend.fontsize': 8.5,
                         'axes.edgecolor': MUTED, 'axes.labelcolor': INK, 'xtick.color': MUTED,
                         'ytick.color': MUTED, 'axes.grid': True, 'grid.color': '#e4e3df', 'grid.linewidth': 0.6,
                         'axes.spines.top': False, 'axes.spines.right': False, 'figure.facecolor': '#fcfcfb',
                         'axes.facecolor': '#fcfcfb', 'savefig.facecolor': '#fcfcfb'})


def plot_widths(out_dir, path):
    """(a) Core width and wing fraction against wavelength per detector: every clean
    star's width (at the joint wing fraction), their 25th percentile (the new table),
    their median, and the current table."""
    import matplotlib.pyplot as plt
    _style()
    sel, joint = read_selection(out_dir)
    rec2 = [r for r in read_records(os.path.join(out_dir, 'pass2')) if r['ranges']]
    by_key = {r['key']: r for r in rec2}
    clean = clean_in_pass2(rec2, sel)
    table = read_table(out_dir)
    lamc = range_centres()
    fig, axs = plt.subplots(3, 2, figsize=(10, 9.2), sharex=True, gridspec_kw=dict(height_ratios=[1.6, 1, 1]))
    rng = np.random.default_rng(0)
    for j, det in enumerate(('NRS1', 'NRS2')):
        ax, axf, axw = axs[0, j], axs[1, j], axs[2, j]
        for r in range(len(RANGES)):
            v = [by_key[k]['ranges'][r]['free']['S'] for k in clean.get((det, r), [])]
            ax.plot(lamc[r] * np.exp(rng.uniform(-0.05, 0.05, len(v))), v, 'o', ms=3.2, color=STAR_C, alpha=0.55,
                    mec='none', label='one star' if r == 0 else None, zorder=1)
            ax.text(lamc[r], 0.255, f'{len(v)}', ha='center', va='bottom', fontsize=8, color=MUTED)
        t = table[det]
        ax.plot(lamc, t['S_p50'], 'o', mfc='#fcfcfb', mec=NEW_C, ms=6, mew=1.5, ls='none', label='median of the stars',
                zorder=3)
        ax.plot(lamc, t['S_rows'], 'o-', color=NEW_C, ms=6, lw=2, label='new table (25th percentile)', zorder=4)
        ax.plot(PF.PSF_LAM, PF.PSF_TABLES[det]['S_rows'], 's--', color=OLD_C, ms=5, lw=1.6, label='current table',
                zorder=4)
        ax.set_title(f'{det}: core width (numbers: stars per range)', loc='left')
        ax.set_ylim(0.25, 0.85)
        if j == 0:
            ax.set_ylabel('core sigma [detector rows]')
            ax.legend(loc='upper left', frameon=False)
        fj = [joint[(det, r)]['f'] if (det, r) in joint else np.nan for r in range(len(RANGES))]
        fe = [joint[(det, r)]['f_err'] if (det, r) in joint else np.nan for r in range(len(RANGES))]
        axf.errorbar(lamc, fj, yerr=fe, fmt='o-', color=NEW_C, ms=6, lw=2, capsize=3, label='new (joint fit of the stars)')
        axf.plot(PF.PSF_LAM, PF.PSF_TABLES[det]['f'], 's--', color=OLD_C, ms=5, lw=1.6, label='current table')
        axf.set_title(f'{det}: share of light in the wide wing', loc='left')
        axf.set_ylim(0, 0.6)
        if j == 0:
            axf.set_ylabel('wing fraction')
            axf.legend(loc='upper left', frameon=False)
        fw_new = [fwhm_rows(s, ff) if np.isfinite(s) and np.isfinite(ff) else np.nan
                  for s, ff in zip(t['S_rows'], t['f'])]
        fw_old = [fwhm_rows(s, ff) for s, ff in zip(PF.PSF_TABLES[det]['S_rows'], PF.PSF_TABLES[det]['f'])]
        axw.plot(lamc, fw_new, 'o-', color=NEW_C, ms=6, lw=2, label='new table')
        axw.plot(PF.PSF_LAM, fw_old, 's--', color=OLD_C, ms=5, lw=1.6, label='current table')
        axw.set_title(f'{det}: full width at half maximum of core + wing', loc='left')
        axw.set_ylim(0.6, 1.8)
        axw.set_xscale('log')
        axw.set_xticks([0.7, 1, 1.5, 2, 3, 4, 5])
        axw.set_xticklabels(['0.7', '1', '1.5', '2', '3', '4', '5'])
        axw.set_xlabel('wavelength [µm]')
        if j == 0:
            axw.set_ylabel('FWHM [detector rows]')
            axw.legend(loc='upper left', frameon=False)
    fig.suptitle('Cross-dispersion PSF of the PRISM, measured on calibration-field stars', x=0.01, ha='left',
                 fontsize=12)
    fig.text(0.01, 0.95, f'Wing width = {PF.PSF_K} x core width. Stars fitted through the profile-fit model on the '
             'nod differences stage 3 reads.', fontsize=9, color=MUTED)
    fig.tight_layout(rect=(0, 0, 1, 0.945))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _stack(samples_dir, keys, r, name, bins):
    """Per-star binned mean of the own-image samples, then the median over stars:
    every star counts once."""
    per_d, per_m = [], []
    for k in keys:
        fn = os.path.join(samples_dir, f'{k}.npz')
        if not os.path.exists(fn):
            continue
        z = np.load(fn)
        pre = f'r{r}_{name}_'
        if pre + 'u' not in z:
            continue
        u, d, m = z[pre + 'u'], z[pre + 'd'], z[pre + 'm']
        idx = np.digitize(u, bins) - 1
        ok = (idx >= 0) & (idx < bins.size - 1) & np.isfinite(d) & np.isfinite(m)
        n = np.bincount(idx[ok], minlength=bins.size - 1)
        sd = np.bincount(idx[ok], d[ok], minlength=bins.size - 1)
        sm = np.bincount(idx[ok], m[ok], minlength=bins.size - 1)
        with np.errstate(invalid='ignore', divide='ignore'):
            per_d.append(np.where(n >= 3, sd / n, np.nan))
            per_m.append(np.where(n >= 3, sm / n, np.nan))
    if not per_d:
        return None
    D, M = np.array(per_d), np.array(per_m)
    with np.errstate(invalid='ignore'):
        return np.nanmedian(D, axis=0), np.nanmedian(M, axis=0), len(per_d)


def plot_profiles(out_dir, path, half=4.0, step=0.25):
    """(b) Per detector and range: the clean stars' own images stacked (each pixel as a
    fraction of the star's light, against its distance from the fitted trace centre),
    against the model with the current and the new table, and the residuals."""
    import matplotlib.pyplot as plt
    _style()
    sel, joint = read_selection(out_dir)
    rec2 = [r for r in read_records(os.path.join(out_dir, 'pass2')) if r['ranges']]
    clean = clean_in_pass2(rec2, sel)
    sdir = os.path.join(out_dir, 'samples')
    bins = np.arange(-half, half + 1e-9, step)
    bc = 0.5 * (bins[1:] + bins[:-1])
    nr = len(RANGES)
    fig, axs = plt.subplots(4, nr, figsize=(2.15 * nr, 9.2), sharex=True,
                            gridspec_kw=dict(height_ratios=[1.5, 0.8, 1.5, 0.8], hspace=0.12, wspace=0.12))
    for j, det in enumerate(('NRS1', 'NRS2')):
        for r in range(nr):
            ax, axr = axs[2 * j, r], axs[2 * j + 1, r]
            new = _stack(sdir, clean.get((det, r), []), r, 'new', bins)
            old = _stack(sdir, clean.get((det, r), []), r, 'old', bins)
            if new is None or old is None:
                ax.set_visible(False); axr.set_visible(False)
                continue
            dn, mn, n = new
            do, mo, _ = old
            ax.semilogy(bc, np.clip(dn, 1e-4, None), 'o', color=INK, ms=3, label='stars (median)')
            ax.semilogy(bc, mo, '--', color=OLD_C, lw=1.6, label='model, current table')
            ax.semilogy(bc, mn, '-', color=NEW_C, lw=1.8, label='model, new table')
            ax.set_ylim(3e-4, 1.0)
            pk = np.nanmax(mn)
            axr.axhline(0, color=MUTED, lw=0.8)
            axr.plot(bc, 100 * (do - mo) / pk, '--', color=OLD_C, lw=1.4)
            axr.plot(bc, 100 * (dn - mn) / pk, '-', color=NEW_C, lw=1.6)
            axr.set_ylim(-4, 4)
            ax.set_title(f'{det}  {RANGES[r][0]}–{RANGES[r][1]} µm\n{n} stars', fontsize=9)
            if r == 0:
                ax.set_ylabel('share of the star\'s\nlight per pixel')
                axr.set_ylabel('data − model\n[% of peak]')
            else:
                ax.set_yticklabels([]); axr.set_yticklabels([])
            if j == 1:
                axr.set_xlabel('rows from centre')
    axs[0, 0].legend(loc='lower center', frameon=False, fontsize=7.5)
    fig.suptitle('Stacked star profiles against the model PSF', x=0.01, ha='left', fontsize=12)
    fig.text(0.01, 0.955, 'Each star\'s nod difference less the model\'s negative copies, divided by its spectrum '
             'and throughput; the model is the same pixels\' image. Shutter clipping and pixel integration included.',
             fontsize=8.5, color=MUTED)
    fig.subplots_adjust(left=0.06, right=0.99, top=0.9, bottom=0.06)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_centre_law(out_dir, path, dy=0.229):
    """(c) The trace centre law: drift with wavelength per MSA quadrant and the slope
    with the star's position, refitted against the current ``CENTRE_LAW``."""
    import matplotlib.pyplot as plt
    _style()
    with open(os.path.join(out_dir, 'centre_law_new.json')) as fh:
        law = {k: {kk: (np.array([np.nan if x is None else x for x in vv], float) if isinstance(vv, list) else vv)
                   for kk, vv in v.items()} for k, v in json.load(fh).items()}
    lamc = range_centres()
    fig, axs = plt.subplots(2, 2, figsize=(10, 6.6), sharex=True)
    for j, det in enumerate(('NRS1', 'NRS2')):
        ax, axa = axs[0, j], axs[1, j]
        for q in (1, 2, 3, 4):
            key = f'Q{q}_{det}'
            if key in law:
                ax.errorbar(lamc, np.array(law[key]['D']) / dy, yerr=np.array(law[key]['eD']) / dy, fmt='o-',
                            color=QUAD_C[q], lw=1.8, ms=5, capsize=2, label=f'quadrant {q}, new ({law[key]["n"]} stars)')
            if key in PF.CENTRE_LAW:
                ax.plot(PF.PSF_LAM, PF.CENTRE_LAW[key]['D'] / dy, 's--', color=QUAD_C[q], lw=1.2, ms=4, alpha=0.8,
                        label=f'quadrant {q}, current')
        ax.errorbar(lamc, np.array(law[det]['D']) / dy, yerr=np.array(law[det]['eD']) / dy, fmt='o-', color=INK,
                    lw=1.2, ms=4, capsize=2, label='all quadrants, new')
        ax.plot(PF.PSF_LAM, PF.CENTRE_LAW[det]['D'] / dy, 's--', color=INK, lw=1, ms=3.5, alpha=0.7,
                label='all quadrants, current')
        ax.axhline(0, color=MUTED, lw=0.8)
        ax.set_title(f'{det}: trace shift with wavelength', loc='left')
        ax.legend(frameon=False, fontsize=7.5, loc='lower left', ncol=1)
        axa.errorbar(lamc, law[det]['alpha'], yerr=law[det]['ealpha'], fmt='o-', color=NEW_C, lw=1.8, ms=5,
                     capsize=2, label='new')
        axa.plot(PF.PSF_LAM, PF.CENTRE_LAW[det]['alpha'], 's--', color=OLD_C, lw=1.5, ms=4, label='current')
        axa.axhline(0, color=MUTED, lw=0.8)
        axa.set_title(f'{det}: extra shift per unit of source offset', loc='left')
        axa.set_xscale('log')
        axa.set_xticks([0.7, 1, 1.5, 2, 3, 4, 5])
        axa.set_xticklabels(['0.7', '1', '1.5', '2', '3', '4', '5'])
        axa.set_xlabel('wavelength [µm]')
        axa.legend(frameon=False, loc='lower left')
        if j == 0:
            ax.set_ylabel('shift from the 1.0–2.6 µm centre\n[detector rows]')
            axa.set_ylabel('shift / source offset\n(negative: towards the shutter centre)')
    fig.suptitle('Trace centre law: where the light sits, measured with no law applied', x=0.01, ha='left', fontsize=12)
    fig.text(0.01, 0.935, 'centre(λ) = c + D(λ) + α(λ)·c, with c the source position in the shutter; '
             'D per MSA quadrant, α per detector.', fontsize=9, color=MUTED)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_position(out_dir, path, ranges=(1, 4, 6)):
    """Diagnostic: each usable star's core width against its position in the shutter and
    on the detector (clean, edge and outlier stars)."""
    import matplotlib.pyplot as plt
    _style()
    sel, joint = read_selection(out_dir)
    recs = merged_records(out_dir)
    fig, axs = plt.subplots(2, len(ranges), figsize=(3.6 * len(ranges), 6.4))
    for i, r in enumerate(ranges):
        for j, (xkey, xlab) in enumerate((('pos', 'star position in the shutter [shutter pitch]'),
                                          ('x_start', 'slit start on the detector [column]'))):
            ax = axs[j, i]
            for det, mk in (('NRS1', 'o'), ('NRS2', '^')):
                for kind, col in (('clean', NEW_C), ('edge', EDGE_C), ('outlier', OLD_C)):
                    xs, ys = [], []
                    for rec in recs:
                        if rec['detector'] != det or r >= len(rec['ranges']):
                            continue
                        x = rec['ranges'][r].get('joint') or rec['ranges'][r].get('free')
                        if not x or not range_ok(x, sel['snr_min']):
                            continue
                        k = rec['key']
                        kd = 'outlier' if k in sel['outliers'] else ('edge' if k in sel['edge'] else 'clean')
                        if kd != kind:
                            continue
                        xs.append(sel['centre'].get(k, np.nan) if xkey == 'pos' else rec['x_start'])
                        ys.append(x['S'])
                    ax.plot(xs, ys, mk, color=col, ms=4, alpha=0.7, mec='none',
                            label=f'{det} {kind}' if (i == 0 and j == 0) else None)
            ax.set_xlabel(xlab)
            if i == 0:
                ax.set_ylabel('core sigma [rows]')
            ax.set_title(f'{RANGES[r][0]}–{RANGES[r][1]} µm', loc='left')
            ax.set_ylim(0.2, 1.0)
    axs[0, 0].legend(frameon=False, fontsize=7.5, ncol=2)
    fig.suptitle('Core width against position (diagnostic)', x=0.01, ha='left', fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_star_residuals(root, out_dir, key, path, exposure=0):
    """Diagnostic: one star's nod difference (one exposure) and its residuals with the
    current and the new table, each pixel divided by its column's peak, over every
    range (the fits of ``fit_star`` at each table, centre free)."""
    import matplotlib.pyplot as plt
    _style()
    obs = '_'.join(key.split('_')[:-2])
    task = tasks_for(root, [obs], {key})[0]
    exps = load_star(task)
    new = tables_for_model(read_table(out_dir))
    e = exps[exposure]
    img = {n: np.full(e.y.shape, np.nan) for n in ('data', 'old', 'new')}
    peak = np.full(e.y.shape, np.nan)
    for a, b in RANGES:
        try:
            RF = RangeFit(exps, (a, b))
        except (PF.ProfileFitFallback, ValueError):
            continue
        RF.set_floor(np.zeros(RF.M.npix))
        r0 = RF.fit(tables=PF.PSF_TABLES, clip_rounds=0)
        RF.set_floor(r0['_model'])
        for name, tab in (('old', PF.PSF_TABLES), ('new', new)):
            g = RF.fit(tables=tab)
            n = 0
            for k, m in RF.M.pix_masks:
                nk = int(m.sum())
                if k == exposure:
                    d = RF.M.pix['d'][n:n + nk]
                    img['data'][m] = d
                    img[name][m] = d - g['_model'][n:n + nk]
                    pk = np.zeros(e.y.shape)
                    pk[m] = np.abs(g['_model'][n:n + nk])
                    colmax = pk.max(axis=0)
                    peak[m] = np.broadcast_to(colmax, e.y.shape)[m]
                n += nk
    rows = np.flatnonzero(np.isfinite(img['data']).any(axis=1))
    cols = np.flatnonzero(np.isfinite(img['data']).any(axis=0))
    sl = (slice(rows.min(), rows.max() + 1), slice(cols.min(), cols.max() + 1))
    lam_row = np.nanmedian(e.wav[sl], axis=0)
    fig, axs = plt.subplots(3, 1, figsize=(11, 6.4), sharex=True)
    with np.errstate(invalid='ignore', divide='ignore'):
        panels = [('nod difference / column peak', img['data'] / peak, 1.0),
                  ('residual, current table [% of column peak]', 100 * img['old'] / peak, 5.0),
                  ('residual, new table [% of column peak]', 100 * img['new'] / peak, 5.0)]
    for ax, (title, im, v) in zip(axs, panels):
        h = ax.imshow(im[sl], origin='lower', aspect='auto', cmap='RdBu_r', vmin=-v, vmax=v, interpolation='nearest')
        ax.set_title(title, loc='left')
        ax.grid(False)
        ax.set_ylabel('row')
        fig.colorbar(h, ax=ax, pad=0.01)
    ticks = [t for t in (0.8, 1, 1.5, 2, 3, 4, 5) if np.nanmin(lam_row) <= t <= np.nanmax(lam_row)]
    ok = np.isfinite(lam_row)
    o = np.argsort(lam_row[ok])
    axs[-1].set_xticks(np.interp(ticks, lam_row[ok][o], np.arange(lam_row.size)[ok][o]))
    axs[-1].set_xticklabels([str(t) for t in ticks])
    axs[-1].set_xlabel('wavelength [µm]')
    fig.suptitle(f'{key}, exposure {e.name.split("_")[2]}: data and residuals', x=0.01, ha='left', fontsize=11)
    fig.text(0.01, 0.925, 'The star is the positive trace; the negative traces are the nod partners subtracted by '
             'stage 2b.', fontsize=9, color=MUTED)
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def write_summary(out_dir, log=print):
    """Old against new per detector and range (core, wing, FWHM, light beyond 1.5 rows on
    each side: data and both models); writes summary.json and summary.log."""
    table = read_table(out_dir)
    sides = {name: side_light(out_dir, name) for name in ('old', 'new')}
    lines, out = [], {}
    for det in ('NRS1', 'NRS2'):
        t, o = table[det], PF.PSF_TABLES[det]
        lines.append(f'== {det}')
        lines.append(' lam   n | core old -> new (p50)    | wing old -> new        | FWHM old -> new rows | '
                     'light < -1.5 rows: data / old / new | light > +1.5 rows: data / old / new')
        for r in range(len(RANGES)):
            fo, fn = fwhm_rows(o['S_rows'][r], o['f'][r]), fwhm_rows(t['S_rows'][r], t['f'][r])
            s_o, s_n = sides['old'].get((det, r)), sides['new'].get((det, r))
            rec = dict(lam=t['lam'][r], n=t['n'][r], S_old=float(o['S_rows'][r]), S_new=t['S_rows'][r],
                       S_p50=t['S_p50'][r], f_old=float(o['f'][r]), f_new=t['f'][r], f_err=t['f_err'][r],
                       fwhm_old=fo, fwhm_new=fn, sides_old=s_o, sides_new=s_n)
            out[f'{det}:{r}'] = rec
            fe = rec['f_err'] if rec['f_err'] is not None else np.nan
            sd = (f"{100 * s_n['neg_d']:5.2f}% / {100 * s_o['neg_m']:5.2f}% / {100 * s_n['neg_m']:5.2f}% | "
                  f"{100 * s_n['pos_d']:5.2f}% / {100 * s_o['pos_m']:5.2f}% / {100 * s_n['pos_m']:5.2f}%"
                  if s_o and s_n else '-')
            lines.append(f"{t['lam'][r]:4.2f} {t['n'][r]:3d} | {o['S_rows'][r]:.3f} -> {t['S_rows'][r]:.3f} "
                         f"({t['S_p50'][r]:.3f}) {100 * (t['S_rows'][r] / o['S_rows'][r] - 1):+5.1f}% | "
                         f"{o['f'][r]:.3f} -> {t['f'][r]:.3f}+-{fe:.3f} | {fo:.3f} -> {fn:.3f} "
                         f"{100 * (fn / fo - 1):+5.1f}% | {sd}")
    write_json(os.path.join(out_dir, 'summary.json'), out)
    with open(os.path.join(out_dir, 'summary.log'), 'w') as fh:
        fh.write('\n'.join(lines) + '\n')
    for x in lines:
        log(x)
    return out


def brightest_clean(out_dir, det, r=3):
    sel, _ = read_selection(out_dir)
    rec2 = {x['key']: x for x in read_records(os.path.join(out_dir, 'pass2')) if x['ranges']}
    keys = [k for k in clean_in_pass2(list(rec2.values()), sel).get((det, r), [])]
    return max(keys, key=lambda k: rec2[k]['ranges'][r]['free']['peak_snr']) if keys else None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='PRISM cross-dispersion PSF calibration on calibration-field stars')
    ap.add_argument('step', choices=['fit', 'select', 'refit', 'table', 'samples', 'figures'])
    ap.add_argument('--root', help='reduced CAMPFIRE root holding the observations')
    ap.add_argument('--out', required=True, help='output directory')
    ap.add_argument('--obs', nargs='*', default=[], help='observations')
    ap.add_argument('--snr-min', type=float, default=SNR_MIN)
    ap.add_argument('--edge-max', type=float, default=EDGE_MAX)
    ap.add_argument('--part', default=None, help='i/n: this process takes every n-th star from the i-th (fit, '
                    'refit, samples), to run a step as n processes')
    a = ap.parse_args(argv)
    part = tuple(int(x) for x in a.part.split('/')) if a.part else None
    log = lambda *x: print(*x, flush=True)          # noqa: E731
    if a.step == 'fit':
        run_stars(tasks_for(a.root, a.obs, part=part), os.path.join(a.out, 'pass1'), log=log)
    elif a.step == 'select':
        step_select(a.out, snr_min=a.snr_min, edge_max=a.edge_max, log=log)
    elif a.step == 'refit':
        step_refit(a.root, a.out, a.obs, log=log, part=part)
    elif a.step == 'table':
        step_table(a.out, log=log)
    elif a.step == 'samples':
        step_samples(a.root, a.out, a.obs, log=log, part=part)
    elif a.step == 'figures':
        write_fit_table(a.out)
        fd = os.path.join(a.out, 'figures')
        os.makedirs(fd, exist_ok=True)
        plot_widths(a.out, os.path.join(fd, 'psf_widths.png'))
        plot_profiles(a.out, os.path.join(fd, 'psf_profiles.png'))
        plot_centre_law(a.out, os.path.join(fd, 'centre_law.png'))
        plot_position(a.out, os.path.join(fd, 'psf_position.png'))
        write_summary(a.out, log=log)
        if a.root:
            for det in ('NRS1', 'NRS2'):
                key = brightest_clean(a.out, det)
                if key:
                    plot_star_residuals(a.root, a.out, key, os.path.join(fd, f'star_residuals_{det}.png'))


if __name__ == '__main__':
    main()
