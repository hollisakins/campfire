# NIRSpec Pipeline

The CAMPFIRE NIRSpec pipeline turns raw JWST/NIRSpec exposures taken with the micro-shutter assembly (MSA) into calibrated 1-D spectra and redshifts for every source in every slitlet. It is built on the STScI [JWST calibration pipeline](https://jwst-pipeline.readthedocs.io/) (`jwst`). CAMPFIRE runs `jwst` where it does well and replaces it in three places: the background, the way spectra are extracted, and the correction for light lost at the shutter edges (the path loss, or slit loss).

This page is the map. Each part of the reduction has its own page, with figures that show what the step does to real data:

- [Detector and calibration](/docs/reduction/nirspec/detector-calibration): from raw exposures to calibrated 2-D spectra
- [Background estimation and subtraction](/docs/reduction/nirspec/backgrounds): the master background, stuck and failed-open shutters, nod subtraction
- [Slit losses and the MSA optics](/docs/reduction/nirspec/slit-losses): how much of a source's light the shutters let through, and how CAMPFIRE models it
- [Extraction](/docs/reduction/nirspec/extraction): fitting each source's 2-D spectrum, overlapping spectra, what the reported flux means
- [Redshifts and lines](/docs/reduction/nirspec/redshifts): template redshifts and emission-line fluxes
- [Describing the reduction](/docs/reduction/nirspec/describing): versions to quote, a paragraph to adapt, the choices that are yours

## Describing the reduction in a paper

A paragraph you can adapt. The values in brackets are in each spectrum file's header (see [What each spectrum file records](#what-each-spectrum-file-records)).

> The NIRSpec data were reduced with the CAMPFIRE pipeline (version [`CMPFRVER`]), built on the STScI `jwst` pipeline (version [`CAL_VER`], CRDS context [`CRDS_CTX`]). After ramp fitting, the detector's background and 1/f noise were fitted and removed outside every open shutter's footprint. The background was removed by nod subtraction, leaving out any nod in which a neighboring source fell at the target's position. Sources whose spectra overlap on the detector were fitted together; the light of the neighbors is reported separately and was [not] subtracted. Each source's 2-D spectrum was fitted with a model of the galaxy seen through the MSA's optics, and the reported flux is the source's light within a circle 0.8″ across. Redshifts were fitted with templates and inspected visually.

Two choices in that paragraph are yours, not the pipeline's:

- **The neighbors' light is not subtracted from `fnu`.** It is written as its own column, `fnu_contam`. The source alone is `fnu - fnu_contam`. Subtract it when your science needs the source alone.
- **Each file holds two spectra.** `fnu` comes from nod subtraction. `SPEC1D_MBKG` comes from the same exposures with the master background subtracted instead. The header card `MBKGPREF` says when the second is likely the better one (an extended source, a single exposure, or no clean nod), and `MBKGWHY` says why.

## How the pipeline runs

![The NIRSpec pipeline, one column per stage. Each stage's output feeds the top of the next. Gray boxes are jwst pipeline steps, orange boxes are CAMPFIRE steps, white boxes are files written. A dashed outline marks a step that a configuration option turns on or off, with the option and its default.](/docs/nirspec/flow.png)

Read the diagram one column at a time:

1. **Stage 1** fits the detector ramps with `jwst`, including its 1/f cleaning. CAMPFIRE then removes the rest of the detector's own background and 1/f noise, keeping every spectrum's footprint out of the estimate.
2. **Stage 2a** first maps where every shutter's spectrum falls on the detector (the trace maps; Stage 3 uses them to find overlapping spectra). It measures the background spectrum from the empty shutters of every exposure, finds shutters that were commanded open but stayed closed, and calibrates each source's 2-D spectrum with `jwst`. It ends by building the **master background**: the background spectrum laid out on the detector, shutter by shutter.
3. **Stage 2b** subtracts the background by nodding. Each exposure has subtracted from it the mean of the other nods of the same sequence, in which the source sat in a different shutter. A nod in which a bright neighbor sits at the source's position is left out.
4. **Stage 3** extracts the spectra. It first measures where the MSA's bars sit in this observation. It fits every source together with the sources whose light falls on the same pixels, combines the exposures, and fits each source's 2-D spectrum with a model of the galaxy seen through the MSA. Once every neighbor's brightness is known from that fit, the nod choices of Stage 2b are checked again, and sources whose nods change run again (the orange loop). Stage 3 writes one spectrum file per source and disperser.
5. **Redshifts and lines** fits each spectrum with templates. After visual inspection, it measures the emission lines at the inspected redshift.

## What is different from the standard jwst reduction

| What | The standard `jwst` reduction | CAMPFIRE |
|---|---|---|
| Detector background | 1/f cleaning (optional) | The `jwst` cleaning, then a fit of the detector's background pattern and the remaining 1/f stripes, outside every open shutter's footprint |
| Background | Nod subtraction, or a master background from dedicated background shutters | Nod subtraction, leaving out a nod where a neighbor would be subtracted; a master background measured from every empty shutter, written as a second spectrum |
| Shutters that misbehave | Failed-open shutters from the operations list; stuck-closed shutters not handled | Stuck-closed shutters found from each exposure's own data; failed-open shutters from the operations list, with the light each one passes measured from the data |
| Overlapping spectra | Not modeled | Every source fitted together with its neighbors; their light on each spectrum reported separately, not subtracted |
| Light lost at the shutter edges (path loss) | Point-source or uniform reference | Along the slit, a model of the MSA's optics for a source of any size and position. Along the dispersion (across the shutter), the `jwst` reference with a correction measured from stars |
| Bar shadow | CRDS bar-shadow reference | In the master background, measured from empty shutters; in the spectra, part of the optics model |
| Wavelengths | `jwst` wavelength calibration | The same. A zero-point correction measured by JADES is available and off by default |
| 1-D extraction | Boxcar | A fit of each source's 2-D spectrum; boxcar and optimal extractions kept alongside |

## What CAMPFIRE replaces, and how to switch it back

CAMPFIRE's own calibrations replace parts of two `jwst` reference files, the MSA path loss and the bar shadow. Each spectrum records which were used.

| CAMPFIRE calibration | Replaces | How to use the `jwst` reference instead |
|---|---|---|
| Optics model, along the slit | The path-loss reference's dependence on position along the slit | `[nirspec.stage3] slit_loss_y = "jwst"` |
| Correction along the dispersion (across the shutter) | Part of the path-loss reference's dependence on position across the shutter | `[nirspec.stage3] slit_loss_x = "jwst"` |
| Bar offset per observation | Where the bars sit relative to `jwst`'s slit coordinates | `[nirspec.stage3] bar_offset = "off"` |
| Measured bar-shadow tables | The bar-shadow reference, in the master background | No setting: the measured table is used for every disperser that has one (PRISM, G140M, G235M, G395M); the high-resolution gratings use the CRDS reference |

The calibration files, what they were derived from, and the reference files they were derived against are listed in `pipeline/campfire_pipeline/data/nirspec_calibrations.toml`. The [Slit losses and the MSA optics](/docs/reduction/nirspec/slit-losses) page shows how each was derived.

## What each spectrum file records

Every spectrum file says how it was made, in its primary header:

| Card | What it records |
|---|---|
| `CMPFRVER` | The CAMPFIRE pipeline version |
| `CAL_VER`, `CRDS_CTX` | The `jwst` pipeline version and the CRDS context |
| `PFVER` | The version of the extraction model |
| `PFOPTIC` | The version of the optics model |
| `PFPLREF` | The `jwst` path-loss reference the model starts from |
| `PFSLY`, `PFSLX` | Whose slit loss was used, along the slit and along the dispersion |
| `PFBAROFF` | The bar offset applied |
| `EXTRACT`, `EXTRWHY` | Which extraction the best spectrum (`fnu`) is, and why |
| `MBKGPREF`, `MBKGWHY` | Whether the master-background spectrum is likely the better one, and why |

The [FITS reference](/docs/data-products/fits-columns) lists every column and extension.

## Running it

```bash
cfpipe nirspec run --obs <observation> --all --processes 8
```

`--all` runs the four stages and the redshift fit. Each stage can also run alone (`stage1`, `stage2a`, `stage2b`, `stage3`, `zfit`). A stage skips sources whose products already exist unless `--overwrite` is given. Emission lines are fitted after inspection, with `cfpipe nirspec linefit --obs <observation>` (see [Redshifts and lines](/docs/reduction/nirspec/redshifts)). `cfpipe config` prints the default configuration.
