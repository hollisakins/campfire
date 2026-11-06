# How the 2026.1 data were reduced

This document describes the data in CAMPFIRE data release 2026.1 and the pipeline that produced them. It is frozen with the release. The website documentation describes whatever pipeline is current and will move on; this page will not. The release's counts, programs, downloads and citation are shown on this page outside this text, so they are not repeated here.

The NIRSpec and NIRCam data in 2026.1 were reduced with the first generation of the CAMPFIRE pipeline, `cfpipe`, in its 0.x releases. The pipeline wraps the STScI JWST calibration pipeline (`jwst` 1.20) with its own background subtraction, spectral extraction, redshift fitting and mosaic assembly. The NIRSpec reduction is largely based on the CAPERS pipeline (A. Taylor and P. Arrabal Haro); the NIRCam reduction on the CEERS and COSMOS-Web pipelines (M. Bagley and M. Franco). Every product records the exact pipeline version that made it (see "Pipeline versions" below).

## What the release contains

A release is a frozen snapshot of the published catalog and of the files it names. 2026.1 holds, where a component applies:

- **NIRSpec spectra.** One spectrum per source, grating and observation: the `_spec.fits` file with the 2-D and 1-D spectra, the JSON sidecars the portal plots (the full spectrum, a 1-D only copy, and the redshift fit), and, where an emission-line fit exists, the `_lines.fits` product and its JSON sidecar. A spectrum is extracted for every source that fell in a slitlet, not only the primary targets.
- **The spectroscopic catalog.** Tables of objects (one row per sky position, grouping the targets of every observation and program that covered it), spectra (one row per spectrum), emission-line fits (one row per fitted spectrum, plus one row per line) and photometry cross-matches. The release exports them as one gzip JSONL file per table and program.
- **Redshifts and inspection results.** For each object: the automatic redshift from template fitting, the inspected redshift and quality grade where a person has inspected it, the spectral features and flags recorded at inspection, and the identity of the spectrum the automatic redshift came from. For each spectrum: its own automatic redshift, chi-squared minimum and confidence, and its data-quality flags.
- **NIRCam mosaics.** For each field in the release: the drizzled mosaic per filter and tile, its split science, error, weight, source-mask and exposure-time images, the per-filter exposure maps, the field layout plot, and the mosaic thumbnails and quick-looks.
- **Photometry cross-matches.** For the fields and catalogs in the release: the nearest match between each object and an external photometric catalog, with that catalog's fluxes per band and its photometric redshift.

Not in the release: the pipeline's intermediate products (rate files, per-exposure spectra, per-exposure NIRCam images), the P(z) curves behind the photometric redshifts (the photometry rows carry the redshift and its errors only), map tiles, and the pipeline's quality-assurance plots.

## NIRSpec reduction

The NIRSpec pipeline reduces raw multi-object spectroscopy (MOS) exposures to calibrated 1-D spectra with automatic redshifts. It works source by source. Standalone fixed-slit observations go through the same stages.

| Stage | Input | Output | What it does |
| --- | --- | --- | --- |
| 1 | `_uncal.fits` | `_rate.fits` | Detector processing, iterative background and 1/f subtraction |
| 2a | `_rate.fits` | per-source spectrum exposure | WCS, flat field, path loss, flux and wavelength calibration |
| Stuck shutters | | | Stuck-closed shutters flagged before background subtraction |
| 2b | spectrum exposure | the same file, background-subtracted | Nodded background subtraction |
| 3 | spectrum exposures | `_spec.fits` | 2-D stack, optimal and boxcar extraction |
| Redshift fit | `_spec.fits` | `_zfit.fits` | Template chi-squared fit |

Stages 2a and 2b write one file per exposure, detector and source. That file holds the calibrated arrays and the background-subtracted arrays side by side, with a provenance chain in its header.

### Stage 1: detector processing and background

The STScI `Detector1Pipeline` converts each uncalibrated exposure to a count-rate image. Its `clean_flicker_noise` step runs with the science regions masked, and the jump step expands large events.

The pipeline then subtracts a multi-component background model from each rate file. Pixels under spectral traces are masked from the slitlet WCS solutions, following the trace curvature. For the gratings the masked wavelength range is widened beyond the nominal range so higher-order light is masked too. The fixed-slit region and any pixel with a nonzero DQ value are always masked. The model is built in five passes; each pass clips the residual of the previous one at 2 sigma before fitting, and subtracts in order:

