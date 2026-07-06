# Design: Unified NIRCam background step (striping + sky + variance)

**Status:** draft for review
**Date:** 2026-07-06
**Context:** session exploration — mask-derivation comparison (wisp / striping /
`SubtractBackground`), dilation-vs-threshold test, and a 2048² Sérsic
flux-preservation test. See §3.
**Driver:** Three per-exposure steps — `striping` (CFP_1F), `sky` (CFP_SKY),
and `variance` (CFP_VAR) — each build or consume a source mask, and two of them
build one *independently* with different code and different depth. Striping's
fit-only 2D background is removed from the fit working copy but **left in the
output**, so a detector-shaped residual (per-amp pedestal offsets + banding at
scales larger than the 1/f itself) survives into the canonical SCI. We want one
step, one source mask, and a 1/f model that also removes that detector-shaped
residual — without subtracting the astrophysical background per exposure.

## 1. Goals / non-goals

**Goals**

- Collapse `striping` + `sky` + `variance` into **one per-exposure step** (`bkg`)
  that builds the source mask **once** and shares it across pedestal fit, 1/f
  fit, and variance rescale.
- Standardize source masking on the `SubtractBackground` engine (the clipped
  ring-median pre-filter + tiered `detect_sources`), used at a mosaic-like
  (deep, aggressively dilated) configuration — **for the mask only. We do not
  subtract its 2-D background.**
- Add a **coarse amp-row GP pass** (ρ≈20 rows) to the 1/f model to remove the
  detector-shaped per-amp banding that the current fit-only 2-D background
  leaves behind.
- Iterate mask → pedestal → 1/f a few times so the mask is refined on the
  running residual.
- Correct per-channel pixel-scale handling (SW native ≈31 mas vs LW native
  ≈63 mas) since the mosaic config was tuned at 30 mas. See §4.4.

**Non-goals**

- **Wisp subtraction stays exactly as-is** (`wisp`, step 3, before `image2`),
  with its own crude single-tier mask. It works, it runs before this step, and
  by the time `bkg` runs the wisp is already gone — so wisp-safety is not this
  step's problem. Not touching it.
- **No per-exposure astrophysical 2-D background subtraction.** The §3 Sérsic
  test showed per-exposure fine-mesh subtraction absorbs faint-source flux
  (depth-dependent, worse on a single exposure than a mosaic); the astrophysical
  sky should propagate to the mosaic and be subtracted there (`resample`).
- No change to the FITS schema. `SRCMASK` is still written (diag_striping reads
  it); `meta.background.*` is still populated; `VAR_RNOISE` is still rescaled.
- No change to the combine phase (`apply_mask`/`bad_pixel`/`outlier`/`resample`).

## 2. Current architecture (what we're replacing)

Three consecutive per-exposure steps, `PROCESS_STEPS` (orchestrate.py:44):

| step | CFP key | builds/uses mask | what it subtracts |
|---|---|---|---|
| `striping` | CFP_1F | **builds** `_build_srcmask` (4 tiers, per-tier `detect_threshold`), writes `SRCMASK` | pedestal (fit-only, *not* removed from output), fit-only 2-D bg (*not* removed from output), horizontal+vertical 1/f (removed) |
| `sky` | CFP_SKY | **reads** `SRCMASK` | constant sky pedestal (removed) |
| `variance` | CFP_VAR | **builds** `SubtractBackground.compute` mask (nsigma=3, dilate `[0,0,0,3]`) — discards the 2-D bg, keeps `mask_final` | nothing (rescales `VAR_RNOISE`) |

Key facts from the code:

- **striping** (steps/striping.py:396) builds `seg = _build_srcmask(model, …)`,
  ORs it with the `DO_NOT_USE` DQ into the fit mask (striping.py:421-423),
  measures a pedestal on `~mask` (striping.py:440-450, `fit_sky_tot`),
  subtracts it from the *fit working copy* only, optionally subtracts a fit-only
  2-D background (striping.py:458-466, `subtract_background_box=32`) from the
  working copy, fits horizontal+vertical striping (`fit_residual_striping`,
  `estimator ∈ {median, gp, none}`), and applies **only** horizontal+vertical to
  the output SCI (striping.py:505). The pedestal and 2-D bg are *not* removed
  from the output — hence the leftover detector-shaped signal.
