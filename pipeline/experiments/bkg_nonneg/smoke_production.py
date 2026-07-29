"""Smoke test: the shipped bkgsub.py guard on the real 120as cutouts,
scored with the experiment metrics (expect ~ the prototype numbers)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from astropy import stats as astrostats
import metrics
from run_120 import CUTOUTS_120, load
from campfire_pipeline.nircam.bkgsub import SubtractBackground

for name in ["core", "offcluster"]:
    sb = SubtractBackground(
        ring_downsample=4,
        bg_box_size=10, bg_filter_size=5, bg_sigma=3,
        bg_guard=True,   # mosaic default; tier lists = new tier-0-less defaults
    )
    sub, mask, bitmask = sb.compute(CUTOUTS_120[name][0])
    _, err, wht = load(CUTOUTS_120[name][0])
    off = (bitmask & 1) != 0
    # score exactly like out120v3: sigma_wht + per-arm residual exclusion
    good = np.isfinite(wht) & (wht > 0)
    sqw = np.sqrt(np.where(good, wht, np.nan))
    s = float(astrostats.biweight_scale((sub * sqw)[~mask & ~off]))
    sig_wht = np.where(good, s / sqw, np.nan)
    with np.errstate(invalid="ignore"):
        req = sub / sig_wht
    excl = metrics.compact_source_mask(
        np.where(np.isfinite(req), req, 0.0), off) | off
    neg = metrics.negative_structure(sub, excl, err=sig_wht)
    ap = metrics.empty_aperture_stats(sub, excl, (33,))[33]
    print(f"[{name}] PRODUCTION GUARD: neg n={neg.n_neg} "
          f"area={neg.area_neg}px min={neg.min_signif:.1f}sig "
          f"apmed33={ap.median:+.4f}")
