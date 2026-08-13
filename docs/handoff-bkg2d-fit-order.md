# Handoff: real-frame A/B of `[nircam.bkg.bkg2d].fit_order = "first"`

**Branch:** `claude/nircam-bkg-amp-row-oversubtraction-4t12t6`
**Status:** implemented + synthetic-validated; needs a real-frame A/B before
flipping any field (or the default) to `"first"`.
**Audience:** an agent (or human) in an environment with `$CAMPFIRE_ROOT`,
the `campfire` conda env, and reduced/reducible NIRCam data for a
`subtract_2d` field showing the artifact.

## 1. The artifact and the fix (context)

On `subtract_2d` fields, bright galaxies spanning readout amplifiers show
**amp-blocky oversubtraction**: the per-amp-row 1/f term eats the galaxy's
halo flux and broadcasts it across the host amp's full width — blue
amp-height blocks with hard vertical edges at columns 512/1024/1536 and hard
horizontal edges at the source's top/bottom rows.

Mechanism (full analysis in the `bkg.py` module docstring and the Unreleased
CHANGELOG entry): the halo is structurally invisible to the source mask (the
ring-median pre-filter removes structure broader than its 80 px radius before
tier detection), the coarse conditioning detrend is starved near big masked
blobs (`exclude_percentile`), so halo flux biases the clipped amp-row medians
and the ρ=20 GP follows it. With the legacy order the applied 2-D fit runs
**after** the 1/f terms, on the post-h residual, so it can never reclaim that
flux — corrections accumulate one-way, and iterating makes the leak *grow*
(1.42 → 1.92 in the synthetic scene over the default 3 iterations).

`fit_order = "first"` fits the 2-D model on `resid − ped` with the halo
intact, and measures the 1/f terms on the b2d-subtracted residual. Same
components, same accumulation — different attribution.

## 2. What changed on this branch

| file | change |
|---|---|
| `pipeline/campfire_pipeline/nircam/steps/bkg.py` | per-iteration reorder behind `fit_order` (`"last"` = legacy default); 1/f measured on `cond − ped − b2d`; GP `amplitude_data` now the pre-detrend residual (both orders — restores the rj0911 calibration contract in `gp_amprow_offsets`); `CFP_BKG` records `bkg2d_order`; warning logged for `first` + `reject=true` |
| `pipeline/campfire_pipeline/data/config_default.toml` | `[nircam.bkg.bkg2d].fit_order = "last"` + comments |
| `pipeline/tests/test_nircam_bkg.py` | `test_b2d_fit_order_first_starves_amprow_of_halo` — synthetic pin of the artifact, the fix, and the reject interaction |
| `pipeline/CHANGELOG.md` | Unreleased / Algorithm entry |

## 3. Synthetic results already in hand (what the A/B should confirm on sky)

2048² frame, amp-dependent 100-row banding + a 20 σ-peak, σ=120 px Gaussian
halo hosted by amp C. Metric: halo flux in the amp-row ledger `h` (mean
row-offset difference, halo rows vs far rows), single iteration unless noted:

| arm | leak (amp C) |
|---|---|
| `last` (shipped legacy, any `reject`) | **+1.42** (3 iters: +1.92) |
| `first`, `reject=true` | +1.50 — **reject cancels the fix** |
| `first`, `reject=false` | **+0.77** (3 iters: +0.87) |
| `first`, `reject=false`, `extra_dilate=0` | +0.46 |
| `first`, perfect b2d model (floor) | −0.03 |

Two operational conclusions bake into the A/B design:

1. **`fit_order="first"` must be paired with `reject=false`.** The
   background-map outlier reject flags the halo bump in the first-order map
   as leaked source flux, masks it, refits — and the halo goes back to the
   1/f term. The step logs a warning on this combination.
2. The residual leak in `first` mode is the b2d model deficit inside the
   `extra_dilate` holes; `extra_dilate` is a candidate follow-up sweep
   parameter (it was tuned for flux conservation in the *legacy* order).

## 4. How to run the A/B

Pick a `subtract_2d` field/filter with a known-affected exposure (bright
galaxy spanning an amp boundary — the kind of frame in the issue screenshot).
The cheapest clean A/B is the WCS-clone-arm pattern from
`pipeline/experiments/oneoverf_gp/README.md`: clone the field definition in
`$CAMPFIRE_ROOT/config/fields.toml` (same `files`, same `tangent_point`,
`astrom_cats` symlinked) so each arm writes its own products tree.

Per-field overrides deep-merge over `[nircam.bkg]`; three arms:

