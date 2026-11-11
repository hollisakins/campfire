# HST imaging module for CAMPFIRE: design proposal (Stage 3)

Date: 2026-11-11. Follows `feasibility.md` (Stage 1) and `nircam-fit.md` (Stage 2). Nothing here is implemented.

## 0. Decisions this design is built on

Confirmed at the checkpoint:

1. **Hybrid approach.** STScI tools for what is specific to HST detectors; CAMPFIRE's own alignment and mosaic machinery, generalized from the NIRCam pipeline, for everything from astrometry onward.
2. **Start from RAW**, as the NIRCam pipeline starts from `uncal`. Archive FLT/FLC ingest stays available as a second mode (section 2.2 explains why both).
3. **30 mas for everything by default**, as a configuration value. Coarser products are made by block-downsampling the 30 mas mosaic, a path that would also serve MIRI later.

Terms used below. HST file suffixes: `RAW` is the uncalibrated exposure (the counterpart of JWST `uncal`); `IMA` exists for WFC3/IR only and holds every non-destructive read after per-read calibration, before the ramp fit; `FLT` is the calibrated single exposure (flat-fielded, not resampled; electrons for ACS, electrons per second for WFC3/IR); `FLC` is an ACS FLT with the charge-transfer correction applied; `DRZ`/`DRC` are drizzled mosaics. `calacs` and `calwf3` are the C calibration programs in the `hstcal` package.

Evidence tags as in the other two documents: statements about upstream tools rest on `feasibility.md`; anything new and unverified here is marked *inferred* or *to be measured*.

## 1. Package layout

```
pipeline/campfire_pipeline/
  common/          unchanged (cfp, io, parallel, version, ...)
  imaging/         NEW. Instrument-neutral mosaic machinery, moved out of nircam/
    exposure.py      the exposure adapter interface (section 3.2)
    grid.py          tile grids from fields.toml; output WCS builders
    field.py         FieldBase: tiles, epochs, skip lists, workspace, working copies
    geometry.py      footprint overlap
    drizzle.py       drizzle_tile, drizzle_tile_singles
    outlier.py       single-drizzle, median / minmed, blot, flag
    manifest.py      input hashing, staleness, mosaic naming
    mosaic_io.py     mosaic writers (plain FITS; JWST datamodel)
    mosaic_exp.py    exposure-time and count maps
    bkgsub.py        mosaic background subtraction
    downsample.py    NEW. block-binned derived mosaics
    align/           detect, footprint, histmatch, solve
    refcat/          reference catalogs
  nircam/          JWST-specific: steps/, JwstExposure adapter, NIRCam field and
                   step lists, wisp, 1/f, DVA, jhat. Old import paths kept as
                   re-exports.
  hst/             NEW
    cli.py           `cfpipe hst ...`
    orchestrate.py   step lists and runners (same pattern as nircam/orchestrate.py)
    field.py         HstField: exposure selection for a field
    instruments.py   per-detector constants: chips, native scale, DQ policy, filters
    exposure.py      HstExposure adapter (ACS/WFC chip, WFC3/IR)
    query.py         MAST discovery and download
    steps/
      calibrate.py     bestrefs + calacs / calwf3; WFC3/IR ramp repair
      wcs_init.py      distortion model into headers (stwcs)
      quality.py       guiding and smear gate
      masks.py         satellite trails, persistence, extra bad pixels
      sky.py           ACS amplifier offsets; per-chip sky
      crreject.py      per-visit cosmic-ray flagging; single-exposure fallback
      align.py         thin wrapper over imaging/align with FITS-WCS write-back
      preview.py       S/N previews
```

`hst/` sits beside `nircam/` and `nirspec/`. Both imaging instruments import from `imaging/`; neither imports from the other.

Dependencies go in an optional extra, `campfire-pipeline[hst]`: `drizzlepac`, `stwcs`, `acstools`, `wfc3tools`, `astroscrappy`. `hstcal` (calacs, calwf3) is not on PyPI and comes from conda-forge, so `install.py` gets an `hst` component that installs it into the conda environment. All HST imports are lazy, so a NIRCam-only install is unaffected.

## 2. Processing plan

### 2.1 Overview

Same two-phase shape as NIRCam: a per-exposure **process** phase that produces one canonical file per exposure with step stamps (`CFP_*`), and a **combine** phase that works on disposable copies and writes mosaics.

