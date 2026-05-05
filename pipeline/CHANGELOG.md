# CAMPFIRE Pipeline Changelog

All notable changes to `campfire-pipeline` are recorded here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/), categorized
by scientific impact:

- **Calibration** — changes that alter pixel/flux values for the same input
  (CRDS context bumps, `jwst` upgrades, calibration defaults, reference data).
  Triggers a **MINOR** version bump.
- **Algorithm** — changes that alter output structure or behavior, not
  necessarily values. Triggers **MINOR** (additive) or **MAJOR** (breaking)
  depending on backwards compatibility.
- **Infrastructure** — no scientific impact (CLI ergonomics, plots, perf,
  internal refactors, tests). Triggers a **PATCH** bump.

Versions are git tags of the form `pipeline-vX.Y.Z` (resolved by
`setuptools-scm`). Install a specific release:

```
pip install "git+https://github.com/hollisakins/campfire.git@pipeline-vX.Y.Z#subdirectory=pipeline"
```

Release procedure: edit the `## Unreleased` section below, then run
`scripts/release-pipeline.sh X.Y.Z` (or the `/pipeline-release` slash command).

## Unreleased

### Calibration
- NIRCam stage 1 no longer runs `snowblind.SnowblindStep` after `Detector1Pipeline`.
  The jump step is already configured with `expand_large_events=True`,
  `sat_required_snowball=False`, `expand_factor=2.2`, `sat_expand=2`, and
  `mask_snowball_core_next_int=True` (4000 s), which detects and dilates large
  cosmic-ray clusters at the groupdq level — covering the same cases as
  `SnowblindStep` (and more aggressively, since groupdq flags exclude affected
  groups from the ramp fit and propagate flagging across integrations). The
  `remove_snowballs` orchestrator step and the `[nircam.stage1.remove_snowball]`
  config block are removed. `snowblind` remains a dependency for
  `PersistenceFlagStep` in the persistence step.

### Infrastructure
- NIRCam orchestrator pre-scans every canonical exposure's primary header
  once at the top of `run_process` / `run_combine` / `run_step` and caches
  the set of present `CFP_*` keys in a `StepStatus` object
  (`nircam/status.py`). Each per-exposure step now filters out
  already-stamped files *before* spinning up the multiprocessing pool, so
  no-op passes on a finished field skip the worker spin-up entirely
  (worker processes use `spawn` on macOS, so each one re-imports
  astropy/jwst — that overhead used to be paid once per step regardless
  of whether any work needed to happen). Skymatch and outlier likewise
  short-circuit whole visits whose every member is already up-to-date.
  No change to outputs; `cfp.has_step` remains the fallback path for
  ad-hoc/CLI callers (`status`, `reset`, standalone scripts) and as a
  defensive check inside each step.
- NIRCam `Detector1Pipeline` no longer writes `_rateints.fits`, `_output_pers.fits`,
  `_trapsfilled.fits`, or `_persistence.fits` intermediates. Pipeline-level
  `save_results` is now `False` and the returned rate model is saved explicitly;
  `persistence.save_persistence` and `persistence.save_results` are likewise
  `False`. `_jump.fits` is still written (the jump substep keeps
  `save_results=True`) because `PersistenceFlagStep` reads `groupdq` from it,
  and is removed by the persistence step's cleanup. No change to pixel values
  or `_rate.fits` contents.
- `cfpipe download --instrument nircam` now resolves the per-detector filter
  from MAST's `opticalElements` field (e.g. `"F090W;CLEAR, F410M;CLEAR"`)
  instead of using the fileset's top-level `filter`. Previously a request for
  a single filter pulled in all 10 detectors of every matching fileset and
  tagged them all with the searched filter — so SW detectors landed under
  the LW filter directory (and vice versa) with bogus filter metadata that
  then propagated into `manifest.ecsv` and downstream stages. Pupil-mounted
  narrowbands (`F150W2;F162M`-style) are also handled. Files whose actual
  filter isn't in `--filters` are dropped with a count printed.
- Foundation pieces for the upcoming NIRCam canonical-exposure restructure:
  `common.io.atomic_save` (tmp+rename with optional primary-header updates
  applied in the same atomic operation), the `common.cfp` provenance module
  (ordered `CFP_KEYS`, plus `format`/`has_step`/`get_steps`/`clear_from`),
  and additive `Field.exposures_dir` / `get_exposure_files` /
  `get_exposure_path` getters. Existing stage dirs and getters are
  unchanged; the current pipeline is unaffected.
- Per-step modules `nircam/steps/detector1.py` and `nircam/steps/persistence.py`
  rewritten against the canonical exposures layout (one file per exposure,
  CFP_DET1 / CFP_PERS stamped via `atomic_save`). Not yet wired into a CLI;
  legacy `stage1.py` orchestrator continues to drive the pipeline. The new
  persistence step also moves earlier in the sequence (right after
  detector1) so the 1/f striping source-mask construction sees persistence
  DQ flags — this becomes a real behavior change when the new orchestrator
  lands.
