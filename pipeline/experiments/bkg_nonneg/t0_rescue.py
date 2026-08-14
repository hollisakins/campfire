import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from astropy.io import fits
import metrics, nonneg
from run_cutouts import CUTOUTS, build_masker

with fits.open(CUTOUTS["offcluster"]) as hdu:
    sci = hdu["SCI"].data.astype(float); err = hdu["ERR"].data.astype(float)
off = np.isnan(err) | np.isnan(sci)
excl = metrics.compact_source_mask(sci, off) | off

masker_t0 = build_masker(
    tier_kernel_size=[25, 25, 15, 5, 2], tier_npixels=[30000, 15, 10, 3, 1],
    tier_nsigma=[100.0, 1.5, 1.5, 1.5, 1.5],
    tier_dilate_size=[600, 33, 25, 21, 19])
mask_t0, _ = masker_t0.mask_from_arrays(sci, err)
bkg = masker_t0.estimate_background(sci, mask_t0)
ceil_ms, _ = nonneg.multiscale_ceiling(sci, off, bkg.background, mask_t0)
ms_map, ncap, _ = nonneg.cap_mesh(bkg, ceil_ms, sci.shape)
corr, cfrac = nonneg.trough_correction(sci, ms_map, excl)
resid = sci - (ms_map + corr)
neg = metrics.negative_structure(resid, excl)
ap = metrics.empty_aperture_stats(resid, excl, (33,))[33]
print(f"t0-masked mosaic10 + msceil + trough: neg n={neg.n_neg} "
      f"area={neg.area_neg}px min={neg.min_signif:.1f}sig "
      f"apmed={ap.median:+.4f} (uncon t0 arm was 50368px/-4.5sig/-0.220)")
print(f"mesh cells capped={ncap}, trough area={cfrac:.4f}")
