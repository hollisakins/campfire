# Design — FitsGL Integration ("Cloud DS9")

**Status:** Approved — all open questions (A–E) resolved; ready for phased implementation (§9)
**Scope:** Replace CAMPFIRE's Leaflet + PNG-tile map interface and the `sharp`-based
PNG cutout backend with the [FitsGL](../../fitsgl) FITS tile-pyramid engine, so any
deployed NIRCam mosaic can be opened in the map interface directly — scaled,
stretched, composited with other bands of the same field, and overlaid with the
NIRSpec catalog and MSA-shutter layers — without downloading the mosaic.

**Repos:** `hollisakins/campfire` (the map, the deploy CLI, the DB) and
`hollisakins/fitsgl` (the standalone engine — `fitsgl-py` producer, `@fitsgl/core`
consumer). FitsGL is developed as a standalone package; CAMPFIRE is one consumer.

**Method:** parallel deep-dive exploration of both codebases (web map, deploy/tiles/DB,
`@fitsgl/core` API, `fitsgl-py` CLI/deploy), with load-bearing claims verified against
source. No code changed by this doc.

---

## 1. Executive summary

The two arms are more compatible than expected. FitsGL already implements the exact
WCS model CAMPFIRE's map assumes (TAN, ICRS; and it displays **rotated pixels**
natively so no North-up reprojection is needed), already does client-side RGB /
multiband compositing with live stretch, and already ships a first-class instanced-WebGL
catalog overlay (sky coords, spatial-index hit-testing, tooltips, arbitrary per-marker
`data` payloads). CAMPFIRE's own `web/lib/fits/viewer.ts` already names FitsGL's
`FitsViewer` as the intended tile-pyramid viewer it "deliberately is not."

The target end state is **a single FitsGL pyramid per field**, serving three consumers
from one artifact and retiring the entire PNG stack:

- the **interactive map** (browser HTTP range reads → `<FitsViewer>`);
- the **cutout API** (server-side range reads → FITS cutouts *or* composited RGB);
- (implicitly) **thumbnails / OG images**, which are cutouts.

Retired once migration completes: the PNG tile pyramid, the `map_layers` table, the
`sharp` tile-compositing path, and — importantly — the **reprojection stage** of
`tiles_engine.py` (see §5).

The genuinely new component is the **FitsGL-powered cutout service**. Everything else is
wiring, packaging, and porting overlays.

### Corrections captured (things that looked like blockers and aren't)

- **Multi-tile fields are NOT rejected.** The loader rejects a *band* whose `tiles`
  array (a list of separate manifest/pyramid URLs) has length > 1 — the unbuilt "M6"
  feature of stitching independent pyramids into one band
  (`fitsgl-core/src/viewer-config.ts:117`, `fitsgl-config.ts:219`). A large field is
  one pyramid per band containing many **supertiles / fpack tiles** — fully supported,
  and already CAMPFIRE's working path. Pre-tiled input FITS assemble into one supertiled
  pyramid (SP8).
- **Reprojection is not needed.** CAMPFIRE's current reproject exists only to force
  North-up, not to align bands (bands already share WCS). FitsGL displays rotated pixels
  and offers an in-view `north_up` toggle, so the (slow) reproject stage is dropped from
  the FitsGL build path (§5).

---

## 2. Current state (verified)

### CAMPFIRE map (`web/components/map/`)
- **Leaflet + react-leaflet** in a custom pixel CRS (`MapViewer.tsx`; `L.CRS.Simple`
  with a Y-flip transform). Tiles: `${tile_base_url}/{z}/{x}/{y}.png?v=${tile_version}`.
- **WCS** is TAN, North-up, diagonal CD, hand-implemented in TS (`web/lib/utils/wcs.ts`)
  and **duplicated three times**: frontend, server cutout compositor
  (`web/lib/utils/tile-compositing.ts`), and the tile generator's `OutputGrid`
  (`python/campfire/deploy/tiles_engine.py`).
