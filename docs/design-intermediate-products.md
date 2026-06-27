# Design: Intermediate products & cloud-as-source-of-truth

**Status:** draft for review — adversarially reviewed 2026-06-27 (four-lens
critique against the codebase; corrections folded in, see §16).
**Date:** 2026-06-27
**Context:** migration of CAMPFIRE object storage from Cloudflare R2 → NSF Open
Storage Network (OSN, S3-compatible, 20 TB, upgradeable); related
[NIRCam exposure-major design](design-nircam-exposure-major.md),
[objects migration](design-objects-migration.md).
**Driver:** Today `campfire deploy` is a one-way push of *final* products from a
local `$CAMPFIRE_ROOT/products/` tree to R2 + Supabase. The filesystem is the
source of truth; presence-on-disk == published; there is no in-prep tier, no
publish/revoke lifecycle, and nothing in the DB knows what is actually in the
bucket. This design shifts the architecture so that **the cloud object store is
the system of record, the database is its index and lifecycle controller, and
the web portal and `campfire` CLI are both clients of that index** — and extends
deployment to cover *all intermediate products*, not just finals.

---

## 1. Goals / non-goals

**Goals**

- Deploy **all intermediate products**, not just finals: NIRCam canonical
  exposures and NIRSpec canonical spectrum-exposures (§2.2), at whatever
  reduction stage they have reached — including before a reduction is finished.
- An **admin-only web view** of intermediate products and in-prep reductions,
  extending the existing NIRCam exposure triage UI.
- **Cloud as source of truth**: the DB enumerates every object in storage,
  enabling `reduce → deploy → delete local → recover later` and multi-reducer
  coordination on headless clusters (the user's primary use case).
- A **deployment lifecycle**: `in_prep → published`, plus soft `revoked` and
  `superseded`; recoverable, unlike today's hard-delete `remove`.
- **Migrate storage R2 → OSN** with checksum-verified copy and no user-visible
  downtime.
- Do all of the above **incrementally**, each phase independently valuable and
  non-breaking for the live portal.

**Non-goals (this design)**

- No change to the *final* `_spec.fits` / mosaic science outputs. The NIRSpec
  prerequisite refactor (§3 PR-3) is explicitly **bit-identical** for finals.
- No public access to intermediate products; intermediates are admin-only for
  now. The lifecycle gates *admin-vs-not*, not fine-grained external sharing.
- No new sub-admin role taxonomy (separate "reviewer" vs "publisher"
  authority). Single `is_admin` remains the lever; finer roles are a later add.
- No change to the objects-clustering / inspection-state model (`reconcile.py`);
  intermediate products are deliberately kept off that path.

---

## 2. Core design decisions

### 2.1 The cloud is the system of record; the DB indexes it

The keystone is a new **storage-object registry** table (`storage_objects`,
§5.1). Today object keys are bare convention-built strings on domain rows
(`spectra.fits_path`, `nircam_images.file_path`, `nircam_exposures.png_path`) or
not stored at all (SED PDFs, RGB PNGs — the web reconstructs them from
`obs_name + filename`). You cannot treat the cloud as source of truth if the DB
cannot enumerate what is in the cloud. The registry is the join point for
sync/recover, the OSN copy-and-verify, storage budgeting, and lifecycle.

> Note the registry indexes **object-storage** artifacts. Some deployed products
> live in **Postgres**, not the bucket (inline SVG thumbnails; pointings JSONB);
> §5.3 gives those a parallel lifecycle rather than forcing them into the
> registry.

### 2.2 The unit of an "intermediate product"

The 20 TB budget forces a deliberate answer. **NIRCam dominates** storage; a
COSMOS-Web-scale field is ~5,000 exposures × ~150 MB ≈ 750 GB for a *single*
state, so snapshotting after each of ~15 steps (~10 TB/field) is a non-starter
(§7 quantifies the budget).

- **NIRCam:** the unit is **the canonical exposure file + its `CFP_*` state
  vector** — one object per exposure, re-uploaded only when its content hash
  changes (`manifest.py` already hashes `sha256(SCI+DQ)`). This already exists on
  disk (one `<rootname>.fits` mutated in place; `common/cfp.py`). Enables queries
  like *"all exposures reduced through JHAT in COSMOS."*
- **NIRSpec:** the unit is **the canonical spectrum-exposure file**, granularity
  `(exposure × detector × source)`, plus the separate per-`(exposure, detector)`
  `_rate` tier (shared across sources, cannot be folded in). This **does not
  exist yet** — today the same logical thing is split across four files
  (`_cal`, `_cal_bkgsub`, `_s2d`, `_s2d_bkgsub`; `_s2d` at stage2.py:899,
  `_cal_bkgsub` save at 1131, `_s2d_bkgsub` at 1139, names also built in the
  empty-override deletion path at 1095–1102; the `_cal` product itself comes from
  `Spec2Pipeline.call`, prod_name at ~700/516). It is created by the prerequisite
  refactor **PR-3** (§3).

**Other NIRSpec workspace artifacts** the registry/sync must consciously capture
or exclude (not silently drop): `_x1d.fits` (register as a product or declare it
a regenerable byproduct); the user-edited `*_stuck_closed_shutters.toml` and
`*_nodded_background_overrides.toml` (observation.py:280–285 — these are
state-bearing, like NIRSpec masks, §13); cached augmented wavecorr /
extended-wavelength refs under `$CAMPFIRE_ROOT/cache` (stage2.py:57); and the
intentionally-ephemeral per-source MSA metafiles / ASN / nodata markers
(explicitly out of scope).

**Symmetry principle:** after PR-3, both instruments use *canonical file +
state-keyword chain*, each backed by an admin-only intermediate-products table
(`nircam_exposures`, new `spectrum_exposures`) that hangs off the published
science row. Every downstream layer (registry, deploy, web view, sync) treats
the two instruments uniformly.

### 2.3 Lifecycle: decouple "upload bytes" from "make visible"

Deploy splits into **(a) upload + register** (write bytes to storage, record a
`storage_objects` row, attach to a science/intermediate row with
`deploy_status = in_prep`) and **(b) publish** (flip `in_prep → published`).
`revoked` hides a published row but keeps the bytes (recoverable); `superseded`
marks an object replaced by a newer hash.

**Where visibility is actually enforced (corrected — this is the security core).**
It is *not* a single boundary. There are two distinct read surfaces with
different authorization:

1. **Web portal reads** go through the user session client (anon key + cookies,
   `web/lib/supabase/server.ts`) calling **`SECURITY INVOKER`** RPCs
   (`get_filtered_object_ids`, `get_filtered_objects_paginated`,
   `get_filtered_spectra_paginated`, `object_scoped_aggregates`,
   `get_adjacent_objects`, map RPCs). Because they run as the invoker, **table
   RLS applies** — so an RLS status predicate (`deploy_status='published' OR
   public.is_admin()`) on `spectra`/`objects`/intermediate tables *does* gate the
   portal, and the admin-sees-all case works via `is_admin()` in the policy.
2. **CLI / download reads** go through the **service-role** client, which
   **bypasses RLS entirely**: `/api/v1/sync/{spectra,objects,photometry,lists}`,
   `/api/v1/observations/[obs]/manifest`, and the single-file signed-URL route
   `/api/v1/spectra?path=` (all construct the client with
   `SUPABASE_SERVICE_ROLE_KEY`). For these, RLS is irrelevant; the status filter
   **must be added to the RPC/route bodies** (`get_spectra_for_sync`,
   `get_objects_for_sync`, `get_photometry_for_sync`, `get_observation_manifest`,
   and the inline access check in `/api/v1/spectra`).

Two consequences that the schema must respect:

- **Admin-ness cannot be derived from the program-slug array.**
  `accessible_program_slugs()` already expands to *every* program for admins
  (functions.sql:74-77), so inside an RPC an admin's `p_program_slugs` is
  indistinguishable from a non-admin with broad access. The service-role readers
  must take an explicit `p_include_in_prep BOOLEAN DEFAULT false`, computed by
  the route from the API caller's `is_admin()` (the proven pattern in
  `app/api/v1/deploy/presign/route.ts:93-107`). RLS policies use `is_admin()`
  directly. The admin intermediate-products view uses **distinct admin-only
  RPCs/endpoints**, not relaxed public ones.
