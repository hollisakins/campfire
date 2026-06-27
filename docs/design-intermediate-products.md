# Design: Intermediate products & cloud-as-source-of-truth

**Status:** draft for review
**Date:** 2026-06-27
**Context:** migration of CAMPFIRE object storage from Cloudflare R2 → NSF Open
Storage Network (OSN, S3-compatible, 20 TB, upgradeable); related
[NIRCam exposure-major design](design-nircam-exposure-major.md),
[objects migration](design-objects-migration.md).
**Driver:** Today `campfire deploy` is a one-way push of *final* products from
a local `$CAMPFIRE_ROOT/products/` tree to R2 + Supabase. The filesystem is the
source of truth; presence-on-disk == published; there is no in-prep tier, no
publish/revoke lifecycle, and nothing in the DB knows what is actually in the
bucket. This design shifts the architecture so that **the cloud object store is
the system of record, the database is its index and lifecycle controller, and
the web portal and `campfire` CLI are both clients of that index** — and it
extends deployment to cover *all intermediate products*, not just finals.

---

## 1. Goals / non-goals

**Goals**

- Deploy **all intermediate products**, not just finals: NIRCam canonical
  exposures and NIRSpec canonical spectrum-exposures (see §3), at whatever
  reduction stage they have reached — including before a reduction is finished.
- An **admin-only web view** of intermediate products and in-prep reductions,
  extending the existing NIRCam exposure triage UI.
- **Cloud as source of truth**: the DB enumerates every object in storage
  (key, hash, size, type, stage, status, provenance), enabling
  `reduce → deploy → delete local → recover later` and multi-reducer
  coordination.
- A **deployment lifecycle**: `in_prep → published`, plus soft `revoked` and
  `superseded`, controllable from an admin panel and the CLI. Revocation is
  recoverable, unlike today's hard-delete `remove`.
- **Migrate storage R2 → OSN** with checksum-verified copy and no user-visible
  downtime.
- Do all of the above **incrementally**, each phase independently valuable and
  non-breaking for the live portal.

**Non-goals (this design)**

- No change to the *final* `_spec.fits` / mosaic science outputs. The NIRSpec
  prerequisite refactor (§3) is explicitly **bit-identical** for finals.
- No public access to intermediate products. Intermediates are admin-only for
  now; the lifecycle gates *admin-vs-not*, not fine-grained external sharing.
- No new sub-admin role taxonomy (separate "reviewer" vs "publisher"
  authority). Single `is_admin` remains the lever; finer roles are a later add.
- No change to the objects-clustering / inspection-state model
  (`reconcile.py`); intermediate products are deliberately kept off that path.

---

## 2. Core design decisions

### 2.1 The cloud is the system of record; the DB indexes it

The keystone is a new **storage-object registry** table (`storage_objects`,
§5.1). Today object keys are bare convention-built strings on domain rows
(`spectra.fits_path`, `nircam_images.file_path`, `nircam_exposures.png_path`)
or not stored at all (SED PDFs, RGB PNGs — the web reconstructs them from
`obs_name + filename`). You cannot treat the cloud as source of truth if the DB
cannot enumerate what is in the cloud. The registry is the join point for
sync/recover, the OSN copy-and-verify, storage budgeting, and lifecycle.

### 2.2 The unit of an "intermediate product"

The 20 TB budget forces a deliberate answer. **NIRCam dominates** storage; a
COSMOS-Web-scale field is ~5,000 exposures × ~150 MB ≈ 750 GB for a *single*
state, so snapshotting after each of ~15 steps (~10 TB/field) is a non-starter.

- **NIRCam:** the unit is **the canonical exposure file + its `CFP_*` state
  vector** — one object per exposure, re-uploaded only when its content hash
  changes. This already exists on disk (one `<rootname>.fits` mutated in place;
  `cfp.py`). Gives "every exposure at whatever stage it reached" with no
  explosion, and enables queries like *"all exposures reduced through JHAT in
  COSMOS."*