- **Overlays** are hand-rolled HTML5 canvas layers over Leaflet panes:
  `CanvasMarkerLayer.tsx` (NIRSpec objects, colored by `redshift_quality`, manual
  viewport culling + zoom-transform + hit-test) and `CanvasSlitLayer.tsx` (MSA shutters
  as rotated arcsec-sized rects, slitlet grouping, per-observation color, stuck-closed
  dashed). Overlay data comes from Supabase RPCs (`get_field_object_markers`,
  `get_field_shutters`, `slit_regions`).
- **All stretch/RGB is baked into the PNGs** at deploy time; the frontend does zero pixel
  processing for the map.

### CAMPFIRE cutouts
- `/api/v1/cutout` (public, RGB PNG), `/api/tile-thumbnail` (internal thumbnails),
  `/api/og-image` — all composite the **existing PNG tiles** server-side with `sharp`
  (`web/lib/utils/tile-compositing.ts`). No FITS is read; there is no FITS-cutout service.

### CAMPFIRE tiles + DB
- `campfire deploy tiles` (`python/campfire/deploy/tiles.py` + `tiles_engine.py`)
  reprojects mosaics to a North-up grid and writes 256² PNG `z/x/y` pyramids, uploads to
  the **`tiles` R2 bucket** (`CAMPFIRE_S3_TILES_*`), key `<field>/<filter>/<z>/<x>/<y>.png`
  (scheme-invariant, no `data/` prefix), `Cache-Control: public, max-age=31536000, immutable`;
  cache-busting via `map_layers.tile_version`.
- `map_layers` (one row per field×filter): `tile_base_url`, zoom range, `wcs_params` jsonb
  (the `OutputGrid`), bounds, counts, `tile_version`.
- `nircam_images` (one row per mosaic extension slot): `field, tile, filter, pixel_scale,
  extension, epoch, file_path, deploy_status, deployment_id`. **No sha column** — the
  content hash lives on `storage_objects.content_hash` (`sha256:…`).
- Mosaic FITS live in the **`data` bucket** (migrating R2→OSN), key
  `data/products/nircam/<field>/<filter>/…`. Layout keys are the authority
  (`campfire-layout`, mirrored in `web/lib/layout.ts`).

### FitsGL producer (`fitsgl-py`, pip `fitsgl`)
- CLI `fitsgl {init,build,demo,serve,verify,deploy,index}`. Usable **as a library**:
  `build.build_dataset(config, out_root, …)` (full multi-band dataset + `fitsgl.json`),
  `build_pyramid.build_pyramid(…)` (single pyramid), `deploy.deploy_dataset(…)` with a
  hand-built `DeployConfig` + `R2Target`.
- Build emits a self-contained dataset dir: per-band `manifest.json` + `.fits.fz` supertiles,
  optional `catalog.csv`, `fitsgl.json` (completeness marker), and a vendored viewer.
