import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from astropy import stats as astrostats
from astropy.io import fits
from scipy.ndimage import gaussian_filter
import metrics, nonneg
from run_cutouts import CUTOUTS, build_masker

def multiscale_trough(sci, bkgmap, excl, sigmas=(5, 15, 45), t=2.0):
    """Sequentially apply the trough pass at increasing smoothing scales.
    Each pass lowers the map only where the residual is coherently below
    -t*sigma_sm at that scale; sequential application means a broad lane
    caught at sigma=45 is corrected even if invisible at sigma=5."""
    total = np.zeros_like(bkgmap)
    m = bkgmap.copy()
    for s in sigmas:
        corr, frac = nonneg.trough_correction(sci, m, excl, smooth_sigma=s, t=t)
        m = m + corr
        total += corr
        print(f"    sigma={s}: corrected {frac:.4f} of area, "
              f"min={corr.min():.5g}")
    return m, total

for name in ["offcluster", "core"]:
    with fits.open(CUTOUTS[name]) as hdu:
        sci = hdu["SCI"].data.astype(float); err = hdu["ERR"].data.astype(float)
    off = np.isnan(err) | np.isnan(sci)
    excl = metrics.compact_source_mask(sci, off) | off
    masker = build_masker()
    mask, _ = masker.mask_from_arrays(sci, err)
    bkg = masker.estimate_background(sci, mask)
    ceil_ms, _ = nonneg.multiscale_ceiling(sci, off, bkg.background, mask)
    ms_map, _, _ = nonneg.cap_mesh(bkg, ceil_ms, sci.shape)
    print(f"[{name}] multiscale trough on msceil map:")
    final, total = multiscale_trough(sci, ms_map, excl)
    resid = sci - final
    neg = metrics.negative_structure(resid, excl)
    ap = metrics.empty_aperture_stats(resid, excl, (33,))[33]
    print(f"[{name}] RESULT: neg n={neg.n_neg} area={neg.area_neg}px "
          f"min={neg.min_signif:.1f}sig apmed={ap.median:+.4f} "
          f"(single-scale was: "
          f"{'262px/-2.9' if name=='offcluster' else '3418px/-2.8'})")