| # | Step | Granularity | Input → output | Upstream tool |
| --- | --- | --- | --- | --- |
| 0 | `download` | field | MAST → `raw/hst/<detector>/<proposal>/<filter>/<rootname>_raw.fits`, association and jitter files, `manifest.ecsv` | MAST CAOM TAP (the service `common/query.py` already uses) |
| 1 | `calibrate` | exposure | RAW → canonical `<rootname>_flc.fits` (ACS) or `<rootname>_flt.fits` (WFC3/IR) | `crds bestrefs` with a pinned HST context; `calacs` / `calwf3` through `acstools` / `wfc3tools`; ramp repair after WFC3 ISR 2016-16 |
| 2 | `wcs_init` | exposure | distortion model written into the headers, no catalog solution | `stwcs.updatewcs(use_db=False)` |
| 3 | `quality` | exposure | pass / flag for review | header and jitter keywords; PSF-width check |
| 4 | `masks` | exposure | DQ bits and mask extensions | `acstools.findsat_mrt`; MAST persistence models; own code |
| 5 | `sky` | exposure, per chip | sky level and ACS quadrant offsets recorded (and offsets removed) | own code, following ACS DHB §4.2 and Koekemoer et al. 2011 |
| 6 | `align` | visit pool, then exposure | corrected WCS in the headers | `imaging/align` on `tweakwcs.FITSWCSCorrector`; `stwcs` for WCS bookkeeping |
| 7 | `crreject` | visit and filter | cosmic-ray DQ flags | AstroDrizzle steps 3 to 6 only (`drizzlepac`); `astroscrappy` for lone exposures |
| 8 | `preview` | exposure | S/N PNGs | own code |
| 9 | `apply_mask` | exposure | user region masks → `CFMASK` | shared |
| 10 | `outlier` | overlap group | second-pass flags across visits | `imaging/outlier` (`drizzle` package) |
| 11 | `resample` | tile | mosaic `_i2d`, split files, exposure maps, manifest | `imaging/drizzle` (`drizzle` package) |
| 12 | `downsample` | mosaic, optional | block-binned mosaics | own code |

### 2.2 Step notes

**0. Download.** HST exposures are selected by sky position, not by program list, because one field holds dozens of programs. `HstField` resolves the selection from the field's tile footprints plus per-field include and exclude lists (programs, root names, date ranges) and writes the resolved list to the per-proposal manifest, so a later run is reproducible without re-querying MAST. Imaging only: grism, ramp-filter, subarray, moving-target and scan-mode observations are excluded at query time, and again by a header check before `calibrate` (the same two-gate pattern as NIRCam grism exposures, `nircam/orchestrate.py:204-232`). DASH exposures are excluded in the first version (section 9).

**1. Calibrate.** Two modes, set per detector in configuration:

- `source = "raw"` (default). Reference files are selected with `crds bestrefs --new-context <hst context>` and the calibration runs locally at a pinned `hstcal` version. This gives HST the same property the JWST side has: pixel values are a function of the input file, the pipeline version and one context string, not of the date MAST last reprocessed the file. The HST context becomes a second line in `config_default.toml` with the same policy as `CRDS_CONTEXT` (a change is a Calibration entry and a MINOR release).
- `source = "archive"`. The MAST FLC/FLT is copied in as the canonical file and stamped with its `CAL_VER`, reference-file keywords and retrieval date. This mode exists for the first proof-of-concept phase (it removes `hstcal` and the reference cache from the critical path) and as a fallback where `hstcal` cannot be installed.

Why RAW is worth it for HST even though we override no ACS calibration step today: for WFC3/IR the ramp fit must be redone for exposures with time-variable background and for reads crossed by a satellite, and both need the per-read data; for ACS the monthly dark and bias deliveries and the CTE model generation make archive files depend on retrieval date (`feasibility.md` sections a.2 and c.2). Cost: the ACS pixel-based CTE correction is the slowest step in the whole pipeline (*to be measured*), and the HST reference cache for a field that spans many years of ACS anneals is large (*to be measured*).

WFC3/IR ramp repair: after the per-read stage, measure the median background of each read difference from the IMA. If it varies beyond a threshold, subtract the per-read excess and refit ("flatten ramp"); reads listed in a per-exposure exclusion file (satellite, Earth-limb) are dropped before the fit. The decision and its numbers are written to the step stamp. The IMA is deleted after the FLT is made unless configured otherwise.

The JWST CRDS variables stay as they are. The HST server URL, context and the `iref`/`jref` paths are passed in the environment of the `bestrefs`, `calacs` and `calwf3` subprocesses only, never exported into the pipeline process (`nircam-fit.md` section 3.8).

**2. WCS initialization.** `updatewcs` with the astrometry database switched off gives every exposure the plain distortion-model WCS from the pinned reference files. Archive catalog fits are not used: they differ in quality from exposure to exposure and would make our starting point depend on MAST state. The price is a larger initial error (1 to 2 arcsec for pre-2005 data), which the coarse stage of `align` is designed for; a per-field manual-shift list, like `[[<field>.wcs_shift]]` for NIRCam, covers visits beyond its search radius.

