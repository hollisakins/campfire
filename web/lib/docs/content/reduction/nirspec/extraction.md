# Extraction

Stage 3 produces one 1-D spectrum per source and disperser from the background-subtracted 2-D spectra. Rather than summing pixels within a fixed aperture, CAMPFIRE fits a forward model of the source, convolved with the [MSA optics](/docs/reduction/nirspec/slit-losses), to all exposures simultaneously. The model accounts for the flux lost at the shutter edges and for the negative images left by nod subtraction, so that the extracted flux is that of the source itself.

## Source model

Each source is modeled as a circular Gaussian with a width and position that may vary smoothly with wavelength. In each exposure the model is the source's image through that exposure's shutters, minus its images at the positions of the subtracted background exposures. A single spectrum, common to all exposures, is solved wavelength by wavelength together with the spatial parameters.

![Four PRISM sources. Each row: the combined 2-D spectrum (rows along the slitlet), the fitted model, and the residual on a common scale, with the spatial profile summed over wavelength (data gray, model orange). Positive (red) and negative (blue) signal correspond to the source in its own shutter and its negative images from nod subtraction. From top: a bright compact source, two extended sources (one adjacent to a bar), and a faint source fitted with the default profile. The extended sources leave a small central residual, reflecting departures from a Gaussian profile.](/docs/nirspec/extraction_prism.png)

### Profile selection

Faint sources do not constrain their own spatial profile. The profile is chosen in the following order:

1. **PRISM profile**, for a grating spectrum, if the same source's PRISM spectrum was fitted with its own profile at the same position angle. A grating's own fit supersedes it only for bright sources (detection S/N ≥ 100) whose own fit is both significantly better and significantly different.
2. **Own fitted profile**, if the detection S/N exceeds 25 (PRISM) or 70 (gratings) and no parameter of the fit reaches its bound. A fit whose center alone reaches its bound is kept when it is significantly better than the alternative, since a large offset from the planned position can be real.
3. **Profile from another grating**, for a grating spectrum without a PRISM profile that would otherwise take the default: the center offset and width of the same source's grating spectrum fitted with its own profile (the highest detection S/N if several), held constant with wavelength.
4. **Default profile**: a compact source (0.16″ FWHM) at the planned position.

The keywords `SHAPE` (`prism`, `own`, `grating`, `default`) and `SHAPEWHY` record the profile adopted and the reason.

## Definition of the reported flux

A shutter samples only part of an extended source, and a decreasing fraction with increasing size. Along the slit the fit measures the source's profile, so the reported flux includes all of the fitted source's light in that direction. Across the shutter the data do not constrain the shape, so the reported flux stops at the shutter edges. It is the flux of the fitted, intrinsic (pre-PSF) profile within the 0.22″ open width of the shutter (`FLUXWID`), at each exposure's planned position with an allowance of 0.03″ for pointing scatter, averaged over the exposures. This is the quantity in `fnu`.

A point source at the shutter center gives its total flux; 0.05″ and 0.10″ off center across the shutter, it gives 98 % and 63 %. A centered galaxy of 0.2″ FWHM gives 78 %, and one of 0.4″ gives 48 %. The reported flux is therefore not a total flux; as for any slit spectrum, comparison with photometry sets the normalization. Because it is close to the light the shutter recorded, it depends only weakly on the fitted size.

![Left: a 0.4″ FWHM galaxy at the center of its open shutter, with the adjacent shutters of the slitlet (dashed). The reported flux is the galaxy's intrinsic light within the shaded band: the shutter's open width across the dispersion, and the full extent along the slit. Right: the reported fraction of a source's total flux as a function of its intrinsic size, for three positions across the shutter; the point marks the galaxy on the left.](/docs/nirspec/reported_flux.png)

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

`wave` is in µm, `fnu` and related columns in µJy, and the `flam` columns in erg s⁻¹ cm⁻² Å⁻¹. `fnu_err` is statistical, propagated from the 2-D error array including the noise of the subtracted exposures. It excludes the uncertainty of the contamination prediction (`fnu_contam_err`) and, for sources with the default profile, the uncertainty of the assumed size, which corresponds to roughly ±15 % in flux.

## Master-background spectrum

Each file also contains a spectrum extracted identically from the same exposures with the master background subtracted in place of the nod subtraction; see [Master-background spectra](/docs/reduction/nirspec/backgrounds#master-background-spectra).

## Summary for publications

> Each source's 2-D spectrum was fitted simultaneously in all exposures with a Gaussian source model convolved with a model of the MSA optics; the reported flux is that of the fitted intrinsic profile within the open width of the shutter across the dispersion and over its full extent along the slit. Sources too faint to constrain their own profile were extracted with a compact default profile at the planned position (`SHAPE = default`).
