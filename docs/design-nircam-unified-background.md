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
output**, so a detector-shaped residual survives into the canonical SCI. On
problem exposures (e.g. `jw03990449001_13201_00001_nrca1`) two residuals remain
after striping: **(a)** a different DC pedestal in each of the 4 amps, and
**(b)** ~100 px banding in the *row* direction that is mostly common-mode but
carries a real **amp-dependent** part. The current 1/f GP runs only at a short
length scale (ρ≈5 rows) and the box-32 detrend excludes the ~100 px scale, so
nothing models the slow, amp-dependent banding + pedestal. We want one step, one
source mask, and a **sequential per-amp chain** — per-amp pedestal (DC) + col
median + two-scale amp-row GP (ρ≈5 fine, ρ≈20 banding) — that removes 1/f, the
detector-shaped residual, *and* the per-exposure DC, without subtracting the
astrophysical background per exposure.

This revision folds in the **[unified sky+striping handoff](95883f43-UNIFIED_SKY_STRIPING_HANDOFF.md)**
(the ρ-sweep analysis and the no-skymatch constraint). §4.5–§4.6 and §4.4 are
the sections it changed.

## 1. Goals / non-goals

**Goals**

- Collapse `striping` + `sky` + `variance` into **one per-exposure step** (`bkg`)
  that builds the source mask **once** and shares it across pedestal fit, 1/f
  fit, and variance rescale.
- Standardize source masking on the `SubtractBackground` engine (the clipped
  ring-median pre-filter + tiered `detect_sources`), used at a mosaic-like
  (deep, aggressively dilated) configuration — **for the mask only. We do not
  subtract its 2-D background.**
- Replace striping's 1/f **and** sky's pedestal with a **sequential per-amp
  chain**: per-amp pedestal (owns the DC) → col median → amp-row GP ρ≈5 (fine
  1/f) → amp-row GP ρ≈20 (amp-row banding). The GP code is unchanged; the
  pedestal owns the DC. See §4.5–§4.6.
