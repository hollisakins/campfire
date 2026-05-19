# Design: MIRI Imaging Reduction Arm

**Status:** Proposed
**Date:** 2026-05-19
**Branch:** (not yet started)

## Problem

CAMPFIRE currently reduces JWST NIRSpec (spectroscopy) and NIRCam (imaging)
data. Adding a MIRI (Mid-Infrared Instrument) imaging arm would extend coverage
to 5–28 µm, which is scientifically essential for:

- Dust-obscured AGN and rest-frame mid-IR diagnostics of high-z galaxies
- COSMOS-Web MIRI parallel imaging (~0.2 deg² in F770W, already public)
- Future EMBER/ZENITH ancillary MIRI imaging in the same fields

MIRI imaging is structurally similar to NIRCam imaging — the same JWST pipeline
stages (`calwebb_detector1` → `image2` → `image3`) apply — but the stock
pipeline alone is not sufficient for publication-quality reductions. Four
custom steps have become community standard, drawn from the SMILES (Alberts+
2024, arXiv 2405.15972), JADES DR5 (Alberts+ 2026, arXiv 2601.15955), and
COSMOS-Web (Harish+ 2025, arXiv 2506.03306) reductions.

This document scopes the addition: which custom steps to implement, how they
slot into CAMPFIRE's existing architecture, and what design decisions to lock
in before writing code.

## Design Principles

- **Reuse NIRCam structure.** MIRI is a parallel sibling to the existing NIRCam
  arm, not a refactor of it. The orchestrator pattern, `CFP_*` provenance keys,
  `StepStatus` cache, and `atomic_save()` lifecycle transfer verbatim. Shared
  imaging infrastructure can be extracted to `common/` opportunistically as
  duplication appears.
- **Leverage CAMPFIRE's NIRCam mosaics.** COSMOS-Web NIRCam reductions are
  already in-house, deeper than MIRI by 1–2 mag, and Gaia-tied. They enable
  shortcuts (source masking, astrometric alignment) that the published MIRI
  reductions do not take because their authors did not own the NIRCam data.
- **Single algorithm, pluggable inputs.** Rather than implementing
  Harish-style and SMILES-style super-backgrounds as separate code paths,
  implement one unified single-pass algorithm whose donor set is determined by
  an epoch-grouping step. The two literature recipes become different
  populations of the donor stack, not different algorithms.
- **Defer iteration.** Iterative super-background (SMILES 3-pass) buys little
  when the source mask comes from a deeper companion catalog. Skip in v1; add
  as an optional refinement pass later if a no-companion field arrives.
- **Pin to JADES-era pipeline.** `jwst >= 1.16.1`, CRDS context `>= 1303`. The
  persistence-propagation step requires the JADES treatment, and the CRDS
  context already includes the post-1202 `find_showers` per-filter defaults.
  Use same CRDS context as default CAMPFIRE. 

---

## Part 1: Pipeline Architecture

### Module layout

```
pipeline/campfire_pipeline/miri/
├── __init__.py
├── cli.py                       # cfpipe miri {process,combine,status,reset,...}
├── orchestrate.py               # _RUNNERS dispatch, PROCESS_STEPS, COMBINE_STEPS
├── field.py                     # MiriField dataclass (mirrors NircamField)
├── constants.py                 # MIRI filters, MIRIMAGE geometry (1024×1024)
├── groups.py                    # epoch-grouping algorithm (see Part 3)
├── steps/
│   ├── persistence_prepass.py   # CFP_PERS — pre-detector1 ensemble step
│   ├── detector1.py             # CFP_DET1 — reads modified uncals
│   ├── image2.py                # CFP_IMG2 — pipeline bkg_subtract NOT used
│   ├── warm_pixel_build.py      # CFP_WARM (build)
│   ├── warm_pixel_apply.py      # CFP_WARM (apply)
│   ├── super_bg_build.py        # CFP_SBKG (orchestration)
│   ├── super_bg_apply.py        # CFP_SBKG (per-cal subtraction)
│   ├── edge.py                  # CFP_EDGE — likely shared with NIRCam
│   ├── variance.py              # CFP_VAR — likely shared with NIRCam
│   ├── apply_masks.py           # CFP_MASK — likely shared with NIRCam
│   ├── jhat.py                  # CFP_JHAT — NIRCam-catalog refcat for COSMOS
│   ├── outlier.py               # CFP_OUT — shared algorithm, MIRI shape
│   └── resample.py              # final mosaic
└── super_bg/
    ├── sourcemask.py            # NIRCam-catalog + photutils mask backends
    ├── reproject.py             # sky-frame → per-cal detector-frame mask
    └── filters.py               # Background2D, row/col median wrappers
```

