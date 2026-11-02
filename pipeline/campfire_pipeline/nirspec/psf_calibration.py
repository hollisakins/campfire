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
                                x_start=int(h.get('SLTSTRT1', -1)), y_start=int(h.get('SLTSTRT2', -1)),
                                srcy=float(h.get('SRCYPOS', np.nan)), disperser=disperser))
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

    def __init__(self, exps, lam_range, centre_law_tables=None, nod_law=None):
        self.lam_range = tuple(lam_range)
        law = PF.NO_CENTRE_LAW if centre_law_tables is None else centre_law_tables
        ex = restrict(exps, lam_range)
        M0 = PF.ProfileModel(ex, centre_law_tables=law, nod_law=nod_law)
        g = M0.lam_grid
        g = g[(g > lam_range[0] - GRID_MARGIN) & (g < lam_range[1] + GRID_MARGIN)]
        if g.size < 3:
            raise PF.TooFewWavelengths('psf calibration: too few wavelengths in range')
        self.M = PF.ProfileModel(ex, grid=g, centre_law_tables=law, nod_law=nod_law)
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

    def fit_centres(self, tables=None, S=0.4, f=0.25, free_S=False, free_f=False, x0=None, use=None,
                    clip_rounds=N_CLIP):
        """Fit with the star's centre free in every exposure: the shared centre and drift plus
        a position offset per exposure that contains the star (the first held at zero), on top
        of the model's nod geometry. The PSF is ``tables``, or a constant core sigma ``S`` and
        wing fraction ``f``, each optionally free. For bright isolated stars only (the
        extraction fits no per-exposure offsets). Leaves the offsets in ``self.eoff``.
        Returns a dict like ``fit`` with ``offsets`` (slit units), ``S`` and ``f``."""
        M = self.M
        src = [k for k, e in enumerate(M.exps) if e.has_source]
        free = src[1:]
        x0 = list(x0) if x0 is not None else [M.c_prior, 0.0]
        ps = [x0[0] + 1.0, x0[1] + 1.0]
        lo = [M.c_prior - CENTRE_BOUND + 1.0, 1.0 - DRIFT_BOUND]
        hi = [M.c_prior + CENTRE_BOUND + 1.0, 1.0 + DRIFT_BOUND]
        if tables is None and free_S:
            ps.append(S); lo.append(S_BOUNDS[0]); hi.append(S_BOUNDS[1])
        if tables is None and free_f:
            ps.append(f); lo.append(0.0); hi.append(0.8)
        nshape = len(ps)
        ps += [0.0] * len(free); lo += [-OFFSET_BOUND] * len(free); hi += [OFFSET_BOUND] * len(free)
        lo, hi = np.array(lo), np.array(hi)

        def unpack(x):
            i = 2
            S_, f_ = S, f
            if tables is None and free_S:
                S_, i = x[i], i + 1
            if tables is None and free_f:
                f_, i = x[i], i + 1
            e = np.zeros(len(M.exps))
            e[free] = x[nshape:]
            return x[0] - 1.0, x[1] - 1.0, (tables if tables is not None else constant_tables(S_, f_)), e, S_, f_

        def resid(x):
            c0, dr, tab, e, _, _ = unpack(x)
            self.eoff = e
            fl, _, A = self.solve(c0, dr, tab)
            return np.where(M.use, (M.pix['d'] - A @ fl) / np.sqrt(M.pix['v']), 0.0)

        x = np.clip(np.array(ps), lo + 1e-6, hi - 1e-6)
        M.use = np.ones(M.npix, bool) if use is None else use.copy()
        nround = 0 if use is not None else clip_rounds
        for it in range(nround + 1):
            res = least_squares(resid, x, bounds=(lo, hi), x_scale='jac', ftol=1e-6, xtol=1e-6, gtol=1e-6)
            x = res.x
            if it == nround:
                break
            saved = M.use
            M.use = np.ones(M.npix, bool)
            new = np.abs(resid(x)) < CLIP
            M.use = saved
            if np.array_equal(new, M.use):
                break
            M.use = new
        c0, dr, tab, e, S_, f_ = unpack(x)
        if tables is not None:
            S_ = f_ = np.nan                 # the PSF is the table's, not one constant
        self.eoff = e
        fl, good, A = self.solve(c0, dr, tab)
        model = A @ fl
        r = (M.pix['d'] - model) / np.sqrt(M.pix['v'])
        rr = (M.pix['d'] - model) / np.sqrt(self.v_raw)
        ndof = max(float(np.sum(M.dof_weight()[M.use])) - int(good.sum()) - x.size, 1.0)
        chi2 = float(np.sum(r[M.use] ** 2))
        return dict(c0=float(c0), drift=float(dr), c_node=float(self.centre_at(c0, dr, self.lam_c)), S=float(S_),
                    f=float(f_), offsets=e.tolist(), chi2=chi2, chi2_raw=float(np.sum(rr[M.use] ** 2)), ndof=ndof,
                    chi2r=chi2 / ndof, chi2r_raw=float(np.sum(rr[M.use] ** 2)) / ndof, peak_snr=self.peak_snr(model),
                    _x=[float(c0), float(dr)], _use=M.use.copy(), _model=model, _tables=tab, _f=fl)

    def exposure_pixels(self, k, fitres):
        """Exposure k's fitted pixels at a fit of ``fit_centres`` (``self.eoff`` as it left
        them): the 2-D mask, data and model of its nod difference, the noise, the distance
        from its trace centre [rows] and the column's light (spectrum x throughput)."""
        M = self.M
        n = 0
        for kk, msk in M.pix_masks:
            nk = int(msk.sum())
            if kk == k:
                sl = slice(n, n + nk)
                break
            n += nk
        else:
            return None
        e = M.exps[k]
        c0, dr = fitres['_x']
        p = self.shape(c0, dr)
        y, lam, dy = e.y[msk], e.wav[msk], e.dy[msk]
        fvals = np.where(np.isfinite(fitres['_f']), fitres['_f'], np.nan)
        spec = np.interp(lam, M.lam_grid, fvals, left=np.nan, right=np.nan)
        norm = spec * M.throughput(p, self.eoff, k, lam, dy)
        cen = M.centre(p, self.eoff, k, lam)
        return dict(mask=msk, d=M.pix['d'][sl], m=fitres['_model'][sl], sig=np.sqrt(self.v_raw[sl]),
                    u=(y - cen) / dy, norm=norm, lam=lam, use=M.use[sl])


# ---------------------------------------------------------------------------
# Nod offsets (the measurement behind profile_fit.NOD_OFFSET_LAW)
# ---------------------------------------------------------------------------
MSA_NCOL = 365          # shutters per row of an MSA quadrant: SHUTTRID = (row - 1) * 365 + column
OFFSET_BOUND = 0.1      # per-exposure offset, slit units


def exposure_meta(exps):
    """Per exposure, what could describe its nod offset: the nod (file sequence number and
    PATT_NUM), the commanded dither offset (XOFFSET / YOFFSET, arcsec), the planned source
    position SRCYPOS, the MSA quadrant and the source shutter's row and column (from SHUTTRID),
    the slitlet's start on the detector, the disperser."""
    from astropy.io import fits
    out = []
    for e in exps:
        m = dict(name=e.name, nod=int(e.name.split('_')[2]), src_y=float(e.src_y), has_source=bool(e.has_source),
                 grating=e.grating, detector=e.detector)
        try:
            h0, h1 = fits.getheader(e.path, 0), fits.getheader(e.path, 'SCI')
            sid = int(h1.get('SHUTTRID', 0))
            m.update(patt_num=int(h0.get('PATT_NUM', -1)), xoffset=float(h0.get('XOFFSET', np.nan)),
                     yoffset=float(h0.get('YOFFSET', np.nan)), quadrant=int(h1.get('MSAQUAD', 0)),
                     x_start=int(h1.get('SLTSTRT1', -1)), y_start=int(h1.get('SLTSTRT2', -1)),
                     shutter_row=(sid - 1) // MSA_NCOL + 1 if sid > 0 else -1,
                     shutter_col=(sid - 1) % MSA_NCOL + 1 if sid > 0 else -1)
        except (OSError, KeyError, ValueError, TypeError):
            pass
        out.append(m)
    return out


