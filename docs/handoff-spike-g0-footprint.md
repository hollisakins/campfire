# Handoff: G0 footprint validation of the spike models on real frames

**Branch:** `claude/diffraction-spike-auto-mask-m8tq8j`
**Date:** 2026-08-14
**Status:** harness built and synthetically validated (`selftest.py` passes:
a perfect model passes every arm, an under-predicting model fails loudly).
Needs the real-frame run — no real data was available in the build session.
**Audience:** a session on a machine with `$CAMPFIRE_ROOT`, the `campfire`
conda env, network access (Gaia TAP + the model bucket), and an existing
NIRCam reduction containing bright stars (G ≲ 15) — ideally one SW and one
LW filter over several magnitudes.

## 1. Context — how we got here (one paragraph)

The spike-masking epic (design doc `docs/design-nircam-spike-masking.md`)
is model-driven: the mask is the isophote of a scaled extended-PSF model,
gated by PA coverage. M1 (shared `ref_cache` engine, PR #441) and M2
(two-grade model packaging + published set `spike_models/webbpsf100_v1`,
PR #440) are merged. The models are **pre-flight** (WebbPSF 1.0.0,
requirements OPD, Feb 2022 — design doc §7 OQ6), which makes **G0 the
load-bearing gate**: if the pre-flight footprints don't envelope real
in-flight spikes to within `grow`, M3 onward is built on sand. G0 is an
analysis deliverable, not a merge — it needs an existing reduction, a
Gaia query, and an afternoon. The harness in
`pipeline/experiments/spike_g0_footprint/` makes it mechanical: model
fetch, star selection, orientation, amplitude fit, per-arm envelope
verdicts and overlay PNGs all automated (method + statistics notes in its
README).

## 2. What to run

```bash
cd pipeline/experiments/spike_g0_footprint

# sanity-check the machinery on this machine first (no data needed):
conda run -n campfire python selftest.py --out ./selftest_out

# the real run — glob liberally; frames without a G<15 star are skipped:
conda run -n campfire python g0_footprint.py \
    --frames '$CAMPFIRE_ROOT/products/<field>/**/image2/*_cal.fits' \
    --out ./g0_<field>
```

Models fetch themselves into `$CAMPFIRE_ROOT/cache/spike_models/` (2 grades
× nearest anchor per filter; the mask grade is ~11 MB per anchor,
photometric ~58–930 MB — only fetched anchors download). Add `--model-dir`
if the set is already on disk.

Coverage to aim for:

- **one SW + one LW filter** (different anchors, different pixel scales);
- **a magnitude spread** — G ≈ 8–10 (spikes far beyond any single frame),
  G ≈ 11–13 (spikes comparable to the frame), G ≈ 14–15 (barely-spiked,
  the mask should nearly vanish);
- a field with **multiple PAs** if available (same star at two PAs is a
  free consistency check on the orientation).

A2744 (already reduced for the wisp/bkg work) is a fine first target;
COSMOS-Web adds the bright-star-rich SW case.

## 3. What to look at

1. **The PNGs first** (the metric is the eye, per house convention): do
   the mask-grade contours hug the real arms? Do the red model profiles
   track the black data profiles out to the threshold crossings? Failure
   smells: an arm the model misses entirely (orientation/geometry), all
   arms systematically short (radial scale / λ anchoring), one-sided
   misses (center offset on a saturated core).
2. **`g0_results.json` verdicts**: per star/level/arm `ok`, extents,
   `n_miss_px` vs `n_spike_px`. Selftest calibration: a perfect model
   shows misses ≲ 1–3% of spike px (noise near the isophote boundary);
   an under-predicting model shows tens of percent and `ok=false` arms.
3. **`dtheta` and its stability**: should be ~constant per detector
   across frames/stars (the pupil is fixed in the detector frame). Record
   the per-detector values — M3 wires orientation from the WCS chain and
   this is its ground truth. The flip flag alone is meaningless (mirror
   degeneracy — README).
4. **Fitted amplitudes vs expectation**: a *uniform* footprint scale error
   is absorbed by the amplitude fit (power-law arms make stretch ≈
   amplitude — selftest note), so also sanity-check `amplitude` across
   stars of known G: consistent trend = fine; wild scatter or a strong
   λ dependence = model-profile problem the envelope test can't see.
5. **`censored_*` flags**: `censored_model` = the star's spikes exceed the
   model's 240″ FOV — that's the capsule-fallback regime (§3.2), count
   how often it happens at each mag; `censored_frame` = the frame edge
   truncated the data extent (test passes conservatively; fine unless
   it's most arms).

## 4. Pass criteria (the G0 verdict)

- **PASS**: across stars/filters, at 3σ/5σ/10σ, the envelope holds
  (`ok=true`) for ~all arms — occasional single-arm failures traceable to
  contamination (a galaxy in the wedge) don't count; systematic ones do.
  Mask-grade behavior on real spikes matches the synthetic result
  (misses stay at the noise level).
- **CORRECT**: failures are *systematic and parametrizable* — e.g. all
  arms short by a fixed factor at one anchor (fix: per-anchor radial
  scale), or a constant PA offset (fix: orientation constant). Record the
  correction, patch `build_spike_models.py`/the M3 model layer
  accordingly, re-run. This outcome still green-lights M3 with the
  corrected parameters.
- **FAIL**: footprint *shape* is wrong (arms the model doesn't have,
  strut/primary ratio badly off, halo shape unusable) — the pre-flight
  provenance bites. M3's model backend then needs an in-flight model
  source (e.g. STPSF/WebbPSF current OPD, or empirical stacking) before
  proceeding; the harness stays the referee.

## 5. Recording the result

Write `docs/findings-spike-g0-footprint.md` (pattern:
`findings-aniso-detrend-a2744.md`) with: fields/filters/stars used, the
verdict per §4, representative PNGs, per-detector `dtheta`, amplitude
trend, and any correction parameters. Update the design doc: §7 OQ6
(provenance caveat → measured answer) and §3.2 if corrections apply.
G0 PASS/CORRECT unblocks **M3** (spikes subpackage, report mode — the
next milestone, and the biggest PR of the epic); its build plan is in
§6.1.

## 6. Open items the run should settle

1. The **isophote-level range that matters in practice**: G0 defaults to
   3/5/10σ per-frame; mosaics reach deeper. If arms at 3σ per-frame are
   already enveloped, the mosaic-level margin comes from `grow` — note
   whether that looks sufficient or whether M3 should evaluate levels
   against mosaic depth instead.
2. **`--r-min` default** (3″): is the inner halo/core handoff clean, or
   does the saturated-core region need a larger exclusion at the bright
   end?
3. How often the **capsule-fallback regime** (`censored_model`) occurs
   per magnitude bin — sizes the priority of the §3.2 fallback backend
   inside M3.