- Preserve the **no-skymatch invariant**: the per-exposure DC (the per-amp
  pedestal's frame mean) is the only thing zeroing frames before drizzle, so
  `bkg` must emit and subtract it and leave each exposure's masked background at
  ~0. See §4.5.
- Iterate mask → per-amp chain a few times so the mask refines on the running
  residual.
- Correct per-channel pixel-scale handling (SW native ≈31 mas vs LW native
  ≈63 mas, mosaic config tuned at 30 mas → LW ×0.5). See §4.4.

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

### 4.0 Spatial-scale ownership (the organizing principle)

Background removal is split across two phases; the unified step lives entirely
in the **per-exposure (detector-coordinate)** phase and owns only
detector-shaped signal. Everything smooth and celestial stays deferred to the
mosaic.

| spatial scale | structure | owner |
|---|---|---|
| few rows (ρ≈5) | 1/f readout striping | **unified GP** — short component |
| tens–~100 rows (ρ≈20) | amp-row banding, incl. the amp-*dependent* part | **unified GP** — long component |
| whole-frame DC | per-amp DC steps **+** frame sky pedestal | **unified GP** — per-amp offset *means* (§4.5) |
| whole-field, celestial | true 2-D sky gradient / ICL / extended wings | **mosaic `SubtractBackground`** — left IN the exposure |

The unified step removes `{1/f, amp-row banding, per-amp DC, frame pedestal}` and
**leaves** `{large-scale sky}`. Two properties make the detector/celestial split
clean: the GP length scale is **bounded** (ρ≈20 max — it cannot follow a
whole-field gradient), and the fit is **per amp** (detector banding is
discontinuous at amp boundaries; celestial background is continuous across them).

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
    correction = np.zeros_like(resid)              # everything we will subtract
    total_ped_dc = 0.0                             # frame DC, for the skymatch record
    edge_dq = (model.dq & DO_NOT_USE) != 0

    for it in range(cfg['n_iterations']):          # default 3
        # (1) SOURCE MASK — SubtractBackground, mask only (no 2-D bg subtract)
        srcmask, bitmask = SubtractBackground.from_config(mcfg).mask_from_arrays(
            resid, model.err, model.dq)            # §4.3, new method
        fitmask = edge_dq | srcmask                # + aggressive DQ if configured

        # (2) SEQUENTIAL per-amp chain (§4.6). Pedestal owns the DC (§4.5); the
        #     GP is unchanged (fits a ~zero-per-amp-mean residual, carries ~0 DC).
        ped = peramp_pedestal(resid, fitmask)                  # (H,W): per-amp DC, broadcast
        v   = column_pattern(resid - ped, fitmask)             # vertical (col median)
        h5  = gp_amprow(resid - ped - v, fitmask, rho=5)
        h20 = gp_amprow(resid - ped - v - h5, fitmask, rho=20)

        step = ped + v + h5 + h20
        resid -= step
        correction += step
        total_ped_dc += float(area_weighted_mean(ped))         # frame DC this iteration

    # (3) VARIANCE rescale, using the final mask   (§4.7)
    rescale_var_rnoise(model, srcmask, cfg['variance'])

    # (4) write: subtract the accumulated correction from the canonical SCI
    out = sci0 - correction
    out[sci0 == 0] = 0; out[np.isnan(out)] = 0
    model.data = out
    model.meta.background.level = total_ped_dc     # DC the (absent) skymatch owns (§4.5)
    model.meta.background.subtracted = True
    # skymatch invariant: masked background median of `out` must be ~0 (§4.5)
    atomic_save(model, exposure_file,
                header_updates=cfp.format(CFP_BKG=provenance_string),
                extra_hdus=[fits.ImageHDU(srcmask.astype('uint8'), name='SRCMASK')])
```

The DC is owned by `peramp_pedestal`; the GP is unchanged (§4.5). Convergence:
iteration 2+ fits the *residual* pedestal/1/f/banding (near-zero once the first
pass removed the bulk) while the mask sharpens as the frame flattens.
`n_iterations=3` matches the NIRSpec background loop; 1 is a resume/debug escape
hatch.

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
  lw = 0.5    # 30 mas tuning / 63 mas native ≈ 0.5 — hold the ANGULAR scale fixed
```

The factor that keeps the angular mask scale fixed is `f = tuning_pixscale /
data_pixscale = 30 / 63 ≈ 0.5` for LW: **halve** the pixel values, because LW
pixels are bigger so a fixed angular scale spans *fewer* pixels. (Confirmed — it
is the inverse of "double"; it's a config knob either way, but `0.5` is the
angular-invariant default.) The `tier_npixels` floor keeps the `f²` area scaling
from dropping the detection area below a few px.

### 4.5 Per-amp pedestal owns the DC — and the no-skymatch constraint

**The per-exposure DC is owned by an explicit per-amp pedestal, not by the GP.**
Each iteration subtracts a robust masked median **per amp** (4 values), which
removes both the per-amp DC steps (the first surviving residual, §1) and — via
its area-weighted frame mean — the frame pedestal. `sky_step`'s single *global*
scalar `fit_sky_tot` is retired in favor of this per-amp version. The GP (§4.6)
is left **unchanged**: because the pedestal has already zeroed each amp, the
residual the GP fits has ~zero per-amp mean, so the GP's own DC term is ~0 and
there is nothing left for it to own.

Why per-amp-pedestal rather than "let the GP carry its means" (the handoff's
phrasing): the DC is the quantity the (absent) skymatch depends on, so it should
be a robust, transparent statistic — a masked median — not a number entangled in
a GP posterior mean with edge/extrapolation behavior. It also leaves the GP code
(`gp_amprow_offsets`) untouched — exactly the validated ρ≈20 behavior. The GP is
**not** de-meaned: with the pedestal upstream that would be a no-op, and the
skymatch record is the frame mean of the *total* correction, so it stays exact
even if the GP carries a hair of residual DC.

This DC handling is a hard requirement, not a convenience — **there is no
skymatch downstream.** JWST `SkyMatchStep` is disabled everywhere
(`resample.py:132`, `outlier.py:232`) and the orchestrator-level one was removed
as a proven no-op. Inter-exposure sky matching is owned **entirely** by the
per-exposure DC zeroing each frame to a common level before drizzle. So:

- `bkg` **must emit and subtract a per-exposure DC.** A band-limited / high-pass
  variant that leaves the DC floating (the way an `fit_row_term`-style
  `highpass=80` would) breaks the mosaic: nothing zeroes the frames, per-frame
  offsets can't be recovered after combination, and overlapping exposures enter
  the drizzle at different sky levels — inflating noise/depth in the overlaps.
- **Invariant to validate:** after `bkg`, the masked-background median of each
  exposure must be ≈ 0. Recorded as `meta.background.level` = summed per-amp
  pedestal (frame mean) over the iterations (§4.2).

### 4.6 The sequential per-amp chain (1/f + banding); GP unchanged

Each iteration runs a **sequential** per-amp chain on the masked residual:

```
per-amp pedestal (DC, §4.5)  →  col median (vertical)  →
  amp-row GP ρ≈5  →  amp-row GP ρ≈20     (gp_amprow_offsets unchanged)
```

The two GPs extend the **existing** per-amp GP (`gp_striping.gp_amprow_offsets`,
`celerite2` SHOTerm, Q=1/√2, self-adapting `kernel_sigma`, `rho` = length scale
in *rows*, frozen hyperparameter):

- **ρ≈5** → fast row-to-row 1/f (what striping's GP does today);
- **ρ≈20** → the slow amp-row banding. The ρ sweep (8→300) found **ρ≈20 is the
  sweet spot**: it removes the amp-*dependent* banding, and larger ρ doesn't
  reduce differential banding further while it *does* cost more source flux.
  Validated on real data (HA); a full per-amp row profile (not DC-only) is fine.

Because the fit is **per amp**, the ρ≈20 term captures the amp-*dependent*
banding a single common-row term cannot. Sequential (rather than a joint
two-SHOTerm kernel) is chosen for simplicity and because we already iterate;
each GP self-adapts its amplitude to the residual it sees. `gp_amprow_offsets`
is unchanged from today's `estimator='gp'` path — no `demean` flag.

> **Sequential order (ρ5→ρ20) — why it's fine.** ρ≈20's main added value is
> **bridging masked source gaps**: a ρ≈5 GP reverts to the mean across a gap
> wider than ~5 rows, leaving the slow banding *in the gaps*; ρ≈20 bridges them.
> So ρ5→ρ20 (ρ5 does the bulk in clean regions, ρ20 cleans the gaps) removes the
> full structure, as does ρ20→ρ5. The only reason to prefer coarse-first is
> textbook scale-separation cleanliness — worth a both-orders diff on the §9.2
> exposure, not worth blocking on.

The per-column **vertical** term is the existing `col median`, here run once per
iteration on the pedestal-subtracted residual (it commutes to first order with
the amp-row terms — column DC vs per-amp row profile — and the outer iteration
absorbs the small coupling).

**Drop the fit-only 2-D background** (`subtract_background`,
`subtract_background_box=32`, striping.py:458-466). It existed *only* because the
**median** per-amp-row estimator can't represent a smooth gradient — without a
pre-flatten, a gradient forces amp-boundary steps. A bounded-length-scale GP
represents smooth structure natively, so the pre-detrend is not just unnecessary
but **actively harmful**: pre-subtracting the pedestal/large-scale bg strips
exactly the banding the ρ≈20 term is meant to fit. Dropping it is safe given the
per-amp pedestal owns the DC (§4.5) and ρ stays bounded (§4.0), so amp-boundary
steps don't reappear.

> **Detector-shaped vs astrophysical.** A per-amp-row GP would absorb the
> row-direction projection of a smooth gradient *if* ρ were unbounded. It is
> bounded at ρ≈20 and per-amp, and the pedestal owns the DC, so it cannot follow
> a whole-field celestial gradient — that stays for the mosaic (§4.0). HA's testing
> confirms it behaves; §9.4 keeps a known-background spot-check.

The `estimator` knob survives for A/B work: `gp` (the two-scale default),
`median` (legacy — note it still needs the pre-detrend the GP drops, so it's a
reference arm only), and `none` (cfn-only, SRCMASK-only, striping.py:425).

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
      lw = 0.5                          # angular-invariant (§4.4)

  [nircam.bkg.pedestal]                 # §4.5 — owns the per-exposure DC (skymatch)
    scope = "per_amp"                   # per-amp masked median (was: global scalar)

  [nircam.bkg.striping]                 # sequential per-amp chain (§4.6)
    estimator = "gp"                    # gp (default) | median (legacy ref) | none (cfn-only)
    maxiters = 3
    # (no demean flag — the pedestal owns the DC upstream, §4.5)
    [nircam.bkg.striping.gp]
      rho_short = 5.0                   # fast 1/f
      rho_long  = 20.0                  # amp-row banding (sweep sweet spot; validated)
      # sequential (ρ5 then ρ20); order is a spot-check, not a gate (§4.6, §9.2)

  [nircam.bkg.variance]
    block_size = 7
```

Config resolution stays package-default → user → per-observation (per the
existing loader). Per-channel `pixel_scale_factor` is the one genuinely new
resolution wrinkle.

## 6. Code structure

- `bkgsub.py`: extract `mask_from_arrays` (§4.3); `compute()` calls it. No
  behavior change for `resample`/`variance` callers of `compute`.
- `nircam/steps/bkg.py`: new `bkg_step` — the loop (§4.2). Imports
  `peramp_pedestal` + `column_pattern` + `gp_amprow` (from `oneoverf`/
  `gp_striping`), `SubtractBackground`, and `association.channel_of`.
- `nircam/gp_striping.py`: **unchanged.** `gp_amprow_offsets` is called twice
  (`rho=5` then `rho=20`) on the running residual; the upstream pedestal means it
  fits a ~zero-per-amp-mean residual, so no `demean` path is needed (§4.5).
- `nircam/oneoverf.py` (new shared helper): `peramp_pedestal` (per-amp masked
  median) and `column_pattern` (vertical), factored out of the old
  `fit_residual_striping`; the variance math moves here too.
- `nircam/steps/striping.py`, `sky.py`, `variance.py`: **removed**;
  `_build_srcmask` and `fit_sky_tot`/`sky_step` are deleted, superseded by
  `SubtractBackground` and the explicit per-amp pedestal.
- `orchestrate.py`: `PROCESS_STEPS`, `_PER_EXPOSURE_STEPS` edits (§4.1).
- `data/config_default.toml`: new `[nircam.bkg]`, delete the three old blocks.

### Data flow (per exposure)

```
image2 out (cal SCI) ──► edge (DQ) ──► bkg:
   ┌─ loop ×N ─────────────────────────────────────────────────────────┐
   │ resid ─► SubtractBackground.mask_from_arrays ─► srcmask             │
   │ resid ─► per-amp pedestal (DC) ─► col median ─► GP ρ5 ─► GP ρ20     │
   │         (pedestal owns DC; GP unchanged);  resid -= Σsteps          │
   └───────────────────────────────────────────────────────────────────┘
   VAR_RNOISE rescale(final mask)
   write: SCI = sci0 - correction ; SRCMASK ext ; CFP_BKG ;
          meta.background.level = Σ(per-amp pedestal frame DC)
```
The per-amp pedestal owns the DC (§4.5); the ρ5/ρ20 GP is unchanged.

## 7. Provenance, outputs, back-compat

- **`CFP_BKG`** stamped with the removed DC, estimator, ρ_short/ρ_long,
  n_iterations, channel factor, mask config summary. Single resume key.
- **`SRCMASK`** extension preserved (uint8), now the mosaic-depth mask.
- **`meta.background.level`** = per-exposure DC removed (summed per-amp pedestal
  frame mean, §4.2); `.subtracted = True`; `.method = 'local'`. This is the value
  the (absent) skymatch would otherwise own — keep it accurate.
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
2. **The problem exposure:** `jw03990449001_13201_00001_nrca1` — confirm the two
   surviving residuals (per-amp DC steps + amp-dependent ~100 px banding) are
   removed. Measure differential (amp-to-amp) banding power before/after; the
   long component must knock down the amp-*dependent* part, not just common-mode.
3. **Skymatch invariant (hard gate):** masked-background median ≈ 0 in every
   exposure after `bkg` (§4.5). Also check overlap consistency: overlapping
   exposures should enter the drizzle at matched sky levels (no per-frame DC
   drift). This is the constraint that replaces `SkyMatchStep`.
4. **Long component vs astrophysical background (spot-check, de-risked):** inject
   a known smooth gradient + extended source; confirm the ρ≈20 per-amp
   term removes detector banding but leaves the smooth celestial gradient for the
   mosaic (§4.6). HA's real-data testing already indicates it behaves; this
   confirms it under a controlled gradient. Optional companion: ρ5→ρ20 vs
   ρ20→ρ5 order diff on the item-2 exposure.
5. **Flux preservation:** rerun the Sérsic test at the mask (not 2-D-subtract)
   config; confirm detected-source flux preserved to a few %.
6. **Variance factor:** compare `CFP_VAR` factor old vs new; expect a small,
   explainable shift from the deeper mask.
7. **End-to-end:** one field through `run process`, diff canonical SCI vs the
   three-step pipeline; changes should localize to the detector-shaped residual
   the driver targets, and the mosaic depth in exposure overlaps should not
   regress (the skymatch check, at the mosaic level).

## 10. Open questions / decisions

1. **~~Sky pedestal~~ — resolved.** Explicit per-amp pedestal owns the DC; the
   GP is left unchanged (§4.5 — the pedestal makes de-meaning a no-op). The ρ≈20
   long term stays a **full per-amp row profile** (validated on real data, HA) —
   not DC-only.
2. **~~LW pixel-scale direction~~ — resolved: ×0.5** (angular-invariant, §4.4).
3. **~~Two-component GP: joint vs sequential~~ — resolved: sequential**
   (§4.6). Chain ρ5→ρ20, each GP self-adapts to the residual it sees. Retained
   spot-check (not a gate): ρ5→ρ20 vs ρ20→ρ5 order on the §9.2 exposure.
4. **Long-GP vs astrophysical background — residual check, de-risked.** ρ≈20 is
   bounded and per-amp, and the pedestal owns the DC, so it carries no net DC and
   can't follow a whole-field gradient (§4.0/§4.6); HA's testing confirms it
   behaves. §9.4 keeps
   a known-background spot-check rather than treating this as a live risk.
5. **Legacy CFP keys:** retire vs keep-stamping `CFP_1F`/`CFP_SKY`/`CFP_VAR`.
   §4.1/§7.
6. **`estimator='none'` arm:** the cfn-only reference path (striping.py:425)
   still needs a home for A/B comparison — keep as a `bkg` mode.
7. **Iteration count / convergence criterion:** fixed `n_iterations=3` vs
   converge-on-Δcorrection.