- **sky** (steps/sky.py:52) samples `~DO_NOT_USE & (SRCMASK==0)`,
  `sigma_clip(sigma_upper=3, sigma_lower=10)`, fits `fit_sky_tot`, subtracts the
  scalar pedestal from SCI, sets `meta.background.level`.
- **variance** (steps/variance.py:41-61) constructs a *second*
  `SubtractBackground` (its own config), calls `compute` for `mask_final`, then
  rescales `VAR_RNOISE` by `skyvar / masked_mean_var_rnoise` on a block-reduced
  image (block_size=7).

So there are **two independent tiered maskers** in the process phase
(`_build_srcmask` for striping/sky vs `SubtractBackground` for variance), with
different kernels, thresholds, and dilation — the thing this design unifies.

## 3. What the session's tests established (inputs to this design)

1. **`SubtractBackground` and `_build_srcmask` agree on compact sources** (IoU
   100%, 0/30 centers missed) but diverge on diffuse/faint flux. The divergence
   is **detection threshold, not dilation** — `_build_srcmask` re-references
   `detect_threshold` to each *smoothed* tier (deep); `SubtractBackground`
   scales one *unsmoothed* RMS (shallow). Cranking `tier_dilate_size` only grows
   halos around already-detected sources; lowering `tier_nsigma` is what
   recovers diffuse masking.
2. **Mask depth must be tuned deliberately** if we standardize on
   `SubtractBackground`: the `variance` default (`nsigma=3`) is far shallower
   than the current `SRCMASK`. For striping+sky the mask must be *deep* (source
   wings masked) or the pedestal and amp-row offsets are biased.
3. **Do not subtract a per-exposure 2-D background.** On a realistic 2048²
   frame (gentle background, 234 Sérsics) the mosaic-config `SubtractBackground`
   recovered the background to 0.04σ in blank sky and preserved bright-galaxy
   flux to a few %, but **absorbed ~11–20% of sources** (the faint, near-
   threshold ones) into the background. That absorption is set by depth, so it
   is worse per-exposure than on a mosaic. Use the engine for the **mask**;
   leave the astrophysical background in the frame.

## 4. Proposed design

### 4.1 Position, naming, orchestration

Replace the three `PROCESS_STEPS` entries with one, and move `edge` ahead of it
so edge DQ is available when the mask is built:

```
detector1, persistence, wisp, image2,
edge,            # was after striping; now before bkg (edge DQ feeds the mask)
bkg,             # NEW — replaces striping + sky + variance   (CFP_BKG)
diag_striping,   # opt-in, still reads SRCMASK written by bkg
wcs_shift, preview, jhat
```

- New step name `bkg`, worker `bkg_step`, provenance key **`CFP_BKG`**. Register
  in `_PER_EXPOSURE_STEPS` (orchestrate.py:105) and `PROCESS_STEPS`; drop
  `striping`/`sky`/`variance` from both. `_CRDS_STEPS` is unaffected (bkg runs
  post-`image2`, resolves no reference files).
- `bkg` still writes the `SRCMASK` extension (uint8) so `diag_striping` and any
  external consumer keep working.
- **Legacy CFP keys:** `bkg` is the single resume key (`CFP_BKG`). Decision
  point (§10): either retire `CFP_1F`/`CFP_SKY`/`CFP_VAR` and update the handful
  of consumers, or keep stamping them as pointers to `CFP_BKG` for provenance
  continuity. Recommendation: retire, and add a one-line migration note — a
  half-processed tree from the old pipeline re-runs `bkg` cleanly because it
  keys on `CFP_BKG`, which old files lack.
