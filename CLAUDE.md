# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project Overview

Monorepo with several main components — see each directory's README for details:
- **`pipeline/`** — JWST data reduction (NIRSpec + NIRCam). Local-only, no cloud dependencies.
- **`web/`** — Next.js web portal. Deployed on Vercel.
- **`deploy/`** — CLI for uploading pipeline products to Supabase + Cloudflare R2.
- **`python/`** — under construction Python API Client and CLI interface

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

### Python Environment

Always use the `jwst` conda environment when testing code: `conda run -n jwst python ...`

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

Core tables: `objects` (object catalog), `spectra` (unique spectra, joinable to objects), `programs` (JWST program metadata), `user_profiles` (auth + access, linked to Supabase `auth.users`).

Main RPC function: `get_filtered_object_ids` — server-side filtering, sorting, pagination with Haversine coordinate search.

Migrations tracked in `supabase/migrations/`. Test locally with `supabase db reset`, push with `supabase db push`.

**Naming migrations**: Always check existing files in `supabase/migrations/` before naming a new one. Timestamps must sort after the latest existing migration to avoid conflicts with already-applied remote migrations. Use the pattern `YYYYMMDDHHMMSS_description.sql` and pick a timestamp greater than the last file's.

### Local Supabase

Seed generator: `python scripts/generate_seed.py` → `supabase/seed.sql` → `supabase db reset`

Test users: `admin@campfire.dev`, `user@campfire.dev`, `viewer@campfire.dev` (password: `password123`)

## Deployment

### Git Workflow

- **`main`** → production (auto-deploys via Vercel)
- Feature/fix branches off `main` → preview deployments, merge back to `main` via PR

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

### Deploy CLI

```bash
cd deploy && pip install -e .
cfdeploy --obs <obs_name>                         # full deploy
cfdeploy --obs <obs_name> --dry-run               # validate only
cfdeploy rgb --obs <obs_name>                     # RGB cutouts only
cfdeploy tiles --field cosmos --filter f444w      # map tiles
cfdeploy sync-programs                            # upsert from programs.toml
```

Credentials via env vars (`CAMPFIRE_SUPABASE_URL`, `CAMPFIRE_R2_*`) or gitignored `$CAMPFIRE_ROOT/config/deploy.toml`.

## General Notes

- Pipeline code is local-only, never deployed
- Large files (FITS, raw data) are gitignored and stored in R2
- Secrets are never committed — use Vercel env vars or gitignored config files
- Database schema is tracked in `supabase/migrations/`
