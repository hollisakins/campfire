"""End-to-end composition + tier-0 A/B.

Arms (per cutout):
  ms_tr      : constrained mosaic10 on the raw input (= mosaic10_msceil_tr
               from run_cutouts; what "fix the mosaic stage only" ships)
  end2end    : constrained perexp64 (cap+trough) -> fresh mask -> constrained
               mosaic10 on that output (what "fix both stages" ships,
               applied retroactively to data that already carries the
               per-exposure damage)
  mosaic10_t0: UNconstrained mosaic10 fit using the full mosaic-config mask
               INCLUDING tier 0 (100 sigma / 30k px / 600 px dilation) --
               tests whether tier-0 masking itself drives troughs. Caveat:
               on a 40 arcsec cutout tier 0 masks a far larger fraction
               than on a full tile, so treat degeneracies as an upper bound.
"""

import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from astropy import stats as astrostats
from astropy.io import fits

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics
import nonneg
from run_cutouts import CUTOUTS, build_masker, dilate_source_tiers


def constrained_mosaic10(img, err, off, excl):
    """Fresh mask -> box-10 fit -> multiscale ceiling meshcap -> trough."""
    masker = build_masker()
    mask, _ = masker.mask_from_arrays(img, err)
    bkg = masker.estimate_background(img, mask)
    ceil_ms, _ = nonneg.multiscale_ceiling(img, off, bkg.background, mask)
    ms_map, ncap, _ = nonneg.cap_mesh(bkg, ceil_ms, img.shape)
    corr, cfrac = nonneg.trough_correction(img, ms_map, excl)
    return ms_map + corr, ncap, cfrac


def constrained_perexp64(img, err, off, excl):
    masker = build_masker()
    _, bitmask = masker.mask_from_arrays(img, err)
    m64 = build_masker(bg_box_size=64, bg_reject=True)
    mask64 = dilate_source_tiers(bitmask, 20)
    bkg64 = m64.estimate_background(img, mask64)
    onesided = nonneg.onesided_ceiling_map(img, off, box=64)
    ceil64, _, _ = nonneg.calibrate_ceiling(onesided, bkg64.background, mask64)
    cap_map, _, _ = nonneg.cap_mesh(bkg64, ceil64, img.shape)
    corr, _ = nonneg.trough_correction(img, cap_map, excl)
    return cap_map + corr


def main():
    fig, axes = plt.subplots(2, 4, figsize=(17.5, 8.6), dpi=110)
    for row, name in enumerate(["offcluster", "core"]):
        with fits.open(CUTOUTS[name]) as hdu:
            sci = hdu["SCI"].data.astype(float)
            err = hdu["ERR"].data.astype(float)
        off = np.isnan(err) | np.isnan(sci)
        excl = metrics.compact_source_mask(sci, off) | off

        # --- ms_tr: constrained mosaic10 on raw input ---------------------
        map_ms, ncap_ms, cfrac_ms = constrained_mosaic10(sci, err, off, excl)
        resid_ms = sci - map_ms

        # --- end2end ------------------------------------------------------
        map64 = constrained_perexp64(sci, err, off, excl)
        residA = sci - map64
        exclA = metrics.compact_source_mask(residA, off) | off
        mapB, ncap_b, cfrac_b = constrained_mosaic10(residA, err, off, exclA)
        resid_e2e = residA - mapB

        # --- tier-0 A/B: unconstrained mosaic10, mask WITH tier 0 ---------
        masker_t0 = build_masker(
            tier_kernel_size=[25, 25, 15, 5, 2],
            tier_npixels=[30000, 15, 10, 3, 1],
            tier_nsigma=[100.0, 1.5, 1.5, 1.5, 1.5],
            tier_dilate_size=[600, 33, 25, 21, 19],
        )
        mask_t0, bm_t0 = masker_t0.mask_from_arrays(sci, err)
        t0_frac = float(((bm_t0 >> 1) & 1).mean())
        bkg_t0 = masker_t0.estimate_background(sci, mask_t0)
        resid_t0 = sci - bkg_t0.background

        # baseline no-tier0 mosaic10 for the map comparison
        masker0 = build_masker()
        mask0, _ = masker0.mask_from_arrays(sci, err)
        bkg0 = masker0.estimate_background(sci, mask0)

        print(f"[{name}] tier0 fraction={t0_frac:.3f} "
              f"total mask with t0={mask_t0.mean():.3f} "
              f"(without: {mask0.mean():.3f})")

        panels = [
            ("mosaic10_msceil_tr", resid_ms, excl),
            ("end2end (fix both stages)", resid_e2e, exclA),
            ("mosaic10 + tier0 mask (uncon)", resid_t0, excl),
        ]
        for col, (label, resid, exm) in enumerate(panels):
            neg = metrics.negative_structure(resid, exm)
            ap = metrics.empty_aperture_stats(resid, exm, (33,))
            a = ap[33]
            print(f"[{name}] {label}: neg n={neg.n_neg} "
                  f"area={neg.area_neg}px min={neg.min_signif:.1f}sig "
                  f"| 1\" apmed={a.median:+.4f} sig={a.sigma:.4f}")
            ax = axes[row, col]
            ax.imshow(neg.signif_map, origin="lower", cmap="RdBu_r",
                      vmin=-5, vmax=5)
            ax.set_title(f"{name}: {label}\nneg={neg.area_neg}px "
                         f"min={neg.min_signif:.1f}σ apmed={a.median:+.3f}",
                         fontsize=9)
            ax.set_xticks([]), ax.set_yticks([])

        # tier0-induced map change (t0 minus no-t0 background)
        ax = axes[row, 3]
        d = bkg_t0.background - bkg0.background
        lim = np.nanpercentile(np.abs(d), 99.5)
        im = ax.imshow(d, origin="lower", cmap="PuOr_r", vmin=-lim, vmax=lim)
        plt.colorbar(im, ax=ax, fraction=0.045)
        ax.contour(((bm_t0 >> 1) & 1), levels=[0.5], colors="k",
                   linewidths=0.5)
        ax.set_title(f"{name}: bkg(t0 mask) − bkg(no t0)\n"
                     f"tier0 masks {t0_frac:.0%} (black contour)", fontsize=9)
        ax.set_xticks([]), ax.set_yticks([])

    fig.suptitle("end-to-end composition + tier-0 mask A/B (unconstrained)")
    fig.tight_layout()
    fig.savefig("out/composed.png")
    print("saved out/composed.png")


if __name__ == "__main__":
    main()
