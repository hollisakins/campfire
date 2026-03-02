# NIRSpec Pipeline

The CAMPFIRE NIRSpec pipeline reduces raw JWST/NIRSpec multi-object spectroscopy (MOS) observations into calibrated 1D spectra with automated redshift estimates. It processes data source-by-source, extracting spectra for every object that falls within a slitlet — not just primary targets. Detailed API documentation will be available soon; this document summarizes the pipeline and key changes relative to the default JWST science calibration pipeline. 

## Pipeline Stages

The reduction proceeds through four stages plus redshift fitting:

| Stage | Input | Output | Description |
|-------|-------|--------|-------------|
| **Stage 1** | `_uncal.fits` | `_rate.fits` | Detector processing + iterative background, 1/f subtraction |
| **Stage 2a** | `_rate.fits` | `_cal.fits` | WCS assignment, flat-fielding, flux/wavelength calibration (per-source extraction) |
| **Stuck shutter masking** | — | — | Manual flagging of disobedient stuck-closed shutters |
| **Stage 2b** | `_cal.fits` | `_cal_bkgsub.fits` | Nodded background subtraction |
| **Stage 3** | `_cal_bkgsub.fits` | `_spec.fits` | Combination, optimal extraction |
| **Redshift fitting** | `_spec.fits` | `_zfit.fits` | Template-based chi-squared redshift fitting |

Each stage can be run independently or chained together. Stages automatically skip sources whose output products already exist unless `--overwrite` is specified.

## Stage 1: Detector Processing & Background Subtraction

Stage 1 converts raw uncalibrated exposures into flux-rate images with clean backgrounds. This starts by running the stock STScI `Detector1Pipeline` on each `_uncal.fits` file, producing `_rate.fits` count-rate images.

### Iterative Background Subtraction

The CAMPFIRE pipeline implements subtraction of a custom multi-component background model to each rate file. This is the most significant departure from the stock pipeline at the detector level. Where the stock JWST pipeline can subtract 1/f noise via the `clean_flicker_noise` step (which can be enabled in the CAMPFIRE pipeline) this step doesn't perform adequately when there are substantial inhomogeneities in the background, i.e. due to the "picture frame" effect. Furthermore, the `mask_science_regions` option in the `clean_flicker_noise` step inadequately masks spectral traces, particularly for medium or high-resolution spectra. 

For the CAMPFIRE reduction pipeline, an iterative background subtraction step is run on each rate file. Each pass (5 by default) subtracts four components in sequence:

1. **Picture-frame template** — The thermal "[picture frame](https://jwst-pipeline.readthedocs.io/en/latest/api/jwst.picture_frame.PictureFrameStep.html)" effect (characteristic pedestal offsets at the detector edges) is subtracted using the picture frame reference file provided by STScI, scaling per detector amp-row (512 pixels).

2. **2D background** — A smooth two-dimensional background is estimated using `photutils.Background2D` with a configurable grid size (default 8x8 pixels), sigma-clipped median statistics, and a `BkgZoomInterpolator`. This captures large-scale spatial variations across the detector.

3. **Column 1/f noise** — The median of each column is subtracted.

4. **Row 1/f noise** — The median of each row is subtracted (note: row 1/f is only applied to PRISM data).

For all background estimation, the spectral traces of real sources are masked using the slitlet WCS solutions (accounting for the trace curvature) so that source flux does not bias the background model. The fixed slit region is also always masked.

After background subtraction, the **variance is rescaled** by comparing the observed pixel-to-pixel scatter with the pipeline's noise model. This corrects for cases where the pipeline over- or under-estimates the read noise contribution.

## Stage 2a: Spec2Pipeline Calibration

Stage 2a runs the STScI `Spec2Pipeline` on each source individually, performing WCS assignment, flat-fielding, pathloss correction, and flux/wavelength calibration.

### Source-by-Source Processing

A key aspect of the CAMPFIRE approach is that Stage 2a processes each source independently. For every rate file and every source, the pipeline:

1. Creates a **source-specific MSA metadata file** filtered to contain only the shutters relevant to that source
2. Builds an association file pointing the Spec2Pipeline to the correct rate file and metafile
3. Runs `Spec2Pipeline` with the following steps disabled: background subtraction (handled in Stage 2b), bar shadow correction, spectral resampling, and 1D extraction (handled in Stage 3)

