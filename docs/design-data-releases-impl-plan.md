# Data releases: phased implementation plan (stage 3)

**Status:** v1, 2026-10-11. Built on the stage-1 findings
([design-data-releases-impl-findings.md](design-data-releases-impl-findings.md)) and the
agreed stage-2 infrastructure ([design-data-releases-infra-proposal.md](design-data-releases-infra-proposal.md),
§6 decisions). Implements the design in [design-data-releases.md](design-data-releases.md)
(§0, §3, §7 unchanged); where code or production contradicts the design, the phase says so
and §11 collects the proposed amendments. Nothing here changes the design silently.

## 0. How to read a phase

Every phase has the same eight parts: goal and what users see; code surface (file:line for
existing code, *new* for additions); schema / policy / storage changes including backfills;
tests, mapped to the P-1 infrastructure; verification gates per rung of the rollout ladder;
production risk, how we would notice, how we roll back; dependencies; rough size.

**Rollout ladder** (agreed in stage 2; the rungs are referred to by letter):

| rung | meaning | evidence that advances it |
|---|---|---|
| **L** local | native Supabase stack; the scale DB (scrubbed production dump) where timings or real distributions matter | commands and timings pasted into the PR |
| **C** CI | the four required checks green, including the access matrix and the no-drift check | the merge button |
| **B** preview branch | Supabase preview (seeded) + Vercel preview for anything with a web surface | a screenshot or a curl against the preview URL |
| **S** production shadow | compute and log, never act: `--dry-run`, a `p_dry_run` RPC parameter, a log-only mode flag; or a read-only comparison query run by an agent session under `BEGIN READ ONLY` | counts recorded in the PR or the phase log |
| **A** production, admin-only or flagged | behind `is_admin()` in RLS, an env flag (`OSN_READ_ENABLED` pattern), or a CLI opt-in | a week of admin use without incident, or one real freeze |
| **G** general | visible to every user / default client behaviour | the phase is done |

**Size scale:** S = one PR, a session or two; M = two to three PRs, about a week part-time;
L = four to five PRs, two to three weeks; XL = more, split as shown.

**Standing conventions for every PR in this plan** (from P-1): every migration starts with
`SET lock_timeout = '5s'; SET statement_timeout = '60s';`; every new function has
`SET search_path = public, pg_temp`, `REVOKE ALL ON FUNCTION ... FROM PUBLIC` and explicit
grants; every new policy has an explicit `TO`; every new table gets rows in `seed.sql`, a
row in the access matrix per principal, and (if the client mirrors it) a sqlite schema
bump; a `pg_dump` of `public` is taken before merging any schema-changing PR.

**Constraints carried into every phase** (verified in stage 1 unless noted):

- Web and CLI reach Postgres only through PostgREST: one request is one transaction; any
  multi-statement atomic step is a single SQL function.
- Compute is now Small (512 MB shared buffers, 5 MB `work_mem`, 90 connections); role
  statement timeouts are 3 s anon / 8 s authenticated; functions may raise their own.
- Vercel Pro: 300 s default, 800 s maximum per function (docs, 2026-10-11).
- OSN: conditional PUT and conditional copy work; `UploadPartCopy` works with a 5 MiB
  minimum part; no versioning, no Object Lock; the data-manager key cannot abort multipart
  uploads or read bucket configuration.
- A move to self-hosted Supabase on a VM is planned within months (your statement): no
  phase may add a dependency on Supabase branching, the dashboard, or the GitHub migration
  integration beyond what exists today; everything must work through `supabase db push
  --db-url` and plain Postgres roles.
- No PITR; the daily backup is 7 days; the pre-merge dump is the fine-grained net.

---

## 1. P-1: development infrastructure (agreed)

**Goal.** A red test blocks a merge; the schema files and the migration chain cannot drift;
every principal including `anon` is asserted per table; a production-shaped database exists
locally; the anon exposure is closed. Nothing user-visible except that anonymous PostgREST
reads stop returning rows (no web path depends on them).

**PRs, in order** (sizes are estimates):

| # | PR | contents | gates |
|---|---|---|---|
| 1 | `ci/python-and-web` | `.github/workflows/python.yml` (path filter `layout/**`, `python/**`; `pytest python` + `pytest pipeline/tests/test_layout.py`), `.github/workflows/web.yml` (path filter `web/**`; new `typecheck` script = `tsc --noEmit`, `vitest run`, the existing `next build` + budget folded in). Branch protection: both required, `enforce_admins` on | C green on the PR itself; `gh api .../protection` shows the required contexts |
| 2 | `supabase/native-stack-and-pgdelta` | `config.toml`: `[experimental] stack = true`, `[experimental.pgdelta] enabled = true`, remove fixed ports, `health_timeout`; README section; `database.yml` replacing `supabase-mv-indexes.yml`: `supabase start --runtime native` → `supabase db schema declarative sync --strict-coverage --no-apply` must create no file (drift gate) → `db reset` → 7 guards → `python/tests/sql/*.sql` → access matrix (PR 4). Required check | the no-diff baseline: if `sync` proposes a migration against the current chain, that file is reviewed and committed as the "catch-up" migration (it is a list of real drift, mostly comments and matview details migra could not see) **before** anything else changes |
| 3 | `security/anon-and-public-execute` | migration: `REVOKE ALL ON ALL TABLES/SEQUENCES IN SCHEMA public FROM anon`; `ALTER DEFAULT PRIVILEGES ... REVOKE ... FROM anon` (tables, sequences, functions); add `TO authenticated` to the 22 policies (`policies.sql:229-256, 288-342, 370, 426, 511-679, 698-707, 788-871, 1008-1052, 1565-1668`); `REVOKE ALL ON FUNCTION ... FROM PUBLIC, anon` for every function except an explicit anon allowlist (device-flow start/poll, `validate_api_key`, anything the login flow needs; review each of the 58 advisor hits); keep `GRANT USAGE ON SCHEMA public TO anon` so PostgREST can still answer the auth endpoints | L: matrix row `anon` returns 0 for every table and the lint queries pass; C; B: sign-in, device flow (`campfire login`), share-link visit, `api/programs` as signed-out still work on the preview; S: none needed; G: merge, then re-run the stage-1 HTTP probes against production (expect 401/0 rows) |
| 4 | `tests/access-matrix` | `supabase/tests/check_access_matrix.sql` (fixture: public program, private program + grantee, admin, link account, one draft) with a principal × surface table; the two lint queries (`roles = '{public}'` empty; anon-executable functions ⊆ allowlist). Lands with or just before PR 3 | C |
| 5 | `supabase/seed-regeneration` | extend `scripts/generate_seed.py` to emit `spectrum_line_fits` + `spectrum_lines`, `object_photometry` + bands, `deployments`, `nircam_images`, `share_links` (one live, one revoked), `storage_objects` with the real hashes of the sampled spectra, a draft and a revoked spectrum; regenerate `seed.sql` (< 2 MB) | L: `db reset` succeeds; B: the preview branch of this PR seeds; the RLS harnesses in `python/tests/sql` that are seed-anchored still pass |
| 6 | `tooling/scale-db` | `scripts/dump_prod_for_local.sh` (needs the direct connection string: dashboard → Connect → session pooler; `pg_dump --data-only --schema=public` with the exclusion list from the proposal §3.1), `scripts/scrub_local_dump.sql`, `scripts/load_scale_db.sh` (`db reset` → `psql -f` under `session_replication_role = replica`); README with measured reset and load times | L only; the dump never leaves the machine |
| 7 | `tests/shared-fixtures-and-parity` | one `FakePostgrest` fixture in `python/tests/conftest.py` replacing the 11 copies; an in-process S3 fake for `python/campfire/storage` unit tests; `scripts/sync_layout_products.py` generating `web/lib/layout-products.json` from `products.py` with a `--check` CI step (the `sync_linelist.py` pattern); `pipeline/tests/test_layout.py` and `web/lib/layout.test.ts` wired into `python.yml` / `web.yml`; migration-header grep in `database.yml`; `scripts/measure_rpc.py` (EXPLAIN ANALYZE + `pg_stat_wal` delta + temp usage for a named RPC and parameters) | C |
| 8 | operational | OSN admin email (versioning, abort-incomplete-MPU lifecycle rule, key capabilities); pipeline test runtime measured (`time pytest pipeline`) to decide whether `pipeline/**` joins `python.yml` | — |

**On your CI question.** Each workflow declares `on: pull_request: paths:`; GitHub runs a
workflow only when the PR touches a matching path, and a required check that did not run
is treated as passing only if the workflow is configured to report success on skip (the
usual pattern is a tiny "noop" job for the skipped case, or using GitHub's "paths-filter"
action inside one workflow). So a web-only PR never runs the pipeline suite, and a
pipeline-only PR never builds the web app. The database workflow runs when `supabase/**`,
the seed generator, or the layout package change.

**Tests to write.** The matrix and lints (PR 4); seed-coverage assertion (every table the
schema defines has at least one seed row or is on an explicit "empty is fine" list);
layout generator `--check`; the shared fakes are refactors of existing tests.

**Production risk.** PR 3 is the only production change. Anything that queried PostgREST as
`anon` stops getting rows; the only known caller is `web/app/api/programs/route.ts:16-33`
for signed-out visitors, which already expects zero rows. Auth flows use the `auth` schema
and the service role, not anon table grants. Notice: the HTTP probes after merge; sign-in
and `campfire login` smoke on production. Rollback: a migration re-granting `SELECT` to anon
on the affected tables (kept drafted in the PR, not applied).

**Dependencies.** None. **Size:** L (seven PRs, two to three weeks part-time).

---

## 2. P0: decisions landed, `is_public` guard, science hash

**Goal.** The identity that survives a stamp exists on every NIRSpec FITS row, and nothing
can flip a program private by omission. User-visible: nothing, except that a no-op rerun no
longer badges objects as reprocessed once P0c lands.

The anon test and fix moved to P-1; the OSN questions are answered (findings §2.6) except
the bucket-owner items; what remains of the design's P0 is two things.

### P0a: `is_public` guard (S)

**Code surface.** `python/campfire/deploy/config_sync.py:125-135` (`program_typed_row`;
`section.get("is_public", False)` at `:132`); `python/campfire/deploy/supabase.py:452-495`
(`upsert_programs`, called from `deploy.py:300, 647` and `cli.py:1059`); the config-plane
check it bypasses, `config_sync.py:244-335`.

**Change.** `program_typed_row` emits `is_public` only when the TOML section carries the
key; `upsert_programs` fetches the cloud row first and refuses (with a clear message and a
`--force-program-visibility` escape) to write `is_public = false` over a cloud `true`. Also
log the attempted flip through `log_deploy_event` (`config_sync` action already exists).

**Tests.** Unit tests with the shared `FakePostgrest` fixture: omitted key does not write;
true→false refused; false→true allowed; `--force` path.

**Gates.** L/C unit tests. S: a read-only script compares every `programs.toml` section on
your deploy machines against the cloud `is_public` for the 36 rows and prints the ones
that would flip today (expect zero; if not, that is a live finding). G: merge; the next
deploy's log shows the guard ran.

**Risk.** A deploy that legitimately needs to make a program private is blocked until
`--force`; that is the intent. Rollback: revert the Python change (no schema).

### P0b: science hash, definition and write path (M)

**Definition** (proposed; the design names the volatile set, this fixes the serialization):
`sha256` over, for each HDU in file order, the HDU's data bytes exactly as stored on disk
(no scaling, `do_not_scale_image_data=True`, raw table bytes) and the header rendered as
sorted `KEY=VALUE` pairs after removing `CMPFRTIM`, `CMPFRDR`, `DATE`, `CHECKSUM`,
`DATASUM`, `COMMENT`/`HISTORY` and blank cards. Stored as `sha256:<hex>`. One
implementation, in the pipeline package (`campfire_pipeline/metadata/science_hash.py`,
*new*), because it needs astropy and because `linefit_stage.py:181-192` must use the same
function; the deploy side imports it through the `[deploy]` extra (which already depends on
`campfire-pipeline`, `python/pyproject.toml` `deploy` extra). Precedent to copy:
`python/campfire/storage/hashing.py:120-262` (`sci_dq_hash`/`wcs_hash`, whitelist + salt +
`do_not_scale_image_data`).

**Code surface.** Pipeline: `metadata/reader.py:179-185` (`_compute_file_hash`; add
`science_hash` to `read_fits_metadata`), `linefit_stage.py:181-192, 396-412` (compute both;
refit when the science hash changed; a product lacking the new `SCIHASH` card is compared
by `SPECHASH` as before, so no mass refit). Deploy: `deploy/registry.py:176-219` (row
payload), `storage/plan.py:259-262` (precomputed hashes), `deploy/supabase.py:663-706`
(`batch_upsert_spectra`, unchanged until P0c). Schema: `spectra.science_hash text CHECK
(^sha256:)`, `storage_objects.science_hash text` (NIRSpec FITS types only), btree indexes on
both (for `campfire identify`; today only the partial sync index covers `content_hash`,
`indexes.sql:560-567`). Sync payloads: add `science_hash` to `get_spectra_for_sync`
(`functions.sql:1562`) and the storage stream; sqlite `SCHEMA_VERSION` 9→10
(`store.py:27`); `web/lib/types.ts`.

**Backfill.** `scripts/backfill_science_hash.py` (template `backfill_zfit_scalars.py`):
page `spectra WHERE science_hash IS NULL` (and `storage_objects` for `nirspec_spec` /
`nirspec_lines`), stream each file from OSN, compute, write both rows in 500-row batches.
About 58 GB read; at the laptop's measured rate that is several hours, less from a TACC
node. Resumable by construction. Note the `bump_spectra_updated_at_trigger`
(`triggers.sql:618-636`) fires only `OF` a listed column set, so the backfill does **not**
bump `updated_at` and clients will not see the new column until they `sync --full` or the
column is added to the trigger list; see decision D-P0-1.

**Tests.** Golden: three tiny synthetic FITS (single HDU image, multi-extension with a
binary table, scaled image) with expected hashes committed; invariance tests (rewrite with
new `CMPFRTIM`, add `CMPFRDR`, add `CHECKSUM`/`DATASUM` → same hash) and sensitivity tests
(one data byte, one non-volatile card → different hash); a pipeline↔deploy parity test
(both call the same function on the same file). Backfill: `--dry-run --limit 20` against
the scale DB with files from the OSN read path.

**Gates.** L/C goldens. S: `backfill --dry-run --limit 50` on production (reads only,
prints hashes), then the real backfill with a progress line per 500; an agent session
confirms afterwards with `count(*) WHERE science_hash IS NULL = 0` and reports how many
spectra have identical science hashes but different whole-file hashes across the registry
history (a first measure of CMPFRTIM noise). G: deploy writes it for every new deploy;
`cfpipe nirspec linefit` writes `SCIHASH`.

**Risk.** The backfill reads 58 GB from OSN and issues ~200 batched updates; both are
within what one `campfire sync` already does. A bug in the hash definition after the
backfill means a second pass; the definition is therefore frozen by the golden file and
versioned (`science_hash_version = 1` in a header comment and in the column comment).
Rollback: the column is additive; drop it.

### P0c: switch the "changed" decisions to the science hash (S, after the backfill)

**Code surface.** `deploy/supabase.py:704-705` (`changed_hashes` → compare science hashes
when both sides have one, else whole-file); `reconcile.py:590-613` unchanged (consumes the
set); `views.sql:308-309` and `functions.sql:1884-1885` (`stale_spectrum` uses science
hashes; keep the whole-file comparison as the fallback when either is NULL); the web
CDN/Worker cache identity stays whole-file (`r2.ts:170-183`, `object.ts:88-94`), by design.

**Tests.** SQL guard: a fixture with same science hash / different file hash reads as
not stale; the sync RPC record agrees with the view (the duplicated predicate at
`functions.sql:1877-1885` is replaced by a call to one function, closing that drift).

**Gates.** S: on production, a read-only query runs the new predicate next to the old one
and lists the line fits and objects whose status would change (expect: fewer stale, never
more). G: merge.

**Dependencies.** P0b backfill complete. **Size:** P0 overall M–L (four PRs).

---

## 3. P1: layout roots, key roles, deploy lease, readiness

**Goal.** The key contract knows about `archive/<sha256>/<relpath>` (cloud) and
`dr/<tag>/` (local) without ever mapping an archive key back onto a live key; the presign
PUT allowlist is separate from the read allowlist; a deploy holds a visible lease; an admin
can ask "is the catalog ready to freeze" and get the stage-1 numbers back from a function.
User-visible: nothing (`campfire dr status` is admin-only).

**Code surface.**

| piece | existing | change |
|---|---|---|
| `ParsedKey` | `layout/campfire_layout/bijection.py:23-27`; TS `web/lib/layout.ts:195-199` | add `root: 'live' \| 'archive'` and `archive_hash: str \| None`; default `live` so every current caller is unchanged |
| `parse_key` | `bijection.py:190-238` | accept `archive/<64 hex>/<relpath>` → `parse_relpath(relpath)` with `root='archive'`; `dr/` never appears in cloud keys |
| builders | `keys.py:63-77` (`storage_key`), `bijection.py:245-254`, `config.py:39-50`, `registry.py:136-161` (`row_for_key`), `registry.py:449-469` (`canonical_key_for`); TS `layout.ts:182-189, 366-377`, `r2.ts:35-41` | every builder takes the parsed root into account: `storage_key(spec, root='archive', archive_hash=…)` builds the archive key; rebuilding a `root='archive'` parse without the hash raises; `row_for_key` stores `root` and refuses to derive `product_type`/scope for an archive key into an *active live* row |
| `derive_sibling` | `bijection.py:257-273`; `layout.ts:349-359` | raise on `root='archive'`: siblings of an archived file are found through the registry (each sidecar has its own hash), never by string surgery |
| allowlists | `is_known_key` `bijection.py:298-312`; `isKnownKey` `layout.ts:398-409`; call sites `deploy/presign/route.ts:108` (PUT), `v1/storage/presign/route.ts:54`, `v1/storage/download/route.ts:69`, `nircam-fits/route.ts:58`, `registry.py:521` | split into `is_readable_key` (live + archive) and `is_writable_key` (live only); the presign route uses the writable one, so no client can ever mint a PUT under `archive/` |
| local tree | `paths.py:29-48` (`_TREES`, `Roots`), `lifecycle.py:16-41`, `scope.py:27-38`, `products.py:37` | add tree `dr` with a new `LifecycleClass.RELEASE` ("cloud-backed, immutable, never purged by sync"); `dr/<tag>/meta/campfire.db` and `dr/<tag>/products/<relpath>` |
| goldens | `layout/conformance/layout_golden.json`; `pipeline/tests/test_layout.py`; `web/lib/layout.test.ts` | new sections: `archive_keys` (parse, rebuild with hash, rebuild without hash raises, sibling raises), `writable_keys` / `readable_keys`, `dr_tree`; TS `PRODUCTS` now comes from the generated JSON (P-1 PR 7) |
| chunking | `web/lib/r2.ts:29` (50), `storage-registry.ts:488` (100), Python 200 | archive keys add 72 chars: drop the TS chunk to 25 or switch the lookup to an RPC taking `text[]` (recommended; removes the URL-length cliff for all keys) |
| deploy lease | `deploy_scope_state` (`tables.sql:1384-1394`), `claim_deploy_scope` (`functions.sql:6892-6935`), callers `deploy.py:312, 401, 604, 1004-1012` | add `in_progress_since timestamptz`, `in_progress_by uuid`, `heartbeat_at`; `begin_deploy_scope` (sets the lease, refuses if a fresh lease is held unless `--steal`), heartbeat from the upload loop every N batches, `claim_deploy_scope` clears it at finalize; readiness and the freeze read it |
| readiness RPC *new* | inputs: `spectra.cfpipe_version` × observation, draft rows (`spectra`, `nircam_images`, `deployments`), leases, re-drafted NIRCam fields (published mosaics whose row is now `draft`), dangling `nirspec_spec` rows (280 today), `etag:` rows (0 relevant), `spectrum_line_fits_status` stale counts, needs-review counts, `science_hash IS NULL` counts | `dr_readiness(p_components text[])` SECURITY DEFINER with the admin/service-role guard pattern (`functions.sql:6642-6646`), `REVOKE FROM PUBLIC`; `campfire dr status` prints it; the admin `/admin/deployments` page can show it later |

**Schema.** Three nullable columns on `deploy_scope_state`; `storage_objects.root text NOT
NULL DEFAULT 'live' CHECK (root IN ('live','archive'))` (cheap now, used by P2); one new
function. No backfill.

**Tests.** Goldens in both arms (CI); unit tests for the lease state machine with the shared
fake; matrix row for `dr_readiness` (admin only; anon/user get `permission denied`);
`measure_rpc.py` on the readiness query against the scale DB (it touches `spectra`,
`storage_objects` and the status view; must stay under a few seconds).

**Gates.** L: goldens; readiness on the scale DB reproduces the stage-1 numbers. C. S: an
agent session runs `dr_readiness()` on production read-only and compares with findings
§1 (230 obs, 31 version strings, 280 dangling rows, 1,339 needs review). A: `campfire dr
status` for admins. G: not applicable.

**Risk.** The parse change is on the hottest path of every deploy and every sidecar
resolution; the goldens are the guard. The lease adds a write at deploy start; a crashed
deploy leaves a lease, which `dr status` shows and `--steal` clears. Rollback: revert; the
columns are additive.

**Dependencies.** P-1 PR 7 (generated TS products table). **Size:** M (three PRs: layout,
lease, readiness).

---

## 4. P2: retain-on-supersede, archive authorization, budget, gc dry-run

