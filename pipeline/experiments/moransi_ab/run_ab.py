"""
A/B harness: tiered vs Moran's-I source masking for NIRCam MOSAIC background.

Runs ``SubtractBackground`` on one mosaic with several arms that differ ONLY in
the source-mask method/percentile (the ``Background2D`` fit is identical across
arms, so the mask is the single variable). Computes the primary metrics from
``docs/moransi-background-scoping.md`` §5 and writes a decision table + optional
diagnostic panels.

Usage:
    # real mosaic (use the preserved pre-subtraction input if you have it):
    python run_ab.py path/to/mosaic_..._i2d_before_bkgsub.fits \
        --arms tiered moransi:40 moransi:50 moransi:60 --plot

    # verify the harness end-to-end on synthetic data (no real data needed):
    python run_ab.py --selftest

Metrics per arm (all "lower/flatter is better", compared to the tiered arm):
    bkg_width    mad_std of blank (background) pixels — sky uniformity  [PRIMARY]
    trough       most-negative inner radial-profile value around bright
                 sources, in units of bkg_width — oversubtraction flag  [PRIMARY]
    flux_ratio   aperture flux at shared source positions / tiered arm  [PRIMARY]
    masked_frac  fraction of pixels excluded from the fit
    runtime_s    wall-clock of the mask+fit
"""
import argparse
import os
import sys
import time

import numpy as np
from astropy.io import fits
from astropy.stats import mad_std

from campfire_pipeline.nircam.bkgsub import SubtractBackground

HERE = os.path.dirname(os.path.abspath(__file__))
# Reuse the source/photometry metrics from the 1/f A/B experiment.
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "oneoverf_gp"))
from ab_metrics import (  # noqa: E402
    detect_bright_sources,
    radial_profile,
    aperture_photometry,
)


def parse_arm(token):
    """'tiered' or 'moransi[:percentile]' -> (method, percentile)."""
    if token == "tiered":
        return ("tiered", None)
    if token.startswith("moransi"):
        parts = token.split(":")
        pct = float(parts[1]) if len(parts) > 1 else 40.0
        return ("moransi", pct)
    raise ValueError(f"bad arm {token!r} (use 'tiered' or 'moransi[:pct]')")


def run_arm(path, method, pct, patch, kernel):
    kw = dict(mask_method=method)
    if method == "moransi":
        kw.update(moransi_patch_size=patch, moransi_percentile=pct,
                  moransi_kernel=kernel)
    bkg = SubtractBackground(**kw)
    t0 = time.time()
    sub, mask_final, _ = bkg.compute(path)
    dt = time.time() - t0
    # model = sci - sub is the fitted background; recover sci for panels.
    with fits.open(path, memmap=False) as h:
        sci = h["SCI"].data.astype(np.float64)
    return dict(method=method, pct=pct, sci=sci, sub=sub.astype(np.float64),
                mask=mask_final, runtime_s=dt)


def arm_metrics(arm, sources, ref_flux=None):
    sub, mask = arm["sub"], arm["mask"]
    blank = np.isfinite(sub) & ~mask
    m = {"masked_frac": float(mask.mean()),
         "runtime_s": arm["runtime_s"]}
    m["bkg_width"] = float(mad_std(sub[blank])) if blank.any() else np.nan

    # Oversubtraction: most-negative inner radial-profile value (in bkg_width).
    troughs = []
    for (y, x, _f) in sources:
        _, prof = radial_profile(sub, blank, y, x, rmax=100, nbin=25)
        inner = prof[:8]
        if np.isfinite(inner).any():
            troughs.append(np.nanmin(inner))
    if troughs and np.isfinite(m["bkg_width"]) and m["bkg_width"] > 0:
        m["trough"] = float(np.nanmin(troughs) / m["bkg_width"])
    else:
        m["trough"] = np.nan

    # Aperture flux at the shared source positions; ratio vs the reference arm.
    if sources:
        flux = aperture_photometry(sub, sources)
        m["_flux"] = flux
        if ref_flux is not None:
            good = np.isfinite(flux) & np.isfinite(ref_flux) & (ref_flux != 0)
            m["flux_ratio"] = (float(np.median(flux[good] / ref_flux[good]))
                               if good.any() else np.nan)
        else:
            m["flux_ratio"] = 1.0
    else:
        m["_flux"] = np.array([])
        m["flux_ratio"] = np.nan
    return m


