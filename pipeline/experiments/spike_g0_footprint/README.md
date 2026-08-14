# spike_g0_footprint — G0 footprint validation harness (M2 → M3 gate)

The G0 gate (design doc `docs/design-nircam-spike-masking.md` §6.1) asks:
**does the scaled spike-model isophote envelope real in-flight spikes to
within `grow`, across magnitude and filter?** The packaged models are
pre-flight (WebbPSF 1.0.0, requirements OPD — §7 OQ6), so this must be
answered on real frames before M3 is built. This harness makes the G0 run
mechanical on any machine holding an existing reduction; see
`docs/handoff-spike-g0-footprint.md` for the run plan and pass criteria.

## Method (per bright Gaia star, per level-2 cal frame)

1. **Resample** the published model onto the frame grid around the star
   (nearest-neighbor for the mask grade — linear kernels re-introduce
   under-masking, see `../spike_model_grade/`), with the anchor→pivot
   radial rescale `λ_pivot/λ_anchor`.
2. **Orient** by correlating azimuthal arm profiles (model vs data) over a
   rigid rotation + optional parity flip — self-contained, no roll_ref
   chain. The recovered `dtheta` per detector is itself a deliverable: it
   measures the orientation M3 will wire from the WCS chain. Note the
   8-arm pattern is mirror-symmetric, so (dtheta, flip) is two-fold
   degenerate; both solutions land the arms on identical PAs and give
   identical masks — read the arm PAs, not the flip flag.
3. **Fit amplitude** in-frame (median data/model over bright unsaturated
   model pixels). G0 tests footprint *shape*; Gaia→NIRCam flux prediction
   is an M3 concern and deliberately not exercised here.
4. **Verdict** at each threshold `L = f·σ_bkg` (default f = 3, 5, 10), per
   arm: radial extents from *pixel counts* in a ±4° wedge — data extent =
   outermost pair of consecutive radial bins with ≥3 px above `L`; model
   extent likewise on the mask-grade raster at `L − σ` (data crosses `L`
   wherever the true SB is within ~1σ below it; without that slack a
   perfect model fails on noise skew alone). Envelope holds iff
   `r_model + tol ≥ r_data` with `tol = grow·pixscale + one radial bin`.
   A pixel-level **miss count** (data > L outside the grow-dilated model
   mask, inside the arm wedges) complements the extents.

Statistics note: arm ridges are a few px wide, so any statistic over a
bin much wider than the arm dilutes the ridge into the background. The
harness works on small (radial × 1°) polar cells — cell means, then
median-over-radius for azimuthal profiles (kills point sources) or
max-over-azimuth for ridge profiles (plots) — and the verdict itself uses
raw pixel counts, which have no fill-factor bias at all.

## Outputs

- one PNG per star: asinh cutout + mask-grade isophote contours + arm
  rays, and per-arm data-vs-model ridge profiles
- `g0_results.json`: per frame/star — amplitude, `dtheta`/flip, σ_bkg,
  per-level per-arm extents + envelope verdicts + miss counts

## Run

```bash
conda run -n campfire python g0_footprint.py \
    --frames '$CAMPFIRE_ROOT/products/<field>/**/image2/*_cal.fits' \
    --out ./g0_out
```

Models fetch via the shared `ref_cache` engine into
`$CAMPFIRE_ROOT/cache/spike_models/` (or `--model-dir` for a directory
already holding them). Stars via Gaia DR3 cone query (`refcat/query.py`),
or `--stars ra,dec[,g]`. Frames with no qualifying star are skipped, so
globbing an entire reduction is fine. Key knobs: `--mag-max` (default
G < 15), `--levels`, `--grow`, `--r-min`, `--wedge`.

## Selftest (no data, no network)

```bash
python selftest.py --out ./selftest_out
```

Builds a synthetic 8-arm model + fake cal frames and asserts the harness
answers correctly in both directions: a perfect model passes every arm
with ~noise-level misses (scenario A); a model whose arms are truncated
short of the data fails loudly on every arm (scenario B). Scenario B's
defect is a truncation, not a radial stretch — on a pure power-law arm a
stretch is degenerate with amplitude and the in-frame fit absorbs it
exactly; worth remembering when interpreting real G0 results (§7 OQ6:
a uniform model-vs-sky scale error shows up in the *amplitude*, not the
envelope, so also compare fitted amplitudes against expected fluxes
across stars).