def _fit_offsets(RF, f_wing, x0, use):
    """Core width, centre, drift and a free position offset per exposure that contains the
    source (the first one held at zero; the shared centre absorbs it), on fixed pixels.
    Returns (offsets relative to their mean over those exposures, their errors, chi2), slit units."""
    M = RF.M
    src = [k for k, e in enumerate(M.exps) if e.has_source]
    free = src[1:]
    lo = np.array([x0[0] + 1 - CENTRE_BOUND, 1 - DRIFT_BOUND, S_BOUNDS[0]] + [-OFFSET_BOUND] * len(free))
    hi = np.array([x0[0] + 1 + CENTRE_BOUND, 1 + DRIFT_BOUND, S_BOUNDS[1]] + [OFFSET_BOUND] * len(free))

    def eoff_of(x):
        e = np.zeros(len(M.exps))
        e[free] = x[3:]
        return e

    def resid(x):
        RF.eoff = eoff_of(x)
        f, _, A = RF.solve(x[0] - 1.0, x[1] - 1.0, constant_tables(x[2], f_wing))
        return np.where(use, (M.pix['d'] - A @ f) / np.sqrt(M.pix['v']), 0.0)
    x = np.clip(np.array([x0[0] + 1.0, x0[1] + 1.0, x0[2]] + [0.0] * len(free)), lo + 1e-6, hi - 1e-6)
    try:
        res = least_squares(resid, x, bounds=(lo, hi), x_scale='jac', ftol=1e-6, xtol=1e-6, gtol=1e-6)
    finally:
        RF.eoff = np.zeros(len(M.exps))
    chi2 = float(np.sum(res.fun ** 2))
    ndof = max(float(np.sum(M.dof_weight()[use])) - len(x), 1.0)
    J = res.jac[np.flatnonzero(use)]
    cov = np.linalg.pinv(J.T @ J) * max(chi2 / ndof, 1.0)
    # offsets of the source exposures, the first fixed at 0, then relative to their mean
    n = len(src)
    A = np.eye(n) - 1.0 / n
    C = np.zeros((n, n))
    C[1:, 1:] = cov[3:, 3:]
    e_src = A @ np.concatenate([[0.0], res.x[3:]])
    err_src = np.sqrt(np.abs(np.diag(A @ C @ A.T)))
    off, err = np.full(len(M.exps), np.nan), np.full(len(M.exps), np.nan)
    off[src], err[src] = e_src, err_src
    return off, err, chi2


def grating_ranges(exps, n=3):
    """``n`` wavelength ranges, equal in log wavelength, over a grating's coverage."""
    lam = np.concatenate([e.wav[np.isfinite(e.wav)] for e in exps if e.use_data])
    lo, hi = np.percentile(lam, [2, 98])
    edges = np.exp(np.linspace(np.log(lo), np.log(hi), n + 1))
    return tuple((float(a), float(b)) for a, b in zip(edges[:-1], edges[1:]))


def measure_offsets(task, ranges=None, snr_min=10.0, edge_max=0.45, exps=None):
    """Per range of one star: the per-exposure offsets fitted freely (no nod law in the model),
    and the chi2 with offsets held at zero, free, and given by ``NOD_OFFSET_LAW`` (each with the
    core width, centre and drift refitted, on the same pixels). Ranges: ``RANGES`` for the
    PRISM, else ``grating_ranges``. Only ranges with peak S/N >= ``snr_min`` and the star within
    ``edge_max`` of the shutter centre."""
    exps = load_star(task) if exps is None else exps
    meta = exposure_meta(exps)
    is_prism = exps[0].grating.upper().startswith('PRISM')
    ranges = ranges or (RANGES if is_prism else grating_ranges(exps))
    rec = dict(key=task.key, obs=task.obs, source_id=task.source_id, detector=task.detector,
               grating=exps[0].grating, exposures=meta, ranges=[])
    for r, (a, b) in enumerate(ranges):
        row = dict(range=r, lam=[a, b])
        try:
            RF = RangeFit(exps, (a, b), nod_law=False)
        except (PF.ProfileFitFallback, ValueError) as err:
            row['error'] = str(err)
            rec['ranges'].append(row)
            continue
        f_wing = float(np.interp(0.5 * (a + b), PF.PSF_LAM, PF.PSF_TABLES[task.detector]['f']))
        RF.set_floor(np.zeros(RF.M.npix))
        ref = RF.fit(tables=PF.PSF_TABLES)
        RF.set_floor(ref['_model'])
        fr = RF.fit(f_wing=f_wing, x0=ref['_x'] + [0.45])
        row.update(peak_snr=fr['peak_snr'], c_node=fr['c_node'], S=fr.get('S'), chi2_zero=fr['chi2'], ndof=fr['ndof'])
        if fr['peak_snr'] < snr_min or abs(fr['c_node']) > edge_max:
            rec['ranges'].append(row)
            continue
        off, err, chi2 = _fit_offsets(RF, f_wing, fr['_x'], fr['_use'])
        RF.eoff = PF.nod_shifts(RF.M.exps)
        fl = RF.fit(f_wing=f_wing, x0=fr['_x'], use=fr['_use'])
        RF.eoff = np.zeros(len(RF.M.exps))
        row.update(offsets=off, errors=err, chi2_free=chi2, chi2_law=fl['chi2'],
                   law=PF.nod_shifts(RF.M.exps), dy=float(np.nanmedian(RF.M.exps[0].dy)))
        rec['ranges'].append(row)
    return rec


def run_offsets(tasks, out_dir, snr_min=10.0, log=print):
    os.makedirs(out_dir, exist_ok=True)
    for i, t in enumerate(tasks):
        path = os.path.join(out_dir, f'{t.key}.json')
        if os.path.exists(path):
            continue
        try:
            rec = measure_offsets(t, snr_min=snr_min)
        except Exception as err:
            log(f'{t.key}: failed: {type(err).__name__}: {err}')
            rec = dict(key=t.key, error=str(err), ranges=[])
        write_json(path, rec)
        n = sum('offsets' in x for x in rec['ranges'])
        log(f'[{i + 1}/{len(tasks)}] {t.key}: {n} ranges measured')


def offset_rows(records, group=None):
    """Per measured exposure (contains the source, planned position not a placeholder): the
    offset and its error, and the candidate descriptions. ``group``: a function of the record
    giving a label (default the disperser)."""
    rows = []
    for rec in records:
        meta = rec.get('exposures') or []
        for row in rec['ranges']:
            if 'offsets' not in row:
                continue
            off, err = np.array(row['offsets'], float), np.array(row['errors'], float)
            ok = np.array([m['has_source'] and np.isfinite(o) and np.isfinite(m.get('yoffset', np.nan))
                           for m, o in zip(meta, off)])
            sy = np.array([m['src_y'] for m in meta])
            if any(s != 0.0 for s in sy[ok]):
                ok &= sy != 0.0
            if ok.sum() < 2:
                continue
            yo = np.array([m.get('yoffset', np.nan) for m in meta])
            for k in np.flatnonzero(ok):
                m = meta[k]
                rows.append(dict(key=rec['key'], obs=rec.get('obs'), det=rec.get('detector'),
                                 grating=rec.get('grating'), range=row['range'], lam=0.5 * sum(row['lam']),
                                 nod=m['nod'], off=float(off[k] - off[ok].mean()), err=float(err[k]),
                                 planned=float(sy[k] - sy[ok].mean()), dyoff=float(yo[k] - yo[ok].mean()),
                                 y_start=m.get('y_start'), x_start=m.get('x_start'), quad=m.get('quadrant'),
                                 shutter_row=m.get('shutter_row'), shutter_col=m.get('shutter_col'),
                                 dy=row['dy'], snr=row['peak_snr'], label=group(rec) if group else rec.get('grating')))
    return rows


