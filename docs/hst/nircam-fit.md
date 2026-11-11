# NIRCam pipeline assessment for HST reuse (Stage 2)

Date: 2026-11-11. Branch `hst-feasibility`, based on `259f1bdd`.

This document describes the NIRCam imaging pipeline as it is in the repository today and classifies each part by how it could serve an HST ACS/WFC and WFC3/IR imaging module. No code was changed.

**Evidence tags.** Statements about this repository are *verified* (I read the code at the cited `path:line`) unless marked otherwise. Statements about HST data formats are marked **[HST]** and are backed in `feasibility.md`; where one rests on memory only it is marked *inferred*.

Paths are relative to the repo root. `nircam/` means `pipeline/campfire_pipeline/nircam/`, `common/` means `pipeline/campfire_pipeline/common/`.

## 1. Pipeline map

### 1.1 Entry points

`cfpipe` (`pipeline/pyproject.toml:48`) is a Click group that mounts one subgroup per instrument: `main.add_command(nirspec_cli, 'nirspec')` and `main.add_command(nircam_cli, 'nircam')` (`pipeline/campfire_pipeline/cli.py:56-60`). `cfpipe download` is shared and takes `--instrument nirspec|nircam` (`cli.py:144-146`).

The NIRCam subgroup (`nircam/cli.py`) exposes two phase commands, one command per step, and utilities:

| Command | What it runs | Reference |
| --- | --- | --- |
| `process` | per-exposure phase, detector1 to jhat/align | `nircam/cli.py:166-181` |
| `combine` | ensemble phase, apply_mask to resample | `nircam/cli.py:204-225` |
| `run --process/--align/--combine/--all` | phases in one call | `nircam/cli.py:228-281` |
| `<step>` | any single step, registered from `STEP_NAMES` | `nircam/cli.py:288-308` |
| `resample`, `align` | single steps with `--tiles` / `--epoch` | `nircam/cli.py:184-201`, `311-327` |
| `check` | which mosaic tiles are stale | `nircam/cli.py:439-489` |
| `status`, `reset` | per-exposure step-completion table; clear step stamps | `nircam/cli.py:492-679` |
| `expmap`, `mosaic-exp`, `rgb`, `refcat ...` | exposure maps, RGB composites, reference catalogs | `nircam/cli.py:330-432` |

### 1.2 Configuration

Three layers, merged in this order (later wins):

1. Package defaults, `pipeline/campfire_pipeline/data/config_default.toml`, one flat `[nircam.<step>]` table per step (sections listed at lines 174-966).
2. User config (`--config` or `$CAMPFIRE_ROOT/config/config.toml`), merged in `load_config` (`pipeline/campfire_pipeline/config.py:64`).
3. Per-field step overrides, `[<field>.<step>]` in `fields.toml`, merged by `get_nircam_step_config` (`config.py:366-379`).

Config is parametric only. Which steps run, `--overwrite`, `--processes`, `--tiles` and `--epoch` are CLI arguments (`nircam/cli.py:74-123`).

A field is one top-level table in `$CAMPFIRE_ROOT/config/fields.toml` (schema in `pipeline/fields.example.toml`; parsed by `Field.load`, `nircam/field.py:364-509`). It holds the filter list, the exposure selection as filename globs, an optional skip list, the tangent point, the tile grids, optional epochs (named exposure subsets), manual WCS shifts and the RGB recipe. The repository does not contain the production field list; it lives in that file and in the cloud `fields` table (config plane, `AGENTS.md`).

`setup_environment` exports every key of `[environment]` into `os.environ` for the whole process (`config.py:115-147`). The defaults are `CRDS_SERVER_URL = "https://jwst-crds.stsci.edu"` and `CRDS_CONTEXT = "jwst_1481.pmap"` (`config_default.toml:10-12`).

### 1.3 Directory layout

All paths come from the `campfire_layout` package (`nircam/field.py:25-33`).

| Tree | Path | Reference |
| --- | --- | --- |
| raw | `raw/nircam/{PID}/{filter}/*_uncal.fits`, plus `manifest.ecsv` per PID | `nircam/field.py:655-676`, `cli.py:180-182` |
| canonical exposures, mosaics, manifests, diagnostics | `products/nircam/<field>/<filter>/` | `nircam/field.py:569-588` |
| combine working copies (never deployed) | `products/nircam_work/<field>/<filter>/` | `layout/campfire_layout/paths.py:89-106` |
| per-field reference state | `reference/nircam/<field>/{masks,astrom_cats,bad_pixels}`, `exposures.json` | `nircam/field.py:537-540`, `590-615` |
| shared calibration | `reference/nircam/shared/{flats,wisps}` | `nircam/field.py:554-556` |

One FITS file per exposure and detector, named `<rootname>.fits` with no stage suffix, is created by `detector1` and then rewritten in place by every later process step (`nircam/field.py:703-718`). Each step records itself as a `CFP_<step>` keyword in the primary header (`common/cfp.py:1-23`); the keyword's presence is what makes a re-run skip that exposure (`nircam/orchestrate.py:188-201`).

### 1.4 Stages in order

Process phase, per filter, order from `PROCESS_STEPS` (`nircam/orchestrate.py:46-63`):

