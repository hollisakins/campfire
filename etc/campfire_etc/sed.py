"""Source spectral energy distributions for the calculator and simulator.

Everything is converted to observed-frame f_nu in microjansky on a wavelength
grid in microns. numpy only; FITS input needs astropy (the `fits` extra).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .model import BAND_PIVOT_UM, FNU_UJY_TO_FLAM, ModelError

WAVE_UNITS = {"um": 1.0, "micron": 1.0, "microns": 1.0, "angstrom": 1e-4, "a": 1e-4, "aa": 1e-4,
              "nm": 1e-3, "m": 1e6, "mm": 1e3}
FLUX_UNITS = ("ujy", "njy", "mjy", "jy", "abmag", "flam", "fnu_cgs")


@dataclass
class SED:
    """Observed-frame f_nu [uJy] sampled at wave_um [um]."""

    wave_um: np.ndarray
    fnu_ujy: np.ndarray
    description: str = ""

    def __post_init__(self):
        w = np.asarray(self.wave_um, float); f = np.asarray(self.fnu_ujy, float)
        ok = np.isfinite(w) & np.isfinite(f)
        if ok.sum() < 2:
            raise ModelError("an SED needs at least two finite (wavelength, flux) samples")
        order = np.argsort(w[ok])
        self.wave_um = w[ok][order]
        self.fnu_ujy = f[ok][order]

    @property
    def range_um(self) -> tuple[float, float]:
        return float(self.wave_um[0]), float(self.wave_um[-1])

    def fnu_at(self, wave_um: np.ndarray | float) -> np.ndarray:
        """Linear interpolation; NaN outside the sampled range."""
        w = np.atleast_1d(np.asarray(wave_um, float))
        return np.interp(w, self.wave_um, self.fnu_ujy, left=np.nan, right=np.nan)

    def normalised(self, magnitude_ab: float, wave_um: float) -> "SED":
        f0 = float(self.fnu_at(wave_um)[0])
        if not np.isfinite(f0) or f0 <= 0:
            raise ModelError(f"cannot normalise: the SED has no positive flux at {wave_um:.3f} um")
        target = 10 ** (-0.4 * (float(magnitude_ab) - 23.9))
        return SED(self.wave_um, self.fnu_ujy * target / f0,
                   self.description + f", normalised to AB {magnitude_ab:.2f} at {wave_um:.2f} um")

    def coverage_note(self, lo: float, hi: float) -> str | None:
        a, b = self.range_um
        if a > lo or b < hi:
            return (f"the SED covers {a:.2f}-{b:.2f} um but the disperser spans {lo:.2f}-{hi:.2f} um; "
                    "pixels outside the SED are treated as having no source flux")
        return None


def to_ujy(flux: np.ndarray, unit: str, wave_um: np.ndarray) -> np.ndarray:
    u = unit.strip().lower().replace("μ", "u").replace("µ", "u")
    if u in ("ujy", "microjy", "microjansky"):
        return flux
    if u == "njy":
        return flux * 1e-3
    if u == "mjy":
        return flux * 1e3
    if u == "jy":
        return flux * 1e6
    if u in ("abmag", "ab", "mag"):
        return 10 ** (-0.4 * (flux - 23.9))
    if u in ("flam", "erg/s/cm2/a", "erg/s/cm^2/a", "cgs_flam"):
        return flux * wave_um ** 2 / FNU_UJY_TO_FLAM
    if u in ("fnu_cgs", "erg/s/cm2/hz", "cgs_fnu"):
        return flux * 1e29
    raise ModelError(f"unknown flux unit {unit!r}; use one of {FLUX_UNITS}")


def to_um(wave: np.ndarray, unit: str) -> np.ndarray:
    u = unit.strip().lower().replace("μ", "u").replace("µ", "u")
    if u not in WAVE_UNITS:
        raise ModelError(f"unknown wavelength unit {unit!r}; use um | angstrom | nm")
    return wave * WAVE_UNITS[u]


def flat_sed(magnitude_ab: float | None = None, flux_njy: float | None = None) -> SED:
    if magnitude_ab is not None:
        f = 10 ** (-0.4 * (float(magnitude_ab) - 23.9)); d = f"flat f_nu, AB {magnitude_ab:.2f}"
    elif flux_njy is not None:
        f = float(flux_njy) * 1e-3; d = f"flat f_nu, {flux_njy:g} nJy"
    else:
        raise ModelError("flat_sed needs magnitude_ab or flux_njy")
    return SED(np.array([0.3, 30.0]), np.array([f, f]), d)


def power_law_sed(magnitude_ab: float, wave_um: float, beta: float) -> SED:
    """f_lambda ~ lambda^beta (so f_nu ~ lambda^(beta+2)), AB magnitude at wave_um."""
    w = np.geomspace(0.3, 30.0, 400)
    f = 10 ** (-0.4 * (float(magnitude_ab) - 23.9)) * (w / float(wave_um)) ** (float(beta) + 2)
    return SED(w, f, f"power law beta={beta:g}, AB {magnitude_ab:.2f} at {wave_um:.2f} um")


def _load_table(path: Path) -> tuple[np.ndarray, np.ndarray, str]:
    suf = path.suffix.lower()
    if suf in (".fits", ".fit", ".fz"):
        try:
            from astropy.io import fits  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise ModelError("reading FITS SEDs needs astropy (pip install 'campfire-etc[fits]')") from e
        with fits.open(path) as hd:
            tab = next((h for h in hd if getattr(h, "columns", None) is not None), None)
            if tab is None:
                raise ModelError(f"{path}: no table extension")
            names = [n.lower() for n in tab.columns.names]
            wcol = next((n for n in names if n in ("wave", "wavelength", "lambda", "lam", "wl")), names[0])
            fcol = next((n for n in names if n in ("flux", "fnu", "flam", "f_nu", "f_lambda", "spec")), names[1])
            return np.asarray(tab.data[wcol], float), np.asarray(tab.data[fcol], float), f"{path.name}[{wcol},{fcol}]"
    if suf == ".npz":
        d = np.load(path)
        keys = list(d.keys())
        return np.asarray(d[keys[0]], float), np.asarray(d[keys[1]], float), f"{path.name}[{keys[0]},{keys[1]}]"
    if suf == ".json":
        with open(path) as f:
            d = json.load(f)
        return np.asarray(d["wave"], float), np.asarray(d["flux"], float), path.name
    arr = np.genfromtxt(path, comments="#", dtype=float, usecols=(0, 1), invalid_raise=False)
    if arr.ndim != 2 or arr.shape[0] < 2:
        raise ModelError(f"{path}: expected at least two rows of 'wavelength flux'")
    return arr[:, 0], arr[:, 1], path.name


def parse_sed(spec: Mapping[str, Any] | None, *, allow_files: bool = False) -> SED | None:
    """Build an SED from a JSON-style description.

    Keys: ``wave`` + ``flux`` arrays (or ``file`` when ``allow_files``),
    ``wave_unit`` (um | angstrom | nm; default um), ``flux_unit`` (uJy | nJy |
    mJy | Jy | abmag | flam | fnu_cgs; default uJy), optional ``redshift`` to
    shift the wavelengths by (1+z) (fluxes are left as given), and optional
    ``normalize`` = {"magnitude_ab": m, "wave_um": l} or {"magnitude_ab": m,
    "band": "f277w"} to set the absolute level afterwards.
    """
    if spec is None:
        return None
    if isinstance(spec, SED):
        return spec
    if not isinstance(spec, Mapping):
        raise ModelError("sed must be an object with wave/flux arrays (or a file path)")
    desc = ""
    if "file" in spec:
        if not allow_files:
            raise ModelError("sed.file is only available when the server runs locally (stdio); pass wave/flux arrays instead")
        path = Path(str(spec["file"])).expanduser()
        if not path.exists():
            raise ModelError(f"SED file not found: {path}")
        wave, flux, desc = _load_table(path)
    else:
        if "wave" not in spec or "flux" not in spec:
            raise ModelError("sed needs 'wave' and 'flux' arrays")
        wave = np.asarray(spec["wave"], float); flux = np.asarray(spec["flux"], float)
        desc = f"user SED ({wave.size} samples)"
    if wave.shape != flux.shape:
        raise ModelError("sed wave and flux must have the same length")
    wave_um = to_um(wave, str(spec.get("wave_unit", "um")))
    z = spec.get("redshift")
    if z is not None:
        wave_um = wave_um * (1 + float(z)); desc += f", redshifted to z={float(z):g}"
    fnu = to_ujy(flux, str(spec.get("flux_unit", "uJy")), wave_um)
    sed = SED(wave_um, fnu, desc)
    norm = spec.get("normalize") or spec.get("normalise")
    if norm:
        m = norm.get("magnitude_ab")
        if m is None:
            raise ModelError("normalize needs magnitude_ab")
        w0 = norm.get("wave_um")
        if w0 is None and norm.get("band"):
            b = str(norm["band"]).lower()
            if b not in BAND_PIVOT_UM:
                raise ModelError(f"unknown band {norm['band']!r}; known: {sorted(BAND_PIVOT_UM)}")
            w0 = BAND_PIVOT_UM[b]
        if w0 is None:
            raise ModelError("normalize needs wave_um or band")
        sed = sed.normalised(float(m), float(w0))
    return sed
