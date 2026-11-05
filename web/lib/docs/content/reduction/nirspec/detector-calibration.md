# Detector and calibration

This page covers the first two steps of the [NIRSpec pipeline](/docs/reduction/nirspec): turning the raw detector ramps into count-rate images, and turning those into calibrated 2-D spectra, one per source and exposure. Most of this is the standard `jwst` pipeline. CAMPFIRE adds one thing of its own here: it removes the detector's own background before any spectrum is calibrated.

## From ramps to count rates

`jwst`'s detector-level pipeline (`Detector1Pipeline`) fits each pixel's ramp and writes a count-rate image per exposure and detector. Its 1/f cleaning (`clean_flicker_noise`) is on and runs inside this step.

## The detector's own background

A NIRSpec count-rate image is not zero where no light falls. The detector adds a pattern of its own: a "picture frame" that is brighter toward the edges, an offset that differs between the four quarters of the detector, and stripes along rows and columns from 1/f noise that the `jwst` cleaning does not fully remove. This light comes from the detector, not the sky, so it must be removed before the spectra are cut out and calibrated.

![Stage 1 background subtraction for one PRISM exposure. Left: the background model over the whole detector. The diagonal and edge structure is the picture frame; the fine stripes are 1/f noise. The orange box marks the region shown in the next three panels: the count rate before, the background model, and the count rate after, all on one color scale. Bottom: the median of the empty pixels in each column of that region, before (gray) and after (blue). The detector's offset is removed.](/docs/nirspec/detector_background.png)

CAMPFIRE fits this background to the pixels that receive no light, and subtracts it from the whole image. Pixels that do receive light are kept out of the fit:

- every open shutter's spectrum, mapped from the MSA plan, extended past the nominal wavelength range of the gratings so that higher-order light is also kept out;
- the band of the detector behind the fixed slits;
- bad pixels, and any regions masked by hand in `observations.toml` (see [Masking by hand](#masking-by-hand));
- in each pass, pixels more than two standard deviations above the background, so that faint light the map missed is not fitted as background.

The background model has these parts, fitted in turn:

| Part | How it is fitted |
|---|---|
| Picture frame | The CRDS picture-frame reference, scaled and offset separately in each quarter of the detector |
| Column stripes (1/f) | In each column, the picture-frame shape scaled and offset to that column |
| Row stripes (1/f) | The median of each row; PRISM only |
| Smooth 2-D background | Off by default (`subtract_2d = false`) |

The fit is repeated five times. Each pass refits every part on what the previous passes left, with the mask of bright pixels updated each time.

After the subtraction, the scatter of the empty pixels is compared with the read noise `jwst` predicts for them, and the read-noise variance is scaled to match. The count-rate file keeps the background it subtracted (`CFBKG`), the pixels the fit used (`CFBKGMASK`) and the variance factor (`CFBKGRMS`), so the step can be checked and undone.

### Masking by hand

Some exposures have features no automatic mask catches, for example light scattered onto the detector from a bright source outside the MSA. These can be outlined by hand as DS9 polygons, stored with the observation in `observations.toml`. Masked pixels are flagged `DO_NOT_USE`: they are kept out of the background fit and out of everything that follows. Changing a mask re-runs the background subtraction from the original count rate.

## From count rates to calibrated 2-D spectra

Stage 2a runs `jwst`'s spectroscopic pipeline (`Spec2Pipeline`) once per count-rate file, for every source in it at once. It assigns the world coordinates and wavelengths, cuts out each shutter's 2-D spectrum, flat-fields it and converts it to flux density. Each source's cutout is then written to its own file.

Four `jwst` steps are skipped, because CAMPFIRE does them itself later:

| Skipped `jwst` step | Done instead by |
|---|---|
| Background subtraction (`bkg_subtract`) | [Nod subtraction and the master background](/docs/reduction/nirspec/backgrounds) |
| Bar shadow (`barshadow`) | The [optics model](/docs/reduction/nirspec/slit-losses) |
| Resampling to a rectified grid (`resample_spec`) | CAMPFIRE's own rectification, after the background subtraction |
| 1-D extraction (`extract_1d`) | The [profile extraction](/docs/reduction/nirspec/extraction) |

`jwst` calibrates every source as a point source at its planned position. CAMPFIRE's optics model in Stage 3 then replaces `jwst`'s point-source path loss with one for the source's fitted size and position.

Before the calibration, Stage 2a also finds shutters that were commanded open but stayed closed, so that `jwst` does not cut out a spectrum where no light came through. That, and the measurement of the background spectrum from the empty shutters, are on the [backgrounds](/docs/reduction/nirspec/backgrounds) page.

### Options that change the calibration

Two options replace `jwst` reference files at this stage. Both are off by default.

| Option | What it does |
|---|---|
| `[nirspec.stage2] empirical_wavecorr = true` | Adds the wavelength zero-point correction measured by JADES (Scholtz et al. 2025) to `jwst`'s correction for the source's position across the shutter |
| `[nirspec.stage2] extend_g140m_g235m = true` | Extends G140M/F100LP and G235M/F170LP past their nominal red limit, where the next order overlaps, with flats, wavelength ranges and flux calibration derived for that range. The Stage 1 mask is widened to match, so the extended light is not fitted as background |