- Deploy is config-driven (`bucket`/`endpoint`/`public_url`/`prefix`), diffs by **sha256**
  against a per-prefix `deploy-manifest.json` ledger, push-then-purge, sets bucket CORS.
  Env creds: `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `CLOUDFLARE_API_TOKEN`. Needs a
  direct-key path (LIST/HEAD/DELETE/PutBucketCors) — **cannot** use CAMPFIRE's presigned
  `login`-mode uploads.
- Workspaces: one prefix per `[[field]]`, shared `[deploy]`, a `collection.json` landing page.

### FitsGL consumer (`@fitsgl/core`, npm)
- Pure ESM, own types. Subpaths `.`, `./react` (`<FitsViewer>` bare + `<FitsExplorer>`
  batteries-included), `./internal` (no stability contract), `./worker`. React is an
  **optional** peer dep.
- `<FitsViewer config={ViewerConfig}>` (controlled) + a `ref` `FitsViewerHandle`:
  `setMarkers/addMarkers/updateMarker/removeMarker/clearMarkers`, `autoStretch`,
  `fitToImage`, `setCenter/setZoom`, `screenToImage`/`imageToScreen`, `getViewer`, `exportPNG`.
- WCS (`parseWcs`/`skyToPix`/`pixToSky`, TAN+ICRS only, no SIP/TPV), RGB/multiband render
  sources with live `setChannelStretch`/`setStretchMode`/`setColormap`/`applyTrilogy`.
- Markers accept `{ra,dec}` or `{x,y}` + `shape∈{point,circle,box}`, `size`, `color`,
  `data`. Instanced WebGL, spatial-index hit-test, one reused tooltip DOM node.

---

## 3. Target architecture

```
                        campfire-tiles R2 bucket (CDN-fronted)
                        <field-prefix>/  fitsgl.json
                                         <band>/manifest.json + *.fits.fz supertiles
                                         catalog.csv (optional)
                                         deploy-manifest.json (ledger)
                                  |                         |
              browser range reads |                         | server range reads
                                  v                         v
                     <FitsViewer> (web)          FitsGL cutout service (Python)
                     + marker handle (objects)   -> FITS cutout  |  composited RGB PNG
                     + custom shutter overlay        (/api/v1/cutout, /tile-thumbnail, /og-image)
```

Single source of WCS: each band's `manifest.json` (native, possibly rotated). The
triple-duplicated TS/py WCS collapses to "read from the manifest."

DB: a thin **`fitsgl_datasets`** table replaces `map_layers` as the per-field pointer the
web viewer reads. `map_layers` + the PNG pyramid retire per-field once cutouts move off them.

---

## 4. Packaging / dependency infrastructure

Two independent dependency relationships, different maturity. **Open Question A —
RESOLVED (see below).** The `fitsgl` repo is **public**, and fitsgl changes are expected
to be occasional, so no submodule/workspace coupling is warranted — a plain pinned git
dependency on each side, with a local override for iteration.

### Python: `campfire` → `fitsgl` (ready today) — DECIDED: editable/local, git fallback
- Add `fitsgl[deploy]` as a new optional extra on the CAMPFIRE python package (e.g.
  `campfire[fitsgl]`), so a plain client install stays lean.
- **Dev:** editable local install (`pip install -e ../fitsgl/fitsgl-py[deploy]`) — both
  repos checked out side by side (document the convention). The deploy CLI is
  operator-run, not CI-built, so this is low-friction.
- **Fallback / reproducible:** git dependency
  (`fitsgl[deploy] @ git+https://github.com/hollisakins/fitsgl@<commit>#subdirectory=fitsgl-py`
  — `subdirectory=` required because the package is not at the repo root).
- Call it as a library (no shelling out): `build_dataset`, `build_pyramid`, `deploy_dataset`.

### TypeScript: `web` → `@fitsgl/core` — DECIDED: pinned public git dependency
`web/package.json` depends on the fitsgl repo pinned to a commit, e.g.
`"@fitsgl/core": "github:hollisakins/fitsgl#<commit>"` (public repo ⇒ **Vercel fetches it
with no auth token**). Prerequisites before the first render:
1. **Add a `prepare` build hook to `fitsgl-core/package.json`** (runs `tsc` → `dist/`).
   `dist/` is gitignored and there is no build hook today, so a git dependency would arrive
   with nothing consumable; `prepare` runs automatically on `npm install` from git. *(This
   is a change in the fitsgl repo — Phase 1 issue there.)*
2. **Worker in Next.js.** The default decode worker uses the Vite pattern
   `new Worker(new URL('../worker.js', import.meta.url), {type:'module'})`. Next
   (webpack/Turbopack) differs — CAMPFIRE must inject `tileOptions.workerFactory`
   (importing `@fitsgl/core/worker`) or run `useWorker:false` (inline main-thread decode)
   initially.
