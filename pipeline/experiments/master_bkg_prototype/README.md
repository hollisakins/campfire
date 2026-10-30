# Master-background prototype (design P0)

One-off harness for [docs/design-nirspec-master-background.md](../../../docs/design-nirspec-master-background.md),
phase P0: measure, on an observation's existing canonical spectrum-exposure files, what the
design leaves open before any pipeline code is written. Changes no product.

```bash
conda run -n campfire python pipeline/experiments/master_bkg_prototype/prototype_master_bkg.py \
    --obs <obs> --gratings prism_clear --max-sources 60 --processes 8 --model
```

On the gratings pass `--coverage-tail-px 200`: the overlap counter is built from the
`assign_wcs` bounding boxes, which stop at the nominal cutoff, but the trace carries on
(~200 px at G395M) into the shutters downstream (design doc §9).

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

## Diagnostics (`diagnostics/`)

Stand-alone follow-ups on the prototype's tables, each with `--obs` / `--root` and the
`<obs>_sky.ecsv` / `<obs>_shutters.ecsv` it needs (design doc §9):

| script | question |
| --- | --- |
| `profile_vs_f.py` | the empty-shutter surface brightness stacked against the within-shutter coordinate, per nod and shutter class, with and without the bar-shadow division (`--band`, `--detector`) |
| `barshadow_offset.py` | is the CRDS bar-shadow reference merely shifted / stretched? grid of shift and width against the stacked profile (`--detector`) |
| `sky_scale_maps.py` | where the shutters that disagree with the pooled sky sit: breakdown by detector / quadrant / nod / distance, per-slitlet coherence, maps on the detector and on the MSA plane |
| `trace_tails.py` | the trace past the nominal red cutoff (rate vs distance beyond the WCS bounding box) and the empty-shutter scale against the fraction of the shutter under another trace's tail — the G395M NRS2 excess |
| `slitlet_excess.py` | for the slitlets above the pooled sky: counts or calibration (rate-file DN/s vs calibrated SB), the excess spectrum, and the correlation with the neighbouring traces |
| `nod2_excess.py`, `excess_vs_flux.py`, `by_nod.py` | the ember_egs_p1 one-offs behind §9's first bullets (paths hard-coded) |
