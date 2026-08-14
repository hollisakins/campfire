#!/usr/bin/env python
"""Validation figures for the mask-grade downsample experiment.

Figure 1 (mask_comparison.png): a spike-arm zoom of the 0.9 um SW model at
one representative (factor, level), showing per-pixel agreement of the
round-tripped coarse-grade mask with the full-res mask, for mean- vs
max-pooling. Red = full-res masked but coarse grade missed (under-mask,
the dangerous direction); blue = coarse grade adds (safe overshoot).

Figure 2 (drift_summary.png): p99.9 drift vs isophote level per factor,
2x2 small multiples — rows = SW 0.9 um / LW 4.4 um, cols = mean-pool miss
/ max-pool over — with the grow tolerance and the f*sqrt(2) block-diagonal
bound for reference.

Usage:
    conda run -n campfire python plots.py \
        --model-dir /path/to/nircam_psf_scatlight_models --out ./out
"""

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from astropy.io import fits

# dataviz reference palette (light mode)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e8e7e3"
RED = "#e34948"    # under-mask (serious)
BLUE = "#2a78d6"   # overshoot (safe)
# ordinal blue ramp steps 250/350/450/600 for factors 2/4/8/16
FACTOR_COLORS = {2: "#86b6ef", 4: "#5598e7", 8: "#2a78d6", 16: "#184f95"}

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "text.color": INK, "axes.edgecolor": INK2, "axes.labelcolor": INK,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False,
    "font.size": 9,
})


