# HST imaging module for CAMPFIRE: design proposal (Stage 3)

Date: 2026-11-11. Follows `feasibility.md` (Stage 1) and `nircam-fit.md` (Stage 2). Nothing here is implemented.

## 0. Decisions this design is built on

Confirmed at the checkpoint:

1. **Hybrid approach.** STScI tools for what is specific to HST detectors; CAMPFIRE's own alignment and mosaic machinery, generalized from the NIRCam pipeline, for everything from astrometry onward.
2. **Start from RAW**, as the NIRCam pipeline starts from `uncal`. Archive FLT/FLC ingest stays available as a second mode (section 2.2 explains why both).
3. **30 mas for everything by default**, as a configuration value. Coarser products are made by block-downsampling the 30 mas mosaic, a path that would also serve MIRI later.

Added after review of the first draft of this document:

4. **`ERR` maps are not rescaled for correlated noise.** The mosaic `ERR` keeps the meaning it has for NIRCam: the formal per-pixel variance from the drizzle. Correlated noise is the business of downstream catalog codes, which measure it with random apertures.
5. **The NIRCam production path is `align` plus the `campfire` backend** for outlier and resample, in every field. Those are the only NIRCam code paths the shared machinery has to preserve bit for bit. They become the package defaults (section 7, P0); a field with `align` on and no reference catalog stops with a clear message. `jhat` and the `jwst` backends stay available as options.
6. **`hstcal` from conda-forge and a local HST reference cache are acceptable** on the reduction machines.
7. **`fields.toml` gets a symmetric layout with one sub-table per instrument** (`nircam`, `acswfc`, `wfc3ir`) in place of "JWST at top level, HST in a sub-table" (section 3.2).
8. **Block alignment is the rule for coarser grids.** The explicit 60 mas COSMOS tile entries are unused and can be ignored.

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
- *Reference.* The same reference catalog the field's NIRCam `align` uses. For most fields that is a NIRCam catalog, extracted from a NIRCam mosaic that was itself aligned to Legacy Surveys positions (which are on the Gaia frame and far denser than Gaia; the CEERS HST mosaics used Legacy Surveys the same way). HST and NIRCam exposures are then tied to one catalog, and that catalog is the NIRCam sky itself. Where HST reaches beyond the NIRCam footprint, `refcat query` plus `merge` extend the catalog with Legacy Surveys or Gaia sources. Stars need care across a 10 to 20 year baseline: Gaia stars are propagated to the exposure epoch (`refcat/motion.py`), and stars without proper motions are down-weighted or dropped in favour of galaxies.
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

**Field.** `FieldBase` holds the geometry (tangent point, tiles), epochs, the workspace and the working-copy logic. `NircamField` adds the `jw` glob rules and raw layout; `HstField` adds sky-based selection. Both read the same field table, so HST and NIRCam mosaics of a field share one tile definition by construction.

**`fields.toml` layout.** Today everything at the top of a field table is implicitly NIRCam: `filters`, `files`, `skip`, step overrides, and tiles as any sub-table that happens to contain `crpix` and `naxis` (`nircam/field.py:433-453`). Proposed layout: geometry is shared and explicit, and each instrument has its own sub-table, named with the token used in product names.

```toml
[cosmos]
    tangent_point  = [150.1163213, 2.200973097]
    fiducial_tiles = ['A1', 'A2']

    [cosmos.tiles.A1]            # shared grid: no instrument owns it
        rotation = 20
        [cosmos.tiles.A1.30mas]
            crpix = [-22990, 23090]
            naxis = [19200, 24910]

    [cosmos.nircam]              # everything that is NIRCam today
        filters = ['f115w', 'f150w', 'f277w', 'f444w']
        files   = ['jw01727*', 'jw05893*']
        skip    = []
        [cosmos.nircam.align]
            refcat = 'cosmos_ls_dr10.ecsv'
        [cosmos.nircam.epochs.CW]
            files = ['jw05893*']

    [cosmos.acswfc]
        filters = ['f435w', 'f606w', 'f814w']
        exclude_programs = []
    [cosmos.wfc3ir]
        filters = ['f125w', 'f140w', 'f160w']
        exclude_programs = [14114, 16259]     # DASH
```

Points to decide when this is designed in detail:

