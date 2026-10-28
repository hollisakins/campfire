# Design: NIRSpec master background — feasibility study

**Status:** feasibility study / proposal, not scheduled. Nothing in this document is implemented.
**Sources:** the pipeline code on this branch; jwst 1.20.2 (the pinned version) read from the wheel; msaexp `main`, `jwst_backgrounds` and `jwst_gtvt` read from GitHub on 2026-10-28; jwst readthedocs; RUBIES / JADES papers as cited inline.
**Date:** 2026-10-28
**Context:** the NIRSpec stage 2b nodded (leapfrog) background subtraction in
`pipeline/campfire_pipeline/nirspec/stage2.py`; the canonical spectrum-exposure file
(`nirspec/canonical.py`, issue #212); the deployed intermediates `nirspec_rate` and
`nirspec_spectrum_exposure` ([intermediate-products design](design-intermediate-products.md),
[NIRSpec review loop](design-nirspec-review-loop.md)).
**Driver:** derive, for every NIRSpec exposure, a *master background* — the sky spectrum seen through
the MSA, built from every open shutter that holds no source flux (dedicated background shutters and
the empty shutters of each nodded slitlet) — store it as a product in its own right, and offer it as an
alternative to nod subtraction for the sources where the nods fail (a contaminated or stuck background
shutter, an extended source that fills its slitlet, or a bright neighbour in the pattern).

---

## 0. Summary

**Feasible, and most of the pieces exist.** The pipeline already (a) finds the footprint of every open
shutter on the detector (stage 1 `mask_slits`), (b) carries per-shutter geometry and a per-shutter
flux statistic (`stuck_shutters._compute_shutter_regions`), (c) reads and rewrites MSA metafiles
(`metafile.py`), (d) keeps a reversible calibrated frame in each canonical file (`PRE_BKGSUB_*`), and
(e) has a product/registry/deploy path for source-independent per-exposure intermediates (the rate file).
A master background is a new per-exposure product that sits next to the rate file and a second way of
filling stage 2b's `bkg` argument.

The two things that do **not** exist and carry the work:

1. **A surface-brightness view of every open shutter.** Stage 2a runs `Spec2Pipeline` once per
   *source*, and across all sources the canonicals do cover every *source* slitlet — including its
   empty nod shutters — calibrated as a point source. §1.7 shows that per-pixel surface brightness is
   recoverable from them with two array ratios the slit already carries plus one barshadow
   evaluation, so a prototype of the clean-shutter selection can run on today's products. What the
   canonicals never contain are the MSA plan's dedicated background slitlets and the detached
   background shutters stage 2a prunes. The derivation therefore either assembles from the canonicals
   and adds a small extraction for those, or runs one full-metafile pass per rate file (assign_wcs →
   extract_2d → flat → pathloss(uniform) → barshadow → photom(extended)), which is what both jwst's
   `master_background_mos` and msaexp's `get_global_background` do (§4.3 weighs the two).
2. **Clean-shutter selection.** jwst only uses slitlets the MSA plan labelled as background
   (`source_name` `*_BKG*`); the empty shutters of a nodded slitlet are, to jwst, part of the source's
   slit. Using them is the whole point here, so the selection logic (and its QA table) is ours to write
   (§4.2). This is the scientifically delicate part; the plumbing is not.

Recommended shape: a new stage-2 sub-step (`cfpipe nirspec master-bkg`, run unconditionally inside
`run --all` between 2a and 2b) that writes one `<root>_<nod>_<nrsN>_mbkg.fits` per rate file, a
`BKGMETH` choice (`nodded` | `master`, per observation and per source/nod through the existing
override TOML) that decides what stage 2b subtracts, and no change to the `_spec.fits` format in the
first phase. The dual-product variant (both subtractions in one `_spec.fits`) is possible but doubles
stage 3 and touches the web; it is described in §4.5 as a later phase.

**Measured on `ember_egs_p1` (§9):** the empty shutters agree on one sky to 0.3 %, the sky is stable
across nods to 0.2 %, but the sky level varies shutter to shutter by ±4 % (±2.5 % by MSA quadrant,
~2.7 % per shutter per exposure), which the nods cancel exactly and an ensemble sky cannot. So the
master background is the secondary product for extended sources with no clean sky in their own
slitlet, with per-quadrant scales and a ~3 % error floor; nodding stays the archive default (§9).

Expected science gain before that measurement, as arithmetic: a 3-point nod subtracts the mean of two
other nods, so the subtracted frame carries 1.5× the background variance of a single exposure; a master
background built from tens of clean shutters adds ~σ²/N, so the background-limited noise drops by
≈ √1.5 ≈ 1.22. That matters only where the sky dominates (PRISM, and the red end of the gratings); in
the read-noise-dominated gratings the gain is small and the systematics risk (§2.3) is what decides.

---

## 1. What exists today

### 1.1 Stage 1 — detector background on the rate file (`stage1.py`)

`subtract_background_from_rate_file` fits and removes the *detector-level* background from
pixels that are **not** under any open shutter: the `mask_slits` helper runs `AssignWcsStep` on the
rate file, builds the slitlet WCS for every open slitlet in the metafile **and** every failed-open
shutter (`msaflag_open.create_slitlets`), and masks their full traces (widened to
`[nirspec.stage1.override_wavelength_range]` so higher orders are covered). What is fitted on the
remaining pixels, iteratively: the CRDS *picture frame* template (per-quarter scale + pedestal),
optionally a `Background2D` surface (`subtract_2d`, default off), per-column 1/f (template fit) and,
for the prism, per-row 1/f. The total is stored in the rate file as `CFBKG` with its mask
`CFBKGMASK`, and `VAR_RNOISE` is rescaled to the measured residual rms.

Consequence for this design: after stage 1 the rate file still contains the **sky transmitted through
the open shutters** (zodiacal light, ISM, stray light, thermal). That is exactly the signal a master
background models. Stage 1 also already has the only full-frame "which pixels belong to which open
slitlet" machinery in the repo — the inverse of `slitmask` is the footprint a 2-D master background has
to fill.

### 1.2 Stage 2a — one `Spec2Pipeline` run per source (`stage2.py:run_stage2a_single_rate`)

For each rate file and each catalog source in the metafile, stage 2a writes a *per-source* metafile
(`MetaFile.filter_by_source_id`): only the slitlet(s) containing the source survive, the other shutters
of that slitlet are relabelled `background = 'Y'`, `source_id = 0`, stuck-closed shutters from
`reference/nirspec/<obs>/stuck_closed_shutters.toml` are removed, and `stellarity` is forced to
`set_stellarity` (default 1.0 → `POINT`). `Spec2Pipeline` then runs with `bkg_subtract`, `barshadow`,
`resample_spec`, `extract_1d` skipped. The output is the canonical file
`<root>_<nod>_<nrsN>_<source>.fits`: a one-slit `MultiSlitModel`, pathloss `POINT`, in MJy
(`fix_units` normalises the `UNIFORM` case and stamps `SRCFLUX`).

Two consequences:

- There is **no calibrated view of a whole exposure**. Dedicated background slitlets of the MSA plan
  (jwst names them `<prog>_BKG<slitlet>`, `assign_wcs/nirspec.py`) are never extracted at all, because
  `unique_source_ids` only yields catalog sources.
- jwst's `master_background_mos` step, were it switched on in this `Spec2Pipeline` call, would find
  zero background slits in the per-source metafile and skip (`_classify_slits` → `num_bkg == 0`).

### 1.3 Stage 2b — leapfrog nod subtraction (`stage2.py:run_stage2b_single_slitlet`)

Per `bkg_group` (one source × detector × sub-pixel-dither group, `Observation.group_files`), the
2/3/5 canonicals are loaded, pathloss is inverted, the cutouts are padded to a common detector region,
and jwst's `BackgroundStep` subtracts from each nod the sigma-clipped **mean of the other nods'
cutouts** (`background_sub.average_background`). The per-nod set of background exposures can be
narrowed or emptied by `reference/nirspec/<obs>/nodded_background_overrides.toml` (an empty list
*excludes* the nod from the science stack — the only remedy today for a nod whose partners are both
contaminated). Pathloss is re-applied and the result is written **in place**: the live `SCI` becomes
the subtracted frame, the calibrated arrays are stashed as `PRE_BKGSUB_*` so the operation is
reversible (`canonical.restore_pre_bkgsub`), the rectified `S2D_BKGSUB_*` view is cached, and
`CFP_BKG`, `BKGNODS`, `BKGOVR` are stamped. Stage 3 copies `BKGNODS`/`BKGOVR` into the `EXPOSURES`
table of every `_spec.fits`.

This is the natural insertion point: `bkg` is just a list of slit models with the same detector
footprint as the science cutout. A master-background cutout in the same frame is a drop-in
replacement for `[mod[0] for j, mod in enumerate(models) if j != i]`.

### 1.4 Stage 3 and the final product (`stage3.py`)

`Spec3Pipeline` (outlier detection, resample, extract_1d) runs on the subtracted canonicals whose
`CFP_BKG` is a timestamp (not `skipped:`/`excluded:`), then `opt_ext_single_source` writes
`<obs>_<grating>_<source>_spec.fits` with HDUs, in order: `PRIMARY`, `SPEC1D`, `SCI`, `ERR`,
`WAVELENGTH`, `WHT`, `PROF1D`, `EXPOSURES`. **`slits.py` and `metadata/shutters.py` read
`EXPOSURES` by index (`hdu=7`)**, so any new HDU must be appended after it. The deploy JSON
(`python/campfire/deploy/generate.py`) reads `SPEC1D.wave/fnu/fnu_err`, `SCI`/`ERR` and `PROF1D`
only; extra columns and HDUs are invisible to the web until it is taught to read them.

### 1.5 Products, registry, deploy

Product kinds are registered once in `layout/campfire_layout/products.py` and dispatched by filename
suffix in `bijection.py` (mirrored in `web/lib/layout.ts`, checked by the layout golden fixture).
Within `products/nirspec/<obs>/`, the fallback for a bare `.fits` is `nirspec_spectrum_exposure`, so
`_rate.fits` and `_lines.fits` are dispatched explicitly before it; a new suffix must be too.
`python/campfire/deploy/discover.py` globs `*_nrs[12]_*.fits` for spectrum exposures and excludes only
`_spec.fits`/`_rate.fits`; `spectrum_grid.parse_spectrum_exposure_filename` raises on a non-integer
source token, so an unguarded `_mbkg.fits` would make deploy fail loudly rather than misfile — the
guard is still required. The rate file is the precedent for a *source-independent per-exposure*
intermediate: `nirspec_rate` kind, `nirspec_rate_exposures` table (detector-grain, web-reviewable),
pulled with `campfire pull --intermediate` through `INTERMEDIATE_PRODUCT_TYPES`
(`python/campfire/db/store.py`).

### 1.6 Pieces that are directly reusable

| Need | Exists as |
| --- | --- |
| open-shutter footprint per rate file, incl. failed-open shutters | `stage1.mask_slits` |
| full-metafile slit geometry, shutter ↔ slit-frame mapping | the slit WCS `detector → slit_frame`; `stuck_shutters._compute_shutter_regions` (rectified frame) |
| per-shutter flux statistic vs. read noise | `stuck_shutters._analyze_source_shutters` (`low_frac`) |
| reversible calibrated frame per canonical | `canonical.PRE_BKGSUB_*`, `restore_pre_bkgsub` |
| pathloss inversion / re-application around a subtraction | stage 2b |
| per-source, per-nod reducer decisions that `pull` round-trips | `nodded_background_overrides.toml` (`nirspec_bkg_override`, `USER_STATE`) |
| a stage that writes a TOML of auto-detections for the reducer to vet | stuck-shutter detection (`merge_stuck_shutters`, provenance tags) |
| per-exposure product + web triage row | `nirspec_rate` / `nirspec_rate_exposures` |

### 1.7 What the canonicals already give you (and what they do not)

Because stage 2a runs over *every* catalog source, the union of an exposure's canonicals is the
calibrated cutout of every source slitlet — the source shutter and its empty nod shutters alike. Per
pixel, converting the stored point-source calibration to the surface brightness a sky model needs is
arithmetic on arrays the slit already carries (jwst 1.20.2 source, verified):

| Step | What the canonical holds | Conversion to uniform-source surface brightness |
| --- | --- | --- |
| photom | `MJy` per pixel (point): `conversion = photmj`; uniform would be `photmj / pixelarea_steradians` (`photom.py:762-780`) | divide by `PIXAR_SR` (in the `SCI` header) |
| pathloss | data divided by `pathloss_point`; **both** `pathloss_point` and `pathloss_uniform` 2-D arrays are stored on every MOS slit (`pathloss.py:604-605`) | multiply by `pathloss_point / pathloss_uniform` (the `correct_nrs_fs_bkg` ratio, in the other direction) |
| barshadow | skipped in stage 2a; jwst never applies it to `POINT` slits anyway | run `BarShadowStep(source_type='EXTENDED')` on a copy of the one-slit model to get the `barshadow` array (needs only the slit meta and WCS, which are present), then divide |
| flat | applied; for MOS the same flat serves point and uniform (only `NRS_FIXEDSLIT` keeps `flatfield_point` / `flatfield_uniform`, `flat_field.py:424`) | nothing |
| wavecorr | `slit.wavelength` carries the *point-source* zero-point shift of the source position, which is wrong for an extended sky | the uncorrected wavelengths are still in the WCS: wavecorr inserts a `wavecorr_frame` *after* `slit_frame` (`wavecorr.py:118-126`), so `wcs.get_transform('detector', 'slit_frame')` returns λ without the shift |
| failed-open shutters | `msa_flagging` ran, so `MSA_FAILED_OPEN` is set in `DQ` | mask |
| overlapping slitlets | not recorded per canonical | paint every canonical's `xstart/ystart/xsize/ysize` box onto a detector-frame counter; keep pixels covered once (msaexp's `nexp == 1`) |