- **The sync RPCs build their payload from explicit column lists**, so *adding* a
  `deploy_status` column changes their output by nothing — they keep emitting
  in-prep rows with no error. The failure mode is a **silent** leak, so the
  predicate must be added to the `WHERE` clause in the *same* migration as the
  column (§5.5, §8).

### 2.4 Symmetry principle

(See §2.2.) The two intermediate-product tables (`nircam_exposures`,
`spectrum_exposures`) and the two canonical-file state chains are mirror images,
so registry/deploy/web/sync logic is written once per concern, not per
instrument.

---

## 3. Prerequisite refactors

Independently valuable refactors that de-risk and simplify the main work. The
litmus test is that each pays for itself even if the rest of this design never
shipped.

### PR-1: Storage backend abstraction (S3 endpoint **+ region + CORS** config + factory)

**Why:** the storage layer is already 95% generic S3 (boto3 +
`@aws-sdk/client-s3`, `region:'auto'`, `s3v4`). The Cloudflare lock-ins are
(1) the endpoint `https://{account_id}.r2.cloudflarestorage.com` **hardcoded in
4 places** (`python/campfire/deploy/r2.py` ×2, `app/api/v1/deploy/presign/route.ts`,
`web/lib/r2.ts`); (2) the download Worker's `R2Bucket` binding; (3) public-URL
tile/RGB serving + edge caching.

**Change:**
- Make **both endpoint and region** configurable (`CAMPFIRE_S3_ENDPOINT`/`_REGION`
  and web equivalents) plus `forcePathStyle` as OSN requires — region `'auto'` is
  a Cloudflare-ism OSN may reject, and SigV4 presigned URLs are bound to the
  exact host+region used at signing, so a configurable endpoint with a stale
  `'auto'` region will fail verification at OSN.
- Add a **backend factory** in each language that resolves config **per logical
  bucket / purpose**, not globally: `data → OSN` (private; presigned + the
  streaming-zip proxy of §6) and `tiles → R2` (kept **deliberately** for the CDN
  edge cache; public URL base). The two-bucket credential split already exists
  (`CAMPFIRE_R2_*` vs `CAMPFIRE_R2_TILES_*`), so this generalizes it into a
  per-purpose `{backend, endpoint, region, creds, public_url_base}` block. A
  bonus: `storage_objects.backend` is then `osn` for data and stays `r2` for
  tiles — the registry models the split for free, and the OSN copy (§6) simply
  excludes the tiles bucket.
- Replace the `web/lib/r2.ts` `'#download-placeholder'` soft-fail (which
  downgrades signing errors to a generic 503) with a hard, logged error so a
  cutover endpoint/region/CORS misconfig is diagnosable, not silently masked.
- Neutral naming/aliases (`storage`/`s3`), keeping `r2`/`R2_*` as accepted
  aliases to avoid a breaking rename.

> The download-Worker replacement is OSN-cutover-scoped (§6). Tiles stay on R2,
> so there is **no tile re-host** and no `tile_base_url` backfill. But PR-1 must
> reach the **web download path** (`generateDownloadUrl` in `web/lib/r2.ts`)
> before any **data** object can be served from OSN (§8 dependency).

**Independently valuable:** config hygiene; removes 4-way endpoint duplication;
makes the data-on-OSN / tiles-on-R2 split a config fact, not an accident.

### PR-2: The layout & key contract (pipeline ↔ CLI ↔ cloud)

**Why — the directory layout is now a three-way contract, defined in N places.**
The local `$CAMPFIRE_ROOT/` tree is the contract between (a) the **pipeline**
(which creates it), (b) the **CLI** (which mirrors it on `download` and prunes it
on `delete-local`), and (c) the **cloud** (whose keys must round-trip to it).
Today that contract is implicit and scattered:
- **Local paths are resolved in two independent codebases.** The pipeline package
  builds them in `campfire_pipeline/config.py` (`resolve_paths`), `nircam/field.py`
  (`setup_workspace`/`filter_dir`/`get_exposure_path`), and
  `nirspec/observation.py` (`workspace_dir`); the `campfire` package re-derives
  the same conventions in `deploy/config.py` (`products_dir`/`resolve_obs_dir`).
  Nothing forces them to agree.
- **Storage keys are a *third* vocabulary** that diverges from the local tree:
  `spectra/<obs>/x.fits` in the bucket vs `products/<obs>/x.fits` on disk;
  NIRCam previews under `nircam/exposures/…`; SED/RGB keys not stored at all
  (reconstructed from `obs_name + filename`). The key↔path translation lives ad
  hoc in `deploy.py`, `summary.py`, `nircam.py`, `photometry.py`, `tiles.py` and
  is *mirrored again* in `web/lib/r2.ts`.
- **No tree is classified by lifecycle.** Nothing says which top-level dirs are
  cloud-backed vs local-only vs regenerable vs from-MAST — so `download
  --intermediate`/`delete-local` have no principled way to know what to fetch or
  what is safe to delete (e.g. `reference/` masks are user-state, `raw/` comes
  from MAST, `cache/` is regenerable).

As product types multiply (intermediates) and the cloud becomes source-of-truth,
this is the highest-footgun surface in the whole design. PR-2 codifies it.

