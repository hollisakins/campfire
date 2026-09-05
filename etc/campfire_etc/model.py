"""Empirical NIRSpec/MSA noise model: loading, exposure timing and the depth /
signal-to-noise arithmetic. numpy only.

The model, per disperser/filter, is the per-pixel variance of a centred faint
point source in the drizzled 2-D spectrum,

    sigma_pix^2(lambda) = A(lambda) / T + B(lambda) / (T * t_exp^2)     [uJy^2]

with T the total on-source time and t_exp the per-exposure time (both in
seconds), fitted in 0.1 um bins to the CAMPFIRE archive. Around it sit the
measured multipliers that turn that into a proposal number: the 1-D/2-D noise
ratio of each extraction, the adjacent-pixel correlation, the shutter-placement
penalty, the source-Poisson coefficient and the flux-recovery fractions. See the
published report for the derivation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

# --------------------------------------------------------------------------- constants

C_KMS = 299792.458
# f_lambda [erg s^-1 cm^-2 A^-1] = f_nu [uJy] / lambda[um]^2 * FNU_UJY_TO_FLAM
FNU_UJY_TO_FLAM = 2.99792458e-19
# Seconds per group for the NIRSpec readout patterns (full-frame, IRS2 and
# traditional). Only the IRS2 patterns occur in the archive; NRS/NRSRAPID reuse
# the IRS2 read-noise term and are flagged as extrapolations.
READOUT_GROUP_S: dict[str, float] = {
    "nrsirs2": 72.944,
    "nrsirs2rapid": 14.589,
    "nrs": 42.947,
    "nrsrapid": 10.737,
}
EXTRAPOLATED_READOUTS = frozenset({"nrs", "nrsrapid"})
# Spatial FWHM (drizzled 0.1" pixels) that stands in for each morphology class
# when looking up the flux-recovery table; "point" bypasses the table.
MORPHOLOGY_FWHM_PX: dict[str, float | None] = {
    "point": None,
    "compact": 1.9,
    "typical": 2.3,
    "extended": 2.8,
}
EXTRACTIONS = ("optimal", "3px")
PLACEMENTS = ("typical", "centred", "mean")
# Fraction of an emission line's flux inside a +-1 FWHM window.
LINE_WINDOW_FRACTION = 0.98
# Nearest 0.1-um bin has to be at most this far away for the model to be
# considered defined at a wavelength (guards interpolation across gaps).
_MAX_BIN_GAP_UM = 0.101

# Pivot wavelengths (um) used to normalise an SED "in a band" — a point
# evaluation, not a bandpass integral.
BAND_PIVOT_UM: dict[str, float] = {
    "f090w": 0.90, "f115w": 1.15, "f150w": 1.50, "f200w": 1.99,
    "f277w": 2.76, "f356w": 3.57, "f444w": 4.40,
}


class ModelError(ValueError):
    """Bad inputs to the calculator (unknown disperser, wavelength outside coverage, ...)."""


# --------------------------------------------------------------------------- exposure


@dataclass(frozen=True)
class Exposure:
    """Timing of an observation: total on-source time and per-exposure time."""

    total_s: float
    per_exposure_s: float
    readout: str | None = None
    ngroups: int | None = None
    nint: int = 1
    nexp: int = 1

    @property
    def direct(self) -> bool:
        return self.readout is None

    def describe(self) -> str:
        if self.direct:
            return f"t_exp = {self.per_exposure_s:.0f} s, T = {fmt_time(self.total_s)} on source"
        return (
            f"{self.readout.upper()} with {self.ngroups} groups x {self.nint} integration"
            f"{'s' if self.nint != 1 else ''} x {self.nexp} exposure{'s' if self.nexp != 1 else ''}"
            f" (t_exp = {self.per_exposure_s:.0f} s, T = {fmt_time(self.total_s)} on source)"
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "total_s": round(self.total_s, 1),
            "per_exposure_s": round(self.per_exposure_s, 1),
            "description": self.describe(),
        }
        if not self.direct:
            d.update(readout=self.readout, ngroups=self.ngroups, nint=self.nint, nexp=self.nexp)
        return d

    def scaled(self, total_s: float) -> "Exposure":
        """Same per-exposure time, different total (nexp is rescaled when known)."""
        if self.direct:
            return Exposure(total_s, self.per_exposure_s)
        nexp = max(1, int(math.ceil(total_s / (self.per_exposure_s * self.nint) - 1e-9)))
        return Exposure(self.per_exposure_s * self.nint * nexp, self.per_exposure_s,
                        self.readout, self.ngroups, self.nint, nexp)


def make_exposure(
    readout: str | None = None,
    ngroups: int | None = None,
    nint: int = 1,
    nexp: int = 1,
    total_s: float | None = None,
    per_exposure_s: float | None = None,
) -> Exposure:
    """Build an :class:`Exposure` from either a readout setup or explicit times.

    Give ``readout`` + ``ngroups`` (+ ``nint``, ``nexp``) for the APT-style
    description, or ``total_s`` + ``per_exposure_s`` to bypass it.
    """
    if readout is not None or ngroups is not None:
        if readout is None or ngroups is None:
            raise ModelError("readout and ngroups must be given together")
        key = readout.strip().lower().replace("-", "").replace("_", "")
        if key not in READOUT_GROUP_S:
            raise ModelError(f"unknown readout pattern {readout!r}; choose from {sorted(READOUT_GROUP_S)}")
        ngroups = int(ngroups); nint = int(nint); nexp = int(nexp)
        if ngroups < 2 or nint < 1 or nexp < 1:
            raise ModelError("need ngroups >= 2, nint >= 1 and nexp >= 1")
        te = ngroups * READOUT_GROUP_S[key]
        return Exposure(te * nint * nexp, te, key, ngroups, nint, nexp)
    if total_s is None or per_exposure_s is None:
        raise ModelError("give readout+ngroups(+nint,nexp) or total_s+per_exposure_s")
    total_s = float(total_s); per_exposure_s = float(per_exposure_s)
    if per_exposure_s < 50 or total_s <= 0:
        raise ModelError("per_exposure_s must be >= 50 s and total_s > 0")
    if total_s < per_exposure_s:
        raise ModelError("total_s cannot be smaller than per_exposure_s")
    return Exposure(total_s, per_exposure_s)


def fmt_time(seconds: float) -> str:
    if seconds >= 1000:
        return f"{seconds / 1000:.{1 if seconds >= 10000 else 2}f} ks"
    return f"{seconds:.0f} s"


def fmt_sci(x: float) -> str:
    if not np.isfinite(x) or x <= 0:
        return "nan"
    e = int(math.floor(math.log10(x)))
    return f"{x / 10 ** e:.1f}e{e}"


# --------------------------------------------------------------------------- dispersers


def _finite_interp(x: np.ndarray, xp: np.ndarray, fp: np.ndarray, log: bool = False) -> np.ndarray:
    """Interpolate fp(xp) at x using only finite samples; NaN outside their span
    or where the nearest sample is more than one bin away."""
    ok = np.isfinite(fp)
    if log:
        ok &= fp > 0
    if ok.sum() < 2:
        return np.full_like(x, np.nan, dtype=float)
    xs, ys = xp[ok], fp[ok]
    y = np.interp(x, xs, np.log(ys) if log else ys, left=np.nan, right=np.nan)
    if log:
        y = np.exp(y)
    gap = np.min(np.abs(x[:, None] - xs[None, :]), axis=1)
    y = np.where(gap <= _MAX_BIN_GAP_UM, y, np.nan)
    return y


@dataclass
class Disperser:
    """One disperser/filter combination of the model (a thin view on the JSON)."""

    key: str
    data: Mapping[str, Any]
    _grid_cache: dict[str, np.ndarray] = field(default_factory=dict, repr=False)

    # ---- basic properties
    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def coverage(self) -> tuple[float, float]:
        lo, hi = self.data["coverage"]
        return float(lo), float(hi)

    @property
    def wave(self) -> np.ndarray:
        return np.asarray(self.data["wave"], float)

    @property
    def band_ok(self) -> np.ndarray:
        return np.asarray(self.data["band_ok"], bool)

    def _curve(self, name: str) -> np.ndarray:
        arr = np.array([np.nan if v is None else v for v in self.data[name]], float)
        arr[~self.band_ok] = np.nan
        return arr

    def curves_at(self, wave: np.ndarray | float | Sequence[float]) -> dict[str, np.ndarray]:
        """Model curves interpolated to ``wave`` (um). NaN outside coverage."""
        w = np.atleast_1d(np.asarray(wave, float))
        wc = self.wave
        out = {
            "A": _finite_interp(w, wc, self._curve("A"), log=True),
            "B": _finite_interp(w, wc, self._curve("B"), log=True),
            "f3": _finite_interp(w, wc, self._curve("f3")),
            "fo": _finite_interp(w, wc, self._curve("fo")),
            "rho1": _finite_interp(w, wc, self._curve("rho1")),
            "g": _finite_interp(w, wc, self._curve("g")),
            "dlds": _finite_interp(w, wc, self._curve("dlds"), log=True),
            "R": _finite_interp(w, wc, self._curve("R"), log=True),
        }
        out["rho1"] = np.clip(np.nan_to_num(out["rho1"], nan=0.0), -0.2, 0.5)
        g = out["g"]
        out["g"] = np.where(np.isfinite(g), g, 0.0)
        out["n_res"] = (w / out["R"]) / out["dlds"]
        out["wave"] = w
        return out

    def in_coverage(self, wave: np.ndarray | float) -> np.ndarray:
        lo, hi = self.coverage
        w = np.atleast_1d(np.asarray(wave, float))
        return (w >= lo) & (w <= hi)

    def default_wavelengths(self) -> np.ndarray:
        return np.asarray(self.data.get("lam_out") or self.wave[self.band_ok], float)

    # ---- placement and recovery
    def placement_multiplier(self, x: float, y: float) -> float:
        """Noise multiplier for a source offset |x| (dispersion) and |y| (slit),
        in shutter units (0 = centred, 0.5 = edge)."""
        c = self.data["pos_coef"]
        x = abs(float(x)); y = abs(float(y))
        return float(10 ** (c[1] * x * x + c[2] * y * y + c[3] * x ** 4 + c[4] * y ** 4))

    def recovery(self, fwhm_px: float | None, extraction: str = "optimal") -> float:
        """Fraction of the total photometric flux that lands in the extracted
        spectrum for a source of the given spatial FWHM (None = point source)."""
        if fwhm_px is None:
            return 1.0
        rows = [r for r in self.data["recovery"]["rows"] if r.get("r3") is not None]
        if not rows:
            return 0.42
        xs = np.array([0.5 * (r["fwhm_lo"] + r["fwhm_hi"]) for r in rows])
        key = "ro" if extraction == "optimal" else "r3"
        ys = np.array([r.get(key) if r.get(key) is not None else r["r3"] for r in rows], float)
        return float(np.interp(fwhm_px, xs, ys))

    # ---- native pixel grid (for simulations)
    def pixel_grid(self, oversample: int = 1) -> tuple[np.ndarray, np.ndarray]:
        """Wavelength centres and widths (um) of the native drizzled pixels
        across the disperser's coverage, integrated from the dispersion curve."""
        key = f"grid{oversample}"
        if key not in self._grid_cache:
            d = self.data["dispersion"]
            wref = np.asarray(d["wave"], float); dref = np.asarray(d["dlds"], float)
            lo, hi = self.coverage
            centres = []
            w = lo
            while w <= hi:
                step = float(np.interp(w, wref, dref)) / oversample
                centres.append(w + 0.5 * step)
                w += step
            c = np.array(centres)
            self._grid_cache[key] = c
            self._grid_cache[key + "_d"] = np.interp(c, wref, dref) / oversample
        return self._grid_cache[key], self._grid_cache[key + "_d"]

    def summary(self) -> dict[str, Any]:
        d = self.data; s = d["sample"]
        return {
            "key": self.key,
            "name": self.name,
            "resolution_class": d["resolution_class"],
            "coverage_um": [round(v, 2) for v in self.coverage],
            "median_resolving_power": _round(float(np.nanmedian(self._curve("R")))),
            "spectra_in_archive": s["n_all"],
            "faint_standard_spectra_fitted": s["n_std"],
            "observations": s["n_obs"],
            "programs": s["n_programs"],
            "total_time_range_ks": [round(v / 1000, 1) for v in s["T_range"]],
            "per_exposure_range_s": [round(v) for v in s["texp_range"]],
            "caveats": self.caveats(),
        }

    def caveats(self) -> list[str]:
        d = self.data; out = []
        if d.get("B_constrained"):
            out.append(f"read-noise/sky ratio borrowed from {d.get('B_from')} (archive cannot separate the two terms)")
        if d.get("pos_borrowed"):
            out.append("shutter-placement term borrowed from PRISM (too few faint spectra to fit)")
        if d["recovery"].get("from", "measured") != "measured":
            out.append(f"flux-recovery table {d['recovery']['from']}")
        if d.get("g_source", "measured") != "measured":
            out.append(f"source-Poisson coefficient {d['g_source']}")
        if d["sample"]["n_obs"] < 6:
            out.append(f"only {d['sample']['n_obs']} observations constrain the exposure-time scaling")
        return out


