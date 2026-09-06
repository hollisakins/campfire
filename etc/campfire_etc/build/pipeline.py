"""Rebuild the empirical noise model from a local CAMPFIRE archive.

Ported from the analysis scripts that produced the 2026.09 model. One step per
call, per disperser: ``harvest`` (per-spectrum statistics from every
``*_spec.fits``; the slow step, ~25 min for the whole archive with 10
processes), ``fit`` (the A/T + B/(T t_exp^2) model, shutter-position term,
correlations, source-Poisson coefficient), ``phot`` (flux recovery and achieved
S/N against total photometry), ``depth`` (the calculator payload), ``figs``
(diagnostic figures), then ``assemble`` to write the versioned model JSON.

Run ``prism_clear`` first: dispersers with too few faint spectra borrow its
shutter-position term and flux-recovery table, and the H gratings borrow the
read-noise/sky ratio from their M sibling.

Inputs under ``--root`` (default ``$CAMPFIRE_ROOT``): ``meta/spectra.csv`` and
``meta/photometry.csv`` (written by ``campfire sync``) and the products in
``products/nirspec/<obs>/``. Outputs under ``--workdir/dispersers/<key>/``.
Needs the ``build`` extra (astropy, scipy, pandas, matplotlib).
"""

from __future__ import annotations

import json
import os
import warnings
from importlib import resources
from multiprocessing import Pool
from pathlib import Path

import numpy as np

warnings.simplefilter("ignore")

EDGES = np.arange(0.60, 5.3001, 0.10); NB = len(EDGES) - 1; WC = 0.5 * (EDGES[1:] + EDGES[:-1])
KEYS = ["prism_clear", "g140m_f070lp", "g140m_f100lp", "g140h_f100lp", "g235m_f170lp", "g235h_f170lp", "g395m_f290lp", "g395h_f290lp"]
BAND = {"prism_clear": "f277w", "g140m_f070lp": "f115w", "g140m_f100lp": "f150w", "g140h_f100lp": "f150w", "g235m_f170lp": "f277w",
        "g235h_f170lp": "f277w", "g395m_f290lp": "f356w", "g395h_f290lp": "f356w"}
BANDWAVE = {"f115w": 1.15, "f150w": 1.50, "f200w": 1.99, "f277w": 2.76, "f356w": 3.57, "f444w": 4.40}
BANDS5 = {"0.6-1.0": (0.6, 1.0), "1.0-2.0": (1.0, 2.0), "2.0-3.0": (2.0, 3.0), "3.0-4.0": (3.0, 4.0), "4.0-5.3": (4.0, 5.3)}
SIB = {"g140h_f100lp": "g140m_f100lp", "g235h_f170lp": "g235m_f170lp", "g395h_f290lp": "g395m_f290lp", "g140m_f100lp": "g140m_f070lp",
       "g140m_f070lp": "prism_clear", "g235m_f170lp": "prism_clear", "g395m_f290lp": "prism_clear"}


class Paths:
    def __init__(self, root: str, workdir: str, disp_dir: str | None):
        self.root = Path(root).expanduser()
        self.products = self.root / "products" / "nirspec"
        self.meta = self.root / "meta"
        self.workdir = Path(workdir).expanduser()
        self.disp_dir = Path(disp_dir).expanduser() if disp_dir else _default_disp_dir()

    def out(self, key: str) -> Path:
        d = self.workdir / "dispersers" / key; d.mkdir(parents=True, exist_ok=True); return d


def _default_disp_dir() -> Path:
    try:
        import campfire_pipeline  # type: ignore
        return Path(campfire_pipeline.__file__).parent / "data"
    except ImportError:
        here = Path(__file__).resolve()
        cand = here.parents[3] / "pipeline" / "campfire_pipeline" / "data"   # monorepo checkout
        if cand.exists():
            return cand
        raise SystemExit("cannot find the NIRSpec dispersion reference files; pass --disp-dir")


# ============================================================ harvest

def madstd(x):
    x = x[np.isfinite(x)]
    if x.size < 4: return np.nan
    m = np.median(x); return 1.4826 * np.median(np.abs(x - m))

def bin_index(wave):
    idx = np.digitize(wave, EDGES) - 1; idx[(idx < 0) | (idx >= NB)] = -1; return idx

def pooled(wave, x, clip=3.5, demedian=True):
    idx = bin_index(wave); ok = (idx >= 0) & np.isfinite(x)
    ss = np.full(NB, np.nan); n = np.zeros(NB); med = np.full(NB, np.nan)
    for i in np.unique(idx[ok]):
        v = x[ok & (idx == i)]
        if v.size < 4: continue
        m = np.median(v) if demedian else 0.0; s = madstd(v)
        if not np.isfinite(s) or s == 0: continue
        k = np.abs(v - m) < clip * s
        ss[i] = np.sum((v[k] - m) ** 2); n[i] = k.sum(); med[i] = np.median(v)
    return ss, n, med

def binmed(wave, x):
    idx = bin_index(wave); ok = (idx >= 0) & np.isfinite(x); out = np.full(NB, np.nan)
    for i in np.unique(idx[ok]): out[i] = np.median(x[ok & (idx == i)])
    return out


