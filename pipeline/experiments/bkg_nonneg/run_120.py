"""120-arcsec cutout validation: tier-0 A/B at production scale + the
proposed mosaic-level final guard, scored against the production `after`
mosaics.

Arms:
  input        : before-cutout minus constant robust pedestal
  after        : the production mosaic-level bkgsub output (reference)
  m10          : unconstrained mosaic fit, mask WITHOUT tier 0
  m10_t0       : unconstrained mosaic fit, full production mask (tier 0)
  m10_t0_guard : m10_t0 + multiscale ceiling meshcap + gated iterated trough

WHT handling this round: wht is passed to the pipeline mask builder (its
noise-equalized detection), and off-detector/zero-weight pixels are masked
everywhere; ceiling + trough + metrics still operate in flux space (bulk
depth variation on these cutouts is ~1.2-1.4x; edges are masked). Full
depth-aware significance is a next step.
"""

import argparse
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from astropy import stats as astrostats
from astropy.io import fits
from astropy.visualization import AsinhStretch, ImageNormalize, PercentileInterval

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics  # noqa: E402
import nonneg  # noqa: E402
from run_cutouts import build_masker  # noqa: E402

DL = "/Users/hollis/Downloads"
CUTOUTS_120 = {
    "offcluster": (f"{DL}/a2744_f444w_offcluster_galaxy_before_120as.fits",
                   f"{DL}/a2744_f444w_offcluster_galaxy_after_120as.fits"),
    "core": (f"{DL}/a2744_f444w_cluster_core_before_120as.fits",
             f"{DL}/a2744_f444w_cluster_core_after_120as.fits"),
}
AP_DIAMS = (10, 20, 33, 50)

TIER0_CFG = dict(
    tier_kernel_size=[25, 25, 15, 5, 2],
    tier_npixels=[30000, 15, 10, 3, 1],
    tier_nsigma=[100.0, 1.5, 1.5, 1.5, 1.5],
    tier_dilate_size=[600, 33, 25, 21, 19],
)


def load(path):
    with fits.open(path) as hdu:
        sci = hdu["SCI"].data.astype(float)
        err = hdu["ERR"].data.astype(float)
        wht = hdu["WHT"].data.astype(float) if "WHT" in hdu else None
    return sci, err, wht


