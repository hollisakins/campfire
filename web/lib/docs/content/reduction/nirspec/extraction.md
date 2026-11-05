# Extraction

Stage 3 turns each source's background-subtracted 2-D spectra into one 1-D spectrum per disperser. Instead of summing pixels in a fixed window, CAMPFIRE fits a model of the source, seen through the [MSA optics](/docs/reduction/nirspec/slit-losses), to every exposure at once. The model accounts for where the source's light went, including the light lost at the shutter edges and the negative copies the nod subtraction left, so it can report the source's own flux.

## The model of a source

A source is modeled as a round Gaussian of some width and position in its shutter. Both may change slowly with wavelength. Each exposure sees the source through its own shutters, so the model of one exposure is the source's image in its own shutter, minus its images where it sat in the exposures that were subtracted. One spectrum is shared by every exposure, and it is solved for, wavelength by wavelength, together with the shape.

![Four PRISM sources. Each row: the combined 2-D spectrum (rows are position along the slitlet), the fitted model, and the difference on the same color scale, with the profile along the slit summed over wavelength (data in gray, model in orange). Red is positive, blue negative: the source in its own shutter and its negative copies from the nod subtraction. A bright compact source, two extended ones (one sitting near a bar), and a faint source, which takes a default shape. For the two extended sources a little light at the center is left over: a Gaussian is not a galaxy's exact shape.](/docs/nirspec/extraction_prism.png)

### Which shape a source gets

A faint source does not have enough light to measure its own size. CAMPFIRE picks the shape in this order:

1. **Its shape in the PRISM**, for a grating spectrum, when the same source's PRISM spectrum took its own shape at the same telescope roll. A grating's own fit replaces it only when the source is very bright (a signal-to-noise of 100) and its own fit is clearly better and different.
2. **Its own fitted shape**, when the source is detected strongly enough (a signal-to-noise of 25 in the PRISM, 70 in the gratings) and the fit stays within its limits.
3. **A default shape**: a compact source (0.16″ across) at its planned position.

The header cards `PFSHAPE` and `PFREASON` say which one was used and why.

## What the reported flux means

A shutter catches only part of a galaxy, and a smaller part the bigger the galaxy is. Because the fit models the whole source, it can report the light of the fitted galaxy inside a fixed circle 0.8″ across, centered on it, before the telescope blurs it. For a point source that is all of its light. This is the flux in `fnu`. The circle is 1.5 shutter lengths across, which limits how far the model extrapolates a large source beyond what the shutter saw.

![Left: a galaxy 0.4″ across (FWHM), the open shutter with its neighbors in the slitlet (dashed), and the 0.8″ circle the reported flux refers to. Right: the spectrum of each of 127 galaxies divided by its imaging photometry in a 0.8″ aperture, against the galaxy's fitted size. Gray: the optimal extraction, the light that entered the shutter, which falls with size. Blue: the reported flux, close to the aperture photometry for most galaxies. For the largest, the reported flux is above it.](/docs/nirspec/reported_flux.png)

The optimal extraction (`fnu_opt`) and the boxcar extractions (`fnu_3px`, `fnu_4px`, `fnu_5px`) are kept in the same file. They measure the light that entered the shutter, with `jwst`'s point-source path-loss correction, and are not comparable with `fnu` for an extended source.

## Overlapping spectra

On the gratings, each slitlet's spectrum is long and the MSA packs slitlets close together, so the spectra of different slitlets overlap on the detector. A pixel often receives light from more than one source.

![One G395M exposure. Top: how many slitlets send light to each pixel of the two detectors. Middle: the count rate in a strip of the detector. Bottom: the same strip with every slitlet's footprint outlined and labeled with its source.](/docs/nirspec/overlap.png)

CAMPFIRE maps where every shutter's light falls in every exposure, including light past the calibrated wavelength range. It then fits each source together with every source whose light falls on its pixels, each at a fixed shape, on the same nod-subtracted frames. This first pass gives each neighbor's light on the source's pixels. That light is taken out before the source's own shape is fitted, and it is kept as `CONTAM2D` and `fnu_contam`.

![A G395M source with the light of eight other sources on its spectrum. Top: the data, this source's model, the other sources' predicted light, and what is left after both (some light remains on the trace at 3 to 4.5 µm). Bottom: the spectrum with nothing subtracted (gray), the other sources' light in it (yellow), the source alone (blue), and the same source in the PRISM (dashed), where its spectrum is not overlapped.](/docs/nirspec/contamination_g395m.png)

The neighbors' light is reported, not subtracted:

| Column | What it is |
|---|---|
| `fnu_prof` | The spectrum extracted from the combined 2-D with the model as weights, nothing subtracted |
| `fnu_contam` | The other sources' predicted light in it, extracted the same way |
| `fnu` | Equal to `fnu_prof` by default |

The source alone is `fnu_prof - fnu_contam`. Whether to subtract is left to you: the prediction is only as good as the neighbors' own spectra, and its uncertainty is in `fnu_contam_err`. `fnu_contam` is empty (NaN) where nothing was predicted, and where the fit could not separate the source from a neighbor. The header card `CONTSTAT` says whether any light was predicted; `EXTRWHY` says why `fnu` is what it is. Setting `[nirspec.stage3] contam_1d = "subtract"` subtracts the prediction in `fnu` when it is significant (`EXTRACT = profile_decontam`).

In the 2-D extensions, `PROF2D` is the source's model per µJy of reported flux and `CONTAM2D` the other sources' light, both in the units of `SCI`, so the full model is `PROF2D × (fnu_prof - fnu_contam) + CONTAM2D`.

### Neighbors in the same slitlet

A neighbor in the source's own slitlet also affects the nod subtraction (see [Leaving out a nod](/docs/reduction/nirspec/backgrounds#leaving-out-a-nod)). After the first pass, CAMPFIRE redoes those choices with every neighbor's fitted brightness, redoes the nod subtraction for the sources whose choice changes, and fits them again. It then looks for neighbors that are not in the catalog: it refits each source with a second source added along the slitlet, and reports a position where the fit improves strongly. These reports are proposals for review; they do not change the spectrum.

## Combining exposures

All exposures of a source in one disperser go into one fit. When an observation repeats the same MSA configuration (several exposure groups), they are combined in the same fit by default (`combine_method = "2d"`). The `EXPOSURES` table of the spectrum file lists every exposure used, and which exposures were subtracted from it.

## Units and errors

`wave` is in µm, `fnu` and its companions in µJy, with `flam` versions in erg s⁻¹ cm⁻² Å⁻¹. `fnu_err` is statistical: it comes from the 2-D error, which carries the noise of the subtracted exposures. It does not include the uncertainty of the neighbors' predicted light (`fnu_contam_err`), nor, for a source with the default shape, the uncertainty of the assumed size, which moves its flux by about 15 % either way.

## The master-background spectrum

Every file also carries a second spectrum, extracted the same way from the same exposures with the master background subtracted instead of the other nods. See [Two spectra in every file](/docs/reduction/nirspec/backgrounds#two-spectra-in-every-file) for when it is the better one.

## Describing it in a paper

> Each source's 2-D spectrum, in every exposure at once, was fitted with a Gaussian galaxy seen through a model of the MSA optics; the reported flux is the light of the fitted galaxy within a 0.8″ circle. Sources too faint to measure their own size were extracted as a compact source at the planned position (`PFSHAPE = default`).