def process(args):
    from astropy.io import fits
    spectrum_id, obs, fn = args
    try:
        with fits.open(fn, memmap=False) as f:
            ph = f[0].header; sh = f["SCI"].header
            sci = f["SCI"].data.astype(np.float64) * 1e12; err = f["ERR"].data.astype(np.float64) * 1e12
            wht = f["WHT"].data.astype(np.float64); wav = f["WAVELENGTH"].data.astype(np.float64)
            prof = f["PROF1D"].data; s1 = f["SPEC1D"].data; ex = f["EXPOSURES"].data
            ny, nx = sci.shape
            nexp = len(ex); n_nodpos = len(np.unique(ex["dither_number"])); n_visit = int(round(nexp / max(n_nodpos, 1)))
            texp = float(np.median(ex["exptime"])); fn0 = str(ex["filename"][0])
            det = "nrs1" if "_nrs1" in fn0 else ("nrs2" if "_nrs2" in fn0 else "na")
            shstate = str(ex["shutter_state"][0]); stuck = any(str(s) not in ("N/A", "", "[]") for s in ex["stuck_shutter_list"])
            dtype = ",".join(sorted(set(str(s) for s in ex["dither_type"]))); ntype = ",".join(sorted(set(str(s) for s in ex["nod_type"])))
            fixed = bool(np.any(ex["fixed_slit"])) if "fixed_slit" in ex.columns.names else False
            popt = np.array(prof["opt"], float); p3 = np.array(prof["3px"], float); ypos = np.arange(ny)
            cen = float(np.sum(ypos * p3) / np.sum(p3)) if np.nansum(p3) > 0 else ny / 2 - 0.5
            popt_n = np.where(np.isfinite(popt), popt, 0)
            if popt_n.sum() > 0:
                mu = np.sum(ypos * popt_n) / popt_n.sum(); sig = np.sqrt(np.sum((ypos - mu) ** 2 * popt_n) / popt_n.sum())
                fwhm_opt = 2.355 * sig; peak_frac = popt_n.max(); frac3 = np.sum(popt_n * np.clip(p3, 0, 1))
            else: fwhm_opt = peak_frac = frac3 = np.nan
            dy = ypos - cen
            good = np.isfinite(sci) & np.isfinite(err) & (wht > 0) & (err > 0)
            wmed = np.nanmedian(wht[good]) if good.any() else np.nan; good &= wht > 0.5 * wmed
            R = {"src": np.abs(dy) <= 1.5, "open": (np.abs(dy) > 1.5) & (np.abs(dy) <= 7.5), "off": np.abs(dy) > 1.5}
            z = sci / err; A = {}
            for name, rows in R.items():
                mk = good & rows[:, None]; w = wav[mk]
                ss, n, _ = pooled(w, sci[mk]); A[f"ss_sci_{name}"] = ss; A[f"n_{name}"] = n
                ssz, nz, _ = pooled(w, z[mk]); A[f"ss_z_{name}"] = ssz; A[f"n_z_{name}"] = nz
                A[f"err_{name}"] = binmed(w, err[mk])
            mk = good & R["off"][:, None]
            d = z[:, 1:] - z[:, :-1]; md = mk[:, 1:] & mk[:, :-1]; wd = 0.5 * (wav[:, 1:] + wav[:, :-1])
            A["ss_dz_disp"], A["n_dz_disp"], _ = pooled(wd[md], d[md], demedian=False)
            ds = z[1:, :] - z[:-1, :]; ms_ = mk[1:, :] & mk[:-1, :]; ws = 0.5 * (wav[1:, :] + wav[:-1, :])
            A["ss_dz_spat"], A["n_dz_spat"], _ = pooled(ws[ms_], ds[ms_], demedian=False)
            w1 = np.array(s1["wave"], float)
            f3 = np.array(s1["fnu_3px"], float); e3 = np.array(s1["fnu_3px_err"], float)
            fo = np.array(s1["fnu"], float); eo = np.array(s1["fnu_err"], float); e5 = np.array(s1["fnu_5px_err"], float)
            for arr in (e3, eo, e5): arr[arr == 0] = np.nan
            f3[~np.isfinite(e3)] = np.nan; fo[~np.isfinite(eo)] = np.nan
            A["fnu3"] = binmed(w1, f3); A["fnuopt"] = binmed(w1, fo)
            A["e3"] = binmed(w1, e3); A["eopt"] = binmed(w1, eo); A["e5"] = binmed(w1, e5); A["snr3"] = binmed(w1, f3 / e3)
            A["ss_f3"], A["n_f3"], _ = pooled(w1, f3); A["ss_fo"], A["n_fo"], _ = pooled(w1, fo)
            A["ss_z3"], A["n_z3"], _ = pooled(w1, f3 / e3); A["ss_zo"], A["n_zo"], _ = pooled(w1, fo / eo)
            hp3 = (f3[1:-1] - 0.5 * (f3[:-2] + f3[2:])) / np.sqrt(1.5)
            A["ss_hp3"], A["n_hp3"], _ = pooled(w1[1:-1], hp3, demedian=False)
            d1 = f3[1:] - f3[:-1]; A["ss_d3"], A["n_d3"], _ = pooled(0.5 * (w1[1:] + w1[:-1]), d1, demedian=False)
            A["dlds"] = binmed(0.5 * (w1[1:] + w1[:-1]), np.diff(w1))
            meta = dict(spectrum_id=spectrum_id, obs=obs, program=str(ph.get("PROGRAM", "")).strip(), grating=str(ph.get("GRATING", "")).strip(),
                filter=str(ph.get("FILTER", "")).strip(), ver=str(ph.get("CMPFRVER", "")), cal_ver=str(ph.get("CAL_VER", "")), crds=str(ph.get("CRDS_CTX", "")),
                readpatt=str(ph.get("READPATT", "")), effexptm=float(ph.get("EFFEXPTM", np.nan)), ndriz=int(ph.get("NDRIZ", -1)), nexp=nexp,
                n_nodpos=n_nodpos, n_visit=n_visit, texp=texp, det=det, shstate=shstate, shlen=len(shstate), stuck=stuck, dither_type=dtype,
                nod_type=ntype, fixed_slit=fixed, srcxpos=float(np.median(ex["source_xpos"])), srcypos=float(np.median(ex["source_ypos"])),
                v3pa=float(np.median(ex["v3pa"])), date_obs=str(ph.get("DATE-OBS", "")), stlarity=float(sh.get("STLARITY", np.nan)),
                srcra=float(sh.get("SRCRA", np.nan)), srcdec=float(sh.get("SRCDEC", np.nan)), cmpfropt=str(ph.get("CMPFROPT", "")),
                s_clnfns=str(ph.get("S_CLNFNS", "")), s_nsclen=str(ph.get("S_NSCLEN", "")), ny=ny, nx=nx, cen=cen, fwhm_opt=fwhm_opt,
                peak_frac=peak_frac, frac3=frac3, snr_med=float(np.nanmedian(f3 / e3)), fnu3_med=float(np.nanmedian(f3)),
                err_src_med=float(np.nanmedian(A["err_src"])), rms_src_med=float(np.sqrt(np.nansum(A["ss_sci_src"]) / max(np.nansum(A["n_src"]), 1))))
            return meta, A, None
    except Exception as e:
        return dict(spectrum_id=spectrum_id, obs=obs), None, f"{type(e).__name__}: {e}"