**Change — one declarative contract, consumed everywhere.**
- A single **layout module** that is the sole authority for: (i) the local path of
  any `(instrument, scope, product_type, filename)`; (ii) its storage key; (iii)
  the **bijection** key ↔ local-relative-path (total and reversible, so
  `download` can place any fetched key correctly and `deploy` can derive any key);
  (iv) a **lifecycle class** per top-level tree (see table). Deploy/download/web
  all call it; nobody hand-builds a key or a path.
- **Three consumers, kept honest by conformance tests.** The pipeline package and
  the `campfire` package share a pure-python `campfire_layout` core (no heavy
  deps; importable by both — pipeline is local-only but a tiny pure module is
  safe to depend on); the web gets a mirrored TS module. A single golden
  conformance test asserts all three agree on a fixed set of
  `(scope) → (path, key, class)` cases — the same pattern already proposed for
  python↔TS keys. (Alternative: a language-agnostic `layout.toml` spec with thin
  per-language readers — maximal drift-resistance; heavier to author. Recommend
  the shared-module + conformance-test route first.)
- Deploy **records** the key it used into `storage_objects` rather than relying on
  reconstruction (kills the SED/RGB reconstruction fragility).

**Tree lifecycle classification** (drives `download --intermediate`/`delete-local`):

| Tree | Class | In cloud? | `delete-local` safe? |
|---|---|---|---|
| `products/` | cloud-backed product | yes (data→OSN) | yes, after verified-in-cloud |
| `reference/…/masks` | **user-state** | yes (via DB round-trip / registry) | only after round-tripped |
| `reference/…/{flats,bad_pixels,wisps,astrom_cats}` | reduction input | **decide**: cloud-backed vs regenerable | no unless cloud-backed |
| `raw/` | external (MAST) | no — `cfpipe download` refetches | yes (re-fetchable from MAST) |
| `cache/` (crds, templates) | regenerable | no | yes |
| `meta/`, `cutouts/` | CLI-local | no | n/a |

This makes "recover a re-reducible workspace" precise: it's `products/` **plus**
the cloud-backed `reference/` inputs — not just `products/`. Without the
classification, `download --intermediate` would silently restore an
un-re-reducible tree (missing flats/masks), and `delete-local` could nuke
local-only reference data that exists nowhere else.

**Canonicalize keys during the OSN copy (the free re-key window).** The OSN
migration already copies + verifies every object (§6). That is the *one* moment
to also re-key objects onto the canonical scheme so the key ↔ local-path map
becomes (near-)identity — `products/<obs>/x_spec.fits` on disk ↔
`data/products/<obs>/x_spec.fits` in the bucket — collapsing the third vocabulary.
Doing it then is ~free (we copy anyway); doing it later would require a second
full-bucket pass we'd never want. Safe because `spectra.spectrum_id` is GENERATED
by stripping `^.*/` and `_spec\.fits$` from `fits_path`, which survives any key
prefix change as long as the basename convention holds (it does).

> **Scope boundary — keep the *local* dir layout as-is.** Renaming local
> `products/` dirs would be a **pipeline MAJOR** (CLAUDE.md: file-naming is a
> breaking output change) and touches the pipeline's globbing everywhere. PR-2
> deliberately does **not** re-org local dirs; it centralizes + classifies the
> existing layout and canonicalizes only the *bucket keys* (an infra change). A
> local subdir tidy (e.g. splitting NIRCam `exposures/` vs `mosaics/`) is an
> *optional* coordinated-MAJOR add-on, flagged in §13, not part of this PR.

> **Ordering:** PR-2 must precede Phase 1's "deploy writes registry rows" (so the
> registry records canonical keys) and informs PR-3's NIRSpec canonical naming.

**Independently valuable:** turns the most footgun-prone, duplicated convention in
the repo into one tested contract — useful even with no cloud changes at all.

### PR-3: NIRSpec canonical spectrum-exposure refactor

**Why:** NIRSpec intermediates are split across four files at one granularity.
Consolidating to one **canonical spectrum-exposure file** (4→1) gives NIRSpec the
self-documenting canonical-file model NIRCam already has, drops object count 4×,
and removes basename-collision handling from sync. Without it the
registry/web/sync layers bake the four-suffix mess in permanently.

**Target file model:** anchor on the `_cal` **`MultiSlitModel`** (standard jwst);
the other states become extensions/mutations of it:

- **Background subtraction in place** (eliminates `_cal_bkgsub`): live SCI becomes
  the bkgsub'd data; the pre-bkgsub state recoverable from extra extensions. The
  `_rate` precedent (`CFBKGSUB` sentinel + `CFBKG`/`CFBKGMASK` extensions +
  `restore_pre_bkgsub()`, stage1.py:768-793, masks.py:259-322) is the *model* but
  **not a 1:1 template**: stage2b inverts pathloss before subtraction and
  re-applies it after (stage2.py:1063-1069, 1126-1127) and pads/unpads to a
  common detector region for unequal nod shapes (1073-1123), so the revert state
  is not a simple additive background. **Decision:** stash the full pre-bkgsub
  per-slit SCI/ERR/var arrays as extensions (robust to the pathloss arithmetic),
  not a single `CFBKG`-style background, and regenerate the s2d-bkgsub view from
  the reverted+resubtracted state. Revert is **per-slit** (a `MultiSlitModel` may
  hold >1 slit), not the single frame `_rate` exercises.
- **s2d as extensions** (eliminates standalone `_s2d`/`_s2d_bkgsub`): s2d is
  visualization-only (created only on `plot`/stuck-shutter/`rectify`). Both
  rectified states live as named HDUs (`S2D_SCI`, `S2D_BKGSUB_SCI`, …).

**Consumer inventory (must be ported — the golden-file gate alone will NOT catch
these).** Consumers derive sibling filenames by string-replacement and read with
astropy, so they break the moment the separate files vanish:
- `plots.py` — `f['path'].replace('_cal','_s2d')` (300, reused for the bkgsub
  plot mode), `.replace('_cal.fits','_s2d.fits')` (868), then
  `fits.getdata(..., ext=1)` / `VAR_RNOISE` (377, 387, 937, 954, 1006).
- `stuck_shutters.py` — `.replace('_cal.fits','_s2d.fits')` (89), reads
  `ext=1`/`VAR_RNOISE` (302, 305).
Each becomes "open canonical file, read `S2D_*` HDU + matching `VAR_RNOISE`."
Add stuck-shutter-detection and plot-generation **smoke tests** to PR-3.

**Stage3 selection (reproduce ALL current exclusions, not just no-nod).** Stage3
discovers `ext='cal_bkgsub'` (stage3.py:64); consolidation removes the
file-existence filter, so the canonical-glob + state filter must reproduce three
exclusions:
1. **No valid nod pair** (group ∉ {2,3,5}) → `CFP_BKG=skipped:nods=N` → exclude.
2. **Empty bkg-override for a nod** (stage2.py:1095-1102 deletes that nod's
   bkgsub output today) → a per-nod "excluded" marker (header card / per-slit
   flag, since there is no file to delete) → exclude that nod from the ASN.