def _round(x: float, n: int = 4) -> float | None:
    if x is None or not np.isfinite(x):
        return None
    if x == 0:
        return 0.0
    return float(f"{x:.{n}g}")


# --------------------------------------------------------------------------- model


_ALIASES = {"prism": "prism_clear", "prism_clear": "prism_clear", "clear": "prism_clear"}


class NoiseModel:
    """A versioned noise model (one JSON file) holding every disperser."""

    def __init__(self, data: Mapping[str, Any], source: str = ""):
        self.data = data
        self.source = source
        self.dispersers: dict[str, Disperser] = {k: Disperser(k, v) for k, v in data["dispersers"].items()}

    # ---- loading
    @classmethod
    def from_file(cls, path: str | Path) -> "NoiseModel":
        path = Path(path)
        with open(path) as f:
            return cls(json.load(f), str(path))

    @property
    def version(self) -> str:
        return self.data["version"]

    @property
    def built(self) -> str:
        return self.data.get("built", "")

    def info(self) -> dict[str, Any]:
        a = self.data.get("archive", {})
        return {
            "model_version": self.version,
            "built": self.built,
            "schema": self.data.get("schema"),
            "archive": a,
            "dispersers": [d.summary() for d in self.dispersers.values()],
            "readout_seconds_per_group": READOUT_GROUP_S,
            "extrapolated_readouts": sorted(EXTRAPOLATED_READOUTS),
        }

    # ---- lookup
    def get(self, disperser: str) -> Disperser:
        k = disperser.strip().lower().replace("/", "_").replace("-", "_").replace(" ", "_")
        k = _ALIASES.get(k, k)
        if k in self.dispersers:
            return self.dispersers[k]
        matches = [key for key in self.dispersers if key.startswith(k + "_") or key == k]
        if len(matches) == 1:
            return self.dispersers[matches[0]]
        if len(matches) > 1:
            raise ModelError(f"{disperser!r} is ambiguous: choose one of {matches}")
        raise ModelError(f"unknown disperser {disperser!r}; choose one of {list(self.dispersers)}")