1. **Picture frame.** The STScI picture-frame reference image is fitted in four horizontal quarters of 512 rows, with a scale factor and a pedestal per quarter.
2. **Column 1/f noise.** Each column is fitted as a scaled copy of the picture-frame template plus an offset. Where no picture-frame reference exists, the median of each column is subtracted instead.
3. **Row 1/f noise.** The median of each row is subtracted. This component is applied to PRISM exposures only.

A smooth 2-D background model (photutils `Background2D` with 64-pixel boxes) is implemented in the same loop but is off by default and is not part of the standard reduction.

After subtraction the read-noise variance is rescaled so that the scatter of the background pixels matches the noise model. The factor is recorded in the rate file as `CFBKGRMS`, and the background model and its mask are kept as the `CFBKG` and `CFBKGMASK` extensions. Hand-drawn masks of artifacts such as shorts can be applied at this stage; masked pixels are set `DO_NOT_USE`.

### Stage 2a: calibration

Stage 2a runs the STScI `Spec2Pipeline` once per source and exposure. For each source the pipeline writes an MSA metadata file holding only that source's shutters, then runs the STScI pipeline with background subtraction, bar-shadow correction, spectral resampling and 1-D extraction switched off. WCS assignment, flat fielding, the path-loss correction and the flux and wavelength calibrations are applied.

Every source is given a stellarity of 1 in its metadata file, so the STScI pipeline treats it as a point source. The path-loss correction and the flux units are those of a point source for every spectrum in the release.

Two corrections are applied on top of the STScI products:

- **Units.** When the STScI pipeline decides a slitlet holds no source (a stuck shutter, or a source too close to the slit edge), it writes the output in surface-brightness units and applies a uniform-source path loss. The pipeline converts these exposures back to flux density and marks them `SRCFLUX = F`. Such exposures serve as backgrounds but are not combined into the final spectrum.
- **Wavelength zero point.** The JADES DR4 empirical wavelength correction (Scholtz et al. 2025), which depends on the source position in the shutter, can be added to the CRDS `wavecorr` reference. It is off by default.

An extended-wavelength mode for G140M/F100LP and G235M/F170LP, which calibrates the first-order light past the nominal red cutoff with references derived for that purpose, is opt-in per observation. A spectrum reduced that way carries `CFEXTWAV = T`.

### Stuck shutters

A shutter that fails to open leaves part of a slitlet without signal. Stuck-closed shutters are detected automatically for PRISM observations from the rectified 2-D spectra: a shutter is flagged when, across its nods, more than half of its pixels fall below twice the read noise. Automatic detection for the gratings is off by default. Detected shutters, shutters entered by hand and shutters reported by inspectors on the portal are merged into one list per observation. Flagged shutters are marked closed in the MSA metadata before background subtraction, and the list used for each exposure is recorded in the spectrum's `EXPOSURES` table.

### Stage 2b: nodded background subtraction

The background is subtracted using the other nod positions of the same slitlet. For each exposure the pipeline:

1. groups the exposures of a source by detector and nod pattern (2, 3 and 5 nods are supported);
2. inverts the path-loss correction on the science and background exposures, since the correction depends on the source position in the shutter and so differs between nods;
3. subtracts the background with the STScI `BackgroundStep`, using every other exposure of the group as background;
4. reapplies the path-loss correction.

A per-observation override can restrict which nods serve as background for an exposure, or drop an exposure from the final combination. The nods used are recorded in the `EXPOSURES` table (`bkg_nods`, `bkg_override`). An exposure with no companion nod is left unsubtracted and is not combined.

### Stage 3: stacking and extraction

The STScI `Spec3Pipeline` combines the background-subtracted 2-D exposures of a source and grating into one rectified 2-D spectrum. An `EXPOSURES` table records the contributing exposures, their dither and nod pattern, exposure times, stuck shutters and background nods.

The 1-D spectrum is extracted from the stacked 2-D spectrum by optimal extraction (Horne 1986):

1. The 2-D spectrum is collapsed along the wavelength axis with a median to form the spatial profile.
2. For MSA sources the profile is restricted to the nominal extraction aperture, so flux from neighboring shutters across the bars is excluded. Negative values are set to zero and the profile is normalized to unit integral. Fixed-slit sources, which have no bars, use the full cross-dispersion profile.
3. The spectrum is the profile-weighted, inverse-variance weighted sum of the 2-D spectrum.

