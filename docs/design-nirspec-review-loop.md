# Design: NIRSpec web-based review loop — rate-file masks & a live nods renderer

**Status:** draft for review
**Context:** NIRSpec analogue of the [NIRCam deploy overhaul](design-nircam-deploy-overhaul.md);
builds on epic #210 (intermediate products & cloud-as-source-of-truth, R2 → OSN) and the
[intermediate-products design](design-intermediate-products.md).
**Driver:** NIRSpec reduction still has two reviewer decisions that only happen by hand-editing
local TOML/region files on the reducer's machine: (1) **detector-region masks** on `nrs1`/`nrs2`
rate files (persistence trails, MSA shorts) that must be excluded *before* background subtraction so
the rest of the detector is recovered, and (2) the **nod-by-nod source inspection** currently baked
into a static `*_nods.pdf` — the surface where a reducer spots a stuck-closed shutter or decides a
nod should be overridden as background. This design brings both onto the web portal, mirroring the
NIRCam mask-editor loop, so review happens in the browser against cloud-resident FITS and round-trips
back to the pipeline.

Two features, deliberately kept on **separate data channels** because they have different grains:

- **Feature 1 — rate-file mask system.** Detector-region masks per `(observation × exposure ×
  detector)`, **source-independent**. Web editor mirrors NIRCam's (fitsgl SCI render, DB-resident
  polygon regions); a `pull` materializes them where the pipeline consumes them on the rate file
  *before* stage 2.
- **Feature 2 — live nods renderer.** A web equivalent of `*_nods.pdf` grouping each source's
  spectrum across `nod × detector`, to flag stuck-closed shutters and background overrides.
  **Source-scoped** flags round-trip via the existing stuck-shutter / bkg-override metadata channel.

---

## 1. Goals / non-goals

### Goals
1. Deploy `nrs1`/`nrs2` **rate files** to OSN as intermediate products (reproducibility + delete-local
   restore) under a canonical `campfire-layout` key — a genuinely new product type and deploy path.
2. A web **rate-mask editor** at `/admin/nirspec`, reusing the NIRCam `MaskEditor` + `FitsCanvas` +
   fitsgl stack, backed by a **new detector-grain DB table** (the direct `nircam_exposures` analogue).
3. A `pull` path that materializes DB rate-mask regions to the region format the pipeline already
   consumes on the rate file, honoring the existing reversible DQ-OR contract in `masks.py`.
4. A web **live nods renderer** grouping the already-deployed `S2D_*` cutouts per source across
   `nod × detector`, with in-browser 1D profiles and shutter overlays.
5. A **flag round-trip** for stuck-closed shutters and background overrides that terminates by
   materializing the two `reference/nirspec/<obs>/` TOMLs the pipeline already reads — no pipeline
   *consumption*-side change.

