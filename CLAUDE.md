# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project Overview

Monorepo with several main components — see each directory's README for details:
- **`pipeline/`** — JWST data reduction (NIRSpec + NIRCam). Local-only, no cloud dependencies.
- **`web/`** — Next.js web portal. Deployed on Vercel.
- **`python/`** — Unified Python package: API client, CLI, and deployment tools. Install with `pip install -e ".[deploy]"` for full functionality.
- **`deploy/`** — (deprecated) Standalone deploy CLI, now merged into `python/campfire/deploy/`.

Supporting: `supabase/` (migrations), `scripts/` (one-off utilities)

## Pipeline

### Running

```bash
cd pipeline && pip install -e .
cfpipe nirspec run --obs <obs_name> --all --processes 4
cfpipe nircam run --field <field_name> --all --processes 4
cfpipe config > my_config.toml   # export defaults
cfpipe download --program 6585 --instrument nirspec
```

### Configuration

Config resolution: package defaults → user config (`--config` or `$CAMPFIRE_ROOT/config/config.toml`) → per-observation overrides. 
Config is parametric only — controls *how* stages run, not *whether*. `--overwrite` and `--processes` are CLI-only.

Observations defined in `$CAMPFIRE_ROOT/config/observations.toml`, fields in `fields.toml`.

The `[environment].CRDS_CONTEXT` value in `config_default.toml` is the canonical CRDS context for the current `cfpipe` release. Bumping it always implies a MINOR release (CRDS changes are scientifically equivalent to a calibration update). PRs that change this line must categorize the changelog entry as **Calibration**.

### Python Environment

Always use the `campfire` conda environment when testing code: `conda run -n campfire python ...`

## Pipeline Versioning & Releases

`campfire-pipeline` follows PEP 440 / semver. Version is resolved by `setuptools-scm` from git tags matching `pipeline-vX.Y.Z` — never edited by hand. Between tags, the version is `X.Y.Z+1.devN+g<sha>[.dDATE]`.

### Bump policy

- **MAJOR** (X.0.0): breaking output format change (FITS schema, summary columns, file naming)
- **MINOR** (0.X.0): any change to pixel/flux values for the same input — CRDS bump, `jwst` upgrade, new calibration default, algorithmic change with scientific impact
- **PATCH** (0.0.X): no change to scientific output (CLI, plots, perf, internal refactor)

CHANGELOG categories map directly: **Calibration → MINOR**, **Algorithm → MINOR/MAJOR**, **Infrastructure → PATCH**.

### Workflow

- Every PR touching `pipeline/**` adds an entry under `## Unreleased` in `pipeline/CHANGELOG.md`, categorized.
- Tags happen separately, after merge, when you're ready to deploy. PRs do not bump versions.
- Use `/pipeline-release [X.Y.Z]` (or `bash scripts/release-pipeline.sh X.Y.Z`) to do the rollover, commit, and tag. The script enforces: on `main`, clean tree, up-to-date with origin, non-empty Unreleased section, tag does not exist.
- `campfire deploy` warns and requires explicit confirmation when any FITS being deployed carries a non-release `cfpipe_version` (anything not matching `^X.Y.Z$` — i.e. `.dev`, `+dirty`, `+nondefault`, or a free-form override string). The dev string is preserved verbatim in `spectra.cfpipe_version` so provenance stays visible downstream; the prompt exists so deployers consciously choose to ship unreleased data, not to block it. Pass `--auto-approve` to skip the prompt when knowingly redeploying old or experimental data.

### Override

For ad-hoc tagged runs that aren't going through the release flow, set `[pipeline].version = "..."` in your config. The string passes through verbatim into `CMPFRVER` and deploys through the same warn-and-confirm path as `.dev` builds.

## Web Portal

### Development

```bash
cd web && npm install && npm run dev
```

Requires `.env.local` with Supabase + R2 credentials (see `web/README.md`).

### Key Patterns

- **Server actions**: `web/lib/actions/` with `"use server"` directive
- **Types**: `web/lib/types.ts` (DB types), `web/lib/actions/spectra-types.ts` (sort columns)
- **Flags**: `web/lib/flags.ts` — bitmask flags (spectral features, object flags, DQ) + quality enum
- **Auth**: `useAuth()` from `web/lib/contexts/AuthContext.tsx`
- **Theme/Prefs**: `useTheme()` and `usePreferences()` from respective contexts in `web/lib/contexts/`
- **Plotting**: `web/components/spectra/plotting-utils.ts`

### Database (Supabase)

Core tables: `targets` (target catalog), `spectra` (unique spectra, joinable to targets), `programs` (JWST program metadata), `user_profiles` (auth + access, linked to Supabase `auth.users`).

Main RPC function: `get_filtered_target_ids` — server-side filtering, sorting, pagination with Haversine coordinate search.