This produces per-source `_cal.fits` files with WCS, flat-field, pathloss, and photometric calibrations applied. Bar shadow correction is disabled by default.

### Empirical Wavelength Correction

The pipeline optionally applies the **JADES DR4 empirical wavelength zero-point correction** from Scholtz et al. (2025). This correction accounts for a systematic wavelength offset that depends on the source position within the shutter. When enabled (`empirical_wavecorr = true` in configuration), the pipeline augments the CRDS `wavecorr` reference file with the JADES correction and passes it as an override to Spec2Pipeline's `wavecorr` step.

### Unit Correction

There is a bug in the JWST pipeline where the output units are wrong for slitlets that the pipeline thinks don't have a source in them. This can happen when there are stuck closed shutters or if a source is too close to the edge of the slit. The CAMPFIRE pipeline corrects the units and fixes the pathloss correction for these edge cases. 

## Stuck Shutter Masking

Between Stages 2a and 2b, stuck-closed shutters are identified and flagged. The MSA has shutters that occasionally fail to open; these are identified manually by examining 2D spectra. Stuck shutters are recorded per-observation and are flagged as closed in the MSA metafiles before nodded background subtraction.

## Stage 2b: Nodded Background Subtraction

Stage 2b performs nodded background subtraction using the multiple dithered exposures within each observation.

### Nod Subtraction

For each science exposure, the pipeline:

1. Groups exposures into **background groups** by detector and nod pattern
2. **Inverts the pathloss correction** on both science and background exposures (since pathloss depends on source position, which differs between nods)
3. Subtracts the median of the background exposures using JWST's `BackgroundStep`
4. **Re-applies the pathloss correction** to the background-subtracted result

### Exposure Group Support

The pipeline supports observations with 2, 3, or 5 nod positions. For each science exposure, all other exposures in the background group serve as backgrounds. **Background overrides** can be configured per-observation to allow selective nod pairing when certain exposures are compromised.

## Stage 3: Spectral Extraction

Stage 3 stacks exposures and produces the final 1D spectra through optimal extraction.

### Exposure Stacking

The STScI `Spec3Pipeline` is run first to combine the background-subtracted 2D spectra from multiple exposures into a single stacked 2D spectrum (`_s2d.fits`). The pipeline also records an **exposures table** in the output FITS file documenting the contributing exposures, their dither positions, effective exposure times, and stuck shutter lists.

### Optimal Extraction

The CAMPFIRE pipeline replaces JWST's default boxcar extraction with **optimal extraction** (Horne 1986). The extraction proceeds as follows:

1. The stacked 2D spectrum is **collapsed along the wavelength axis** (median) to build a spatial profile of the source
2. The spatial profile is restricted to the extraction aperture, negative values are zeroed, and the profile is **normalized to unit integral**
3. The 1D spectrum is extracted using **inverse-variance weighted** summation:

| Method | Description | Best for |
|--------|-------------|----------|
| **Optimal** | Profile-weighted, inverse-variance weighted | Faint sources, maximizing S/N |
| **3px boxcar** | Uniform weights, +/-1.5 pixel aperture | Compact sources |
| **4px boxcar** | Uniform weights, +/-2.0 pixel aperture | Slightly extended sources |
| **5px boxcar** | Uniform weights, +/-2.5 pixel aperture | Extended sources |

All four extractions are stored in the output `_spec.fits` file. The optimal extraction is the recommended default for most science applications, as it provides the highest signal-to-noise for unresolved or marginally resolved sources.

### Combining multiple dithers

When an observation has multiple exposure groups (e.g., from separate visits/dithers or MSA configurations), the per-group 1D spectra are combined into a single spectrum:

1. Each per-group spectrum is **resampled** onto a common wavelength grid using `spectres` 
2. Spectra are combined with **exposure-time weighting** 
3. Optional **sigma clipping** (default 3-sigma, 5 iterations) rejects outlier pixels when three or more spectra are available

Combining the 1D spectra avoids inaccuracies from combining 2D spectra with different intra-shutter positions. The 2D spectra are still produced, but the 1D is derived by stacking the component 1D spectra, not directly extracted from the 2D. 

## Redshift Fitting

After extraction, the pipeline performs automated redshift fitting using a chi-squared template-fitting approach.