def harvest(P: Paths, key: str, nproc: int = 8, limit: int | None = None):
    import pandas as pd
    grating, filt = key.split("_"); d = P.out(key)
    s = pd.read_csv(P.meta / "spectra.csv"); s["filt"] = s.fits_path.str.extract(r"_(?:g\d{3}[mh]|prism)_(\w+?)_\d+_spec")[0]
    p = s[(s.grating == grating.upper()) & (s.filt == filt)].copy()
    p["fn"] = [str(P.products / o / os.path.basename(fp)) for o, fp in zip(p.observation, p.fits_path)]
    p = p[[os.path.exists(fn) for fn in p.fn]]
    if limit: p = p.sample(min(limit, len(p)), random_state=1)
    tasks = list(zip(p.spectrum_id, p.observation, p.fn)); print(f"[{key}] {len(tasks)} files", flush=True)
    metas, arrs, errs = [], {}, []
    with Pool(nproc) as pool:
        for i, (m, a, e) in enumerate(pool.imap_unordered(process, tasks, chunksize=10)):
            if e is not None: errs.append((m["spectrum_id"], e)); continue
            metas.append(m)
            for k, v in a.items(): arrs.setdefault(k, []).append(v)
            if i % 4000 == 0: print(f"[{key}] {i}", flush=True)
    df = pd.DataFrame(metas); df.to_csv(d / "meta.csv", index=False)
    np.savez_compressed(d / "arrays.npz", edges=EDGES, spectrum_id=df.spectrum_id.values, **{k: np.array(v) for k, v in arrs.items()})
    pd.DataFrame(errs, columns=["spectrum_id", "error"]).to_csv(d / "errors.csv", index=False)
    print(f"[{key}] done {len(df)} errors {len(errs)} {errs[:3]}", flush=True)


# ============================================================ fit

_BIAS = None

def bias_table():
    """Monte-Carlo bias of the clipped per-bin variance estimator vs sample size (bundled)."""
    global _BIAS
    if _BIAS is None:
        with resources.files("campfire_etc.build").joinpath("estimator_bias.json").open() as f:
            _BIAS = {int(k): v for k, v in json.load(f).items()}
    return _BIAS

def bcorr(nmed):
    b = bias_table(); nn = np.clip(np.round(np.nan_to_num(nmed, nan=4)).astype(int), 4, 200); return np.array([b[k] for k in nn])