| # | Step | Granularity | Input | Output | Upstream tool | Stamp |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | download | program | MAST | `_uncal.fits`, `manifest.ecsv` | MAST JWST Search API or CAOM TAP (`common/query.py:30-42`) | |
| 1 | detector1 | exposure | uncal | canonical rate `ImageModel`, `_jump.fits` sidecar | `jwst` `Detector1Pipeline` (`steps/detector1.py:1-13`) | `CFP_DET1` |
| 2 | jackknife | exposure | canonical + jump sidecar | ramp zero-point correction | `jwst` `RampFitStep` (`steps/jackknife.py:1-22`, `225-226`) | `CFP_JACK` |
| 3 | persistence | filter ensemble | canonical + jump sidecars | DQ flags | `snowblind` (`steps/persistence.py:1-19`) | `CFP_PERS` |
| 4 | wisp | exposure | canonical | wisp-subtracted SCI | `nmfwisp` or scaled templates (`steps/wisp.py:1-22`) | `CFP_WISP` |
| 5 | image2 | exposure | rate | cal `ImageModel` in MJy/sr with gwcs | `jwst` `Image2Pipeline` (`steps/image2.py:1-14`) | `CFP_IMG2` |
| 6 | edge | exposure | cal | DQ edge flags | own (`steps/edge.py:1-12`) | `CFP_EDGE` |
| 7 | bkg | exposure | cal | pedestal, 1/f and optional 2-D background removed; `SRCMASK` extension; `VAR_RNOISE` rescaled | own (`steps/bkg.py:1-66`) | `CFP_BKG` |
| 8 | diag_striping | exposure, opt-in | cal | diagonal scattered-light stripes removed | own (`steps/diag_striping.py:1-22`) | `CFP_DIAG` |
| 9 | wcs_shift | exposure, opt-in | cal | manual gwcs offset | `jwst.tweakreg.utils.adjust_wcs` (`steps/wcs_shift.py:190-192`) | `CFP_SHFT` |
| 10 | preview | exposure | cal | S/N PNGs for web triage | own (`steps/preview.py:1-22`) | `CFP_PREV` |
| 11 | jhat *or* align | exposure / detector pool | cal + reference catalog | corrected gwcs | `jhat` (`steps/jhat.py:1-14`) or own solver on `tweakwcs` (`nircam/align/solve.py:1-58`) | `CFP_JHAT` / `CFP_ALGN` |

`align` replaces `jhat` for fields that set `[<field>.align].enabled` (`nircam/orchestrate.py:671-687`).

Combine phase, per filter, order from `COMBINE_STEPS` (`nircam/orchestrate.py:65-70`):

| # | Step | Granularity | Input | Output | Upstream tool | Stamp |
| --- | --- | --- | --- | --- | --- | --- |
| 12 | apply_mask | exposure | canonical + `.reg` files | `CFMASK` extension on the canonical | `regions` (`steps/apply_masks.py:1-22`) | `CFP_MASK` |
| | materialize_work | filter | canonical | working copy with `CFMASK` fused into DQ as `DO_NOT_USE` | own (`nircam/field.py:814-876`) | |
| 13 | bad_pixel | filter ensemble, opt-in | working copies | empirical bad-pixel masks | own (`steps/bad_pixel.py:1-22`) | `CFP_BPIX` |
| 14 | outlier | visit | working copies | `OUTLIER` DQ bits; per-visit manifest | `jwst` `Image3Pipeline` or own drizzle + `stcal` median + `jwst` `flag_resampled_model_crs` (`nircam/outlier_detect.py:1-31`, `128-131`) | `CFP_OUT` |
| 15 | resample | tile | working copies with `CFP_OUT` | `_i2d.fits` (SCI, ERR, WHT, CON, EXPTIME, NEXP), then mosaic background subtraction, split `_sci/_err/_wht/_srcmask/_exp` files, thumbnails, `_manifest.json` | `jwst` `Image3Pipeline` or own drizzle on the `drizzle` package (`steps/resample.py:43-198`, `nircam/drizzle.py:580-772`) | `CMPFRVER`, `CMPFRTIM`, `CFP_BKGS` on the mosaic |

Two backends exist for both outlier and resample, selected by `implementation = "jwst" | "campfire"`; the default is `"jwst"` for both (`config_default.toml:945`, `1013`). In production every field overrides this: all use `align` (not `jhat`) and the `campfire` backend for both combine steps (stated by H. Akins on review, 2026-11-11; the overrides live in the local `fields.toml`, not in the repo). The package defaults therefore do not describe the production path.

### 1.5 Mosaic grids and tangent points

The output grid is fully specified in `fields.toml` and does not depend on the input exposures.

- Each field has one `tangent_point = [RA, Dec]`; a tile may override it (`nircam/field.py:989`).
- Each tile has a `rotation` and, at one or more pixel scales, `crpix` (FITS 1-indexed) and `naxis` (`pipeline/fields.example.toml:20-36`).
- `Field.get_tile_wcs` returns `(crpix, crval, shape, rotation)` with `crpix` converted to 0-indexed (`nircam/field.py:927-990`). The projection is TAN with `CD = scale * [[-cos r, sin r], [sin r, cos r]]` (`nircam/field.py:1093-1103`).
- If a tile does not declare the requested scale, the grid is derived from the first declared scale by `crpix' = (crpix - 0.5) * ratio + 0.5`, `naxis' = round(naxis * ratio)` (`nircam/field.py:975-981`). This keeps pixel edges aligned, so a derived 60 mas pixel is exactly a 2x2 block of 30 mas pixels.
- The default output scale is 30 mas (`config_default.toml:1014`).
- The drizzle output WCS is a gwcs built by `stcal.alignment.util.wcs_from_sregions` from those numbers, using the first input exposure only as a template for the frame (`nircam/drizzle.py:81-128`).
- Tile membership of an exposure is decided by intersecting the detector footprint with the tile polygon (`nircam/geometry.py:29-56`, called from `steps/resample.py:264-265`).