def _wlsq(X, y, e, floor):
    w = 1.0 / (e ** 2 + floor ** 2)
    A = X * np.sqrt(w)[:, None]
    p, *_ = np.linalg.lstsq(A, y * np.sqrt(w), rcond=None)
    return p


def offset_law_models(rows, floor=None):
    """Compare descriptions of the measured offsets (slit units). Each is fitted by weighted
    least squares (errors plus an intrinsic scatter ``floor``, default the scatter about the
    planned positions), scored by weighted rms, and by the rms when each observation is
    predicted from the others (does the description carry over?).

    planned_1   : offset = SRCYPOS_k - mean (no parameter; the planning metadata)
    planned_k   : offset = kappa x (SRCYPOS_k - mean)
    row_x_nod   : offset = (a + b x slitlet detector row) x commanded nod step (YOFFSET, arcsec)
    msa_x_nod   : offset = (a_quadrant + b x source shutter row) x commanded nod step
    row         : offset = (a + b x slitlet detector row) x sign of the nod step (not proportional)"""
    y = np.array([r['off'] for r in rows])
    e = np.array([r['err'] for r in rows])
    P = np.array([r['planned'] for r in rows])
    d = np.array([r['dyoff'] for r in rows])
    row_ = np.array([r['y_start'] for r in rows], float) / 1000.0
    srow = np.array([r['shutter_row'] for r in rows], float) / 100.0
    quad = np.array([r['quad'] for r in rows])
    obs = np.array([r['obs'] for r in rows])
    if floor is None:
        floor = float(1.4826 * np.median(np.abs(y - P)))
    quads = sorted(set(quad))
    designs = {
        'planned_1': None,
        'planned_k': np.column_stack([P]),
        'row_x_nod': np.column_stack([d, d * row_]),
        'msa_x_nod': np.column_stack([d * (quad == q) for q in quads] + [d * srow]),
        'row': np.column_stack([np.sign(d), np.sign(d) * row_]),
        'zero': np.zeros((len(rows), 0)),
    }
    w = 1.0 / (e ** 2 + floor ** 2)
    out = {}
    for name, X in designs.items():
        if X is None:
            pred, p = P, []
        elif X.shape[1] == 0:
            pred, p = np.zeros_like(y), []
        else:
            p = _wlsq(X, y, e, floor)
            pred = X @ p
        res = y - pred
        cv = np.full_like(y, np.nan)
        for o in np.unique(obs):
            m = obs == o
            if X is None:
                cv[m] = res[m]
            elif X.shape[1] == 0:
                cv[m] = y[m]
            else:
                if (~m).sum() <= X.shape[1] or np.linalg.matrix_rank(X[~m]) < X.shape[1]:
                    continue
                q = _wlsq(X[~m], y[~m], e[~m], floor)
                cv[m] = y[m] - X[m] @ q
        ok = np.isfinite(cv)
        out[name] = dict(params=[float(v) for v in p], wrms=float(np.sqrt(np.sum(w * res ** 2) / np.sum(w))),
                         chi2r=float(np.sum(res ** 2 / (e ** 2 + floor ** 2)) / max(len(y) - len(p), 1)),
                         cv_wrms=float(np.sqrt(np.sum(w[ok] * cv[ok] ** 2) / np.sum(w[ok]))), n=len(y))
    out['_floor'] = floor
    out['_quads'] = [int(q) for q in quads]
    return out


def residual_trends(rows):
    """Slopes of the offsets left after the planned positions (offset - planned) against
    detector column, wavelength, detector row and nod step (weighted least squares)."""
    y = np.array([r['off'] - r['planned'] for r in rows])
    e = np.array([r['err'] for r in rows])
    floor = float(1.4826 * np.median(np.abs(y)))
    out = {}
    for name, x in (('x_start', [r['x_start'] / 1000.0 for r in rows]), ('lam', [r['lam'] for r in rows]),
                    ('y_start', [r['y_start'] / 1000.0 for r in rows]), ('planned', [r['planned'] for r in rows])):
        x = np.array(x, float)
        X = np.column_stack([np.ones_like(x), x])
        w = 1.0 / (e ** 2 + floor ** 2)
        A = X * np.sqrt(w)[:, None]
        p, *_ = np.linalg.lstsq(A, y * np.sqrt(w), rcond=None)
        r = y - X @ p
        cov = np.linalg.pinv(A.T @ A) * max(np.sum(w * r ** 2) / max(len(y) - 2, 1), 1.0)
        out[name] = dict(slope=float(p[1]), err=float(np.sqrt(cov[1, 1])), intercept=float(p[0]))
    return out


def step_offset_law(out_dir, log=print):
    """Compare the descriptions of the measured nod offsets (``offset_law_models``), overall,
    for the PRISM and for each grating, plus the trends left after the planned positions;
    writes offset_law.json and offset_law.log."""
    recs = [r for r in read_records(os.path.join(out_dir, 'offsets')) if r.get('ranges')]
    rows = offset_rows(recs)
    groups = {'all': rows}
    for g in sorted({r['grating'] for r in rows}):
        groups[g] = [r for r in rows if r['grating'] == g]
    lines, out = [], {}
    for name, rr in groups.items():
        if len(rr) < 10:
            continue
        m = offset_law_models(rr)
        out[name] = m
        nst = len({r['key'] for r in rr})
        lines.append(f'== {name}: {len(rr)} exposure offsets from {nst} stars; scatter floor {m["_floor"]:.4f} slit units')
        lines.append('   description   params                         wrms [slit units]  chi2/dof  predicted for a held-out '
                     'observation: wrms')
        for k, v in m.items():
            if k.startswith('_'):
                continue
            ps = ' '.join(f'{p:+.4f}' for p in v['params'])
            lines.append(f'   {k:12s}  {ps:30s} {v["wrms"]:.4f}            {v["chi2r"]:6.2f}    {v["cv_wrms"]:.4f}')
    tr = residual_trends(rows)
    out['_trends'] = tr
    lines.append('== offsets left after the planned positions, weighted slopes (all dispersers):')
    for k, v in tr.items():
        lines.append(f'   against {k:8s}: {v["slope"]:+.5f} +- {v["err"]:.5f} slit units per unit '
                     f'(x_start, y_start in 1000 px; lam in um; planned in slit units)')
    write_json(os.path.join(out_dir, 'offset_law.json'), out)
    with open(os.path.join(out_dir, 'offset_law.log'), 'w') as fh:
        fh.write('\n'.join(lines) + '\n')
    for x in lines:
        log(x)
    return rows, out


