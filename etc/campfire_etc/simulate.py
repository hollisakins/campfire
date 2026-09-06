"""Mock NIRSpec/MSA spectra: an SED plus optional emission lines, observed with
a given disperser and exposure, on the native pixel grid, with noise drawn from
the empirical model (including the measured adjacent-pixel correlation).
numpy only.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np

from .model import (
    C_KMS, FNU_UJY_TO_FLAM, Disperser, Exposure, ModelError, continuum, line as line_snr,
    resolve_morphology, resolve_placement,
)
from .sed import SED


def _erf(x: np.ndarray) -> np.ndarray:
    """Abramowitz & Stegun 7.1.26 (|error| < 1.5e-7), vectorised."""
    x = np.asarray(x, float)
    s = np.sign(x); a = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-a * a)
    return s * y


def _lsf_convolve(values: np.ndarray, sigma_px: np.ndarray, chunk: int = 256) -> np.ndarray:
    """Gaussian smoothing with a slowly varying width (in samples).

    The width changes by a few per cent across a disperser, so the array is
    processed in chunks with the chunk's median width and a fixed kernel
    (np.convolve), padded so chunk edges see their neighbours. NaNs are treated
    as missing and renormalised away.
    """
    n = values.size
    out = np.full(n, np.nan)
    finite = np.isfinite(values)
    v = np.where(finite, values, 0.0)
    wgt = finite.astype(float)
    for s0 in range(0, n, chunk):
        s1 = min(n, s0 + chunk)
        s = float(np.nanmedian(sigma_px[s0:s1]))
        if not np.isfinite(s) or s <= 0:
            out[s0:s1] = values[s0:s1]; continue
        half = int(math.ceil(4 * s))
        lo, hi = max(0, s0 - half), min(n, s1 + half)
        k = np.exp(-0.5 * (np.arange(-half, half + 1) / s) ** 2)
        # "full" then slice: unlike mode="same", the result is aligned with the
        # segment even when the segment is shorter than the kernel.
        num = np.convolve(v[lo:hi], k, mode="full")[half:half + (hi - lo)]
        den = np.convolve(wgt[lo:hi], k, mode="full")[half:half + (hi - lo)]
        with np.errstate(invalid="ignore", divide="ignore"):
            sm = np.where(den > 0, num / den, np.nan)
        out[s0:s1] = sm[s0 - lo:s1 - lo]
    return out


def _ma1_coefficient(rho: float) -> float:
    """MA(1) mixing coefficient giving lag-1 correlation rho for
    y_i = (e_i + a e_{i-1} + a e_{i+1}) / sqrt(1 + 2 a^2)."""
    rho = float(np.clip(rho, 0.0, 0.7))
    if rho < 1e-6:
        return 0.0
    return (1 - math.sqrt(1 - 2 * rho * rho)) / (2 * rho)


def correlated_noise(sigma: np.ndarray, rho: np.ndarray, rng: np.random.Generator, n_real: int) -> np.ndarray:
    """n_real x n_pix Gaussian noise with per-pixel sigma and neighbour
    correlation rho (a 3-tap moving average of white noise)."""
    n = sigma.size
    e = rng.standard_normal((n_real, n + 2))
    a = np.array([_ma1_coefficient(r) for r in rho])
    y = (e[:, 1:-1] + a * (e[:, :-2] + e[:, 2:])) / np.sqrt(1 + 2 * a * a)
    return y * sigma


def simulate(
    disp: Disperser,
    exp: Exposure,
    sed: SED | None = None,
    lines: Sequence[Mapping[str, float]] | None = None,
    *,
    morphology: str | None = "typical",
    fwhm_px: float | None = None,
    recovery: float | None = None,
    line_recovery: float | None = None,
    extraction: str = "optimal",
    placement: str | Sequence[float] = "typical",
    margin: float = 1.0,
    wave_range: Sequence[float] | None = None,
    n_realizations: int = 1,
    seed: int | None = None,
    oversample: int = 5,
) -> dict[str, Any]:
    """Simulate the extracted 1-D spectrum.

    ``lines`` items: {"wave_um": observed wavelength, "flux_cgs": total flux in
    erg/s/cm2, "fwhm_kms": intrinsic FWHM (0 = unresolved)}. Lines are added
    analytically (a Gaussian in wavelength, instrumental resolution included,
    integrated over each pixel), so they need not be resolved by the SED grid.

    Returns pixel arrays (wave_um, model_ujy, err_ujy, flux_ujy per realization,
    snr_model) plus per-line analytic S/N and a summary.
    """
    if n_realizations < 1:
        raise ModelError("n_realizations must be >= 1")
    if sed is None and not lines:
        raise ModelError("give an SED (or a magnitude) and/or emission lines to simulate")
    rec, rec_label = resolve_morphology(disp, morphology, fwhm_px, extraction, recovery)
    line_rec = rec if line_recovery is None else float(np.clip(line_recovery, 0.05, 1.0))
    mult, place_label = resolve_placement(disp, placement)

    wave, dl = disp.pixel_grid()
    if wave_range is not None:
        lo, hi = float(wave_range[0]), float(wave_range[1])
        sel = (wave >= lo) & (wave <= hi)
        if sel.sum() < 2:
            raise ModelError(f"wave_range {lo:.3f}-{hi:.3f} um selects fewer than two pixels of {disp.name}")
        wave, dl = wave[sel], dl[sel]
    cur = disp.curves_at(wave)
    notes: list[str] = []

    # --- continuum: SED sampled on an oversampled grid, LSF-smoothed, pixel-averaged
    n = wave.size
    cont = np.zeros(n)
    if sed is not None:
        note = sed.coverage_note(float(wave[0]), float(wave[-1]))
        if note:
            notes.append(note)
        os_ = max(1, int(oversample))
        sub = (wave[:, None] + dl[:, None] * ((np.arange(os_) + 0.5) / os_ - 0.5)).ravel()
        f_sub = sed.fnu_at(sub)
        f_sub = np.where(np.isfinite(f_sub), f_sub, 0.0)
        n_res_sub = np.repeat(np.where(np.isfinite(cur["n_res"]), cur["n_res"], 2.2), os_)
        sigma_sub = n_res_sub * os_ / 2.354820045
        f_sm = _lsf_convolve(f_sub, sigma_sub)
        cont = np.nanmean(f_sm.reshape(n, os_), axis=1)
        cont = np.where(np.isfinite(cont), cont, 0.0)
    model = cont * rec

    # --- emission lines: analytic Gaussians integrated over pixel edges
    edges_lo = wave - 0.5 * dl; edges_hi = wave + 0.5 * dl
    line_results = []
    for ln in lines or []:
        try:
            w0 = float(ln["wave_um"]); F = float(ln["flux_cgs"])
        except (KeyError, TypeError, ValueError) as e:
            raise ModelError("each line needs wave_um and flux_cgs (erg/s/cm2)") from e
        v = float(ln.get("fwhm_kms", 0.0) or 0.0)
        if not disp.in_coverage(w0)[0] or w0 < wave[0] or w0 > wave[-1]:
            line_results.append({"wave_um": w0, "flux_cgs": F, "fwhm_kms": v, "status": "outside the simulated range"})
            continue
        R0 = float(disp.curves_at(w0)["R"][0])
        sig_um = math.sqrt((w0 * v / C_KMS) ** 2 + (w0 / R0) ** 2) / 2.354820045
        frac = 0.5 * (_erf((edges_hi - w0) / (math.sqrt(2) * sig_um)) - _erf((edges_lo - w0) / (math.sqrt(2) * sig_um)))
        flam_pix = F * frac / (dl * 1e4)                       # erg/s/cm2/A averaged over the pixel
        model = model + line_rec * flam_pix * wave ** 2 / FNU_UJY_TO_FLAM
        cont_here = float(np.interp(w0, wave, cont)) * rec
        r = line_snr(disp, exp, w0, F, v, cont_flux_spec_ujy=cont_here, line_recovery=line_rec,
                     extraction=extraction, placement_mult=mult, margin=margin)
        r["status"] = "ok"
        line_results.append(r)

    # --- noise from the model, correlated realisations
    c = continuum(disp, exp, wave, model, extraction=extraction, placement_mult=mult, margin=margin, bin_mode="pixel")
    err = c["sig_1d"]
    good = np.isfinite(err)
    wave, dl, model, err = wave[good], dl[good], model[good], err[good]
    rho = c["rho1"][good]
    if wave.size < 2:
        raise ModelError("the model is undefined over the requested wavelength range")
    rng = np.random.default_rng(seed)
    noise = correlated_noise(err, rho, rng, int(n_realizations))
    flux = model + noise
    with np.errstate(invalid="ignore", divide="ignore"):
        snr = np.where(err > 0, model / err, np.nan)

    if exp.readout in ("nrs", "nrsrapid"):
        notes.append("NRS/NRSRAPID readouts are extrapolated from the IRS2 read-noise term")
    if exp.per_exposure_s > 1.2 * disp.data["sample"]["texp_range"][1] or exp.per_exposure_s < 0.8 * disp.data["sample"]["texp_range"][0]:
        notes.append(f"per-exposure time {exp.per_exposure_s:.0f} s is outside the archive range for this disperser "
                     f"({disp.data['sample']['texp_range'][0]:.0f}-{disp.data['sample']['texp_range'][1]:.0f} s)")

    finite_snr = snr[np.isfinite(snr)]
    return {
        "disperser": disp.key, "disperser_name": disp.name, "exposure": exp.to_dict(),
        "source": {"sed": sed.description if sed else None, "morphology": rec_label, "flux_recovery": round(rec, 3),
                   "line_recovery": round(line_rec, 3), "extraction": extraction,
                   "placement": place_label, "placement_multiplier": round(mult, 3), "margin": margin},
        "n_pixels": int(wave.size), "n_realizations": int(n_realizations), "seed": seed,
        "wave_um": wave, "dlambda_um": dl, "model_ujy": model, "err_ujy": err, "flux_ujy": flux, "snr_model": snr,
        "lines": line_results,
        "summary": {
            "wave_range_um": [float(wave[0]), float(wave[-1])],
            "median_snr_per_pixel": float(np.median(finite_snr)) if finite_snr.size else None,
            "median_err_njy_per_pixel": float(np.median(err) * 1e3),
            "median_model_njy": float(np.median(model) * 1e3),
        },
        "notes": notes,
    }


def write_spectrum(result: Mapping[str, Any], path: str, flux_unit: str = "uJy") -> str:
    """Write a simulation to disk. Format by extension: .txt/.dat/.ecsv (ASCII
    columns), .npz, .json, .fits (needs astropy). Returns the path written."""
    from pathlib import Path
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    scale = {"ujy": 1.0, "njy": 1e3, "mjy": 1e-3, "jy": 1e-6}.get(flux_unit.lower().replace("µ", "u"), None)
    if scale is None:
        raise ModelError("flux_unit for files must be uJy | nJy | mJy | Jy")
    unit = flux_unit
    wave = np.asarray(result["wave_um"]); model = np.asarray(result["model_ujy"]) * scale
    err = np.asarray(result["err_ujy"]) * scale; flux = np.asarray(result["flux_ujy"]) * scale
    nreal = flux.shape[0]
    header = (f"campfire-etc mock spectrum: {result['disperser_name']}, {result['exposure']['description']}; "
              f"{result['source']['morphology']}, recovery {result['source']['flux_recovery']}, "
              f"{result['source']['placement']} x{result['source']['placement_multiplier']}; flux unit {unit}")
    suf = p.suffix.lower()
    if suf == ".npz":
        np.savez_compressed(p, wave_um=wave, model=model, err=err, flux=flux, unit=unit, header=header)
    elif suf == ".json":
        import json
        with open(p, "w") as f:
            json.dump({"header": header, "unit": unit, "wave_um": wave.tolist(), "model": model.tolist(),
                       "err": err.tolist(), "flux": flux.tolist()}, f)
    elif suf in (".fits", ".fit"):
        try:
            from astropy.io import fits  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise ModelError("writing FITS needs astropy (pip install 'campfire-etc[fits]')") from e
        cols = [fits.Column(name="wave", format="D", unit="um", array=wave),
                fits.Column(name="model", format="D", unit=unit, array=model),
                fits.Column(name="err", format="D", unit=unit, array=err)]
        for i in range(nreal):
            cols.append(fits.Column(name="flux" if nreal == 1 else f"flux_{i + 1}", format="D", unit=unit, array=flux[i]))
        hdu = fits.BinTableHDU.from_columns(cols, name="SPEC1D")
        hdu.header["COMMENT"] = header
        hdu.header["DISPRSR"] = result["disperser"]
        hdu.header["TOTEXP"] = (result["exposure"]["total_s"], "total on-source time [s]")
        hdu.header["PEREXP"] = (result["exposure"]["per_exposure_s"], "per-exposure time [s]")
        fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(p, overwrite=True)
    else:
        cols = [wave, model, err] + [flux[i] for i in range(nreal)]
        names = ["wave_um", f"model_{unit}", f"err_{unit}"] + ([f"flux_{unit}"] if nreal == 1 else [f"flux{i + 1}_{unit}" for i in range(nreal)])
        if suf == ".ecsv":
            head = "# %ECSV 1.0\n# ---\n# datatype:\n" + "".join(f"# - {{name: {n}, datatype: float64}}\n" for n in names) + f"# meta: {{comment: '{header}'}}\n" + " ".join(names) + "\n"
        else:
            head = f"# {header}\n# columns: " + " ".join(names) + "\n"
        np.savetxt(p, np.column_stack(cols), header=head, comments="", fmt="%.6g")
    return str(p)
