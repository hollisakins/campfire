# Describing the reduction

This page collects material that may be useful in describing a CAMPFIRE NIRSpec reduction in a publication: the version information recorded in each spectrum file, a summary paragraph, and the processing choices left to the user.

## Version information

The primary header of each spectrum file records:

| Keyword | Content |
|---|---|
| `CMPFRVER` | CAMPFIRE pipeline version |
| `CAL_VER` | `jwst` pipeline version |
| `CRDS_CTX` | CRDS context, which determines every `jwst` reference file |
| `PFOPTIC` | MSA optics model version |
| `PFVER` | Extraction model version |

All keywords are listed in the [FITS reference](/docs/data-products/fits-columns).

## Summary paragraph

> The NIRSpec data were reduced with the CAMPFIRE pipeline (version `CMPFRVER`), built on the STScI `jwst` pipeline (version `CAL_VER`, CRDS context `CRDS_CTX`). After ramp fitting, the detector background and residual 1/f noise were fitted and subtracted outside the footprints of all open shutters. Stuck-closed shutters were identified from each exposure; failed-open shutters were taken from the MSA operations list, with their transmission measured from the data. The background was removed by nod subtraction, excluding background exposures in which a cataloged neighbor fell at the target position. Sources with overlapping traces were fitted jointly. Each source's 2-D spectrum was fitted simultaneously in all exposures with a Gaussian source model convolved with a diffraction model of the MSA (telescope pupil, open shutters, spectrograph pupil stop and detector response, calibrated on stars); the reported flux is that of the fitted intrinsic profile within a 0.8″-diameter aperture. Redshifts were measured by template fitting and visually inspected; emission-line fluxes were measured at the inspected redshift.

More detailed summaries of individual steps are given in [Slit losses](/docs/reduction/nirspec/slit-losses#summary-for-publications) and [Extraction](/docs/reduction/nirspec/extraction#summary-for-publications).

## Processing choices

| Choice | Default | Notes |
|---|---|---|
| Contamination | Reported in `fnu_contam`, not subtracted from `fnu` | The decontaminated spectrum is `fnu_prof - fnu_contam`. Catalog line fluxes are measured on `fnu` |
| Background | Nod subtraction (`fnu`) | The master-background spectrum (`SPEC1D_MBKG`) is in the same file; `MBKGPREF` flags cases where it is likely preferable |
| Extraction | Forward-model extraction (`fnu`) | `fnu_opt` and the boxcar extractions measure the flux entering the shutter, with a point-source path-loss correction |
| Redshift | Inspected redshift, from the portal or `Campfire.query_objects()` | Line fluxes are measured for [redshift quality](/docs/inspection/redshift-quality) "probable" and above |

## Uncertainties

`fnu_err` is statistical, including the noise of the subtracted background exposures. It excludes the systematic uncertainty of the contamination prediction (`fnu_contam_err`) and, for sources with the default profile (`PFSHAPE = default`), the uncertainty of the assumed size, roughly ±15 % in flux.

## Departures from the CRDS calibration

CAMPFIRE replaces parts of two CRDS references:

- **Path loss along the slit**: replaced by the optics model evaluated for each source's fitted size and position ([Slit losses](/docs/reduction/nirspec/slit-losses)). Across the shutter, the CRDS point-source path loss is used with an empirical correction derived from stars.
- **Bar shadow**: replaced by tables measured from empty shutters in the construction of the master background ([Bar-shadow calibration](/docs/reduction/nirspec/backgrounds#bar-shadow-calibration)); the high-resolution gratings retain the CRDS reference. In both extracted spectra the bar shadow is part of the optics model; science frames are never divided by a bar-shadow correction.
- **Bar position**: an offset measured per observation from the empty shutters and applied in the optics model (`PFBAROFF`; disabled with `bar_offset = "off"`).

All other calibrations (flat fields, wavelength solution, flux calibration) use the CRDS references as delivered, unless one of the overrides in [Detector and calibration](/docs/reduction/nirspec/detector-calibration#optional-calibration-overrides) was enabled; the reference-file keywords (`R_WAVCOR`, `R_FFLAT`, `R_PHOTOM`) record which.

## References

- CAMPFIRE: no pipeline paper exists yet. Data releases can be cited as given on the page of the release named in the file's `CMPFRDR` keyword.
- `jwst` calibration pipeline: Bushouse et al., JWST Calibration Pipeline (Zenodo), at the version in `CAL_VER`.
- JADES wavelength correction, if named in `R_WAVCOR`: Scholtz et al. (2025), JADES Data Release 4 (arXiv:2510.01034).
- Redshift templates: `bagpipes` (Carnall et al. 2018).
