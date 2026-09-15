# Design: NIRCam exposure catalog — publishing the frames behind the mosaics

**Status:** draft for review
**Date:** 2026-09-15
**Driver:** [#562](https://github.com/hollisakins/campfire/issues/562) (Ivo Labbé) — record the
corrected WCS of every frame that went into a mosaic, so that for any mosaic pixel a user can
determine which frames covered it and drizzle a theoretical PSF (or a simulation) through the
identical geometry.
**Related:** [#309](https://github.com/hollisakins/campfire/issues/309) (exposure footprints +
spatial overlap queries, deferred on `pg_sphere`/self-hosting),
[design-intermediate-products.md](design-intermediate-products.md),
[design-nircam-deploy-overhaul.md](design-nircam-deploy-overhaul.md).

---

## 1. The gap, in one line

**The exposure bytes are public; the index to them is admin-only.**

- Canonical exposure FITS are uploaded to OSN (`build_fits_upload_tasks`,
  `python/campfire/deploy/nircam.py:318`), registered in `storage_objects` as `nircam_exposure`,
  and a *published NIRCam field deployment* is visible to **everyone** — no program scoping
  (`get_storage_objects_for_sync` / `filter_accessible_storage_keys`, the `d.field IS NOT NULL`
  branch).
- `nircam_exposures` — the table that says which exposures exist — is gated by
  `admin_select_exposures` RLS (`supabase/schemas/policies.sql:905`).

So a user can download any exposure whose key they can guess, and has no way to learn the keys.
Issue #562 is one symptom. The others: no exposure search, no mosaic↔exposure cross-match, no
footprint, no WCS.

## 2. What already exists

Worth stating plainly, because most of this design is *exposure*, not construction.

| Capability | Where | Status |
|---|---|---|
| Corrected WCS (gwcs in ASDF + refreshed SIP FITS keys) | `nircam/align/apply.py:372` (`_write_solution` → `update_fits_wcsinfo`) | On every canonical exposure |
| Canonical exposure FITS on OSN | `deploy/nircam.py:318` | Uploaded + registered |
| Public read of those bytes | `get_storage_objects_for_sync`, `filter_accessible_storage_keys` | Works today |
| Pull path | `campfire pull --field X --intermediate` (`INTERMEDIATE_PRODUCT_TYPES`, `db/store.py:35`) | Works today |
| Exposure rows (field/filter/detector/visit/date_obs/ra_center/dec_center/stage/review) | `nircam_exposures`, written by `_upsert_exposures` (`deploy/nircam.py:790`) | Admin-only |
| Exposure browser UI (filters, sort, neighbors, related, filter options, progress) | `web/lib/actions/nircam-exposures.ts`, `/admin/nircam`, `/admin/intermediate-products` | Admin-only |
| Mosaic → input-frame list | `nircam/manifest.py:324` (`create_manifest`) | On disk; **parsed by deploy and discarded** (`deploy/nircam.py:930`) |
| Per-pixel input bitmask (CON) | `nircam/drizzle.py:288` | In the i2d, which deploy deliberately withholds (`deploy/nircam.py:889`) |
| Footprint parsing (S_REGION → polygon) | `nircam/geometry.py`, `nircam/expmap.py` | Local diagnostics only |
| Storage keys for mosaic siblings | `layout/campfire_layout/bijection.py:132`, `web/lib/layout.ts:272` | `mosaic_*` is prefix-dispatched — new siblings need **no** layout change |

Two details that shape the design:

- `_read_exposure_metadata` (`deploy/nircam.py`) **already opens the SCI header** to pull
  `CRVAL1`/`CRVAL2`. Extending it to the full SIP WCS + `S_REGION` is nearly free — same open,
  same header.
- `discover_mosaics` **already parses every manifest** at deploy time. The mosaic→exposure
  mapping is in hand and thrown away.

## 3. What is actually missing

1. **No mosaic↔exposure relation.** Nothing joins `nircam_images` to `nircam_exposures`.
2. **No geometry on exposure rows.** Only a pointing centre — not a footprint. A NIRCam detector
   is ~2.2′ across, so a position can sit well inside an exposure whose centre is arcminutes away.
3. **No WCS on exposure rows.** This is what turns #562 from a file into a query.

## 4. Decisions

**D1 — A SIP summary table, not lossless gwcs.** There is no format that is both lossless gwcs and
readable by `astropy` alone (gwcs serializes to ASDF, which needs `asdf` + `gwcs`). We publish the
SIP representation `update_fits_wcsinfo` already writes, and record the **achieved fit residual**
per exposure so users can judge sufficiency rather than trust a blanket claim. Anyone who needs
exact geometry downloads the exposure, which is already possible. *(Decided: Akins, on #562.)*

**D2 — CON is not deployed, and should become optional to build.** For deep fields the context
array is the dominant memory cost of the drizzle — our own config comment measures an a2744
1.26 Gpix tile with 534 inputs at **17 planes = 80 GiB**, against 14 GiB for SCI+ERR+WHT combined
(`config_default.toml:945-950`); in practice this has reached >400 GB. Publishing it is out of the
question. Separately, `compress_context` only controls how it is *written* — `drizzle.py:619`
allocates `outctx` unconditionally even though nothing in the repo reads CON and the underlying
`Drizzle` already accepts `disable_ctx=True` (we pass it for the variance pass, `drizzle.py:634`).
A `[nircam.resample].context = false` knob is a pure memory win. **Filed separately** — it is
unrelated to #562 and worth more. Note the default backend is `implementation = "jwst"`
(`config_default.toml:968`), so the stcal-side equivalent needs its own check.

**D3 — The relation lives in the database; the CSV is a rendered export.** Not a per-mosaic
sidecar product. A sidecar would need a layout entry, a deploy path, a staleness story, and would
answer only the per-tile question. A join table plus WCS-bearing exposure rows answers per-tile
*and* per-field, is always current, and serves exposure search at the same time. The DJA-style
`wcs.csv` becomes a formatting of `exposures ⋈ mosaic_inputs`, generated on demand.

**D4 — WCS as `jsonb`, not columns.** SIP for NIRCam runs to a high enough order that `A_i_j` /
`B_i_j` / `AP_i_j` / `BP_i_j` would add ~100 columns of mostly-null. One `wcs` jsonb keyed by FITS
card name round-trips into `astropy.wcs.WCS(fits.Header(row['wcs']))` directly. The handful of
fields we filter or sort on (`pa_v3`, footprint, exposure time) are promoted to real columns.

**D5 — `manifest.json` stays local.** It is pipeline rebuild bookkeeping (hashes, mtimes, config
hash) that happens to contain the input list, not a user-facing provenance artifact. Deploy reads
it to populate the relation; it is not itself published. This also gives the backfill path: the
manifests on the reduction cluster are the source for populating the relation for every mosaic
already deployed. *(Decided: Akins.)*

**D6 — Access model: published field ⇒ public exposure catalog.** Admin-only intermediates is a
*management* constraint, not a security one — while a reduction is in progress we do not want the
portal serving half-reduced exposures. Once a field's deployment is published, any user may see
the full exposures table for that field. This makes the catalog's visibility gate identical to the
one already governing the bytes, which is the property worth having: one predicate, not two.
*(Decided: Akins.)*

> This supersedes `design-intermediate-products.md` §1 ("No public access to intermediate
> products; intermediates are admin-only for now") for the NIRCam exposure catalog. That document
> and the implemented storage RLS already disagreed — the RLS does not gate on product type for
> field deployments. D6 resolves the disagreement in favour of what the RLS does.

## 5. Schema

### 5.1 `nircam_exposures` — additions

```
deployment_id   integer REFERENCES deployments(id)   -- provenance + lifecycle anchor
deploy_status   text NOT NULL DEFAULT 'draft'        -- draft | published | revoked
wcs             jsonb                                -- SIP card set (see D4)
wcs_residual    double precision                     -- achieved SIP fit residual, pixels
wcs_hash        text                                 -- mirrors manifest.compute_wcs_hash
s_region        text                                 -- footprint, verbatim from the header
ra_min/ra_max/dec_min/dec_max  double precision      -- bbox prefilter, extension-independent
pa_v3           double precision                     -- display/sort (confirm keyword, §6.1)
exposure_time   double precision                     -- XPOSURE/EFFEXPTM
```

`deployment_id` + `deploy_status` mirror `nircam_images` exactly (`tables.sql`), so
`set_deployment_status` flips exposures with the same batch that flips mosaics — one lifecycle,
not two. **`deploy_status` defaults to `draft` here** (unlike `nircam_images`, which defaults
`published` for backward compatibility): an exposure row is written on every deploy including
in-progress reductions, so the safe default is invisible. The backfill (§6.3) sets `published`
for fields whose deployment is already published.

The bbox columns are the extension-independent half of #309 and cost nothing here. The indexed
polygon-overlap query stays deferred on `pg_sphere`; a bbox prefilter plus an exact
point-in-polygon test in the API is adequate for the query volumes involved and does not commit
us to a spatial extension.

**Unique constraint required.** `nircam_exposures` currently has no uniqueness on
`(field, filter, filename)` — only btree indexes on `(field, filter)` and `review_status`
(`indexes.sql:368-375`), with `_upsert_exposures` hand-rolling a select-then-insert/update. The
join table needs a real key, so add:

```sql
ALTER TABLE nircam_exposures
  ADD CONSTRAINT nircam_exposures_field_filter_filename_key
  UNIQUE (field, filter, filename);
```

This may surface pre-existing duplicates. Check before writing the migration.

### 5.2 `nircam_mosaic_inputs` — new

```sql
CREATE TABLE nircam_mosaic_inputs (
  mosaic_id    integer NOT NULL REFERENCES nircam_images(id) ON DELETE CASCADE,
  exposure_id  integer NOT NULL REFERENCES nircam_exposures(id) ON DELETE CASCADE,
  PRIMARY KEY (mosaic_id, exposure_id)
);
```

Indexed both ways: `(mosaic_id)` for "what went into this mosaic", `(exposure_id)` for "which
mosaics is this frame in".

Deliberately **not** carrying a `ctx_id` column. The drizzle context bit is only meaningful
alongside a CON array we have decided not to publish (D2), and it is not simply the input's
ordinal — `drizzle_tile` skips inputs with no tile overlap (`drizzle.py:645`) and the bit is
assigned per successful `add_image`. Recording a number nobody can join against invites misuse.

`nircam_images` has one row per *extension* (`sci`, `err`, `wht`, `srcmask`) of the same logical
mosaic, so `mosaic_id` here must reference a single canonical row per `(field, tile, filter,
pixel_scale, epoch)` — pick `sci`, or introduce a mosaic-identity row. **Open question**, §9.

### 5.3 RLS

Replace `admin_select_exposures` with the predicate already used for the bytes:

```
admin  OR  (deploy_status = 'published'
            AND EXISTS (SELECT 1 FROM deployments d
                        WHERE d.id = deployment_id
                          AND d.field IS NOT NULL
                          AND d.status = 'published'))
```

INSERT/UPDATE policies stay admin-only — triage writes are unchanged. `nircam_mosaic_inputs`
inherits its visibility from the exposure side (a row is visible when its exposure is).

Keep this in lock-step with `filter_accessible_storage_keys` and
`get_storage_objects_for_sync`; the whole point of D6 is that the three agree.

## 6. Pipeline & deploy

### 6.1 Populate the WCS at discovery

Extend `_read_exposure_metadata` (`deploy/nircam.py`) to carry the SCI header's WCS card set,
`S_REGION`, `pa_v3` and exposure time out alongside the `CRVAL` pair it already reads. The card
whitelist should reuse `manifest._WCS_KEY_RE` (`nircam/manifest.py:66`) rather than being
re-derived — that regex is already the authority on "which cards define this file's sky mapping",
and a second, drifting list is exactly the bug we do not want.

Derive the bbox from the footprint polygon with the helpers in `nircam/geometry.py`
(`polygon_from_sregion`).

`pa_v3`: the exact keyword is unconfirmed — candidates are `ROLL_REF`, `PA_V3`, `PA_APER` (#309
flagged the same uncertainty). Confirm against a sample canonical header before writing the
reader; leave the column null rather than guessing.

`wcs_residual`: `update_fits_wcsinfo` fits SIP to a tolerance. Whether it exposes the achieved
residual or only the requested bound needs checking against the installed `jwst`; if only the
bound is available, record that and name the column accordingly. Do not publish a number whose
meaning we have not verified.

**Deploy must not import the pipeline.** `campfire.deploy` is a separate package; `_WCS_KEY_RE`
lives in `campfire_pipeline`. Either lift the regex into `campfire-layout` (the existing
zero-dependency shared authority) or restate it with a drift test, the same way
`campfire.storage.hashing` already mirrors the pipeline's hashing recipe.

### 6.2 Populate the relation at deploy

`discover_mosaics` already loads every manifest. Carry `m['inputs']` forward and upsert
`nircam_mosaic_inputs` after both `nircam_images` and `nircam_exposures` are written.

**Join-key gotcha:** `nircam_exposures.filename` strips the extension
(`basename = name.removesuffix('.fits')` in `discover_exposures`), while manifest inputs store
`os.path.basename(f)` *with* `.fits`. Normalize on one side and assert on the other — a silent
no-match here produces an empty relation that looks like "this mosaic has no inputs".

Resolve unmatched inputs loudly: an input in a manifest with no exposure row means the exposure
was never deployed (or was deployed under a different field/filter). Log the count and the first
few; do not drop silently.

### 6.3 Backfill

The manifests for every already-deployed mosaic are on the reduction cluster. A
`scripts/backfill_nircam_mosaic_inputs.py` walks `products/nircam/<field>/<filter>/mosaic_*_manifest.json`,
resolves each input to an exposure row, and upserts the relation — the same code path as §6.2,
run standalone. Same script (or a sibling) backfills the WCS columns by re-reading canonical
exposure headers, which requires the tree on disk or a pull.

Set `deploy_status = 'published'` for exposures whose field deployment is already published.

## 7. Mosaic header provenance (adjacent, ships independently)

Users downloading a deployed mosaic today get a WCS and essentially no provenance:

- `pixfrac` / `kernel` / `weight_type` **are** recorded (`drizzle.py:242-244`) into the i2d
  primary header — but the i2d is the one extension deploy withholds, and the split step copies
  only the SCI *extension* header (`resample.py:444`), so `_sci/_err/_wht` lose `PIXFRAC`,
  `KERNEL`, `WEIGHTYP`, `NDRIZ`, `CMPFRVER`, `CAL_VER`, `CRDS_CTX`. Deploy's own comment
  acknowledges this (`deploy/nircam.py:884-886`).
- **`good_bits` is not recorded anywhere**, despite determining which input pixels were eligible
  at all.

Fix: copy the informative primary-header cards onto the SCI header at split time, and stamp
`good_bits`. A handful of lines, and it is what makes a downloaded mosaic self-describing enough
to re-drizzle against. Worth doing regardless of the rest of this design.

Pipeline changelog category: **Infrastructure** (no pixel change).

## 8. Surface

### 8.1 API

Per decision D-C (#506), reads are `GET` route handlers, not server actions.

- `GET /api/v1/nircam/exposures` — filter by field / filter / detector / visit / stage, cone
  search on the bbox + exact footprint test. Cursor pagination per D-F (#511):
  `p_after_sort_text` / `p_after_sort_num` / `p_after_tiebreak`, `pagination.next_cursor`,
  fingerprinted cursor.
- `GET /api/v1/nircam/mosaics/{id}/inputs` — the contributing exposures. `format=csv` renders the
  DJA-style table (one row per frame, SIP columns flattened); `format=json` returns rows.
- `GET /api/v1/nircam/exposures/{id}/mosaics` — the reverse direction.

Response types exported from the route; `Cache-Control: private`; client fetches via `fetchJson()`
inside a `useQuery` keyed on *what*, never on the viewer.

### 8.2 Python client

`Campfire.query_exposures(...)` / `iter_exposures(...)` alongside `query_objects` /
`query_spectra` / `query_lines` (`python/campfire/client.py`), returning an astropy `Table` with
the pagination block in `Table.meta`. A `campfire nircam wcs --field X --tile Y` CLI writes the
CSV directly — that is the shape #562 actually asked for, and it composes with
`campfire pull --field X --intermediate` for anyone who then wants the frames.

Consider an `exposures` stream on `/api/v1/sync/*` so the local mirror carries the catalog. Not
required for v1; the query API covers the use case and the sync streams already run catalog-wide
counts on their first page.

### 8.3 Web

The exposure browser already exists (`web/lib/actions/nircam-exposures.ts`) — the work is moving
the read paths to route handlers, dropping the admin gate to the D6 predicate, and hiding the
triage-only columns (`review_status`, `correction`, `mask_regions`, `notes`) from non-admins. On
the NIRCam field page, a mosaic row gains a link to its contributing frames and a direct CSV
download; an exposure gains the reverse link.

## 9. What this gives you — and what it does not

State these in the user-facing docs, not just here.

- **Geometric coverage, not actual contribution.** The catalog says which frames *cover* a pixel.
  It cannot say which frames *contributed* to it — DQ flags, `good_bits='~DO_NOT_USE'`, outlier
  rejection and manual masks all drop pixels after the geometry is decided. CON is the only exact
  record and we are not publishing it (D2).
- **SIP is an approximation of the authoritative gwcs.** Sufficiency is the user's call, which is
  why `wcs_residual` is published (D1).
- **Weighting is not reconstructible from the catalog.** `weight_type='ivm'` means per-pixel
  inverse-variance maps that no table can carry. Exposure-time weighting approximates it; exact
  re-drizzling needs the exposure files, which are downloadable.

### Open questions

1. **Mosaic identity for the FK.** `nircam_images` is extension-major. Reference the `sci` row, or
   add a mosaic-identity table? The latter is cleaner and a bigger change.
2. **`pa_v3` keyword** — confirm from a sample header (§6.1).
3. **`wcs_residual` semantics** — achieved residual or requested bound (§6.1).
4. **Pre-existing duplicates** in `nircam_exposures` before adding the unique constraint (§5.1).
5. **Where `_WCS_KEY_RE` should live** so deploy can use it without importing the pipeline (§6.1).

## 10. Work breakdown

| # | Unit | Depends on | Notes |
|---|---|---|---|
| E1 | Mosaic header provenance: primary cards → SCI, stamp `good_bits` | — | §7, ships alone, pipeline changelog entry |
| E2 | `[nircam.resample].context = false` knob | — | §D2, separate issue, memory win |
| E3 | Schema: exposure columns + unique constraint + `nircam_mosaic_inputs` | — | §5.1, §5.2; declarative schema + generated migration |
| E4 | `_read_exposure_metadata` reads WCS/footprint/PA/exptime | E3 | §6.1 |
| E5 | Deploy populates `nircam_mosaic_inputs` | E3, E4 | §6.2 |
| E6 | RLS: D6 predicate on exposures + relation | E3 | §5.3; keep in lock-step with the storage functions |
| E7 | Backfill script (cluster-side, from manifests on disk) | E4, E5 | §6.3 |
| E8 | API routes (list, inputs, reverse) | E5, E6 | §8.1 |
| E9 | Python client + `campfire nircam wcs` CLI | E8 | §8.2 — closes #562 |
| E10 | Web: un-gate the browser, cross-links, CSV download | E8 | §8.3 |

E1 and E2 are independent and can go first.

## 11. Definition of done

- A logged-in user, with no admin flag, can list the exposures of a published field, filter them,
  and cone-search them.
- From a mosaic they can retrieve the contributing frames with full SIP WCS as CSV, and from a
  frame the mosaics it contributed to.
- `campfire nircam wcs --field X --tile Y` writes that CSV locally.
- Exposures of an unpublished (in-progress) field remain invisible to non-admins.
- A deployed `_sci.fits` carries its drizzle parameters.
- The relation is populated for every already-deployed mosaic via backfill.
- The three visibility predicates (exposure RLS, `filter_accessible_storage_keys`,
  `get_storage_objects_for_sync`) agree.

## 12. Out of scope

- Publishing CON, in any form (D2).
- The indexed spherical-polygon overlap query — still #309, still deferred on `pg_sphere` and the
  self-hosted migration. The bbox columns here are its extension-independent groundwork.
- A lossless gwcs/ASDF bundle per mosaic (D1). Reconsider if the SIP residual proves inadequate.
- NIRSpec exposure-level equivalents.
