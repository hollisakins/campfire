# CAMPFIRE Pipeline Changelog

All notable changes to `campfire-pipeline` are recorded here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/), categorized
by scientific impact:

- **Calibration** — changes that alter pixel/flux values for the same input
  (CRDS context bumps, `jwst` upgrades, calibration defaults, reference data).
  Triggers a **MINOR** version bump.
- **Algorithm** — changes that alter output structure or behavior, not
  necessarily values. Triggers **MINOR** (additive) or **MAJOR** (breaking)
  depending on backwards compatibility.
- **Infrastructure** — no scientific impact (CLI ergonomics, plots, perf,
  internal refactors, tests). Triggers a **PATCH** bump.

Versions are git tags of the form `pipeline-vX.Y.Z` (resolved by
`setuptools-scm`). Install a specific release — `campfire-layout` is a sibling
package in the same repository that isn't on PyPI, so install it alongside:

```
pip install \
  "campfire-layout @ git+https://github.com/hollisakins/campfire.git@pipeline-vX.Y.Z#subdirectory=layout" \
  "campfire-pipeline @ git+https://github.com/hollisakins/campfire.git@pipeline-vX.Y.Z#subdirectory=pipeline"
```

Release procedure: edit the `## Unreleased` section below, then run
`scripts/release-pipeline.sh X.Y.Z` (or the `/pipeline-release` slash command).

## Unreleased

### Calibration
- NIRCam wisp subtraction now defaults to the multi-component non-negative
  matrix factorization model of Wu et al. 2026 (JADES DR5, arXiv:2601.15958),
  via the new `nmfwisp` dependency (templates ship in the wheel). Per-exposure
  wisps are fit as a non-negative linear combination of filter/detector-specific
  components (NNLS, inverse-variance weighted, sources masked) rather than a
  single scaled STScI template, capturing exposure-to-exposure morphology.
  Controlled by `[nircam.wisp].method` (`"nmf"` default, `"template"` for the
  legacy path); `"nmf"` falls back to the template method for any
  `(detector, filter)` nmfwisp ships no template for (e.g. F140M, F162M). The
  method used is recorded per exposure in `CFP_WISP` (`nmf <version>`). Changes
  wisp-region pixel values for the same input. The shared source mask used by
  the fit (both methods) now grows source footprints — `[nircam.wisp].mask_nsigma`
  (default 3) and `mask_dilate` (default 8, binary-dilation iterations) — so
  faint source wings past the detection isophote aren't fit as wisp flux; an
  nsigma x dilate sweep showed the old (5.5-sigma, no-dilate) mask inflated the
  fitted wisp amplitude by ~20% where a bright source overlapped the wisp region.