def fit(P: Paths, key: str, minobs: int = 8):
    import pandas as pd
    from scipy.optimize import least_squares
    from astropy.coordinates import SkyCoord, GeocentricTrueEcliptic, get_sun
    from astropy.time import Time
    import astropy.units as u
    d = P.out(key); m = pd.read_csv(d / "meta.csv"); a = np.load(d / "arrays.npz", allow_pickle=True); wc = WC
    m["ngroups"] = np.where(m.readpatt == "NRSIRS2RAPID", m.texp / 14.589, m.texp / 72.944).round().astype(int); m["T"] = m.effexptm
    ok = np.isfinite(m.srcra) & np.isfinite(m.srcdec)
    sc = SkyCoord(m.srcra[ok].values * u.deg, m.srcdec[ok].values * u.deg).transform_to(GeocentricTrueEcliptic())
    m.loc[ok, "ecl_lat"] = sc.lat.deg; m.loc[ok, "ecl_lon"] = sc.lon.deg
    okd = ok & m.date_obs.astype(str).str.match(r"^\d{4}-\d{2}-\d{2}")
    try:
        sun = get_sun(Time([str(x)[:10] for x in m.date_obs[okd].values])).transform_to(GeocentricTrueEcliptic())
        m.loc[okd, "sun_dlon"] = np.abs((m.ecl_lon[okd].values - sun.lon.deg + 180) % 360 - 180)
    except Exception as e: print("sun elong failed", e); m["sun_dlon"] = np.nan
    def pool(ss, n, idx):
        v = a[ss][idx] / np.where(a[n][idx] > 0, a[n][idx], np.nan); k = np.nanmedian(a[n][idx], axis=0)
        return np.sqrt(np.nanmedian(v, axis=0) / (1 - 2 / (3 * np.maximum(k, 4))) / bcorr(k))
    def per_spec(ss, n, idx):
        nn = a[n][idx]; v = a[ss][idx] / np.where(nn > 0, nn, np.nan); b = bias_table()
        bb = np.vectorize(lambda k: b[int(np.clip(round(k), 4, 200))])(np.nan_to_num(nn, nan=4))
        return np.sqrt(v / (1 - 2 / (3 * np.maximum(nn, 4))) / bb)
    faint = (m.snr_med < 1.5) & np.isfinite(m.rms_src_med) & (~m.fixed_slit)
    base_sel = faint & (m.shlen == 3) & (m.dither_type == "NONE") & (~m.stuck)
    std3 = base_sel & (m.n_nodpos == 3); std_def = "3 nod positions"
    if std3.sum() < 500 or m.obs[std3].nunique() < 6:
        std3 = base_sel & (m.n_nodpos <= 3); std_def = "1-3 nod positions (3-nod subset too small; PRISM shows no per-unit-time penalty for 1-2 nod positions)"
    m["ax"] = m.srcxpos.abs(); m["ay"] = m.srcypos.abs()
    print(f"[{key}] all {len(m)} faint {faint.sum()} std3 {std3.sum()} ({std_def})")
    RATIO = {"val": None, "from": None}; DROPPED = []
    def sibling_ratio():
        # B/A (read-noise / sky variance ratio) of the sibling, rescaled by the pixel-bandwidth ratio (sky per pixel ~ dlds)
        k2 = SIB.get(key); fn2 = P.workdir / "dispersers" / k2 / "final.json" if k2 else None
        if not fn2 or not fn2.exists(): return None
        S2 = json.load(open(fn2)); A2 = np.array(S2["A"]); B2 = np.array(S2["B"]); dl2 = np.array(S2["oned"]["dlds"], float)
        dl1 = np.nanmedian(a["dlds"][np.where(std3)[0]], axis=0)
        r = (B2 / A2) * (dl2 / dl1); r = np.where(np.isfinite(r) & (r > 0), r, np.nan)
        lr = np.log(r); okr = np.isfinite(lr)
        if okr.sum() < 3: return None
        RATIO["from"] = k2; return np.exp(np.interp(np.arange(NB), np.where(okr)[0], lr[okr]))
    def fit_model(SIG, T, TE, W, p=2, ratio=None):
        A_, B_ = np.full(NB, np.nan), np.full(NB, np.nan); RES = np.full(SIG.shape, np.nan)
        for i in range(NB):
            s = SIG[:, i]; okk = np.isfinite(s) & (s > 0)
            if ratio is not None and okk.sum() >= 1 and np.isfinite(ratio[i]):
                # one-parameter fit: sigma^2 = A/T * (1 + ratio/te^2)
                Ai = np.exp(np.average(np.log(s[okk] ** 2 * T[okk] / (1 + ratio[i] / TE[okk] ** 2)), weights=W[okk]))
                A_[i] = Ai; B_[i] = Ai * ratio[i]; RES[okk, i] = (np.log(s[okk]) - np.log(np.sqrt(Ai / T[okk] + B_[i] / (T[okk] * TE[okk] ** 2)))) / np.log(10); continue
            if okk.sum() < 4: continue
            y = np.log(s[okk]); t = T[okk]; te = TE[okk]; w = W[okk]
            def resid(q):
                A1, B1 = np.exp(q); return (np.log(np.sqrt(A1 / t + B1 / (t * te ** p))) - y) * w
            q0 = np.log([np.nanmedian(s[okk] ** 2 * t), 0.3 * np.nanmedian(s[okk] ** 2 * t * np.median(te) ** p)])
            r = least_squares(resid, q0, loss="soft_l1", f_scale=0.25); A_[i], B_[i] = np.exp(r.x); RES[okk, i] = r.fun / w / np.log(10)
        return A_, B_, RES
    def model(T, te, A, B, p=2): return np.sqrt(A[None, :] / T[:, None] + B[None, :] / (T[:, None] * te[:, None] ** p))
    idx_all = np.where(std3)[0]
    if len(idx_all) < 30: print(f"[{key}] too few faint standard spectra"); return
    sig_sp = per_spec("ss_sci_src", "n_src", idx_all)
    X = np.c_[np.ones(len(idx_all)), m.ax.values[idx_all] ** 2, m.ay.values[idx_all] ** 2, m.ax.values[idx_all] ** 4, m.ay.values[idx_all] ** 4]
    # position term: fit here if enough spectra, else borrow PRISM's
    prism_fn = P.workdir / "dispersers" / "prism_clear" / "final.json"
    borrow = len(idx_all) < 1500 and key != "prism_clear" and prism_fn.exists()
    coef = np.zeros(5)
    if borrow: coef = np.array(json.load(open(prism_fn))["pos_coef"]); print(f"[{key}] borrowing PRISM position term")
    band_ok = np.isfinite(sig_sp).mean(axis=0) > 0.3
    for it in range(3):
        pen = X @ coef; sig_c = sig_sp / 10 ** pen[:, None]; rows = []
        for obs, g in m.loc[idx_all].groupby("obs"):
            j = np.where(np.isin(idx_all, g.index.values))[0]
            if len(j) < minobs: continue
            k = np.nanmedian(a["n_src"][idx_all[j]], axis=0); v = np.nanmedian(sig_c[j] ** 2, axis=0) / (1 - 2 / (3 * np.maximum(k, 4)))
            rows.append(dict(obs=obs, program=g.program.iloc[0], readpatt=g.readpatt.iloc[0], T=g["T"].median(), texp=g.texp.median(), nexp=g.nexp.median(),
                             n=len(j), ecl_lat=g.ecl_lat.median(), sun_dlon=g.sun_dlon.median(), sig=np.sqrt(v)))
        om = pd.DataFrame(rows)
        if len(om) < 3: print(f"[{key}] too few observations ({len(om)})"); return
        SIG = np.vstack(om.sig.values); W = np.sqrt(om.n.values).clip(2, 12) / 6
        constrain = (len(om) < 6) or (om.texp.max() / om.texp.min() < 1.5)
        if constrain and RATIO["val"] is None: RATIO["val"] = sibling_ratio()
        A, B, RES = fit_model(SIG, om["T"].values, om.texp.values, W, ratio=RATIO["val"] if constrain else None)
        # reject observations that are grossly off the model (broken reductions, e.g. empty SCI arrays), then refit
        bad = np.abs(np.nanmedian(RES[:, band_ok], axis=1)) > 0.5
        if bad.any() and (~bad).sum() >= 3:
            DROPPED[:] = list(om.obs[bad]); om = om[~bad].reset_index(drop=True)
            SIG = np.vstack(om.sig.values); W = np.sqrt(om.n.values).clip(2, 12) / 6
            A, B, RES = fit_model(SIG, om["T"].values, om.texp.values, W, ratio=RATIO["val"] if constrain else None)
        if borrow: break
        mod = model(m["T"].values[idx_all], m.texp.values[idx_all], A, B)
        r = np.nanmedian(np.log10(sig_sp / mod)[:, band_ok], axis=1); okr = np.isfinite(r)
        coef = np.linalg.lstsq(X[okr], r[okr], rcond=None)[0]
    if borrow:
        mod = model(m["T"].values[idx_all], m.texp.values[idx_all], A, B)
        r = np.nanmedian(np.log10(sig_sp / mod)[:, band_ok], axis=1); coef = coef.copy(); coef[0] = np.nanmedian(r[np.isfinite(r)] - (X[np.isfinite(r), 1:] @ coef[1:]))
    pen0 = coef[0]; A_c = A * 10 ** (2 * pen0); B_c = B * 10 ** (2 * pen0); coef_c = coef.copy(); coef_c[0] = 0
    mult = 10 ** (X[:, 1:] @ coef_c[1:])
    pos_tab = [[float(10 ** (coef_c[1:] @ np.array([x ** 2, y ** 2, x ** 4, y ** 4]))) for y in (0, 0.15, 0.3, 0.4, 0.5)] for x in (0, 0.15, 0.3, 0.4, 0.5)]
    for k, (lo, hi) in BANDS5.items(): om["res_" + k] = np.nanmedian(RES[:, (wc >= lo) & (wc < hi)], axis=1)
    om["res"] = np.nanmedian(RES[:, band_ok], axis=1)
    sl = []
    for i in range(NB):
        okk = np.isfinite(SIG[:, i]) & (SIG[:, i] > 0)
        sl.append(np.polyfit(np.log10(om["T"].values[okk]), np.log10(SIG[okk, i]), 1)[0] if okk.sum() >= 4 and om["T"][okk].max() / om["T"][okk].min() > 2 else np.nan)
    A1, B1, RES1 = fit_model(SIG, om["T"].values, om.texp.values, W, p=1) if RATIO["val"] is None else (A, B, RES); A0, B0, RES0 = fit_model(SIG, om["T"].values, om.texp.values, W, p=0) if RATIO["val"] is None else (A, B, RES)
    sc = lambda R: float(np.nanmedian(np.sqrt(np.nanmean(R ** 2, axis=0))))
    te_ref = float(np.median(m.texp[std3])); fr = (B_c / te_ref ** 2) / (A_c + B_c / te_ref ** 2)
    within = float(pd.Series(r - X @ coef).groupby(m.obs.values[idx_all]).std().median())
    print(f"[{key}] B constrained from {RATIO['from']}" if RATIO["val"] is not None else f"[{key}] free A,B fit"); print(f"[{key}] dropped obs: {DROPPED}")
    print(f"[{key}] obs={len(om)} programs={om.program.nunique()} T {om['T'].min():.0f}-{om['T'].max():.0f} texp {om.texp.min():.0f}-{om.texp.max():.0f} slope={np.nanmedian(sl):.3f} scatter p2/p1/p0={sc(RES):.3f}/{sc(RES1):.3f}/{sc(RES0):.3f} within={within:.3f} RNfrac(te={te_ref:.0f})={np.nanmedian(fr[band_ok]):.2f} coef={np.round(coef_c,3)}")
    # 1D / correlation quantities
    idx = idx_all
    sig_pix = pool("ss_sci_src", "n_src", idx); err_pix = np.nanmedian(a["err_src"][idx], axis=0)
    sig3 = pool("ss_f3", "n_f3", idx); sigo = pool("ss_fo", "n_fo", idx); e3 = np.nanmedian(a["e3"][idx], axis=0); eo = np.nanmedian(a["eopt"][idx], axis=0); e5 = np.nanmedian(a["e5"][idx], axis=0)
    zoff = pool("ss_z_off", "n_z_off", idx); zsrc = pool("ss_z_src", "n_z_src", idx)
    rho_d = 1 - pool("ss_dz_disp", "n_dz_disp", idx) ** 2 / (2 * zoff ** 2); rho_s = 1 - pool("ss_dz_spat", "n_dz_spat", idx) ** 2 / (2 * zoff ** 2)
    rho1 = 1 - pool("ss_d3", "n_d3", idx) ** 2 / (2 * sig3 ** 2)
    oned = pd.DataFrame(dict(wave=wc, sig_pix=sig_pix, err_pix=err_pix, z_src=zsrc, z_off=zoff, sig_3px=sig3, e3=e3, z3=sig3 / e3, sig_opt=sigo, eo=eo, zo=sigo / eo, e5=e5,
                             f3=sig3 / sig_pix, fo=sigo / sig_pix, rho_disp=rho_d, rho_spat=rho_s, rho_1d=rho1, dlds=np.nanmedian(a["dlds"][idx], axis=0)))
    cfg = []
    for name, s in [("5-shutter slitlet", faint & (m.n_nodpos == 3) & (m.shlen == 5)), ("2-shutter slitlet", faint & (m.n_nodpos == 3) & (m.shlen == 2)),
                    ("1 nod position", faint & (m.n_nodpos == 1)), ("2 nod positions", faint & (m.n_nodpos == 2)), ("6 nod positions", faint & (m.n_nodpos == 6)),
                    ("stuck-shutter flag", faint & (m.n_nodpos == 3) & m.stuck), ("dithered", faint & (m.n_nodpos == 3) & (m.dither_type != "NONE"))]:
        j = np.where(s)[0]
        if len(j) < 20: continue
        sg = per_spec("ss_sci_src", "n_src", j); Xj = np.c_[m.ax.values[j] ** 2, m.ay.values[j] ** 2, m.ax.values[j] ** 4, m.ay.values[j] ** 4]
        sg = sg / 10 ** (Xj @ coef_c[1:])[:, None]; mod = model(m["T"].values[j], m.texp.values[j], A_c, B_c); rr = np.nanmedian(np.log10(sg / mod), axis=0)
        cfg.append(dict(config=name, n=int(len(j)), nobs=int(m.obs[s].nunique()), **{k: float(np.nanmedian(rr[(wc >= lo) & (wc < hi)])) for k, (lo, hi) in BANDS5.items()}))
    gl = []
    for lo, hi in [(10, 30), (30, 100)]:
        j = np.where((m.snr_med >= lo) & (m.snr_med < hi) & (m.n_nodpos == 3) & (m.shlen == 3))[0]
        if len(j) < 20: continue
        es = a["err_src"][j]; eo_ = a["err_open"][j]; f3_ = a["fnu3"][j]; T = m["T"].values[j][:, None]
        g = 3 * (es ** 2 - eo_ ** 2) * T / f3_; g[f3_ <= 0] = np.nan; gl.append(np.nanmedian(g, axis=0))
    gpo = np.nanmedian(np.array(gl), axis=0) if gl else np.full(NB, np.nan)
    g_source = "measured"
    if (~np.isfinite(gpo[band_ok])).mean() > 0.5 or not gl:
        # fallback: scale PRISM's g by the pixel-bandwidth ratio (g ~ 1/(throughput * dlambda_pix))
        if prism_fn.exists():
            Pp = json.load(open(prism_fn)); gp = np.array(Pp["g_poisson"], float); dp = np.array(Pp["oned"]["dlds"])
            gpo = gp * dp / oned.dlds.values; g_source = "scaled from PRISM by pixel bandwidth"
    with np.errstate(all="ignore"):
        lg = np.log(gpo); okg = np.isfinite(lg)
        if okg.sum() > 5: gpo = np.where(okg, np.exp(np.interp(np.arange(NB), np.where(okg)[0], np.convolve(lg[okg], np.ones(3) / 3, mode="same"))), np.nan)
    out = dict(key=key, wave=wc.tolist(), A=A_c.tolist(), B=B_c.tolist(), A_p1=(A1 * 10 ** (2 * pen0)).tolist(), B_p1=(B1 * 10 ** (2 * pen0)).tolist(), pbest=2,
               band_ok=band_ok.tolist(), pos_coef=coef_c.tolist(), pos_borrowed=bool(borrow), B_constrained=RATIO["val"] is not None, B_from=RATIO["from"], dropped_obs=DROPPED, pen_median=float(-pen0), pos_tab=pos_tab,
               mult_median=float(np.median(mult)), mult_mean=float(np.mean(mult)), mult_84=float(np.percentile(mult, 84)),
               oned=oned.to_dict(orient="list"), cfg=cfg, g_poisson=[None if not np.isfinite(v) else float(v) for v in gpo], g_source=g_source,
               rn_frac=fr.tolist(), te_ref=te_ref, slope=float(np.nanmedian(sl)), scatter=dict(p2=sc(RES), p1=sc(RES1), p0=sc(RES0)),
               obs_scatter={k: float(np.nanstd(om["res_" + k])) for k in BANDS5}, within_obs_scatter=within,
               n_all=int(len(m)), n_faint=int(faint.sum()), n_std3=int(std3.sum()), std_def=std_def, n_obs=int(len(om)), n_programs=int(om.program.nunique()),
               T_range=[float(om["T"].min()), float(om["T"].max())], texp_range=[float(om.texp.min()), float(om.texp.max())],
               readpatt=m.readpatt[std3].value_counts().to_dict(), om=om.drop(columns=["sig"]).to_dict(orient="records"))
    json.dump(out, open(d / "final.json", "w"), default=float); om.to_pickle(d / "om.pkl"); m.to_csv(d / "meta2.csv", index=False)


