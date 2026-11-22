# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository. This file is loaded at the start of every session, so it holds what applies everywhere. Each component has its own `AGENTS.md` with the rules for working there; read it before editing files in that directory.

## What CAMPFIRE is

CAMPFIRE (COSMOS Archive of MultiPle-Field Internal Reductions & Extractions) is an end-to-end framework for reducing and distributing JWST survey data, built for imaging and spectroscopy in extragalactic deep fields (originally COSMOS) by a small team of astronomers. It has three parts:

1. **Reduction.** The `cfpipe` CLI (`pipeline/`) downloads raw data from MAST and runs custom reductions of NIRCam imaging and NIRSpec MSA spectroscopy on a local machine. Outputs land in a structured data directory outside the repo, `$CAMPFIRE_ROOT`. It needs no cloud connection: anyone can install and run `cfpipe` on their own data.
2. **The archive.** Reductions are published to the cloud by the `campfire` CLI (`campfire push` / `campfire deploy`, in `python/`): bulk files go to object storage (OSN for data products, Cloudflare R2 for `fitsgl` map tiles), and catalog info (targets, spectra, objects, line fits, photometry, etc.) goes to a Postgres database (self-hosted Supabase, `supabase/`). The database is the record of every published reduction, and frozen **data releases** are snapshots of it.
3. **Access.** The archive is read three ways:
   - the web portal (`web/`, Next.js) at campfire-jwst.org, where collaborators browse, filter, inspect and visually classify spectra and imaging;
   - a REST API (`/api/v1`, served by the same app) and a read-only SQL login for public data;
   - the `campfire` Python client and CLI (`python/`) for bulk work: mirroring the catalog (`campfire sync`), pulling files (`campfire pull`), and reading data releases with citations and provenance (`campfire dr`, `Campfire(dr=tag)`).

Alongside these, `campfire-etc` (`etc/`) is an empirical NIRSpec exposure-time calculator fitted to the archive, served as an MCP server and a portal page.

Programs carry access control: proprietary programs are visible only to their members, public ones to everyone. Access rules live in the database via RLS.

## Repository map

| Path | What | Read |
|---|---|---|
| `layout/` | `campfire-layout`: the single authority for directory and storage-key layout (local paths ↔ keys). Zero-dependency Python; mirrored in TS at `web/lib/layout.ts`. | `layout/AGENTS.md` |
| `pipeline/` | `campfire-pipeline` (`cfpipe`): JWST reduction. Runs locally, doesn't interface with the cloud. | `pipeline/AGENTS.md` |
| `python/` | `campfire`: API client, CLI, sync/storage engine, deployment and data-release tooling. | `python/AGENTS.md` |
| `web/` | Next.js portal + `/api/v1` REST API. | `web/AGENTS.md` |
| `supabase/` | Declarative database schema, migrations, seed, SQL guard tests. | `supabase/AGENTS.md` |
| `etc/` | `campfire-etc`: numpy-only ETC, simulator and MCP server. | `etc/AGENTS.md` |
| `ops/` | Production VM (Docker Compose, Caddy, backups, deploy script). | `ops/AGENTS.md`, `ops/README.md` |
| `scripts/` | One-off utilities, backfills, generators. | |
| `docs/` | Temporary: design docs and plans for work in progress (see Documentation). | |

## Environment and install