- Two-fit 2-D background architecture in the unified `bkg` step (adapted
  from R. Endsley's cluster reduction; validated on synthetic scenes +
  rj0911 F444W). (1) A **conditioning detrend** (`[nircam.bkg.detrend]`,
  on by default): a fit-only, zero-median coarse-box `Background2D` model
  subtracted from the measurement copies so the pedestal and per-amp-row
  1/f terms never see sky gradients or diffuse scattered-light structure —
  which they otherwise absorb per-amp-asymmetrically, imprinting seams at
  the amp boundaries (the real-exposure flatness sweep's headline finding).
  Never subtracted from SCI. (2) An opt-in **applied** subtraction
  (`[nircam.bkg].subtract_2d`, per-field — lensing clusters): a smooth
  `Background2D` (`[nircam.bkg.bkg2d]`: box 64 px SW ≈ 2", source tiers
  grown by `extra_dilate = 20`, background-map outlier reject via the new
  `SubtractBackground.bg_reject*` guard) for sky-matching / ICL removal,
  with parameters set by flux conservation (zero median aperture loss in
  the synthetic cluster sweep). Pedestal `scope = "auto"` resolves to the
  incumbent per-amp (safe under gradients once conditioned); a full-frame
  fallback covers the detrend-off escape hatch. **Calibration (MINOR): the
  default-on conditioning detrend changes 1/f/pedestal solutions (and hence
  pixel values) for all NIRCam exposures**, materially where frames carry
  gradients or diffuse artifacts. The applied subtraction stays opt-in
  (default `subtract_2d = false`), and the mosaic path defaults
  `bg_reject = false` (wired under `[nircam.resample]` with the tile
  config-hash extended only when enabled, so existing tiles keep their
  hash). Validated by a synthetic harness
  (`experiments/bkg2d_synthetic/`): layered truth scenes (galaxies vs ICL
  as separate planes) driven through the real `bkg_step`, with
  aperture-to-aperture flux-conservation metrics on the correction-error
  map, plus a real-exposure amp-seam flatness sweep.

### Algorithm
- Waterfall growth of large OUTLIER DQ regions (opt-in,
  `[nircam.outlier].grow_large_regions`, default off). Detector-fixed
  artifacts (scattered-light arcs/glints) move on-sky between dithers, so
  outlier detection flags their bright cores while the sub-threshold wings
  drizzle into the mosaic. Connected OUTLIER components above
  `grow_min_area` seed a hysteresis expansion through connected pixels of
  the smoothed residual above `grow_expand_nsigma`·σ (SExtractor-isophote
  style, following the artifact's actual morphology), with a
  `grow_max_factor` growth cap that falls back to plain dilation when a
  seed floods a bright star's own PSF halo. Cosmic-ray hits and
  galaxy-core speckles are untouched by construction (rj0911 F444W: ~5
  large vs ~13000 small components per frame; <1% of frame masked; the
  mosaic A/B shows arc contributions removed at their per-dither sky
  positions with only local one-dither depth cost). Wired in both outlier
  implementations; growth stats stamped into `CFP_OUT`. Additive: default
  off leaves existing outputs unchanged.

### Infrastructure
- Dependency declarations now match actual imports (#330): added `requests`,
  `h5py`, and `Pillow` (previously satisfied only transitively) plus the
  directly-imported JWST-stack packages (`crds`, `asdf`, `stdatamodels`,
  `stcal`, `drizzle`, `gwcs` — versions still ride the `jwst` pin); dropped the
  never-imported `tomlkit`. No change to resolved environments in practice —
  every added package was already installed via `jwst`. Ships alongside the new
  repo-root `install.py` interactive installer.
- NIRCam expmap plots gain tile footprints + a squared, in-ticked panel: the
  per-filter maps (`expmap_*.png`/`.pdf`) now overlay the same tile outlines as
  `<field>_layout.png`, so a single filter's coverage reads against the tile
  grid; the main panel of every expmap/layout plot is forced square (predictable
  web layout, WCS aspect preserved so a non-square field pads rather than
  stretches), its tick marks point inward (colorbar unchanged), and the imshow
  sits behind the axes so grid, ticks, and outlines render on top. Plots only;
  no change to FITS pixel values or filenames (`nircam/expmap.py`).
- NIRCam mosaics now get a size-capped **thumbnail pair** instead of one
  fixed-/4-downsampled PNG: `<base>_thumb.png` (long side ≤
  `thumbnail_max_dim`, default 500 px — the web table rendition) and a new
  `<base>_quicklook.png` (long side ≤ `quicklook_max_dim`, default 4096 px —
  the click-to-enlarge popup). The fixed /4 factor scaled with the mosaic (a
  wide 30 mas strip produced a ~10k-px, tens-of-MB "thumbnail"); the caps
  bound both renditions regardless of field geometry, and mosaics smaller
  than a cap are saved at native size. Named `quicklook` (not
  `_preview`/`_full`) because those suffixes already mean the per-exposure
  triage PNGs in the same directory. Display PNGs only, no change to FITS
  pixel values (`nircam/steps/_plots.py`, `nircam/steps/resample.py`,
  `data/config_default.toml`; layout contract + deploy + `storage_objects`
  CHECK gain the `nircam_mosaic_quicklook` product type).
- NIRCam expmap/layout plot styling: the deployable dark PNGs (per-filter
  `expmap_*.png` and `<field>_layout.png`) no longer carry a title — the web
  page embedding them already labels field/filter, so it was duplicated
  information. The light per-filter PDF keeps its title but drops the
  `canonical` stage label (the fiducial map is undecorated, matching the
  filename convention; `uncal` quick-looks still label their stage). Tile
  outlines and labels on the layout plot are heavier/larger and stroked with
  the background colour so they stay legible over both the bright and dark
  ends of the colormap. Plots only; no change to FITS pixel values or
  filenames (`nircam/expmap.py`).
- NIRCam per-exposure triage previews (`{rootname}_preview.png` /
  `{rootname}_full.png`) now render per-pixel **SNR** (`SCI/ERR`) instead of raw
  SCI (still ZScale-stretched). The `jump` step drops snowball/cosmic-ray groups
  from the ramp fit, inflating `VAR_RNOISE`→`ERR` on those pixels, so an SNR view
  sinks a correctly error-weighted snowball back into the noise floor while
  leaving residuals the error model under-weights visibly significant —
  surfacing the pixels that actually warrant a hand mask rather than every
  bright-but-already-downweighted artifact. Quick-look only; no change to FITS
  pixel values, DQ, or the `_preview.png`/`_full.png` filenames the web mask
  editor consumes. Existing reductions upgrade automatically: `CFP_PREV` now
  records a render-format marker (`snr`) and the skip check requires it, so a
  normal `cfpipe nircam process` re-renders exposures whose stamp predates this
  change without needing `--overwrite` (`nircam/steps/preview.py`,
  `data/config_default.toml`).
- NIRCam fields gain an optional field-level `fiducial_tiles = ["A1", "A2", …]`
  declaration in `fields.toml` — the subset of a field's tiles that share a
  tangent point + rotation and span the field, forming the FitsGL field-composite
  map view (epic #337). `Field.load` parses and validates the names against the
  declared tiles, and a new `Field.fiducial_tile_set()` returns the set (honoring a
  per-tile `fiducial = true` fallback) after asserting the tiles co-grid (shared
  `crval`/rotation), so an off-grid tile mistakenly included fails fast. Additive
  and opt-in — fields without the key are unchanged; no effect on pixel values
  (`nircam/field.py`).
- Install docs: `pip install` instructions now pull the unpublished
  `campfire-layout` sibling package from the monorepo alongside the pipeline, so
  a fresh `pip install "git+…#subdirectory=pipeline"` (and `conda env create`)
  resolves instead of failing on the missing `campfire-layout` dependency. The
  post-release "Install with" hint printed by `scripts/release-pipeline.sh` is
  fixed the same way. Docs only — no code or output change (`CHANGELOG.md`,
  `README.md`, `environment.yml`, `scripts/release-pipeline.sh`).
- NIRCam exposure maps: the fiducial (`canonical`-stage) per-filter map is now
  written with an **undecorated filename** — `expmap_<field>_<filter>.fits`
  (previously `expmap_<field>_<filter>_canonical.fits`) — so it presents to users
  simply as *the* exposure map rather than one stage among several. The
  reducer-only `uncal` quick-look keeps its explicit `_uncal` suffix (and its
  matching PDF / `footprints.reg` follow the same rule), so the two never collide.
  The FITS `STAGE` header keyword is unchanged (kept as provenance). `cfpipe nircam
  expmap` and its `--stage` options are otherwise unchanged. Deploy ships only the
  fiducial map (the `_uncal`/legacy `_canonical` variants are skipped) and it is now
  surfaced for download on the web NIRCam page. Pure output-file naming change with
  no effect on pixel values (`nircam/expmap.py`).
- NIRCam exposure maps now emit web-ready plots + a coverage summary (for the
  NIRCam page redesign). Each per-filter map gains a **dark PNG**
  (`expmap_<field>_<filter>[_uncal].png`) beside the existing light PDF —
  rendered on a dark plot well to match CAMPFIRE's data surfaces (map/spectrum
  wells stay dark in both app themes); the PDF is kept as the local diagnostic.
  A new **field layout** plot (`<field>_layout[_uncal].png`) stacks every
  per-filter expmap (total exposure across filters) with tile-footprint
  outlines + axes + colorbar so it stands alone as a coverage/tiling product,
  and a companion `<field>_layout.json` records the **exact survey area**
  (non-zero pixels of the stack × pixel area) + per-filter areas for the
  deploy/DB layer. Each per-filter expmap FITS also gains an `AREA` header
  (covered arcmin²). The layout + coverage summary is written only on full-field
  runs: a `cfpipe nircam expmap --filters <subset>` rerun skips it so a partial
  stack never overwrites the complete survey area, and a cached expmap FITS
  predating the `AREA` header has its per-filter area recomputed from the array
  rather than reported as zero. Plots + metadata only — no change to expmap pixel
  values (`nircam/expmap.py`, `nircam/cli.py`).
- NIRCam wisp templates are now fetched from a public HTTPS host into
  `$CAMPFIRE_ROOT/cache/wisps/` against a checksummed manifest shipped with the
  package (`data/wisp_manifest.toml`), instead of being manually copied into the
  user-supplied `reference/nircam/shared/wisps/` tree. A single-process preflight
  in `run_process` warms every needed template before the parallel fan-out
  (`nircam/wisp_cache.py`, `orchestrate._prefetch_wisp_templates`), downloads are
  sha256-verified and written atomically, and the fetch path is fully independent
  of the campfire CLI/auth (plain `urllib`, no login, no cloud credentials). The
  step now resolves templates cache→legacy-dir→fetch, so machines that already
  have templates in the legacy dir are unaffected. Manifests are (re)generated
  with `scripts/build_wisp_manifest.py`; hosting is documented in
  `WISP_TEMPLATE_HOSTING.md`.
  **Behavioral change / categorization note:** the wisp step no longer *silently*
  skips when a template is absent. A `(detector, filter)` the manifest lists but
  that can't be found or fetched is now a hard error; one the manifest does not
  list stamps `CFP_WISP='skipped (no template)'` (visible, not silent). This
  fixes the failure mode where a machine missing templates produced mosaics with
  no wisp subtraction and no record of it. Output on a correctly-provisioned
  machine is unchanged (Infrastructure/PATCH); a machine that was previously
  *silently* skipping will now subtract (different pixels) or fail — a releaser
  may judge that worth escalating to Calibration/MINOR.

### Calibration
- **NIRCam `align` refcat gains an epoch / proper-motion contract** and
  propagates reference positions to each exposure's mid-time. The refcat schema
  grows optional columns — `source_id, ref_epoch, pmra, pmdec, parallax` (+
  `pmra_err`/`pmdec_err`) — alongside the required `RA/DEC/mag/mag_err`;
  `query_gaia` now populates them (Gaia DR3), while galaxy backends (LS/HSC) omit
  them. When a refcat carries proper motions, the align worker moves each star to
  the exposure mid-time (`EXPMID`) via `astropy` `apply_space_motion` **before**
  the footprint clip and match (`refcat/motion.py`), so a fast Gaia star is
  matched where it actually was, not where the catalog recorded it years earlier.
  **Fully backward-compatible**: a catalog without the columns — or a row with a
  non-finite proper motion — is treated as stationary (the prior zero-motion
  behavior), so today's galaxy-anchored refcats are a strict no-op. This changes
  the fitted WCS only when a motion-bearing catalog is used. `refcat/{io,query,
  motion}.py`, `align/apply.py`; covered by `tests/test_refcat_motion.py`.
- NIRCam `apply_masks` now reads web-defined masks instead of crashing on them.
  Masks drawn in the web editor materialize (via `campfire deploy pull-masks`)
  as DS9 **image**-coordinate `.reg` files, which `Regions.read` parses back as
  `PolygonPixelRegion`s. The step called `reg.to_pixel(wcs)` unconditionally —
  a method only sky regions have — so every web-defined mask aborted the combine
  phase with `AttributeError: 'PolygonPixelRegion' object has no attribute
  'to_pixel'`. Pixel regions are now rasterized directly and only sky regions
  (legacy FK5/ICRS hand-drawn masks) are projected through the exposure WCS,
  matching the guard the deploy-side `import-masks` already used. Regions whose
  footprint falls entirely off the frame (`to_image` → `None`) are now skipped
  rather than crashing on `None.astype`. Net effect: web-defined masks reach the
  mosaic (excluding masked pixels) where they previously produced no output.
- NIRCam manual masks now actually reach the mosaic, and uncovered pixels are
  `NaN` (epic #261, N7). The `apply_mask` step painted user region masks as DQ
  bit `1024` (`DEAD`), which `good_bits='~DO_NOT_USE'` **ignores** — so a mask
  had no effect on the mosaic unless the (default-off) `mask_set_nan` knob was
  set. Masks are now honored via `DO_NOT_USE` (fused onto the combine working
  copy), so masked pixels are correctly dropped from outlier detection and
  resample. Separately, both drizzle backends now use `fillval='NaN'`, so mosaic
  pixels with no coverage — or masked in every overlapping exposure — read as
  `NaN` instead of `0`, retiring the post-drizzle "SCI=NaN where WHT=0" pass
  (`bkgsub` is NaN-safe, so covered pixels are unchanged). Both change mosaic
  pixel values. The `[nircam.apply_mask]` `mask_flag` / `mask_set_nan` config
  knobs are removed — the mask now lives purely in the DQ contract.
- NIRCam `striping` now runs **after** `image2`, on flat-fielded, flux-
  calibrated cal-stage data, and fits *and applies* the 1/f correction in that
  same frame. Process order is now detector1 → persistence → wisp → image2 →
  striping → edge → sky → … Previously striping fit on a flat-fielded *copy*
  but subtracted the correction from the *un-flat* rate SCI, which image2 then
  re-divided by the per-amp-structured flat — leaving a coherent per-amp DC
  step at the amplifier boundaries (`≈ N/g·(1−1/g)`, a ~10–30σ residual
  amp-to-amp offset in the column background, verified to reproduce at
  r=0.9997). Fitting and subtracting in the cal frame removes that leak.
  Consequences: striping measures its pedestal with the scale-free `fit_sky_tot`
  Gaussian sky-peak fit rather than the rate-tuned `fit_pedestal`;
  `[nircam.striping]` no longer takes `apply_flat` / `use_custom_flat` and no
  longer resolves a flat (dropped from CRDS prefetch). `subtract_background` is
  now a **fit-only** 2D detrend (default **on**, `box=32` / `filter=3`): it
  removes the field's large-scale structure (e.g. cluster ICL / scattered
  light) from the working copy so the per-amp-row/per-column medians estimate
  the 1/f rather than the background, but the model is **never** subtracted from
  the output SCI — so it cannot leave negative wings around sources (unlike the
  fine-box mosaic bkgsub) and the ICL is retained for mosaic-level removal.
  Without it, a smooth gradient that per-amp-row constants cannot represent gets
  imprinted as amp-boundary steps. `wisp` is unchanged (still rate-frame); its
  analogous flat round-trip is a separate, SW-only follow-up.

- Opt-in extended-wavelength reduction for G140M/F100LP and G235M/F170LP
  (`[nirspec.stage2].extend_g140m_g235m`, default off). The F100LP/F170LP
  long-pass filters pass light redward of the nominal grating cutoffs; when
  enabled, spec2 extracts the redder 1st-order light out to 5.3 um. Ships a
  SPURS-derived calibrated `photom` reference (`extended_jwst_nirspec_photom_0015.fits`,
  PR #163) and generates extended `fflat`/`sflat`/`wavelengthrange` references on
  the fly (cached under `$CAMPFIRE_ROOT/cache`). Stage 1's background-subtraction
  mask auto-widens for these gratings so the extended-order flux is not subtracted
  as background; the R-curve is linearly extrapolated past the tabulated range for
  redshift fitting. A CRDS-compatibility guard refuses to run if the active context
  resolves the flats/photom to versions newer than the calibration baseline
  (re-derive with `data/Generate_Extended_Cals.py`). Only those two grating/filter
  combos are affected; every other config is unchanged.
- Extended-wavelength reduction now handles NIRSpec fixed-slit sources, which
  require a separately-calibrated `photom` reference
  (`extended_jwst_nirspec_photom_0014.fits`) than MSA sources (v0015). Fixed-slit
  sources are detected per-source from the `fixed_slit` column of the MSA metafile
  in `run_stage2a_single_rate`, the correct extended photom is selected, and the
  status is recorded in the product header (`CFFXSLT`). Previously the extended
  feature failed for fixed-slit sources.

### Algorithm
- **NIRCam `striping` + `sky` + `variance` are unified into one `bkg` step**
  (`CFP_BKG`), replacing three per-exposure steps and their two independent
  source maskers with one. Process order is now detector1 → persistence → wisp →
  image2 → **edge → bkg** → diag_striping → … (`edge` moved before `bkg` so edge
  DQ feeds the mask). The step builds a single source mask via
  `SubtractBackground` (mask only — **no** 2-D background subtraction; the
  astrophysical sky is left for the mosaic) at mosaic-like depth with per-channel
  pixel-scaling (LW ×0.5), then runs an iterative per-amp chain — **per-amp
  pedestal → column median → amp-row GP ρ≈5 → amp-row GP ρ≈20** — that removes
  the per-amp DC steps and the amp-*dependent* ~100 px banding the old chain left
  behind (the striping fit-only 2-D background, which was restored into the
  output, is dropped). The per-amp pedestal owns the per-exposure DC, preserving
  the no-skymatch invariant (masked-background median ≈ 0 per exposure). Pixel
  and flux values change; `VAR_RNOISE` rescaling is folded in and now uses the
  shared, deeper mask (correction factor shifts slightly). New `[nircam.bkg]`
  config replaces `[nircam.striping]` / `[nircam.sky]` / `[nircam.variance]`.
  Provenance keys `CFP_1F` / `CFP_SKY` / `CFP_VAR` are retired in favor of
  `CFP_BKG` (deploy stage tracking + the `nircam_reduction_progress` view and web
  columns updated in lockstep). `diag_striping` now rebuilds its source mask via
  `SubtractBackground`. The shared 1/f/pedestal/variance numerics move to a new
  `campfire_pipeline.nircam.oneoverf` module. See
  `docs/design-nircam-unified-background.md`. (Note: the offline research scripts
  under `pipeline/experiments/oneoverf_gp/` still import the retired striping
  internals and need updating before they run again.)
- **NIRCam epoch mosaics** — `cfpipe nircam combine`/`resample`/`run`/`check` take
  a new `--epoch <name>` flag that builds a mosaic from a *subset* of a field's
  exposures (e.g. one program or one observing season), **additive and default-off**
  so a run without `--epoch` is byte-for-byte unchanged. Epochs are defined per
  field in fields.toml under `[<field>.epochs.<name>]` by an optional `files` glob
  list and/or an inclusive `date_range` (matched against `DATE-OBS`); the subset
  scopes the *whole* combine phase (apply_mask → bad_pixel → outlier → resample,
  like `--tiles`), so the epoch mosaic is a distinct reduction from only those
  exposures. The epoch name is appended as a trailing filename segment
  (`mosaic_nircam_<filter>_<field>_<scale>_<tile>_<epoch>_i2d.fits`), stamped as
  `CFEPOCH` in the mosaic header, and recorded in the manifest; deploy indexes
  epoch mosaics on the portal as a new `epoch` axis (`nircam_images.epoch`, `''` =
  full field) alongside full-field mosaics. Category is Algorithm (new science
  product / naming), though it's arguably Infrastructure since it changes no
  existing output.
- **NIRCam field-level astrometric `align` phase is now runnable** (`cfpipe nircam
  align` / `run --align`), **opt-in and default-off** so existing reductions are
  unchanged. When a field sets `[<field>.align].enabled = true` (and names its
  Gaia-tied `campfire-refcat-v1` catalog via `[<field>.align].refcat`), a bespoke
  field-level phase runs between `process` and `combine`: it groups all detectors of
  each exposure across the SW+LW filter dirs, triangle-matches a pooled source catalog
  to the reference (no prior on the offset), fits one shared shift+rotation per exposure
  via `tweakwcs` (`fitgeom='rshift'`, SIAF distortion fixed) with an adaptive
  per-detector shift, and writes the corrected gwcs back with a `CFP_ALGN` stamp
  (or a `NOT_ALIGNED` sentinel). The process phase then skips `jhat`+`wcs_shift` for that
  field — exactly one alignment path. Replaces the JHAT-based alignment for opt-in
  fields; JHAT remains the default and coexists during validation. New `[nircam.align]`
  config block; covered by `tests/test_align_run.py`.
- **NIRCam field-level `align` phase — matcher / solve / detection / combine
  hardening (S1–S3a).** Opt-in and default-off, so no existing (JHAT) reduction
  changes; for align-enabled fields it changes which sources match and thus the
  fitted WCS. One PR, four parts:
    - *Refcat footprint + all-source refine.* The full field refcat (~550k rows
      for COSMOS) used to reach the matcher, so the brightest-N vertex cap kept
      globally-brightest, mostly off-frame sources — and that same cap gated the
      final fit (~17–30 pairs). The shared solve now clips the refcat to the
      exposure's detector-union footprint + `ref_border_arcmin` margin (default
      0.5′, local gnomonic tangent plane, `align/footprint.py`), runs
      `TriangleMatch` as a bounded **bootstrap** only (`brightest` →
      `bootstrap_max`), then refines on **all** sources with a one-to-one
      `tweakwcs.XYXYMatch` pass (`fitgeom='rshift'`, σ-clip, `refine_niter`, no
      2-D-histogram re-acquisition) — the fit rests on every matched source, not
      the capped vertices. Per-detector residuals and the reject gate use
      mutual-NN (one-to-one) matching; the translation-invariant matcher still
      drives the adaptive per-detector refit (recovers offsets up to
      `match_radius`). Also folds in the earlier pooled-catalog starvation fix
      (the `create_group_catalog` `mag`-strip bug): the bootstrap cap spreads
      vertices evenly across the pooled catalog when the ranking column is absent.
    - *Quality-selected detection.* `detect.py` adds a peak-SNR floor (`snr_min`)
      and an (uncalibrated) DAO-magnitude range trim (`objmag_lim`), masks
      `SATURATED` + `NO_LIN_CORR` DQ (not only `DO_NOT_USE`), keys the detection
      PSF FWHM per filter (`psf_fwhm_by_filter`, F070W→F480M), and drops the fixed
      `brightest` count cap so the whole quality-cut catalog reaches the refine.
    - *`NOT_ALIGNED` is never silent.* An exposure the solve can't tie to the
      reference — **or any solver/refine/detection exception, which now degrades
      instead of crashing the align worker** — is stamped `CFP_ALGN = NOT_ALIGNED`
      (raw WCS preserved) and quarantined from combine by `Field.materialize_work`
      (the single ensemble gate; the outlier pre-scan now also re-runs a visit
      whose membership shrank, so surviving frames don't reuse CR masks computed
      with the dropped exposure). For an align-enabled field, combine likewise
      quarantines exposures carrying **no** `CFP_ALGN` at all (align enabled but
      never solved them → raw WCS), each surfaced with its own fix rather than
      silently drizzled. `run_align` reports every failure in a loud
      end-of-command banner (failed files + how to fix) and **re-attempts**
      `NOT_ALIGNED` exposures on a normal re-run (no `--overwrite`), while
      already-solved exposures are skipped. `--include-unaligned` forces
      inclusion. Sentinel centralized as `cfp.NOT_ALIGNED`; new `cfp.step_value`.
  New `[nircam.align]` knobs (`ref_border_arcmin`, `bootstrap_max`,
  `refine_searchrad`/`refine_tolerance`/`refine_niter`, `snr_min`,
  `psf_fwhm_by_filter`); `brightest` removed. Touches
  `align/{footprint,solve,matcher,detect,apply}.py`, `field.py`, `orchestrate.py`,
  `cli.py`, `common/cfp.py`; covered by `tests/test_align_*`,
  `tests/test_nircam_work_tree.py`, `tests/test_cfp.py`.
- NIRCam `combine` now honors per-exposure reviewer exclusions (epic #261, N6 /
  D10). `Field.setup_workspace` reads `reference/<field>/exposures.json`
  (materialized by `campfire deploy nircam pull` from the portal's
  `review_status='excluded'` flags) and folds the listed rootnames into the skip
  set in `get_exposure_files` / `get_uncal_files` — so a flagged exposure drops
  from **both** resample and outlier detection (it can no longer pollute the
  outlier median for its visit-mates). Additive + reversible: an absent or empty
  file is exactly today's behavior, and un-excluding in the portal + re-pulling
  re-includes the exposure on the next combine. No change to any exposure's
  pixel values.
- **NIRCam combine no longer mutates the canonical per-exposure FITS** (epic
  #261, N7). `bad_pixel`, `outlier`, and `resample` now run on disposable
  working copies under `products/nircam_work/<field>/<filter>/`, materialized
  from the frozen canonical by `Field.materialize_work` (copy where stale + fuse
  `CFMASK` → `DO_NOT_USE`). Only `apply_mask` still writes the canonical, and
  only its `CFMASK` extension — the canonical's SCI/DQ stay byte-identical to the
  process-phase output. This is what lets a mosaic re-deploy leave the pristine
  exposure in OSN untouched (instead of overwriting it with outlier-rejected
  bytes) and lets a restored exposure re-combine from a clean input. The working
  tree is local-only and never deployed; combine stays incremental because the
  working copies retain their `CFP_OUT` stamps across runs (re-copied only when
  the canonical is re-processed or its mask changes). `campfire deploy`
  additionally hard-refuses to upload a canonical carrying combine-phase CFP
  stamps (`CFP_BPIX`/`CFP_OUT`) — a field reduced by the old in-place combine
  must be re-run through `cfpipe nircam process` before it can deploy.
- **BREAKING (MAJOR):** the NIRCam mosaic `version` axis is retired (epic #261,
  N2 / D3). Mosaic products are now named `mosaic_nircam_<filter>_<field>_<scale>_<tile>_<ext>.fits`
  with **no** `_<version>_` segment — one canonical mosaic per
  `(field, filter, tile, pixel_scale, extension)`, overwritten in place on
  re-combine. The `[nircam.resample].version` config key and the `--version`
  option on `cfpipe nircam refcat extract` are removed; the `_latest_` symlink
  farm is gone (the canonical name *is* the latest). The `version` key is dropped
  from mosaic manifests. **Consequence:** existing `..._v0_1_..._i2d.fits` mosaics
  become orphaned vs the new name, so the first post-upgrade `combine` rebuilds
  every tile fresh — intended (the portal re-serves mosaics from OSN per field/
  filter as they are re-reduced; see #261 N3). Readers (`rgb`, `refcat`) resolve
  the direct version-free name and intentionally do **not** fall back to a stale
  versioned/`_latest_` file.
- NIRSpec optimal extraction no longer bounds the cross-dispersion profile to
  the nominal aperture for fixed-slit sources. The aperture bounding in
  `optext_profile` exists to keep the profile from picking up flux from
  neighbouring shutters across the bars in MSA slitlets; NIRSpec fixed slits
  have no such bars, so the full cross-dispersion cut is a valid spatial
  profile. Fixed-slit sources (detected from the `fixed_slit` column of the
  s2d `EXPOSURES` table / `CFFXSLT` header, covering both standalone
  `NRS_FIXEDSLIT` and fixed-slit-in-MSA sources) now extract with
  `bounded=False`, changing the optimally-extracted `fnu`/`flam` for those
  sources. MSA sources are unchanged (still bounded), and the boxcar
  extractions are unaffected.
- **NIRSpec canonical spectrum-exposure + instrument-parity layout (issue #212).
  BREAKING file-naming/structure change — a pipeline MAJOR.** The four NIRSpec
  intermediate files per `(exposure, detector, source)` (`_cal` / `_cal_bkgsub` /
  `_s2d` / `_s2d_bkgsub`) collapse into **one bare canonical `MultiSlitModel`
  file** (`{root}_{config}_{nod}_{detector}_{source}.fits`, NIRCam-parity naming),
  mutated in place across stage2→3: the live slit SCI/ERR/var hold the current
  state (calibrated → background-subtracted), the pre-bkgsub arrays are stashed as
  `PRE_BKGSUB_*` extensions (reversible via `restore_pre_bkgsub`), the rectified
  views are cached as `S2D_*`/`S2D_BKGSUB_*` extensions, and a per-instrument
  `CFP_CAL→CFP_BKG→CFP_S2D` provenance chain (`common/cfp.py` keysets) records
  reduction depth. The three stage3 exclusions, previously realized by
  file-absence, become explicit `CFP_BKG` state markers (`skipped:nods=N` /
  `excluded:override`) plus the existing `SRCFLUX` filter. **Science is
  bit-identical** — verified byte-for-byte on a real `ember_egs_p1` reduction
  (PRISM MOS, 4 sources incl. a `NoDataOnDetector` slit): `_spec`/`_x1d`/`_s2d`
  arrays `worst|d| = 0.000e+00` vs the four-file flow; the MAJOR is purely the
  naming/structure break. Layout also moves to instrument parity:
  `products/nirspec/<obs>/`, `raw/nirspec/<subdir>/`,
  `reference/nirspec/<obs>/{stuck_shutters, bkg_overrides}`, and NIRCam custom
  flats / wisp templates hoist to the shared (de-fielded)
  `reference/nircam/shared/{flats,wisps}`. **Adopting requires a one-time data
  move** to the new tree (the pipeline reads/writes the new locations only).
  Path config is collapsed to a single root: `[paths].data_dir` /
  `products_dir` overrides are removed (they were unused, half-wired — deploy
  ignored them — and the source of a NIRSpec/NIRCam `reference/` divergence);
  `raw/`, `products/`, and `reference/` now derive uniformly from
  `$CAMPFIRE_ROOT`. Relocate the whole tree via `$CAMPFIRE_ROOT`, or symlink an
  individual subdir. The one-time adoption move is scripted:
  `pipeline/scripts/migrate_layout_212.py` (dry-run by default; `--apply` to
  execute; idempotent, never clobbers, writes a JSONL audit manifest).
- NIRSpec fixed-slit: fixed the summary/deploy source position. Fixed-slit
  products carry no catalog `SRCRA`/`SRCDEC` in the SCI header (those are
  MSA-only), so the summary reader recorded `(0, 0)` for every fixed-slit
  target — which collapsed all of them into a single object at the origin during
  friends-of-friends clustering at deploy. The reader now falls back to the
  EXPOSURES-table `source_ra`/`source_dec` (the target position), and stage3
  writes `SRCRA`/`SRCDEC` into the `_spec.fits` SCI header from that table so the
  product is self-describing. Existing fixed-slit products only need a `summary`
  re-run (no re-reduction); MSA products are unaffected.
- NIRSpec standalone **fixed-slit** (`NRS_FIXEDSLIT`) reduction is now supported
  end-to-end (stage1→3). Previously only MSA exposures (`NRS_MSASPEC`) and the
  fixed-slit-in-MSA hybrid were handled, because both describe their slits in the
  MSA metadata file (`*_msa.fits`); standalone fixed-slit exposures carry no such
  file. The pipeline now detects `NRS_FIXEDSLIT` from the exposure header and
  routes around the MSA-metafile machinery, letting jwst's native fixed-slit path
  (`assign_wcs.get_open_fixed_slits` → `extract_2d` selecting the primary slit by
  name) build the WCS and extract the spectrum. Specifically: `Observation.glob`
  accepts `NRS_FIXEDSLIT` uncals; a new `_run_stage2a_fixedslit` runs
  `Spec2Pipeline` with `extract_2d.slit_names=[FXD_SLIT]` and emits a product keyed
  on jwst's primary-slit `source_id=1` (`{root}_{nod}_{detector}_1`), so the
  mode-agnostic nodded background subtraction (2b) and combination (3) consume it
  unchanged; `group_files` handles fixed-slit along-slit nod patterns
  (`*-NOD`, e.g. `3-POINT-NOD`); and the stage3 provenance table + Spec3 output
  renaming tolerate the fixed-slit header set (no MSA `SHUTSTA`; slit-level
  `SRCRA`/`SRCDEC` fall back to the target position) and slit-name-embedded Spec3
  product names. stage1 (Detector1 + background subtraction) needed no changes —
  the science region is already protected by the hardcoded fixed-slit detector
  band. MSA reductions are byte-for-byte unaffected. Validated on program 1967
  (z≈6 quasar census, S200A2/G395M-F290LP): full 2.85–5.29 µm spectra recovered.
- NIRSpec fixed-slit provenance + slit-overlay geometry now propagate to the
  deliverables. The stage-3 `EXPOSURES` HDU gains `fixed_slit` / `slit_name`
  columns and the final `_spec.fits` primary header carries `CFFXSLT` /
  `CFFSSLIT`, so a fixed-slit source is identifiable downstream (this also flags
  fixed-slit sources observed *inside* MSA exposures, via the metafile
  `fixed_slit` column). The shutters ECSV becomes self-describing: each row
  carries `aperture_name`, `aperture_width_arcsec`, and `aperture_height_arcsec`,
  and fixed-slit sources export a single aperture rectangle sized to the slit
  (e.g. S200A2 = 0.2"x3.2") instead of MSA shutter geometry — which `slits.py`'s
  `get_exposure_table` would have rejected outright. MSA rows keep their geometry
  and now carry the existing 0.22"x0.46" dimensions explicitly. Aperture sizes
  live in `nirspec/constants.py` (`FIXED_SLIT_SIZE_ARCSEC`,
  `MSA_SHUTTER_SIZE_ARCSEC`). Consumed by the web/Python slit overlays (DB
  column, `get_nearby_shutters`/`get_field_shutters` RPCs, deploy, and the
  SVG/canvas/matplotlib renderers updated in lockstep).
- NIRCam `striping`: new opt-in per-amp-row 1/f offset estimator selectable via
  `[nircam.striping].estimator` (default **`"median"`** — the production
  2σ-clipped median with full-row fallback, byte-for-byte unchanged). The new
  `"gp"` estimator fits a 1-D Gaussian Process (celerite2 `SHOTerm`,
  `Q = 1/sqrt(2)`, CPU O(n)) along the slow (row) axis *per amplifier*, with
  each amp-row weighted by its sampling error `sigma_r ≈ 1.25·MAD/sqrt(N_r)`
  and carrying its own DC mean term. It interpolates the offset across
  source-masked rows using clean rows of the *same* amplifier instead of
  substituting the cross-amp full-row median, removing the amp-boundary +
  slow-axis "box" of striping artifacts around bright/extended sources. The
  per-column (vertical) step is untouched. Only the length scale `rho` (in
  rows) is a frozen hyperparameter — a detector readout property, independent
  of filter and flux units (calibrate with `scripts/calibrate_gp_striping.py`).
  A single channel-agnostic `rho = 5.0` is used: it was measured stable across
  five filters spanning both channels on rj0911 (LW f277w/f356w/f444w =
  4.51/4.44/5.03, SW f200w/f150w = 4.10/4.11 rows — one cluster inside its
  broad flat optimum), so no SW/LW split is needed. The kernel **amplitude
  self-adapts per
  exposure** (the marginal `mad_std` of the clean per-amp-row medians,
  measured on the pre-2D-bg frame — a deterministic robust statistic, not a
  per-exposure fit), so it tracks the cal-stage flux units that vary ~3× by
  filter instead of carrying a frozen absolute number. Nothing is optimized
  per exposure. An aggressive
  masking variant (`mask_aggressive`) dilates the source mask and folds in
  JUMP/SATURATED/PERSISTENCE DQ — over-masking only inflates `sigma_r` (the GP
  interpolates across), whereas under-masking biases the median and the GP
  would oversubtract. A third value, `estimator = "none"`, builds/writes the
  `SRCMASK` and runs the rest of the pipeline but applies no campfire 1/f
  (for comparison runs against JWST's own ramp-stage `clean_flicker_noise`).
  Default config reproduces the current pipeline exactly; no change unless
  `estimator` is set away from `"median"`. A/B testbed in
  `experiments/oneoverf_gp/`, which also evaluates `clean_flicker_noise`: on a
  cluster field the GP beats the median by ~14% on the amp-row 1/f residual
  (clean *and* source rows, photometry conserved, slightly faster), while
  `clean_flicker_noise` is not adopted — its `fit_method="fft"` is NIRSpec-only
  (skipped for `NRC_IMAGE`) and its `"median"` mode underperforms our amp-row
  estimators and introduces per-amp DC steps.
- NIRCam campfire-native drizzle (`resample.implementation = "campfire"`): the
  ERR map no longer fills with `inf`/`nan`. The variance pass summed the three
  variance components before drizzling, so a single input pixel with a
  non-finite or negative component (e.g. `var_poisson = inf` at a pixel that is
  not flagged `DO_NOT_USE`) poisoned `var_total`, and cdriz spread it across
  every output pixel its kernel touched — `inf` is sticky in the running
  weighted mean, so one bad input pixel blew up the ERR for all co-located
  inputs and could dominate a tile. Each variance component is now masked
  independently with `(var >= 0) & isfinite(var)` before the sum (matching the
  per-component masking stcal applies in `resample_variance_arrays`): a bad
  component drops only its own term, so a component that is bad across many
  inputs (e.g. a flat-field column) degrades gracefully to the surviving terms
  instead of leaving a NaN hole. A pixel is dropped entirely only where no
  component is valid, and dropped pixels are excluded from both the variance
  numerator and its normalizing weight (`outvarw`, now used in place of the SCI
  weight) so there is no dilution bias. The SCI/WHT pass is unchanged; ERR is
  bit-identical to before at pixels with no masked components, and only
  previously-`inf`/`nan` pixels change. The default `jwst` implementation was
  never affected.

### Infrastructure
- NIRCam combine no longer crashes on interrupted-save debris. A killed
  `atomic_save` (e.g. a combine worker that dies mid-write) stages to
  `<name>.tmp.fits` — `.tmp` inserted *before* the extension so the datamodel's
  format dispatch still sees `.fits`. `Field.get_exposure_files` filtered
  sidecars with `base.endswith('.tmp')`, which never matches that name, so the
  truncated fragment (no ASDF/WCS extension) was pulled in as a phantom input
  and blew up outlier detection with `AttributeError: No attribute 'wcs'` plus
  an astropy truncation warning. The enumeration guard now also drops
  `*.tmp.fits`, and `atomic_save` removes its staging file if the save raises so
  it doesn't leave debris in the first place. No scientific-output change.
- **NIRCam `align` closes the cross-filter dependency and gates observing mode.**
  Two correctness fixes to the align orchestration (opt-in, default-off):
    - *Cross-filter closure.* `run_align` now pools each physical exposure across
      **all** field filters, even when `--filters`/`--tiles` selects a subset — so
      `align --filters f200w` still sees its paired F444W (LW) complement for the
      shared solve, and a tile gate can't split a dither at a tile edge. The
      selection then just chooses **which** exposures to process; each is solved
      and its corrected gwcs written across its full SW+LW complement (one
      attitude corrects the whole dither), so `--filters f200w` now also stamps
      the paired f444w canonicals — a deliberate behavior change that keeps an
      exposure from ending up half-aligned. Status is scanned over all filters to
      match.
    - *Observing-mode gating.* Align now reads `EXP_TYPE`/`SUBARRAY` per exposure
      and **hard-stops** with a clear, listed error if any exposure in scope is in
      an unsupported mode (subarray / coronagraph / TSO / WFSS), rather than
      silently feeding it through generic full-frame-imaging logic. The user must
      exclude it (fields.toml `skip` / reviewer exclusions) or select a supported
      subset. Missing metadata is treated leniently. `orchestrate.py`,
      `association.py`; covered by `tests/test_align_run.py`, `test_association.py`.
- **NIRCam `--tiles` now pre-filters the exposure set for `process`/`align`/`combine`,
  not just `resample`.** Previously `--tiles` scoped only which mosaics were drizzled;
  every earlier step ran over the whole field, so building one tile meant processing
  all of it. `--tiles` now restricts each phase to exposures overlapping the named
  tile(s): `detector1` gates on the uncal `S_REGION` footprint (present before any WCS
  is assigned), and the canonical-stage steps + `align` groups inherit the subset. The
  overlap gate lives in `nircam/geometry.py` (`select_overlapping_by_sregion` +
  `filter_exposures_to_tiles`, exposure-union so a straddling dither keeps its full
  detector complement; the tile polygon is buffered ~11″ to stay a conservative
  superset of resample's precise SCI-WCS selection; missing/blank `S_REGION` fails
  open). This makes a single-tile reduction cheap — e.g. an `align`-vs-`jhat` A/B on one
  COSMOS tile (`scripts/nircam_ab_astrometry.py` compares the two mosaics' extracted
  catalogs to each other and to the reference). **Default (no `--tiles`) runs are
  byte-identical.** A tile-scoped run restricts `outlier`/`bad_pixel` to the overlapping
  subset, so tile-edge pixels may differ from a full-field pass — expected, since a
  tile-scoped run is a distinct input set. *(Categorized Infrastructure because the
  canonical full-field reduction is unchanged; noting the tile-scoped caveat.)* Covered
  by `tests/test_tile_filter.py` + `tests/test_ab_astrometry.py`.
- **NIRCam `--tiles` overlap scan is ~10× faster (cold-NFS + per-phase memo).** The
  `S_REGION` footprint gate (`read_sregion_polygon`) was the silent multi-minute stall
  at the start of a tile-scoped `process` on a cold NFS mount. Two fixes: (1) it now
  reaches the `SCI` header by name instead of slicing `hdul[1:]`, which forced astropy
  to enumerate every extension — seeking past all ~9 data units (~24 NFS round-trips/file
  vs ~6), ~8× fewer round-trips cold (~348→~43 ms/file); no pixel data was ever read,
  the cost was purely `lseek`-triggered 1 MB readahead. (2) `run_process` runs the gate
  once per step (~10 `get_exposure_files(tiles=)` calls), so results are now memoized by
  path for the phase (`reset_sregion_cache` at phase entry) — the scan runs once instead
  of once per step. Net: a 1600-file tile scan drops from ~13 min/phase to ~1.3 min.
  Footprint results are byte-identical (verified against the prior implementation on real
  exposures); no scientific output change. Also removes a stale test that asserted the
  pre-`5059e87` `run --process --tiles` rejection (superseded by
  `test_cli_run_tiles_allowed_with_process`).
- **NIRCam `align` phase — exposure I/O + `CFP_ALGN` stamp.** New
  `nircam/align/apply.py` (`align_exposure_group`) is the FITS layer: it reads each
  detector's gwcs and detects sources, runs the in-memory solve, and writes the
  corrected gwcs back onto the canonical (`model.meta.wcs` + `update_fits_wcsinfo`,
  re-attaching `SRCMASK`, via `atomic_save`) with a per-detector `CFP_ALGN` provenance
  value (`dof`/residual/match-count) — or a `NOT_ALIGNED` sentinel when the exposure
  can't be tied to the reference (WCS preserved, never retried). The original
  (un-aligned) gwcs is stashed in a `WCS_BAK` extension so an `overwrite` re-run solves
  from the original and never double-corrects (mirrors the `wcs_shift` contract).
  Right-sizes the `CFP_ALGN` card comment set in the earlier scaffolding so the value +
  comment fit one 80-char FITS card. Still not wired into any run; covered by
  `tests/test_align_apply.py` (persistable-gwcs canonical round-trip). No behavior change.
- **NIRCam `align` phase — per-exposure solve core.** New `nircam/align/solve.py`
  (`solve_exposure_group`) fits ONE shared shift+rotation per exposure via
  `tweakwcs.align_wcs` (`fitgeom='rshift'`, SIAF distortion untouched) against the
  static Gaia-tied reference catalog: one `JWSTWCSCorrector` per detector, all sharing
  the exposure's `group_id` (the pooling constraint made mechanical); distinct
  per-exposure group_ids + `expand_refcat=False` keep exposures independent (no global
  collapse). Per-detector residuals are recomputed directly (`det_to_world` vs matched
  reference positions), and a detector whose residual exceeds tolerance gets an adaptive
  shift-only refit against a distractor-free local reference subset (accepted only if it
  improves). A `SUCCESS` fit that matches too few sources is rejected to a NOT_ALIGNED
  identity fallback (reject-to-identity). Returns corrected gwcs + diagnostics per
  detector; no FITS I/O yet. Prototype-validated (2″ offset → 0.036 mas); covered by
  `tests/test_align_solve.py` with a CRDS-free mock gwcs. No behavior change.
- **NIRCam `align` phase — centroid-only source detection.** New
  `nircam/align/detect.py` (`detect_star_centroids` / `detect_in_exposure`) finds
  point-source centroids on a detector's SCI image with `photutils.DAOStarFinder` —
  **centroids only, no aperture photometry**, so it structurally avoids JHAT's `-99.99`
  sky-annulus sentinel (which floods the matcher with fake constant-magnitude sources
  on CAMPFIRE's sky-subtracted frames). Returns `x, y` (0-indexed detector pixels) plus
  a PSF-fit brightness proxy (`flux`/`mag`), masking `DO_NOT_USE` DQ and off-detector
  pixels. WCS-free and `jwst`-free (reads SCI/ERR/DQ via `astropy.io.fits`). Standalone
  library module for the forthcoming align solve — not yet wired in; covered by
  `tests/test_align_detect.py`. No behavior change.
- **NIRCam `align` phase — triangle/asterism matcher.** New `nircam/align/` subpackage
  with `TriangleMatch`, a `tweakwcs` `MatchCatalogs` subclass that matches source
  catalogs by triangle *shape* (side ratios) — invariant to translation/rotation/scale,
  so it recovers correspondences with no prior on the WCS offset (the regime where the
  default 2d-histogram + nearest-neighbour matcher silently mis-aligns). Wraps
  `tristars.match_catalog_tri` (correspondence path only; the fit stays `tweakwcs`'s
  job). Color-free — magnitude is used only to cap each catalog to its brightest-N
  triangle vertices, never as a match constraint. Standalone library module for the
  forthcoming align solve — not yet wired in; covered by `tests/test_align_matcher.py`.
  No behavior change.
- **NIRCam `align` phase — exposure-association layer.** New `nircam/association.py`
  groups canonical exposure files into per-exposure `ExposureGroup`s keyed on the
  exposure token (`rootname.rsplit('_',1)[0]`), pooling every detector of one dither
  across the SW and LW filter directories (they share the token). Reuses
  `Field.get_exposure_files` so per-filter effective-skip (field `skip` + reviewer
  `excluded_exposures` + caller skip) is honored; classifies module/channel from the
  detector token; is imaging-only (grism is gated upstream) and reads filenames only
  (never opens a FITS). Standalone library module for the forthcoming `align` phase —
  not yet wired into orchestration; covered by `tests/test_association.py`. No behavior
  change.
- **NIRCam `align` phase — foundation scaffolding (no behavior change).** Registers
  the `CFP_ALGN` provenance keyword in the `NIRCAM` CFP keyset (immediately after
  `CFP_JHAT`, so `reset --from jhat` / `--from wcs_shift` also clears it — both mutate
  the WCS), opens the `[<field>.align]` per-field config namespace (`known_steps`), and
  declares the two dependencies the forthcoming adaptive astrometric align step will
  import (`tweakwcs>=0.8`, previously only transitive via `jwst`; `tristars==0.1`, the
  triangle/asterism catalog matcher). Nothing new runs yet — this is the pipeline-side
  foundation for the field-level `align` phase that will replace the JHAT-based
  `jhat`/`wcs_shift` alignment. No change to any output values.
- **NIRCam resample tile selection moved from config to a `--tiles` CLI flag.**
  Which mosaic tiles get drizzled is a runtime choice, not a processing
  parameter, so the undocumented `[nircam.resample].tile` config key is retired
  in favor of `--tiles` on `cfpipe nircam {resample,combine,run,check}` (variadic,
  e.g. `--tiles A1 A2`; default: all tiles in the field). The flag scopes the
  resample step *only* — the exposure/visit-level combine steps (`apply_mask`,
  `bad_pixel`, `outlier`) still run over the full field, so a tile built from a
  subset run is bit-identical to the same tile from a whole-field run (truncating
  outlier's cross-visit median pool would change edge pixels). `run` rejects
  `--tiles` unless the combine phase is selected. Covered by
  `tests/test_nircam_resample_tiles.py`. CLI ergonomics only; no change to mosaic
  pixel values.
- **NIRCam `jhat` accepts a single `refcat` in addition to `refcat_dict`.** A
  field that aligns every filter to the same reference catalog can now set
  `[<field>.jhat].refcat = "<file>"` instead of repeating that filename across a
  `[<field>.jhat.refcat_dict]` block. When both are given, `refcat_dict` entries
  win per-filter and `refcat` is the fallback for any filter it doesn't list.
  Resolution moved into a small `_resolve_refcat` helper (covered by
  `tests/test_nircam_jhat_refcat.py`). Config ergonomics only — for a given
  configuration the same catalog is passed to JHAT as before, so no change to
  aligned WCS or pixel values.
- **NIRSpec `stuck_closed_shutters.toml` entries now carry a `# hand` / `# web` /
  `# auto` provenance tag.** `write_stuck_shutters_toml` preserves the tag of each
  existing entry (via a new `provenance` arg) instead of collapsing it to untagged,
  and a new `load_stuck_shutters_tagged` reads the tags back out (the reader
  `toml.load` still ignores them). The stage2a / detect-stuck auto-detect callsites
  now thread tags through the rewrite, tagging freshly auto-detected entries `# auto`.
  This lets the new `campfire deploy nirspec pull-stuck-shutters` authority merge
  (`hand > web > auto`) survive an auto-detect rewrite — preserving hand entries,
  refreshing web entries from the DB, and letting auto fill gaps. Pure provenance
  plumbing: no change to which shutters are detected or dropped, or to extracted flux.
  (NIRSpec review loop, P7.)
- **NIRSpec canonical spectrum-exposure FITS now carry a `CFEXPGRP` primary-header
  card** recording the pipeline-computed `exp_group` (the sub-pixel-dither grouping
  id from `Observation.group_files`). Stamped by a final pass at the end of
  `run_stage2a`/`run_stage2b` (via `canonical.append_extras`, so it's additive and
  survives stage2b's MultiSlitModel re-save like `CFSCHEMA`). `exp_group` is not
  derivable from a single filename — it depends on the whole exposure set's dither
  pattern — so the stamp lets `campfire deploy` populate the web nods-renderer grid
  (`spectrum_exposures`) with the exact pipeline grouping. Additive provenance only:
  no change to pixel/flux values or file layout. (NIRSpec review loop, P4.)
- **NIRSpec rate-mask region strings now come from
  `reference/nirspec/<obs>/masks/*.reg`, not `observations.toml`.** The NIRSpec
  web review loop (design §3.5). `Observation.setup_workspace_directory` populates
  `manual_masks` by reading `.reg` files (one per `<exposure_root>_<detector>.reg`,
  DS9 image coords) from the observation's reference `masks/` dir, materialized by
  the new `campfire deploy nirspec pull-rate-masks` from the web editor's DB rows.
  The `observations.toml [<obs>.masks]` read path and the `workspace_dir/
  manual_masks/` mirror (`materialize_reg_files`) are removed; the local `mask
  edit` / `mask clear` writers now target the same reference `.reg` store. Only the
  *source* of the region string changes — the `apply_mask_dq` / `CFDQMASK`
  reversible DQ OR, `CFMASKSH` staleness, and `bkgsub_with_masks` / `ensure_fresh`
  auto-re-apply are unchanged, so a web-edited mask with a changed canonical hash
  still re-applies before bkg sub. No change to pixel/flux values for the same
  regions.
- **NIRCam expmaps now live in the canonical filter directory.** `cfpipe nircam
  expmap` writes each per-filter coverage map to
  `products/nircam/<field>/<filter>/expmap_<field>_<filter>_<stage>.fits` (with
  its diagnostic `.pdf` alongside) instead of a shared `<field>/expmaps/` dir;
  the combined `footprints_<stage>.reg` and metadata cache now sit at the field
  products root. This puts the expmap under the same `<field>/<filter>/` key
  shape as every other per-filter NIRCam product, so the deployed coverage map
  carries a real `filter` in the storage registry. Breaking file-location change
  for the expmap product only; pixel/flux values are unchanged (the `--out-dir`
  override now names the *base* products dir under which `<filter>/` subdirs are
  created). Categorized Infrastructure — no scientific-output change — though it
  does move where a product file lands.
- **NIRCam canonical exposures now carry `CMPFRVER` provenance.** `detector1`
  stamps the CAMPFIRE reduction version (+ `CMPFRTIM`) on each canonical
  exposure's primary header at creation, mirroring the mosaic stamp in
  `resample.py` (jwst already writes `CAL_VER` / `CRDS_CTX`, but not the
  CAMPFIRE version). This lets `campfire deploy` record real pipeline-version
  provenance for a mid-reduction `--draft` exposure deploy — before any mosaic
  exists — instead of leaving `deployments.cfpipe_version` NULL (admin audit
  2026-07-03, B2). Additive header card only; no pixel/flux change.
- **`cfpipe download --target` accepts comma-separated coordinates.** MAST's
  JWST search API resolves the top-level `target` field as either an object
  name or a *space*-separated `"RA Dec"` pair in decimal degrees (matching
  astroquery's `MastMissions`, which sends `f"{ra.deg} {dec.deg}"`). A
  comma-separated pair such as `--target 215.0,52.9` is not a valid name, so
  the resolver raised server-side and the whole search failed with an opaque
  `HTTPError: 500 Server Error` traceback. The download tool now folds an exact
  `"num,num"` coordinate pair into the space-separated form before querying
  (object names and already-spaced coords are untouched), MAST error bodies are
  surfaced instead of discarded by `raise_for_status`, and the `download`
  command catches `HTTPError` to print an actionable `--target`-format hint
  rather than a stack trace. **No change to scientific output** (download CLI
  ergonomics only).
- **`migrate_layout_212.py` now delegates to a shared migrator (#244).** The
  one-time `$CAMPFIRE_ROOT` re-org logic moved verbatim into the zero-dependency
  `campfire_layout.migrate` core, so the operator script and `campfire sync` run
  the exact same code (no drift between the two migration paths). The script is
  now a thin wrapper that keeps its pipeline-specific parts — root resolution,
  `observations.toml` loading, and the stale-`[paths]` warning — and its CLI
  (`--apply` / `--clean-intermediates` / `--root`), dry-run default, crash-safe
  JSONL manifest, and idempotent no-clobber behavior are unchanged. Enables
  `campfire sync` to detect the old layout and offer to migrate it in place.
  **No change to scientific output** (a filesystem/CLI refactor only).
- **Canonical format-version keyword (`CFSCHEMA`, epic #210 B-track prereq).**
  Every NIRSpec canonical spectrum-exposure now carries an integer `CFSCHEMA`
  card (value `1`) stamped at birth in `_finalize_canonical`, self-identifying
  the on-disk layout so a future format migrator can locate old-layout files. It
  is deliberately *not* a `CFP_*` provenance card — it tags the file format, not a
  processing step, so `cfpipe nirspec reset` never strips it, and it rides through
  stage2b's `MultiSlitModel` re-save via the jwst `extra_fits` round-trip. A file
  with no `CFSCHEMA` is byte-format identical to a `v1` file, so
  `canonical.read_schema_version` reads "absent" back as `1`; the keyword only
  discriminates once it increments. **No change to scientific output** (a header
  keyword only). Bumped only when the canonical layout itself changes.
- **Layout & key contract (`campfire_layout`, #213, PR-2 of epic #210).** The
  `$CAMPFIRE_ROOT/` directory tree — previously a three-way contract re-derived
  independently in the pipeline, the deploy/download client, and the web portal —
  is now owned by one tested, zero-dependency package (`layout/campfire_layout/`,
  mirrored in TypeScript at `web/lib/layout.ts`). It is the single authority for
  every product's local path, its storage key, the key↔path bijection, and a
  per-tree lifecycle class; a shared golden fixture
  (`layout/conformance/layout_golden.json`) keeps the python and TS arms in
  lockstep. Pipeline workspace/raw/reference/cache path construction
  (`config.resolve_paths`, `Observation.setup_workspace_directory`,
  `Field.setup_workspace`, `common.query._output_path_for`) now routes through
  the module. **No change to scientific output**; the local tree shape is
  unchanged (it encodes the #212 PR-4 layout). Fixes two latent bugs the swaps
  surfaced: the download/sync client wrote/read `products/<obs>/` without the
  PR-4 `nirspec/` segment, and the raw-NIRSpec download writer and the
  Observation reader are now single-sourced on one partition key. `campfire-layout`
  is a new dependency — install it first (`pip install -e ./layout`).
- Provenance is now carried verbatim from the FITS primary header through to the
  catalog and Python client, fixing four ways it was dropped or distorted
  (closes #202). (1) `cfpipe_version` is the single pipeline-version string,
  read from `CMPFRVER` — it replaces the redundant `reduction_version` column
  (collapsed in both `spectra` and `deployments`), so a `[pipeline].version`
  override now reaches the catalog instead of being recomputed from a
  config-less package `__version__`. (2) `CMPFRTIM` is stamped as UTC ISO-8601
  (was naive local time) and read into a new per-spectrum `spectra.reduced_at`;
  `deployments.reduced_at` is the **earliest** `CMPFRTIM` across an observation's
  products, so re-running `summary` on unchanged pixels no longer advances it to
  "now". (3) `get_spectra_for_sync`, the local SQLite store, and the `Spectrum`
  model now carry `cfpipe_version` / `crds_context` / `jwst_version` / `date_obs`
  / `reduced_at`, with new `query_spectra(crds_context=, cfpipe_version=,
  reduced_after=)` filters and a `SpectrumCollection.provenance()` lens that
  flags calibration-heterogeneous samples. (4) `deploy` warns when one
  observation ships mixed CRDS contexts. **Output-format note for the release
  manager:** the summary ECSV schema changed (`reduction_version` → `cfpipe_version`,
  new `reduced_at` column; deploy reads old ECSVs via a fallback), and the local
  client store `SCHEMA_VERSION` bumped 4→5 (forces a one-time delete + re-sync).
  No pixel/flux values change. Tests: `pipeline/tests/test_provenance_reader.py`,
  `python/tests/test_provenance.py`, plus store/deploy round-trip coverage.
- NIRCam `skyfit.fit_sky` now takes `box_size` / `filter_size` (the striping
  2D-background detrend exposes them via `subtract_background_box` /
  `subtract_background_filter`), and a byte-order guard prevents a corruption
  bug: the bottleneck-accelerated path used to `byteswap(inplace=True)` then
  re-`view` the input, which corrupted native-byte-order arrays in place (the
  cal-frame `fitdata` is native) and produced garbage background → ±100s in the
  output SCI. It now casts a copy to native order only when needed. Regression
  test in `tests/test_nircam_skyfit.py`.
- NIRCam `detector1` exposes `clean_flicker_noise_opts` — a passthrough merged
  over the JWST `clean_flicker_noise` step defaults (e.g. `fit_method`,
  `background_method`), used only when `clean_flicker_noise = true`. Enables the
  cfn comparison arms; no effect on the default config (cfn off).
- NFS cache tier for the NIRCam pipeline (PR 1 of
  `docs/design-nircam-exposure-major.md`; findings H4/H5/M2/M9 of
  `docs/nfs_audit.md`). No change to pixel values or reference selection —
  verified by the parity/equivalence checks noted below.
  - Per-worker reference-data caches: wisp templates are read once per worker
    instead of 5x per exposure (`wisp._load_template`, lru, NaN-cleaning in
    the cache, exposure-specific masking on a copy); flat references are
    cached as pristine `FlatModel`s with a deep copy handed to each
    `do_correction` call (jwst mutates the flat it is given, so the cache
    never exposes a mutated model); per-detector bad-pixel masks are read
    once per worker (`bad_pixel._load_fl_mask`). Caches die with each step's
    worker pool, so rebuilt references are always re-read on the next step.
  - Detector-major dispatch ordering for per-exposure steps
    (`orchestrate._detector_sorted`): `Pool.map` chunks become
    detector-contiguous so the per-detector caches hit instead of thrash.
    Tasks are independent; only log ordering changes.
  - CRDS cold-fetch races are closed at the source rather than avoided. The
    two direct `crds.getreferences` calls outside stpipe — the in-memory flat
    lookup in `steps/_flat.resolve_flat` and the prefetch warm-up — now take
    the global `crds.cache` lock, the same lock stpipe holds for its own
    lookups. A worker that hits a cold reference downloads it once while the
    others block and then find it already cached (CRDS double-checks existence
    inside the lock). The serial prefetch is therefore a pure warm-up, not a
    correctness requirement: it reads each uncal header once (both the
    detector1 and image2 dedup keys come from one `getheader`) and skips
    exposures whose detector1/image2 output already exists, so re-running a
    finished field no longer pays the multi-minute NFS header scan. Reference
    selection is unchanged.
  - `memmap=False` on FITS opens that read whole arrays or only headers
    (steps, drizzle/outlier inputs, manifest hashing, CFP/status header
    probes, geometry/S_REGION scans): NFS serves one sequential read far
    better than memmap's page-faulted small reads. Hash values are unchanged
    (`do_not_scale_image_data` still reads raw stored bytes).
  - `cfpipe --version` is resolved lazily: the four git subprocesses
    (including a `git status` walk of the pipeline subtree on NFS) no longer
    run at module import on every cfpipe invocation.
- NIRCam CLI startup is now proportional to the work being run, not to the
  whole step catalog. `orchestrate.py` previously imported all 14 step modules
  at module top, so every `cfpipe nircam` invocation — including `--help` and a
  `combine` run that touches none of the process-phase steps — eagerly imported
  `photutils.segmentation` (via `wisp`/`striping`) and `matplotlib` (via
  `outlier`). On cluster NFS that was ~140s for photutils + ~40s for matplotlib
  of pure startup latency before the first log line. Step workers are now
  imported lazily inside their runner functions (only when a phase actually
  dispatches the step), and the headless matplotlib backend is selected via
  `MPLBACKEND=Agg` in `_thread_caps` instead of an eager
  `import matplotlib; matplotlib.use('Agg')` in each CLI module. No change to
  outputs or step behavior; `import campfire_pipeline.nircam.cli` now loads zero
  step modules and zero heavy scientific deps.
- NIRCam `resample` now recovers split-extension files after an interrupted
  run. Previously the `_sci/_err/_wht/_srcmask` split was gated solely on
  `needs_rebuild`, which only checks for the `_i2d.fits` mosaic. If a prior run
  produced the i2d but crashed before splitting, a re-run found the i2d present
  and inputs unchanged, took the up-to-date branch, and skipped splitting — so
  the extension files (and their `_latest_` symlinks) were never created
  without a manual `--overwrite`. The step now detects missing extension files
  for an otherwise up-to-date tile and re-splits from the existing i2d (no
  re-drizzle, no bkgsub redo). SRCMASK is only re-split when the i2d actually
  carries that extension.
- NIRCam `jhat` WCS-alignment step no longer aborts the entire `process` run
  on exposures near/over the edge of the reference-catalog footprint. Two
  distinct jhat crash modes were taking down the whole run (one bad exposure
  killed the other 1600+):
  - *Zero refcat overlap.* When no refcat sources land on the detector, jhat
    skips its matching steps leaving `refcat_xcol` unset, then indexes
    `phot.t[None]` and raises a bare `KeyError(None)`. `jhat_step` now catches
    that specific signature, leaves the input WCS untouched, and stamps
    `CFP_JHAT=NO_REFCAT_OVERLAP` so the exposure reads as
    intentionally-not-aligned and isn't retried every run.
  - *Degenerate diagnostic plot.* jhat's dx/dy plotters compute a NaN axis
    limit from a degenerate best-match panel (few matches, common at the
    footprint edge) and raise `ValueError: Axis limits cannot be NaN or Inf` —
    sometimes *after* a perfectly good WCS solution has already been written,
    so the crash discarded a valid alignment. The plots are diagnostic-only,
    but jhat gates them on several independent flags not all reachable through
    `align_wcs`, so the `saveplots` config flag alone can't suppress them.
    Diagnostic plotting now defaults to off and is enforced by no-op'ing jhat's
    plot functions at the source; the affected exposures align normally. Plots
    re-enable per-field with `[<field>.jhat].saveplots = true` (restoring the
    crash risk on edge exposures).
  Net effect: the `process` run completes, edge exposures with real refcat
  coverage are aligned and included, and only the truly-uncoverable ones are
  skipped (and recorded as such). Genuine alignment errors still propagate.
- `Observation.load` now validates the observations.toml `program` field
  against `programs.toml` (when present): if the value is not a known program
  *slug* it raises immediately, with a hint when the value matches a program
  *name* instead. Previously a slug/name mix-up was silently baked into the
  ECSV `program_slug` metadata and only surfaced much later at deploy time as
  a cryptic Supabase row-level-security error.
- NIRCam campfire-native drizzle (`resample.implementation = "campfire"`): a
  tile no longer aborts with `ValueError: No or too few valid pixels in the
  pixel map` when a selected exposure only partially overlaps it. The pixmap
  was built with `output_wcs.invert(ra, dec)` at its default
  `with_bounding_box=True`, which returns NaN for every input pixel mapping
  outside the output WCS bounding box. cdriz (`drizzle` 2.x) raises on *any*
  NaN in the pixmap, so an exposure that merely grazes a (rotated) tile — e.g.
  a COSMOS exposure overlapping tile `B2` by ~2 pixels at a corner — crashed
  the entire drizzle. The inverse is now called with `with_bounding_box=False`,
  producing a finite, geometrically-continuous pixmap; cdriz drops off-frame
  pixels through its normal output-bounds clipping while keeping correct kernel
  geometry at the tile edge, and overlap detection (`_output_bbox_in_tile`) now
  keys off pixels mapping *inside* the frame rather than finite ones. Replacing
  the NaNs with an out-of-frame sentinel was rejected: cdriz derives each
  pixel's drizzle footprint from neighbouring pixmap entries, so a sentinel
  poisons the geometry and silently zeroes the whole exposure's contribution.
  Output for tiles that already built is unchanged (any exposure that
  previously succeeded had an all-finite pixmap, computed identically here).
  The default `jwst` implementation was never affected.
- NIRCam campfire-native drizzle (`resample.implementation = "campfire"`): the
  output mosaic i2d now carries the same header metadata as a jwst
  `Image3Pipeline` product. Previously `_write_i2d_fits` built a blank
  `ImageModel` and set only the WCS + exposure time, so the i2d (and the
  split `_sci/_err/_wht` files derived from its SCI header) were missing
  `BUNIT`, `PIXAR_SR`/`PIXAR_A2`, `PHOTMJSR`/`PHOTUJA2`, all instrument/program/
  target identity, exposure timing, and the HDRTAB provenance table — 220
  PRIMARY and 39 SCI keywords absent vs. the jwst path on real rj0911 data.
  The drizzle now feeds each contributing input through
  `jwst.model_blender.ModelBlender` (reusing the open already done per input)
  and `_apply_output_metadata` finalizes the blend into the output model.
  `PIXAR_SR`/`PIXAR_A2` are recomputed for the output pixel scale (copying the
  native value would bias MJy/sr → Jy/pixel by the scale ratio squared, ~4× at
  30 mas); `BUNIT`/`PHOTMJSR`/`PHOTUJA2` are per-steradian and ride along
  unchanged; `WCSAXES`/`CUNIT`, `NDRIZ`, `PXSCLRT`, and `S_REGION` are set for
  the resampled grid. SCI/ERR header parity with jwst is now complete except
  for `VELOSYS` (a radial-velocity keyword irrelevant to imaging); `S_OUTLIR`/
  `S_SKYMAT` are intentionally left unset because campfire runs its own outlier
  and background steps rather than jwst's. Pixel/flux/ERR array values are
  unchanged. Controlled by the new `resample.blendheaders` knob (default true);
  the `jwst` implementation is unaffected.
- `cfpipe --version` now reports the same live, git-derived version as
  `cfpipe info` (via `get_reduction_version()`) instead of the package
  metadata frozen at install time. Previously the two could diverge in a
  checkout — `--version` showed the last-installed tag (e.g. `0.5.1`) while
  `info` showed the live `0.5.2.devN+g<sha>`. The version stamped into output
  FITS headers is unchanged; only the `--version` display source moved.
- Multiprocessing start method is now platform-aware: `forkserver` on Linux,
  `spawn` on macOS. The earlier blanket `forkserver` switch (added to fix
  `ENOMEM`-at-fork on candide) also forced macOS off its safe `spawn` default,
  and `forkserver` still `fork()`s its workers — unsafe on macOS, where Apple's
  threaded frameworks and cv2's `parallel_for_` pool deadlock in the child. The
  symptom was the NIRSpec stage-1 jump step hanging (`S` / 0% CPU) at "Flagging
  Snowballs", since stcal's snowball flagging calls into cv2. macOS has no
  `ENOMEM`-at-fork concern, so `spawn` is both safe and sufficient there;
  candide keeps `forkserver` and its preload list unchanged.
- `cfpipe download` no longer aborts the whole run when a single MAST
  `/list_products` batch exhausts its retries. `list_products_batched`
  previously called `fut.result()` directly inside `as_completed`, so one
  batch that timed out after its per-request retries propagated the
  exception and discarded every batch that had already succeeded — on a large
  NIRCam program (e.g. 5893, 1521 filesets / 61 batches) a single slow
  response near the end threw away ~30 minutes of completed work, and the
  re-run started from scratch. Failed batches are now isolated per-future and
  retried in up to `max_rounds=3` successive rounds; only if batches still
  fail after the last round does it raise a descriptive `RuntimeError`
  (reporting the failed batch/fileset counts) instead of a raw `ReadTimeout`.
  Per-request backoff sleeps also gained ±1s of jitter so the parallel
  workers don't retry in lockstep against an overloaded endpoint. No change
  to which products are returned on success.
- NIRSpec file ingest no longer crashes with `KeyError: 'NOD_TYPE'` on
  programs whose exposures omit the `NOD_TYPE` primary-header keyword (seen in
  some Cycle 1 programs). `Observation` table building (`observation.py`) and
  the stage-3 exposures table (`stage3.py`) now read it through a shared
  `read_nod_type` helper that falls back to `3-SHUTTER-SLITLET` (the canonical
  `N-SHUTTER-SLITLET` form for a 3-shutter slitlet) and logs a warning naming
  the offending file. Files that already carry `NOD_TYPE` are unaffected.

## v0.5.1 — 2026-05-27

### Infrastructure
- Pipeline version resolution (`campfire_pipeline.common.version`) now
  recounts the distance from the last `pipeline-v*.*.*` tag using
  `git rev-list <tag>..HEAD --count -- pipeline`, so the version stamped
  onto reduced data (`CMPFRVER`, `spectra.cfpipe_version`) reflects only
  commits that touched `pipeline/`. Previously the distance came from
  `git describe --long`, which counts every commit on the branch — any
  `web/`, `python/`, `supabase/`, or `scripts/` activity bumped a
  bit-identical reduction from `0.5.0` to `0.5.1.devN+gSHA`, tripping
  the `campfire deploy` warn-and-confirm prompt for "unreleased" data.
  The dirty flag was already pipeline-scoped (issue #135); this closes
  the matching gap for the distance counter. The `setuptools-scm`
  configuration in `pyproject.toml` is unchanged — it only feeds the
  build-time stamp in `_version.py`, which the runtime resolver only
  reads as a last-resort fallback.

## v0.5.0 — 2026-05-21

### Calibration
- NIRCam stage 1 no longer runs `snowblind.SnowblindStep` after `Detector1Pipeline`.
  The jump step is already configured with `expand_large_events=True`,
  `sat_required_snowball=False`, `expand_factor=2.2`, `sat_expand=2`, and
  `mask_snowball_core_next_int=True` (4000 s), which detects and dilates large
  cosmic-ray clusters at the groupdq level — covering the same cases as
  `SnowblindStep` (and more aggressively, since groupdq flags exclude affected
  groups from the ramp fit and propagate flagging across integrations). The
  `remove_snowballs` orchestrator step and the `[nircam.stage1.remove_snowball]`
  config block are removed. `snowblind` remains a dependency for
  `PersistenceFlagStep` in the persistence step.

### Algorithm
- NIRCam ``striping``, ``diag_striping``, and ``sky`` steps — fit/sample
  masks now use only the ``DO_NOT_USE`` bit instead of ``dq > 0``.
  Informational DQ bits (``JUMP_DET``, ``UNRELIABLE_BIAS``, ``NO_LIN_CORR``)
  flag pixels that have already been corrected and are still usable for
  background and striping estimation. On most exposures this reduces the
  fit mask from ~20% to ~3% of pixels; on rare anomalous frames where
  jump detection flags >97% of pixels (e.g. some MEDIUM8/NGROUPS=9 bright-
  target MSATA parallels) the previous behavior masked the entire frame
  and the 2D ``Background2D`` fit raised. Fixed sites: ``striping.py``
  (per-amp horizontal/vertical fit mask), ``diag_striping.py`` (diagonal
  stripe fit mask, applied at iter-1, after srcmask filter, and after
  per-iter SRCMASK rebuild), ``sky.py`` (sky pedestal Gaussian sample).
- NIRCam ``diag_striping`` step — substantial rewrite of the iteration,
  amplitude estimator, and angle metric:
  - **Column blending bug fix**: ``_column_weights`` now ramps each
    strip's weight across the *full* overlap region (previously only
    over ``overlap // 2`` on each side). Adjacent strips' weights are
    now complementary, giving a constant unit weight sum across the
    overlap (a true partition of unity). The old behavior produced a
    kink at the strip boundary when adjacent per-bin amplitudes
    differed — visible as residual edges at strip seams when amplitude
    varied sharply (e.g. near a bright off-axis source).
  - **Global per-bin median fallback** in
    ``diagonal_stripe_model_blended``: when every strip covering a
    pixel has too few unmasked values in that pixel's bin (below
    ``min_pixels``), the model falls back to the global per-bin median
    (computed across all strips combined) instead of silently emitting
    zero. The previous behavior left stripes untouched at exactly the
    rows where SRCMASK had eaten the brightest pixels — a
    self-reinforcing trap once iteration started rebuilding the mask
    on the (still-bright) residual.
  - **Strip-blended applied every iteration**, not just iter 2+. The
    earlier global-only iter 1 was a guard against the SRCMASK-eats-
    stripe-peaks trap, but the global per-bin median fallback inside
    ``diagonal_stripe_model_blended`` already covers that case
    (pixels whose every covering strip lacks ``min_pixels`` in a bin
    get the global estimate). A global-only first pass meanwhile
    under-corrects when scattered-light amplitude varies across
    strips — exactly the regime the strip-blended model was added to
    handle. Under ``n_iterations >= 2``, iter 2+ rebuilds SRCMASK on
    the running residual (default when ``n_iterations > 1``) so
    stripe peaks initially flagged as sources are released as the
    amplitude bleeds into the running model. θ stays fixed at iter
    1's optimum: re-scoring on a cleaned residual gives a flat score
    landscape, so argmin walks rather than locks. Per-iteration
    diagonal and H+V contributions accumulate into single cumulative
    models.
  - **Angle metric**: scoring switched from ``MAD²(residual)`` of a
    global per-bin median to ``-Var(M(θ))`` on the strip-blended model
    image. Same argmin by total-variance decomposition
    (``Var(D) = Var(M) + Var(D−M)`` with the residual-cross-term
    independent of θ for fixed mask), but the score is the captured
    signal itself — sharper minimum and decoupled from the θ-
    independent un-modeled-source-residual floor. The score model now
    matches the applied model (strip-blended with the configured
    ``column_width``/``overlap``/``max_strip_delta_ratio``), so the
    angle search rewards exactly the model the pipeline will subtract.
  - **Robust per-bin clip**: ``_per_bin_clipped_median`` now uses an
    inlined ``mad_std = 1.4826 * MAD`` threshold instead of bespoke
    ``np.std``-based clipping. Non-robust ``np.std`` is inflated by
    the very SRCMASK leakers the iteration is meant to reject — for
    small per-bin N, a few stripe-peak leakers float the clip
    threshold above themselves, defeating the rejection. Inlined
    rather than calling ``astropy.stats.sigma_clipped_stats`` per bin:
    that helper has ~50–100 µs of per-call machinery overhead and we
    call it ~500 K times per exposure (n_bins × n_strips × n_angles).
  - **``maxiters`` threaded** from ``[nircam.diag_striping].maxiters``
    all the way through ``diagonal_stripe_model{,_blended}`` →
    ``_per_bin_clipped_median`` (previously hardcoded to 2 in the
    helper, so the config knob only affected the H+V residual fit).
  - **Scoring perf**: angle scoring (a) skips the global per-bin
    median fallback (``compute_fallback=False``) since NaN model
    pixels are filtered from the score anyway; (b) skips the
    cross-strip regularizer (``regularize=False``) since it
    compresses ``Var(M)`` slightly without shifting argmax; (c)
    reuses the output buffer as the ``np.divide`` target instead of
    allocating a (H, W) ``float64`` copy per call; (d) hoists the
    θ-independent masking pass (``np.where(mask | ~isfinite(data))``)
    out of the angle loop in ``_coarse_fine_search`` so it runs once
    instead of once per angle.
  - **NaN preservation**: pre-existing NaN pixels in the input SCI now
    propagate through to the corrected output unchanged (with the
    DO_NOT_USE bit still set). The previous behavior overwrote them
    with 0, silently changing pixel values relative to the post-sky
    upstream snapshot.
  - **Skip-condition gating** (new, default on via ``skip_abs_range``):
    after the angle search, exposures whose -Var(M(θ)) curve provides
    no meaningful stripe signal skip the subtraction entirely. Two-tier
    OR (empirically derived from the F356W UDS audit of 306 exposures
    in ``scripts/diag_striping_score_audit.py``):
      - ``abs_range < skip_abs_range`` (default 1e-7): the score curve
        is essentially flat at any θ — no real stripe geometry to fit.
      - ``abs_range < skip_abs_range_at_edge`` (default 2e-7) AND the
        optimum θ within ``skip_boundary_dist`` (default 0.3°) of the
        search-range boundary: the search hit a wall with no interior
        minimum. The flat-tier alone would let these through.
    Skipped exposures write ``CFP_DIAG = 'SKIPPED: <reason>; would-be
    theta=..., range=[...]'`` so the decision is auditable, and the
    diagnostic PDF still renders with a ``[SKIPPED]`` title annotation
    showing the flat or boundary-walked score curve. The canonical
    SCI is bit-identical to the post-sky input on skip (apart from
    the CFP_DIAG header and any new DO_NOT_USE flagging for NaN). Set
    ``skip_abs_range = 0`` (and the at-edge pair) to disable. Reapply
    on a different field requires re-auditing — defaults are tuned to
    UDS data character, not universal.
  - **Stripe-aware SRCMASK filter** (new, default on via
    ``unmask_stripe_aligned``): after θ is determined, connected
    components in the SRCMASK whose principal axis lies within
    ``stripe_angle_tol_deg`` of θ and whose aspect ratio exceeds
    ``stripe_aspect_min`` are unmasked before the per-bin median fit
    (and after every iter 2+ SRCMASK rebuild). The ``striping``
    masking pass uses a 25-px Gaussian smooth after a 40-px ring-
    median that occasionally connects a bright scattered-light
    stripe into a "source"; once masked, the diagonal bin running
    along that stripe loses every unmasked pixel, the per-bin
    median collapses to the (also-empty) global-median fallback,
    and the stripe survives the subtraction intact. The filter
    targets that failure mode without releasing genuine compact
    sources (round components fail the aspect test) or off-axis
    elongated galaxies (axis-orientation gating). Provenance:
    ``unmask_aligned=1(ar=...,tol=...)`` in ``CFP_DIAG``.
  - Provenance recorded as ``niter=N`` in ``CFP_DIAG``.
- NIRCam ``wcs_shift`` step (new, opt-in): applies a per-rule bulk
  astrometric shift to the GWCS via ``jwst.tweakreg.utils.adjust_wcs``
  before ``jhat``, for visits whose pipeline astrometry lands outside
  JHAT's source-matching radius. Rules live as an array of tables under
  ``[[<field>.wcs_shift]]`` in ``fields.toml`` (``files`` rootname globs,
  optional ``filters``, ``delta_ra``/``delta_dec``/``delta_roll``/``scale``).
  The original GWCS is stashed in a ``WCS_BAK`` FITS extension on first
  apply and restored before re-applying on ``--overwrite``, so the step
  is declarative — config specifies the desired shift, on-disk state is
  brought into agreement. Provenance recorded in ``CFP_SHFT`` between
  ``CFP_VAR`` and ``CFP_JHAT``. No-op for fields without rules.
- NIRCam ``diag_striping`` step (new, opt-in): subtracts scattered-light
  diagonal stripe artifacts caused by off-axis bright stars. Runs after
  ``sky`` (so the data is flat-corrected and pedestal-subtracted, which
  the cross-strip ``max_strip_delta_ratio`` regularization needs to be
  meaningful — a fractional constraint against a non-zero pedestal is
  effectively unconstrained), before ``variance``. Reads the source mask
  from the ``SRCMASK`` extension that ``striping`` writes and that
  ``image2``/``edge``/``sky`` carry through. Coarse + fine grid search over θ scored by the
  residual MAD² of a global per-bin median; applies a strip-blended
  per-bin median at the optimal θ to capture spatial amplitude variation;
  re-fits horizontal + vertical 1/f residuals via a new
  ``fit_residual_striping`` helper extracted from ``striping`` (pure
  refactor — no change to ``striping`` behaviour). Provenance recorded
  in ``CFP_DIAG``. Disabled by default; enable per field with
  ``[field.diag_striping].enabled = true`` and tune
  ``theta_min``/``theta_max`` to the field's scattered-light geometry.
- NIRCam ``diag_striping``: default ``column_width`` raised from 256 to
  512 so each strip is one NIRCam amplifier (4 strips per SCA), with
  ``column_overlap`` defaulted to 0 (no inter-strip blending — strips
  align cleanly with amp boundaries). New ``max_strip_delta_ratio`` knob
  (default 0.3) regularizes the per-bin amplitude across adjacent amps
  via iterative pair projection — caps ``|M[k+1,b] - M[k,b]| ≤ ratio ·
  max(|M[k,b]|, |M[k+1,b]|)`` per diagonal bin so the spatial amplitude
  variation across amps stays smooth without letting any single amp's
  per-bin median run wild from a single bright source. Bin indices are
  now computed once on the full image rather than per-strip so bin ``b``
  refers to the same diagonal in every strip — required for the
  cross-strip constraint to be meaningful. Set
  ``max_strip_delta_ratio = 0`` to disable.
- NIRCam ``bad_pixel`` step: now disabled by default, only stacks the
  DO_NOT_USE bit (not all DQ bits), and defaults to a stricter
  ``threshold = 0.8``. The previous behaviour — flagging any pixel
  with *any* nonzero DQ bit in ≥20% of exposures as permanently bad
  — was adapted from a many-exposure COSMOS-Web reduction and
  over-rejected in the small-N regime: transient flags like JUMP_DET
  (cosmic rays, ~4–5%/exposure), SATURATED, and PERSISTENCE were
  promoted to permanent DO_NOT_USE, producing per-cal NaN fractions
  of ~20% in fields with only a handful of exposures per filter.
  Behaviour now: (1) the orchestrator skips the step unless
  ``[nircam.bad_pixel].enabled = true`` (intended to be opted in
  for COSMOS-style fields); (2) only the DO_NOT_USE bit (bit 0) is
  considered when stacking, so transients can no longer accumulate;
  (3) the threshold is normalised by the count of contributing
  exposures (was ``np.max(arr)``), making the threshold a true
  exposure fraction. Existing static defects are already covered by
  CRDS DQ in cal files, so disabling this step does not regress
  bad-pixel rejection — it only removes the over-counting.
- NIRCam tile WCS: ``Field.get_tile_wcs`` now converts the ``crpix``
  declared in ``fields.toml`` (FITS 1-indexed, the natural convention —
  ``(NAXIS+1)/2`` lands at array centre) to 0-indexed before returning.
  Both ``stcal.alignment.util.wcs_from_sregions`` (campfire-native
  drizzle) and ``jwst.resample.resample_step`` (jwst-path drizzle)
  document their ``crpix`` argument as 0-indexed; the previous
  pass-through introduced a constant +1-pixel astrometric offset on
  every mosaic. ``ResampleImage.update_fits_wcsinfo`` adds the +1 back
  when serialising to FITS-WCS, so the published ``CRPIX`` matches the
  user's intent and existing reference mosaics for the same tile. All
  mosaics produced before this fix carry a one-pixel sky offset
  relative to their declared ``crval`` and need to be re-drizzled.
- NIRCam mosaic resample now sets SCI=NaN at WHT=0 pixels in the final i2d
  before extension splitting (`steps/resample.py`), matching the ERR
  convention. The drizzle output already initialises SCI=0 at uncovered
  pixels, but `bkgsub` subtracts a smooth background everywhere, leaving
  small nonzero residuals there; the explicit NaN-fill makes the
  "no coverage ⇒ no signal" state unambiguous in the published
  `_sci.fits` and `_i2d.fits`, and matches ERR=NaN at the same pixels.
- NIRCam campfire-native drizzle (`drizzle.drizzle_tile` →
  `_write_i2d_fits`) now calls
  `jwst.resample.resample.ResampleImage.update_fits_wcsinfo(model)`
  before `model.save()`, populating `model.meta.wcsinfo` (CRPIX/CRVAL/
  CDELT/PC/CTYPE) directly from the gwcs's forward-transform parameters.
  `model.save` then serialises those into the SCI extension header in
  the standard PC+CDELT form a jwst i2d carries. Previously the campfire
  path wrote the gwcs only into the asdf-in-fits extension and left the
  SCI header without any legacy FITS-WCS keys, so DS9 (and astropy.wcs)
  saw no celestial WCS at all on campfire-path mosaics. Using the
  canonical jwst helper (rather than re-deriving keys ourselves)
  guarantees byte-equivalent encoding to a reference jwst pipeline
  i2d for the same geometry.
- NIRCam outlier detection's cross-visit overlap padding is now
  scoped to the same JWST program by default. The previous behavior
  (any spatially-overlapping exposure regardless of program) is
  available behind `[nircam.outlier].cross_program_overlap = true`.
  Motivation: in heavily-observed footprints (e.g. COSMOS-Web center
  in F200W where many programs dither over the same area), the
  cross-program padding caused each CRF to be drizzled once for its
  own visit plus once for every other program's visit it overlapped,
  driving an N²-ish redundant-drizzle cost. Intra-program scoping
  removes that scaling problem; CR statistics within the program
  (the only median pool that contributes to that program's
  exposures) are unchanged.

- NIRCam outlier detection has an opt-in campfire-native drizzle path
  (`[nircam.outlier].implementation = "campfire"`) in
  `nircam/outlier_detect.py:outlier_detect_for_visit`. Same per-visit
  grouping, intra-program scoping, manifest conventions, and
  `CFP_OUT` semantic as the jwst path; the drizzle/median/blot
  routine routes through campfire's bbox-sliced
  `drizzle.drizzle_tile_singles` + `stcal.MedianComputer` instead of
  `Image3Pipeline`'s stcal Resample. The per-visit intermediate WCS
  is built via `wcs_from_sregions` with `pscale=None`, `rotation=None`
  (input native scale, ref-input rotation — the same convention
  `jwst.outlier_detection` uses internally), so the drizzle/blot
  roundtrip preserves PSF cores rather than smearing them through a
  fixed-rotation tile grid. CR flagging still goes through the
  upstream `flag_resampled_model_crs` two-pass SNR scheme. Default
  stays `"jwst"` until COSMOS-scale validation confirms the speed and
  flagging-quality trade.

  Replaces the dead-end per-tile path that briefly lived under
  `outlier_step_per_tile` / `outlier_detect_for_tile` — the per-tile
  framing forced the median onto the science tile WCS, which both
  inflated per-input drizzle scaffolding (full-tile output buffers
  per input) and degraded PSF-core preservation in the blot
  roundtrip due to rotation/pscale mismatch with the inputs. The
  per-visit framing fixes both. The bbox-sliced `drizzle_tile_singles`
  primitive that came out of that work is retained and reused.

  Helpers extracted to `nircam/geometry.py:select_overlapping_files`
  (deduplicated from `steps/resample.py` and `manifest.py`) and
  `nircam/drizzle.py:_prepare_drizzle_input` /
  `_add_image_kwargs` (shared between `drizzle_tile` and
  `drizzle_tile_singles`).
- NIRCam stage-3 resample now has an opt-in campfire-native drizzle path
  (`[nircam.resample].implementation = "campfire"`) that replaces
  `jwst.pipeline.calwebb_image3.Image3Pipeline` with a direct
  `drizzle.resample.Drizzle` loop in `nircam/drizzle.py`. The structural
  win over `stcal.resample.resample.Resample` is the **variance trick** —
  a single persistent accumulator is filled by drizzling
  `var_total · wht` weighted by `wht`, with the final ERR computed as
  `sqrt(outvar / outwht)`. This replaces stcal's three transient
  per-component variance drizzles plus full-tile Python masked
  accumulator updates (the `wsum[mask] = ...` loops at COSMOS-Web tile
  size were the dominant per-tile cost).

  The output WCS is built via `stcal.alignment.util.wcs_from_sregions`
  using the campfire-supplied `(crpix, crval, shape, rotation,
  pixel_scale)` from `Field.get_tile_wcs`. The i2d FITS is written
  through `stdatamodels.jwst.datamodels.ImageModel` so the
  `SCI`/`ERR`/`WHT`/`CON` HDU layout matches what `bkgsub` and the
  extension splitter consume; per-component `VAR_*` extensions are
  intentionally not written (nothing in pipeline/, python/, or web/
  reads them from i2d files).

  Validation on rj0911 venus f277w (60mas, 8 inputs, 23 MP): SCI, WHT,
  and coverage are bit-exact (modulo float32 accumulation order). ERR
  is systematically ~5% larger than stcal's ERR at the median because
  the trick computes the canonical kernel-weighted estimator
  `V = (Σᵢ kᵢ wᵢ² varᵢ_total) / (Σᵢ kᵢ wᵢ)²` while stcal computes a
  per-component sum `Σ_xx wsum_xx / (wt² · pixel_scale_ratio²)` after
  drizzling each `sqrt(varᵢ)` separately. The bias is concentrated at
  low-coverage edges (1.13× at p25 WHT) and uniform at ~1.03× in
  well-covered regions; nearly zero correlation with var_poisson /
  var_rnoise (Spearman 0.008) so it's a geometry/kernel artifact, not
  a noise-model artifact. Wall-time speedup on the validation tile is
  4.4× (28.4 s vs 125.9 s); expected to grow at COSMOS-Web tile sizes
  where stcal's per-input full-tile bookkeeping dominates. Default
  stays `"jwst"` until COSMOS-Web spot-checks confirm the bias is
  acceptable for downstream catalog use.
- NIRCam combine phase no longer runs `skymatch`. The step has been removed
  from `COMBINE_STEPS` (and dropped from `STEP_NAMES`, `_SCI_MUTATING_STEPS`,
  `_STEP_LABELS`, and the `CFP_*` provenance keys), along with the
  `nircam/steps/skymatch.py` module and the `[nircam.skymatch]` config block.
  The step had been a silent no-op since it was wired through
  `Image3Pipeline` with every other substep skipped — `Image3Pipeline.process`
  only propagates `save_results` to `outlier_detection`/`resample`/
  `source_catalog`, so the modified models were never written to disk and
  the in-place SCI subtraction was discarded. Per-exposure background
  subtraction (the `sky` step, `CFP_SKY`) and the resample-time 2-D source-
  masked background (`SubtractBackground` inside `resample_step`) cover the
  remaining background work; existing reductions have effectively been
  running this two-pass setup all along, so this changelog entry records
  the removal of plumbing that wasn't doing anything rather than a change
  in pixel values. `outlier_step` and `resample_step` still pass
  `'skymatch': {'skip': True}` to their JWST `Image3Pipeline` calls — that
  keeps JWST's own skymatch substep disabled inside those calls and is
  unrelated to the orchestrator-level step we removed.

### Infrastructure
- NIRCam preview step: pass `format='png'` explicitly to `plt.imsave`.
  The `.tmp` suffix on the temp path made newer Pillow raise
  `KeyError: 'TMP'` during format sniffing.
- NIRCam preview step now writes a second native-resolution PNG
  (`{rootname}_full.png`) alongside the existing downsampled
  `{rootname}_preview.png`. Same ZScale stretch (computed on the
  downsampled array, reused for both), same `origin='lower'` orientation,
  no SCI/DQ mutation. The full-res PNG is uploaded to R2 by `campfire
  deploy nircam` and consumed by the in-browser polygon mask editor at
  `/admin/nircam/[id]`. SCI pixel data is unchanged.
- NIRCam: dedup + perf pass on the step-based pipeline.
  - `outlier.py`: O(N²) → O(N) overlap scan (replaced `filter_files.index(f)`
    in the per-visit loop with a `{path: sregion}` dict); extracted
    `_compute_overlap_inputs`, `_visit_is_up_to_date`, and
    `_write_outlier_manifest` to collapse ~100 lines of byte-identical
    preamble/epilogue between `outlier_step` and `outlier_step_campfire`.
  - `common/parallel.py`: removed the unconditional `sleep(1)` before every
    parallel dispatch (accumulated to >1 minute of pure wait per full run
    across 12+ per-exposure steps × N filters).
  - `nircam/manifest.py`: added `(size, mtime_ns)` fast-path to the input
    change-detection so up-to-date manifests skip the SHA-256 hash of
    SCI+DQ. Per-tile up-to-date checks no longer re-read ~4 GB of input
    data on a 200-input mosaic. Backward-compatible: missing prefilter
    fields fall back to hashing. Manifest writers (`create_manifest`,
    `_write_outlier_manifest`) now record size and mtime alongside hash
    via a shared `input_entry` helper.
  - `nircam/steps/resample.py`: collapsed 4 separate `fits.getdata` /
    `fits.getheader` calls plus a redundant SCI re-read into one
    `fits.open` block shared between the split-extensions and thumbnail
    paths.
  - `nircam/steps/diag_striping.py`: cached the `(y, x)` pixel-index grids
    in `_bin_indices` via `lru_cache(maxsize=4)` on a new `_pixel_grid(shape)`
    helper. The grids are shape-only (independent of θ) so the ~130
    angle-search calls per exposure each save ~32 MB of array allocation.
  - `nircam/steps/_flat.py`: new shared module with `resolve_flat` and
    `apply_flat_with_retry`. Removes a copy-paste pair between `wisp.py`
    and `striping.py` whose retry delays had accidentally diverged
    (`(0,5,5)` in striping vs `(0,3,10)` in wisp); settled on `(0,3,10)`
    with `delays` as a parameter for callers that want different timing.
  - `common/cfp.py`: new `cfp.should_skip(exposure_file, key, rootname,
    step_name, status, overwrite)` helper. Replaced the repeated
    `if not overwrite: already_done = (status.has(...) if status is not
    None else cfp.has_step(...)); if already_done: log(...); return`
    block across 12 per-exposure step modules (`detector1`, `wisp`,
    `striping`, `image2`, `edge`, `sky`, `variance`, `diag_striping`,
    `bad_pixel`, `apply_masks`, `jhat`, `preview`).
  - Trimmed narration comments in `orchestrate.py` and `outlier.py` that
    restated the next 1-2 lines of code.
  - Net `-127` lines across 18 files; new `_flat.py` helper.
- NIRCam: new `preview` per-exposure step, inserted as the penultimate
  process step (after `wcs_shift`, before `jhat`). Renders a downsampled
  ZScale-stretched grayscale PNG of the canonical SCI to
  ``{filter_dir}/{rootname}_preview.png`` for the web admin triage UI.
  Read-only — no SCI/DQ/ERR mutation. New `CFP_PREV` provenance key
  registered in the dependency chain so `cfpipe nircam reset --from <step>`
  invalidates the preview alongside any upstream re-run. Configurable via
  `[nircam.preview].max_dim` (default 1024) and `cmap` (default "Greys").
- NIRCam: removed `Field.get_excluded_exposures` (read from a deploy-
  generated ``reference/nircam/{field}/exposures.json`` contract) and the
  matching ``campfire deploy nircam pull`` subcommand. Nothing in the
  pipeline consumed the contract — exclusion was effectively a dead path.
  Reviewer-set exclusions in the web admin UI are now surfaced as a
  copy-paste list for the field's ``skip = [...]`` block in fields.toml,
  keeping fields.toml as the single source of truth for what the pipeline
  processes.
- NIRCam: `cfpipe nircam refcat query --backend hsc_ssp` is now a real
  query (previously raised `NotImplementedError`). Implements a thin
  stdlib client against the HSC SSP PDR3 async catalog_jobs API:
  submit/poll/download/cancel, credentials resolved from CLI flags →
  `HSC_SSP_USER`/`HSC_SSP_PASSWORD` env vars → `~/.netrc` machine
  `hsc-release.mtk.nao.ac.jp`, and stripped from the ECSV provenance
  block. New `--hsc-release {auto,wide,dud}` flag: `auto` (default)
  uses a coordinate-based check against hard-coded DUD field bounding
  boxes (COSMOS / DEEP2-3 / ELAIS-N1 / XMM-LSS), routing the cone to
  `pdr3_dud_rev.summary` and/or `pdr3_wide.summary` and vstacking when
  it straddles a DUD edge. DUD queries pre-filter with `tractSearch`
  using the field's known tract envelope; Wide queries rely on
  `coneSearch` alone. Output uses cmodel mags (`{band}_cmodel_mag`,
  default `i`); `--no-point-sources` toggles the
  `{band}_extendedness_value < 0.5` stellar cut.
- NIRCam: reject WFSS/TSGRISM exposures from the imaging pipeline at
  three layers. The MAST downloader (`cfpipe download --instrument
  nircam`) now filters uncal products by their *per-product* `exp_type`
  (JWST visits regularly bundle imaging + WFSS exposures into a shared
  MAST fileset, so the search-level fileset condition isn't enough), and
  a belt-and-suspenders post-download pass reads `EXP_TYPE` from the
  FITS primary header of every newly-fetched and previously-present
  uncal and unlinks anything that doesn't match `--exp-type`. The kept
  `exp_type` is recorded in `manifest.ecsv`. The orchestrator's
  `_run_detector1` and `image2_step` read `EXP_TYPE` and skip
  `NRC_WFSS` / `NRC_TSGRISM` exposures so a stale grism canonical
  doesn't reach `Image2Pipeline`. Without this, routing grism through
  the imaging pipeline raised a cryptic `MatchFitsTableRowError` from
  `photom.find_row` because NIRCam's photom table has one row per
  `(filter, pupil, order)` for grism but the imaging branch matches
  only `filter+pupil`.
- NIRCam: new `cfpipe nircam expmap` command. Builds per-filter exposure
  maps by stacking each input's `S_REGION` polygon weighted by `XPOSURE`
  into an auto-sized TAN WCS (no tile dependency, no drizzling — exposure
  time is a scalar per-exposure property, so polygon-mask × XPOSURE is the
  correct accumulator). Outputs `expmap_{filter}_{stage}.fits` (BUNIT='s',
  WCS in header), a matching diagnostic PDF with RA/Dec gridlines, and a
  combined `footprints_{stage}.reg` ds9 file color-coded by filter.
  Supports `--stage uncal` (raw quick-look) and `--stage canonical`
  (post-jhat). Default pixel scale 0.5"/pix; per-filter parallelism via
  `-p`.
- NIRCam `expmap` polish:
  - **Shared WCS across filters**: the auto-WCS is now sized to enclose
    the union of S_REGION polygons across *every* filter in the
    invocation (rather than per-filter), so per-filter expmaps are
    pixel-registered and can be stacked or differenced directly.
  - **Shared colorbar (vmin/vmax) across filters**: PDFs are now
    rendered with a single LogNorm computed from the union of nonzero
    pixels across every filter, so the diagnostic plots are visually
    identical apart from the data — flip through them to compare.
    PDFs are always regenerated (the shared norm is invocation-
    dependent); FITS files keep their up-to-date short-circuit. The
    `ΣXPOSURE` line is dropped from the plot title.
  - **Field name in output filenames**: now
    `expmap_{field}_{filter}_{stage}.{fits,pdf}` (was
    `expmap_{filter}_{stage}.{fits,pdf}`).
  - **Header-scan progress**: per-filter `tqdm` bar while reading
    XPOSURE/S_REGION headers in phase 1 (previously silent).
  - **Threaded header scan + persistent metadata cache**: phase-1 header
    reads are now dispatched through an 8-way `ThreadPoolExecutor`
    (header I/O is GIL-friendly and IOPS-bound, so threads give
    near-linear speedup on network FS like CANDIDE). Per-file metadata
    (XPOSURE + parsed S_REGION) is cached to
    `{out_dir}/.expmap_cache.json`, keyed by `(abspath, mtime_ns, size)`
    so the cache self-invalidates on rsync or pipeline re-reduction.
    Cache hits skip the FITS open entirely; repeat invocations of
    `cfpipe nircam expmap` complete near-instantly.
  - **PDF colormap**: switched from `Greys` (lowest-exposure pixels
    indistinguishable from white background) to `magma` with zeros
    masked, and dropped `vmin` from the 5th percentile to the actual
    nonzero minimum. Low-exposure edges are now clearly visible against
    the off-footprint background.
- NIRCam `resample` step (campfire-native drizzle path): log a
  `[i/N] drizzled <basename>` line per input exposure, mirroring the
  per-exposure progress the JWST `Image3Pipeline` path already prints.
  Inputs that don't overlap the tile log `[i/N] <basename>: no tile
  overlap, skipping` in place of the prior batched summary.
- NIRCam `outlier` step: keep stcal's on-disk median scratch out of the
  user's home directory. `MedianComputer` creates its temp dir via
  `tempfile.TemporaryDirectory(dir=tempdir)`, where the stcal default
  `tempdir=""` resolves against the **current working directory**
  (not `$TMPDIR`). On networked-FS clusters like CANDIDE, CWD is the
  user's home, so every visit's median buffer (`tmpXXXX/N.bin` per
  section) accumulated against the home quota. Both outlier paths are
  fixed: the campfire path (`outlier_detect_for_visit`) now passes
  `tempdir=tempfile.gettempdir()` when no explicit tempdir is given,
  and the jwst path (`outlier_step`) `chdir`s into its `outlier-*`
  scratch (which already lives under `$TMPDIR` and is auto-cleaned)
  for the duration of `Image3Pipeline.call`, so the implicit CWD-rooted
  scratch lands inside the scratch dir.
- NIRCam orchestrator: skip the `_scan_status` pre-scan when
  `--overwrite` is set, returning an empty `StepStatus` instead. With
  `--overwrite`, every step runs regardless of prior state, so the
  pre-scan's only product (cached "this CFP key is already present"
  decisions) is unused for skip purposes. But `StepStatus.mark_all`
  only adds keys to the cache and never removes them, so the
  pre-scanned snapshot went stale mid-phase whenever a fresh-model
  step (`detector1`, `image2`) stripped prior CFP_* keys and
  non-schema extensions like `WCS_BAK` from disk on its rerun. The
  symptom was a `wcs_shift` `RuntimeError` during a `process
  --overwrite` rerun ("CFP_SHFT is set but WCS_BAK extension is
  missing"): the cache lied that `CFP_SHFT` was still present after
  `image2` had wiped both it and `WCS_BAK` from disk, so `wcs_shift`
  took the restore-and-reapply branch and found the backup gone.
  With an empty cache, `StepStatus.has` falls back to a live
  `cfp.has_step` read for any path not yet seen, so in-step checks
  match disk reality at the moment they fire.
- NIRCam `outlier` step: dispatch one visit per worker via
  `common.parallel.dispatch` so the combine phase honors `--processes N`
  past `apply_mask`/`bad_pixel`. Previously visits ran sequentially and
  `n_processes` was silently dropped past those two steps. Each visit
  writes only to its own canonical files (atomic_save); cross-visit
  overlap files are read-only inputs and outlier_detection only adds
  DQ bits, so parallel runs cannot crash. The only semantic difference
  vs. serial is that a worker may read an overlap file's DQ before the
  visit owning that file has stamped its new outlier bits — a small
  median bias in those overlap pixels. Intra-program overlap scoping
  (the default) keeps the affected footprint small. Use `--processes 1`
  for a strictly ordering-stable run.
- NIRCam `jhat` step: stage the JHAT-aligned exposure to a sibling `.tmp`
  on the products filesystem before the atomic rename, instead of
  `os.replace`-ing directly out of the `tempfile.TemporaryDirectory`
  scratch area. On networked-FS clusters where `TMPDIR` is node-local
  (e.g. CANDIDE: `/tmp` per compute node, products on `/n23data2/...`),
  the direct rename failed with `OSError: [Errno 18] Invalid cross-device
  link`. Copying into `<canonical>.tmp.fits` first puts the rename within
  the products device, preserving atomicity, and keeps JHAT's many
  intermediate writes on fast local scratch.
- Cap BLAS/OpenMP thread counts to 1 by default for all `cfpipe` runs
  (`OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `OMP_NUM_THREADS`,
  `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`, `BLIS_NUM_THREADS`),
  set only when not already in the environment. Pipeline stages
  parallelize via fork-pool processes; without this, each worker spawns
  one BLAS thread per visible core and on high-core HPC nodes (e.g.
  candide, 64 cores) the collective thread count exhausts
  `RLIMIT_NPROC`. Symptom on candide was JHAT failing mid-run with
  cascading `OpenBLAS blas_thread_init: pthread_create failed for
  thread N of 64: Resource temporarily unavailable` warnings followed
  by spurious `KeyboardInterrupt` tracebacks inside
  `astropy.modeling.core` (workers losing a thread-spawn race during
  the `world_to_detector` transform on the full reference catalog in
  `jhat/simple_jwst_phot.py`). Implemented as a tiny
  `campfire_pipeline._thread_caps` shim imported as the first thing in
  every CLI entry point (before `matplotlib`/`numpy`), with the same
  defaults applied in `setup_environment` for the programmatic-import
  path. Override per-run via `[environment].OPENBLAS_NUM_THREADS = "N"`
  in your config or by exporting the variable before `cfpipe`.
  Numba-parallel hot paths (NIRSpec redshift fitting,
  `common.spectral` LSF resampling) are unaffected — they use
  `numba.set_num_threads(ncores)` against `NUMBA_NUM_THREADS`, which we
  do not pin.
- Switch `common.parallel.dispatch` from the platform-default multiprocessing
  start method (`fork` on Linux, `spawn` on macOS) to an explicit `forkserver`
  context. Forking 32 workers from a multi-GB parent — the state after
  `persistence` even with batch-by-detector cleanup, since glibc malloc
  doesn't return arena memory to the OS — was failing on candide with
  `OSError: [Errno 12] Cannot allocate memory` at `os.fork()` because Linux
  commit accounting requires `parent_RSS × n_workers` of committable memory
  upfront, regardless of copy-on-write. Forkserver launches a small helper
  early in the run; subsequent worker forks come from that ~tens-of-MB helper
  rather than the bloated main process. Heavy scientific imports (`numpy`,
  `scipy`, `astropy.io.fits/wcs/table`, `jwst`, `jwst.datamodels`,
  `stdatamodels`, `stcal`, `crds`, `snowblind`, plus campfire common
  modules) are listed in `set_forkserver_preload` so workers inherit them
  COW from the helper instead of re-importing per pool. `jhat` and
  `tweakreg` are intentionally not preloaded (they touch the CRDS singleton
  in a way that locks the context — see `feedback_lazy_jwst_imports`); they
  remain lazy imports inside the worker functions that need them. Behaviour
  is unchanged on macOS (already used `spawn`-equivalent semantics by
  default); the change is the cross-platform consistency and the Linux
  ENOMEM fix.
- NIRCam `persistence` step: hand snowblind one detector at a time instead of
  the whole filter. Snowblind's `process()` does `results = images.copy()`,
  deep-copying every model's SCI/ERR/DQ; with a full SW filter that doubled
  the working set into multi-GB territory and leaked into the next step.
  Per-detector batching caps peak at `exposures_per_detector × 2` (≈8× win
  for SW, 2× for LW). Also explicitly closes input models after each batch
  (snowblind copies them, so the originals are independent objects whose
  asdf-backed arrays don't always release on refcount alone) and
  `gc.collect()`s between detectors so the parent process is lean before
  wisp/striping dispatch.
- Fix CRDS serverless-mode lock-in on machines without `CRDS_SERVER_URL` set
  in the shell. The `jhat` step's `from jhat import align_wcs_batch` ran at
  module load and transitively imported `stpipe → crds` before
  `setup_environment()` populated `CRDS_SERVER_URL`, locking CRDS's module-
  level proxy into serverless mode for the rest of the process. Moved the
  import into `jhat_step()` so it fires after env setup. Symptom was
  `CrdsNetworkError: Failed downloading cache config from: JSON RPC service
  at 'https://crds-serverless-mode.stsci.edu'` even with a reachable server
  and `cfpipe info` showing the correct URL.
- `cfpipe download` accepts a positional filter: `--target` (object name or
  `"RA Dec"` decimal-degree string, repeatable) plus `--radius` /
  `--radius-units` (default 3 arcmin; server cap 30 arcmin). Forwarded to the
  MAST JWST search API's native `target`/`radius` fields, so spatial pruning
  happens server-side and returns only filesets within the cone.
- `cfpipe download`: parallelized the MAST `/list_products` step. Batches
  (still 25 filesets each — bigger batches push past the server's per-request
  budget and time out) are dispatched concurrently via a ThreadPoolExecutor
  with workers from `--processes` (default 4), and each request retries on
  429 / 5xx / transient network errors with exponential backoff that honours
  the `Retry-After` header. Cuts the product-listing phase ~`workers`×.
- New `cfpipe nircam rgb --field <name>` subcommand: combines per-filter
  per-tile mosaics produced by `cfpipe nircam combine` into trilogy-style
  RGB PNGs (one native-resolution PNG plus one downsampled preview per
  tile, written next to the per-filter products under
  `products/nircam/<field>/<tile>_<pixscale>_rgb[_preview].png`). Filter
  channel weights and stretch tunables (`noisesig`, `noiselum`,
  `satpercent`) come from a new optional `[<field>.rgb]` block in
  `fields.toml`; pixel scale defaults to `[nircam.resample].pixel_scale`
  and is overridable via `--pixel-scale`. The trilogy stretch core lives
  in `nircam/trilogy.py` (small, dependency-light copy of the algorithm
  in `python/campfire/deploy/tiles_engine.py` — to be consolidated when
  the deploy-side tile generator is rewired to read from NIRCam outputs
  directly). Pipeline-only; produces no FITS, does not build a tile
  pyramid.
- NIRCam striping: removed the unused `find_optimal_threshold` maskparam
  sweep (an 11-point per-exposure search that was dead code under the default
  asymmetry-based fallback) and the legacy mask-fraction code path
  (`CAMPFIRE_STRIPING_METHOD` env var, `maskparam` config key). The
  asymmetry-based per-row fallback introduced in `bb348f4` is now the only
  behavior. CFP_1F header is now stamped with the asymmetry/prefilter
  thresholds instead of the (always-overwritten) maskparam.
- NIRCam now serializes a CRDS reference-file pre-fetch pass before parallel
  `detector1` / `wisp` / `striping` / `image2` dispatch. Mirrors the existing
  NIRSpec pattern (`nirspec/stage1.py`, `nirspec/stage2.py`): one
  `crds.getreferences()` call per unique `(DETECTOR, READPATT, SUBARRAY)` for
  Detector1Pipeline reftypes and one per `(DETECTOR, FILTER, PUPIL)` for
  Image2Pipeline reftypes (covers the `flat` lookup used by wisp/striping
  too). Fixes "empty or corrupt FITS" / "no SIMPLE card found" failures
  caused by multiple workers racing to download the same reference file on
  cold-cache runs. No-op when `--processes 1`. Wired in
  `nircam/orchestrate.py::run_process` and (for CRDS-touching steps only)
  `run_step`; new module at `nircam/prefetch.py`.
- NIRCam `fields.toml` now supports bash-style brace expansion in `files`
  patterns (e.g. `'jw01727{001,002,003}*'` → three patterns), and a
  field-wide top-level `skip = [...]` exclude list that applies to every step
  resolving exposures via `Field.get_uncal_files` / `get_exposure_files`.
  Both lists go through the same `_expand_braces` pre-filter; per-step
  `files_to_skip` (e.g. under `[field.resample]`) stacks on top of the
  field-wide list. Skip patterns must start with `jwNNNNN` like `files`.
- `cfpipe download` now writes raw uncal files to a PID directory named with
  the unpadded integer program ID (e.g. `raw/1727/...` and
  `raw/nircam/1727/{filter}/...`), instead of the 5-digit zero-padded form
  (`raw/01727/...`). The NIRCam field-config PID extractor strips leading
  zeros from `jwNNNNN*` patterns to match. Existing downloads under
  `raw/0NNNN/` need to be renamed (or re-downloaded) to the unpadded form.
- NIRCam diagnostic plots extended across the per-exposure and mosaic
  steps. Previously only `striping` and `wisp` produced diagnostic PDFs.
  Adds: `<rootname>_sky.pdf` (histogram of the masked sky-pixel
  distribution with the fitted Gaussian and pedestal overlaid, plus
  before/after SCI stamps); `<rootname>_outlier.pdf` (SCI snapshot plus
  newly flagged OUTLIER pixels — works for both
  `[nircam.outlier].implementation = "jwst"` and `"campfire"` paths);
  `<mosaic>_thumb.png` (block-mean-downsampled ZScale render of the
  final i2d, default 4× downsample, axis-free PNG); and
  `<mosaic>_bkgsub.png` (three-panel PNG: pre-bkgsub, post-bkgsub,
  background model, with shared SCI ZScale on the first two and a
  symmetric diverging colormap on the model panel — diagnostic for
  over-subtraction of extended sources). Each is gated behind a
  `plot = true` flag in the corresponding config block
  (`[nircam.sky]`, `[nircam.outlier]`, `[nircam.resample]`); mosaic
  downsample factor is configurable via
  `[nircam.resample].plot_downsample`. `fit_sky_tot` gains a
  `return_diagnostics=True` mode that returns the full Gaussian
  `popt` alongside the fitted mean so the histogram overlay aligns
  without re-fitting.
- NIRCam products directory is now flat per (field, filter). The previous
  layout nested outputs under `products/nircam/<field>/exposures/<filter>/`
  (canonical FITS, plus `diagnostics/` and `manifests/` subdirs) and
  `products/nircam/<field>/mosaics/<filter>/` (i2d files, plus
  `extensions/` and `manifests/` subdirs); everything now lives directly
  in `products/nircam/<field>/<filter>/`. `Field.exposures_dir` and
  `Field.mosaic_dir` are removed; the new `Field.filter_dir(filter_name)`
  returns the single per-filter directory used by every step (detector1,
  wisp/striping/jhat diagnostics, outlier and mosaic manifests, mosaic
  i2d + split extensions). The `jw*` field globs naturally exclude
  `mosaic_*` outputs from `get_exposure_files`. No change to filenames
  or FITS contents; existing reductions need to be re-run (or relocated)
  to populate the new layout.
- NIRCam: new `cfpipe nircam refcat {query,extract,merge,compare}` utility
  for building and managing astrometric reference catalogs. ``query``
  pulls from Gaia DR3 (astroquery) or Legacy Surveys DR10 (NOIRLab TAP);
  ``extract`` runs SEP-on-SNR detection + Kron/circle photometry on a
  mosaic to bootstrap relative-alignment refcats from an absolutely-aligned
  filter; ``merge`` stacks catalogs with positional dedup (first wins);
  ``compare`` reports ΔRA/ΔDec residuals between two catalogs with a 2D
  histogram diagnostic. Output schema (`RA`, `DEC`, `mag`, `mag_err` ECSV)
  matches what `[<field>.jhat.refcat_dict]` already consumes. Adds `sep`,
  `astroquery`, and `pyvo` to the pipeline dependencies. (`nircam/refcat/`)
- NIRCam mosaic-level background subtraction (`nircam/bkgsub.py`) is now
  ~10–50× faster on COSMOS-Web-scale tiles. The dominant cost — the
  ring-median filter at `radius=80, width=4` — now runs on a block-reduced
  copy of the SCI array (configurable via `[nircam.stage3].ring_downsample`,
  default `4`); the result is bilinearly zoomed back to full resolution
  before subtraction. The ring-median is by construction a smooth
  large-scale estimator, so this is equivalent to within sampling noise.
  Per-tier dilation in `tier_mask` switches from `binary_dilation` with a
  large `circular_footprint` to `scipy.ndimage.distance_transform_edt`
  thresholded at the dilate radius (O(N) instead of O(N×footprint), and
  bit-identical for integer radii since `circular_footprint` is itself an
  integer Euclidean disk). Tier convolution moves from
  `astropy.convolution.convolve_fft` to `scipy.ndimage.gaussian_filter`
  (separable, C-optimized, no full-image FFT). The biweight scale/location
  used by every tier is hoisted out of the tier loop in `mask_sources`
  (single pass over the unmasked image instead of four), as is the
  filled-image array fed to the smoothing kernel. `np.choose` is replaced
  by `np.where` throughout. No CRDS / pipeline / output-format change;
  default behaviour for the per-exposure variance step is unchanged
  (`ring_downsample` defaults to `1` in the dataclass and is only enabled
  in `[nircam.stage3]`).
- NIRCam `resample_step` extracts its per-tile drizzle body into
  `_drizzle_tile_via_jwst(selected_files, output_path, *, crpix, crval, shape,
  rotation, pixel_scale, resample_cfg, reduction_version)`. The function
  builds the ASN, runs `Image3Pipeline` with every substep but `resample`
  skipped, and stamps `CMPFRTIM` / `CMPFRVER` on the i2d primary header.
  No behavior change — sets up a clean swap point for the upcoming
  campfire-native drizzle (issue #138).
- NIRCam tile `corners` are now optional in `fields.toml`. If omitted, the
  pipeline derives the tile sky polygon from the first `<scale>mas`
  subsection (`crpix` + `naxis`) plus the tile/field `tangent_point` and
  `rotation`, so a tile that already specifies its WCS doesn't need to
  duplicate the same information as a hand-typed corner list. Existing
  `corners` entries continue to override the WCS-derived polygon.
- NIRCam tiles only need to declare `crpix`/`naxis` at one pixel scale.
  `Field.get_tile_wcs(tile, pixel_scale=...)` now rescales `crpix` and
  `naxis` from any defined subsection to the requested pixel scale (the
  tile covers the same sky region at every scale); explicit `[<scale>mas]`
  blocks still take precedence when present. Resampling at `60mas` no
  longer requires duplicating a `30mas` definition (or vice-versa).
- `compute_file_hash` (NIRCam mosaic manifests) now opens FITS with
  `do_not_scale_image_data=True` so memmap stays available on extensions
  that carry `BZERO`/`BSCALE`/`BLANK` keywords. The previous behavior
  raised `ValueError: Cannot load a memory-mapped image` on the first
  manifest write for a visit whose CRF outputs were stored with integer
  scaling (jwst 1.20.x).
- Pin `pandas<3` to keep `jhat` 0.3.6 working. pandas 3.0 removed the
  `delim_whitespace` keyword that `jhat/pdastro.py` still passes to
  `pd.read_csv` / `pd.read_table` when loading reference catalogs, which
  caused the jhat WCS-alignment step to crash on every exposure under
  pandas 3.x. Lift this pin once a pandas-3-compatible `jhat` release is
  available on PyPI.
- NIRCam orchestrator pre-scans every canonical exposure's primary header
  once at the top of `run_process` / `run_combine` / `run_step` and caches
  the set of present `CFP_*` keys in a `StepStatus` object
  (`nircam/status.py`). Each per-exposure step now filters out
  already-stamped files *before* spinning up the multiprocessing pool, so
  no-op passes on a finished field skip the worker spin-up entirely
  (worker processes use `spawn` on macOS, so each one re-imports
  astropy/jwst — that overhead used to be paid once per step regardless
  of whether any work needed to happen). Skymatch and outlier likewise
  short-circuit whole visits whose every member is already up-to-date.
  No change to outputs; `cfp.has_step` remains the fallback path for
  ad-hoc/CLI callers (`status`, `reset`, standalone scripts) and as a
  defensive check inside each step.
- NIRCam `Detector1Pipeline` no longer writes `_rateints.fits`, `_output_pers.fits`,
  `_trapsfilled.fits`, or `_persistence.fits` intermediates. Pipeline-level
  `save_results` is now `False` and the returned rate model is saved explicitly;
  `persistence.save_persistence` and `persistence.save_results` are likewise
  `False`. `_jump.fits` is still written (the jump substep keeps
  `save_results=True`) because `PersistenceFlagStep` reads `groupdq` from it,
  and is removed by the persistence step's cleanup. No change to pixel values
  or `_rate.fits` contents.
- `cfpipe download --instrument nircam` now resolves the per-detector filter
  from MAST's `opticalElements` field (e.g. `"F090W;CLEAR, F410M;CLEAR"`)
  instead of using the fileset's top-level `filter`. Previously a request for
  a single filter pulled in all 10 detectors of every matching fileset and
  tagged them all with the searched filter — so SW detectors landed under
  the LW filter directory (and vice versa) with bogus filter metadata that
  then propagated into `manifest.ecsv` and downstream stages. Pupil-mounted
  narrowbands (`F150W2;F162M`-style) are also handled. Files whose actual
  filter isn't in `--filters` are dropped with a count printed.
- Foundation pieces for the upcoming NIRCam canonical-exposure restructure:
  `common.io.atomic_save` (tmp+rename with optional primary-header updates
  applied in the same atomic operation), the `common.cfp` provenance module
  (ordered `CFP_KEYS`, plus `format`/`has_step`/`get_steps`/`clear_from`),
  and additive `Field.exposures_dir` / `get_exposure_files` /
  `get_exposure_path` getters. Existing stage dirs and getters are
  unchanged; the current pipeline is unaffected.
- Per-step modules `nircam/steps/detector1.py` and `nircam/steps/persistence.py`
  rewritten against the canonical exposures layout (one file per exposure,
  CFP_DET1 / CFP_PERS stamped via `atomic_save`). Not yet wired into a CLI;
  legacy `stage1.py` orchestrator continues to drive the pipeline. The new
  persistence step also moves earlier in the sequence (right after
  detector1) so the 1/f striping source-mask construction sees persistence
  DQ flags — this becomes a real behavior change when the new orchestrator
  lands.
- Per-step modules `nircam/steps/wisp.py` and `nircam/steps/striping.py`
  also written against the canonical layout. Wisp drops the
  `_rate_without_wisps_sub.fits` backup (PDFs generated inline with the
  in-memory before/after arrays). Striping replaces the
  `_rate_1fmask.fits` sidecar with a `SRCMASK` extension on the canonical
  file, written atomically alongside the SCI mutation via the new
  `atomic_save(..., extra_hdus=...)` parameter. Diagnostic PDFs land in
  `exposures/<filter>/diagnostics/`. A small shared
  `nircam/steps/_plots.py` carries the `plot_two` helper so the new
  modules don't import from `stage1.py`.
- Per-step modules `nircam/steps/{image2,edge,sky,variance}.py` round out
  the calibrate-phase per-exposure rewrites. image2 runs
  `Image2Pipeline.call(input, save_results=False)` and atomic-saves the
  returned cal-stage model to the canonical path, re-attaching the
  `SRCMASK` extension that the JWST pipeline doesn't carry through. sky
  reads `SRCMASK` from the canonical file's extension instead of the
  former `_rate_1fmask.fits` sidecar in `stage1_dir`. variance uses a new
  `SubtractBackground.compute()` method that performs the source-rejection
  + background fit in memory only — the legacy `_cal_bkgsub.fits` scratch
  file is no longer written. `SubtractBackground.call()` is refactored as
  a thin wrapper around `compute()` plus the existing FITS write so the
  mosaic-level usage in stage3 is unaffected.
- Per-step module `nircam/steps/jhat.py` finishes the calibrate-phase
  rewrites. Runs `jhat.align_wcs_batch` against a private scratch dir
  (one `TemporaryDirectory` per worker), stamps `CFP_JHAT` with the
  refcat name on the scratch output, then atomic-replaces the canonical
  file. JHAT preserves all FITS extensions through its WCS update so the
  `SRCMASK` extension carries through unchanged. Diagnostic PDFs and
  photometry tables are copied from the scratch dir to
  `exposures/<filter>/diagnostics/` before the scratch dir is cleaned up.
- Mosaic-phase per-step modules
  `nircam/steps/{apply_masks,bad_pixel,skymatch,outlier,resample}.py`.
  apply_masks rebuilds a `CFMASK` extension from the user `.reg` files
  on every run (replaces any existing CFMASK; OR's into DQ — DQ is
  cumulative, so mask removal requires `--reset-from apply_masks`).
  bad_pixel splits into a `build_bad_pixel_masks` ensemble step (writes
  `fl_pixels_<filter>_<detector>.fits` reference products) and a
  per-exposure `bad_pixel_step` that ORs the per-detector mask into DQ.
  skymatch and outlier both run JWST `Image3Pipeline` in a private
  scratch dir per visit, stamp `CFP_SMAT` / `CFP_OUT` on the scratch
  outputs, and atomic-replace the canonicals (with belt-and-suspenders
  capture/restore of `SRCMASK`/`CFMASK`). Outlier manifests now live in
  `exposures/<filter>/manifests/`. resample switches input source to
  `field.get_exposure_files(filter, with_step='CFP_OUT')` so only
  outlier-detection-finished exposures are eligible to be drizzled;
  mosaic outputs and the manifest format are unchanged. `CFP_SMAT` is
  added to `common.cfp.CFP_KEYS` between `CFP_BPIX` and `CFP_OUT`.

### Infrastructure (continued)
- `cfpipe nircam status --field <name>` reads CFP_* keywords across all
  canonical exposures and prints a per-step completion table plus a
  per-step summary (done / skipped / total). Reads each FITS primary
  header once via `cfp.get_steps`.
- `cfpipe nircam reset --field <name>` for clearing pipeline state.
  `--from <step>` clears the named CFP key and every later one on each
  canonical exposure (header-only, atomic via tmp+rename); refused for
  SCI-mutating steps (wisp, striping, image2, sky, variance, skymatch)
  since re-running them on already-mutated data would compound the
  effect. `--uncal` deletes every canonical exposure file (and any
  ``_jump.fits`` sidecars) for the selected filters; reference products
  (`bad_pixel_dir`, `refcat`, mask `.reg` files) and diagnostic PDFs
  are kept. Both modes prompt for confirmation; pass `--yes` to skip.

### Infrastructure (cleanup)
- Removed the legacy NIRCam stage modules (`stage1.py`, `stage2.py`,
  `stage3.py`) and the `engine.py` `ReductionEngine` wrapper. Their
  numerical helpers (`fit_pedestal`, `fit_sky`, `fit_sky_tot`,
  `collapse_image`, `find_optimal_threshold`,
  `measure_fullimage_striping`) move to a new `nircam/skyfit.py` module
  which `steps/striping.py` and `steps/sky.py` now import from.
  `Field.stage{1,2,3}_dir`, `Field.stage_overrides`, and the
  `get_rate_files` / `get_cal_files` / `get_jhat_files` /
  `get_all_jhat_files` / `get_crf_files` / `get_files` getters are
  removed; `Field.exposures_dir` and `get_exposure_files` /
  `get_exposure_path` are the single source of truth for per-exposure
  paths. `manifest.get_stale_tiles` switches its CRF glob to
  `field.get_exposure_files(filter, with_step='CFP_OUT')`. The
  `get_nircam_stage_config` helper is dropped from `config.py` (only
  `get_nircam_step_config` remains).

### Algorithm
- NIRCam pipeline restructured into a two-phase canonical-exposure flow.
  `cfpipe nircam process` runs the per-exposure work (detector1 →
  persistence → wisp → striping → image2 → edge → sky → variance →
  jhat) into a single canonical FITS file per exposure at
  `products/nircam/<field>/exposures/<filter>/<rootname>.fits`. User
  intervention (region masks in `mask_dir/<filter>/<rootname>.reg`,
  exclusion contract) sits between the two phases. `cfpipe nircam
  combine` runs the ensemble work (apply_mask → bad_pixel → skymatch →
  outlier → resample), promoting per-visit Image3Pipeline outputs back
  to the canonical paths via atomic_save. Persistence moves earlier in
  the sequence (immediately after detector1 instead of last in the
  per-exposure flow), so the 1/f striping source-mask construction now
  sees persistence DQ flags — small calibration delta. Snowblind's
  `jumpify` expects a `_rate.fits` filename, so the persistence step
  temporarily munges `meta.filename` around the call. Single canonical
  file per exposure replaces the old rate/cal/jhat/crf chain and the
  `_rate_orig`/`_rate_without_wisps_sub`/`_rate_1fmask`/`_cal_bkgsub`
  scratch files. NIRCam config namespace flattens from
  `[nircam.stage{1,2,3}.<step>]` to `[nircam.<step>]`. Per-field
  overrides in `fields.toml` use the matching flat layout
  (`[<field>.<step>]`). Legacy `cfpipe nircam stage{1,2,3}` CLI
  commands are removed; the new CLI is `process` / `combine` /
  `<step>` / `run` / `check` (status and reset land next). The
  `Image2Pipeline` round-trip drops custom extensions, so image2 and
  the Image3Pipeline-driven steps capture/restore `SRCMASK` / `CFMASK`
  via `atomic_save(extra_hdus=...)`.

## v0.4.0 — 2026-05-04

### Algorithm
- NIRSpec now includes an optional masking step between stage1 and stage2, 
  to mask specific regions contaminated by artifacts (e.g., shorts) which
  then get set as DQ `DO_NOT_USE`. 
- NIRSpec optimal extraction now falls back to a 3-pixel boxcar when the
  in-aperture collapsed cross-dispersion profile is corrupted (fewer than
  3 finite positive pixels, or positive flux / total |flux| below 0.5),
  e.g. due to background over-subtraction. Previously such cases produced
  a near-delta-function profile from the single surviving positive pixel
  and a degenerate optimal extraction. The chosen extraction method is
  recorded in the new `CMPFROPT` primary-header keyword (`'optimal'` or
  `'boxcar-3px'`); the QA profile plot is relabeled accordingly when the
  fallback triggers.

### Infrastructure
- Switched version resolution to `setuptools-scm` with a `pipeline-v*` tag
  prefix, scoping releases to the pipeline subpackage rather than the monorepo
  HEAD. The reduction version embedded in FITS (`CMPFRVER`) is now PEP 440 —
  e.g. `0.4.0` for releases, `0.4.1.dev3+g7f4e2c1.d20260504` for dev builds —
  rather than a raw monorepo git short SHA.
- Added `scripts/release-pipeline.sh` and the `/pipeline-release` Claude Code
  slash command to drive the tag-and-push workflow.
- Scoped the `CMPFRVER` dirty-flag check to `pipeline/`, so edits in `web/`,
  `python/`, `supabase/`, etc. no longer flip `+dDATE` on the pipeline
  version string (#135). `git describe --dirty` checks the whole working
  tree; we now call `git describe` (no `--dirty`) and pair it with a
  pipeline-scoped `git status --porcelain -- pipeline`.

## v0.3.0 — legacy

Initial unified `cfpipe` package version, prior to this changelog format and
prior to setuptools-scm. See `git log -- pipeline/` for the change history up
to this point.