When the collapsed profile is unusable (fewer than three positive pixels in the aperture, or positive flux below half of the absolute flux, typically after background over-subtraction), the optimal extraction falls back to a 3-pixel boxcar. The outcome is recorded in the `CMPFROPT` header card as `optimal` or `boxcar-3px`.

Three boxcar extractions are made alongside, centered on the expected source position: 3, 4 and 5 pixels wide, with uniform weights and fractional pixel edges. All four extractions are stored. The optimal extraction is the recommended one for unresolved or marginally resolved sources; the wider boxcars suit extended sources.

Fluxes are reported as flux density per unit frequency in microjansky (the `fnu` columns) and per unit wavelength in erg/s/cm^2/Å (the `flam` columns), each with an error column. The 2-D arrays in the file are the stacked STScI product and are in MJy.

By default every exposure of a source and grating goes into one 2-D stack, and the 1-D spectrum is extracted once from it. The pipeline also has a 1-D combination mode: each dither group is stacked and extracted on its own, and the per-group 1-D spectra are resampled onto a common wavelength grid and combined with exposure-time weights, with 3-sigma clipping when three or more spectra contribute. A spectrum made that way carries `CMPFRSTG = 'stage3-1d'`, an `NCOMBINE` card, and an `n_combined` column.

### Redshift fitting

The automatic redshift comes from chi-squared template fitting. At each trial redshift the spectrum is fitted as a non-negative linear combination of templates (Lawson-Hanson NNLS). The solve works on Gram matrices, with a small ridge term that stabilizes nearly collinear templates. Attenuation by the intergalactic medium (Inoue et al. 2014) is applied to the templates, which are convolved to the instrument resolution using the grating's dispersion curve scaled by a factor (1.3 for PRISM, 1.5 for the gratings).

The fit has two passes. The coarse pass covers the full range on a velocity-spaced grid: 500 km/s for PRISM (z = 0 to 20), 100 km/s for the medium-resolution gratings and 30 km/s for the high-resolution gratings (z = 0 to 15). The fine pass refits within 3000 km/s of the coarse minimum with 30, 10 and 5 km/s steps respectively.

The template library:

- **Continuum: 16 stellar population models** from `bagpipes`, with delayed-tau star formation histories, ages from 10 Myr to 0.95 times the age of the universe at the trial redshift, a range of metallicities, and dust attenuation. The parameters evolve with redshift: the metallicity falls by a factor of two from z = 0 to z = 10, and the dust attenuation is boosted by a Gaussian in redshift peaking near z = 2.8.
- **Emission lines: 21 line templates** from Lyman-alpha to Paschen-alpha. Several are tied in fixed ratios to constrain the fit, for example the Balmer lines, [OIII] 4959/5007, [NII], [NeIII] and [SIII].
- **Broad lines:** Gaussian H-alpha and H-beta at 1500 and 3000 km/s, for broad-line AGN.
- **Blackbodies:** Planck functions at 500, 2500 and 5000 K, and one modified blackbody, for dust-dominated or featureless sources.

A confidence is computed from the chi-squared curve: p(z) is proportional to exp(-(chi2 - chi2_min)), and the confidence is the percentage of the total probability within 0.03 of the best redshift. A high value means an isolated minimum; a low value means competing solutions. The confidence is a starting point for inspection, not a quality grade.

### Catalog redshifts, inspection and flags

Each spectrum row carries the automatic redshift of its own fit (`redshift_auto`), the chi-squared minimum and the confidence. The object's automatic redshift is taken from one member spectrum by a fixed rule. The PRISM spectrum with the longest exposure anchors it. If a grating spectrum agrees with the PRISM value to within 1 percent in (1 + z), the best such grating (G395M, G395H, G235M, G235H, G140M, G140H, in that order) refines it. Without a PRISM spectrum the best grating is used. The spectrum chosen is recorded as `z_source_spectrum_id`.

Inspection is done on the portal, per object, by people. An inspector records a redshift quality, may override the redshift, tags the spectral features that informed the decision, and sets object and data-quality flags. The catalog's `redshift` is the inspected value where one exists and the automatic value otherwise, and is empty for an object graded impossible. `redshift_quality` is:

