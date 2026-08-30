import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from astropy.io import fits
import metrics, nonneg
from run_cutouts import CUTOUTS, build_masker

def iterated_trough(sci, bkgmap, excl, sigmas=(5, 15, 45), t=2.0,
                    max_iter=4, min_frac=1e-4):
    m = bkgmap.copy()
    for s in sigmas:
        for it in range(max_iter):
            corr, frac = nonneg.trough_correction(sci, m, excl,
                                                  smooth_sigma=s, t=t)
            m = m + corr
            print(f"    sigma={s} iter={it}: frac={frac:.4f} "
                  f"min={corr.min():.5g}")
            if frac < min_frac:
                break
    return m

for name in ["core", "offcluster"]:
    with fits.open(CUTOUTS[name]) as hdu:
        sci = hdu["SCI"].data.astype(float); err = hdu["ERR"].data.astype(float)
    off = np.isnan(err) | np.isnan(sci)
    excl = metrics.compact_source_mask(sci, off) | off
    masker = build_masker()
    mask, _ = masker.mask_from_arrays(sci, err)
    bkg = masker.estimate_background(sci, mask)
    ceil_ms, _ = nonneg.multiscale_ceiling(sci, off, bkg.background, mask)
    ms_map, _, _ = nonneg.cap_mesh(bkg, ceil_ms, sci.shape)
    print(f"[{name}] iterated multiscale trough:")
    final = iterated_trough(sci, ms_map, excl)
    resid = sci - final
    neg = metrics.negative_structure(resid, excl)
    ap = metrics.empty_aperture_stats(resid, excl, (33,))[33]
    tot = ms_map - final
    print(f"[{name}] RESULT: neg n={neg.n_neg} area={neg.area_neg}px "
          f"min={neg.min_signif:.1f}sig apmed={ap.median:+.4f} "
          f"total corr: area={float((tot>0).mean()):.4f} "
          f"max={tot.max():.5g}")