- `cfpipe nircam bkg` becomes a valid single-step command automatically
  (CLI auto-registers from `STEP_NAMES`); `striping`/`sky`/`variance` stop being
  valid — acceptable, they no longer exist as steps.

### 4.2 The iterative loop

Operate on an in-memory working residual; accumulate corrections; write once.

```python
def bkg_step(exposure_file, field, cfg, ...):
    model = ImageModel(exposure_file)          # post-image2, cal frame
    sci0  = model.data.copy()
    channel = channel_of(detector_of(exposure_file))   # 'sw' | 'lw'  (association.channel_of)
    mcfg  = scale_mask_config(cfg['mask'], channel)     # §4.4

    resid = sci0.astype(np.float64, copy=True)
    total_pedestal = 0.0
    horizontal = vertical = np.zeros_like(resid)
    edge_dq = (model.dq & DO_NOT_USE) != 0

    for it in range(cfg['n_iterations']):          # default 3
        # (1) SOURCE MASK — SubtractBackground, mask only (no 2-D bg subtract)
        srcmask, bitmask = SubtractBackground.from_config(mcfg).mask_from_arrays(
            resid, model.err, model.dq)            # §4.3, new method
        fitmask = edge_dq | srcmask                # + aggressive DQ if configured

        # (2) SKY PEDESTAL   ***to be revised — §4.5***
        pedestal = fit_pedestal(resid, fitmask)
        resid -= pedestal
        total_pedestal += pedestal

        # (3) 1/f: column median + fine amp-row GP (ρ≈5) + coarse amp-row GP (ρ≈20)
        h, v = fit_oneoverf(resid, fitmask, cfg['striping'])   # §4.6
        resid -= (h + v)
        horizontal += h; vertical += v

    # (4) VARIANCE rescale, using the final mask   (§4.7)
    rescale_var_rnoise(model, srcmask, cfg['variance'])

    # (5) write: subtract accumulated corrections from the canonical SCI
    out = sci0 - total_pedestal - horizontal - vertical
    out[sci0 == 0] = 0; out[np.isnan(out)] = 0
    model.data = out
    model.meta.background.level = total_pedestal
    model.meta.background.subtracted = True
    atomic_save(model, exposure_file,
                header_updates=cfp.format(CFP_BKG=provenance_string),
                extra_hdus=[fits.ImageHDU(srcmask.astype('uint8'), name='SRCMASK')])
```

Convergence: iteration 2+ fits the *residual* pedestal and 1/f (near-zero once
the first pass removed the bulk), while the mask sharpens as the frame flattens.
`n_iterations=3` matches the NIRSpec background loop; 1 reproduces roughly the
current behavior for a resume/debug escape hatch.

### 4.3 Source masking — `SubtractBackground`, mask only

Add a mask-only entry point so we never run `estimate_background` and never
touch a scratch file:

```python
# bkgsub.py — new method, factored out of compute()
def mask_from_arrays(self, sci, err, dq):
    self.has_dq, self.dq = True, dq
    self.mask_by_dq()
    mask = self.off_detector(sci, err) | self.dqmask | np.isnan(sci)
    bitmask = np.left_shift(mask.astype(np.uint32), 0)
    filtered = self.clipped_ring_median_filter(sci, mask)   # ring-median ALWAYS
    bitmask  = self.mask_sources(filtered, bitmask, starting_bit=1)
    return (bitmask != 0), bitmask
```

`compute()` is refactored to call this and then run `estimate_background`, so
the resample step's behavior is byte-identical. The unified step calls
`mask_from_arrays` on the in-memory residual each iteration (no file round-trip
— fits the exposure-major I/O model).

**Config: mosaic-like depth.** Start from the `resample` `[nircam.resample]`
block (deep + aggressive) rather than the shallow `variance` defaults:
`tier_nsigma=[1.5,1.5,1.5,1.5]`, `tier_dilate_size=[33,25,21,19]`,
`tier_kernel_size=[25,15,5,2]`, `tier_npixels=[15,10,3,1]`, `ring_radius_in=80`,
`ring_width=4`, `ring_downsample=4`. The §3 tests validate this preserves
detected-source flux; the depth is what striping+sky need. The `bg_*` keys are
**unused** here (no `estimate_background` call) and can be omitted.

