# AGENTS.md

Shared guidance for AI coding agents working in this repository.

## Project Overview

Monorepo with several main components — see each directory's README for details:
- **`layout/`** — `campfire-layout`: the single, zero-dependency authority for the directory/key contract (local paths, storage keys, key↔path bijection, tree lifecycle). Pure-python core depended on by both `pipeline/` and `python/`; mirrored in TypeScript at `web/lib/layout.ts`. **Install this first** (it's not on PyPI).
- **`pipeline/`** — JWST data reduction (NIRSpec + NIRCam). Local-only, no cloud dependencies.
- **`web/`** — Next.js web portal. Deployed on Vercel.
- **`python/`** — Unified Python package: API client, CLI, and deployment tools (including the `campfire deploy` CLI). Install with `pip install -e .` for full functionality — the base install carries the science stack (matplotlib, scipy, photutils, reproject, Pillow); interactive Plotly figures (`[plotting]`) and `specutils` are optional extras.

Supporting: `supabase/` (migrations), `scripts/` (one-off utilities)

**Editable install order** (in the `campfire` conda env): `pip install -e ./layout && pip install -e ./pipeline && pip install -e ./python` — `campfire-layout` must precede the two packages that depend on it. The repo-root `install.py` automates this (component profiles, conda env management, ordering, verification — see `install.py --help`). `campfire[deploy]` additionally depends on `campfire-pipeline` (deploy machines always carry the pipeline), resolved the same editable-first way.

**FitsGL co-development** (epic #337): the `campfire[fitsgl]` extra depends on [FitsGL](https://github.com/hollisakins/fitsgl)'s `fitsgl-py` producer, which isn't on PyPI and lives in that repo's `fitsgl-py/` subdirectory. The extra pins a git dependency as the reproducible fallback (`pip install -e "./python[fitsgl]"`), but the dev convention is to check `fitsgl` out **as a sibling of this repo** and install it editable *before* the base client, so local FitsGL edits stay live and pip never fetches from git:

```bash
pip install -e ../fitsgl/fitsgl-py[deploy]   # sibling checkout, editable
pip install -e ./python                      # base client leaves fitsgl untouched
```

The web side consumes FitsGL as the published npm package `@fitsgl/core` (a plain version range in `web/package.json`); use `npm link` against a local `fitsgl-core` checkout only when co-editing. See `python/README.md` and `docs/design-fitsgl-integration.md` §4.

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

- Before a PR touching `pipeline/**` is opened, its `## Unreleased` section in `pipeline/CHANGELOG.md` should carry one categorized entry covering the pipeline changes in that PR. This is a **PR-completeness** check, not a per-edit obligation: edit freely during a session and add (or update) the single entry once, before you open the PR — several related commits share one entry. Everything that ships in a pipeline tag stays logged, including pure Infrastructure/refactor changes (PATCH).
- **Do not defer or avoid a pipeline change to sidestep the changelog.** If a pipeline fix belongs in the session you're working, make it — then add the Unreleased line. The entry is cheap; a deferred fix that should have shipped is the real cost. Uncertain which category? Pick the closest of Calibration / Algorithm / Infrastructure and note the uncertainty in the entry — a rough categorization is far better than skipping the change.
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
- **Reads vs mutations (decision D-C, #506)**: server actions are for *mutations* only. Read-only data that a client component fetches (mount effects, TanStack queries) goes through a `GET` route handler under `web/app/api/` — Next serializes server-action POSTs per client, so a read implemented as an action queues behind every other action on the page and cannot be aborted or cached. Pattern: the route resolves identity with `getRequestIdentity()` / `getRequestPrincipal()`, queries under RLS, exports its response type, and sets `Cache-Control: private, …` (never `public`; add `Vary: Cookie` when the response is browser-cached for longer than a session, since sign-out does not clear the HTTP cache); the client fetches it with `fetchJson()` from `web/lib/fetch-json.ts` inside a `useQuery` whose key names *what* is fetched, never the viewer (the QueryClient is cleared on sign-out in `AuthContext`). Shared read logic that both a route and an action need lives in `web/lib/server/` (`import 'server-only'`). Examples: `/api/shutters`, `/api/objects/adjacent`, `/api/objects/near`, `/api/filter-options`, `/api/metadata/*`, `/api/lists/membership`.
- **Types**: `web/lib/types.ts` (DB types), `web/lib/actions/spectra-types.ts` (sort columns)
- **Flags**: `web/lib/flags.ts` — bitmask flags (spectral features, object flags, DQ) + quality enum
- **Auth (client)**: `useAuth()` from `web/lib/contexts/AuthContext.tsx`
- **Auth (server)**: identity is resolved once per request — `getRequestIdentity()` / `getRequestPrincipal()` / `requireAdmin()` from `web/lib/auth/identity.ts` (cookie session verified locally against `SUPABASE_JWT_SECRET`; `middleware.ts` refreshes the cookie), and `getAccessContext(userId)` from `web/lib/auth/access-context.ts` (60 s per-user memo of admin flag, share-link scope and accessible program slugs, mirroring `accessible_program_slugs()`). Bearer `/api/v1` routes go through `validateAuth()` / `authenticateApiRequest()` in `web/lib/api-auth.ts`. Never call `supabase.auth.getUser()` or read `SUPABASE_SERVICE_ROLE_KEY` directly — ESLint rejects both; use `createServiceClient()` from `web/lib/supabase/service.ts`.
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
- A PR touching `pipeline/**` should, by the time it's opened, have one categorized entry (Calibration / Algorithm / Infrastructure) under `## Unreleased` in `pipeline/CHANGELOG.md` covering its pipeline changes — a PR-completeness check, not a gate on individual edits (see Pipeline Versioning & Releases → Workflow). Don't hold back an in-session pipeline change to avoid the entry; make the change and log it. PRs touching only `web/`, `python/`, or `supabase/` do not need a changelog entry.

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

### Storage plane (push / pull / verify)

The local products tree and cloud storage are two ends of one sync relationship,
mediated by the `storage_objects` registry (server) and its local mirror
(`$CAMPFIRE_ROOT/meta/campfire.db`). One engine (`python/campfire/storage/` +
`campfire/deploy/push.py`) serves both directions with content-identity dedup
(whole-file everywhere except NIRCam exposures, which use the two-component
**exposure identity**: `sci_dq_hash` over the SCI/DQ/CFMASK arrays *and*
`wcs_hash` over the WCS header cards — both must match to skip an upload,
because `align`/`wcs_shift` move an exposure's astrometry without touching a
science pixel), a stat fast-path (unchanged files are never re-read), and
per-batch registration (interrupted transfers resume at file granularity).
Registry rows predating `wcs_hash` are reconciled against `content_hash` and
backfilled in place, so upgrading never stampedes a full re-upload. **All data
products live on OSN; map tiles are the sole R2 exception.**

```bash
campfire sync                          # refresh the local index (never touches the tree)
campfire status [--obs X | --field Y]  # bidirectional diff; scoped = push-side dry run
campfire pull --obs X [--intermediate] # cloud→local products (+ review annotations, admins)
campfire push --obs X | --field Y      # local→cloud bytes ONLY — no catalog, no publication
campfire verify [--cloud] [--deep]     # tree↔index (bulk scan, size-match quick check); --cloud adds registry↔bucket
campfire drop-local --obs X --yes      # delete local files verified in cloud
```

Slow-link workflow (CANDIDE→OSN): `campfire push` for the heavy bytes
(re-runnable until clean), then `campfire deploy` — it dedup-skips everything
already landed and attaches it to the new deployment. `download` remains an
alias of `pull`.

### Config plane (issue #303)

The storage plane moves bytes; the **config plane** moves the three
data-management TOMLs (`programs.toml` / `observations.toml` / `fields.toml`)
with the same verbs. The cloud registry (`programs` / `observations` /
`fields` tables) is the source of truth: each row mirrors its TOML section
losslessly in `config` jsonb (stage overrides, tile WCS, everything) with a
canonical sha256 in `config_hash`.

```bash
campfire config push [--programs|--observations|--fields|--obs X|--field Y]  # local → cloud (admin)
campfire config pull [--theirs]        # cloud → local TOMLs, comment-preserving (any logged-in user)
campfire config diff                   # three-way divergence report (read-only)
campfire config retire <kind> <name> [--undo]  # soft-retire a definition (admin; rename = retire + push)
```

Reconciliation is three-way per section against the last-synced hash in
`$CAMPFIRE_ROOT/meta/config_sync_state.json`: push refuses to clobber a cloud
section someone else changed (`--force` to override), pull refuses to clobber
local hand-edits (local-ahead sections are kept; true conflicts prompt, or
`--theirs`). Pull rewrites only changed sections via tomlkit, preserving
comments and formatting elsewhere. Rows are never deleted — removal is the
explicit `config retire` (never inferred from a section missing locally),
which pull skips and push refuses; `--undo` re-activates. `fields.programs`
is resolved at write time by mapping the field's `jwst_program_ids` through
`observations` rows; unresolved writes omit the column rather than clobber
it. Bare TOML datetimes are rejected at push
(they wouldn't survive the jsonb round trip — use quoted ISO strings).
`campfire deploy` still upserts the config of what it deploys automatically;
`config push` is the explicit/bulk path. `deploy sync-programs` /
`deploy sync-fields` are hidden legacy aliases now. This is what lets an
ephemeral container bootstrap: `campfire config pull` → reduce → deploy.

### Deploy CLI (publication)

`campfire deploy` = push (via the shared engine) + catalog upserts + deployment
lifecycle. Part of the unified `campfire` CLI (install from `python/`):

```bash
cd python && pip install -e .
campfire deploy --obs <obs_name>                         # full deploy
campfire deploy --obs <obs_name> --dry-run               # validate only
campfire deploy pointings --obs <obs_name>               # pointings JSONB backfill
campfire deploy tiles --field cosmos --filter f444w      # map tiles
campfire config push --programs                          # programs.toml → cloud (config plane)
```

Migration-era one-time tools (the `deploy registry` subgroup —
backfill/reconcile/copy/prune — and `deploy nircam import-*`) are deleted (A1/A2
complete, issue #371). The storage budget shows in `campfire status` (admins);
registry↔bucket verification is `campfire verify --cloud`. The `deploy nircam
pull*` / `deploy nirspec pull-*` annotation round-trips are folded into
`campfire pull` (hidden but working individually). RGB/SED static cutouts are
fully deprecated (superseded by the on-the-fly `/api/v1/cutout` API): deploy
neither generates nor uploads them, the `deploy rgb`/`deploy sed` subcommands
and their generators are removed, and the `targets.has_sed_plot` column is
dropped — their legacy R2 remnants retire with A2.

**Deploy auth (issue #250).** Two decisions, kept independent:

- **Supabase auth mode** is chosen *explicitly*, never inferred from which creds are present:
  - `login` (**default**): `campfire login` (device-flow OAuth). Operates through RLS; uploads go via **presigned URLs** so admin machines hold no object-store write keys. This is the only path the normal `campfire deploy` uses — presigning is required, with **no silent boto3 fallback**.
  - `service-role` (**explicit opt-in** — `--service-role` or `CAMPFIRE_DEPLOY_MODE=service-role`): bypasses RLS for unattended / CI deploys. Needs `CAMPFIRE_SUPABASE_URL` + `CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY` (or a TOML `[supabase]` block). A service-role key merely *present* in the env no longer wins by precedence.
  - `local` (`--local` / `CAMPFIRE_DEPLOY_MODE=local`): local Supabase (127.0.0.1:54321) with the standard CLI service-role key.
- **Object-store credentials** (`CAMPFIRE_S3_*`; legacy `CAMPFIRE_R2_*` aliases; or a gitignored `$CAMPFIRE_ROOT/config/deploy.toml`) resolve the same way in every mode and are decoupled from the Supabase decision. In `login` mode they're only needed by the direct-boto3 maintenance commands (`deploy remove`, `objects reconcile`, `registry backfill`/`copy`, tile deletion) — the LIST/HEAD/DELETE/cross-bucket-COPY ops presigned PutObject URLs can't express. Storage endpoint/region/path-style are configurable per purpose (`CAMPFIRE_S3_*` data, `CAMPFIRE_S3_TILES_*` tiles, `CAMPFIRE_S3_OSN_*` OSN); see `python/campfire/deploy/backend.py`.

## General Notes

- Pipeline code is local-only, never deployed
- Large files (FITS, raw data) are gitignored and stored in R2
- Secrets are never committed — use Vercel env vars or gitignored config files
- Database schema definitions live in `supabase/schemas/` (source of truth); migrations in `supabase/migrations/` (deployment)