def block_reduce(img, f, agg):
    n = img.shape[0]
    m = (n // f) * f
    o = (n - m) // 2
    c = img[o:o + m, o:o + m].reshape(m // f, f, m // f, f)
    out = c.mean(axis=(1, 3)) if agg == "mean" else c.max(axis=(1, 3))
    return out, o, m


def fig_mask_comparison(model_dir, out, factor=8, level=1e-8):
    name = "PSF+scatlight_0.9micron.fits"
    with fits.open(model_dir / name) as h:
        img = h[0].data
        pix = h[0].header["PIXELSCL"]
    n = img.shape[0]
    c = (n - 1) / 2

    # locate a diffraction arm: brightest pixel on the r ~ 45" ring
    yy, xx = np.indices((n, n))
    r = np.hypot(yy - c, xx - c) * pix
    ring = (r > 44) & (r < 46)
    iy, ix = np.unravel_index(np.argmax(np.where(ring, img, 0)), img.shape)
    theta = np.arctan2(iy - c, ix - c)

    # zoom box centered on the arm at r ~ 55" (fits arm width + tip region)
    r0 = 55 / pix
    cy, cx = int(c + r0 * np.sin(theta)), int(c + r0 * np.cos(theta))
    half = 250
    sl = np.s_[cy - half:cy + half, cx - half:cx + half]

    fig, axes = plt.subplots(1, 3, figsize=(12.5, 4.6))

    # context panel: full model, log stretch, zoom box
    ax = axes[0]
    stretch = np.log10(np.maximum(img[::8, ::8], 1e-13))
    ax.imshow(stretch, cmap="gray_r", origin="lower",
              extent=[0, n, 0, n], vmin=-12, vmax=-4)
    ax.add_patch(plt.Rectangle((cx - half, cy - half), 2 * half, 2 * half,
                               fill=False, edgecolor=RED, linewidth=1.2))
    ax.set_title("0.9 μm SW model (log stretch, 0.0311″/px)",
                 fontsize=10, color=INK)
    ax.grid(False)

    full_mask = img >= level
    for ax, agg in zip(axes[1:], ["mean", "max"]):
        ds, o, m = block_reduce(img, factor, agg)
        up = np.repeat(np.repeat(ds, factor, 0), factor, 1)
        coarse = np.zeros_like(full_mask)
        coarse[o:o + m, o:o + m] = up >= level

        base = np.log10(np.maximum(img[sl], 1e-13))
        ax.imshow(base, cmap="gray_r", origin="lower", vmin=-12, vmax=-5)

        overlay = np.zeros((*base.shape, 4))
        both = full_mask[sl] & coarse[sl]
        miss = full_mask[sl] & ~coarse[sl]
        add = coarse[sl] & ~full_mask[sl]
        n_miss, n_add = int(miss.sum()), int(add.sum())
        # display-only dilation: the ridge above threshold is 1-2 px wide,
        # unreadable at figure scale; counts stay undilated
        from scipy.ndimage import binary_dilation
        show = {k: binary_dilation(v, iterations=2)
                for k, v in [("both", both), ("miss", miss), ("add", add)]}
        overlay[show["both"]] = matplotlib.colors.to_rgba(INK2, 0.45)
        overlay[show["add"]] = matplotlib.colors.to_rgba(BLUE, 0.85)
        overlay[show["miss"]] = matplotlib.colors.to_rgba(RED, 0.95)
        ax.imshow(overlay, origin="lower")
        ax.set_title(f"block-{agg} ×{factor}, isophote {level:.0e}",
                     fontsize=10, color=INK)
        ax.text(0.02, 0.98,
                f"missed {n_miss:,} px\nadded {n_add:,} px",
                transform=ax.transAxes, va="top", fontsize=9, color=INK)
        ax.set_xticks([]); ax.set_yticks([]); ax.grid(False)

    handles = [
        plt.Rectangle((0, 0), 1, 1, color=matplotlib.colors.to_rgba(INK2, 0.35)),
        plt.Rectangle((0, 0), 1, 1, color=RED),
        plt.Rectangle((0, 0), 1, 1, color=BLUE),
    ]
    fig.legend(handles,
               ["masked by both", "missed by coarse grade (under-mask)",
                "added by coarse grade (overshoot) — overlays display-dilated ×2"],
               loc="lower center", ncol=3, frameon=False, fontsize=9)
    fig.suptitle("Round-tripped coarse-grade mask vs full-resolution mask "
                 "(zoom on one diffraction arm)", fontsize=11, color=INK)
    fig.tight_layout(rect=[0, 0.06, 1, 0.95])
    p = out / "mask_comparison.png"
    fig.savefig(p, dpi=160)
    plt.close(fig)
    print("wrote", p)


def fig_drift_summary(out):
    res = {}
    for agg in ["mean", "max"]:
        res[agg] = json.load(open(out / f"drift_results_{agg}.json"))

    panels = [("PSF+scatlight_0.9micron.fits", "SW 0.9 μm (0.0311″/px)"),
              ("PSF+scatlight_4.4micron.fits", "LW 4.4 μm (0.063″/px)")]
    cols = [("mean", "miss_p999", "block-MEAN: missed footprint (under-mask)"),
            ("max", "over_p999", "block-MAX: overshoot (safe direction)")]

    fig, axes = plt.subplots(2, 2, figsize=(9.5, 6.6), sharex=True)
    for i, (model, row_label) in enumerate(panels):
        for j, (agg, key, col_label) in enumerate(cols):
            ax = axes[i, j]
            ends = []
            for f in [2, 4, 8, 16]:
                recs = [r for r in res[agg]
                        if r["model"] == model and r["factor"] == f]
                recs.sort(key=lambda r: r["level"])
                lv = [r["level"] for r in recs]
                dr = [max(r[key], 0.3) for r in recs]  # 0.3 = log-floor for 0
                ax.plot(lv, dr, color=FACTOR_COLORS[f], linewidth=2,
                        marker="o", markersize=3.5)
                ends.append((f, lv[-1], dr[-1]))
            # dodge end labels vertically by final-value rank so converging
            # curves don't stack their labels
            for rank, (f, x1, y1) in enumerate(
                    sorted(ends, key=lambda e: e[2], reverse=True)):
                ax.annotate(f"×{f}", (x1, y1),
                            xytext=(6, 8 - 6 * rank), textcoords="offset points",
                            color=FACTOR_COLORS[f], fontsize=8, va="center")
            ax.set_xscale("log"); ax.set_yscale("log")
            ax.set_ylim(0.2, 4000)
            ax.axhline(2, color=INK2, linewidth=1, linestyle=":")
            if j == 1:
                for f in [2, 4, 8, 16]:
                    ax.axhline(f * np.sqrt(2), color=FACTOR_COLORS[f],
                               linewidth=0.8, linestyle="--", alpha=0.5)
            if i == 0:
                ax.set_title(col_label, fontsize=10, color=INK)
            if j == 0:
                ax.set_ylabel(f"{row_label}\np99.9 drift (raw px)")
            if i == 1:
                ax.set_xlabel("isophote level (flux fraction / raw px)")
    axes[0, 0].annotate("grow = 2 px tolerance", (1.3e-11, 2.3),
                        fontsize=8, color=INK2)
    axes[0, 1].annotate("max-pool never under-masks:\nmiss ≡ 0 at every level\n"
                        "(dashed: f·√2 block-diagonal bound)",
                        (1.3e-11, 300), fontsize=8, color=INK2)
    fig.suptitle("Isophote drift of the round-tripped coarse grade, "
                 "by downsample factor", fontsize=11, color=INK)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    p = out / "drift_summary.png"
    fig.savefig(p, dpi=160)
    plt.close(fig)
    print("wrote", p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("out"))
    args = ap.parse_args()
    fig_drift_summary(args.out)
    fig_mask_comparison(args.model_dir, args.out)