**Goal.** Before any redeploy overwrites a live NIRSpec final, its old bytes exist under
`archive/<sha>/<relpath>` with a registry row, immutably. Archive objects are readable by
exactly the people who may read the live spectrum they came from. User-visible: nothing;
deploys get slightly slower on redeploys.

**Mechanism as the code allows it** (amends design §4.2, see §11.5): the live registry row
cannot be both "superseded" and the pointer for the new bytes, because the upsert keyed on
`(backend,bucket,storage_key)` (`tables.sql:2118`) is what the new bytes land on. So the
retain step **inserts** an `archive` row (`root='archive'`, `status='archived'`, same
`product_type`/`spectrum_id`/`program_slug`, old `content_hash`, new `archived_from_key`,
`archived_at`, `archived_by_deployment`) and writes a `supersede` event; the live row is
then overwritten as today. "Superseded" becomes a relation (archive row → live key), not a
status on the live row.

**Code surface.**

| piece | existing | change |
|---|---|---|
| retain helper (TS) *new* | presign route `web/app/api/v1/deploy/presign/route.ts:48-135`; registry lookup pattern `web/lib/r2.ts:124-212` | `web/lib/server/retain.ts`: for each upload whose key is a retained type and whose active row's `content_hash` ≠ the client's `new_hash`: `HeadObject` (ETag, size; size must equal the row's `size_bytes`, else 409 `drift`), `CopyObject` to the archive key with `x-amz-copy-source-if-match: <etag>` (412 → 409 `drift`), skip when the archive key already exists with the same size, insert the archive row, log the event, then sign. Parallelism 16; the route's `maxDuration` set to 300 |
| client contract | `python/campfire/deploy/r2.py:162-277` (presign call), `storage/plan.py:282-303` (knows the server hash) | the presign body carries `expected_old_hash` and `new_hash` per key; a 409 aborts the batch with the drifted keys listed; `--no-retain` is admin-only and logged |
| boto3 path | `r2.py:555-652`, `:432-437` | `python/campfire/deploy/retain.py` (*new*), same algorithm with boto3, called before `upload_files_direct`; a conformance test feeds both implementations the same fixture and asserts identical archive keys and rows |
| bypassing writers | `deploy.py:1091-1110, 1181-1199`, `lines.py:272-283`, `photometry.py:1416-1440`, `fitsgl/deploy.py:321-323` | json/zfit/lines go through the same presign route so they are retained; P(z) and FitsGL are not retained types |
| archive authz | `filter_accessible_storage_keys` `functions.sql:5139-5168`, `filter_link_storage_keys` `:5197-5238`, policy `policies.sql:1213-1248`, Worker `object.ts:82-94` | archive rows carry `program_slug` (row-local), so the predicate adds `OR (so.root='archive' AND so.status='archived' AND so.program_slug = ANY(p_program_slugs) AND EXISTS published live spectrum with that spectrum_id)`; link accounts: observation match on the row; Worker `registeredAt` for archive rows = `archived_at` (copy completion) or the Worker never caches them |
| immutability | — | BEFORE UPDATE/DELETE trigger on `storage_objects` refusing changes to `root='archive'` rows except `status → 'collected'` by an admin; presign refuses `archive/` PUTs (P1); the copy uses the archive key only once (exists check) |
| delete paths | `remove.py:145-182`; the manual grating-delete procedure | `deploy remove` refuses an observation whose spectra are referenced by a published DR (P4) and never touches archive rows; today it also leaves 280 dangling rows: add a `--sweep-registry` that marks dangling live rows `revoked` |
| budget | `functions.sql:6399-6477` | classes `live`, `archive`, `collected`; `COALESCE(stored_size_bytes, size_bytes)`; the 20 TB cap moves to a `settings`-style row or stays hard-coded with a comment (OSN is expandable per the NIRCam overhaul D2) |
| gc *new* | `deploy_events` CHECK `tables.sql:1369`, `log_deploy_event` `functions.sql:6625-6659` | add actions `archive`, `collect`; `campfire archive gc --dry-run` lists mosaic archive rows older than the retention window and not referenced by any `dr_manifest`; the real `gc` lands with P4 (it needs `dr_manifest`) and marks rows `collected` with size and date, never deleting the row |
| verify / reconcile | `registry.py:472-522, 728-780` (orphans), `storage-registry.ts:461-508` | `archive/` keys with rows are registered, not adoptable; archive keys without rows are reported as "unregistered archive object" (a failed retain), never adopted as live |

**Schema.** `storage_objects`: `root` (P1), `status` CHECK gains `archived`, `collected`;
columns `program_slug text`, `archived_from_key text`, `archived_at timestamptz`,
`archived_by_deployment integer`, `collected_at`; partial index on `(spectrum_id) WHERE
root='archive'`; the immutability trigger; `deploy_events.action` CHECK + function list.
Backfill: `program_slug` on existing NIRSpec live rows from `spectra` (480k rows; a
batched script by observation, or one SQL statement per observation inside a migration
that is measured on the scale DB first).

**Storage.** New root `archive/` on OSN. Growth: a full NIRSpec redeploy retains ~92 GB
(57 GB FITS + 35 GB sidecars); never collected. Mosaics are retained only at freeze (P4).

**Tests.** Unit (in-process S3 fake, P-1 PR 7): plan → retain → sign; 409 on size or
ETag drift; archive-exists skip; idempotent re-run after a crash between copy and row
insert (the row insert is `ON CONFLICT DO NOTHING` on the archive key). TS↔Python
conformance test on one fixture. OSN integration (manual, `scratch/ci/<run>/`): the real
corpus (a 1 MB spectrum, one of the 7 files over 5 MiB, a lines FITS, a gzipped mosaic),
conditional copy semantics, the trigger refusing an overwrite. Matrix rows: archive rows
for anon / no-grant user / grantee / admin / link (in and out of scope). Concurrency (scale
DB + fake S3): two deploys of one observation interleaving presign batches; a deploy
racing `pull`. `verify --cloud` on the scale DB with archive keys present.

**Gates.** L/C as above. B: not needed (no UI). **S: shadow mode first.** The presign route
ships with `RETAIN_MODE=log`: it computes the retain set and logs `would archive <key>
<old hash> → <archive key>` without copying or inserting; run three or four real redeploys
and compare the logged set with the planner's `changed` list. Then `RETAIN_MODE=enforce`.
A: enforce for one admin's deploys for a week (env flag per deploying account is not
possible; use the CLI opt-in `--retain` during that week). G: default on; `--no-retain`
logged.

**Risk.** Redeploy latency (HEAD + copy ≈ 0.5 s per retained file, parallel 16 → a
3,000-file redeploy adds ~2 min); the route hitting 300 s on very large batches (presign
batches are ≤ 500 keys, so ≤ 16 s each at the measured rate). A failing copy fails the
deploy closed (409), which is the design's intent; the admin override exists. Notice: the
`supersede` events and the `archive` budget class; a nightly check that every active
NIRSpec row whose `content_hash` changed since the last check has an archive row for the
old hash (an agent query or a pg_cron job writing to `deploy_events`). Rollback:
`RETAIN_MODE=off`; archive rows and objects are harmless and stay.

**Dependencies.** P1 (roots, writable allowlist, `root` column), P0b (science hash on the
rows, used by `identify` later; not required for retention itself). **Size:** L (five
PRs: schema + trigger; TS helper + shadow mode; Python helper + conformance; authz + matrix
+ Worker; budget + gc dry-run + verify).

---

## 5. P3: needs-review redesign and bulk reaffirm

**Goal.** Two signals, both derived from hashes and membership, neither stuck: `data_changed`
(chip) and `needs_review` (badge); a bulk reaffirm. User-visible: the badge count changes
(1,339 today), the chip appears, and a "reaffirm" action exists.

**Amendment to the design (§11.6):** the design says "neither stamped". Computing the
predicate per row inside the seven list RPCs is the multi-hop join that timed out the band
filter (findings §1.2, recon H72). Proposal: `object_review_state` holds what the inspector
signed off; two **maintained** boolean columns `objects.data_changed` and
`objects.needs_review` are recomputed by one SQL function `recompute_review_flags(object_ids)`
called from the four places that can change the inputs (spectra science-hash change via
deploy reconcile, membership change via `apply_object_reconciliation`, sign-off/reaffirm,
line-fit invalidation). They are derived columns no human writes, so they cannot "get
stuck" the way `staleness_reason` does, and the list RPCs filter on an indexed boolean.

