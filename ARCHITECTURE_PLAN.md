# CAMPFIRE Architecture Plan

*Last updated: 2026-02-25. Reflects discussion between Hollis Akins and Claude Sonnet 4.6.*

---

## Overview

CAMPFIRE is a web portal for sharing reduced JWST NIRSpec spectroscopy and NIRCam imaging. It is built by one person (a PhD student), primarily serves a collaboration network, and has a target horizon of ~1 year until EMBER data release and a public-facing repo. The code is aspirationally a monorepo but currently lacks clear internal contracts.

This document describes the restructuring plan and guiding principles to make the codebase maintainable, publicly usable, and ready to scale.

---

## Goals

1. **Reduce babysitting overhead.** Pipeline runs should not require constant attention. Inspection workflows should be fast and browser-based rather than rsync-heavy manual processes.

2. **Make the repo genuinely public-ready for reproducibility.** Anyone should be able to clone the repo, install `campfire-pipeline`, point it at raw JWST data, and reproduce a reduction without needing any CAMPFIRE cloud credentials.

3. **Scale CAMPFIRE to more users.** The portal should be ready for the broader astronomical community when proprietary EMBER data is released (~1yr).

4. **Unify NIRSpec and NIRCam under one architecture.** The NIRCam pipeline currently lives in a separate repo (`~/codes/nircamx`) with an inconsistent CLI. It should eventually be brought in and given the same structure as NIRSpec.

5. **Preserve manual inspection as a first-class workflow.** Visual inspection of intermediate products is a deliberate quality differentiator, not a bottleneck to automate away. The goal is to make it faster and better-documented, not to remove it.

---

## Principles

These are constraints that should guide all implementation decisions:

### 1. One-way dependency direction

Dependencies flow in one direction only:

```
campfire-pipeline  →  no external deps (standalone, no cloud)
       ↑
campfire-deploy    →  reads pipeline filesystem, writes to Supabase + R2
       ↑
web/               →  reads from Supabase, serves via Vercel

campfire (CLI)     →  reads from Supabase + R2, no dependency on pipeline code
```

`campfire-pipeline` must **never** import boto3, supabase-py, or any cloud SDK. If it did, it would be unusable by anyone without CAMPFIRE credentials. All cloud interaction belongs in `campfire-deploy` or `web/`.

### 2. CAMPFIRE_ROOT for all data

Raw data and pipeline products should **not** live inside the source code repo. A single environment variable `CAMPFIRE_ROOT` points to a directory outside the repo:

```
$CAMPFIRE_ROOT/
├── raw/              # Raw JWST uncal files (from MAST, never synced via CLI)
│   └── {program_id}/
├── products/         # Pipeline outputs; also where campfire CLI syncs to
│   └── {obs_name}/
└── cache/            # CRDS calibration cache, template grids, etc.
```

The pipeline config resolves paths in order:
1. Explicit `data_dir` / `products_dir` in `config.toml` (for overrides)
2. `$CAMPFIRE_ROOT/{raw,products,cache}` if env var is set
3. Raise a clear error — no silent fallback to repo-relative paths

`$CAMPFIRE_ROOT/products/` doubles as the target for `campfire sync`. A collaborator who runs the pipeline locally and one who downloads via the CLI end up with data in the same directory structure. This creates a unified mental model.

### 3. Format config lives in the repo; instance config lives in `$CAMPFIRE_ROOT`

Two types of config files look similar but have different homes:

- **Format config** — defines what fields the software understands, documents available parameters. Committed to the repo. Examples: `pipeline/config.toml`, `pipeline/observations.example.toml`.
- **Instance config** — describes *this specific deployment/workflow* (which observations exist, which programs are tracked). Lives in `$CAMPFIRE_ROOT/config/` and is not source code.

The software resolves instance config in order: `--file` flag → `$CAMPFIRE_ROOT/config/` → error. No silent fallbacks to repo-relative paths.

`observations.toml` is instance config and stays outside the repo until EMBER data release, at which point committing it is actually valuable for reproducibility.

### 4. TOML flag files are fine — and stay that way

The current stuck-closed-shutter TOML format (keyed by file root, `source_id = [shutter_numbers]`) is human-readable, git-committable, and already understood by `reduction.py`. These files should stay in `$CAMPFIRE_ROOT/products/{obs_name}/` alongside pipeline outputs. They are records of inspection decisions, not source code.

The inspection workflow improvement is about *writing* these files faster (via browser UI), not replacing the format.

### 5. No DAG engine