One thing to settle before HST mosaics are put "on the same grid" at a scale other than 30 mas: the production COSMOS tiles declare their 60 mas grids explicitly as `crpix60 = crpix30 / 2` (for example `[-22990, 23090]` and `[-11495, 11545]`, the same numbers as `pipeline/fields.example.toml:92-97`). That is a different convention from the derivation formula above, which would give `-11494.75`. With the explicit values a 60 mas pixel is offset by a quarter of its width (15 mas) from the 2x2 blocks of the 30 mas grid. Both are valid grids on the same tangent plane; they are just not block-aligned with each other. (Production values read from the local `fields.toml`, which is outside the repo.)

### 1.6 Change tracking and provenance

- Per exposure: `CFP_*` keywords (`common/cfp.py`).
- Per mosaic: a JSON manifest listing every input with a SHA-256 over its SCI and DQ arrays, a separate SHA-256 over its WCS header cards, size and mtime, plus a hash of the pixel-affecting resample settings (`nircam/manifest.py:25-57`, `66-122`, `288-317`, `324-391`). A tile is rebuilt when inputs or settings change (`steps/resample.py:270-290`).
- Pipeline version: resolved from `pipeline-vX.Y.Z` git tags and stamped as `CMPFRVER` (`common/version.py:1-49`, `nircam/drizzle.py:337-345`).

### 1.7 How outputs reach storage and the database

This path was traced by a read-only sub-agent; the references marked † are ones I re-read myself. The rest are the sub-agent's reading and should be treated as *verified by delegate*.

Brief orientation, since this part is outside the pipeline package. The pipeline writes files under `$CAMPFIRE_ROOT/products/`. A separate client package (`python/campfire/deploy/`) uploads them to object storage and writes rows into a Postgres database (Supabase). The web app reads only the database and storage, never the local tree. A **storage key** is the object's path in the bucket; a **registry row** (`storage_objects`) records each object with its content hash; a **row-level security policy** is a database rule deciding which logged-in users may read which rows.

**Layout contract.** `layout/campfire_layout` is the one place that maps a product to its local path and storage key. Each product kind is a `ProductSpec` tied to an `Instrument`, which has exactly two members, `nirspec` and `nircam` (`layout/campfire_layout/scope.py:22-24`). NIRCam kinds are `nircam_exposure`, `nircam_exposure_preview`, `nircam_exposure_full`, `nircam_mosaic`, `nircam_rgb`, `nircam_expmap`, `nircam_expmap_plot`, `nircam_mosaic_thumbnail`, `nircam_mosaic_quicklook`, `nircam_layout`, `tile`, plus reference kinds (`layout/campfire_layout/products.py:180-266`, `320-360`). Keys have the form `data/products/nircam/<field>/<filter>/<filename>` (`layout/campfire_layout/keys.py:58-105`). The reverse parser classifies files in a filter directory by prefix: `mosaic*` is a mosaic, `expmap*` an exposure map, and any other `.fits` is an exposure (`layout/campfire_layout/bijection.py:151-171`). A golden fixture (`layout/conformance/layout_golden.json`) is checked from Python (`pipeline/tests/test_layout.py`) and from the TypeScript mirror (`web/lib/layout.ts`, `web/lib/layout.test.ts`); a test fails if a product kind has no fixture row (`pipeline/tests/test_layout.py:260-264`).

**Deploy.** `campfire deploy --field X` calls `deploy_nircam` (`python/campfire/deploy/cli.py:310-405`, `python/campfire/deploy/nircam.py`). It:

- finds exposures as `jw*.fits` in each filter directory and takes visit and detector from the JWST filename fields (`nircam.py:192-221`);
- finds mosaics through their `mosaic_*_manifest.json` and requires the name to equal `mosaic_nircam_<filter>_<field>_<scale>_<tile>[_<epoch>]` (`nircam.py:940-968`); uploads the split `_sci/_err/_wht/_srcmask/_exp` files and thumbnails, never the `_i2d` (`nircam.py:879-912`);
- reads `CMPFRVER`, `CAL_VER` and `CRDS_CTX` from a product header into `deployments.cfpipe_version`, `jwst_version`, `crds_context` (`nircam.py:427-478`);
- writes one `deployments` row, the `storage_objects` rows, `nircam_exposures` (the exposure review queue), `nircam_images` (one row per field, tile, filter, pixel scale, extension, epoch) and the `fields` row (`nircam.py:657-870`, `1153-1192`; `python/campfire/deploy/fields.py:190-217`).