**3. Quality gate.** Flags exposures with `EXPFLAG` other than `NORMAL`, failed-guiding jitter keywords, or (for data after June 2024) a stellar PSF broader than its visit-mates. Flagged exposures go to the existing reviewer-exclusion mechanism (`reference/<field>/exposures.json`), not silently dropped.

**4. Masks.** ACS: satellite-trail candidates from `findsat_mrt` are written as a mask extension and shown in the preview for confirmation; confirmed trails go to DQ. WFC3/IR: persistence mask from the MAST persistence model where one exists, thresholded against the exposure's noise and grown by a few pixels; an in-visit self-persistence mask built from the positions of saturated sources in earlier exposures of the same visit, for use when no model exists (*new code, to be validated*); the extra bad-pixel masks used by grizli. Blob pixels (DQ 512) are treated as bad unless the exposure's flat includes the blob correction (six broad filters, post-2020 reference files), set per filter in `instruments.py`.

**5. Sky.** ACS: fit and remove the per-quadrant offsets on source-masked data (the one sky operation that changes canonical pixels, because it is a detector effect), then measure one sky value per chip. WFC3/IR: one sky value per exposure. The sky value is recorded in the header and subtracted when the adapter hands data to the mosaic code; the canonical pixels keep their sky so that the cosmic-ray noise model sees true counts. The source mask comes from `SubtractBackground.mask_from_arrays`, as in the NIRCam `bkg` step. In cluster fields a constant per-chip sky still contains intracluster light; that is left for the mosaic-level background step, which is configurable per field and already has a guard against over-subtraction.

**6. Align.** The method is the NIRCam one (`nircam/align/solve.py:1-58`): detect sources, clip the reference catalog to the footprint, find the gross offset with the offset-histogram matcher, fit a pooled shift and rotation, then a gated finer fit.

- *Pool.* All chips of all exposures of one visit and filter, because within a visit HST exposures share guide stars and are rigid to a few mas, while a single short exposure in a sparse field has too few sources to stand alone. The fine fit is per exposure, shift only, accepted by the same significance test as for NIRCam detectors.
- *Reference.* The field's NIRCam reference catalog (the one its NIRCam `align` uses), or a catalog extracted from the CAMPFIRE NIRCam mosaic with `refcat extract`. HST is then tied to the NIRCam frame directly. Where HST extends beyond NIRCam, the catalog is extended with `refcat query` and `merge`. Stars need care across a 10 to 20 year baseline: Gaia stars are propagated to the exposure epoch (`refcat/motion.py`), and stars without proper motions are down-weighted or dropped in favour of galaxies.
- *Cosmic rays in the detection image.* ACS exposures are full of cosmic rays before step 7. Detection runs on a copy cleaned with L.A.Cosmic (`astroscrappy`); that mask is used for detection only and never written to the science DQ.
- *Write-back.* The corrected WCS is written into each `SCI` header with a CAMPFIRE `WCSNAME` that includes the reference-catalog hash; the pre-alignment WCS is archived as an alternate WCS in the same file (`stwcs`), which plays the role `ALGN_BAK` plays for NIRCam. `CFP_ALGN` has the same format, and exposures that fail are quarantined from the combine phase exactly as for NIRCam (`nircam/field.py:878-925`).

**7. Cosmic rays.** Per visit and filter, on working data with the aligned WCS: AstroDrizzle with only the separate-drizzle, median (`minmed`), blot and `driz_cr` steps enabled, user-supplied sky, and the survey-standard thresholds (ACS SNR `3.5 3.0`, scale `1.2 0.7`; WFC3/IR looser, as grizli uses, because the ramp fit has already removed most hits). For ACS the flags are grown along the CTE trail direction (`driz_cr_ctegrow`). Flags are written to the canonical DQ as bit 4096. A filter with a single exposure in a visit and no overlap gets L.A.Cosmic flags (ACS only), marked as such in the stamp.

**10. Second-pass outliers.** Once all visits are on one frame, the shared outlier step runs over each overlap group (all exposures of a filter that overlap, any program), as CANDELS did across epochs. This uses our own single-drizzle and median code, extended with `minmed` for pixels with two or three contributors, and a generic blot from the `drizzle` package. It removes what a two-exposure visit could not: pixels hit in both frames, residual trails, persistence that moves with the dither.

**11. Resample.** The shared tile drizzle, section 4.

### 2.3 Canonical exposure files

