# Scoping Document — Moran's-I Background Subtraction for CAMPFIRE NIRCam

**Date:** 2026-07-02 · **Status:** Draft for team decision
**Scope:** Integrate the donated Moran's-I background method as a config-selectable alternative to the current NIRCam background subtraction, callable on **both individual exposures and mosaics**, with a rigorous A/B against the incumbent.

> Provenance: produced by a fan-out mapping of the CAMPFIRE background subsystem (per-exposure, mosaic, architecture, diagnostics, provenance) followed by an adversarial accuracy/completeness pass. Donated code lives at `~/Downloads/moransi_background_code/` (`moransi_background.py`, `gradient.py`, and a demo notebook); it is **not** yet in the repo.

---

## 1. Executive summary

**Feasible, and the mosaic path is nearly drop-in.** The donated `moransi_background()` returns a photutils `Background2D` plus a source mask — the exact currency CAMPFIRE's mosaic background already trades in (`SubtractBackground.compute`, `pipeline/campfire_pipeline/nircam/bkgsub.py:381`, returns `(bkgd_subtracted, mask_final, bitmask)` and is invoked once at `steps/resample.py:288-315`).

**Core recommendation:** do *not* adopt the donated `moransi_background()` wholesale. Instead add a `mask_method = "tiered" | "moransi"` switch to the existing `SubtractBackground` so the *only* thing that changes is the mask generator (`mask_sources`, `bkgsub.py:292`), while the proven `estimate_background` fitter (`bkgsub.py:322`: `BiweightLocationBackground` + `exclude_percentile=90` + `BkgZoomInterpolator`) is held constant. This isolates the scientific A/B variable to "tiered detection vs Moran's-I masking, same fitter," and inherits the output contract, provenance, and file plumbing for free. Ship it **mosaic-first** (the method's tested regime — drizzled GRIZLI mosaics), gated behind config, with the A/B harness landing *before* any default flip.

**The single correctness gate is one real bug:** the donated code sets masked / off-detector pixels to `0.0` (`moransi_background.py:47`, `np.choose(keep,(0.,input_image))`) *before* `morans_i_local` computes raw per-patch `np.mean`/`np.std` (`:117-125`). On CAMPFIRE frames — large NaN off-footprint borders, masked bright sources — this biases the statistic for every partially-masked patch and must be fixed (make the statistic NaN/valid-aware) before any real run. *(An earlier draft flagged photutils' `exclude_percentile` default as the headline risk; that is largely a non-issue — see §6.5 — because `box_size == patch_size` makes each fit box ~100%- or ~0%-masked, and the recommended `estimate_background` route uses `exclude_percentile=90` regardless.)*

**The single decision that needs your scientific judgment:** whether Moran's-I should ever be applied as a **2-D subtraction to individual exposures**. The pipeline deliberately applies *no* 2-D background per-exposure today, precisely to avoid carving negative wings around bright galaxies (`config_default.toml:159` comment block). The safe, recommended exposure default is **mask-only** (improve the source mask, keep the scalar-pedestal sky), reserving any applied 2-D model for mosaics.

### Sequencing & abstraction (decided 2026-07-02)

- **Serial, mosaic-first.** Implement and A/B the mosaic path in full (Phases 0→3) before touching exposures. Applying a 2-D background to individual exposures is a **separate, later** effort, revisited only after the mosaic A/B lands.
- **Flexible source-masking is built in from the start.** The source-mask generator is a single **site-agnostic primitive** — `morans_source_mask(sci, *, input_mask, error, …) -> bool[True=source]` — with a uniform signature that matches the data available at *every* masking site (mosaic `SubtractBackground`, exposure `striping._build_srcmask`, exposure `variance`). Selection is a consistent `mask_method = "tiered" | "moransi"` config key. Mosaics wire it now; wiring it into the exposure mask (`striping._build_srcmask`) later is then a config-switch change, not a re-architecture. This satisfies "any source masking on individual exposures must be able to use Moran's-I *or* tiered," while keeping the actual work serial.

---

## 2. The Moran's I method

**What it does.** Moran's I is a spatial-autocorrelation statistic. `morans_i_local(image, patch_size, kernel)` (`moransi_background.py:88`) tiles the image into non-overlapping `patch_size × patch_size` blocks and, per block, standardizes to zero-mean/unit-variance and computes `I ∝ Σ z·conv(z, kernel)` with the default **queen** kernel (3×3, 0 center, 1 on the 8 neighbors). Source-contaminated patches have coherent structure → high I; blank-sky patches are near-random → low I. `morans_mask(...)` (`:25`) block-replicates the low-res I map back to full resolution and thresholds it, keeping the low-I patches as background (returns `True = keep/background`). `moransi_background(...)` (`:191`) inverts that to a source mask and fits a photutils `Background2D`, returning `(bkg, patch_mask)`; the model is `bkg.background`.

**Parameters (author's tested values, header comment `moransi_background.py:8-14`):**
- `patch_size = 20` — patch side in **pixels**. In the donated top-level function it *also* becomes `Background2D`'s `box_size` (`:236`); note the CAMPFIRE integration decouples these (§4.2).
- `percentile = 40.` — the threshold is a percentile of I taken **over valid pixels** (`rmi[keep]`, `:74-75`), not over patches. Because the replicated I map is constant within each patch, this is *area-weighted* and down-weights partially-masked patches — so it is not a clean "lowest-I 40% of patches." **Lower → preserves more bright-galaxy wings; higher → removes them.** Explicitly a hand-tuned auto-threshold to absorb "artificial correlations induced by 1/f noise and drizzling."
- `kernel` — neighbor weights (default queen).
- `sigma=3.`, `filter_size=(3,3)`, `bkg_estimator=MedianBackground()` — passthrough to `Background2D`.
- `input_mask` convention **0=use, 1=mask** (bool True=mask); `error`/wht: **NaN or 0 ⇒ ignored**.

**Good at:** rejecting extended/diffuse source flux that flat tiered detection under-masks, on **drizzled mosaics** where residual noise is decorrelated. **Caveats:**
- **Tuned on MOSAICS, not exposures** (NGDEEP / Abell2744 / J0027 GRIZLI drizzles). `patch_size=20` / `percentile=40` have no validated meaning on native cal frames.
- The W-normalization (`:137-138`) is a global constant across blocks, so returned values are **not true Moran's I** — harmless for a *percentile-ranked* mask, but must not be read quantitatively.
- No background-uncertainty propagation; nested-Python-loop performance; research-grade hygiene (§6).

---

## 3. Current CAMPFIRE background subtraction

### (a) Per-exposure — **no 2-D model is subtracted today**

Ordering (`orchestrate.py:44-57`): `… image2 → striping → edge → sky → diag_striping → variance …`. `image2` explicitly **skips** JWST's own `bkg_subtract` (`steps/image2.py:81`). Per-exposure "background" is three *scalar/additive* corrections on the cal-stage (MJy/sr) frame:

| Step | Seam (file:line) | What it does to SCI |
|---|---|---|
| `striping` | `steps/striping.py:341` | Scalar pedestal (`fit_sky_tot`) + **fit-only** 2-D detrend (`skyfit.fit_sky`, used only to detrend the working copy, *never* subtracted) + per-amp row/col 1/f. **Writes the canonical `SRCMASK` extension** via `_build_srcmask(model, extra_dilation=0) -> uint8` (`striping.py:54`, `1=source`). |
| `sky` | `steps/sky.py:23` | Subtracts a **single scalar pedestal** only. `sky_step(exposure_file, field, step_config, overwrite=False, status=None)`; reads `SRCMASK`, samples `~DO_NOT_USE & (seg==0)`, `sigma_clip(upper=3,lower=10)`, `fit_sky_tot` → `model.data = sci - sky`; sets `meta.background.level/subtracted/method='local'`; stamps `CFP_SKY` (`sky.py:74-88`). |
| `variance` | `steps/variance.py:41-64` | Constructs `SubtractBackground` and calls `.compute()` **only for its source mask** to rescale `VAR_RNOISE`. The 2-D model is discarded; SCI untouched. |

**Data model:** jwst `ImageModel` — `model.data / .err / .dq` (bits via `jwst.datamodels.dqflags`), GWCS in `model.meta.wcs`; written via `common.io.atomic_save(model, path, header_updates=cfp.format(...))`.

**The `SRCMASK` is authoritative and shared.** It is written once by `striping._build_srcmask` and then *consumed* by `sky` (pedestal sample), `diag_striping`, and (via a fresh `SubtractBackground` mask) `variance`. This makes striping — not sky — the correct seam for a mask-only Moran's-I experiment on exposures (§4.3).

**Design philosophy (a real constraint).** The striping 2-D background is fit-only *by design*: an applied fine-box background can carve negative wings around bright galaxies, whereas a median fit-only detrend "cannot put negative wings in the output" (`config_default.toml:159` comment block). Applying a Moran's-I 2-D model per-exposure re-introduces exactly that risk.

### (b) Mosaic — the clean seam

`resample_step` (`steps/resample.py:167`) drizzles the tile (skymatch skipped), then — gated on `step_config['background_subtract']` (default `true`, `config_default.toml:414`) — runs `SubtractBackground` **once**:

- Construction + call: `steps/resample.py:288-315` (all `ring_*/tier_*/bg_*` knobs pulled from config), `replace_sci=True`.
- `SubtractBackground.compute(filepath) -> (bkgd_subtracted, mask_final, bitmask)` (`bkgsub.py:381`): `open_file` reads `SCI`+`ERR` via `astropy.io.fits` (`:128`); `off_detector = np.isnan(err)` (`:160`); `clipped_ring_median_filter` (`:188`) → tiered `mask_sources` (`:292`, `detect_sources`) → `estimate_background(img, mask) -> Background2D` (`:322`); `bkgd_subtracted = sci - bkg.background`.
- `.call(filepath) -> outfile` (`bkgsub.py:424`) writes `SRCMASK` (`bitmask.astype("uint8")`) and, with `replace_sci`, overwrites SCI.
- Post: pre-sub sidecar `_i2d_before_bkgsub.fits` preserved (`resample.py:282, 318`); `SCI=NaN` where `WHT==0` (`:333-344`); extensions split; `plot_mosaic_bkgsub` reconstructs the model as `before − after` (`:434-435`).

**Data model:** plain numpy from FITS extensions `SCI/ERR/WHT/CON` (no DQ, no `VAR_*`), MJy/sr, FITS WCS. **This is an exact match to the donated numpy `(sci, error)` API.** `bkgsub.py` already imports `Background2D, BiweightLocationBackground, BkgZoomInterpolator, SigmaClip` — identical primitives to the donated `fit_background`.

**Mask polarity to reconcile everywhere:** the pipeline uses **`True = masked/source`**; the donated `patch_mask` is **`True = background/keep`**. The donated `input_mask` (0=use, 1=mask) *matches* the pipeline fit-mask, so a pipeline mask passes straight into `input_mask`; but `patch_mask` must be **inverted** (`srcmask = ~patch_mask`) before it is stored as `SRCMASK`.

---

## 4. Proposed API design

**Guiding principle: minimize the A/B variable.** Route the Moran's-I *mask* through the existing, validated `estimate_background` fitter rather than the donated `fit_background`. This (1) makes the comparison "tiered detection vs Moran's-I masking, same fitter," (2) inherits the `(bkgd_subtracted, mask_final, bitmask)` output contract so `resample.py` needs no downstream change, and (3) sidesteps two donated-code bugs (§6.5–6.6).

### 4.1 Module & core

New module `pipeline/campfire_pipeline/nircam/moransi_bkgsub.py` (numpy-native core + adapters):

```python
def morans_i_local(image, patch_size, kernel=None, valid=None) -> np.ndarray:
    """Vectorized per-patch Moran's-I ranking statistic (H//p, W//p).
    NaN-aware: `valid` (bool, True=usable) excludes masked/off-detector pixels
    from each patch's mean/std and neighbor sums. Returns NaN for patches with
    too few valid pixels (see min_valid_frac)."""

def morans_source_mask(sci, patch_size, *, input_mask=None, error=None,
                       kernel="queen", percentile=40.0,
                       min_valid_frac=0.25) -> np.ndarray:
    """Return CAMPFIRE-convention source mask (bool, True = source/exclude).
    Computes low-res I, block_replicates, thresholds at `percentile`, then
    inverts to source=True. Handles NaN borders and invalid-pixel
    contamination correctly (unlike the donated version)."""
```

### 4.2 Mosaic (and shared) integration — extend `SubtractBackground`

Add fields + one branch to the existing dataclass (`bkgsub.py:50`):

```python
mask_method: str = "tiered"          # "tiered" | "moransi"
moransi_patch_size: int = 20
moransi_percentile: float = 40.0
moransi_kernel: str = "queen"
```

In `compute()` (`bkgsub.py:414-416`), swap only the mask stage; the fitter is untouched:

```python
if self.mask_method == "moransi":
    src = morans_source_mask(sci, self.moransi_patch_size,
                             input_mask=mask, error=err,
                             kernel=self.moransi_kernel,
                             percentile=self.moransi_percentile)
    bitmask = np.bitwise_or(bitmask, np.left_shift(src, 1))
else:  # existing tiered path
    filtered = self.clipped_ring_median_filter(sci, mask)
    bitmask = self.mask_sources(filtered, bitmask, starting_bit=1)
mask_final = bitmask != 0
bkg = self.estimate_background(sci, mask_final)   # UNCHANGED fitter
```

`from_config` (`bkgsub.py:112`) already filters unknown keys, so the new fields flow in automatically. **No change to `steps/resample.py:288-315` except passing `mask_method=step_config.get('mask_method','tiered')`** plus the three moransi params.

Three integration realities to nail down here:

- **Shared-class coupling — keep `variance` on tiered.** `SubtractBackground` is *also* instantiated by the per-exposure `variance` step (`variance.py:41`) to build the mask it uses for `VAR_RNOISE` rescaling. Because `mask_method` defaults to `"tiered"` and `from_config` only forwards keys present in the section, a `[nircam.resample]` moransi switch does **not** leak into `variance` unless someone adds `mask_method` under `[nircam.variance]`. Default behavior is safe; state this explicitly and do **not** put moransi params in the variance section during the A/B.
- **Mask granularity ≠ fit box.** The moransi mask is computed at `patch_size = 20 px`, but the shared `estimate_background` fits at `bg_box_size = 10` (`bkgsub.py:87`). That is intentional and fine — the mask defines *which* pixels the fitter sees; the 10-px box then sets the background smoothing scale — but it means the moransi arm's effective resolution is set by `bg_box_size`, not `patch_size`. Keep `bg_box_size` identical across both A/B arms so only the mask differs.
- **ERR vs WHT for the `error=` input.** Use the mosaic **ERR** plane. Drizzle NaN-fills ERR at no-coverage before bkgsub (`drizzle.py:571-572`), and `estimate_background` already derives its off-detector coverage from `isnan(err)` (`bkgsub.py:160`), so ERR is the correct coverage signal here; WHT is not needed as a separate input.

*(Optional second arm — the author's exact recipe, `MedianBackground` + bundled fit — can be exposed as `mask_method="moransi_full"` calling a cleaned `moransi_background()` directly, to test whether the **fitter** choice matters. Keep it out of the primary A/B to avoid confounding mask vs fitter.)*

### 4.3 Exposure integration — the mask seam is `striping`, not `sky`

The meaningful, low-risk exposure experiment is **mask-only**: produce a better `SRCMASK` and let the existing scalar-pedestal sky consume it. That mask is written by `striping._build_srcmask` (`striping.py:54`) and is consumed downstream by sky, diag_striping, and variance — so the branch belongs **there**, not in `sky_step`:

```python
# in the mask-building path of striping (Seam B, striping.py:54)
if mask_method == "moransi":
    input_mask = (np.bitwise_and(model.dq, DO_NOT_USE) != 0)
    src = morans_source_mask(model.data, patch_px, input_mask=input_mask,
                             error=model.err, percentile=pct, kernel=kern)
    srcmask = src.astype('uint8')     # replaces the tiered SRCMASK for this run
```

Putting the branch inside `sky_step` instead would be a near **no-op**: it would change only sky's own pedestal sample, and the robust Gaussian sky-peak fit (`fit_sky_tot`) is nearly insensitive to modest source-mask changes — while leaving striping's 1/f fit and diag_striping/variance masking on the tiered mask.

Reserve `sky_step` strictly for the **applied-2-D** case (§8, `subtract_2d=true`), which subtracts a Moran's-I 2-D model from `model.data`, sets `meta.background.method='moransi'`, and reuses `CFP_SKY`. That path is opt-in and carries the negative-wing and downstream-variance risks discussed below.

**Exposure idempotency footgun (must document).** Unlike mosaics, per-exposure steps skip via `cfp.should_skip` keyed on the **presence** of `CFP_SKY` (`sky.py:28`), with *no* config hash. Switching the exposure `method` on an already-processed exposure therefore **silently no-ops** unless you pass `--overwrite` (or `reset --uncal`). This is the exposure analog of the mosaic stale-tile bug (§7.2). Encode `method` into the `CFP_SKY` provenance string so a re-run with a different method is at least auditable.

### 4.4 Exposure-vs-mosaic differences the API must handle

- **`patch_size` scaling.** Tuned at drizzled scale; a *fixed* 20 px is wrong across regimes. Expose **`patch_size_arcsec`** (default ≈0.6″ ≈ 20 px at 30 mas) and convert per output/channel: 30 mas mosaic → 20 px; SW exposure (0.031″/px) → ~19 px; LW (0.063″/px) → ~10 px. Keep a raw `patch_size` override for calibration sweeps.
- **1/f autocorrelation confound (exposures).** Native cal frames carry per-amp-row 1/f striping — coherent row structure that Moran's I (queen kernel) reads as "signal," biasing the I distribution and the percentile cut. The author flags this exact effect (`moransi_background.py:8-14`). Mitigations: run Moran's-I **after** `striping` (1/f already removed from SCI), and/or offer an anisotropic kernel that down-weights the row direction. Mosaics largely avoid this (1/f removed upstream, drizzle decorrelates residuals) — the reason to prefer mosaic-first.
- **Performance.** `morans_i_local` is a nested double loop: ~250k iterations on a 10k² tile (patch=20), ~10k on a 2048² exposure, each doing an `ndimage.convolve` on a 20² block, all under `multiprocessing.Pool`. **Must vectorize** (reshape to `(nby, p, nbx, p)`, batch per-patch mean/std, compute neighbor sums with a single convolution / four shifted adds). Target < 1 s/exposure, < 30 s/tile. The companion `gradient.py` is out of scope for v1.

### 4.5 Config switch (the exact A/B knobs)

Use **flat keys**, matching the existing `[nircam.resample]` style (`bg_box_size`, `tier_npixels`, …) and `SubtractBackground.from_config` field mapping — not a nested sub-table:

```toml
[nircam.resample]                     # MOSAIC — wired now (Phases 0–3)
    background_subtract = true        # master on/off (unchanged)
    mask_method = "tiered"            # "tiered" | "moransi"   <- A/B switch
    moransi_patch_size = 20           # px at output scale
    moransi_percentile = 40.0
    moransi_kernel = "queen"
    moransi_min_valid_frac = 0.25
    # existing ring_*/tier_*/bg_* still drive the SHARED fitter

[nircam.striping]                     # EXPOSURE — later, separate phase (Phase 4)
    mask_method = "tiered"            # same switch, same primitive at the exposure seam
    moransi_patch_size_arcsec = 0.6
    moransi_percentile = 40.0
```

Resolution is the standard 3-layer deep-merge (`get_nircam_step_config`), so per-field overrides via `fields.toml [<field>.resample]` compose cleanly. This mirrors the established `estimator = "median" | "gp"` (striping, `config:192`) and `implementation = "jwst" | "campfire"` (resample, `config:407`) idioms. **Deliberately keep `[nircam.variance]` with no `mask_method`** so variance stays on tiered — and note `variance_step` (`variance.py:41`) constructs `SubtractBackground` with explicit per-key `step_config.get(...)` that omits `mask_method`, so variance is doubly pinned to tiered regardless of config.

---

## 5. A/B testing harness

**Reuse `pipeline/experiments/oneoverf_gp/`** (`ab_metrics.py`, `analyze_ab.py`, `diag_bgsub_boxsize.py`, `README.md`). Its "arm = product tree" structure generalizes from the GP-vs-median 1/f study to method A/B with almost no change.

**What runs.** Two arms per regime, each a WCS-identical product tree differing only in config:
- **Mosaic:** `bkg_tiered` (`mask_method="tiered"`) vs `bkg_moransi` (`mask_method="moransi"`). Because `_i2d_before_bkgsub.fits` is always preserved (`resample.py:282,318`), each arm's model is trivially `before − after`.
- **Exposure:** `strip_tiered` vs `strip_moransi` (mask-only, at the striping seam). Requires `--overwrite` / `reset --uncal` per the idempotency footgun (§4.3).
- **Percentile sweep** (30/40/50/60) generalizing `diag_bgsub_boxsize.py` — replace its box-size axis with a `(method, percentile)` axis; it already renders the 4-panel row (before | mask overlay | 2-D model | after) and annotates `amp_boundary_step` (`diag_bgsub_boxsize.py:38-43`) and `bgσ`.

**Metrics (already in `ab_metrics.py`).**
- `bkg_width` — background-uniformity `mad_std` (`ab_metrics.py:111`). **Primary.**
- `radial_profile` around bright sources — negative trough = oversubtraction detector (`ab_metrics.py:208-229`). **Primary** (precisely the negative-wing risk of an applied fine 2-D model).
- `aperture_photometry` — flux conservation (`ab_metrics.py:232-249`). **Primary.**
- `SubtractBackground.evaluate_bias` (`bkgsub.py:348`) — masked-vs-unmasked background bias in σ; defined-but-unwired today, call it directly per arm.
- Exposure-specific: `amp_boundary_step` (median step across 512/1024/1536 amp boundaries) to confirm Moran's-I isn't re-imprinting amp structure.

**Statistical rigor (stratify — do not pool).** The design itself predicts regime-dependent behavior (SW 0.031″ vs LW 0.063″ vs 30 mas mosaic; depth; field), so a single pooled mean±std can mask a regime-specific regression. Report metrics **stratified by channel (SW/LW), pixel-scale regime (native vs 30 mas drizzled), and depth/field**, with explicit N per cell and a robust-difference / significance test — and require the acceptance criteria to pass **per regime**, not pooled. The percentile sweep must likewise pass per regime.

**Absolute ground truth (not just relative).** Relative-to-tiered acceptance can bless a *flatter-but-oversubtracted* sky. Add at least one absolute anchor: injected synthetic sources (recover input flux) and/or a deep blank-field region (measure over-subtraction directly), so "flatter sky" cannot pass on oversubtraction alone.

**Proposed acceptance criterion (Moran's-I adopted as default for a regime only if all hold, per regime):**
1. `bkg_width` no worse than tiered (Δ ≤ 0, equal-or-flatter sky).
2. No `radial_profile` negative trough deeper than tiered around bright sources.
3. Aperture flux conserved to **< 0.05%** (the 1/f study's threshold) **and** injected-source flux recovered within tolerance.
4. `evaluate_bias` significance ≤ tiered.
5. Runtime ≤ 2× tiered after vectorization.

Ties/mixed → keep tiered as default, ship Moran's-I as an opt-in arm. Record the decision in `figs/summary.md` and the PR body.

---

## 6. Code-quality remediation of the donated code

Before it enters `campfire_pipeline/`:

1. **Remove the import-time side effect** — `gradient.py:10` `image = np.random.rand(100, 100)` executes on import; incompatible with the lazy-import model in `orchestrate.py`. (Defer `gradient.py` for v1; if vendored, delete this line and the module-level `matplotlib` import.)
2. **Strip all `print()` debug** — e.g. `moransi_background.py:76,79-85`; route needed logging through `campfire_pipeline.common.io.log`.
3. **Remove commented-out dead code** — `moransi_background.py:49-53,71-72`; `gradient.py:125-129`.
4. **[CORRECTNESS — the singular gate] Fix invalid-pixel contamination.** `morans_mask` sets invalid pixels to `0.0` (`:47`) *before* `morans_i_local` computes raw `np.mean`/`np.std` per block (`:117-125`). On CAMPFIRE frames (NaN borders, masked sources) this biases every partially-masked patch. Fix: make `morans_i_local` **NaN/valid-aware** — exclude invalid pixels from each patch's mean, std, and neighbor sums; return NaN for patches below `min_valid_frac`. This is the one true bug and blocks any real run.
5. **[Edge case, not a headline risk] `Background2D` `exclude_percentile`.** The donated `fit_background` never sets `exclude_percentile` (photutils default 10). This is **largely benign for the donated path**: `moransi_background` forces `box_size = patch_size` (`:236`) and the source mask is block-replicated (constant within each patch-aligned box), so each box is ~100%-masked (source patch → correctly excluded) or ~0%-masked (background patch → kept); the default 10 does not degenerate. The only real effect is partial-NaN-footprint boxes making exclusion slightly conservative. The **recommended `estimate_background` route (`exclude_percentile=90`) avoids it entirely** — this is a further reason to prefer §4.2 over the donated fitter.
6. **Fix `**kwargs` silently dropped.** `fit_background(...)` accepts `**kwargs` but the `Background2D(...)` call omits it, so `exclude_percentile`, `interpolator`, and the `coverage_mask` passed by `moransi_background` (`:241`) never reach photutils. Forward them, or (preferred) bypass with `estimate_background`.
7. **Vectorize `morans_i_local`** (§4.4) — mandatory before per-exposure use, strongly recommended before full-tile use.
8. **Document the W-normalization** (`:137-141`) as a global constant / monotone *ranking* statistic, not a true Moran's I; keep it (harmless for percentile masking) but name returned values accordingly.
9. **Align the estimator** — donated default `MedianBackground` vs CAMPFIRE `BiweightLocationBackground`; hold constant in the A/B (the `estimate_background` route does this automatically).
10. **dtype/NaN hygiene** — keep **float32**: a 10k² plane is ~350 MB float32 (~700 MB float64), and `block_replicate` back to full res is another **~800 MB in float64** (~400 MB float32) — cast the replicated map to float32. Handle the right/bottom partial-patch loss from NaN padding (`:56-59`).
11. **Error propagation** — the fitted background carries no uncertainty and ERR is not updated. This matches the *current* `SubtractBackground` (ERR untouched), so parity is fine — but **document it explicitly**, since the pipeline otherwise threads `var_*`/ERR carefully. See §8 for the applied-2-D/variance ordering interaction.
12. **Tests** — follow `tests/test_nircam_diag_striping.py`'s planted-synthetic-signal + quantitative-assertion pattern: a synthetic sky gradient + injected sources, assert Moran's-I recovers the background to tolerance and does not carve wings; plus a smoke test on a small tile for both `mask_method` values. There is **no** existing test for `bkgsub.py`.
13. **Dependencies:** the donated code imports only numpy / scipy.ndimage / astropy / photutils — **no scikit-image**; no new dependency. Env caveat: `pyproject.toml` pins `photutils>=1.13`; verify the actual deploy/`campfire` conda env resolves ≥1.13 (the `Background2D` API used is present from 1.12).

---

## 7. Provenance & downstream

Background provenance is essentially absent today — mosaics carry only `CMPFRVER`/`CMPFRTIM`; the manifest records a bare boolean. Add three layers:

1. **FITS header (mosaic primary).** Add `CMPFRBKG` (method string) plus compact params `BKGPATCH`, `BKGPCTL`, `BKGKERN`, stamped in `SubtractBackground.call()` / `resample_step`. `CFP_BKG` exists **only** in the NIRSPEC keyset (`cfp.py:115`), not NIRCAM (`cfp.py:66-100`) — use the `CMPFR*` family for mosaics; don't overload `CFP_BKG`.
2. **[Load-bearing] Manifest `config_hash`.** Extend it to include `mask_method` + moransi params in **both** locations or A/B tiles won't rebuild: `create_manifest` (`manifest.py:167-173`) *and* the mirror in `check_config_changed` (`manifest.py:300-306`). Today the hash covers only `{pixfrac, kernel, pixel_scale, background_subtract}` — switching tiered↔moransi with `background_subtract` still `true` leaves the hash unchanged, so `get_stale_tiles` (`manifest.py:312`) reports tiles fresh and skips the rebuild. Also add `background_method` + `background_params` to the `processing` block (`manifest.py:185-190`).
3. **Exposure.** Reuse `CFP_SKY`, set `meta.background.method='moransi'`, and **fold `method` into the `CFP_SKY` provenance string** (the presence-based skip has no config hash — §4.3). Avoid a new `CFP_*` key (it would need adding to `NIRCAM.keys` *with a comment*, else `Keyset.__post_init__` raises, plus `_STEP_LABELS`/`_SCI_MUTATING_STEPS` churn) — not worth it for an A/B.
4. **DB/web — only if needed.** A pure pipeline A/B needs nothing here. If it must be portal-queryable, add `storage_objects.background_method` (`supabase/schemas/tables.sql` + generated migration) populated from `CMPFRBKG` at `campfire deploy`.
5. **Layout — avoid a method axis.** Side-by-side deployment of both methods would need a filename/tile method axis, cutting against the deliberately retired version axis (N2, #264) and colliding at the web map-tile layer. **Prefer sequential A/B** (run A, inspect, run B into the same name, re-deploy); the extended `config_hash` forces the rebuild and content-hash dedup handles re-upload.

Per CLAUDE.md, a background-algorithm change is **Calibration → MINOR**; any `pipeline/**` PR must add a categorized entry under `## Unreleased` in `pipeline/CHANGELOG.md`.

---

## 8. Risks & open questions (ranked)

1. **[Correctness — engineering-owned]** Invalid pixels set to `0` before the statistic (§6.4). The one true bug; must be fixed before any real run.
2. **[Science — needs your judgment]** Should Moran's-I be applied as a **2-D subtraction to exposures** at all? The pipeline applies no 2-D model per-exposure by design to avoid negative galaxy wings (`config_default.toml:159`). **Recommendation: default mask-only on exposures (`subtract_2d=false`); reserve applied-2-D for mosaics.** Decision required.
3. **[Science — needs your judgment]** Acceptance thresholds and ground truth (§5): are Δ≤0 `bkg_width`, no deeper radial trough, <0.05% photometry, bias-σ parity, and an injected-source/blank-field anchor the right bar?
4. **[Science]** `patch_size`/`percentile` recalibration across drizzled vs native and SW vs LW (§4.4). The percentile sweep addresses this; the arcsec-scale default needs sign-off.
5. **[Correctness]** Applied-2-D exposure case interacts with the **variance** step: `variance` runs *after* `sky` and rescales `VAR_RNOISE` assuming a ~flat sky; subtracting a 2-D model in sky changes the sky statistics `variance` then depends on. If applied-2-D on exposures is ever pursued, the `variance`↔`sky` ordering must be revisited.
6. **[Correctness]** 1/f autocorrelation confound on exposures (§4.4) — mitigated by running after `striping` and/or an anisotropic kernel; verify empirically in the exposure A/B.
7. **[Perf]** Vectorization of `morans_i_local` — mandatory for exposures, strongly advised for tiles.
8. **[Downstream]** `config_hash` not extended (§7.2) → silent stale-tile rebuild skip; and exposure `CFP_SKY`-presence skip → silent method-switch no-op (§4.3). Both must land with the feature.
9. **[Scope]** `gradient.py` wing-preservation mask — defer (shares all maturity problems; second tuning surface).
10. **[Env]** photutils pin vs installed version and the `campfire` conda env (§6.13) — verify the deploy env.

---

## 9. Phased implementation plan

Ordered so the **A/B harness runs before any default is committed** (Phase 3 gates Phase 5).

**Implementation status (2026-07-02): Phases 0–3 landed for the mosaic path; all 217 pipeline tests green (16 new).** Phase 3's harness is built and self-tested on synthetic data; the *scientific* A/B run on real tiles (Phase 3b) and the exposure work (Phase 4) are the remaining, serial follow-ups.

| Phase | Status | Deliverable | Files touched |
|---|---|---|---|
| **0 — Vendor & clean** | ✅ done | `moransi_bkgsub.py`: cleaned, numpy-native, NaN/valid-aware `morans_i_local` + `morans_source_mask`; `gradient.py` deferred; fixes §6.1–6.6, 6.10. | `nircam/moransi_bkgsub.py` |
| **1 — Vectorize + fit reuse + tests** | ✅ done | Vectorized `morans_i_local` (matches naive ref, NaN-aware); `mask_method` + `moransi_*` fields on `SubtractBackground`, branch in `compute()` reusing `estimate_background`; `variance` confirmed pinned to tiered; 12 tests (vectorization equivalence, zero-fill regression, mask polarity/fraction, integration). | `bkgsub.py`; `tests/test_moransi_bkgsub.py` |
| **2 — Wire mosaic + provenance** | ✅ done | `mask_method`+params into `resample_step`; stamps `CMPFRBKG`/`BKGPATCH`/`BKGPCTL`/`BKGKERN`; `config_hash` via shared `_resample_config_hash` (folded in only when non-default → no mass rebuild) + `processing.background_method`; TOML defaults; CHANGELOG (Algorithm, additive). 4 more tests. | `steps/resample.py`; `manifest.py`; `bkgsub.py`; `config_default.toml`; `CHANGELOG.md` |
| **3 — A/B harness** | ✅ built | Runnable `run_ab.py`: arms `tiered` + `moransi:{40,50,60}`, primary metrics (`bkg_width`, radial `trough`, `flux_ratio`, `masked_frac`, `runtime`) reusing `ab_metrics`; `summary.md` + panels; `--selftest` verifies end-to-end on synthetic data. | `experiments/moransi_ab/run_ab.py` + README |
| **3b — Scientific A/B run** | ✅ done (2026-07-04) | Ran the mosaic A/B on **EGS F444W** (ceers2 pointing + full non-contiguous field). Tiered wins; default stays `"tiered"`. See "Phase 3b results" below. | `experiments/moransi_ab/run_ab.py` (harness hardening) |
| **4 — Per-exposure (serial follow-up)** | ⏳ later | Mask-only Moran's-I at the **striping** seam (`_build_srcmask`) via the same `mask_method` switch; arcsec→px scaling; fold `method` into `CFP_SKY`; 1/f-order study; exposure A/B. Applied-2-D (`sky_step`, `subtract_2d`) only if favorable, with negative-wing + variance-ordering validation. | `steps/striping.py:54`; `steps/sky.py:23`; `config_default.toml` |
| **5 — Default flip + release (conditional)** | ⏳ later | Flip default per favorable regime; re-categorize CHANGELOG **Calibration → MINOR** at flip; optional `storage_objects.background_method` column + web surfacing. | `config_default.toml`; `CHANGELOG.md`; (opt) `supabase/schemas/tables.sql` + migration |

**Critical path to a decision:** Phases 0→3 (~6–8 days) deliver a fully instrumented mosaic A/B with the incumbent held as baseline and the fitter held constant — enough to accept/reject Moran's-I for mosaics before investing in the more delicate exposure integration (Phase 4).

### Phase 3b results (2026-07-04, EGS F444W)

**Decision: keep `mask_method = "tiered"` as the default.** Moran's-I did not beat tiered on the one field tested; it is retained as an opt-in for future work.

Ran the mosaic A/B on the freshly-reduced EGS F444W tiles — the `ceers2` pointing (71 Mpix, ~78% covered) and the `full` non-contiguous CEERS field (2.42 Gpix, 32.5% covered) — arms `tiered` + `moransi:{40,50,60}`, `Background2D` held constant.

- **Sky uniformity:** on a *common blank* (identical pixels every arm), Moran's-I is marginally *worse* than tiered but the gap is small (Δ`bkg_width` ~ +0.3–2%). The larger pooled gaps first seen (+2% ceers2, +6% full) were partly the different-pixel-set confound.
- **The real failure is bright-source oversubtraction.** Moran's-I masks *patches* but never **dilates** around sources, so bright stars/spikes leak into the `Background2D` fit → source-shaped bumps in the background model → deep negative bowls in the subtracted image (≈ −5.5σ annulus depression vs tiered; clearly visible in the zoom cutouts). `flux_ratio` stayed ≈1.0 because the *local-annulus* aperture photometry sits inside the same depression and cancels it — so `flux_ratio` alone is **not** a sufficient oversubtraction guard; the bg-model imprint and the zoom cutouts are.
- **Cause is the missing dilation, not the threshold.** A tuning sweep (percentile 40→30→20, patch 20→10, dilation 0→7→15 px) showed lower percentile / finer patches do **not** help (patch-10 is *worse*, −9σ), while a ~7 px mask **dilation** restores tiered-like behavior (oversubtraction −5.5σ → −0.6σ). Tiered's advantage is precisely its `tier_dilate_size` growth around bright objects; `morans_source_mask` currently has none.
- **Speed:** Moran's-I is ~20–40× faster than tiered (tiered took 5.0 h on the 2.42-Gpix full tile; Moran's-I ~15 min). That is its only clear win.

**If revisited:** add a `dilate`/`dilate_arcsec` parameter to `morans_source_mask` (analogous to `tier_dilate_size`) and re-run the A/B — even dilated it only *ties* tiered on quality here, so the case for switching rests on speed. Broader stratification (SW channel, other fields/depths) was not exercised; only EGS F444W (LW) was tested.

**Load-bearing code locations for review:** donated core `~/Downloads/moransi_background_code/moransi_background.py:25,47,74,88,137,191,236,241`; `gradient.py:10`. CAMPFIRE seams `pipeline/campfire_pipeline/nircam/bkgsub.py:50,87,112,160,292,322,381,414,424`; `steps/resample.py:279-322`; `steps/sky.py:23-88`; `steps/striping.py:54`; `steps/variance.py:41`; `manifest.py:167-173,300-306`; `common/cfp.py:66-100,115`; `data/config_default.toml:159,192,284,398,414`; `experiments/oneoverf_gp/ab_metrics.py`, `diag_bgsub_boxsize.py`.