Exposure deduplication uses the two-part identity described in `AGENTS.md`: a hash over the arrays in extensions named `SCI`, `DQ`, `CFMASK`, and a hash over a whitelist of WCS cards in the primary and `SCI` headers (`python/campfire/storage/hashing.py:120-137`, `191-262`). The whitelist is the same one as in `nircam/manifest.py:66-75` and has the same gaps for HST (section 3, items 2 and 3).

Neither the deploy code nor the web app reads `BUNIT`, `PIXAR_SR` or `PHOTMJSR`. Units are carried only by the FITS headers of the mosaics themselves.

**Database.**

| Table | Relevant shape | Reference |
| --- | --- | --- |
| `fields` | name, filters, tiles, epochs, `jwst_program_ids`, config; no instrument column | `supabase/schemas/tables.sql:892-924` |
| `nircam_images` | unique on (field, tile, filter, pixel_scale, extension, epoch); no instrument column; no CHECK on filter or scale values | `tables.sql:936-955`, `2682-2683` |
| `nircam_exposures` | unique on (field, filter, filename); `detector NOT NULL` | `tables.sql:975-1005` |
| `deployments` | exactly one of observation / field; `jwst_version`, `crds_context` | `tables.sql:804-839` |
| `storage_objects` | `instrument` CHECK limited to `nirspec`, `nircam` †; `product_type` CHECK enumerates every kind | `tables.sql:1372`, `1385-1400` |
| `fitsgl_datasets` | one dataset per field (or tile) at one pixel scale with a list of bands | `tables.sql:299-313` |
| `dr_nircam_images`, `dr_fields` | frozen copies for data releases; release components are named `nircam/<field>` | `tables.sql:1967-2017` |

Access: published imaging is public to every account. The `nircam_images` read policy is `deploy_status = 'published' OR is_admin()`, with no program scoping † (`supabase/schemas/policies.sql:927-948`). So HST mosaics, which are all public data, fit the existing access model without a new rule.

**Web.** There is no table of imaging filter metadata in the web app. Three places infer wavelength from the filter name using NIRCam's convention (digits = microns x 100):

- the FitsGL build sorts bands with `f(\d{3,4})[wmn]` and multiplies three-digit names by 10 † (`python/campfire/fitsgl/build.py:209-226`). `f814w` becomes 8140 and sorts redder than `f444w`; `f850lp` does not match and sorts last.
- the field page counts short- and long-wavelength filters with `parseInt(f.slice(1,4)) < 240` † (`web/app/nircam/[field]/NircamFieldPageContent.tsx:52-64`). F814W and F606W would be counted as long-wavelength.
- pixel-scale labels are parsed as `NNmas` (`NircamFieldPageContent.tsx:89-97`).

The cutout routes and the map viewer read the FitsGL pyramid of a field; a FitsGL dataset holds one pixel scale and is built with `shared_grid = True` (`python/campfire/fitsgl/build.py`, around lines 196-204). Science cutouts return pyramid pixels unchanged, with no unit conversion and no `BUNIT` (`web/lib/cutout/science.ts:1-7`, `web/lib/cutout/fits.ts:119-145`). The consequence for HST: mosaics on the same 30 mas grid and in the same unit can join the existing field dataset as extra bands; mosaics at another scale or in another unit cannot share that dataset as it is built now.

The Python client already knows HST band names for catalog photometry: pivot wavelengths and filter-curve names for ACS/WFC F435W to F850LP and WFC3/IR F098M to F160W (`python/campfire/deploy/photometry.py:48-94`, `python/campfire/calibration.py:40-138`).

**Existing HST mosaics on the CAMPFIRE grid.** `scripts/cosmos_inspec.py:52-55` † reads ACS/WFC and WFC3/IR mosaics with names such as `mosaic_cosmos_web_30mas_tile_{tile}_hst_acs_wfc_f814w_{ext}.fits` and `mosaic_cosmos_all_hst_acs_wfc_f606w_30mas_tile_{tile}_v0.3_{ext}.fits`. These are externally produced HST mosaics on the COSMOS-Web 30 mas tile layout. They are not produced by this repository, but they show that a comparison reference on the same tile grid already exists for COSMOS.

## 2. Classification of modules

Classes: **A** reusable for HST as is; **B** reusable after generalization; **C** JWST-only; **D** missing and needed for HST.

### 2.1 Core mosaic machinery

