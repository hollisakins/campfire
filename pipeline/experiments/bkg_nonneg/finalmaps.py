# Compact comparison: input / baseline / final-constrained for both cutouts
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from astropy.io import fits
from astropy import stats as astrostats
import metrics, nonneg
from run_cutouts import CUTOUTS, build_masker, dilate_source_tiers

fig, axes = plt.subplots(2, 4, figsize=(17, 8.6), dpi=110)
for row, name in enumerate(["offcluster", "core"]):
    with fits.open(CUTOUTS[name]) as hdu:
        sci = hdu["SCI"].data.astype(float); err = hdu["ERR"].data.astype(float)
    off = np.isnan(err) | np.isnan(sci)
    masker = build_masker()
    mask_final, bitmask = masker.mask_from_arrays(sci, err)
    excl = metrics.compact_source_mask(sci, off) | off
    ped = astrostats.biweight_location(sci[~mask_final])

    m64 = build_masker(bg_box_size=64, bg_reject=True)
    mask64 = dilate_source_tiers(bitmask, 20)
    bkg64 = m64.estimate_background(sci, mask64)
    onesided = nonneg.onesided_ceiling_map(sci, off, box=64)
    ceil64, _, _ = nonneg.calibrate_ceiling(onesided, bkg64.background, mask64)
    cap_map, _, _ = nonneg.cap_mesh(bkg64, ceil64, sci.shape)
    corr, _ = nonneg.trough_correction(sci, cap_map, excl)
    final = cap_map + corr

    panels = [("input", sci - ped), ("perexp64 (current)", sci - bkg64.background),
              ("perexp64 cap+trough", sci - final)]
    for col, (label, resid) in enumerate(panels):
        neg = metrics.negative_structure(resid, excl)
        ax = axes[row, col]
        im = ax.imshow(neg.signif_map, origin="lower", cmap="RdBu_r", vmin=-5, vmax=5)
        ax.set_title(f"{name}: {label}\nneg area={neg.area_neg}px min={neg.min_signif:.1f}σ",
                     fontsize=9)
        ax.set_xticks([]); ax.set_yticks([])
    ax = axes[row, 3]
    d = (bkg64.background - final)
    im = ax.imshow(d, origin="lower", cmap="PuOr_r",
                   vmin=-np.nanpercentile(np.abs(d), 99.5),
                   vmax=np.nanpercentile(np.abs(d), 99.5))
    plt.colorbar(im, ax=ax, fraction=0.045)
    ax.set_title(f"{name}: bkg lowered by (map diff)", fontsize=9)
    ax.set_xticks([]); ax.set_yticks([])
fig.suptitle("per-exposure path: one-sided ceiling meshcap + residual trough pass")
fig.tight_layout()
fig.savefig("out/final_perexp.png")
print("saved out/final_perexp.png")
