#!/usr/bin/env python
"""Continuity check: does the mask-grade mask have holes along the arm?

Shows the ACTUAL mask each grade produces (not the diff): full-res vs
block-max x4 vs x8, same zoom as mask_comparison.png, with connected-
component counts over the window. The full-res isophote is intrinsically
beaded (fringe oscillations along the arm cross a fixed threshold), and
block-max bridges gaps smaller than its cell -- the coarse mask should be
MORE contiguous, never less covering.

Usage: conda run -n campfire python mask_continuity.py \
           --model-dir <raw_dir> --out ./out
"""

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from astropy.io import fits
from scipy.ndimage import label

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
BLUE = "#2a78d6"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "text.color": INK,
    "axes.edgecolor": INK2, "axes.labelcolor": INK,
    "xtick.color": INK2, "ytick.color": INK2, "font.size": 9,
})


def block_max_mask(img, f, level):
    n = img.shape[0]
    m = (n // f) * f
    o = (n - m) // 2
    ds = img[o:o + m, o:o + m].reshape(m // f, f, m // f, f).max(axis=(1, 3))
    up = np.zeros_like(img, dtype=bool)
    up[o:o + m, o:o + m] = np.repeat(np.repeat(ds >= level, f, 0), f, 1)
    return up


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--level", type=float, default=1e-8)
    args = ap.parse_args()

    with fits.open(args.model_dir / "PSF+scatlight_0.9micron.fits") as h:
        img = h[0].data
        pix = h[0].header["PIXELSCL"]
    n = img.shape[0]
    c = (n - 1) / 2
    yy, xx = np.indices((n, n))
    r = np.hypot(yy - c, xx - c) * pix
    ring = (r > 44) & (r < 46)
    iy, ix = np.unravel_index(np.argmax(np.where(ring, img, 0)), img.shape)
    theta = np.arctan2(iy - c, ix - c)
    r0 = 55 / pix
    cy, cx = int(c + r0 * np.sin(theta)), int(c + r0 * np.cos(theta))
    half = 250
    sl = np.s_[cy - half:cy + half, cx - half:cx + half]

    cases = [("full resolution", (img >= args.level)[sl]),
             ("block-max ×4", block_max_mask(img, 4, args.level)[sl]),
             ("block-max ×8", block_max_mask(img, 8, args.level)[sl])]

    fig, axes = plt.subplots(1, 3, figsize=(12.5, 4.5))
    base = np.log10(np.maximum(img[sl], 1e-13))
    for ax, (title, mask) in zip(axes, cases):
        ncomp = label(mask)[1]
        ax.imshow(base, cmap="gray_r", origin="lower", vmin=-12, vmax=-5)
        overlay = np.zeros((*mask.shape, 4))
        overlay[mask] = matplotlib.colors.to_rgba(BLUE, 0.75)
        ax.imshow(overlay, origin="lower")
        ax.set_title(f"{title} — mask as applied", fontsize=10, color=INK)
        ax.text(0.02, 0.98,
                f"{ncomp} connected components\n{int(mask.sum()):,} px",
                transform=ax.transAxes, va="top", fontsize=9, color=INK)
        ax.set_xticks([]); ax.set_yticks([])
    fig.suptitle(f"The mask each grade actually produces "
                 f"(0.9 μm SW, isophote {args.level:.0e}, pre-grow)",
                 fontsize=11, color=INK)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    p = args.out / "mask_continuity.png"
    fig.savefig(p, dpi=160)
    print("wrote", p)
    for title, mask in cases:
        print(f"  {title:18s} components={label(mask)[1]:3d}  px={int(mask.sum()):,}")


if __name__ == "__main__":
    main()
