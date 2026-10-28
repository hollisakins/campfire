# Master-background prototype (design P0)

One-off harness for [docs/design-nirspec-master-background.md](../../../docs/design-nirspec-master-background.md),
phase P0: measure, on an observation's existing canonical spectrum-exposure files, what the
design leaves open before any pipeline code is written. Changes no product.

```bash
conda run -n campfire python pipeline/experiments/master_bkg_prototype/prototype_master_bkg.py \
    --obs <obs> --gratings prism_clear --max-sources 60 --processes 8 --model
```

Needs `$CAMPFIRE_ROOT` with the observation's canonicals (after stage 2a; stage 2b-subtracted
files are fine, the `PRE_BKGSUB_*` arrays are used), CRDS for the bar-shadow reference
(`--no-barshadow` to skip, at the cost of a geometric bar mask), and optionally
`pip install jwst_backgrounds` for `--model` / `--seed model`.

Outputs in `./master_bkg_prototype_<obs>/`:

| file | content |
| --- | --- |
| `<obs>_shutters.ecsv` | one row per empty shutter: scale `a_s` against the pooled sky, χ², longest line-like run, `clean`, `reason` |
| `<obs>_sky.ecsv` | pooled sky per exposure group (`root`): wavelength, surface brightness, error, shutters per bin, the median seed, the scaled `jwst_backgrounds` model |
| `<obs>_exposures.ecsv` | each exposure × detector's clean sky fitted as `a · pooled + b`: decides whether per-exposure terms are needed (§4.1) |
| `<obs>_master_bkg_prototype.pdf` | per group: all shutter spectra coloured by verdict with the pooled sky and model; NRS1 / NRS2 and per-exposure ratios; the `a_s` histogram; the Paschen-β depth for gratings covering 1.282 µm |

What it does **not** have: the MSA plan's dedicated background slitlets and the pruned detached
shutters (the canonicals never contain them; design §1.7). Those arrive with the single-call
stage 2a (P1a). The pixel budget printed at the end says how many pixels each cut removed.