- Per-step modules `nircam/steps/wisp.py` and `nircam/steps/striping.py`
  also written against the canonical layout. Wisp drops the
  `_rate_without_wisps_sub.fits` backup (PDFs generated inline with the
  in-memory before/after arrays). Striping replaces the
  `_rate_1fmask.fits` sidecar with a `SRCMASK` extension on the canonical
  file, written atomically alongside the SCI mutation via the new
  `atomic_save(..., extra_hdus=...)` parameter. Diagnostic PDFs land in
  `exposures/<filter>/diagnostics/`. A small shared
  `nircam/steps/_plots.py` carries the `plot_two` helper so the new
  modules don't import from `stage1.py`.
- Per-step modules `nircam/steps/{image2,edge,sky,variance}.py` round out
  the calibrate-phase per-exposure rewrites. image2 runs
  `Image2Pipeline.call(input, save_results=False)` and atomic-saves the
  returned cal-stage model to the canonical path, re-attaching the
  `SRCMASK` extension that the JWST pipeline doesn't carry through. sky
  reads `SRCMASK` from the canonical file's extension instead of the
  former `_rate_1fmask.fits` sidecar in `stage1_dir`. variance uses a new
  `SubtractBackground.compute()` method that performs the source-rejection
  + background fit in memory only — the legacy `_cal_bkgsub.fits` scratch
  file is no longer written. `SubtractBackground.call()` is refactored as
  a thin wrapper around `compute()` plus the existing FITS write so the
  mosaic-level usage in stage3 is unaffected.
- Per-step module `nircam/steps/jhat.py` finishes the calibrate-phase
  rewrites. Runs `jhat.align_wcs_batch` against a private scratch dir
  (one `TemporaryDirectory` per worker), stamps `CFP_JHAT` with the
  refcat name on the scratch output, then atomic-replaces the canonical
  file. JHAT preserves all FITS extensions through its WCS update so the
  `SRCMASK` extension carries through unchanged. Diagnostic PDFs and
  photometry tables are copied from the scratch dir to
  `exposures/<filter>/diagnostics/` before the scratch dir is cleaned up.
- Mosaic-phase per-step modules
  `nircam/steps/{apply_masks,bad_pixel,skymatch,outlier,resample}.py`.
  apply_masks rebuilds a `CFMASK` extension from the user `.reg` files
  on every run (replaces any existing CFMASK; OR's into DQ — DQ is
  cumulative, so mask removal requires `--reset-from apply_masks`).
  bad_pixel splits into a `build_bad_pixel_masks` ensemble step (writes
  `fl_pixels_<filter>_<detector>.fits` reference products) and a
  per-exposure `bad_pixel_step` that ORs the per-detector mask into DQ.
  skymatch and outlier both run JWST `Image3Pipeline` in a private
  scratch dir per visit, stamp `CFP_SMAT` / `CFP_OUT` on the scratch
  outputs, and atomic-replace the canonicals (with belt-and-suspenders
  capture/restore of `SRCMASK`/`CFMASK`). Outlier manifests now live in
  `exposures/<filter>/manifests/`. resample switches input source to
  `field.get_exposure_files(filter, with_step='CFP_OUT')` so only
  outlier-detection-finished exposures are eligible to be drizzled;
  mosaic outputs and the manifest format are unchanged. `CFP_SMAT` is
  added to `common.cfp.CFP_KEYS` between `CFP_BPIX` and `CFP_OUT`.

### Infrastructure (continued)
- `cfpipe nircam status --field <name>` reads CFP_* keywords across all
  canonical exposures and prints a per-step completion table plus a
  per-step summary (done / skipped / total). Reads each FITS primary
  header once via `cfp.get_steps`.
- `cfpipe nircam reset --field <name>` for clearing pipeline state.
  `--from <step>` clears the named CFP key and every later one on each
  canonical exposure (header-only, atomic via tmp+rename); refused for
  SCI-mutating steps (wisp, striping, image2, sky, variance, skymatch)
  since re-running them on already-mutated data would compound the
  effect. `--uncal` deletes every canonical exposure file (and any
  ``_jump.fits`` sidecars) for the selected filters; reference products
  (`bad_pixel_dir`, `refcat`, mask `.reg` files) and diagnostic PDFs
  are kept. Both modes prompt for confirmation; pass `--yes` to skip.