3. **`SRCFLUX` absent** (stage3.py:74-79, 89; stage2b skip_sources 400-410) →
   keep the existing `SRCFLUX` header filter.
**Confirmed decision:** for the no-nod case the canonical SCI stays = cal
(un-subtracted) and stage3 excludes any canonical not background-subtracted.

**Hard invariant — jwst `DataModel.save()` drops non-schema HDUs.** Same
constraint the NIRCam canonical model lives with (design-nircam audit M7). Every
mutating step: run jwst step → `MultiSlitModel.save()` → reopen with astropy →
(re)append revert + `S2D_*` HDUs → stamp the state keyword.

**Pre-implementation gate (promote, don't defer).** The whole 4→1 rests on
"Spec3Pipeline reads the canonical file's live (bkgsub'd) SCI and ignores the
extra HDUs." Treat this like the NIRCam exposure-major Q1 gate: before the
refactor lands, build one real spec3 ASN over a consolidated canonical file (with
`S2D_*` + revert HDUs) on the pinned jwst/stdatamodels/crds stack and confirm
byte-identical `_spec.fits`/`_x1d.fits` vs the four-file flow. Record the result
here.

**State chain — reuse the existing `common/cfp.py`, separate key set.**
`cfp.py` already lives in `campfire_pipeline/common/` and is generic
(key-table-driven: `CFP_KEYS`, `format`/`has_step`/`should_skip`/`clear_from`,
`CFP_COMMENTS`), imported by ~15 NIRCam steps. So "keep the two instruments
separate, aligned in spirit" resolves to: **add a NIRSpec key set / vocabulary
(e.g. `CFP_CAL → CFP_MASK → CFP_BKG → CFP_S2D`) selected per instrument within
the shared common module** — separate *namespaces*, shared *mechanics*. (This
refines the earlier "separate mirror module" framing, which was wrong: the module
is already common; duplicating its logic would be gratuitous.) `CFBKGSUB` already
proves keyword-driven skip works for NIRSpec; this extends it to the chain, since
once four files collapse to one, file-existence can no longer encode reduction
depth.

**Validation:** golden-file test (byte-identical `_spec.fits`/`_x1d.fits`); the
spec3-ASN gate above; a `restore_pre_bkgsub` round-trip test on the consolidated
file exercising **(a)** a padded multi-nod group and **(b)** a `pathloss=COMPLETE`
source; the plot/stuck-shutter smoke tests; stage3-selection tests for all three
exclusion cases. Passing these earns the **Infrastructure / PATCH** label (no
CRDS bump). Keep `_rate` as-is.

> **Sequencing:** PR-3 must land before the registry schema is frozen (Phase 1),
> or NIRSpec registry rows migrate twice.

---

## 4. Target architecture by layer

| Layer | Today | Target |
|---|---|---|
| **Storage** | R2; endpoint hardcoded ×4; two buckets; CF Worker (R2 binding) for ZIP; public-URL tiles | **data → OSN** via per-purpose factory (PR-1); ZIP Worker **retired** → storage-agnostic streaming-zip proxy (OSN egress is free); **tiles stay on R2** for the CDN edge cache |
| **Registry** | none (keys bare/reconstructed) | `storage_objects` indexes every object: key, hash, size, type, scope FKs, stage, status, deployment, backend, provenance |
| **Database** | presence == published; lifecycle only on `nircam_exposures` | `deploy_status` on science + intermediate rows; status enforced in **RLS *and* service-role RPCs/routes**; `spectrum_exposures` mirrors `nircam_exposures`; audit log |
| **Deploy CLI** | one-way push of finals; hard-delete `remove` | upload+register (`--in-prep`) decoupled from `publish`/`revoke`; writes registry; intermediates for both instruments; every product subcommand registry+lifecycle-aware (§5.4 table) |
| **Web** | NIRCam triage UI; no publish concept | admin intermediate-products view (clone of NIRCam triage) + deploy control panel (stage/publish/revoke) + audit view |
| **Sync / download** | metadata-only `sync`; `download` = one final FITS per spectrum | `sync` unchanged (index only); `download` **widened** to intermediates via `--intermediate`/`--all` (admin), writing into the mirrored `products/` tree; `delete-local` is its verified-in-cloud inverse. No separate `recover` verb. |

---

## 5. Database & storage schema

> **Land changes on `objects` (user unit) / `spectra` / the new registry — never
> the deprecated `targets.*` columns. The catalog is mid-migration (Phase A–E);
> the live list RPC is `get_filtered_object_ids`, not the doc-stale
> `get_filtered_target_ids`.**

### 5.1 `storage_objects` (new — keystone)

```
id              bigint pk
backend         text not null check (backend in ('r2','osn'))
bucket          text not null check (bucket in ('data','tiles'))
storage_key     text not null
content_hash    text not null            -- 'sha256:<hex>'
size_bytes      bigint not null
content_type    text not null
product_type    text not null check (...) -- nirspec_spec | nirspec_spectrum_exposure |
                                           --   nirspec_rate | nirspec_x1d | nircam_exposure |
                                           --   nircam_mosaic | rgb | sed | tile | photometry ...
instrument      text check (instrument in ('nirspec','nircam'))
status          text not null default 'active' check (status in ('active','superseded','revoked'))
-- typed nullable scope FKs (NOT opaque jsonb — must be joinable/indexable):
observation     text references observations(obs_name)
field           text
spectrum_id     text references spectra(spectrum_id)
exposure_ref    text                     -- nircam rootname / nirspec (root,nod,detector,source)
deployment_id   bigint references deployments(id) on delete set null
cfpipe_version  text  ...                -- provenance mirror
uploaded_by     uuid
created_at / updated_at  timestamptz not null default now()

-- uniqueness & indexes (required for the registry's stated jobs):
unique (backend, bucket, storage_key)              -- dual-backend rows coexist during cutover
unique (product_type, exposure_ref) where status='active'  -- one current object per product
index on (backend, status)                         -- copy/verify + budget walks
index on content_hash                              -- copy-verify is by hash
index on deployment_id                             -- cascade a deployment to its products
```

Notes driven by review:
- **`storage_key` is NOT unique on its own** — during the dual-backend window the
  same logical key exists with `backend='r2'` and `backend='osn'`. Uniqueness is
  `(backend, bucket, storage_key)`.
- **`fits_path` does not become a view/FK into this table.** `spectra.fits_path`
  is `NOT NULL`, `UNIQUE`, and two `GENERATED STORED` columns (`spectrum_id`,
  `search_text`) are derived from it by stripping `_spec.fits` (tables.sql:317,
  338-354). Intermediate keys don't end in `_spec.fits` and have **no** place in
  that derivation. Keep `fits_path` as a denormalized pointer maintained by
  deploy; intermediate keys live **only** in `storage_objects`; `spectrum_id`
  stays finals-only.
- Budgeting is `SUM(size_bytes)` via a **`SECURITY DEFINER` RPC** (tracked by
  `db diff`), *not* a materialized view (migra doesn't track matviews — CLAUDE.md).

### 5.2 `spectrum_exposures` (new — NIRSpec intermediate, mirrors `nircam_exposures`)

One row per `(exposure, detector, source)` canonical file, **child of the final
`spectra` row** (a published spectrum = the stage3 combination of N
spectrum-exposures). Columns mirror `nircam_exposures`: `stage`/state,
`storage_key`, `content_hash`, `review_status`, `masking`, `notes`, with an
explicit `UNIQUE` on the identity tuple (cf. `nircam_exposures_unique`,
tables.sql:1230) and admin-only RLS mirroring `nircam_exposures`
(policies.sql:619-638). Gives NIRSpec "query by reduction stage" symmetric with
NIRCam.

### 5.3 Postgres-resident & non-object products

Some deployed products never touch the bucket and need lifecycle **without** a
`storage_objects` row:
- **Inline SVG thumbnails** `spectra.thumbnail_svg_fnu/_flambda` (deploy.py:778).
- **Pointings JSONB** on `observations` (`deploy pointings`).
- **Photometry** pz JSON *is* an object (`photometry/...json`) but its DB-facing
  rows are also queried directly.

**Decision:** lifecycle (`deploy_status`) attaches to the **logical row**
regardless of where bytes live. Object-backed products additionally get a
`storage_objects` row; Postgres-resident products get only the status column.
State this boundary explicitly so nothing falls through the registry crack.

### 5.4 Lifecycle status on existing rows + deploy command coverage

- `spectra`, `spectrum_exposures`, `nircam_exposures`, `nircam_images`: add
  `deploy_status text not null default 'published' check (... in
  ('in_prep','published','revoked'))`; existing rows backfill to `published`.
- `deployments`: add `status`, `published_at`, `revoked_at` (all nullable for
  existing rows); add an **admin `UPDATE` RLS policy** (today only select-all +
  admin-insert, policies.sql:735-747) **with a `WITH CHECK`** so a revoke cannot
  mutate unrelated columns; add a per-product `deployment_id` FK
  **`ON DELETE SET NULL` (never CASCADE)** so revoke stays recoverable. Backfill
  `deployment_id` on existing rows from `observations.latest_deployment_id` (the
  only available hint; document the heuristic) or leave null.

**Object visibility derives from member spectra (not free).**
`get_filtered_object_ids` filters `objects` only by `o.programs && slugs AND
o.is_active` (functions.sql:1385-1395) — never by member-spectrum status. So an
object whose only spectrum is `in_prep`/`revoked` would still list/map. Fix:
recompute an object-level `has_published_spectrum` flag in
`reconcile_field_objects` at publish/revoke (cheap, mirrors `is_active`), and
gate the object RPC on it. Add a "object whose only spectrum is revoked" test.

**Every deploy subcommand maps to the new model** (none silently orphaned):

| Subcommand | Writes registry? | Gets `deploy_status` / `--in-prep`/publish/revoke? |
|---|---|---|
| spectra (finals), nircam exposures, nirspec spectrum-exposures | yes | yes |
| rgb, sed, json, zfit, photometry, tiles | yes (object-backed) | yes |
| thumbnails, pointings | no (Postgres-resident, §5.3) | status on logical row only |
| slits, shutters | no | follow parent spectrum status |
| sync-programs, fetch-config | no | out of scope (config/metadata) |
| import-masks, pull-masks | n/a (round-trip; §13) | n/a |
| objects reconcile/split/merge/rebuild | no | **kept off the lifecycle path** |

### 5.5 Enforcement (the security core — patch BOTH layers)

1. **RLS policies** on `spectra`/`objects`/`spectrum_exposures`/`nircam_images`
   get `deploy_status='published' OR public.is_admin()`. Covers the **web portal**
   (SECURITY INVOKER RPCs under the user session) and any direct-select path.
2. **Service-role RPCs/routes** bypass RLS, so add the status predicate to the
   `WHERE` clause of `get_spectra_for_sync`, `get_objects_for_sync`,
   `get_photometry_for_sync`, `get_observation_manifest`, and the inline check in
   `/api/v1/spectra` — each taking `p_include_in_prep BOOLEAN DEFAULT false`
   computed from the API caller's `is_admin()` (presign-route pattern). **Never**
   derive admin-ness from `p_program_slugs`.
3. The web-facing `SECURITY INVOKER` filter RPCs also get an explicit
   `p_include_in_prep`/predicate for defense-in-depth and so the admin view can
   request in-prep rows deliberately.
4. **Exit criterion:** an automated test proves a non-admin caller gets **zero**
   `in_prep`/`revoked` rows from *every* RPC and REST route (not "any"). Because
   the sync RPCs use explicit column lists, adding the column is silently
   non-protective — predicate and column ship in the **same** migration.

### 5.6 Audit / observability

Multi-admin + revoke demands an audit trail beyond a single `uploaded_by`.
Add a `deploy_events` log (`actor`, `action ∈ {upload,publish,revoke,recover,
supersede,delete}`, object/row ref, `cfpipe_version`, host, `at`) and an admin
view, mirroring the existing `download_log` + `get_download_stats` pattern
(functions.sql:2709-2771). `revoke`/`recover`/`publish` write audit rows.

> **migra caveats (CLAUDE.md):** column **comments** (used pervasively in
> tables.sql) and matviews are not tracked by `db diff` — every new
> `COMMENT ON COLUMN` and any matview needs a **hand-authored** migration after
> the schema-file edit. Prefer the budgeting RPC over a matview to stay tracked.

---

## 6. Storage migration (R2 → OSN)

**Decision — what moves and what stays.** Only the **data** bucket moves to OSN.
**Tiles stay on R2** (decided), specifically to keep the CDN edge cache for the
map; this is codified as the per-purpose backend split in PR-1, so it is a config
fact, not a migration step. Consequently there is **no `tile_base_url` backfill
and no tile re-host** — the tile path is untouched by this migration.

**OSN egress is free** (it is an academic-use service, not commercial), which
removes the cost concern I previously raised. So the only real constraints on the
download path are (1) OSN has **no R2 binding**, and (2) Vercel function
size/time limits make server-side zip on Vercel a non-starter for multi-GB
bundles. Neither is a cost problem.

**ZIP download — replace the R2-binding Worker with a storage-agnostic streaming
proxy.** The ZIP/batch Worker (`web/workers/download-worker`) uses a Cloudflare
`R2Bucket` binding (`env.R2_BUCKET.get`) and an HMAC-JWT key allowlist. OSN has no
such binding, so the binding is retired — but the **pattern is kept**: a small
streaming-zip proxy (it can still be a Cloudflare Worker, just `fetch()`-ing OSN
over HTTP via presigned/public GET instead of the binding) that verifies the HMAC
token, streams each object, and pipes through a streaming zip
(`archiver`/zip-stream). Streaming ⇒ constant memory regardless of bundle size,
so no Vercel ceiling; free egress ⇒ proxying bytes is fine. This preserves the
current `zipFilename` + key-allowlist semantics. (Alternative: server returns
presigned GETs and the browser zips client-side with `fflate`/`client-zip` —
zero-infra but caps on browser memory and doesn't help the CLI; the streaming
proxy is the primary recommendation.) Fix the JWT `exp` unit if reused — the
Worker compares an `exp` documented in **milliseconds** to `Date.now()`;
standard S3/JWT `exp` is seconds.

**CORS & SigV4.** Browser direct PUT (`upload_files_presigned`) and direct GET
against OSN require **OSN bucket CORS** for the portal origins — an explicit
cutover task. SigV4 presigned URLs bind to the signing host+region; PR-1's
configurable region + `forcePathStyle` are prerequisites.

**Re-key during copy.** Per PR-2, this same copy pass is the once-only window to
canonicalize **data**-bucket keys to the products-relative scheme (the OSN copy
writes to the new keys). Tiles, staying on R2, keep their existing keys.

**Sequence:** registry-driven bulk copy (to canonical keys) + `content_hash`
verify → dual-read shadow (try OSN via `storage_objects.backend`, fall back to
R2) → smoke tests (single GET, ZIP, RGB; tiles untouched) → flip default data
backend → retire R2 data reads (R2 keeps serving tiles).

---

## 7. Cost & budget model

The 20 TB cap is the central constraint, so quantify it (fill with real numbers
before committing):

| Item | Unit size | Count | Subtotal |
|---|---|---|---|
| NIRCam canonical exposures (single state) | ~150 MB | exposures/field × N_fields | _TBD_ |
| NIRCam mosaics + split exts | few GB/tile | tiles × N_fields | _TBD_ |
| NIRCam `superseded` tombstones | ~150 MB | churn × retention | _TBD_ |
| NIRSpec rate + canonical spectrum-exposures | 10s of GB/obs | N_obs | _TBD_ |
| Finals + RGB + SED + photometry + tiles (today) | — | current bucket | _measure_ |

- **Egress is free on OSN** (academic service), so the budget is purely storage,
  not traffic. Tile-serving stays on R2's CDN and is unaffected. So this model
  only has to track bytes-at-rest against the 20 TB cap.
- **Trigger:** a budgeting RPC drives an alert at e.g. 80% of cap; the alert must
  have a remediation lever (§ GC below), or it fires with nothing to do.
- **GC / retention (promote out of "later").** `superseded` tombstones retain
  bytes by definition and NIRCam re-uploads on every hash change. Define when
  `superseded`/`revoked` objects become GC-eligible, the bounded revoke→recover
  window, and a `campfire deploy gc` command tied to the budget alert.

---

## 8. Phased rollout (staggered, non-breaking)

A shared **Foundation**, then two largely-parallel tracks (**A: OSN**, **B:
intermediates + lifecycle**) with explicit safety gates. Each phase is shippable
without the next; each has a rollback.

```
FOUNDATION
  F0  Prereqs        PR-1 (per-purpose backend: data→OSN / tiles→R2, +region+CORS+factory),
                     PR-2 (layout & key contract: paths+keys+bijection+tree classification),
                     PR-3 (NIRSpec canonical)
  F1  Registry       storage_objects (SHADOW index): deploy writes canonical keys; backfill;
                     reconciliation report. NOT authoritative until coverage proven.

TRACK A — OSN (data bucket only; after F1; needs PR-1 in the web download path)
  A1  Copy+verify    registry-driven bulk copy → canonical keys, content_hash verify, dual-read
  A2  Cutover        streaming-zip proxy (replaces R2-binding Worker), OSN CORS, flip data backend
                     (tiles stay on R2 — no tile migration)

TRACK B — Intermediates + lifecycle (after F1)
  B1  Enforcement    deploy_status columns + status predicates in ALL readers (RLS + the four
                     service-role RPCs + manifest + /api/v1/spectra) + admin RPCs + admin gate.
                     Ships with everything 'published' → no user-visible change. SAFETY PREREQ.
  B2  Intermediate deploy + in_prep   NIRCam exposures first, then NIRSpec spectrum-exposures.
                     `deploy --in-prep` gated on a B1 capability marker (refuses otherwise).
  B3  Admin UI       intermediate-products view (clone NIRCam triage) + publish/revoke panel + audit
  B4  Download/local  widen `download` to intermediates (--intermediate/--all, admin) into the
                     mirrored products tree; delete-local (verified-in-cloud interlock);
                     multi-reducer concurrency safety (§9). No separate `recover` verb.

LATER
  L1  Consolidation  GC/retention command, budgeting dashboards, finer roles
```

**Dependency & safety gates (corrected):**
- PR-2 ⟶ F1 (registry must record canonical keys).
- PR-3 ⟶ F1 (NIRSpec schema frozen after consolidation).
- PR-1(web download path) ⟶ any OSN-resident object (A-track + any B object on
  OSN).
- **B1 (status predicates in *every* reader) ⟶ B2 in_prep.** Shipping in_prep
  deploy before all readers filter status is a **live data leak**; `deploy
  --in-prep` must refuse until a DB capability marker proves readers are
  status-aware.
- Tracks A and B are independent in the data model but **both consume
  `storage_objects.backend`/keys**; running them concurrently needs F1's backend
  column live and PR-1 in the web download path.

**Rollback per phase:**
- F1: `storage_objects` is additive → safe to drop.
- A1/A2: dual-read means R2 stays authoritative until the flip; rollback = flip
  back; do not delete R2 bytes until a bake period passes.
- B1: status column defaults `published` and readers tolerate it → revertable;
  feature-flag the predicate behind `p_include_in_prep`.
- B2: requires the B3 soft-revoke to exist as the un-deploy path **or** a defined
  pre-B3 un-deploy that preserves inspection state (do **not** fall back to
  hard-delete `remove.py`, which destroys inspection state).

**F1 partial-window guard:** during F1 the registry is a **shadow** index —
written and reconciled but **not read as authoritative**. No consumer (OSN copy,
budgeting, sync manifest) may treat it as source of truth until a coverage check
proves 100% of live `fits_path`/`file_path`/`png_path` rows have matching
registry rows *and* deploy has written registry rows for a full cycle. SED/RGB
keys (no DB key to backfill from) are enumerated via bucket `LIST` or PR-2
regeneration; orphan bucket objects (no domain row) are classified/adopted in
this step.

---

## 9. Multi-reducer concurrency & the headless-cluster workflow

The user's primary use case (reduce → deploy in-prep on a cluster → inspect in
web → mask → publish; and reduce → deploy → delete local → `download` again to
restore) is multi-actor, so core safety is in **B4**, not deferred to "later".

The restore step is **`download --intermediate` scoped to the reduction**, not a
separate `recover` verb — because the CLI already mirrors the pipeline's
`products/` tree and PR-2's key↔path bijection means a fetched key lands in the
right place automatically. "Restore a re-reducible workspace" = `products/` plus
the cloud-backed `reference/` inputs (PR-2's tree classification), content-
addressed and idempotent. Raw `_uncal` files are *not* in this picture — they
come from MAST via `cfpipe download`.

- **Deploy ownership / optimistic lock.** Two reducers deploying the same
  exposure/observation must not clobber: reuse the `objects.version`
  optimistic-lock pattern, or a coarser per-(observation|field) deploy lock row
  in the DB. A deploy that loses the race re-reads and retries.
- **Register protocol (closes the R2↔DB transactional gap).** Today
  `upload_files_presigned` returns success counts only — no read-back, and the
  presigned PUT signs no checksum (presign/route.ts:142-147). Define: CLI PUTs
  with an `x-amz-checksum-sha256` (store rejects corrupt writes), then calls an
  authenticated `/register` endpoint per object/batch with key+hash+size; the
  server `HEAD`s/validates and writes the `storage_objects` row **only on match**.
  Write-after-verify ⇒ at worst orphan bytes (swept by reconciliation), never
  dangling rows.
- **delete-local interlock:** refuse to delete a local file unless its object is
  registered **and** `content_hash`-verified present in the (current backend)
  store.
- **download/supersede ordering:** a restoring `download` fetches the latest
  non-tombstoned object; a supersede during an in-flight download is resolved by
  the optimistic lock (the manifest is re-read). Reconciliation `LIST` cost for
  million-key buckets is acknowledged and budgeted (incremental, by prefix).

---

## 10. CLI surface

The model stays the two existing file/metadata verbs (`sync` = index, `download`
= files), widened — plus admin lifecycle verbs. Preserve `--dry-run` parity
(universal today):
- `campfire sync` — **unchanged** (metadata/index only).
- `campfire download [scope] --intermediate` (admin) — widen the existing
  `download` from finals to the products **needed to resume reduction** (NIRSpec
  `_rate` + canonical spectrum-exposures at their CFP state, NIRCam canonical
  exposures, + cloud-backed `reference/` inputs), placed into the mirrored
  `products/` tree. `--all` pulls *every* registered object (QA PDFs, previews)
  for the scope. Composes with existing `--obs/--program/--field/--grating/--stale`.
  No separate `recover` verb.
- `campfire delete-local [scope] [--verify|--verify-deep]` (client) — the inverse
  of `download`, with the verified-in-cloud interlock (§9). Marks files
  recoverable-but-absent in the local catalog.
- `campfire deploy publish|revoke` (admin) — lifecycle transitions.
- `campfire deploy gc` (admin) — reclaim GC-eligible superseded/revoked bytes.
- `campfire status` gains a cloud/registry view (published vs in_prep vs
  locally-materialized) alongside its existing local sync state (cli.py:326).

---

## 11. Risks & mitigations

- **Enforcement spread across two surfaces (RLS + service-role RPCs).** The
  in-prep tier is only as safe as the *last* unpatched reader; the §5.5 exit test
  must cover every RPC/route, and B1 must precede B2.
- **NIRCam in-place mutation / budget.** Canonical-file-as-unit only; never
  per-step snapshots; budget RPC + alert + GC (§7).
- **PR-3 touches the scientific core.** Mitigated by the bit-identical
  golden-file gate + the spec3-ASN pre-impl gate + plot/stuck-shutter smoke
  tests; the `_rate` precedent is a model, not a template (pathloss/padding).
- **ZIP download path (not egress — OSN egress is free).** The risk is the R2
  binding and Vercel function limits: replace the binding Worker with a
  storage-agnostic streaming-zip proxy (constant memory); keep R2 for tiles/CDN
  and dual-read data until OSN is proven (§6).
- **Layout contract drift (PR-2).** The pipeline↔CLI↔cloud directory contract is
  defined in multiple places; if the shared module + conformance tests aren't the
  single source, `download`/`delete-local` can place or prune files wrongly.
  Mitigated by PR-2's tested bijection + tree classification.
- **`reconcile.py` aborts on split/merge** (human-in-the-loop). Intermediates
  stay off the clustering path.
- **deployments append-only → updatable.** FK `ON DELETE SET NULL`, scoped
  UPDATE policy with `WITH CHECK`, RLS test that a revoke can't mutate a
  published row's bytes/visibility.
- **Mid-migration schema.** Land on `objects`/`spectra`/registry, not deprecated
  `targets.*`. Seed regeneration is **mandatory** (not conditional) for any phase
  adding columns/tables/FKs, or preview branches go red.

---

## 12. Testing & validation

- **PR-3:** golden-file (`_spec.fits`/`_x1d.fits` byte-identical); spec3-ASN gate;
  `restore_pre_bkgsub` round-trip (padded multi-nod + `pathloss=COMPLETE`);
  plot + stuck-shutter smoke tests; stage3-selection (no-nod, empty-override,
  `SRCFLUX`-absent). New suite under `pipeline/tests/`.
- **PR-1/PR-2:** python↔TS key-conformance test; deploy against a local
  MinIO/S3 stand-in exercising configurable endpoint+region+path-style.
- **F1:** backfill reconciliation report (every live key has a row; no dangling);
  coverage gate before authoritative.
- **B1 (security-critical):** a **new DB-backed RLS/RPC harness** (none exists in
  `python/tests/` today; run against `supabase db reset`) asserting the three
  seed users (`user@`, `viewer@`, `admin@`) and a non-admin API token get zero
  in_prep/revoked rows from every RPC **and** every REST route, while admin sees
  them. Extends `test_deploy_supabase_auth.py`/`test_local_first.py` patterns.
- **A:** copy-verify by `content_hash`; dual-read shadow; ZIP/tile/RGB smoke vs
  OSN before cutover; egress sanity.
- **Seed:** regenerate `supabase/seed.sql` whenever columns/tables/FKs change;
  add `spectrum_exposures`/`storage_objects` admin-RLS rows to the test matrix.

---

## 13. Open questions

- **NIRSpec mask round-trip parity.** `import-masks`/`pull-masks` are NIRCam-only
  (nircam_masks.py); NIRSpec masks + the stuck-shutter/bkg-override TOMLs are
  local-only. If `spectrum_exposures` carries masking, true symmetry needs a
  NIRSpec round-trip; otherwise record the asymmetry as accepted debt.
- **Object versioning:** single-current + `superseded` tombstones (recommended)
  vs a full version chain for rollback.
- **Non-release version gate × in_prep.** Deploying intermediates implies
  `.dev`/non-release `CMPFRVER` almost always (CLAUDE.md warn-and-confirm gate).
  Recommend: `--in-prep` auto-confirms (not public); `publish` re-checks. Decide
  whether intermediate deploys ever need a CHANGELOG entry.
- **Concurrency primitive:** per-row optimistic lock vs per-observation lock row.
- **`reference/` classification (PR-2).** Are NIRCam `flats`/`bad_pixels`/`wisps`/
  `astrom_cats` **cloud-backed** (so `delete-local` → `download --intermediate`
  restores a truly re-reducible workspace) or **regenerable/shared** (excluded
  from the cloud)? Masks are already user-state (DB round-trip); the rest is the
  open call, and it directly determines whether a recovered workspace can actually
  re-run.
- **Optional local-layout tidy.** A one-time re-org of the *local* `products/`
  dirs (e.g. NIRCam `exposures/` vs `mosaics/`; flatter NIRSpec) would be a
  coordinated **pipeline MAJOR** (file-naming change) touching pipeline globbing.
  Out of scope for PR-2 (which keeps local dirs as-is); worth deciding whether to
  bundle it into the same window since the cloud keys are being canonicalized
  anyway.

---

## 14. Decisions recorded

- **Enforcement is in the reader bodies, not RLS alone.** Web portal (SECURITY
  INVOKER) is covered by RLS; the service-role REST surface bypasses RLS and needs
  explicit status predicates + an explicit admin/`p_include_in_prep` param. B1
  (all readers status-aware) precedes B2 (in_prep deploy).
- **NIRSpec no-nod:** canonical SCI stays = cal; `CFP_BKG=skipped`; stage3
  excludes any canonical not background-subtracted (plus the empty-override and
  `SRCFLUX` exclusions).
- **`common/cfp.py` is reused with a separate NIRSpec key set** — separate
  namespaces, shared mechanics (refines the earlier "separate module" framing;
  the module is already common).
- **PR-3 bit-identical for finals** (Infrastructure/PATCH), gated by golden-file +
  spec3-ASN tests.
- **Registry uniqueness `(backend,bucket,storage_key)`**; `fits_path` stays a
  denormalized pointer, not a view/FK; intermediate keys live only in the
  registry.
- **`deployment_id` FK `ON DELETE SET NULL`**; revoke is soft/recoverable.
- **Data → OSN, tiles → R2 (kept for the CDN edge cache)**, codified as a
  per-purpose backend block in PR-1. OSN egress is free, so the ZIP Worker is
  replaced by a storage-agnostic streaming-zip proxy (not retired for cost).
- **No `recover` verb** — `download --intermediate`/`--all` (admin) widens the
  existing `download` into the mirrored `products/` tree; `delete-local` is its
  inverse.
- **Layout & key contract is a prerequisite (PR-2, expanded).** One tested module
  owns local paths, storage keys, the key↔path bijection, and a per-tree
  lifecycle classification — shared across pipeline/CLI/web. **Bucket keys are
  canonicalized to the products-relative scheme during the OSN copy** (the free
  re-key window); the **local dir layout is left unchanged** (renaming it would be
  a pipeline MAJOR).

---

## 15. Appendix: current-state reference

- **Deploy:** `deploy_observation()` pushes finals only; provenance in
  `deployments` + `observations.latest_deployment_id`; `remove.py` hard-deletes;
  `reconcile.py` preserves inspection state across redeploys.
- **Storage:** generic S3 (boto3/aws-sdk); endpoint hardcoded ×4; buckets
  `data`+`tiles`; ZIP via CF Worker `R2Bucket` binding; presigned GET for single
  files; public-URL tiles. `file_hash` (sha256) + `file_size` end-to-end.
- **Read auth:** web portal = user session (anon+cookies) + `SECURITY INVOKER`
  filter RPCs (RLS applies); CLI/download = service-role routes (RLS bypassed,
  hand-written `program_slug` predicates only). `accessible_program_slugs()`
  expands all programs for admins.
- **Pipeline:** NIRCam = one canonical FITS/exposure mutated in place, `CFP_*`
  chain (`common/cfp.py`, generic/key-table-driven); NIRSpec = four suffixed
  files per `(exposure,detector,source)` + per-exposure `_rate`; consumers derive
  s2d paths by string-replace; aggressive cleanup of some intermediates.
- **Sync:** `campfire sync` = metadata-only into SQLite; `campfire download` =
  one FITS/spectrum via presigned URLs; content-addressed verify/diff exists; no
  client-side role awareness.
- **Web:** server actions + RLS for the portal; `/admin/nircam` exposure triage +
  `MaskEditor` is the template for the intermediate view; roles =
  `is_admin`/`can_comment`/`is_group_account`. `app/api/v1/deploy/presign` is the
  working REST admin-gate pattern.
- **DB:** `objects` (user unit) / `spectra` / `targets` (deprecated cols);
  `nircam_exposures` is the only existing lifecycle model; storage refs are bare
  text columns; `deployments` append-only; `download_log` is the audit precedent.

---

## 16. Appendix: adversarial review notes (2026-06-27)

A four-lens critique (pipeline-science, storage-db, sequencing, completeness) was
run against this doc + the codebase. Material corrections folded in:
- **Blocker — RLS is not the only boundary.** The service-role REST surface
  (sync/manifest/signed-URL) bypasses RLS; enforcement must live in the RPC/route
  bodies too, with an explicit admin param (admins can't be inferred from the
  slug array). The filter RPCs are `SECURITY INVOKER` (not `DEFINER`), so RLS
  *does* cover the web portal — both layers are patched (§2.3, §5.5).
- **Blocker — phase order.** in_prep deploy (B2) must follow all-readers-status-aware
  (B1); `deploy --in-prep` is capability-gated (§8).
- **Major — `cfp.py` is already `common/`** and generic → reuse with a separate
  key set, don't duplicate (§3 PR-3, §14).
- **Major — broken `_s2d` consumers** (plots, stuck_shutters derive filenames by
  string-replace) → consumer inventory + smoke tests (§3).
- **Major — registry constraints** (uniqueness, indexes, typed scope FKs, CHECK,
  NOT NULL) and the `fits_path` GENERATED/UNIQUE collision (§5.1).
- **Major — OSN egress** (Worker is egress-free; presigned GET / Vercel zip are
  not), CORS, region, Worker retirement (§6).
- **Major — Postgres-resident products, audit log, cost quantification, deploy
  subcommand coverage, object-visibility-from-member-spectra, multi-reducer
  safety** all added (§5.3, §5.6, §7, §5.4, §9).
- Plus nits: stage3 exclusion completeness, spec3-ASN pre-impl gate, tile
  `tile_base_url` backfill, migra comment caveat, seed/RLS test harness.