# ============================================================ phot

def phot(P: Paths, key: str):
    import pandas as pd
    d = P.out(key); m = pd.read_csv(d / "meta2.csv"); a = np.load(d / "arrays.npz", allow_pickle=True); wc = WC
    s = pd.read_csv(P.meta / "spectra.csv")[["spectrum_id", "object_id", "field"]]; ph = pd.read_csv(P.meta / "photometry.csv")
    cols = ["object_id"] + [f"f_{b}" for b in BANDWAVE] + [f"e_{b}" for b in BANDWAVE]
    mm = m.merge(s, on="spectrum_id", how="left").merge(ph[cols].drop_duplicates("object_id"), on="object_id", how="left")
    for b, w in BANDWAVE.items():
        i = (wc > w - 0.2) & (wc < w + 0.2)
        mm[f"spec_{b}"] = np.nanmedian(a["fnu3"][:, i], axis=1); mm[f"speco_{b}"] = np.nanmedian(a["fnuopt"][:, i], axis=1)
        mm[f"snr_{b}"] = np.nanmedian(a["snr3"][:, i], axis=1); mm[f"mag_{b}"] = -2.5 * np.log10(mm[f"f_{b}"].where(mm[f"f_{b}"] > 0)) + 23.9
    mm.to_csv(d / "phot.csv", index=False)
    std = (mm.n_nodpos == 3) & (mm.shlen == 3); b = BAND[key]; rec = {}
    f = mm[f"f_{b}"]; e = mm[f"e_{b}"]; good = std & (f > 0) & (e > 0) & (f / e > 10) & (mm[f"snr_{b}"] > 3)
    r3 = (mm[f"spec_{b}"] / f)[good]; ro = (mm[f"speco_{b}"] / f)[good]; fw = mm.fwhm_opt[good]
    rows = []
    for lo, hi in [(1.4, 1.8), (1.8, 2.0), (2.0, 2.2), (2.2, 2.4), (2.4, 2.6), (2.6, 2.8), (2.8, 3.0), (3.0, 3.5)]:
        sel = (fw >= lo) & (fw < hi)
        rows.append(dict(fwhm_lo=lo, fwhm_hi=hi, n=int(sel.sum()), r3=float(np.nanmedian(r3[sel])) if sel.sum() > 3 else None, ro=float(np.nanmedian(ro[sel])) if sel.sum() > 3 else None,
                         r3_16=float(np.nanpercentile(r3[sel], 16)) if sel.sum() > 5 else None, r3_84=float(np.nanpercentile(r3[sel], 84)) if sel.sum() > 5 else None))
    rec[b] = rows; rec_from = "measured"
    if good.sum() < 50:
        pfn = P.workdir / "dispersers" / "prism_clear" / "phot_results.json"
        if pfn.exists() and key != "prism_clear":
            PP = json.load(open(pfn)); rec[b] = PP["recovery"][PP["band"]]; rec_from = f"borrowed from PRISM ({PP['band']})"
    snr_tab = {}
    for Tlo, Thi in sorted(set([(2000, 3500), (4500, 7000), (10000, 20000), (20000, 200000)])):
        sel = std & (mm.effexptm >= Tlo) & (mm.effexptm < Thi) & (mm[f"f_{b}"] > 0) & np.isfinite(mm[f"snr_{b}"])
        if sel.sum() < 30: continue
        g = mm[sel].groupby(pd.cut(mm[f"mag_{b}"][sel], np.arange(20, 30.1, 1.0)), observed=True)[f"snr_{b}"]
        snr_tab[f"{Tlo/1000:g}-{Thi/1000:g} ks"] = [dict(mag_lo=float(iv.left), mag_hi=float(iv.right), n=int(c), snr_med=float(v), snr_25=float(q1), snr_75=float(q3))
                                                 for iv, v, c, q1, q3 in zip(g.median().index, g.median().values, g.size().values, g.quantile(.25).values, g.quantile(.75).values) if c >= 5]
    print(f"[{key}] phot band {b}: n={good.sum()} median 3px/phot={np.nanmedian(r3):.3f} opt/phot={np.nanmedian(ro):.3f}; snr tables: {list(snr_tab)}")
    json.dump(dict(band=b, band_wave=BANDWAVE[b], n_match=int(good.sum()), rec_from=rec_from, r3_median=float(np.nanmedian(r3)) if good.sum() >= 50 else None, ro_median=float(np.nanmedian(ro)) if good.sum() >= 50 else None, recovery=rec, snr_vs_mag=snr_tab), open(d / "phot_results.json", "w"))