### Phase model

```
PROCESS phase (per-exposure, parallel):
  persistence_prepass [ensemble]
    → detector1
    → image2
    → warm_pixel_apply
    → super_bg_apply
    → edge, variance, apply_masks, jhat

COMBINE phase (ensemble, serial):
  warm_pixel_build
    → super_bg_build
    → outlier
    → resample
```

Two new ensemble→exposure handoffs that NIRCam doesn't have: persistence
propagation runs *before* `detector1` (inverting CAMPFIRE's current
"detector1 first" assumption), and super-background's `apply` step depends on
the `build` step's per-group outputs.

### Code reuse from NIRCam

**Move to `common/imaging.py` when extracting:**
- `nircam/geometry.py::select_overlapping_files()` — already parameterized on
  `in_shape`, just needs to leave the NIRCam namespace.
- `nircam/status.py::StepStatus` — zero instrument coupling.
- `nircam/bkgsub.py::SubtractBackground` — purely algorithmic.

**Copy with minimal change (candidates for future shared modules):**
- `edge`, `variance`, `apply_masks`, `jhat`, `preview` — instrument-agnostic
  algorithms wrapped in instrument-aware step headers.

**NIRCam-specific, no MIRI counterpart:**
- `wisp.py` — NIRCam-only artifact.
- `persistence.py` (snowblind) — NIRCam-specific.
- `striping.py` — NIRCam's 4-amp/512-col geometry; MIRI's amp readout is
  different. Defer until we have MIRI data showing what striping correction is
  needed.

### `CFP_*` provenance keys

`common/cfp.py` currently encodes NIRCam's ordered key chain. The cleanest
factoring is per-instrument key lists: keep NIRCam's `CFP_KEYS` unchanged, add
a separate `MIRI_CFP_KEYS` constant. The `clear_from()` machinery and FITS
header stamping are reused as-is.

---

## Part 2: Custom Reduction Steps

Four steps that go beyond `calwebb_detector1/image2/image3`. The bg-subtraction
recipe (3) is the highest-leverage; (1) and (2) are smaller; (4) is config only.

### 2.1 Persistence propagation

**Source:** JADES DR5 (Alberts+ 2026), §III.1.1. **Not used in SMILES or Harish.**

**Concept.** MIRI exhibits *negative persistence* — pixels that saturated in
an earlier exposure show suppressed sensitivity for tens of minutes to hours
afterward, surviving filter wheel changes. Mitigate by running only
`group_scale + dq_init + saturation` on the time-ordered uncal stack first,
identifying pixels with <3 unsaturated groups in any exposure, then propagating
`DO_NOT_USE` forward into the `PixelDQ` of every subsequent exposure in the
same visit sequence — regardless of whether those exposures saturate at the
affected pixel.

**Module:** `miri/steps/persistence_prepass.py`. New ensemble step that runs
*before* `detector1`. Mutates uncal files in place via `atomic_save()`.

**Key decision: visit-sequence scoping.** Group by APT visit ID
(`jw{PID}{OBS}{VIS}` from filename). This is finer than CAMPFIRE's `Field` unit
and matches JADES's "observation" semantics.

**Config (`config_default.toml`):**
```toml
[miri.persistence_prepass]
min_unsaturated_groups = 3
propagate_across_filters = true
group_by = "visit"          # alternatives: "observation", "exposure_window"
```

**Pseudocode:**
```python
def persistence_prepass(field: MiriField, cfg):
    for visit_id, uncals in group_by_visit(field.get_uncal_files()):
        uncals_sorted = sorted(uncals, key=lambda u: u.mjd_start)
        accumulated_mask = np.zeros((1024, 1024), dtype=bool)

        for uncal_path in uncals_sorted:
            partial = run_partial_detector1(
                uncal_path,
                steps=["group_scale", "dq_init", "saturation"],
            )

            if accumulated_mask.any():
                inject_into_pixeldq(uncal_path, accumulated_mask,
                                    dqflags.pixel["DO_NOT_USE"])
                stamp_cfp(uncal_path, "CFP_PERS", reduction_version)

            sat = dqflags.pixel["SATURATED"]
            n_unsat = ((partial.groupdq & sat) == 0).sum(axis=(0, 1))
            accumulated_mask |= (n_unsat < cfg["min_unsaturated_groups"])
```