#### Declarative Schemas (`supabase/schemas/`)

The database uses Supabase's native declarative schema system. **`supabase/schemas/` is the single source of truth** for the entire database schema — tables, functions, triggers, views, indexes, and policies are all defined here. Never read migration files to understand current definitions; read the schema files instead.

`supabase db diff` works by building two databases: one from migrations (the "current" state) and one from schema files (the "desired" state), then generating a migration to reconcile any differences. This means schema files must define the complete database.

Files (applied in this order via `schema_paths` in `config.toml`):
- `tables.sql` — extensions, tables, sequences, constraints, table grants, default privileges
- `functions.sql` — all RPC and helper functions
- `triggers.sql` — trigger functions and triggers
- `views.sql` — views and materialized views
- `indexes.sql` — all indexes
- `policies.sql` — RLS policies

**Workflow for schema changes:**
1. Edit the relevant file in `supabase/schemas/`
2. Apply locally: `supabase db reset` (rebuilds from migrations + schema files + seed)
3. Generate migration: `supabase db diff -f <description>`
4. Review the generated migration SQL
5. Commit both the schema file change and the generated migration
6. Open a PR — Supabase runs migrations on a preview branch automatically
7. On merge to `main`, migrations are applied to production via the GitHub integration (`supabase db push --linked` only if necessary)

**Caveats (migra limitations):** Materialized views, comments, and partitions are not tracked by the diff engine. Changes to these require manual migration authoring after editing the schema file.

#### Migrations (`supabase/migrations/`)

Migrations are the deployment mechanism, not the source of truth. They are applied sequentially by `supabase db reset` and `supabase db push`. Never edit existing migrations. New migrations are auto-generated via `supabase db diff`.

The migration history was squashed on 2026-03-28 into a single baseline (`20260328200000`) + normalization (`20260328204719`). Pre-squash migrations are archived in `supabase/migrations_archive/` for reference.

### Local Supabase

`supabase/seed.sql` contains seed data used for preview database branches & deployments. 
It should be periodically re-generated from a sample of public production data

```bash
python scripts/generate_seed.py          # stratified sample (~100 targets)
python scripts/generate_seed.py --objects-per-program 10  # more targets per program
supabase db reset                        # applies migrations + seed locally (or automatic on PR)
```

Because `seed.sql` is applied automatically on preview branches, it must stay compatible
with the current migration state. If a migration adds/removes/renames columns or tables,
regenerate the seed file. Seed failures will show up as a failed Supabase check on the PR.

Test users: `admin@campfire.dev`, `user@campfire.dev`, `viewer@campfire.dev` (password: `password123`)

## Deployment

### Git Workflow

- **`main`** → production (auto-deploys via Vercel + Supabase migration push)
- Feature/fix branches off `main` → preview deployments with branched Supabase instances, merge back to `main` via PR
- On PR open: Supabase creates a preview branch (isolated DB + Auth), runs migrations, seeds from `supabase/seed.sql`, and injects credentials into the Vercel preview deployment
- On PR merge to `main`: Supabase automatically runs new migrations against production
- PRs touching `pipeline/**` must add an entry to `pipeline/CHANGELOG.md` under `## Unreleased`, categorized as Calibration / Algorithm / Infrastructure. PRs touching only `web/`, `python/`, or `supabase/` do not need a changelog entry.

### Build Verification

**Required before merging to main:**
```bash
cd web && npm run build && cd ..
```

### Infrastructure

- **Frontend**: Vercel (root dir: `web/`, framework: Next.js)
- **Database**: Supabase PostgreSQL
- **Storage**: Cloudflare R2 for FITS files
- **Auth**: Supabase Auth with email/password
- **CI/CD**: Supabase GitHub integration (branching + migration checks) + Vercel (preview deploys)

### Deploy CLI

Deploy commands are part of the unified `campfire` CLI (install from `python/`):

```bash
cd python && pip install -e ".[deploy]"
campfire deploy --obs <obs_name>                         # full deploy
campfire deploy --obs <obs_name> --dry-run               # validate only
campfire deploy rgb --obs <obs_name>                     # RGB cutouts only
campfire deploy pointings --obs <obs_name>               # pointings JSONB backfill
campfire deploy tiles --field cosmos --filter f444w      # map tiles
campfire deploy sync-programs                            # upsert from programs.toml
```

Credentials via env vars (`CAMPFIRE_SUPABASE_URL`, `CAMPFIRE_R2_*`) or gitignored `$CAMPFIRE_ROOT/config/deploy.toml`.

## General Notes

- Pipeline code is local-only, never deployed
- Large files (FITS, raw data) are gitignored and stored in R2
- Secrets are never committed — use Vercel env vars or gitignored config files
- Database schema definitions live in `supabase/schemas/` (source of truth); migrations in `supabase/migrations/` (deployment)
