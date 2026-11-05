# Detector and calibration

This page covers the first two steps of the [NIRSpec pipeline](/docs/reduction/nirspec): turning the raw detector ramps into count-rate images, and turning those into calibrated 2-D spectra, one per source and exposure. Most of this is the standard `jwst` pipeline, with the CRDS reference files for the flat fields, the wavelength solution and the flux calibration. CAMPFIRE adds one step of its own here: it removes the detector's own background before any spectrum is calibrated.

## From ramps to count rates

`jwst`'s detector-level pipeline (`Detector1Pipeline`) fits each pixel's ramp and writes a count-rate image per exposure and detector. Two of its settings differ from the defaults:

- the 1/f cleaning (`clean_flicker_noise`) is on, with the science regions masked;
- the jump step expands the flagged area around large cosmic-ray events (`expand_large_events`).

## The detector's own background

A NIRSpec count-rate image is not zero where no light falls. The detector adds a signal of its own:

- a "picture frame": darker edges and faint diagonal lines, the same pattern in every exposure, with an offset that differs between bands of the detector;
- 1/f noise: slow drifts of the readout electronics, seen as stripes along columns and rows. The `jwst` cleaning removes most of it, but not all.

This signal comes from the detector, not from the astronomical background, so it must be removed before the spectra are cut out and calibrated.

![Stage 1 background subtraction for one PRISM exposure. Left: the background model over the whole detector, smoothed so that the large-scale pattern shows. The dark edges and diagonal lines are the picture frame, from the CRDS reference. The orange box marks the region in the next three panels: the count rate before (pixels kept out of the fit tinted orange: spectra, and patches of faint light found by the clipping), the background model, and the count rate after, on one color scale centered on zero. Bottom: the median of the fitted pixels in each column of the region, before (gray) and after (blue). The detector's offset is removed.](/docs/nirspec/detector_background.png)

CAMPFIRE fits this background to the pixels that receive no light, and subtracts it from the whole image. Pixels that do receive light are kept out of the fit:

- every open shutter's spectrum, mapped from the MSA plan, including shutters known to have failed open. Each spectrum's footprint runs past the grating's nominal wavelength range, to cover light that falls beyond it (table below);
- the band of the detector behind the fixed slits;
- any pixel with a data-quality flag, including regions masked by hand (see [Masking by hand](#masking-by-hand));
- in each pass, pixels more than two standard deviations above the background, so that faint light the map missed is not fitted as background.

| Grating | Footprint kept out of the fit (µm) |
|---|---|
| G140M, G140H | 0.70 to 2.5 |
| G235M, G235H | 1.66 to 4.0 |
| G395M, G395H | 2.87 to 5.5 |

The background model has these parts, fitted in turn:

| Part | How it is fitted |
|---|---|
| Picture frame | The CRDS picture-frame reference, scaled and offset separately in each of four horizontal bands of 512 rows |
| Column stripes (1/f) | In each column, the picture-frame shape scaled and offset to that column |
| Row stripes (1/f) | The median of each row; PRISM only |
| Smooth 2-D background | Off by default (`subtract_2d = false`) |

The fit is repeated five times. Each pass refits every part on what the previous passes left, with the mask of bright pixels updated each time. Where CRDS has no picture-frame reference, that part is skipped and each column's stripe is its median.

After the subtraction, the scatter of the empty pixels is compared with the read noise `jwst` predicts for them, and the read-noise variance is scaled to match. The count-rate file keeps the background it subtracted (`CFBKG`), the pixels the fit used (`CFBKGMASK`) and the variance factor (`CFBKGRMS`), so the step can be checked and undone.

### Masking by hand

Some exposures have features no automatic mask catches, for example light scattered onto the detector from a bright source outside the MSA. These are outlined by hand as DS9 polygons, one file per exposure under `reference/nirspec/<observation>/masks/`, drawn in the portal's mask editor or with `cfpipe nirspec mask edit`. Masked pixels are flagged `DO_NOT_USE`: they are kept out of the background fit and out of everything that follows. Changing a mask re-runs the background subtraction from the original count rate.

## From count rates to calibrated 2-D spectra

Stage 2a runs `jwst`'s spectroscopic pipeline (`Spec2Pipeline`) once per count-rate file, for every source in it at once. It assigns the world coordinates and wavelengths, flags the light from shutters known to have failed open, cuts out each shutter's 2-D spectrum, corrects the wavelengths for the source's position across the shutter, flat-fields it and converts it to flux density. Each source's cutout is then written to its own file.

Every source is calibrated as a point source (`set_stellarity = 1.0`), at its planned position in the shutter. Stage 3 keeps `jwst`'s point-source path loss across the shutter, and replaces it along the slit with the [optics model](/docs/reduction/nirspec/slit-losses)'s loss for the source's fitted size and position.

Some `jwst` steps are skipped or moved, because CAMPFIRE does them later or differently:

| `jwst` step | What happens instead |
|---|---|
| Background subtraction (`bkg_subtract`), master background (`master_background_mos`) | [Nod subtraction and CAMPFIRE's master background](/docs/reduction/nirspec/backgrounds) |
| Bar shadow (`barshadow`) | Part of the [optics model](/docs/reduction/nirspec/slit-losses) |
| Resampling to a rectified grid (`resample_spec`) | Still `jwst`, but after the background subtraction: in Stage 2b for each exposure, and in Stage 3 for the combined spectrum |
| 1-D extraction (`extract_1d`) | The [profile extraction](/docs/reduction/nirspec/extraction) |

Stage 2a also does three things around the `jwst` call, described on the [backgrounds](/docs/reduction/nirspec/backgrounds) page:

- before it, it finds shutters that were commanded open but stayed closed, so that `jwst` does not extract a shutter that passed no light;
- it extracts the planned background shutters as well, calibrated for uniform light;
- after it, it measures the background spectrum from every empty shutter and builds the master background.

### Options that change the calibration

Two options replace `jwst` reference files at this stage. Both are off by default.

| Option | What it does |
|---|---|
| `[nirspec.stage2] empirical_wavecorr = true` | Adds the wavelength zero-point correction measured by JADES (Scholtz et al. 2025) to `jwst`'s correction for the source's position across the shutter |
| `[nirspec.stage2] extend_g140m_g235m = true` | Extends G140M/F100LP and G235M/F170LP to 5.3 µm, past the red limit of the CRDS reference files, with flats and wavelength ranges made on the fly and a flux calibration derived once against the SPURS program. The Stage 1 footprints are widened to match, so the extended light is not fitted as background |