def _models_dir():
    return resources.files("campfire_etc").joinpath("models")


def available_versions() -> dict[str, Any]:
    with _models_dir().joinpath("manifest.json").open() as f:
        return json.load(f)


def load_model(version: str | None = None) -> NoiseModel:
    """Load a bundled model by version (default: the manifest's ``latest``),
    or any JSON file if ``version`` is a path."""
    if version and (version.endswith(".json") or "/" in version):
        return NoiseModel.from_file(version)
    manifest = available_versions()
    version = version or manifest["latest"]
    entry = next((v for v in manifest["versions"] if v["version"] == version), None)
    if entry is None:
        raise ModelError(f"no bundled model version {version!r}; available: {[v['version'] for v in manifest['versions']]}")
    with _models_dir().joinpath(entry["file"]).open() as f:
        return NoiseModel(json.load(f), f"bundled:{entry['file']}")


# --------------------------------------------------------------------------- source flux


def resolve_placement(disp: Disperser, placement: str | Sequence[float] | Mapping[str, float]) -> tuple[float, str]:
    """Placement multiplier and a label from 'typical' | 'centred' | 'mean' |
    (x, y) shutter offsets."""
    if isinstance(placement, str):
        p = placement.strip().lower()
        if p in ("typical", "typ", "median"):
            return float(disp.data["mult_median"]), "typical MSA placement (archive median)"
        if p in ("centred", "centered", "cen", "center", "centre"):
            return 1.0, "centred in the shutter"
        if p in ("mean", "random"):
            return float(disp.data["mult_mean"]), "random placement (archive mean)"
        raise ModelError(f"unknown placement {placement!r}; use typical | centred | mean | [x, y]")
    if isinstance(placement, Mapping):
        x, y = placement.get("x", 0.0), placement.get("y", 0.0)
    else:
        x, y = placement
    m = disp.placement_multiplier(x, y)
    return m, f"offset |x|={abs(x):.2f}, |y|={abs(y):.2f} shutter units"