The per-tier `bitmask` is retained (uint8 → `SRCMASK`); if a future consumer
wants a shallower/compact-only mask it can select tier bits without a re-detect.

### 4.4 Pixel-scale adaptation (SW vs LW)

The `SubtractBackground` length parameters are **angular scales expressed in
pixels**, calibrated on 30 mas mosaics. Applied to native-scale exposures:

- SW native ≈ 31 mas ≈ 30 mas → use the config as-is (factor **1.0**).
- LW native ≈ 63 mas → the same angular structure spans ~half the pixels.

Introduce a per-channel factor `f` applied by parameter class:

| class | parameters | scaling |
|---|---|---|
| linear length | `ring_radius_in`, `ring_width`, `tier_kernel_size[*]`, `tier_dilate_size[*]`, `ring_clip_box_size` | `× f` |
| area / count | `tier_npixels[*]` | `× f²` (floored at a few px) |
| dimensionless | `tier_nsigma[*]`, `ring_clip_filter_size` (mesh cells), `ring_downsample` | unchanged |

Detector channel comes from `association.channel_of(detector)`
(association.py:76 — `'lw'` iff the token ends in `long`/`5`). Config:

```toml
[nircam.bkg.mask.pixel_scale_factor]
  sw = 1.0
  lw = 2.0    # <-- per your note; but see the direction check below
```

> ⚠ **Direction check (resolve in the sky-pedestal revision pass).** The factor
> that holds the *angular* mask scale fixed is `f = tuning_pixscale /
> data_pixscale = 30 / 63 ≈ 0.5` for LW — i.e. **halve** the pixel values,
> because LW pixels are bigger so a fixed angular scale is *fewer* pixels. Your
> note said to *double* (`f = 2.0`), which instead targets ~2× the angular
> scale (much more aggressive masking on LW). Making it a config knob costs
> nothing; flagging so we lock the intended reference before merge. Default
> shown as `2.0` per your instruction, pending that confirmation.

### 4.5 Sky pedestal — placeholder (to be revised next)

**This section is the explicit hook for the next iteration.** For now, carry the
current behavior so the loop is complete and comparable: sample
`~fitmask & isfinite`, `sigma_clip(sigma_upper=3, sigma_lower=10, maxiters=5)`,
fit `fit_sky_tot` (scale-free Gaussian sky-peak), subtract the scalar.

Open items we will revise here: (a) whether the pedestal stays a single scalar
or becomes low-order per-amp (given the coarse GP also removes per-amp
structure — see the §4.6/§10 interaction); (b) re-fit every iteration vs once;
(c) estimator (Gaussian peak vs mode vs clipped median). Left as a clean seam:
`fit_pedestal(resid, fitmask) -> float | ndarray`.

### 4.6 1/f striping, incl. the coarse GP pass

Keep the existing structure (`fit_residual_striping`: per-amp-row horizontal +
per-column vertical, `estimator ∈ {median, gp, none}`, cal-frame apply). Two
changes:

1. **Drop the fit-only 2-D background** (`subtract_background`,
   `subtract_background_box=32`). Its job was to keep large-scale structure out
   of the amp-row fit, but it is the source of the "subtract-then-restore"
   leftover the driver calls out. The coarse GP replaces its role for
   *detector-shaped* structure.
2. **Add a coarse amp-row GP pass (ρ≈20 rows)** after the fine pass (ρ≈5).
   Reuse `gp_amprow_offsets` (gp_striping.py:140) with a larger `rho`; the fine
   pass removes row-scale 1/f, the coarse pass removes the slow per-amp
   pedestal-offset/banding the fit-only 2-D bg used to leave behind. Both are
   per-amp (independent between amps, continuous within), so they target
   detector structure, not smooth cross-amp astrophysical gradients.