- **NIRSpec:** the unit is **the canonical spectrum-exposure file**, granularity
  `(exposure × detector × source)`, plus the separate per-`(exposure, detector)`
  `_rate` tier (shared across sources, cannot be folded in). This **does not
  exist yet** — today the same logical thing is split across four files
  (`_cal`, `_cal_bkgsub`, `_s2d`, `_s2d_bkgsub`). It is created by the
  prerequisite refactor **PR-3** (§3).

**Symmetry principle:** after PR-3, both instruments use *canonical file +
state-keyword chain*, each backed by an admin-only intermediate-products table
(`nircam_exposures`, new `spectrum_exposures`) that hangs off the published
science row. Every downstream layer (registry, deploy, web view, sync) then
treats the two instruments uniformly.

### 2.3 Lifecycle: decouple "upload bytes" from "make visible"

Deploy splits into **(a) upload + register** (write bytes to storage, record a
`storage_objects` row, attach to a science/intermediate row with
`status = in_prep`) and **(b) publish** (flip `in_prep → published`). In-prep
rows are visible to admins only, enforced in **RLS** (the real boundary in this
codebase — everything routes through `accessible_program_slugs()` + `is_admin()`).
`revoked` hides a published row but keeps the bytes (recoverable);
`superseded` marks an object replaced by a newer hash. This is a generalization
of the `nircam_exposures.review_status` model that already exists.

---

## 3. Prerequisite refactors

These are independently valuable refactors that de-risk and simplify the main
work. The litmus test for "prerequisite" is that each pays for itself even if
the rest of this design never shipped.

### PR-1: Storage backend abstraction (S3 endpoint/region config + factory)

**Why:** the storage layer is already 95% generic S3 (boto3 +
`@aws-sdk/client-s3`, `region:'auto'`, `s3v4`). The only Cloudflare lock-ins are
(1) the endpoint string `https://{account_id}.r2.cloudflarestorage.com`
**hardcoded in 4 places** (`python/campfire/deploy/r2.py` ×2, web
`app/api/v1/deploy/presign/route.ts`, `web/lib/r2.ts`); (2) the download Worker's
`R2Bucket` binding; (3) public-URL tile/RGB serving + edge caching.

**Change:**
- Introduce explicit `CAMPFIRE_S3_ENDPOINT` / `S3_ENDPOINT` (+ region) config
  keys in `python/campfire/deploy/config.py` `_ENV_VARS` and the web env
  contract; replace the interpolated hostname with the configured endpoint.
- Add a thin **backend factory** in each language (one place builds the S3
  client) so a backend swap is a config change, not a code edit at N call sites.
- Neutral naming/aliases (`storage`/`s3` rather than `r2`) — keep `r2`/`R2_*`
  as accepted aliases to avoid a breaking rename.
- Leave the download Worker and tile public-serving rework to Phase 2 (they are
  the genuinely hard parts and are OSN-cutover-scoped).

**Independently valuable:** config hygiene; removes 4-way endpoint duplication.

### PR-2: Canonical object-key module (record, don't reconstruct)

**Why:** object keys are built ad hoc in `deploy.py`, `summary.py`,
`nircam.py`, `photometry.py`, `tiles.py` and *mirrored* in `web/lib/r2.ts`. SED
PDF and RGB keys aren't stored anywhere — the web reconstructs them from
`obs_name + filename`. As product types multiply (intermediates), this drift
becomes untenable, and the registry (§5.1) needs a single authority for keys.

**Change:**
- One module that builds **every** object key from typed inputs
  (`product_type`, scope, filename), shared in spirit across python and web
  (Python module + a mirrored TS module with a single conformance test that
  asserts they agree on a fixed set of cases).
- Deploy **records** the key it used (into `storage_objects`, Phase 1) instead
  of relying on reconstruction.

**Independently valuable:** kills the "reconstruct from convention" fragility
that already exists for SED/RGB; one place to audit the key namespace.

### PR-3: NIRSpec canonical spectrum-exposure refactor