def resolve_morphology(disp: Disperser, morphology: str | None, fwhm_px: float | None,
                       extraction: str, recovery_override: float | None) -> tuple[float, str]:
    """Flux-recovery fraction and a label from the morphology inputs."""
    if recovery_override is not None:
        r = float(np.clip(recovery_override, 0.05, 1.0))
        return r, f"flux recovery set to {r:.2f}"
    m = (morphology or "typical").strip().lower()
    if fwhm_px is not None:
        fw = float(np.clip(fwhm_px, 1.4, 3.5))
        return disp.recovery(fw, extraction), f"spatial FWHM {fw:.1f} px"
    if m not in MORPHOLOGY_FWHM_PX:
        raise ModelError(f"unknown morphology {morphology!r}; use point | compact | typical | extended, or give fwhm_px")
    fw = MORPHOLOGY_FWHM_PX[m]
    labels = {"point": "true point source", "compact": "compact galaxy (FWHM ~1.9 px)",
              "typical": "typical spectroscopic target (FWHM ~2.3 px)", "extended": "extended galaxy (FWHM ~2.8 px)"}
    return disp.recovery(fw, extraction), labels[m]


def source_flux(
    disp: Disperser,
    wave: np.ndarray,
    *,
    magnitude_ab: float | None = None,
    flux_njy: float | None = None,
    sed=None,
    morphology: str | None = "typical",
    fwhm_px: float | None = None,
    extraction: str = "optimal",
    recovery: float | None = None,
) -> tuple[np.ndarray | None, float, str]:
    """Flux landing in the extracted spectrum [uJy] at ``wave``, the recovery
    fraction used, and a description. Returns (None, rec, label) with no source."""
    rec, label = resolve_morphology(disp, morphology, fwhm_px, extraction, recovery)
    n_given = sum(v is not None for v in (magnitude_ab, flux_njy, sed))
    if n_given == 0:
        return None, rec, label
    if n_given > 1:
        raise ModelError("give only one of magnitude_ab, flux_njy or sed")
    if sed is not None:
        total = sed.fnu_at(wave)
    elif magnitude_ab is not None:
        total = np.full_like(wave, 10 ** (-0.4 * (float(magnitude_ab) - 23.9)), dtype=float)
    else:
        total = np.full_like(wave, float(flux_njy) * 1e-3, dtype=float)
    return total * rec, rec, label