| Module | Lines | Class | What is JWST-specific now |
| --- | --- | --- | --- |
| `nircam/drizzle.py` | 852 | B | The arithmetic is instrument-neutral: per input it needs `data`, a variance array, a weight array and a pixel map into the output frame, and hands them to `drizzle.resample.Drizzle.add_image` (`drizzle.py:567-577`, `693-726`). JWST enters in four places: reading inputs as `ImageModel` and taking `meta.wcs` (gwcs), exposure times and pixel area from it (`drizzle.py:514-523`); building the weight with `stcal` `build_driz_weight` and `jwst` DQ flag names (`drizzle.py:534-540`); requiring the three variance components `var_rnoise`, `var_poisson`, `var_flat` (`drizzle.py:544-549`); and writing the result through `ImageModel` with `ModelBlender` header blending, `MJy/sr` units and `PIXAR_*` (`drizzle.py:200-251`, `254-362`). The output WCS builder needs a JWST reference gwcs and `wcsinfo` (`drizzle.py:107-128`). |
| `nircam/steps/resample.py` | 560 | B | The tile loop, staleness test, mosaic background subtraction, extension splitting and thumbnails operate on the mosaic only (`resample.py:249-555`). The `jwst` backend (`resample.py:83-198`) is JWST-only. |
| `nircam/outlier_detect.py` | 254 | B | Same shape as drizzle: per-input single drizzles, a streaming median (`stcal` `MedianComputer`, generic), then `jwst.outlier_detection.utils.flag_resampled_model_crs`, which takes a JWST datamodel and blots through its gwcs (`outlier_detect.py:128-131`, `207-212`). The intermediate WCS again comes from `wcs_from_sregions` (`outlier_detect.py:44-70`). |
| `nircam/steps/outlier.py` | 640 | B | Visit grouping, overlap pooling and manifests are generic in idea but keyed on JWST filenames: program ID is `basename[2:7]` (`outlier.py:284-290`), footprints come from `S_REGION` (`outlier.py:293-309`). The region-growing of large outlier components works on arrays (`outlier.py:145-256`). |
| `nircam/bkgsub.py` | 977 | A/B | `SubtractBackground` works on a mosaic's SCI/ERR/WHT arrays. It imports `jwst.datamodels.dqflags` at module top for an optional DQ mask (`bkgsub.py:35`, `273`). Mask sizes are in pixels tuned at 30 mas (`steps/bkg.py:80-86`). |
| `nircam/manifest.py` | 593 | B | Hashing reads `hdul['SCI']` and `hdul['DQ']` by name, which returns only the first chip of a two-chip file (`manifest.py:49-56`). The WCS-card whitelist covers linear terms, SIP and JWST `V2_REF`-style keys, and not the HST lookup-table distortion (`manifest.py:66-75`). Input records parse visit and detector from JWST filename fields (`manifest.py:357-361`). The mosaic name template is `mosaic_nircam_[filter]_[field_name]_[pixel_scale]_[tile]` (`manifest.py:185`). |
| `nircam/geometry.py` | 189 | B | Footprint from the WCS in `hdul[1]` with a default 2048x2048 frame (`geometry.py:29-56`); the tile pre-filter relies on the `S_REGION` keyword (`geometry.py:97-131`). |
| `nircam/field.py` | 1118 | B | Tile and grid logic is instrument-neutral (`field.py:927-1110`), as are epochs, skip lists and working copies. JWST-specific: every `files`, `skip`, `wcs_shift` and epoch glob must match `^jw\d{5}` (`field.py:38`, `405-426`, `205-210`, `283-288`); raw files are found as `raw/nircam/{PID}/{filter}/*_uncal.fits` (`field.py:672-676`); the products root is hard-wired to `nircam` (`field.py:533`); `DO_NOT_USE = 1` is hard-coded for the mask fusion (`field.py:111-136`); the list of known step names is fixed (`field.py:433-438`). |
| `nircam/mosaic_exp.py` | 375 | B | Works on drizzle weight changes and FITS WCS; reads `XPOSURE` for exposure time (`mosaic_exp.py:290`). |
| `nircam/expmap.py` | 811 | B | Stacks `S_REGION` polygons weighted by `XPOSURE` (`expmap.py:1-22`, `110-146`). |
| `nircam/orchestrate.py` | 1082 | B | The phase runner, skip logic, lazy step loading and parallel dispatch are generic (`orchestrate.py:188-201`, `320-348`, `733-767`, `983-1036`). The step lists are constants (`orchestrate.py:46-93`). Filename parsing assumes JWST: detector is the fourth `_` field (`orchestrate.py:96-113`), visit is the first (`orchestrate.py:146-152`); `S_REGION` is read from `hdul[1]` (`orchestrate.py:173-181`); `EXP_TYPE` gates grism exposures (`orchestrate.py:204-232`). |
| `nircam/status.py`, `common/cfp.py` | 96, 242 | A | `cfp` already supports one key set per instrument (`cfp.py:35-64`, `66`, `110`); an HST key set is an addition, not a change. |
| `common/io.py` (`atomic_save`) | 110 | A | Accepts either a datamodel or an astropy `HDUList` (`io.py:14-80`). |
| `common/parallel.py` (`dispatch`) | 148 | A | |
| `common/version.py` | 330 | A | |
| `nircam/rgb.py`, `trilogy.py` | 265, 178 | B | Reads split mosaics by the NIRCam name pattern (`rgb.py:50-63`). |

### 2.2 Astrometry

