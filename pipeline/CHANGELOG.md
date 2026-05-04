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
