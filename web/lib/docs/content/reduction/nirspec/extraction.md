# Extraction

Stage 3 produces one 1-D spectrum per source and disperser from the background-subtracted 2-D spectra. Rather than summing pixels within a fixed aperture, CAMPFIRE fits a forward model of the source, convolved with the [MSA optics](/docs/reduction/nirspec/slit-losses), to all exposures simultaneously. The model accounts for the flux lost at the shutter edges and for the negative images left by nod subtraction, so that the extracted flux is that of the source itself.

## Source model

Each source is modeled as a circular Gaussian with a width and position that may vary smoothly with wavelength. In each exposure the model is the source's image through that exposure's shutters, minus its images at the positions of the subtracted background exposures. A single spectrum, common to all exposures, is solved wavelength by wavelength together with the spatial parameters.

![Four PRISM sources. Each row: the combined 2-D spectrum (rows along the slitlet), the fitted model, and the residual on a common scale, with the spatial profile summed over wavelength (data gray, model orange). Positive (red) and negative (blue) signal correspond to the source in its own shutter and its negative images from nod subtraction. From top: a bright compact source, two extended sources (one adjacent to a bar), and a faint source. The extended sources leave a small central residual, reflecting departures from a Gaussian profile.](/docs/nirspec/extraction_prism.png)

### Profile selection

The width and center of every fit have priors, so a faint source, which does not constrain its own profile, settles near a compact source at its planned position. The profile is chosen in the following order:

1. **PRISM profile**, for a grating spectrum, if the same source's PRISM spectrum was fitted with its own profile at the same position angle. A grating's own fit supersedes it only for bright sources (detection S/N ≥ 100) whose own fit is both significantly better and significantly different.
2. **Own fitted profile**, at any S/N. A fit with a parameter at its limit is kept and flagged in `SHAPEWHY` (`bound:`).
3. **Profile from another grating**, for a grating spectrum without a PRISM profile that would otherwise take the default: the center offset and width of the same source's grating spectrum fitted with its own profile (the highest detection S/N if several), held constant with wavelength.
4. **Default profile**: a compact source (0.16″ FWHM) at the planned position, where no own fit is possible (contamination leaves too few pixels, or the planned positions disagree between exposures).

The keywords `SHAPE` (`prism`, `own`, `grating`, `default`) and `SHAPEWHY` record the profile adopted and the reason.

## Definition of the reported flux

The model describes each source with an intrinsic (pre-PSF) profile, fitted or assumed (see [Profile selection](#profile-selection)). Along the slit, where the data sample the profile, `fnu` is that profile's full flux, including light that fell on the bars and the adjacent shutters. Along the dispersion, where a shutter gives no spatial information, it is the light within the shutter's 0.22″ open width (`FLUXWID`), as for any slit spectrum.

The optimal (`fnu_opt`) and boxcar (`fnu_3px`, `fnu_4px`, `fnu_5px`) extractions are also provided. They measure the flux entering the shutter with the `jwst` point-source path-loss correction, and are not directly comparable with `fnu` for extended sources.

## Overlapping spectra

On the gratings, long traces and dense MSA configurations cause spectra from different slitlets to overlap on the detector, so that many pixels receive flux from more than one source.

![One G395M exposure. Top: the number of slitlets contributing to each pixel of both detectors. Middle: count rate in a detector strip. Bottom: the same strip with each slitlet's footprint outlined and labeled by source.](/docs/nirspec/overlap.png)

CAMPFIRE maps the footprint of every shutter in every exposure, including the light beyond the calibrated wavelength range, and fits each source jointly with all sources whose traces overlap it, each with a fixed profile, on the same nod-subtracted frames. This first pass yields each neighbor's contribution to the target's pixels, which is removed before the target's own profile is fitted and retained as `CONTAM2D` and `fnu_contam`.

![A G395M source contaminated by eight other sources. Top: data, model of the target, predicted contamination, and residual (some signal remains on the trace at 3–4.5 µm). Bottom: uncorrected spectrum (gray), predicted contamination (yellow), decontaminated spectrum (blue), and the same source's uncontaminated PRISM spectrum (dashed).](/docs/nirspec/contamination_g395m.png)

The contamination is reported, not subtracted:

| Column | Content |
|---|---|
| `fnu_prof` | Spectrum extracted from the combined 2-D spectrum using the model as weights, without contamination subtraction |
| `fnu_contam` | Predicted contamination, extracted the same way |
| `fnu` | Equal to `fnu_prof` by default |

The decontaminated spectrum is `fnu_prof - fnu_contam`, with the systematic uncertainty of the prediction in `fnu_contam_err`. `fnu_contam` is NaN where no contamination was predicted and where the target could not be separated from a neighbor. `CONTSTAT` records whether contamination was predicted, and `EXTRWHY` the reason for the extraction adopted in `fnu`.

In the 2-D extensions, `PROF2D` is the source model per µJy of reported flux and `CONTAM2D` the contamination, both in the units of `SCI`; the full model is `PROF2D × (fnu_prof - fnu_contam) + CONTAM2D`.

### Neighbors within the slitlet

Neighbors in the target's own slitlet also affect the nod subtraction (see [Exclusion of contaminated background exposures](/docs/reduction/nirspec/backgrounds#exclusion-of-contaminated-background-exposures)). After the first pass, the exclusions are re-evaluated with each neighbor's fitted flux, and affected sources are re-subtracted and refitted. A search for uncataloged neighbors then refits each source with an additional component along the slitlet and reports positions where the fit improves significantly; these are flagged for review and do not modify the spectrum.

## Combination of exposures

All exposures of a source in a given disperser enter a single fit. Repeated MSA configurations (multiple exposure groups) are combined in the same fit by default (`combine_method = "2d"`). The `EXPOSURES` table lists each exposure used and the background exposures subtracted from it.

## Units and uncertainties

`wave` is in µm, `fnu` and related columns in µJy, and the `flam` columns in erg s⁻¹ cm⁻² Å⁻¹. `fnu_err` is statistical, propagated from the 2-D error array including the noise of the subtracted exposures. It excludes the uncertainty of the contamination prediction (`fnu_contam_err`) and the uncertainty of the source's size where the data do not measure it (`SHAPE = default`, or a faint fit held near the prior width), roughly ±10 % in flux.

## Master-background spectrum

Each file also contains a spectrum extracted identically from the same exposures with the master background subtracted in place of the nod subtraction; see [Master-background spectra](/docs/reduction/nirspec/backgrounds#master-background-spectra).