> **Interaction to watch (§10):** a per-amp-row GP will also absorb the
> row-direction projection of any smooth astrophysical gradient within an amp.
> With the fit-only 2-D bg gone, the coarse GP (ρ=20) is the only thing that
> could eat real background. This is the crux of the "detector-shaped vs
> astrophysical" separation and needs validation on real frames — it is the
> main scientific risk of the unification. Candidate guards: cap the coarse-pass
> amplitude, or require per-amp *offset* (DC) only for the coarse term rather
> than a full row profile.

### 4.7 Variance rescale (post-loop)

Move `variance`'s `VAR_RNOISE` rescale to the end of `bkg`, reusing the **final
loop mask** instead of building its own (steps/variance.py:72-89 logic
unchanged: block_reduce, `biweight_midvariance` for sky variance,
`biweight_location` for masked VAR_RNOISE, scale, zero→inf cleanup). Because the
shared mask is deeper than variance's old `nsigma=3` mask, the correction factor
will shift slightly — part of why this is an Algorithm/MINOR change (§8).

## 5. Config schema

New `[nircam.bkg]` block; retire `[nircam.striping]`, `[nircam.sky]`,
`[nircam.variance]`.

```toml
[nircam.bkg]
    n_iterations = 3
    plot = true

  [nircam.bkg.mask]                     # SubtractBackground, mask-only, mosaic-like
    ring_radius_in = 80
    ring_width = 4
    ring_downsample = 4
    ring_clip_max_sigma = 5.0
    ring_clip_box_size = 100
    ring_clip_filter_size = 3
    tier_kernel_size = [25, 15, 5, 2]
    tier_npixels = [15, 10, 3, 1]
    tier_nsigma = [1.5, 1.5, 1.5, 1.5]
    tier_dilate_size = [33, 25, 21, 19]
    mask_aggressive_dq = true           # fold JUMP_DET|SATURATED|PERSISTENCE into fitmask
    [nircam.bkg.mask.pixel_scale_factor]
      sw = 1.0
      lw = 2.0                          # see §4.4 direction check

  [nircam.bkg.pedestal]                 # §4.5 — placeholder, revised next
    estimator = "sky_peak"              # fit_sky_tot
    sigma_upper = 3
    sigma_lower = 10
    maxiters = 5

  [nircam.bkg.striping]                 # 1/f; largely inherits [nircam.striping]
    estimator = "gp"                    # median | gp | none
    maxiters = 3
    [nircam.bkg.striping.gp]
      rho = 5.0                         # fine pass
    [nircam.bkg.striping.gp_coarse]     # NEW coarse pass
      enabled = true
      rho = 20.0

  [nircam.bkg.variance]
    block_size = 7
```

Config resolution stays package-default → user → per-observation (per the
existing loader). Per-channel `pixel_scale_factor` is the one genuinely new
resolution wrinkle.

## 6. Code structure

- `bkgsub.py`: extract `mask_from_arrays` (§4.3); `compute()` calls it. No
  behavior change for `resample`/`variance` callers of `compute`.
- `nircam/steps/bkg.py`: new `bkg_step` — the loop (§4.2). Imports the pedestal
  fit (`skyfit.fit_sky_tot`), `fit_residual_striping`/`gp_amprow_offsets`
  (striping/gp_striping), `SubtractBackground`, and `association.channel_of`.
- `nircam/steps/striping.py`, `sky.py`, `variance.py`: **removed** (their pure
  numerics — `fit_residual_striping`, `_median_amprow_offsets`, `fit_sky_tot`,
  the variance math — move to shared helpers `nircam/oneoverf.py` and
  `nircam/skyfit.py`; `_build_srcmask` is deleted, superseded by
  `SubtractBackground`).
- `orchestrate.py`: `PROCESS_STEPS`, `_PER_EXPOSURE_STEPS` edits (§4.1).
- `data/config_default.toml`: new `[nircam.bkg]`, delete the three old blocks.

### Data flow (per exposure)