**Why:** NIRSpec's intermediate products are split across four files at one
granularity — `{root}_{nod}_{detector}_{source_id}_{cal|cal_bkgsub|s2d|s2d_bkgsub}.fits`
(stage2.py:700, 899, 1131, 1139). Consolidating them into one **canonical
spectrum-exposure file** (4→1) gives NIRSpec the same self-documenting
canonical-file model NIRCam already has, drops object count 4×, and removes
basename-collision handling from sync. Without it, the registry/web/sync layers
bake the four-suffix mess in permanently.

**Target file model:** anchor on the `_cal` **`MultiSlitModel`** (standard jwst).
The other states become extensions/mutations of it:

- **Background subtraction in place** (eliminates `_cal_bkgsub`): the live SCI
  becomes the bkgsub'd data; the pre-bkgsub state is recoverable from extra
  extensions. This mirrors the *existing* `_rate` pattern exactly —
  `CFBKGSUB`/`CFBKGRMS`/`CFBKGDT` sentinels + `CFBKG`/`CFBKGMASK` extensions +
  `restore_pre_bkgsub()` (stage1.py:789-793, masks.py:260-308).
- **s2d as extensions** (eliminates standalone `_s2d`/`_s2d_bkgsub`): s2d is
  visualization-only (created only when `plot`/stuck-shutter/`rectify`), and
  every consumer already reads it via **astropy** (`s2d['SCI'].data`,
  stage3.py:339-341; `stuck_shutters`), not as a jwst datamodel. So both
  rectified states live as named HDUs (`S2D_SCI`, `S2D_BKGSUB_SCI`, …).

**Hard invariant — jwst `DataModel.save()` drops non-schema HDUs.** This is the
same constraint the NIRCam canonical model already lives with (design-nircam,
audit M7). Every mutating step must: run the jwst step → `MultiSlitModel.save()`
→ reopen with astropy → (re)append the revert + s2d extensions → stamp the state
keyword. This is precisely the `_rate` pattern (Detector1Pipeline saves, then
`subtract_background_from_rate_file` appends `CFBKG` via astropy).

**State chain (mirrors `cfp.py`, kept as a separate module — see §10 decision).**
NIRSpec gains its own ordered keyword chain (e.g. `CFP_CAL → CFP_MASK →
CFP_BKG → CFP_S2D`), because once the four files collapse to one, file-existence
can no longer encode "how far has this been reduced." `CFBKGSUB` already proves
keyword-driven skip works for NIRSpec; this extends it to the whole chain. The
module is a *mirror* of `nircam/.../cfp.py` (same `has_step`/`should_skip`/
`clear_from` shape, separate key definitions), per the alignment-not-coupling
decision.

**Behavior-preservation traps (must replicate exactly):**

1. **Stage3 input selection.** Stage3 today discovers `ext='cal_bkgsub'`
   (stage3.py:64), so sources *without* a valid nod pair (groups not in
   `{2,3,5}`, stage2.py:1057) silently get no `_cal_bkgsub` and are excluded.
   **Confirmed decision:** for the no-nod case the canonical SCI stays = cal
   (un-subtracted), `CFP_BKG` records `skipped:nods=N`, and **stage3 must
   exclude any canonical file whose `CFP_BKG` is not done.** `discover_files`
   changes from suffix-glob to canonical-glob + state-keyword filter
   (observation.py:434).
2. **Spec3 ASN load.** Because live SCI == bkgsub'd, stage3's ASN can point at
   canonical files directly and jwst reads the right science — *verify* jwst
   ignores the extra HDUs on `MultiSlitModel` load with one real ASN. The
   canonical file is read-only at stage3 (Spec3 emits new crf/spec), so there is
   no save-drop problem there — only within stage2.
3. **Per-slit revert extensions.** `_rate`'s revert is a single detector frame;
   a `MultiSlitModel`'s is per-slit. Per-source cal is usually one slit, but the
   append logic must handle multi-slit cleanly.

