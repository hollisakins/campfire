# bkg_nonneg — conditioning background subtraction against negative flux

Prototype (2026-07-27) for constraining `SubtractBackground` so the
subtracted image contains no statistically significant negative structure.
Physical prior: true flux >= 0, so every pixel — masked or not — asserts
`bkg(x) <= sci(x) + k*sigma(x)`. Absorbing smooth wing/ICL light stays
intentional; the sole objective is no coherent oversubtraction.

Test data: two 40" A2744 F444W cutouts (30 mas, pre-mosaic-bkgsub) at
`~/Downloads/a2744_f444w_{offcluster_galaxy,cluster_core}_before_40as.fits`.
Both carry negativity baked in by the per-exposure `subtract_2d` stage
(A2744 is a cluster field): a coherent negative lane between the two
bright core galaxies, and a band along the offcluster spiral's major axis.

## Files

- `metrics.py` — validation metrics, independent of the pipeline mask:
  empty-aperture statistics (0.3–1.5" apertures, radial profiles),
  negative-structure detector (smoothed significance map + inverted
  `detect_sources`, with a positive control), mesh mask-fraction maps.
  All significance is empirical (correlated-noise safe).
- `nonneg.py` — the constrained-fit mechanisms (see below).
- `run_cutouts.py` — arm sweep on the two cutouts; writes
  `out/<name>/{maps,profiles}.png` + `summary.json`.
- `test_blank_null.py` — success-criterion #3: synthetic blank tile
  (gradient + correlated noise + faint sources); the constraints must be a
  no-op there.
- `finalmaps.py` — before/after figure for the per-exposure path
  (`out/final_perexp.png`).

## Mechanisms (in `nonneg.py`)

1. **Asymmetric sigma clip** in the main fit
   (`AsymmetricSubtractBackground`, sigma_upper=2). **REJECTED**: biases
   the whole sky estimate low — 1" empty-aperture median jumps
   +0.17 vs +0.014 (offcluster). The handoff's warning #3 realized.
2. **One-sided ceiling meshcap** (`onesided_ceiling_map` + `cap_mesh`).
   Maskless coarse Background2D with hard upper clip (sigma_upper=1)
   tracks the lower envelope of local flux — a valid upper bound on the
   background everywhere (true flux >= 0). Per-image self-calibration
   (`calibrate_ceiling`): robust median offset vs the arm's own masked fit
   on quiet pixels removes the one-sided clip bias; slack = 2x the robust
   scatter of that difference. Enforced at the *mesh* level + re-zoom
   (smooth by construction; a mask-and-refit enforcement is provably a
   no-op for violations under fully-masked regions — the interpolator
   extrapolates identically). `multiscale_ceiling` takes the min over
   box 32/64/128 (min of upper bounds = tighter bound; finer scales catch
   the sky gaps next to bright masked sources).
3. **Residual trough pass** (`trough_correction`, handoff C6). The
   residual is observable even under the fit mask: where its 5-px-smoothed
   map is coherently below -t*sigma_sm (t=2), lower the map by
   (sm + t*sigma_sm) — continuous at the boundary, no seams, pedestal
   bounded by the t-tail. Catches what the ceiling misses (meshes mixing
   wing flux with the trough).

## Results (negative-structure area / deepest trough)

| arm | offcluster | core |
|---|---|---|
| input (per-exp damage baked in) | 6118 px / -3.3σ | 5996 px / -3.0σ |
| perexp64 (current cluster path) | 4837 px / -3.3σ | 6306 px / -3.1σ |
| perexp64 + cap + trough | 2403 px / -2.7σ | **0 px / -2.2σ** |
| mosaic10 (current mosaic fit) | 1905 px / -4.2σ | 10373 px / -6.0σ |
| mosaic10 + msceil + trough | 262 px / -2.9σ | 3418 px / -2.8σ |

- The ceiling alone nearly cleans the coarse per-exposure fit (37/441
  cells capped on core); the trough pass finishes it. On the fine mosaic
  fit the trough pass carries more of the load.
- The constraint also *heals baked-in* negativity: where the image sits
  coherently below zero the ceiling goes negative and pulls the model
  down (adds flux back). Legitimate under the prior, but it means the
  right home for this is the per-exposure stage, with the mosaic stage as
  a second guard.
- Pedestal cost: ceiling arms +1e-5/px (0.1% sigma_pix); trough pass none
  measurable. Blank-tile null: median map shift exactly 0, p99 = +5e-5.
- Aside: the *current* fine-box fit shows a -6σ negative empty-aperture
  median on the synthetic blank tile (absorption of unresolved faint
  flux) — pre-existing, not introduced here.

## Composition + tier-0 A/B (`run_composed.py`, out/composed.png)

- **end2end** (constrained perexp64 -> fresh mask -> constrained mosaic10)
  is statistically identical to `mosaic10_msceil_tr` alone (offcluster
  258 vs 262 px; core 3206 vs 3418 px) with a slightly lower wing pedestal.
  Retroactively, the constrained mosaic stage recovers nearly everything;
  no double-subtraction pathology. (In production the per-exposure fix is
  still the right home -- the damage is drizzle-coherent and should never
  enter the mosaic.)
- **Tier-0 A/B** (offcluster; tier 0 masks 87% of this 40" cutout --
  unrepresentative of a full tile, treat as an upper bound): the
  unconstrained box-10 fit under the tier-0 mask is far WORSE (50368 px /
  -4.5 sigma / 1" apmed -0.22 vs 1905 px without): inside the hole the fit
  is pure extrapolation, box-scale artifacts appear, and the real negative
  lane goes uncorrected. Tier 0 trades "biased local data" (the bowl) for
  "no local data at all"; it provides zero negativity protection inside
  the hole. Needs a >=120" cutout (~3x dilation radius) for a fair
  production-scale verdict.
- **Rescue test** (`t0_rescue.py`): ceiling + trough are mask-independent
  (maskless map; residual visible everywhere), so they still fire inside
  the tier-0 hole: 50368 -> 2544 px, -4.5 -> -2.7 sigma. A -0.13 aperture
  pedestal remains -- nothing anchors the fit level in the hole and the
  k=2 slack tolerates sub-threshold bias. The guard makes the pipeline
  robust to masking choices, but cannot conjure an anchor from no data.

## Mosaic-level "final guard" design (`ms_trough.py`, `ms_trough2.py`, `gated_test.py`)

Candidate production recipe for the mosaic stage (msceil meshcap + trough):

- **Iterate the trough pass per scale** (a single pass under-corrects deep
  troughs: re-smoothing erodes the correction peak). Converges in 2-4
  iterations.
- **Multi-scale** (sigma = 5, 15 px): the sigma=15 pass is what catches
  broad lanes (offcluster). sigma=45 contributed nothing on either real
  cutout and is the worst blank-field offender -- drop it.
- **Detection-gate the correction** (`gated_trough_correction`): apply only
  inside connected regions below -t*sigma_sm of >= 8*sigma_sm^2 px.
  Continuous at the segment edge by construction (correction field is zero
  at the -t isophote). With gating + iteration both real cutouts reach
  **0 px** of detected negative structure (corrected area <= 0.6%).
- **Quantified residual risk**: even gated, the blank-tile null fires on
  0.7%/2% of area (sigma=5/15) -- not on noise but on the box-10 fit's own
  error field (~1.7e-3 wiggles coherent at the ~50 px mesh scale look like
  significant dips at large smoothing sigma). One-sided correction of
  symmetric fit error => median map shift exactly 0, mean pulled ~0.75%
  of sigma_pix. Mitigations to evaluate on real data: t=3 for sigma=15,
  and note the synthetic blank lacks confusion-level faint flux (which
  biases residuals positive), so this is likely an upper bound. Needs a
  real blank-tile cutout for the production regression.

## 120" validation (`run_120.py`, out120 = flux-space, out120dw = depth-aware)

4000x4000 cutouts with WHT, plus production `after` mosaics as reference.
Two separate negativity mechanisms appear at this scale:

1. **Tier-0 hole extrapolation is the dominant source of the wing troughs.**
   A/B at production scale (tier 0 masks 10% of the frame): m10 68,744 px
   vs m10_t0 195,940 px (depth-aware scoring) — and the production `after`
   morphology around the galaxy matches m10_t0, not m10. Tier 0 still
   never fires on the BCGs (even with full context), so it also doesn't
   protect cluster cores. The guard neutralizes the hole: corrections
   land under/around the tier-0 region and the wing trough disappears.
2. **Inter-visit depth strips** carry real (small) sky-level offsets; in
   flux-space scoring they also FAKE negativity (input 182k px flux-space
   -> 119k depth-aware: a third of the flux-space number was scoring
   artifact).

**Depth-awareness is mandatory at this scale** (`err=` arguments
throughout): significance and corrections computed on resid/ERR, mapped
back through local ERR; the one-sided ceiling is fit on sci/ERR (upper
bound survives the transform since flux >= 0) and its clip bias becomes
scale-uniform (-0.21 sigma at box 32/64/128) — one global calibration
valid everywhere. The flux-space version overshot positive in the shallow
strip; the depth-aware version does not. Trough iteration needs
max_iter ~ 8-12 at 120" scale (converges, frac -> 0).

**Scoreboard (depth-aware negative area / deepest trough):**

| arm | offcluster | core |
|---|---|---|
| input | 119,448 px / -5.4σ | 146,157 px / -9.1σ |
| after (production) | 170,854 px / -7.7σ | 153,776 px / -12.7σ |
| m10_t0 + guard | **8,773 px / -7.1σ** | **320 px / -4.1σ** |

Guard survivors on offcluster are ~41 compact (~200-400 px) scattered
patches, NOT the wing trough (fully healed). These are sub-background-
scale features (likely snowball/persistence-class artifacts); correcting
them through the background model is the wrong tool — flag/DQ territory.
Deliberately not chased.

## Lane + edge diagnostics (`analyze_zoom.py`, out120dw/core/zoom_lane.png)

- **Inter-BCG lane (core)**: the trough is (a) baked into the input, (b)
  deepened broadly by production, (c) turned into compact deeper
  pinch-point troughs by the fine-box fit (saddle bias: a smooth fit
  overestimates the background at the valley between two peaks; the lane
  meshes are interpolated from wing-elevated edge cells). The zoom shows
  the guard's blind spot directly: the SCI-based `excl` covers 45% of the
  lane region, so the significance map is undefined over the lane interior
  and the trough gate cannot fire there; surviving blue sits at the excl
  boundary. FIX (not yet applied): build the trough-pass exclusion from
  positive detections in the *residual*, not SCI brightness, and switch
  ERR -> alpha/WHT so ICL Poisson noise stops suppressing trough
  significance and loosening the ceiling.
- **Offcluster strips/border**: quantified, the depth-aware guard does NOT
  overcorrect them (strip median signif -0.31 -> -0.29, f(>+2 sigma)
  0.0042 -> 0.0039; border similarly unchanged). The earlier flux-space
  guard DID overshoot there (visible in out120/maps.png) -- depth-
  awareness removed it. The strips remain coherently negative (median
  -0.3 sigma_sm over ~900k px) in every arm including production: real
  inter-visit DC offsets with sharp coverage-boundary edges, deliberately
  below the guard's 2-sigma regional threshold. Right fix is upstream
  visit-level sky matching, not the background fit.

## v3: alpha/WHT noise model + residual-based exclusion (out120v3)

Both lane fixes applied and validated:

- **Noise model**: sigma(x) = s/sqrt(WHT), s calibrated as the robust
  scale of resid*sqrt(WHT) on unmasked sky. Median ERR/sigma_wht ~ 3.6 on
  sky — the drizzle correlation factor (ERR propagates uncorrelated
  variance; the map's per-pixel scatter is ~3.6x smaller). Consistency:
  the ceiling clip bias is -0.21 in ERR units and -0.77 in sigma_wht
  units (0.21 x 3.6). All guard significance/corrections + scoring now use
  sigma_wht (source-independent — no Poisson suppression over ICL).
- **Exclusion**: built per-arm from positive detections in the
  (equalized) residual, not SCI brightness — negative lanes stay visible
  to the trough gate and the metric.

Scoring changed (lane interior now visible) — v3 numbers are not
comparable to earlier tables. v3 scoreboard:

| arm | offcluster | core |
|---|---|---|
| after (production) | 185,711 px / -6.8σ | 158,892 px / -13.3σ |
| m10(_t0) | 210,486 px / -7.0σ | 52,889 px / -18.7σ |
| guard | **15,534 px / -7.2σ** | **1,314 px / -4.8σ** |

The lane zoom (out120v3/core/zoom_lane.png): area below -2 sigma in the
window drops 57,810 px (after) / 27,388 px (m10, with its true -11.9σ
pinch-trough depth now visible) -> 5,764 px (guard), and the guard's
remnants are shallow + concentrated at the star-spike region. Aperture
medians unchanged (guard +0.0125 vs m10 +0.0130 on core): no pedestal.
Offcluster guard survivors remain the compact artifact-class patches
(flag-don't-correct).

## Open questions / next steps

- Offcluster's residual lane only partially heals (t=2 at sigma_sm=5 px
  misses broad ~2σ-coherent structure). Knobs: larger smoothing scale in
  the trough pass (or multi-scale), t sweep against blank-field false-fire
  rate. Caution: some of that lane may be a genuine artifact (1/f / wisp
  residual) that *should* stay visible rather than be papered over — check
  against the exposure layout before tuning it away.
- Ceiling calibration currently references the arm's own masked fit
  (quiet-pixel diff). On fields with almost no quiet sky this could bias;
  consider calibrating on the blank corners of the full tile instead.
- Tier-0 finding: the 100σ/30k-px pre-tier fires on the offcluster spiral
  but NOT on the A2744 BCGs (too diffuse after 25-px smoothing) — the
  bowl guard doesn't protect cluster cores at all.
- Integration sketch: opt-in config block on `SubtractBackground`
  (`bg_ceiling = true`, boxes, k, trough t/sigma) applied in
  `estimate_background` after the existing `bg_reject` pass; per-exposure
  `[nircam.bkg.bkg2d]` first, mosaic later. Needs the WHT-aware sigma
  logic for variable-depth tiles before production.

## Tier-0 A/B with the guard (`run_m10_guard.py`)

Guard applied to the no-tier-0 fit: offcluster **1,809 px / -7.4σ** vs
15,534 px / -7.2σ with tier 0 (core identical, tier 0 no-op). Tier 0 is
net harmful even guard-cleaned — the guard subsumes its entire purpose
(the bowl it prevented is a negativity failure the guard fixes from the
data side, including where tier 0 can't reach: BCGs, saddles). Production
recommendation: with the guard enabled, drop tier 0. Confirm on 1-2 more
tier-0-firing objects (bright spiked star archetype) before flipping the
default.