# ============================================================ depth / payload

def depth(P: Paths, key: str):
    from astropy.io import fits
    d = P.out(key); F = json.load(open(d / "final.json")); PH = json.load(open(d / "phot_results.json"))
    grating = key.split("_")[0]; disp = fits.open(P.disp_dir / f"jwst_nirspec_{grating}_disp.fits")[1].data
    wc = WC; A = np.array(F["A"]); B = np.array(F["B"]); od = F["oned"]; ok = np.array(F["band_ok"])
    f3 = np.array(od["f3"]); fo = np.array(od["fo"]); rho1 = np.clip(np.nan_to_num(np.array(od["rho_1d"], float), nan=0), -0.2, 0.5)
    dlds = np.array(od["dlds"], float); R = np.interp(wc, disp["WAVELENGTH"], disp["R"])
    dl_ref = np.interp(wc, disp["WAVELENGTH"], disp["DLDS"]); dlds = np.where(np.isfinite(dlds), dlds, dl_ref)
    g = np.array([np.nan if v is None else v for v in F["g_poisson"]], float)
    n_res = (wc / R) / dlds
    te0 = 1000.0
    def sig_pix(T, te): return np.sqrt(A / T + B / (T * te ** 2))
    def sig_1d(T, te, kind): return sig_pix(T, te) * (fo if kind == "opt" else f3)
    def line_5sig(T, te, kind):
        s = sig_1d(T, te, kind); flam = s / wc ** 2 * 2.99792458e-19; n = np.maximum(2 * n_res, 2); neff = n * (1 + 2 * rho1 * (n - 1) / n)
        return 5 * flam * (dlds * 1e4) * np.sqrt(neff) / 0.98
    def ab5(T, te, kind, res=False):
        s = sig_1d(T, te, kind)
        if res: n = np.maximum(n_res, 1); neff = n * (1 + 2 * rho1 * (n - 1) / n); s = s * np.sqrt(neff) / n
        return -2.5 * np.log10(5 * s * 1e-6) + 8.9
    Ts = [1000, 2000, 3000, 5000, 10000, 20000, 50000, 100000]
    lam_out = [float(w) for w in wc[ok][::max(1, int(np.ceil(ok.sum() / 11)))]]
    ii = [int(np.argmin(np.abs(wc - l))) for l in lam_out]
    tab = {}
    for T in Ts:
        tab[T] = dict(sig_pix_nJy=(1e3 * sig_pix(T, te0))[ii].tolist(), sig_opt_nJy=(1e3 * sig_1d(T, te0, "opt"))[ii].tolist(), sig_3px_nJy=(1e3 * sig_1d(T, te0, "3px"))[ii].tolist(),
                      ab5_pix=ab5(T, te0, "opt")[ii].tolist(), ab5_res=ab5(T, te0, "opt", True)[ii].tolist(), line5=line_5sig(T, te0, "opt")[ii].tolist())
    payload = dict(key=key, wave=wc.tolist(), band_ok=ok.tolist(), A=A.tolist(), B=B.tolist(), f3=f3.tolist(), fo=fo.tolist(), rho1=rho1.tolist(), dlds=dlds.tolist(), R=R.tolist(),
                   g=[None if not np.isfinite(v) else float(v) for v in g], g_source=F["g_source"], pos_coef=F["pos_coef"], pos_borrowed=F["pos_borrowed"], pos_tab=F["pos_tab"],
                   mult_median=F["mult_median"], mult_mean=F["mult_mean"], mult_84=F["mult_84"], recovery=PH["recovery"], rec_band=PH["band"], rec_n=PH["n_match"],
                   r3_median=PH["r3_median"], ro_median=PH["ro_median"], rec_from=PH.get("rec_from", "measured"), B_constrained=F.get("B_constrained", False), B_from=F.get("B_from"), snr_vs_mag=PH["snr_vs_mag"], z3=od["z3"], zo=od["zo"], z_src=od["z_src"], rho_disp=od["rho_disp"], rho_spat=od["rho_spat"],
                   scatter=F["scatter"], obs_scatter=F["obs_scatter"], within=F["within_obs_scatter"], table=tab, lam_out=lam_out, Ts=Ts, te0=te0, rn_frac=F["rn_frac"], te_ref=F["te_ref"],
                   cfg=F["cfg"], dropped_obs=F.get("dropped_obs", []), slope=F["slope"], std_def=F.get("std_def", "3 nod positions"), n_all=F["n_all"], n_faint=F["n_faint"], n_std3=F["n_std3"], n_obs=F["n_obs"], n_programs=F["n_programs"], T_range=F["T_range"],
                   texp_range=F["texp_range"], readpatt=F["readpatt"], n_res=n_res.tolist(), sig_pix_med=[float(x) for x in od["sig_pix"]])
    json.dump(payload, open(d / "payload.json", "w"), default=float)
    print(f"[{key}] depth: lam_out={np.round(lam_out,2).tolist()} n_res~{np.nanmedian(n_res[ok]):.2f}; 10ks 5sig/px AB at mid: {np.array(tab[10000]['ab5_pix'])[len(ii)//2]:.2f}; line5 at mid: {np.array(tab[10000]['line5'])[len(ii)//2]:.2e}")


