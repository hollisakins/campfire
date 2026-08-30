import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from astropy.io import fits
import metrics
from run_120 import CUTOUTS_120, load

# ---------- core zoom: inter-BCG lane ----------
d = np.load("out120dw/core/arms.npz")
sci, err, wht = load(CUTOUTS_120["core"][0])
excl = d["excl"]
y0, y1, x0, x1 = 2000, 3000, 1200, 2600
fig, axes = plt.subplots(1, 4, figsize=(18, 4.8), dpi=110)
for i, arm in enumerate(["after", "m10", "m10_t0_guard"]):
    neg = metrics.negative_structure(d[f"resid_{arm}"], excl, err=err)
    ax = axes[i]
    ax.imshow(neg.signif_map[y0:y1, x0:x1], origin="lower", cmap="RdBu_r",
              vmin=-6, vmax=6, extent=[x0, x1, y0, y1])
    ax.contour(excl[y0:y1, x0:x1], levels=[0.5], colors="k",
               linewidths=0.4, extent=[x0, x1, y0, y1])
    ax.set_title(f"{arm}: signif + excl (black)", fontsize=9)
    ax.set_xticks([]); ax.set_yticks([])
from astropy.visualization import AsinhStretch, ImageNormalize, PercentileInterval
norm = ImageNormalize(sci, interval=PercentileInterval(99.5),
                      stretch=AsinhStretch(0.05))
axes[3].imshow(sci[y0:y1, x0:x1], origin="lower", cmap="Greys_r", norm=norm,
               extent=[x0, x1, y0, y1])
axes[3].contour(excl[y0:y1, x0:x1], levels=[0.5], colors="#d62728",
                linewidths=0.4, extent=[x0, x1, y0, y1])
axes[3].set_title("SCI + excl (red)", fontsize=9)
axes[3].set_xticks([]); axes[3].set_yticks([])
fig.suptitle("core: inter-BCG lane zoom")
fig.tight_layout()
fig.savefig("out120dw/core/zoom_lane.png")
print("saved out120dw/core/zoom_lane.png")

# fraction of the deep m10 lane trough that is excl-blinded
neg_m10 = metrics.negative_structure(d["resid_m10"], excl, err=err)
sub = neg_m10.signif_map[y0:y1, x0:x1]
deep = np.isfinite(sub) & (sub < -2)
print(f"[core lane zoom] px with signif<-2 (m10): {deep.sum()}, "
      f"of which inside excl-dilated border... excl frac in zoom: "
      f"{excl[y0:y1, x0:x1].mean():.3f}")

# ---------- offcluster edges: strip + border overshoot ----------
d2 = np.load("out120dw/offcluster/arms.npz")
sci2, err2, wht2 = load(CUTOUTS_120["offcluster"][0])
excl2 = d2["excl"]; off2 = d2["off"]
stats = {}
for arm in ["m10", "m10_t0", "m10_t0_guard"]:
    neg = metrics.negative_structure(d2[f"resid_{arm}"], excl2, err=err2)
    s = neg.signif_map
    ok = np.isfinite(s) & ~excl2
    N = s.shape[0]
    border = np.zeros_like(ok); b = 60
    border[:b, :] = border[-b:, :] = border[:, :b] = border[:, -b:] = True
    # shallow strip: use WHT to define it (below 60% of median weight)
    wmed = np.nanmedian(wht2[wht2 > 0])
    strip = (wht2 > 0) & (wht2 < 0.6 * wmed) & ~border
    interior = ok & ~border & ~strip
    stats[arm] = (np.nanmedian(s[ok & strip]), np.nanmedian(s[ok & border]),
                  np.nanmedian(s[interior]))
    print(f"[offcluster] {arm}: median signif strip={stats[arm][0]:+.2f} "
          f"border={stats[arm][1]:+.2f} interior={stats[arm][2]:+.2f} "
          f"(strip px={int((ok&strip).sum())})")
