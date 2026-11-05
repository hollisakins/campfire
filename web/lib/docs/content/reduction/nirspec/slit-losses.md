# Slit losses and the MSA optics

An MSA shutter is about 0.2″ wide and 0.46″ tall on the sky, smaller than the telescope's image of a point source at long wavelengths and smaller than most galaxies. Part of every source's light falls on the bars between shutters and never reaches the detector. How much is lost depends on the wavelength, the size of the source, and where it sits in the shutter. Correcting for it is called the path loss, or slit loss.

`jwst` corrects it with two reference tables: one for a point source, one for uniform light filling the shutter. A real galaxy is neither. Along the slit, CAMPFIRE replaces the correction with a model of the MSA's optics that works for a source of any size and position. Across the shutter it keeps `jwst`'s point-source table, with a correction measured from stars.

## How the model makes a source's image

The model follows the light through the instrument in four steps.

![The light of one point source, 0.16″ (0.3 of a shutter pitch) from the center of the middle shutter of a three-shutter slitlet, along the slit at 1 µm (blue) and 5 µm (orange), on a log scale so the faint light shows. 1: the telescope's diffraction pattern arriving at the MSA, its rings several times wider at 5 µm. 2: the same image spread by the kernel (dashed: step 1). The kernel fills in the rings and moves a little light onto the bar and into the neighboring shutters; at 5 µm the image is already wide and changes less. Inset: how much of the light the kernel moves farther than a given distance. 3: the shutters cut the image sharply at their edges; the neighboring open shutters pass the light that reaches them. 4: on the detector, after the light diffracted at the shutter edges is partly lost at the pupil stop, and after the detector's blur. The circles are calibration stars seen at this position in a slitlet like this one (programs 1133 and NGC 2506, single exposures with the background removed; the median of their pixels, with bars from the 25th to the 75th percentile), each scaled to the model's total light: the model's shape along the slit, compared with real stars.](/docs/nirspec/optics_steps.png)

1. **Arriving at the MSA.** The telescope's diffraction pattern, computed from its 6.5 m pupil for each wavelength.
2. **Spread by the kernel.** The telescope's wavefront error and the pointing jitter during an exposure move a source's light by small amounts before it reaches the MSA. The kernel says how much light lands at each shift; it is a table measured from calibration stars, one per detector, changing smoothly with wavelength. Every shifted copy of the image then goes through the next two steps on its own. Uniform light does not feel the kernel (shifting uniform light changes nothing), so the stars, not the empty shutters, fix it.
3. **Transmitted by the MSA.** The open area of each shutter passes light; the bars block it, cutting the image sharply at the shutter edges.
4. **Reaching the detector.** Light diffracted at the shutter edges spreads out of the spectrograph's beam, and the spectrograph's pupil stop removes part of it. The detector blurs what is left before it is sampled by 0.1″ pixels.

A galaxy is the sum of such images over its shape: a round Gaussian of fitted width, centered at its fitted position.

Each exposure's own open shutters enter the model, so a source in the end shutter of a slitlet, or in a slitlet of one shutter, loses the light that falls on the closed neighbors.

## From a point source to uniform light

The same model gives the light that reaches the detector for any source. For a point source at the shutter center it agrees with `jwst`'s point-source table by construction; off center, and for every size, it follows the instrument. Between a point source and uniform light, where most galaxies are, `jwst` has nothing.

![Of the light that falls on the open shutters, the fraction that reaches the detector, against where the source sits along the slit, at 0.8 to 1.2 µm (top) and 4.5 to 5.3 µm (bottom), for sources of increasing size from left to right. Light that falls on the bars is lost as well; the curves do not include it. The vertical scale is set to jwst's value for a point source at the center. Left: a point source, with jwst's point-source table (dashed) and calibration stars (gray points, black medians). Right: uniform light, with jwst's uniform table. In between: galaxies of 0.1″, 0.3″ and 1″ across (FWHM). A small source loses most near the shutter edges; at 5 µm a 0.1″ galaxy is still much smaller than the telescope's image, so it behaves almost like a point source. A large one loses about the same wherever it sits.](/docs/nirspec/pathloss.png)

The model sets how the loss changes with position and size. Its level, the loss of a point source at the shutter center, is taken from `jwst`'s point-source table. The model was not fitted to either `jwst` table.

## How the model was derived

Two kinds of data fix the model's numbers:

- **Calibration stars.** Stars observed through the MSA at many positions in their shutters: 593 stars in programs 6642 and 12497 and in ten MSA configurations of the open cluster NGC 2506 (program 1493), all through the PRISM. A star is a point source, so its image on the detector shows the telescope's pattern, the shutter's cut and the detector's blur directly. The stars set the kernel before the MSA (one per detector) and the correction across the shutter.
- **Uniform light.** The background in empty shutters shows how the shutter edges dim light that fills them (the bar shadow, see [Backgrounds](/docs/reduction/nirspec/backgrounds#the-bar-shadow)). The shape of the telescope's pupil, the size of the pupil stop and the detector blur were chosen so the model matches the measured PRISM bar shadow, without more widely spread light than the stars show. The kernel was then fitted to the stars.

One model serves every disperser: the light arriving at the MSA does not depend on the disperser. The grating stars and the G395M bar shadow were checked against it. The model's version is in the `PFOPTIC` header card, and the files it was derived from are listed in `pipeline/campfire_pipeline/data/nirspec_calibrations.toml`.

## Across the shutter

Across the dispersion, a source's position in its shutter is set by the pointing, not measured from the spectrum, and the data do not show its shape. CAMPFIRE keeps `jwst`'s point-source table in this direction and multiplies it by a correction measured from 93 stars of NGC 2506 seen at several positions, and, for an extended source, by the model's ratio between that source and a point source. Redward of about 2 µm, `jwst`'s table loses too little light for a source off center; the correction takes more.

## Where the bars are

The model needs to know where the shutter edges sit on the detector. CAMPFIRE measures this once per observation and disperser, from the edges of the empty shutters' background, and applies the offset in every fit (header card `PFBAROFF`).

## Switching back to jwst

| Setting | Effect |
|---|---|
| `[nirspec.stage3] slit_loss_y = "jwst"` | Along the slit, `jwst`'s point-source path loss at the planned position; the model only shapes the image |
| `[nirspec.stage3] slit_loss_x = "jwst"` | Across the shutter, `jwst`'s table without the star correction; the extended-source ratio still applies |
| `[nirspec.stage3] bar_offset = "off"` | No bar offset |

Each spectrum records the choice in `PFSLY`, `PFSLX` and `PFBAROFF`. The boxcar and optimal extractions in the same file (`fnu_3px`, `fnu_4px`, `fnu_5px`, `fnu_opt`) always carry `jwst`'s path-loss correction as Stage 2 applied it (point-source for nearly every source) and no bar-shadow correction.

## Describing it in a paper

> Slit losses along the slit were computed with a diffraction model of the MSA (the telescope pupil, the open shutters of each exposure, the spectrograph pupil stop and the detector blur, with an empirical kernel fitted to calibration stars), for a Gaussian source of the fitted width at the fitted position. Across the shutter, the `jwst` point-source path loss was used, with a correction fitted to stars.
