# Admin Panel Audit — Reduction Loop & Data Management

**Date:** 2026-07-03
**Scope:** the `/admin` portal (`web/app/admin/**`), its server actions and API routes, the deployment/registry/inspection schema (`supabase/schemas/`), and the Python deploy CLI paths that feed them — audited against the goal of a **unified reduction dashboard**: complete OSN transparency, detailed deployment logs, and comprehensive reduction status (NIRSpec by program→observation, NIRCam by field→filter), in service of the local↔cloud reduction loop where **OSN is the single source of truth** and every exposure is human-inspected.
**Method:** five parallel deep-dive investigations (UI/UX architecture; deployment logging end-to-end; OSN visibility; reduction-status tracking; DB schema/query performance), with headline findings independently verified against the source. This document is the synthesis. No code was changed.

**Companion documents:** `docs/db_audit_2026-06-19.md` (prior DB audit — several findings there compound the ones here), `docs/design-intermediate-products.md` (§5.6 designed the audit ledger this audit finds half-implemented), `docs/design-nircam-deploy-overhaul.md` (epic #261; N6 remains open and overlaps this audit's Theme D).

---

## 1. Executive summary

The admin panel is a collection of nine hand-rolled pages that predate the cloud-as-source-of-truth architecture. The *data layer* underneath it is in far better shape than the panel itself: epic #210/#261 built a real registry (`storage_objects`), a lifecycle model (`deployments` + `deploy_events`), a working NIRCam inspection loop, and a layout contract mirrored in TypeScript. The panel simply doesn't surface most of it — and where it does, the NIRCam retrofit stopped at the web consumer.

The five headline problems, in order of how much they undermine the reduction-loop mandate:

1. **The deployment log is broken for NIRCam — at the consumer, not the schema.** The `deployments`/`deploy_events` schema was correctly retrofitted with a `field` scope, and the deploy CLI writes field-scoped rows, but `web/lib/actions/deployments.ts` never selects `field` and the page renders `observation` as row identity. Every NIRCam deployment appears as a blank-named row of `—`s; publish/revoke always reports "0 spectra updated"; the audit log shows `—` for every NIRCam event. Five distinct bugs, all small (§3.B).
2. **The audit trail has structural holes.** Only three operations write deployment records (NIRSpec full deploy, NIRSpec intermediates draft, NIRCam field deploy). `deploy remove`, `registry prune`/supersede, `tiles`, `rgb`/`sed`, `pointings`, and `photometry` mutate the bucket or DB with **no record at all** — even though the `deploy_events` action enum already reserves `delete` and `supersede` for exactly this. Recorded counts are *intended*, not *actual* (upload failures are discarded), and nothing ever reconciles `deployments` against bucket reality (§3.B).
3. **There is no way to see, verify, or download what's in OSN.** The registry browser (`/admin/intermediate-products`) caps at 200 rows with no next page, shows key basenames only, and has **no download link — an admin literally cannot download an intermediate product from the panel**. The web tier has GET/PUT presign but no LIST or HEAD, so registry-vs-bucket drift is invisible, and tiles/rgb/sed are unregistered by design and therefore invisible to any registry-backed view (§3.C).
4. **The reduction-status dashboard is half-built and one-instrument.** NIRCam has a genuine closed loop (deploy → inspect → pull → combine) with a per-(field, filter) progress view already rendered in the panel. NIRSpec has **nothing**: the `spectrum_exposures` table exists as a scaffold with zero readers or writers (verified), there is no per-exposure NIRSpec review, no program→observation status view, and no `fields` entity to hang NIRCam field status on. The "every exposure inspected" mandate is queryable for NIRCam but **enforced nowhere**: `pending` exposures flow into mosaics, `approved` has zero pipeline effect (§3.D).
5. **Every admin table is hand-rolled and state-amnesiac.** No server-side sorting anywhere, three inconsistent data-access mechanisms, filters and pagination held in `useState` (lost on back-nav), silent truncation (deployments 100, events 50, registry 200), and the single worst query in the app: `/api/admin/activity` fetches **every comment and every audit row on every page load**, sorts in JS, then slices — twice, because it re-scans both tables to build the user dropdown (§3.A, §3.E).

The good news: the house patterns needed to fix all of this already exist in the codebase — the public NIRSpec explorer's URL-as-state + TanStack Table + server-RPC pattern, the `nircam_reduction_progress` view, the `presignExposurePngs` admin-presign template, and the `get_filtered_objects_paginated` windowed-count RPC pattern. The revamp is mostly *generalization*, not invention.

---

## 2. What the loop looks like today (verified end-to-end)

```
LOCAL (cluster)                    OSN + SUPABASE                      WEB ADMIN
cfpipe nircam run --field F        FITS+PNGs → canonical keys          /admin/nircam (progress view
  → products FITS + previews  ───▶ deployments row (field, draft)  ──▶  + exposure table)
                                   nircam_exposures upsert (stage)     /admin/nircam/[id] (review:
                                   storage_objects registered           approve/exclude/mask/notes,
                                                                        presigned OSN PNGs)
cfpipe nircam combine              reference/nircam/F/exposures.json         │
  reads exclusions+masks ◀─── campfire deploy nircam pull --field F ◀───────┘
  → mosaics → deploy (published)
```

This NIRCam loop (epic #261 N1–N5, PRs #283/#285/#286) **works**. The asymmetries:

- **NIRSpec** deploys intermediates to OSN and registers them (`deploy.py:626-632` registers `nirspec_spectrum_exposure` objects) but the loop stops there: no review rows, no review UI, no pull, no consumer. NIRSpec exclusions run through an unrelated header-marker mechanism (`CFP_BKG skipped:/excluded:`, `pipeline/.../nirspec/stage3.py:75`) with no portal round-trip.
- **Status is implicit everywhere.** Nothing models "obs X is at stage2 / deployed-intermediate / inspecting / finalized." What exists: `deployments.status` (publish-state only), `observations.latest_deployment_id`, `nircam_exposures.stage` (the only real stage tracking in the system), and file-existence inference.
- Stale doc bug found in passing: `python/campfire/deploy/nircam.py:27-30` claims the local `exposures.json` contract is retired because "nothing in the pipeline consumes it" — false since PR #285; `pull_exclusions` writes it and `field.py:_load_excluded_exposures` consumes it.

---

## 3. Findings by theme

Severity: **HIGH** = blocks or corrupts the stated reduction-loop goals; **MED** = significant friction or scale risk; **LOW** = hygiene.

### Theme A — Information architecture: there is no dashboard

**A1 (HIGH). `/admin` redirects to Access Codes.** `app/admin/page.tsx:4` — the panel's front door is its least reduction-relevant page. There is no overview: no "N exposures pending review," no draft-deployments queue, no storage budget at a glance, despite every one of those aggregates already having a server action (`getReductionProgress`, `getDeployments`, `getStorageBudget`).

**A2 (MED). Flat 9-item sidebar hides the real structure.** `app/admin/layout.tsx:9-19`. The pages cluster naturally into **Reduction** (NIRCam, Deployments, Intermediate Products — the latter two literally cross-reference each other in body prose: `deployments/page.tsx:113-119`, `intermediate-products/page.tsx:102-106`), **Access** (Users, Codes, Programs, Inspection Requests), and **Analytics** (Activity, Downloads). The sidebar presents them as nine peers, and the strict one-tab-one-table semantics prevents any cross-cutting view (e.g. an observation page that shows its deployments *and* its objects *and* its exposures together).

**A3 (HIGH). NIRSpec reduction has no admin surface at all.** The target model is "nirspec by program then observation" — today the only place an observation appears in admin is as a text string in the deployments table. No observation list, no per-observation drill-down, no exposure review.

**Proposed direction (A):**
- Make `/admin` a real dashboard: stat tiles + "needs attention" queues (pending reviews per field/filter, draft deployments, pending inspection-access requests, storage budget, recent deploy events), each deep-linking into a section with a pre-applied URL filter. This also satisfies epic #261 N6/D11 ("surface per-(field, filter) inspection coverage % in … the admin dashboard") — committed scope, not new invention.
- Regroup the sidebar into labeled sections (Reduction / Access / Analytics). Cheap, high-clarity.
- Add the NIRSpec reduction section: program → observation → (eventually) exposure review, mirroring `/admin/nircam`. Depends on Theme D.
- Structure Reduction pages around *scopes* (observation, field/filter) rather than *tables*: an observation detail page aggregates its deployments, storage objects, spectra counts, and review state. The `deployments`/`storage_objects`/`nircam_exposures` tables all already carry the scope columns needed for this join.

### Theme B — Deployment logging: broken NIRCam rendering + a leaky ledger

**B1 (HIGH, small fix). Five concrete NIRCam rendering bugs.** Root cause: the web consumer was never updated for the field-scoped schema (all verified):

| # | Bug | Location |
|---|---|---|
| 1 | Query selects `id, observation, status, n_targets, n_spectra, cfpipe_version, …` — **no `field`**; `DeploymentRow` type has no `field` | `web/lib/actions/deployments.ts:66-77`, `:31-41` |
| 2 | Row identity renders `{d.observation}` → blank cell for NIRCam; `n_spectra`/`cfpipe_version` are NULL → `—`; exposure count displays under a "Targets" header | `web/app/admin/deployments/page.tsx:173-177` |
| 3 | Confirm dialog interpolates `dep.observation` → `Revoke ""? … affects ? spectra.`; the observation filter (`.eq('observation', …)`) can never match a NIRCam row | `page.tsx:85-87`, `deployments.ts:76` |
| 4 | Lifecycle result parses `data?.spectra?.updated` but the NIRCam branch of `set_deployment_status` returns `{nircam_images: {updated: N}}` → toast always says "0 spectra updated" | `deployments.ts:195`, `functions.sql:3397-3399` |
| 5 | Events query selects `observation` but not `metadata`; NIRCam events carry field only in `metadata->>'field'` → audit log shows `—` for every NIRCam event | `deployments.ts:127-128`, `page.tsx:226` |

**B2 (HIGH). NIRCam deployment rows carry no provenance.** `insert_deployment` at `python/campfire/deploy/nircam.py:484` passes only `field`, `deployed_by`, `status`, and stuffs the exposure count into `n_targets`. `cfpipe_version`, `jwst_version`, `crds_context`, `config_snapshot` are all NULL — from a NIRCam deployment row you cannot answer "what pipeline version produced this field," even though the same deploy writes per-object `cfpipe_version` into `storage_objects`. The NIRSpec intermediates-draft path has the same hole (`deploy.py:291` hardcodes `cfpipe_version=None, n_targets=0, n_spectra=0`).

**B3 (HIGH). Silent bucket mutations — the ledger designed in `design-intermediate-products.md` §5.6 is only one-third implemented.** Exactly three code paths write `deployments`/`deploy_events` (`deploy.py:797/291`, `nircam.py:484`, plus lifecycle flips inside the `set_deployment_status`/`set_spectra_deploy_status` RPCs). Meanwhile these mutate storage or catalog state with **zero audit record**: `deploy remove` (deletes spectra/targets/shutters + bucket prefixes — verified zero `log_deploy_event`/`insert_deployment` calls in `remove.py`), `registry prune`/supersede, `deploy tiles`, `deploy rgb`/`sed`, `deploy pointings`, `deploy photometry`, `sync-programs`. The `deploy_events` action CHECK already includes `'delete'` and `'supersede'` (`tables.sql:939`) — the enum anticipated this; the writers were never added.

**B4 (MED). Recorded counts are intended, not actual.** `insert_deployment` records the size of the *planned* upload set; `success/failed` from `upload_files_parallel` are printed and discarded. A half-failed deploy logs a full-count `upload` event and the record drifts from reality permanently (nothing reconciles `deployments` against the bucket; the reconciler that exists targets `storage_objects` only, and its bucket LIST is R2-only — `registry.py:661`).

**B5 (MED). `deploy_events` has no `field` column** (verified: `tables.sql:925-940`) and the `log_deploy_event` RPC (`functions.sql:3240`) has no `p_field` — NIRCam events bury their scope in `metadata`. Event metadata has **four divergent shapes** (NIRSpec upload, NIRCam upload, NIRSpec lifecycle, NIRCam lifecycle) with no shared envelope. And neither `deployments.field` nor `deploy_events.observation` is indexed.

**B6 (LOW). Service-role deploys have no actor.** By design (`supabase.py:169-170`), unattended deploys log `deployed_by`/`actor` NULL. Consider recording a host/CI identity string so the "who" column is never empty.

**Proposed direction (B):**
1. *Immediately:* fix the five consumer bugs (select `field` + `metadata`, identity = `observation ?? field`, parse `spectra ?? nircam_images` updated counts, per-instrument labels, add an instrument filter). This is a small PR with outsized payoff.
2. Schema: add `deploy_events.field` + `p_field` on the RPC; indexes on `deployments(field)` and `deploy_events(observation)`; either add `n_exposures`/`n_mosaics` or a generic `n_items + item_kind` to `deployments`; optionally a generated `scope_label = COALESCE(observation, field)` for one stable identity column. Normalize event metadata to one envelope: `{instrument, scope, counts, flags:{draft, partial}}`.
3. Producer: populate NIRCam provenance columns; thread actual success/failed counts into the record with a partial-failure flag; emit `delete`/`supersede` events from `remove`, registry prune/supersede, and (at minimum) a summary event from tiles/rgb/pointings deploys.
4. *Bigger lever (aligns with design doc §5.6):* treat `deploy_events` as **the** ledger — every bucket-mutating op writes one typed event — plus a periodic reconcile that diffs `storage_objects` against the bucket (extend `list_bucket_keys` to OSN) so "records match reality" is provable, not assumed.

### Theme C — OSN transparency: browse, metadata, download

**C1 (HIGH). No download path for an admin.** `/api/download` is spectra-only; `/api/v1/storage/presign` requires an API bearer token (CLI path), not the admin cookie session, and authorizes via program/publish scoping. There is no admin-session action that presigns a GET for an arbitrary registry key — the Intermediate Products page has no download button because none is possible. Meanwhile the exact template exists: `presignExposurePngs` (`web/lib/actions/nircam-exposures.ts:173`, the PR #283 pattern) — admin-gated, key re-derived server-side, backend resolved from the registry, presigned GET. Generalizing it to any `storage_objects` row is a ~50-line server action (`requireAdmin` → fetch row for `backend`+`storage_key` → `getS3ClientForBackend(backend)` → short-TTL GET). Credentials are not a blocker; OSN read creds (`S3_OSN_*`) are already configured.

**C2 (HIGH). The registry browser is un-navigable.** `/admin/intermediate-products` hardcodes `pageSize: 200` (verified `page.tsx:78`) with no pagination controls (the action supports `page` — `storage-registry.ts:56`), filters on only 5 hardcoded product types (of the 22 in the CHECK enum), has no `field` filter, no free-text key search, no sort, and truncates keys to basename (`page.tsx:173`) so two objects in different observations can look identical.

**C3 (MED). No object detail / no live verification.** Every field a detail view needs is already in the row (`content_hash`, `sci_dq_hash`, `size_bytes`, `content_type`, `cfpipe_version`, `deployment_id`, `uploaded_by`, timestamps) — there's just no single-object action or drill-down UI. And the web tier has **no HEAD capability** (zero `HeadObjectCommand`/`ListObjectsV2Command` matches in `web/**`), so nothing can confirm an object is actually present/current in the bucket; registry `size_bytes`/`content_hash` are as-recorded (sometimes provisional `etag:`) and never re-verified.

**C4 (MED). "Single source of truth" has structurally invisible corners.** By design: tiles are never registered per-object (decision F1-B, `registry.py:24-26,176` — only `map_layers.total_size_bytes` aggregates), rgb/sed are never registered (`UNREGISTERED_PRODUCT_TYPES`, `registry.py:60`), and out-of-band writes have no row until someone manually runs `registry reconcile` (whose LIST is R2-only, so OSN-native objects are never cross-checked at all). A registry-backed browser can be excellent for products and reference files but cannot honestly claim to show "the bucket."

**C5 (LOW). Presign ergonomics.** `resolveObjectBackends` fails open to R2 on any error (silent wrong-backend risk as OSN becomes primary) and presigned URLs are never cached (re-signed on every call).

**Proposed direction (C):**
1. `presignStorageObjectDownload(id | key[])` admin server action (C1) + download buttons/object detail drawer in the registry UI. Optionally log downloads to `deploy_events`/`download_log`.
2. Rebuild the registry page on the shared table framework (Theme E): full pagination, all product types + status + backend + `field` + `observation` facets, key search, sortable size/created columns. Show full keys, grouped/tree-able by parsing with the already-existing `web/lib/layout.ts` `parseKey` (no new server capability needed — the TS layout mirror is a complete browser toolkit: parse, scope typing, sibling derivation, `isKnownKey`).
3. Supporting indexes (see §4): `storage_objects(created_at DESC)` (current default sort is unindexed), `(product_type, created_at)`, `(status, created_at)`, `(field)`, and pg_trgm on `storage_key` for substring search.
4. `headStorageObject(id)` admin action (live size/ETag/LastModified + present/absent flag) for per-object drift checks; and either an admin-only `listBucketObjects(backend, prefix)` (true bucket browse — the honest answer to C4) or, cheaper, surface the `registry reconcile` report (orphans/dangling/missing) in the panel and extend its LIST to OSN.

### Theme D — Reduction status: one instrument, no lifecycle, no enforcement

**D1 (HIGH). NIRSpec per-exposure review is a dead scaffold.** `spectrum_exposures` (`tables.sql:785`) has the full column set (`stage`, `review_status`, `masking`, `notes`, `exposure_ref`), RLS, indexes — and **zero readers or writers anywhere in the codebase** (verified by grep across `python/`, `pipeline/`, `web/`; the only matches are file-discovery helpers with a similar name). The deploy path registers the physical files but never creates review rows. Consequently "was every NIRSpec exposure looked at?" is unanswerable — not just unenforced but unqueryable.

**D2 (HIGH). No status model above publish-state.** No table answers "where is obs X / field F in its lifecycle?" The pieces exist to *infer* it (deployments + storage_objects + exposure stages + spectra counts) but every consumer would re-derive it differently. Epic #261 N6 already commits to surfacing inspection coverage; this audit recommends going one step further with a small explicit status model (below).

**D3 (MED). No `fields` entity.** `observations` is a table with `latest_deployment_id`; a NIRCam field is a text column scattered across three tables, authoritative only in a local TOML. There is nowhere to hang field-level status, no latest-deployment pointer per field (computing it seq-scans unindexed `deployments.field`), and the web can't even enumerate fields without `SELECT DISTINCT`.

**D4 (MED). The mandate has no teeth.**
- Combine consumes only `excluded` (`field.py:440-455`); `pending` exposures flow into mosaics silently. Nothing gates publish on `pending_review = 0`.
- `approved` is pure bookkeeping — the pipeline treats `pending` and `approved` identically, so the state machine can't distinguish "inspected and fine" from "never looked at" *in its effects*.
- Re-deploy resets new/changed exposures to `pending` (`nircam.py:641`) with no signal — a "fully inspected" field can silently regress.
- `review_status` has no DB CHECK constraint (values enforced only in a TS type).

**D5 (LOW).** `masking='needed'`/`correction='needed'` are advisory-only; fine, but the dashboard should surface them as queues, and the stale `nircam.py:27-30` docstring should be fixed before it misleads someone into deleting the exclusions contract.

**Proposed direction (D):**
1. **Activate the NIRSpec loop** (the largest single gap vs. the stated goal): wire the NIRSpec deploy path to upsert `spectrum_exposures` rows alongside the registration it already does (mirroring `_upsert_exposures`'s preserve-review-fields semantics); add a `nirspec_reduction_progress` view (per observation × grating, mirroring `views.sql:144`); build the NIRSpec review UI as a clone of `/admin/nircam` (+ the generalized review shell from Theme E); add `campfire deploy nirspec pull` materializing exclusions into the pipeline's existing exclusion mechanism.
2. **`fields` table** (`name` PK, `program_slugs`, `filters`, `latest_deployment_id`, …) populated by a `sync-fields` command mirroring `sync-programs` — gives field status a home and the dashboard an enumerable entity. Plus `deployments(field)` index regardless.
3. **A `reduction_status` table** keyed `(scope_type, scope, sub_scope)` — e.g. `('field','cosmos','f444w')` / `('observation','rubies-egs61','')` — carrying `stage`, `updated_at`, `updated_by`, upserted by `cfpipe` at stage boundaries and by `campfire deploy`. This is the single cheap table that makes "where is everything?" a `SELECT`, instead of an N-way join each consumer reimplements. (Alternative if that feels heavy: a `reduction_dashboard` SQL view UNION-ing the NIRCam and NIRSpec progress views with latest-deployment info — read-model only, no new writers — and accept that pre-deploy local stages stay invisible.)
4. **Enforcement:** publish gate (in `set_deployment_status` or a CLI pre-flight) refusing (or `--force`-warning) when `pending_review > 0` for the scope; a re-deploy summary line ("N exposures reset to pending"); a DB CHECK on `review_status`; decide whether `approved` should ever gate combine (allowlist mode) or is formally advisory.

### Theme E — Tables, filters, and state (the daily-use friction)

**E1 (HIGH). `/api/admin/activity` is unbounded.** Verified: `paginateQuery` pulls **all** comments (`route.ts:78`) and **all** flag-audit rows (`:128`) with per-row embedded joins, JS-sorts (`:193`) and slices (`:206`), then runs a **second full scan of both tables** (`:234-247`) for the user dropdown — on every page load, every filter change. Replace with one `get_activity_feed` RPC (UNION ALL + server filter/sort/paginate + windowed count) and a tiny `get_activity_users()`.

**E2 (HIGH). No server-side sorting on any admin list; no URL state anywhere.** Sort orders are hard-coded (`deployments.ts:72`, `storage-registry.ts:73`, `nircam-exposures.ts:66-69`); every page keeps filters/page in `useState`, so back-navigation, refresh, and link-sharing all lose state. The fix already exists in-repo: the public explorer's URL-as-state pattern (`app/nirspec/page.tsx:34-46,98-106` + `lib/utils/url-params.ts`) and its TanStack Table + `TablePagination` + `ColumnVisibilityDropdown` stack (`components/spectra/SpectraTable.tsx`, `components/ui/TablePagination.tsx`).
**E3 (MED). Silent truncation:** deployments 100 / events 50 (`deployments/page.tsx:72-73`), registry 200 (`intermediate-products/page.tsx:78`) — all despite the actions supporting `page`.
**E4 (MED). Three data-access mechanisms** (REST `fetch` routes, server actions, one direct browser-side supabase RPC in downloads) and refetch-everything-on-mutation everywhere; no TanStack Query in admin despite `useSpectraQuery` existing.
**E5 (MED). `count:'exact'` on every list** (`deployments.ts`, `storage-registry.ts`, `nircam-exposures.ts`) — second full COUNT per request; will dominate on `storage_objects`. Prior DB audit finding #1 is the same disease on the public side.
**E6 (MED). Fetch-all-then-JS paths:** `getNircamImages` pulls all mosaics and filters client-side (`nircam.ts:41-49,95-105`); `getExposureFilterOptions` scans all exposures for distinct values (`nircam-exposures.ts:418-442`); `getNircamExposureIds` returns the entire filtered ID list on every page/filter change (`:97-125`, fired from `nircam/page.tsx:299-307`) to feed a sessionStorage nav cache that breaks on refresh/direct-entry anyway; `/api/users` fetches all users+access+programs.
**E7 (LOW).** Whole-page spinner gates, `alert()`/`window.confirm()` for errors/destructive actions, users page (776 LOC) with no search, inconsistent filter UIs (raw `<select>` vs pill buttons vs `FilterChip`).

**Proposed direction (E):**
1. One shared **admin table framework**: TanStack Table + `TablePagination` + filter-chip bar + skeletons, driven by a small typed URL-query-state hook (generalize `lib/utils/url-params.ts`), fetching via TanStack Query. Build it once, migrate every list onto it — this single move fixes sorting, pagination, back-nav, truncation, and the loading UX across the whole panel.
2. Back each heavy list with an **admin RPC** following the `get_filtered_objects_paginated` house pattern (whitelisted sort column/direction, one scan, `count(*) OVER()`): `get_admin_storage_objects`, `get_admin_deployments`, `get_admin_deploy_events`, `get_activity_feed`, `get_admin_users`, `get_admin_exposures` (which should also return the nav-ID slice, retiring the unbounded ID fetch). Small tables (codes, programs) can stay on simple selects.
3. Generalize the NIRCam detail page's review shell (keyboard triage, auto-save-on-nav, prefetch window) into a reusable component, with prev/next derived from URL-encoded filter state rather than sessionStorage — then reuse it for NIRSpec review (Theme D).

### Theme F — Database hygiene (supporting findings)

**F1 (MED). Missing indexes for admin access paths** — see §4 for the consolidated DDL. The two that fix current-default-path full scans: `storage_objects(created_at DESC)` and `deploy_events(observation)`.
**F2 (MED). `get_download_stats` (`functions.sql:2926`) and `get_storage_budget` (`functions.sql:3029`) are SECURITY DEFINER without `SET search_path`** — the standard Supabase linter finding and a real definer-function hazard. Add `SET search_path = public, pg_temp`. (The helper functions `is_admin()` etc. do this correctly, and RLS policies use initplan-wrapped calls throughout — RLS is *not* an admin-perf problem today.)
**F3 (LOW).** `get_storage_budget` runs four full GROUP BY scans per call (`functions.sql:3068-3099`) — fine now; if the dashboard polls it, move to a rollup refreshed at deploy time. `nircam_reduction_progress` is a plain view doing a full GROUP BY per call — same story (promote to matview only when it hurts; note the migra caveat: matviews need hand-written migrations).
**F4 (LOW).** `nircam_exposures.review_status`/`stage` and `spectrum_exposures.review_status` have no CHECK constraints; `deployments` count columns are NIRSpec-shaped (see B). The prior DB audit's finding #6 (naive `timestamp` columns) also touches `deploy_events.occurred_at`'s neighbors — fold into the same migration if convenient.

---

## 4. Consolidated schema changes

New indexes (plain `CREATE INDEX`, all diff-trackable):

```sql
-- fixes current default-path full scans (do these first)
CREATE INDEX idx_storage_objects_created_at ON public.storage_objects (created_at DESC);
CREATE INDEX idx_deploy_events_observation ON public.deploy_events (observation) WHERE observation IS NOT NULL;
-- browse/filter/sort support
CREATE INDEX idx_storage_objects_status_created  ON public.storage_objects (status, created_at DESC);
CREATE INDEX idx_storage_objects_product_created ON public.storage_objects (product_type, created_at DESC);
CREATE INDEX idx_storage_objects_field ON public.storage_objects (field) WHERE field IS NOT NULL;
CREATE INDEX idx_deployments_field ON public.deployments (field) WHERE field IS NOT NULL;
CREATE INDEX idx_deployments_status_deployed ON public.deployments (status, deployed_at DESC);
CREATE INDEX idx_user_profiles_created_at ON public.user_profiles (created_at DESC);
-- activity feed RPC support
CREATE INDEX idx_comments_created_id ON public.comments (created_at DESC, id DESC) WHERE NOT is_deleted;
CREATE INDEX idx_flag_audit_changed_id ON public.flag_audit_log (changed_at DESC, id DESC);
-- key search (requires pg_trgm, already used for comments.content)
CREATE INDEX idx_storage_objects_key_trgm ON public.storage_objects USING gin (storage_key gin_trgm_ops);
```

New columns / constraints:
- `deploy_events.field text` (+ `p_field` on `log_deploy_event`); consider generated `scope_label = COALESCE(observation, field)` on `deployments` and `deploy_events`.
- `deployments`: `n_items int` + `item_kind text` (or `n_exposures`/`n_mosaics`) so NIRCam stops overloading `n_targets`; optional `partial boolean` + `n_failed int` for actual-vs-intended counts.
- CHECKs on `nircam_exposures.review_status`, `spectrum_exposures.review_status`.

New tables:
- `fields` (D3) — field entity + `latest_deployment_id`.
- `reduction_status` (D — option 3) — explicit lifecycle keyed `(scope_type, scope, sub_scope)`.

New RPCs (all STABLE, admin-gated, `SET search_path`, whitelisted sort, windowed count): `get_admin_storage_objects`, `get_admin_deployments`, `get_admin_deploy_events`, `get_activity_feed` + `get_activity_users`, `get_admin_users`, `get_admin_exposures`, `get_admin_spectrum_exposures`; new views `nirspec_reduction_progress` and (optionally) `reduction_dashboard`.

Function fixes: `SET search_path = public, pg_temp` on `get_download_stats` and `get_storage_budget`.

---

## 5. Prioritized roadmap

Ordered so each phase is independently shippable and merges inert (per the epic-#261 process rules: one schema-changing PR at a time; regenerate `seed.sql` on relevant column changes).

**Phase 0 — Stop the bleeding (small PRs, days).**
1. Fix the five NIRCam deployment-log bugs (B1) — pure web change.
2. Rewrite `/api/admin/activity` as `get_activity_feed`/`get_activity_users` RPCs (E1) + the two supporting indexes.
3. Index migration: `idx_storage_objects_created_at`, `idx_deploy_events_observation`, `idx_deployments_field`; `SET search_path` on the two definer RPCs (F2).
4. Fix the stale `nircam.py:27-30` docstring (D5).

**Phase 1 — The admin table framework (the multiplier).**
5. Build the shared URL-state + TanStack Table/Query admin list stack (E, prop 1) and migrate Deployments + Intermediate Products (→ "Storage") + NIRCam exposures onto it; add the admin RPCs behind them (E, prop 2). Wire real pagination/sort/filters everywhere; retire the sessionStorage nav cache.
6. Regroup the sidebar; land the `/admin` dashboard page with the tiles that are already computable today (A).

**Phase 2 — OSN transparency.**
7. `presignStorageObjectDownload` + object detail drawer + full-key display/search/facets on the Storage page (C1–C3).
8. `headStorageObject` live verification; surface the reconcile report (or add `listBucketObjects`) and extend reconcile's LIST to OSN (C3–C4).

**Phase 3 — Complete the ledger (schema PR).**
9. `deploy_events.field` + metadata envelope normalization + NIRCam/draft provenance population + actual-count recording + `delete`/`supersede` events from remove/prune (B2–B5). This is mostly Python-side with one small migration.

**Phase 4 — Reduction status, both instruments (overlaps epic #261 N6).**
10. `fields` table + `sync-fields`; unified `reduction_dashboard` read model; dashboard rendering of NIRSpec obs status from deployments+spectra+storage_objects (D2–D3).
11. Activate `spectrum_exposures` (deploy writer, progress view, review UI clone, `nirspec pull`) (D1).
12. Enforcement: publish gate on `pending_review`, re-deploy pending-reset warnings, `review_status` CHECKs; decide `approved` semantics (D4). Coordinate with N6's coverage-% and provenance-stamping deliverables.

---

## 6. Cross-references

- **Epic #261 / N6 (#268):** Phase 4 here is the superset of N6's dashboard deliverables (coverage %, `campfire status` parity). The NIRSpec-loop activation (D1) is *not* in N6's scope — it is the NIRSpec mirror of what N1–N6 built for NIRCam and probably deserves its own epic.
- **`docs/design-intermediate-products.md` §5.6:** the `deploy_events` ledger + admin view were designed there; B3/B5 document how far implementation got and what's left.
- **`docs/db_audit_2026-06-19.md`:** findings #1 (exact-count pagination), #6 (naive timestamps), #20 (access-preamble duplication) compound the admin-specific findings here; the RPC-with-windowed-count pattern recommended there is the same one Phases 0–1 adopt.
