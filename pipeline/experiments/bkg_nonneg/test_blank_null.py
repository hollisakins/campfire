"""Blank-field null test (success criterion #3): on a source-free tile the
constrained machinery must (a) leave the background model within noise of
the unconstrained fit, and (b) inject no positive pedestal.

Synthetic blank tile: smooth sky gradient + correlated noise (white noise
gaussian-smoothed to mimic drizzle correlation, rescaled to a target
per-pixel sigma), plus a sprinkling of faint compact sources so the tiered
mask has something realistic to chew on.
"""

import os
import sys

import numpy as np
from astropy import stats as astrostats
from scipy.ndimage import gaussian_filter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics
import nonneg
from run_cutouts import build_masker

rng = np.random.default_rng(7)
N = 1333
SIG = 0.012          # per-pixel sigma, matches the A2744 cutouts
SKY0 = 0.003

yy, xx = np.mgrid[0:N, 0:N] / N
sky = SKY0 * (1 + 0.5 * xx + 0.3 * yy - 0.4 * xx * yy)  # smooth gradient
noise = gaussian_filter(rng.normal(size=(N, N)), 1.5, mode="wrap")
noise *= SIG / noise.std()

# faint compact sources (gaussian blobs)
src = np.zeros((N, N))
for _ in range(150):
    y0, x0 = rng.uniform(20, N - 20, 2)
    amp = rng.lognormal(np.log(5 * SIG), 1.0)
    s = rng.uniform(1.5, 4.0)
    y1, y2 = int(y0) - 20, int(y0) + 20
    x1, x2 = int(x0) - 20, int(x0) + 20
    dy = np.arange(y1, y2)[:, None] - y0
    dx = np.arange(x1, x2)[None, :] - x0
    src[y1:y2, x1:x2] += amp * np.exp(-(dy**2 + dx**2) / (2 * s * s))

sci = (sky + noise + src).astype(np.float32)
err = np.full_like(sci, SIG)
off = np.zeros_like(sci, bool)

masker = build_masker()
mask_final, bitmask = masker.mask_from_arrays(sci, err)
excl = metrics.compact_source_mask(sci, off) | off

bkg10 = masker.estimate_background(sci, mask_final)
base_map = bkg10.background

# constrained: multiscale ceiling meshcap + trough pass
ceil_ms, cal = nonneg.multiscale_ceiling(sci, off, base_map, mask_final)
ms_map, ncap, nmesh = nonneg.cap_mesh(bkg10, ceil_ms, sci.shape)
corr, cfrac = nonneg.trough_correction(sci, ms_map, excl)
final_map = ms_map + corr

d_map = final_map - base_map
d_sky = final_map - sky

print(f"mask fraction: {mask_final.mean():.3f}, mesh cells capped: "
      f"{ncap}/{nmesh}, trough-corrected area: {cfrac:.4f}")
print(f"constrained - unconstrained map: median={np.median(d_map):.3e} "
      f"(= {np.median(d_map)/SIG:.4f} sigma_pix), "
      f"p1={np.percentile(d_map,1):.3e} p99={np.percentile(d_map,99):.3e}")
print(f"constrained map - true sky: median={np.median(d_sky):.3e} "
      f"robust sigma={astrostats.biweight_scale(d_sky):.3e}")
print(f"unconstrained map - true sky: "
      f"median={np.median(base_map - sky):.3e} "
      f"robust sigma={astrostats.biweight_scale(base_map - sky):.3e}")

for r, (resid, label) in enumerate(
    [(sci - base_map, "baseline"), (sci - final_map, "constrained")]):
    ap = metrics.empty_aperture_stats(resid - src, excl, (33,))
    a = ap[33]
    print(f"{label}: 1\" empty-aperture median={a.median:+.4e} "
          f"sigma={a.sigma:.4e} mean_signif={a.mean_signif:+.2f}")