So a prototype of §4.2's clean-shutter selection needs no pipeline change: load an observation's
canonicals, apply the three corrections, split by slit-frame y, and look at the per-shutter spectra.

What the canonicals **cannot** supply:

- **Dedicated background slitlets** of the MSA plan. `MetaFile.unique_source_ids` yields catalog
  sources only (`_source_rows`: `source_id > 0`, or `0` with `background == 'N'`), so a slitlet with
  no primary source is never extracted. These are the cleanest shutters available.
- **Detached background shutters**: `_prune_detached_shutters` drops shutters more than two columns
  from the source group so jwst does not build an absurdly wide slit — exactly the shutters a sky
  model would like to have.
- **Stuck-closed shutters** are removed from the per-source metafile before extraction — correct for
  the science, and harmless here (they are rejected candidates either way), but it means the TOML's
  state is baked into which pixels exist.
- One frame with all slits of an exposure together, and the correction arrays of a *uniform*-source
  calibration pass (jwst's de-calibration trick, §3.1, needs them).

**Decision (2026-10-28): stage 2a is reworked to extract the background-only slitlets too**, kept in
a form that is trivial to clean up — and without a second `Spec2Pipeline` pass over the rate file.
Two ways to get there; the second is the one to build:

- *Minimal*: one extra `Spec2Pipeline.call` per rate file with a metafile holding only the
  background-only slitlets. Relative to today's cost (one call per *source*, i.e. 100–200 calls per
  rate file) it is +1 call, a per-cent-level increase — but it is still a whole call, and the heavy
  fixed work (CRDS reference I/O, the flat-field references, `assign_wcs` set-up) is repeated.
