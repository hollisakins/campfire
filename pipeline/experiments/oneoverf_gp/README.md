# NIRCam 1/f ("striping") — GP estimator A/B + clean_flicker_noise study

Testbed behind the cal-frame 1/f rework. Two studies, both on **rj0911 f444w**
(8 LW exposures over a cluster — bright/extended sources + ICL, the failure
case for the median full-row fallback, plus blank regions for regression):

1. **GP vs median** per-amp-row estimator (`[nircam.striping].estimator`).
2. **clean_flicker_noise (cfn)** — JWST's ramp-stage 1/f removal, standalone
   and as a pre-pass under our GP (nirspec-style residual cleanup).

All arms are exact WCS clones of `rj0911` (`$CAMPFIRE_ROOT/config/fields.toml`)
with `astrom_cats` symlinked so astrometry is held constant; each writes its own
products/reference tree and differs only in the 1/f treatment.

## Context: the cal-frame move

Striping now runs **after `image2`**, fitting and subtracting the 1/f in the
flat-fielded cal frame (order: detector1 → persistence → wisp → image2 →
striping → …). The legacy path fit on a flat-fielded *copy* but subtracted from
the *un-flat* rate SCI, which image2 then re-divided by the per-amp-structured
flat — leaving a coherent per-amp DC step at the amp boundaries. See
`pipeline/CHANGELOG.md` (Calibration).

## What the GP fixes

The production estimator measures one offset per *amp-row* as a 2σ-clipped
median of its background pixels, and **falls back to the full-row median**
(across all four amps) when a bright source masks most of an amp-row. Because
the four amps carry physically distinct offsets, that fallback is the wrong
quantity exactly under the source — a step at the amp boundary + slow-axis steps
at the source's top/bottom (the "box" of amp-row artifacts). The GP interpolates
the offset along the slow axis *within the same amplifier*, weighting each row by
its sampling error `σ_r ≈ 1.25·MAD/√N_r` and reverting smoothly toward the
per-amp DC only where no nearby anchor exists. See
`pipeline/campfire_pipeline/nircam/gp_striping.py`.

## Results (high-passed, ICL-insensitive 1/f residual)

**GP vs median** — GP wins across the board, slightly faster:

| metric | median | gp | Δ |
|---|---|---|---|
| stripe_std (amp-row 1/f) | 7.39e-4 | 6.24e-4 | **−16%** |
| stripe_hf | 1.06e-3 | 8.74e-4 | **−18%** |
| stripe_std, clean rows | 6.08e-4 | 5.14e-4 | −15% |
| stripe_std, source rows | 8.73e-4 | 7.31e-4 | −16% |
| runtime (uncal→mosaic) | 312 s | 283 s | faster |

Background uniformity unchanged, photometry conserved (<0.05%), no negative
wings. The GP gain holds on clean *and* source rows.

**clean_flicker_noise** — `fit_method="fft"` is **NIRSpec-only** (jwst skips it
for `NRC_IMAGE`; logged 8× per run), so only `"median"` is testable on NIRCam:

| arm | stripe_std (amp-row) | col_std | vs gp |
|---|---|---|---|
| uncorrected | 1.97e-3 | 9.57e-4 | +216% |
| cfn-med alone | 1.71e-3 | 8.94e-4 | +175% |
| gp (ours) | 6.24e-4 | 6.47e-4 | — |
| cfn-med + gp | 6.45e-4 | 5.13e-4 | +3.5% |

cfn-median alone removes only ~13% of the amp-row 1/f (vs GP's ~68%) and
introduces per-amp DC steps via `fit_by_channel`. As a pre-pass it gives **no**
amp-row gain over GP-alone and only a detector-specific column gain (−45% on
NRCB-long, +10% on NRCA-long) at the cost of full ramp-stage reprocessing.
**Conclusion: GP-alone is the best single approach; cfn is not adopted.**

## Arms

| arm | field | detector1 | striping |
|---|---|---|---|
| control | `rj0911_med` | — | median |
| GP | `rj0911_gp` | — | gp |
| GP+aggr | `rj0911_gpa` | — | gp, dilated mask + JUMP/SAT/PERS DQ |
| cfn alone | `rj0911_cfn` | cfn median | none |
| cfn+GP | `rj0911_cfngp` | cfn median | gp |
| cfn-fft alone | `rj0911_cfnfft` | cfn fft (→skipped) | none |
| cfn-fft+GP | `rj0911_cfnfftgp` | cfn fft (→skipped) | gp |

`estimator = "none"` builds/writes `SRCMASK` and runs the rest of the pipeline
but applies no campfire 1/f — used for the cfn-standalone arms.

## Reproduce

```bash
# GP A/B (seeds gp/gpa trees from the post-image2 snapshot, runs to mosaic)
python ../../scripts/calibrate_gp_striping.py --field rj0911_med --filter f444w
#   -> paste kernel_sigma_lw / rho_lw into config_default.toml [nircam.striping.gp]
bash run_arms_medgp.sh && python analyze_ab.py        # -> figs/

# cfn matrix (full from uncal — cfn changes detector1)
bash run_arms_cfn.sh && python analyze_cfn.py         # -> figs_cfn/
```

## Files (the rest — FITS snapshots, figs, logs, scratch — are gitignored)

- `ab_metrics.py` — residual-stripe, background-uniformity, radial-profile,
  aperture-photometry metrics (high-pass isolates 1/f from retained ICL).
- `analyze_ab.py` — GP-vs-median table (`figs/summary.md`) + figures.
- `analyze_cfn.py` — 6-arm cfn table (`figs_cfn/summary_cfn.md`) + figures,
  adds a per-column residual metric (`col_std`).
- `calibrate_gp_striping.py` — in `../../scripts/`; freezes SW/LW hyperparams.
- `run_arms_medgp.sh`, `run_arms_cfn.sh` — drivers.
- `rho_scan.py`, `scan_fitframe.py`, `diag_bgsub_boxsize.py` — diagnostics.
