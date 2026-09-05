"""MCP server exposing the empirical NIRSpec ETC.

One server object serves both transports: stdio for a local install (mock
spectra can then be written straight to disk) and streamable HTTP for the
hosted instance (large results are offered as short-lived downloads instead of
being inlined into the conversation). Needs the `mcp` extra.
"""

from __future__ import annotations

import io
import os
import secrets
import time
from typing import Any, Sequence

import numpy as np

from . import __version__
from .model import (
    EXTRAPOLATED_READOUTS, Exposure, ModelError, continuum, fmt_sci, fmt_time, line as line_calc,
    load_model, make_exposure, pandeia_comparison, resolve_placement, source_flux, time_for_snr,
)
from .sed import flat_sed, parse_sed, power_law_sed
from .simulate import simulate as simulate_calc, write_spectrum

try:
    from mcp.server.mcpserver import Context, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError as e:  # pragma: no cover
    raise ImportError("the MCP server needs the 'mcp' extra: pip install 'campfire-etc[mcp]'") from e

import functools


def _safe(fn):
    """Turn calculator input errors into ToolErrors so the client sees the
    message instead of a generic 'error executing tool'."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ModelError, ValueError) as e:
            raise ToolError(str(e)) from e
    return wrapper

MODEL = load_model(os.environ.get("CAMPFIRE_ETC_MODEL") or None)
# Set by the CLI: True when running over stdio on the user's machine, which
# enables sed.file inputs and output_path for simulations.
LOCAL_FILES = False
PUBLIC_URL = os.environ.get("CAMPFIRE_ETC_PUBLIC_URL", "").rstrip("/")
DOWNLOAD_TTL_S = 3600
MAX_INLINE_VALUES_DEFAULT = 6000
REPORT_URL = "https://claude.ai/code/artifact/15c73c7d-1281-43a6-bbc1-2b20825648c8"

INSTRUCTIONS = f"""Empirical JWST NIRSpec/MSA exposure-time calculator (model {MODEL.version}), fitted to
{MODEL.data['archive']['n_spectra']:,} archival CAMPFIRE spectra instead of a simulated instrument. It answers "what
S/N or depth will this observation deliver" and "what would the extracted spectrum look like".

Units: wavelengths in microns (observed frame), fluxes in nJy or AB magnitudes, line fluxes in
erg/s/cm2, times in seconds. Exposure setups are readout pattern + groups + integrations + exposures
(t_exp = groups x seconds-per-group; T = t_exp x nint x nexp); or give total_s + per_exposure_s.
Every tool accepts the disperser as e.g. "prism", "g395m", "G140M/F100LP".

Defaults are the archive medians for a real galaxy: morphology "typical" (only ~45% of the total flux
lands in the extracted spectrum), placement "typical" (median shutter-position noise penalty), and a
+10% noise margin (margin=1.1) for field-to-field scatter. Use morphology "point" and placement
"centred" for a best case, or when comparing with the official ETC (pandeia), which assumes both.