The pipeline runs on the pattern "I sit down and run it when new data comes in." A formal DAG engine (Prefect, Airflow) would be over-engineering. The goal is a composable CLI where individual stages are named, loggable, and re-runnable for specific source IDs — not automated scheduling.

### 6. Containerization is deferred

Containerizing the pipeline (Docker + AWS Batch) is a future goal. It requires the pipeline to be a proper package first. Don't design for containerization now, but don't make decisions that actively prevent it.

---

## Current State Assessment

### What's already in good shape

- **`python/campfire-api`** is a nearly complete public-facing CLI: device-flow auth, sync, Plotly plotting, catalog queries. Needs renaming and a PyPI release, but the code is solid.
- **`web/`** is production-grade: 45+ API routes, inspection UI, admin system, access codes. Significantly more mature than the pipeline.
- **`pipeline/` has zero cloud imports.** `reduction.py` and `fitting.py` import only JWST pipeline libraries, numpy, astropy, etc. The standalone-pipeline principle is already satisfied in the code — it's just not structurally enforced yet.
- **`deploy.py` has smart upsert logic.** User inspection data is preserved by default, overwritten only with `--force-overwrite`. This is correct behavior.

### What needs fixing

- **`pipeline/` is not a Python package.** No `pyproject.toml`. `reduction.py` (3,427 lines) and `deploy.py` (2,402 lines) are monolithic scripts with no unit tests. You can't import pipeline stages, can't run them in CI, can't install them cleanly.
- **`deploy.py` conflates two concerns.** "Read pipeline filesystem outputs and produce metadata" and "upload to cloud + write to Supabase" are mixed in one 2,400-line script. These should be separated: the former belongs in `campfire-pipeline`, the latter in `campfire-deploy`.
- **Raw data and products live inside the repo directory.** `pipeline/data/` and `pipeline/products/` are gitignored but co-located with source code. This must change before the repo goes public.
- **The NIRSpec inspection workflow is CLI-only.** Identifying stuck shutters requires manually inspecting `*_nods.pdf` plots and writing TOML by hand. The TOML structure is good; the UX is not.
- **The NIRCam inspection workflow is painful.** Rsync thumbnails → manual review on laptop → write filenames → rsync back → draw masks in DS9. The identification step (which frames are bad) can be made dramatically faster with a browser-based frame gallery.
- **No observation state tracking.** There's no way to see at a glance: which observations are at which stage, what needs review, what's ready to deploy.

---

## Target Package Structure

Directory names are renamed to be self-describing. Each Python subdirectory is its own installable package with its own `pyproject.toml`.

```
campfire/
├── pipeline/                       # campfire-pipeline (standalone, no cloud deps)
│   ├── pyproject.toml              # name = "campfire-pipeline"
│   ├── campfire_pipeline/
│   │   ├── nirspec/                # from reduction.py, fitting.py, plots.py
│   │   │   ├── stage1.py
│   │   │   ├── stage2.py           # stage2a + stage2b
│   │   │   ├── stage3.py
│   │   │   └── fitting.py
│   │   ├── nircam/                 # from nircamx (Phase 3)
│   │   │   ├── stage1.py
│   │   │   ├── stage2.py
│   │   │   └── stage3.py
│   │   └── cli.py                  # campfire-reduce entry point
│   ├── config.toml                 # Pipeline params (no secrets, committed)
│   └── observations.example.toml   # Format reference only; actual obs list is instance config
│
├── deploy/                         # renamed from scripts/ — CAMPFIRE-specific, needs credentials
│   ├── deploy.py                   # Final product deploy (from scripts/deploy.py)
│   ├── push_qa.py                  # NEW: upload intermediate thumbnails to R2
│   ├── pull_flags.py               # NEW: download inspection flags from Supabase → TOML
│   ├── deploy_nircam.py            # NIRCam deploy (from scripts/deploy_nircam.py)
│   ├── programs.example.toml       # Format reference; actual programs.toml is instance config
│   └── config.toml                 # Credentials (gitignored)
│
├── tools/                          # Dev/maintenance scripts (not deployment, not reduction)
│   ├── generate_seed.py            # from scripts/generate_seed.py
│   ├── generate_slits.py
│   ├── generate_tiles.py
│   └── backfill_*.py
│
├── cli/                            # renamed from python/ — campfire public CLI
│   ├── pyproject.toml              # name = "campfire" (renamed from campfire-api)
│   └── campfire/                   # (unchanged internally)
│
├── web/                            # Next.js (mostly unchanged)
│   └── app/
│       └── pipeline/               # NEW: Pipeline QA + status dashboard
│
└── supabase/
    └── migrations/                 # NEW tables: pipeline_runs, pipeline_qa
```

