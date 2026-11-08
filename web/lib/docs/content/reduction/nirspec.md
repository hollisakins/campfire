# NIRSpec Pipeline

The CAMPFIRE NIRSpec pipeline reduces JWST/NIRSpec multi-object spectroscopy taken with the micro-shutter assembly (MSA) to calibrated 1-D spectra and redshifts for every source in every slitlet. It is built on the STScI [JWST calibration pipeline](https://jwst-pipeline.readthedocs.io/) (`jwst`), and extends and updates the default processing with custom recipes for several steps.

For describing the reduction in a publication, [Describing the reduction](/docs/reduction/nirspec/describing) provides a summary paragraph, the version information recorded in each spectrum file, and the processing choices left to the user.

## Pipeline overview

![The NIRSpec pipeline, one column per stage; each stage's outputs feed the next. Gray boxes are jwst steps, orange boxes CAMPFIRE steps, and white boxes files written. Dashed outlines mark steps enabled or disabled by a configuration option, shown with its default.](/docs/nirspec/flow.png)

1. **Stage 1** runs `jwst` ramp fitting, including its 1/f cleaning, then fits and subtracts the residual detector background and 1/f noise outside the footprint of every open shutter.
2. **Stage 2a** maps the detector footprint of every shutter (the trace maps used in Stage 3 to identify overlapping spectra), measures the background spectrum from the empty shutters of each exposure, identifies stuck-closed shutters, and calibrates each source's 2-D spectrum with `jwst`. It ends by building the **master background**: the background spectrum forward-modeled onto the detector, shutter by shutter.
3. **Stage 2b** performs nod subtraction: each exposure minus the mean of the other exposures in its group, excluding any background exposure in which a bright neighbor falls at the target's position.
4. **Stage 3** measures the bar offset of the observation, fits each source jointly with the sources whose traces overlap it, combines the exposures, and fits each source's 2-D spectrum with a model of the galaxy convolved with the MSA optics. With each neighbor's flux now known, the Stage 2b exclusions are re-evaluated and affected sources reprocessed (orange loop). One spectrum file is written per source and disperser.
5. **Redshifts and lines**: template-fit redshifts for every spectrum, and, after visual inspection, emission-line fluxes at the inspected redshift.

## Differences from the standard jwst reduction

| Step | Standard `jwst` reduction | CAMPFIRE |
|---|---|---|
| Detector background | 1/f cleaning (optional) | `jwst` cleaning, followed by a fit of the picture-frame pattern and residual 1/f noise outside every open shutter's footprint |
| Background subtraction | Nod subtraction, or master background from dedicated background shutters | Nod subtraction with exclusion of contaminated background exposures; master background from all empty shutters, provided as a secondary spectrum |
| Shutter failures | Failed-open shutters from the operations list; stuck-closed shutters not handled | Stuck-closed shutters identified from each exposure; failed-open shutters from the operations list, with transmission measured from the data |
| Overlapping spectra | Not modeled | Joint fit of each source with its neighbors; neighbor flux reported separately, not subtracted |
| Path loss | Point-source or uniform-source reference | Along the slit, a model of the MSA optics for a source of any size and position; across the shutter, the `jwst` reference with a correction measured from stars |
| Bar shadow | CRDS reference | Master background: measured from empty shutters. Spectra: part of the optics model |
| Wavelength calibration | `jwst` | `jwst`; the JADES zero-point correction is available and off by default |
| 1-D extraction | Boxcar | Forward-model fit of each source's 2-D spectrum; boxcar and optimal extractions also provided |

## Replaced calibrations

CAMPFIRE replaces parts of two `jwst` reference files, the MSA path loss and the bar shadow. Each spectrum records which were applied.

| CAMPFIRE calibration | Replaces | Reverting to the `jwst` reference |
|---|---|---|
| Optics model, along the slit | Path-loss dependence on position along the slit | `[nirspec.stage3] slit_loss_y = "jwst"` |
| Correction across the shutter | Part of the path-loss dependence on position across the shutter | `[nirspec.stage3] slit_loss_x = "jwst"` |
| Per-observation bar offset | Position of the bars relative to the `jwst` slit frame | `[nirspec.stage3] bar_offset = "off"` |
| Measured bar-shadow tables | Bar-shadow reference, in the master background | No option: the measured table is used wherever one exists (PRISM, G140M, G235M, G395M); the high-resolution gratings use the CRDS reference |

The calibration files, their inputs, and the reference files they were derived against are listed in `pipeline/campfire_pipeline/data/nirspec_calibrations.toml`. Their derivation is described in [Slit losses and the MSA optics](/docs/reduction/nirspec/slit-losses).

## Provenance keywords

The primary header of each spectrum file records:

| Keyword | Content |
|---|---|
| `CMPFRVER` | CAMPFIRE pipeline version |
| `CAL_VER`, `CRDS_CTX` | `jwst` version and CRDS context |
| `EXTRVER` | Extraction model version |
| `OPTICVER` | Optics model version |
| `SLOSSY`, `SLOSSX` | Path-loss treatment along the slit and across the shutter |
| `BAROFF` | Bar offset applied |
| `EXTRACT`, `EXTRWHY` | Extraction adopted for `fnu`, and the reason |
| `MBKGPREF`, `MBKGWHY` | Whether the master-background spectrum is likely preferable, and the reason |

The [FITS reference](/docs/data-products/fits-columns) lists all columns and extensions.

## Running the pipeline

```bash
cfpipe nirspec run --obs <observation> --all --processes 8
```

`--all` runs the four stages and the redshift fit; each stage can also be run alone (`stage1`, `stage2a`, `stage2b`, `stage3`, `zfit`). Existing products are skipped unless `--overwrite` is given. Emission lines are fitted after inspection with `cfpipe nirspec linefit --obs <observation>` (see [Redshifts and lines](/docs/reduction/nirspec/redshifts)). `cfpipe config` prints the default configuration.