# ============================================================ figs

def figs(P: Paths, key: str):
    import pandas as pd
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    d = P.out(key); Pd = json.load(open(d / "payload.json")); om = pd.read_pickle(d / "om.pkl"); F = json.load(open(d / "final.json"))
    wc = WC; ok = np.array(Pd["band_ok"]); A = np.array(Pd["A"]); B = np.array(Pd["B"]); pen0 = F["pen_median"]
    om["sig"] = [s / 10 ** pen0 for s in om.sig]
    plt.rcParams.update({"font.size": 10, "axes.grid": True, "grid.alpha": 0.3, "figure.dpi": 130, "savefig.dpi": 130, "savefig.bbox": "tight", "axes.facecolor": "white", "figure.facecolor": "white"})
    C = plt.cm.viridis; name = key.upper().replace("_", "/")
    def model(T, te): return np.sqrt(A / T + B / (T * te ** 2))
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    for i, T in enumerate([1e3, 3e3, 1e4, 3e4, 1e5]): ax.plot(wc[ok], 1e3 * model(T, 1000)[ok], color=C(i / 4.5), lw=2, label=f"T = {T/1e3:g} ks")
    ax.set_yscale("log"); ax.set_xlabel("wavelength [µm]"); ax.set_ylabel("1σ per-pixel noise  [nJy / pixel]"); ax.set_title(f"{name}: per-pixel noise, centred source (3-nod, t_exp = 1000 s)"); ax.legend(ncol=2, fontsize=9)
    fig.savefig(d / "fig1_sigma_lambda.png"); plt.close(fig)
    wsel = wc[ok]; picks = [wsel[int(len(wsel) * q)] for q in (0.15, 0.5, 0.85)]
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8))
    for ax, w0 in zip(axes, picks):
        i = int(np.argmin(np.abs(wc - w0))); sig = np.array([s[i] for s in om.sig]) * 1e3
        sc = ax.scatter(om["T"], sig, c=np.log10(om.texp), cmap="coolwarm", s=18 + om.n / 8, edgecolor="k", lw=0.3, zorder=3, vmin=2.6, vmax=3.65)
        Tg = np.logspace(2.8, 5.3, 50)
        for te, ls in [(423, ":"), (1000, "-"), (2772, "--")]: ax.plot(Tg, 1e3 * np.sqrt(A[i] / Tg + B[i] / (Tg * te ** 2)), "k", ls=ls, lw=1.2, label=f"t_exp = {te} s")
        ax.set_xscale("log"); ax.set_yscale("log"); ax.set_xlabel("total exposure time T [s]"); ax.set_title(f"λ = {wc[i]:.2f} µm")
    axes[0].set_ylabel("1σ per pixel [nJy]"); axes[0].legend(fontsize=8); cb = fig.colorbar(sc, ax=axes, pad=0.01); cb.set_label("log10 t_exp [s]")
    fig.savefig(d / "fig2_sigma_T.png"); plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4), gridspec_kw=dict(width_ratios=[2, 1]))
    ax = axes[0]; order = om.groupby("program").res.median().sort_values().index
    ax.boxplot([om.res[om.program == p].values for p in order], tick_labels=[str(p) for p in order], widths=0.6); ax.axhline(0, color="k", lw=0.8)
    ax.set_ylabel("log10 (measured / model)"); ax.set_xlabel("JWST program ID"); ax.tick_params(axis="x", rotation=90, labelsize=8); ax.set_title(f"{name}: observation-level residuals")
    ax = axes[1]; ax.scatter(np.abs(om.ecl_lat), om.res, c=om.sun_dlon, cmap="plasma", s=18 + om.n / 8, edgecolor="k", lw=0.3); ax.axhline(0, color="k", lw=0.8)
    ax.set_xlabel("|ecliptic latitude| [deg]"); ax.set_ylabel("residual [dex]"); ax.set_title("vs ecliptic latitude (colour: solar elongation)")
    fig.savefig(d / "fig3_residuals.png"); plt.close(fig)
    od = pd.DataFrame(F["oned"]); fig, axes = plt.subplots(1, 2, figsize=(12, 3.8)); ax = axes[0]
    ax.plot(wc[ok], od.z_src[ok], label="2D per-pixel: empirical / pipeline ERR (source rows)", lw=2); ax.plot(wc[ok], od.z_off[ok], label="2D per-pixel: empirical / ERR (other rows)", lw=1.2, alpha=0.7)
    ax.plot(wc[ok], od.z3[ok], label="1D 3-px boxcar: empirical / fnu_3px_err", lw=2); ax.plot(wc[ok], od.zo[ok], label="1D optimal: empirical / fnu_err", lw=2)
    ax.axhline(1, color="k", lw=0.8); ax.set_ylim(0.5, 1.6); ax.set_xlabel("wavelength [µm]"); ax.set_ylabel("ratio"); ax.legend(fontsize=8); ax.set_title(f"{name}: how realistic are the pipeline errors?")
    ax = axes[1]; ax.plot(wc[ok], od.rho_disp[ok], label="adjacent pixels along dispersion"); ax.plot(wc[ok], od.rho_spat[ok], label="adjacent pixels along slit")
    ax.plot(wc[ok], od.f3[ok], label="σ(3-px boxcar) / σ(pixel)", lw=2); ax.plot(wc[ok], od.fo[ok], label="σ(optimal) / σ(pixel)", lw=2)
    ax.set_xlabel("wavelength [µm]"); ax.set_ylabel("correlation  /  ratio"); ax.legend(fontsize=8); ax.set_title("pixel correlations and 1D/2D noise ratio")
    fig.savefig(d / "fig4_pipeline_vs_empirical.png"); plt.close(fig)
    ph = pd.read_csv(d / "phot.csv"); b = Pd["rec_band"]; bw = BANDWAVE[b]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4)); ax = axes[0]
    for (lab, rows), col in zip(Pd["snr_vs_mag"].items(), C(np.linspace(0, 0.95, max(len(Pd["snr_vs_mag"]), 2)))):
        x = [0.5 * (r["mag_lo"] + r["mag_hi"]) for r in rows]; ax.plot(x, [r["snr_med"] for r in rows], "o-", color=col, label=f"T = {lab} (n={sum(r['n'] for r in rows)})")
        ax.fill_between(x, [r["snr_25"] for r in rows], [r["snr_75"] for r in rows], color=col, alpha=0.15)
    ax.set_yscale("log"); ax.set_xlabel(f"{b.upper()} total magnitude [AB]"); ax.set_ylabel(f"median S/N per pixel at {bw-0.2:.1f}–{bw+0.2:.1f} µm (3-px boxcar)"); ax.legend(fontsize=8); ax.set_title(f"{name}: achieved S/N vs magnitude")
    ax.axhline(3, color="k", lw=0.6, ls=":"); ax.axhline(5, color="k", lw=0.6, ls=":")
    ax = axes[1]; f = ph[f"f_{b}"]; e = ph[f"e_{b}"]; good = (f > 0) & (e > 0) & (f / e > 10) & (ph[f"snr_{b}"] > 3) & (ph.n_nodpos == 3) & (ph.shlen == 3)
    r = (ph[f"spec_{b}"] / f)[good]; fw = ph.fwhm_opt[good]; g = r.groupby(pd.cut(fw, np.arange(1.4, 3.61, 0.2)), observed=True)
    x = np.array([iv.mid for iv in g.median().index]); ax.plot(x, g.median().values, "o-", color="C1", label=f"{b.upper()} (n={good.sum()})"); ax.fill_between(x, g.quantile(0.25).values, g.quantile(0.75).values, color="C1", alpha=0.12)
    ax.set_xlabel("spatial FWHM of the extracted profile [px]"); ax.set_ylabel("spectrum (3-px) / total photometric flux"); ax.set_ylim(0, 1.0); ax.legend(fontsize=8); ax.set_title("flux recovered through the MSA vs source size")
    fig.savefig(d / "fig5_snr_mag_slitloss.png"); plt.close(fig); print(f"[{key}] figs done")


