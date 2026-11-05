# Describing the reduction

This page collects what you need to describe a CAMPFIRE NIRSpec reduction in a paper: the versions to quote, a paragraph to adapt, and the choices that are yours to state.

## What to quote

Every spectrum file records how it was made in its primary header. Quote these for the spectra you use:

| Card | What it is |
|---|---|
| `CMPFRVER` | The CAMPFIRE pipeline version |
| `CAL_VER` | The `jwst` pipeline version |
| `CRDS_CTX` | The CRDS context, which fixes every `jwst` reference file |
| `PFOPTIC` | The version of the MSA optics model |
| `PFVER` | The version of the extraction model |

If your spectra span several versions, say so; the [FITS reference](/docs/data-products/fits-columns) lists every card.

## A paragraph to adapt

> The NIRSpec data were reduced with the CAMPFIRE pipeline (version `CMPFRVER`), built on the STScI `jwst` pipeline (version `CAL_VER`, CRDS context `CRDS_CTX`). After ramp fitting, the detector's background and 1/f noise were fitted and removed outside every open shutter's footprint. Shutters that stayed closed were identified from each exposure's own data; shutters stuck open were taken from the operations list, with the light each passes measured from the data. The background was removed by nod subtraction, leaving out any nod in which a cataloged neighbor fell at the target's position. Sources whose spectra overlap on the detector were fitted together. Each source's 2-D spectrum, in every exposure at once, was fitted with a Gaussian galaxy seen through a diffraction model of the MSA (the telescope pupil, the open shutters, the spectrograph pupil stop and the detector blur, calibrated on stars), and the reported flux is the light of the fitted galaxy within a 0.8″ circle. Redshifts were measured with templates and inspected visually; emission-line fluxes were measured at the inspected redshift.

Each page of the reduction has its own sentence for the step it covers: [slit losses](/docs/reduction/nirspec/slit-losses#describing-it-in-a-paper), [extraction](/docs/reduction/nirspec/extraction#describing-it-in-a-paper).

## Choices that are yours

The pipeline leaves these to you. State which you made.

| Choice | Default | What to say |
|---|---|---|
| Other sources' light | Reported in `fnu_contam`, not subtracted from `fnu` | Whether you used `fnu` or `fnu_prof - fnu_contam`. The catalog's line fluxes are measured on `fnu` as it is |
| Background | Nod subtraction (`fnu`) | Whether you used the master-background spectrum (`SPEC1D_MBKG`) for any source, for example where `MBKGPREF` is true |
| Extraction | The profile extraction (`fnu`) | If you used `fnu_opt` or a boxcar instead: these measure the light that entered the shutter, corrected as a point source |
| Redshift | The inspected redshift, from the portal or `Campfire.query_objects()` | The [redshift quality](/docs/inspection/redshift-quality) you required (line fluxes are measured from "probable" up) |

## Errors

`fnu_err` is statistical: it carries the noise of the exposure and of the exposures subtracted from it. It does not include the uncertainty of other sources' predicted light (`fnu_contam_err`, systematic), nor, for a source too faint to measure its own size (`PFSHAPE = default`), the uncertainty of the assumed size, which moves its flux by about 15 % either way.

## Where CAMPFIRE departs from the CRDS calibration

CAMPFIRE replaces parts of two CRDS references. A paper should say so:

- **Path loss along the slit**: replaced by the optics model for the source's fitted size and position ([Slit losses](/docs/reduction/nirspec/slit-losses)). Across the shutter, the CRDS point-source path loss is used, with a correction measured from stars.
- **Bar shadow**: replaced by tables measured from empty shutters, in building the master background ([Backgrounds](/docs/reduction/nirspec/backgrounds#the-bar-shadow)); the high-resolution gratings keep the CRDS reference there. The spectra themselves, both versions, take the bar shadow from the optics model; science frames are never divided by one.
- **Where the bars sit**: measured once per observation from the empty shutters and applied in the optics model (`PFBAROFF`; `bar_offset = "off"` turns it off).

Everything else (flat fields, wavelength solution, flux calibration) is the CRDS reference as delivered, unless the observation turned on one of the options on the [Detector and calibration](/docs/reduction/nirspec/detector-calibration#options-that-change-the-calibration) page. The reference-file cards of the spectrum file (`R_WAVCOR`, `R_FFLAT`, `R_PHOTOM`) record which.

## What to cite

- CAMPFIRE: there is no pipeline paper yet. For data from a release, cite the release named in the file's `CMPFRDR` card, as given on its release page.
- The `jwst` calibration pipeline: Bushouse et al., the JWST Calibration Pipeline (Zenodo), at the version in `CAL_VER`.
- If `R_WAVCOR` names the JADES correction: Scholtz et al. (2025), JADES Data Release 4 (arXiv:2510.01034).
- The redshift templates are built with `bagpipes` (Carnall et al. 2018).
