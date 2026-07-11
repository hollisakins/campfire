# NIRCam Page Redesign — Implementation Plan

Status: Draft · Design reference: [`docs/wireframes/nircam-page-redesign.html`](wireframes/nircam-page-redesign.html)

## Goal

The map view (FitsGL, epic #337) becomes the definitive *visual* view of a field. `/nircam`
becomes the *detailed data access* surface and cleanly links into the map. Two routes:

- **`/nircam`** — landing: a grid of fields, each with a `_layout.png` coverage preview + key stats.
- **`/nircam/[field]`** — field detail: searchable field selector, overview (metadata + exposure-map
  browser), and the full data-products table (mosaics + exposure maps + thumbnails), with a
  per-mosaic "View in map" bridge.

No program metadata for now. Copy stays minimal (data access, not marketing).

## New/changed data products

| Product | Naming | Scope | Status today | Action |
|---|---|---|---|---|
| Exposure-map plot | `expmap_<field>_<filter>.png` | field·filter | pipeline emits **light** PDF, local-only | re-theme **dark**, emit PNG, deploy+register |
| Field layout plot | `<field>_layout.png` | field | does not exist | **new** pipeline plot, deploy+register |
| Mosaic thumbnail | `<mosaic>_thumb.png` | field·filter·tile (sci) | produced, local-only | deploy+register (sci only) |
| Exposure-map FITS | `expmap_<field>_<filter>.fits` | field·filter | deployed | none (already `nircam_expmap`) |
| Science mosaic | `mosaic_..._<ext>.fits` | field·filter·tile | deployed | none |

---

## Layer 1 — Pipeline (`pipeline/campfire_pipeline/nircam/`)

**1a. Dark exposure-map PNG.** `expmap.py` currently renders a **light-mode** PDF in `_render_pdf`
(~L469) with a shared vmin/vmax across filters (~L576). Add a dark PNG output:
- Bake on a dark well (`#0d0b12`-ish), light-gray axes/ticks/labels, keep viridis and the shared
  vmin/vmax (so filters stay comparable). One artifact, theme-agnostic — matches the CAMPFIRE house
  rule that data wells stay dark in both themes.
- Emit `expmap_<field>_<filter>.png` beside the `.fits` (naming via `_expmap_stem`, ~L396). Keep the
  PDF for local inspection (optionally re-theme it dark too, low priority).

**1b. Field layout plot (new).** New `<field>_layout.png` at the field root
(`field.products_dir`, `field.py:533`): the stack of all per-filter expmaps (union coverage, viridis)
with **tile outlines** drawn on top.
- Reuse the expmap auto-WCS (union footprint, 0.5″/pix) as the canvas; project each fiducial tile's
  sky corners into it via `field.get_tile_wcs()` (`field.py:926`) / `fiducial_tile_set()` (~L1002)
  and stroke the polygons. Dark theme, minimal chrome.
- New helper (e.g. `run_layout_plot(field)` in `expmap.py` or a small `layout_plot.py`), invoked after
  `run_expmap`.

**1c. Mosaic thumbnails.** Already produced by `resample.py:434` (`plot_mosaic_thumbnail`,
`steps/_plots.py:194`). No pipeline change — only deploy coverage (Layer 3).

**1d. Changelog.** These are plots — no change to pixel/flux science output → **PATCH /
Infrastructure** entry in `pipeline/CHANGELOG.md` `## Unreleased` (per AGENTS.md, required before the
pipeline PR opens).

---

## Layer 2 — Layout contract (`layout/campfire_layout/` + `web/lib/layout.ts`)

Register the three new keys in the declarative registry `products.py` (~L169–188) and mirror in
`web/lib/layout.ts` (~L228–239); update the golden fixture (pytest + vitest):

- `nircam_expmap_plot` — `data/products/nircam/<field>/<filter>/expmap_<field>_<filter>.png`
- `nircam_layout` — `data/products/nircam/<field>/<field>_layout.png`
- `nircam_mosaic_thumbnail` — `data/products/nircam/<field>/<filter>/<mosaic>_thumb.png`

**Bijection caveat.** Reverse dispatch (`bijection.py:98`) currently keys `nircam_mosaic`/`nircam_expmap`
by filename **prefix** (`mosaic`/`expmap`) — but `expmap_*.png` and `expmap_*.fits` share the `expmap`
prefix. Update dispatch to branch on **extension** (`.png` → plot; `.fits` → FITS) before the prefix
check. `_thumb.png`/`_layout.png` dispatch cleanly by suffix.

---

## Layer 3 — Deploy / registry (`python/campfire/deploy/nircam.py`)

`deploy_nircam()` (~L492) already records a field `deployments` row, uploads to **OSN**, and registers
`storage_objects` tagged with `deployment_id` (visibility rides `deployment.status`). Add:

- **Expmap PNGs** — extend `discover_expmap_tasks` (~L416) to also glob `expmap_*.png`; register
  `product_type='nircam_expmap_plot'`, scope field·filter, `content_type=image/png`.
- **Layout PNG** — new discover for `<field>_layout.png`; register `nircam_layout`, scope field.
- **Mosaic thumbnails** — in `discover_mosaics`/`_deploy_field_mosaics` (~L874/937), upload the
  `_thumb.png` for **sci** mosaics only; register `nircam_mosaic_thumbnail`, scope field·filter·tile.

`storage_objects.product_type` is free-form text (no enum/CHECK) — **no migration** for the new types;
they ride the existing `select_storage_objects_by_access` RLS via `deployment_id`. Verify the leak-test
gate still passes with the new types.

---

## Layer 4 — DB / RPC (`supabase/schemas/`)

No table changes. Two read RPCs in `functions.sql` (RLS-safe: respect `deploy_status`), plus indexes
if needed:

- **`get_nircam_fields()`** — landing grid. One row per field with ≥1 published mosaic:
  `field`, filter count, tile count, mosaic-file count, `sum(file_size)` volume, `max(created_at)`
  (last reduced), and the `nircam_layout` storage_key for the preview.
- **`get_nircam_field_summary(p_field)`** — field-page overview: filters[], tiles, pixel_scales[],
  extensions[], epochs[], file count, volume, sky coverage.

**Sky coverage / area — decision needed.** Options: (a) approximate from `map_layers` ra/dec bounds
(cheap, bbox-inflated); (b) compute the *exact* covered area in the pipeline from the expmap
(non-zero pixels × pixel area) and surface it — more accurate, which matters for survey-area use, but
needs somewhere to store a per-field scalar (there's no `fields` table). Recommend (b) with the value
written at deploy time onto the `nircam_layout`/expmap `storage_objects` row (or a lightweight
`nircam_field_summary` view/table); fall back to (a) if we want to ship the web first.

---

## Layer 5 — Web (`web/`)

**Routes**
- `web/app/nircam/page.tsx` → **landing** (field grid). Move today's flat table into the field route.
- `web/app/nircam/[field]/page.tsx` → **field detail**.
- Nav "NIRCam" → `/nircam`; field card / breadcrumb link into `/nircam/[field]`.

**Server actions (`web/lib/actions/nircam.ts`)**
- `getNircamFields()` → landing cards (`get_nircam_fields` RPC) + presigned `_layout.png` URLs.
- `getNircamFieldSummary(field)` → overview metadata.
- Scope `getNircamImages(field)` / `getNircamExpmaps(field)` / `getNircamFilterOptions(field)` by field.
  Merge expmaps into the products list as synthetic `extension='exp'` rows with `tile=null`.
- Image presign: extend the `download.ts` presign pattern (`generateNircamExpmapDownloadUrls`, etc.)
  to return **GET** URLs for the expmap-plot / thumbnail `<img>` sources.
- `getFitsglDatasets()` (from `map.ts`) → to enable per-row "View in map".

**Components (`web/components/nircam/`)**
- Landing: `NircamFieldGrid` / `NircamFieldCard` (layout preview `<img>` + stats + last-reduced).
- Field: reuse `NircamTable` (add Preview col + Actions col, `exp` rows), `NircamFilterBar` (drop the
  Field facet — page is scoped), `CurlScriptGenerator` (unchanged). New: `NircamFieldOverview`
  (metadata + `ExpmapBrowser`), `ExpmapBrowser` (left vertical filter tabs, ↑/↓ keyboard nav, dark
  expmap-PNG `<img>` + all-filter-overlay = the `_layout.png`), `FieldSelectorDropdown` (searchable
  single-select), `ThumbnailPopup`.
- **Actions**: `↓ FITS` (download) on every row; `↗ View` + inline thumbnail on **sci** rows only.
  "View" links to `/map?field=<field>&filter=<filter>` (reuse `ShowOnMapLink`), enabled when
  `getFitsglDatasets()` shows the field's dataset covers that filter.

**Types (`web/lib/types.ts`)** — `NircamFieldCard`, `NircamFieldSummary`; extend `NircamImage` for
`extension:'exp'` + optional `thumbnail_key`.

**Seed** — regenerate `supabase/seed.sql` to include the new `storage_objects` product_types so
preview branches render previews/thumbnails.

---

## Sequencing (stackable PRs)

1. **Pipeline** — dark expmap PNG + `<field>_layout.png` (+ CHANGELOG PATCH/Infrastructure).
2. **Layout contract** — 3 product specs + extension-aware bijection + golden fixtures (py + ts).
3. **Deploy** — register the 3 new product types.
4. **DB** — `get_nircam_fields` + `get_nircam_field_summary` RPCs (+ area decision).
5. **Web** — landing route + field route + components + View-in-map wiring; regenerate seed.

Web (5) can start against a hand-seeded row before the pipeline products exist; it only hard-depends
on the RPCs (4) and the registered keys (2–3) for live data.

## Decisions needed

1. **Expmap PDF** — keep the local PDF alongside the new dark PNG, or drop it? (recommend keep)
2. **Sky area** — exact (pipeline-computed, stored) vs. approximate (`map_layers` bbox)? (recommend exact)
3. **Layout plot styling** — colorbar/labels on `<field>_layout.png`, or bare coverage + tile
   outlines? (wireframe assumes bare + outlines)