def summarize(arms, metrics, out_md):
    ref = metrics[0]
    lines = ["# Moran's-I vs tiered — mosaic background A/B", "",
             "Reference arm (Δ baseline): "
             f"**{arms[0]['method']}"
             f"{'' if arms[0]['pct'] is None else ':'+str(arms[0]['pct'])}**",
             "",
             "| arm | bkg_width | Δbkg_width | trough(σ) | flux_ratio | "
             "masked_frac | runtime_s |",
             "|---|---|---|---|---|---|---|"]
    for arm, mm in zip(arms, metrics):
        name = arm["method"] + ("" if arm["pct"] is None else f":{arm['pct']:g}")
        dbw = mm["bkg_width"] - ref["bkg_width"]
        lines.append(
            f"| {name} | {mm['bkg_width']:.4g} | {dbw:+.3g} | "
            f"{mm['trough']:.2f} | {mm['flux_ratio']:.4f} | "
            f"{mm['masked_frac']:.2f} | {mm['runtime_s']:.1f} |")
    lines += ["",
              "Acceptance (adopt Moran's-I for this regime only if ALL hold, "
              "per regime): Δbkg_width ≤ 0; trough not more negative than "
              "tiered; |flux_ratio − 1| < 5e-4; runtime ≤ 2× tiered. "
              "See docs/moransi-background-scoping.md §5."]
    text = "\n".join(lines)
    os.makedirs(os.path.dirname(out_md), exist_ok=True)
    with open(out_md, "w") as fp:
        fp.write(text + "\n")
    print("\n" + text + f"\n\nwrote {out_md}")