**Open question:** restart semantics. The accumulated mask must be cached
per-visit-sequence to survive interrupted runs. Suggestion: write the mask to
`$field.workspace/persistence_masks/{visit_id}.fits` after each exposure.

### 2.2 Warm/hot pixel temporal mask

**Source:** SMILES §4.2.1, JADES §III.1.2. Closely follows NIRCam's existing
`bad_pixel.py` shape.

**Concept.** Pixels drift warm between CRDS `MASK` reference updates.
Median-stack `_cal` files **in detector frame** within a multi-month epoch,
per filter; flag pixels >3σ above the global median; OR the mask into each
cal's `PixelDQ` during the per-exposure process phase.

**Modules:**
- `miri/steps/warm_pixel_build.py` (combine phase) — builds per-(epoch, filter)
  masks.
- `miri/steps/warm_pixel_apply.py` (process phase) — applies mask to each cal.

**Config:**
```toml
[miri.warm_pixel_build]
sigma_threshold = 3.0           # SMILES = 3
epoch_window_months = 3         # SMILES = whole survey; JADES = per epoch
require_min_cals = 30           # SMILES had 60-66 per filter
dq_bit = "OTHER_BAD_PIXEL"      # NOT DO_NOT_USE — keep distinguishable
```

**Pseudocode:**
```python
def warm_pixel_build(field: MiriField, cfg):
    groups = assign_groups(field.cal_files(),
                           max_gap_days=cfg["epoch_window_months"] * 30)

    for key, cals in groups.items():
        if len(cals) < cfg["require_min_cals"]:
            log.warning(f"warm_pixel: skipping {key}, only {len(cals)} cals")
            continue

        stack = np.stack([fits.getdata(c, "SCI") for c in cals])
        median_image = np.nanmedian(stack, axis=0)
        global_med = np.nanmedian(median_image)
        global_std = mad_std(median_image, ignore_nan=True)
        warm_mask = median_image > (global_med
                                    + cfg["sigma_threshold"] * global_std)

        save_mask(field.warm_pixel_dir / f"{key}.fits", warm_mask)
```

**Apply step:** straight OR of the mask into `PixelDQ`. Use a distinguishable
bit (not `DO_NOT_USE`) so downstream tooling can audit how many pixels were
flagged at this stage.

**Cold/dark pixel branch:** neither paper does this; default one-sided
(warm only).

### 2.3 Super-background subtraction (unified single-pass)

**Source:** Synthesis of Harish (single-pass, NIRCam-mask-eligible) and SMILES
(iterative, MIRI-self-mask). **Single-pass in v1.**

**Concept.** For each target cal, build a sky-background template by
median-stacking all *other* source-masked cals in its group, then subtract.

**Modules:**
- `miri/steps/super_bg_build.py` (combine phase) — orchestrates mask build +
  per-group bg construction.
- `miri/steps/super_bg_apply.py` (process phase) — per-cal subtraction with
  CFP stamp.
- `miri/super_bg/sourcemask.py` — pluggable mask backends.

**Mask source backends:**
1. `nircam_catalog` — load segmentation map from the corresponding NIRCam
   field, reproject to MIRI cals, dilate by MIRI PSF FWHM in pixels. Default
   for any field with NIRCam coverage.
2. `miri_self` — drizzle first-pass mosaic, run photutils source detection.
   Fallback for fields without NIRCam.
3. `external` — accept a user-supplied segmap or region file. Escape hatch.

**Future (v2):** chained backends — run with `nircam_catalog` mask first,
then run a second pass with `miri_self` mask built from the cleaned mosaic.
Catches sources with dramatically different NIR/MIR brightness ratios (AGN,
heavily obscured starbursts). Defer until v1 is in production.