3. **Local iteration:** `npm link ../fitsgl/fitsgl-core` (or a temporary `file:` override)
   on the dev machine when co-editing; the committed manifest keeps the git pin so Vercel
   builds cleanly. Bumping the pin (commit + push in fitsgl → update the SHA in
   `web/package.json`) is the "release" step during this phase.
4. **Later:** publish `@fitsgl/core` to npm and switch to a version range once the API
   settles — removes the pin-bump step.

---

## 5. Build path (no reprojection)

`campfire fitsgl build --field <f>`:
1. Gather the field's per-filter mosaic FITS (native grid; a single FITS or a pre-tiled
   list/glob per band).
2. Generate a `fitsgl.toml` programmatically — bands from deployed mosaics, `[viewer]`
   RGB roles + stretch mirrored from CAMPFIRE's existing `get_rgb_configs`, catalog from a
   NIRSpec export. **CAMPFIRE controls the FitsGL config; scientists never hand-edit toml.**
3. Call `build.build_dataset` → dataset dir under a scratch/out root.

No `reproject_interp` / `OutputGrid` — the slowest current stage is gone, and skipping the
resample is *more* photometry-faithful before the quantize-8 step. The viewer displays
rotated pixels; `north_up` rotates in-view on demand.

**Grid uniformity (Open Question C — largely settled by the pipeline).** The pipeline's
`get_tile_wcs(tile_name, pixel_scale)` (`pipeline/.../nircam/field.py:894`) is
**filter-independent**: it returns `(crpix, crval, shape, rotation)` where `crval` is the
tile tangent point and `rotation` the tile rotation. So every filter resampled at the same
`pixel_scale` for the same tile lands on the **identical grid** — FitsGL's co-grid
precondition holds and filters composite with no resample. A field's tiles share the field
tangent point (`crval` defaults to `self.tangent_point`) and assemble into one pre-tiled
FitsGL pyramid (SP8) when they share rotation and differ by integer CRPIX — which CAMPFIRE
fields already satisfy (large multi-tile fields render in FitsGL today).

The one residual: **filters produced at *different* pixel scales** (e.g. SW at `30mas`, LW
at `60mas`) don't share a grid and can't RGB-composite. Resolution (**confirmed**): build each FitsGL
dataset at **one chosen pixel scale per field**, producing any needed filter at that scale
via the pipeline's derive path (cheap — CRPIX/NAXIS rescale about the same tangent point).
CAMPFIRE already produces all compositable filters (SW + LW) at a single common scale
(e.g. both `30mas`). Record the chosen scale in `fitsgl_datasets`.

### 5a. Dataset scope: tiles, fiducial sets, field composite

A field has multiple tiles, and **overlapping tile sets on different grids** can coexist —
e.g. COSMOS-Web `A1–B10` spanning the field vs. a convenience PRIMER N-up tile. Tiles on a
different tangent point / rotation cannot be composited into one pyramid.

- **Fiducial set** — a designated subset of a field's tiles that share tangent point +
  rotation and span the field, **declared in `fields.toml`** (e.g. a per-tile `fiducial =
  true` or a `[field].fiducial_tiles = [...]` list). The pipeline is the authority on tile
  WCS, so the declaration lives there.