def plot_panels(arms, sources, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n = len(arms)
    sci = arms[0]["sci"]
    blank0 = np.isfinite(sci)
    vmin, vmax = np.nanpercentile(sci[blank0], [30, 99])
    fig, axes = plt.subplots(n, 4, figsize=(22, 5.2 * n), squeeze=False)
    titles = ["(a) input SCI", "(b) source mask", "(c) bg model",
              "(d) subtracted"]
    for r, arm in enumerate(arms):
        model = arm["sci"] - arm["sub"]
        panels = [arm["sci"], arm["sci"], model, arm["sub"]]
        for c, (ax, img) in enumerate(zip(axes[r], panels)):
            ax.imshow(img, origin="lower", cmap="Greys_r",
                      vmin=vmin, vmax=vmax, interpolation="nearest")
            if c == 1:
                ov = np.zeros((*arm["mask"].shape, 4))
                ov[arm["mask"]] = [1, 0, 0, 0.35]
                ax.imshow(ov, origin="lower", interpolation="nearest")
            if c == 3:
                for (y, x, _f) in sources:
                    ax.plot(x, y, "c+", ms=8)
            ax.set_xticks([]); ax.set_yticks([])
            if r == 0:
                ax.set_title(titles[c], fontsize=13)
        name = arm["method"] + ("" if arm["pct"] is None
                                else f":{arm['pct']:g}")
        axes[r, 0].set_ylabel(name, fontsize=13)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    fig.savefig(out_png, dpi=130)
    print(f"wrote {out_png}")


_SEP_PIXEL_LIMIT = 2 ** 31 - 1   # sep uses 32-bit indexing; guard huge tiles.


def _pick_covered_crop(valid, size=8000, stride=32):
    """Top-left (cy0, cx0) of a ``size``x``size`` window over the densest-
    coverage region, found cheaply on a strided coverage map. Used to keep
    ``sep`` off arrays larger than 2**31 px (it would integer-overflow) while
    still detecting real interior sources on a non-contiguous mosaic.
    """
    from scipy.ndimage import uniform_filter
    cov = valid[::stride, ::stride].astype(np.float32)
    w = max(1, size // stride)
    dens = uniform_filter(cov, size=w, mode="constant")
    iy, ix = np.unravel_index(int(np.argmax(dens)), dens.shape)
    cy0 = min(max(0, iy * stride - size // 2), max(0, valid.shape[0] - size))
    cx0 = min(max(0, ix * stride - size // 2), max(0, valid.shape[1] - size))
    return cy0, cx0


def detect_sources_safe(image, valid, nmax=30, crop=8000):
    """Detect bright sources on the INPUT image (positions are defined on the
    shared input, identical across arms, and only *measured* on each arm's
    subtracted output). ``valid`` is the coverage mask (True = usable). On
    arrays larger than sep's 2**31-px limit, detection is confined to a covered
    crop (sep would integer-overflow on the full array); positions are returned
    in FULL-array coordinates so downstream cutouts index the full arrays."""
    if image.size <= _SEP_PIXEL_LIMIT:
        return detect_bright_sources(image, valid, nmax=nmax)
    cy0, cx0 = _pick_covered_crop(valid, size=crop)
    print(f"tile {image.shape} > 2**31 px: detecting sources on covered crop "
          f"[{cy0}:{cy0+crop}, {cx0}:{cx0+crop}] to avoid sep overflow")
    img_c = image[cy0:cy0 + crop, cx0:cx0 + crop]
    valid_c = valid[cy0:cy0 + crop, cx0:cx0 + crop]
    src = detect_bright_sources(img_c, valid_c, nmax=nmax)
    return [(y + cy0, x + cx0, f) for (y, x, f) in src]


def _window(shape, y, x, half):
    """In-bounds (y0, y1, x0, x1) cutout window, or None if it runs off-array."""
    y, x = int(round(y)), int(round(x))
    y0, y1, x0, x1 = y - half, y + half, x - half, x + half
    if y0 < 0 or x0 < 0 or y1 > shape[0] or x1 > shape[1]:
        return None
    return (y0, y1, x0, x1)


def plot_zoom_cutouts(arms, sources, out_png, valid=None, half=100, nsrc=6,
                      min_covered=0.9):
    """Per-source zoom cutouts of the subtracted residual, arms vs sources.

    Rows = arms, columns = the same interior bright sources; a diverging
    colormap centered at 0 (scaled to the reference arm's ``bkg_width``) so
    negative wings around bright sources read as blue. Only sources whose
    ``2*half`` window is fully in-bounds AND ``>= min_covered`` covered (per the
    ``valid`` coverage mask) are used — this deliberately skips gap-adjacent
    sources so the wing comparison stays meaningful on **non-contiguous**
    mosaics (where the scalar ``trough`` metric breaks). Returns the written
    path, or None if no clean source qualified.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ref = arms[0]
    if valid is None:                         # fall back to finite-SCI coverage
        valid = np.isfinite(ref["sci"])
    chosen = []
    for (y, x, f) in sources:
        win = _window(ref["sub"].shape, y, x, half)
        if win is None:
            continue
        y0, y1, x0, x1 = win
        if valid[y0:y1, x0:x1].mean() < min_covered:
            continue
        chosen.append((y, x, f, win))
        if len(chosen) >= nsrc:
            break
    if not chosen:
        print("no fully-covered interior bright sources; skipping zoom cutouts")
        return None

    blank_ref = np.isfinite(ref["sub"]) & ~ref["mask"]
    w = float(mad_std(ref["sub"][blank_ref])) if blank_ref.any() else np.nan
    lim = 5.0 * w if np.isfinite(w) and w > 0 else None

    n, m = len(arms), len(chosen)
    fig, axes = plt.subplots(n, m, figsize=(3.0 * m, 3.0 * n), squeeze=False)
    for r, arm in enumerate(arms):
        for c, (y, x, f, win) in enumerate(chosen):
            y0, y1, x0, x1 = win
            ax = axes[r, c]
            kw = dict(vmin=-lim, vmax=lim) if lim else {}
            ax.imshow(arm["sub"][y0:y1, x0:x1], origin="lower", cmap="RdBu_r",
                      interpolation="nearest", **kw)
            ax.set_xticks([]); ax.set_yticks([])
            if r == 0:
                ax.set_title(f"src {c} @({int(x)},{int(y)})", fontsize=9)
            if c == 0:
                name = arm["method"] + ("" if arm["pct"] is None
                                        else f":{arm['pct']:g}")
                ax.set_ylabel(name, fontsize=11)
    scale = f"±5·bkg_width = ±{lim:.3g}" if lim else "auto"
    fig.suptitle(f"zoom residual cutouts ({2*half}px), diverging {scale}; "
                 "blue = negative (oversubtraction / wings)", fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    fig.savefig(out_png, dpi=130)
    print(f"wrote {out_png}")
    return out_png


def run(path, arm_tokens, patch, kernel, do_plot, outdir,
        zoom_half=100, zoom_nsrc=6):
    arms = [run_arm(path, m, p, patch, kernel)
            for (m, p) in (parse_arm(t) for t in arm_tokens)]
    # Detect bright sources ONCE on the reference arm, reuse positions for all.
    # nmax is generous so the zoom-cutout coverage filter still leaves enough
    # clean interior sources on a gappy (non-contiguous) mosaic.
    ref = arms[0]
    # Detect on the shared INPUT SCI (identical across arms) with coverage as
    # the valid mask; measure per-arm on the subtracted output. Detecting on a
    # single arm's subtracted+masked image is fragile (it found nothing on real
    # tiles); the input positions are unambiguous and arm-independent.
    #
    # Coverage comes from ERR, not isfinite(SCI): drizzled tiles zero-FILL the
    # no-coverage SCI (finite), so only ERR (NaN or <=0 off-footprint) marks
    # real coverage — matching bkgsub.off_detector = isnan(err).
    sci_in = ref["sci"]
    with fits.open(path, memmap=True) as h:
        err_in = h["ERR"].data
        valid_in = np.isfinite(err_in) & (err_in > 0)
    big = sci_in.size > _SEP_PIXEL_LIMIT
    try:
        sources = detect_sources_safe(sci_in, valid_in, nmax=30)
    except Exception as e:
        print(f"source detection failed ({e}); skipping flux/trough metrics")
        sources = []
    # arm_metrics runs sep photometry on the FULL array, which overflows past
    # 2**31 px; on such tiles report bkg_width/masked_frac only (flux/trough
    # NaN). Panels + crop-detected zoom cutouts still use `sources`.
    metric_sources = [] if big else sources
    if big and sources:
        print("full-array sep photometry skipped (>2**31 px); flux_ratio/trough "
              "reported NaN — judge oversubtraction from the zoom cutouts.")
    metrics = []
    ref_flux = None
    for arm in arms:
        mm = arm_metrics(arm, metric_sources, ref_flux=ref_flux)
        if ref_flux is None:
            ref_flux = mm["_flux"]
        metrics.append(mm)
    summarize(arms, metrics, os.path.join(outdir, "summary.md"))
    if do_plot:
        plot_panels(arms, sources, os.path.join(outdir, "ab_panels.png"))
        plot_zoom_cutouts(arms, sources,
                          os.path.join(outdir, "ab_zoom_cutouts.png"),
                          valid=valid_in, half=zoom_half, nsrc=zoom_nsrc)
    return arms, metrics


def _make_synthetic(path, shape=(400, 400), seed=13):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    sky = 5.0 + 0.02 * xx + 0.015 * yy
    sci = (sky + 0.05 * rng.standard_normal(shape)).astype(np.float32)
    for (y, x, a) in [(90, 110, 60), (250, 300, 45), (320, 80, 40),
                      (160, 210, 55)]:
        sci += (a * np.exp(-((yy - y) ** 2 + (xx - x) ** 2)
                           / (2 * 8.0 ** 2))).astype(np.float32)
    err = (0.05 * np.ones(shape)).astype(np.float32)
    sci[:8, :] = np.nan
    err[:8, :] = np.nan
    fits.HDUList([fits.PrimaryHDU(),
                  fits.ImageHDU(sci, name="SCI"),
                  fits.ImageHDU(err, name="ERR")]).writeto(path, overwrite=True)


def selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "synthetic_i2d.fits")
        _make_synthetic(path)
        outdir = os.path.join(td, "figs")
        arms, metrics = run(path, ["tiered", "moransi:40", "moransi:60"],
                            patch=20, kernel="queen", do_plot=True,
                            outdir=outdir)
        assert len(arms) == 3 and len(metrics) == 3
        for mm in metrics:
            assert np.isfinite(mm["bkg_width"]) and mm["bkg_width"] > 0
        # higher percentile keeps more background -> fewer masked pixels
        assert metrics[2]["masked_frac"] < metrics[1]["masked_frac"]
        assert os.path.exists(os.path.join(outdir, "summary.md"))
        assert os.path.exists(os.path.join(outdir, "ab_panels.png"))
    print("\nSELFTEST OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mosaic", nargs="?", help="mosaic i2d FITS (SCI+ERR)")
    ap.add_argument("--arms", nargs="+",
                    default=["tiered", "moransi:40", "moransi:50", "moransi:60"])
    ap.add_argument("--patch", type=int, default=20)
    ap.add_argument("--kernel", default="queen")
    ap.add_argument("--plot", action="store_true")
    ap.add_argument("--outdir", default=os.path.join(HERE, "figs"))
    ap.add_argument("--zoom-half", type=int, default=100,
                    help="half-size (px) of per-source zoom cutouts")
    ap.add_argument("--zoom-nsrc", type=int, default=6,
                    help="number of interior bright sources to zoom on")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    if not a.mosaic:
        ap.error("provide a mosaic path or --selftest")
    run(a.mosaic, a.arms, a.patch, a.kernel, a.plot, a.outdir,
        zoom_half=a.zoom_half, zoom_nsrc=a.zoom_nsrc)


if __name__ == "__main__":
    main()