**Validation:** a **golden-file test** — fixed inputs → byte-identical
`_spec.fits` (and `_x1d.fits`) before/after. This is what earns the
**Infrastructure / PATCH** classification (no CRDS bump). Keep `_rate` exactly
as-is.

**Independently valuable:** fewer files, cleaner per-observation workspace,
keyword-driven resumability, queryable reduction state — wins even absent the
cloud work.

> **Sequencing note:** PR-3 must land before the registry schema is frozen
> (Phase 1), or you migrate the registry's NIRSpec rows twice.

---

## 4. Target architecture by layer

| Layer | Today | Target |
|---|---|---|
| **Storage** | R2, endpoint hardcoded ×4, two buckets, CF Worker for ZIP, public-URL tiles | OSN via configurable S3 endpoint + factory (PR-1); Worker replaced by presigned GET / server zip route; tiles via OSN public-read or CDN |
| **Registry** | none (keys are bare strings / reconstructed) | `storage_objects` indexes every object: key, hash, size, type, scope, stage, status, deployment, provenance |
| **Database** | presence == published; lifecycle only on `nircam_exposures` | status/visibility on science + intermediate rows; RLS in-prep tier; `spectrum_exposures` mirrors `nircam_exposures` |
| **Deploy CLI** | one-way push of finals; hard-delete `remove` | upload+register (`--in-prep`) decoupled from `publish`/`revoke`; writes registry; intermediates for both instruments |
| **Web** | NIRCam triage UI; no publish concept | admin intermediate-products view (clone of NIRCam triage) + deploy control panel (stage/publish/revoke) |
| **Sync/recover** | metadata-only sync; one FITS per spectrum | generalized artifact manifest; admin `sync --all-products`; delete-local + recover |

---

## 5. How the database connects (schema detail)

> **Land changes on `objects` (user unit) / `spectra` / a new registry — never
> the deprecated `targets.*` columns. The catalog is mid-migration (Phase A–E);
> the live list RPC is `get_filtered_object_ids`, not the doc-stale
> `get_filtered_target_ids`.**

### 5.1 `storage_objects` (new — keystone)

One row per object in storage. Indicative columns:

```
id              bigint pk
storage_key     text unique            -- the object key (== spectra.fits_path for finals)
backend         text                   -- 'r2' | 'osn' (during/after migration)
bucket          text                   -- 'data' | 'tiles'
content_hash    text                   -- 'sha256:<hex>'  (already carried end-to-end)
size_bytes      bigint
content_type    text
product_type    text                   -- nirspec_spec | nirspec_spectrum_exposure |
                                       --   nirspec_rate | nircam_exposure | nircam_mosaic |
                                       --   rgb | sed | preview | tile | photometry ...
instrument      text                   -- 'nirspec' | 'nircam'
scope           jsonb / fk columns     -- observation/field/exposure/spectrum/object linkage
stage           text                   -- reduction stage (CFP state summary)
status          text                   -- active | superseded | revoked
deployment_id   bigint fk -> deployments
cfpipe_version  text  ...              -- provenance mirror
uploaded_by     uuid
created_at / updated_at
```

This makes storage enumerable (`SUM(size_bytes)` budgeting; OSN copy is a
table walk), and is the manifest source for sync/recover. `spectra.fits_path`
stays as the back-compat pointer (optionally a view/FK into this table).

### 5.2 `spectrum_exposures` (new — NIRSpec intermediate, mirrors `nircam_exposures`)

One row per `(exposure, detector, source)` canonical file, **child of the final
`spectra` row** (a published spectrum = the stage3 combination of N
spectrum-exposures). Columns mirror `nircam_exposures`: `stage`/state,
`storage_key`, `content_hash`, `review_status`, masking, `notes`. Admin-only
RLS, mirroring `nircam_exposures` policies. This is what gives NIRSpec the
"query by reduction stage" capability symmetric with NIRCam.

### 5.3 Status / lifecycle on existing rows

- `spectra` (and intermediate tables): a visibility column
  `deploy_status default 'published'` (existing rows backfilled to `published`)
  with `in_prep | published | revoked`.