**Config:**
```toml
[miri.super_bg]
mask_backend = "nircam_catalog"     # alternatives: "miri_self", "external"
nircam_dilate_arcsec = 0.5          # ≈ MIRI PSF FWHM at F770W
miri_self_threshold_sigma = 1.5     # only used if mask_backend = "miri_self"
miri_self_min_npixels = 10
bkg2d_box = [128, 128]              # photutils.Background2D box_size
bkg2d_filter = [3, 3]               # photutils.Background2D filter_size
rowcol_window = 51                  # median filter window
exclude_self_from_stack = true      # SMILES verbatim
fallback_min_donors = 10            # below this, skip super-bg, log warning
hole_fill_method = "median"         # alternatives: "interpolate", "nearest"
```

**Pseudocode:**
```python
def super_bg_build(field: MiriField, cfg):
    groups = assign_groups(field.cal_files())
    mask_backend = make_mask_backend(cfg["mask_backend"], field, cfg)

    for key, cals in groups.items():
        if len(cals) < cfg["fallback_min_donors"]:
            log.warning(f"super_bg: {key} has {len(cals)} cals; "
                        f"applying 2D-gradient + row/col only")
            for cal in cals:
                apply_gradient_and_rowcol_only(cal, cfg)
            continue

        for target in cals:
            donors = [c for c in cals if c.path != target.path]

            zero_med_stack = []
            for donor in donors:
                donor_mask = (mask_backend.project_to(donor)
                              | edge_mask(donor))
                donor_data = donor.data.copy()
                donor_data[donor_mask] = np.nan
                zero_med_stack.append(donor_data - np.nanmedian(donor_data))

            super_bg = np.nanmedian(np.stack(zero_med_stack), axis=0)
            super_bg = fill_holes(super_bg, method=cfg["hole_fill_method"])

            target.data -= super_bg

            bkg2d = photutils.Background2D(
                target.data,
                box_size=cfg["bkg2d_box"],
                filter_size=cfg["bkg2d_filter"],
                bkg_estimator=MedianBackground(),
                mask=mask_backend.project_to(target),
            )
            target.data -= bkg2d.background
            target.data = subtract_row_col_medians(
                target.data,
                window=cfg["rowcol_window"],
                mask=mask_backend.project_to(target),
            )

            stamp_cfp(target, "CFP_SBKG", reduction_version)
            atomic_save(target)
```

**Slot in pipeline:** between Image2 and Image3. Pipeline's own
`Image2Pipeline.bkg_subtract` is *disabled*. Stage 3 `skymatch` runs as normal
to handle residual leveling.

**Memory pressure:** stacking ~60 MIRIMAGE cals at float32 is ~250 MB per
filter. Single-process combine step; do not parallelize the donor stack across
workers.

**Reprojection cost:** projecting one sky-frame segmap to N detector frames is
the per-group bottleneck. Pre-compute the per-cal inverse pixel→sky→pixel maps
once and cache; use `reproject` package.

### 2.4 Jump step (find_showers) tuning

**Source:** SMILES §4.1. **Effectively config-only.**

**Concept.** Enable `jump.find_showers` for F560W–F1500W; disable for
F1800W–F2550W (thermal-background-dominated, where the correction introduces
arcsec-scale artifacts). The CRDS post-1202 defaults already encode this, so
pinning `CRDS_CONTEXT >= 1303` likely makes this a no-op.

**Implementation.** Per-filter override via existing `[miri.detector1.jump]`
section in `fields.toml`, mirroring NIRCam's per-step override pattern.

**No new code required** beyond verifying the per-filter override mechanism
exists in the MIRI config-resolution chain.

---

## Part 3: Group Assignment Algorithm

The super-background and warm-pixel-mask steps both depend on grouping cals
into "donor pools" where the background level is approximately constant.

### Grouping axes

**Must-split (hard):**
- **Subarray** — different array shapes can't co-add in detector frame.
- **Filter** — different zodi, thermal contribution, photom calibration.
- **Field** — CAMPFIRE's existing `Field` unit.
- **Epoch** — bg level drifts on weeks-months timescale.

