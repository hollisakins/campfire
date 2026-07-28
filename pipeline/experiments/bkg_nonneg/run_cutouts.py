"""Characterize + prototype negativity-constrained bkg subtraction on real
A2744 F444W cutouts (30 mas mosaic pixels, SCI+ERR, pre-mosaic-bkgsub).

Round 1 arms:
  input     : cutout minus a constant robust pedestal (reference: what
              negativity is already baked in from the per-exposure stage)
  mosaic10  : current mosaic-level fit (box 10, filter 5, sym 3-sigma clip)
  perexp64  : per-exposure-like fit (box 64, extra_dilate 20, reject on) --
              the [nircam.bkg.bkg2d] cluster path in mosaic pixels

Usage:
  conda run -n campfire python run_cutouts.py --out out
"""

import argparse
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from astropy import stats as astrostats
from astropy.io import fits
from astropy.visualization import AsinhStretch, ImageNormalize, PercentileInterval
from scipy.ndimage import distance_transform_edt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics  # noqa: E402
import nonneg  # noqa: E402

from campfire_pipeline.nircam.bkgsub import SubtractBackground  # noqa: E402

CUTOUTS = {
    "offcluster": "/Users/hollis/Downloads/a2744_f444w_offcluster_galaxy_before_40as.fits",
    "core": "/Users/hollis/Downloads/a2744_f444w_cluster_core_before_40as.fits",
}

# arcsec per px (30 mas)
PIXSCALE = 0.03
AP_DIAMS = (10, 20, 33, 50)  # px: 0.3, 0.6, 1.0, 1.5 arcsec


def build_masker(**over) -> SubtractBackground:
    """Mosaic-style config sans tier 0 (30k px / 600 px dilation are sized
    for a full tile, not a 40 arcsec cutout)."""
    kw = dict(
        ring_radius_in=80, ring_width=4, ring_downsample=4,
        ring_clip_max_sigma=5.0, ring_clip_box_size=100,
        ring_clip_filter_size=3,
        tier_kernel_size=[25, 15, 5, 2],
        tier_npixels=[15, 10, 3, 1],
        tier_nsigma=[1.5, 1.5, 1.5, 1.5],
        tier_dilate_size=[33, 25, 21, 19],
        bg_box_size=10, bg_filter_size=5, bg_exclude_percentile=90,
        bg_sigma=3, bg_interpolator="zoom",
    )
    kw.update(over)
    return SubtractBackground(**kw)


def dilate_source_tiers(bitmask: np.ndarray, extra: float) -> np.ndarray:
    """Grow the source tiers (bits 1+) by `extra` px; keep bit 0 ungrown."""
    src = (bitmask >> 1) != 0
    if extra > 0:
        src = distance_transform_edt(~src) <= extra
    return src | ((bitmask & 1) != 0)


