# Slit losses and the MSA optics

An MSA shutter is 0.20″ wide and 0.46″ tall on the sky, smaller than the telescope's image of a point source at long wavelengths and smaller than most galaxies. Part of every source's light falls on the shutter's walls and never reaches the detector. How much is lost depends on the wavelength, the size of the source, and where it sits in the shutter. Correcting for it is called the path loss, or slit loss.

`jwst` corrects it with two reference tables: one for a point source, one for uniform light filling the shutter. A real galaxy is neither. CAMPFIRE replaces the dependence on position along the slit with a model of the MSA's optics that works for a source of any size and position, and keeps `jwst`'s point-source table across the shutter, with a correction measured from stars.

## How the model makes a source's image

The model follows the light through the instrument in three steps.

![The light of one point source, sitting 0.3 shutter widths from the center of the middle shutter of a three-shutter slitlet, along the slit at 1 µm (blue) and 5 µm (orange). 1: the telescope's image arriving at the MSA; at 5 µm it is several times wider and reaches the bar. 2: the shutter cuts it sharply at its edges. 3: on the detector, after the light diffracted at the shutter edges is partly lost at the pupil stop, and after the detector's blur. At 1 µm almost all the light arrives; at 5 µm a fifth is lost.](/docs/nirspec/optics_steps.png)

1. **Arriving at the MSA.** The telescope's diffraction pattern, computed from its 6.5 m pupil for each wavelength. Before the MSA, each point is also spread by a small measured kernel: the telescope's wavefront error and pointing jitter. A galaxy is the sum of such images over its shape (a round Gaussian of fitted width).
2. **Transmitted by the MSA.** The open area of each shutter passes light; the bars and walls block it. Near a shutter's edge the transmission falls smoothly, because light diffracted at the edge spreads out of the spectrograph's beam.
3. **Reaching the detector.** The spectrograph's pupil stop removes part of that diffracted light, and the detector blurs what is left before it is sampled by 0.1″ pixels.

Each exposure's own open shutters enter the model, so a source in the end shutter of a slitlet, or in a slitlet of one shutter, loses the light that falls on the closed neighbors.

## From a point source to uniform light

The same model gives the light that reaches the detector for any source. For a point source it should agree with `jwst`'s point-source table; for light that fills the shutter, with `jwst`'s uniform table. Between the two, where most galaxies are, `jwst` has nothing.

![The fraction of the light reaching the detector, as a function of where the source sits along the slit, at 0.8 to 1.2 µm (top) and 4.5 to 5.3 µm (bottom), for sources of increasing size from left to right. Left: a point source, with jwst's point-source table (dashed) and calibration stars (gray points, black medians). Right: uniform light, with jwst's uniform table. In between: galaxies of 0.1″, 0.3″ and 1″ across (FWHM). A small source loses most near the shutter edges; a large one loses about the same wherever it sits.](/docs/nirspec/pathloss.png)

The model was not fitted to either `jwst` table: it was fitted to calibration stars and to the background in empty shutters (next section). Where it agrees with them, it is because both describe the same instrument.

## How the model was derived

Two kinds of data fix the model's numbers:

- **Calibration stars.** Stars observed through the MSA at many positions in their shutters, in programs 6642 and 12497 and in ten MSA configurations of the open cluster NGC 2506 (program 1493), all through the PRISM. A star is a point source, so its image on the detector shows the telescope's pattern, the shutter's cut and the detector's blur directly. The stars set the kernel before the MSA (one per detector), the blur after it, how the transmission falls at a shutter's edge, and the correction across the shutter.
- **Uniform light.** The background in empty shutters shows how the shutter edges dim light that fills them (the bar shadow, see [Backgrounds](/docs/reduction/nirspec/backgrounds#the-bar-shadow)). The model was fitted to it together with the stars, and reproduces the measured bar shadow of every disperser.

One model serves every disperser. The PRISM stars fix its numbers; the grating stars and the grating bar-shadow tables were checked against it. The model's version is in the `PFOPTIC` header card, and the files it was derived from are listed in `pipeline/campfire_pipeline/data/nirspec_calibrations.toml`.

## Across the shutter

The shutter is narrower across the dispersion (0.20″) than along the slit, and a source's position across it is set by the pointing, not measured from the spectrum. CAMPFIRE keeps `jwst`'s point-source table in this direction and multiplies it by a correction measured from the calibration stars, plus, for an extended source, the model's ratio between that source and a point source.

## Where the bars are

The model needs to know where the shutter edges sit on the detector. CAMPFIRE measures this once per observation and disperser, from the edges of the empty shutters' background, and applies the offset in every fit (header card `PFBAROFF`).

## Switching back to jwst

| Setting | Effect |
|---|---|
| `[nirspec.stage3] slit_loss_y = "jwst"` | Along the slit, `jwst`'s point-source path loss at the planned position; the model only shapes the image |
| `[nirspec.stage3] slit_loss_x = "jwst"` | Across the shutter, `jwst`'s table without the star correction |
| `[nirspec.stage3] bar_offset = "off"` | No bar offset |

Each spectrum records the choice in `PFSLY`, `PFSLX` and `PFBAROFF`. The boxcar and optimal extractions in the same file (`fnu_3px`, `fnu_4px`, `fnu_5px`, `fnu_opt`) always use `jwst`'s point-source calibration.