- `deployments`: add `status` + `published_at`/`revoked_at`; add an **admin
  UPDATE RLS policy** (it is append-only today) and a per-product
  `deployment_id` FK so a deployment can cascade to its products (today
  `source_ids_filter int[]` is a snapshot, not a link).

### 5.4 RLS + RPC changes

- Extend the two visibility chokepoints (`accessible_program_slugs()`,
  `is_admin()`) and add a status predicate to `select_*_by_access` policies so
  **non-admins never see `in_prep`/`revoked`**; admins (who already inherit all
  programs) see everything.
- Add status/role params + predicates to `get_filtered_object_ids`,
  `get_filtered_objects_paginated`, `get_filtered_spectra_paginated` — they
  pre-filter and must not leak in-prep rows.
- New admin-gated sync RPC/endpoint emitting the generalized artifact list
  (mirrors `get_spectra_for_sync`).

> **migra caveat (CLAUDE.md):** if budgeting uses materialized views or column
> comments, those aren't tracked by `db diff` — hand-author those migrations.

---

## 6. Phased rollout (staggered, non-breaking)

```
Phase 0  Prerequisites      PR-1 (backend abstraction)  ─┐
                            PR-2 (key module)            ├─ independent, parallel
                            PR-3 (NIRSpec canonical)    ─┘  (PR-3 before Phase 1)
Phase 1  Registry           storage_objects + backfill + deploy writes rows   (additive)
Phase 2  OSN migration      registry-driven copy+verify; Worker re-arch; tile serving; cutover
Phase 3  Intermediate deploy NIRCam exposures first, then NIRSpec spectrum-exposures
Phase 4  Lifecycle          status cols + RLS; admin intermediate view; publish/revoke panel
Phase 5  Sync/recover       artifact manifest; API admin gate; sync --all-products; delete-local
Phase 6  Consolidation      multi-reducer coordination; retention/GC; control-panel polish
```

**Dependencies / parallelism:**
- Phase 0 is the shared foundation. PR-1/PR-2 are non-breaking and can ship any
  time; PR-3 must precede Phase 1.
- Phase 1 (registry) unblocks both Phase 2 and Phase 3 — do it early.
- **Phase 2 (OSN) and Phases 3–4 (intermediates/lifecycle) are independent** —
  one touches the storage backend, the other the data/visibility model. They can
  proceed in parallel after Phase 1.
- Each phase is shippable to production without the next: Phase 1 changes
  nothing user-visible; Phase 2 is a backend swap behind dual-read; Phase 3
  uploads bytes that Phase 4 later makes visible.

**Per-phase exit criteria** are listed in §8.

---

## 7. Risks & mitigations

- **NIRCam in-place mutation / budget.** Commit to "canonical file + state
  vector as the unit"; never per-step snapshots. Wire `SUM(size_bytes)`
  budgeting and an alert before OSN fills.
- **No R2↔DB transactionality.** Uploads and upserts are independent today; the
  registry amplifies this. Design deploy as upload → verify hash → register,
  with a reconciliation sweep that finds storage objects without a row (orphans)
  and rows without an object (dangling), surfaced in the admin panel.
- **RLS is the real boundary.** The in-prep tier must be enforced in
  policies/RPCs, not just server actions, or admins' unpublished data leaks.
- **`reconcile.py` aborts on split/merge** (human-in-the-loop). Keep
  intermediate products off the object-clustering path entirely; they are not
  user-state-bearing (except masks, which already round-trip).
- **Download Worker re-architecture** is the single hardest OSN piece (CF
  `R2Bucket` binding → presigned GET or server-side zip). Scope it explicitly in
  Phase 2; keep R2 as fallback until verified.
- **OSN specifics:** no CDN (caching plan for tiles/previews), may reject region
  `'auto'`, different public-access model, check presign/multipart limits.
- **PR-3 touches the scientific core.** Mitigated by the bit-identical
  golden-file gate; classify as Infrastructure/PATCH only if that passes.