### Infrastructure (cleanup)
- Removed the legacy NIRCam stage modules (`stage1.py`, `stage2.py`,
  `stage3.py`) and the `engine.py` `ReductionEngine` wrapper. Their
  numerical helpers (`fit_pedestal`, `fit_sky`, `fit_sky_tot`,
  `collapse_image`, `find_optimal_threshold`,
  `measure_fullimage_striping`) move to a new `nircam/skyfit.py` module
  which `steps/striping.py` and `steps/sky.py` now import from.
  `Field.stage{1,2,3}_dir`, `Field.stage_overrides`, and the
  `get_rate_files` / `get_cal_files` / `get_jhat_files` /
  `get_all_jhat_files` / `get_crf_files` / `get_files` getters are
  removed; `Field.exposures_dir` and `get_exposure_files` /
  `get_exposure_path` are the single source of truth for per-exposure
  paths. `manifest.get_stale_tiles` switches its CRF glob to
  `field.get_exposure_files(filter, with_step='CFP_OUT')`. The
  `get_nircam_stage_config` helper is dropped from `config.py` (only
  `get_nircam_step_config` remains).

### Algorithm
- NIRCam pipeline restructured into a two-phase canonical-exposure flow.
  `cfpipe nircam process` runs the per-exposure work (detector1 →
  persistence → wisp → striping → image2 → edge → sky → variance →
  jhat) into a single canonical FITS file per exposure at
  `products/nircam/<field>/exposures/<filter>/<rootname>.fits`. User
  intervention (region masks in `mask_dir/<filter>/<rootname>.reg`,
  exclusion contract) sits between the two phases. `cfpipe nircam
  combine` runs the ensemble work (apply_mask → bad_pixel → skymatch →
  outlier → resample), promoting per-visit Image3Pipeline outputs back
  to the canonical paths via atomic_save. Persistence moves earlier in
  the sequence (immediately after detector1 instead of last in the
  per-exposure flow), so the 1/f striping source-mask construction now
  sees persistence DQ flags — small calibration delta. Snowblind's
  `jumpify` expects a `_rate.fits` filename, so the persistence step
  temporarily munges `meta.filename` around the call. Single canonical
  file per exposure replaces the old rate/cal/jhat/crf chain and the
  `_rate_orig`/`_rate_without_wisps_sub`/`_rate_1fmask`/`_cal_bkgsub`
  scratch files. NIRCam config namespace flattens from
  `[nircam.stage{1,2,3}.<step>]` to `[nircam.<step>]`. Per-field
  overrides in `fields.toml` use the matching flat layout
  (`[<field>.<step>]`). Legacy `cfpipe nircam stage{1,2,3}` CLI
  commands are removed; the new CLI is `process` / `combine` /
  `<step>` / `run` / `check` (status and reset land next). The
  `Image2Pipeline` round-trip drops custom extensions, so image2 and
  the Image3Pipeline-driven steps capture/restore `SRCMASK` / `CFMASK`
  via `atomic_save(extra_hdus=...)`.

## v0.4.0 — 2026-05-04

### Algorithm
- NIRSpec now includes an optional masking step between stage1 and stage2, 
  to mask specific regions contaminated by artifacts (e.g., shorts) which
  then get set as DQ `DO_NOT_USE`. 
- NIRSpec optimal extraction now falls back to a 3-pixel boxcar when the
  in-aperture collapsed cross-dispersion profile is corrupted (fewer than
  3 finite positive pixels, or positive flux / total |flux| below 0.5),
  e.g. due to background over-subtraction. Previously such cases produced
  a near-delta-function profile from the single surviving positive pixel
  and a degenerate optimal extraction. The chosen extraction method is
  recorded in the new `CMPFROPT` primary-header keyword (`'optimal'` or
  `'boxcar-3px'`); the QA profile plot is relabeled accordingly when the
  fallback triggers.

### Infrastructure
- Switched version resolution to `setuptools-scm` with a `pipeline-v*` tag
  prefix, scoping releases to the pipeline subpackage rather than the monorepo
  HEAD. The reduction version embedded in FITS (`CMPFRVER`) is now PEP 440 —
  e.g. `0.4.0` for releases, `0.4.1.dev3+g7f4e2c1.d20260504` for dev builds —
  rather than a raw monorepo git short SHA.
- Added `scripts/release-pipeline.sh` and the `/pipeline-release` Claude Code
  slash command to drive the tag-and-push workflow.
- Scoped the `CMPFRVER` dirty-flag check to `pipeline/`, so edits in `web/`,
  `python/`, `supabase/`, etc. no longer flip `+dDATE` on the pipeline
  version string (#135). `git describe --dirty` checks the whole working
  tree; we now call `git describe` (no `--dirty`) and pair it with a
  pipeline-scoped `git status --porcelain -- pipeline`.

## v0.3.0 — legacy

Initial unified `cfpipe` package version, prior to this changelog format and
prior to setuptools-scm. See `git log -- pipeline/` for the change history up
to this point.