- **Field-composite dataset** — the main full-field map view — is built from the fiducial
  set only. In FitsGL terms this is **one pyramid per band whose supertiles _are_ the
  fiducial tiles' mosaics** (the SP8 pre-tiled path), so it is not a separate expensive
  assembly, and FitsGL's per-object sha-diff gives **per-tile incremental rebuilds** for
  free (change one tile's mosaic → only its supertiles re-upload).
- **Per-tile datasets** — any tile can also be built as its own standalone dataset for a
  single-tile interactive viewer. Off-grid tiles (PRIMER) *require* this (they can't join
  the composite); on-grid fiducial tiles can alternatively be viewed as a bookmarked
  viewport into the composite rather than duplicated.
- All bands within any one dataset are at a single common pixel scale (SW + LW both e.g.
  `30mas`) so RGB composites; filters missing on some fiducial tiles are NaN-padded by
  `shared_grid`.

---

## 6. Backend + tracking

### R2
- Point FitsGL deploy at the **`campfire-tiles`** bucket with a per-field `prefix` disjoint
  from the PNG key space (`<field>/<filter>/z/x/y.png`), so FitsGL's prefix-scoped
  diff/delete never touches existing PNGs.
- One-time Cloudflare setup on the tiles domain: a **Cache Rule for `.fits.fz`** (not on
  Cloudflare's default cacheable-extension list; `fitsgl verify --origin <web origin>`
  flags its absence) and CORS `viewer_origin = <CAMPFIRE web origin>`.
- **Creds:** FitsGL tile deploy is a direct-key / service-role operation (it needs
  LIST/HEAD/DELETE/PutBucketCors and cannot ride presigned uploads). This is consistent
  with CAMPFIRE today — `clean_tiles` already requires direct `r2_tiles` creds. When
  driving `deploy_dataset` as a library, map `CAMPFIRE_S3_TILES_*` into FitsGL's
  `R2Target`/`DeployConfig` rather than setting `R2_*`.
- Cache-busting: FitsGL serves tiles cacheable + purges the edge on change, pointers
  `no-cache`, and namespaces its client disk cache by a manifest content-hash (auto-invalidates
  on rebuild). No `tile_version` equivalent needed.

### DB: `fitsgl_datasets` (new, thin)
One row per **scoped** dataset (multi-band, so per field-composite or per tile — **not** per
`nircam_images` extension). A field has one composite row plus optional per-tile rows:

```
fitsgl_datasets(
  prefix text primary key,          -- R2 key namespace + stable identity
  field text not null,
  kind text not null,               -- 'field' (fiducial composite) | 'tile'
  tile text,                        -- null for kind='field'; tile name for kind='tile'
  pixel_scale text not null,        -- e.g. '30mas'
  fitsgl_json_url text not null,    -- what the web viewer points <FitsViewer> at
  bands text[] not null,
  source_hashes jsonb not null,     -- {tile|filter -> sha256} of mosaics this was built from
  is_default boolean default false, -- the field's default map dataset (the composite)
  schema_version int not null,
  deployed_at timestamptz not null
)
```

The web **map** view selects the field's `kind='field'` (fiducial composite) row; a
**single-tile viewer** selects a `kind='tile'` row. `source_hashes` still drives
mosaic-level sha-dedup (§ below); for the composite it maps each fiducial tile→filter mosaic.

- **Sha dedup at the mosaic level:** `source_hashes` records the mosaic content hashes
  (from `storage_objects.content_hash`) the dataset was built from. On a (re)build request,
  compare current mosaic hashes to `source_hashes`; unchanged + `fitsgl.json` present in R2
  ⇒ skip. FitsGL's own object-level sha256 diff then makes the actual upload incremental.
- The web viewer reads `fitsgl.json` for the band inventory + default view, so this table
  stays thin.

---

## 7. Cutout service (the one new component)

A Python service (reusing `fitsgl-py` manifest geometry) that serves the same pyramid the
browser uses.

- **Addressing:** the manifest carries level scale, per-supertile `tile_origin`/`tile_count`,
  and WCS. Reuse that (Python side of `resolveSupertile`/`SupertileInfo`) to pick the level
  matching the requested FOV/output size (same `idealZoom` logic `tile-compositing.ts` has)
  and identify covering supertiles.
- **Read:** `.fits.fz` supertiles are standard fpacked FITS → astropy reads them directly;
  fetch just the covering supertile(s) from R2 (coarse levels keep these small); go to
  fpack-tile byte-range reads later if thumbnails need it.
- **Return FITS:** assemble the pixel region, carry the (rotated) WCS header; rotate to
  N-up server-side if a caller wants it (cheap at cutout sizes).
- **Return RGB:** reimplement FitsGL's stretch/colormap/trilogy formulas in numpy (small)
  and composite → PNG/JPEG. No headless GL needed at cutout sizes.
- **Fidelity (Open Question D — RESOLVED):** the API takes **`?fast=true` (default)**. When
  `fast=true`, cutouts come from the display pyramid (RICE Q=8, ~0.03% lossy) — much faster;
  `fast=false` reads the **raw lossless mosaic** (data/OSN bucket) for photometry-grade
  output. Documented caveat verbatim: *"`fast=true` fetches cutouts from compressed mosaics
  (RICE Q=8, lossy to ~0.03% level). Much faster, but sensitive science should use
  `fast=false`."* v1 ships the `fast=true` path (FITS + RGB from the pyramid); the
  `fast=false` raw-mosaic path is a fast-follow (its cost depends on how raw mosaics are
  stored for range access).
- **Consumers to re-point:** `/api/v1/cutout`, `/api/tile-thumbnail` (latency-sensitive —
  lean on coarse-level fetch + the existing 1-week cache), `/api/og-image`.

---

## 7a. Region overlay primitive (FitsGL feature)

A new overlay glyph class in FitsGL's instanced-WebGL overlay system, distinct from the
existing point/circle/box markers (which are CSS-px, unrotatable, catalog-oriented). Drives
CAMPFIRE MSA shutters now, and NIRSpec pointing footprints / NIRCam tile boundaries / DS9
regions later. Proposed scope:

- **On-sky sizing** — extent given in angular units (arcsec) or world pixels, so a region
  scales with zoom (a shutter keeps its true angular size). This is the key difference from
  markers.
- **Rotation** — a position angle, so shutters/tiles orient correctly on rotated (non-N-up)
  native pixels.
- **Shapes, phased:** (1) rotatable rectangle — covers MSA shutters and NIRCam tile
  boundaries; (2) general polygon — arbitrary footprints (NIRSpec MSA quadrant footprints).
  Optionally a world-sized circle (apertures) later.
- **Style** — per-region fill (with alpha), stroke width, and **dashed stroke** (CAMPFIRE
  needs it for stuck-closed shutters), per-region color.
- **Perf** — instanced (a field can have thousands of shutters); rectangles instance
  cleanly (center + half-extents + PA + color + fill/stroke flags). Polygons are a separate
  path (triangulation / line loops).
- **Interaction seam** — extend the existing spatial-index hit-test so regions can carry a
  `data` payload and fire click/hover, enabling future interactivity (click a shutter →
  jump to its NIRSpec spectrum). Not needed for v1 display, but designed in.
- **CAMPFIRE keeps the domain logic** — it computes shutter/footprint geometry (MSA layout,
  slitlet grouping, per-observation color, stuck flags) and feeds resolved regions to
  FitsGL; FitsGL only renders + hit-tests generic regions.

## 8. Open questions

- **A. Dependency sourcing / packaging** — **RESOLVED.** fitsgl repo is public; changes
  occasional ⇒ no submodule/workspace. Python: editable/local install, git-pin fallback.
  TypeScript: pinned public git dependency (`github:hollisakins/fitsgl#<commit>`) + a
  `prepare` hook in fitsgl-core + `npm link` for local iteration; publish to npm later.
  See §4.
- **B. Shutters / region overlays** — **RESOLVED: build a generic region primitive in
  FitsGL** (not the CAMPFIRE-canvas seam). Rationale: reusable well beyond shutters —
  NIRSpec pointing footprints, NIRCam tile boundaries, arbitrary DS9-style regions — which
  aligns with the standalone-FitsGL goal. Existing markers are **CSS-px sized and
  unrotatable** (`markers.ts:56`), so they cannot represent on-sky shutters even
  approximately; the region primitive is the real fix. See §7a. This is a FitsGL-repo
  feature that gates CAMPFIRE's shutter + footprint overlays (§9 phasing).
- **C. Per-field grid uniformity** — **RESOLVED.** All compositable filters (SW + LW) are
  produced at one common pixel scale (e.g. `30mas`), so RGB composites with no resample.
  Datasets are **tile-scoped**: a `fields.toml`-declared **fiducial tile set** (shared
  tangent point/rotation, spans the field) forms the field-composite map view; off-grid
  tiles (PRIMER) get standalone per-tile datasets. See §5, §5a. *(Requires a small pipeline
  change: a fiducial declaration in `fields.toml`.)*
- **D. Cutout fidelity** — **RESOLVED.** `?fast=true` default (display pyramid, Q=8 lossy,
  fast); `fast=false` reads the raw lossless mosaic for science. v1 ships `fast=true` FITS +
  RGB; `fast=false` is a fast-follow. See §7.
- **E. Migration cutover** — **RESOLVED.** Per-field feature flag, Leaflet + FitsGL side by
  side; retire Leaflet + the PNG stack per field only once that field is on FitsGL and its
  cutouts are migrated. See §9 (Phase 4/5).

---

## 9. Phased plan

1. **Packaging unblock.** `@fitsgl/core` `prepare` hook + Next worker-factory shim; wire
   `fitsgl[deploy]` into CAMPFIRE Python. *(Resolves Open Question A first.)*
2. **One field, native, end-to-end.** `campfire fitsgl build` (no reproject) →
   `fitsgl serve` → render in a throwaway Next page. Validates the pixel-scale assumption
   (Open Question C) early.
3. **Backend + tracking.** `.fits.fz` Cache Rule + CORS on `campfire-tiles`;
   `campfire fitsgl deploy` to a prefix; `fitsgl_datasets` table + source-hash dedup; the
   `campfire deploy` "tiles missing/stale → suggest `campfire fitsgl …`" hook (suggest, don't
   auto-build — pyramid builds are expensive).
4. **Swap the map behind a flag (objects first).** `<FitsViewer>` + ported object markers
   (via the ref handle, deleting `CanvasMarkerLayer`) alongside Leaflet, per-field. Shutters
   are *not* in this sub-phase — they wait on the region primitive (4b).
4b. **Region primitive (FitsGL) + shutters.** Build the FitsGL region overlay (§7a; rotatable
   rectangle first), pin the new FitsGL commit, then port `CanvasSlitLayer`'s geometry to feed
   FitsGL regions. (Rectangle-only unblocks shutters + NIRCam tiles; polygon footprints follow.)
5. **Cutout service + retire PNG stack.** FitsGL-powered FITS+RGB cutouts; re-point the three
   API routes; then retire PNG tiles + `map_layers` + the reproject pipeline per field.

---

## 10. CLI surface

`campfire fitsgl` command group (registered lazily like `deploy` in `python/campfire/cli.py`):

- `campfire fitsgl build --field <f> [--pixel-scale 30mas]` — build the **fiducial
  composite** (default) from `fields.toml`'s fiducial set; generate `fitsgl.toml` +
  `build_dataset`.
- `campfire fitsgl build --field <f> --tile <t>` — build a **single-tile** standalone
  dataset (the only path for off-grid tiles like PRIMER).
- `campfire fitsgl deploy --field <f> [--tile <t>]` — deploy the composite (or a tile) to
  `campfire-tiles/<prefix>`, upsert the matching `fitsgl_datasets` row.
- Hook in `campfire deploy`: after a mosaic deploy, check `<prefix>/fitsgl.json` + compare
  `source_hashes`; if missing/stale, print a suggested `campfire fitsgl …` command.

FitsGL's workspace model (one prefix per field, shared `[deploy]`, `collection.json` landing
page) maps ~1:1 onto CAMPFIRE fields; start per-field, adopt the workspace/collection later
if a unified landing page is wanted.