| Value | Grade | Meaning |
| --- | --- | --- |
| 0 | Not inspected | No one has reviewed the object |
| 1 | Impossible | No redshift can be determined from the data |
| 2 | Tentative | About 50 percent confidence, for example a single emission line |
| 3 | Probable | About 80 percent confidence |
| 4 | Secure | Above 95 percent confidence, several features agree |

The spectral feature tags are: continuum shape, Lyman break, Balmer break, absorption features, single emission line, multiple emission lines. Data-quality flags are a bitmask (`dq_flags`): chip gap (1), contamination (2), stuck closed shutter (4), multiple sources (8), no detection (16), low S/N (32), spectral overlap (64), PRISM corrupted (128), grating corrupted (256). The frozen catalog holds the feature tags and flags on each target, which the portal shows when browsing the release; the exported spectra rows carry `dq_flags`. Object-type classifications made on the portal (little red dot, broad line, quiescent and the like) are not part of the release tables. The `signal_to_noise` of a spectrum row is the maximum per-pixel S/N of its optimal extraction.

### Emission-line fluxes

Line fluxes are measured only at an inspected redshift of grade probable or secure, never at the automatic redshift, so the line catalog holds no flux fit at an unvetted redshift. The fit uses a catalog of 43 rest-frame vacuum lines from the ultraviolet to Paschen-alpha. Lines whose windows overlap are fitted together as a complex. In each window the model is a sum of Gaussians integrated over the pixel plus a low-order polynomial continuum. The line width is the quadrature sum of the instrument resolution (the same scaled dispersion curve the redshift fitter uses) and an intrinsic velocity width. Fixed-ratio doublets are tied. Lines closer than twice the width are reported as one blend, carried by the heavier line of the pair. Close doublets with a free ratio (CIII], [OII], [SII] and others) are also reported as a total, so the same quantity means the same thing in every grating. Kinematics are anchored on lines with S/N of 5 or more and held fixed for the rest of the spectrum. On the gratings a broad component is tried on permitted lines and kept when it improves chi-squared by 25 or more.

Per line the product records the flux in erg/s/cm^2 with its error and S/N, the rest-frame equivalent width, the local continuum, the velocity offset and width, the fit statistics and a flag bitmask. Fluxes are unconstrained in sign, so a non-detection carries a flux and error from which a limit can be formed. Each fit records the redshift used, its source and quality, the object version and a hash of the spectrum, so a fit can be recognized as stale when the inspected redshift or the spectrum changed after it was made. The release tables carry `stale_redshift` and `stale_spectrum` flags evaluated at the freeze.

### Pipeline versions

The pipeline is versioned with git tags of the form `pipeline-vX.Y.Z`. The releases of the first-generation line, any of which a 2026.1 product may carry, are:

| Version | Date | Notable changes |
| --- | --- | --- |
| 0.3.0 | legacy | First unified `cfpipe` package. Versions were recorded as a git commit hash. |
| 0.4.0 | 2026-05-04 | Manual masking step after stage 1; boxcar fallback for corrupted extraction profiles (`CMPFROPT`); PEP 440 version strings. |
| 0.5.0 | 2026-05-21 | Mostly NIRCam: the two-phase per-exposure pipeline, scattered-light and striping steps, bad-pixel stacking off by default, a tile WCS fix. |
| 0.5.1 | 2026-05-27 | Version resolution counts only pipeline commits. |
| 0.6.0 | 2026-08-18 | NIRSpec: one file per exposure, detector and source; fixed-slit support; stuck-shutter provenance; unbounded extraction profile for fixed slits. NIRCam: unified background step, jackknife, NMF wisp model, field-level alignment, mosaic background guard. |

A product reduced from a development build between tags carries a version such as `0.6.1.dev12+g1a2b3c4`, and one reduced from a modified working tree carries a date suffix. The pipeline's default configuration pins CRDS context `jwst_1481.pmap`, but the context can differ per observation. The exact pipeline version of each spectrum is in its `CMPFRVER` header card and in the `cfpipe_version` column of the spectra table; the CRDS context and `jwst` version are in `CRDS_CTX` and `CAL_VER` and in the `crds_context` and `jwst_version` columns. Each mosaic carries its version in `CMPFRVER`.

## NIRCam reduction

The NIRCam pipeline reduces imaging exposures to drizzled mosaics per field, filter and tile. It runs in two phases: per-exposure processing, which writes one calibrated file per exposure, and combination, which builds the mosaics from working copies of those files.