- Preferred installation via the `install.py` script, which installs each package in the correct order, and editable. 
- Installation prefers a clean mamba/conda environment (default: `campfire`). Use `conda run -n campfire python ...`.
- **Git worktrees:** the editable installs point at the main checkout; if running code from a worktree, make sure to set  `PYTHONPATH=<worktree>/layout:<worktree>/pipeline:<worktree>/python` (only the packages you changed are needed).
- [FitsGL](https://github.com/hollisakins/fitsgl) (`campfire[fitsgl]` extra) is installed as a sibling checkout before `./python`, if needed; details in `python/README.md`.
- The web app needs `web/.env.local` (see `web/README.md`). A local database runs without Docker: `supabase start --runtime native` (see `supabase/AGENTS.md`).

## Build and test

| Component | Command | CI check |
|---|---|---|
| web | `cd web && npm run typecheck && npm test && npm run build` (`npm run budget` for the bundle budget) | `web` |
| python client | `CAMPFIRE_ROOT=<scratch dir> pytest python -q` | `python` |
| layout | `pytest pipeline/tests/test_layout.py -q` and `cd web && npx vitest run lib/layout.test.ts` | `python` |
| pipeline | `pytest pipeline/tests -q` (informational in CI) | `python` |
| database | `supabase db reset`, then the `supabase/tests/check_*.sql` guards (see `supabase/AGENTS.md`) | `database` |
| etc | `pytest etc -q` | `etc-server.yml` workflow |

`python`, `web` and `database` are required status checks on `main`; each path-filters internally.

## Rules that apply everywhere

**Real data is not a test fixture.** Machines running agents often hold the production data tree (under `$CAMPFIRE_ROOT`). Run tests and experiments with `CAMPFIRE_ROOT` pointed at a scratch directory. A test that misses a patch can otherwise write into the real tree. 

**Secrets are never committed.** They live in the VM's `ops/.env` / `ops/web.env`, 1Password, or gitignored config (`web/.env.local`, `$CAMPFIRE_ROOT/config/deploy.toml`). Never print them.

**Single sources of truth, with generated mirrors.** Edit the source, regenerate the mirror, and commit both. CI fails on drift.

| Source | Mirror | Regenerate |
|---|---|---|
| `supabase/schemas/*.sql` | `supabase/migrations/` | `supabase db schema declarative sync -f <name> --no-apply` |
| `layout/campfire_layout/products.py` (+ `bijection.py` dispatch tables) | `web/lib/layout-products.json` | `python scripts/sync_layout_products.py` (checked by `pipeline/tests/test_layout.py`) |
| `pipeline/campfire_pipeline/nirspec/linelist.py` | `web/lib/linelist.json` (read by `linelist.ts`) | `python scripts/sync_linelist.py` |
| `etc/campfire_etc/models/` | `web/public/etc/models/` | `cd web && npm run etc-models` |

Never edit an applied migration, a published ETC model file, or a generated mirror by hand.

**Access control is enforced in the database.** Reads run under RLS as the requesting user. Don't add a path that reads catalog rows with the service-role client on a user's behalf, unless it scopes the rows itself the same way the policies do.

**Pipeline changes are versioned science.** A change to `pipeline/**` needs one categorized entry under `## Unreleased` in `pipeline/CHANGELOG.md` by the time the PR opens (see `pipeline/AGENTS.md`). Don't stress about it; the changelog should be written before the PR opens, not as soon as a task is done. 

**User-facing copy is minimal.** Portal pages carry as little text as possible: avoid explanatory microcopy, irrelevant backend/implementation details, CLI hints. Write in American English. 

## Git and deployment

- Branch off `main`; open a PR. Commit or push only when asked.
- Merging to `main` deploys production: `.github/workflows/deploy.yml` builds the web image, and `ops/scripts/deploy.sh` on the VM applies pending migrations, swaps the image, and rolls back if `/api/health` fails. There are no preview deployments or preview databases.
- Production runs on one Jetstream2 VM (`ops/`): Next.js container, self-hosted Supabase (Postgres 17, PostgREST, GoTrue, Kong), Caddy behind Cloudflare. The API host is `campfire-api.hollisakins.com`; `campfire.hollisakins.com` is an alias of the portal (see `web/AGENTS.md`).
- Pipeline tags (`pipeline-vX.Y.Z`) are cut separately after merge with `/pipeline-release` or `scripts/release-pipeline.sh`.

## Documentation

- **The code is the documentation of current behavior.** Rationale lives in module docstrings and comments next to the code it explains; invariants live in tests (`supabase/tests/check_*.sql`, `python/tests/`).
- **Permanent docs live with their component:** each directory's `README.md` (usage) and `AGENTS.md` (rules), plus a few contracts read by people outside the code: `python/dr-format-v1.md` (data-release format), `supabase/reader-role.md` (public SQL access), `web/design-system.md` (visual language), `ops/README.md` (production runbook). User-facing docs for the portal are in `web/lib/docs/content/`.
- **`docs/` is temporary.** It holds design docs and plans for work in progress, and nothing in it is a source of truth. A design doc may be out of date the day its work merges: never cite it as current behavior, and don't link to it from code. When the work lands, move whatever rationale is still needed into docstrings, comments or the component's `AGENTS.md`, then delete the doc (git history keeps it).

## Maintaining these files

AGENTS.md files are instructions, not a changelog or a design record. Add something only when an agent would otherwise get it wrong: a rule, a non-obvious invariant, a command, or a pointer. Explain how a subsystem works in its module docstrings, record history in commits and PRs, and leave out what the code already says. Prefer the nested `AGENTS.md` closest to the code, and edit existing text rather than appending a new paragraph.