### Non-goals
- **Data-management config sync (#303).** Getting web decisions back into `observations.toml` /
  `fields.toml` is a separate workstream. This design routes web state through DB channels + `pull`
  materialization, **not** through `observations.toml`. Where the current pipeline reads a decision
  from TOML, we say explicitly how the web bypasses it.
- **Dashboard unification.** New `/admin/nirspec` pages mirror `/admin/nircam` but we do not merge or
  restructure the admin shell.
- **Any NIRCam change.** NIRCam is the template we copy from, not something we touch.
- **Changing the pipeline's mask insertion point or bkgsub timing.** The rate-mask apply point and
  the stuck-shutter/bkg consumption in stage 2 already exist and are correct; we only supply inputs.

---

## 2. Where NIRSpec sits today (the starting line)

Grounded in the investigation. Three facts shape everything below.

**(a) The rate-level detector mask already exists in the pipeline.** The design's "mask the detector,
recover the rest, source-independent, `DO_NOT_USE`" is **already fully implemented** as
`pipeline/campfire_pipeline/nirspec/masks.py` — the "manual mask" system. The web feature is a new
*front-end* + *storage channel* for an existing mechanism, **not** a new pipeline capability. Key
mechanics we must not break:

- `apply_mask_dq(rate_file, reg_string)` (`masks.py:186`) rasterizes a DS9 region string in **image
  coords** (rate files have no useful sky WCS, `masks.py:66`) and OR's `DO_NOT_USE` into the rate
  `.dq`, recording the flipped pixels in a `CFDQMASK` uint8 extension so the OR is **cleanly
  reversible** (`clear_manual_mask_dq`, `masks.py:220`).
- The mask is consumed by `subtract_background_from_rate_file` (`stage1.py:476`), which drops masked
  pixels from the background fit (`slitmask[model.dq > 0] = False`, `stage1.py:533`). There is no
  separate jwst "mask step" — it's a direct DQ OR every downstream step honors.
- Staleness is tracked by `CFMASKSH` = `sha256[:12]` of the canonicalized region string
  (`hash_mask`, `masks.py:54`); `is_stale` (`masks.py:155`) drives automatic re-apply
  (`bkgsub_with_masks`, `masks.py:387`; `ensure_fresh`, `masks.py:428`).
- **The rate tier is deliberately outside the CFP provenance chain** (`cfp.py:108-109`): it keeps its
  own `CFBKGSUB` / `CFMASKSH` sentinels. The reserved canonical-tier `CFP_MASK` slot (`cfp.py:112`,
  "round-trip is open") is a *different* concept and is **not** what the web rate mask uses.

The **only** thing that is TOML-bound today: the authoritative region source is
`observations.toml [<obs>.masks]` (read by `Observation.load`, `observation.py:170`), from which
`.reg` files are derived one-way (`materialize_reg_files`, `masks.py:118`). Since config sync (#303)
is out of scope, the web loop must reach `masks.apply` **without** going through `observations.toml`
— see §3 and OQ-1.

**(b) Rate files are not deployed and not a layout product.** No `_rate.fits` entry exists in the
layout suffix tables (`bijection.py:32-42`) or product registry (`products.py`); a bare `.fits` that
isn't a canonical `_nrs[12]_<source>.fits` falls through `_nirspec_obs_product`
(`bijection.py:58-64`) — there is no rate key, and deploy has no rate handling. The mask-region
storage key `nirspec_manual_mask` (USER_STATE, `products/nirspec/<obs>/manual_masks/*.reg`,
`products.py:213-218`) **does** already exist as a designated cloud home for `.reg` mirrors.

**(c) `spectrum_exposures` is a dead scaffold, and the wrong grain for both features.** The table
exists (`tables.sql:785-800`) — per `(spectrum_id, exposure_ref)`, FK to `spectra.id` **ON DELETE
CASCADE**, with `root/nod/detector/source_id/grating/review_status/masking/notes` — but **nothing
reads or writes it** (only `b2_lifecycle.sql`'s RLS test inserts a row). Deploy uploads the canonical
per-source FITS and registers them in `storage_objects` with `product_type='nirspec_spectrum_exposure'`
but **never inserts a `spectrum_exposures` row** (`deploy.py:655-663`). It is wrong for feature 1
(source-keyed, no `mask_regions`/dims/render-key columns, CASCADE-dies with a spectrum that may not
exist until stage-3 combine — i.e. *after* the rate-mask review that gates combine) and, by design
decision, wrong for feature 2 (flags go to the separate stuck-shutter channel). Leave it alone.

**(d) Stuck-shutter / bkg-override state lives in two local TOMLs** under `reference/nirspec/<obs>/`,
both consumed by stage 2, only one of which reaches the cloud today:

| File | Grain | Structure | Consumed at | Reaches cloud? |
|---|---|---|---|---|
| `stuck_closed_shutters.toml` | `(root, source_id)` → 1-indexed shutter ordinals | `observation.py:307-344` | `stage2.py:708-736` (drops shutter columns; `STKSHTRS` card; `_nodata` if all stuck) | **Yes** — as a per-deployment TOML *snapshot* in `deployments.stuck_shutters` jsonb (`deploy.py:823-834`, `tables.sql:645`) |
| `nodded_background_overrides.toml` | `(root, source_id, nod)` → `[bkg nods]` (empty = exclude nod) | `observation.py:346-381` | `stage2.py:1144-1155` (`CFP_BKG='excluded:override'` when empty) | **No** — no deploy path reads or uploads it |

Both `nirspec_stuck_shutters` and `nirspec_bkg_override` are **reserved** as USER_STATE layout keys
(`products.py:219-230`) and in the `storage_objects` CHECK (`tables.sql:882-883`) but **no deploy
command uploads them**. Stuck shutters have a second complication: the TOML already has **two local
writers** — pipeline auto-detection (`detect_stuck_shutters` + `merge_stuck_shutters`, which gives
manual/existing entries priority, `stuck_shutters.py:393-429`) and hand edits. Bkg overrides are
hand-authored only (no auto-writer).

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
      suffix="_rate.fits", legacy_prefix=None))   # reserved → CANONICAL key both schemes
  ```
- **Bijection — the bug-prone edit.** `_nirspec_obs_product` currently routes *any* non-`_spec.fits`
  `.fits` to `nirspec_spectrum_exposure` (`bijection.py:62-63`), so a `_rate.fits` would be
  **silently mis-parsed** as a spectrum-exposure. Add `("_rate.fits", "nirspec_rate")` to
  `_NIRSPEC_OBS_SUFFIXES` (`bijection.py:32-42`) **before** the `.fits` fallback, and mirror it in
  `web/lib/layout.ts` (`NIRSPEC_OBS_SUFFIXES` `:183`, `nirspecObsProduct` `:207`, spec at `:77`).
  The golden round-trip test (`layout.test.ts`) needs a `nirspec_rate` row.
- **exposure_ref.** Add `nirspec_rate` to `_exposure_ref_for` (`registry.py:120-133`) so each rate
  object gets a stable `exposure_ref` (rootname stem `jw..._nrs1_rate`, which already encodes
  detector + exposure) backing the partial-unique `(product_type, exposure_ref) WHERE status='active'`.
  Add to `DEFAULT_COPY_PRODUCT_TYPES` (`registry.py:844-850`) as belt-and-suspenders.
- **CHECK enum.** Add `'nirspec_rate'` to the `storage_objects.product_type` CHECK
  (`tables.sql:876-885`) — a hand-authored additive migration (see §5).
- **Deploy path.** Add `discover_rate_files(obs_dir)` (mirrors `discover_spectrum_exposures`) and an
  upload block in **both** NIRSpec entry points — full `deploy_observation` (`deploy.py:652-664`) and
  `_deploy_intermediates_only` (`deploy.py:300-310`) — appending
  `UploadTask(rate_path, storage_key('nirspec_rate', scope, name, CANONICAL), 'application/fits')`.
  The existing `upload_files_parallel(..., backend='osn')` + `build_registry_rows` +
  `upsert_storage_objects` handle the rest — **no new upload/presign plumbing**.

**Scope note:** rate files are source-independent — deploy the whole detector **once per
obs×exposure×detector regardless of `--source-ids`**, unlike the spectrum-exposure block which filters
by source (`deploy.py:657-658`). Deploy on every deploy (cloud-as-source-of-truth + delete-local
restore).

### 3.2 New schema — a detector-grain rate-mask table

`spectrum_exposures` is the wrong grain (§2c). Model a **new** table directly on `nircam_exposures`
(`tables.sql:734-756`), keyed on `(observation, exposure_root, detector)`:

```sql
CREATE TABLE nirspec_rate_exposures (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- identity (deploy-owned)
    observation    text NOT NULL,             -- observations.name (NOT an FK to spectra)
    exposure_root  text NOT NULL,             -- rate rootname stem, e.g. jw07076020001_04101_00001
    detector       text NOT NULL,             -- 'nrs1' | 'nrs2'
    filename       text NOT NULL,             -- <root>_<detector>_rate.fits
    grating        text,
    image_width    int,
    image_height   int,
    storage_key    text,                      -- canonical nirspec_rate key for the fits route
    stage          text NOT NULL DEFAULT 'rate',
    -- review state (web-owned)
    review_status  text NOT NULL DEFAULT 'pending',   -- pending | approved | excluded
    masking        text NOT NULL DEFAULT 'none',      -- none | needed | done
    mask_regions   jsonb,                     -- {version, polygons:[{id, source, vertices:[[x,y]...]}]}
    notes          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT nirspec_rate_exposures_unique UNIQUE (observation, exposure_root, detector)
);
```

- `mask_regions` jsonb shape reuses NIRCam's verbatim (`MaskEditor.tsx:1-20`, `nircam_masks.py:164-168`):
  vertices in **DS9 `image` 1-indexed** coords. Because the pipeline consumes **image coords** already
  (`mask_regions.py:19`), this is a clean match — no WCS projection like NIRCam's `import-masks` needs.
- **No FK to `spectra`.** Rate masks gate stage-3 combine, which *produces* spectra; the review must
  exist before any spectrum row (OQ-5). Key/scope to `observations.name`.
- RLS **admin-only**, mirroring `nircam_exposures` (`policies.sql:654-678`) — three
  `admin_select/insert/update` policies gated on `is_admin`; deploy writes via service_role.
- Indexes: btree `(observation)` + a partial `review_status WHERE review_status <> 'approved'`
  review-queue index (mirror `indexes.sql:343-353`).
- The table carries **only masking state**, never shutter flags — those are Feature 2's separate
  channel.

**Split-ownership upsert** (the load-bearing pattern from `nircam.py:704-742`): the deploy re-register
UPDATE writes **only** identity/render columns (`filename/detector/dims/storage_key/stage`) and
**omits** `review_status`/`mask_regions`/`notes`, so re-deploy never clobbers web triage; new rows
seed `review_status='pending'`, `masking='none'`.

### 3.3 Pipeline apply point (unchanged)

The insertion point is fixed and correct: `DO_NOT_USE` OR into the rate `.dq` **before**
`subtract_background_from_rate_file`, reversible via `CFDQMASK` + `CFBKG` restore, staleness by
`CFMASKSH`. The web supplies region strings; it does not change pipeline timing. A web-edited region
with a changed hash triggers `bkgsub_with_masks` / `ensure_fresh` re-apply automatically. **No CFP
change**, no new provenance keyword — the rate tier stays outside the CFP chain.

### 3.4 Web editor (reuse)

Almost entirely NIRCam reuse — the mask editor is "the NIRCam `MaskEditor` pointed at a `nirspec_rate`
key":

- **FITS proxy** `web/app/api/nircam-fits/route.ts` — admin-gated Range-forwarding proxy, resolves the
  object's backend from `storage_objects` (`:73-78`), with a `CAMPFIRE_LOCAL_DATA_ROOT` filesystem
  fast-path so a reducer on the same machine renders **before** deploy (`:52-67`). The **only**
  NIRCam-specific line is the `parseKey(key).productType !== 'nircam_exposure'` guard (`:46`):
  generalize it to accept `{'nircam_exposure','nirspec_rate'}` or clone as `/api/nirspec-fits`.
- **SCI decoder** `web/lib/fits/fetch.ts` `fetchSciImage` (`:95-132`) — two Range GETs (header window
  → SCI float32 block). Rate SCI is the full 2048² detector, same size class as NIRCam, so the range
  budget holds. **Caveat:** it throws on `BITPIX != -32` (`:117`) and on fpack `ZIMAGE` compression
  (`:120`) — see OQ-2.
- **Canvas + editor** `FitsCanvas.tsx` + `MaskEditor.tsx` — instrument-agnostic (fits key + dims +
  regions + `onSave`); reused verbatim except parametrizing the hardcoded route URL. Canonical
  storage frame is DS9 `image` 1-indexed with a Y-flip assuming `origin='lower'`
  (`MaskEditor.tsx:74-79`) — confirm the rate SCI orientation matches (OQ-3).
- **Persist** — mirror `saveExposureMaskRegions` (`nircam-exposures.ts:321-352`): writes `mask_regions`
  jsonb and flips `masking` to `'done'` iff ≥1 polygon, else `'none'`. No PNG presign needed — the
  live-FITS path renders directly.

### 3.5 The pull → consume loop

Mirror `nircam_masks.pull_masks` (`nircam_masks.py:241-305`) verbatim: `SELECT mask_regions` per
`(observation, exposure_root, detector)` → serialize to DS9 `image`-coord `.reg` files, atomic
tmp-write + replace, `# Generated by campfire deploy` header, full overwrite = reversible, only
non-null rows written. **The open decision is where these land and how `masks.py` reads them**
(OQ-1): since `observations.toml` sync is out of scope, the natural target is the already-registered
`nirspec_manual_mask` location (`products/nirspec/<obs>/manual_masks/*.reg`), making the `.reg` mirror
(not TOML `[<obs>.masks]`) the authoritative pipeline input. That requires refactoring
`Observation.load` / `masks.apply_to_observation` to accept a non-TOML region source — the one real
change to `masks.py`'s source-of-truth. A new CLI verb `campfire deploy nirspec pull-rate-masks --obs`.

---

## 4. Feature 2 — live nods renderer

### 4.1 Data source — reuse the already-deployed spectrum-exposures

**No new deploy target is needed for the render.** The `*_nods.pdf` (`plots.py:282-478`) draws the
`S2D_SCI` / `S2D_BKGSUB_SCI` 2D arrays, which are **named HDUs on the canonical
`nirspec_spectrum_exposure` FITS** (`canonical.py:20`) — **already uploaded to OSN and registered in
`storage_objects`** (`deploy.py:300-310`, `655-663`). Everything else the PDF shows is *derived*:

- The **1D cross-dispersion profile** is `np.nanmedian(S2D, axis=1)` computed at plot time
  (`plots.py:401,411,460`) — not a stored product. Compute it in JS from the fetched array.
- The **normalization** is shared ZScale/MAD across all panels (`plots.py:345-365`) — client-side.
- **Overlays** need the `SHUTSTA` / `STKSHTRS` / `SRCFLUX` header cards (`plots.py:341-342,397-398`),
  which are range-fetchable from the FITS header.

The FITS render stack transfers directly (same as Feature 1): generalize `findSci` to target
`S2D_SCI` / `S2D_BKGSUB_SCI` instead of hardcoded `SCI` (`fetch.ts:62-75`) and widen the proxy guard
to allow `nirspec_spectrum_exposure`. Range-fetch is **cheaper** here — S2D cutouts are tiny rectified
slitlets (tens×hundreds px), not the 16 MB full detector; the renderer could fetch whole files.

**Caveat (OQ-6):** `S2D_*` HDUs are "visualization-only," written only if a plot/rectify/stuck-shutter
step ran (`canonical.py:20-21`). A bare stage-2 canonical may lack them — the renderer needs a
presence check / fallback, or the reduction must always rectify before deploying intermediates.

### 4.2 The source × nod × detector grouping model

The identity tuple is `(root, nod, detector, source_id)`, already materialized in the canonical
filename (`{root}_{nod}_nrs[12]_{source}.fits` → `exposure_ref`, `registry.py:120-133`). The web
groups exactly as the PDF does: filter to one `(observation, source_id)`, then **rows = distinct
`nod`** (ordered by exp_group then nod), **columns = detector ∈ {nrs1, nrs2}** with a derived 1D
profile column per detector.

**Grouping-source gap (OQ-4).** `spectrum_exposures` has the ideal columns
(`root/nod/detector/source_id/grating/review_status`) but is **unpopulated** (§2c). Two options:
(a) reconstruct grouping by parsing `storage_objects.exposure_ref` (works today, no reviewer-lifecycle
columns), or (b) start upserting `spectrum_exposures` in the NIRSpec deploy path (mirror
`nircam.py:_upsert_exposures`) to get the lifecycle columns. **Recommend (b)** if the nods renderer
needs any per-source review state; otherwise (a) is sufficient for a pure render. Note: even under (b),
the nods **flags** do not land in `spectrum_exposures` — they go to the stuck-shutter channel (§4.3).
The `d{eg}:{nod}` row labels fold in subpixel-dither exp_groups (`plots.py:311-325`) — whether
exp_group is recoverable from `root`/`nod` alone for exact label parity is open (OQ-7).

### 4.3 The flag → metadata → pull-back loop

Two source-scoped annotations, both routed to the **separate** stuck-shutter/bkg channel (per the
fixed decision — **not** the rate-mask table, **not** `observations.toml`):

- **Stuck-closed shutter** — entity `(root, source_id, shutter_ordinal)`, source-scoped and
  detector-independent. Terminates by materializing `reference/nirspec/<obs>/stuck_closed_shutters.toml`
  (consumed at `stage2.py:708-736`).
- **Background override** — entity `(root, source_id, nod)` → `[bkg nods]` (empty = exclude nod).
  Terminates by materializing `reference/nirspec/<obs>/nodded_background_overrides.toml` (consumed at
  `stage2.py:1144-1155`).

**The consumption side needs no change** — stage 2 already reads both TOMLs. What is missing and must
be built:

1. **An editable per-source DB channel.** Today `deployments.stuck_shutters` jsonb is a per-deployment
   TOML *snapshot* (`deploy.py:823-834`), **not** a live editable surface, and bkg overrides have no
   cloud representation at all. Mirror `nircam_exposures.mask_regions` (a jsonb editable column) but
   keyed `(observation, root, source_id)` for stuck shutters and `(observation, root, source_id, nod)`
   for bkg overrides. Whether this is a new table/column vs repurposing the reserved
   `nirspec_stuck_shutters`/`nirspec_bkg_override` product slots is OQ-8/OQ-10.
2. **New pull verbs** `campfire deploy nirspec pull-stuck-shutters` / `pull-bkg-overrides`, modeled on
   `pull_masks` / `pull_exclusions` (`nircam_exclusions.py:34-79`): full-overwrite, atomic, reversible,
   generated-header. Bkg overrides have no auto-writer, so a clean full-overwrite is safe.
3. **Reconcile the multi-writer authority conflict on `stuck_closed_shutters.toml`** — the single
   hardest decision here (OQ-9). Pipeline auto-detection (`merge_stuck_shutters`, manual-priority) +
   hand edits + web pull all contend for that file; a naive full-overwrite pull would clobber
   auto-detected and hand entries. `merge_stuck_shutters` (`stuck_shutters.py:393-429`) is the existing
   precedence seam to reuse or supersede.

Because the nods renderer's natural unit **is** the per-source view, the flag UI sits directly on the
rendered source (a stuck-shutter toggle per shutter boundary; a bkg-override control per nod row).

---

## 5. Schema changes (concrete sketch)

All additive and hand-authored where the diff engine can't help. Per CLAUDE.md: edit
`supabase/schemas/*.sql`, `supabase db reset`, `supabase db diff -f <desc>`, review, commit both;
the Supabase preview branch validates on PR. **One schema-changing PR to `main` at a time**;
**regenerate `seed.sql`** on any new-column/table add or preview branches go red.

| Change | File | Diff-tracked? | Caveat |
|---|---|---|---|
| `nirspec_rate` added to `storage_objects.product_type` CHECK | `schemas/tables.sql:876-885` | ✅ CHECK-constraint change, tracked | Not a migra blind spot |
| New `nirspec_rate_exposures` table (§3.2) | `schemas/tables.sql` | ✅ | Reserved-word check: `stage`, `notes`, `grating` are fine; avoid bare `references` etc. |
| Unique `(observation, exposure_root, detector)` + review-queue partial index | `schemas/tables.sql`, `schemas/indexes.sql` | ✅ | — |
| Admin-only RLS (3 policies) on `nirspec_rate_exposures` | `schemas/policies.sql` | ✅ | Mirror `nircam_exposures` policies |
| Editable stuck-shutter / bkg-override channel (table or column, OQ-8) | `schemas/tables.sql` | ✅ | If a jsonb column on a new/existing table |
| (If OQ-4 (b)) populate `spectrum_exposures` at deploy | code only, no schema change | n/a | Table already exists |

Caveats to call out per CLAUDE.md's migra limitations: **materialized views, comments, partitions are
not diff-tracked** — none of the above touches those, so the standard `db diff` flow applies. The
`nirspec_rate` layout suffix and `web/lib/layout.ts` mirror are **not** schema and ride with §3.1.

---

## 6. Web surface

Mirror `/admin/nircam`; add `/admin/nirspec` review pages. Reused vs new:

| Piece | NIRCam source | For NIRSpec |
|---|---|---|
| Rate-mask **list** page | `app/admin/nircam/page.tsx` | New `app/admin/nirspec/rate/page.tsx` — TanStack `AdminTable`/`AdminFilterBar`, URL-state filters, review/masking badges |
| Rate-mask **detail** cockpit | `app/admin/nircam/[id]/page.tsx` | New — in-memory cache, ±window prefetch, keyboard shortcuts (1/2/3 review, ←/→ nav w/ auto-save, S save), the `MaskEditor` pane |
| **Nods renderer** page | (no analogue) | New `app/admin/nirspec/nods/[source]/page.tsx` — the rows=nod × cols=detector grid, per-detector 1D profile, shutter/nod flag controls |
| `MaskEditor` / `FitsCanvas` | `components/nircam/` | Reused verbatim (parametrize route URL) |
| FITS proxy | `app/api/nircam-fits/route.ts` | Generalize guard or clone `/api/nirspec-fits` |
| Server actions | `lib/actions/nircam-exposures.ts` (6 exports) | New `lib/actions/nirspec-rate.ts` mirroring `getExposures`/`getNeighbors`/`getById`/`presign?`/`updateReview`/`saveMaskRegions`; all `requireAdmin()`-gated |
| Nods flag actions | (no analogue) | New `saveStuckShutter` / `saveBkgOverride` writing the §4.3 channel |

All new pages are admin-only and **merge inert** — no public surface is touched.

---

## 7. The local ↔ cloud loop, end to end

**Feature 1 (rate masks):**
```
cfpipe nirspec run --obs X (produces _rate.fits, local)
  → campfire deploy --obs X            # NEW: uploads nrs1/nrs2 _rate.fits to OSN, registers,
                                        #      upserts nirspec_rate_exposures (pending)
  → /admin/nirspec/rate  (admin)       # fitsgl renders rate SCI; draw DO_NOT_USE polygons
  → saveMaskRegions → mask_regions jsonb, masking='done'
  → campfire deploy nirspec pull-rate-masks --obs X   # NEW: DB → .reg at manual_masks/ (OQ-1)
  → cfpipe nirspec run --obs X         # masks.py OR's DO_NOT_USE into rate .dq BEFORE bkgsub;
                                        #   CFMASKSH staleness triggers auto re-apply
  → campfire deploy --obs X            # re-upload rate (content-hash change) + downstream products
```

**Feature 2 (nods flags):**
```
cfpipe nirspec run --obs X (canonical _nrs[12]_<source>.fits w/ S2D_* HDUs, deployed already)
  → /admin/nirspec/nods/<source>  (admin)   # renders S2D cutouts rows=nod × cols=detector,
                                             #   client-derived 1D profile, SHUTSTA overlay
  → mark stuck shutter / set bkg override → editable DB channel (§4.3)
  → campfire deploy nirspec pull-stuck-shutters / pull-bkg-overrides --obs X   # NEW
       → reference/nirspec/<obs>/stuck_closed_shutters.toml       (authority merge, OQ-9)
       → reference/nirspec/<obs>/nodded_background_overrides.toml (clean overwrite)
  → cfpipe nirspec run --obs X         # stage2 already consumes both TOMLs (no change)
```

Both loops are non-destructive and reversible: DB is authoritative, `pull` materializes, re-run
re-applies. Delete-local/restore works for rate files once §3.1 registers them.

---

## 8. Open questions / decisions to resolve

**Feature 1 — rate masks**
- **OQ-1 (delivery channel — the biggest one).** Since `observations.toml` sync is out of scope,
  does `pull-rate-masks` write `.reg` to the existing `nirspec_manual_mask` location and does
  `masks.py` read from there instead of `obs.manual_masks`/TOML? This requires refactoring
  `Observation.load` / `apply_to_observation` to accept a non-TOML region source.
- **OQ-2.** Are JWST `nrs1`/`nrs2` `_rate.fits` deployed fpack-compressed (`ZIMAGE`)? `fetch.ts:120`
  rejects compressed SCI → deploy uncompressed, or extend the decoder.
- **OQ-3.** Does rate SCI orientation match `MaskEditor`'s `origin='lower'` DS9-image Y-flip, or does
  the NIRSpec detector need a different flip than the NIRCam path assumes?
- **OQ-5.** Confirmed: the rate-mask table must **not** FK to `spectra` (review precedes combine which
  makes spectra). Key to `observations.name`. (Investigator confident.)
- **OQ (confirm).** Rate masks are strictly rate-level; they never touch the canonical per-source
  MultiSlit files. Do not conflate rate `CFDQMASK` with the reserved canonical `CFP_MASK`
  (`cfp.py:112`) — believed the rate `CFMASKSH` sentinel is the whole story.

**Feature 2 — nods renderer**
- **OQ-4.** Populate `spectrum_exposures` at deploy (mirror `_upsert_exposures`) for lifecycle
  columns, or reconstruct grouping from `storage_objects.exposure_ref`?
- **OQ-6.** `S2D_*` are visualization-only — always rectify before deploying intermediates, or have
  the web fall back / trigger rectify when `S2D_SCI` is absent?
- **OQ-7.** Is exp_group recoverable from `root`/`nod` alone to reproduce the exact `d{eg}:{nod}` row
  grouping?
- **OQ-8.** New per-source DB table/column for the editable flags, or repurpose the reserved
  `nirspec_stuck_shutters` / `nirspec_bkg_override` slots? (`deployments.stuck_shutters` is a snapshot,
  not editable.)
- **OQ-9 (the hardest).** Authority order on `stuck_closed_shutters.toml` among {web pull, pipeline
  auto-detect (`merge_stuck_shutters`, manual-priority), hand edit} — full-overwrite or merge, and
  does the pull preserve `# auto-detected` provenance tags?
- **OQ-10.** Do the reserved cloud reference keys actually deploy the TOMLs to OSN, or does the flag
  channel stay DB-only with `pull` materializing locally (parallels the NIRCam `.reg` pull, which is
  not registered in `storage_objects`)?
- **OQ-11.** Bkg-override nod identity: the TOML uses raw exposure-sequence integers (TACONFIRM-shifted),
  not sequential indices — the renderer must surface the same integer or the pull writes mismatched
  keys. And the "drop this nod" case (empty list → `CFP_BKG='excluded:override'`) needs a clear UI
  affordance distinct from "use nods [X]".

---

## 9. Proposed phased breakdown (each mergeable-inert)

```
P0 layout contract ─────────────┬───────────────────────────────────────────────┐
   (nirspec_rate spec + suffix   │                                               │
    bijection, py+TS, tests)     │                                               │
                                 │                                               │
Track R (rate masks):            │  Track N (nods renderer):                     │
  P1 rate deploy → OSN ──────────┤    P4 generalize fits proxy + findSci for     │
     + storage_objects enum      │       S2D_SCI (nirspec_spectrum_exposure)     │
  P2 nirspec_rate_exposures      │    P5 nods renderer page (render only) ────────┤
     table + RLS + deploy upsert  │    P6 editable stuck/bkg DB channel +         │
  P3 web rate-mask editor        │       flag UI                                 │
     (reuse MaskEditor)          │    P7 pull-stuck-shutters / pull-bkg-overrides │
  P3b pull-rate-masks +          │       + authority merge (OQ-9)                │
      masks.py source refactor ──┘                                               │
                                                                                 │
(config sync #303 — OUT OF SCOPE, separate epic) ────────────────────────────────┘
```

- **P0 — Layout contract first** (mirrors #210's "contract first"). Add `nirspec_rate` ProductSpec +
  the `_rate.fits` bijection suffix in both python and TS, with the round-trip conformance test.
  Inert: nothing deploys yet. **The suffix edit is the single most bug-prone step** — a missing suffix
  silently mis-parses rate keys as spectrum-exposures.
- **P1 — Rate deploy → OSN** (deps P0). `discover_rate_files` + upload block in both entry points,
  `backend='osn'`; `nirspec_rate` in the CHECK enum, `_exposure_ref_for`, `DEFAULT_COPY_PRODUCT_TYPES`.
  Inert: uploads an intermediate no public surface reads.
- **P2 — `nirspec_rate_exposures` table** (deps P1). Table + unique + partial index + admin RLS +
  split-ownership deploy upsert. Regenerate `seed.sql`. Inert: admin-only table.
- **P3 — Web rate-mask editor** (deps P2, P4-proxy). New `/admin/nirspec/rate` pages + server actions,
  reusing `MaskEditor`/`FitsCanvas`. Resolve OQ-2/OQ-3 here. Inert: admin-only.
- **P3b — pull + pipeline source refactor** (deps P3). `pull-rate-masks` (mirror `pull_masks`) +
  `masks.py` non-TOML region source (OQ-1). Rides a **pipeline tag** (Infrastructure/PATCH — no change
  to scientific output, only the region source seam). Add the `## Unreleased` CHANGELOG entry.
- **P4 — Generalize fits proxy** (deps none beyond existing NIRCam stack). Widen the productType guard
  to `{'nircam_exposure','nirspec_rate','nirspec_spectrum_exposure'}` (or clone `/api/nirspec-fits`)
  and generalize `findSci` to `S2D_SCI`/`S2D_BKGSUB_SCI`. Serves both tracks.
- **P5 — Nods renderer (render only)** (deps P4). The rows=nod × cols=detector grid + client-side 1D
  profile + `SHUTSTA` overlay, grouping via OQ-4's chosen source. Read-only, inert.
- **P6 — Editable flag channel + UI** (deps P5). The §4.3 DB channel (OQ-8) + stuck/bkg flag controls
  on the nods view. Inert: admin-only writes.
- **P7 — Flag pull-back** (deps P6). `pull-stuck-shutters` / `pull-bkg-overrides` (mirror
  `pull_masks`/`pull_exclusions`), resolving the authority merge (OQ-9). No pipeline *consumption*
  change — stage 2 already reads both TOMLs — so no calibration impact; a PATCH CHANGELOG entry covers
  the deploy-side addition if any `pipeline/**` file is touched.

**Sequencing & merge safety.** P0 first; then Track R (P1→P2→P3→P3b) and Track N (P4→P5→P6→P7) run
largely parallel, joined at the shared fits proxy (P4). Every step merges inert (admin-only or a
non-public intermediate). One schema-changing PR at a time off the squashed baseline; regenerate
`seed.sql` on P2 and P6. Pipeline-touching PRs (P3b, and P7 if it touches `pipeline/**`) carry a
categorized `## Unreleased` entry — both are **Infrastructure/PATCH** (no change to pixel/flux values
for the same input).

---

## 10. Definition of done

- `campfire deploy` uploads `nrs1`/`nrs2` **rate files** to OSN under a canonical `nirspec_rate` key,
  registered in `storage_objects`, with a `nirspec_rate_exposures` triage row — the NIRSpec analogue
  of NIRCam exposure parity.
- The admin `/admin/nirspec/rate` page renders the rate SCI **in-browser** (fitsgl), reviewers draw
  source-independent `DO_NOT_USE` polygons, and `pull-rate-masks` materializes them where `masks.py`
  consumes them **before** stage 2 — a closed `run → deploy → mask → pull → run` loop honoring the
  existing reversible DQ/`CFMASKSH` contract.
- A live **nods renderer** reproduces `*_nods.pdf` in the browser from the already-deployed `S2D_*`
  cutouts, grouped per source across `nod × detector`, and stuck-shutter / bkg-override flags
  round-trip through their **separate** channel to the two `reference/nirspec/<obs>/` TOMLs stage 2
  already reads — no pipeline consumption change.
- Both features merge inert; `spectrum_exposures` is left untouched; config sync (#303) is not
  entangled.
