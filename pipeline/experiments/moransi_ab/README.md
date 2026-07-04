# Moran's-I vs tiered — mosaic background A/B

Phase 3 of `docs/moransi-background-scoping.md`. Compares the two source-mask
methods for the NIRCam **mosaic** background fit, holding the `Background2D`
fitter constant so the mask is the only variable.

## Run

```bash
# verify the harness (synthetic data, no real files needed)
python run_ab.py --selftest

# real mosaic — prefer the preserved pre-subtraction input so all arms start
# from the same pixels:
python run_ab.py /path/to/mosaic_nircam_f444w_<field>_30mas_<tile>_i2d_before_bkgsub.fits \
    --arms tiered moransi:40 moransi:50 moransi:60 --plot --outdir figs
```

Input just needs `SCI` + `ERR` extensions (any i2d works; the
`_i2d_before_bkgsub.fits` sidecar that `resample_step` preserves is ideal
because every arm then starts from identical, un-subtracted pixels).

## Output

- `figs/summary.md` — the decision table (per-arm metrics, Δ vs the tiered
  reference) with the acceptance criteria.
- `figs/ab_panels.png` — per-arm rows: input · source-mask overlay · fitted
  background model · subtracted result, with detected sources marked.

## Metrics (see scoping doc §5)

| metric | meaning | good |
|---|---|---|
| `bkg_width` | `mad_std` of blank (background) pixels — sky uniformity | lower |
| `trough` | most-negative inner radial-profile value near bright sources, in `bkg_width` units — oversubtraction / negative-wing flag | not more negative than tiered |
| `flux_ratio` | aperture flux at shared source positions ÷ tiered arm | ≈ 1 (|Δ| < 5e-4) |
| `masked_frac` | fraction of pixels excluded from the fit | context |
| `runtime_s` | wall-clock of mask + fit | ≤ 2× tiered |

## Decide per regime, not pooled

Run the harness on representative tiles per **channel (SW/LW)**, **pixel scale
(native vs 30 mas)**, and **depth/field**, and require the acceptance criteria
to pass in each regime — a pooled pass can hide a regime-specific regression
(scoping doc §5). Source detection/photometry use `sep`; if `sep` is missing the
harness still reports `bkg_width`/`masked_frac`/`runtime`.