# --------------------------------------------------------------------------- the arithmetic


def _neff(n: np.ndarray | float, rho: np.ndarray | float) -> np.ndarray:
    """Effective number of independent pixels when summing n adjacent pixels
    with lag-1 correlation rho (AR(1)-like)."""
    n = np.asarray(n, float)
    return n * (1 + 2 * rho * (n - 1) / n)


def continuum(
    disp: Disperser,
    exp: Exposure,
    wave: np.ndarray | Sequence[float] | None = None,
    flux_spec_ujy: np.ndarray | float | None = None,
    *,
    extraction: str = "optimal",
    placement_mult: float = 1.0,
    margin: float = 1.0,
    bin_mode: str = "resolution",
    bin_value: float | None = None,
    line_recovery: float = 1.0,
) -> dict[str, np.ndarray]:
    """Continuum noise, depth and S/N at each wavelength.

    ``flux_spec_ujy`` is the flux *in the extracted spectrum* (total flux times
    the recovery fraction), scalar or per wavelength; None gives depths only.
    ``line_recovery`` is the fraction of an emission line's total flux that
    reaches the extraction; the reported line limit is a *total* flux, so it
    is divided by it (1.0 = point source).
    Returns arrays on ``wave`` (NaN outside coverage): sig_pix (per 2-D pixel),
    sig_1d (per 1-D pixel, source Poisson included), sig_res/sig_bin, n_res/n_bin,
    ab5_pix/ab5_res/ab5_bin (5-sigma AB limits), line5 (5-sigma unresolved-line
    limit, erg/s/cm2), snr_pix/snr_res/snr_bin.
    """
    if extraction not in ("optimal", "opt", "3px"):
        raise ModelError("extraction must be 'optimal' or '3px'")
    w = np.atleast_1d(np.asarray(disp.default_wavelengths() if wave is None else wave, float))
    c = disp.curves_at(w)
    T, te = exp.total_s, exp.per_exposure_s
    mult = float(placement_mult) * float(margin)
    sig_pix = np.sqrt(c["A"] / T + c["B"] / (T * te ** 2)) * mult
    f = c["fo"] if extraction in ("optimal", "opt") else c["f3"]
    sig_bg = sig_pix * f
    if flux_spec_ujy is None:
        F = None
        sig_1d = sig_bg
    else:
        F = np.broadcast_to(np.asarray(flux_spec_ujy, float), w.shape).astype(float)
        sig_1d = np.sqrt(sig_bg ** 2 + c["g"] * np.clip(F, 0, None) / T)
    rho, n_res, dlds = c["rho1"], c["n_res"], c["dlds"]
    mode = (bin_mode or "resolution").lower()
    if mode in ("pixel", "pix"):
        n_bin = np.ones_like(w)
    elif mode in ("resolution", "res", "element"):
        n_bin = n_res
    elif mode in ("r", "resolving_power"):
        if not bin_value:
            raise ModelError("bin_value (resolving power) is required for bin_mode='R'")
        n_bin = np.maximum(1, (w / float(bin_value)) / dlds)
    elif mode in ("dlambda", "dl", "um"):
        if not bin_value:
            raise ModelError("bin_value (bin width in um) is required for bin_mode='dlambda'")
        n_bin = np.maximum(1, float(bin_value) / dlds)
    else:
        raise ModelError(f"unknown bin_mode {bin_mode!r}; use pixel | resolution | R | dlambda")
    sig_bin = sig_1d * np.sqrt(_neff(n_bin, rho)) / n_bin
    sig_res = sig_1d * np.sqrt(_neff(n_res, rho)) / n_res
    ab5 = lambda s: -2.5 * np.log10(5 * s * 1e-6) + 8.9
    flam = sig_1d / w ** 2 * FNU_UJY_TO_FLAM
    nl = np.maximum(2 * n_res, 2)
    line5 = 5 * flam * (dlds * 1e4) * np.sqrt(_neff(nl, rho)) / LINE_WINDOW_FRACTION / float(line_recovery)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = dict(
            wave=w, sig_pix=sig_pix, sig_bg=sig_bg, sig_1d=sig_1d, sig_res=sig_res, sig_bin=sig_bin,
            n_res=n_res, n_bin=n_bin, ab5_pix=ab5(sig_1d), ab5_res=ab5(sig_res), ab5_bin=ab5(sig_bin),
            line5=line5, rho1=rho, dlds=dlds, R=c["R"],
            flux_spec=F if F is not None else np.full_like(w, np.nan),
            snr_pix=(F / sig_1d) if F is not None else np.full_like(w, np.nan),
            snr_res=(F / sig_res) if F is not None else np.full_like(w, np.nan),
            snr_bin=(F / sig_bin) if F is not None else np.full_like(w, np.nan),
        )
    return out