# ============================================================ driver

def main(a) -> None:
    P = Paths(a.root, a.workdir, a.disp_dir)
    if a.step == "assemble":
        from .assemble import assemble, update_manifest
        if not a.version:
            raise SystemExit("assemble needs --version (e.g. 2026.09)")
        out_dir = Path(a.out_dir) if a.out_dir else Path(__file__).resolve().parents[1] / "models"
        fname = f"nirspec-{a.version}.json"
        model = assemble(P.workdir, P.disp_dir, out_dir / fname, version=a.version, built=a.built,
                         pandeia_json=Path(a.pandeia) if a.pandeia else None, notes=a.notes)
        update_manifest(out_dir, a.version, fname, model["built"], a.notes, make_latest=not a.no_latest)
        return
    keys = KEYS if a.dispersers == "all" else a.dispersers.split(",")
    for key in keys:
        if key not in KEYS:
            raise SystemExit(f"unknown disperser {key}; choose from {KEYS}")
        if a.step in ("harvest", "all") and not (a.step == "all" and (P.out(key) / "arrays.npz").exists()):
            harvest(P, key, nproc=a.processes, limit=a.limit)
        if a.step in ("fit", "all"): fit(P, key)
        if a.step in ("phot", "all"): phot(P, key)
        if a.step in ("depth", "all"): depth(P, key)
        if a.step in ("figs", "all"): figs(P, key)