def run_arms(sci, err, out, name):
    os.makedirs(out, exist_ok=True)
    off = np.isnan(err) | np.isnan(sci)

    # ---- pipeline source mask (shared across fit arms) -------------------
    masker = build_masker()
    mask_final, bitmask = masker.mask_from_arrays(sci, err)
    tier_frac = {f"tier{b}": float(((bitmask >> b) & 1).mean())
                 for b in range(5)}
    print(f"[{name}] mask fractions: total={mask_final.mean():.3f} "
          + " ".join(f"{k}={v:.3f}" for k, v in tier_frac.items()))

    # what tier 0 WOULD do on this cutout (mosaic config) — log only
    masker_t0 = build_masker(
        tier_kernel_size=[25, 25, 15, 5, 2],
        tier_npixels=[30000, 15, 10, 3, 1],
        tier_nsigma=[100.0, 1.5, 1.5, 1.5, 1.5],
        tier_dilate_size=[600, 33, 25, 21, 19],
    )
    _, bm_t0 = masker_t0.mask_from_arrays(sci, err)
    print(f"[{name}] tier0 (mosaic cfg) would mask "
          f"{(((bm_t0 >> 1) & 1).mean()):.3f} of the cutout")

    # ---- metrics-internal exclusion mask ---------------------------------
    excl = metrics.compact_source_mask(sci, off) | off
    print(f"[{name}] metrics source-exclusion fraction: {excl.mean():.3f}")

    arms = {}

    # input: constant pedestal only
    ped = astrostats.biweight_location(sci[~mask_final])
    arms["input"] = (sci - ped, None)
    print(f"[{name}] constant pedestal = {ped:.5g} MJy/sr")

    # mosaic-level fit, box 10
    bkg10 = masker.estimate_background(sci, mask_final)
    arms["mosaic10"] = (sci - bkg10.background, bkg10.background)

    # per-exposure-like fit, box 64 (mosaic px), extra_dilate 20, reject on
    m64 = build_masker(bg_box_size=64, bg_reject=True)
    mask64 = dilate_source_tiers(bitmask, 20)
    bkg64 = m64.estimate_background(sci, mask64)
    arms["perexp64"] = (sci - bkg64.background, bkg64.background)

    # --- constrained arms -------------------------------------------------
    # asymmetric clip in the main fit (mask held fixed)
    masym = nonneg.AsymmetricSubtractBackground(
        **{k: getattr(masker, k) for k in (
            "bg_box_size", "bg_filter_size", "bg_exclude_percentile",
            "bg_sigma", "bg_interpolator")})
    bkg_asym = masym.estimate_background(sci, mask_final)
    arms["mosaic10_asym"] = (sci - bkg_asym.background, bkg_asym.background)

    # one-sided ceiling (shared across enforcement arms)
    onesided = nonneg.onesided_ceiling_map(sci, off, box=64)
    ceil10, d10, s10 = nonneg.calibrate_ceiling(
        onesided, bkg10.background, mask_final, k=2.0)
    print(f"[{name}] ceiling calib vs mosaic10: delta={d10:.5g} "
          f"sigma_diff={s10:.5g}")

    cl_map, viol = nonneg.clamp_map(bkg10.background, ceil10)
    print(f"[{name}] mosaic10 clamp engaged on {viol.mean():.3f} of pixels, "
          f"max violation={np.nanmax(bkg10.background - ceil10):.5g}")
    arms["mosaic10_clamp"] = (sci - cl_map, cl_map)

    mc_map, ncap, nmesh = nonneg.cap_mesh(bkg10, ceil10, sci.shape)
    print(f"[{name}] mosaic10 meshcap: {ncap}/{nmesh} mesh cells capped")
    arms["mosaic10_meshcap"] = (sci - mc_map, mc_map)

    # multi-scale ceiling for the fine-box fit
    ceil_ms, cal = nonneg.multiscale_ceiling(
        sci, off, bkg10.background, mask_final, boxes=(32, 64, 128), k=2.0)
    print(f"[{name}] multiscale ceiling calib: " + " ".join(
        f"b{b}: d={d:.5f} s={s:.5f};" for b, (d, s) in cal.items()))
    ms_map, ncap_ms, _ = nonneg.cap_mesh(bkg10, ceil_ms, sci.shape)
    print(f"[{name}] mosaic10 msceil: {ncap_ms} mesh cells capped")
    arms["mosaic10_msceil"] = (sci - ms_map, ms_map)

    # residual-driven trough pass composed on the capped fine-box map
    corr, cfrac = nonneg.trough_correction(sci, ms_map, excl)
    print(f"[{name}] trough pass on msceil: corrected {cfrac:.4f} of area, "
          f"min corr={corr.min():.5g}")
    arms["mosaic10_msceil_tr"] = (sci - (ms_map + corr), ms_map + corr)

    ceil64, d64, s64 = nonneg.calibrate_ceiling(
        onesided, bkg64.background, mask64, k=2.0)
    mc64_map, ncap64, nmesh64 = nonneg.cap_mesh(bkg64, ceil64, sci.shape)
    print(f"[{name}] perexp64 meshcap: {ncap64}/{nmesh64} mesh cells capped "
          f"(delta={d64:.5g} sigma_diff={s64:.5g})")
    arms["perexp64_meshcap"] = (sci - mc64_map, mc64_map)

    corr64, cfrac64 = nonneg.trough_correction(sci, mc64_map, excl)
    print(f"[{name}] trough pass on perexp64_meshcap: corrected "
          f"{cfrac64:.4f} of area, min corr={corr64.min():.5g}")
    arms["perexp64_cap_tr"] = (sci - (mc64_map + corr64), mc64_map + corr64)

    # ---- metrics ---------------------------------------------------------
    rows = []
    results = {}
    for arm, (resid, bmap) in arms.items():
        ap = metrics.empty_aperture_stats(resid, excl, AP_DIAMS)
        neg = metrics.negative_structure(resid, excl)
        rows.append(metrics.summarize(arm, ap, neg))
        results[arm] = (resid, bmap, ap, neg)
        print(f"[{name}] {arm}: neg n={neg.n_neg} area={neg.area_neg}px "
              f"(pos control n={neg.n_pos} area={neg.area_pos}px) "
              f"min_signif={neg.min_signif:.1f}")

    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump([{k: (float(v) if isinstance(v, (int, float, np.floating))
                        else v) for k, v in r.items()} for r in rows],
                  f, indent=2)

    plot_maps(sci, excl, mask_final, results, out, name)
    plot_profiles(results, out, name)
    return rows


