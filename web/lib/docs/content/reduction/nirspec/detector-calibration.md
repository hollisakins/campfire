# Detector and calibration

This page covers the first stages of the [NIRSpec pipeline](/docs/reduction/nirspec): ramp fitting to count-rate images, and calibration to 2-D spectra for each source and exposure. These stages use the standard `jwst` pipeline and the CRDS flat-field, wavelength and flux calibrations, with one CAMPFIRE addition: subtraction of the detector background before calibration.

## Ramp fitting

`Detector1Pipeline` fits each pixel's ramp and writes one count-rate image per exposure and detector. Two settings differ from the defaults:

- 1/f cleaning (`clean_flicker_noise`) is enabled, with science regions masked;
- the jump step expands the flagged area around large cosmic-ray events (`expand_large_events`).

## Detector background subtraction

NIRSpec count-rate images carry a detector-intrinsic signal in addition to the astronomical background:

- the "picture frame", a fixed pattern of darker edges and faint diagonal features, with an offset that differs between detector bands;
- residual 1/f noise, appearing as column and row stripes, which the `jwst` cleaning reduces but does not remove.

This component is fitted and subtracted before the spectra are extracted and calibrated.

![Stage 1 background subtraction for one PRISM exposure. Left: the background model over the full detector, smoothed to show the large-scale structure; the dark edges and diagonal features are the CRDS picture-frame reference. The outlined region is shown in the next three panels: count rate before subtraction (pixels excluded from the fit tinted orange: spectral traces and faint emission rejected by clipping), the background model, and the count rate after subtraction, on a common scale centered on zero. Bottom: per-column median of the fitted pixels in that region before (gray) and after (blue) subtraction.](/docs/nirspec/detector_background.png)

The model is fitted to unilluminated pixels and subtracted from the full frame. Excluded from the fit are:

- the footprints of all open shutters from the MSA plan, including known failed-open shutters, extended beyond each grating's nominal wavelength range to include light past it (table below);
- the fixed-slit region;
- all pixels with a data-quality flag, including manual masks (see [Manual masks](#manual-masks));
- in each iteration, pixels more than 2σ above the background, to reject faint emission not in the footprint map.

| Grating | Excluded wavelength range (µm) |
|---|---|
| G140M, G140H | 0.70–2.5 |
| G235M, G235H | 1.66–4.0 |
| G395M, G395H | 2.87–5.5 |

The model components, fitted in sequence:

| Component | Model |
|---|---|
| Picture frame | CRDS picture-frame reference, with an independent scale and offset in each of four 512-row bands |
| Column 1/f | Picture-frame shape with a per-column scale and offset |
| Row 1/f | Per-row median (PRISM only) |
| Smooth 2-D background | Disabled by default (`subtract_2d = false`) |

The fit is iterated five times, refitting all components to the residual and updating the clipping mask at each iteration. Where no CRDS picture-frame reference exists, that component is omitted and the column term reduces to a per-column median.

After subtraction, the read-noise variance is rescaled so that it matches the measured scatter of unilluminated pixels. The count-rate file retains the subtracted model (`CFBKG`), the fit mask (`CFBKGMASK`) and the variance scale factor (`CFBKGRMS`), so the step can be inspected or reverted.

### Manual masks

Features not captured by the automatic mask, such as scattered light from bright sources outside the MSA, can be masked with DS9 polygon regions, stored per exposure under `reference/nirspec/<observation>/masks/` and created in the portal's mask editor or with `cfpipe nirspec mask edit`. Masked pixels are flagged `DO_NOT_USE` and excluded from the background fit and all subsequent steps. Editing a mask re-runs the background subtraction from the original count rate.

## Spectroscopic calibration

Stage 2a runs `Spec2Pipeline` once per count-rate file for all sources simultaneously: WCS assignment, flagging of failed-open shutters, 2-D extraction, wavelength correction for the source position across the shutter, flat-fielding, and flux calibration. Each source's cutout is then written to its own file.

All sources are calibrated as point sources (`set_stellarity = 1.0`) at their planned positions. Stage 3 retains the `jwst` point-source path loss across the shutter and replaces it along the slit with the [optics model](/docs/reduction/nirspec/slit-losses) evaluated for the source's fitted size and position.

`jwst` steps skipped or deferred:

| `jwst` step | Replaced by |
|---|---|
| `bkg_subtract`, `master_background_mos` | [Nod subtraction and the CAMPFIRE master background](/docs/reduction/nirspec/backgrounds) |
| `barshadow` | The [optics model](/docs/reduction/nirspec/slit-losses) |
| `resample_spec` | `jwst` resampling after background subtraction: in Stage 2b per exposure, and in Stage 3 for the combined spectrum |
| `extract_1d` | [Forward-model extraction](/docs/reduction/nirspec/extraction) |

Stage 2a also performs three steps around the `jwst` call, described in [Background estimation and subtraction](/docs/reduction/nirspec/backgrounds): identification of stuck-closed shutters before calibration, so that `jwst` does not extract shutters that transmitted no light; extraction of dedicated background slitlets, calibrated as uniform sources; and, after calibration, measurement of the background spectrum and construction of the master background.

### Optional calibration overrides

Two options replace `jwst` reference files at this stage; both are disabled by default.

| Option | Effect |
|---|---|
| `[nirspec.stage2] empirical_wavecorr = true` | Adds the JADES wavelength zero-point correction (Scholtz et al. 2025) to the `jwst` correction for source position across the shutter |
| `[nirspec.stage2] extend_g140m_g235m = true` | Extends G140M/F100LP and G235M/F170LP to 5.3 µm, beyond the red limit of the CRDS references, using flats and wavelength ranges generated on the fly and a flux calibration derived against the SPURS program. The Stage 1 footprints are widened accordingly |
