"""`campfire-etc` command line: run the MCP server, quick calculations, and the
model rebuild pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import numpy as np


def _json(obj: Any) -> str:
    def default(o):
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        return str(o)
    return json.dumps(obj, indent=2, default=default, allow_nan=True)


def _add_exposure_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("exposure (readout setup or explicit times)")
    g.add_argument("--readout", choices=["nrsirs2", "nrsirs2rapid", "nrs", "nrsrapid"])
    g.add_argument("--ngroups", type=int)
    g.add_argument("--nint", type=int, default=1)
    g.add_argument("--nexp", type=int, default=1, help="all nods x visits")
    g.add_argument("--total", type=float, dest="total_s", help="total on-source time [s]")
    g.add_argument("--texp", type=float, dest="per_exposure_s", help="per-exposure time [s]")


def _add_source_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("source")
    g.add_argument("--mag", type=float, help="total AB magnitude (flat f_nu unless --beta)")
    g.add_argument("--flux-njy", type=float)
    g.add_argument("--sed", help="SED file (2-column text, .npz, .json or FITS table)")
    g.add_argument("--sed-wave-unit", default="um"); g.add_argument("--sed-flux-unit", default="uJy")
    g.add_argument("--redshift", type=float, help="shift the SED wavelengths by (1+z)")
    g.add_argument("--norm-mag", type=float, help="normalise the SED to this AB magnitude at --norm-wave")
    g.add_argument("--norm-wave", type=float, help="[um]")
    g.add_argument("--beta", type=float, help="f_lambda ~ lambda^beta, with --mag at --beta-wave")
    g.add_argument("--beta-wave", type=float)
    g.add_argument("--morphology", default="typical", choices=["point", "compact", "typical", "extended"])
    g.add_argument("--fwhm-px", type=float); g.add_argument("--recovery", type=float)
    g.add_argument("--extraction", default="optimal", choices=["optimal", "3px"])
    g.add_argument("--placement", default="typical", help="typical | centred | mean | x,y")
    g.add_argument("--margin", type=float, default=1.1)


def _exposure(a) -> Any:
    from .model import make_exposure
    return make_exposure(a.readout, a.ngroups, a.nint, a.nexp, a.total_s, a.per_exposure_s)


def _placement(a):
    if "," in a.placement:
        x, y = a.placement.split(",")
        return [float(x), float(y)]
    return a.placement


def _sed(a):
    from .sed import parse_sed, power_law_sed
    if a.beta is not None:
        if a.mag is None or a.beta_wave is None:
            raise SystemExit("--beta needs --mag and --beta-wave")
        return power_law_sed(a.mag, a.beta_wave, a.beta)
    if a.sed:
        spec = {"file": a.sed, "wave_unit": a.sed_wave_unit, "flux_unit": a.sed_flux_unit}
        if a.redshift is not None:
            spec["redshift"] = a.redshift
        if a.norm_mag is not None:
            spec["normalize"] = {"magnitude_ab": a.norm_mag, "wave_um": a.norm_wave}
        return parse_sed(spec, allow_files=True)
    return None


def cmd_serve(a) -> None:
    from . import server as srv
    if a.http:
        import uvicorn
        hosts = [h.strip() for h in (a.allowed_host or os.environ.get("CAMPFIRE_ETC_ALLOWED_HOSTS", "")).split(",") if h.strip()]
        if a.public_url:
            srv.PUBLIC_URL = a.public_url.rstrip("/")
        app = srv.build_http_app(allowed_hosts=hosts or None, host=a.host)
        print(f"campfire-etc {srv.__version__} (model {srv.MODEL.version}) serving MCP over HTTP at http://{a.host}:{a.port}/mcp", file=sys.stderr)
        uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    else:
        srv.LOCAL_FILES = True
        srv.server.run(transport="stdio")


def cmd_info(a) -> None:
    from .model import load_model, pandeia_comparison
    m = load_model(a.model)
    if a.disperser:
        d = m.get(a.disperser)
        print(_json(d.summary() | {"pandeia": pandeia_comparison(d)}))
    else:
        print(_json(m.info()))


def cmd_snr(a) -> None:
    from .model import continuum, load_model, resolve_placement, source_flux, time_for_snr, fmt_time
    m = load_model(a.model); d = m.get(a.disperser); e = _exposure(a)
    mult, plabel = resolve_placement(d, _placement(a))
    w = np.array(a.wave, float) if a.wave else d.default_wavelengths()
    sed = _sed(a)
    F, rec, mlabel = source_flux(d, w, magnitude_ab=a.mag if sed is None else None, flux_njy=a.flux_njy, sed=sed,
                                 morphology=a.morphology, fwhm_px=a.fwhm_px, extraction=a.extraction, recovery=a.recovery)
    c = continuum(d, e, w, F, extraction=a.extraction, placement_mult=mult, margin=a.margin)
    print(f"# {d.name}; {e.describe()}; {mlabel}, recovery {rec:.2f}; {plabel} x{mult:.2f}; margin x{a.margin:g}")
    hdr = "wave_um  sig1d_nJy  ab5_pix  ab5_res  line5_cgs" + ("  snr_pix  snr_res" if F is not None else "")
    print(hdr)
    for i, ww in enumerate(w):
        if not np.isfinite(c["sig_1d"][i]):
            print(f"{ww:7.3f}  (outside coverage)"); continue
        row = f"{ww:7.3f}  {c['sig_1d'][i]*1e3:9.2f}  {c['ab5_pix'][i]:7.2f}  {c['ab5_res'][i]:7.2f}  {c['line5'][i]:9.2e}"
        if F is not None:
            row += f"  {c['snr_pix'][i]:7.2f}  {c['snr_res'][i]:7.2f}"
        print(row)
    if F is not None and a.target_snr:
        ok = np.isfinite(c["snr_res"]); i = int(np.where(ok)[0][len(np.where(ok)[0]) // 2])
        t = time_for_snr(e, float(c["snr_res"][i]), a.target_snr)
        print(f"# S/N = {a.target_snr:g} per resolution element at {w[i]:.2f} um needs T = {fmt_time(t)}")


def cmd_line(a) -> None:
    from .model import line, load_model, resolve_placement, source_flux
    m = load_model(a.model); d = m.get(a.disperser); e = _exposure(a)
    mult, plabel = resolve_placement(d, _placement(a))
    F, rec, mlabel = source_flux(d, np.array([a.wave]), magnitude_ab=a.mag, flux_njy=a.flux_njy,
                                 morphology=a.morphology, fwhm_px=a.fwhm_px, extraction=a.extraction, recovery=a.recovery)
    r = line(d, e, a.wave, a.flux, a.fwhm_kms, cont_flux_spec_ujy=float(F[0]) if F is not None else 0.0,
             line_recovery=rec, extraction=a.extraction, placement_mult=mult, margin=a.margin)
    print(_json(r | {"disperser": d.name, "exposure": e.to_dict(), "morphology": mlabel, "placement": plabel}))


def cmd_simulate(a) -> None:
    from .model import load_model
    from .sed import flat_sed
    from .simulate import simulate, write_spectrum
    m = load_model(a.model); d = m.get(a.disperser); e = _exposure(a)
    sed = _sed(a)
    if sed is None and a.mag is not None:
        sed = flat_sed(magnitude_ab=a.mag)
    elif sed is None and a.flux_njy is not None:
        sed = flat_sed(flux_njy=a.flux_njy)
    lines = []
    for spec in a.line or []:
        parts = [float(x) for x in spec.split(",")]
        lines.append({"wave_um": parts[0], "flux_cgs": parts[1], "fwhm_kms": parts[2] if len(parts) > 2 else 0.0})
    r = simulate(d, e, sed, lines, morphology=a.morphology, fwhm_px=a.fwhm_px, recovery=a.recovery, extraction=a.extraction,
                 placement=_placement(a), margin=a.margin, wave_range=a.wave_range, n_realizations=a.n, seed=a.seed)
    path = write_spectrum(r, a.out, a.flux_unit)
    print(f"wrote {path}: {r['n_pixels']} pixels x {r['n_realizations']} realization(s); {_json(r['summary'])}")
    for ln in r["lines"]:
        print(f"  line {ln['wave_um']:.4f} um: {ln['status']}" + (f", S/N {ln['snr']:.1f}" if "snr" in ln else ""))


def cmd_build(a) -> None:
    from .build import pipeline
    pipeline.main(a)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="campfire-etc", description="Empirical NIRSpec/MSA ETC from the CAMPFIRE archive")
    p.add_argument("--model", help="model version (default: latest bundled) or a JSON path")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the MCP server (stdio by default)")
    s.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    s.add_argument("--public-url", help="base URL advertised for downloads (or CAMPFIRE_ETC_PUBLIC_URL)")
    s.add_argument("--allowed-host", help="comma-separated Host headers to accept (or CAMPFIRE_ETC_ALLOWED_HOSTS); unset disables the check")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("info", help="model and disperser summary"); s.add_argument("--disperser"); s.set_defaults(func=cmd_info)

    s = sub.add_parser("snr", help="continuum S/N and depth vs wavelength")
    s.add_argument("--disperser", required=True); _add_exposure_args(s); _add_source_args(s)
    s.add_argument("--wave", type=float, nargs="+", help="wavelengths [um] (default: a grid over the coverage)")
    s.add_argument("--target-snr", type=float, default=5.0)
    s.set_defaults(func=cmd_snr)

    s = sub.add_parser("line", help="emission-line S/N")
    s.add_argument("--disperser", required=True); _add_exposure_args(s); _add_source_args(s)
    s.add_argument("--wave", type=float, required=True, help="observed wavelength [um]")
    s.add_argument("--flux", type=float, required=True, help="line flux [erg/s/cm2]")
    s.add_argument("--fwhm-kms", type=float, default=0.0)
    s.set_defaults(func=cmd_line)

    s = sub.add_parser("simulate", help="write a mock spectrum")
    s.add_argument("--disperser", required=True); _add_exposure_args(s); _add_source_args(s)
    s.add_argument("--line", action="append", metavar="WAVE_UM,FLUX_CGS[,FWHM_KMS]")
    s.add_argument("--wave-range", type=float, nargs=2)
    s.add_argument("-n", type=int, default=1, help="realizations"); s.add_argument("--seed", type=int)
    s.add_argument("--flux-unit", default="uJy")
    s.add_argument("--out", required=True, help="output file (.ecsv .txt .npz .json .fits)")
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("build", help="rebuild the noise model from a local CAMPFIRE archive (needs the [build] extra)")
    s.add_argument("step", choices=["harvest", "fit", "phot", "depth", "figs", "all", "assemble"])
    s.add_argument("dispersers", nargs="?", default="all", help="comma-separated keys or 'all'")
    s.add_argument("--root", default=os.environ.get("CAMPFIRE_ROOT", os.path.expanduser("~/campfire")), help="CAMPFIRE root (products/ and meta/ under it)")
    s.add_argument("--workdir", default="etc-build", help="where harvest/fit outputs go")
    s.add_argument("--disp-dir", help="directory with jwst_nirspec_<grating>_disp.fits (default: the installed campfire_pipeline data)")
    s.add_argument("--processes", type=int, default=8); s.add_argument("--limit", type=int, help="harvest only a random subset (testing)")
    s.add_argument("--version", help="assemble: model version label, e.g. 2026.09")
    s.add_argument("--built", help="assemble: build date (default today)")
    s.add_argument("--pandeia", help="assemble: etc_results_all.json from the pandeia comparison runs")
    s.add_argument("--notes", default="", help="assemble: free-text notes for the manifest")
    s.add_argument("--out-dir", help="assemble: models directory (default: the package's models/)")
    s.add_argument("--no-latest", action="store_true", help="assemble: register the version without making it the default")
    s.set_defaults(func=cmd_build)

    a = p.parse_args(argv)
    try:
        a.func(a)
    except Exception as e:  # keep CLI errors short
        if os.environ.get("CAMPFIRE_ETC_DEBUG"):
            raise
        print(f"error: {e}", file=sys.stderr); sys.exit(1)


if __name__ == "__main__":
    main()