def line(
    disp: Disperser,
    exp: Exposure,
    wave_um: float,
    flux_cgs: float,
    fwhm_kms: float = 0.0,
    *,
    cont_flux_spec_ujy: float = 0.0,
    line_recovery: float = 1.0,
    extraction: str = "optimal",
    placement_mult: float = 1.0,
    margin: float = 1.0,
) -> dict[str, float]:
    """S/N of an emission line of total flux ``flux_cgs`` [erg/s/cm2] at
    ``wave_um`` with intrinsic FWHM ``fwhm_kms`` (0 = unresolved), integrated
    over a +-1 FWHM window that includes the instrumental resolution. The
    continuum under the line (flux in the spectrum, uJy) adds its photon noise.
    """
    w = float(wave_um)
    if not disp.in_coverage(w)[0]:
        lo, hi = disp.coverage
        raise ModelError(f"{w:.3f} um is outside {disp.name} coverage ({lo:.2f}-{hi:.2f} um)")
    c = continuum(disp, exp, [w], cont_flux_spec_ujy, extraction=extraction,
                  placement_mult=placement_mult, margin=margin, bin_mode="pixel")
    cw = disp.curves_at(w)
    R, dlds, rho, g = float(cw["R"][0]), float(cw["dlds"][0]), float(cw["rho1"][0]), float(cw["g"][0])
    sig1 = float(c["sig_1d"][0])
    if not np.isfinite(sig1):
        raise ModelError(f"model undefined at {w:.3f} um for {disp.name}")
    fw = math.sqrt((w * float(fwhm_kms) / C_KMS) ** 2 + (w / R) ** 2)  # um
    n = max(2 * fw / dlds, 2.0)
    neff = float(_neff(n, rho))
    # Sum of the per-pixel f_nu values across the window, uJy. flux/dlds_A is the
    # window-averaged f_lambda; the window holds 98% of the line.
    S = LINE_WINDOW_FRACTION * float(line_recovery) * (float(flux_cgs) / (dlds * 1e4)) * w * w / FNU_UJY_TO_FLAM
    sigS = math.sqrt(neff * sig1 ** 2 + g * S / exp.total_s)
    snr = S / sigS if sigS > 0 else float("nan")
    # 5-sigma limit on the line's *total* flux: the window sees only
    # LINE_WINDOW_FRACTION * line_recovery of it.
    lim5 = 5 * sig1 * math.sqrt(neff) * (dlds * 1e4) * FNU_UJY_TO_FLAM / (w * w) / LINE_WINDOW_FRACTION / float(line_recovery)
    return dict(wave_um=w, flux_cgs=float(flux_cgs), fwhm_kms=float(fwhm_kms), fwhm_um=fw,
                fwhm_A=fw * 1e4, window_px=n, n_eff=neff, resolving_power=R, snr=snr,
                limit_5sigma_cgs=lim5, sig_1d_ujy=sig1)


def time_for_snr(exp: Exposure, snr_now: float, snr_target: float) -> float:
    """Total time (s) that reaches ``snr_target`` at the same per-exposure time.
    Every variance term in the model scales as 1/T at fixed t_exp, so S/N grows
    exactly as sqrt(T)."""
    if not np.isfinite(snr_now) or snr_now <= 0:
        return float("nan")
    return exp.total_s * (float(snr_target) / float(snr_now)) ** 2


def pandeia_comparison(disp: Disperser) -> dict[str, Any] | None:
    """Ratio of the pandeia (official ETC) noise to this model's, if the model
    JSON carries the comparison runs."""
    p = disp.data.get("pandeia")
    if not p or not p.get("runs"):
        return None
    runs = []
    for r in p["runs"]:
        runs.append({k: r[k] for k in ("label", "total_s", "per_exposure_s", "background", "ratio_median") if k in r}
                    | {"ratio_by_band": r.get("ratio_by_band")})
    return {"pandeia_version": p.get("version"), "runs": runs,
            "note": "ratio = pandeia full-shutter 1-sigma noise / empirical optimal-extraction noise for a centred point source"}