def plot_maps(sci, excl, mask_final, results, out, name):
    arms = list(results)
    nrow = len(arms)
    fig, axes = plt.subplots(nrow, 3, figsize=(13, 4 * nrow), dpi=110)
    axes = np.atleast_2d(axes)
    norm = ImageNormalize(sci, interval=PercentileInterval(99.5),
                          stretch=AsinhStretch(0.05))
    sig_pix = astrostats.biweight_scale(results["input"][0][~excl])
    for i, arm in enumerate(arms):
        resid, bmap, ap, neg = results[arm]
        ax = axes[i, 0]
        ax.imshow(resid, origin="lower", cmap="RdBu_r",
                  vmin=-3 * sig_pix, vmax=3 * sig_pix)
        ax.set_title(f"{arm}: residual (±3σ_pix)")
        ax = axes[i, 1]
        im = ax.imshow(neg.signif_map, origin="lower", cmap="RdBu_r",
                       vmin=-6, vmax=6)
        ax.set_title(f"{arm}: smoothed significance")
        plt.colorbar(im, ax=ax, fraction=0.045)
        ax = axes[i, 2]
        ax.imshow(sci, origin="lower", cmap="Greys_r", norm=norm)
        if neg.neg_segmap is not None:
            ax.contour(neg.neg_segmap > 0, levels=[0.5], colors="#d62728",
                       linewidths=1.0)
        ax.contour(mask_final, levels=[0.5], colors="#1f77b4",
                   linewidths=0.3, alpha=0.6)
        ax.set_title(f"{arm}: neg regions (red) on SCI")
        for a in axes[i]:
            a.set_xticks([]), a.set_yticks([])
    fig.suptitle(name)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "maps.png"))
    plt.close(fig)


def plot_profiles(results, out, name):
    bins = np.arange(0, 700, 50)
    d_show = 33  # 1.0 arcsec apertures
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), dpi=110)
    colors = {"input": "#555555", "mosaic10": "#1f77b4",
              "perexp64": "#d62728", "mosaic10_asym": "#2ca02c",
              "mosaic10_clamp": "#9467bd", "mosaic10_meshcap": "#ff7f0e",
              "mosaic10_msceil": "#e377c2", "perexp64_meshcap": "#8c564b",
              "mosaic10_msceil_tr": "#17becf", "perexp64_cap_tr": "#bcbd22"}
    for arm, (resid, bmap, ap, neg) in results.items():
        prof = metrics.radial_aperture_profile(ap[d_show], bins)
        c = colors.get(arm, None)
        axes[0].plot(prof["r_px"] * PIXSCALE, prof["median"], "-o", ms=3,
                     label=arm, color=c)
        lo = np.percentile(ap[d_show].fluxes, [1, 99])
        h = np.linspace(lo[0], lo[1], 60)
        axes[1].hist(ap[d_show].fluxes, bins=h, histtype="step",
                     density=True, label=arm, color=c)
    axes[0].axhline(0, color="k", lw=0.8)
    axes[0].set_xlabel("distance from cutout center [arcsec]")
    axes[0].set_ylabel(f'median aperture flux (d={d_show}px = '
                       f'{d_show*PIXSCALE:.1f}")')
    axes[0].legend(fontsize=8)
    axes[1].axvline(0, color="k", lw=0.8)
    axes[1].set_xlabel("aperture flux")
    axes[1].set_ylabel("density")
    axes[1].legend(fontsize=8)
    fig.suptitle(f"{name}: empty-aperture statistics")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "profiles.png"))
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="out")
    p.add_argument("--cutouts", default="offcluster,core")
    args = p.parse_args()
    all_rows = {}
    for name in args.cutouts.split(","):
        path = CUTOUTS[name]
        with fits.open(path) as hdu:
            sci = hdu["SCI"].data.astype(float)
            err = hdu["ERR"].data.astype(float)
        rows = run_arms(sci, err, os.path.join(args.out, name), name)
        all_rows[name] = rows
    print(json.dumps(all_rows, indent=2, default=float))


if __name__ == "__main__":
    main()