| Module | Lines | Class | What is JWST-specific now |
| --- | --- | --- | --- |
| `nircam/refcat/` (query, extract, merge, compare, motion, hsc, io) | 2079 | A | Catalog-level tools with one ECSV schema (`refcat/__init__.py:1-19`). `extract` builds a catalog from any mosaic through `astropy.wcs` (`refcat/extract.py:102`). This is the natural way to make an HST reference catalog from a CAMPFIRE NIRCam mosaic. |
| `nircam/align/histmatch.py` | 647 | A/B | The offset-histogram matcher subclasses `tweakwcs` `MatchCatalogs` and works on tangent-plane catalogs (`histmatch.py:191-200`). Defaults such as `slope_max = 10/2048` assume a 2048-pixel detector (`histmatch.py:295`). |
| `nircam/align/detect.py` | 239 | B | Source detection is generic; the AB zeropoint is derived from `BUNIT = MJy/sr` and `PIXAR_SR` (`detect.py:197-213`). |
| `nircam/align/footprint.py` | 208 | B | Default 2048x2048 frame and gwcs bounding boxes (`footprint.py:32-34`, `52-83`). |
| `nircam/align/solve.py` | 715 | B | The coarse pooled fit, the gated per-detector fine fit and the diagnostics are a method, not a JWST feature. The WCS wrapper is `tweakwcs` `JWSTWCSCorrector`, which needs `v2_ref`, `v3_ref`, `roll_ref` (`solve.py:65`, `96-101`, `267-287`). `tweakwcs` 0.8.12 as installed in the `campfire` environment also ships `FITSWCSCorrector`, whose docstring says it supports FITS WCS with SIP, `CPDIS` and `DET2IM` distortions, which is the HST representation (checked by importing it, not by running it on HST data). |
| `nircam/align/apply.py` | 554 | C (I/O) | Reads and writes gwcs through `ImageModel`, backs the original up as ASDF in a FITS extension (`apply.py:148-164`, `343-376`, `378-435`). |
| `nircam/align/dva.py` | 207 | C | Differential velocity aberration re-pivot in the JWST gwcs pipeline (`dva.py:71-101`, `183`). |
| `nircam/steps/jhat.py`, `steps/wcs_shift.py` | 308, 252 | C | JHAT; gwcs adjustment. |
| `nircam/association.py` | 313 | C/B | Groups detector files of one NIRCam exposure by filename token; the detector list is NIRCam's (`association.py:44-47`, `50-64`, `212`). The concept (one exposure, several chips, solved together) applies to the two ACS/WFC chips. |

### 2.3 Detector-level processing

| Module | Class | Note |
| --- | --- | --- |
| `steps/detector1.py`, `steps/image2.py`, `steps/_flat.py`, `nircam/prefetch.py` | C | `jwst` pipelines and JWST CRDS. The HST counterpart is `calacs` / `calwf3`, already run by the archive. |
| `steps/jackknife.py`, `steps/persistence.py`, `steps/wisp.py`, `nircam/wisp_cache.py`, `steps/diag_striping.py` | C | NIRCam detector and optics effects. |
| `steps/bkg.py`, `nircam/oneoverf.py`, `nircam/gp_striping.py` | C | Built around four 512-column amplifiers and 1/f noise (`steps/bkg.py:22-31`, `nircam/constants.py:29-60`). |
| `nircam/skyfit.py` | B | `fit_pedestal` / `fit_sky` are plain array functions (`skyfit.py:32-131`) and could serve a per-chip sky estimate. |
| `steps/edge.py` | B | Array logic is generic; I/O is `ImageModel` (`edge.py:33-36`). |
| `steps/apply_masks.py` | B | Region rasterization is generic (`apply_masks.py:38-66`); the WCS comes from `ImageModel` (`apply_masks.py:96-109`). The `CFMASK` idea (user mask stored beside the data, fused only into working copies) transfers directly. |
| `steps/preview.py` | B | S/N PNG from SCI and ERR; reads through `ImageModel` (`preview.py:89-94`). |
| `steps/bad_pixel.py` | B, low value | Needs many exposures per detector to be meaningful (`bad_pixel.py:5-11`). |
| `nircam/constants.py` | C | NIRCam filter lists and amplifier geometry. |

### 2.4 Shared infrastructure

| Module | Class | Note |
| --- | --- | --- |
| `common/query.py` (download) | C, with a reusable part | The JWST Search API calls are JWST-only (`query.py:30`). The CAOM TAP path (`query.py:31`, `383-389`, `481-566`) queries a catalog that also holds HST, but the instrument map, product filtering and output paths are written for JWST. |
| `config.py` | B | `get_nircam_step_config` reads `config['nircam']` (`config.py:366-379`); CRDS variables are process-global (see 3.8). |
| `layout/campfire_layout` | B | `Instrument` has two members, NIRSpec and NIRCam (`layout/campfire_layout/scope.py:22-24`); product kinds are declared per instrument with `nircam/<field>/<filter>` sub-directories (`layout/campfire_layout/products.py:94-99`, `179-262`); `raw_dir` and `reference_dir` branch on the instrument name (`layout/campfire_layout/paths.py:113-158`). |

### 2.5 Missing and needed for HST (class D)

| Need | Why |
| --- | --- |
| HST exposure discovery and download (FLT/FLC, optionally RAW/IMA and association tables) | Nothing in the repo queries or stores HST data. |
| An exposure adapter that presents an HST chip as (science array in a defined unit, variance, DQ mask, pixel-to-sky function, footprint, exposure time, identity) | Every shared routine above currently obtains these from a JWST `ImageModel`. |
| Multi-chip handling | ACS/WFC delivers two chips in one file **[HST]**. All existing code assumes one detector per file. |
| HST DQ-to-mask translation | Bit meanings differ from JWST's (see 3.6). |
| A variance model for HST exposures | The drizzle wants variance components; HST calibrated files carry one ERR array **[HST]**. |
| Unit conversion | Electrons or electrons per second per native pixel **[HST]** to whatever the CAMPFIRE mosaics use (MJy/sr today, `nircam/drizzle.py:237-240`). |
| WCS update path for HST | Writing an alignment solution back to FITS headers (the `stwcs` / headerlet mechanism **[HST]**), in place of the gwcs path in `align/apply.py`. |
| Cosmic-ray rejection that works with two to four exposures | The current outlier step relies on a median over the visit pool (see 3.10). |
| Per-exposure preprocessing specific to HST (for example WFC3/IR variable background, persistence masks, satellite trails, ACS amplifier offsets) | See `feasibility.md` section c. |
| Storage, database and web support for a second imaging instrument | See 1.7. |