The canonical file keeps the native HST layout and name (`<rootname>_flc.fits`, `<rootname>_flt.fits`), because `stwcs`, `drizzlepac` and `acstools` all operate on that structure. Both ACS chips stay in one file; the adapter presents them as two chips. Files live in `products/hst/<field>/<detector>-<filter>/` (for example `acswfc-f814w`, `wfc3ir-f160w`), so the detector is part of every path and key.

Step stamps use a new HST key set in `common/cfp.py`, which already supports one per instrument (`common/cfp.py:35-64`).

## 3. Generalizing the NIRCam components

### 3.1 What moves

From `nircam-fit.md` section 2: `drizzle.py`, `outlier_detect.py`, the generic half of `steps/outlier.py` and `steps/resample.py`, `manifest.py`, `geometry.py`, `mosaic_exp.py`, `bkgsub.py`, the tile, epoch and working-copy logic of `field.py`, `align/{detect,footprint,histmatch,solve}.py`, and all of `refcat/`. They move to `imaging/`. What stays in `nircam/`: every step that calls `jwst`, the NIRCam detector corrections, `align/apply.py` and `align/dva.py`, and `association.py`.

### 3.2 Shared interfaces

**Exposure adapter.** The single seam between an instrument and the mosaic code. One object per chip:

```python
class Chip(Protocol):
    key: str            # stable identity, e.g. "jw01727028001_04101_00003_nrcalong" or "jbhj03xxq:2"
    path: str
    instrument: str     # "nircam", "acswfc", "wfc3ir"
    filter: str
    visit: str          # grouping token for per-visit steps
    program: str
    date_obs: date
    exptime: float      # seconds, for exposure maps
    shape: tuple[int, int]
    footprint: Polygon  # sky polygon of the chip

    def load(self) -> ChipArrays: ...
    # ChipArrays: sci (surface brightness, sky-subtracted), var_rnoise,
    # var_poisson, var_flat, good (bool), pixel_area_sr

    def pixel_to_world(self, x, y) -> tuple[ndarray, ndarray]: ...
    def provenance(self) -> dict: ...          # one row of the mosaic's input table
    def flag(self, mask, bit_name) -> None: ... # write outlier flags back
    def identity(self) -> dict: ...            # pixel hash and WCS hash for manifests
```

`open_exposure(path) -> list[Chip]` is registered per instrument. `JwstChip` wraps today's `ImageModel` code path unchanged. `HstChip` does the HST-specific translation:

- *Units.* Rate in e-/s (ACS counts divided by exposure time) times the exposure's own `PHOTFNU`, divided by the reference pixel solid angle, gives MJy/sr. Using each exposure's zeropoint handles the time-dependent sensitivity. Because FLT/FLC are flat-fielded to make uniform sky flat, this is a surface brightness in the same sense as a NIRCam `cal` image, and the drizzle is then called exactly as for NIRCam (`in_units='cps'`, `scale=1`). The reference solid angle each detector's flat is normalized to is *to be confirmed* and is tested directly by the photometric comparison in section 6.
- *Variance.* HST gives one `ERR` array that includes source noise. The adapter splits it: `var_poisson` is the source-only term (sky-subtracted signal over effective exposure time; the WFC3/IR `TIME` array gives the per-pixel time), `var_rnoise` is the remainder (sky, dark and read noise), `var_flat` is zero. The background term is then rescaled to the measured sky scatter, as the NIRCam `bkg` step does for `VAR_RNOISE`. With the existing `weight_type = 'ivm'` the mosaic weight is therefore inverse background variance, the convention of the CANDELS-lineage mosaics, and the mosaic `ERR` includes source noise, as for NIRCam.
- *Good-pixel mask.* From DQ through a per-detector table in `hst/instruments.py` (which bits are bad, which are tolerated), plus the mask extensions. The shared code never sees instrument DQ bit values.

**Grid.** `TileGrid(crval, crpix, shape, rotation, pixel_scale)` comes from `fields.toml` through the logic now in `Field.get_tile_wcs`. It offers two realizations of the same tangent plane: the gwcs object built by `stcal` (what NIRCam uses today) and a plain FITS TAN `astropy.wcs.WCS`. A test asserts they agree to better than 1e-4 pixel over a full production tile.

**Mosaic writer.** NIRCam keeps writing through the JWST datamodel. HST mosaics are written by a plain FITS writer producing the same extension layout (`SCI`, `ERR`, `WHT`, `CON`, `EXPTIME`, `NEXP`) and the same primary keywords the downstream steps read (`CMPFRVER`, `CMPFRTIM`, `CFP_BKGS`, `BUNIT`, `PIXAR_SR`, `PIXAR_A2`), with a per-input table in place of the JWST `HDRTAB`.