### Fitting Strategy

The fitting uses a **two-stage approach**:

1. **Coarse pass** — The spectrum is fitted against templates on a velocity-spaced redshift grid (e.g., 500 km/s spacing for prism, 100 km/s for medium-resolution gratings) spanning the full redshift range. This identifies the approximate best-fit redshift.

2. **Fine refinement** — A high-resolution grid is constructed in a narrow window (typically +/-3000 km/s) around the coarse best-fit, with much finer velocity spacing (e.g., 30 km/s for prism, 10 km/s for medium-resolution). This refines the redshift to higher precision.

The velocity spacing and refinement parameters are configured per grating to match their spectral resolution.

### Template Library

The fitting uses a comprehensive template library that includes:

**Continuum templates (16 models)** — Stellar population synthesis models generated with `bagpipes`, spanning a range of star-formation histories, ages, metallicities, and dust attenuation laws. The templates use delayed-tau star formation histories with ages from 0.01 Gyr to ~0.95x the age of the universe. 

The templates incorporate **redshift-dependent evolution**: metallicity decreases by a factor of 2 from z=0 to z=10, and dust attenuation peaks around z~2.8 (near the peak of cosmic star formation) following a Gaussian profile.

**Emission line templates (20 lines)** — Rest-frame emission lines including Lyman-alpha, CIV, CIII], [OII], H-beta, [OIII] doublet, H-alpha, [NII] doublet, and others. Several groups of lines are tied together in order to constrain the parameter space (e.g., the H-beta template includes some H-alpha). Lines are placed with sub-pixel precision using linear interpolation.

**Broadline templates (4 models)** — Gaussian emission line profiles for H-alpha and H-beta at two velocity widths (1500 and 3000 km/s), enabling identification of broad-line AGN.

**Blackbody and modified blackbody templates (4 models)** — Planck functions at 500K, 2500K, and 5000K, plus a modified blackbody model. These capture the spectral shapes of dust-dominated or featureless sources that stellar population models may not adequately describe.

### NNLS Fitting

At each trial redshift, the observed spectrum is fitted as a **non-negative linear combination** of templates using the Lawson-Hanson NNLS (non-negative least squares) algorithm. The non-negativity constraint ensures physically meaningful template coefficients (no negative flux contributions).

The fitting operates on **Gram matrices** (A^T A and A^T b) rather than the full template matrix, reducing memory usage and enabling efficient parallelization across redshifts using numba. A small ridge regularization term prevents numerical issues from nearly-collinear templates.

After fitting, a **confidence metric** is computed from the chi-squared curve. The chi-squared values are converted to relative probabilities via p(z) = exp(-(chi2 - chi2_min)), and the confidence is defined as the fraction of total probability within +/-0.03 of the best-fit redshift. Higher confidence indicates a well-constrained, isolated chi-squared minimum; lower confidence suggests multiple competing redshift solutions.

This automated confidence is a starting point — all redshifts are subsequently [visually inspected](/docs/inspection) and assigned human quality ratings.

## Output Products

The pipeline produces two primary output files per source per grating:

### Spectrum File (`_spec.fits`)

| HDU | Name | Contents |
|-----|------|----------|
| 0 | PRIMARY | Header with observation metadata |
| 1 | SPEC1D | 1D spectrum table: wavelength, optimal and boxcar flux/error in f_nu and f_lambda units |
| 2 | SCI | 2D spectrum (flux in microjansky) |
| 3 | ERR | 2D error array |
| 4 | WAVELENGTH | 2D wavelength map |
| 5 | WHT | 2D weight map |
| 6 | PROF1D | Spatial profile weights for each extraction method |
| 7 | EXPOSURES | Per-exposure metadata (filenames, dither info, exposure times) |

### Redshift Fit File (`_zfit.fits`)

| HDU | Name | Contents |
|-----|------|----------|
| 0 | PRIMARY | Header with `ZBEST`, `ZQUAL`, `ZCONF`, `CHI2MIN` |
| 1 | MODEL | Best-fit model spectrum (wavelength, flux) |
| 2 | CHI2 | Chi-squared vs. redshift curve |

## Next Steps

- [Visual Inspection](/docs/inspection) — How reduced spectra are inspected and quality-assessed
- [Data Products](/docs/data-products) — Detailed column definitions and file formats
