# AGENTS.md

Shared guidance for AI coding agents working in this repository.

## Project Overview

Monorepo with several main components — see each directory's README for details:
- **`layout/`** — `campfire-layout`: the single, zero-dependency authority for the directory/key contract (local paths, storage keys, key↔path bijection, tree lifecycle). Pure-python core depended on by both `pipeline/` and `python/`; mirrored in TypeScript at `web/lib/layout.ts`. **Install this first** (it's not on PyPI).
- **`pipeline/`** — JWST data reduction (NIRSpec + NIRCam). Local-only, no cloud dependencies.
- **`web/`** — Next.js web portal. Self-hosted on the Jetstream2 VM (`ops/`), deployed by `.github/workflows/deploy.yml` on merge to `main`.
- **`python/`** — Unified Python package: API client, CLI, and deployment tools (including the `campfire deploy` CLI). Install with `pip install -e .` for full functionality — the base install carries the science stack (matplotlib, scipy, photutils, reproject, Pillow); interactive Plotly figures (`[plotting]`) and `specutils` are optional extras.
- **`etc/`** — `campfire-etc`: the empirical NIRSpec/MSA exposure-time calculator and spectrum simulator fitted to the archive, with its MCP server. Deliberately **numpy-only** and independent of `campfire-layout` so `uvx` can install it straight from GitHub; the model builder is behind the `[build]` extra. See `etc/README.md`.

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

### Emission-line fluxes (post-inspection loop)

`cfpipe nirspec linefit` measures line fluxes **only at the inspected redshift**
(docs/design-emission-line-fitting.md). It is not part of `run --all`: `campfire pull
--obs X` materializes `objects.redshift` / `redshift_quality` to
`reference/nirspec/<obs>/redshifts.toml` (any user with program access; writer and
reader in `campfire_pipeline/nirspec/redshift_reference.py`), `linefit` fits spectra
at `[nirspec.line_fitting].min_quality` (default 3) or better into `<base>_lines.fits`
(refit only when redshift / quality / spectrum bytes change), and `campfire deploy
lines --obs X` (or the full deploy) upserts one `line_fits` row per spectrum
(its `lines` rows, one per line keyed by the `nirspec/linelist.py` names — components plus the
doublet totals `CIII1908` / `OII3727` / `SII6725` / ... that are consistent across
gratings, `component` narrow | broad | doublet; provenance `z_used`,
`z_quality`, `object_version`, `fit_version`, `spectrum_hash`). `campfire sync` mirrors
it to `meta/lines.csv` (wide) and `Campfire.query_lines()`; the
`line_fits_status` view and the sync records flag `stale_redshift` /
`stale_spectrum`. Fits at the auto redshift are opt-in end to end (`--allow-auto`,
`--allow-auto-z`, `z_source`). The portal filters and sorts on a line through
`lines` (one row per spectrum × line, the source of truth since phase 2c, indexed on
`(line, snr)`): every RPC on the catalog filter contract takes `p_line` /
`p_line_snr_min` / `p_line_snr_max` / `p_line_include_stale` and the `line_snr` sort,
the web sends them only while a line is set (`buildFilterParams`), the objects-side
filter and `line_snr` column both come from `object_line_values` (SECURITY DEFINER with
the two `object_band_values` gates, plan pinned with `LATERAL … LIMIT 1` lookups — #619;
guarded by `check_line_filter_scoping.sql`), the picker offers
doublet totals and stand-alone lines only (`web/lib/linelist.ts`, generated from
`linelist.py` by `scripts/sync_linelist.py`; CI fails on drift), and the object page's
Emission Lines section reads `/api/objects/lines`. Deploy also emits a `_lines.json`
sidecar per product (layout kind `nirspec_lines_json`; `generate_lines_json`: the `MODEL`
extension in fν + a line summary), the fourth entry the spectrum sidecar resolver answers
(`lines` / `has_lines`), which the spectrum plot's "Lines" toggle draws (model + continuum;
`/api/line-fit` streams it when the front is off).

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

## Empirical NIRSpec ETC (`etc/`)

`campfire-etc` turns the archive into an exposure-time calculator: per disperser, σ²_pix(λ) = A(λ)/T + B(λ)/(T·t_exp²) fitted to faint standard-configuration spectra, plus the measured 1-D/2-D noise ratios, pixel correlation, shutter-placement penalty, source-Poisson term and flux-recovery fractions. `campfire_etc/model.py` (arithmetic), `sed.py`, `simulate.py` (mock spectra on the native pixel grid with correlated noise) depend on numpy only; `server.py` is the MCP server (`mcp>=2`, `MCPServer`), serving stdio locally and streamable HTTP when hosted (Fly.io via `etc/Dockerfile` + `etc/fly.toml`, deployed by `.github/workflows/etc-server.yml` on merge; hosted URL `https://campfire-etc.hollisakins.com/mcp`). The Claude Code plugin (skill + `.mcp.json`) lives in the separate `claude-astro-tools` marketplace as `nirspec-etc`; this repo owns the code, the models and the deployment.