```toml
[myfield_last.bkg]                 # arm 1: status quo (control)
    subtract_2d = true

[myfield_first.bkg]                # arm 2: the fix
    subtract_2d = true
    [myfield_first.bkg.bkg2d]
        fit_order = "first"
        reject = false

[myfield_firstrej.bkg]             # arm 3 (optional): pins the reject
    subtract_2d = true             # interaction on sky — expect ~no change
    [myfield_firstrej.bkg.bkg2d]   # vs arm 1
        fit_order = "first"
```

Run per arm (bkg is per-exposure; the skip is `CFP_BKG`-presence-based, so
already-processed trees need `--overwrite` on the bkg step or
`cfpipe nircam reset --from image2`):

```bash
conda run -n campfire cfpipe nircam run --field myfield_first --all --processes 4
```

Note the `amplitude_data` change rides along in **every** arm (including the
control): arm 1 vs pre-branch products isolates it if needed (see §6 risk 3).

## 5. What to measure

1. **The artifact itself (primary).** On the affected exposures' cal frames
   (and the mosaic): amp-boundary step amplitude near the bright galaxy.
   Simplest robust statistic: per-amp-row medians of the background
   (SRCMASK==0) pixels in thin column strips either side of the amp boundary,
   differenced, in the source's row range vs far rows. Expect the `last` arm
   to reproduce the blocks and `first` to cut them ≳2x; visually, the
   before/after `*_bkg.pdf` plots and a mosaic cutout like the issue
   screenshot.
2. **Component attribution.** `bkg_step(..., components_out=dict())` gives
   the accumulated `h`, `b2d`, `vcol`, `ped` ledgers per exposure
   (`pipeline/experiments/bkg2d_synthetic/inspect_components.py` is the
   existing harness). Difference `h` between arms: the amp-blocky component
   should migrate from `h` into `b2d`.
3. **1/f removal must not regress.** `stripe_std` / `stripe_hf` from
   `pipeline/experiments/oneoverf_gp/ab_metrics.py` (high-passed, so
   ICL-insensitive), clean rows and source rows separately. The b2d-first fit
   sees ~20-row banding under its mesh; the synthetic test says the clipped
   64/32 px boxes average it out — confirm on sky (arm 2 ≈ arm 1 on blank
   regions).
4. **Flux conservation.** Aperture photometry deltas arm 2 − arm 1 for
   bright/extended galaxies (the `first` fit sees halos the `last` fit never
   saw — absorption of true wings is the accepted `subtract_2d` trade, but
   quantify it; the `bkg2d_synthetic` sweep with `--estimator gp` + a
   `fit_order` arm is the controlled version of this and is the right place
   for a truth-based number).
5. **Skymatch invariant.** Masked-background median ≈ 0 per exposure in all
   arms (`meta.background.level` stays sensible; overlap consistency in the
   mosaic).
6. **`CFP_BKG` sanity.** `bkg2d_order=first` stamped in arm 2/3 headers;
   the warning line appears in arm 3 logs.

## 6. Risks to check / open questions

1. **Banding absorption by the first-order b2d** (§5.3). If blank-region
   stripe metrics regress, consider `filter_size` up or box up for `first`.
2. **Flux conservation under `first`** (§5.4). If wing absorption is
   unacceptable, the `extra_dilate` sweep point moves; note the synthetic
   result that *smaller* dilation reduces the amp-row leak — the two trade.
3. **The `amplitude_data` restoration** affects all fields (it changes the GP
   amplitude wherever the detrend is on, i.e. everywhere). Spot-check one
   blank field: stripe metrics and photometry vs pre-branch products should
   be neutral-to-better (the rj0911 calibration says post-detrend amplitude
   *loses* to the median estimator).
4. **`reject` semantics in `first` mode** are a blunt instrument: off = no
   guard against compact-source leakage into the fine-box fit. If arm 2 shows
   compact-source-shaped bumps in `b2d` ledgers, the follow-up is a
   scale-aware reject (reject bumps spanning ≲2 mesh cells, keep broad ones),
   not a return to `last`.
5. **Should `first` become the default** for `subtract_2d` fields once
   validated? The knob + CHANGELOG are written assuming yes, later, in a
   separate flip.

## 7. Definition of done

- Arm 1 reproduces the artifact; arm 2 visibly and quantitatively (≳2x)
  reduces it with no stripe-metric or photometry regression beyond the
  accepted trades; findings written up (numbers + cutouts).
- If results support it: flip the affected production field(s) to
  `fit_order = "first"`, `reject = false` in `fields.toml`, and open the
  default-flip / scale-aware-reject follow-ups as issues.
- The branch's PR carries the CHANGELOG entry (already written) — categorized
  **Algorithm → MINOR**.