### Per-exposure processing

In order:

1. **Detector processing.** The STScI `Detector1Pipeline`, with the jump step set to detect and expand large events (snowballs) and to mask snowball cores in the next integration.
2. **Jackknife.** After a cosmic ray or snowball a pixel's ramp is fitted from a different set of groups than its neighbors. Because the mean ramp has a frame-wide curvature, such pixels read a different zero point. The step measures the offset per flag pattern by refitting clean pixels with the same groups removed, then subtracts it. Recorded in `CFP_JACK`.
3. **Persistence.** Persistence flagging with `snowblind`.
4. **Wisps.** Wisps are subtracted with the multi-component non-negative matrix factorization model of Wu et al. (2026), fitted per exposure with sources masked and the mask iterated so a bright wisp cannot mask itself. Detector and filter combinations without such a template fall back to a scaled STScI template. Recorded in `CFP_WISP`.
5. **Calibration.** The STScI `Image2Pipeline`: flat field and photometric calibration.
6. **Edges.** Noisy outer rows and columns are flagged `DO_NOT_USE`.
7. **Background.** One step (`CFP_BKG`) builds a source mask and removes, per amplifier, the pedestal, the column pattern and the row-wise 1/f noise, the last with a Gaussian process at two correlation lengths that interpolates across masked sources. A coarse 2-D model conditions the fit so that sky gradients do not leak into the per-amplifier terms. A smooth 2-D background, in 32-pixel boxes at the short-wavelength scale, is then subtracted from every exposure; this removes intracluster light and the wings of bright galaxies by construction. The read-noise variance is rescaled against the measured scatter.
8. **Previews** for the review portal.
9. **Astrometric alignment** of each exposure to a field reference catalog tied to Gaia, with JHAT by default. A field can instead opt into the pipeline's own alignment step, which fits one attitude per exposure across its detectors and refines per detector when the shift is significant. Exposures that cannot be tied to the reference are marked and kept out of the mosaics.

A correction for the diagonal scattered-light stripes of off-axis bright stars exists for fields that need it and is off by default. Pixel masks drawn on the portal and reviewer exclusions of whole exposures are honored at combination.

### Combination

1. **Masks.** Reviewer-drawn regions are recorded on each exposure and excluded from everything that follows.
2. **Bad pixels.** A per-field mask of consistently bad pixels can be stacked from the exposures. It is off by default, since the CRDS masks already cover static defects.
3. **Outliers.** Cosmic rays and other outliers are flagged per visit with the STScI outlier detection, using the overlapping exposures of the same program as the comparison set.
4. **Resampling.** Exposures are drizzled onto the field's tile grid with the STScI resample step (square kernel, pixfrac 1, inverse-variance-map weights), at 30 mas per pixel by default. Pixels with no coverage, or masked in every exposure, are NaN.
5. **Mosaic background.** A final source-masked 2-D background is subtracted from each mosaic: tiered source detection on the noise-equalized image, a ring-median filter, and a `Background2D` fit in 10-pixel boxes, with a guard that keeps the subtracted mosaic from carrying significant negative structure. Recorded in `CFP_BKGS`.

### Mosaic products

Each mosaic is named `mosaic_nircam_<filter>_<field>_<scale>_<tile>` and is delivered as the `_i2d.fits` file plus split single-extension files: `_sci` (MJy/sr), `_err`, `_wht`, `_srcmask` (the source mask used for the background) and, where the mosaic carries it, `_exp` (per-pixel exposure time). The `_i2d` file holds `SCI`, `ERR`, `WHT`, the drizzle context `CON` (stored tile-compressed, which astropy reads transparently) and `SRCMASK`. Its primary header carries the STScI keywords blended from the input exposures plus `CMPFRVER`, `CMPFRTIM` and `CFP_BKGS`. Per filter there is an exposure map, `expmap_<field>_<filter>.fits`, in seconds with the covered area in its `AREA` card, and a plot of it; per field, a layout plot of all filters with the tile outlines.

## Photometry cross-matches

Where a field has a photometric catalog configured, each object is matched to the nearest catalog source within a configured radius (0.3 arcsec in the example configuration). The match records the catalog name and identifier, the separation, the catalog's flux per band and its photometric redshift with errors. These are the external catalog's own measurements; CAMPFIRE does no photometry of its own. The catalogs in the release are listed on this page.