- **Mid-migration schema.** Land on `objects`/`spectra`/registry, not deprecated
  `targets.*`.

---

## 8. Testing & validation strategy

- **PR-3:** golden-file test (byte-identical `_spec.fits`/`_x1d.fits`); a
  round-trip test for `restore_pre_bkgsub` on the consolidated file; an
  stage3-selection test asserting no-nod canonicals are excluded.
- **PR-1/PR-2:** key-conformance test (python ↔ TS agree); deploy against a
  local MinIO/S3 stand-in to exercise the configurable endpoint.
- **Phase 1:** backfill reconciliation report (every existing `fits_path` etc.
  has a `storage_objects` row; no dangling rows).
- **Phase 2:** copy-verify every object by `content_hash`; dual-read shadow
  period; tile/RGB/ZIP-download smoke tests against OSN before cutover.
- **Phase 4:** RLS tests that a non-admin session cannot see `in_prep`/`revoked`
  rows via any RPC or direct select (this is the security-critical test).
- **Seed/migrations:** regenerate `supabase/seed.sql` if columns change; preview
  branch must stay green.

---

## 9. Open questions

- Status granularity: on `storage_objects` (object lifecycle) **and** on the
  logical rows (visibility)? Current lean: both, with distinct vocabularies
  (`active/superseded/revoked` vs `in_prep/published/revoked`).
- Object versioning: keep one current object per product (delete-then-replace,
  as today) or a version chain for rollback? Registry supports either;
  v1 recommendation is single-current + `superseded` tombstones.
- Multi-reducer concurrency control: reuse the `objects.version` optimistic-lock
  pattern, or a coarser per-observation/field deploy lock in the DB?
- Does `campfire sync --all-products` materialize the storage key tree locally
  (subdirs per stage to avoid basename collisions), or a flat hashed cache?
- NIRSpec mask cloud round-trip (currently `observations.toml`, local-only) —
  in scope for this work or a follow-up? (NIRCam already round-trips masks.)

---

## 10. Decisions recorded

- **NIRSpec no-nod case:** canonical SCI stays = cal (un-subtracted); `CFP_BKG`
  records `skipped`; **stage3 excludes any canonical not background-subtracted.**
- **State chains kept separate per instrument, aligned in spirit:** NIRSpec gets
  its own mirror of `cfp.py` (same mechanics, separate keys) rather than a
  shared module — the two pipeline halves stay independent.
- **PR-3 is bit-identical for finals** (Infrastructure/PATCH), gated by a
  golden-file test.

---

## 11. Appendix: current-state reference

- **Deploy:** `deploy_observation()` (deploy.py) pushes finals only; provenance
  in `deployments` + `observations.latest_deployment_id`; `remove.py` is a hard
  delete; `reconcile.py` preserves inspection state across redeploys.
- **Storage:** generic S3 (boto3 / aws-sdk); endpoint hardcoded ×4; buckets
  `data` + `tiles`; ZIP downloads via CF Worker `R2Bucket` binding; presigned
  GET for single files; public-URL tiles. `file_hash` (sha256) + `file_size`
  carried end-to-end.
- **Pipeline:** NIRCam = one canonical FITS/exposure mutated in place, `CFP_*`
  state chain (`cfp.py`); NIRSpec = four suffixed files per
  `(exposure,detector,source)` + per-exposure `_rate`; aggressive cleanup of
  some intermediates by default.
- **Sync:** `campfire sync` = metadata-only into SQLite; `campfire download` =
  one FITS per spectrum via presigned URLs; content-addressed verify/diff
  already implemented; no admin/role awareness client-side.
- **Web:** server actions + RLS; R2 never browsed; `/admin/nircam` exposure
  triage + `MaskEditor` is the template for the intermediate-products view;
  roles = `is_admin`/`can_comment`/`is_group_account` only.
- **DB:** `objects` (user unit) / `spectra` / `targets` (deprecated cols);
  `nircam_exposures` is the only existing lifecycle model; storage refs are bare
  text columns; `deployments` append-only.