## 3. Assumptions an HST module would violate

Each item is an assumption that appears in several modules, with representative references.

1. **JWST datamodels are the I/O layer.** Steps open `ImageModel`, use `model.meta.*`, and save through the datamodel (`nircam/drizzle.py:108`, `512`; `nircam/outlier_detect.py:131`; `nircam/align/apply.py:500`; ten step modules). HST calibrated files are plain multi-extension FITS **[HST]**.

2. **The WCS is a gwcs object stored as ASDF.** Pixel maps come from `input_gwcs(ix, iy)` and `output_wcs.invert(...)` (`nircam/drizzle.py:131-159`); alignment edits and backs up gwcs (`nircam/align/apply.py:148-164`). HST distortion lives in FITS headers as SIP polynomials plus lookup-table extensions, evaluated by `stwcs` **[HST]**.

3. **One detector per file, science in the first extension.** `hdul[1].header` is taken as the WCS or `S_REGION` source (`nircam/geometry.py:51`, `nircam/orchestrate.py:180`); `hdul['SCI']` is taken as the whole science array (`nircam/manifest.py:49-56`); the frame is 2048x2048 (`nircam/geometry.py:29`, `nircam/align/footprint.py:34`). ACS/WFC has two 4096x2048 chips per file **[HST]**; WFC3/IR is 1014x1014 **[HST]**.

4. **JWST filenames carry the structure.** `jw<PID><obs><visit>_<group>_<exp>_<detector>`: program from `basename[2:7]` (`nircam/steps/outlier.py:284-290`), visit from the first `_` field (`nircam/orchestrate.py:146-152`), detector from the fourth (`nircam/orchestrate.py:96-113`, `nircam/manifest.py:357-361`), exposure token by dropping the last field (`nircam/association.py:50-64`). Field definitions are validated against `^jw\d{5}` (`nircam/field.py:405-426`). HST root names are nine characters (`ipppssoot`) with a different encoding **[HST]**.

5. **JWST header keywords.** `S_REGION` (`nircam/geometry.py:97-131`, `nircam/expmap.py:110-146`, `nircam/steps/outlier.py:293-309`), `EXP_TYPE` (`nircam/orchestrate.py:204-232`), `XPOSURE` (`nircam/mosaic_exp.py:290`), `PIXAR_SR` and `BUNIT` (`nircam/align/detect.py:197-213`), `V2_REF` / `V3_REF` / `ROLL_REF` (`nircam/align/apply.py:353-356`). `DATE-OBS` is the one keyword used here that HST also has in the primary header **[HST]** (`nircam/field.py:83-108`). Whether HST files carry any footprint keyword comparable to `S_REGION` was not established; none appears in the keyword tables read (*inferred* absent).

6. **DQ semantics.** Good pixels are selected with `good_bits = '~DO_NOT_USE'` (`config_default.toml:947`, `nircam/drizzle.py:592`), `DO_NOT_USE` is bit value 1 (`nircam/field.py:111-114`), and outliers are written as the JWST `OUTLIER` bit (`nircam/outlier_detect.py:186`). HST DQ arrays are 16-bit with instrument-specific meanings **[HST]**.

7. **Units are surface brightness.** Inputs and mosaics are MJy/sr (`nircam/drizzle.py:237-240`), which does not change when the pixel size changes; only `PIXAR_*` is recomputed (`nircam/drizzle.py:232-235`). The drizzle is called with `in_units='cps'` and the default `scale=1.0` (`nircam/drizzle.py:567-577`). HST calibrated exposures are in electrons (ACS) or electrons per second (WFC3/IR) per native pixel, without the pixel-area correction **[HST]**, so either the inputs are converted to surface brightness first or the drizzle call must apply the pixel-scale flux factor.

8. **One CRDS observatory per process.** `CRDS_SERVER_URL` and `CRDS_CONTEXT` are set globally for JWST (`config.py:115-147`, `config_default.toml:10-12`), and `CRDS_CONTEXT` is the pipeline's calibration version by policy (`AGENTS.md`, "Configuration"). HST uses a separate CRDS server and context series **[HST]**. An HST module that needs reference files (for example to re-run `calwf3`) needs its own context key and must not inherit the JWST variables.

9. **Variance comes as three components.** `var_rnoise`, `var_poisson`, `var_flat` are read and sanitized separately (`nircam/drizzle.py:452-490`, `544-549`); the weight is inverse read-noise variance (`weight_type = 'ivm'`, `nircam/drizzle.py:534-540`). HST provides a single ERR array per chip **[HST]**.

10. **Outlier rejection assumes a usable median.** The step drizzles each input of a visit pool and compares it with the pool median (`nircam/outlier_detect.py:152-180`, `207-212`). With two to four exposures per pointing, typical for HST survey imaging **[HST]**, a plain median keeps cosmic rays that coincide in two frames; AstroDrizzle uses a "minmed" combination for that case **[HST]**. The pool is also scoped by JWST program and visit tokens (item 4).

