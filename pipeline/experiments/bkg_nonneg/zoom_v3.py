import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import metrics
from run_120 import CUTOUTS_120, load

d = np.load("out120v3/core/arms.npz")
sci, err, wht = load(CUTOUTS_120["core"][0])
off = d["off"]; sw = d["sig_wht"]
y0, y1, x0, x1 = 2000, 3000, 1200, 2600
fig, axes = plt.subplots(1, 3, figsize=(15, 5.0), dpi=110)
for i, arm in enumerate(["after", "m10", "m10_t0_guard"]):
    r = d[f"resid_{arm}"]
    with np.errstate(invalid="ignore"):
        req = r / sw
    excl_arm = metrics.compact_source_mask(
        np.where(np.isfinite(req), req, 0.0), off) | off
    neg = metrics.negative_structure(r, excl_arm, err=sw)
    ax = axes[i]
    ax.imshow(neg.signif_map[y0:y1, x0:x1], origin="lower", cmap="RdBu_r",
              vmin=-8, vmax=8, extent=[x0, x1, y0, y1])
    ax.contour(excl_arm[y0:y1, x0:x1], levels=[0.5], colors="k",
               linewidths=0.35, extent=[x0, x1, y0, y1])
    sub = neg.signif_map[y0:y1, x0:x1]
    fin = np.isfinite(sub)
    ax.set_title(f"{arm}: min={np.nanmin(sub):.1f}σ "
                 f"area(<-2σ)={int((fin&(sub<-2)).sum())}px", fontsize=10)
    ax.set_xticks([]); ax.set_yticks([])
fig.suptitle("core inter-BCG lane, WHT-noise + residual-based exclusion")
fig.tight_layout()
fig.savefig("out120v3/core/zoom_lane.png")
print("saved out120v3/core/zoom_lane.png")
