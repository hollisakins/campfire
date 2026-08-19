# NIRCam Pipeline

The CAMPFIRE NIRCam pipeline reduces raw JWST/NIRCam imaging into background-subtracted, astrometrically calibrated mosaics. Like the [NIRSpec pipeline](/docs/reduction/nirspec), it is a wrapper around the STScI [JWST calibration pipeline](https://jwst-pipeline.readthedocs.io/) — the standard detector-level calibration, flat-fielding, photometric calibration, and drizzling are inherited from `jwst` — with custom steps targeting the artifacts and systematics that matter most for deep extragalactic imaging: snowball residuals, wisps, 1/f striping, scattered light, sky background, and astrometric registration in crowded fields. The pipeline builds on the heritage of the CEERS and COSMOS-Web reductions (M. Bagley and M. Franco, respectively), with substantial subsequent development.

This page describes the reduction stage by stage, focusing on where and why CAMPFIRE departs from the stock pipeline. The stock steps themselves are documented in detail in the [JWST pipeline documentation](https://jwst-pipeline.readthedocs.io/) and are only summarized here.

## Overview

NIRCam reduction is organized by **field** — a contiguous sky region with an associated set of filters and output mosaic tiles (e.g. a cluster pointing, or a contiguous survey mosaic), potentially drawing exposures from multiple JWST programs. Processing runs in two phases:

**Per-exposure processing** — each exposure is carried from the raw ramp to a calibrated, background-subtracted, aligned image:

| Step | What it does | Origin |
|------|--------------|--------|
| Detector processing | Ramp fitting, jump detection, snowball flagging | Stock `jwst`, retuned |
| Jackknife correction | Removes ramp-fitting zero-point bias at snowballs/cosmic rays | CAMPFIRE |
| Persistence flagging | Flags persistence from earlier bright exposures | [`snowblind`](https://github.com/mpi-astronomy/snowblind) |
| Wisp subtraction | Removes scattered-light "wisps" on the affected SW detectors | NMF model + CAMPFIRE fitting |
| Flat field & photometric calibration | WCS assignment, flat-fielding, calibration to MJy/sr | Stock `jwst` |
| Edge flagging | Masks noisy detector border rows/columns | CAMPFIRE |
| Background & 1/f subtraction | Unified sky background, per-amplifier pedestal, and 1/f noise removal | CAMPFIRE |
| Diagonal striping removal | Removes angled scattered-light stripes (opt-in, per field) | CAMPFIRE |
| Astrometric alignment | Per-exposure WCS correction against a Gaia-tied reference catalog | [JHAT](https://jhat.readthedocs.io/) / CAMPFIRE |

**Mosaic combination** — the aligned exposures for each filter are combined into mosaic tiles:

| Step | What it does | Origin |
|------|--------------|--------|
| Manual masks | Applies reviewer-drawn artifact masks from visual inspection | CAMPFIRE |
| Bad-pixel rejection | Ensemble identification of consistently bad pixels (opt-in) | CAMPFIRE |
| Outlier detection | Cosmic-ray and artifact rejection via dither-median comparison | Stock `jwst`, custom orchestration |
| Resampling | Drizzles each tile at 30 mas, then subtracts a mosaic-level background | Stock `jwst` drizzle + CAMPFIRE background |

Every product records full provenance: each processing step stamps the file header with what ran and with which parameters, and mosaics carry the exact pipeline version (`CMPFRVER`) and a manifest of their input exposures.

### What we turn off in the stock pipeline, and why

Several stock `jwst` steps are deliberately disabled and replaced:

| Stock step | Status | Replaced by |
|------------|--------|-------------|
| `clean_flicker_noise` (1/f cleaning) | Off | The unified background step's Gaussian-process 1/f model, fit on flat-fielded data with a much deeper source mask |
| `bkg_subtract` (image2) | Off | The unified background step |
| `skymatch` (inter-exposure sky matching) | Off | The per-exposure pedestal inside the background step |
| `tweakreg` (alignment) | Off | JHAT or the CAMPFIRE alignment engine, against an external Gaia-tied catalog |
| `source_catalog` | Off | Source catalogs are built downstream, outside the pipeline |

## Stage 1: Detector Processing

### Ramp fitting and snowball flagging

Each raw exposure is processed with the stock `Detector1Pipeline` (superbias, reference-pixel correction, linearity, dark, jump detection, ramp fitting). The main departure from defaults is an aggressive **snowball** configuration in the jump step: large cosmic-ray events are expanded with fitted ellipses (expansion factor 2.2, extended events grown out to a 200 px radius) so that the halo of a snowball — not just its saturated core — is excluded from the ramp fit. Shower detection is off, and the stock 1/f cleaning (`clean_flicker_noise`) is off because CAMPFIRE removes 1/f noise later, in the flat-fielded frame, with a source mask far deeper than the detector-stage step can build.

### Jackknife correction of snowball "circles"

Even with good jump flagging, snowballs leave a subtle artifact in the rate images: coherent light or dark **disks** at snowball footprints, reaching up to ~25% of the sky level on the worst exposures. The cause is a bias in ramp fitting itself. The mean calibrated ramp is not exactly linear in time — a frame-wide, exposure-dependent curvature survives the detector calibration — and ramp fitting is only estimator-independent on a linear ramp. A pixel whose jump flags force a different ramp-segment pattern therefore reads a slightly different zero-point than its unflagged neighbours. For scattered cosmic rays these offsets are incoherent, but a snowball hands the *same* segment pattern to thousands of contiguous pixels, and the offset becomes a visible disk.

![Schematic of jump-segmented ramp fitting on a curved ramp, the measured ramp nonlinearity, the sign-flipping bias vs. flag timing, and before/after images showing the snowball circles removed.](/docs/reduction/nircam-jackknife.png)

CAMPFIRE removes this bias with a **jackknife** measurement ([#465](https://github.com/hollisakins/campfire/pull/465)): the flagged pixels' segment patterns are replayed onto clean sky pixels of the same exposure, the ramp fit is rerun, and the difference between the two fits directly measures the bias each pattern induces — which is then subtracted from the flagged pixels. Because the curvature varies by an order of magnitude between consecutive exposures, the correction is measured per exposure rather than calibrated globally. On the worst snowball disks the residual is suppressed by ~70–80%; only the science values change (the correction is a bias, not additional noise).

### Persistence flagging

Persistence — residual charge from earlier bright or saturated exposures — is flagged with the [`snowblind`](https://github.com/mpi-astronomy/snowblind) package, which tracks each detector's exposure history within the observation. This runs immediately after detector processing (earlier than in typical reductions) so that persistence-affected pixels are already flagged when the background step builds its source mask.

### Wisp subtraction

**Wisps** are diffuse scattered-light features that appear at fixed detector positions on four of the short-wavelength detectors (A3, A4, B3, B4). CAMPFIRE subtracts them in the count-rate frame, before flat-fielding, using the multi-component non-negative matrix factorization (NMF) wisp model of [Wu et al. 2026](https://arxiv.org/abs/2601.15958) (JADES DR5): a small per-detector, per-filter basis of wisp components whose amplitudes are fit to each exposure, capturing exposure-to-exposure changes in wisp morphology that a single scaled template cannot ([#405](https://github.com/hollisakins/campfire/pull/405)).

CAMPFIRE modifies how the model amplitudes are fit, in two ways that measurably change the result:

- **The fit is restricted to the wisp core** (pixels above 50% of the template peak) rather than the model's default fit region, ~90% of which is low-signal background. In the default region the numerous background pixels dominate the fit and drag the wisp amplitude down by a factor of ~2, leaving the brightest wisp filaments visibly under-subtracted in the final mosaic ([#456](https://github.com/hollisakins/campfire/pull/456)).
- **The source mask is iterated.** The source mask must be built from the frame itself — but on a frame that still contains a bright wisp, the wisp is detected *as a source* and masks the very pixels the fit needs. On the worst cases this starves the fit entirely and produces an over-subtracted bowl. The mask is therefore re-detected on the wisp-subtracted frame and the fit repeated (typically 2–4 passes) until the model converges ([#458](https://github.com/hollisakins/campfire/pull/458); [validation figures](https://github.com/hollisakins/campfire/tree/main/docs/figures/nircam-wisp-mask-iteration)).

For detector/filter combinations without NMF templates, the pipeline falls back to fitting scaled versions of the STScI wisp templates. Both the fitted amplitudes and the exact template version are recorded in the header, so the subtraction is fully reversible.

## Stage 2: Calibration, Background, and Alignment

### Flat fielding and photometric calibration

The stock `Image2Pipeline` assigns the WCS (SIAF + CRDS distortion), applies the flat field, and calibrates each exposure to MJy/sr. Its built-in background subtraction and per-exposure resampling are disabled — background is handled by the dedicated CAMPFIRE step below, and resampling happens only at the mosaic stage. Per-detector custom flats can be substituted for the CRDS flats on a per-field basis. A short custom step then flags the noisy outermost rows and columns of each detector so they don't bias the background fit.

### Background and 1/f subtraction

This is the scientifically deepest custom step, and the largest departure from the stock pipeline ([#344](https://github.com/hollisakins/campfire/pull/344), [#402](https://github.com/hollisakins/campfire/pull/402), [#457](https://github.com/hollisakins/campfire/pull/457)). It runs per exposure on the flat-fielded, flux-calibrated images and jointly models four components that the stock pipeline either handles separately, or not at all:

1. **A smooth 2D sky background**, fit with a source-masked mesh and subtracted. This carries the sky-matching role: because it (together with the pedestal below) zeroes each exposure's background, no inter-exposure `skymatch` is needed before drizzling. The mesh scale (~1″) is chosen fine enough to also remove residual detector-scale banding and — on cluster fields — intracluster light, which is the intended behavior for these reductions (see the trade-offs below).
2. **A per-amplifier pedestal** — NIRCam's four readout amplifiers carry distinct DC offsets (~3–5% of sky in the SW channel) that would otherwise imprint vertical seams at the amplifier boundaries.
3. **Column (vertical) 1/f noise**, as a per-column median.
4. **Row-wise 1/f noise, modeled with a Gaussian process.** The conventional approach — a sigma-clipped median per amplifier-row — fails around bright or extended sources: when a source fills most of an amplifier's row, the median is starved and typical implementations fall back to a full-row estimate, substituting the *wrong* amplifier's offset exactly where a good local estimate is hardest. The result is the familiar "box" of striping residuals around bright galaxies. CAMPFIRE instead models each amplifier's row offsets as a smooth function of row number with a Gaussian process (correlation lengths of 5 rows for fast 1/f and 20 rows for slower banding, calibrated offline and held fixed): heavily masked rows are automatically down-weighted and interpolated from neighbouring rows of the *same* amplifier, with no hard fallback and no amp-to-amp steps.

![Before/after of the background step on a real COSMOS-Web F150W exposure: the input flat-fielded frame shows row banding and amplifier structure; the middle panel shows the removed background model (amp pedestals, smooth sky, column and row 1/f); the output frame is flat at the same stretch.](/docs/reduction/nircam-bkg-before-after.png)

*The `bkg` step applied to a public COSMOS-Web F150W exposure (`jw01727167001_02101_00001`, NRCA1), starting from the standard MAST `_cal.fits` product. Left: the input frame at ±2σ around the sky level — row banding and amplifier-block structure are clearly visible. Middle: the background model the step removes (shown without its DC level, at a tighter stretch). Right: the corrected frame at the same stretch as the left panel.*

All components share a single deep, multi-scale source mask, and the chain is iterated three times so the mask sharpens as the frame flattens. A fit-only "conditioning" model of large-scale structure — anisotropic, so it can follow the scattered-light and halo structure around bright sources without absorbing the banding the 1/f terms should fit — protects the per-amplifier terms from bias without itself touching the science pixels. Finally the read-noise variance is rescaled to match the observed pixel-to-pixel scatter of the cleaned background.

The step was validated on synthetic scenes with known truth (flux conservation on injected Sérsic sources, blank-sky recovery to 0.04σ) and on real COSMOS and Abell 2744 exposures re-reduced from raw data ([figures](https://github.com/hollisakins/campfire/tree/main/docs/figures/nircam-bkg2d)). The current configuration reduces residual common-mode row banding from 1.8× the pure-noise expectation to 0.6× while changing aperture photometry of compact sources by +0.24% and of extended sources by less than 0.1%.

![Comparison of 1/f models on an Abell 2744 exposure with a bright galaxy group: a plain per-amp-row median absorbs an amp-wide box of galaxy flux, while the CAMPFIRE default leaves only a small residual band.](/docs/reduction/nircam-gp-vs-median.png)

*Why the Gaussian-process treatment matters, demonstrated on a public UNCOVER exposure (`jw02561006002_07201_00001`, NRCBLONG) whose bright galaxy group spans amplifier rows. Left: the input frame. Middle: the 1/f model removed by a plain per-amp-row median with no conditioning (the conventional approach) — the galaxy's envelope leaks into the row estimates and is subtracted as a coherent box across the amplifier's full width (mean +1.4σ of sky over the galaxy's rows, hard edges at the amp boundaries), and per-amp asymmetric absorption of the sky gradient imprints seams between amplifiers. Right: the CAMPFIRE default (GP + conditioning detrend + deep mask) suppresses the misattributed flux by roughly a factor of 7; a small residual band at the very brightest rows remains and is the subject of continued refinement.*

**Trade-offs to know when using the data:**

- The 2D background fit removes intracluster light and the outermost wings of bright, extended galaxies *by construction*. This is intended for the cluster and deep-field reductions CAMPFIRE targets, but means the mosaics are not suitable for ICL science without a dedicated re-reduction.
- The fine background mesh removes a small fraction (~1%, measured on one bright star) of the flux in bright-star diffraction spikes. This is real PSF flux — relevant if you are modeling the PSF or doing bright-star photometry in very large apertures.

### Diagonal striping removal (opt-in)

Some fields show stripe artifacts running at an arbitrary angle across a detector — scattered light from bright stars just outside the field of view, distinct from row/column 1/f noise. An opt-in step searches for the stripe angle, builds a striping model in rotated coordinates with amplitude allowed to vary across the detector (the stripes are brighter closer to the off-axis source), and subtracts it. The step refuses to fit when no significant stripe signal is present, since subtracting a model fit to noise does more harm than good. It is enabled per field, with the angle search range tuned to the actual scattered-light geometry.

### Astrometric alignment

Exposures are aligned individually against an external, field-level **reference catalog** tied to Gaia DR3, rather than relatively to one another (as the stock `tweakreg` would do). Reference catalogs are built per field from Gaia DR3, ground-based surveys (DESI Legacy Surveys DR10, HSC-SSP PDR3), and/or a source catalog extracted from an existing aligned CAMPFIRE mosaic — the usual bootstrap is to align one long-wavelength filter to Gaia and then use its mosaic as the astrometric reference for the remaining filters, which guarantees cross-band registration. Where the catalog carries proper motions, positions are propagated to each exposure's epoch before matching.

For each exposure, the fit is a rigid shift + rotation of the pointing — the SIAF/CRDS distortion solution is never re-fit. Matching to the reference catalog uses an offset-histogram consensus method (following [JHAT](https://jhat.readthedocs.io/), which the pipeline used as its original alignment engine): a 2D histogram of source–reference offset candidates recovers gross pointing errors out to ~70″, and true matches are then selected by consensus rather than pair enumeration, which keeps the method robust in clustered extragalactic fields where triangle- and pair-based matchers fail ([#411](https://github.com/hollisakins/campfire/pull/411)).

The current alignment engine adds several refinements over a stock per-detector solve ([#334](https://github.com/hollisakins/campfire/pull/334), [#355](https://github.com/hollisakins/campfire/pull/355), [#420](https://github.com/hollisakins/campfire/pull/420)):

- **All detectors of an exposure are solved together** in one pooled fit. All detectors share a single telescope attitude, and a lone short-wavelength detector with a handful of reference matches is a badly conditioned place to fit a rotation; the pooled fit uses every match across the focal plane.
- **A velocity-aberration correction between detectors.** The stock pipeline corrects differential velocity aberration per detector about each detector's own reference point, which leaves a residual *relative* offset between detectors — ~13 mas between the two long-wavelength modules, confirmed on 3,258 COSMOS exposures (see [spacetelescope/jwst#9400](https://github.com/spacetelescope/jwst/issues/9400)). CAMPFIRE re-references the correction to a common point before the pooled solve; after the fix, the measured module-to-module offset is 0.26 mas.
- **A statistically gated per-detector refinement**: each detector may refine its own solution on top of the pooled fit, but the refinement is only accepted when the shift it applies is significant against its own measurement noise — preventing sparse detectors from chasing noise.
- **Hard quality gates.** A solution must have enough matches, with enough spatial extent to constrain the rotation, and a final matched residual below 0.1″; otherwise the exposure is marked unaligned, keeps its original WCS, and is excluded from the mosaic (with a loud report) rather than being combined with a wrong solution. Healthy solutions typically show median residuals of 0.02–0.03″ against the reference catalog.

Every aligned exposure stores its matched-source table and match-confidence diagnostics in the file, so alignment quality can be re-verified independently, source by source, without re-running anything. For rare exposures whose header pointing is corrupted beyond the matcher's capture range, a manual per-exposure WCS offset can be declared in the field configuration and is applied before alignment.

## Stage 3: Mosaic Combination

### Visual inspection and manual masks

Every exposure gets a signal-to-noise quick-look image, rendered for the web review UI. SNR — rather than flux — is the deliberate choice: artifacts the pipeline has already correctly down-weighted (e.g. snowball cores with inflated errors) sink into the noise floor, while residuals the error model *doesn't* know about remain visible, so a reviewer's eye lands on exactly the artifacts that still need a hand-drawn mask. Reviewer-drawn region masks are stored alongside the science data and applied non-destructively at combination time; the calibrated exposures themselves are never modified, so masks are fully reversible and editable. Reviewers can also exclude entire exposures from the mosaic.

### Outlier (cosmic-ray) detection

Cosmic rays and other transient artifacts are rejected with the stock JWST outlier detection (drizzle–median–blot comparison across dithers), run per visit with each visit's comparison stack padded by spatially overlapping exposures from the same program.

An opt-in **artifact-region growth** extends the stock detection for detector-fixed artifacts such as scattered-light arcs and glints ([#404](https://github.com/hollisakins/campfire/pull/404), [#406](https://github.com/hollisakins/campfire/pull/406)). These features move on the sky between dithers, so outlier detection flags their bright cores — but their wings survive below the per-pixel threshold and drizzle into the mosaic. Large flagged regions are therefore grown outward through connected pixels of the smoothed residual (a two-threshold, isophote-following expansion), so the mask traces the artifact's actual morphology and stops at the flat background. Guards release genuine galaxies whose light merely touches an artifact, cap runaway growth on bright-star halos, and leave small detections (ordinary cosmic rays) untouched ([before/after figures](https://github.com/hollisakins/campfire/tree/main/docs/figures/nircam-outlier-grow)). The depth cost is one dither, confined to artifact neighbourhoods.

An additional opt-in step for very well-dithered fields identifies consistently bad pixels empirically — pixels flagged unusable in ≥80% of a filter's exposures — supplementing the CRDS bad-pixel mask.

### Resampling and mosaic-level background

Each field defines one or more fixed mosaic **tiles** (a WCS, pixel grid, and orientation shared across filters). Exposures overlapping a tile are drizzled with the stock JWST resampling at a default scale of 30 mas (square kernel, `pixfrac = 1`, inverse-variance weighting), producing one mosaic per field/filter/tile combination. Mosaics of the same field are pixel-aligned across filters and pixel scales by construction.

After drizzling, a **mosaic-level background subtraction** removes residual large-scale background from the coadd. The method derives from the STScI `nircamx` approach (H. Ferguson): a ring-median filter isolates large-scale structure, a tiered cascade of source-detection kernels masks sources from bright/extended to faint/compact, and a smooth 2D background is fit to what remains. CAMPFIRE adds two pieces:

- **Depth-aware masking** ([#419](https://github.com/hollisakins/campfire/pull/419)): detection thresholds are evaluated on the noise-equalized image (science × √weight), so a single threshold is valid across a mosaic with strongly varying depth. Without this, the deepest region sets the global threshold and the shallow regions have noise mass-flagged as sources, leaving the background fit unconstrained exactly there.
- **A non-negativity guard** ([#430](https://github.com/hollisakins/campfire/pull/430)): since true flux is non-negative, the observed image is an upper bound on the background even under masked sources. The fitted background is capped by that bound, and any remaining coherent over-subtracted (negative) regions are corrected. On the Abell 2744 cluster core this reduced significantly negative structure by two orders of magnitude with no measurable change to blank-sky photometry.

The pre-subtraction mosaic is preserved alongside the final product, so the background model can always be inspected or undone.

## Output Products

For each field, filter, and tile the pipeline produces:

| Product | Contents |
|---------|----------|
| `*_i2d.fits` | The mosaic: science (MJy/sr), error, weight, and context extensions |
| `*_sci.fits`, `*_err.fits`, `*_wht.fits` | The same planes split into single-extension files for convenience |
| `*_i2d_before_bkgsub.fits` | The mosaic before mosaic-level background subtraction |
| Manifest | Input exposure list with content digests, configuration hash, and pipeline version |

Mosaic naming follows `mosaic_nircam_[filter]_[field]_[scale]_[tile]` (e.g. `mosaic_nircam_f444w_a2744_30mas_A1`). Fields can also define **epochs** (date- or visit-based exposure subsets) for time-domain work; epoch mosaics carry the epoch name as a suffix. Alongside the mosaics, each calibrated exposure retains per-step diagnostic plots (wisp fit, background components, outlier maps) and the SNR preview images used in review.

## Known Limitations

- **Diffraction spikes are not yet automatically masked.** Bright-star spikes are self-consistent across a visit (all dithers share one roll angle), so outlier detection cannot catch them; they are currently handled by manual masks where needed. An automatic, coverage-aware spike-masking step — masking spikes only where data at another roll angle can fill in — is [designed](https://github.com/hollisakins/campfire/blob/main/docs/design-nircam-spike-masking.md) and under development.
- **Extended low-surface-brightness emission** (ICL, bright-galaxy outskirts) is removed by the per-exposure and mosaic-level background fits by design; these mosaics are not suitable for ICL or LSB science without a re-reduction with different background settings.
- **Bright-star spike photometry** loses ~1% of spike flux to the background fit (see the background trade-offs above).

## Next Steps

- [Data Reduction Overview](/docs/reduction) — pipeline philosophy and architecture
- [NIRSpec Pipeline](/docs/reduction/nirspec) — the spectroscopic counterpart
- [Data Products](/docs/data-products) — file formats and column definitions