## FITS products

### The spectrum file

File name: `<observation>_<grating>_<filter>_<source>_spec.fits`, grating and filter in lower case.

| HDU | Name | Contents |
| --- | --- | --- |
| 0 | PRIMARY | Observation and calibration keywords copied from the STScI products, plus the CAMPFIRE cards below |
| 1 | SPEC1D | The 1-D spectra (table) |
| 2 | SCI | Stacked 2-D spectrum, MJy |
| 3 | ERR | 2-D error |
| 4 | WAVELENGTH | 2-D wavelength map, microns |
| 5 | WHT | 2-D weight map; zero marks pixels with no data |
| 6 | PROF1D | Spatial profile weights per extraction: `ypos`, `opt`, `3px`, `4px`, `5px` |
| 7 | EXPOSURES | One row per contributing exposure |

SPEC1D columns:

| Column | Unit | Description |
| --- | --- | --- |
| `wave` | micron | Wavelength |
| `fnu`, `fnu_err` | microjansky | Optimal extraction and its error |
| `flam`, `flam_err` | erg/s/cm^2/Å | The same per unit wavelength |
| `fnu_3px`, `fnu_3px_err`, `flam_3px`, `flam_3px_err` | as above | 3-pixel boxcar; likewise `_4px` and `_5px` |
| `n_combined` | | Spectra contributing per pixel (1-D combination mode only) |

EXPOSURES columns: `filename`, `dither_type`, `nod_type`, `nod_number` (the number of primary dither points), `dither_number`, `exptime`, `stuck_shutter_list`, `bkg_nods`, `bkg_override`, `shutter_state`, `source_ra`, `source_dec`, `source_xpos`, `source_ypos`, `v3pa`, `fixed_slit`, `slit_name`.

CAMPFIRE cards in the primary header:

| Card | Meaning |
| --- | --- |
| `CMPFRVER` | Pipeline version |
| `CMPFRTIM` | Reduction time, UTC |
| `CMPFROPT` | `optimal`, or `boxcar-3px` when the profile fell back |
| `CMPFRSTG`, `NCOMBINE` | Present only in 1-D combination mode |
| `CFFXSLT`, `CFFSSLIT` | Fixed-slit source, and the slit name |
| `CMPFRDR` | The release whose archive copy this file is, on a file pulled from a release |

The STScI cards `CRDS_CTX`, `CAL_VER`, `EFFEXPTM`, `DATE-OBS`, `PROGRAM`, `GRATING`, `FILTER` and the reference-file (`R_*`) and step-status (`S_*`) cards are copied from the STScI products. The SCI header carries `SRCTYPE`, `SRCRA`, `SRCDEC`, `XPOSURE`, `PHOTMJSR`, `PHOTUJA2`, `PIXAR_SR`, `PIXAR_A2`, `DISPAXIS`, `SPORDER`, `SOURCEID`, `STLARITY`, `WAVECOR` and `PTHLOSS`.

### The JSON sidecars

- `_spec.json` and `_spec_1d.json` are the portal's plot payloads, derived from the spectrum file.
- `_zfit.json` is derived from the pipeline's redshift-fit file: the best redshift, chi-squared minimum and confidence, the redshift grid with its chi-squared curve, and the best-fit model spectrum in microjansky.
- `_lines.json` is derived from the line-fit file: the fitted model and continuum in microjansky on the spectrum's wavelength grid, and a per-line summary.

### The line-fit file

`<spectrum>_lines.fits` has a PRIMARY header with the fit's provenance (`ZUSED`, `ZSRC`, `ZQUAL`, `OBJID`, `OBJVER`, `ZFIT`, `DVGLOB`, `SIGGLOB`, `KINSRC`, `NLINES`, `NDETECT`, `NBROAD`, `LFITVER`, `SPECHASH`, `FLSF`, `CMPFRVER`, `CMPFRTIM`), a `LINES` table, a `MODEL` table (`wave`, `model`, `cont`, in erg/s/cm^2/Å) and a `COMPLEXES` table of the fitted windows.