- **Models are versioned data**: `etc/campfire_etc/models/nirspec-<version>.json` (schema 1) + `manifest.json` (`latest`). Never edit a published model file; add a new version with `campfire-etc build assemble --version <YYYY.MM>` after re-running the harvest/fit steps on a fresh archive snapshot (`etc/README.md` has the run order and the pitfalls). Every tool result carries `model_version`. The regression numbers in `etc/tests/test_model.py` pin the 2026.09 model; if a rebuild changes the default, keep those tests pointing at 2026.09 explicitly (`load_model("2026.09")`) rather than loosening them.
- Test with `pytest etc` (numpy + mcp only, ~5 s). The HTTP transport is smoke-tested in CI by starting the server and curling `/health`.
- Keep the core numpy-only: anything needing astropy/scipy goes behind the `[fits]` or `[build]` extras with a lazy import.

## Web Portal

### Development

```bash
cd web && npm install && npm run dev
```

Requires `.env.local` with Supabase + R2 credentials (see `web/README.md`).

### Key Patterns

- **Copy**: pages carry as little text as possible. No explanatory microcopy, no implementation detail (hashes, formats, snapshot semantics), no CLI hints on user-facing pages (those belong under `/admin`). A header gets one short line; sections show numbers in cards or badges; detail goes behind a collapsible or into `/docs`.
- **Server actions**: `web/lib/actions/` with `"use server"` directive
- **Reads vs mutations (decision D-C, #506)**: server actions are for *mutations* only. Read-only data that a client component fetches (mount effects, TanStack queries) goes through a `GET` route handler under `web/app/api/` — Next serializes server-action POSTs per client, so a read implemented as an action queues behind every other action on the page and cannot be aborted or cached. Pattern: the route resolves identity with `getRequestIdentity()` / `getRequestPrincipal()`, queries under RLS, exports its response type, and sets `Cache-Control: private, …` (never `public`; add `Vary: Cookie` when the response is browser-cached for longer than a session, since sign-out does not clear the HTTP cache); the client fetches it with `fetchJson()` from `web/lib/fetch-json.ts` inside a `useQuery` whose key names *what* is fetched, never the viewer (the QueryClient is cleared on sign-out in `AuthContext`). Shared read logic that both a route and an action need lives in `web/lib/server/` (`import 'server-only'`). Examples: `/api/shutters`, `/api/objects/adjacent`, `/api/objects/near`, `/api/filter-options`, `/api/metadata/*`, `/api/lists/membership`.
- **Types**: `web/lib/types.ts` (DB types), `web/lib/actions/spectra-types.ts` (sort columns)
- **Flags**: `web/lib/flags.ts` — bitmask flags (spectral features, object flags, DQ) + quality enum
- **Auth (client)**: `useAuth()` from `web/lib/contexts/AuthContext.tsx`
- **Auth (server)**: identity is resolved once per request — `getRequestIdentity()` / `getRequestPrincipal()` / `requireAdmin()` from `web/lib/auth/identity.ts` (cookie session verified locally against `SUPABASE_JWT_SECRET`; `middleware.ts` refreshes the cookie), and `getAccessContext(userId)` from `web/lib/auth/access-context.ts` (60 s per-user memo of admin flag, share-link scope and accessible program slugs, mirroring `accessible_program_slugs()`). Bearer `/api/v1` routes go through `validateAuth()` / `authenticateApiRequest()` in `web/lib/api-auth.ts`. Never call `supabase.auth.getUser()` or read `SUPABASE_SERVICE_ROLE_KEY` directly — ESLint rejects both; use `createServiceClient()` from `web/lib/supabase/service.ts`.
- **Planner-visible access scope (Catalog Read Model phase 1, #533)**: `objects.is_public` (any member program public), `targets.is_public` and `spectra.is_public` (the row's program) are trigger-maintained copies of `programs.is_public` (`sync_object_is_public` / `sync_row_is_public` on the row, `propagate_program_is_public` when a program flips; never written by hand). The three read policies test `(NOT is_link_account() AND is_public) OR <scope array>` so the planner sees a real column's statistics instead of the opaque `accessible_program_slugs()` InitPlan (whose default ~1% selectivity picked the programs index for a predicate that matches ~76% of rows). Same row set as the array alone; `supabase/tests/check_is_public_scoping.sql` guards both the maintenance and the no-widening invariant. A new program-gated catalog table gets the same column, trigger and policy shape.
- **Measurement rows carry their object (Catalog Read Model phase 2a)**: `lines.object_id`, `photometry.object_id` and `line_fits.stale_redshift` are trigger-maintained (set on write by the unnest / `compute_line_fit_stale` triggers, re-pointed by `relink_lines_from_targets` / `relink_lines_from_spectra` / `relink_photometry_from_matches`, recomputed by `recompute_line_fit_stale_from_objects` when an object is inspected or bumped; FKs are ON DELETE SET NULL for the objects rebuild). The filter helpers (`object_line_values`, `object_band_values`, `spectra_matching_line_filter`) read them directly; `check_measurement_object_links.sql` guards every maintenance path and the catalog-wide invariants. Never write these columns by hand; `scripts/backfill_measurement_links.sql` fills rows that predate them.
- **The predicate tree (Catalog Read Model phase 3a, 2026-10-26)**: one query language for the catalog, JSON carried as `?q=` in the portal and `q=` on `/api/v1/objects` and `/api/v1/spectra/list` (the Python client's `q=`). Three node kinds: a group (`{"and": [...]}` / `{"or": [...]}`), a predicate on a column of the table the node is evaluated against (`{"col", "op", "v"}`), and an exists over a child table (`{"exists": "lines", "where": [...], "not"?}`), plus `{"cone": {ra, dec, radius}}`. `catalog_tree_sql(tree, root)` compiles it in plpgsql against `catalog_query_whitelist()` (table → column → type, the only source of identifiers; the objects' member aggregates `programs` / `gratings` / `observations` / `n_targets` / `n_spectra` are deliberately not in it, since they count hidden members, so those questions are an exists over `targets` or `spectra`) and `catalog_query_correlations()` (how each child joins its parent); every value is a literal, anything else raises 22023, which the routes turn into a 400. `catalog_match_ids(root, tree)` runs it SECURITY INVOKER against the base tables (or the `cat_*` views while a release is browsed; the views cost 7x on the EXISTS shape), and `catalog_query_objects` / `catalog_query_spectra` hand the ids to the legacy list RPCs through their new `p_object_ids` / `p_spectrum_ids`, so the row shape, sort, paging, cursor and the Line S/N / Mag columns are one code path; `p_line` / `p_band` on the wrappers are display knobs only. `check_catalog_query.sql` guards the whitelist, the semantics, parity with every legacy filter as a viewer, RLS, release browsing and the wrappers. New filter dimensions are whitelist entries, not RPC parameters.
- **The tree is the filter state, two nouns are its words (Catalog Read Model phase 3b)**: the portal's `FilterOptions` keeps its legacy slots for the RPC contract, but the UI state is `conditions: TreeNode[]` (plus the search box), carried as `?q=` (`lib/utils/url-params.ts`; an older link's `programs=` / `z_min=` / `line=` … become leading nodes). What the user sees is the vocabulary of `lib/catalog-query/vocabulary.ts` (design doc "The vocabulary: two nouns", rev. 4): **Object** (properties: program, field, position, redshift, quality, tags, grating coverage, max S/N, …; measurements keyed by band) and **Spectrum** (properties: grating, S/N, exposure time, quality flags, …; measurements keyed by line). `targets`, `line_fits`, `catalog_matches` and `object_list_members` are folds behind a property, never named. At the object level a spectrum's grating and a line are questions about any of the object's spectra (the Grating chip's any / all / none of are the legacy grating pill's three shapes; a bare `exists lines` is the object's line row), and **scope is containment** only inside a Spectrum card: one card is one row of `spectra`, written as `exists spectra where [{and: [...]}]` so a card holding a grating alone is never the chip's bare shape; a card is "one spectrum with all of" / "no spectrum with all of"; "any of" is the only group, for alternatives across properties (a multi-select chip is already the OR over one property's values). A choice or flags chip owns every node of its key at its level: three-state options include, exclude or clear a value (`FilterChip`'s `excluded`, reported with the included set in one change), with an any / all switch for the multi-valued relations, so "tagged A but not B" is one chip (`compileChoice` / `readChoiceState`); a replacement is compiled against the table its node sits in (`tableAtPath`) and replaces in place. Objects view = Object chips, Photometry and Lines tables and Spectrum cards; spectra view = Spectrum chips and Lines plus one "its object" section; the view switch is `rerootConditions` (the first explicit card becomes the spectrum, everything else its object's, and back without loss). `readConditions` turns the tree into items with paths (`replaceAt` / `appendAt` / `removePaths` / `addToCard` / `setNegated` / `tidyConditions` edit it); anything the cards cannot show is an `advanced` item printed in the same words, never hidden. `compileProperty` / `compileMeasurement` write the legacy pills' own shapes for the primary properties (proven at parity by the SQL guard; `vocabulary.test.ts` checks them against `legacyNodes`), so a tree of cards still takes the legacy RPCs: every server boundary calls `normalizeFilters(filters, root)` and then `needsTreePath` (a residual, or a column other than the pill's line / band) decides between the legacy RPCs and the tree path (list pages → `catalog_query_objects` / `_spectra`; map, inspection queue, CSV, prev/next → `catalog_match_ids`, null ids when there is nothing to select on, then `p_object_ids` / `p_spectrum_ids`); a column with nothing to select on is the legacy RPC with `p_measurements_display_only`. A spectra-view `grating nin` is never lifted (the spectra RPCs apply `= ANY(p_gratings)` whatever the mode). Columns follow the rows (phase 3c, #660): `show_lines` / `show_bands` are derived from the tree at every boundary (`columnKeys`: every line and band filtered on that every selected row is sure to have, not negated, not under an OR; `components/filters/filter-state.ts`), `activeLinesOf` / `activeBandsOf` read nothing else, and every catalog RPC takes them as `p_line_columns` / `p_band_columns`, JSON maps of the shown keys each with the window of its own row (`{"Halpha": {"snr_min": 3}}`, `{"f444w": {"mag_max": 27}}`, tested on the aggregates the filters test, max S/N over the fits of the line and min magnitude / max S/N over the matches in the band, so a column shows the pair the filter admitted and never a value outside its window), display only (`measurementColumnParams` / `columnSpecs`; with no line pill set `p_line_include_stale` is false only when a shown line's row asks for a fresh fit, with a pill set the flag stays the pill's own, since it governs the pill's filter): the rows carry `line_columns` (`{line: {snr, flux}}`, the object's best fit or this spectrum's) and `band_columns` (`{band: {mag, snr}}`) beside the API's scalar `line_snr` / `band_mag` / `band_snr`, the table shows one column per key (`line_snr:<line>`, `band_mag:<band>`, `band_snr:<band>`, which are also the sort keys the RPCs take, `measurement_sort_key()`), the CSV exports write `snr_<line>` / `flux_<line>` / `mag_<band>` / `snr_<band>`, and `/api/v1/objects` / `/spectra/list` take `line_columns=` / `band_columns=` and the prefixed `sort=`. `object_line_columns` / `object_band_columns` are the scoped helpers (the gates of `object_line_values` / `object_band_values`, one row per object with the map; `check_catalog_query.sql` section 9). Components: `components/filters/FiltersModal.tsx` ("Advanced Filters", centred, a draft applied on Done, Escape closes an open dropdown first, a live count through `GET /api/catalog-query/count` aborted on the next edit, Copy / Paste of the `?q=` JSON through the clipboard; the same on the map), `FilterCards.tsx` (`PropertyChip` over the familiar chips, `MeasurementTable` with click-to-edit range cells over the shared `components/ui/RangeEditor.tsx`, a multi-select picker that adds and removes rows and a per-row details expander, `SpectrumCard`, any-of groups seeded by one property picker, a checkbox "+ property" adder, the advanced block, `NounBody`), `filter-choices.ts` (option lists and value labels); the filter bar shows Program, Field, Position | Redshift, Quality | Grating, Tags, then one chip per other item, then "Advanced", with the Map link beside the search box. Chip dropdowns render through `components/ui/Popover.tsx` so a scrolling container never clips them, and nothing in a three-state dropdown moves under the pointer (no re-sort, the any / all switch and Clear always in place).
- **Measurement tables carry their scientist-facing names (Catalog Read Model phase 2b, 2026-10-26)**: `line_fits` (was `spectrum_line_fits`, the fit run), `lines` (was `spectrum_lines`, one row per spectrum x line), `catalog_matches` (was `object_photometry`, the cross-match) and `photometry` (was `object_photometry_bands`, one row per match x band; its parent key is `match_id`), with frozen twins `dr_lines` / `dr_catalog_matches` / `dr_photometry` and source views `cat_lines` / `cat_catalog_matches` / `cat_photometry`; `line_fits_status` is the staleness view. The API contract did not move: the `/api/v1/sync/lines` and `/sync/photometry` stream names, the release `counts` keys and the RPC signatures are unchanged, and the Python mirror (`campfire/db/store.py`) keeps its old sqlite table names until phase 2c changes their shape (one forced resync, not two). A rename is a hand-authored migration (`ALTER ... RENAME`; pg-delta would DROP + CREATE) followed by a generated one for the function bodies and comments. Every trigger that copies a parent value onto a row reads the parent `FOR SHARE` (`sync_spectra_target_scope` included); `python/tests/test_target_scope_race.py` is its two-session check.
- **Long tables carry every field (Catalog Read Model phase 2c-1, 2026-10-26)**: `lines` holds all 24 fields of a per-line record (`label`, `wave_obs`, continuum, kinematics, `complex` / `chi2` / `dof` / `npix`, `tied_to`, `members` as `text[]`) and `photometry` holds `wav_min` / `wav_max`, copied by the unnest triggers and, for rows that predate the columns, by `scripts/backfill_long_table_fields.sql` (resumable, per-parent batches, no table lock); frozen twins and `cat_*` views match; `check_long_table_fields.sql` guards the copy. **2c-2 (2026-10-26) dropped the blobs**: `line_fits_upsert(p_rows)` / `catalog_matches_upsert(p_rows)` take the payload the deploy has always sent and write the parent row plus its `lines` / `photometry` rows in one transaction, replacing a parent's rows wholesale (the deploy's only write path; the admin policies gate them); `lines_jsonb_of(spectrum_id, tag)` / `bands_jsonb_of(match_id, tag)` rebuild the map shape for the sync streams, the release exports and the CSV export, so no wire format moved; the object page reads `cat_lines` / `cat_photometry` rows; `sync_line_object` / `sync_photometry_object` BEFORE triggers set `object_id` (a client value is recomputed away); the seed inserts rows; the Python mirror's sqlite tables took the production names (`SCHEMA_VERSION` 12, one forced resync).
- **Theme/Prefs**: `useTheme()` and `usePreferences()` from respective contexts in `web/lib/contexts/`
- **Immutable product delivery (decision D-D, #507)**: bulk product bytes (sidecar JSON, NIRCam PNG/FITS) are not streamed through Vercel functions. A route authorizes the read, then mints a content-addressed url on the Cloudflare Worker's `/o/<key>` endpoint via `frontUrlFor()` / `frontUrlsFor()` in `web/lib/server/cdn-front.ts` (registry `content_hash` as the cache identity, a 6 h window-stable presigned upstream, an HMAC token the Worker checks on every serve, and the row's `updated_at` so an upstream overwritten in place is served but never cached under the old hash). Products are immutable per `storage_objects.content_hash`, not per path. Gated on `CDN_FRONT_URL` + `WORKER_JWT_SECRET`; callers fall back to streaming when it returns null. Serving side: `web/workers/download-worker/src/object.ts`. Spectrum sidecars (#508): the client resolves all four urls (full JSON, 1-D, zfit, line fit) with one `/api/spectrum/sidecars` call and fetches the 1-D sidecar (`_spec_1d.json`, layout kind `spectrum_1d_json`, emitted by deploy next to the full JSON) before the full payload; the zfit scalars `chi2_min` / `confidence` live on `spectra` rows (deploy writes them; `scripts/backfill_zfit_scalars.py` and `scripts/split_spectrum_json.py` backfill older deploys).
- **Object page streaming (decision D-C, #510)**: `app/nirspec/objects/[id]/page.tsx` renders its HTML from `loadObjectHeader()` alone (object row + access-scoped member targets/spectra; `lib/server/objects.ts`, request-memoized so `generateMetadata` shares the row lookup) and streams the rest: `loadObjectPhotometry()` is passed to the client tree as a promise and settles behind a `<Suspense>` in `UnifiedObjectPage` (it never rejects — a rejection there is a page error, not a missing section). The same render resolves every member spectrum's sidecar urls once (`resolveSpectrumSidecars()` in `lib/server/spectrum-sidecars.ts`, shared with `/api/spectrum/sidecars`), seeds them into the TanStack cache through a `<HydrationBoundary>` (keys in `lib/spectrum-sidecars.ts`, no directive, importable from both sides), and emits `ReactDOM.preload` hints for the 1-D payloads and — via `components/plot/PlotlyPreload.tsx`, which reads the react-loadable manifest under the ids in `plotly-loadable-ids.json` (CI checks they still exist) — the Plotly chunks, so the browser's first spectrum request is the 1-D payload itself, in flight before any script runs. Below-the-fold sections (comments, nearby objects, the SED's P(z)) fetch when they scroll into view; 2-D S/N arrays load only for expanded cards, after their 1-D trace. Inspection mode still fetches the joined `ObjectDetail` through the `getObjectById` action.
- **Cutout store (decision D-D, #509)**: the three render routes (`/api/tile-thumbnail`, `/api/og-image`, `/api/v1/cutout`) consult the content-addressed store in `web/lib/cutout/store.ts` first — `cutouts/<field>/v<imaging version>/<size>/<fov>/<ra>_<dec>.png` on the public tiles bucket, written after the response — and 302/stream from it on a hit; browser sizes round up to the 64 / 300 / 600 ladder. Program access is checked in the route before the store is consulted; only renders from public imagery are stored. Needs `S3_TILES_PUBLIC_URL_BASE`; a 30-day lifecycle rule on `cutouts/` expires prefixes orphaned by a field re-deploy.
- **Photometry band filter**: the portal filters and sorts on a broadband magnitude or S/N through `photometry` — one row per (cross-match, band), written with the match by `catalog_matches_upsert` (the source of truth since phase 2c), with `snr` and `mag` (AB, `23.9 - 2.5*log10(flux[uJy])`, NULL for a non-positive flux) as STORED generated columns and partial indexes on `(band, mag)` / `(band, snr)`. Exactly the `lines` pattern on the photometry side, and the same filter contract: every catalog RPC takes `p_band` / `p_band_mag_min` / `p_band_mag_max` / `p_band_snr_min` / `p_band_snr_max` plus the `band_mag` / `band_snr` sorts, and the web sends them only while a band is set (`buildFilterParams`). Rows are keyed on `match_id`, never `object_id` (that FK is refreshed out-of-band after every objects rebuild), so readers join through the parent. One helper serves both matching and display: `object_band_values(p_band, mag/snr bounds, p_program_slugs, p_include_unpublished)` returns `(object_id, mag, snr)` for every caller-visible object in the window, with `mag` = min and `snr` = max over the object's cross-matches in that band; `objects_matching_band_filter` is its id projection (materialized once per RPC call into an `INTEGER[]`), and each RPC `LEFT JOIN`s the function for the `Mag` / `Band S/N` columns and sort keys, so a row can never show a value outside the window that admitted it. It is **SECURITY DEFINER** on purpose: read under RLS, every band row re-ran the derived table's two-hop policy chain (parent cross-match → object) and the filter timed out in production, so the function bypasses RLS and scopes its rows itself with two gates: the caller's explicit `p_program_slugs` / `p_include_unpublished` (the same arguments `objects_matching_line_filter` takes, and the only gate when the caller is the service-role client the `/api/v1` bearer routes use, which carries no `auth.uid()`), plus, whenever there is a user identity, a verbatim copy of the `select_objects_by_access` predicate (keep the two in step; `supabase/tests/check_band_filter_scoping.sql` guards both gates). The picker is fed by `mv_filter_options.photometry_bands` via `/api/filter-options` — band names are per-field `photometry.toml` config, not a fixed catalog like the line list, so an unknown name is passed through and simply matches nothing.
- **Cursor pagination (decision D-F, #511)**: `/api/v1/objects` and `/api/v1/spectra/list` page with an opaque keyset cursor. The list RPCs (`get_filtered_objects_paginated` / `get_filtered_spectra_paginated`) take `p_after_sort_text` / `p_after_sort_num` / `p_after_tiebreak` and return `has_more` plus the next cursor's components (`next_sort_text` / `next_sort_num` / `next_tiebreak`), computed from the same columns the ORDER BY sorts on (one row past the page is fetched so `has_more` is exact; the spectra ORDER BY tail is `target_id, grating, spectrum_id`, a total order). The route encodes them with `web/lib/api-cursor.ts` — base64url JSON carrying a fingerprint of the filter+sort, so a cursor sent under different filters is a 400 — and returns `pagination.next_cursor` (null on the last page); the count runs on the first page only (`total` is -1 when skipped; `count=` overrides). `offset=` is accepted for one client release with a `Deprecation` header. The `/api/v1/sync/*` routes are keyset-only: `offset>0` is a 400 via `rejectLegacyOffset()` (`web/lib/api-sync-pagination.ts`), the RPCs have no `p_offset` (it stayed on the signatures for one release after #536 because the migration and the Vercel deploy land independently on merge — a signature drop while the old routes still sent it would have 500'd every sync), and `/api/v1/version` floors the client at 0.5.0. Python: `iter_objects` / `iter_spectra` follow `next_cursor`; `query_*` return the pagination block in `Table.meta`; `campfire.deploy.registry._iter_rows` keysets on the PK with pushed-down `.eq()` filters.
- **Plotting**: `web/components/spectra/plotting-utils.ts`
- **NIRSpec ETC page (`/nirspec/etc`, unlisted)**: the empirical exposure-time calculator lives at `web/app/nirspec/etc` + `web/components/etc/` with the arithmetic in `web/lib/etc/model.ts` (a port of `etc/campfire_etc/model.py`; `model.test.ts` pins the same 2026.09 numbers as `etc/tests/test_model.py`). It is deliberately not in the navbar — share the URL. It fetches the model JSON from `web/public/etc/models/`, which must be a verbatim copy of `etc/campfire_etc/models/`: run `cd web && npm run etc-models` after adding a model version (CI's `web-etc-models.yml` fails on drift). The summary / ETC-comparison tables and per-disperser caveats are generated from the model file, so a rebuilt model updates the page without prose edits.

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

**Workflow for schema changes** (pg-delta engine, `[experimental.pgdelta]` in `config.toml`; see `supabase/README.md`):
1. Edit the relevant file in `supabase/schemas/`
2. Generate the migration from the schema files: `supabase db schema declarative sync -f <description> --no-apply` (compares `supabase/schemas/` against the migration chain in a shadow database; needs a running local stack, `supabase start`)
3. Prepend the header every migration must start with, one statement per line (CI reads the first two non-comment lines and rejects anything else):
   ```sql
   SET lock_timeout = '5s';
   SET statement_timeout = '60s';
   ```
4. Apply locally and review: `supabase db reset` (migration chain + `seed.sql`), read the generated SQL
5. Commit both the schema file change and the generated migration
6. Open a PR — the `database` check rebuilds the stack, fails on any schema↔migration drift (`--strict-coverage`), runs the `supabase/tests/check_*.sql` guards and the Python SQL harnesses; Supabase also runs the migrations on a preview branch
7. On merge to `main`, migrations are applied to production via the GitHub integration (`supabase db push --linked` only if necessary)

**Local stack without Docker:** `config.toml` enables the CLI's experimental `stack` commands, so `supabase start --runtime native` runs Postgres and the services as host processes (Linux, Apple-silicon macOS; CLI ≥ 2.119, `npx -y supabase@2.120.0 …` works without a global install). `db reset` takes ~5 s. `pg_cron` jobs do not run on the native runtime (nothing local needs them).

**Caveats:** pg-delta models materialized views, comments, publications and security-invoker views (the migra blind spots that forced hand-authored migrations are gone), but casts, operators, text-search objects and DML are not declarative: put those in versioned migrations under `supabase/migrations/` directly. Never edit an applied migration.

#### Migrations (`supabase/migrations/`)

Migrations are the deployment mechanism, not the source of truth. They are applied sequentially by `supabase db reset` and `supabase db push`. Never edit existing migrations. New migrations are generated by `supabase db schema declarative sync` (pg-delta); the 50 hand-authored migrations from the no-Docker era (e.g. `20260904030000_row_local_rls.sql`) predate the native stack and stay as they are. Every new migration starts with `SET lock_timeout = '5s'; SET statement_timeout = '60s';` and never contains a bulk backfill over `spectra`, `objects` or `storage_objects` (backfills are resumable scripts, template `scripts/backfill_zfit_scalars.py`).

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

`supabase/seed_admin_fixtures.sql` (second entry in `[db.seed] sql_paths`) is hand-maintained
and derives from the rows `seed.sql` inserted: the NIRCam exposure-review queue, deploy
events and leases, an inspection access request, a sync snapshot and a fresh burst of
inspection activity, so `/admin` renders populated locally. A seed regeneration never
touches it.

Test users: `admin@campfire.dev`, `user@campfire.dev`, `viewer@campfire.dev` (password: `password123`)

## Deployment

### Git Workflow

- **`main`** → production: on merge, `.github/workflows/deploy.yml` builds the web image, pushes it to GHCR and the VM's `ops/scripts/deploy.sh` applies pending `supabase/migrations/` to the self-hosted database, swaps the image and gates on `/api/health` (rollback on failure)
- Feature/fix branches off `main` → PR; CI's `web`, `python` and `database` checks (the `database` job rebuilds a local stack from the migration chain + seed and runs the SQL guards). There are no preview deployments or preview databases since the 2026-10-25 cutover.
- On PR open: nothing is deployed; the `database` check is the only database gate.
- On PR merge to `main`: `deploy.sh` on the VM applies the new migrations to the production database, then swaps the web image
- A PR touching `pipeline/**` should, by the time it's opened, have one categorized entry (Calibration / Algorithm / Infrastructure) under `## Unreleased` in `pipeline/CHANGELOG.md` covering its pipeline changes — a PR-completeness check, not a gate on individual edits (see Pipeline Versioning & Releases → Workflow). Don't hold back an in-session pipeline change to avoid the entry; make the change and log it. PRs touching only `web/`, `python/`, or `supabase/` do not need a changelog entry.

### Build Verification

**Required status checks on `main`** (`.github/workflows/`): `python` (client + layout golden; pipeline suite informational), `web` (typecheck, vitest, build, bundle budget), `database` (drift gate, migration chain + seed, SQL guards, Python SQL harnesses). Each runs on every PR and path-filters internally. Locally, the same build:
```bash
cd web && npm run typecheck && npm test && npm run build && cd ..
```

### Infrastructure

- **Frontend**: Next.js standalone container on the Jetstream2 VM behind Caddy and Cloudflare (`ops/`); the Vercel project is a decommissioning fallback only
- **Database**: self-hosted Supabase (Postgres 17 + PostgREST + GoTrue + Kong) on the same VM, `ops/docker-compose.yml`; API at `campfire-api.hollisakins.com`
- **Storage**: Cloudflare R2 for FITS files
- **Auth**: Supabase Auth with email/password
- **CI/CD**: GitHub Actions (`web` / `python` / `database` checks on PRs; `deploy.yml` on merge to `main`). The Supabase GitHub integration and Vercel previews are retired.
- **Self-hosting**: done 2026-10-25 (web on 2026-10-24, database on 2026-10-25). `ops/README.md` is the runbook: architecture, first boot, deploy, backups, the dump/restore scripts, the executed cutover checklist and the rollback paths.

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

**What `campfire sync` mirrors.** The five `/api/v1/sync/*` streams (objects,
spectra, storage, photometry, line fits) are walked concurrently with keyset
cursors (5000 rows per page, storage 10000; `CAMPFIRE_SYNC_PAGE_SIZE` /
`CAMPFIRE_SYNC_STORAGE_PAGE_SIZE`). The storage mirror carries **finals only**
(`FINAL_PRODUCT_TYPES`: `nirspec_spec`, `nircam_mosaic`) — not the sidecar
JSON / zfit / line-fit / preview / rate rows that make up most of the registry,
and not the intermediates: `pull --intermediate` indexes the intermediates of
its selection on demand (`refresh_storage_scope()`, a full walk of that
product-kind × observation/field slice, purged within the slice) before it
plans. `campfire push` never depends on the mirror being complete (it fetches
the live registry rows for its keys). Incremental syncs carry **tombstones**:
the first page of each of objects / spectra / storage returns `deleted_ids`
(soft-deleted objects, revoked spectra and members of a deleted object,
registry rows that left the visible set) which the client deletes locally, so
`pull` no longer falls back to a full resync after every deploy that regroups
objects. Tombstones key on `updated_at`: soft-deletes stamp it in
`apply_object_reconciliation`, `recompute_has_published_spectrum` stamps objects whose
publication flag flips, and `bump_spectra_updated_at_trigger` fires on
`deploy_status`. A tombstone taken on the first page yields to a row a later
page of the same walk re-fetched (the fetched row is the newer fact). Not
covered: hard deletes (`deploy remove`) and NIRCam field-deploy revokes
(`deployments` has no `updated_at`, so the storage stream cannot key a
tombstone on them) still need `campfire sync --full`; the count-mismatch
fallback that triggers one from `pull` exists for the objects stream only. The sync RPCs
run under a 120 s `statement_timeout` (service_role otherwise inherits
authenticator's 8 s): the first page of every stream runs catalog-wide counts.

**Sync catalog snapshot.** A first-time or `--full` sync does not page the streams out
of Postgres. A systemd timer on the VM (`ops/systemd/campfire-sync-snapshot.timer`, 05:00 UTC, `CRON_SECRET`; formerly a Vercel cron) runs
`/api/cron/sync-snapshot`, which walks the five streams once for the **public programs
only** through the same `fetchSyncPage` the routes use (`web/lib/server/sync-streams.ts`).
It writes one gzip JSONL file per stream to the private OSN bucket under
`sync-snapshots/<id>/` and records the build in `sync_snapshots` (service-role only;
the builder keeps the newest 3 ready builds). The keys are not a layout product: they
are served only through the authenticated `/api/v1/sync/snapshot`, which returns
presigned urls, or `available:false` for admins (they mirror drafts), before the first
build, or when a snapshot program went private. The client (`campfire/snapshot.py`,
`_bootstrap_from_snapshot` in `sync.py`) runs four steps:
1. loads the files (sha256-verified);
2. runs the **extras walk**, `snapshot=<id>` on every sync route: the caller's
   accessible programs minus the snapshot's set, read from the row and never the
   request. `get_objects_for_sync(p_filter_program_slugs)` also returns mixed
   public/proprietary objects, whose aggregates need the full scope, and members of
   the caller's private lists;
3. runs the normal walk as the catch-up from the snapshot's `started_at`;
4. applies the hard deletes since `started_at` from `/api/v1/sync/deletions`, because a
   hard-deleted row leaves nothing for the catch-up to return. Statement triggers
   journal every delete from the five synced tables into `sync_deletions`, which the
   builder trims to its oldest kept snapshot; the endpoint answers 410 for anything
   older, and the client then walks live in the same run;
5. purges what none of the steps touched.

`started_at` is a watermark, not the build time: the start of the oldest transaction open when the build began, minus 5 minutes, so a write already in flight is not missed. The purge runs after the catch-up, so rows the catch-up restores keep their local download state. A `sync_bootstrap` `_meta` state makes an interrupted bootstrap recover on the next sync: an interrupted load forces a full sync, and an interrupted catch-up resumes from `started_at`. Any unusable snapshot, or any failure during load or the extras walk, falls back to the live walk in the same run (`--no-snapshot` forces it).

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
campfire deploy photometry --field goods-n --supersede  # catalog cross-match + P(z) sidecars; retire the previous catalog_name
campfire config push --programs                          # programs.toml → cloud (config plane)
```

Photometry is configured per field in `$CAMPFIRE_ROOT/config/photometry.toml`
(schema and a UNICORN example: `python/campfire/deploy/photometry.example.toml`).
Rows key on `(field, catalog_name, catalog_id)`, so a new catalog release lands
beside the old one until `--supersede` deletes the other `catalog_name`s in the
field (refused when the new match count is under half the rows it would retire,
unless `--force`); `[field.photoz].format` selects the `lazy` (native Lazy.jl) or `unicorn`
(release `*_photz_v*.fits` + template cube) reader. UNICORN releases are pulled
from the open Corral host `https://web.corral.tacc.utexas.edu/unicorn/Catalogs/`.

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

**Deploy leases and data-release freezes.** A freeze (`campfire dr freeze`) is one
snapshot transaction, so it is internally consistent by construction; its post-freeze
integrity checks (finals present with matching hashes, mosaics of the published
deployment, non-empty components) are what make the release usable. The deploy lease
(`deploy_scope_state`) exists so a freeze never snapshots a catalog write that spans
several requests half-way: `begin_deploy_scope` refuses while a freeze runs, and a freeze
refuses while any lease is live. So every `deploy` command that writes, over several
requests, anything the freeze reads (spectra, objects, line fits, photometry, registry
rows of release members, `nircam_images`) runs under a scope lease (`_under_scope_lease`
and friends in `deploy/cli.py`), and no deploy runs unleased (`ScopeLease.begin` fails
closed). `python/tests/test_deploy_freeze_contract.py` classifies every command; a new one
fails it until it is classified. Run `dr freeze` when `dr status` shows no activity; an
unleased admin edit overlapping a freeze yields a consistent snapshot of an intermediate
catalog, not a corrupt one.

**Retain-on-supersede.** The archive (`archive/<sha256>/<relpath>` on OSN, registry rows with
`root = 'archive'`, immutable by trigger) holds exactly the files data releases reference. Before
a deploy overwrites a live NIRSpec final or sidecar, the gate (`web/lib/server/retain.ts` on the
presign route for login-mode deploys, `campfire/deploy/retain.py` on the direct path) asks
`retain_required_hashes` whether the object's current bytes belong to a frozen release whose
`dr archive` has not copied them yet; only then are they copied first, and a copy that drifts or
fails refuses the batch (409 `retain_failed`). Every other overwrite proceeds as always and
nothing else is retained. The gate is always on and has no mode or override (decision D-P2-4);
the fix for a refused batch is finishing `campfire dr archive` or purging the release.

**Deploy auth (issue #250).** Two decisions, kept independent:

- **Supabase auth mode** is chosen *explicitly*, never inferred from which creds are present:
  - `login` (**default**): `campfire login` (device-flow OAuth). Operates through RLS; uploads go via **presigned URLs** so admin machines hold no object-store write keys. This is the only path the normal `campfire deploy` uses — presigning is required, with **no silent boto3 fallback**.
  - `service-role` (**explicit opt-in** — `--service-role` or `CAMPFIRE_DEPLOY_MODE=service-role`): bypasses RLS for unattended / CI deploys. Needs `CAMPFIRE_SUPABASE_URL` + `CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY` (or a TOML `[supabase]` block). A service-role key merely *present* in the env no longer wins by precedence.
  - `local` (`--local` / `CAMPFIRE_DEPLOY_MODE=local`): local Supabase (127.0.0.1:54321) with the standard CLI service-role key.
- **Object-store credentials** (`CAMPFIRE_S3_*`; legacy `CAMPFIRE_R2_*` aliases; or a gitignored `$CAMPFIRE_ROOT/config/deploy.toml`) resolve the same way in every mode and are decoupled from the Supabase decision. In `login` mode they're only needed by the direct-boto3 maintenance commands (`deploy remove`, `objects reconcile`, `registry backfill`/`copy`, tile deletion) — the LIST/HEAD/DELETE/cross-bucket-COPY ops presigned PutObject URLs can't express. Storage endpoint/region/path-style are configurable per purpose (`CAMPFIRE_S3_*` data, `CAMPFIRE_S3_TILES_*` tiles, `CAMPFIRE_S3_OSN_*` OSN); see `python/campfire/deploy/backend.py`.

## General Notes

- Pipeline code is local-only, never deployed
- Large files (FITS, raw data) are gitignored and stored in R2
- Secrets are never committed — they live in the VM's `ops/.env` / `ops/web.env` (and 1Password) or gitignored config files
- Database schema definitions live in `supabase/schemas/` (source of truth); migrations in `supabase/migrations/` (deployment)