- Whether a field-level block should hold settings both HST detectors share (the reference catalog, program exclusions), or whether the two detector tables simply repeat them.
- Epochs: a date range is instrument-neutral, a file glob is not. The sketch puts epochs under the instrument; a shared date-range epoch could live at field level.
- Migration. The loader would accept the current flat layout as "all NIRCam" for a transition period, and a converter rewrites a file in place (comment-preserving, with `tomlkit` as `campfire config pull` already does). The change is wider than the pipeline: the config plane mirrors each field section into `fields.config` with a hash, and the deploy client and web app read `filters` and tiles from it (`nircam-fit.md` section 1.7). It therefore needs its own short design and is a separate phase (section 7), not something to fold into the mosaic refactor.

### 3.3 Order of refactoring, and how we know NIRCam did not change

Every step below is its own PR with a PATCH-level Infrastructure changelog entry.

- **R0. Baselines first.** (a) A small synthetic golden test in the repo: a handful of mock `ImageModel` exposures with mock gwcs (the helper in `pipeline/tests/_align_gwcs.py` already builds these), drizzled and outlier-flagged, with array hashes pinned. It runs in CI. (b) A real-data baseline on the reduction machine: one small tile (for example `rj0911` `venus`, two filters, one SW and one LW), `combine` run at the current commit with each backend, and SHA-256 of every output array (`SCI`, `ERR`, `WHT`, `EXPTIME`, `NEXP`, each working copy's `DQ`) and of the manifests with timestamps removed.
- **R1. Pure moves.** Files move to `imaging/` with re-exporting stubs left in `nircam/`. No logic change.
- **R2. Adapter seam.** `drizzle.py` and `outlier.py` take `Chip` objects; `JwstChip.load()` contains the current body of `_prepare_drizzle_input` verbatim.
- **R3. Remove hard-coded JWST structure** behind adapter attributes with NIRCam values as defaults: `hdul[1]`, the 2048 frame, filename parsing for visit, detector and program, the mosaic name template.
- **R4. Field split** into `FieldBase` and `NircamField`.
- **R5. Additions used only by HST**: FITS TAN grid, plain FITS writer, `minmed`, generic blot.

Acceptance for R1 to R4: the full test suite passes without edits to test logic; the synthetic golden hashes are unchanged; the real-data baseline is **bit-identical** in every array, with header differences limited to `CMPFRTIM` and `CMPFRVER`. No tolerance is needed because R1 to R4 change no arithmetic and no ordering. If a step cannot meet that, it is split until it can, or the difference is reported before merging. R5 does not touch the NIRCam path.

The baseline covers the production path only: `align` for astrometry and `implementation = "campfire"` for outlier and resample, which every production field uses. The `jhat` step and the `jwst` backends are kept as options. They are not shared with HST, so they stay in `nircam/` and must keep working through the refactor (the existing tests cover their wiring), but they are not held to the bit-identical test. One thing still to settle before R0: whether the pipeline CI job should become a required check once the golden test exists.

## 4. Output grids

**Same grid as NIRCam.** `cfpipe hst resample --field X` asks the same `TileGrid` for the same tile at the same scale label as NIRCam does. Tangent point, rotation, `crpix` and `naxis` are the same numbers, so pixel (i, j) of an HST mosaic and of the NIRCam mosaic of that tile are the same patch of sky.

**Default scale: 30 mas for ACS/WFC and WFC3/IR**, read from `[hst.resample].pixel_scale`, overridable per detector and per field. Consequences, with numbers from the Fruchter and Hook formula (my arithmetic):

| Detector | Native | Output | `pixfrac` | Correlated-noise factor R |
| --- | --- | --- | --- | --- |
| ACS/WFC | 50 mas | 30 mas | 0.8 | 1.8 |
| WFC3/IR | 128 mas | 30 mas | 1.0 | 4.6 |
| WFC3/IR | 128 mas | 30 mas | 0.8 | 3.8 |

R is the ratio between the true noise on large scales and the pixel-to-pixel scatter. At 30 mas a WFC3/IR mosaic looks about four times smoother than it is. This loses no information. The `ERR` map is not rescaled for it: as for NIRCam, `ERR` is the formal per-pixel variance from the drizzle, and aperture noise including the covariance between pixels is measured downstream with random apertures. The table is here so that nobody reads pixel scatter in an HST mosaic as its depth.

`pixfrac` and kernel are configuration; starting values are 0.8 (ACS) and 1.0 (WFC3/IR, where two-exposure coverage is common), square kernel, to be tuned on the proof of concept by PSF width and weight uniformity.

**Downsampled products.** `downsample` makes an N x N block-binned mosaic from the 30 mas one: weighted mean for `SCI`, sum for `WHT` and `EXPTIME`, and `ERR` propagated formally from the per-pixel variances (same meaning as in the parent mosaic, no covariance term). With N = 2 or 4 this gives 60 or 120 mas products whose pixels are exact unions of 30 mas pixels, which is the grid `Field.get_tile_wcs` derives when a scale is not declared (`nircam/field.py:975-981`). This is the route for WFC3/IR users who prefer near-native sampling, and for a future MIRI.

Block alignment is the rule for every coarser grid. The explicit 60 mas entries in the COSMOS tiles follow a different convention (`nircam-fit.md` section 1.5); they are unused and are ignored here.

**Sparse coverage.** HST often covers a small part of a NIRCam tile (the Abell 2744 tile is 56759 x 22171 pixels; the Frontier Fields core is a few arcminutes). The mosaic keeps the full tile shape so the grids stay identical, and is mostly NaN. Split files are already gzip-compressed for upload. If local disk becomes a problem, the alternative is HST sub-tiles defined as integer-pixel crops of a NIRCam tile, which keeps the pixel grid; I would not add that until it is needed.

## 5. Products and metadata

**Names.** `mosaic_<instrument>_<filter>_<field>_<scale>_<tile>[_<epoch>]`, with instrument tokens `acswfc` and `wfc3ir` (NIRCam stays `nircam`). This is the existing template with the instrument made a variable (`nircam/manifest.py:185`). WFC3/UVIS would later be `wfc3uvis`, which keeps its F606W and F814W apart from ACS.

**Mosaic headers.** `BUNIT = MJy/sr`, `PIXAR_SR`, `PIXAR_A2`; `TELESCOP`, `INSTRUME`, `DETECTOR`, `FILTER`; `CMPFRVER`, `CMPFRTIM`; calibration provenance (`hstcal` version, HST CRDS context, or `archive` plus retrieval date); versions of `drizzlepac`, `stwcs`, `drizzle`; reference-catalog hash; `pixfrac`, kernel, weight type. The per-input table records for each chip: root name, proposal, date, exposure time, `PHOTFNU` used, sky value, WCS name, `CAL_VER`.

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
4. For COSMOS, A. Koekemoer's HST mosaics on the COSMOS-Web 30 mas tile grid. The files `scripts/cosmos_inspec.py:52-55` reads are usable; newer versions probably exist and are to be located before this comparison is run.

**Metrics and targets.** Targets are starting values to be revised after the proof of concept.

| Area | Measurement | Target |
| --- | --- | --- |
| Astrometry, absolute | Offsets of compact sources between the HST mosaic and the NIRCam mosaic of the same tile (same pixel grid, so measured directly); against Gaia with proper motions where stars exist | median below 5 mas per axis; scatter below 15 mas per axis for bright sources (CEERS reports 12 to 15 mas HST to NIRCam) |
| Astrometry, internal | Per-exposure residuals from the align diagnostics; residual vector map across the chip (tests distortion and the ACS skew) | no coherent pattern above 5 mas; stellar FWHM in the mosaic within 5% of the reference mosaic |
| Photometry | Aperture and total fluxes, ours over reference, against magnitude, for isolated sources; zeropoint check on stars; ACS F814W against NIRCam F090W stellar locus | median ratio within 2% for 20 < m < 25; no trend with position on the chip (tests the pixel-area and unit handling) |
| Combination | Pixel-level difference between our drizzle and AstroDrizzle run on the same inputs, grid and weights (proof of concept only) | differences consistent with interpolation of the pixel map; no flux scale offset |
| Noise | Random-aperture scatter as a function of aperture size, reported as a diagnostic and compared with the reference mosaic (no `ERR` rescaling); `ERR` against the reference rms map in blank sky; weight against exposure time; depth (5 sigma) against the reference at matched aperture | large-aperture noise and depth within 0.1 mag of the reference for the same exposures; `WHT` proportional to exposure time at fixed background |
| Artifacts | Unmatched compact detections per arcmin^2 (in ours, absent from the reference and from NIRCam); count of residual trails and persistence images by inspection of previews; fraction of pixels masked per exposure | residual cosmic-ray density no higher than the reference; every masked trail confirmed |
| Calibration modes | Mosaic from `source = "raw"` against `source = "archive"` for the same exposures | differences explained by reference-file vintage and ramp repair |
| NIRCam regression | Section 3.3 | bit-identical |

## 7. Phased implementation

Sizes are rough: S is about one working session, M two to three, L four or more.

| Phase | Content | Size | Exit criterion |
| --- | --- | --- | --- |
| P0 | Environment and baselines: install `drizzlepac`, `acstools`, `wfc3tools`, `hstcal` next to the pipeline in a throwaway environment on the reduction machine; time `calacs` on one exposure; size the reference cache with a `bestrefs` dry run. Make the production NIRCam path the package default (`implementation = "campfire"` for outlier and resample, `align` in place of `jhat`), as its own small PR. Then the NIRCam golden test and real-data baseline (R0) | S-M | install works or a blocking conflict is documented; defaults flipped with production output unchanged; baselines stored |
| P1 | Refactor R1 to R4 | M-L | NIRCam bit-identical |
| P1b | `fields.toml` multi-instrument layout (section 3.2): short design, loader with legacy fallback, converter, config-plane and deploy readers | M | existing fields load identically from old and new layouts |
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

## 8a. P0 results so far (2026-11-11, on the development Mac)

Measured, in a throwaway conda environment cloned from `campfire` (Apple silicon, 24 cores):

- **Install.** `pip install drizzlepac acstools wfc3tools astroscrappy` added `drizzlepac 3.11.0`, `acstools 3.8.2`, `wfc3tools 1.6.1`, `astroscrappy 1.3.0` and changed none of `jwst 1.20.2`, `stcal`, `stdatamodels`, `drizzle 2.1.1`, `tweakwcs`, `gwcs`, `astropy`, `numpy`, `photutils`, `scipy`, `pandas`; `pip check` reports no broken requirements. `conda install -c conda-forge hstcal` added `hstcal 3.2.0` (calacs 10.4.1; conda-forge is one release behind the 3.3.0 tag) with only `cfitsio` and `libcurl`-related additions. `jwst`, `drizzlepac`, `stwcs` and the `drizzle` package import together. This answers open question 9 for macOS; the Linux cluster still has to be checked.
- **Reference selection.** `crds bestrefs --new-context hst_1372.pmap --update-bestrefs --sync-references=1` on one ACS/WFC RAW file downloaded 16 reference files, 1.3 GB, into a private cache. The cache is laid out as `references/hst/acs/` with a `jref` entry beside it.
- **`calacs`.** One full-frame ACS/WFC exposure (F606W, 400 s, 2020), RAW to FLT and FLC: **67.5 s wall on 24 threads**, of which about 48 s is the CTE correction. The per-core cost is not measured yet: a second run with `OMP_NUM_THREADS=1` took 65.8 s and its log still reports 24 threads, so calacs does not honour that variable. Its own options are `-1` and `--nthreads <N>`; the `calibrate` step must pass one of them when exposures are processed in parallel, or each worker will try to use every core.
- **File sizes.** RAW 34.3 MB (the 70 MB in section 8 was an overestimate); FLC 167.8 MB, as derived; calacs also writes an FLT of the same size, which can be deleted.
- **MAST access: inconclusive.** Three listing attempts failed on the day: the legacy portal product-listing call (astroquery; not the route `cfpipe download` uses) timed out after 600 s; a CAOM TAP query through the pipeline's own `_tap_query` helper, selecting HST files by target position, did not return within 10 minutes; and the same query selecting by observation-id prefix, the form `cfpipe download` uses, timed out at 120 s. A control run of that last query for a JWST observation (`jw06882060`) also timed out at 120 s, so the TAP service was slow for everything at the time and these attempts say nothing about HST in particular. Direct download by product URI took one second. Whether the existing TAP route lists HST files as quickly as it lists JWST files is still to be tested, on a day when the JWST query answers in seconds.
- **Test suite.** Eight tests fail on this machine at the base commit, before any change of mine: seven in `pipeline/tests/test_align_apply.py` (the coarse matcher declines the gross shift and the pool ends `NOT_ALIGNED`) and `test_linefit.py::test_observation_carries_line_fitting_overrides`. Cause not yet investigated. Since `align` is the production path, the first of these has to be understood before the refactor's acceptance gate is meaningful.

## 8b. Implementation notes (kept current as phases land)

- **R0 done.** `pipeline/tests/test_nircam_mosaic_golden.py` with its reference, and `pipeline/scripts/mosaic_baseline.py`. The real-data baseline is still to be taken (MAST listing was unavailable when it was attempted, and this machine holds no NIRCam raw data); commit `c204ae74` is the last one before any module moved.
- **R1 done in two batches.** Moved to `imaging/`: `geometry`, `manifest`, `mosaic_exp`, `bkgsub`, `drizzle`, the `refcat` library modules, and `align.{detect,footprint,histmatch}`. Old paths are aliases of the same module objects. Not moved yet, because they still reach into NIRCam-specific code: the align solver (JWST corrector, DVA), `align/apply.py`, `outlier_detect.py` (calls `steps/outlier.py` and the plotting module), and `field.py`.
- **R2, as built, differs from the plan in section 3.3.** The plan was to wrap the JWST route in a `JwstChip` whose `load()` held the old code verbatim. What was built instead: the drizzle accepts either file paths, in which case the existing JWST code runs untouched, or `Chip` objects, in which case a new branch prepares the input and a plain-FITS writer writes the result. Everything between (accumulators, variance pass, exposure maps) is shared. Reason: zero risk to NIRCam, since no existing line executes differently. Cost: two "prepare" and two "write" implementations in the drizzle. NIRCam can move onto a `JwstChip` later, with the golden test as the judge. Measured on the golden scene: the FITS `TileGrid` equals the `stcal` gwcs grid to 4e-9 pixel at rotations 0, 20 and -51.5 degrees; the same exposures wrapped as chips give the JWST-route mosaic to float32 rounding (largest SCI difference 2e-7 on values up to 2.2), with identical coverage, `NEXP` and `EXPTIME`.
- **One difference in weights on the chip route.** Under `ivm`, a pixel whose background variance is zero or non-finite gets weight 0. `stcal` gives such a pixel weight 1. A chip adapter is expected to supply a real variance wherever it marks a pixel good.
- **First light on real HST data (P2, ACS/WFC part).** One full-frame ACS/WFC exposure (`je2860juq`, F606W, 400 s, 2020), calibrated here from RAW with `calacs`, then `stwcs.updatewcs(use_db=False)`, then `hst.exposure.open_exposure` and the shared `drizzle_tile` onto a 9694 x 9694 north-up 30 mas `TileGrid`; the same file through AstroDrizzle's final step on that grid with the same good-pixel bits, no sky subtraction on either side. Result: coverage 45,172,668 against 45,172,655 pixels; flux ratio ours / AstroDrizzle 1.000000 in the median at sky level, on the brightest 0.5% of pixels, in the sum over common pixels, and near the centre of each chip; pixel-difference rms 6.8e-5 e-/s against an image rms of 0.152 e-/s. This answers open questions 7 and 8 for ACS/WFC: the `drizzle` package reproduces AstroDrizzle through a plain `astropy.wcs` pixel map, and `IDCSCALE**2` is the right reference pixel area for the conversion to surface brightness. Run time for the two chips: 16 s for our drizzle (science and variance passes, exposure maps, writing) and 4 s for AstroDrizzle's single pass. WFC3/IR is not yet tested on real data.
- **Facts about our own `calacs` output** (from that file): no distortion in the headers until `updatewcs` runs (linear WCS only; the adapter refuses such a file); after it, SIP order 5 plus `WCSDVARR` and `D2IMARR` lookup tables, all read by `astropy.wcs`; no `S_REGION`; no `PHOTFNU` on ACS (computed from `PHOTFLAM` and `PHOTPLAM`); `PHOTMODE` carries the observation date, so the zeropoint is the exposure's own; DQ bits present are 4, 16, 32, 64, 128, 256, 512, 1024, with 4.5% of pixels dropped under the default policy and no cosmic-ray flags.
- **Variance model changed from the sketch in section 3.2.** The draft split `ERR**2` per pixel into a source term and a remainder and weighted by the remainder. On CCD data the remainder still follows the pixel's own noise (HST `ERR` is computed from the noisy counts), which biases a weighted mean. As built (`HstChip._background_variance`): the weight variance is one background level per chip (median of `ERR**2` minus source Poisson, times effective exposure time), applied as level / `t_eff`, so it is constant for a CCD exposure and inversely proportional to the per-pixel `TIME` for WFC3/IR. Everything in `ERR**2` above that level goes into `var_poisson` and so into the mosaic `ERR`. This is close to what the CANDELS-lineage weights are (background only) and to grizli's one-number-per-chip weight, with the IR time scaling added. A later refinement would add the flat-field and dark structure from the reference files. **Please review this choice**; it sets the `WHT` maps.
- **`ERR` underestimates the noise of the CTE-corrected frame.** On the test exposure the background level implies 8.2 to 8.4 e- per pixel, and the measured blank-sky scatter (robust, sources clipped) is 9.7 e-. The planned rescaling of the background variance to the measured sky scatter (section 2.2, step 5) is therefore needed, not optional, for ACS; expected factor about 1.3 to 1.4 in variance on recent data.
- **WFC3/IR first light.** One archive FLT (`icqtbbbxq`, F140W, `WCSNAME = IDC_w3m18525i-HSC30`, calwf3 3.7.3) through the adapter and the shared drizzle onto a 5200 x 5200 30 mas grid, against AstroDrizzle on the same grid: flux ratio 1.000000 in the median at sky level, on bright pixels and at the chip centre; sum ratio 0.999975. `IDCSCALE` is 0.12825 arcsec. AstroDrizzle covers 0.09% more output pixels; the adapter also drops pixels with non-positive `ERR` or zero effective time, which the DQ bits alone do not exclude. The unit conversion therefore holds for both detectors.
- **Sky step, as built** (`hst/steps/sky.py`). Sources are masked on a 5-pixel box-smoothed image at 3 sigma, sharp spikes at 5 sigma of the unsmoothed sky, both grown by 3 pixels. (A first version clipped single pixels at 2.5 sigma and grew them; it masked half of each frame and biased the measured scatter low. Found on the real frames, not by the synthetic test.) Results on the two real exposures:

  | Exposure | Sky | Measured scatter | Amplifier offsets removed | `CFVARSC` | Blank fraction |
  | --- | --- | --- | --- | --- | --- |
  | ACS `je2860juq` chip `SCI,1` | 41.80 e- | 9.62 e- | +0.07, -0.08 e- | 1.31 | 0.73 |
  | ACS `je2860juq` chip `SCI,2` | 41.90 e- | 9.66 e- | -0.20, +0.19 e- | 1.39 | 0.73 |
  | WFC3/IR `icqtbbbxq` | 0.998 e-/s | 0.047 e-/s | none | 2.15 | 0.83 |

  The ACS blank fraction is low because a single 400 s frame is full of cosmic rays, each masked with a 3-pixel border. The WFC3/IR variance scale of 2.15 is unexplained: that exposure comes from the set STScI uses to demonstrate time-variable background, which would do it, but this was not checked. If the scale turns out to track bad ramp fits, it is a cheap detector for them.
- **`wcs_init` as built** (`hst/steps/wcs_init.py`): `stwcs.updatewcs(use_db=False)` with `jref` / `iref` set only for the duration of the call. Exercised by hand on the ACS exposure; the unit test replaces `updatewcs` with a stand-in because the real one needs reference files.
- **A real single-visit mosaic through pipeline code** (not a CAMPFIRE field: three dithered ACS/WFC F606W exposures of program 15843, association `je2860010`, 400 s each). Chain: `calibrate` (RAW, context `hst_1372.pmap`, calacs 10.4.1) → `wcs_init` → `sky` → `crreject` → shared `drizzle_tile` (pixfrac 0.8, `ivm` weights) onto a 9812 x 9812 30 mas grid. Timings on the Mac: calibrate 51 to 65 s per exposure; wcs_init and sky about 5 s per exposure; crreject 16 s for the visit; drizzle 40 s for six chips. `crreject` flags 1.40 to 1.55% of pixels per exposure. Sky differs between exposures (41.8, 42.1, 50.2 e-), as expected within an orbit; variance scale 1.29 to 1.39 on every chip. The three chip-gap positions are filled by the dithers (`NEXP` = 2 there, 3 elsewhere). Figure kept at `~/campfire-data/scratch/claude/hst_visit_f606w.png`. No alignment step was run: within one visit the header WCS is good enough to combine.
- **That visit mosaic against AstroDrizzle's own full product** (its own sky and cosmic-ray rejection on copies of the same three calibrated exposures, same grid, pixfrac 0.8, exposure-time weights):

  | Measure | Result |
  | --- | --- |
  | Sources detected (5 sigma, 9 pixels or more) | 1233 in ours, 1234 in AstroDrizzle; 1229 matched |
  | Centroid offset, ours minus AstroDrizzle | 0.01 mas in x, 0.00 mas in y; scatter 0.01 mas |
  | 0.3 arcsec aperture flux ratio, brightest 30% | median 0.9998; 16 to 84% range 0.9992 to 1.0002 |
  | Pixel rms of blank sky | 0.0241 MJy/sr in both (ratio 0.999) |
  | Pixel difference in units of our `ERR` | robust scatter 0.022 where three exposures overlap, 0.012 where two do |
  | Sky level left in the mosaic | ours +1.8e-3 MJy/sr, AstroDrizzle +0.3e-3 MJy/sr (a 0.04 `ERR` constant offset) |
  | Detections in one mosaic and not the other | 4 in each, with the same split by coverage |

  The two reductions agree to well under the noise in every measure. The one systematic difference is the sky level: our source-masked sky sits about 1.5e-3 MJy/sr below AstroDrizzle's `localmin` estimate, so slightly more sky is left in our mosaic; the mosaic-level background step will remove a constant like this. The compact features noticed in the two-exposure bands are present in both mosaics, so they are either real or missed by both rejections; this comparison cannot tell which.
- **`crreject` as built.** AstroDrizzle's separate-drizzle, `minmed` median, blot and `driz_cr` steps on copies in a private directory, final combination off. The flags are read from the per-chip `*_crmask.fits` files, because AstroDrizzle writes the DQ bit into its inputs only during the final drizzle (`drizzlepac/adrizzle.py`, `updateInputDQArray`). The first version copied the DQ arrays back and stamped the files with zero flags; the step now refuses to stamp an ACS exposure in which nothing was flagged.
- **`calibrate` as built.** `python -m crds.bestrefs --new-context ... --update-bestrefs --sync-references=1`, then `calacs.e` or `calwf3.e`, each in a subprocess whose environment alone carries the HST CRDS settings and the `jref` / `iref` directory. Work happens in a private directory; only the stamped canonical file is moved into place. `--nthreads` is passed to calacs when requested. Run for real on ACS/WFC only; the WFC3/IR branch (including the IMA and ramp repair) is not built.
- **One of the eight known test failures is environmental.** `test_linefit.py::test_observation_carries_line_fitting_overrides` fails here because it validates its program slug against the machine's own `$CAMPFIRE_ROOT/config/programs.toml`. The other seven (`test_align_apply.py`) are being fixed separately.

## 9. Scope notes

- **`nep` (NEXUS)** has no HST imaging to reduce. Fields declare their HST filters explicitly, so a field with none is simply skipped.
- **DASH** (COSMOS-DASH, 3D-DASH F160W). These exposures drift during the integration and can only be used by splitting each ramp into its 25 s read differences and aligning those. Starting from RAW makes this possible later as a special `calibrate` branch (grizli's `wfc3dash` is MIT-licensed), but it is not in the first version; the deep CANDELS F160W in COSMOS is ordinary guided data and is included.
- **WFC3/UVIS** is not implemented, and nothing here rules it out: it is a two-chip CCD with FLC files like ACS/WFC, and the instrument token in names and keys keeps its filters distinct.

## 10. Open questions

Answered in review (recorded in section 0): the production NIRCam path is `align` plus the `campfire` backend everywhere; `hstcal` and a reference cache are fine; the COSMOS HST mosaics on the tile grid are A. Koekemoer's and usable, with newer ones to be located; block alignment is the rule; the CEERS HST mosaics are on the Gaia frame through Legacy Surveys positions; most CAMPFIRE NIRCam fields are aligned to a NIRCam catalog taken from a mosaic that was aligned to Legacy Surveys.

Also answered: one `fields.toml` sub-table per instrument; the default flips are approved (logged as an Algorithm changelog entry, since default-config output changes even though no production field does); `jhat` and the `jwst` backends are kept.

Still for you, none blocking P0:

1. In the new `fields.toml` layout, where should settings shared by both HST detectors go (reference catalog, program exclusions): repeated in `acswfc` and `wfc3ir`, or in a field-level block?
2. Should the pipeline CI job become a required check once the golden mosaic test exists?

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