- **One call per rate file, split afterwards.** Every edit `filter_by_source_id` makes is a per-slitlet
  row operation on the metafile — drop stuck-closed shutters, prune detached shutters, mark the one
  primary shutter, relabel the rest `background = 'Y'`, set `stellarity` — and the jwst steps that
  follow are per slit and independent of one another (`assign_wcs` builds each slitlet's WCS from its
  own rows; `extract_2d`, `wavecorr`, `flat_field`, `pathloss`, `photom` act slit by slit;
  `msa_flagging` is exposure-wide in both arrangements). So one **edited full metafile** carrying all
  the per-source edits at once, plus the untouched background-only slitlets, gives one
  `MultiSlitModel` with every source slit (`POINT`, as now) and every background slit (jwst assigns
  background slitlets `stellarity = 0.0` → `srctype` makes them `EXTENDED`, so with `barshadow`
  enabled in the call it is applied to the background slits and skipped for the point sources, which
  is exactly the mix wanted). The orchestrator then splits the model: one one-slit canonical per
  source, written through `_finalize_canonical` as today (`MultiSlitModel()` + `update(parent)` +
  `slits.append(slit)`, the construction jwst's own `_extend_bg_slits` uses), and the background slits
  into the `bkgslits/` file. The whole of stage 2a drops from *N_source × (fixed overhead + one slit)*
  to *fixed overhead + N_source slits* per rate file — a large speed-up in its own right, with the
  background slitlets as a by-product.

  *Why the two slit types are calibrated differently, and why that is right.* The bar-shadow
  correction models how the MSA bars dim a shutter that is **uniformly illuminated**: the PSF-blurred
  shadow of the bars lowers the surface brightness as a function of position across the shutter and
  of wavelength, and dividing by the reference restores the true sky surface brightness. A **point
  source** is not uniform across the shutter; what the bars (and the shutter edges) take from it is
  the fraction of its PSF that misses the aperture, which is the point-source *pathloss* correction.
  Applying bar shadow to a point source would double-count the loss, so jwst applies bar shadow only
  to `EXTENDED` slits and point pathloss only to `POINT` slits. The sky is extended **everywhere**,
  including inside the source's own shutter, so every sky pixel — in a background slitlet or in an
  empty shutter of a source slitlet — needs the uniform treatment to read as true surface
  brightness, and that is what §1.7's conversions and the `bkgslits/` calibration both produce. The
  consistency requirement is not "calibrate both slit types the same way"; it is "subtract the sky in
  a frame where source and sky carry the same per-pixel factor". In the rate frame that holds
  trivially (DN/s = source counts + sky counts), which is why `MBKG2D` is a rate-frame image and why
  jwst's own step de-calibrates the master background with *uniform* arrays and subtracts before the
  science slit's own (point) calibration continues. Stage 2b's §4.4 shortcut is the same statement:
  `cal(mbkg) = F_point · mbkg_rate` and `science_cal = F_point · (source + sky)_rate`, so the difference
  is `F_point · source_rate`. The nods work for the same reason — the other nods' cutouts carry the
  same per-pixel `F_point` on the same pixels. Residual risk is the bar-shadow reference's own
  accuracy (a few per cent next to the bars), mitigated by excluding bar-adjacent rows when
  *measuring* the sky (msaexp's `bar > 0.35` cut; §4.2 step 2) — the *expansion* in step 5 still
  fills those rows, because the sky under the source really is dimmed there.

  Constraints the split has to respect, each already visible in today's code:
  - jwst raises `MSAFileError` for a slitlet with more than one `primary_source == 'Y'` row
    (`assign_wcs/nirspec.py`, "more than 1 shutter with primary source"), so the duplicate-source rule
    (`_duplicate_source_ids`: keep the lowest id) is applied while editing the metafile, and the
    dropped twin's rows become background rows of the kept slitlet — the same canonical as today.
  - jwst gives a background slitlet `source_id = slitlet_id`, which can collide with a real catalog
    id; the split must key background slits on `source_name` (`*_BKG*`), never on `source_id`.
  - `--source-ids` and `overwrite=False` become metafile filters (only the sources without a product
    go into the edited metafile), and a `NoDataOnDetectorError` becomes "slit absent from the output"
    → the existing `_nodata` marker.
  - Error isolation: today one source's failure does not stop the others. The single call needs a
    fallback that re-runs the failing rate file per source (the current code path, kept as the
    fallback), so a pathological slitlet costs time, not the observation.
  - The one per-*run* parameter that differs between sources is the extended-wavelength photom
    override for fixed-slit sources inside MSA exposures (`get_extended_photom_path(fixed_slit=True)`
    vs the MSA one). Only when `extend_g140m_g235m` is on *and* the metafile has fixed-slit sources do
    those sources need their own call; everything else shares one.
  - Bit-identity gate before switching: run both arrangements on one observation and diff every
    slit array of every canonical (the same kind of check the issue #212 cutover used). The arrays
    should match exactly; the primary headers will differ in the per-source `MSAMETFL` name and in
    nothing that stage 3 reads.
- Optionally the same edited metafile re-admits the detached shutters `_prune_detached_shutters`
  dropped from source slitlets, as synthetic slitlets (a fresh `slitlet_id` per contiguous group;
  jwst only uses the id to group rows). Worth doing: these are the shutters MPT placed as background
  on purpose.
- **Storage**: `products/nirspec/<obs>/bkgslits/<root>_<nod>_<nrsN>_bkgslits.fits`. A subdirectory, not
  a suffix in the workspace root, for three reasons: `Observation.glob` and
  `deploy.discover_spectrum_exposures` are non-recursive, so nothing that enumerates canonicals,
  finals or rate files can ever see them; the layout needs one `nirspec_bkgslits` kind with a subdir
  builder exactly like `nirspec_manual_mask` (`subdir=lambda s: f"nirspec/{s.obs}/manual_masks"`),
  no change to the bare-`.fits` fallback; and cleanup is `shutil.rmtree(bkgslits/)`. They are
  regenerable intermediates: `master-bkg` deletes the directory once the `_mbkg.fits` products are
  written unless `[nirspec.stage2].keep_bkgslits = true`, and the per-shutter facts survive in
  `MBKG1D` / `SHUTTERS`. They are not deployed (lifecycle `LOCAL_ONLY`, or simply never discovered).
- Skip logic as for the canonicals: file present and stuck-shutter TOML mtime unchanged → skip.

With that in place the derivation has everything in hand without a full-metafile pass: source slitlets
from the canonicals (§1.7 conversions) plus background slitlets natively calibrated. §4.3 keeps the
full pass as the fallback if the two-source bookkeeping proves worse than one pass.

---

## 2. What a master background is, and when it beats the nods

### 2.1 Definition

Per exposure (one rate file: exposure × detector), the sky surface brightness B(λ) in MJy/sr that
every open shutter sees, assumed spatially uniform over the 3.6′ × 3.4′ MSA field. Expanded to 2-D on
a slit through the calibration chain (flat, uniform-source pathloss, barshadow, extended-source photom)
it predicts the sky counts in each pixel of each shutter; subtracting that from the source shutter
removes the sky without touching the other nods. Empty shutters elsewhere in the exposure measure B(λ)
with N-fold better statistics than the two partner nods.

### 2.2 Which shutters are "clean"

In a 3-shutter slitlet with a 3-point nod, two of the three shutters are nominally empty at each nod.
They are **not** clean when:

- the catalog source is extended enough to spill over the 0.07″ bar into the next shutter (common for
  z ≲ 2 galaxies; the nod subtraction then self-subtracts, which is the classic failure mode);
- an uncatalogued neighbour sits in the shutter (continuum, or a single emission line at one wavelength);
- the shutter is stuck closed or partially closed (no sky, or less sky) — `stuck_closed_shutters.toml`
  and the stage 2a auto-detection already know about most of these;
- the slitlet's trace overlaps a failed-open shutter's trace on the detector (`msa_flagging` flags the
  pixels; they must be excluded, not modelled);
- a bright source in a neighbouring slitlet scatters into it (rare, but the per-shutter residual test
  below catches it).

Dedicated background slitlets of the MSA plan (`background = 'Y'` with no primary source) are the
cleanest candidates when they exist; many archive programs have none, which is why the nod shutters
cannot be ignored.

### 2.3 Where it helps and where it does not

- **Helps**: the prism and the long-wavelength ends of G235M/G395M/G395H, where zodiacal + thermal
  background dominates the pixel variance; sources whose nods are contaminated or stuck (today
  excluded or dropped to a single pair); very extended sources; the per-shutter QA table doubles as a
  **contamination detector** that can propose `nodded_background_overrides.toml` entries
  automatically, the way stuck-shutter detection proposes its TOML.
- **Does not help, or hurts**: nod subtraction cancels, pixel for pixel, every multiplicative
  systematic that is the same across nods (flat-field error, barshadow residual, the detector-position
  dependence of the sky itself through the MSA optics, 1/f residuals left by stage 1). A master
  background inherits those at the few-per-cent level of the sky, which on the prism can exceed the
  statistical gain for faint continuum. JADES kept nod subtraction and dropped contaminated background shutters
  instead; msaexp/DJA differences nods unless a global sky file exists; RUBIES built a master sky for the
  prism only and still reports the nods winning for compact faint sources (§3.2). The honest position is that the master background is an *alternative*
  whose value has to be measured on real CAMPFIRE data, which argues for the "derive and store
  always, apply by choice" plan.

---

## 3. Prior art

### 3.1 jwst `master_background_mos` (verified against jwst 1.20.2, the pinned version)

Source: `jwst/master_background/master_background_mos_step.py`, `nirspec_utils.py`,
`expand_to_2d.py`; wired into `Spec2Pipeline._process_nirspec_msa_slits` (`calwebb_spec2.py`) for
`NRS_MSASPEC` only, between `srctype` and `wavecorr`, i.e. **on rate-frame cutouts before flat-field**.

- **Background slits** are the slits whose `source_name` contains `BKG` or `BACKGROUND`
  (`is_background_msa_slit`); `assign_wcs` gives that name to slitlets with no primary source
  (`<prog>_BKG<slitlet_id>`). A source slitlet's empty nod shutters are part of the source slit and are
  never used. With no background slit or no source slit the step skips.
- **Flow** (`_calc_master_background`): run `flat_field`, `pathloss`, `barshadow`, `photom` on *all*
  slits with `source_type` forced to `EXTENDED` (uniform pathloss, surface-brightness photom); copy the
  background slits out; `pixel_replace` → `resample_spec` → `extract_1d` each; `combine_1d_spectra`
  (exposure-time weighted, `sigma_clip`, default 3) into one `CombinedSpecModel`; optional moving
  median (`median_kernel`). Then `expand_to_2d(..., allow_mos=True)`: `np.interp` of `surf_bright`
  onto each science slit's 2-D wavelength array (zero and `DO_NOT_USE` outside the table's range); and
  the 2-D background is **de-calibrated** by running `photom`, `barshadow`, `pathloss`, `flat_field`
  with `inverse=True` and `use_correction_pars=True` (the correction arrays saved by the forward pass),
  so it lands in the same rate-frame units as the science cutouts. `apply_master_background` subtracts
  slit by slit (`subtract_2d_background`).
- **Parameters**: `sigma_clip`, `median_kernel`, `force_subtract`, `save_background`,
  `user_background` (a 1-D `x1d`-style table to use instead of deriving one), `inverse`.
- **Products** with `save_background=True`: `_masterbg1d` (the combined 1-D table), `_masterbg2d`
  (per-slit 2-D background `MultiSlitModel`), `_bkgx1d` (the per-slit background extractions).
- **Interaction with nods**: if `cal_step.bkg_subtract` or `master_background` is already `COMPLETE`
  the step skips unless `force_subtract`. jwst treats the two as alternatives, not a hybrid. In the
  spec3 `master_background` step MOS is explicitly refused (`allow_mos=False` zeroes it).

What to take from it: the de-calibration trick (forward pass saves correction arrays, inverse pass
applies them) is the clean way to move a 1-D sky onto a slit in rate-frame units; `expand_to_2d` and
`combine_1d_spectra` are reusable as functions. What not to take: the "only `BKG` slits" selection and
the slit-level (not shutter-level) granularity.

### 3.2 msaexp (read from `gbrammer/msaexp` `main` on 2026-10-28)

msaexp keeps **nod differencing as the default** and has three sky-model mechanisms beside it, all in
`msaexp/slit_combine.py` / `slit_group.py`:

- `SlitGroup(diffs=True)` differences nods; for the gratings `grating_diffs` forces it on. With
  `diffs=False` the subtracted term becomes `sky_background` instead of the other nods — that flag *is*
  the "model the sky" mode (`make_diff_image`, `get_flat_diff_arrays`, `SlitGroup.data`: `clean = sci − sky`).
- `estimate_sky(mask_yslit=[[-4.5, 4.5]], min_bar=0.95, df=51, high_clip=0.8, outlier_threshold=7, …)`
  fits a flexible **B-spline in wavelength** to the science slitlet's *own* off-source pixels
  (`mask_yslit` excludes the source and contaminants), producing a per-slitlet `sky2d`; when a global
  sky template is supplied the spline becomes a low-order (`global_sky_df=7`) rescaling of it. Off by
  default (`estimate_sky_kwargs=None`).
- A **global background per exposure**: `NirspecCalibrated.get_global_background(sample=1.0,
  min_yslit=5, suffix="gbkg")` in `slit_group.py` — "Get background spectrum from all available MSA
  shutters" — masks as source-contaminated every shutter of a slitlet with `num_shutters > 2` within
  `|yslit| < min_yslit`, drops stuck shutters (`stuck_mask`), restricts to quadrants 3–4 on NRS1 and
  1–2 on NRS2, and takes **percentiles [5, 16, 50, 84, 95] per wavelength bin** of the remaining
  pixels, written as `<root>_gbkg.fits`. `set_background_spectra()` reads those, interpolates `p50`
  with `perr`, adds a 3 % floor (`esky² + (0.03·sky)²`) and optionally refits a spline/Chebyshev
  rescaling. The production driver (`msaexp/cloud/combine.py`) sets
  `diffs = (no *gbkg.fits present)`: nods are differenced unless a global background exists.
  ~100 per-visit `msa_sky/<visit>_sky.csv` tables for public programs ship in the repo.
- The older `msaexp/pipeline.py` has no sky model (background = other exposures with
  `|dy_off| ≥ 0.2`; background slitlets recognised by `source_name.startswith("background")`).

The RUBIES reduction built on it (de Graaff et al. 2024, arXiv:2409.05948, App. A.2) describes the
same idea: an average 1-D sky per mask "using both the empty sky slitlets included in the mask design
and relatively empty portions of slitlets of faint sources", fitted as a solar spectrum with a modified
slope (reflected zodiacal light) plus cubic splines, subtracted from the bar-shadow-corrected 2-D
spectrum, PRISM only — and the explicit caveat that "nodded background subtraction still performs better
for compact sources with low S/N". JADES (arXiv:2306.02465 §V.7) instead kept the nods and excluded
background shutters that NIRCam/HST imaging showed to be contaminated.

**msaexp's selection, pixel by pixel** (`NirspecCalibrated` in `slit_group.py`, read on
2026-10-28). It is a full-frame, full-metafile method: the rate file is run through the jwst steps for
*all* slits, and each calibrated slit is painted back onto a 2048 × 2048 detector-frame set of maps
(`full[...]`), per pixel: `data` (× pixel area × barshadow × pathloss, i.e. surface brightness), `wave`,
`bar` (the barshadow value), `nexp` (how many slits cover the pixel), `num_shutters` (shutters in the
pixel's slitlet, capped at 3), `exp_index` (which slit), `yslit`, `slit` (quadrant, shutter x/y). An
`edge_mask` already drops half a shutter (`shutter_pad = 0.5`) at both ends of each slitlet. `set_mask`
then keeps a pixel only if **all** of:

- `nexp == 1` — covered by exactly one slitlet (no overlap with another slitlet or a failed-open trace);
- `bar > 0.35` — not under or next to an MSA bar (the barshadow model is the geometry);
- **not** (`num_shutters > 2` **and** `|yslit| < min_yslit`) — this is the geometric source cut: in a
  slitlet of three or more shutters, every pixel within `min_yslit` of the source's nominal trace is
  treated as source. `yslit = y_pixel − y_trace(x)` is the cross-dispersion distance in **pixels** from
  the trace position the slit WCS predicts for the catalog source (`utils.get_slit_coordinates`), so with
  `min_yslit = 5` (the `get_global_background` default; `set_mask`'s own default is 3) a band of ±5 px ≈
  ±0.5″, about one shutter, is masked and the two empty nod shutters survive. Slitlets of one or two
  shutters get no source mask at all — they are kept whole (that is a modelling choice to be aware of,
  not obviously right for CAMPFIRE's data);
- not a fixed slit other than S200A1/A2/B1;
- `data > −3 σ_rnoise` (no deeply negative pixels).

`mask_stuck_closed` adds the stuck-shutter mask (fraction of pixels below 2 σ_rnoise, prism threshold
0.28). `get_global_background` then keeps, when building NRS1's sky, only pixels whose slitlet sits in
MSA quadrants 3 or 4, and for NRS2 only quadrants 1 or 2 (`np.isin(quad, [3, 4])` / `[1, 2]` on the
per-pixel quadrant map). That is what "quadrants restricted per detector" means: the MSA's four
quadrants project so that, for the prism, Q3/Q4 spectra land on NRS1 and Q1/Q2 on NRS2, and the
restriction drops the stray edge pixels of a slitlet whose spectrum belongs mainly to the other
detector. In a per-pixel formulation it is redundant — a pixel is on the detector it is on, and the
`nexp == 1` and edge masks already remove slivers — so CAMPFIRE need not copy it; the quadrant stays
useful only as a grouping variable for QA (does the sky agree across quadrants?). Finally
`percentile_bins` takes percentiles 5/16/50/84/95 of the surviving pixels per bin of a standard
wavelength grid, with `p50` the sky and `(p50 − p16)/√n` its error. There is no iteration and no
per-shutter decision: contamination is handled by the median over a large pixel population.

What to take from it: the **median (p50) per wavelength bin over all empty-shutter pixels** as the
robust estimator, the `num_shutters`/`|yslit|` geometric source cut, `nexp == 1` as the overlap guard,
per-detector-half quadrant restriction as a cheap spatial-uniformity guard, a few-per-cent error floor
on the sky, and the honest expectation that the nods win for compact faint sources on the prism. What
CAMPFIRE can add over it is the per-*shutter* verdict (§4.2 step 4), which the pixel-population median
never produces and which is what feeds the nod-override proposals.

### 3.3 `jwst_backgrounds` (the sky model) vs. `jwst_gtvt` (the visibility tool)

The tool that predicts the sky is **`jwst_backgrounds`** (JBT), not `jwst_gtvt`: `jwst_gtvt`
"calculates the allowed position angles for a given Right Ascension and Declination" (visibility
windows and V3PA) and has no background content. JBT (`jbt.background(ra, dec, wavelength, thresh=1.1)`
/ `get_background(...)`) reads the precompiled STScI background cache "the same as used by the JWST
Exposure Time Calculator" (HEALPix `nside=128`, fetched from MAST on first use) and returns, per
calendar day of the year (366 entries, Jan 1 = 0), the components `zodi_bg`, `nonzodi_bg` (Galactic
ISM), `stray_light_bg`, `thermal_bg` and `total_bg`, in **MJy/sr** ("equivalent in-field radiance").
Its wavelength grid is 108 points from 0.5 to 31 µm — 0.1 µm steps to 3.5 µm, 0.25–0.3 µm steps from
3.7 to 5 µm — i.e. R ≈ 10–50 across the NIRSpec range, far below even the prism.

**On spectral structure in the sky** (what is verified and what is not). Shortward of ~3 µm the
zodiacal light is scattered sunlight, and it reproduces the solar Fraunhofer lines faithfully — CIBER
measures the zodiacal brightness *from* the Ca II 854.2 nm line depth on that basis, noting early work
found the equivalent widths matched the solar spectrum to < 2 % (arXiv:2104.07104, read 2026-10-28; the
same paper notes the ZL *continuum* is reddened relative to the Sun, 1.25 ± 0.1 vs 1.5 between 854 nm
and 1.25 µm). So the NIRSpec sky at 1–3 µm is a reddened solar spectrum, not a smooth curve, and
longward of ~3 µm it is dominated by smooth thermal dust emission. **How deep the solar lines are at
grating resolution is not something I could verify**: the strongest solar features in 1–2.5 µm are the
hydrogen Paschen/Brackett lines (Paβ 1.282, Paγ 1.094, Brγ 2.166 µm) and the CO band heads near 2.3 µm,
and a line of equivalent width ~1 Å would be a ~7 % dip in a 13 Å (R ≈ 1000) resolution element at
1.28 µm and ~1 % at prism resolution — but I did not find a quiet-Sun Paβ equivalent width to put in
that estimate, so treat the magnitude as unverified. It is cheap to settle empirically: stack the
empty-shutter G140M spectra of one observation and look at 1.282 µm. For the design the consequence
is small either way: the data-driven reference contains whatever lines the sky has, so per-shutter
clipping is unaffected; only the comparison against the smooth JBT model has to be done at R ≈ 20, and
a model-seeded pass 0 must not reject shutters on the solar lines (hence the coarse-binned scale fit).

So JBT gives a **date- and pointing-specific absolute level and broad shape** of the sky, which is
exactly what the plausibility check in §4.2 needs (`DATE-OBS` → day of year; `MBKGSCL` = data / model),
and *not* a spectrum one could subtract or use as a template at any disperser's resolution. STScI's
own guidance notes a case where a mask's empty-shutter prism background came out "significantly lower
than the model", and that a background measured from shutters concentrated on one part of the MSA or
one detector "may not be representative" (NIRSpec background office hours) — both arguments for the
data-driven reference with the model as a sanity bound.

---

## 4. Proposed design

### 4.1 Unit of work and the product

**Two units, deliberately different.** The 1-D sky and the 2-D rate-frame product have
different natural grains:

- *The 1-D sky B(λ)* is a property of the pointing, the MSA configuration and the hour, so its natural
  unit is the **exposure group** — all nods of one `(root, config)` on **both** detectors. Reasons:
  for the gratings each slitlet's spectrum spans both detectors (blue half on NRS1, red on NRS2) so a
  B(λ) over the full range needs both; for the prism, quadrants 3–4 fall on NRS1 and 1–2 on NRS2,
  so the two detectors are two independent samples of the *same* λ range and their agreement is the
  best available check on detector-dependent systematics; and the nods are minutes apart under the
  same MSA mask, so pooling them triples the clean-pixel statistics. The clean set is still decided
  **per exposure** (the source occupies a different shutter in each nod), the stage 1 residuals
  (picture frame, 1/f, pedestal) differ per exposure, and the detectors have independent flats — so a
  per-exposure × detector scale and additive offset against the pooled B(λ) *may* be needed.
  Decision (2026-10-28): **not part of the design until shown necessary.** The master background is
  derived and stored regardless; what the prototype (§7, P0 — a script over existing canonicals, run
  before any pipeline code is written) settles is only whether the pooled sky needs per-exposure
  correction terms. It computes, for each exposure × detector, the ratio and offset of that exposure's
  own clean-shutter sky to the pooled one. If those scatter at the level of the pooled sky's error,
  the terms are not fitted and the product merely records the diagnostic (`MBKGSCL`, `MBKGOFF`); if
  they do not, the fit is added to the derivation.
- *The 2-D product* `MBKG2D` is per **rate file** (exposure × detector): it is a picture of that
  detector, built on that exposure's slit geometry, and it is what stage 2b cuts out for a canonical.

So: one `_mbkg.fits` per rate file (the product the storage plane and the web already know how to
handle), each carrying the exposure group's pooled `MBKG1D` plus this exposure's own scale/offset,
its per-shutter table and its 2-D expansion. The sky is a property of the exposure, not of a source,
so this stays a *source-independent* intermediate like the rate file, with one row per
(observation, exposure_root, detector) if it is ever shown on the web.

**Product:** `<root>_<nod>_<nrsN>_mbkg.fits`, written beside the rate file in `products/nirspec/<obs>/`.

| HDU | Content |
| --- | --- |
| `PRIMARY` | provenance: `CFMBKGV` algorithm version, `MBKGNSH` clean shutters used (this exposure / the group), `MBKGNTOT` candidates, `MBKGSCL` / `MBKGOFF` this exposure's scale and offset against the pooled sky, `MBKGMDL` ratio to the `jwst_backgrounds` level, `MBKGSEED`, timestamps, the stuck-shutter TOML mtime (as stage 2a stamps `STKSHTIM`) |
| `MBKG1D` | table: `wavelength` (µm), `surf_bright` (MJy/sr), `surf_bright_err`, `n_shutters`, `model_surf_bright` (the `jwst_backgrounds` prediction, for QA) |
| `SHUTTERS` | one row per candidate shutter: slitlet_id, quadrant, row, column, source_id of the slitlet, nod role (`source` / `nod_empty` / `bkg_slitlet`), scale `a_s`, χ², `clean` flag, `reason` (`source`, `stuck_closed`, `continuum_excess`, `line_excess`, `failed_open_overlap`, `too_few_pixels`) |
| `MBKG2D` | full-frame float32 (2048 × 2048) **rate-frame** background: the de-calibrated B(λ(x, y)) on every open-shutter pixel, 0 elsewhere (compresses well; ~16 MB raw). Alternative: a `MultiSlitModel` of per-slit cutouts like jwst's `_masterbg2d`, smaller but not a picture of the exposure |
| `MBKG2DDQ` | `DO_NOT_USE` where the pixel's wavelength falls outside `MBKG1D`'s range or under a failed-open overlap |

Rate frame (DN/s, after stage 1's detector background) is chosen on purpose: it is the frame every
source's calibration starts from, so "subtract the master background" is well defined for a canonical
produced with *any* source type, stuck-shutter removal or extended-wavelength override (§4.4), and the
image can be rendered next to the rate file in the existing rate-exposure web view.

Not written into the rate file: an extra HDU there would change the rate file's content hash and
re-upload every rate file on the next push (whole-file dedup, storage plane). A sibling product keeps
the rate file immutable.

### 4.2 Derivation algorithm

Per rate file, in a worker (parallel over rate files like stage 1 / 2a):

1. **Full-metafile calibrated pass.** With the *original* metafile (`OGMETFL`, every slitlet of this
   `MSAMETID`), run `AssignWcsStep` → `MSAFlagOpenStep` → `Extract2dStep` → `SrcTypeStep` (force
   `EXTENDED` on every slit) → `FlatFieldStep` → `PathLossStep` → `BarShadowStep` → `PhotomStep`
   (`inverse`-capable steps, so their correction arrays can be reused in step 5). This yields every
   open slitlet in MJy/sr with its 2-D wavelength array and slit WCS. Stuck-closed shutters from the
   TOML are *kept* at this stage (they are candidates to be rejected with a reason, not removed);
   `set_stellarity` is irrelevant because everything is extended.
2. **Split slits into shutters.** Use the slit WCS transform `detector → slit_frame`: the slit-frame
   y coordinate is in units of the shutter pitch (1.15 per shutter, see `stuck_shutters._MSA_PITCH`),
   so each pixel maps to a shutter ordinal without resampling. Drop the bar rows and the 0.5-pitch
   padding beyond the outer shutters (the same exclusions `_compute_shutter_regions` applies). Keep
   the metafile's `primary_source == 'Y'` shutter(s) labelled `source`.
3. **Per-shutter spectrum.** For each shutter, pixels → (λ, SB, var) samples; bin onto a common
   wavelength grid for the disperser (the prism's dispersion varies along the slit, so bin, do not
   assume a shared pixel grid). Median or inverse-variance mean per bin; carry the pixel count.
4. **Clean-shutter selection, iterated (2–3 passes).**
   - Pass 0 reference (`master_bkg_seed`, two choices, both legitimate):
     `median` — the per-bin median across all non-`source` shutter pixels (msaexp's `p50`, §3.2),
     after the geometric cut, generalised from msaexp's and applied **per pixel**: drop every pixel
     within `source_pad` (default 5 px ≈ 0.5″, one shutter) in slit-frame y of the predicted trace of
     *any* catalog source in the metafile that falls in that slitlet — the slitlet's own source at
     this nod, and any second catalog source sharing the slitlet — regardless of the slitlet's
     shutter count (msaexp exempts one- and two-shutter slitlets; CAMPFIRE should not, since a
     one-shutter slitlet with a source in it has no empty pixels to offer). Sources *not* in the
     metafile are what the iteration is for; unbiased as long as fewer than half the pixels at a
     wavelength are contaminated, which holds for every realistic MSA. `model` — the
     `jwst_backgrounds` total for the pointing and `DATE-OBS` (§3.3), with **one global scale fitted
     to the data** (its absolute level is not trustworthy to the few per cent that matter, see §3.3),
     compared at the model's own resolution (bin both to R ≈ 20 for the scale fit; the model is a
     smooth continuum, and the real sky shortward of ~3 µm is scattered sunlight, which carries the
     solar Fraunhofer lines — see the note in §3.3 on how much that matters). The model seed
     has the merit of being independent of whether the "empty" shutters are empty; the median seed
     has the merit of the right spectral shape. In practice the iteration converges to the same clean
     set from either start when both are within ~20 % of the truth, which is why the choice is a
     config knob and not a design decision. The one case neither seed settles is a *common-mode*
     excess — every empty shutter carrying a few per cent of its own source's wings — because the
     model's absolute uncertainty is larger than that excess; §2.3's phase-3 measurement is the only
     arbiter there. Whichever seed is used, record the data/model ratio (`MBKGMDL`) and warn when it
     is far from 1.
   - For each shutter fit a single scale `a_s` against the reference and form the residual spectrum
     `r_s(λ) = SB_s − a_s · B_ref`. Reject: `a_s` significantly above 1 (continuum contaminant), a
     contiguous run of bins with `r_s > k σ` (emission-line contaminant; keep the shutter but mask
     those bins, or reject — a config knob), `a_s ≪ 1` (stuck closed / partially closed — this also
     cross-checks the TOML), `failed_open_overlap` from `msa_flagging`, too few pixels.
   - Recompute `B_ref` as the σ-clipped inverse-variance mean over clean shutters, with its error and
     `n_shutters(λ)`. Iterate.
   - Not recommended as the *selection rule*: "the lowest N % after subtracting the model". Ranking
     shutters by residual flux and keeping the faintest biases the sky low by construction (it picks
     the negative noise excursions and the partially vignetted shutters, and the bias grows as N
     shrinks); clipping *both* sides against a robust reference does not. Neither jwst (`combine_1d`
     clips values against a per-wavelength median) nor msaexp (percentiles) selects by rank. The model
     is welcome as the seed (above); the rule should be symmetric clipping.
   - Carry an error floor of a few per cent of the sky in `surf_bright_err` (msaexp uses 3 %), so that
     downstream variance does not pretend the master background is noiseless where calibration
     systematics dominate.
   - Optional refinement for a later version: per-quadrant (or per-detector-half) scale factors on top
     of the global B(λ), to absorb quadrant-level throughput differences; msaexp simply restricts to
     the two quadrants that project onto each detector, and STScI warns that shutters concentrated on
     one part of the MSA give an unrepresentative sky (§3.3). Keep v1 global but record each shutter's
     quadrant in `SHUTTERS` so the residual-by-quadrant QA can show whether it matters.
5. **Expand to 2-D and de-calibrate.** For every open-shutter pixel (the footprint from step 1),
   `np.interp` B(λ) onto the slit's wavelength array (jwst's `expand_to_2d.bkg_for_multislit` does
   exactly this per slit), then run `photom`, `barshadow`, `pathloss`, `flat_field` with
   `inverse=True, use_correction_pars=True` from the forward pass, and paint each slit's rate-frame
   cutout into the full-frame `MBKG2D` at its `xstart/ystart`. Where slitlets overlap on the detector
   (failed-open), flag `DO_NOT_USE`.
6. **Write** the product (atomic sibling-tmp write, `common/io` style) and a QA PDF: the 1-D sky with
   the model curve and the per-shutter spectra coloured by clean/rejected reason; the `MBKG2D` image
   next to the rate file.

Skip logic: product exists and its stamped stuck-shutter TOML mtime and stage 1 `CFBKGDT` match the
rate file's → skip unless `--overwrite` (the same pattern as `_bkgsub_group_done`, but keyed on the
product's own header because the rate file must not be stamped).

### 4.3 Where it runs, and what it costs

The user's intent is "part of stage 2b, always". Two constraints shape that:

- Its dispatch unit is the **rate file**, not stage 2b's `bkg_group`, and it needs stage 2
  reference files (flat, pathloss, barshadow, photom) that stage 1 deliberately does not touch.
- It must run **after** stuck-shutter detection has settled (stage 2a may re-run itself and rewrite
  the TOML) and **before** the nod subtraction that may consume it.

So: a distinct sub-stage, `run_master_bkg(obs, ...)`, exposed as `cfpipe nirspec master-bkg` and
called at the top of `run_stage2b` (so `stage2b` and `run --all` always produce it — the config rule
"config controls how, never whether" is respected: the product is unconditional, only its *use* is a
choice). Config under `[nirspec.stage2]`: `master_bkg_sigma_clip`, `master_bkg_line_sigma`,
`master_bkg_min_shutters`, `master_bkg_model_check` (the `jwst_backgrounds` comparison, off when the
cache is absent), `master_bkg_plot`.

Cost: with the single-call stage 2a of §1.7, the inputs (source canonicals and the `bkgslits/` file)
exist before `master-bkg` starts, and the derivation itself is array work — the §1.7 conversions per
canonical (one `BarShadowStep` evaluation each, seconds), binning, clipping, and the inverse
expansion. Order of a minute per rate file single-threaded, parallel over rate files, and stage 2a as
a whole gets faster, not slower. If the single-call rework is deferred, the minimal +1 call per rate
file costs about one source's worth of today's stage 2a. Memory: a `MultiSlitModel` of a few hundred
small cutouts is fine (~0.4 MB per slit for all arrays). `_prefetch_crds_references` already exists for
the multiprocessing race.

**Assemble from the canonicals, or run a full pass?** §1.7 shows the canonicals already hold the
source slitlets in a form two array ratios away from surface brightness, and the stage 2a rework
decided in §1.7 adds the background-only slitlets, so the derivation can skip the full-metafile pass: load the exposure's canonicals, apply `pathloss_point / pathloss_uniform`,
`1 / PIXAR_SR` and a `BarShadowStep(source_type='EXTENDED')` evaluation, read uniform-source
wavelengths from the `slit_frame` transform, paint the bounding boxes onto a coverage counter, and
run §4.2 steps 2–4. Cost: a few seconds per canonical, no `assign_wcs`/`extract_2d`. What it loses:
only the uniform-pass correction arrays for step 5 (the de-calibration can instead use the pixelwise
factor of §4.4, or the stored `pathloss_*` arrays, the barshadow evaluation and `PIXAR_SR` in reverse). The full pass is one code path, jwst- and msaexp-shaped,
and sees every open shutter; the assembling route is cheaper and can be prototyped today. A sensible
order: **prototype the selection on existing canonicals** (that answers the science question with no
pipeline change), then implement the production stage as the assembling route over canonicals plus
the `bkgslits/` products, falling back to the full pass only if the two-source bookkeeping proves
worse than one pass.

An alternative placement — folding the clean-shutter analysis into stage 2a's existing
`Spec2Pipeline.call` by passing the full metafile — was rejected: stage 2a's per-source metafile is
load-bearing (stuck-shutter removal, `set_stellarity`, duplicate-source handling, the fixed-slit
branch), and the per-source canonical must not change.

### 4.4 Applying it to the science path

Stage 2b subtracts in the pathloss-inverted calibrated frame of the per-source canonical. To subtract
the master background there, the rate-frame `MBKG2D` cutout at the canonical's slit footprint
(`xstart/ystart/xsize/ysize`) has to be brought to the same frame. Two exact ways, both cheap:

- **Forward-calibrate the cutout.** Build a one-slit `MultiSlitModel` from the canonical (it carries
  the slit WCS and meta), replace `data` with the `MBKG2D` cutout, clear the `cal_step` flags, and run
  `FlatFieldStep` and `PhotomStep` with the science slit's `source_type` (so the units match
  the canonical's MJy; pathloss stays off because stage 2b works in the pathloss-inverted frame).
  Three deterministic steps on one slit: seconds.
- **Pixelwise factor.** Every post-`extract_2d` step is multiplicative per pixel, so
  `F = pathloss⁻¹(cal_SCI) / rate_SCI[bbox]` is the exact calibration factor wherever `rate_SCI ≠ 0`,
  and `cal(mbkg) = F · mbkg[bbox]`. No jwst step calls at all, but it needs the rate file at hand and
  a smooth fill where the rate is zero or flagged. Fine as a cross-check of the first method.

Then `run_stage2b_single_slitlet` gets a third branch for `bkg`: `[cal_mbkg_slit]` instead of the
other nods. Everything downstream is unchanged: pathloss re-applied, `PRE_BKGSUB_*` stashed,
`S2D_BKGSUB_*` cached, and new provenance `BKGMETH = 'master'` (with `BKGNODS = 'master'`), which
stage 3 already copies into `EXPOSURES.bkg_nods`. `restore_pre_bkgsub` already makes the choice
reversible per canonical, so switching a source between methods is a stage 2b re-run, not a stage 2a
one.

**Who chooses.**

- `[nirspec.stage2].bkg_method = "nodded" | "master"` as the observation default (today's
  `[nirspec.stage3].method = "nodded"` is only a discovery switch and raises for anything else; the
  real knob belongs in stage 2).
- Per source and nod, the existing `nodded_background_overrides.toml` grammar extended with the
  literal `"master"` in a nod's list (`12345 = {3 = ["master"]}`): the override file already
  round-trips through `campfire pull` and the web nods renderer plans (review-loop §4.3), so no new
  reducer-state file is needed. An auto-detector can **propose** entries from the `SHUTTERS` table —
  a nod whose partner shutters are both flagged contaminated gets `["master"]` instead of today's
  `[]` (exclusion) — written with an `auto` provenance tag exactly as `merge_stuck_shutters` does, for
  the reducer to vet.
- A hybrid worth leaving room for: `["master", 1]` meaning "nod 1 and the master background"
  (jwst's `BackgroundStep` averages whatever list it is given). Not for v1.

**"Let users do it themselves."** With the product deployed as an intermediate, this already works
along the grain of the storage plane: `campfire pull --obs X --intermediate` materialises the
canonicals, rate files and (new) `_mbkg.fits`, `campfire pull` brings the reference TOMLs, and a user
runs `cfpipe nirspec stage2b --obs X` with their own `bkg_method`/overrides and `stage3` locally —
the canonicals restore from `PRE_BKGSUB_*` and are re-subtracted. The cost to the user is a pipeline
install with jwst and a CRDS cache, which they need for stage 3 anyway. What a user cannot do is get
both subtractions out of one deployed `_spec.fits`; that is §4.5.

### 4.5 Dual products in `_spec.fits` (later phase, optional)

To ship both subtractions, the canonical has to hold both states. The additive way: keep the live
`SCI` as the chosen method and add `MBSUB_{SCI,ERR,DQ,VAR_RNOISE,VAR_POISSON}` custom HDUs with the
other method's arrays (same mechanism as `PRE_BKGSUB_*`; `CUSTOM_HDU_PREFIXES` gains a prefix;
`CFSCHEMA` bumps to 2). Stage 3 then runs `Spec3Pipeline` a second time on a temporary
`MultiSlitModel` set with the `MBSUB_*` arrays swapped in, and `opt_ext_single_source` appends — after
`EXPOSURES`, because of the `hdu=7` readers — `SCI_MB`, `ERR_MB`, `WHT_MB` and adds `fnu_mb`,
`fnu_mb_err`, `flam_mb`, … columns to `SPEC1D`. Costs: stage 3 roughly doubles (it is the slow
NIRSpec stage: outlier detection + resample + extract per source), `_spec.fits` grows by the 2-D
arrays, the deploy JSON and `spectra` row need a second trace (a fifth sidecar url or new keys in
`spectrum_json`; the web's spectrum plot grows a toggle), and `linefit` / `zfit` have to say which
trace they fit. All additive, none of it hard, but it is a web + DB + pipeline change and should wait
until §4.4 has shown on real data that the master background is worth showing to users.

### 4.6 Touch points outside the pipeline

| Layer | Change |
| --- | --- |
| `layout/campfire_layout/products.py`, `bijection.py` | `nirspec_master_bkg` ProductSpec (`CLOUD_PRODUCT`, scope `obs`, suffix `_mbkg.fits`); dispatch the suffix before the bare-`.fits` fallback; mirror in `web/lib/layout.ts`; add golden rows |
| `python/campfire/deploy/discover.py` | exclude `_mbkg.fits` from `discover_spectrum_exposures`; add `discover_master_bkg_files` next to `discover_rate_files`; upload with the rate files |
| `python/campfire/db/store.py` | add the kind to `INTERMEDIATE_PRODUCT_TYPES` so `pull --intermediate` fetches it |
| web (optional, later) | render `MBKG2D` beside the rate file in the rate-exposure review view; a `nirspec_master_bkg` row or a `storage_key` column on `nirspec_rate_exposures` |
| `metadata/summary.py` | nothing in phase 1; `EXPOSURES.bkg_nods` already records `master` |

### 4.7 Versioning and changelog

- Phase 1 (derive + store, nothing subtracted): no pixel of any existing product changes → **PATCH**,
  changelog **Infrastructure** (new product kind). The layout change is a `campfire-layout` change too.
- Phase 2 (`bkg_method` / `"master"` overrides): opt-in, so existing outputs are unchanged unless a
  reducer chooses it; still **Algorithm** in the changelog (a new scientific path exists), and the
  first observation re-deployed with it carries the choice in `EXPOSURES.bkg_nods`. If the default
  ever flips to `master`, that is a **MINOR** with a Calibration/Algorithm entry.
- Phase 3 (dual `_spec.fits`): additive HDUs/columns and a `CFSCHEMA` bump; by the letter of the
  policy a FITS-schema change is MAJOR, by its spirit (readers by index keep working, nothing is
  removed) it is MINOR with an **Algorithm** entry. Decide when it is scheduled.

---

## 5. Feasibility assessment

| Component | Effort | Risk | Notes |
| --- | --- | --- | --- |
| full-metafile calibrated pass per rate file | medium | low | jwst steps called directly, no `Spec2Pipeline`; `mask_slits` shows the `assign_wcs` call pattern; `_prefetch_crds_references` for parallelism |
| shutter splitting via slit-frame y | small | low | geometry constants already in `stuck_shutters.py`; verify the 1.15 pitch mapping on a real slitlet |
| clean-shutter selection + QA table | medium | **medium-high** | the science of it; needs tuning on PRISM and gratings separately (gratings have far fewer sky photons per pixel, so `a_s` is noisy — bin coarser) |
| `jwst_backgrounds` comparison | small | low | optional dependency behind a lazy import; needs its data cache; QA only |
| expand + de-calibrate to `MBKG2D` | small | low | `expand_to_2d.bkg_for_multislit` + inverse steps, as jwst does |
| product kind, bijection, TS mirror, golden | small | low | mechanical; the `_rate.fits` precedent |
| stage 2b `master` branch + provenance | small | low | one more way to fill `bkg`; `BKGMETH` card; `restore_pre_bkgsub` already handles re-runs |
| override grammar `"master"` + auto-proposal | small–medium | low | mirrors stuck-shutter detection's TOML merge |
| deploy / pull of the intermediate | small | low | `discover.py` guard is mandatory |
| dual `_spec.fits` + web toggle | large | medium | phase 3; doubles stage 3 |

The honest unknown is scientific, not engineering: how much the few-per-cent calibration systematics
(flat, barshadow, position-dependent throughput) that the nods cancel will show up as residual sky
in master-subtracted prism spectra. Phase 1 answers that without committing any product to it:
with `MBKG1D` and the per-shutter table on disk for every exposure, the residual of each *empty*
shutter after master subtraction, compared with its residual after nod subtraction, is a direct
measurement on the archive.

---

## 6. Decisions to make

0. **Grain of the 1-D sky**: pooled over the exposure group (both detectors, all nods), recommended
   (§4.1); whether a per-exposure scale/offset is needed is a P0 measurement, not a decision.
1. **Granularity of `MBKG2D`**: full-frame image (renderable, 16 MB raw, compresses to a few MB) vs.
   per-slit `MultiSlitModel` (smaller, jwst-shaped). Recommendation: full frame; it is the only form
   that composes with any later per-source geometry and with the rate-exposure web view.
2. **Line-contaminated shutters**: mask the bins or drop the shutter. Recommendation: mask bins and
   record the wavelength range in `SHUTTERS.reason`; dropping whole shutters starves the gratings.
3. **Spatial model**: global B(λ) in v1; per-quadrant scales later if the residual-by-quadrant QA
   shows structure.
4. **Where `bkg_method` lives**: `[nirspec.stage2]` (recommended) versus reusing
   `[nirspec.stage3].method`.
5. **Auto-proposed overrides**: write `["master"]` entries with an `auto` tag (recommended, vettable)
   versus only reporting in the QA PDF.
6. **Whether phase 3 is wanted at all** before phase 2 has produced numbers.
7. **Derivation route**: decided — assemble from canonicals plus the stage 2a `bkgslits/` extraction
   (§1.7, §4.3); the full pass stays as the fallback. Open: whether to re-admit pruned detached
   shutters as synthetic slitlets, and the `source_pad` default.

## 7. Phased plan

0. **P0 — prototype on existing products** (the algorithm, not a go/no-go). A script over one
   observation's canonicals (§1.7 conversions, §4.2 steps 2–4) that settles the knobs the design leaves
   open: how many shutters are clean and why the rest are not, whether NRS1 and NRS2 agree on the
   prism, whether exposures need their own scale/offset (§4.1), the `source_pad` default, how the
   level compares with `jwst_backgrounds`, and whether solar lines are visible in the stacked G140M sky
   (§3.3). No pipeline change; its findings become the defaults of P1. **Script:**
   `pipeline/experiments/master_bkg_prototype/prototype_master_bkg.py` (README beside it).
1. **P1a — single-call stage 2a** (§1.7): one edited full metafile per rate file, split into the
   per-source canonicals and the `bkgslits/` file; bit-identity gate against the per-source runs;
   per-source fallback on failure. Pure speed-up for everything else, PATCH / Infrastructure.
   **Built (2026-10-28).** `MetaFile.rows_for_source` is the per-source edit both arrangements
   share, `MetaFile.for_sources` stacks the edits into one metafile (SOURCE_INFO kept whole,
   stellarity forced for all), `stage2._plan_rounds` keeps two sources of one slitlet (and, under
   the extended-wavelength override, fixed-slit sources) in separate calls,
   `stage2._split_spec2_result` builds each one-slit canonical with `MultiSlitModel().update(parent,
   extra_fits=True)` + `slits.extend` and promotes it through the unchanged `_finalize_canonical`,
   and `_write_bkgslits` applies `BarShadowStep` to the background model alone (the main call keeps
   `barshadow` skipped, so the `POINT` science slits are untouched whatever the stellarity setting).
   `[nirspec.stage2].single_call = false` selects the old loop; the single call also falls back to
   it for the sources it did not produce. The split routes each output slit to its source **by
   slitlet** (`_source_of_slit`), not by jwst's `source_id`: at a nod where the source's shutter was
   removed as stuck closed, the slitlet has no primary shutter left and jwst extracts it under the
   background name (`<program>_BKG<slitlet>`, `stellarity = 0`, uniform pathloss) — the per-source
   loop kept that as the source's nod product (`fix_units` then marks it `SRCFLUX = 'F'`), so the
   single call must too; only slitlets that belong to no source of the round are background
   slitlets. (The first gate run caught this: 4 of 564 references came out as `_nodata` markers,
   all nods of hand-flagged stuck shutters.) Gate on `ember_cosmos_p1` (prism + G395M, 564
   reference canonicals from the per-source loop): every data array identical; headers differ only
   in `MSAMETFL` (now the observation's real metafile, as the pointings reader already assumed),
   `ASNTABLE` and the `CFP_CAL` timestamp. Background slitlets verified on `egs4106_p6` (15 in the
   plan: 8 on NRS1 at G395M, 5 at prism — `EXTENDED`, `UNIFORM` pathloss, `MJy/sr`, bar shadow
   applied with the array stored). EMBER plans have no dedicated background slitlets at all
   (`CFNBKGSL = 0`), which is the point of §4.2's use of the source slitlets' empty shutters.
2. **P1b — derive and store.** `run_master_bkg`, the `_mbkg.fits` product, QA PDF, layout kind, deploy
   + pull as an intermediate. Wired into `run_stage2b`, unconditional. PATCH / Infrastructure.
3. **P2 — apply by choice.** `bkg_method`, the `"master"` override literal, `BKGMETH` provenance,
   auto-proposed overrides from the `SHUTTERS` table. Opt-in. Algorithm entry.
4. **P3 — measure.** A script over an observation: per empty shutter, residual sky after nod vs.
   master subtraction, by disperser and wavelength; decide defaults from it.
5. **P4 (optional) — dual product.** `MBSUB_*` on the canonical, second stage 3 pass, `_spec.fits`
   extensions after `EXPOSURES`, web toggle.

## 8. Files this would touch

- `pipeline/campfire_pipeline/nirspec/master_bkg.py` (new), `stage2.py` (P1a done: single-call
  stage 2a with the split and the per-source fallback; still to come: the `master-bkg` call site and
  the stage 2b branch), `metafile.py` (P1a done: `rows_for_source` / `for_sources` /
  `background_slitlet_ids`), `observation.py` (P1a done: `bkgslits_dir`; later the override
  grammar), `canonical.py` (`BKGMETH` card, later `MBSUB_*`),
  `cli.py` (`master-bkg` command), `plots.py` (QA), `data/config_default.toml` (P1a done:
  `single_call`, `extract_bkgslits`), `pipeline/CHANGELOG.md`,
  `pipeline/tests/test_nirspec_stage2a_single_call.py` (P1a)
- `layout/campfire_layout/products.py`, `bijection.py`, `conformance/layout_golden.json`,
  `web/lib/layout.ts`, `web/lib/layout-products.json` (P1a done: the `nirspec_bkgslits` kind,
  `products/nirspec/<obs>/bkgslits/<root>_<nrsN>_bkgslits.fits`, lifecycle regenerable, never
  cloud-backed)
- `python/campfire/deploy/discover.py`, `deploy.py` (upload), `python/campfire/db/store.py`
- later: `stage3.py`, `python/campfire/deploy/generate.py`, web spectrum plot, `supabase/schemas/`

## 9. P0 results on `ember_egs_p1` (prism, 2026-10-28)

Run with `pipeline/experiments/master_bkg_prototype/prototype_master_bkg.py` (defaults: shutter
core `--open-frac 0.2`, `--source-pad-px 3`, `--model`) over 714 canonicals, 215 sources, 3 nods ×
2 detectors; figures in `docs/figures/master-bkg-prototype/`.

**What the data say**

- **The empty shutters agree on one sky.** 847 of 943 empty shutters (90 %) are clean; every class
  (nod 1/2/3, adjacent or far from the source) has a median scale of 1.004–1.007 against the pooled sky
  with 8–12 % rejected and reduced χ² ≈ 0.8. The pooled sky has 0.2–0.6 % error per bin from ~730
  shutters. NRS1 and NRS2 agree to 1.001 ± 0.013 over the whole prism range (a ±1 % shape
  difference over 1–1.8 µm).
- **No per-exposure terms.** Each exposure × detector against the pooled sky: scales 0.998–1.002
  (errors 0.0015), offsets within ±0.5 % of the sky, the offsets alternating in sign by detector.
  The sky is stable across the three nods at the 0.2 % level; design §4.1's per-exposure
  scale/offset is not needed for this observation.
- **The bar-shadow reference is misregistered with the data by a fraction of a row.** Stacking
  ~1000 empty shutters against the within-shutter coordinate (`ember_egs_p1_profile_vs_f.png`): the
  measured profile is flat at the pooled sky for |f| ≤ 0.2 and rolls off into the bars; the reference
  has the same shape shifted by a fraction of a row, so *dividing* by it leaves +10–35 % on one side of
  every shutter and a few per cent deficit on the other. Which neighbour a bar row rounds into
  depends on sub-pixel placement, which produced a spurious 8 % excess in nod 2's middle shutters in
  the first runs (not source light: independent of source flux). Consequences: the 1-D sky needs only
  the shutter core and no bar-shadow correction at all; the 2-D expansion for subtraction (§4.2 step 5)
  must **not** trust the reference in the bar rows, where it is wrong by 10–35 % of the sky — the
  measured profile from the empty shutters is the bar model to use there (the nods never see this
  because they subtract the same pixels).
- **`jwst_backgrounds` is a shape at the 10 % level and wrong in the blue.** One global scale gives
  data/model = 0.976; by band the ratio is 0.70 at 0.6–1 µm, 1.08 at 1–3, 1.15 at 3–4, 1.06 at 4–5.4.
  Good enough for the plausibility check and a pass-0 seed, not for the level.
- **Shutter-to-shutter scatter is the systematic floor.** Per-shutter scales scatter by ±4 %
  (16/84: 0.975/1.04) with 0.6 % measurement error, so the scatter is real sky-level variation between
  shutters within one exposure. It decomposes as: a **quadrant term** of ±2.5 % (medians Q1 0.975,
  Q2 1.026, Q3 0.990, Q4 1.017 — the per-quadrant scales §4.2 left for later are needed), a
  **per-slitlet** part (the two empty shutters of one slitlet correlate at 0.53), and a **per-shutter,
  per-exposure** remainder of ~2.7 % that is only 40 % repeatable for the same physical shutter in
  another nod. The 10 % "rejected" are mostly the tail of this distribution, not neighbours.

**What it means for the design**

- The nod subtraction cancels all of this exactly, pixel for pixel. An ensemble sky expanded onto a
  source shutter carries the quadrant term (removable), the slitlet term (partly removable from the
  slitlet's own empty shutters) and a ~2–3 % irreducible per-shutter floor. On the prism the per-pixel
  noise of one exposure is ~20 % of the sky, so the floor is subdominant per pixel but reaches the
  noise of a 15-pixel extraction (3 nods × 5 rows) and dominates for broad continuum bins — this is the
  quantitative form of RUBIES' "nods do better for compact faint sources".
- Decision (2026-10-28): **no hybrid.** Scaling the ensemble sky per shutter from the nods is a third
  method (a local background) and offers nothing over nodding where nodding works. The master
  background exists for the case nodding cannot serve: **extended sources whose slitlet has no clean
  sky of its own**. There the ~3 % per-shutter floor is an accepted cost, carried as an error floor on
  the expanded background (msaexp's `(0.03 · sky)²` term is the same number), and the quadrant term
  is removed with per-quadrant scales. This is a secondary product, not the archive default.
- Settled knobs for P1: shutter core |f| ≤ 0.2 (no bar-shadow division for the 1-D sky), source pad
  3 px (5 px leaves only one row per adjacent shutter), per-quadrant scales on, no per-exposure terms,
  median seed (the model seed needs the same coarse-bin scaling and is 30 % off below 1 µm), rejection
  at 5 % above the ensemble catches the few real neighbours (scales > 1.2) and the distribution's tail
  alike — a per-quadrant reference will tighten it.
- **Empirical bar shadow as a by-product (proposed).** The same per-pixel samples (surface brightness
  without the bar-shadow division, within-shutter coordinate, wavelength, shutter position in the
  slitlet, quadrant) stacked in (f, λ) per disperser and shutter geometry are an empirical bar-shadow
  model — what msaexp ships as `prism_{1,2,3}_bar_coeffs_wave.yaml` (a B-spline in shutter y with
  wavelength-dependent coefficients, plus a per-slit y offset fitted from the data, `bar_dy='auto'`),
  applied after undoing jwst's. One prism observation gives ~2 M empty-shutter pixels, enough for a
  (f, λ) table at prism resolution; the gratings need grating canonicals. First question to settle:
  is the jwst reference only *shifted* (a y offset of ~0.05 shutter ≈ 0.25 px, fixable inside our own
  evaluation of the reference) or wrong in shape. Measuring it belongs in the master-background stage
  (free); *applying* it to the science calibration is a separate Calibration change (MINOR) that
  touches only extended-source slits (stage 2a never applies bar shadow to point sources) and the
  master background's own 2-D expansion.
- **Is the reference just offset? Tested (`diagnostics/barshadow_offset.py`,
  `ember_egs_p1_barshadow_registration.png`): no.** Evaluating the CRDS reference exactly as jwst's
  `_calc_correction` does but at `y + dy`, a shift of −0.02 to −0.03 pitch (−0.1 to −0.15 px) halves
  the mismatch over the open area (rms 8.0 % → 4.7 %), and that best shift is the same on both
  detectors, in all four quadrants, at all wavelengths and for end and middle shutters — so the
  registration part is one fixed offset. A depth change does not help, and pixel-integrating the
  reference makes it worse (the measured edges are *sharper* than the model's). Adding a 4 % wider
  open area brings the rms to 4.0 %, with the bar rows at ±10 % instead of ±30 %. What remains is
  asymmetric about the source: the two sides want different shifts (−0.04 pitch for the shutters
  below the source, −0.01 above), and in the rows next to the bars the ratio data/reference is 1.22 on
  the source side and 0.89 on the far side of the shutter below the source, 0.98 / 1.07 above it — the
  same for sources fainter than 0.02 µJy and between 0.02 and 0.1 µJy, so not source light (only
  sources above 0.1 µJy add to it). The shutter profiles the pixels actually see are displaced
  toward +y relative to the reference by an amount that differs with the side of the source, which
  points at the slit-frame y mapping (fiducial placement or pitch) rather than at the shadow shape.
  Consequence: a shift-and-width tweak rescues the reference only to 4 %; an empirical profile
  stacked in the pipeline's own slit-frame coordinates absorbs whatever the mapping does, which is
  the stronger argument for building it, and for turning bar shadow back on only once that table
  flattens held-out empty shutters to ~1 %.
- **Second field, and the first grating (`ember_cosmos_p1`, PRISM + G395M, 70 of 446 sources
  through stage 2a; 2026-10-28).** The prism replicates `ember_egs_p1` in every number: 444 of 483
  shutters clean (92 %), per-exposure scales 0.988–1.004, NRS1/NRS2 0.994 ± 0.013, the sky 0.18–0.4
  MJy/sr at 1–2 µm (COSMOS is nearer the ecliptic), data/model 0.93, the same bar-row pattern
  (edge toward the source below it 1.21, far edge 0.88, above it 0.96 / 1.05) and the same best shift
  (−0.03 pitch = −0.16 px, rms 7.6 % → 3.0 %). Two lessons from the grating:
  - **The overlap map must come from the whole exposure, not from the processed canonicals.** On
    G395M, 1.58 M of the 4.2 M pixels of each detector lie under more than one slitlet footprint
    (13 k for the prism): the 750–1400 px traces of 446 slitlets abut with no gap
    (`ember_cosmos_p1_g395m_rate_frame.png`). A counter built from the 70 processed slitlets let the
    other 376 slitlets' light pass for sky, and the "empty" shutters of one slitlet then differed by
    factors of 5–10 with smooth gradients across the bars. The prototype now builds the counter with
    `assign_wcs` over every open slitlet plus the failed-open shutters (stage 1's `mask_slits`, kept
    as a count), cached per rate file — the production stage must do the same.
  - **With that map the grating sky agrees with the prism sky in the same field to ~5 %** (3–3.5 µm:
    0.190 vs 0.182 MJy/sr; 4–4.5: 0.429 vs 0.433; 4.5–5: 0.91 vs 0.86; 5–5.4: 1.40 vs 1.49), a
    cross-check of the flat / pathloss / photom chain between dispersers. But it is a statistics
    problem: after the overlap cut only 162 shutters survive from 70 sources, 66 clean, with 10–14 %
    error per 2-pixel bin at 3–4 µm; the full 446 sources would give ~100 clean shutters per
    exposure, and the grating sky is a smooth thermal continuum, so a low-order model is enough.
  - **No registration offset is visible at G395M** (4.3–5.3 µm, 175 k pixels): best shift +0.01 pitch,
    rms 9.0 % → 8.4 % (noise-limited), and the stacked profile divided by the reference is flat to
    ±10 % with none of the prism's ±35 % spikes (`ember_cosmos_p1_g395m_profile_vs_f.png`). Since the
    prism shows the same −0.15 px shift at 3.5–5 µm as at 1–2 µm, the misregistration is a property of
    the **prism's** slit-frame mapping, not of wavelength. Bar shadow may well be fine to switch back
    on for the gratings as it stands; for the prism it needs either the empirical table or a fix in
    the prism WCS y zero point.
- Not yet measured: a full-source grating observation (S/N for the bar profile), the bar profile as a
  function of wavelength and quadrant, and the solar-line check (§3.3).
