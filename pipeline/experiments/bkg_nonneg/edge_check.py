import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import metrics
from run_120 import CUTOUTS_120, load

d2 = np.load("out120dw/offcluster/arms.npz")
sci2, err2, wht2 = load(CUTOUTS_120["offcluster"][0])
excl2 = d2["excl"]
wmed = np.nanmedian(wht2[wht2 > 0])
N = 4000; b = 60
border = np.zeros((N, N), bool)
border[:b, :] = border[-b:, :] = border[:, :b] = border[:, -b:] = True
strip = (wht2 > 0) & (wht2 < 0.6 * wmed) & ~border
for arm in ["m10", "m10_t0_guard"]:
    s = metrics.negative_structure(d2[f"resid_{arm}"], excl2, err=err2).signif_map
    ok = np.isfinite(s) & ~excl2
    for label, m in [("strip", strip), ("border", border), 
                     ("interior", ~strip & ~border)]:
        sel = ok & m
        print(f"{arm:14s} {label:8s}: med={np.nanmedian(s[sel]):+.2f} "
              f"f(>+2)={np.mean(s[sel]>2):.4f} f(<-2)={np.mean(s[sel]<-2):.4f}")