---

## $CAMPFIRE_ROOT as Working Directory

`$CAMPFIRE_ROOT` is where you do science; the repo is where you develop software. After `pip install -e pipeline/` (editable install), the two are independent — code changes in the repo propagate immediately without re-installing.

```
$CAMPFIRE_ROOT/
├── config/
│   ├── observations.toml       # instance config (not in repo)
│   └── programs.toml
├── raw/
│   └── {program_id}/
├── products/
│   └── {obs_name}/
├── cache/
│   ├── crds/
│   └── templates/
├── logs/                       # pipeline run logs
└── scripts/                    # personal run scripts (not in repo)
    ├── reduce_ember_cosmos.sh
    └── deploy_ember.sh
```

The `$CAMPFIRE_ROOT/scripts/` bash scripts document exactly what was run and with what flags. At data release, these scripts plus `config/observations.toml` and `config/*.toml` go into a **companion release repo** (e.g., `hollis/ember-reduction-release`) that provides the full reproduction recipe. This separates "the software" (`campfire-pipeline`, public) from "what I did with it" (the release artifact).

Reproducibility documentation for the web portal lives at `/docs/observations` — a page that describes the current state of what's in CAMPFIRE. This is complementary to the release repo: the portal shows what's deployed, the release repo shows how to reproduce it.

---

## Phased Plan

### Phase 1 — Foundation (weeks 1–4)
*Prerequisite for everything else. No visible new features.*

**1a. Package `pipeline/` as `campfire-pipeline`**
- Add `pipeline/pyproject.toml` with entry point `campfire-reduce`
- Create `pipeline/campfire_pipeline/nirspec/` sub-package
- Decompose `reduction.py` into `stage1.py`, `stage2.py`, `stage3.py`, `fitting.py` — each file exports the key functions, currently called in sequence in the `ReductionEngine` class
- `scripts/reduce.py` is deleted; `campfire-reduce` becomes the entry point installed by the package
- Goal: `pip install -e pipeline/` works; stages are importable

**1b. Migrate data directories to `CAMPFIRE_ROOT`**
- Add `CAMPFIRE_ROOT` env var resolution to `pipeline/campfire_pipeline/config.py`
- Update `config.toml` `[paths]` defaults to use env var
- Move `pipeline/data/` and `pipeline/products/` to `$CAMPFIRE_ROOT/` (outside repo)
- Verify `.gitignore` no longer needs to cover large files in-repo

**1c. Publish `campfire` CLI to PyPI**
- Rename `python/campfire-api` → `campfire` in `pyproject.toml`
- Verify all imports still work
- Publish to PyPI (first public release)
- Update `web/` docs page with install instructions

---

### Phase 2 — NIRSpec Inspection Workflow (weeks 4–8)
*Makes the stuck-shutter inspection significantly faster.*

**2a. Database: `pipeline_runs` and `pipeline_qa` tables**

```sql
CREATE TABLE pipeline_runs (
    id          serial PRIMARY KEY,
    obs_name    text NOT NULL,
    stage       text NOT NULL,           -- 'stage1', 'stage2a', 'stage2b', 'stage3', 'deploy'
    status      text NOT NULL,           -- 'running', 'complete', 'failed', 'awaiting_review'
    started_at  timestamptz,
    finished_at timestamptz,
    error       text
);

CREATE TABLE pipeline_qa (
    id          serial PRIMARY KEY,
    obs_name    text NOT NULL,
    source_id   text,                    -- NULL for frame-level (NIRCam)
    frame_id    text,                    -- NULL for source-level (NIRSpec)
    stage       text NOT NULL,
    flag_type   text,                    -- 'stuck_shutter', 'artifact', 'ok', etc.
    flag_data   jsonb,                   -- e.g. {"shutters": [2, 3]}
    note        text,
    flagged_by  uuid REFERENCES auth.users,
    created_at  timestamptz DEFAULT now()
);
```

**2b. `scripts/push_qa.py`** — reads `*_nods.png` thumbnails from `$CAMPFIRE_ROOT/products/{obs_name}/`, uploads to R2 under `qa/nirspec/{obs_name}/stage2a/{source_id}.png`, creates `pipeline_runs` record with `status = 'awaiting_review'`.

**2c. `scripts/pull_flags.py`** — reads `pipeline_qa` rows for an obs, writes `_stuck_closed_shutters.toml` back to `$CAMPFIRE_ROOT/products/{obs_name}/`.

