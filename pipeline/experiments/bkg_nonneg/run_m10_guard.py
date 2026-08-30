import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from astropy import stats as astrostats
import metrics, nonneg
from run_120 import CUTOUTS_120, load
from run_cutouts import build_masker

for name in ["offcluster", "core"]:
    sci, err, wht = load(CUTOUTS_120[name][0])
    off = ~np.isfinite(sci) | ~np.isfinite(err)
    if wht is not None:
        off |= ~(wht > 0)
    sci = np.where(off, np.nan, sci)

    m_no = build_masker()
    mask_no, _ = m_no.mask_from_arrays(sci, err, wht=wht)
    bkg = m_no.estimate_background(sci, mask_no)

    sig_wht = nonneg.wht_sigma_map(sci - bkg.background, wht, ~mask_no & ~off)
    ceil_ms, _ = nonneg.multiscale_ceiling(
        sci, off, bkg.background, mask_no, boxes=(32, 64, 128), k=2.0,
        err=sig_wht)
    ms_map, ncap, nmesh = nonneg.cap_mesh(bkg, ceil_ms, sci.shape)
    with np.errstate(invalid="ignore"):
        req0 = (sci - ms_map) / sig_wht
    excl_tr = metrics.compact_source_mask(
        np.where(np.isfinite(req0), req0, 0.0), off) | off
    guard_map = nonneg.iterated_gated_trough(sci, ms_map, excl_tr,
                                             sigmas=(5, 15), t=2.0,
                                             err=sig_wht, verbose=False)
    resid = sci - guard_map

    with np.errstate(invalid="ignore"):
        req = resid / sig_wht
    excl_arm = metrics.compact_source_mask(
        np.where(np.isfinite(req), req, 0.0), off) | off
    ap = metrics.empty_aperture_stats(resid, excl_arm, (33,))[33]
    neg = metrics.negative_structure(resid, excl_arm, err=sig_wht)
    print(f"[{name}] m10_guard (no tier0): neg n={neg.n_neg} "
          f"area={neg.area_neg}px min={neg.min_signif:.1f}sig "
          f"apmed33={ap.median:+.4f} (meshcap {ncap}/{nmesh})")
    np.savez_compressed(f"out120v3/{name}/m10_guard.npz",
                        resid=resid.astype(np.float32),
                        sig_wht=sig_wht.astype(np.float32))
