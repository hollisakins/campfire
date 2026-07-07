# Design: NIRSpec web-based review loop — rate-file masks & a live nods renderer

**Status:** draft for review (decisions recorded — see §8)
**Context:** NIRSpec analogue of the [NIRCam deploy overhaul](design-nircam-deploy-overhaul.md);
builds on epic #210 (intermediate products & cloud-as-source-of-truth, R2 → OSN) and the
[intermediate-products design](design-intermediate-products.md).
**Driver:** NIRSpec reduction has two reviewer decisions that today only happen by hand-editing local
region/TOML files on the reducer's machine: (1) **detector-region masks** on `nrs1`/`nrs2` rate files
(persistence trails, MSA shorts) that must be excluded *before* background subtraction so the rest of
the detector is recovered, and (2) the **nod-by-nod source inspection** currently baked into a static
`*_nods.pdf` — the surface where a reducer spots a stuck-closed shutter or decides a nod should be
overridden as background. This design brings both onto the web portal, mirroring the NIRCam
mask-editor loop, so review happens in the browser against cloud-resident FITS and round-trips back to
the pipeline. The pipeline stays fully independent of the web: it reads only local files that `pull`
materializes.

Two features, deliberately on **separate data channels** because they have different grains:

- **Feature 1 — rate-file mask system.** Detector-region masks per `(observation × exposure ×
  detector)`, **source-independent**. Web editor mirrors NIRCam's (fitsgl SCI render, DB-resident
  polygon regions); a `pull` materializes `.reg` files where the pipeline consumes them on the rate
  file *before* stage 2 — exactly like NIRCam, with no `observations.toml` involvement.
- **Feature 2 — live nods renderer.** A web equivalent of `*_nods.pdf` grouping each source's spectrum
  across `nod × detector`, to flag stuck-closed shutters and background overrides. **Source-scoped**
  flags live in a DB table and `pull` materializes them into the two `reference/nirspec/<obs>/` TOMLs
  the pipeline already reads.

---

## 1. Goals / non-goals

### Goals
1. Deploy `nrs1`/`nrs2` **rate files** to OSN as intermediate products (reproducibility + delete-local
   restore) under a canonical `campfire-layout` key — a genuinely new product type and deploy path.
2. A web **rate-mask editor** at `/admin/nirspec`, reusing the NIRCam `MaskEditor` + `FitsCanvas` +
   fitsgl stack, backed by a **new detector-grain DB table** (`nirspec_rate_exposures`, the direct
   `nircam_exposures` analogue).
3. A `pull-rate-masks` path that materializes DB rate-mask regions to `.reg` files under
   `reference/nirspec/<obs>/masks/`, **mirroring NIRCam exactly** — `masks.py` reads those files, not
   `observations.toml`.
4. A web **live nods renderer** grouping the already-deployed `S2D_*` cutouts per source across
   `nod × detector`, with in-browser 1D profiles and shutter overlays, backed by a deploy-populated
   grouping table (`spectrum_exposures`, revived).
5. A **flag round-trip** for stuck-closed shutters and background overrides through a **new live DB
   table**, materialized by `pull` into the two `reference/nirspec/<obs>/` TOMLs the pipeline already
   reads — no pipeline *consumption*-side change.