def plot_offset_law(out_dir, path):
    """The nod offset law: measured per-exposure offsets against the planned ones (SRCYPOS),
    against the slitlet's detector row per unit nod step, and what is left against wavelength
    and detector column."""
    import matplotlib.pyplot as plt
    _style()
    recs = [r for r in read_records(os.path.join(out_dir, 'offsets')) if r.get('ranges')]
    rows = offset_rows(recs)
    prism = np.array([r['grating'].upper().startswith('PRISM') for r in rows])
    off = np.array([r['off'] / r['dy'] for r in rows])
    pln = np.array([r['planned'] / r['dy'] for r in rows])
    err = np.array([r['err'] / r['dy'] for r in rows])
    good = err < 0.02
    step = np.array([r['dyoff'] for r in rows])
    yst = np.array([r['y_start'] for r in rows], float)
    lam = np.array([r['lam'] for r in rows])
    xst = np.array([r['x_start'] for r in rows], float)
    fig, axs = plt.subplots(2, 2, figsize=(10.5, 8.2))
    ax = axs[0, 0]
    for sel_, col, lab in ((prism, NEW_C, 'PRISM'), (~prism, OLD_C, 'gratings')):
        m = sel_ & good
        ax.plot(pln[m], off[m], 'o', ms=3, color=col, alpha=0.45, mec='none', label=f'{lab} ({m.sum()} exposure-ranges)')
    lim = np.nanmax(np.abs(pln)) * 1.15
    ax.plot([-lim, lim], [-lim, lim], '-', color=INK, lw=1.2, label='law: measured = planned')
    ax.set_xlabel('planned shift from SRCYPOS [detector rows]')
    ax.set_ylabel('measured shift [detector rows]')
    ax.set_title('Each nod\'s measured position against its planned one', loc='left')
    ax.legend(frameon=False, fontsize=8)
    ax = axs[0, 1]
    nz = good & (np.abs(step) > 0.1)
    for sel_, col, lab in ((prism, NEW_C, 'PRISM'), (~prism, OLD_C, 'gratings')):
        m = nz & sel_
        ax.plot(yst[m], off[m] / step[m], 'o', ms=3, color=col, alpha=0.45, mec='none', label=f'measured, {lab}')
    ax.plot(yst[nz], pln[nz] / step[nz], '.', ms=2, color=INK, alpha=0.6, label='planned (SRCYPOS)')
    ax.axhline(0, color=MUTED, lw=0.8)
    ax.set_xlabel('slitlet start on the detector [row]')
    ax.set_ylabel('shift per arcsec of commanded nod [rows/arcsec]')
    ax.set_title('The field dependence: both follow the detector row', loc='left')
    ax.legend(frameon=False, fontsize=8)
    for ax, x, xl, bins in ((axs[1, 0], lam, 'wavelength [µm]', np.array([0.7, 1.0, 1.5, 2.0, 2.6, 3.3, 4.1, 5.3])),
                            (axs[1, 1], xst, 'slitlet start on the detector [column]', np.linspace(0, 2048, 9))):
        res = off - pln
        for sel_, col, lab, mk in ((prism, NEW_C, 'PRISM', 'o'), (~prism, OLD_C, 'gratings', 's')):
            m = sel_ & good
            idx = np.digitize(x[m], bins) - 1
            med = [np.median(res[m][idx == i]) if np.sum(idx == i) > 5 else np.nan for i in range(bins.size - 1)]
            sem = [1.2533 * np.std(res[m][idx == i]) / np.sqrt(np.sum(idx == i)) if np.sum(idx == i) > 5 else np.nan
                   for i in range(bins.size - 1)]
            ax.errorbar(0.5 * (bins[1:] + bins[:-1]), med, yerr=sem, fmt=mk + '-', color=col, ms=5, capsize=2,
                        label=f'{lab}, median')
        ax.axhline(0, color=MUTED, lw=0.8)
        ax.set_xlabel(xl)
        ax.set_ylabel('measured - planned [detector rows]')
        ax.set_ylim(-0.03, 0.03)
        ax.legend(frameon=False, fontsize=8)
    axs[1, 0].set_title('What is left, against wavelength', loc='left')
    axs[1, 1].set_title('What is left, against detector column', loc='left')
    fig.suptitle('Nod offset law: every nod sits at its own planned position', x=0.01, ha='left', fontsize=12)
    fig.text(0.01, 0.94, 'Per-exposure positions of calibration stars fitted freely through the profile model '
             '(relative to each star\'s mean), against SRCYPOS from the MSA planning metadata.', fontsize=9, color=MUTED)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Per star
# ---------------------------------------------------------------------------
def _public(d):
    return {k: v for k, v in d.items() if not k.startswith('_')}


def current_wing(det):
    return dict(zip(range(len(RANGES)), np.asarray(PF.PSF_TABLES[det]['f'], float)))