Workflow: list_dispersers -> continuum_snr / line_snr / depth for numbers (each returns a ready
proposal sentence and the time needed for a target S/N) -> simulate_spectrum for mock data.
Quote results as "empirical estimate from the CAMPFIRE archive" and mention the assumed morphology
and placement. Read model_info for caveats before relying on G140H or the extended-wavelength edges."""

server = MCPServer(
    name="campfire-etc",
    title="CAMPFIRE NIRSpec ETC",
    description="Empirical NIRSpec/MSA exposure-time calculator and spectrum simulator from the CAMPFIRE archive",
    instructions=INSTRUCTIONS,
    website_url=REPORT_URL,
    version=__version__,
)


# --------------------------------------------------------------------------- helpers

def _sig(x: Any, n: int = 4) -> Any:
    """Round to n significant digits; NaN/inf -> None. Works on scalars and arrays."""
    if isinstance(x, np.ndarray):
        return [_sig(float(v), n) for v in x]
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return x
    if not np.isfinite(xf):
        return None
    if xf == 0:
        return 0.0
    return float(f"{xf:.{n}g}")


def _exposure(readout, ngroups, nint, nexp, total_s, per_exposure_s) -> Exposure:
    try:
        return make_exposure(readout, ngroups, nint or 1, nexp or 1, total_s, per_exposure_s)
    except ModelError as e:
        raise ValueError(str(e)) from e


def _disp(name: str):
    try:
        return MODEL.get(name)
    except ModelError as e:
        raise ValueError(str(e)) from e


def _notes(disp, exp: Exposure) -> list[str]:
    notes = list(disp.caveats())
    if exp.readout in EXTRAPOLATED_READOUTS:
        notes.append(f"{exp.readout.upper()} is not in the archive; its read noise is extrapolated from the IRS2 term")
    lo, hi = disp.data["sample"]["texp_range"]
    if exp.per_exposure_s < 0.8 * lo or exp.per_exposure_s > 1.2 * hi:
        notes.append(f"per-exposure time {exp.per_exposure_s:.0f} s is outside the archive range for this disperser ({lo:.0f}-{hi:.0f} s)")
    Tlo, Thi = disp.data["sample"]["T_range"]
    if exp.total_s > 1.5 * Thi:
        notes.append(f"total time {fmt_time(exp.total_s)} extrapolates beyond the archive maximum ({fmt_time(Thi)}); no noise floor is assumed")
    return notes


def _wavelengths(disp, wavelengths) -> np.ndarray:
    if wavelengths is None:
        return disp.default_wavelengths()
    w = np.atleast_1d(np.asarray(wavelengths, float))
    if w.size == 0 or w.size > 200:
        raise ValueError("give between 1 and 200 wavelengths")
    return w


def _source(disp, wave, *, magnitude_ab, flux_njy, sed, beta, beta_wave_um, morphology, fwhm_px, recovery, extraction):
    sed_obj = None
    if sed is not None:
        try:
            sed_obj = parse_sed(sed, allow_files=LOCAL_FILES)
        except ModelError as e:
            raise ValueError(str(e)) from e
    if beta is not None:
        if magnitude_ab is None:
            raise ValueError("beta needs magnitude_ab (the AB magnitude at beta_wave_um)")
        w0 = beta_wave_um or float(np.mean(disp.coverage))
        sed_obj = power_law_sed(magnitude_ab, w0, beta); magnitude_ab = None
    try:
        return source_flux(disp, wave, magnitude_ab=magnitude_ab, flux_njy=flux_njy, sed=sed_obj,
                           morphology=morphology, fwhm_px=fwhm_px, extraction=extraction, recovery=recovery) + (sed_obj,)
    except ModelError as e:
        raise ValueError(str(e)) from e


def _rows(c: dict[str, np.ndarray], with_source: bool) -> list[dict[str, Any]]:
    rows = []
    for i, w in enumerate(c["wave"]):
        if not np.isfinite(c["sig_1d"][i]):
            rows.append({"wave_um": _sig(w), "in_coverage": False}); continue
        r = {
            "wave_um": _sig(w),
            "sigma_2d_pixel_njy": _sig(c["sig_pix"][i] * 1e3),
            "sigma_1d_pixel_njy": _sig(c["sig_1d"][i] * 1e3),
            "sigma_res_element_njy": _sig(c["sig_res"][i] * 1e3),
            "pixels_per_res_element": _sig(c["n_res"][i], 3),
            "ab_5sigma_per_pixel": _sig(c["ab5_pix"][i]),
            "ab_5sigma_per_res_element": _sig(c["ab5_res"][i]),
            "line_5sigma_cgs": _sig(c["line5"][i], 3),
        }
        if with_source:
            r.update({
                "flux_in_spectrum_njy": _sig(c["flux_spec"][i] * 1e3),
                "snr_per_pixel": _sig(c["snr_pix"][i]),
                "snr_per_res_element": _sig(c["snr_res"][i]),
                "snr_per_bin": _sig(c["snr_bin"][i]),
                "pixels_per_bin": _sig(c["n_bin"][i], 3),
            })
        rows.append(r)
    return rows


def _nearest(c: dict[str, np.ndarray], wave_um: float | None) -> int:
    ok = np.isfinite(c["sig_1d"])
    if not ok.any():
        raise ValueError("none of the requested wavelengths lies inside the disperser's coverage")
    idx = np.where(ok)[0]
    if wave_um is None:
        return int(idx[len(idx) // 2])
    return int(idx[np.argmin(np.abs(c["wave"][idx] - wave_um))])


# --------------------------------------------------------------------------- downloads (HTTP mode)

_FILES: dict[str, tuple[bytes, str, str, float]] = {}
# Downloads live in this process's memory, bounded in total size: once the
# budget is exceeded the oldest entries go first, TTL or not (the server is
# anonymous, so a burst of requests must not be able to exhaust memory).
FILE_STORE_MAX_BYTES = int(os.environ.get("CAMPFIRE_ETC_FILE_STORE_MB", "48")) * 1024 * 1024
FILE_MAX_BYTES = 8 * 1024 * 1024
# Behind a load balancer that runs several instances (Fly), the file id
# carries the instance that made it so another instance can ask the proxy to
# replay the request there.
INSTANCE_ID = os.environ.get("FLY_MACHINE_ID", "")


def _store_file(data: bytes, content_type: str, filename: str) -> str:
    if len(data) > FILE_MAX_BYTES:
        raise ValueError(f"result is {len(data) / 1e6:.1f} MB, above the {FILE_MAX_BYTES / 1e6:.0f} MB download limit; "
                         "narrow wave_range or reduce n_realizations")
    now = time.time()
    for k, (_, _, _, exp) in list(_FILES.items()):
        if exp < now:
            _FILES.pop(k, None)
    total = sum(len(v[0]) for v in _FILES.values()) + len(data)
    for k in list(_FILES):                      # insertion order = age
        if total <= FILE_STORE_MAX_BYTES:
            break
        total -= len(_FILES.pop(k)[0])
    fid = (INSTANCE_ID + "." if INSTANCE_ID else "") + secrets.token_urlsafe(12)
    _FILES[fid] = (data, content_type, filename, now + DOWNLOAD_TTL_S)
    return fid


ALLOWED_HOSTS = [h.strip().lower() for h in os.environ.get("CAMPFIRE_ETC_ALLOWED_HOSTS", "").split(",") if h.strip()]


def _public_base(ctx: Context | None) -> str:
    """Base URL for download links: the hostname the client actually reached us
    on (so both the custom domain and the fly.dev name work), but only if it is
    one of the configured allowed hosts — X-Forwarded-Host is client-settable,
    so an unlisted value must not end up in a link. Falls back to
    CAMPFIRE_ETC_PUBLIC_URL; with no allow-list only the Host header counts."""
    try:
        h = ctx.headers if ctx is not None else {}
        proto = (h.get("x-forwarded-proto") or "https").split(",")[0].strip().lower()
        if proto not in ("http", "https"):
            proto = "https"
        candidates = [h.get("x-forwarded-host"), h.get("host")] if ALLOWED_HOSTS else [h.get("host")]
        for host in candidates:
            host = (host or "").split(",")[0].strip().lower()
            if not host:
                continue
            if ALLOWED_HOSTS and host not in ALLOWED_HOSTS and host.split(":")[0] not in ALLOWED_HOSTS:
                continue
            return f"{proto}://{host}"
    except Exception:  # pragma: no cover
        pass
    return PUBLIC_URL


@server.custom_route("/files/{fid}", methods=["GET"])
async def _serve_file(request):
    from starlette.responses import PlainTextResponse, Response
    fid = request.path_params["fid"]
    item = _FILES.get(fid)
    if item is None or item[3] < time.time():
        owner = fid.split(".", 1)[0] if "." in fid else ""
        if owner and INSTANCE_ID and owner != INSTANCE_ID and "fly-replay-src" not in request.headers:
            # Made by a sibling instance: have Fly's proxy replay the request there.
            return PlainTextResponse("", status_code=307, headers={"fly-replay": f"instance={owner}"})
        return PlainTextResponse("not found or expired (downloads live for one hour; re-run the simulation)", status_code=404)
    data, ctype, fname, _ = item
    return Response(content=data, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@server.custom_route("/health", methods=["GET"])
async def _health(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"ok": True, "server": "campfire-etc", "version": __version__, "model_version": MODEL.version})


@server.custom_route("/", methods=["GET"])
@server.custom_route("/index.html", methods=["GET"])   # a zone-wide Cloudflare rule rewrites "/" to this
async def _index(request):
    from starlette.responses import PlainTextResponse
    return PlainTextResponse(
        "campfire-etc: empirical NIRSpec/MSA exposure-time calculator (MCP server)\n\n"
        f"server {__version__}, noise model {MODEL.version} (built {MODEL.built})\n"
        f"MCP endpoint: {PUBLIC_URL or ''}/mcp (streamable HTTP)\n\n"
        "Claude Code:  claude mcp add --transport http campfire-etc "
        f"{PUBLIC_URL or 'https://<this host>'}/mcp\n"
        "Source:       https://github.com/hollisakins/campfire (etc/)\n"
        f"Report:       {REPORT_URL}\n"
    )


# --------------------------------------------------------------------------- tools


@server.tool()
@_safe
def list_dispersers() -> dict[str, Any]:
    """List the NIRSpec disperser/filter combinations the model covers, with
    wavelength coverage, resolving power, how much archive data constrains each,
    and per-disperser caveats. Call this first if unsure which disperser to use."""
    return {"model_version": MODEL.version, "built": MODEL.built,
            "dispersers": [d.summary() for d in MODEL.dispersers.values()],
            "readout_seconds_per_group": MODEL.data["readout_seconds_per_group"]}


@server.tool()
@_safe
def model_info(disperser: str | None = None) -> dict[str, Any]:
    """Describe the empirical noise model: what it is fitted to, its form,
    the systematic terms it includes, its caveats, and how it compares with the
    official ETC (pandeia). With a disperser, adds that disperser's fit details
    (exposure-time scaling, read-noise share, shutter-placement penalty, flux
    recovery vs source size, achieved S/N vs magnitude in the archive)."""
    out: dict[str, Any] = {
        "model_version": MODEL.version, "built": MODEL.built, "notes": MODEL.data.get("notes"),
        "archive": MODEL.data["archive"],
        "method": (
            "Per-pixel variance of a centred faint point source in the drizzled 2-D spectrum, "
            "sigma_pix^2(lambda) = A(lambda)/T + B(lambda)/(T t_exp^2) [uJy^2], fitted in 0.1-um bins to per-observation "
            "noise curves of faint (S/N < 1.5 per pixel) sources in the standard 3-shutter-slitlet, 3-nod, undithered configuration. "
            "First term: background (zodiacal + thermal) photon noise; second: read noise (falls as t_exp^-3 per exposure for IRS2 ramps). "
            "No noise floor is needed out to 100 ks. On top: the measured 1-D/2-D noise ratio for optimal and 3-px extractions, "
            "the adjacent-pixel correlation (binned S/N uses n_eff = n(1 + 2 rho (n-1)/n)), a shutter-placement noise multiplier "
            "(quartic in the |x|, |y| offsets; the pipeline's point-source pathloss correction rescales flux and noise together), "
            "a source-Poisson term g(lambda) F / T, and flux-recovery fractions vs spatial size from matches to total photometry."
        ),
        "what_the_official_etc_leaves_out": [
            "only ~40-45% of a typical galaxy's total flux reaches the extracted spectrum (55-60% compact, ~20% for FWHM >= 2.6 px); the ETC takes the flux you give it",
            "sources sit off-centre in their shutters: x1.1 median noise penalty, x2-3 at the dispersion-direction edge",
            "pandeia's full-shutter noise for a centred point source is 1.4-2.1x the empirical optimal-extraction noise, so realistic S/N for a typical target is 0.6-0.9x the ETC point-source value, 1.5-2x better for a compact source, 0.3-0.4x for an extended one or an edge placement",
        ],
        "caveats": [
            "describes CAMPFIRE reductions (jwst 1.14-1.20, nod subtraction, no bar-shadow correction, native-scale drizzle); other reductions differ by 10-20% in per-pixel noise, the scalings are instrumental",
            "exposure-time scalings are inferred across programs; dispersers with few observations constrain the read-noise term weakly",
            "3-shutter nod-subtracted MSA data only: no master-background or fixed-slit strategies; only IRS2 readouts occur in the archive",
            "G140M and G235M coverage extends beyond the nominal cut-offs (extended-wavelength reductions) with lower throughput",
            "pipeline errors are conservative by ~13% (2-D) and 5-10% (1-D); mock spectra carry the empirical (true) noise",
        ],
        "report_url": REPORT_URL,
    }
    if disperser:
        d = _disp(disperser); p = d.data
        ok = d.band_ok
        rn = np.array([np.nan if v is None else v for v in p["rn_frac"]], float)
        out["disperser"] = d.summary() | {
            "T_slope_free_fit": _sig(p.get("slope"), 3),
            "read_noise_share_of_variance_at_te_ref": _sig(float(np.nanmedian(rn[ok])), 3),
            "te_ref_s": _sig(p.get("te_ref"), 4),
            "model_scatter_dex": p.get("scatter"),
            "placement_multiplier": {"median": _sig(p["mult_median"], 3), "mean": _sig(p["mult_mean"], 3),
                                     "84th_percentile": _sig(p.get("mult_84"), 3), "borrowed_from_prism": p["pos_borrowed"],
                                     "table_x_by_y_at_0_0.15_0.3_0.4_0.5": p.get("pos_tab")},
            "flux_recovery": p["recovery"],
            "achieved_snr_vs_magnitude_in_archive": p.get("snr_vs_mag"),
            "pipeline_error_ratio_empirical_over_reported": {k: _sig(float(np.nanmedian(np.array([np.nan if x is None else x for x in v], float)[ok])), 3)
                                                             for k, v in p["pipeline_error_ratio"].items()},
            "pandeia_comparison": pandeia_comparison(d),
        }
    return out


@server.tool()
@_safe
def exposure_time(readout: str, ngroups: int, nint: int = 1, nexp: int = 1) -> dict[str, Any]:
    """Per-exposure and total on-source time for a NIRSpec readout setup
    (readout: nrsirs2 | nrsirs2rapid | nrs | nrsrapid; nexp counts all nods and
    visits). Pure timing, no overheads."""
    e = _exposure(readout, ngroups, nint, nexp, None, None)
    return {"model_version": MODEL.version} | e.to_dict() | {"extrapolated_readout": e.readout in EXTRAPOLATED_READOUTS}


@server.tool()
@_safe
def depth(
    disperser: str,
    readout: str | None = None, ngroups: int | None = None, nint: int = 1, nexp: int = 1,
    total_s: float | None = None, per_exposure_s: float | None = None,
    wavelengths: list[float] | None = None,
    extraction: str = "optimal", placement: str | list[float] = "typical", margin: float = 1.1,
) -> dict[str, Any]:
    """Continuum and emission-line depth (no source needed): 1-sigma noise per
    pixel and per resolution element, 5-sigma AB continuum limits, and the
    5-sigma flux limit for an unresolved line, at each wavelength (default: a
    grid across the disperser). Setup: readout+ngroups(+nint,nexp) or
    total_s+per_exposure_s. placement: typical | centred | mean | [x, y] shutter
    offsets. margin multiplies the noise (1.1 = the recommended +10%)."""
    d = _disp(disperser); e = _exposure(readout, ngroups, nint, nexp, total_s, per_exposure_s)
    mult, plabel = resolve_placement(d, placement)
    w = _wavelengths(d, wavelengths)
    c = continuum(d, e, w, None, extraction=extraction, placement_mult=mult, margin=margin)
    i = _nearest(c, None)
    return {
        "model_version": MODEL.version, "disperser": d.name, "exposure": e.to_dict(),
        "assumptions": {"extraction": extraction, "placement": plabel, "placement_multiplier": _sig(mult, 3), "margin": margin},
        "rows": _rows(c, False),
        "proposal_sentence": (
            f"NIRSpec/MSA {d.name}, {e.describe()}: 5-sigma continuum limit {c['ab5_res'][i]:.1f} AB per resolution element "
            f"({c['ab5_pix'][i]:.1f} per pixel) and 5-sigma unresolved-line limit {fmt_sci(c['line5'][i])} erg/s/cm2 at "
            f"{c['wave'][i]:.2f} um for a point source ({extraction} extraction, {plabel}; empirical estimate from the CAMPFIRE archive)."
        ),
        "notes": _notes(d, e),
    }


@server.tool()
@_safe
def continuum_snr(
    disperser: str,
    readout: str | None = None, ngroups: int | None = None, nint: int = 1, nexp: int = 1,
    total_s: float | None = None, per_exposure_s: float | None = None,
    magnitude_ab: float | None = None, flux_njy: float | None = None,
    sed: dict[str, Any] | None = None, beta: float | None = None, beta_wave_um: float | None = None,
    morphology: str = "typical", fwhm_px: float | None = None, recovery: float | None = None,
    wavelengths: list[float] | None = None, wave_of_interest_um: float | None = None,
    bin_mode: str = "resolution", bin_value: float | None = None, target_snr: float = 5.0,
    extraction: str = "optimal", placement: str | list[float] = "typical", margin: float = 1.1,
) -> dict[str, Any]:
    """Continuum S/N of a source: per pixel, per resolution element and per
    custom bin at each wavelength, the 5-sigma limits, the time needed to reach
    target_snr, and a proposal-ready sentence.

    Source (one of): magnitude_ab (total AB, flat f_nu), flux_njy, an sed
    object {wave, flux, wave_unit, flux_unit, redshift?, normalize?}, or
    magnitude_ab + beta (f_lambda ~ lambda^beta, normalised at beta_wave_um).
    Morphology sets the flux-recovery fraction: point | compact | typical |
    extended, or fwhm_px (spatial FWHM in 0.1" pixels); recovery overrides it.
    bin_mode: resolution | pixel | R (bin_value = resolving power) | dlambda
    (bin_value in um). wave_of_interest_um picks the headline wavelength."""
    d = _disp(disperser); e = _exposure(readout, ngroups, nint, nexp, total_s, per_exposure_s)
    mult, plabel = resolve_placement(d, placement)
    w = _wavelengths(d, wavelengths)
    if wave_of_interest_um is not None and wavelengths is None:
        w = np.unique(np.r_[w, float(wave_of_interest_um)])
    F, rec, mlabel, sed_obj = _source(d, w, magnitude_ab=magnitude_ab, flux_njy=flux_njy, sed=sed, beta=beta,
                                      beta_wave_um=beta_wave_um, morphology=morphology, fwhm_px=fwhm_px,
                                      recovery=recovery, extraction=extraction)
    if F is None:
        raise ValueError("give a source: magnitude_ab, flux_njy, sed, or magnitude_ab + beta")
    try:
        c = continuum(d, e, w, F, extraction=extraction, placement_mult=mult, margin=margin, bin_mode=bin_mode, bin_value=bin_value,
                      line_recovery=rec)
    except ModelError as err:
        raise ValueError(str(err)) from err
    i = _nearest(c, wave_of_interest_um)
    t_need = time_for_snr(e, float(c["snr_bin"][i]), target_snr)
    bm = bin_mode.lower()
    bin_label = ("per resolution element" if bm in ("resolution", "res", "element") else "per pixel" if bm in ("pixel", "pix")
                 else f"per R={bin_value:g} bin" if bm in ("r", "resolving_power") else f"per {bin_value:g} um bin")
    src_desc = (f"a source with m_AB = {magnitude_ab:.1f}" if magnitude_ab is not None and beta is None else
                f"a source of {flux_njy:g} nJy" if flux_njy is not None else f"the given SED ({sed_obj.description})")
    sentence = (
        f"NIRSpec/MSA {d.name}, {e.describe()}: {src_desc} ({mlabel}, {rec * 100:.0f}% of the flux in the extraction) reaches "
        f"S/N = {c['snr_pix'][i]:.1f} per pixel and {c['snr_res'][i]:.1f} per resolution element at {c['wave'][i]:.2f} um; "
        f"5-sigma continuum limit {c['ab5_res'][i]:.1f} AB per resolution element and 5-sigma unresolved-line limit "
        f"{fmt_sci(c['line5'][i])} erg/s/cm2 at the same wavelength. Empirical estimate from the CAMPFIRE archive "
        f"({plabel} x{mult:.2f} noise, margin x{margin:g}; no noise floor assumed)."
    )
    notes = _notes(d, e)
    if sed_obj is not None:
        n = sed_obj.coverage_note(*d.coverage)
        if n:
            notes.append(n)
    return {
        "model_version": MODEL.version, "disperser": d.name, "exposure": e.to_dict(),
        "source": {"description": src_desc, "morphology": mlabel, "flux_recovery": _sig(rec, 3)},
        "assumptions": {"extraction": extraction, "placement": plabel, "placement_multiplier": _sig(mult, 3),
                        "margin": margin, "binning": bin_label},
        "headline": {
            "wave_um": _sig(c["wave"][i]), "snr_per_pixel": _sig(c["snr_pix"][i]),
            "snr_per_res_element": _sig(c["snr_res"][i]), "snr_per_bin": _sig(c["snr_bin"][i]), "binning": bin_label,
            "ab_5sigma_per_res_element": _sig(c["ab5_res"][i]), "line_5sigma_cgs": _sig(c["line5"][i], 3),
            "time_for_target_snr": {"target_snr": target_snr, "binning": bin_label, "total_s": _sig(t_need, 4),
                                    "description": fmt_time(t_need) if np.isfinite(t_need) else None,
                                    "factor_vs_requested": _sig(t_need / e.total_s, 3)},
        },
        "rows": _rows(c, True),
        "proposal_sentence": sentence,
        "notes": notes,
    }


@server.tool()
@_safe
def line_snr(
    disperser: str, wave_um: float, flux_cgs: float, fwhm_kms: float = 0.0,
    readout: str | None = None, ngroups: int | None = None, nint: int = 1, nexp: int = 1,
    total_s: float | None = None, per_exposure_s: float | None = None,
    continuum_magnitude_ab: float | None = None, continuum_flux_njy: float | None = None,
    morphology: str = "typical", fwhm_px: float | None = None, recovery: float | None = None,
    line_recovery: float | None = None, target_snr: float = 5.0,
    extraction: str = "optimal", placement: str | list[float] = "typical", margin: float = 1.1,
) -> dict[str, Any]:
    """S/N of an emission line of total flux flux_cgs [erg/s/cm2] at observed
    wave_um with intrinsic FWHM fwhm_kms (0 = unresolved), the 5-sigma line
    limit at that wavelength, and the time to reach target_snr. The line is
    integrated over +-1 FWHM including the instrumental resolution, with the
    measured pixel correlation. An underlying continuum (magnitude or nJy) adds
    its photon noise. The morphology's flux-recovery fraction applies to the
    line too unless line_recovery is given."""
    d = _disp(disperser); e = _exposure(readout, ngroups, nint, nexp, total_s, per_exposure_s)
    mult, plabel = resolve_placement(d, placement)
    F, rec, mlabel, _ = _source(d, np.array([wave_um]), magnitude_ab=continuum_magnitude_ab, flux_njy=continuum_flux_njy,
                                sed=None, beta=None, beta_wave_um=None, morphology=morphology, fwhm_px=fwhm_px,
                                recovery=recovery, extraction=extraction)
    lrec = rec if line_recovery is None else float(np.clip(line_recovery, 0.05, 1.0))
    try:
        r = line_calc(d, e, wave_um, flux_cgs, fwhm_kms, cont_flux_spec_ujy=float(F[0]) if F is not None else 0.0,
                      line_recovery=lrec, extraction=extraction, placement_mult=mult, margin=margin)
    except ModelError as err:
        raise ValueError(str(err)) from err
    t_need = time_for_snr(e, r["snr"], target_snr)
    sentence = (
        f"NIRSpec/MSA {d.name}, {e.describe()}: a {flux_cgs / 1e-18:.1f}e-18 erg/s/cm2 line at {wave_um:.2f} um"
        f"{f' (FWHM {fwhm_kms:g} km/s)' if fwhm_kms else ' (unresolved)'} is detected at S/N = {r['snr']:.1f} "
        f"({lrec * 100:.0f}% of the line flux in the extraction, {mlabel}); the 5-sigma line limit at that wavelength is "
        f"{fmt_sci(r['limit_5sigma_cgs'])} erg/s/cm2. Empirical estimate from the CAMPFIRE archive ({plabel} x{mult:.2f} noise, margin x{margin:g})."
    )
    return {
        "model_version": MODEL.version, "disperser": d.name, "exposure": e.to_dict(),
        "line": {k: _sig(v, 4) for k, v in r.items()},
        "continuum_under_line_njy": _sig(F[0] * 1e3) if F is not None else 0.0,
        "assumptions": {"extraction": extraction, "placement": plabel, "placement_multiplier": _sig(mult, 3), "margin": margin,
                        "morphology": mlabel, "line_flux_recovery": _sig(lrec, 3)},
        "time_for_target_snr": {"target_snr": target_snr, "total_s": _sig(t_need, 4),
                                "description": fmt_time(t_need) if np.isfinite(t_need) else None,
                                "factor_vs_requested": _sig(t_need / e.total_s, 3)},
        "proposal_sentence": sentence,
        "notes": _notes(d, e),
    }


@server.tool()
@_safe
def simulate_spectrum(
    disperser: str,
    readout: str | None = None, ngroups: int | None = None, nint: int = 1, nexp: int = 1,
    total_s: float | None = None, per_exposure_s: float | None = None,
    magnitude_ab: float | None = None, flux_njy: float | None = None,
    sed: dict[str, Any] | None = None, beta: float | None = None, beta_wave_um: float | None = None,
    lines: list[dict[str, float]] | None = None,
    morphology: str = "typical", fwhm_px: float | None = None, recovery: float | None = None, line_recovery: float | None = None,
    wave_range: list[float] | None = None, n_realizations: int = 1, seed: int | None = None,
    extraction: str = "optimal", placement: str | list[float] = "typical", margin: float = 1.1,
    output_path: str | None = None, output_format: str = "ecsv", flux_unit: str = "uJy",
    max_inline_values: int = MAX_INLINE_VALUES_DEFAULT, ctx: Context | None = None,
) -> dict[str, Any]:
    """Generate a mock extracted 1-D spectrum on the disperser's native pixel
    grid: the source (magnitude/flux/SED/power law, see continuum_snr) plus
    optional emission lines [{wave_um, flux_cgs, fwhm_kms}], smoothed to the
    instrumental resolution, with noise drawn from the empirical model
    (correlated between neighbouring pixels as in real data). Returns wave_um,
    model (noiseless), err and flux (one list per realization) in flux_unit
    (uJy | nJy | mJy | Jy), the analytic S/N of each line, and a summary.

    Large results are not inlined: on the hosted server they come back as a
    download_url (valid one hour; formats ecsv | txt | npz | json | fits) and on
    a local install you can pass output_path to write the file directly. Use
    wave_range [lo, hi] to simulate only part of the coverage."""
    d = _disp(disperser); e = _exposure(readout, ngroups, nint, nexp, total_s, per_exposure_s)
    if not (1 <= n_realizations <= 50):
        raise ValueError("n_realizations must be between 1 and 50")
    probe = np.array([float(np.mean(d.coverage))])
    F, rec, mlabel, sed_obj = _source(d, probe, magnitude_ab=magnitude_ab, flux_njy=flux_njy, sed=sed, beta=beta,
                                      beta_wave_um=beta_wave_um, morphology=morphology, fwhm_px=fwhm_px,
                                      recovery=recovery, extraction=extraction)
    if sed_obj is None and magnitude_ab is not None:
        sed_obj = flat_sed(magnitude_ab=magnitude_ab)
    elif sed_obj is None and flux_njy is not None:
        sed_obj = flat_sed(flux_njy=flux_njy)
    try:
        r = simulate_calc(d, e, sed_obj, lines, morphology=morphology, fwhm_px=fwhm_px, recovery=recovery,
                          line_recovery=line_recovery, extraction=extraction, placement=placement, margin=margin,
                          wave_range=wave_range, n_realizations=n_realizations, seed=seed)
    except ModelError as err:
        raise ValueError(str(err)) from err
    scale = {"ujy": 1.0, "njy": 1e3, "mjy": 1e-3, "jy": 1e-6}.get(flux_unit.lower().replace("µ", "u"))
    if scale is None:
        raise ValueError("flux_unit must be uJy | nJy | mJy | Jy")
    out: dict[str, Any] = {
        "model_version": MODEL.version, "disperser": r["disperser_name"], "exposure": r["exposure"], "source": r["source"],
        "n_pixels": r["n_pixels"], "n_realizations": r["n_realizations"], "seed": r["seed"],
        "flux_unit": flux_unit, "summary": r["summary"],
        "lines": [{k: _sig(v, 4) if not isinstance(v, str) else v for k, v in ln.items()} for ln in r["lines"]],
        "notes": list(r["notes"]) + _notes(d, e),
    }
    fmt = output_format.lower().lstrip(".")
    if fmt not in ("ecsv", "txt", "dat", "npz", "json", "fits"):
        raise ValueError("output_format must be ecsv | txt | npz | json | fits")
    if fmt == "fits":
        try:
            import astropy  # noqa: F401
        except ImportError:
            raise ValueError("FITS output is not available on this server (astropy missing); use ecsv, npz, json or txt")
    if output_path:
        if not LOCAL_FILES:
            raise ValueError("output_path only works with a local (stdio) install; on the hosted server use the download_url instead")
        out["output_path"] = write_spectrum(r, output_path, flux_unit)
    n_values = r["n_pixels"] * (3 + r["n_realizations"])
    if n_values <= max_inline_values:
        out["wave_um"] = _sig(r["wave_um"], 6)
        out["model"] = _sig(r["model_ujy"] * scale, 5)
        out["err"] = _sig(r["err_ujy"] * scale, 4)
        out["flux"] = [_sig(row * scale, 5) for row in r["flux_ujy"]]
    else:
        out["arrays_inlined"] = False
        out["notes"].append(
            f"{n_values} values exceed max_inline_values={max_inline_values}; arrays are not inlined. "
            + ("Use output_path to write a file, " if LOCAL_FILES else "Fetch download_url, ")
            + "or narrow wave_range / reduce n_realizations.")
    if not LOCAL_FILES:
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            p = write_spectrum(r, str(pathlib.Path(td) / f"mock_{d.key}.{fmt}"), flux_unit)
            data = pathlib.Path(p).read_bytes()
        ctype = {"npz": "application/octet-stream", "json": "application/json", "fits": "application/fits"}.get(fmt, "text/plain")
        fid = _store_file(data, ctype, f"mock_{d.key}.{fmt}")
        base = _public_base(ctx)
        out["download_url"] = f"{base}/files/{fid}" if base else f"/files/{fid}"
        out["download_expires_in_s"] = DOWNLOAD_TTL_S
        out["download_format"] = fmt
    return out


# --------------------------------------------------------------------------- prompt

@server.prompt(name="nirspec_etc_guide", title="How to use the empirical NIRSpec ETC")
def nirspec_etc_guide() -> str:
    """Guidance for using these tools well in proposal writing and simulations."""
    return INSTRUCTIONS + """

Recipes:
- "What S/N will we get on a m_AB = 27 galaxy with PRISM in 5 ks?" -> continuum_snr(disperser="prism",
  readout="nrsirs2", ngroups=13, nexp=6, magnitude_ab=27, wave_of_interest_um=2.5). Quote snr_per_res_element
  and the proposal_sentence; say "typical target, ~45% of the flux in the extraction" and "+10% margin".
- "Will we detect [OIII] at 3e-18 at z=6?" -> line_snr(disperser="g395m", ..., wave_um=0.5007*(1+z),
  flux_cgs=3e-18, fwhm_kms=150, continuum_magnitude_ab=27.5). Use time_for_target_snr for the requested time.
- "How deep is this observation?" -> depth(...). ab_5sigma_per_res_element is the number to quote.
- "Make a mock spectrum of this SED" -> simulate_spectrum with sed={wave, flux, wave_unit, flux_unit,
  normalize: {magnitude_ab, band}} and lines=[...]; save the download (or output_path) and plot it yourself.
- Comparing with the official ETC: use morphology="point", placement="centred", margin=1.0, and read
  model_info(disperser).disperser.pandeia_comparison for the measured ratio."""


def build_http_app(*, stateless: bool = True, json_response: bool = True, allowed_hosts: Sequence[str] | None = None, host: str = "127.0.0.1"):
    """The ASGI app for the hosted transport (mounts /mcp plus the custom routes)."""
    from mcp.server.transport_security import TransportSecuritySettings
    if allowed_hosts:
        sec = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=list(allowed_hosts),
                                        allowed_origins=[f"https://{h}" for h in allowed_hosts] + [f"http://{h}" for h in allowed_hosts])
    else:
        sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return server.streamable_http_app(streamable_http_path="/mcp", stateless_http=stateless, json_response=json_response,
                                      transport_security=sec, host=host)
