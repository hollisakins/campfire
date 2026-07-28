import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from astropy import stats as astrostats
from scipy.ndimage import gaussian_filter
import metrics, nonneg
from run_cutouts import build_masker
from ms_trough2 import iterated_trough

rng = np.random.default_rng(7)
N = 1333; SIG = 0.012; SKY0 = 0.003
yy, xx = np.mgrid[0:N, 0:N] / N
sky = SKY0 * (1 + 0.5 * xx + 0.3 * yy - 0.4 * xx * yy)
noise = gaussian_filter(rng.normal(size=(N, N)), 1.5, mode="wrap")
noise *= SIG / noise.std()
src = np.zeros((N, N))
for _ in range(150):
    y0, x0 = rng.uniform(20, N - 20, 2)
    amp = rng.lognormal(np.log(5 * SIG), 1.0); s = rng.uniform(1.5, 4.0)
    y1, y2, x1, x2 = int(y0)-20, int(y0)+20, int(x0)-20, int(x0)+20
    dy = np.arange(y1, y2)[:, None] - y0; dx = np.arange(x1, x2)[None, :] - x0
    src[y1:y2, x1:x2] += amp * np.exp(-(dy**2 + dx**2) / (2*s*s))
sci = (sky + noise + src).astype(np.float32)
err = np.full_like(sci, SIG); off = np.zeros_like(sci, bool)

masker = build_masker()
mask, _ = masker.mask_from_arrays(sci, err)
excl = metrics.compact_source_mask(sci, off) | off
bkg = masker.estimate_background(sci, mask)
ceil_ms, _ = nonneg.multiscale_ceiling(sci, off, bkg.background, mask)
ms_map, _, _ = nonneg.cap_mesh(bkg, ceil_ms, sci.shape)
final = iterated_trough(sci, ms_map, excl)
d = final - bkg.background
print(f"final - unconstrained map: median={np.median(d):.3e} "
      f"({np.median(d)/SIG:+.4f} sig_pix), p1={np.percentile(d,1):.3e} "
      f"p99={np.percentile(d,99):.3e}, mean={d.mean():.3e}")
print(f"map error vs truth: constrained median={np.median(final-sky):.3e} "
      f"unconstrained median={np.median(bkg.background-sky):.3e}")
