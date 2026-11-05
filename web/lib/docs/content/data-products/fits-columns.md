# FITS File Reference

The NIRSpec pipeline writes three files per source and disperser:

| File | What it holds |
|---|---|
| `<obs>_<grating>_<filter>_<source>_spec.fits` | The spectrum: 1-D and 2-D, both background versions, the model, the exposures |
| `<obs>_<grating>_<filter>_<source>_zfit.fits` | The automatic template redshift |
| `<obs>_<grating>_<filter>_<source>_lines.fits` | Emission-line fluxes at the inspected redshift |

Read extensions by name, not by number: the order can change between pipeline versions.

## The spectrum file

### Extensions

| Extension | Contents |
|---|---|
| `SPEC1D` | The 1-D spectrum, nod-subtracted (table below) |
| `SCI`, `ERR` | The combined 2-D spectrum and its error, in MJy per pixel; rows run along the slitlet |
| `WAVELENGTH` | The wavelength of every 2-D pixel, in µm |
| `WHT` | The combination weight of every 2-D pixel |
| `PROF2D` | The source's model on the 2-D grid, per µJy of reported flux (MJy / µJy) |
| `CONTAM2D` | The other sources' predicted light on the 2-D grid (MJy) |
| `PROF1D` | The weights of the optimal and boxcar extractions along the slit |
| `EXPOSURES` | One row per exposure combined (table below) |
| `SPEC1D_MBKG` | The 1-D spectrum with the master background subtracted instead of the other nods; same columns as `SPEC1D` |
| `SCI_MBKG`, `ERR_MBKG` | Its 2-D spectrum and error, on the same grid as `SCI` |
| `PROF1D_MBKG` | Its extraction weights |

The full model of the 2-D spectrum is `PROF2D × (fnu_prof - fnu_contam) + CONTAM2D`.

### `SPEC1D` columns

Every flux column comes in µJy (`fnu_*`) and in erg s⁻¹ cm⁻² Å⁻¹ (`flam_*`), each with an `_err` column.

| Column | What it is |
|---|---|
| `wave` | Wavelength, µm |
| `fnu` | The best spectrum. By default the profile extraction, `fnu_prof`; the header card `EXTRACT` says which |
| `fnu_prof` | The profile extraction: the light of the fitted source within a 0.8″ circle, nothing subtracted |
| `fnu_contam` | Other sources' predicted light in `fnu_prof`. NaN where nothing was predicted or the source could not be separated from a neighbor. Its error is systematic and not part of `fnu_err` |
| `fnu_opt` | The optimal extraction: the light that entered the shutter, corrected as a point source by `jwst` |
| `fnu_3px`, `fnu_4px`, `fnu_5px` | Boxcar extractions over 3, 4 and 5 rows, corrected as a point source by `jwst` |

The source with other sources' light removed is `fnu_prof - fnu_contam`. See [Extraction](/docs/reduction/nirspec/extraction) for what each means and when to use it.

### `EXPOSURES` columns

| Column | What it is |
|---|---|
| `filename` | The calibrated exposure |
| `nod_number`, `nod_type`, `dither_number`, `dither_type` | Where the exposure sits in the nod and dither pattern |
| `exptime` | Exposure time, s |
| `bkg_nods` | Which exposures were subtracted from it |
| `bkg_override`, `bkg_auto` | Whether that choice was set by hand, or by the pipeline because of a neighbor |
| `bkg_why` | Why an exposure was left out, if any was |
| `shutter_state` | The slitlet's open (1) and closed (x) shutters, with the source's shutter marked |
| `stuck_shutter_list` | Shutters treated as closed |
| `source_ra`, `source_dec` | The source's planned position |
| `source_xpos`, `source_ypos` | Its planned position in the shutter, across and along |
| `v3pa` | The telescope's position angle |
| `fixed_slit`, `slit_name` | Whether the source was in a fixed slit, and which |

### Primary header

The primary header keeps the cards `jwst` writes, including the versions and every reference file used (`CAL_VER`, `CRDS_CTX`, `R_PTHLOS`, `R_PHOTOM`, ...) and which `jwst` steps ran (`S_*`). CAMPFIRE adds:

| Card | What it records |
|---|---|
| `CMPFRVER`, `CMPFRTIM` | The CAMPFIRE pipeline version, and when the spectrum was made |
| `EXTRACT`, `EXTRWHY` | Which extraction `fnu` is, and why |
| `CONTSTAT` | Whether other sources' light was predicted (`off`, `none`, `predicted`) |
| `NEXP`, `NEXPBKG` | Exposures combined; fewest exposures subtracted from any one |
| `MBKGPREF`, `MBKGWHY` | Whether the master-background spectrum is likely the better one, and why |
| `PFVER` | The version of the extraction model |
| `PFOPTIC` | The version of the optics model |
| `PFAPER` | The diameter of the circle the reported flux refers to, arcsec |
| `PFSHAPE`, `PFREASON` | Which shape the source took (`own`, `prism`, `default`) and why |
| `PFWIDTH`, `PFCEN` | The fitted Gaussian width (σ) and center at `PFPIVOT`, in units of the shutter's open height (0.46″) |
| `PFSLY`, `PFSLX` | Whose slit loss was used along and across the shutter |
| `PFPLREF` | The `jwst` path-loss reference the model starts from |
| `PFBAROFF` | The bar offset applied |
| `PFCONTAM`, `PFCONSIG` | Other sources' predicted light relative to the spectrum, and relative to its noise |
| `PFNBR` | A neighbor not in the catalog may sit in the slitlet (a flag for review) |

Further `PF*` cards record the details of the fit (the detection statistic, the χ² of each shape tried, the masked fraction of the shutter) for diagnosing a spectrum.

## The redshift file

| Extension | Contents |
|---|---|
| Primary | `ZBEST` (the best redshift), `ZCONF` (the share of the fit's probability within 0.03 of it, %), `CHI2MIN` |
| `MODEL` | The best-fit template combination: `wav` (µm), `fnu` (µJy) |
| `CHI2` | The fit at every trial redshift: `z`, `chi2` |

This is the automatic redshift. The inspected redshift lives in the portal's catalog.

## The line-fit file

| Extension | Contents |
|---|---|
| Primary | The redshift used (`ZUSED`) and its source and quality, the spectrum's global velocity offset and width, the input spectrum's hash (`SPECHASH`) |
| `LINES` | One row per line or doublet total: rest and observed wavelength, flux and error (erg s⁻¹ cm⁻²), continuum, rest-frame equivalent width, signal-to-noise, velocity and width, flags (`BLEND`, `BLENDED`, `RESOLVED`, ...) |
| `MODEL` | The fitted model and continuum on the spectrum's wavelength grid (erg s⁻¹ cm⁻² Å⁻¹) |
| `COMPLEXES` | The groups of lines fitted together, and their χ² |

See [Redshifts and lines](/docs/reduction/nirspec/redshifts) for how the lines are fitted.