### Non-goals
- **Data-management config sync (#303).** `programs.toml` / `observations.toml` / `fields.toml`
  current-state upsert is a separate workstream. This design routes all web state through DB channels
  + `pull` materialization and **never** touches `observations.toml` — including for masks, which move
  fully out of the TOML (see §2a, §3.5).
- **Dashboard unification.** New `/admin/nirspec` pages mirror `/admin/nircam` but we do not merge or
  restructure the admin shell.
- **Any NIRCam change.** NIRCam is the template we copy from, not something we touch.
- **Changing the pipeline's mask insertion point or bkgsub timing.** The rate-mask apply point and the
  stuck-shutter/bkg consumption in stage 2 already exist and are correct; we only supply inputs.

---

## 2. Where NIRSpec sits today (the starting line)

Grounded in the investigation. Four facts shape everything below.

**(a) The rate-level detector mask already exists in the pipeline — only its *source* changes.** The
design's "mask the detector, recover the rest, source-independent, `DO_NOT_USE`" is **already fully
implemented** as `pipeline/campfire_pipeline/nirspec/masks.py` (the "manual mask" system). The web
feature is a new *front-end* + *storage channel* for an existing mechanism, **not** a new pipeline
capability. Mechanics we must not break:

- `apply_mask_dq(rate_file, reg_string)` (`masks.py:186`) rasterizes a DS9 region string in **image
  coords** (rate files have no useful sky WCS, `masks.py:66`) and OR's `DO_NOT_USE` into the rate
  `.dq`, recording the flipped pixels in a `CFDQMASK` uint8 extension so the OR is **cleanly
  reversible** (`clear_manual_mask_dq`, `masks.py:220`).
- Consumed by `subtract_background_from_rate_file` (`stage1.py:476`), which drops masked pixels from
  the background fit (`slitmask[model.dq > 0] = False`, `stage1.py:533`). No separate jwst "mask step"
  — a direct DQ OR every downstream step honors.
- Staleness by `CFMASKSH` = `sha256[:12]` of the canonicalized region string (`hash_mask`,
  `masks.py:54`); `is_stale` (`masks.py:155`) drives automatic re-apply (`bkgsub_with_masks`,
  `masks.py:387`; `ensure_fresh`, `masks.py:428`). The rate tier keeps its own `CFBKGSUB`/`CFMASKSH`
  sentinels and is deliberately **outside** the canonical CFP chain (`cfp.py:108-109`); the reserved
  canonical `CFP_MASK` slot is a *different* concept and unused here.

**The one pipeline change** (decided, OQ-1): today `masks.py`'s authoritative region source is
`observations.toml [<obs>.masks]` (`obs.manual_masks`), from which transient `.reg` mirrors are
derived one-way into `workspace_dir/manual_masks/` (`masks.py:98-102,118`). We **retire that TOML path
entirely** and mirror NIRCam: `.reg` files under `reference/nirspec/<obs>/masks/`, filename-matched to
rate files, become the authoritative input. The maintainer has never used the `observations.toml
[masks]` path, so **there is no data to migrate** — we delete the read path and repoint it.

**(b) Rate files are not deployed and not a layout product.** No `_rate.fits` entry exists in the
layout suffix tables (`bijection.py:32-42`) or product registry (`products.py`); a bare `.fits` that
isn't a canonical `_nrs[12]_<source>.fits` falls through `_nirspec_obs_product` to
`nirspec_spectrum_exposure` (`bijection.py:58-64`) — so a `_rate.fits` would be **silently
mis-parsed**. Adding `nirspec_rate` is the single most bug-prone edit (see §3.1, §9-P0). The existing
`nirspec_manual_mask` product (`products/nirspec/<obs>/manual_masks/`, `products.py:214`) is
**retired** — masks move to the NIRCam-parallel `reference/.../masks/` location and, like NIRCam
masks, are **not** registered in `storage_objects` (DB-resident; `pull` materializes).

**(c) `spectrum_exposures` is a dead scaffold that we will *revive* (revised) as the nods grid.** The
table exists (`tables.sql:785-800`) — per `(spectrum_id, exposure_ref)`, **FK to `spectra.id` ON
DELETE CASCADE**, with `root/nod/detector/source_id/grating/review_status/masking/notes` — but
**nothing reads or writes it** (only a b2_lifecycle RLS test inserts a row); deploy registers the
per-source FITS in `storage_objects` (`nirspec_spectrum_exposure`) but never inserts a
`spectrum_exposures` row (`deploy.py:655-663`). Its current shape is **broken for our use**: the
`spectrum_id NOT NULL` FK to `spectra` (the *combined* product) can't be satisfied for an
intermediates-only deploy, which happens *before* stage-3 combine makes any spectrum row (the same
"review precedes combine" fact as OQ-5). **Decision (OQ-4/OQ-7):** revise it — drop the hard
`spectra` FK, key it on `(observation, exposure_root, nod, detector, source_id)`, add render columns
(`storage_key`, dims) and an `exp_group` column, and **populate it at deploy** so the nods grid
matches the pipeline's grouping exactly. It is the render *grid* only; the editable flags live
elsewhere (§2d, §4.3).

**(d) Stuck-shutter / bkg-override state lives in two local TOMLs** under `reference/nirspec/<obs>/`,
both consumed by stage 2. Verified grains:

| File | Grain | Structure | Consumed at | Reaches cloud today? |
|---|---|---|---|---|
| `stuck_closed_shutters.toml` | `(root, source_id)` → `[shutter ordinals]` | `[root]` table; `source_id = [1,2,3]` (`observation.py:307-344`) | `stage2.py:708-736` (drops shutter columns; `STKSHTRS`; `_nodata` if all stuck) | Only a per-deployment **snapshot** in `deployments.stuck_shutters` jsonb (`deploy.py:823-834`) — not editable |
| `nodded_background_overrides.toml` | `(root, source_id)` → `{nod: [bkg nods]}` | `[root]` table; `source_id = {3=[1]}` (nod nested; `observation.py:346-381`) | `stage2.py:1144-1155` (`CFP_BKG='excluded:override'` when empty) | **No** cloud path |

Both `nirspec_stuck_shutters` / `nirspec_bkg_override` are **reserved** as layout product names
(`products.py:219-230`, `storage_objects` CHECK `tables.sql:882-883`) but **no deploy uploads them**.
**Decision (OQ-8/OQ-10):** the editable flags do **not** use those file-product slots (which stay
unused) — they get a **new live DB table** (§4.3), and `pull` materializes the TOMLs locally, exactly
like the NIRCam `.reg` pull. Note the multi-writer wrinkle on `stuck_closed_shutters.toml`: the
pipeline auto-detects (`detect_stuck_shutters` + `merge_stuck_shutters`, manual-priority,
`stuck_shutters.py:393-429`) *and* it can be hand-edited — so the web pull must **merge**, not
overwrite (OQ-9). Bkg overrides are hand-authored only (no auto-writer), so a clean overwrite is safe.

---

## 3. Feature 1 — rate-file mask system

### 3.1 Deploy the rate file (new product)

Adding `nirspec_rate` is the well-worn `nirspec_spectrum_exposure` path, one pipeline tier earlier.
Additive changes:

- **Layout** (`layout/campfire_layout/products.py`, alongside `nirspec_spectrum_exposure` at
  `:118-122`):
  ```python
  _register(ProductSpec(
      name="nirspec_rate", instrument=NS, tree="products", bucket="data",
      lifecycle=LC.CLOUD_PRODUCT, scope_keys=("obs",), subdir=_nirspec_obs_dir,
      suffix="_rate.fits", legacy_prefix=None))
  ```
- **Bijection — the bug-prone edit.** `_nirspec_obs_product` routes *any* non-`_spec.fits` `.fits` to
  `nirspec_spectrum_exposure` (`bijection.py:62-63`), so a `_rate.fits` would be **silently
  mis-parsed**. Add `("_rate.fits", "nirspec_rate")` to `_NIRSPEC_OBS_SUFFIXES` (`bijection.py:32-42`)
  **before** the `.fits` fallback, and mirror it in `web/lib/layout.ts` (`NIRSPEC_OBS_SUFFIXES` `:183`,
  `nirspecObsProduct` `:207`, spec `:77`). The golden round-trip test (`layout.test.ts`) needs a
  `nirspec_rate` row.
- **exposure_ref.** Add `nirspec_rate` to `_exposure_ref_for` (`registry.py:120-133`) so each rate
  object gets a stable `exposure_ref` (rootname stem `jw..._nrs1_rate`, encoding detector + exposure)
  backing the partial-unique `(product_type, exposure_ref) WHERE status='active'`. Add to
  `DEFAULT_COPY_PRODUCT_TYPES` (`registry.py:844-850`).
- **CHECK enum.** Add `'nirspec_rate'` to the `storage_objects.product_type` CHECK
  (`tables.sql:876-885`) — a hand-authored additive migration (see §5).
- **Deploy path.** Add `discover_rate_files(obs_dir)` (mirrors `discover_spectrum_exposures`) and an
  upload block in **both** NIRSpec entry points — full `deploy_observation` (`deploy.py:652-664`) and
  `_deploy_intermediates_only` (`deploy.py:300-310`). The existing `upload_files_parallel(...,
  backend='osn')` + `build_registry_rows` + `upsert_storage_objects` handle the rest — **no new
  upload/presign plumbing**.
- **No compression (OQ-2).** Rate files deploy **uncompressed** — the web SCI decoder rejects fpack
  `ZIMAGE` (§3.4). This is a deploy convention, not a code branch.

**Scope note:** rate files are source-independent — deploy the whole detector **once per
obs×exposure×detector regardless of `--source-ids`**, unlike the spectrum-exposure block which filters
by source (`deploy.py:657-658`).

### 3.2 New schema — a detector-grain rate-mask table

`spectrum_exposures` is the wrong grain (it's per-source; masks are not). Model a **new** table
directly on `nircam_exposures` (`tables.sql:734-756`), keyed on `(observation, exposure_root,
detector)`:

```sql
CREATE TABLE nirspec_rate_exposures (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation    text NOT NULL,             -- observations.name (NOT an FK to spectra)
    exposure_root  text NOT NULL,             -- rate rootname stem, e.g. jw07076020001_04101_00001
    detector       text NOT NULL,             -- 'nrs1' | 'nrs2'
    filename       text NOT NULL,             -- <root>_<detector>_rate.fits
    grating        text,
    image_width    int,
    image_height   int,
    storage_key    text,                      -- canonical nirspec_rate key for the fits route
    stage          text NOT NULL DEFAULT 'rate',
    review_status  text NOT NULL DEFAULT 'pending',   -- pending | approved | excluded
    masking        text NOT NULL DEFAULT 'none',      -- none | needed | done
    mask_regions   jsonb,                     -- {version, polygons:[{id, vertices:[[x,y]...]}]}
    notes          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT nirspec_rate_exposures_unique UNIQUE (observation, exposure_root, detector)
);
```

- `mask_regions` jsonb reuses NIRCam's shape verbatim (`MaskEditor.tsx`, `nircam_masks.py:164-168`):
  vertices in **DS9 `image` 1-indexed** coords. The pipeline consumes image coords already
  (`masks.py:66`), so this is a clean match — no WCS projection like NIRCam's `import-masks`.
- **No FK to `spectra`** (OQ-5, confirmed): rate masks gate stage-3 combine, which *produces* spectra;
  the review must exist before any spectrum row. Key/scope to `observations.name`.
- RLS **admin-only**, mirroring `nircam_exposures` (`policies.sql:654-678`); deploy writes via
  service_role.
- Indexes: btree `(observation)` + a partial `review_status WHERE review_status <> 'approved'`
  review-queue index (mirror `indexes.sql:343-353`).

**Split-ownership upsert** (the load-bearing pattern from `nircam.py:704-742`): the deploy
re-register UPDATE writes **only** identity/render columns and **omits**
`review_status`/`mask_regions`/`notes`, so re-deploy never clobbers web triage; new rows seed
`review_status='pending'`, `masking='none'`.

### 3.3 Pipeline apply point (unchanged)

`DO_NOT_USE` OR into the rate `.dq` **before** `subtract_background_from_rate_file`, reversible via
`CFDQMASK`, staleness by `CFMASKSH`. A web-edited region with a changed hash triggers
`bkgsub_with_masks` / `ensure_fresh` re-apply automatically. **No CFP change**, no new provenance
keyword — the rate tier stays outside the CFP chain.

### 3.4 Web editor (reuse)

Almost entirely NIRCam reuse — "the NIRCam `MaskEditor` pointed at a `nirspec_rate` key":

- **FITS proxy** `web/app/api/nircam-fits/route.ts` — admin-gated Range-forwarding proxy, resolves the
  object's backend from `storage_objects` (`:73-78`), with a `CAMPFIRE_LOCAL_DATA_ROOT` filesystem
  fast-path so a reducer on the same machine renders **before** deploy (`:52-67`). The **only**
  NIRCam-specific line is the `parseKey(key).productType !== 'nircam_exposure'` guard (`:46`):
  generalize to `{'nircam_exposure','nirspec_rate','nirspec_spectrum_exposure'}` (the third serves the
  nods renderer, §4) or clone as `/api/nirspec-fits`.
- **SCI decoder** `web/lib/fits/fetch.ts` `fetchSciImage` (`:95-132`) — two Range GETs (header window
  → SCI float32 block). Rate SCI is the full 2048² detector, same size class as NIRCam. It throws on
  `BITPIX != -32` (`:117`) and on fpack `ZIMAGE` (`:120`) — resolved by deploying **uncompressed**
  (OQ-2). Orientation: the canvas frame is DS9 `image` 1-indexed with a Y-flip assuming
  `origin='lower'` (`MaskEditor.tsx:74-79`); no reason to expect NIRSpec rate SCI differs, but
  **confirm during P3** (OQ-3).
- **Canvas + editor** `FitsCanvas.tsx` + `MaskEditor.tsx` — instrument-agnostic; reused verbatim except
  parametrizing the hardcoded route URL.
- **Persist** — mirror `saveExposureMaskRegions` (`nircam-exposures.ts:321-352`): writes `mask_regions`
  jsonb and flips `masking` to `'done'` iff ≥1 polygon. No PNG presign — the live-FITS path renders
  directly.

### 3.5 The pull → consume loop (decided)

Mirror NIRCam **exactly**. A new CLI verb `campfire deploy nirspec pull-rate-masks --obs X`
(modeled on `nircam_masks.pull_masks`, `nircam_masks.py:241-305`): `SELECT mask_regions` per
`(observation, exposure_root, detector)` → serialize to DS9 `image`-coord `.reg` files under
`reference/nirspec/<obs>/masks/` (filename-matched to the rate file, e.g.
`<root>_<detector>.reg`), atomic tmp-write + replace, `# Generated by campfire deploy` header, full
overwrite = reversible, only non-null rows written. DB-resident, **not** registered in
`storage_objects` (like NIRCam masks).

**The pipeline change:** `masks.py` reads its region strings from those `.reg` files instead of
`observations.toml`. `apply_mask_dq` already takes a region *string* — only the *source* moves.
Concretely: replace the `obs.manual_masks`/`materialize_reg_files` path (`masks.py:98-121`,
`observation.py:170`) with a read of `reference/nirspec/<obs>/masks/<root>_<detector>.reg`. No
back-compat shim (the `observations.toml [masks]` path was never used). Rides a **pipeline tag**
(Infrastructure/PATCH — no change to scientific output, only the region source seam); add the
`## Unreleased` CHANGELOG entry.

---

## 4. Feature 2 — live nods renderer

### 4.1 Data source — reuse the already-deployed spectrum-exposures

**No new deploy target is needed for the render.** The `*_nods.pdf` (`plots.py:282-478`) draws the
`S2D_SCI` / `S2D_BKGSUB_SCI` 2D arrays, which are **named HDUs on the canonical
`nirspec_spectrum_exposure` FITS** (`canonical.py:20`) — **already uploaded to OSN and registered in
`storage_objects`** (`deploy.py:300-310,655-663`). Everything else the PDF shows is derived:

- **1D cross-dispersion profile** = `np.nanmedian(S2D, axis=1)` at plot time (`plots.py:401,411,460`)
  — compute client-side.
- **Normalization** = shared ZScale/MAD across panels (`plots.py:345-365`) — client-side.
- **Overlays** = `SHUTSTA` / `STKSHTRS` / `SRCFLUX` header cards (`plots.py:341-342,397-398`) —
  range-fetchable from the FITS header.

The FITS render stack transfers directly: generalize `findSci` to target `S2D_SCI`/`S2D_BKGSUB_SCI`
(`fetch.ts:62-75`) and widen the proxy guard to allow `nirspec_spectrum_exposure` (§3.4). Range-fetch
is cheaper here — S2D cutouts are small rectified slitlets, not the full detector.

**S2D presence (OQ-6, decided).** `S2D_*` HDUs are written only if a plot/rectify step ran
(`canonical.py:20-21`). Policy: **always rectify before deploying intermediates** (deprecate the
ability to turn rectify off), and the renderer **fails gracefully** with an explanatory "no rectified
view — re-run with rectify" message if `S2D_SCI` is absent rather than erroring.

### 4.2 The source × nod × detector grouping model (deploy-populated)

The nods grid is **rows = (`exp_group`, `nod`), columns = detector ∈ {nrs1, nrs2}**, filtered to one
`(observation, source_id)` — exactly the PDF's layout (`plots.py:305-325`). The web must reproduce the
pipeline's grouping *exactly*, so we **populate it at deploy** rather than reconstruct it client-side
(OQ-4/OQ-7).

**The catch — and its fix.** `exp_group` is **not** in a header or the filename; it is *computed* by
`observation.py:529-605`, which assigns group ids by subpixel-dither position across the whole
exposure set. Neither deploy (which cannot import the pipeline) nor the web can reproduce it
faithfully. **Decision (OQ-7):** the pipeline **stamps the computed `exp_group` onto each canonical
spectrum-exposure FITS header** (an additive card, mirroring the Phase-3 `CMPFRVER` stamp — a pipeline
Infrastructure/PATCH change). Deploy then reads it (it already opens headers) and writes a revived
`spectrum_exposures` row per `(observation, exposure_root, nod, detector, source_id)` carrying
`exp_group`, `storage_key`, dims, `grating` — an exact match to pipeline grouping, by construction.

`spectrum_exposures` is thus **revived and revised** (§2c): drop the `spectrum_id NOT NULL` FK to
`spectra`, re-key to `(observation, exposure_root, nod, detector, source_id)`, add `exp_group` +
render columns. It is the render *grid* backbone only — **no flags live here** (they'd duplicate
across the nod/detector rows; see §4.3). Deploy upsert uses the same split-ownership pattern as the
rate table.

### 4.3 The flag → metadata → pull-back loop

Two source-scoped annotations. Because both are per-`(root, source)` — coarser than the grid's
per-`(root, nod, detector, source)` — they do **not** belong on `spectrum_exposures` (folding them in
duplicates each flag across every nod/detector row and forces a consistency burden). Instead, **one
new live table** at the natural grain (OQ-4/OQ-8):

```sql
CREATE TABLE nirspec_source_review (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation    text NOT NULL,
    exposure_root  text NOT NULL,
    source_id      int  NOT NULL,
    stuck_shutters jsonb,   -- [1,2,3]     ordinal list  (mirrors stuck_closed_shutters.toml)
    bkg_overrides  jsonb,   -- {"3":[1]}   {nod: [bkg nods]}, nod nested (mirrors the bkg TOML)
    notes          text,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT nirspec_source_review_unique UNIQUE (observation, exposure_root, source_id)
);
```

Both flag types collapse into **one** table (not two): the bkg nod-dimension lives *inside* the jsonb,
mirroring the TOML's own nesting (`source_id = {nod = [...]}`) so the pull serializes 1:1. Admin-only
RLS; web-editable; **not** deployed to OSN (OQ-10 — these need live editing; `deployments.stuck_shutters`
is a per-deploy *snapshot*, not this).

**The consumption side needs no change** — stage 2 already reads both TOMLs (`stage2.py:708-736`,
`:1144-1155`). What's built:

1. **New pull verbs** `campfire deploy nirspec pull-stuck-shutters` / `pull-bkg-overrides` (modeled on
   `pull_masks`/`pull_exclusions`, `nircam_exclusions.py:34-79`): atomic, reversible, generated-header.
2. **Bkg overrides** — clean full-overwrite (no auto-writer to conflict with).
3. **Stuck shutters — authority merge (OQ-9).** Ranking **hand > web > auto**. Because hand and web are
   both "manual identification," the pull **merges** rather than overwrites: it writes provenance tags
   (`# hand` / `# web` / `# auto`) so a re-pull preserves hand entries, overlays web entries, and lets
   auto fill gaps. Reuse/extend `merge_stuck_shutters` (`stuck_shutters.py:393-429`, already
   manual-priority) with a `web` tier below `hand` and above `auto`.

The flag UI sits directly on the rendered source — a stuck-shutter toggle per shutter boundary, a
bkg-override control per nod row.

---

## 5. Schema changes (concrete sketch)

All additive and hand-authored where the diff engine can't help. Per AGENTS.md: edit
`supabase/schemas/*.sql`, `supabase db reset`, `supabase db diff -f <desc>`, review, commit both; the
Supabase preview branch validates on PR. **One schema-changing PR to `main` at a time**; **regenerate
`seed.sql`** on any new-column/table add or preview branches go red.

| Change | File | Diff-tracked? | Caveat |
|---|---|---|---|
| `nirspec_rate` in `storage_objects.product_type` CHECK | `schemas/tables.sql:876-885` | ✅ | — |
| New `nirspec_rate_exposures` table + unique + review-queue index + admin RLS | `tables.sql`/`indexes.sql`/`policies.sql` | ✅ | Reserved-word check: `stage`,`notes`,`grating` fine |
| **Revise** `spectrum_exposures`: drop `spectra` FK, re-key `(observation, exposure_root, nod, detector, source_id)`, add `exp_group`/`storage_key`/dims | `tables.sql:785-800` | ✅ (but see caveat) | Table is dead (no data) so a drop+recreate in one migration is safe; **regenerate seed** |
| New `nirspec_source_review` table (§4.3) + admin RLS | `tables.sql`/`policies.sql` | ✅ | jsonb columns; `source_id` is int |
| `exp_group` header stamp on canonical spectrum-exposure FITS | `pipeline/` (not schema) | n/a | Pipeline Infrastructure/PATCH; enables exact deploy-time grouping |

The `nirspec_rate` layout suffix + `web/lib/layout.ts` mirror are **not** schema and ride with §3.1.
Matviews/comments/partitions aren't diff-tracked — none of the above touches those. The reserved
`nirspec_manual_mask` / `nirspec_stuck_shutters` / `nirspec_bkg_override` product slots are left
unused (the first is retired; the latter two were never wired and stay dormant).

---

## 6. Web surface

Mirror `/admin/nircam`; add `/admin/nirspec` review pages. Reused vs new:

| Piece | NIRCam source | For NIRSpec |
|---|---|---|
| Rate-mask **list** page | `app/admin/nircam/page.tsx` | New `app/admin/nirspec/rate/page.tsx` — `AdminTable`/`AdminFilterBar`, URL-state filters, review/masking badges |
| Rate-mask **detail** cockpit | `app/admin/nircam/[id]/page.tsx` | New — in-memory cache, ±window prefetch, keyboard shortcuts, the `MaskEditor` pane |
| **Nods renderer** page | (no analogue) | New `app/admin/nirspec/nods/[source]/page.tsx` — rows=(`exp_group`,`nod`) × cols=detector grid from the revived `spectrum_exposures`, per-detector 1D profile, shutter/nod flag controls |
| `MaskEditor` / `FitsCanvas` | `components/nircam/` | Reused verbatim (parametrize route URL) |
| FITS proxy | `app/api/nircam-fits/route.ts` | Generalize guard (`nirspec_rate` + `nirspec_spectrum_exposure`) or clone `/api/nirspec-fits` |
| Server actions | `lib/actions/nircam-exposures.ts` | New `lib/actions/nirspec-rate.ts` (mask review) + `lib/actions/nirspec-nods.ts` (grid + flags); all `requireAdmin()`-gated |
| Nods flag actions | (no analogue) | `saveStuckShutter` / `saveBkgOverride` writing `nirspec_source_review` (§4.3) |

All new pages are admin-only and **merge inert** — no public surface is touched.

---

## 7. The local ↔ cloud loop, end to end

**Feature 1 (rate masks):**
```
cfpipe nirspec run --obs X (produces _rate.fits, local)
  → campfire deploy --obs X            # NEW: uploads nrs1/nrs2 _rate.fits (uncompressed) to OSN,
                                        #      registers, upserts nirspec_rate_exposures (pending)
  → /admin/nirspec/rate  (admin)       # fitsgl renders rate SCI; draw DO_NOT_USE polygons
  → saveMaskRegions → mask_regions jsonb, masking='done'
  → campfire deploy nirspec pull-rate-masks --obs X   # NEW: DB → .reg at reference/nirspec/X/masks/
  → cfpipe nirspec run --obs X         # masks.py reads .reg (NOT observations.toml), OR's DO_NOT_USE
                                        #   into rate .dq BEFORE bkgsub; CFMASKSH triggers re-apply
  → campfire deploy --obs X            # re-upload rate (content-hash change) + downstream products
```

**Feature 2 (nods flags):**
```
cfpipe nirspec run --obs X (canonical _nrs[12]_<source>.fits w/ S2D_* + stamped exp_group; deployed)
  → campfire deploy --obs X            # populates revived spectrum_exposures grid rows (exp_group)
  → /admin/nirspec/nods/<source>  (admin)   # renders S2D rows=(exp_group,nod) × cols=detector,
                                             #   client 1D profile, SHUTSTA overlay
  → mark stuck shutter / set bkg override → nirspec_source_review (DB, editable)
  → campfire deploy nirspec pull-stuck-shutters / pull-bkg-overrides --obs X   # NEW
       → reference/nirspec/X/stuck_closed_shutters.toml       (authority merge: hand>web>auto, OQ-9)
       → reference/nirspec/X/nodded_background_overrides.toml (clean overwrite)
  → cfpipe nirspec run --obs X         # stage2 already consumes both TOMLs (no change)
```

Both loops are non-destructive and reversible: DB is authoritative, `pull` materializes, re-run
re-applies. Delete-local/restore works for rate files once §3.1 registers them.

---

## 8. Decisions recorded

Resolved with the maintainer (formerly OQ-1…OQ-11):

1. **Rate-mask delivery — mirror NIRCam, no TOML (OQ-1).** `.reg` files under
   `reference/nirspec/<obs>/masks/`, DB-resident + `pull`; `masks.py` reads them instead of
   `observations.toml`; `nirspec_manual_mask` retired; no migration (the TOML `[masks]` path was never
   used).
2. **Deploy rate files uncompressed (OQ-2).** Avoids the SCI decoder's `ZIMAGE` rejection.
3. **No new tables for the flags beyond one (OQ-8).** Stuck + bkg share one `nirspec_source_review`
   table via two jsonb columns; the reserved file-product slots stay unused.
4. **Grid populated at deploy, not reconstructed (OQ-4/OQ-7).** Revive+revise `spectrum_exposures` as
   the render grid; the pipeline stamps `exp_group` in the header so deploy populates it exactly.
5. **Rate-mask table does not FK to `spectra` (OQ-5).** Review precedes combine.
6. **Always rectify before deploy; graceful web fallback if `S2D_SCI` absent (OQ-6).**
7. **Flags are DB-only; `pull` materializes the TOMLs (OQ-10).** Not pushed to OSN.
8. **Stuck-shutter authority: hand > web > auto, via a provenance-tagged merge (OQ-9).** Bkg overrides
   overwrite cleanly.

**To verify during implementation (not gating):**
- **Orientation (OQ-3).** Confirm rate SCI matches the `MaskEditor` `origin='lower'` Y-flip.
- **`exp_group` stamp fidelity (OQ-7).** Confirm the stamped card round-trips through
  `_deploy_intermediates_only` and the full deploy identically, and that a re-save doesn't drop it.
- **Bkg nod identity (OQ-11).** The bkg TOML uses raw exposure-sequence integers (TACONFIRM-shifted),
  not sequential indices — the renderer must surface and the pull must write the same integer; and the
  "drop this nod" case (empty list → `CFP_BKG='excluded:override'`) needs a UI affordance distinct from
  "use nods [X]".

---

## 9. Proposed phased breakdown (each mergeable-inert)

```
P0 layout contract ─────────────┬───────────────────────────────────────────────┐
   (nirspec_rate spec + suffix   │                                               │
    bijection, py+TS, tests)     │                                               │
                                 │                                               │
Track R (rate masks):            │  Track N (nods renderer):                     │
  P1 rate deploy → OSN ──────────┤    P4 exp_group header stamp (pipeline) +     │
     + storage_objects enum      │       revive/revise spectrum_exposures +      │
  P2 nirspec_rate_exposures      │       deploy-populate grid                    │
     table + RLS + deploy upsert  │    P5 generalize fits proxy + findSci for     │
  P3 web rate-mask editor        │       S2D_SCI; nods renderer page (render)     │
     (reuse MaskEditor)          │    P6 nirspec_source_review table + flag UI   │
  P3b pull-rate-masks +          │    P7 pull-stuck-shutters / pull-bkg-overrides │
      masks.py .reg source ──────┘       + authority merge (OQ-9)               │
                                                                                 │
(config sync #303 — OUT OF SCOPE, separate epic) ────────────────────────────────┘
```

- **P0 — Layout contract first.** `nirspec_rate` ProductSpec + `_rate.fits` bijection suffix (python +
  TS) with the round-trip conformance test. Inert. **The suffix edit is the single most bug-prone
  step** (silent mis-parse as spectrum-exposure).
- **P1 — Rate deploy → OSN** (deps P0). `discover_rate_files` + upload block in both entry points,
  uncompressed; `nirspec_rate` in the CHECK enum, `_exposure_ref_for`, `DEFAULT_COPY_PRODUCT_TYPES`.
- **P2 — `nirspec_rate_exposures` table** (deps P1). Table + unique + partial index + admin RLS +
  split-ownership deploy upsert. Regenerate `seed.sql`.
- **P3 — Web rate-mask editor** (deps P2, P5-proxy). `/admin/nirspec/rate` pages + actions, reusing
  `MaskEditor`/`FitsCanvas`. Resolve OQ-3 here.
- **P3b — pull + pipeline `.reg` source** (deps P3). `pull-rate-masks` (mirror `pull_masks`) +
  `masks.py` reading `reference/nirspec/<obs>/masks/*.reg` instead of `observations.toml`. Pipeline
  tag (Infrastructure/PATCH); `## Unreleased` CHANGELOG entry.
- **P4 — exp_group stamp + grid** (deps none beyond pipeline). Pipeline stamps `exp_group` on the
  canonical FITS (Infrastructure/PATCH); revive/revise `spectrum_exposures` (drop FK, re-key, add
  columns) and populate at deploy. Regenerate `seed.sql`. Inert (admin-read grid).
- **P5 — Nods renderer (render only)** (deps P4). Generalize the fits proxy + `findSci` for
  `S2D_SCI`/`S2D_BKGSUB_SCI`; the rows=(exp_group,nod) × cols=detector grid + client 1D profile +
  `SHUTSTA` overlay. Read-only, inert.
- **P6 — Editable flag channel + UI** (deps P5). `nirspec_source_review` + stuck/bkg controls on the
  nods view. Regenerate `seed.sql`. Inert (admin-only writes).
- **P7 — Flag pull-back** (deps P6). `pull-stuck-shutters` / `pull-bkg-overrides` with the authority
  merge (OQ-9). No pipeline *consumption* change — stage 2 already reads both TOMLs — so PATCH at most
  if any `pipeline/**` file is touched.

**Sequencing & merge safety.** P0 first; then Track R (P1→P2→P3→P3b) and Track N (P4→P5→P6→P7) run
largely parallel, joined at the shared fits proxy (P5). Every step merges inert (admin-only or a
non-public intermediate). One schema-changing PR at a time off the squashed baseline; regenerate
`seed.sql` on P2, P4, P6. Pipeline-touching PRs (P3b, P4, and P7 if it touches `pipeline/**`) carry a
categorized `## Unreleased` entry — all **Infrastructure/PATCH** (no change to pixel/flux values for
the same input; the `exp_group`/mask-source changes are provenance/plumbing, not calibration).

---

## 10. Definition of done

- `campfire deploy` uploads `nrs1`/`nrs2` **rate files** (uncompressed) to OSN under a canonical
  `nirspec_rate` key, registered in `storage_objects`, with a `nirspec_rate_exposures` triage row —
  the NIRSpec analogue of NIRCam exposure parity, and enough for delete-local/restore.
- The admin `/admin/nirspec/rate` page renders the rate SCI **in-browser** (fitsgl), reviewers draw
  source-independent `DO_NOT_USE` polygons, and `pull-rate-masks` materializes `.reg` under
  `reference/nirspec/<obs>/masks/` where `masks.py` reads them (no `observations.toml`) **before**
  stage 2 — a closed `run → deploy → mask → pull → run` loop honoring the reversible DQ/`CFMASKSH`
  contract.
- A live **nods renderer** reproduces `*_nods.pdf` in the browser from the already-deployed `S2D_*`
  cutouts, grouped per source across (`exp_group`,`nod`) × detector via the deploy-populated
  `spectrum_exposures` grid, and stuck-shutter / bkg-override flags round-trip through
  `nirspec_source_review` + `pull` into the two `reference/nirspec/<obs>/` TOMLs stage 2 already reads.
- Both features merge inert; config sync (#303) is not entangled.