11. **Filter name is the only spectral axis.** Directories, mosaic names and storage keys carry the filter but not the instrument (`nircam/field.py:585`, `nircam/manifest.py:185`, `layout/campfire_layout/products.py:94-95`), and filters are checked against NIRCam lists (`steps/image2.py:54`, `nircam/constants.py:11-22`). ACS/WFC and WFC3/IR filter names do not collide with the NIRCam filters used in the current fields, but `f164n` exists on both NIRCam (`nircam/constants.py:12`) and WFC3/IR **[HST, inferred]**, and ACS/WFC and WFC3/UVIS share several names (F606W, F814W) **[HST, inferred]**. An instrument or detector axis is needed in names and keys.

12. **Tuning is in NIRCam pixels.** Background mask scales (`steps/bkg.py:80-86`), `psf_fwhm_by_filter` for alignment (`config_default.toml:856`), and the outlier grow thresholds (`config_default.toml:927-934`) are set for 30 to 60 mas pixels and NIRCam PSFs.

## 4. Invocation and versioning of an HST module

**Invocation.** The CLI already has the right shape: a third subgroup, `cfpipe hst ...`, mounted in `pipeline/campfire_pipeline/cli.py` next to the two existing ones (`cli.py:56-60`). It would take the same `--field`, `--filters`, `--tiles`, `--epoch`, `-p`, `--overwrite` arguments, because fields, tiles and epochs are the shared concepts. Whether HST exposures are declared inside the existing field table in `fields.toml` or in a parallel table is a design choice for Stage 3; the constraint is that `Field.load` currently rejects any glob that does not start with `jw` (`nircam/field.py:405-412`) and treats every unknown sub-table with `crpix`/`naxis` as a tile (`nircam/field.py:444-453`).

**Packaging.** Same distribution, `campfire-pipeline`, with the HST-only dependencies behind an optional extra and imported lazily, as the NIRCam step modules already do for heavy imports (`nircam/orchestrate.py:31-40`). The pipeline pins `jwst>=1.20,<1.21` (`pipeline/pyproject.toml:8`). The `campfire` environment currently has `jwst 1.20.2`, `drizzle 2.1.1`, `tweakwcs 0.8.12`, `numpy 2.2.0`, Python 3.13, and already contains `stwcs 1.7.7` (pulled in by `stsci.skypac`); `drizzlepac`, `acstools`, `wfc3tools` and `grizli` are not installed (checked with `importlib.metadata` and `pip show`, read-only).

**Versioning.** One version for the whole pipeline package, from `pipeline-vX.Y.Z` tags, stamped as `CMPFRVER` (`common/version.py:1-49`). HST products would carry the same string. The bump policy in `AGENTS.md` maps directly: a change of HST reference-file context or of an upstream HST package that alters pixel values is a Calibration entry and a MINOR bump, in the same way as `CRDS_CONTEXT` today. A consequence to be aware of: a release made for an HST-only reason also changes the version string that NIRCam and NIRSpec products are stamped with from then on.

**Tests and what protects a refactor.** (Test inventory by the same sub-agent; *verified by delegate*.) `pipeline/tests/` holds about 45 NIRCam-related test files. They are unit tests and synthetic-data tests of helpers and contracts. The modules that would be generalized map to tests as follows:

| Module | Tests |
| --- | --- |
| `nircam/drizzle.py` | `test_nircam_drizzle.py` (variance sanitization, bounding box, partial overlap, output metadata, context compression; drives the real `Drizzle` and `ImageModel`), `test_nircam_mosaic_exp.py`, `test_nircam_resample_write_context.py` |
| `nircam/field.py` | `test_field_fiducial.py`, `test_nircam_work_tree.py`, `test_tile_filter.py`, `test_nircam_exclusions.py` and others |
| `nircam/manifest.py` | `test_manifest_wcs_identity.py`, `test_nircam_manifest_hash.py`, `test_nircam_mosaic_naming.py`, `test_nircam_resample_bkgsub_stamp.py` |
| `nircam/geometry.py` | `test_tile_filter.py` |
| `nircam/steps/resample.py` | `test_nircam_resample_tiles.py` (CLI wiring), `test_nircam_resample_bkgsub_stamp.py` |
| `nircam/steps/outlier.py` | `test_nircam_outlier_grow.py` (region growing only) |
| `nircam/align/*` | ten `test_align_*.py` files, including an end-to-end solve on a synthetic field |
| `nircam/refcat/*` | `test_refcat_*.py` |

Gaps that matter for Stage 3:

- There is no end-to-end or golden-pixel test of a mosaic. Nothing runs `resample_step` or `drizzle_tile` on a fixed input set and compares output pixels.
- Nothing imports `nircam/outlier_detect.py`.
- In CI the pipeline test job is informational (`continue-on-error: true`) and not part of the required checks (`.github/workflows/python.yml:84-117`).

So "NIRCam behavior does not change" cannot be demonstrated with the current suite alone. A refactor of the shared mosaic machinery needs a before-and-after pixel comparison on real data as its acceptance test, and ideally a small golden-mosaic test added first.
