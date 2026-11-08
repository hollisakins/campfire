# Slit losses and the MSA optics

An MSA shutter subtends about 0.2″ × 0.46″, smaller than the point-spread function at long wavelengths and than most galaxies. The fraction of a source's flux lost to the shutter bars and edges (the path loss, or slit loss) depends on wavelength, source size, and position within the shutter.

`jwst` corrects path losses with two references, one for a point source and one for uniform illumination; real galaxies lie between the two. Along the slit, CAMPFIRE replaces this correction with a diffraction model of the MSA optics, valid for a source of any size and position. Across the shutter, it retains the `jwst` point-source reference with an empirical correction derived from stars.

## Optical model

The model propagates the light through four stages.

![A point source 0.16″ (0.3 shutter pitches) from the center of the middle shutter of a three-shutter slitlet, along the slit at 1 µm (blue) and 5 µm (orange), on a logarithmic scale. 1: the telescope PSF at the MSA plane. 2: the PSF convolved with the kernel (dashed: stage 1); the kernel suppresses the diffraction minima and redistributes a small fraction of the flux onto the bar and into the adjacent shutters. Inset: the kernel. 3: truncation at the shutter edges. 4: the detector-plane profile, averaged over the wavelength bands of the calibration stars overplotted (circles: median of the stellar pixel values, bars: interquartile range; programs 1133 and NGC 2506, background-subtracted single exposures, each normalized to the model's total flux). Bottom: ratio of the stars to the model.](/docs/nirspec/optics_steps.png)

1. **Telescope PSF.** The diffraction pattern of the 6.5 m pupil at the MSA plane, computed at each wavelength.
2. **Kernel.** Wavefront error and pointing jitter redistribute the light by small amounts before it reaches the MSA. This is modeled as a convolution with an empirical kernel, fitted to calibration stars for each detector and interpolated in wavelength. Uniform illumination is invariant under the convolution, so the kernel is constrained by the stars rather than by the empty shutters.
3. **MSA transmission.** The open area of each shutter transmits the light; the bars block it, truncating the image sharply at the shutter edges.
4. **Detector plane.** Light diffracted at the shutter edges is partly removed by the spectrograph pupil stop; the remainder is blurred by the detector response and sampled by 0.1″ pixels.

An extended source is modeled as a circular Gaussian of fitted width and position, integrated over its profile. The open shutters of each exposure enter the model individually, so a source in the end shutter of a slitlet, or in a single-shutter slitlet, incurs the additional loss to its closed neighbors.

## From point sources to uniform illumination

The same model gives the transmitted fraction for any source size. For a centered point source it matches the `jwst` point-source reference by construction; elsewhere it follows from the instrument model. Between a point source and uniform illumination, which bracket most galaxies, `jwst` provides no correction.

![Fraction of a source's total flux reaching the detector as a function of position along the slit, at 0.8–1.2 µm (top) and 4.5–5.3 µm (bottom), for increasing source size from left to right. All losses are included: flux falling on the bars, and edge-diffracted light removed at the pupil stop. The vertical scale is normalized to the jwst value for a centered point source. Left: point source, with the jwst point-source reference (dashed) and calibration stars (gray: individual visits; black: medians). Right: uniform illumination, as a fraction of the flux incident on the slitlet footprint (bars included), with the jwst uniform reference on the same scale. Middle: Gaussian sources of 0.05″ and 0.2″ FWHM. Point sources lose flux only near the shutter edges; larger sources lose an increasing fraction to the bars at all positions. At 5 µm, a 0.05″ source remains much smaller than the PSF and behaves nearly as a point source.](/docs/nirspec/pathloss.png)

The model sets the dependence of the loss on position and source size; its normalization, the loss of a centered point source, is taken from the `jwst` point-source reference. The model was not fitted to either `jwst` reference.

## Calibration of the model

The model parameters are constrained by two data sets:

- **Calibration stars.** PRISM spectra of 590 star-detector pairs (a star recorded on both detectors counts twice) at a range of positions within their shutters: programs 6642 and 12497, ten MSA configurations of the open cluster NGC 2506 (program 1493), and the commissioning slit-loss program, which stepped stars across their shutters (program 1133). Each star-detector pair is weighted once in the fit regardless of its number of exposures, and each observation's bar offset is applied. The stars constrain the kernel (one per detector) and the correction across the shutter.
- **Uniform illumination.** The bar shadow measured from empty shutters ([Bar-shadow calibration](/docs/reduction/nirspec/backgrounds#bar-shadow-calibration)). The pupil apodization, the pupil-stop size and the detector blur were chosen to reproduce the measured PRISM bar shadow without exceeding the wide-angle light observed in the stars; the kernel was then fitted to the stars.

![The model for uniform illumination of a three-shutter slitlet (lines) compared with the bar shadow measured from empty PRISM shutters (circles), both averaged over 0.8–1.2 µm (blue) and 4.5–5.3 µm (orange) and normalized at the center of the middle shutter. Bottom: ratio of measurement to model within the slitlet.](/docs/nirspec/optics_uniform.png)

A single model is used for all dispersers, since the light incident on the MSA does not depend on the disperser; it was verified against grating observations of calibration stars and the G395M bar shadow. The model version is recorded in the `OPTICVER` keyword, and its input files are listed in `pipeline/campfire_pipeline/data/nirspec_calibrations.toml`.

## Path loss across the shutter

Across the dispersion direction, the source position within the shutter is set by the pointing and is not measured from the spectrum, and the source profile is not resolved. CAMPFIRE therefore retains the `jwst` point-source reference in this direction, multiplied by a correction measured from 93 NGC 2506 stars observed at several positions and, for extended sources, by the model ratio between the extended and point-source transmission. Redward of about 2 µm, the `jwst` reference underestimates the loss for off-center sources; the correction accounts for this.

## Bar offset

The model requires the position of the shutter edges on the detector. The offset of the bars relative to the `jwst` slit frame is measured once per observation and disperser from the edges of the empty-shutter background, and applied in every fit (keyword `BAROFF`).

## Reverting to the jwst references

| Option | Effect |
|---|---|
| `[nirspec.stage3] slit_loss_y = "jwst"` | Along the slit: `jwst` point-source path loss at the planned position; the model determines only the spatial profile |
| `[nirspec.stage3] slit_loss_x = "jwst"` | Across the shutter: `jwst` reference without the stellar correction; the extended-source ratio is still applied |
| `[nirspec.stage3] bar_offset = "off"` | No bar offset |

The settings are recorded in `SLOSSY`, `SLOSSX` and `BAROFF`. The boxcar and optimal extractions in the same file (`fnu_3px`, `fnu_4px`, `fnu_5px`, `fnu_opt`) always carry the `jwst` path-loss correction applied in Stage 2 (point-source for nearly all sources) and no bar-shadow correction.

## Summary for publications

> Slit losses along the slit were computed with a diffraction model of the MSA (telescope pupil, open shutters of each exposure, spectrograph pupil stop and detector response, with an empirical kernel fitted to calibration stars), evaluated for a Gaussian source of the fitted width and position. Across the shutter, the `jwst` point-source path loss was used with an empirical correction fitted to stars.