def run(name, outdir):
    os.makedirs(outdir, exist_ok=True)
    before, after = CUTOUTS_120[name]
    sci, err, wht = load(before)
    sci_after, _, _ = load(after)
    off = ~np.isfinite(sci) | ~np.isfinite(err)
    if wht is not None:
        off |= ~(wht > 0)
    sci = np.where(off, np.nan, sci)

    excl = metrics.compact_source_mask(np.where(off, 0.0, sci), off) | off
    print(f"[{name}] off={off.mean():.4f} excl={excl.mean():.3f}")

    arms = {}
    # masks
    m_no = build_masker()
    mask_no, _ = m_no.mask_from_arrays(sci, err, wht=wht)
    m_t0 = build_masker(**TIER0_CFG)
    mask_t0, bm_t0 = m_t0.mask_from_arrays(sci, err, wht=wht)
    t0_frac = float(((bm_t0 >> 1) & 1).mean())
    print(f"[{name}] mask no-t0={mask_no.mean():.3f} with-t0={mask_t0.mean():.3f} "
          f"tier0 alone={t0_frac:.3f}")

    ped = astrostats.biweight_location(sci[~mask_no & ~off])
    arms["input"] = sci - ped
    arms["after"] = np.where(off, np.nan, sci_after)

    bkg_no = m_no.estimate_background(sci, mask_no)
    arms["m10"] = sci - bkg_no.background
    bkg_t0 = m_t0.estimate_background(sci, mask_t0)
    arms["m10_t0"] = sci - bkg_t0.background

    # --- alpha/WHT sky-noise map (source-independent), calibrated on the
    # baseline-fit residual over unmasked sky ---------------------------------
    sig_wht = nonneg.wht_sigma_map(sci - bkg_t0.background, wht,
                                   ~mask_t0 & ~off)
    with np.errstate(invalid="ignore"):
        infl = np.nanmedian((err / sig_wht)[~mask_t0 & ~off])
    print(f"[{name}] sigma_wht calibrated; median ERR/sigma_wht on sky = "
          f"{infl:.3f}")

    ceil_ms, cal = nonneg.multiscale_ceiling(
        sci, off, bkg_t0.background, mask_t0, boxes=(32, 64, 128), k=2.0,
        err=sig_wht)
    print(f"[{name}] ceiling calib: " + " ".join(
        f"b{b}: d={d:.5f} s={s:.5f};" for b, (d, s) in cal.items()))
    ms_map, ncap, nmesh = nonneg.cap_mesh(bkg_t0, ceil_ms, sci.shape)
    print(f"[{name}] meshcap: {ncap}/{nmesh} cells")
    # trough-pass exclusion: positive detections in the guard-input RESIDUAL
    # (equalized), so negative lanes stay visible to the corrector
    with np.errstate(invalid="ignore"):
        resid_eq0 = (sci - ms_map) / sig_wht
    excl_tr = metrics.compact_source_mask(
        np.where(np.isfinite(resid_eq0), resid_eq0, 0.0), off) | off
    print(f"[{name}] trough exclusion (residual-based): {excl_tr.mean():.3f} "
          f"(SCI-based was {excl.mean():.3f})")
    guard_map = nonneg.iterated_gated_trough(sci, ms_map, excl_tr,
                                             sigmas=(5, 15), t=2.0,
                                             err=sig_wht)
    arms["m10_t0_guard"] = sci - guard_map

    rows, results = [], {}
    for arm, resid in arms.items():
        # score each arm against its OWN residual-based positive exclusion
        with np.errstate(invalid="ignore"):
            r_eq = resid / sig_wht
        excl_arm = metrics.compact_source_mask(
            np.where(np.isfinite(r_eq), r_eq, 0.0), off) | off
        ap = metrics.empty_aperture_stats(resid, excl_arm, AP_DIAMS)
        neg = metrics.negative_structure(resid, excl_arm, err=sig_wht)
        rows.append(metrics.summarize(arm, ap, neg))
        results[arm] = (resid, ap, neg)
        print(f"[{name}] {arm}: neg n={neg.n_neg} area={neg.area_neg}px "
              f"min={neg.min_signif:.1f}sig apmed33={ap[33].median:+.4f} "
              f"excl={excl_arm.mean():.3f}")

    with open(os.path.join(outdir, "summary.json"), "w") as f:
        json.dump(rows, f, indent=2, default=float)

    np.savez_compressed(
        os.path.join(outdir, "arms.npz"),
        excl=excl, off=off, tier0=((bm_t0 >> 1) & 1).astype(bool),
        mask_t0=mask_t0, sig_wht=sig_wht.astype(np.float32),
        **{f"resid_{a}": r.astype(np.float32) for a, r in arms.items()},
    )

    # maps figure
    nrow = len(results)
    fig, axes = plt.subplots(nrow, 3, figsize=(14, 4.4 * nrow), dpi=100)
    norm = ImageNormalize(np.where(off, 0, sci),
                          interval=PercentileInterval(99.5),
                          stretch=AsinhStretch(0.05))
    sig_pix = astrostats.biweight_scale(arms["input"][~excl & ~off])
    for i, (arm, (resid, ap, neg)) in enumerate(results.items()):
        ax = axes[i, 0]
        ax.imshow(resid, origin="lower", cmap="RdBu_r",
                  vmin=-3 * sig_pix, vmax=3 * sig_pix)
        ax.set_title(f"{arm}: residual", fontsize=9)
        ax = axes[i, 1]
        im = ax.imshow(neg.signif_map, origin="lower", cmap="RdBu_r",
                       vmin=-6, vmax=6)
        ax.set_title(f"{arm}: smoothed signif "
                     f"(neg={neg.area_neg}px min={neg.min_signif:.1f}σ)",
                     fontsize=9)
        plt.colorbar(im, ax=ax, fraction=0.045)
        ax = axes[i, 2]
        ax.imshow(np.where(off, 0, sci), origin="lower", cmap="Greys_r",
                  norm=norm)
        if neg.neg_segmap is not None:
            ax.contour(neg.neg_segmap > 0, levels=[0.5], colors="#d62728",
                       linewidths=1.0)
        if arm in ("m10_t0", "m10_t0_guard"):
            ax.contour(((bm_t0 >> 1) & 1), levels=[0.5], colors="#ff7f0e",
                       linewidths=0.7)
        ax.set_title(f"{arm}: neg regions (red); tier0 (orange)", fontsize=9)
        for a in axes[i]:
            a.set_xticks([]), a.set_yticks([])
    fig.suptitle(f"{name} 120as")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "maps.png"))
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="out120")
    p.add_argument("--cutouts", default="offcluster,core")
    args = p.parse_args()
    for name in args.cutouts.split(","):
        run(name, os.path.join(args.out, name))


if __name__ == "__main__":
    main()