**2d. Web: `/pipeline` route** — new section of the CAMPFIRE portal (admin/operator-only):
- List of observations with current pipeline stage and status
- Stage 2a review UI: grid of source thumbnails (loaded from R2), each with flag controls (shutter numbers, "ok", free-text note)
- Submit button writes to `pipeline_qa`, updates `pipeline_runs` status to `approved`
- Reuses existing inspection UI patterns (canvas viewer, keyboard shortcuts)

**Note:** The pipeline CLI never calls `push_qa.py` or `pull_flags.py` automatically. The operator runs them manually between stages. The pipeline reads only the TOML file, as it does today. The web UI is an ergonomic improvement to writing that TOML — not an architectural gate.

---

### Phase 3 — NIRCam Integration (weeks 8–16)
*Higher lift; requires bringing nircamx into the monorepo.*

**3a. Import `nircamx` into `pipeline/campfire_pipeline/nircam/`**
- Port or wrap nircamx stages into the same `stage1.py / stage2.py / stage3.py` structure as NIRSpec
- Unify CLI: `campfire-reduce --obs cosmos_nircam_p1 --instrument nircam --stage 2`
- The config-file-driven approach in nircamx can be kept internally; the CLI interface should match NIRSpec

**3b. NIRCam frame gallery in `/pipeline` UI**
- After stage 2, `push_qa.py --instrument nircam` uploads frame PNG thumbnails to R2 under `qa/nircam/{obs_name}/stage2/{frame_id}.png`
- Web UI: scrollable grid of frame thumbnails with per-frame flag controls ("ok", "artifact — needs mask", free text)
- This replaces the rsync-to-laptop identification step; identifying bad frames happens in the browser
- Output: `pipeline_qa` rows with `flag_type = 'artifact'`, then `pull_flags.py` generates a text file listing flagged frame IDs for the DS9 masking step
- The actual DS9 masking remains manual — it's specialized enough that replacing it isn't worth the effort yet

---

### Phase 4 — Observation State Dashboard (weeks 12–20)
*Makes the overall pipeline status visible without opening a terminal.*

A pipeline status view at `/pipeline` (already introduced in Phase 2, extended here):

```
ember_uds_p4
  Stage 1:   ✓ complete  2026-01-15
  Stage 2a:  ✓ complete  3 sources flagged for re-run
  Stage 2b:  ⏳ awaiting re-runs
  Stage 3:   —
  Deploy:    —

capers_cosmos_p1
  Stage 1:   ✓ complete  2026-02-01
  Stage 2a:  👁 awaiting review  (42 sources to inspect)
  ...
```

This reads from `pipeline_runs`. The CLI writes a new row on each stage start/complete. The web UI is read-only for this view — it's a dashboard, not a control panel.

---

## Explicitly Out of Scope (for now)

- **Formal DAG engine** (Prefect, Airflow, etc.) — "I sit down and run it" does not need orchestration
- **Containerization / Docker** — deferred until the package foundation is solid
- **Cloud compute (AWS Batch, EC2)** — not needed until data volume or collaboration patterns require it
- **Formal `DataProduct` provenance with `parent_ids`** — useful eventually; the `pipeline_runs` + `pipeline_qa` tables are sufficient for now
- **`campfire-models` shared Pydantic package** — premature abstraction until NIRCam is imported and real shared logic emerges
- **In-browser mask drawing to replace DS9** — the identification step is the bottleneck; DS9 masking stays as-is

---


## Open Questions / Deferred Decisions

1. **`campfire-deploy` as a proper package or a just a directory of scripts?** Right now just a directory of scripts is the right home. If the deploy logic grows significantly (multiple instruments, complex provenance), it may warrant its own `pyproject.toml`. Revisit after Phase 3.

2. **How does `campfire-reduce` know when stage 2a is "done" and ready for QA push?** Currently: you run it and it exits. A simple `--write-run-record` flag that creates/updates a `pipeline_runs` TOML or JSON file in the products directory is probably enough, and avoids requiring network access during the reduction run itself.

3. **Multiple rounds of NIRSpec inspection?** The current TOML format (`source_id = [shutter_numbers]`) implies a single flagging pass. If a re-run reveals new problems, the TOML is just edited again. This is probably fine — the UI should allow editing previously submitted flags.

4. **NIRCam masking format compatibility.** When `pull_flags.py` generates the artifact frame list for DS9 masking, what format does the existing nircamx pipeline expect? Needs to be confirmed before Phase 3b.


