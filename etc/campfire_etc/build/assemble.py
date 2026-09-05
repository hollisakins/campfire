"""Assemble a versioned model JSON from the per-disperser build outputs.

Inputs (per disperser, under ``<workdir>/dispersers/<key>/``): ``payload.json``
written by the ``depth`` step. Plus the NIRSpec dispersion reference files
(``jwst_nirspec_<grating>_disp.fits``) for the native pixel grid and,
optionally, the pandeia comparison runs (``etc_results_all.json``).

The output schema (``schema: 1``) is what `campfire_etc.model` reads; keep it
backwards compatible or bump the schema number.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import numpy as np

KEYS = ["prism_clear", "g140m_f070lp", "g140m_f100lp", "g140h_f100lp",
        "g235m_f170lp", "g235h_f170lp", "g395m_f290lp", "g395h_f290lp"]
RESOLUTION_CLASS = {k: ("prism" if k.startswith("prism") else "high" if k.split("_")[0].endswith("h") else "medium") for k in KEYS}
BANDS5 = ["0.6-1.0", "1.0-2.0", "2.0-3.0", "3.0-4.0", "4.0-5.3"]
READOUT_GROUP_S = {"nrsirs2": 72.944, "nrsirs2rapid": 14.589}


def _clean(v: Any) -> Any:
    """Recursively turn numpy scalars/arrays into JSON-safe values, NaN -> None."""
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_clean(x) for x in v]
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def _coverage(p: dict) -> list[float]:
    w = np.array(p["wave"]); ok = np.array(p["band_ok"], bool)
    return [float(w[ok].min() - 0.05), float(w[ok].max() + 0.05)]


def dispersion_curve(disp_fits: Path, coverage: list[float], max_points: int = 240) -> dict[str, list[float]]:
    """Subsample the reference dispersion table to the coverage (+ margin)."""
    from astropy.io import fits  # build extra
    d = fits.open(disp_fits)[1].data
    w = np.asarray(d["WAVELENGTH"], float); dl = np.asarray(d["DLDS"], float); R = np.asarray(d["R"], float)
    sel = (w >= coverage[0] - 0.15) & (w <= coverage[1] + 0.15)
    w, dl, R = w[sel], dl[sel], R[sel]
    step = max(1, int(np.ceil(w.size / max_points)))
    idx = np.unique(np.r_[np.arange(0, w.size, step), w.size - 1])
    return {"wave": w[idx].round(5).tolist(), "dlds": dl[idx].tolist(), "R": R[idx].round(3).tolist()}


def pandeia_block(runs: list[dict], p: dict) -> dict[str, Any] | None:
    """Ratios of the pandeia noise to the empirical model on the model grid."""
    key = p["key"]
    mine = [r for r in runs if r.get("disperser", "").replace("/", "_") == key]
    if not mine:
        return None
    wc = np.array(p["wave"]); ok = np.array(p["band_ok"], bool)
    A = np.array(p["A"]); B = np.array(p["B"]); fo = np.array(p["fo"])
    out = []
    for e in mine:
        T = e["total_exposure_time"]; te = READOUT_GROUP_S[e["readout"]] * e["ngroup"]
        w = np.array(e["wave"]); n = np.array(e["noise_mjy"]) * 1e6; good = np.isfinite(n)
        etc_i = np.interp(wc, w[good], n[good], left=np.nan, right=np.nan)
        ours = 1e3 * np.sqrt(A / T + B / (T * te ** 2)) * fo
        r = etc_i / ours; r[~ok] = np.nan
        by_band = {}
        for b in BANDS5:
            lo, hi = [float(x) for x in b.split("-")]; sel = (wc >= lo) & (wc < hi)
            by_band[b] = float(np.nanmedian(r[sel])) if np.isfinite(r[sel]).any() else None
        out.append({"label": e["label"].split(" ", 1)[1], "total_s": T, "per_exposure_s": te, "background": e["bg"],
                    "ratio_median": float(np.nanmedian(r)), "ratio_by_band": by_band,
                    "noise_curve_njy": _clean(etc_i)})
    return {"version": "2026.7", "runs": out}


def assemble(workdir: Path, disp_dir: Path, out_path: Path, *, version: str, built: str | None = None,
             pandeia_json: Path | None = None, notes: str = "") -> dict[str, Any]:
    workdir = Path(workdir); disp_dir = Path(disp_dir); out_path = Path(out_path)
    runs = json.load(open(pandeia_json)) if pandeia_json and Path(pandeia_json).exists() else []
    dispersers: dict[str, Any] = {}
    n_all = n_std = n_obs = 0; programs: set[str] = set()
    for key in KEYS:
        fn = workdir / "dispersers" / key / "payload.json"
        if not fn.exists():
            print(f"[assemble] missing {fn}, skipping {key}")
            continue
        p = json.load(open(fn))
        cov = _coverage(p)
        grating = key.split("_")[0]
        rec_band = p["rec_band"]
        d = {
            "name": key.upper().replace("_", "/"),
            "grating": grating.upper(), "filter": key.split("_")[1].upper(),
            "resolution_class": RESOLUTION_CLASS[key],
            "coverage": cov,
            "wave": p["wave"], "band_ok": p["band_ok"],
            "A": p["A"], "B": p["B"], "f3": p["f3"], "fo": p["fo"], "rho1": p["rho1"], "g": p["g"],
            "dlds": p["dlds"], "R": p["R"], "n_res": p["n_res"], "lam_out": p["lam_out"],
            "g_source": p["g_source"],
            "pos_coef": p["pos_coef"], "pos_borrowed": p["pos_borrowed"], "pos_tab": p.get("pos_tab"),
            "mult_median": p["mult_median"], "mult_mean": p["mult_mean"], "mult_84": p.get("mult_84"),
            "recovery": {"band": rec_band, "from": p.get("rec_from", "measured"), "n_match": p.get("rec_n"),
                         "r3_median": p.get("r3_median"), "ro_median": p.get("ro_median"),
                         "rows": p["recovery"][rec_band]},
            "B_constrained": p.get("B_constrained", False), "B_from": p.get("B_from"),
            "rn_frac": p["rn_frac"], "te_ref": p["te_ref"], "slope": p["slope"],
            "scatter": p["scatter"], "obs_scatter": p["obs_scatter"], "within": p["within"],
            "pipeline_error_ratio": {"pixel_2d": p["z_src"], "oned_3px": p["z3"], "oned_optimal": p["zo"]},
            "rho_2d": {"dispersion": p["rho_disp"], "spatial": p["rho_spat"]},
            "cfg": p["cfg"], "dropped_obs": p.get("dropped_obs", []), "std_def": p.get("std_def"),
            "sample": {"n_all": p["n_all"], "n_faint": p["n_faint"], "n_std": p["n_std3"], "n_obs": p["n_obs"],
                       "n_programs": p["n_programs"], "T_range": p["T_range"], "texp_range": p["texp_range"],
                       "readpatt": p["readpatt"]},
            "snr_vs_mag": p["snr_vs_mag"],
            "dispersion": dispersion_curve(disp_dir / f"jwst_nirspec_{grating}_disp.fits", cov),
        }
        pb = pandeia_block(runs, p)
        if pb:
            d["pandeia"] = pb
        dispersers[key] = _clean(d)
        n_all += p["n_all"]; n_std += p["n_std3"]; n_obs += p["n_obs"]
    model = {
        "schema": 1,
        "version": version,
        "built": built or dt.date.today().isoformat(),
        "notes": notes,
        "archive": {"n_spectra": n_all, "n_faint_standard_fitted": n_std, "n_observations_fitted": n_obs,
                    "reduction": "CAMPFIRE (jwst 1.14-1.20, nod subtraction, no bar-shadow correction, native-scale drizzle)"},
        "readout_seconds_per_group": {"nrsirs2": 72.944, "nrsirs2rapid": 14.589, "nrs": 42.947, "nrsrapid": 10.737},
        "dispersers": dispersers,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(model, f, separators=(",", ":"))
    print(f"[assemble] wrote {out_path} ({out_path.stat().st_size / 1024:.0f} kB, {len(dispersers)} dispersers)")
    return model


def update_manifest(models_dir: Path, version: str, filename: str, built: str, notes: str = "", make_latest: bool = True) -> None:
    mf = Path(models_dir) / "manifest.json"
    manifest = json.load(open(mf)) if mf.exists() else {"latest": version, "versions": []}
    manifest["versions"] = [v for v in manifest["versions"] if v["version"] != version]
    manifest["versions"].append({"version": version, "file": filename, "built": built, "notes": notes})
    manifest["versions"].sort(key=lambda v: v["version"])
    if make_latest:
        manifest["latest"] = version
    with open(mf, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"[assemble] manifest: latest = {manifest['latest']}, versions = {[v['version'] for v in manifest['versions']]}")