**Code surface.** New table `object_review_state(object_id int PK, reviewed_at,
reviewed_by uuid, reviewed_z numeric, reviewed_quality int, reviewed_members int[],
reviewed_hashes jsonb)`; function `recompute_review_flags`; `object_needs_review` rule set
(§5 of the design plus two refinements: for `inspected_used_auto = false` compare the new
auto to the previous auto, not to the override, so explicit overrides can clear, recon H71;
quality 1 objects never badge). Writers: inspect route `web/app/api/objects/[id]/inspect/route.ts:110-154`
(sign-off writes the state row; "mark as reviewed" becomes reaffirm), a new bulk reaffirm
route (admin or `can_inspect`, list-scoped), `pin_redshift_on_signoff`
(`triggers.sql:174-203`) unchanged, reconcile call sites `reconcile.py:1522-1543` and
`functions.sql:7362-7560` (call recompute for touched objects), `compute_object_redshift_auto`
`functions.sql:7649-7745` (stop writing `staleness_reason`; also add the `deploy_status =
'published'` filter the design's §1 complains about, as a separate decision D-P3-2). Readers:
the seven predicate copies (`functions.sql:2330, 2766, 2937, 3381, 3750, 4040, 4286`)
become `o.needs_review = p_needs_review`; `StalenessBadge.tsx:65-66` reads the booleans
and `data_changed` reason text; `store.py:580-584` and `web/lib/types.ts` mirror the
columns; sync payload carries them.

**Schema.** New table + RLS (`TO authenticated`; readable where the object is readable;
writable where `can_inspect()` and the object is readable, admin for bulk); two boolean
columns + partial index `WHERE needs_review`; `staleness_reason` kept read-only for one
release then dropped. **Backfill:** the rule set itself is reviewed first: P3 opens
with a one-page `docs/design-needs-review.md` stating each rule with the production
counts it would produce (computed read-only on the scale DB), which you sign off before
any code. Seeding then follows that review: `object_review_state` seeded for the 24,351
inspected objects from current science hashes and members; whether today's 1,339 badged
objects stay badged (`reviewed_hashes = '{}'`) or are cleared is a line in that document
(decision D-P3-1, open until the review). The 15,443
implicit pins (H37) are reported by `dr status` as a class, not re-pinned automatically
(decision D-P3-3).

**Tests.** SQL unit tests for `recompute_review_flags` on a fixture covering each rule;
matrix rows for the new table; `measure_rpc.py` on `get_filtered_objects_paginated` with
`p_needs_review` before and after on the scale DB; a sqlite/TS/SQL agreement test on one
fixture (closing the divergence in `store.py:580-584`).

**Gates.** L: scale DB timings within 1.1× of today; backfill rehearsed on the scale DB,
counts recorded. C. B: badge and chip on the preview. **S:** ship the columns and the
backfill with the old predicate still driving the UI; an agent compares `needs_review` with
the old predicate on production (expected: identical 1,339 plus the objects the new rules
add; list and eyeball the differences). A: switch the RPCs for admins first via
`p_needs_review_v2`? Not worth two code paths: switch everyone once the S comparison is
clean. G.

**Risk.** A wrong rule hides a real review need. Notice: the S comparison and the
`dr status` counts over time. Rollback: the old columns stay for a release; the RPC switch
is one migration to revert.

**Dependencies.** P0b/P0c (science hash populated). Independent of P1/P2. **Size:** M–L
(three PRs: table + function + backfill; RPC switch + web; bulk reaffirm).

---

## 6. P4: freeze core (split into four)

**Status (2026-10-14): merged and live** (#599–#602; see §13 "2026-10-14 P4 merged"). Open: the S gates (a production rehearsal release, staged archive, export, then `dr diff` and the files route checked read-only).

**Goal.** `campfire dr freeze --tag DR2026.1 --nirspec --field cosmos …` produces frozen
tables, per-partition exports and archive bytes, resumably; `publish` makes the DR
visible under RLS; `withdraw` hides it; `diff` reports changes since. User-visible: nothing
until `publish` (frozen rows are admin-only by policy, which makes the first freeze on
production a natural shadow run).

### P4a: frozen tables, freeze RPC, RLS, `dr status / freeze` (L)

**Schema** (`tables.sql`, policies with explicit `TO authenticated`, every function
`REVOKE FROM PUBLIC`):

| table | key columns | notes |
|---|---|---|
| `dr_releases` | `tag PK`, `state` (`draft, freezing, frozen, exporting, exported, copying, ready, published, withdrawn`), `components jsonb`, `frozen_at`, `frozen_by`, `snapshot_xmin`, `published_at`, `withdrawn_at`, `doi`, `notes`, `known_issues`, `dr_format_version int` | visible to every authenticated user when `state='published'`, admins always; `NOT is_link_account()` |
| `dr_spectra` | `(tag, spectrum_id) PK`, `program_slug`, `observation`, `grating`, `target_id`, `object_id int`, `file_hash`, `science_hash`, `cfpipe_version`, `crds_context`, `jwst_version`, `reduced_at`, `redshift_auto`, `dq_flags`, zfit scalars | row-local `program_slug`; policy `program_slug = ANY(accessible_program_slugs()) AND release published-or-admin AND NOT link` |
| `dr_objects` | `(tag, object_id int) PK`, `name`, `field`, `ra`, `dec`, `programs text[]`, `evidence_programs text[]`, `z_source_spectrum_id`, `redshift`, `redshift_quality`, `inspected_used_auto`, `last_inspected_at`, `last_inspected_by`, `version`, aggregates, `review_state jsonb` | policy `programs && accessible_program_slugs()` (scoped aggregates computed at read time by the existing helper shape) |
| `dr_manifest` | `(tag, storage_key) PK`, `component` (`nirspec`, `nircam/<field>`, `photometry/<field>/<catalog>`), `program_slug` or `field`, `product_type`, `spectrum_id`, `content_hash`, `science_hash`, `archive_key`, `byte_state` (`pending, copied, stamped, failed`), `stamped_hash` | ~5 rows per spectrum plus mosaics ≈ 400k rows per NIRSpec DR |
| `dr_line_fits` | `(tag, spectrum_id) PK` + the `spectrum_line_fits` columns + `stale_redshift`, `stale_spectrum` computed **inside** the transaction | |
| `dr_photometry` | `(tag, photometry_id) PK`, `field`, `catalog_name`, `object_id`, `photometry jsonb` | |
| `dr_exports` | `(tag, stream, partition) PK`, `storage_key`, `sha256`, `rows`, `state` | the resumable export ledger |

No foreign keys to live rows (design §4.3). `deploy_events.action` gains `freeze`,
`publish_dr`, `withdraw_dr`.

**Freeze RPC** `dr_freeze(p_tag, p_components)`: SECURITY DEFINER with the admin/service
guard, `SET default_transaction_isolation = 'repeatable read'`, `SET statement_timeout =
'600s'`; refuses if `dr_readiness` reports a live lease or the tag is `published`; deletes
any partial rows for the tag in state `freezing`; one `INSERT … SELECT` per table from
`spectra` (`deploy_status = 'published'`), `targets`→`objects`, `spectrum_line_fits` with
the staleness predicate evaluated in the same snapshot, `object_photometry`,
`storage_objects` joined **from** `spectra.spectrum_id` and `nircam_images.file_path`
(never through `get_storage_objects_for_sync`); records `pg_current_snapshot()` and moves
`state` to `frozen`. **Executor (decided D-P4-1): `pg_cron`, not a direct call.** The database only
grows, and an in-database job has no HTTP timeout in its path. Mechanism: the admin CLI
calls a small SECURITY DEFINER wrapper `dr_enqueue_freeze(p_tag, p_components)`
(admin-guarded, through PostgREST) that inserts the `dr_releases` row in state `freezing`
and runs `cron.schedule('dr-freeze-<tag>', '<next minute>', $$SELECT public.dr_freeze_job('<tag>')$$)`;
`dr_freeze_job` runs the freeze, writes `state` and any error text to the row, and ends
with `cron.unschedule('dr-freeze-<tag>')`. The CLI polls `dr_releases` and prints
progress. Two things this changes: the freeze function's guard must accept the cron
runner (`session_user = 'postgres'`, since `auth.uid()`/`auth.role()` are NULL inside a
cron job) in addition to admin/service-role; and on the native local stack `pg_cron` jobs
are known to fail, so local tests call `dr_freeze_job` directly and the cron hop is
exercised on a Docker stack (CI) and on production. `pg_cron` 1.6.4 is on production and
is a `CREATE EXTENSION` on a self-hosted VM. The scale-DB measurement still happens; it
sizes the `statement_timeout` and tells us whether the Small instance copes.

**Code surface.** `python/campfire/cli.py` (new `dr` group next to `sync`/`pull`,
`:180-245`), `python/campfire/deploy/supabase.py` (RPC call pattern `:316-350`), admin
route `web/app/api/v1/dr/freeze/route.ts` *new* (bearer admin pattern
`deploy/presign/route.ts:48-60`), `dr status` from P1, `log_deploy_event`.

**Tests.** The freeze-at-scale harness (stage 2 §3.5) on the scale DB: wall time, WAL
delta, temp usage, lock waits while a simulated redeploy and sync pages run; kill mid-insert
and re-run (must converge: same row counts and hashes); SQL unit tests on a fixture (draft
excluded, revoked excluded, staleness computed in-snapshot, `evidence_programs` correct for
a mixed object); matrix rows for every `dr_*` table with a `published` and an unpublished
tag for anon / no-grant / grantee / admin / link.

**Gates.** L: harness numbers in the PR; executor chosen from them. C. B: seed carries a
tiny published DR so the matrix and later pages have rows. **S: the first production
freeze is the shadow run**: `campfire dr freeze --tag DR2026.0-rehearsal` writes frozen rows
nobody but admins can see; an agent session compares row counts with live (`79,978`
spectra etc.), checks `pg_stat_wal` and the duration from `deploy_events`, and the row is
then deleted (`dr withdraw --purge`, allowed only before publish). A: admins see the DR in
`dr status`. G: not until P4d.

**Risk.** Database growth (~250 MB per DR, verify on the harness) and the freeze's load;
both measured before production. Lock conflicts with a concurrent deploy: the freeze reads
only (REPEATABLE READ takes no row locks) and the deploy's upserts are not blocked;
readiness refuses to start while a lease is held, and a deploy that starts mid-freeze is
reported by `dr diff` afterwards. Rollback: `withdraw --purge` before publish deletes the
tag's rows; tables are additive.

### P4b: exports (M)

Per (stream, program | field) JSONL.gz written to OSN under
`dr/<tag>/exports/<partition>/<stream>.jsonl.gz` in `dr_format_version = 1` (documented in
`docs/dr-format-v1.md`), built from the frozen tables by the scoped-partition queries
(the per-program objects export uses the `get_objects_for_sync(p_filter_program_slugs)`
shape, `functions.sql:1178-1454`, but reads `dr_*`). Keys under `dr/` are **not** layout
products (like `sync-snapshots/`): never registered, never presignable through the storage
routes; served only by `/api/v1/dr/<tag>/files`, which returns presigned urls for the
partitions the caller holds. **Amended 2026-10-13: the builder is `campfire dr export` on the admin's machine (`python/campfire/dr_export.py`), not a server route; the stream is `manifest`, the route `/files`; the order is freeze → archive → export (`campfire dr build`).** Original text: Builder: an admin route with `maxDuration = 800` that
processes partitions until 700 s then returns `incomplete`, driven in a loop by the CLI;
each partition is idempotent (`dr_exports` ledger). Exclusions: `latestReadySnapshot`
(`web/lib/server/sync-snapshot.ts:55-67`) and the prune/trim in the cron route never see
these keys because they are a different prefix and table.

Tests: partition content equals the scoped sync RPC output for a program on the fixture;
resume after killing the builder mid-stream; the route refuses non-admins; the partitions
route returns only held partitions (matrix-style test over the five principals).
Gates: L/C; S: build the rehearsal DR's exports on production, verify sizes and sha256 by
downloading one partition per stream; A: admins can list; G: with P5.

### P4c: byte pass and stamping (M)

`campfire dr archive --tag` (needs object-store credentials; runs on the admin machine):
for each `dr_manifest` row with `byte_state='pending'`: if an archive object with that
science hash already exists for a NIRSpec FITS (dedup across DRs), point at it; else for
`nirspec_spec` / `nirspec_lines` under 5 MiB: GET, patch the primary header
(`CMPFRDR = '<tag>'`, growing by one 2880-byte block if there is no room), PUT to
`archive/<sha_of_stamped>/<relpath>` with `If-None-Match: *` (verified supported), verify
the science hash is unchanged by re-reading the uploaded bytes' header (or trust the local
computation and spot-check 1 %); the 7 files over 5 MiB (max 8 MB) take the same
download-patch-upload path, so **no multipart upload is used anywhere in the byte pass**
and the data-manager key's lack of `AbortMultipartUpload` does not matter (D-OPS-1
becomes a nice-to-have); sidecar JSON and mosaics: conditional server-side copy,
unstamped (a mosaic over 5 GB would need `UploadPartCopy`; none exists today, and a
crashed multipart copy would merely leave un-abortable garbage, not corrupt anything). Insert the archive
registry rows (`status='archived'`, `root='archive'`, `program_slug`), update
`byte_state` and `stamped_hash`. Resumable per row; a crash between PUT and row insert is
healed by the exists-check. Rate: the measured 0.25 s per copy and the laptop's network
for ~57 GB of download-patch-upload; from a TACC node far faster. **Stamp semantics
(D-P4-2, decided: header stamp, first DR wins):** an archive object is created once per
science hash, when first frozen; a file unchanged between DR2026.1 and DR2026.2 keeps
`CMPFRDR = 'DR2026.1'` and the catalog (`dr_manifest`) records both memberships, which is
what `identify`, the provenance line and the DR README report ("in DR2026.1 and
DR2026.2; bytes stamped DR2026.1"). A new copy is made only when the science hash
changes. This keeps design decision 5 (dedup by hash, copy only new hashes) intact.

Tests: the OSN corpus (manual) with astropy reopen, HDU data byte-identical, science hash
unchanged, whole-file hash changed, header block growth case, the >5 MiB case; unit tests
of the header patcher on synthetic FITS; resume after kill. Gates: L/C; S: byte pass on
the rehearsal DR for one observation (`--obs`), then full; A/G with publish.

### P4d: publish, withdraw, diff, admin routes (S–M)

`dr publish --doi` flips `state` (policies already gate on it); `dr withdraw` flips back
(bytes kept); `dr diff <tag> live` lists objects whose bytes (science hash), z, Q or name
differ, plus review contradictions since the freeze; `deploy remove` and the grating-delete
procedure consult `dr_manifest` and refuse (P2 hook). Tests: state machine; matrix after
publish/withdraw; `diff` on a fixture. Gates: S: `diff` against the rehearsal DR; A:
publish the first real DR visible to admins only (D-P4-4: the first real DR follows the
jwst 2.0.1 re-reduction campaign; until then the rehearsal DR, admin-only, is what P5 and
P6 are built and tested against, so none of the client or web work waits on the campaign) (a `preview_until` or a second flag is
not worth it: publish to admins by leaving `state='ready'` and letting admins read
`ready` rows, then `published` for everyone). G: `published`.

**Dependencies.** P1 (roots, lease, readiness), P2 (archive rows, authz, immutability),
P0 (science hash), P3 (review state to freeze; a DR before P3 freezes `staleness_reason`
instead, acceptable). **Size:** XL (P4a L, P4b M, P4c M, P4d S–M; five to six PRs).

---

## 7. P5: clients

**Goal.** `campfire pull --dr`, `Campfire(dr=)`, `cite`, `identify`, `provenance`, the sync
tail notice. User-visible: new CLI verbs; a client release (0.7.0) with the version floor
unchanged.

**Code surface.** DR store: a second `LocalStore` instance over
`dr/<tag>/meta/campfire.db` (same DDL, `store.py:125-336`; its own `SCHEMA_VERSION`
tag), loaded from the kept partitions (`snapshot.py:58-106` download/verify/iterate
reused); the live purge (`:769-2132`) and tombstones (`:1408-1497`) never run on it;
schema bump rebuilds it from the partitions instead of deleting (`client.py:200-208`
behaviour is live-only). `Campfire(dr="DR2026.1")`: `client.py:74-141` opens the DR store,
`_api_session` becomes lazy (`:79`; `session.py:66-82` raise moved to first remote use),
every remote branch (`:343-388, 434-440, 513-570, 721-727, 764-777`) raises
`DRModeError` instead of falling back; `table.meta['channel'] = 'DR2026.1'`;
`query_lines(exclude_stale=True)` default in DR mode (`:606-616`). `pull --dr` (`cli.py:725`)
fetches the export files from `/api/v1/dr/<tag>/files`, loads, then downloads bytes via
`dr_manifest` → the storage download route (archive keys are readable keys after P1/P2);
`--stale` moves the old local file to `.superseded/<hash>/` (`sync.py:686`). `identify`:
local sqlite lookup by whole-file and science hash across live and DR stores, falling back
to a new `/api/v1/identify` (bearer) that queries `spectra`, `storage_objects` (live and
archive) and `dr_manifest`. `cite`: from `dr_releases.doi` and the viewer's programs/fields
intersected with a selection. `provenance` / `provenance check`: `paper.campfire.json` from the
local ledger (`store.py:1613-1816`, hashes verified with `deep=True` for the files listed,
not adopted). Version/announce: `latest_dr` added to the **authenticated**
`/api/v1/auth/whoami` (`web/app/api/v1/auth/whoami/route.ts`) together with an access-set
fingerprint (sha256 of sorted accessible slugs + fields), not to the public, 1-hour-cached
`/api/v1/version` (`route.ts:27-37`); `sync` prints "DR2026.1 published; you are on live"
and "your program access changed; run `campfire sync --full`" (`cli.py:707-712`).

**Download design constraints (decided 2026-10-13).** A reader gets **one table per stream
holding every program they can access**, e.g. `campfire-DR2026.1-objects.fits`, not one file
per program. Storage stays per (stream, program) because each reader's program set
differs; the merge happens on the client: union the reader's files of a stream, one row per
id (an object in several of the reader's programs appears in each program's file;
deduplicate by `id`, union `programs` / `evidence_programs`, and recompute the scoped
aggregates over the union, or document that they are per-program). Output formats: **CSV
and FITS** (JSONL stays the stored format; nested fields — photometry bands, line fits —
become flattened columns or extra HDUs). The same download must be possible **from the web
portal**, since many users will fetch a release there: either a browser-side merge (fetch the
presigned files, merge in a worker, write CSV / FITS) or a streaming server route that
merges within its time and memory budget; decide in P5, keeping the per-program files and
`/api/v1/dr/<tag>/files` as the shared source. The paper-file concept is the **provenance
file** (renamed from "manifest", which names a release's file list since P4).

**Schema / routes.** `/api/v1/dr` (list, detail), `/api/v1/dr/<tag>/files` (P4b),
`/api/v1/identify`; `whoami` fields. All bearer, all through `authenticateApiRequest`
(link accounts refused, `api-auth.ts:63`).

**Tests.** Store tests with fixture partitions (load, rebuild on schema bump, no purge);
fail-closed tests for every remote branch in DR mode; lazy session test (construct with
no credentials, local query works, remote raises); CLI tests with the shared fake API;
`identify` golden (a file, a stamped copy, an unknown file); `provenance check` against a
fixture DR. Route tests (vitest) for the three new routes with the five principals.

**Gates.** L/C. S: against the production rehearsal DR with the test account
(`hollis.akins+cftest`, non-admin, one private grant): `pull --dr` of one program's
partitions, byte download of ten files, `identify` on them. A: client 0.7.0 released;
`minimum` unchanged. G: docs updated.

**Risk.** Disk: a full DR pull is ~92 GB plus the sqlite; `pull --dr --obs` scopes it.
A client bug cannot damage the server (reads only). Rollback: pin the previous client.

**Dependencies.** P4a–c. **Size:** L (four PRs: store + DR mode; pull + identify; cite +
provenance; whoami + sync tail).

---

## 8. P6: web

**Goal.** `/releases`, `/releases/<tag>`, the per-spectrum provenance line, the cite
block, DR downloads, the frozen object view, ~~aliases~~ (dropped, D-P6-6). User-visible: yes, this is the
public face of the DR.

**Code surface.** Pages under `web/app/releases/` *new* (RSC reading `dr_releases` and the
per-program counts under RLS through the cookie client; `DATA_RELEASE` in
`web/lib/updates/versions.ts:60-62` replaced by a DB read). Provenance line: GET
`/api/objects/dr-provenance?id=` (decision D-C pattern; template
`web/app/api/objects/lines/route.ts:57-70` with `private, max-age=60` + `Vary: Cookie`)
reading `dr_spectra` by `spectrum_id` for all published tags; rendered in
`web/components/spectra/SpectrumDetailCard.tsx` beside the download button
(`handleDownload` at `:89`); TanStack key `['dr-provenance', spectrumId]`. DR downloads:
`/api/dr/download?tag=&key=` authorizes through `dr_manifest` + the archive authz function,
then `frontUrlFor` (`web/lib/server/cdn-front.ts:110-112`) with `registeredAt =
archived_at`; the sidecar resolver (`spectrum-sidecars.ts:25-82`) is **not** used for DR
copies (siblings come from `dr_manifest`). Frozen object view
`/releases/<tag>/objects/<name>` from `dr_objects` + `dr_spectra` (no live row needed;
`loadObjectRow` `objects.ts:62-82` unchanged). Aliases: `object_aliases(name, object_id,
valid_from, valid_to, reason)` written by `apply_object_reconciliation`
(`functions.sql:7418, 7444, 7472`) and the Python split/merge paths
(`reconcile.py:1361-1472`) from now on; the object page's miss path tries aliases, then
the latest DR's frozen name, before `notFound()` (`page.tsx:135-137`). Exports: CSV from
the frozen tables (`download.ts:110` pattern) with a provenance header row.

**Schema.** `object_aliases` + trigger hooks; nothing else.

**Tests.** vitest route tests for the two routes with the principals; a component test for
the provenance line states (in DR unchanged / re-reduced since / not in any DR); matrix
rows for `object_aliases`; bundle budget; the no-live-row object view on the preview seed.

**Gates.** L/C. **B: the preview branch is the main gate here** (seeded DR rows). A: pages
behind `NEXT_PUBLIC_DR_PAGES=admin` (admins only) for a week with the rehearsal DR; G: flag
to `all` when the first real DR is published.

**Public summary page (D-P6-1: yes).** Because P-1 removes anon's table grants, a
signed-out `/releases/<tag>` cannot read through RLS as `anon`; it is a server component
reading `dr_releases` and the public-program counts with the service-role client, the
pattern `lib/updates/visibility.ts:20-31` and the OG-image route already use, showing
only the tag, notes, DOI and public-program counts (never the private roster). It carries
the site-wide `noindex` like everything else; the DOI resolves to Zenodo regardless.

**Risk.** The provenance route adds one query per expanded spectrum card; it is keyed and
cached. Rollback: flag off.

**Dependencies.** P4d, P5 (for the client snippet on the page). **Size:** L.

---

### P6b: frozen tables for a browsing mode (amended 2026-10-15, D-P6-2) — schema, before any real freeze

**Why now.** `dr_releases` is empty in production, so the frozen tables can be dropped and
recreated by one migration. After the first real freeze every reshaping is a format problem.

**Inventory facts that shape the design** (full inventory 2026-10-15): there is no
`spectra.object_id`; every live join is `spectra.target_id → targets.target_id → targets.object_id
→ objects.id`, so `targets` must be frozen. The current `dr_objects` *recomputes* `programs`,
aggregates, `photo_z*`, `redshift_auto` and `redshift` over published members in the snapshot and
names columns differently (`name` = `objects.object_id`, `object_id` = `objects.id`); the live
RPCs instead scope aggregates per viewer at read time (`object_scoped_aggregates`). The line and
band filters read the derived child tables `spectrum_lines` and `object_photometry_bands`
(generated `snr`/`mag`), the dropdowns read `mv_filter_options`, the NIRCam field page reads
`fields` + `nircam_images` + the field's `storage_objects` (expmaps, renders, layout). Sizes in
production: spectra 280 MB (141 MB of inline SVG thumbnails), bands 121 MB, photometry 73 MB,
line fits 63 MB, objects 61 MB, lines 54 MB, targets 43 MB: ~0.7 GB of catalog tables per
release, beside the ~0.8 GB manifest.

**Rule.** Every frozen catalog table is `tag text NOT NULL` + **the live table's columns, same
names and types** (generated columns become plain columns holding the generated value;
`auth.users` references become bare uuids; no foreign keys, as today) + frozen-only extras. The
freeze copies **by name** (`INSERT INTO dr_x (tag, c1, c2, …) SELECT p_tag, x.c1, x.c2, … FROM x …`),
not `x.*`: the migration chain's physical column order differs from the schema files' CREATE
blocks (found 2026-10-15), and a positional copy of two same-typed columns swapped would be
silent. The guard `check_dr_parity.sql` asserts, for every pair, that the frozen column set and
types equal the live ones and that `dr_freeze_job`'s INSERT list names every live column, so a
live column added later fails CI until it is added to the copy and the freeze. Values are copied
**verbatim** ("the catalog as it stood"); the in-snapshot z-source rule still runs, but only to
fill the export's `z_source_spectrum_id` and to count disagreements with the live
`redshift_auto` into `counts.redshift_auto_mismatch` (informational, never a failure).

| frozen table | live source | rows frozen | PK | extras |
|---|---|---|---|---|
| `dr_spectra` | `spectra` | nirspec component: `deploy_status = 'published'` | (tag, id); unique (tag, spectrum_id) | — (field / object via `dr_targets`) |
| `dr_targets` | `targets` | targets with ≥ 1 frozen spectrum | (tag, id); unique (tag, target_id) | — |
| `dr_objects` | `objects` | active objects with ≥ 1 frozen spectrum | (tag, id); unique (tag, object_id) | `evidence_programs text[]`, `z_source_spectrum_id int` |
| `dr_line_fits` | `spectrum_line_fits` | fits of frozen spectra | (tag, spectrum_id) | `stale_redshift`, `stale_spectrum` (in-snapshot, as today) |
| `dr_spectrum_lines` | `spectrum_lines` | lines of frozen fits | (tag, live PK) | — |
| `dr_photometry` | `object_photometry` | rows of frozen objects in the selected catalogs | (tag, id) | — (partition via the object's `programs`) |
| `dr_photometry_bands` | `object_photometry_bands` | bands of frozen photometry rows | (tag, live PK) | — |
| `dr_nircam_images` | `nircam_images` | published mosaics of the component fields | (tag, id) | — |
| `dr_fields` | `fields` | component fields + fields of frozen targets | (tag, name) | — |
| `dr_filter_options` | `mv_filter_options` (same query over the frozen rows) | one row | (tag) | — |
| `dr_manifest` | unchanged | | | |

**RLS.** Each frozen table's SELECT policy is the live table's predicate **verbatim** (spectra:
`program_slug` / `observation` / `deploy_status`; targets; objects: `programs &&` +
`has_published_spectrum` + `observations &&`; photometry / bands / line fits / lines: EXISTS on the
frozen parent of the same tag; nircam_images and fields: `deploy_status` and link field) plus
`NOT is_link_account()` and `tag = ANY(dr_visible_tags())`. `dr_objects` therefore moves from the
P4a "all programs held" (`<@`) rule to the live overlap rule: in browsing mode the RPCs scope
aggregates per viewer exactly as live, and the exports keep partitioning per program (the
`/files` route and `dr_export_rows` are unchanged in what they hand out). The RLS matrix in
`check_dr_freeze.sql` is rewritten for the new tables (5 principals × every table, before and
after publish).

**Indexes.** Each frozen table mirrors the live indexes the catalog RPCs use, tag-prefixed:
objects (tag, redshift), (tag, field), GIN programs / gratings / observations, trgm search_text,
(tag) WHERE needs_review; spectra (tag, target_id), (tag, program_slug, observation), trgm
search_text; targets (tag, object_id), (tag, target_id), (tag, program_slug, observation);
lines (tag, line, snr); bands (tag, band, mag) / (tag, band, snr); photometry (tag, object_id);
line fits (tag, target_id); nircam_images (tag, field); fields (tag, name).

**Readers updated in the same PR** (export JSON stays byte-identical to format v1 by aliasing):
`dr_export_partitions` / `dr_export_rows` / `dr_export_begin` (objects: `id` → `object_id` text
name mapping, `needs_review` + `reviewed_spectrum_ids` read from the verbatim columns, aggregates per
partition from `dr_spectra ⋈ dr_targets`; spectra: `field`/`object_id` via `dr_targets`;
photometry `photometry_id` = `id`), `dr_diff` (`dr_objects.id`, `object_id`, `needs_review`),
`dr_freeze_job` counts (`nirspec_fields` via targets), the four `check_dr_*.sql` guards, the seed
block (unchanged call), `docs/dr-format-v1.md` (no change expected; verified). Python and web read
the frozen tables through RPCs only, so they do not change.

**Gates.** L: guards + parity guard + drift gate on the native stack; the freeze of the seed
(DR-seed) succeeds and `dr_export_rows` for DR-seed is identical before and after (captured
fixture). C: two independent review agents (SQL/RLS adversarial; harness + equivalence) before
the PR is marked ready. B: preview branch seeds. No production step: the tables are empty.

### P6b-2: source views, the request hook, the RPC switch (schema + SQL)

- Views `cat_objects`, `cat_targets`, `cat_spectra`, `cat_line_fits`, `cat_spectrum_lines`,
  `cat_photometry`, `cat_photometry_bands`, `cat_nircam_images`, `cat_fields`
  (`security_invoker = true`): the live table when `current_setting('campfire.dr_tag', true)` is
  null/empty, else the frozen rows of that tag (UNION ALL with a one-time filter per branch; the
  unused branch is never scanned). Filter options: `cat_filter_options` over
  `mv_filter_options` / `dr_filter_options`.
- `dr_request_context()`: the PostgREST pre-request function; reads
  `request.headers ->> 'x-campfire-dr'`, validates the tag (format, exists, visible to the caller
  via `dr_visible_tags()`; otherwise `RAISE` with sqlstate `PT404`) and `set_config('campfire.dr_tag',
  tag, true)` (transaction-local, so pooled connections never leak it). Registered by a
  hand-authored migration: `ALTER ROLE authenticator SET pgrst.db_pre_request =
  'public.dr_request_context'; NOTIFY pgrst, 'reload config';` (plain PostgREST; verified on the
  native stack 2026-10-15; documented by Supabase; survives self-hosting). Only the Data API runs
  it (not Storage/Realtime), which is all we need.
- The read-only catalog RPCs (inventory group (a): filtered objects/spectra/ids, adjacent, near,
  markers, CSV exports, scoped aggregates, line/band/observation/grating filters, line SNR,
  nircam fields/summary, observation manifest, overview readers) switch their `FROM` clauses to
  the `cat_*` views. Writers, sync streams, deploy and auth helpers keep reading the live tables
  (they ignore the setting). A guard asserts no function that writes references a `cat_*` view.
- **Equivalence harness**: DR-seed is the seed catalog frozen at the end of seeding, so every
  group-(a) RPC must return the same rows live and under
  `set_config('campfire.dr_tag','DR-seed', true)`; `check_dr_browse.sql` runs each with a fixed
  argument set as admin, public user and grantee and compares the JSON. Also: an unknown or
  unpublished tag from a reader is refused by the hook; an admin may browse an unpublished tag.

### P6b-3: field products in the manifest, the release page document

- Manifest: for each component field, the active `storage_objects` of product types
  `nircam_expmap`, `nircam_expmap_plot`, `nircam_layout`, `nircam_mosaic_thumbnail`,
  `nircam_mosaic_quicklook` join the mosaics (component `nircam/<field>`; archived by the byte
  pass with the mosaic path, server-side copy).
- D-P6-3: `dr_releases.page_md text`; assets under `dr/<tag>/page/<name>` as manifest rows
  (component `page`, product type `dr_page_asset`, program_slug NULL); `dr_page_set(tag, md,
  assets jsonb)` admin-only, refused once published; `campfire dr page push <tag> <dir>` uploads
  (OSN credentials as `dr archive`) then calls it. Figures come from scripts kept under
  `scripts/dr/`, run by hand.

### P6c: the web browsing mode

Split (2026-10-15): **P6c-1** = the mode itself — `dr` context from `?dr=` (URL param carried on
every catalog link; a `useDrTag()` hook and a `withDr(href)` helper), the server clients send
`X-Campfire-DR` (cookie client via `global.headers` per request; GET route handlers take `dr`
and forward it; `fetchJson` appends it), the per-request mode confirmation (`rpc/dr_browse_tag`
must echo the tag, else 500), the banner, mutation UI hidden, the catalog / spectra / object /
NIRCam field / metadata pages reading through the switched RPCs, `/releases/<tag>` cards linking
in, `cat_filter_options` for the dropdowns, the `NEXT_PUBLIC_DR_PAGES` gate on the entry points;
file access (sidecars, downloads, images) is not wired to the manifest yet, so P6c-1 ships the
pages with those controls disabled under a release (no explanatory copy), until P6c-2. **P6c-2** = files through `dr_manifest`: the sidecar resolver, the
download routes and the NIRCam field images resolve the release's archived copies (`archive_key`,
`frontUrlFor` with `archived_at`), the `/api/v1` bearer routes gate `dr`, the page document
(`page_md` + assets) rendered on `/releases/<tag>`, object-name aliases, the frozen field
summary (`area`, filters) and `layout_key` from the manifest.

`?dr=<tag>` → a DR context (URL param carried on every catalog link, like the filters); the
server clients send `X-Campfire-DR` on every PostgREST call for that request; the GET route
handlers take `dr` and forward it; a banner "Browsing <tag> · switch to live"; mutation UI hidden
(inspection, flags, lists, comments posting); the sidecar resolver, downloads and the NIRCam field
images resolve through `dr_manifest` to the archived copies; cutouts live (D-P6-4); the FitsGL
map live-only with no DR entry point; object names through the aliases table; `/releases/<tag>`
cards link into the mode. The `NEXT_PUBLIC_DR_PAGES` flag gates the entry points until the first
real release. **Rules from the P6b-2 review (2026-10-15):** (1) a DR request must never fall back
to live silently: the server confirms the mode once per request (first call `rpc/dr_browse_tag`
with the header; a mismatch is a 500, never a live render), and CI curls the hook end to end;
(2) the `/api/v1` bearer routes run as the service role, for which the hook accepts any
post-freeze tag, so a route must gate a caller-supplied `dr` against the bearer's own visibility
(published tags, every state for admins) before forwarding the header; (3) the `?dr=` value is
passed through verbatim (tags are case-sensitive); (4) sync streams refuse to run under a DR tag
(they are live-only by contract).

---

## 9. P7: public deposit (M) and P8: bundles (later)

**P7.** Flat exports for the public-program join (CSV exists; Parquet needs `pyarrow` in
the deploy extra, decision D-P7-1; FITS tables via astropy), the Zenodo deposit script
(`scripts/zenodo_deposit.py`, DOI written to `dr_releases.doi`), `programs.public_since`
set by the `is_public` guard path from P0a, the NIRCam per-field deposit rule as a
`dr_releases.components` attribute decided at deposit time. Gates: S: a Zenodo sandbox
deposit. Depends on P4d, P6.

**P8.** Reuses the DR format with `kind='bundle'`; the catalog-rows question collides with
`nircam_images_unique` (`tables.sql:2075-2076`) and `spectra.fits_path` UNIQUE; recommend
download-only (decision D-P8-1). Not planned further here.

---

## 9a. P4e: the archive is exactly the releases (amendment, 2026-10-27)

**Why this amendment.** The design made the archive "every final product that was ever
published" so that a file downloaded from live between releases could be identified and
re-pulled later (§4.2, §4.6, §4.8 of the design, decision 9). Reviewed on 2026-10-27 with
the rehearsal still unrun: the between-releases citation story is not wanted ("cite a
release" is the policy), and strict retain-on-supersede has sat in `log` mode since P2
because its growth (every redeploy of an observation adds its old finals, forever) was
never the point. Two operator needs replace it: a rehearsal must be fully reversible,
including its archive bytes and the `CMPFRDR` stamps, and a frozen release's bytes must
survive the window between `dr freeze` and the end of `dr archive`. Object-name aliases
(P6) are dropped in the same review.

**Goal.** An archive object exists if and only if a release's manifest references it.
Retain-on-supersede narrows to that invariant and is always on. `dr purge` of an
unpublished release removes the archive objects only that release references. Unreferenced
archive objects are collectable by `campfire archive gc`, with no time window. A published
release's bytes are forever, because a published release is never purged.

**Decisions.** D-P2-4, D-P4-6, D-P4-7, D-P6-6 in §12; D-P2-2 and D-P4-3 amended there.

### Mechanism

**The retain gate (both implementations, D-P2-3 conformance kept).** A live row needs a
retained copy before its key is overwritten when its `content_hash` is named by a
`dr_manifest` row whose bytes are not yet in the archive:

```sql
-- retain_required_hashes(p_hashes text[]) RETURNS text[]   (admin or service_role)
SELECT array_agg(DISTINCT m.content_hash)
FROM dr_manifest m
WHERE m.content_hash = ANY (p_hashes)
  AND m.byte_state NOT IN ('copied', 'stamped');
```

`plan_retain` / `planRetain` gain one decision, `skip-unreferenced`, for a retained-type
row whose hash is not in that set; `retain` and `drift` keep their meaning for the rows
that are. A manifest row of a published release is always `copied` / `stamped` (publish
requires `ready`), so in steady state the gate fires only between a freeze and the end of
its archive pass, and `dr build` keeps that window short. The modes go: `RETAIN_MODE` and
`CAMPFIRE_RETAIN_MODE` are retired, `off` and `log` with them; the presign route and
`retain_before_direct_upload` always copy-then-sign and fail closed (409 `retain_failed`
unchanged). There is no override: a member that cannot be retained is a release problem,
fixed by finishing `dr archive` or purging the release that names it, never by
overwriting the bytes.

**Purging a release's archive objects** (*revised after the local Codex pass on PR 2, 2026-10-27:
collection is a three-phase claim, see below*). "Referenced by tag T" for an archive row `r`:

```sql
EXISTS (SELECT 1 FROM dr_manifest m WHERE m.tag = T
        AND (m.archive_key = r.storage_key                                  -- the byte pass's copy
             OR (r.archived_from_key = m.storage_key AND r.content_hash = m.content_hash)))  -- a retained copy
```

`dr_purge_archive_plan(p_tag)` (admin; refuses `published` / `withdrawn`; the same waits as
`dr_purge_release` on a live archive or export claim) returns the archived rows referenced
by `p_tag` and by no other tag. `dr_collect_archive(p_keys text[], p_tag text)` flips them
`archived → collected` (the P2 trigger already allows exactly this transition and stamps
`collected_at`) and logs one `collect` deploy event (`deploy_events.action` already lists
it). `campfire dr purge <tag>` becomes: plan → `DeleteObjects` in batches of 1000 with the
OSN credentials it already needs for `dr/<tag>/exports/` → collect → `dr_purge_release`.
Order matters and heals by re-run: a crash after the deletes leaves `archived` rows with no
object, which `verify --cloud` reports as dangling and the next `dr purge` of the same tag
resolves (the plan is recomputed from the manifest rows, which go last; `DeleteObjects` on a
missing key succeeds). `--keep-archive` skips the sweep; `--local` never touches bytes
(same rule as the exports, Codex on #600).

**The claim (PR 2 as built).** Nothing can hold a lock across the bucket delete, and a plan the
operator confirms minutes later can be stale: a release published meanwhile, a freeze that came to
reference one of the planned objects, or the deploy gate finding the object "already there" and
skipping its copy just before the delete. So collection is `dr_archive_collect_begin(keys, tag)` →
the bucket delete → `dr_archive_collect_finish(keys, tag)`. Phase 1 takes the freeze advisory
lock (no freeze can add a reference) and, with a tag, the release row lock (`dr_publish` takes the
same), re-checks the references and the state, and flips the rows `archived → collecting` (a new
status; the P2 trigger allows it). A `collecting` row is offered to nobody: `dr_bytes_pending`'s
source and stamp joins require `archived`, `dr_bytes_mark` refuses to record a member on it, and the
gate's registration moved from a blind upsert to `archive_register_retained(row)`, which inserts
or accepts an *archived* twin and refuses a `collecting` one, so the gate fails closed (409) and the
deploy retries after the collection. Phase 3 deletes the collecting rows (the only delete the
trigger allows, under the transaction-local `campfire.archive_collect`), so the content-addressed
key is free for a fresh row when the same bytes are archived again; this amends decision 9's "rows
never dropped" for purged and unreferenced objects, which no release could cite. Every phase counts
what is already done instead of refusing it, and the plan and `archive_collectable` list
`collecting` rows, so an interrupted run resumes. `dr_purge_archive_plan` is keyset-paged
(PostgREST caps a table-returning RPC at 1000 rows) and the client walks every page before the
confirmation.

*Second local Codex pass, three more interleavings closed:* (1) a release could be published
between the claim and the delete, since the row lock ends with phase 1: phase 1 now leaves the tag
in a new state `purging`, which `dr_publish`, `dr_archive_claim`, `dr_export_begin` and
`dr_enqueue_freeze` refuse and only `dr_purge_release` ends; (2) two collectors could both "resume"
the same key: the claim carries the run's token (`storage_objects.collect_claim`), a claim refreshed
in the last 10 minutes refuses any other run, an older one is a dead run's and is taken over (the
archive claim's rule), and the bucket delete is gated by `dr_archive_collect_verify(keys, claim)`,
which returns and refreshes only the keys the token still owns, with `_finish` acting on those alone,
so a stalled run that resumes after a takeover deletes nothing (third Codex pass); (3) the gate or the byte pass could find an
object under a key while a collection was deleting it and register after the collection finished:
the gate now registers *before* it copies (the row is referenced by the member, so no collection can
claim it; a `collecting` row refuses it; a crash between registration and copy is healed by the next
run), and for the byte pass phase 3 writes a 7-day tombstone (`archive_collections`),
`dr_bytes_pending` exposes it as `target_collected_at` so a copy target deleted after the object the
pass found was written is copied again rather than healed, hands out `page_at` (its own clock) with
every row, and `dr_bytes_mark` fails (not raises) a member whose key a collection deleted after that
page time, heal or fresh copy alike, or that lands on a `collecting` row (fourth Codex pass: a fresh
copy can be deleted by a resumed collector before its mark, so the fence covers every outcome). Residual, documented: clock skew between OSN and Postgres larger than the gap between a collection
and the next copy of the same bytes (the tombstone comparison), and the window between a `verify` and its
`DeleteObjects`, which the collector keeps under a minute (it re-verifies after a longer stall) while a
takeover needs a claim ten minutes stale.

**Stamps.** The reuse join in `dr_bytes_pending` (D-P4-2, "the first release to archive a
file owns its stamp") gains `JOIN dr_releases dr ON dr.tag = d.tag AND dr.state IN
('published', 'withdrawn')`: only a published release's stamp is ever reused, so an
unpublished tag can never reach a published file's `CMPFRDR`, whatever the purge order.
The rule reads "the first *published* release to archive a file owns its stamp".
`check_dr_bytes.sql` sets its reuse fixture's other tag to `published`.

**Collection without a window.** `archive_collectable(p_after_id bigint, p_limit int)`
(admin) pages the `archived` rows referenced by no manifest row of any tag, every product
type (NIRSpec included: an unreferenced NIRSpec copy can only be a purged release's
leftover or a retained copy of a member that was later purged). `campfire archive gc`
drops `--older-than` and the mosaic-only rule, keeps `--dry-run` as the default, and
collects through the same delete-then-collect helper `dr purge` uses
(`python/campfire/archive.py` owns it). D-P4-3's "2 DRs or 12 months" becomes "while a
release references it": a mosaic in DR1 and not in DR2 stays because DR1's manifest names
it, and a withdrawn release keeps its manifest rows, so its bytes stay too.

### Code surface and downstream trace

| place | today | change |
|---|---|---|
| `supabase/schemas/functions.sql` | `dr_bytes_pending` reuse join; `dr_purge_release` | `retain_required_hashes`, `dr_purge_archive_plan`, `dr_collect_archive`, `archive_collectable` (all `REVOKE FROM PUBLIC`, admin / service_role gate, `SET search_path`); reuse join restricted to published tags; `dr_purge_release` comment ("archive bytes stay") rewritten; one generated migration |
| `supabase/schemas/triggers.sql` `protect_archive_rows` | allows `archived → collected` only | unchanged; it is the mechanism |
| `supabase/schemas/tables.sql` | `collected` status, `collected_at`, `collect` event | unchanged |
| `web/lib/server/retain.ts`, `web/app/api/v1/deploy/presign/route.ts` | `retainMode()`, `log` branch, `enforce` branch | `retainMode` deleted; `planRetain` calls `retain_required_hashes` once per batch with the candidate rows' hashes and emits `skip-unreferenced`; the route always executes retains and 409s on failure; `route.test.ts` and `retain.test.ts` rewritten for the three outcomes (unreferenced → signed, referenced → copied then signed, referenced + drift → 409) |
| `python/campfire/deploy/retain.py`, `r2.py:671` | `retain_mode()`, `mode=` argument | same narrowing through the RPC; `retain_mode` and `mode=` deleted; `test_deploy_retain.py` and the TS↔Python conformance fixture gain the unreferenced case |
| `python/campfire/dr.py`, `python/campfire/cli.py` `dr purge` | rows + export files | the archive sweep (plan → delete → collect) before the rows; `--keep-archive`; docstring and the "archive bytes stay" wording |
| `python/campfire/archive.py`, `cli.py` `archive gc` | mosaic window dry run, `--older-than`, `COLLECTABLE_PRODUCT_TYPES` | reference-based candidates through `archive_collectable`; real collection; `test_archive_gc.py` |
| `python/campfire/deploy/registry.py` `cloud_reconcile_report` | dangling = registry row with no object | **confirm in the PR** that `collected` rows are excluded from the danglable set (a collected row has no object by definition) |
| `check_dr_bytes.sql` | reuse fixture from any tag | other tag `published`; a new case: an unpublished tag's stamp is not reused |
| `check_archive_collect.sql` *new* | — | the reference predicate: a row referenced by T only is planned, a row shared with another tag is not, a retained copy of a T member is, a withdrawn tag's rows are never planned, `archive_collectable` never returns a referenced row, and `dr_collect_archive` refuses a referenced key |
| `check_archive_immutability.sql`, `check_storage_budget_classes.sql` | — | unchanged (the `collected` class already reports count + logical size, never bytes) |
| `get_storage_objects_for_sync` | `status = 'active'` | unchanged: archive rows never reach the clients' mirror |
| `ops/.env.example`, the VM's `web.env`, the deploy machine's shell | `RETAIN_MODE=log` | line removed; a stale value is ignored (the variable is no longer read) |
| `docs/design-data-releases.md` §0, §4.2, §4.6, §4.8, §7 (9), §8 (3) | archive = everything ever published; never collected | amended in place with a dated note: archive = the releases; `identify` of a superseded live file not in any release answers "superseded on <date>, bytes not kept"; a provenance file (§4.8) resolves only while its files are current or in a release |
| `python/README.md`, `AGENTS.md` | retain modes | the one-line description of the gate |

Not affected, checked: the layout root and key bijection (`archive/<sha>/<relpath>`);
the archive authorization predicate and the Worker (rows stay, status aside); the
`gc_pending` budget class (superseded / revoked live rows, a different thing); the P5
client design (reads only; `identify` already had the "collected" answer); P7 (the deposit
points at a published release, whose bytes are forever); the freeze (`dr_manifest` rows are
written the same way); the export files (deleted by the same `dr purge` as today).

### Tests, gates, risk

**Tests.** The SQL guard above; vitest for the route; pytest for both Python modules and
the conformance fixture; `test_deploy_freeze_contract.py` classifies `dr purge` as it does
today (frozen rows only, no lease).

**Gates.** L/C. S: the rehearsal (§13 runbook, revised): freeze, export, `dr archive` of
the **whole** release, the read-only checks, then `dr purge` and `verify --cloud` showing
no dangling row, no unregistered archive object, and the `collected` class equal to the
count the sweep printed. A redeploy of one observation while a release is frozen but not
archived must be refused (409) for the members and succeed once `dr archive` has copied
them.

**Risk.** The sweep deletes bytes: it is admin-only, refuses published and withdrawn tags,
plans from the manifest rows only, and the predicate is guarded; the first production run
is the rehearsal, on bytes the rehearsal itself created. A deploy that hits the gate during
an archive pass stalls on those files until the pass reaches them; the operator sees the
409 with the keys and the release tag. Rollback: the RPCs and the CLI flags are additive;
the old `log` behaviour cannot be restored without re-adding the variable, by design.

**Dependencies.** P4 (merged). **Size:** M, two PRs: (1) the gate (RPC + TS + Python +
tests + env/docs), (2) the sweep and collection (RPCs + guard + `dr purge` + `archive gc`
+ stamp-reuse restriction). Then the rehearsal.

---

## 10. Phase order, dependencies and size at a glance

| phase | depends on | size | first production-visible change | status (2026-10-14) |
|---|---|---|---|---|
| P-1 | — | L | anon reads stop (no user impact) | done |
| P0a | P-1 | S | deploy refuses accidental private flips | done |
| P0b | P-1 | M | `science_hash` columns populated | done |
| P0c | P0b backfill | S | fewer false `reprocessed` / `stale_spectrum` | done |
| P1 | P-1 PR 7 | M | `campfire dr status` (admin) | done |
| P2 | P1, (P0b) | L | redeploys retain old bytes; archive grows | done; the gate narrowed and the modes retired by P4e (2026-10-27) |
| P3 | P0c | M–L | badge count changes; reaffirm action | done |
| P4a–d | P1, P2, P0, (P3) | XL | a published DR row visible under RLS | merged 2026-10-14; rehearsal gate open |
| P4e | P4 | M | `dr purge` removes a rehearsal's archive bytes; redeploys stop retaining non-members | planned 2026-10-27 (§9a); before the rehearsal |
| P5 | P4 | L | client 0.7.0 | after P4e and the rehearsal |
| P6 | P4d, P5 | L | `/releases` | merged 2026-10-16 behind `NEXT_PUBLIC_DR_PAGES`; aliases dropped (D-P6-6) |
| P7 | P4d, P6 | M | a DOI | — |
| P8 | P4 | later | — | — |

P3 can run in parallel with P1/P2 once P0c is in. P4b and P4c can be developed in parallel
once P4a's tables exist. Total, excluding P7/P8: roughly 30 PRs.

---

## 11. Design assumptions contradicted by code or production (proposed amendments)

These are proposals for the design doc, not changes made here.

1. **§1 numbers.** Storage is ~12.1 TB stored / 15 TB logical, not 10.8 TB; NIRSpec
   finals total 92 GB; the stamped set per DR is ~57 GB of FITS, not "~100 GB". All
   79,978 spectra are published today; 1,339 objects match the needs-review predicate;
   15,443 objects carry implicit pins.
2. **§4.6 stamping.** Only 7 of 80,258 finals exceed 5 MiB (max 8 MB): every NIRSpec
   FITS goes through download-patch-upload and the header-patching multipart copy is
   dropped from the design (no multipart upload in the byte pass at all). The stamp stays
   in the header; a file unchanged across DRs keeps the tag of the first DR that froze it
   and the catalog records the later memberships (decided; decision 5 stands).
3. **§4.2 / §8.1 immutability.** Object Lock and versioning are not available on the
   bucket; write-once is enforced by conditional PUT (`If-None-Match: *`, verified), the
   registry trigger and the writable-key allowlist. Copy-before-sign uses conditional copy
   on the source ETag for the 409-on-drift semantics.
4. **§4.3 freeze executor.** One RPC with function-level isolation and timeout, run as
   a one-shot `pg_cron` job enqueued by an admin-guarded wrapper (decided D-P4-1); the
   admin CLI enqueues and polls. The function's guard must also admit the cron runner.
5. **§1 / §4.2 "superseded".** No writer sets `superseded` today. The live row cannot be
   both superseded and the pointer to the new bytes; the archive row carries the
   relation (`archived_from_key`), the live row stays `active`. The 30 existing
   `superseded` expmap rows are reclassified or left as legacy.
6. **§5 needs-review.** Derived columns maintained by one function, not a per-row
   predicate inside the list RPCs (perf trap H72), and the ≥1 % rule compares to the
   previous auto for explicit overrides (H71).
7. **§4.7 version endpoint.** `latest_dr` and the access fingerprint go on the
   authenticated `whoami`, not the public cached `/api/v1/version`.
8. **§4.7 client store.** A DR cannot share the live sqlite (PK/UNIQUE on live identities);
   it is a separate file per tag.
9. **§6 / Appendix A.** `deploy remove` leaves 280 dangling registry rows today; `etag:`
   rows are not a NIRSpec readiness concern; `nircam_layout` PNG is a product type.
10. **§1 anon exposure** is confirmed at runtime and is wider (functions executable by
    anon); fixed in P-1, not P0, with the roster leak accepted by you.
11. **New constraint.** The planned move to self-hosted Supabase: no phase depends on
    branching, the dashboard or the GitHub migration integration beyond today's use.
12. **§4.9 CLI.** `campfire dr archive` is a separate resumable verb from `freeze` (the byte
    pass needs object-store credentials the freeze RPC does not).

---

## 12. Decisions per phase (your notes of 2026-10-11 folded in)

| id | before | decision | status / notes |
|---|---|---|---|
| D-P0-1 | P0b | The science-hash backfill bumps `spectra.updated_at` (add `science_hash` to the trigger's column list) so clients pick the column up incrementally, ~184 MB once per client | **decided: yes, bump** |
| D-P0-2 | P0b | Science-hash definition as in §2 (sorted non-volatile cards + raw HDU data), versioned | **decided: yes** |
| D-P1-1 | P1 | Replace the chunked PostgREST `.in()` key lookups with an RPC taking `text[]` | **decided: yes** |
| D-P2-1 | P2 | Retained on redeploy: the six NIRSpec types; mosaics archived at freeze only | **decided** |
| D-P2-2 | P2 | A failing retain fails the deploy closed, with a logged admin override | **decided**. Meaning: if the copy of the old file into the archive fails for any reason (OSN error, ETag drift because someone else overwrote the key between plan and PUT), the deploy of that file stops instead of overwriting the live bytes without a retained copy, because an overwrite without a copy is the one unrecoverable event. `--no-retain` lets an admin proceed anyway and writes a `supersede` event saying so. |
| D-P2-3 | P2 | Two retain implementations (TS for presign, Python for service-role mode) kept in step by a conformance test | **decided** |
| D-P2-4 | P4e | The archive holds exactly what a release references. Retain-on-supersede copies a live object before overwrite only when a `dr_manifest` row names its hash and the byte pass has not archived it yet (`retain_required_hashes`); the gate is always on, `RETAIN_MODE` / `CAMPFIRE_RETAIN_MODE` and the `off` / `log` modes are retired, there is no override | **decided 2026-10-27.** Replaces "every final ever published" (design §0, §4.2, decision 9) and the between-releases citation story (§4.6, §4.8): the policy is "cite a release". D-P2-2's logged override goes with it |
| D-P4-5 | P4b | P(z) sidecars are not release members (export format v1) | **decided 2026-10-12** (recorded in the P4b log; row added 2026-10-27 so the id is defined) |
| D-P4-6 | P4e | `campfire dr purge` of an unpublished release also removes the archive objects only that release references (bytes deleted, rows `archived → collected`, one `collect` event); only a *published* release's stamp is ever reused by `dr_bytes_pending`, so no unpublished tag can reach a published file's `CMPFRDR` | **decided 2026-10-27**; makes the rehearsal fully reversible, including a full `dr archive` |
| D-P4-7 | P4e | No retention window: an archive object lives while any release's manifest references it (published, unpublished or withdrawn); `campfire archive gc` collects unreferenced objects of every product type | **decided 2026-10-27**; amends D-P4-3 and design decision 9 |
| D-P6-6 | P6 | Object-name aliases | **dropped 2026-10-27.** A release page shows an object under its frozen name from `dr_objects` with no live row, and a file is identified by hash; a stale live link gets a not-found that names the release it came from |
| D-P3-1 | P3 | Review the needs-review rule set before implementing; seeding (keep today's 1,339 badged, or clear) decided in that review | **open until the P3 design page**; P3 now starts with `docs/design-needs-review.md` |
| D-P3-2 | P3 | Add `deploy_status='published'` to `compute_object_redshift_auto` | **decided: yes** |
| D-P3-3 | P3 | Implicit pins: re-pin on reconcile when the auto moved less than tolerance (and badge as today when it moved more) | **decided: re-pin.** Background: when an inspector saves quality ≥ 2 without typing a redshift, the trigger copies the auto redshift *of that moment* into `redshift_inspected` and sets `inspected_used_auto` (15,443 objects). After a re-reduction the auto may move a little (under 1 % in 1+z): no badge fires, and the object keeps showing the old reduction's number forever, while its spectra are new. "Report only" lists these in `dr status`; "re-pin" updates `redshift_inspected` to the new auto automatically when the move is sub-tolerance, on the argument that the inspector's decision was "I accept the auto". Re-pinning updates `redshift_inspected` (and the line-fit staleness follows through `objects.version`), so the P3 review page specifies: only when `inspected_used_auto`, only below tolerance, audit row written with a system actor. |
| D-P4-1 | P4a | Freeze executor | **decided: `pg_cron` one-shot job**, enqueued by an admin-guarded wrapper; §6 P4a updated |
| D-P4-2 | P4c | Stamp in the header; one archive object per science hash, so an unchanged file keeps the tag of the first DR that froze it and the catalog records every DR it belongs to | **decided: header, first DR wins; no per-DR copies** |
| D-P4-3 | P4a | Mosaic retention: 2 DRs or 12 months, whichever is longer; `gc --dry-run` first | **superseded by D-P4-7 (2026-10-27)**: no window, reference-based |
| D-P4-4 | P4d | First real DR after the jwst 2.0.1 re-reduction campaign; a rehearsal DR (admin-only) is frozen as soon as P4a lands and is what P5/P6 are built against | **decided** |
| D-P5-1 | P5 | No client floor bump for 0.7.0 | **decided: no bump** |
| D-P6-1 | P6 | Public `/releases/<tag>` summary page | **decided: yes**; served through the service role for signed-out visitors (anon grants are gone after P-1), public-program counts only, `noindex` as everywhere |
| D-P6-2 | P6b/P6c | **DR browsing mode (`?dr=<tag>`)**: the normal catalog, object, spectra and NIRCam field pages browse a release; the FitsGL map/tiles stay live-only | **decided 2026-10-15: yes.** Frozen tables become full-row copies of the live tables (`tag` + every live column; parity guard), plus `dr_targets`, `dr_nircam_images` and a per-release copy of the filter options; the RPCs read through source views (`cat_objects`, …) that return live rows or the selected tag's frozen rows from a per-request setting; the tag travels as a request header read by a PostgREST `db_pre_request` function (`ALTER ROLE authenticator SET pgrst.db_pre_request`, documented by Supabase, plain PostgREST so it survives self-hosting); DR mode resolves files through `dr_manifest` (exposure maps + PNG renders and field layout plots join the manifest); mutation UI hidden in DR mode |
| D-P6-3 | P6b | Release page document: `dr_releases.page_md` (markdown) + figures under `dr/<tag>/page/` as manifest rows (component `page`), pushed with `campfire dr page push <tag> <dir>` before publish, locked with the row at publish; figures come from ad-hoc scripts kept in the repo, not a CLI generator | **decided 2026-10-15** |
| D-P6-4 | P6c | Cutouts in DR mode | **decided 2026-10-15: live** (rendered from the live mosaics; no explanatory copy) |
| D-P6-5 | P6b | NIRCam area on the release card | **decided 2026-10-15:** `nircam_images.area_arcmin2` computed at deploy from the mosaic coverage (expmap / non-NaN science pixels), copied by the freeze; the field-grid bounding box is not used (rotated grids overstate it) |
| D-P7-1 | P7 | Flat exports: CSV + FITS; Parquet deferred | **decided** |
| D-P8-1 | P8 | Bundles download-only | **decided** |
| D-OPS-1 | — | OSN lifecycle rule for incomplete multipart uploads and abort rights | **downgraded to nice-to-have.** Mosaics are never stamped, so they are never downloaded: a mosaic is archived with one server-side `CopyObject` (measured 0.5 s per 128 MiB in-cluster; a 4 GB mosaic is seconds to a minute), which has a 5 GB per-object protocol limit; no stored mosaic exceeds 4.1 GB today. Only NIRSpec FITS (≤ 8 MB) take the download-patch-upload path. `UploadPartCopy` is needed only if a mosaic ever exceeds 5 GB, and a crash there leaves un-abortable garbage, nothing worse. The stranded 6 MB upload from the recon is the only cost today. One email, nothing waits on it. |

---

## 13. Progress log

### 2026-10-11 overnight session (autonomous; nothing merged, nothing applied to production)

PRs opened, in merge order (3, 5, 6 and P0b are stacked on 2 and must be retargeted to
`main` after it merges: `gh pr edit <n> --base main`):

| PR | branch | base | what | production effect on merge |
|---|---|---|---|---|
| #579 | `claude/ci-python-and-web` | main | P-1 PR 1: `python` + `web` required checks | none (CI only); then run the branch-protection command in its description |
| #580 | `claude/supabase-native-stack-pgdelta` | main | P-1 PR 2: native stack, pg-delta, `database` check, harness DB resolver | **migration** `20261011021845` (8 comments + 6 sequence `AS integer`) |
| #581 | `claude/security-anon-public-execute` | #580 | P-1 PR 3: anon + PUBLIC-execute closure, access matrix + lints | **migration** `20261011022503` (22 `ALTER POLICY … TO authenticated`, relation/function revokes, default privileges) |
| #582 | `claude/scale-db-tooling` | #580 | P-1 PR 5: dump / scrub / load scripts | none; **the dump was not run** (sandbox refused); user runs `bash scripts/scale_db/dump_prod.sh` |
| #583 | `claude/p0a-is-public-guard` | main | P0a: `is_public` written only when stated; public→private refused unless forced | none (Python) |
| #584 | `claude/layout-parity` | main | P-1 PR 6b: generated web product table, foreign-root goldens | none |
| #585 | `claude/shared-test-fakes` | main | P-1 PR 6a: `FakePostgrest`, `FakeS3Client` | none; conflicts with #583 in `test_config_sync.py` (small rebase) |
| #586 | `claude/p0b-science-hash` | #580 | P0b: science hash definition + goldens, pipeline/deploy write path, schema, backfill script | **migration** `20261011025750` (two nullable columns, partial indexes, trigger column list, sync payload); backfill not run |
| #587 | `claude/p1-layout-roots` | #580 (+ #584 commits) | P1 layout half: `ParsedKey.root`, archive/dr parsing, `key_for_parsed`, read vs write allowlists, presign PUT uses `isWritableKey`, RELEASE lifecycle, goldens | none |
| #588 | `claude/seed-regeneration` | #580 | P-1 PR 4: regenerated seed (31 public programs, synthetic identities, line fits, photometry, deployments, NIRCam, share links, draft/revoked spectrum, real registry hashes) + `check_seed_privacy.py` in CI | none (seed only reaches previews / local) |
| #589 | `claude/p2-archive-schema` | #580 | P2 schema half: `root` / `program_slug` / archive columns, status values, root↔status CHECK, `protect_archive_rows`, archive indexes, DR event actions, backfill script | **migration** `20261011031603` (NOT VALID + VALIDATE checks; no rewrite) |
| #590 | `claude/p2-retain-shadow` | #589 (+ #587/#584 commits) | P2 presign gate: `planRetain` / `executeRetain`, `RETAIN_MODE` off/log/enforce (default log = shadow), 409 on drift; unit + route tests; CLI sends `expected_old_hash` / `new_hash` per upload (`UploadTask` fields stamped by `plan_push`); the same gate runs client side on the direct boto3 path (`campfire/deploy/retain.py`, `CAMPFIRE_RETAIN_MODE`, default log) | none until `RETAIN_MODE=enforce` is set on Vercel; `log` writes only function-log lines |
| #591 | `claude/p1-lease-readiness` | #580 | P1 deploy-lease + readiness half: `deploy_scope_state` lease columns, `begin/heartbeat/release_deploy_scope` (+ `claim` clears), `deploy_lease_ttl()` 30 min, `ScopeLease` in all three deploy flows, `--steal-lease`; `dr_readiness()` jsonb + `campfire dr status [--json]`; SQL harnesses `deploy_lease.sql` / `dr_readiness.sql` | **migration** `20261011103719` (four nullable columns, functions, grants) |

Verified in CI so far: `python` and `web` green on #579 (the informational `pipeline` job
installs the jwst stack fine in ~6 min and reports the 8 pre-existing `test_align_apply` /
`test_linefit` failures that exist on `main`); `database` green on #580, #581, #582 (the
native stack starts on the GitHub runner).

P1's deploy-lease + readiness half landed as #591 (~10:25 UTC; the delegated agent took
longer than planned). `science_hash_missing` in `dr_readiness()` is null until #586's columns
exist (checked via `information_schema`).

**Review round (2026-10-11 ~03:00–03:45 UTC).** The automated reviewers (Codex, Claude) left
inline findings on most PRs; all the real ones were fixed on their branches and answered in a
PR comment each (merge order unchanged):
- #579 / #580: the three required aggregates (`python`, `web`, `database`) failed open when
  the `changes` path-filter job itself failed; they now require it to have succeeded. The
  migration header check requires both timeout directives with exact values.
- #582: scrub covers every user-authored text column (object lists, list-member notes, the
  four exposure-review `notes` columns); test users get the `password123` hash + identities;
  the dump refuses the checkout root itself.
- #583: the forced-flip audit event is logged after the upsert; a private cloud row never
  gets `is_public=false` re-sent (immune to the check→write race).
- #584: three adversarial probe scopes for the template generator; pipeline changelog entry.
- #585: `FakePostgrest` exact count precedes `range`/`limit`; `FakeS3Client` raises real
  `ClientError` subclasses so `except client.exceptions.NoSuchKey` works.
- #586 (P1 findings): value-less header cards hashed non-deterministically (fixed token
  `<UNDEFINED>`, new golden); `spectra.science_hash` never reached the spectra upsert (summary
  column + `get_spectra_records`); `Store.upsert_spectra` dropped it; the backfill now writes
  `spectra` first and repairs stragglers.
- #587: `is_archivable` / `isArchivable` is the single predicate (data bucket only, so tiles
  and summaries are out of archive keys and DR relpaths); `archive_hex` accepts only the sha256
  scheme. New golden sections `archive_rejected_hashes` / `archive_rejected_products`.
- #588: the generator fetches published spectra only and refuses non-published rows (the
  existing sample had none, verified against production); the privacy checker walks
  `program_slugs` arrays, which immediately caught the seeded access code referencing the
  non-public `ember` program (now scoped to a public program).
- #589: `protect_archive_rows` freezes the whole row (`to_jsonb` minus status / collected_at /
  updated_at), assigns `collected_at` itself and fires on INSERT; migration edited in step
  (unapplied), pg-delta no-drift, harness green on the native stack.
- #590: also carries the CLI hash fields and the direct-boto3 retain gate (above), #589's
  trigger fix (merged) and #587's / #584's fixes (cherry-picked).
- #591 (~10:45 UTC): `begin_deploy_scope` bumps the version and returns it as the holder's
  ownership token (heartbeat / release take `p_version`; a displaced deploy's claim conflicts);
  `ScopeLease` heartbeats from a daemon timer independent of upload progress;
  `dr_readiness()` runs under a 120 s statement_timeout. Migration regenerated.

Known: `pg8000` was pip-installed into the `campfire` conda env to run a SQL harness locally
(no `psql` on this machine). The conda editable installs point at the main checkout, so tests
in a worktree need `PYTHONPATH=pipeline:python:layout`.

Facts learned tonight that change the plan text above:
- `supabase status -o env` does not exist under the stack commands; use
  `status --output-format json` (`.env.DB_URL`). Every git branch is its own local project
  with its own ports; fixed ports in `config.toml` are commented out for that reason.
- pg-delta modelled the function revokes and `ALTER POLICY` statements but **not** the
  relation privileges, default privileges and six long-signature RPC revokes; those are
  hand-authored in the same migration and the schema files carry matching statements.
  `declarative sync --strict-coverage` → "No schema changes found" after each migration.
- The schema-less default privileges for `postgres` (set at project creation) also
  granted anon; the migration revokes those too. `supabase_admin`'s defaults are out of
  reach and excluded from the lints.
- Guards and harnesses that create `pg_temp` helper functions must `GRANT EXECUTE` them
  before `SET ROLE`, since functions now default to no PUBLIC execute.
- The Python client suite needs `supabase`, `boto3`, `spectres` installed in CI (deploy
  modules import them at module level); `campfire[deploy]` would pull the jwst stack.

### 2026-10-11 merge day (user merged; nothing applied by hand)

Merged to `main` in this order: #579, #580, #581, #588, #586, #587, #589, #590, #591, #582,
#583, #585. #584 (layout parity) was **closed, not merged**: #587 carried cherry-picked copies
of its three commits and deliberately flipped its "foreign roots are rejected" tests, so
merging it afterwards would have conflicted or re-introduced failing tests.

Between merges every stacked branch was refreshed with a *merge* of `origin/main` (never a
rebase, so the stacked SHAs the review bots had seen stayed valid). Conflicts, all
append-at-end or adjacent-line:
- `pipeline/CHANGELOG.md` on #586 / #587 / #590 (two Unreleased entries, both kept);
- `supabase/schemas/indexes.sql` and `triggers.sql` on #589 (#581's REVOKE list and #586's
  science-hash indexes beside the archive blocks; both kept). Re-verified on the native stack:
  `declarative sync --strict-coverage` → no drift, every `check_*.sql` guard, the eight SQL
  harnesses;
- `web/app/api/v1/deploy/presign/route.ts` on #590 (retain imports next to #587's
  `isKnownKey` → `isWritableKey` line).
#590 had to be retargeted from `claude/p2-archive-schema` to `main` by hand
(`gh api -X PATCH …/pulls/590 -f base=main`; `gh pr edit` fails on the Projects-classic
GraphQL error) and pushed once, because a base change alone never triggers the required
checks ("Expected — waiting").

Production (verified read-only via the MCP migration list, 2026-10-11 ~16:40 UTC): all five
new migrations applied — `20261011021845` pgdelta catch-up, `022503` anon grants / PUBLIC
execute / policies, `025750` science_hash columns, `031603` archive root columns +
immutability, `103719` deploy lease + dr_readiness.

The informational `pipeline` CI job fails on `main` with the same seven
`pipeline/tests/test_align_apply.py` cases (`NOT_ALIGNED` vs `SOLVED`/`SKIPPED`), a
pre-existing failure unrelated to this work; it is a separate task.

Still with the user (unchanged): `bash scripts/scale_db/dump_prod.sh` (outside 04:30–05:30
UTC); `scripts/backfill_science_hash.py`; `scripts/backfill_registry_program_slug.py`;
`RETAIN_MODE=enforce` (Vercel) + `CAMPFIRE_RETAIN_MODE=enforce` when ready; abort the
stranded OSN MPU `scratch/dr-recon/mpc-small.bin`. Local stacks stopped (`stack stop --all`)
and all run worktrees removed.

Next implementation work (P2 remainder, then P3): archive authz policy +
`filter_accessible_storage_keys` + Worker `registeredAt`; budget classes; `gc --dry-run`.

### 2026-10-11 post-merge operations (user ran; verified read-only afterwards)

- Production dump: `scripts/scale_db/dump_prod.sh` → 199 MB custom-format (zstd:3), 45 s
  through the session pooler (record these in `scripts/scale_db/README.md` "Expectations"
  with the next docs PR). The circular-FK warning (observations ↔ deployments) is expected;
  `load.sh` restores with triggers off. Not yet loaded into a local stack.
- `scripts/backfill_registry_program_slug.py`: 231 observations, 578,176 rows, no retries;
  afterwards 0 NULL, 0 disagreements with `observations.program_slug`, 0 `updated_at` bumps.
- `scripts/backfill_science_hash.py --workers 8`: 96,194 registry rows / 62.4 GB streamed
  from OSN, `ok=96194`, ~1.7 h wall from this Mac; afterwards 0 pending on both tables,
  0 spectra↔registry disagreements, 79,978 hashed spectra with 79,978 distinct hashes (no
  duplicate science content in the live catalog). Every spectra row's `updated_at` moved
  once (expected: next incremental sync re-pulls the metadata).
- Still open: OSN email (abort MPU `scratch/dr-recon/mpc-small.bin`, ask for an
  incomplete-MPU lifecycle rule); `RETAIN_MODE=enforce` after 3–4 real redeploys observed
  under `[retain:log]` (none yet: 0 deploy events in the 24 h before 17:00 UTC).
- P0c (switch the "changed" decisions to the science hash) is now unblocked.

### 2026-10-12 P0c + P2 remainder (autonomous; nothing merged, nothing applied to production)

Five PRs opened off `main` (`279c429e`), each verified on a fresh native stack (`db reset`,
every `supabase/tests/check_*.sql`, drift gate `--strict-coverage`, Python SQL harnesses) and
with the client / web suites that touch them:

| PR | branch | content | migration |
|---|---|---|---|
| #592 | `claude/p0c-science-hash-changed` | P0c: `spectrum_content_changed()` decides the planner's `changed` set on the science hash when both sides carry one; `spectrum_line_fits.spectrum_science_hash` (the `SCIHASH` card via `deploy lines`); `line_fit_stale_spectrum()` IMMUTABLE, called by the `spectrum_line_fits_status` view and `get_line_fits_for_sync` (whose inline `stale_redshift` copy now calls `line_fit_stale_redshift()` too); guard `check_line_fit_stale_spectrum.sql` | `20261012005235` (column + CHECK, function, view + RPC bodies; no backfill) |
| #593 | `claude/p2-archive-authz` | P2 archive authz: policy + `filter_accessible_storage_keys` + `filter_link_storage_keys` gain the archive branch (row-local `program_slug`, link narrowed on the row's `observation`, live spectrum published / draft-for-drafts-link; `collected` never; mosaic copies admin-only until P4); `resolveObjectBackends` resolves archived rows with `registeredAt = archived_at`; guard `check_archive_authz.sql` (anon, no-grant, grantee, admin, obs / no-download / drafts / field links) | `20261012010045` (policy + two functions) |
| #594 | `claude/p2-budget-classes` | P2 budget: bytes at rest (`COALESCE(stored_size_bytes, size_bytes)`) over `active` + `archived`; `by_class` {live, archive, collected (0 bytes, `logical_bytes`)}; cap stays hard-coded 20 TB with a comment; `campfire status` second line; admin card; guard `check_storage_budget_classes.sql` | `20261012010726` (function body) |
| #595 | `claude/p2-archive-gc-dry-run` | P2 gc: `campfire archive gc --dry-run` (mosaics only, `--older-than`, JSON; refuses to run without `--dry-run` until P4); `verify --cloud` names `unregistered_archive` objects and fails on them | none |
| #596 | `claude/fix-main-ci` | `main` was red: `test_config_sync.py` used `_FakeClient` (#583) after #585 removed it. Ported to `FakePostgrest`. **Merge first**; the four PRs above carry it as a merge commit so CI can run | none |

Shadow gate for P0c (production, read-only, 2026-10-12 00:55 UTC): 15,916 line fits in 57
observations, 0 stale under the whole-file rule, 0 fits without `spectrum_hash`, 0 spectra
without `science_hash`; the new predicate cannot differ until a fit carries `SCIHASH`.

Also learned: the migration header check reads the first two non-comment *lines*, so the
two `SET`s must be on separate lines (fixed on all three migration branches); CI runs only
on PRs, so `main` can be red without a signal (two independently green PRs merged into a
NameError) — worth a push-to-main run of `python` / `web` / `database`.

Open decisions surfaced (plan §12 candidates):
- **Mosaic retention window** (design: "time-boxed"): `DEFAULT_RETENTION_DAYS = 365` is a
  placeholder behind `--older-than`; the real `gc` (P4) needs the number.
- Mosaic freeze copies (no `spectrum_id`) are admin-only in every archive predicate until
  P4 adds a deployment-scoped branch.

Not started (by request): P3 (needs-review redesign) waits for discussion.

#### 2026-10-12 (later) — Codex review fixes on #593 / #594 / #595

- **#593** (P1): `resolveObjectBackends` sent an `archive/…` key to R2 under the input key whenever `OSN_READ_ENABLED` was off, the registry lookup failed, or the row was missing — and R2 never holds archive objects. Archive keys now resolve to OSN under their own key in every path (they are OSN-only by construction: retain-on-supersede and the freeze pass register them with `backend = 'osn'`); live keys keep the legacy-key R2 fallback. Three dual-read tests.
- **#594** (P1): the budget scoped bytes-at-rest to `active` + `archived`, so a revoked field (`deploy revoke` retains bytes) or a superseded tombstone left the usage although nothing was freed. Scope is now every status except `collected`; `(backend, bucket, storage_key)` is unique across statuses so nothing double-counts. New `by_class.gc_pending` (superseded + revoked); `live + archive + gc_pending = registry_bytes`. Production today: 30 superseded `nircam_expmap` rows (1.1 GB, 2026-07-28), no revoked rows. Migration regenerated (`20261012143628_p2_budget_classes.sql`). Timing of the scope on prod: 0.8 s seq scan (a DISTINCT ON variant tried first took 18 s and was unnecessary given the unique constraint).
- **#595** (2× P2): `archive gc` is now admin-gated through the shared `_gate_admin` (a non-admin's RLS view holds no archive rows, so the ungated report read as an empty archive); `--limit N` caps only the listed rows, the totals always cover every candidate (`rows_shown` in the JSON).
- Lesson: the Codex reviewer posts inline threads on the PR, not a summary comment — check `gh api repos/…/pulls/<n>/comments` before declaring a PR ready; the `claude` review comment ("No issues found") is not the only review.

### 2026-10-12 P3 discussion and PR 1 (needs-review)

Discussed with the user; outcome in `docs/design-needs-review.md` (untracked, like the other
design docs). The rule collapsed to one: an object signed off at quality 1–3 needs review iff
its published member spectrum set differs from the set the inspector saw at sign-off (either
direction). Quality 4 never badges, reprocessing never badges, the auto redshift moving never
badges. No `data_changed` chip, no `object_review_state` table, no bulk reaffirm (D-P3-5).
Production read-only: 1,339 badged today → 46 under the new rule (36 of them badged today).

PR 1 (`claude/p3-needs-review`): `objects.reviewed_spectrum_ids` + `objects.needs_review`
(partial index), `recompute_needs_review()` set-based, `stamp_reviewed_spectra` BEFORE trigger
on `last_inspected_at`, statement-level AFTER triggers with transition tables on spectra /
targets / objects (Postgres forbids `UPDATE OF col` with transition tables, so the UPDATE
triggers fire on every statement and the function narrows to rows whose relevant columns
changed), `seed_reviewed_spectra()` + `scripts/backfill_reviewed_spectra.py` (resumable,
service role; the reviewed set = published members whose spectrum row predates the sign-off,
compared as UTC because `spectra.created_at` is timestamp without time zone), guard
`check_needs_review.sql` (11 steps). Old predicate still drives the UI; PR 2 switches the seven
RPC copies, `dr_readiness`, sync / store / types, the badge copy, and stops the three
`staleness_reason` writers.

PR 1 is #597 (Codex: seed must not depend on current publication status → members that existed
at sign-off regardless of status, exact today since all 79,978 spectra are published; flag
coalesced; flip count measured around the stamping update — all fixed). PR 2 is stacked on it
(`claude/p3-needs-review-switch`): the seven RPC predicate copies, `dr_readiness`, every object
payload, the web badge (`needsReview` prop, one reason copy), sqlite mirror schema 11, and the
three staleness writers stopped (`apply_object_reconciliation`, `compute_object_redshift_auto`,
`reconcile.py`). Local-seed caveat: `generate_seed.py` does not carry `spectra.created_at`, so
a local seed badges every q1–3 inspected object (27 of 70); production uses real timestamps.
Order: merge #597 → run `scripts/backfill_reviewed_spectra.py` → merge PR 2.

### 2026-10-12 P3 merged and seeded

#597 and #598 merged by the user (~18:34 UTC); migrations `20261012165329_p3_needs_review_state`
and `20261012165723_p3_needs_review_switch` verified live in production (read-only): columns,
partial index, seven triggers, six functions, the nine reader/writer RPC bodies, `dr_readiness`
(as admin: `needs_review` 0, `implicit_pins` 15,456) and the object-id RPC with
`p_needs_review` (0 / 32,234 public). Vercel production is the #598 build (user's dashboard).
Seed run by the user: dry run 31,436 pending; real run seeded 31,436, flipped 46, 46 badged
(q1 19 / q2 13 / q3 14; none inactive; 0 pending) — the id set is identical to the read-only
preview taken before implementation (`scratch` list, 46 of 46). Only the 46 flipped rows had
`updated_at` bumped, so sync clients pick up exactly those.

Supabase Preview check notes: #597 went red with "Remote migration versions not found" because
the migration was renamed (new sync timestamp) after the preview branch had applied the first
version — keep a migration's version stable once a PR has a preview branch, or reset the branch;
#598 was "Maximum number of concurrent branches reached" (the open #495 holds a slot; stacked PRs
get no branch anyway). Neither is a required check.

Follow-ups carried: `recompute_needs_review` is executable by `authenticated` through the
schema's default function privileges (same as `apply_object_reconciliation` /
`recompute_has_published_spectrum`; it only rewrites the derived flag) — tighten with an explicit
`REVOKE … FROM authenticated` in the next schema PR; `generate_seed.py` should carry
`spectra.created_at`; drop `staleness_reason` / `last_data_change_at` next release. Next: P4a.

### 2026-10-12 P4a (freeze core) — PR open (autonomous; nothing merged)

Branch `claude/p4a-freeze-core`. Seven `dr_*` tables (releases, spectra, objects, members,
line_fits, photometry, exports), RLS (published → everyone non-link within their programs;
every state → admins; exports admin-only; SELECT-only grants), `dr_visible_tags`,
`dr_require_admin`, `dr_cron_unschedule`, `dr_parse_components`, `dr_enqueue_freeze`
(pg_cron one-shot per D-P4-1, inline where absent / `--local`), `dr_freeze_job` (ONE
statement of data-modifying CTEs = one snapshot; published rows only; evidence_programs,
z-source spectrum, staleness and review state computed in-snapshot; zfit / `_lines`
sidecars joined by key because production registered them without `spectrum_id`),
`dr_purge_release`, `dr_readiness.releases`; CLI `dr freeze` / `dr purge`; seed `DR-seed`
frozen by the real freeze; guard `check_dr_freeze.sql` (5 principals × 7 tables, before
and after publish); `scripts/measure_dr_freeze.sh`.

Harness on the scale DB (gate L): 79,978 spectra / 42,452 objects / 15,916 fits /
353,452 members in **10.3 s** (14.1 s re-run), WAL 1.43 GB, temp 155 MB, frozen tables
1.31 GB (members 786 MB with 5 indexes, line fits 244 MB, spectra 204 MB), 0 conflicts
under concurrent sync pages + redeploy-like touches, cut-short run → `failed` and the
re-run converges. Size is ~5× the §6 estimate; slimming `dr_members` is a follow-up before
the first real DR. Trap found: the first rehearsal ran 10+ min on a no-stats plan right
after the dump load → `load.sh` truncates before the restore, uses `pg_restore -f -`, and
ANALYZEs explicitly.

Deviations (plan amendments): CLI calls `dr_enqueue_freeze` through PostgREST directly
(no Vercel admin route needed); `dr purge` lands in P4a (the shadow-run gate needs it;
P4d's `withdraw` is the published → withdrawn path); a `failed` state was added. Follow-up:
deploy's registry registration should set `spectrum_id` on zfit / lines rows.

### 2026-10-12 P4b (exports) — PR #600 open, stacked on #599 (autonomous)

Branch `claude/p4b-exports`. `docs/dr-format-v1.md` (one gzip JSONL per (stream, partition);
partitions = program slug for objects / spectra / line_fits / photometry / members, component
name for field-level member rows; objects rows carry aggregates scoped to the partition
program; mixed objects appear in each program's partition and readers merge by id; P(z)
sidecars are not members, D-P4-5). SQL: `dr_export_key` (key-safe names as is, otherwise
sanitised + 8-hex md5 suffix — Codex), `dr_export_partitions`, `dr_export_rows` (keyset,
`next_cursor` / `row_json`), `dr_export_begin` / `dr_export_mark` / `dr_export_invalidate`
(ledger), `dr_derive_state` (frozen → exporting → exported → copying → ready from the ledger +
member byte states). Web: `lib/server/dr-exports.ts` (budgeted resumable builder), `POST
/api/v1/dr/<tag>/export` (admin, 300 s ceiling, budget ≤ 280 s), `GET /api/v1/dr/<tag>/partitions`
(held partitions with 1 h presigned urls; published, or any state to admins; `complete` flag).
CLI `dr export`. Guard `check_dr_exports.sql`: the DR-seed partitions equal the live scoped
sync RPCs for a one-program caller (ids, staleness, scoped n_spectra / gratings), keyset
paging, ledger state machine, key rule, invalidation, derived states, gates. Codex P1 (member
files stale after the byte pass) → the byte pass invalidates the member partitions it changes
and `dr export` re-runs rebuild them; P2 (lossy key sanitising) → md5 suffix. Amendment: the
builder runs under the 300 s function ceiling, not the plan's 800 s; the CLI loop hides it.

### 2026-10-13 P4a Codex fixes; P4c (byte pass) — PR open, stacked on #600 (autonomous)

P4a (#599) review fixes: `begin_deploy_scope` refuses (CF409) while a release is `freezing`,
both sides take one advisory lock and the job re-checks leases before its snapshot; frozen
object `programs` / aggregates / `redshift_auto` / `redshift` come from published members only;
photometry components require `nirspec`; the pg_cron job uses the `'5 seconds'` interval and
unschedules itself first. #600 rebased.

P4c, branch `claude/p4c-byte-pass`. SQL: `dr_bytes_pending(tag, after, limit, include_failed,
prefix)` (keyset page; per member the source of its frozen bytes — the live key while the live
row still holds the frozen hash, else a retained archive copy, else NULL = gone — and a stamped
object of another release with the same science to reuse); `dr_bytes_mark(tag, results)`
(batches ≤ 2000: states, archive registry rows, one `archive` event per batch, member-partition
export invalidation, derived state once per batch — **amendment**: the plan's per-member mark
would run `dr_derive_state`'s count over every member ~350k times); `dr_bytes_summary`. The
pending partial index now covers `failed` too. Python `campfire.deploy.dr_bytes`: raw primary-
header card insert (`CMPFRDR`, one 2880-byte block of growth only when END is in the last slot;
CHECKSUM in the primary header refused), FITS GET → sha256 check → stamp → science-hash check
→ PUT `If-None-Match: *` to `archive/<stamped sha>/<relpath>`; everything else CopyObject with
`CopySourceIfMatch` to `archive/<content_hash>/<relpath>` (5 GB ceiling checked; largest stored
mosaic 4.29 GB); reuse per D-P4-2; 412 / existing target = heal; drift → `failed` with the reason.
CLI `dr bytes --tag [--obs | --field] [--workers] [--limit] [--retry-failed] [--dry-run]
[--status]` (OSN credentials: the `[r2_osn]` block, since `data` still resolves to R2 on the
admin laptop). Production facts (read-only 2026-10-13): every member product has sha256 hashes,
every NIRSpec FITS has a science hash, 0 archive rows yet. Real-corpus check (read-only GETs,
60 finals incl. the 7 largest > 5 MiB): all stamped in place, data segments byte-identical,
science hash unchanged and equal to the registry's.

### 2026-10-13 Review rounds on #599–#601; P4d (publish) — PR open, stacked on #601 (autonomous)

Codex review fixes, all pushed and answered in-thread:
- **#599**: frozen `photo_z` / `has_photometry` come from the selected catalogs (newest
  cross-match), NULL / false without one; `--json` writes only JSON to stdout; a published
  mosaic without an active registry row fails the freeze (production: 2084 / 2084 registered).
- **#600**: `dr_export_begin` refuses an empty release; the builder keeps a reserve of
  max(30 s, 2× slowest partition) before starting another.
- **#601**: reused archive objects are HEAD-checked (a missing stamp object is stamped afresh;
  a missing copy fails with `verify --cloud`); a science hash computed by the pass is persisted;
  server-side copies are bound to the frozen content. A live source modified after the freeze
  (`frozen_at` − 2 min) is downloaded, sha256-checked and uploaded (≤ 64 MiB), else drift.

P4d, branch `claude/p4d-publish`. SQL: `dr_publish(tag, doi, known_issues)` (ready, re-derived,
or withdrawn → published; DOI format checked; `publish_dr` event); `dr_withdraw(tag, reason)`
(published → withdrawn, reason appended to `known_issues`, `withdraw_dr` event; rows and bytes
kept, purge still refused); `dr_diff(tag, limit)` (spectrum science changed / unpublished / gone
/ new; object gone / renamed / redshift / quality; review raised / cleared; counts + capped
samples); `dr_observation_references(obs)` (`blocks` when the release is published, or
unpublished with unarchived members). CLI `dr publish | withdraw | diff | refs`. `deploy remove`
refuses while any release blocks, and fails closed when the check cannot run. **Amendment**: a
fully archived unpublished release, a withdrawn one and a failed one do not block a removal (the
plan said "referenced by a published DR"; the unarchived case is added because removing live
bytes before the byte pass would lose them). Seed note: `dr_diff` on a fresh DR-seed reports 8
redshift changes because the seed copies production's stored auto redshifts but only a sample
of their spectra; recomputing the live rule in a rolled-back transaction reproduces the frozen
values exactly.

**P4 review round (2026-10-13, late).** P4a: `deploy publish/revoke --field` (one RPC per
deployment) and `objects reconcile/rebuild/split/merge` run under the field lease; standalone
writers refuse when the lease degrades instead of running unguarded; `dr_releases.freeze_requested_by`
carries a refreeze's requester to the pg_cron job (`frozen_by`, audit actor). P4c: **amendment** —
the "unchanged since the freeze" LastModified shortcut is dropped: `campfire push` writes an object
before it registers its hash, so a modification time is no proof of identity; every live source is
verified against the frozen hash (one read per member the first time any release archives it, about
1.4 TB stored for the mosaics today; later releases reuse by content hash). Stamp reuse is limited to
releases frozen earlier in `(frozen_at, tag)` order. Noted, not changed: `deploy publish --field`
still requires a dummy `--obs` (pre-existing, `shared_options`).

**P4 review round (2026-10-13, night).** P4a: `ScopeLease.begin` fails closed for every deploy
(`DeployScopeUnavailable`); the multi-obs deferred reconcile/photometry runs under field leases;
frozen mosaics need `so.deployment_id = ni.deployment_id` (production: no NULLs on either side);
**bug fixed** — the standalone lease helpers collided with the writers' `dry_run=` keyword
(TypeError on `deploy json/zfit/lines/photometry`, `objects reconcile/rebuild` as pushed in the
previous round), now positional-only with an end-to-end test; `objects split/merge` confirm before
leasing. P4b: per-build export keys `<partition>.<sha16>.<claim8>.jsonl.gz`, an end-of-run sweep of
unreferenced keys older than 30 min, `dr purge` deletes `dr/<tag>/exports/`, lost claims are
skips. P4c: stamp selection atomic (serialized marks; earlier-frozen stamp wins; later unpublished
stamps re-pended). P4d: interactive `deploy remove` confirms before leasing, re-checks under it.

**P4 restructuring (2026-10-13, night).** After independent reviews of all four PRs (each
"merge after listed fixes", fixes in) and a design pass with the operator: a release's file
list is its **manifest** (`dr_members` → `dr_manifest`, the `members` export stream →
`manifest`); the readers' route is `/api/v1/dr/<tag>/files` (`files: [{stream, scope, ...}]`);
**exports are built locally** by `campfire dr export` (`python/campfire/dr_export.py`), so the
server builder route, its 300 s budget, `dr_export_garbage`, the 2 h grace and the cleanup
cron are gone (claims, per-build keys and the ledger-checked self-delete stay; the end-of-run
sweep needs no grace because readers only see published releases, whose ledger never
changes); `campfire dr bytes` is **`campfire dr archive`**; the stamp rule is **first release
to archive a file owns its stamp** (the cross-release lock / supersede / re-pend machinery is
gone); **`campfire dr build`** runs freeze → archive → export resumably, `dr --help` explains
the four steps and every step prints the next. NIRCam mosaics stay unstamped (provenance by
hash: manifest + `identify`). The author-written paper file is renamed the **provenance file**
(`campfire provenance`). P5 constraints recorded in §7: one merged table per stream for the
reader's programs, CSV and FITS, the same download from the web portal.

### 2026-10-14 P4 merged (user merged #599–#602; nothing applied by hand)

**Final review rounds** (Codex, triaged by the agreed bar: fix data loss, privacy, wrong
output and broken normal-path commands; document races between two of the admin's own
operations). Fixed: `archive gc --json` key `dr_membership_checked` (rename damage); `deploy
publish/revoke --field` resolves its deployments only under the lease (a field's first
deployment is no longer missed); `dr export` reads the status after reconciling failures another
run repaired; `dr archive --local` refused without `--dry-run` (shared bucket); `dr_bytes_summary`
rejects an unknown tag; `dr publish/withdraw --json` keep their prompts on stderr; a conditional
PUT that lost a race registers the winning object's size; `dr diff` shows the file hashes it
compared when a spectrum has no science hash; an unexpected `claim_deploy_scope` payload carries
an `error`, so `deploy remove` fails closed on it (the advisory deploy path is unchanged).

**Decision (operator, 2026-10-14): one `dr archive` run per release.** Codex kept finding races
between two concurrent archive runs of the same release; a per-release claim removes the class.
`dr_releases.archive_claim` / `archive_claimed_at`; `dr_archive_claim(tag, claim)` takes the
claim (refused while another was refreshed in the last 10 minutes) or refreshes our own;
`dr_archive_release(tag, claim)`; `dr_bytes_mark(tag, results, claim)` accepts only the holder's
outcomes and refreshes it; the client refreshes every minute; `dr_purge_release` waits while a
claim is live. `--dry-run` / `--status` take no claim. After a hard kill the release is held up
to 10 minutes (a `--take-over` flag was offered, not built). Archive stays resumable: per-member
state, marks per 200, a crash after an upload is healed by the content-addressed existence check.

**Known limitations (documented in the PR threads, not fixed).** A purge of a tag while an
export build of that tag runs past its 10-minute claim; an admin's shadow-run presigned URL can
404 if that file is rebuilt within the hour; a lost (vanished) gzipped archive object recreated
from a recompressed live copy keeps the original registered size (archive rows are immutable,
P2 trigger; `verify --cloud` reports the loss, not the size); a live registry row whose object
is missing fails its member as drift rather than falling back to a retained copy (the drift is
the signal); two *different* releases archiving the same large mosaic while it is re-pushed can
register the other encoding's size; `config push` of an observation's field during an open
`deploy remove` prompt (recover with `deploy objects reconcile --field <old>`).

**Merged** 2026-10-14 15:25 (#599), 15:40 (#600), 15:47 (#601), 15:50 UTC (#602), as merge
commits; #605 (unrelated) landed in between without touching `supabase/`. **Verified read-only
in production** after each Supabase check passed: migrations `20261012200216_p4a_dr_freeze_core`,
`20261012205751_p4b_dr_exports`, `20261013152931_p4c_dr_byte_pass`, `20261013154023_p4d_dr_publish`
applied; `dr_manifest`, `dr_export_rows`, `idx_dr_objects_tag_name`, `dr_archive_claim`,
`dr_archive_release`, `dr_bytes_mark(text, jsonb, uuid)`, `dr_publish`, `dr_withdraw`, `dr_diff`,
`dr_observation_references` present, `dr_export_garbage` absent, none executable by `anon`;
`dr_releases` empty (no release frozen yet).

**CI note.** The informational `pipeline` job fails on any PR touching `pipeline/` or `layout/`:
the 7 pre-existing `test_align_apply.py` failures on `main`, plus 2 `test_wcs_shift.py` failures
from a newer photutils (`DEFAULT_COLUMNS` no longer in `photutils.segmentation.catalog`). Not P4;
a separate fix.

**P4 status: code complete and live; gates still open.** The S gates need production writes,
so they are the operator's. *Revised 2026-10-27 (P4e, §9a): run after P4e is merged, so the
rehearsal is reversible including its bytes.*
1. Rehearsal release: `campfire dr freeze --tag DR2026.0-rehearsal --nirspec --field …` (or
   `dr build`), `dr status`. No deploy during the freeze (the interlock refuses it anyway).
2. `campfire dr archive --tag DR2026.0-rehearsal --obs <one obs>` first, then the whole release
   (about 353k archive objects), `dr archive --status` until nothing is pending or failed.
3. `campfire dr export --tag DR2026.0-rehearsal`, `dr status` shows ready.
4. Then (agent, read-only): `dr diff` against live, and one file per stream through
   `/api/v1/dr/<tag>/files` with the non-admin test account (sha256 / size / rows).
5. `campfire dr purge DR2026.0-rehearsal` (the sweep deletes its archive objects and export
   files), then `campfire verify --cloud`: no dangling row, no unregistered archive object, the
   `collected` class equal to the sweep's count. Repeat the freeze if P5 needs a release to build
   against (D-P4-4); purge it before the first real `dr archive` in any case, although the
   published-only stamp reuse (D-P4-6) makes the order safe.
The first real release follows the jwst 2.0.1 re-reduction campaign.

**Not built in P4, deferred:** the admin web routes for publish/withdraw (the CLI covers them;
the web side belongs with P6); an `archived` release state in `dr status` (the next-step hint
covers it). Pre-existing, noted: `deploy publish --field` still needs a dummy `--obs`.

**Next: P5** (clients), with the download constraints in §7.

### 2026-10-15 P6a (/releases pages) — PR #609 draft, and the P6 design widened

**#609** (`claude/p6a-release-pages`, branch of main): `/releases` (published only, for admins
too) and `/releases/<tag>` (header; NIRSpec card = spectra · objects · programs · data volume,
photometry catalogs as its footer; NIRCam card = fields · mosaics · data volume; Download table +
`curl`, "schema v1"; Cite), behind `NEXT_PUBLIC_DR_PAGES` (unset / `admin` / `all`); an
unpublished tag renders for admins as "Preview · not published". `/admin/releases`: every state
with the next CLI step; all admin hints live there. Schema: `dr_freeze_job` records
`counts.by_component` ({files, bytes} per component), `nirspec_programs`, `nirspec_fields`
(migration `20261015170000_p6a_dr_release_counts`; guard extended). AGENTS.md gained the web
copy guideline (minimal text, no implementation detail on user-facing pages). Not merged.
Demoed locally (native stack + `next dev`, `DR-seed` + a local-only rehearsal freeze).

**Operator review (2026-10-15) and decisions.** Far less microcopy (logged); admin behaviour
isolated under `/admin`; counts reframed per instrument; "format" → "schema"; photometry is
columns, not a component card; per-program breakdown dropped from the page (the DR catalog
view does it); NIRCam area wanted (D-P6-5). **The big one: D-P6-2, a `?dr=` browsing mode**
for the whole UI except the FitsGL map, accepted at the cost of rewriting the RPC reads and
archiving the field-level products. Decided while no release exists in production, so the
frozen tables can still be dropped and recreated: **P6b (schema, before any real freeze)** —
full-row frozen copies (`dr_objects`, `dr_spectra`, `dr_targets`, `dr_photometry`,
`dr_line_fits`, `dr_nircam_images`, per-release filter options), source views + the
`db_pre_request` header hook, manifest product types for expmaps/renders/layout plots,
`page_md` + `dr/<tag>/page/` (D-P6-3), `nircam_images.area_arcmin2`, the freeze rewritten as
plain `INSERT … SELECT tag, * …`, harness run in both modes against the seed; **P6c (web)** —
RPC `FROM` clauses onto the views, DR context from `?dr=` carried on every link, banner with
"switch to live", mutation UI hidden, files resolved through `dr_manifest`, cutouts live
(D-P6-4), aliases for renamed objects. P5 (clients) is unaffected by the table widening
(exports keep the v1 streams). The release page document replaces the "custom figures" wish.

### 2026-10-15 P6b-1 (frozen tables as full-row copies) — PR opened, stacked on #609

Implemented by a fork agent from the §8 P6b spec, then two independent review agents before the
PR was opened (adversarial SQL/RLS; validation run with PostgREST probes), the process chosen to
avoid the P4 bot-review churn. Branch `claude/p6b-frozen-tables`, three commits + the fix commit;
migration `20261015180000_p6b_frozen_tables` (hand-assembled; fail-loud precondition that
`dr_releases` is empty). Ten frozen tables per the spec table; copy **by name** (spec amended: the
migration chain's physical column order differs from the schema files); parity guard
`check_dr_parity.sql` (column set, types, nullability, and the freeze's INSERT list); live RLS
predicates verbatim + tag visibility; `dr_objects` on the live overlap rule.

Review findings, all fixed before opening: `select_dr_fields` had dropped live's `OR is_admin()`
(admins saw 1/5 frozen fields; readers unaffected); value-equality guards were missing for
targets / line fits / lines / bands (a swapped same-typed pair would have passed CI); nullability
not in the parity guard (`dr_spectra.spectrum_id`, `dr_filter_options.id` now nullable like
live); stale comments. Validation: 0 differing cells across every frozen row of DR-seed; RLS
through PostgREST (anon 401 everywhere; a program made private removes exactly its rows; an
unpublished tag returns nothing to readers, including the release row); refreeze identical by
checksum (only `dr_manifest.updated_at` differs); failure path clean; export replay scoped and
keyset-clean; freeze of the seed ~50–110 ms.

**Documented, not changed (P4a behaviour):** a caller-armed `statement_timeout` (authenticated
8 s) can cancel the inline freeze before its protected block and leave `freezing` (pg_cron path
unaffected); a failed re-freeze of a frozen unpublished tag keeps the previous snapshot under the
`failed` row until purge/re-freeze. Deferred to P6b-2: a photometry component in the seed's
DR-seed (band filters in the equivalence harness); the live indexes without frozen twins
(objects `redshift_quality`; spectra `grating`, `signal_to_noise`, `exposure_time`, `dq_flags`;
targets trgm) once browsing is measurable; a tag-less index on `dr_spectra.observation` for
`dr_observation_references`.

### 2026-10-15 P6b-2 (source views, request hook, RPC switch) — PR opened, stacked on #611

Same process (fork implementer → two fresh reviewers → one fix round → push). Branch
`claude/p6b2-source-views`; migrations `20261015190000_p6b2_source_views` (pg-delta: 23
functions, 10 views, 7 indexes) and `20261015190500_p6b2_pre_request_hook` (hand-authored
`ALTER ROLE authenticator SET pgrst.db_pre_request`). `dr_browse_tag()`, `cat_*` views, 22
switched RPCs, `dr_assert_live()` in the seven sync streams, guards `check_cat_views.sql` and
`check_dr_browse.sql` (40 argument sets × 3 principals, live = DR-seed), `hook_e2e.sh` in CI.
Planner note: the frozen branch needs `dr_browse_tag() IS NOT NULL AND tag = dr_browse_tag()`;
`tag = f()` alone is not pseudo-constant and left `dr_targets` scanned in live mode.

Review findings fixed before opening: the observation overview readers served live matview
data under a tag for non-admins (the harness never saw it: the seed's matview is empty and the
calls were allowed to be empty; now refreshed and compared, with a mutation check that fails on
the pre-fix functions); `get_objects_for_sync` reached frozen aggregates through
`object_scoped_aggregates` (sync streams now refuse under a tag; the guard catches indirect
callers); link accounts passed the hook then saw an empty catalog (PT404 now); the service role
could name `freezing`/`failed` tags (post-freeze states only); `object_band_values` Gate 2 now
honours the frozen policy; three vacuous harness comparisons (`search_text` never populated in
the seed, a line-S/N lookup on an object without the line) and principals that collapsed to two
classes (one program made private inside the harness transaction). Validation: real-mutation
isolation, 500-request pool-bleed test clean, hook overhead ≤ 0.1 ms, planner "never executed"
on the unused branch in both modes under RLS, header matrix 5 principals × 9 variants fail-closed.
Side fix: `member_targets` in `get_filtered_objects_paginated` ordered by `target_id`.

Runbook note: `dr_purge_release` requires an admin JWT or service-role claims; the bare postgres
superuser is refused by `dr_require_admin` (documented on the function).

**P6b-2 Codex round (2026-10-15, #612):** three mixed-source reads the agent reviews and the
harness missed because the seed's live and frozen states coincide: the observation readers took
versions / deploy timestamps from live `deployments`; the overview was driven by live
`observations` (non-member and post-freeze observations listed with zero counts); the NIRCam
field card's `layout_key` came from the live registry. Fixed in `39fe3808`: under a release the
version fields derive from the frozen spectra (most common non-null value per observation,
latest `reduced_at`), deploy timestamp / deployer / patch fields NULL (no frozen deployment
record); the overview is driven by frozen stats; `layout_key` resolves from `dr_manifest`
(NULL until P6b-3 freezes field products). Harness cases insert a later deployment, a new
observation and a newer layout and fail on the pre-fix functions. **Review-brief rule from
now on:** for every RPC switched to `cat_*`, list each live table it still touches under a
release and justify it (config tables `observations` / `programs` / `fields` are the accepted
exceptions; `pointings`, `gratings` fallback and `cycle` still come from them, noted for P6c).

### 2026-10-15 P6b-3 (field products in the manifest, release page document) — PR opened, stacked on #612

Branch `claude/p6b3-page-and-products`; migration `20261015200000_p6b3_page_and_products`
(`page_md`, `dr_page_set`, the freeze's field products gated on a published deployment of the
field, `page_dr` event). CLI `dr page push|show` (`python/campfire/dr_page.py`), `scripts/dr/`.
`by_component.mosaics` separate from `files`. Reviews before opening found the one serious
thing: **field products selected without the deployment gate** (a draft redeploy's renders would
have been frozen into a published release's open partition; reproduced, fixed, guard fixture
fails on the pre-fix predicate), plus upload-before-gate / unconditional overwrite / hash-then-
reread in `dr page push` (conditional PUT, state re-check, sweep, orphan report), `--local`, and
`CAMPFIRE_LOCAL_SUPABASE_URL` for native stacks on random ports. Page rows are spared by
re-freeze and excluded from counts / readiness / exports; `dr_page_set` is allowed on `failed`.
Runbook: a withdrawn release is never purged (by design); the guards force `state='frozen'`
as postgres to clean up test tags.

**P6b-3 Codex round (2026-10-15, #613):** four findings on `dr page push`, answered by a design
change (`83193e87`): page assets are **content-addressed** (`dr/<tag>/page/<sha16>/<name>`,
validated exactly by `dr_page_set`), so nothing ever overwrites or deletes-then-replaces a key
and the push/publish race cannot corrupt a published manifest; `dr_page_set` returns
`removed_keys` and the CLI deletes exactly those (no client-side sweep; concurrent pushes are
safe); a 412 is resolved by reading the object back and hashing it, never by size/ETag;
`--local` without `--dry-run` is refused (shared bucket), as for `dr archive`. D-P6-3 amended
accordingly.

### 2026-10-15 P6c-1 (web browsing mode) — PR opened, stacked on #613

Branch `claude/p6c1-browse-mode` (57 files). Middleware is the only `?dr=` parser (valid tag →
`x-campfire-dr`, client header always dropped, repeated/empty/malformed → 404, `/map` stripped,
flag off → ignored); the cookie client forwards the header and confirms the mode via
`rpc/dr_browse_tag` before the first read; **one source of truth**: the root layout provides the
confirmed tag (`DrProvider`), the client never reads the URL for it; `drPagesAccess` on `?dr=`
URLs (not admitted → 404, never live); `assertLiveRequest()` 409 on every mutation route/action
and on the download paths until P6c-2; file-driven controls omitted under a release; links carry
the tag except map/home/tags/releases/docs/inspect. Two reviews before opening found: the client
honoured `?dr=` with the flag off (banner over live data); parser disagreement on a repeated
param let the object page emit live file preloads under a frozen catalog; `/map` showed the
banner; writes were hidden, not refused (a list-add posted to a `?dr=` URL wrote live); live
reads left (NIRCam row download + layout presign, P(z), provenance from live `deployments`, the
lines route, OG fallback); `/nircam` kept frozen cards after "Live catalog"; `notFound()`
swallowed. All fixed; 575 web tests; three-flag page matrix × four principals; header injection
neutralised (incl. middleware-bypass header tricks); isolation proven with a committed mutation.
Not guarded (not reachable from a release page): admin-area and auth routes.

**P6c-1 Codex round (2026-10-15, #614):** the root layout captured the tag once (stale across
client navigation) → the provider moved to a root `template.tsx` (re-mounts per navigation,
re-reads the stamped header); writes refused centrally (middleware 409 on any non-GET `/api/*`
while a release is stamped, in addition to the action guards); `dr` accepted only on an
allowlist of release-capable paths (`/nirspec` minus tags, `/nircam` minus cutouts, `/releases`,
their API routes); everything else redirects without it; `/api/v1` is never stamped (bearer
gate in P6c-2). Fixed in `f0678141`.

### 2026-10-15 P6c-2 (files from the archive, bearer dr=, page document) — PR opened, stacked on #614

Branch `claude/p6c2-release-files` (72 files). `lib/server/dr-files.ts` resolves live keys to the
release's manifest rows (viewer RLS, or service-role with the policy's program scope) and mints
urls on the archive key only; pending → unavailable, never live. Sidecars, plots, line fits
(`cat_line_fits` + frozen stale flags), downloads (single/bulk/ZIP), NIRCam images/expmaps/
layouts all through it. Bearer routes: `assertBearerReleaseAccess` (readers published; admins
all but draft/freezing/failed; links refused) + mode confirmation on the service client; live-only
routes 400; `/api/v1` outside the middleware decision; bearer `dr` independent of the pages flag
by design. Page document rendered with react-markdown (skipHtml, images from page assets or
https only) via `/api/dr/page-asset`. Guard `check_dr_manifest_scoping.sql`. Reviews found and
fixed before opening: the per-observation manifest route presigned live keys under a release;
redshift-fit ignored `dr`; sync ignored instead of refused; no bearer mode confirmation; gate/hook
state mismatch; HTML 404 on `/api/v1`; layout from the RPC's live fallback; `has_spectrum`;
page-doc image sources; dead component. 620 web tests, 22 guards.

**Remaining for P6:** object-name aliases (schema + reconcile paths + the object page miss path);
the P5 client side of `?dr=` (`campfire pull --dr`) uses the bearer routes as gated here.


### 2026-10-16 Merge-safety check; Codex rounds on #609 and #615

**Is the chain safe to merge before any release exists?** Yes, checked rather than assumed. #609's
pages are gated by `NEXT_PUBLIC_DR_PAGES` (unset in Vercel: `/releases` 404s for everyone, admins
included, and no nav entry; `admin` / `all` widen it; inlined at build, so a flip needs a redeploy);
`/admin/releases` is the only page that always exists. #611's migration raises if `dr_releases` has
a row; production has none (read-only check, 2026-10-16) and no hook / `cat_*` objects yet. #612 is
the one production-wide change: the pre-request hook runs on every Data API request (returns on its
first line without the header) and 22 RPCs read through `cat_*`; EXPLAIN ANALYZE on the local chain
with no tag shows a One-Time Filter per view, the frozen branch "never executed" and the live branch
keeping its indexes. Residual: plan shape at production volume is only checkable after merge (the
views do not exist there); a regression would show in query performance and the switch reverts with
one migration. #613–#615 are inert with the flag off (`?dr=` ignored; middleware additionally drops a
client-sent `x-campfire-dr`). Caveat: a Supabase preview branch seeded before #611 was pushed to it
trips the guard (the seed freezes `DR-seed`); production is unaffected, the check is not required.

**#609 (first Codex review — the PR had been a draft, so none had run).** P1: the public release
page carried a curl command, an API-keys pointer and a `campfire pull` hint against the Copy rule;
P2: the Download table rendered as soon as any ledger row existed (an export in progress read "No
files for your programs" or a partial table); P2: a multi-field release with one field lacking
`coverage_area_arcmin2` reported the other fields' sum as its sky area. Fixed in `c5679850`: the
endpoint URL alone behind a collapsible (the "schema v1" and "DOI pending" asides dropped too), the
table waits for `complete`, the NIRCam totals are null unless every field has the value. The fix was
merged forward through #611 → #612 → #613 → #614 → #615 (merge commits, no rebase; one conflict in
the release page resolved on #615), so the chain still merges bottom-up without conflicts.

**#615 (Codex P2 + an inline finding).** Inline: the NIRCam card's fallback branch wrapped its field
links in the card's own `<Link>` (nested anchors). Codex: under `dr=`, `/api/v1/metadata` offered
every accessible program from the live `programs` table even when the release held none of its
spectra. The design cause: `programs` has no frozen counterpart (config rows, by design) and the
*set* of programs in a release was materialised nowhere, so the same gap sat in `/api/filter-options`
and in the `/nirspec/metadata` programs overview (`get_programs_overview` read the live
`mv_programs_overview` under a release — live data under the banner). Fixed once, in `d34e3379`:
`cat_program_slugs()` (programs with a published spectrum visible to the caller through
`cat_spectra`; live or the release's; STABLE, security invoker, authenticated + service_role);
`get_programs_overview()` aggregates `cat_targets` / `cat_spectra` under a release (`programs` and
`observations` stay the live config rows, as in `get_observations_overview`; `last_reduced_at` is the
newest frozen spectrum; both branches one-time filters, the live call never touches the frozen
tables); both routes intersect the viewer's accessible programs with `cat_program_slugs()` under a
release. Guards: both functions join the switched set in `check_cat_views.sql` (24 now); the
equivalence harness compares the programs overview and the slug list live vs DR-seed for the three
principals — stripped by design: `target_count`, `fields`, `observations` (live counts every target
of the program, a release the targets of its spectra: the known `n_targets` difference),
`n_observations` (config rows vs observations held), `last_reduced_at` (deployment vs frozen
spectrum); and `mv_programs_overview` is refreshed after the harness flips a program private, since
the live path is the matview and the frozen path reads the live `programs` row. Review-brief rule:
`get_programs_overview` still touches `programs`, `observations` (config) and `mv_programs_overview`
(live branch only). Migration `20261016191408_p6c2_release_programs.sql` (pg-delta). 23 guards, 621
web tests, strict drift clean. Reviewed by two agents (adversarial; validation by execution) before
the push — see below.

Review outcome on `d34e3379` (two agents, both also executed against the p6c2 stack): no HIGH.
Validation: both plans prune (live never touches frozen tables, a release never touches live
catalog tables), the three principals get exactly the frozen programs they may see, PostgREST
refuses anon (42501) and unknown tags (PT404), service role sees every program of the release,
live output equals the matview column for column, link accounts get nothing. Adversarial MEDIUM:
`last_reduced_at` is null for most programs under a release (it is the release's newest spectrum's
header time, the P6b-2 rule for the observation readers; most of the archive lacks `CMPFRTIM`),
and the harness stripped the column — the semantics are kept (consistent with the observation
readers; no frozen deployments record exists), but the metadata page's recency filter, which that
null would have emptied, is omitted under a release and ignored from the URL. LOW: no guard proved
the overview's live arm gated — a live-edit case (move a target, refresh the matview, the reader's
DR-seed count must stay frozen) added, as for the observation overviews. Noted, not changed:
`getProgramDetail` reads the whole overview for one row (an aggregation under a release); picker
PIDs from live observations vs the overview's frozen-target observations. Fix commit `d671f666`
pushed with the round; Codex threads on #609 and #615 answered. #615's earlier red CI was an npm
`ECONNRESET` during install; the rerun is green.

**2026-10-16 19:49 UTC — #609 merged** (user's instruction; merge commit `1ac51ca2`; head-commit review
state checked first: claude-review ran on `c5679850` with nothing new, Codex had not re-run on the head).
Branch deleted so #611 retargets `main`. Production change: the `dr_freeze_job` counts migration only;
the pages stay dark until `NEXT_PUBLIC_DR_PAGES` is set.
Trap hit: deleting the merged `claude/p6a-release-pages` branch did not retarget #611, it CLOSED it (a
closed PR cannot change base; `gh pr edit --base` fails on this repo with a GraphQL projectCards error).
Recovered by re-pushing the branch at `c5679850`, reopening #611 and retargeting it to `main` through
the REST API, then deleting the branch again. Rule for the rest of the chain: PATCH the child's base to
`main` first, delete the merged branch second.

**2026-10-16 — #611 merged by the user** (merge commit `2b103b2c`, branch deleted from the UI, which did
retarget #612 to `main` — the API deletion earlier did not). Frozen tables land in production empty; the
migration's guard (no `dr_releases` rows) held.

**2026-10-16 — #612 merged by the user; production regression found and hotfixed.** Hook verified
live (authenticator carries `pgrst.db_pre_request`; a request with `x-campfire-dr: nope` is PT404; anon
stays 42501). Timings as the admin (read-only): `cat_spectra` count 80 ms, `cat_objects ⋈ cat_targets`
125 ms (pruning holds), but `get_filtered_spectra_paginated` 1.0 s (history 0.17–0.55 s),
`get_filtered_objects_paginated` 4.8 s (history 0.3–1.8 s) and `get_observations_overview` (admin
path) > 20 s (history 0.43 s; 8 s through PostgREST, so the metadata page failed for admins). Cause,
verified with EXPLAIN ANALYZE in production: PostgreSQL does not flatten UNION ALL arms that carry a
WHERE (`is_safe_append_member`), so each `cat_*` arm stayed a Subquery Scan that a join cannot
parameterize — every join through a view became a hash join over the whole visible table (a 50-row
join: 46 ms through the view, 0.3 ms on the table). Local plans never showed it because the seed is
tiny and hash joins win there anyway. Hotfix PR `claude/cat-views-flatten` (`9f6e3940`): the switch
becomes a qual on an outer subquery over a WHERE-less UNION ALL, each arm projecting `cat_src`
('live'/'dr') and `cat_tag` (''/tag); pushed into the flattened arms it is a One-Time Filter on both
plus the tag index condition on the frozen arm, and the same 50-row join plans in production as a
nested loop with index lookups (0.6 ms). Columns, grants, RLS unchanged; `check_cat_views.sql` §5
pins the plan shape (no Subquery Scan; unselected arm never executed, live and under a tag) and fails
on the previous definition. Lesson for the review brief: **plan-shape claims must be checked at
production volume with a join, not with a single-table count on the seed.**
Hotfix #616 merged (user's instruction; merge commit `25977664`; Codex ran on the head with no findings;
database + python CI green). Post-merge production timings follow below.
Post-#616 timings (admin, read-only): spectra list 1.0 → 0.47 s, objects list 4.8 → 0.20 s, database
overview 14 ms; the 50-row join through `cat_spectra` plans as a nested loop (0.6 ms). **Second cause**,
`get_observations_overview` admin path still > 20 s: the FROM-less `frozen_dep` LATERAL's `WHERE
dr_browse_tag() IS NOT NULL` is pulled up into a join filter, so its four correlated subqueries ran per
observation in live mode too (≈900 full scans of spectra per call). Hotfix 2 (`claude/frozen-dep-gate`):
each column `CASE WHEN dr_browse_tag() IS NOT NULL THEN (subquery) END` in both observation readers;
production EXPLAIN ANALYZE of the live query: 0.57 s. Guard: `check_cat_views.sql` §2 rejects a FROM-less
lateral gated only by the tag test. Review brief rule added: **for every `dr_browse_tag()` gate, state
which plan node evaluates it (One-Time Filter on a scan, or a join filter), and EXPLAIN ANALYZE the
RPC's live query at production volume.**
Hotfix 2 = PR #617, merged (user's instruction; merge commit `01e8acad`; Codex no findings; CI green).
Post-#617 production timings (admin, read-only, same probe): `cat_spectra` count 88 ms,
`cat_objects ⋈ cat_targets` 246 ms, spectra list 0.49 s, objects list 0.20 s, database overview 15 ms,
observations overview (admin live path) 0.91 s — back to the September audit's 1.0–1.15 s for that path.
Regression closed; #613 → #614 → #615 remain, all based on `main` by retarget.

**2026-10-16 — user authorised merging #613 → #614 → #615 in order with production checks between
each.** #613 merged (`6ec52f86`; CI green on the head, the one unanswered thread was already fixed in
83193e87 and is now answered); #614 retargeted to `main` by REST PATCH before the branch deletion.
#613 verified in production (page_md column, dr_page_set present; zero releases / manifest rows; hook
probes and timings unchanged). #614 merged (`2d4e3a3c`); #615 retargeted to `main` by REST PATCH.
#614 verified in production (Vercel deployment 20:48 UTC; `/`, `/nirspec`, `/nircam` 200, `?dr=` and a
client `x-campfire-dr` header inert with the flag unset, `/releases` 404, `/api/v1/version` 200, RPC
timings unchanged). #615 merged next.
#615 merged (`fb5ba651`). Production after its migration: `get_programs_overview()` 16 ms live (matview
arm; frozen arm never executed), `cat_program_slugs()` 116 ms, list RPCs unchanged (objects 0.18 s,
spectra 0.46 s, observations overview admin 0.58 s), hook probes unchanged. Web deployment pending at
the time of writing; probed below.
#615 web verified in production (Vercel deployment 20:58 UTC): `/`, `/nirspec`, `/nircam` 200; `?dr=`
and a client `x-campfire-dr` header inert with the flag unset; `/releases` 404; `/api/v1/version` 200;
bearer `dr=` without auth 401. **The whole P6 chain (#609, #611–#615) plus hotfixes #616, #617 is on
`main` and in production; nothing is user-visible until `NEXT_PUBLIC_DR_PAGES` is set.** Next: the
operator's rehearsal freeze (`dr freeze` → `dr status`; purge available while unpublished), then
`NEXT_PUBLIC_DR_PAGES=admin` on Vercel (needs a redeploy) to look at it; object-name aliases; P5 clients.

### 2026-10-27 Review with the operator: the archive is exactly the releases (P4e), aliases dropped

No freeze has been run; nothing DR-related has landed since 2026-10-16 (the self-hosting cutover
and the catalog read model, #650–#662, took the interval). The operator's questions, in order:
is the rehearsal safe (yes for the frozen rows and exports, purge reverts them; no for the archive
bytes, which were never deletable and whose `CMPFRDR` stamps a later release would reuse), what
retain-on-supersede in `enforce` means (copy the old live bytes to the archive before every
overwrite, forever), and whether aliases are needed (no). Decisions D-P2-4, D-P4-6, D-P4-7,
D-P6-6 recorded in §12; the plan is §9a; the design doc carries dated amendments in §0, §4.2,
§4.6, §4.8, §7 and §8. The downstream trace in §9a covers both retain implementations and their
tests, the presign route, `dr purge`, `archive gc`, the stamp-reuse join, the P2 trigger (unchanged,
it is the mechanism), the budget classes (unchanged), the sync stream (archive rows never reach
it), `verify --cloud` (one item to confirm: collected rows out of the danglable set), the env files
and the docs. Next: PR 1 (gate), PR 2 (sweep + collection + reuse restriction), then the rehearsal
runbook above, revised to end in a purge.

### 2026-10-27 P4e: PR 1 merged and deployed; PR 2 built

**PR 1 (#663, merged 0a2d3a67, deployed 01:13 UTC, migration `20261028005618` applied, health ok):**
`retain_required_hashes(text[])` + the partial index `idx_dr_manifest_unarchived_hash` (the function's
predicate is the explicit `IN ('pending', 'failed')` so the index serves it: the local Codex pass caught
`NOT IN ('copied', 'stamped')`, which the planner cannot match to the partial predicate; verified Index Only
Scan), both gates narrowed and the modes retired, guard `check_retain_gate.sql`. Remote Codex: no findings.

**PR 2 (`claude/p4e-archive-sweep`):** `archive_object_references(key, archived_from_key, content_hash)`
(service_role-only helper; the two ways a manifest names an object), `archive_collectable(after_id, limit)`
(keyset page of unreferenced objects), `dr_purge_archive_plan(tag)` (only the tag's objects; refuses
published / withdrawn, waits for a live archive claim), `dr_collect_archive(keys, tag)` (archived →
collected, refuses a key another tag references, counts an already collected key, one `collect` event);
`dr_bytes_pending` reuses a stamp only from a published or withdrawn release; indexes on
`dr_manifest (archive_key)` and `(storage_key, content_hash)`. Python: `campfire.archive` is
reference-based (`gc_candidates` → `archive_collectable`, `delete_archive_objects`, `collect`), `archive gc`
collects after a confirmation (`--dry-run`, `--yes`, `--local` lists only), `dr purge` sweeps first
(plan → delete → collect → rows → files; `--keep-archive`; `--local` touches no bytes), `verify --cloud`
leaves `collected` rows out of the danglable set (`registry_keys` pushes `status <> collected`). Guards:
`check_archive_collect.sql` (new), `check_dr_bytes.sql` (published-only reuse, an unpublished stamp never
offered, a withdrawn one still is).

**PR 2 review rounds (2026-10-27/28).** Four local Codex passes on the sweep, nine findings, every one a
concurrency hole in a delete path and every one closed in the design (§9a "The claim"): the stale plan,
re-archiving after collection, the 1000-row cap, publication between claim and delete (`purging`), two
collectors on one key (`collect_claim`, takeover after 10 minutes, `verify` before every delete, re-verify
after a stall), a registration after a collection finished (register-first gate; `page_at` + tombstone for
the byte pass, heals and fresh copies alike). The last two rounds each found a narrower variant of the
round before; stopped there, by the rule in the review-loop memory, with the residuals written down.
Remote Codex review requested on the PR.

### 2026-10-28 P4e: PR 2 merged and deployed

**PR 2 (#664, merged b39d195e by the operator, deployed 10:09 UTC, migration `20261028020751` applied, health
ok).** Remote Codex left one P1 on the head (d57824d0): the bucket delete cannot be fenced against an expired
claim. That is the documented residual of §9a "The claim" (OSN has no conditional delete, versioning or object
lock; the verify-to-delete window is under a minute against the 10-minute takeover, and the interleaving needs
the process suspended for over 10 minutes between the clock check and the HTTP send); answered on the thread,
accepted as is. P4e is complete. The rehearsal (§13 runbook, revised 2026-10-27) is next, in the operator's
hands: `dr freeze` → `dr archive --obs <one>` → `dr archive` → `dr export` → read-only checks → `dr purge
<tag>` (sweeps the archive objects) → `verify --cloud`. Nothing is frozen in production yet.

### 2026-10-28 The production rehearsal: freeze → staged archive → export → checks → purge. Reversible.

Run by the operator from a laptop on the Princeton network through an SSH SOCKS tunnel to the VM
(`ssh -N -D 1080 campfire-prod`, `ALL_PROXY=socks5h://127.0.0.1:1080`; the campus firewall resets
TLS to `campfire-jwst.org` for non-browser clients and sinkholes `hollisakins.com`; the CLI needs
`PySocks` for requests/boto3 and `socksio` for httpx). Read-only checks by the agent through the same tunnel.

1. **Freeze #1 failed** on its integrity check: `capers_uds_p3_prism_clear_126973` had been reprocessed in
   place on 2026-10-16 and redeployed without regenerating `capers_uds_p3_summary.ecsv`, so
   `spectra.file_hash` / `science_hash` described the old FITS while the registry and the bucket held the
   new one. Fix: `cfpipe nirspec summary --obs capers_uds_p3` + `campfire deploy --obs capers_uds_p3`.
   Issue #665: check summary hashes against the FITS at deploy time. The pg_cron path on the self-hosted
   stack works (first freeze ever there).
2. **Freeze #2:** `DR2026.0-rehearsal`, nirspec only, 81,420 spectra · 43,113 objects · 16,330 line fits ·
   358,244 manifest files, **31.6 s**.
3. **Staged archive** `--obs go2565_uds_p1`: 304 members (76 stamped, 228 copied), 0.09 GB, 36 s through
   the tunnel, no failures. Verified read-only: manifest rows all `stamped` / `copied`, stamped finals keyed
   under `stamped_hash`, sidecars under `content_hash`; 304 `archive` / `archived` registry rows with
   `archived_from_key` = the live key; an archive copy read back carries `CMPFRDR = DR2026.0-rehearsal`
   with the frozen science hash; the live FITS byte-identical to the freeze, unstamped. Claim released.
   **The full pass was skipped on purpose** (102 GB, ~12 h through the tunnel; the first real release runs
   it): untested at scale are the byte pass, the purge sweep over 358k objects and a multi-hour run's
   claim heartbeats.
4. **Export:** 133 files; one (`line_fits/jades`) failed with `Server disconnected` on the first run and
   built on the rerun (issue #666: retry transient transport errors before marking a file failed; also make
   the next-step hint say the byte pass is partial). `dr diff` vs live: no differences. `/api/v1/dr/<tag>/files`
   as the admin: 133 files, one per stream downloaded and matched on sha256 / size / rows. Not tested: the
   non-admin view (no non-admin production token on the machine).
5. **Purge:** 304 claimed → 304 deleted → 304 rows removed → frozen rows → 133 export files, state `purging`
   on the way. Verified read-only: no `dr_releases` / `dr_manifest` rows for the tag, no `archive`-root
   registry rows, nothing under `archive/` or `dr/DR2026.0-rehearsal/` in the bucket, the 304 live rows
   under `go2565_uds_p1` still `active`, the live final's bytes = registry hash with no `CMPFRDR`,
   `campfire archive gc --dry-run` empty, `dr status` back to "none frozen yet".

**Verdict:** every step of the lifecycle except publish works in production and the rehearsal left no
trace. Follow-ups: #665, #666; P5 clients with the `campfire_reader` role; the pages flag; P7.

### 2026-10-28 Rehearsal follow-ups (#665, #666)

Branch `claude/rehearsal-followups`. **#665:** `stale_summary_rows(summary, obs_dir)` in `deploy/summary.py`
hashes every final the summary names and `deploy_obs` refuses (dry run included) with the rows, both hashes,
the file's mtime and the fix (`cfpipe nirspec summary --obs X`). No override: the summary is cheap to regenerate
and the catalog row must describe the file. **#666:** `campfire/transient.py` (`is_transient`, `retry_transient`:
httpx `TransportError`, requests `ConnectionError` / `Timeout`, botocore connection / timeout classes, the stdlib
reset / pipe / timeout errors, and S3 `SlowDown` / `InternalError` / 5xx; three attempts, 1 s / 2 s backoff,
logged) wrapped around the export's page fetch and upload, the byte pass's whole member (idempotent: HEAD-verify,
conditional PUT, bound copy; `_process_member` now raises on a transport error and `process_member` classifies),
and the collection's claim / verify / delete / finish. A delete whose answer was lost is re-sent and finds the
keys gone. The next-step hint prints the pending manifest count and whether the exports are built. Local Codex pass: a delete re-sent after a dropped connection must go back through
the verify gate, not reuse the stale key list (the claim may have expired and another run taken the keys over
during the stall); fixed, with a takeover-during-retry test. Second pass clean. PR #667. Remote Codex: three
rounds, each "retry these calls too" (the byte pass's page / mark / summary, the export's ledger / begin / status, the
purge plan and gc candidate pages), each a one-line wrap of an idempotent read plus a test; done, and the AGENTS.md
sentence now says *every idempotent request* and names the two deliberate exceptions (a new archive claim, the export's
per-file marks). Stopped re-requesting after the third round (review-loop rule). CI green on 2a90c0b4.

### 2026-10-28 Phase 4 (reader role) + P5 (clients): implementation plan

Agreed order with the operator: the catalog read model's phase 4 and P5 together, four PRs, each
independently mergeable, none touching production rows.

**PR A, phase 4: `campfire_reader`.** A LOGIN role with SELECT on the catalog and the published
releases and nothing else, its own RLS policies with pure column predicates (no `auth.uid()`, no
helper functions): `objects` (`is_public AND is_active AND has_published_spectrum`), `targets`
(`is_public AND has_published_spectrum`), `spectra` (`is_public AND deploy_status = 'published'`),
`lines` / `line_fits` (EXISTS the reader-visible spectrum), `catalog_matches` (EXISTS the visible
object), `photometry` (EXISTS the visible match), `programs` (`is_public`), `observations` (program
public), `nircam_images` (`published`), `fields` (has a published image); `dr_releases` (`published`)
and every `dr_*` twin by published tag plus the frozen `is_public` / published / active columns, the
manifest by public program or open component. Role settings: `statement_timeout 30s`,
`idle_in_transaction_session_timeout 60s`, `default_transaction_read_only on`, `search_path public`,
`CONNECTION LIMIT 20`; no EXECUTE on any function. Roles are not declarative: `supabase/roles.sql`
creates it for the local stack and the shadow database, a hand-authored migration creates it in
production (idempotent DO block, no password: the operator sets one), and the grants and policies are
declarative with a generated migration. Guard `check_reader_role.sql`: the access-matrix fixture plus
a draft spectrum, an inactive object and the seed's `DR-seed`; as `campfire_reader` every granted
table returns the public rows and zero private ones, every other table and every function is
permission denied, writes fail, the grant set equals the allowlist, the Figure 5 query runs. Ops:
a `pgbouncer` service (transaction pooling, SCRAM userlist holding the reader only, TLS with a
self-signed cert so clients use `sslmode=require`; `verify-full` later with a public cert) on
0.0.0.0:6432, `ops/scripts/reader-setup.sh` (password, userlist, restart), README section with the
operator steps (security group 6432, a DNS-only `db.campfire-jwst.org` record, since Cloudflare's
proxy carries no Postgres), `docs/reader-role.md` with the connection string and pandas / DuckDB /
psql examples. A per-query row limit is not a Postgres feature; the timeout and `work_mem` bound a
query instead (noted in the doc).

**PR B, P5-1: the DR store and `Campfire(dr=)`.** `campfire/dr_store.py`: a release lives under
`$CAMPFIRE_ROOT/dr/<tag>/` (`exports/<stream>/<scope>.jsonl.gz` kept, `meta/campfire.db` a second
`LocalStore` over the same DDL, `release.json` with the files route's answer and `loaded_at`,
`products/` for the bytes). `load_release(session, tag)` fetches the files the caller may read,
verifies sha256 / size / rows, loads them into the store (objects deduplicated by id across the
reader's per-program files: `programs` / `observations` / `gratings` unioned, counts taken as max),
never purges, and rebuilds from the kept files on a schema bump. `Campfire(dr=...)` opens that store,
makes the API session lazy, raises `DRModeError` on every remote branch, stamps
`table.meta['channel']`, defaults `query_lines(exclude_stale=True)`, reads bytes from the DR tree.

**PR C, P5-2: `pull --dr`, `dr list`, `identify`.** `campfire pull --dr TAG [--obs/--program]`:
load (PR B), then the bytes through the existing release-aware presign (`POST /storage/presign?dr=`)
into `dr/<tag>/products/`, verified against the manifest (`stamped_hash` for finals, `content_hash`
otherwise); resumable through the DR store's `storage_objects`. `GET /api/v1/dr` lists the releases
the caller may read (`campfire dr list`). `campfire identify FILE...`: sha256 (+ science hash when the
pipeline is installed) looked up in the live and DR stores, then `GET /api/v1/identify` backed by an
RPC with explicit scope over `spectra`, `storage_objects` (live + archive) and `dr_manifest`.

**PR D, P5-3: `cite`, `provenance`, the sync tail.** `campfire cite TAG` (DOI + the reader's
programs), `campfire provenance [check]` (`paper.campfire.json` from the local ledgers, hashes
verified), `latest_dr` + an access fingerprint on `/api/v1/auth/whoami`, `campfire sync` prints
"DR… published; you are on live" / "your access changed; run sync --full"; client 0.7.0, docs.

### 2026-10-28 PR A (phase 4, `campfire_reader`) built

Branch `claude/phase4-reader-role`. Role in `supabase/roles.sql` (local + shadow; the CLI applies it before the
chain) and `20261028193000_phase4_reader_role.sql` (production); grants in `tables.sql`, 23 `reader_select_*`
policies in `policies.sql`, generated migration `20261028193250_phase4_reader_grants_policies.sql`; guard
`check_reader_role.sql` (5 sections); pooler service under the compose profile `reader`; `reader-setup.sh`;
`docs/reader-role.md`; AGENTS.md. Lessons: `CREATE ROLE … ADMIN postgres` is refused when postgres itself runs
it ("cannot be granted back to your own grantor"), so `roles.sql` grants postgres ADMIN only when another role
ran it, and the guard grants itself SET on the role inside its rolled-back transaction; `temp_file_limit` is
superuser-only as a role setting; `db reset` lost the native stack again (applied with psql -f, migration rows
inserted by hand); the drift gate works with roles.sql in the shadow. All 34 guards green, RLS harness green.
Local Codex: two P1s, both real and both fixed: (1) any role may change its own password and the read-only
default is lifted per transaction, so a holder of the public credential can desync the pooler's server-side
login: `reader-heal.sh` + a 2-minute timer re-apply the password from `.env` and HUP the pooler, and the
stronger pg_hba fix is written down for the operator; (2) transaction pooling leaked session state (a temp
`objects` shadowing the table for the next reader): `SERVER_RESET_QUERY_ALWAYS = 1` with `DISCARD ALL`. Third local pass (after the
`load_env` fix): (3) the objects tables' member aggregates count private members (the design excluded them
from the query whitelist for that reason): `reader.objects` / `reader.dr_objects` views compute them over
public published members (stored when every member is visible, as `object_scoped_aggregates`), the role's
`search_path` is `reader, public`, and the tables are granted column by column without the aggregates
(`search_text` and, on the twin, `evidence_programs` / `z_source_spectrum_id` included; pg-delta models
column grants); (4) the pooler's files moved to a world-traversable `pgbouncer/` directory of their own
(`certs/` is 0750); (5) the heal also reconciles the role settings (a role may `ALTER ROLE … SET` its own
defaults) and ends the role's sessions. Guard: a mixed object (one public, one private member) reads with
the public program only; `SELECT *` on `public.objects` refused, `objects` (the view) works. Fourth pass: `security_barrier` on both views (a reader can define a
temp function), the stored-aggregate shortcut also requires every target visible (a public target without
a published spectrum is hidden by the targets policy but counted by `build_objects`), and the heal resets
`ALTER ROLE … IN DATABASE … SET` overrides too (`pg_db_role_setting`, not `rolconfig`). Remote Codex on #668: the freeze copies
only the published spectra but the live aggregates verbatim, so a draft member made the twin's stored
shortcut wrong: the shortcut now also requires the stored counts to equal the frozen members' (as
`object_scoped_aggregates`); the guard asserts on both views that every visible object's `n_spectra`,
`n_targets` and `gratings` describe exactly its reader-visible members. Second remote round: the manifest policy followed the live program flag
while `dr_spectra` follows the frozen one: a NIRSpec manifest row is now visible exactly when its frozen
spectrum is (field products open, program-scoped rows without a spectrum hidden); `dr_filter_options`
withdrawn from the reader (arrays over every frozen member, not scopable row by row). Guard: hiding one
frozen spectrum hides its manifest rows; filter options denied.

### 2026-10-29 PR A merged + operator setup; PR B (P5-1) built

PR A merged as #668 (29903ead) and deployed; operator setup done the same day (reader-setup.sh, security
group, DNS-only `db.campfire-jwst.org`, heal timer), verified from the VM over the public IP. #669 then
made the string passwordless on the default port (`postgresql://campfire_reader@db.campfire-jwst.org/postgres`:
pgbouncer `auth_type = trust` over a one-name userlist, `READER_PASSWORD` the pooler's own db login,
host port 5432 bound to the VM's interface address since db publishes 127.0.0.1:5432). TAP (DaCHS over
the reader role) discussed and deferred to after the first real release. The campus firewall resets the
TLS handshake to the pooler as it does on 443.

PR B, branch `claude/p5-dr-store`: `campfire/dr_store.py` (`load_release`, `release_dir`,
`export_filename` = the server's key rule, `read_rows`, `open_store`, `read_release_json`;
`ReleaseLoadReport`), `DRModeError` / `DRLoadError`, `Campfire(dr=)` (`_NoRemote` in place of the API
client, `_open_release`, `dr` / `release` properties, `_stamp` on every table, metadata getters from the
store, `query_lines(exclude_stale=None)` → True on a release, `open_spectrum` refuses the API path on a
release, `sync` / `download` refused), tests `test_dr_store.py` (12) and `test_dr_mode.py` (17), README
section, dr-format-v1.md "Local store", AGENTS.md paragraph. Decisions: the store is rebuilt (never
patched) from the kept files when the file set or SCHEMA_VERSION changes, built beside the old one and
swapped so a failed load leaves the previous store; `storage_objects.content_hash` in the release store is
the hash of the bytes the reader gets (stamped copy when stamped); the manifest rows are kept whole in a
release-only `release_manifest` table instead of widening the live mirror's schema (no forced resync);
objects' aggregates recomputed from the spectra held rather than max over the per-program copies.