LINES columns: `name`, `component` (narrow, broad or doublet), `wave_rest`, `wave_obs`, `flux`, `flux_err`, `snr`, `ew_rest`, `ew_rest_err`, `cont`, `cont_err`, `dv`, `dv_err`, `sigma_v`, `sigma_v_err`, `sigma_lsf_kms`, `complex`, `chi2`, `dof`, `npix`, `flags`, `blend_into`, `tied_to`. The flag bits are: tied (1), blended (2), blend (4), edge (8), global kinematics (16), default kinematics (32), broad (64), no continuum (128), fit failed (256), masked (512), width unresolved (1024), resolved doublet (2048).

## Known limitations

NIRSpec:

- **No bar-shadow correction.** The STScI bar-shadow step is switched off. Flux of extended sources near the shutter bars is not corrected.
- **Point-source calibration for every source.** Every source is treated as a point source for the path-loss correction and the flux calibration. Fluxes of extended sources are affected accordingly.
- **Extraction fallback.** Where the spatial profile was corrupted, the optimal extraction is a 3-pixel boxcar (`CMPFROPT = 'boxcar-3px'`).
- **Exposures without the source.** Exposures the STScI pipeline flagged as holding no source serve only as backgrounds. A source whose every nod landed on a stuck shutter has no spectrum.
- **Stuck shutters.** Automatic detection runs on PRISM data only; for the gratings it relies on hand entries and inspector reports. An undetected stuck shutter leaves a nod with no signal and biases the background subtraction.
- **Wavelength zero point.** The empirical correction for the source position in the shutter is off by default.
- **Automatic redshifts.** The automatic redshift and its confidence are a starting point. Single emission lines are inherently ambiguous and warrant a tentative grade at most.
- **Line fluxes.** Line fluxes exist only for objects inspected at grade probable or secure, and only for the lines of the fit's catalog. A fit can be stale when the inspected redshift or the spectrum changed after it; the `stale_redshift` and `stale_spectrum` flags say so.

NIRCam:

- **Extended low-surface-brightness structure.** The per-exposure 2-D background removes intracluster light and the wings of bright galaxies by construction, and the mosaic background removes large-scale structure. The mosaics are not suited to low-surface-brightness science.
- **Diffraction spikes.** The background mesh removes about 1 percent of the flux in a bright star's diffraction spike, measured on one star. Compact-source photometry is unaffected.
- **Residual snowball offsets.** The jackknife correction removes 70 to 80 percent of the zero-point offsets at snowball footprints; a residual remains. On 5-group ramps the jump detection itself biases single-jump pixels by 1 to 3 DN per group, which this version does not correct.
- **Mosaics from pipeline versions before 0.5.0** carry a one-pixel sky offset relative to the declared tangent point. Check `CMPFRVER`.
- **Alignment.** The alignment path (JHAT or the pipeline's own step) is a per-field choice, and exposures that could not be aligned are absent from the mosaics rather than included with a poor solution.

Catalog:

- **Photometric redshifts** carry only the catalog's value and its errors; the P(z) curves are not part of the release.

## Getting the data and citing

The "Download" section of this page lists the release's export files for the programs you can read, and the API endpoint behind it. The "Cite" section gives the citation text and BibTeX.

The CAMPFIRE Python package installs the `campfire` command. After `campfire login`:

```
campfire dr list                                   # the releases you may read
campfire pull --dr 2026.1 --obs <observation>      # one observation of the release
campfire pull --dr 2026.1 --program <slug>         # one program
campfire pull --dr 2026.1 --field <field>          # a NIRCam field
campfire pull --dr 2026.1 --all                    # everything you may read
campfire cite 2026.1                               # the citation sentence
```

`campfire pull --dr` lands the release under `dr/2026.1/` in your CAMPFIRE data directory, never in the live tree: the catalog into `dr/2026.1/meta/`, the archived files into `dr/2026.1/products/`. The `--grating` and `--filters` options narrow a selection; `--intermediate` and `--stale` do not apply to a release. Every file is verified against the hash the release records.

In Python, `Campfire(dr="2026.1")` opens the pulled release offline. Its queries return the release's tables and nothing live, and `cite()` returns the citation sentence. `campfire identify <file>` reports whether a FITS file on your disk is a member of the release and whether it is still current in the live catalog. `campfire provenance` writes a provenance file for work done from the live catalog; `campfire pull --provenance <file>` fetches exactly the bytes such a file records.

Public programs are cited through the release's DOI. Proprietary programs in the release are not covered by the DOI; the citation sentence describes them with the pipeline versions and CRDS contexts they were reduced with, as read from the spectra you hold.