def fit_star(task, exps=None, wing=None, f_grid=F_GRID, sample_tables=None, snr_floor=5.0, log=None, nod_law=None):
    """Fit one star in every range.

    Per range: a reference fit with the current table (centre and drift free), which
    sets the variance floor; then the core sigma free at wing fraction ``wing[r]``
    (default: the current table's at the range centre), with clipping; then, when
    ``f_grid`` is given, the range's peak S/N reaches ``snr_floor`` and the star is not
    clearly off-centre (``EDGE_MAX`` + 0.05: no use to the joint wing fit), the core
    sigma and centre refitted at each wing fraction of the grid on the same pixels.

    sample_tables : {name: PSF tables or None (the current table)}: also return the
        stacking samples (``RangeFit.samples``) of a fit at each of these tables.
    nod_law : the model's nod geometry (``ProfileModel``): None for ``NOD_OFFSET_LAW``,
        False for one position shared by all exposures (the geometry before 2026-11-02).
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
            RF = RangeFit(exps, (a, b), nod_law=nod_law)
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


def side_light(out_dir, name, edge=1.5, samples='samples'):
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
            fn = os.path.join(out_dir, samples, f'{k}.npz')
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
            V = np.array(vals)
            v = np.median(V, axis=0)
            # one-sidedness per star: (data - model) beyond +edge minus the same beyond -edge
            asym = (V[:, 2] - V[:, 3]) - (V[:, 0] - V[:, 1])
            out[(det, r)] = dict(neg_d=v[0], neg_m=v[1], pos_d=v[2], pos_m=v[3], core_d=v[4], core_m=v[5],
                                 n=len(vals), asym=float(np.median(asym)),
                                 asym_err=float(1.2533 * np.std(asym) / np.sqrt(len(asym))),
                                 asym_frac_pos=float(np.mean(asym > 0)))
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
            for st in ('ref', 'free', 'joint', 'fit_old', 'fit_new', 'fit_zero'):
                x = row.get(st)
                if x:
                    for k in ('e_S', 'e_c', 'S', 'c_node', 'chi2r', 'peak_snr'):
                        if k in x and x[k] is None:
                            x[k] = np.nan
    return out


def fit_table(records, stages=('ref', 'free', 'joint', 'fit_old', 'fit_new', 'fit_zero')):
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
              log=print, nod_law=None):
    """``fit_star`` over tasks, one JSON per star in ``out_dir`` (skipped when present
    unless ``overwrite``); samples (``sample_tables``) to ``sample_dir`` as npz.
    ``wing_for(det)`` gives the per-range wing fractions (default: the current table);
    ``nod_law`` as in ``fit_star``."""
    os.makedirs(out_dir, exist_ok=True)
    for i, t in enumerate(tasks):
        path = os.path.join(out_dir, f'{t.key}.json')
        if os.path.exists(path) and not overwrite:
            continue
        try:
            rec, samp = fit_star(t, wing=None if wing_for is None else wing_for(t.detector), f_grid=f_grid,
                                 sample_tables=sample_tables, log=log, nod_law=nod_law)
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
def tasks_for(root, observations, keys=None, part=None, dispersers=('prism_clear',)):
    """Star tasks of the observations (only ``keys`` when given); ``part`` = (i, n) keeps
    every n-th task from the i-th, to split a step over n processes."""
    out = []
    for name in observations:
        for disp in dispersers:
            out += [t for t in star_tasks(root, name, disperser=disp) if keys is None or t.key in keys]
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


def step_refit(root, out_dir, observations, log=print, part=None, nod_law=None):
    """Pass 2: every usable star (clean and edge) refitted at the joint wing fraction."""
    sel, joint = read_selection(out_dir)
    keys = {k for v in sel['usable'].values() for k in v}
    tasks = tasks_for(root, observations, keys, part=part)
    run_stars(tasks, os.path.join(out_dir, 'pass2'), wing_for=wing_for_joint(joint), f_grid=None, log=log,
              nod_law=nod_law)


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


def step_samples(root, out_dir, observations, log=print, part=None, nod_law=None, extra_tables=None):
    """Pass 3: every clean star fitted with the current and the new table (and
    ``extra_tables``: {name: table directory}, e.g. an earlier refit), with the stacking
    samples (``RangeFit.samples``) for the profile figure."""
    sel, joint = read_selection(out_dir)
    rec2 = [r for r in read_records(os.path.join(out_dir, 'pass2')) if r['ranges']]
    clean = clean_in_pass2(rec2, sel)
    keys = {k for v in clean.values() for k in v}
    tabs = {'old': PF.PSF_TABLES, 'new': tables_for_model(read_table(out_dir))}
    for name, d in (extra_tables or {}).items():
        tabs[name] = tables_for_model(read_table(d))
    run_stars(tasks_for(root, observations, keys, part=part), os.path.join(out_dir, 'pass3'),
              wing_for=wing_for_joint(joint), f_grid=None, sample_tables=tabs,
              sample_dir=os.path.join(out_dir, 'samples'), log=log, nod_law=nod_law)


def free_centre_star(task, joint, table, ranges_of_star, exps=None):
    """One star, each range in ``ranges_of_star``, its centre free in every exposure
    (``RangeFit.fit_centres``): with (a) the PSF at the clean stars' median core width and the
    joint wing fraction ('med'), (b) the new table ('p25'), (c) its own core width and wing
    fraction ('own'). Returns (record, samples of 'med' and 'p25')."""
    exps = load_star(task) if exps is None else exps
    det = task.detector
    newtab = tables_for_model(table)
    rec = dict(key=task.key, obs=task.obs, detector=det, ranges=[])
    samples = {}
    for r in ranges_of_star:
        a, b = RANGES[r]
        try:
            RF = RangeFit(exps, (a, b))
        except (PF.ProfileFitFallback, ValueError) as err:
            rec['ranges'].append(dict(range=r, error=str(err)))
            continue
        fj = float(joint[(det, r)]['f']) if (det, r) in joint else float(table[det]['f'][r])
        RF.set_floor(np.zeros(RF.M.npix))
        ref = RF.fit(tables=PF.PSF_TABLES)
        RF.set_floor(ref['_model'])
        fr = RF.fit(f_wing=fj, x0=ref['_x'] + [0.4])
        row = dict(range=r, lam=[a, b])
        for name, kw in (('med', dict(tables=constant_tables(table[det]['S_p50'][r], fj))),
                         ('p25', dict(tables=newtab)),
                         ('own', dict(S=fr['S'], f=fj, free_S=True, free_f=True))):
            g = RF.fit_centres(x0=fr['_x'][:2], use=fr['_use'], **kw)
            row[name] = {k: v for k, v in g.items() if not k.startswith('_')}
            if name != 'own':
                samples[(r, name)] = RF.samples(g, g['_tables'])
        RF.eoff = np.zeros(len(RF.M.exps))
        rec['ranges'].append(row)
    return rec, samples


def step_free_centres(root, out_dir, observations, part=None, log=print):
    """Every clean star (pass 2 selection) through ``free_centre_star``: records in pass4/,
    stacking samples in samples_free/."""
    sel, joint = read_selection(out_dir)
    rec2 = [r for r in read_records(os.path.join(out_dir, 'pass2')) if r['ranges']]
    clean = clean_in_pass2(rec2, sel)
    table = read_table(out_dir)
    ranges_of = {}
    for (det, r), keys in clean.items():
        for k in keys:
            ranges_of.setdefault(k, []).append(r)
    tasks = tasks_for(root, observations, set(ranges_of), part=part)
    d4, ds = os.path.join(out_dir, 'pass4'), os.path.join(out_dir, 'samples_free')
    os.makedirs(d4, exist_ok=True)
    os.makedirs(ds, exist_ok=True)
    for i, t in enumerate(tasks):
        path = os.path.join(d4, f'{t.key}.json')
        if os.path.exists(path):
            continue
        try:
            rec, samp = free_centre_star(t, joint, table, sorted(ranges_of[t.key]))
        except Exception as err:
            log(f'{t.key}: failed: {type(err).__name__}: {err}')
            rec, samp = dict(key=t.key, error=str(err), ranges=[]), {}
        write_json(path, rec)
        if samp:
            np.savez_compressed(os.path.join(ds, f'{t.key}.npz'),
                                **{f'r{r}_{name}_{k}': v for (r, name), s in samp.items() for k, v in s.items()})
        log(f'[{i + 1}/{len(tasks)}] {t.key}')


def merged_records(out_dir):
    """Pass 1 records with pass 2's fits as 'joint' and pass 3's table fits added."""
    recs = {r['key']: r for r in read_records(os.path.join(out_dir, 'pass1'))}
    for sub, pairs in (('pass2', (('free', 'joint'),)),
                       ('pass3', (('fit_old', 'fit_old'), ('fit_new', 'fit_new'), ('fit_zero', 'fit_zero')))):
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
COMPARE_LABELS = {'zero': 'refit without the nod law'}
QUAD_C = {1: '#2a78d6', 2: '#eb6834', 3: '#1baf7a', 4: '#6250d6'}


def _style():
    import matplotlib as mpl
    mpl.rcParams.update({'font.size': 10, 'axes.titlesize': 10.5, 'axes.labelsize': 10, 'legend.fontsize': 8.5,
                         'axes.edgecolor': MUTED, 'axes.labelcolor': INK, 'xtick.color': MUTED,
                         'ytick.color': MUTED, 'axes.grid': True, 'grid.color': '#e4e3df', 'grid.linewidth': 0.6,
                         'axes.spines.top': False, 'axes.spines.right': False, 'figure.facecolor': '#fcfcfb',
                         'axes.facecolor': '#fcfcfb', 'savefig.facecolor': '#fcfcfb'})


def plot_widths(out_dir, path, compare=None):
    """(a) Core width and wing fraction against wavelength per detector: every clean
    star's width (at the joint wing fraction), their 25th percentile (the new table),
    their median, the current table and (``compare``: {label: directory}) earlier refits."""
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
        comp = {lab: read_table(d)[det] for lab, d in (compare or {}).items()}
        for lab, ct in comp.items():
            ax.plot(lamc, ct['S_rows'], '^:', color=EDGE_C, ms=5, lw=1.6, label=lab, zorder=4)
        ax.set_title(f'{det}: core width (numbers: stars per range)', loc='left')
        ax.set_ylim(0.25, 0.85)
        if j == 0:
            ax.set_ylabel('core sigma [detector rows]')
            ax.legend(loc='upper left', frameon=False)
        fj = [joint[(det, r)]['f'] if (det, r) in joint else np.nan for r in range(len(RANGES))]
        fe = [joint[(det, r)]['f_err'] if (det, r) in joint else np.nan for r in range(len(RANGES))]
        axf.errorbar(lamc, fj, yerr=fe, fmt='o-', color=NEW_C, ms=6, lw=2, capsize=3, label='new (joint fit of the stars)')
        axf.plot(PF.PSF_LAM, PF.PSF_TABLES[det]['f'], 's--', color=OLD_C, ms=5, lw=1.6, label='current table')
        for lab, ct in comp.items():
            axf.plot(lamc, ct['f'], '^:', color=EDGE_C, ms=5, lw=1.6, label=lab)
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
        for lab, ct in comp.items():
            axw.plot(lamc, [fwhm_rows(s, ff) for s, ff in zip(ct['S_rows'], ct['f'])], '^:', color=EDGE_C, ms=5,
                     lw=1.6, label=lab)
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


def _star_curves(samples_dir, keys, r, name, bins):
    """Per star: binned mean data and model (fraction of the star's light per pixel) against
    the distance from the trace centre. Returns (D, M) arrays (stars x bins) or None."""
    D, M = [], []
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
        with np.errstate(invalid='ignore', divide='ignore'):
            D.append(np.where(n >= 3, np.bincount(idx[ok], d[ok], minlength=bins.size - 1) / n, np.nan))
            M.append(np.where(n >= 3, np.bincount(idx[ok], m[ok], minlength=bins.size - 1) / n, np.nan))
    return (np.array(D), np.array(M)) if D else None


def plot_stack_free(out_dir, path, half=4.0, step=0.25):
    """Stacked clean-star profiles with every star's centre free in every exposure, against
    (a) the PSF at the median star width with the joint wing fraction and (b) the new table.
    Residuals in % of the peak: the median over stars, the spread between stars (16-84 %) and
    the uncertainty of the median."""
    import matplotlib.pyplot as plt
    _style()
    sel, joint = read_selection(out_dir)
    rec2 = [r for r in read_records(os.path.join(out_dir, 'pass2')) if r['ranges']]
    clean = clean_in_pass2(rec2, sel)
    sdir = os.path.join(out_dir, 'samples_free')
    bins = np.arange(-half, half + 1e-9, step)
    bc = 0.5 * (bins[1:] + bins[:-1])
    nr = len(RANGES)
    fig, axs = plt.subplots(4, nr, figsize=(2.2 * nr, 9.6), sharex=True,
                            gridspec_kw=dict(height_ratios=[1.4, 1.0, 1.4, 1.0], hspace=0.14, wspace=0.12))
    for j, det in enumerate(('NRS1', 'NRS2')):
        for r in range(nr):
            ax, axr = axs[2 * j, r], axs[2 * j + 1, r]
            cm = _star_curves(sdir, clean.get((det, r), []), r, 'med', bins)
            cp = _star_curves(sdir, clean.get((det, r), []), r, 'p25', bins)
            if cm is None or cp is None:
                ax.set_visible(False); axr.set_visible(False)
                continue
            with np.errstate(invalid='ignore'):
                dmed = np.nanmedian(cm[0], axis=0)
                pk = np.nanmax(np.nanmedian(cm[1], axis=0))
                ax.semilogy(bc, np.clip(dmed, 1e-4, None), 'o', color=INK, ms=3, label='stars (median)')
                ax.semilogy(bc, np.nanmedian(cm[1], axis=0), '-', color=NEW_C, lw=1.8,
                            label='model: median star width')
                ax.semilogy(bc, np.nanmedian(cp[1], axis=0), '--', color=OLD_C, lw=1.5,
                            label='model: new table (25th percentile)')
                ax.set_ylim(3e-4, 1.0)
                axr.axhline(0, color=MUTED, lw=0.8)
                for (D, Mm), col, ls, spread in ((cm, NEW_C, '-', True), (cp, OLD_C, '--', False)):
                    R = 100 * (D - Mm) / pk
                    med = np.nanmedian(R, axis=0)
                    n = np.sum(np.isfinite(R), axis=0)
                    err = 1.2533 * np.nanstd(R, axis=0) / np.sqrt(np.maximum(n, 1))
                    if spread:
                        axr.fill_between(bc, np.nanpercentile(R, 16, axis=0), np.nanpercentile(R, 84, axis=0),
                                         color=col, alpha=0.15, lw=0, label='spread between stars (16-84 %)')
                    axr.fill_between(bc, med - err, med + err, color=col, alpha=0.35, lw=0)
                    axr.plot(bc, med, ls, color=col, lw=1.5)
            axr.set_ylim(-6, 6)
            ax.set_title(f'{det}  {RANGES[r][0]}–{RANGES[r][1]} µm\n{cm[0].shape[0]} stars', fontsize=9)
            if r == 0:
                ax.set_ylabel("share of the star's\nlight per pixel")
                axr.set_ylabel('data − model\n[% of peak]')
            else:
                ax.set_yticklabels([]); axr.set_yticklabels([])
            if j == 1:
                axr.set_xlabel('rows from centre')
    axs[0, 0].legend(loc='upper left', frameon=False, fontsize=6.5)
    axs[1, 0].legend(loc='lower left', frameon=False, fontsize=7)
    fig.suptitle('Stacked star profiles, each star\'s centre fitted in every exposure', x=0.01, ha='left', fontsize=12)
    fig.text(0.01, 0.955, 'Residuals: median over stars (line), its uncertainty (dark band), spread between stars '
             '(light band, median-width model). Positive rows = +y in the slit.', fontsize=8.5, color=MUTED)
    fig.subplots_adjust(left=0.06, right=0.99, top=0.9, bottom=0.06)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def star_own_width(root, out_dir, key, exposure=0):
    """One star, every range: its own core width and wing fraction and its centre in every
    exposure fitted (``RangeFit.fit_centres``). Returns (task, exposures, per-range fits, per-range
    pixels of ``exposure``)."""
    sel, joint = read_selection(out_dir)
    table = read_table(out_dir)
    obs = '_'.join(key.split('_')[:-2])
    task = tasks_for(root, [obs], {key})[0]
    exps = load_star(task)
    fits_, pix = {}, {}
    for r, (a, b) in enumerate(RANGES):
        try:
            RF = RangeFit(exps, (a, b))
        except (PF.ProfileFitFallback, ValueError):
            continue
        fj = float(joint[(task.detector, r)]['f']) if (task.detector, r) in joint else float(table[task.detector]['f'][r])
        RF.set_floor(np.zeros(RF.M.npix))
        ref = RF.fit(tables=PF.PSF_TABLES)
        RF.set_floor(ref['_model'])
        fr = RF.fit(f_wing=fj, x0=ref['_x'] + [0.4])
        if fr['peak_snr'] < SNR_MIN:
            continue
        g = RF.fit_centres(S=fr['S'], f=fj, free_S=True, free_f=True, x0=fr['_x'][:2], use=fr['_use'])
        px = RF.exposure_pixels(exposure, g)
        if px is not None:
            fits_[r], pix[r] = {k: v for k, v in g.items() if not k.startswith('_')}, px
    return task, exps, fits_, pix


def plot_star_own_width(root, out_dir, key, path, exposure=0, cuts=(1, 3, 5, 6), half=12.0, step=0.25):
    """One bright star at its own width: one exposure's nod difference, the model (own core
    width and wing fraction per range, centre free in every exposure), the residual in % of the
    column peak and in sigma; and cross-sections over a few ranges: data against model (each
    pixel as a share of the star's light, against its distance from the trace centre), the
    residual underneath with its 1-sigma errors."""
    import matplotlib.pyplot as plt
    _style()
    task, exps, fits_, pix = star_own_width(root, out_dir, key, exposure)
    e = exps[exposure]
    shape = e.y.shape
    img = {n: np.full(shape, np.nan) for n in ('d', 'm', 'sig', 'peak')}
    for r, px in pix.items():
        msk = px['mask']
        img['d'][msk], img['m'][msk], img['sig'][msk] = px['d'], px['m'], px['sig']
        pk = np.zeros(shape)
        pk[msk] = np.abs(px['m'])
        img['peak'][msk] = np.broadcast_to(pk.max(axis=0), shape)[msk]
    rows = np.flatnonzero(np.isfinite(img['d']).any(axis=1))
    cols = np.flatnonzero(np.isfinite(img['d']).any(axis=0))
    sl = (slice(rows.min(), rows.max() + 1), slice(cols.min(), cols.max() + 1))
    lam_col = np.nanmedian(e.wav[sl], axis=0)
    cuts = [r for r in cuts if r in pix]
    fig = plt.figure(figsize=(12, 13.5))
    gs = fig.add_gridspec(7, max(len(cuts), 1), height_ratios=[1, 1, 1, 1, 0.55, 1.5, 0.9], hspace=0.45,
                          wspace=0.22)
    with np.errstate(invalid='ignore', divide='ignore'):
        panels = [('nod difference / column peak', img['d'] / img['peak'], 1.0, 'RdBu_r'),
                  ('model (own width, centre free per exposure) / column peak', img['m'] / img['peak'], 1.0, 'RdBu_r'),
                  ('residual [% of column peak]', 100 * (img['d'] - img['m']) / img['peak'], 5.0, 'RdBu_r'),
                  ('residual [sigma]', (img['d'] - img['m']) / img['sig'], 5.0, 'RdBu_r')]
    ok = np.isfinite(lam_col)
    o = np.argsort(lam_col[ok])
    ticks = [t for t in (0.8, 1, 1.5, 2, 3, 4, 5) if np.nanmin(lam_col) <= t <= np.nanmax(lam_col)]
    for i, (title, im, v, cmap) in enumerate(panels):
        ax = fig.add_subplot(gs[i, :])
        h = ax.imshow(im[sl], origin='lower', aspect='auto', cmap=cmap, vmin=-v, vmax=v, interpolation='nearest')
        ax.set_title(title, loc='left', fontsize=10)
        ax.grid(False)
        ax.set_ylabel('row')
        ax.set_xticks(np.interp(ticks, lam_col[ok][o], np.arange(lam_col.size)[ok][o]))
        ax.set_xticklabels([str(t) for t in ticks] if i == 3 else [])
        if i == 3:
            ax.set_xlabel('wavelength [µm]')
        fig.colorbar(h, ax=ax, pad=0.01)
    bins = np.arange(-half, half + 1e-9, step)
    bc = 0.5 * (bins[1:] + bins[:-1])
    for j, r in enumerate(cuts):
        px, g = pix[r], fits_[r]
        u = px['u']
        keep = px['use'] & np.isfinite(px['norm']) & (px['norm'] > 0)
        idx = np.digitize(u[keep], bins) - 1
        good = (idx >= 0) & (idx < bc.size)
        n = np.bincount(idx[good], minlength=bc.size)
        sd = np.bincount(idx[good], (px['d'] / px['norm'])[keep][good], minlength=bc.size)
        sm = np.bincount(idx[good], (px['m'] / px['norm'])[keep][good], minlength=bc.size)
        se = np.bincount(idx[good], ((px['sig'] / px['norm']) ** 2)[keep][good], minlength=bc.size)
        with np.errstate(invalid='ignore', divide='ignore'):
            D, Mo, E = sd / n, sm / n, np.sqrt(se) / n
        pk = np.nanmax(Mo)
        fin = np.isfinite(D) & np.isfinite(Mo)
        ax = fig.add_subplot(gs[5, j])
        axr = fig.add_subplot(gs[6, j], sharex=ax)
        ax.plot(bc[fin], Mo[fin], '-', color=NEW_C, lw=1.5, label='model, own width')
        ax.errorbar(bc[fin], D[fin], yerr=E[fin], fmt='o', color=INK, ms=2.5, lw=0.6, label='data')
        ax.axhline(0, color=MUTED, lw=0.6)
        ax.set_title(f'{RANGES[r][0]}–{RANGES[r][1]} µm\ncore {g["S"]:.2f} rows, wing {g["f"]:.2f}\n'
                     f'chi2/dof {g["chi2r_raw"]:.1f}', fontsize=8.5)
        axr.axhline(0, color=MUTED, lw=0.8)
        axr.errorbar(bc[fin], 100 * (D - Mo)[fin] / pk, yerr=100 * E[fin] / pk, fmt='o-', color=OLD_C, ms=2.5, lw=0.8)
        axr.set_ylim(-4, 4)
        axr.set_xlabel('rows from the trace centre')
        if j == 0:
            ax.set_ylabel("share of the star's\nlight per pixel")
            axr.set_ylabel('data − model\n[% of peak]')
            ax.legend(frameon=False, fontsize=8, loc='upper left')
    c = np.nanmedian([g['c_node'] for g in fits_.values()]) if fits_ else np.nan
    with np.errstate(invalid='ignore'):
        up = np.nanmedian(np.diff(e.y[sl], axis=0)) > 0         # does slit +y run up the image?
    fig.suptitle(f'{key}: one star at its own width, exposure {e.name.split("_")[2]} (position in the shutter '
                 f'{c:+.2f} of the pitch)', x=0.01, ha='left', fontsize=11.5)
    fig.text(0.01, 0.955, 'Core width and wing fraction fitted per wavelength range, the star\'s centre in every '
             'exposure; negative traces are the nod partners. Slit +y runs '
             f'{"up" if up else "down"} the images. Cross-sections: this exposure, pixels averaged in 0.25-row bins '
             '(+ = slit +y).', fontsize=8.5, color=MUTED)
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    return fits_


def plot_profiles(out_dir, path, half=4.0, step=0.25, compare=None):
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
            for name, lab in (compare or {}).items():
                z = _stack(sdir, clean.get((det, r), []), r, name, bins)
                if z is not None:
                    ax.semilogy(bc, z[1], ':', color=EDGE_C, lw=1.6, label=f'model, {lab}')
            ax.semilogy(bc, mn, '-', color=NEW_C, lw=1.8, label='model, new table')
            ax.set_ylim(3e-4, 1.0)
            pk = np.nanmax(mn)
            axr.axhline(0, color=MUTED, lw=0.8)
            axr.plot(bc, 100 * (do - mo) / pk, '--', color=OLD_C, lw=1.4)
            for name in (compare or {}):
                z = _stack(sdir, clean.get((det, r), []), r, name, bins)
                if z is not None:
                    axr.plot(bc, 100 * (z[0] - z[1]) / pk, ':', color=EDGE_C, lw=1.4)
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


def read_centre_law(out_dir):
    with open(os.path.join(out_dir, 'centre_law_new.json')) as fh:
        return {k: {kk: (np.array([np.nan if x is None else x for x in vv], float) if isinstance(vv, list) else vv)
                    for kk, vv in v.items()} for k, v in json.load(fh).items()}


def plot_centre_law(out_dir, path, dy=0.229, compare=None):
    """(c) The trace centre law: drift with wavelength per MSA quadrant and the slope
    with the star's position, refitted against the current ``CENTRE_LAW`` (and, for the
    detector-level drift and the slope, ``compare``: {label: directory} of earlier refits)."""
    import matplotlib.pyplot as plt
    _style()
    law = read_centre_law(out_dir)
    comp = {lab: read_centre_law(d) for lab, d in (compare or {}).items()}
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
        for lab, cl in comp.items():
            ax.plot(lamc, np.array(cl[det]['D']) / dy, '^:', color=STAR_C, lw=1.2, ms=4, label=f'all quadrants, {lab}')
        ax.axhline(0, color=MUTED, lw=0.8)
        ax.set_title(f'{det}: trace shift with wavelength', loc='left')
        ax.legend(frameon=False, fontsize=7.5, loc='lower left', ncol=1)
        axa.errorbar(lamc, law[det]['alpha'], yerr=law[det]['ealpha'], fmt='o-', color=NEW_C, lw=1.8, ms=5,
                     capsize=2, label='new')
        axa.plot(PF.PSF_LAM, PF.CENTRE_LAW[det]['alpha'], 's--', color=OLD_C, lw=1.5, ms=4, label='current')
        for lab, cl in comp.items():
            axa.plot(lamc, cl[det]['alpha'], '^:', color=EDGE_C, lw=1.5, ms=4, label=lab)
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


def write_summary(out_dir, log=print, compare=None):
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
    for name, d in (compare or {}).items():
        ct = read_table(d)
        lines.append(f'== against {name} ({d})')
        lines.append('det  lam  | core: that -> new          | wing: that -> new | FWHM: that -> new')
        for det in ('NRS1', 'NRS2'):
            t, c = table[det], ct[det]
            for r in range(len(RANGES)):
                fc, fn = fwhm_rows(c['S_rows'][r], c['f'][r]), fwhm_rows(t['S_rows'][r], t['f'][r])
                lines.append(f"{det} {t['lam'][r]:4.2f} | {c['S_rows'][r]:.3f} -> {t['S_rows'][r]:.3f} "
                             f"{100 * (t['S_rows'][r] / c['S_rows'][r] - 1):+5.1f}% | {c['f'][r]:.3f} -> {t['f'][r]:.3f} | "
                             f"{fc:.3f} -> {fn:.3f} {100 * (fn / fc - 1):+5.1f}%")
    # summed chi2 of the clean stars with each table (pass 3: same pixels and weights, centre and drift free)
    sel, _ = read_selection(out_dir)
    rec2 = [r for r in read_records(os.path.join(out_dir, 'pass2')) if r['ranges']]
    clean = clean_in_pass2(rec2, sel)
    p3d = os.path.join(out_dir, 'pass3')
    if os.path.isdir(p3d):
        p3 = {r['key']: r for r in read_records(p3d)}
        names = ['new'] + list(compare or {})
        lines.append('== summed chi2 of the clean stars with each table, relative to the current table')
        lines.append('det  range      n | ' + ' | '.join(f'{n:>8s}' for n in names))
        for det in ('NRS1', 'NRS2'):
            for r in range(len(RANGES)):
                ks = [k for k in clean.get((det, r), []) if k in p3 and all(
                    f'fit_{n}' in p3[k]['ranges'][r] for n in ['old'] + names)]
                if not ks:
                    continue
                o = sum(p3[k]['ranges'][r]['fit_old']['chi2'] for k in ks)
                vals = [sum(p3[k]['ranges'][r][f'fit_{n}']['chi2'] for k in ks) / o for n in names]
                lines.append(f'{det} {RANGES[r][0]}-{RANGES[r][1]} {len(ks):4d} | ' + ' | '.join(f'{v:8.4f}' for v in vals))
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
    ap.add_argument('step', choices=['offsets', 'offset_law', 'fit', 'select', 'refit', 'table', 'samples', 'figures',
                                     'free_centres', 'free_centre_figures', 'own_width'])
    ap.add_argument('--keys', nargs='*', default=[], help='own_width step: star keys')
    ap.add_argument('--dispersers', nargs='*', default=['prism_clear'], help='offsets step: dispersers')
    ap.add_argument('--no-nod-law', action='store_true',
                    help='fit with one position shared by all exposures (the geometry before NOD_OFFSET_LAW)')
    ap.add_argument('--compare', nargs='*', default=[], help='samples / figures: name=dir of an earlier refit '
                    'to score and draw beside the current and the new table')
    ap.add_argument('--root', help='reduced CAMPFIRE root holding the observations')
    ap.add_argument('--out', required=True, help='output directory')
    ap.add_argument('--obs', nargs='*', default=[], help='observations')
    ap.add_argument('--snr-min', type=float, default=SNR_MIN)
    ap.add_argument('--edge-max', type=float, default=EDGE_MAX)
    ap.add_argument('--part', default=None, help='i/n: this process takes every n-th star from the i-th (fit, '
                    'refit, samples), to run a step as n processes')
    a = ap.parse_args(argv)
    part = tuple(int(x) for x in a.part.split('/')) if a.part else None
    law = False if a.no_nod_law else None
    compare = dict(x.split('=', 1) for x in a.compare)
    log = lambda *x: print(*x, flush=True)          # noqa: E731
    if a.step == 'offsets':
        run_offsets(tasks_for(a.root, a.obs, part=part, dispersers=a.dispersers), os.path.join(a.out, 'offsets'),
                    log=log)
    elif a.step == 'free_centres':
        step_free_centres(a.root, a.out, a.obs, part=part, log=log)
    elif a.step == 'free_centre_figures':
        os.makedirs(os.path.join(a.out, 'figures'), exist_ok=True)
        plot_stack_free(a.out, os.path.join(a.out, 'figures', 'psf_stack_free_centres.png'))
        for name in ('med', 'p25'):
            for (det, r), v in sorted(side_light(a.out, name, samples='samples_free').items()):
                log(f'{name} {det} {RANGES[r][0]}-{RANGES[r][1]} um: beyond -1.5 rows data {100 * v["neg_d"]:.2f}% '
                    f'model {100 * v["neg_m"]:.2f}% | beyond +1.5 rows data {100 * v["pos_d"]:.2f}% model '
                    f'{100 * v["pos_m"]:.2f}% | one-sided excess {100 * v["asym"]:+.2f} +- {100 * v["asym_err"]:.2f}% '
                    f'of the light, positive in {100 * v["asym_frac_pos"]:.0f}% of {v["n"]} stars')
    elif a.step == 'own_width':
        os.makedirs(os.path.join(a.out, 'figures'), exist_ok=True)
        for key in a.keys:
            f_ = plot_star_own_width(a.root, a.out, key, os.path.join(a.out, 'figures', f'star_own_width_{key}.png'))
            write_json(os.path.join(a.out, 'own_width', f'{key}.json'), f_)
            log(key + ': ' + ' | '.join(f"{RANGES[r][0]}-{RANGES[r][1]} S {g['S']:.3f} f {g['f']:.2f} "
                                         f"chi2r {g['chi2r_raw']:.2f}" for r, g in sorted(f_.items())))
    elif a.step == 'offset_law':
        step_offset_law(a.out, log=log)
        os.makedirs(os.path.join(a.out, 'figures'), exist_ok=True)
        plot_offset_law(a.out, os.path.join(a.out, 'figures', 'nod_offset_law.png'))
    elif a.step == 'fit':
        run_stars(tasks_for(a.root, a.obs, part=part), os.path.join(a.out, 'pass1'), log=log, nod_law=law)
    elif a.step == 'select':
        step_select(a.out, snr_min=a.snr_min, edge_max=a.edge_max, log=log)
    elif a.step == 'refit':
        step_refit(a.root, a.out, a.obs, log=log, part=part, nod_law=law)
    elif a.step == 'table':
        step_table(a.out, log=log)
    elif a.step == 'samples':
        step_samples(a.root, a.out, a.obs, log=log, part=part, nod_law=law, extra_tables=compare)
    elif a.step == 'figures':
        write_fit_table(a.out)
        fd = os.path.join(a.out, 'figures')
        os.makedirs(fd, exist_ok=True)
        labels = {name: COMPARE_LABELS.get(name, name) for name in compare}
        plot_widths(a.out, os.path.join(fd, 'psf_widths.png'), compare={labels[n]: d for n, d in compare.items()})
        plot_profiles(a.out, os.path.join(fd, 'psf_profiles.png'), compare=labels)
        plot_centre_law(a.out, os.path.join(fd, 'centre_law.png'), compare={labels[n]: d for n, d in compare.items()})
        plot_position(a.out, os.path.join(fd, 'psf_position.png'))
        write_summary(a.out, log=log, compare=compare)
        if a.root:
            for det in ('NRS1', 'NRS2'):
                key = brightest_clean(a.out, det)
                if key:
                    plot_star_residuals(a.root, a.out, key, os.path.join(fd, f'star_residuals_{det}.png'))


if __name__ == '__main__':
    main()