**Soft-check (warn but don't split unless mixed):**
- `READPATT` — typically constant within a (field, filter, program).
- `NGROUPS` / `NINTS` — bg in MJy/sr is rate-normalized, so these shouldn't
  affect bg level. Sanity-check mixing rather than hard-split.

**Don't matter:**
- Exposure time per integration (absorbed by MJy/sr units).
- Dither pattern (affects mask coverage holes only).
- Position angle (PA diversity is actively *good* for mask quality).

### Epoch identification

Gap-based 1D clustering on MJD. Sort cals by start time; split anywhere there's
a gap larger than `max_gap_days`.

```python
def group_into_epochs(cals, max_gap_days=14):
    cals_sorted = sorted(cals, key=lambda c: c.mjd_start)
    epochs = [[cals_sorted[0]]]
    for cal in cals_sorted[1:]:
        if cal.mjd_start - epochs[-1][-1].mjd_end > max_gap_days:
            epochs.append([cal])
        else:
            epochs[-1].append(cal)
    return epochs
```

**Default `max_gap_days = 14`** sits between the JWST plan-window cadence
(~7 days) and the SMILES/JADES epoch separations (months). Per-field override
in `fields.toml`.

### Full group key

```python
GroupKey = (field, filter, subarray, epoch_id)
```

`epoch_id` is an integer index from the gap-clustering pass. Cache the
`(field → epochs)` decision at config-resolution time so re-runs are
deterministic.

### Minimum-donors fallback chain

When a group has fewer than `fallback_min_donors` cals:
1. Merge with adjacent-in-time epoch (same field, filter, subarray).
2. If still too few, use all-epochs-same-(field, filter, subarray).
3. If still too few, log warning and skip super-bg for those cals (apply only
   2D-gradient + row/col filter).

### Introspection subcommand

`cfpipe miri groups --field <name>` prints the groups it would form, with
donor counts per group. Run this after every new MIRI observation arrives to
catch oddities (a visit at a weird time, a single-exposure group, subarray
mixing) before they bite the bg subtraction.

---

## Part 4: COSMOS-Web–Specific Shortcuts

CAMPFIRE already owns the COSMOS-Web NIRCam mosaics, Gaia-tied to ~10 mas
precision. Two shortcuts none of the published MIRI papers take, because their
authors did not own the companion NIRCam reductions.

### 4.1 NIRCam-catalog source mask

Replace the first-pass MIRI mosaic + SourceExtractor step with a NIRCam
segmentation map dilated to MIRI PSF size. NIRCam goes 1–2 mag deeper than
MIRI; the resulting mask captures faint extended emission MIRI itself cannot
detect.

Implemented as the `nircam_catalog` backend in `miri/super_bg/sourcemask.py`.
Inputs: NIRCam mosaic + segmap path from `fields.toml`, dilation kernel size.

### 4.2 NIRCam-direct astrometric alignment

Skip alignment to HST/F814W (Harish: 28 mas MAD). Align MIRI directly to the
COSMOS-Web NIRCam catalog via JHAT — higher source density at MIRI-relevant
magnitudes, already Gaia-tied. Expected precision: ~10 mas.

Implemented by pointing the existing `jhat.py` step at the NIRCam refcat
instead of Gaia DR3 or HST.

---

## Part 5: Cross-Cutting Work Beyond the Pipeline

Adding MIRI touches more than `pipeline/`. From the touchpoint audit:

### Database (Supabase)

- **New tables** mirroring NIRCam: `miri_images`, `miri_exposures`.
- **New view:** `miri_reduction_progress` (per-(field, filter) per-stage
  counts).
- **RPC extension:** `get_filtered_objects_paginated` — extend signature with
  MIRI filter parameter, or generalize to instrument-keyed filter pool.

### Deploy (`python/campfire/deploy/`)

- **New module** `deploy/miri.py` mirroring `deploy/nircam.py`. Discovery,
  filter detection, exposure upload.
- **Tile + RGB engines** (`tiles_engine.py`, `generate_rgb.py`) currently
  filter-hardcoded for NIRCam. Refactor to instrument-dispatch rather than
  copy-paste, since MIRI tiles use different pixel scale (0.11"/px vs NIRCam
  0.031"/0.063").
- **New deploy subcommand:** `campfire deploy miri --field <name>`.

### Web (`web/`)

- **Types:** `MiriImage`, `MiriExposure` interfaces in `web/lib/types.ts`;
  `MIRI_STAGES` constant.
- **Server actions:** `getMiriFilterOptions()` in `web/lib/actions/miri.ts`.
- **UI components:** `MiriFilterBar.tsx`, `/app/miri/page.tsx` route.
- **Admin:** `/admin/miri/` and `/admin/miri/[id]/` for exposure review.

### Config

- New `[miri.*]` block in `pipeline/campfire_pipeline/data/config_default.toml`.
- New `[miri]` section in field definitions in `fields.toml` (filters,
  per-field overrides).
- New `campfire-miri` entry point in `pyproject.toml`.
- Extend `INSTRUMENT_DEFAULTS` in `cli.py` with MIRI `exp_type`.

---

## Part 6: Implementation Sequencing

Suggested build order:

1. **Pipeline scaffolding.** `miri/` package, `MiriField` dataclass,
   `[miri.*]` config block, CLI dispatch (`cfpipe miri ...`), stock-only
   detector1+image2+image3 working end-to-end on a test field. Verifies the
   skeleton before adding custom steps.

2. **Warm pixel mask.** Smallest piece of genuinely new code; closest parallel
   to existing NIRCam `bad_pixel.py`; no orchestrator changes. Establishes the
   pattern for combine→process handoffs.

3. **Group assignment + introspection subcommand.** Build `groups.py` and
   `cfpipe miri groups`. Needed by both warm-pixel and super-bg; tested
   independently first.

4. **Super-background (Harish-shaped, single-pass).** Highest scientific
   leverage. Implement with `nircam_catalog` backend first; `miri_self` second;
   `external` third. NIRCam-direct astrometric alignment (4.2) folds in here
   since the same NIRCam catalog feeds both.

5. **Persistence propagation.** Requires orchestrator surgery (pre-detector1
   ensemble phase). Highest-complexity step; least urgent because it matters
   most for visits with multiple-filter sequences, which is a fraction of the
   data.

6. **Jump tuning.** Verify per-filter overrides reach the JWST `Step.call()`
   parameters correctly. Likely a no-op if pinned to CRDS ≥ 1303.

7. **Cross-cutting work** (database, deploy, web) in parallel with steps 4–6.

---

## Open Questions

- **Persistence-prepass restart semantics.** Cache accumulated mask per-visit
  on disk between exposures (proposed) vs. recompute from scratch on each
  invocation? Disk caching is faster on resume but adds state to clean up on
  `cfpipe miri reset`.

- **Cold-pixel branch in warm-pixel mask.** Both papers do warm-only.
  Symmetric (warm + cold) might be worth ~1 day of effort. Defer until we see
  MIRI dark current behavior in our actual data.

- **Iterative super-bg.** Skipped in v1. The mask-backend interface
  accommodates a future "refine" pass that drizzles the once-cleaned mosaic and
  re-detects, without requiring orchestrator changes.

- **Striping correction.** NIRCam has `striping.py`; MIRI has different amp
  geometry. Defer; assess from real data.

- **Pipeline version pin.** `jwst >= 1.16.1`, CRDS `>= 1303` is the floor
  driven by persistence-propagation. Confirm against current CAMPFIRE
  `CRDS_CONTEXT` in `config_default.toml` — bumping would be a MINOR pipeline
  release.

- **First MIRI field for v1.** COSMOS-Web F770W is the obvious target (data
  public, NIRCam shortcuts available, large enough for grouping to be
  exercised). Confirm before scaffolding `fields.toml`.

---

## References

- Alberts et al. 2024 (SMILES IDR) — arXiv [2405.15972](https://arxiv.org/abs/2405.15972)
- Alberts et al. 2026 (JADES DR5) — arXiv [2601.15955](https://arxiv.org/abs/2601.15955)
- Harish et al. 2025 (COSMOS-Web MIRI) — arXiv [2506.03306](https://arxiv.org/abs/2506.03306)
- Yang et al. 2023 (CEERS MIRI) — arXiv [2307.14509](https://arxiv.org/abs/2307.14509); code: github.com/ceers/ceers-miri
- Argyriou et al. 2023 (MIRI detector effects) — arXiv [2308.16327](https://arxiv.org/abs/2308.16327)
- Dicken et al. 2024 (MIRI imaging flight performance) — arXiv [2403.16686](https://arxiv.org/abs/2403.16686)
- Liu, Crab.Toolkit.JWST — github.com/1054/Crab.Toolkit.JWST
