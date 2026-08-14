#!/usr/bin/env python
"""Mask-grade downsample experiment for the spike footprint models (M2 -> G0).

Question (design doc §3.4 / §7 OQ6): how far can the PSF+scatlight models be
block-downsampled before threshold isophotes move by more than the mask
tolerance (`grow`, default 2 detector px)?

Method: for each (model, factor, level):
  - block-mean downsample by `factor` (mean preserves surface-brightness units,
    so the same absolute threshold applies at every grade)
  - nearest-upsample back to the full grid
  - compare binary masks (SB >= level) full-res vs round-tripped:
      miss  = max/p99.9 distance of full-res mask pixels from the round-trip
              mask ("footprint area the coarse grade would fail to cover")
      over  = same, roles swapped ("extra area the coarse grade adds")
    distances via EDT, reported in original detector px.

Acceptance: p99.9 drift < grow (2 px) at every level in the plausible
isophote range. The max is reported too but is dominated by isolated
near-noise-floor islands, hence the percentile.

Usage:
    conda run -n campfire python downsample_drift.py \
        --model-dir /path/to/nircam_psf_scatlight_models \
        --out ./out
"""

import argparse
import json
from pathlib import Path

import numpy as np
from astropy.io import fits
from scipy.ndimage import distance_transform_edt

# Representative anchors: blue SW (sharpest structure), red SW, red LW.
DEFAULT_MODELS = [
    "PSF+scatlight_0.9micron.fits",
    "PSF+scatlight_2.0micron.fits",
    "PSF+scatlight_4.4micron.fits",
]
FACTORS = [2, 4, 8, 16]
# Isophote levels in model SB units (sum-normalized model, per original
# detector px). Spans faint spike tips near the model floor (~1e-11..1e-12)
# up through the bright inner arms. The physical meaning: level = f * sigma_bkg
# / F_star, so lower levels correspond to brighter stars / deeper data.
LEVELS = np.geomspace(1e-11, 1e-6, 11)


def block_reduce(img, f, agg):
    """Crop to a multiple of f (centered) and block-aggregate.

    agg='mean' preserves flux/SB (photometric semantics) but dilutes narrow
    spike ridges below threshold — measured to under-mask whole arm segments.
    agg='max' is the footprint semantics ("any sub-pixel exceeds L"): the
    round-trip mask is a superset of the full-res mask at every threshold, so
    miss drift is 0 by construction and overshoot is bounded by the block
    diagonal f*sqrt(2).
    """
    n = img.shape[0]
    m = (n // f) * f
    o = (n - m) // 2
    c = img[o:o + m, o:o + m].reshape(m // f, f, m // f, f)
    out = c.mean(axis=(1, 3)) if agg == "mean" else c.max(axis=(1, 3))
    return out, o, m


def drift_stats(mask_ref, mask_other):
    """Distances (px) of mask_ref pixels from the mask_other footprint."""
    if not mask_ref.any():
        return dict(max=0.0, p999=0.0, npix=0)
    if not mask_other.any():
        return dict(max=np.inf, p999=np.inf, npix=int(mask_ref.sum()))
    d = distance_transform_edt(~mask_other)
    vals = d[mask_ref]
    return dict(max=float(vals.max()),
                p999=float(np.percentile(vals, 99.9)),
                npix=int(mask_ref.sum()))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-dir", type=Path, required=True)
    ap.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--factors", nargs="*", type=int, default=FACTORS)
    ap.add_argument("--agg", choices=["mean", "max"], default="mean")
    ap.add_argument("--out", type=Path, default=Path("out"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    results = []
    for name in args.models:
        path = args.model_dir / name
        with fits.open(path) as hdul:
            img = hdul[0].data.astype(np.float64)
            hdr = hdul[0].header
        pixscl = hdr["PIXELSCL"]
        print(f"== {name}  N={img.shape[0]} pixscl={pixscl}")

        for f in args.factors:
            ds, o, m = block_reduce(img, f, args.agg)
            up = np.repeat(np.repeat(ds, f, axis=0), f, axis=1)
            full = img[o:o + m, o:o + m]
            for level in LEVELS:
                mf = full >= level
                mu = up >= level
                miss = drift_stats(mf, mu)   # full-res area the coarse grade misses
                over = drift_stats(mu, mf)   # extra area the coarse grade adds
                rec = dict(model=name, pixscl=pixscl, factor=f, agg=args.agg,
                           level=float(level),
                           miss_max=miss["max"], miss_p999=miss["p999"],
                           over_max=over["max"], over_p999=over["p999"],
                           mask_frac=miss["npix"] / mf.size)
                results.append(rec)
                print(f"  f={f:2d} L={level:8.1e} "
                      f"miss p99.9={miss['p999']:6.2f} max={miss['max']:7.2f}  "
                      f"over p99.9={over['p999']:6.2f} max={over['max']:7.2f}  "
                      f"({100 * rec['mask_frac']:.3f}% masked)")

    out = args.out / f"drift_results_{args.agg}.json"
    out.write_text(json.dumps(results, indent=1))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