```
image2 out (cal SCI) ──► edge (DQ) ──► bkg:
   ┌─ loop ×N ────────────────────────────────────────────┐
   │ resid ─► SubtractBackground.mask_from_arrays ─► srcmask │
   │ resid ─► fit_pedestal(mask) ─► pedestal ─► resid -= ped │
   │ resid ─► 1/f (col median + GP ρ5 + GP ρ20) ─► resid -=  │
   └───────────────────────────────────────────────────────┘
   VAR_RNOISE rescale(final mask)
   write: SCI = sci0 - Σpedestal - Σh - Σv ; SRCMASK ext ; CFP_BKG ; meta.background
```

## 7. Provenance, outputs, back-compat

- **`CFP_BKG`** stamped with pedestal, estimator, ρ (fine/coarse), n_iterations,
  channel factor, mask config summary. Single resume key.
- **`SRCMASK`** extension preserved (uint8), now the mosaic-depth mask.
- **`meta.background.level`** = total pedestal; `.subtracted = True`;
  `.method = 'local'`.
- **`VAR_RNOISE`** rescaled as before (factor shifts slightly, §4.7).
- Consumers of `CFP_1F`/`CFP_SKY`/`CFP_VAR` (summary/metadata, deploy dry-run
  checks) — audit and update to `CFP_BKG` (§10 decision).

## 8. Versioning / changelog

Pixel and flux values change (unified mask depth; variance mask change; coarse
GP removes additional signal; 2-D-bg-fit removal). Per CLAUDE.md this is an
**Algorithm** change → **MINOR** bump, one `## Unreleased` entry in
`pipeline/CHANGELOG.md` before the PR opens. `CRDS_CONTEXT` is untouched (not a
Calibration change). FITS schema is unchanged, so not MAJOR.

## 9. Validation plan

Reuse the session harness (`/scratchpad/verify_masks.py`, `sersic_bkgtest.py`)
plus real-frame regression:

1. **Mask parity:** unified mosaic-depth mask vs current `SRCMASK` on real cal
   frames — IoU on compact sources (expect ~1.0), masked-fraction delta, and
   confirm faint-source completeness at *single-exposure* depth (§3 caveat).
2. **Pedestal parity:** `bkg` pedestal vs current `sky` pedestal (should track
   until §4.5 revises it).
3. **1/f + coarse GP:** residual detector-shaped power before/after — the coarse
   pass should reduce per-amp banding *without* eating the injected/known
   background (the §4.6 risk). Test on a frame with a known smooth gradient.
4. **Flux preservation:** rerun the Sérsic test at the mask (not 2-D-subtract)
   config; confirm detected-source flux preserved to a few %.
5. **Variance factor:** compare `CFP_VAR` factor old vs new; expect a small,
   explainable shift from the deeper mask.
6. **End-to-end:** one field through `run process`, diff canonical SCI vs the
   three-step pipeline; changes should localize to the detector-shaped residual
   the driver targets.

## 10. Open questions / decisions

1. **Sky pedestal (next iteration, by request):** scalar vs low-order per-amp;
   re-fit per iteration vs once; estimator. §4.5 is the seam.
2. **LW pixel-scale factor direction:** ×0.5 (angular-invariant) vs ×2 (your
   note). §4.4. Resolve before merge.
3. **Coarse-GP vs astrophysical background:** does ρ=20 per-amp-row eat real
   gradients now that the fit-only 2-D bg is gone? §4.6. Needs a
   known-background real-frame test; may need an amplitude cap or DC-only coarse
   term. **Highest scientific risk.**
4. **Legacy CFP keys:** retire vs keep-stamping `CFP_1F`/`CFP_SKY`/`CFP_VAR`.
   §4.1/§7.
5. **`estimator='none'` arm:** the cfn-only reference path (striping.py:425)
   still needs a home for A/B comparison — keep as a `bkg` mode.
6. **Iteration count / convergence criterion:** fixed `n_iterations=3` vs
   converge-on-Δpedestal/Δcorrection.