**Field.** `FieldBase` holds tiles, epochs, skip lists, the workspace and the working-copy logic. `NircamField` adds the `jw` glob rules and raw layout; `HstField` adds sky-based selection. Both read the same `fields.toml` table, so HST and NIRCam mosaics of a field share one tile definition by construction. HST settings live in a `[<field>.hst]` sub-table (filters per detector, selection lists, step overrides); `Field.load` has to learn to treat `hst` as a reserved key (`nircam/field.py:445-453`).

### 3.3 Order of refactoring, and how we know NIRCam did not change

Every step below is its own PR with a PATCH-level Infrastructure changelog entry.

- **R0. Baselines first.** (a) A small synthetic golden test in the repo: a handful of mock `ImageModel` exposures with mock gwcs (the helper in `pipeline/tests/_align_gwcs.py` already builds these), drizzled and outlier-flagged, with array hashes pinned. It runs in CI. (b) A real-data baseline on the reduction machine: one small tile (for example `rj0911` `venus`, two filters, one SW and one LW), `combine` run at the current commit with each backend, and SHA-256 of every output array (`SCI`, `ERR`, `WHT`, `EXPTIME`, `NEXP`, each working copy's `DQ`) and of the manifests with timestamps removed.
- **R1. Pure moves.** Files move to `imaging/` with re-exporting stubs left in `nircam/`. No logic change.
- **R2. Adapter seam.** `drizzle.py` and `outlier.py` take `Chip` objects; `JwstChip.load()` contains the current body of `_prepare_drizzle_input` verbatim.
- **R3. Remove hard-coded JWST structure** behind adapter attributes with NIRCam values as defaults: `hdul[1]`, the 2048 frame, filename parsing for visit, detector and program, the mosaic name template.
- **R4. Field split** into `FieldBase` and `NircamField`.
- **R5. Additions used only by HST**: FITS TAN grid, plain FITS writer, `minmed`, generic blot.

Acceptance for R1 to R4: the full test suite passes without edits to test logic; the synthetic golden hashes are unchanged; the real-data baseline is **bit-identical** in every array, with header differences limited to `CMPFRTIM` and `CMPFRVER`. No tolerance is needed because R1 to R4 change no arithmetic and no ordering. If a step cannot meet that, it is split until it can, or the difference is reported before merging. R5 does not touch the NIRCam path.

Two things to settle before R0: which production fields run `implementation = "campfire"` for outlier and resample (the default is `"jwst"`, `config_default.toml:945`, `1013`), since the HST module can only use the native backend and the baseline must cover whatever production uses; and whether the pipeline CI job should become a required check once the golden test exists.

## 4. Output grids

**Same grid as NIRCam.** `cfpipe hst resample --field X` asks the same `TileGrid` for the same tile at the same scale label as NIRCam does. Tangent point, rotation, `crpix` and `naxis` are the same numbers, so pixel (i, j) of an HST mosaic and of the NIRCam mosaic of that tile are the same patch of sky.

**Default scale: 30 mas for ACS/WFC and WFC3/IR**, read from `[hst.resample].pixel_scale`, overridable per detector and per field. Consequences, with numbers from the Fruchter and Hook formula (my arithmetic):

| Detector | Native | Output | `pixfrac` | Correlated-noise factor R |
| --- | --- | --- | --- | --- |
| ACS/WFC | 50 mas | 30 mas | 0.8 | 1.8 |
| WFC3/IR | 128 mas | 30 mas | 1.0 | 4.6 |
| WFC3/IR | 128 mas | 30 mas | 0.8 | 3.8 |

R is the ratio between the true noise on large scales and the pixel-to-pixel scatter. At 30 mas a WFC3/IR mosaic looks about four times smoother than it is. This does not lose information, but every noise estimate made from pixel scatter must be corrected, and the `ERR` map must be validated against empty-aperture measurements (section 6). The measured factor is written to the mosaic header.

`pixfrac` and kernel are configuration; starting values are 0.8 (ACS) and 1.0 (WFC3/IR, where two-exposure coverage is common), square kernel, to be tuned on the proof of concept by PSF width and weight uniformity.

**Downsampled products.** `downsample` makes an N x N block-binned mosaic from the 30 mas one: weighted mean for `SCI`, sum for `WHT` and `EXPTIME`, and `ERR` propagated with the measured correlation factor. With N = 2 or 4 this gives 60 or 120 mas products whose pixels are exact unions of 30 mas pixels, which is the grid `Field.get_tile_wcs` derives when a scale is not declared (`nircam/field.py:975-981`). This is the route for WFC3/IR users who prefer near-native sampling, and for a future MIRI.

One inconsistency to be aware of: the COSMOS tiles declare explicit 60 mas grids with `crpix60 = crpix30 / 2`, which is offset by 15 mas from the block-aligned grid (`nircam-fit.md` section 1.5). Block-binned products cannot land on those explicit grids. I propose block alignment as the rule for derived products and leaving the existing explicit NIRCam 60 mas tiles as they are.

**Sparse coverage.** HST often covers a small part of a NIRCam tile (the Abell 2744 tile is 56759 x 22171 pixels; the Frontier Fields core is a few arcminutes). The mosaic keeps the full tile shape so the grids stay identical, and is mostly NaN. Split files are already gzip-compressed for upload. If local disk becomes a problem, the alternative is HST sub-tiles defined as integer-pixel crops of a NIRCam tile, which keeps the pixel grid; I would not add that until it is needed.

## 5. Products and metadata

**Names.** `mosaic_<instrument>_<filter>_<field>_<scale>_<tile>[_<epoch>]`, with instrument tokens `acswfc` and `wfc3ir` (NIRCam stays `nircam`). This is the existing template with the instrument made a variable (`nircam/manifest.py:185`). WFC3/UVIS would later be `wfc3uvis`, which keeps its F606W and F814W apart from ACS.

**Mosaic headers.** `BUNIT = MJy/sr`, `PIXAR_SR`, `PIXAR_A2`; `TELESCOP`, `INSTRUME`, `DETECTOR`, `FILTER`; `CMPFRVER`, `CMPFRTIM`; calibration provenance (`hstcal` version, HST CRDS context, or `archive` plus retrieval date); versions of `drizzlepac`, `stwcs`, `drizzle`; reference-catalog hash; `pixfrac`, kernel, weight type; the measured correlation factor. The per-input table records for each chip: root name, proposal, date, exposure time, `PHOTFNU` used, sky value, WCS name, `CAL_VER`.

**Manifest.** Same JSON as NIRCam. The input identity must cover both chips of an ACS file and the full HST WCS (linear terms, SIP, and the lookup-table extensions or at least `WCSNAME`), which the current hash recipes do not (`nircam/manifest.py:49-75`).

**Versioning.** One pipeline version. The HST CRDS context joins `CRDS_CONTEXT` under the Calibration → MINOR rule; `hstcal` and `drizzlepac` versions are pinned in the extra and treated like the `jwst` pin.

**Changes outside the pipeline package, flagged and not designed here** (details in `nircam-fit.md` section 1.7):

- *Layout contract.* New `Instrument` member(s), product kinds for HST exposures, previews and mosaics, parser branches, golden-fixture rows, and the TypeScript mirror.
- *Database.* The `storage_objects` instrument and product-type CHECK constraints need a migration. `nircam_images` has no instrument column and its unique key is (field, tile, filter, pixel scale, extension, epoch); either it gains an instrument column or a generalized table replaces it, with the same change in its data-release twin. `nircam_exposures` assumes one detector per file. `deployments` records `jwst_version` and `crds_context` only. `fields` records `jwst_program_ids` only. Access rules need no change: published imaging is already public.
- *Deploy client.* Discovery by `jw*.fits` and the `mosaic_nircam_` name check; exposure-identity hashing by extension name.
- *Web.* A filter-metadata table (pivot wavelength, instrument) to replace wavelength-from-name logic in the FitsGL build and the field page; routes and components named `nircam`. HST mosaics at 30 mas in MJy/sr on the same tiles can join a field's existing FitsGL dataset as additional bands, which is the main practical argument for the 30 mas default.

These deserve their own design document once the pipeline side has produced real mosaics.

## 6. Validation plan

**Datasets.**

1. `pg004` cluster core (proof of concept): 12 ACS/WFC and 16 WFC3/IR exposures from one 2016 program, seven filters, dense Gaia, existing CAMPFIRE NIRCam mosaic. Reference: RELICS 30 and 60 mas mosaics (WISE-based frame; fit and remove the offset first).
2. One CANDELS WFC3 tile in EGS with its overlapping ACS (both 2004-05 and post-2009 data, several programs, few Gaia stars). Reference: the CEERS HST release v1.9 (Gaia-tied, 30 mas, science, weight and rms maps).
3. Abell 2744 as the first scale test. References: BUFFALO v1.0 and the UNCOVER/DJA mosaics.
4. For COSMOS, the externally made HST mosaics already on the CAMPFIRE tile grid (`scripts/cosmos_inspec.py:52-55`), if available, and the IRSA ACS v2.0 mosaic.

**Metrics and targets.** Targets are starting values to be revised after the proof of concept.

| Area | Measurement | Target |
| --- | --- | --- |
| Astrometry, absolute | Offsets of compact sources between the HST mosaic and the NIRCam mosaic of the same tile (same pixel grid, so measured directly); against Gaia with proper motions where stars exist | median below 5 mas per axis; scatter below 15 mas per axis for bright sources (CEERS reports 12 to 15 mas HST to NIRCam) |
| Astrometry, internal | Per-exposure residuals from the align diagnostics; residual vector map across the chip (tests distortion and the ACS skew) | no coherent pattern above 5 mas; stellar FWHM in the mosaic within 5% of the reference mosaic |
| Photometry | Aperture and total fluxes, ours over reference, against magnitude, for isolated sources; zeropoint check on stars; ACS F814W against NIRCam F090W stellar locus | median ratio within 2% for 20 < m < 25; no trend with position on the chip (tests the pixel-area and unit handling) |
| Combination | Pixel-level difference between our drizzle and AstroDrizzle run on the same inputs, grid and weights (proof of concept only) | differences consistent with interpolation of the pixel map; no flux scale offset |
| Noise | Empty-aperture scatter against the `ERR`-map prediction, as a function of aperture size; weight against exposure time; depth (5 sigma) against the reference at matched aperture | ratio constant with aperture after the correlation factor; depth within 0.1 mag of the reference for the same exposures |
| Artifacts | Unmatched compact detections per arcmin^2 (in ours, absent from the reference and from NIRCam); count of residual trails and persistence images by inspection of previews; fraction of pixels masked per exposure | residual cosmic-ray density no higher than the reference; every masked trail confirmed |
| Calibration modes | Mosaic from `source = "raw"` against `source = "archive"` for the same exposures | differences explained by reference-file vintage and ramp repair |
| NIRCam regression | Section 3.3 | bit-identical |

## 7. Phased implementation

Sizes are rough: S is about one working session, M two to three, L four or more.

| Phase | Content | Size | Exit criterion |
| --- | --- | --- | --- |
| P0 | Environment and baselines: install `drizzlepac`, `acstools`, `wfc3tools`, `hstcal` next to the pipeline in a throwaway environment on the reduction machine; time `calacs` on one exposure; NIRCam golden test and real-data baseline (R0) | S-M | install works or a blocking conflict is documented; baselines stored |
| P1 | Refactor R1 to R4 | M-L | NIRCam bit-identical |
| P2 | First light on `pg004`, archive mode: download, `HstChip` (units, variance, DQ), `wcs_init`, FITS grid and writer, drizzle one ACS and one WFC3/IR filter onto the `venus` tile using the archive's own WCS and cosmic-ray flags | M | mosaics exist on the NIRCam grid; flux scale agrees with AstroDrizzle and RELICS |
| P3 | Process phase proper: quality gate, sky and quadrant offsets, align to the NIRCam catalog with write-back, per-visit cosmic rays, previews; all seven `pg004` filters | L | astrometry and photometry targets met on `pg004` |
| P4 | RAW mode: `bestrefs`, `calacs`, `calwf3`, variable-background repair, HST context in configuration | M | RAW and archive mosaics compared and understood |
| P5 | EGS tile against the CEERS release: sparse-Gaia alignment, pre-2007 ACS, several programs, satellite-trail and persistence masks, second-pass outliers | M-L | all section 6 targets met against CEERS |
| P6 | Scale: Abell 2744 (about 950 exposures), memory and run-time tuning, epochs, then full EGS, UDS, COSMOS | L | full-field mosaics; compute and storage numbers replaced by measurements |
| P7 | Layout, deploy, database and web support (separate design) | L | HST mosaics visible in the portal |
| Later | Downsampled products; DASH; WFC3/UVIS | M each | |

P2 deliberately uses archive files and archive flags so that the first comparison isolates one question: is our unit conversion, pixel mapping and drizzle correct for HST data?

## 8. Compute and storage estimates

All of these are estimates to be replaced by measurements in P0 to P2. File sizes marked *derived* follow from array dimensions; others are *inferred*.

**Per exposure.**

| Item | ACS/WFC | WFC3/IR |
| --- | --- | --- |
| RAW | about 70 MB (*inferred*) | about 35 MB for 16 reads (*inferred*) |
| IMA | none | about 270 MB for 16 reads (*derived*: 16 x 1024^2 x 16 bytes); deleted after use |
| Canonical FLC / FLT | 168 MB (*derived*: 2 chips x 4096 x 2048 x 10 bytes) | 17 MB (*derived*) |
| Working copy | 168 MB | 17 MB |

**Abell 2744** (DJA cluster footprint: about 550 ACS and 440 WFC3/IR exposures):

- Exposures: ACS canonical 92 GB plus the same again as working copies; RAW about 38 GB; WFC3/IR under 25 GB in total. About 250 GB.
- Mosaics: the tile is 1.26 gigapixels; `SCI`, `ERR` and `WHT` at 4 bytes each are 15 GB per filter in the `_i2d`, the same again as split files, and the same again if the pre-background snapshot is kept: 15 to 45 GB per filter, 140 to 400 GB for nine filters uncompressed. Most pixels are NaN and compress well.
- Memory for the tile drizzle: about 22 bytes per pixel of accumulators without the context cube, so about 28 GB, the same as a NIRCam filter on this tile.

**COSMOS F814W** over the full field (about 2,300 exposures, *inferred* count; 20 tiles of 478 megapixels): canonical exposures about 390 GB plus working copies; mosaics 115 GB per representation.

**Compute.**

- `calacs` with the CTE correction dominates. Unknown until timed; plan for minutes per exposure on several cores (*inferred*), so tens of core-hours for Abell 2744 and hundreds for COSMOS.
- `calwf3`: seconds to tens of seconds per exposure (*inferred*).
- Per-visit AstroDrizzle cosmic-ray pass: a few minutes per visit (*inferred*).
- Tile drizzle: an ACS exposure has four times the pixels of a NIRCam detector image; expect tens of seconds per exposure per pass on one core (*inferred* from the NIRCam cost), so a few hours per filter for Abell 2744.

**Reference cache.** ACS darks and biases change every anneal cycle. A field with data from many epochs needs many of them; size *to be measured* in P0 by a `bestrefs` dry run over one field's exposure list.

## 9. Scope notes

- **`nep` (NEXUS)** has no HST imaging to reduce. Fields declare their HST filters explicitly, so a field with none is simply skipped.
- **DASH** (COSMOS-DASH, 3D-DASH F160W). These exposures drift during the integration and can only be used by splitting each ramp into its 25 s read differences and aligning those. Starting from RAW makes this possible later as a special `calibrate` branch (grizli's `wfc3dash` is MIT-licensed), but it is not in the first version; the deep CANDELS F160W in COSMOS is ordinary guided data and is included.
- **WFC3/UVIS** is not implemented, and nothing here rules it out: it is a two-chip CCD with FLC files like ACS/WFC, and the instrument token in names and keys keeps its filters distinct.

## 10. Open questions

For you:

1. Which production fields use `implementation = "campfire"` for outlier and resample? If the answer is "none yet", the HST module would be the first production user of the native drizzle, and P1 should include a NIRCam comparison of the two backends on a full tile.
2. Is `hstcal` from conda-forge acceptable on the reduction cluster (CANDIDE), and is there disk for an HST reference cache?
3. Are the HST mosaics that `scripts/cosmos_inspec.py` reads (COSMOS-Web tile grid, 30 mas) available as a comparison reference, and who made them?
4. Should the HST section of a field live inside the field's table in `fields.toml` (proposed) or in a separate file? This also affects the config plane (`campfire config push`), which mirrors `fields.toml` sections to the cloud.
5. Block-aligned grids for downsampled products, given the existing explicit COSMOS 60 mas tiles use the other convention: acceptable?
6. You recalled that the CEERS HST mosaics were aligned to Legacy Surveys before NIRCam data existed. The release README says the frame is Gaia EDR3, and my notes contain one unverified hint that Legacy Surveys was the intermediate catalog. Both can be true (Gaia frame, carried by a denser catalog); I could not confirm it. It matters only for how we interpret residual offsets against that reference.

To be answered by measurement or reading, in the phase noted:

7. Does the `drizzle` package reproduce AstroDrizzle on an FLC, including the flux scale? (P2)
8. Which reference pixel area do the ACS/WFC and WFC3/IR flats normalize to, for the conversion to MJy/sr? (P2, tested by photometry)
9. Does `drizzlepac` 3.11 install beside `jwst` 1.20.2 in practice, and what is the `calacs` run time? (P0)
10. Can our own outlier code with `minmed` match AstroDrizzle's per-visit flags closely enough to replace it, removing `drizzlepac` from the run-time path? (P3; not required)
11. Do MAST persistence models exist for all our WFC3/IR exposures, and can they be fetched in bulk? (P5)
12. How should stars without proper motions in a NIRCam-derived reference catalog be handled across a 20-year baseline? (P3, `rj0911` is the test case)
13. Does `tweakwcs.FITSWCSCorrector` handle the ACS lookup-table distortion on write-back without loss? Its documentation says such WCS are supported; not exercised. (P3)
14. Is an in-visit self-persistence mask adequate where no MAST model exists? (P5)
15. What `pixfrac` gives acceptable weight uniformity for two-exposure WFC3/IR coverage at 30 mas? (P2 to P3)
