# FITS File Reference

The NIRSpec pipeline writes three files per source and disperser:

| File | Content |
|---|---|
| `<obs>_<grating>_<filter>_<source>_spec.fits` | 1-D and 2-D spectra for both background treatments, the source and contamination models, and the exposure list |
| `<obs>_<grating>_<filter>_<source>_zfit.fits` | Automatic template redshift |
| `<obs>_<grating>_<filter>_<source>_lines.fits` | Emission-line fluxes at the inspected redshift, written after inspection by `cfpipe nirspec linefit` |

Extensions should be accessed by name; their order may change between pipeline versions. Other files in the product directory (`_s2d.fits`, `_x1d.fits`, and `.pdf` plots) are intermediate products and diagnostic plots.

## Spectrum file

### Extensions

| Extension | Content |
|---|---|
| `SPEC1D` | Nod-subtracted 1-D spectrum (columns below) |
| `SCI`, `ERR` | Combined 2-D spectrum and uncertainty, MJy per pixel; rows along the slitlet |
| `WAVELENGTH` | Wavelength of each 2-D pixel, µm |
| `WHT` | Combination weight of each 2-D pixel |
| `PROF2D` | Source model on the 2-D grid, per µJy of reported flux (MJy / µJy) |
| `CONTAM2D` | Predicted contamination on the 2-D grid (MJy) |
| `PROF1D` | Spatial weights of the optimal and boxcar extractions |
| `EXPOSURES` | One row per combined exposure (columns below) |
| `SPEC1D_MBKG` | Master-background-subtracted 1-D spectrum; same columns as `SPEC1D` |
| `SCI_MBKG`, `ERR_MBKG` | Master-background-subtracted 2-D spectrum and uncertainty, on the `SCI` grid |
| `PROF1D_MBKG` | Extraction weights for the master-background spectrum |

The full 2-D model is `PROF2D × (fnu_prof - fnu_contam) + CONTAM2D`.

### `SPEC1D` columns

Each flux column is given in µJy (`fnu_*`) and in erg s⁻¹ cm⁻² Å⁻¹ (`flam_*`), with a corresponding `_err` column.

| Column | Content |
|---|---|
| `wave` | Wavelength, µm |
| `fnu` | Adopted spectrum; by default `fnu_prof` (see `EXTRACT`) |
| `fnu_prof` | Forward-model extraction: flux of the fitted source within a 0.8″ aperture, without contamination subtraction |
| `fnu_contam` | Predicted contamination in `fnu_prof`; NaN where none was predicted or where the source could not be separated from a neighbor. Its uncertainty is systematic and not included in `fnu_err` |
| `fnu_opt` | Optimal extraction: flux entering the shutter, with the `jwst` point-source path-loss correction |
| `fnu_3px`, `fnu_4px`, `fnu_5px` | Boxcar extractions over 3, 4 and 5 rows, with the `jwst` point-source path-loss correction |

The decontaminated spectrum is `fnu_prof - fnu_contam`. See [Extraction](/docs/reduction/nirspec/extraction) for details.

### `EXPOSURES` columns

| Column | Content |
|---|---|
| `filename` | Calibrated exposure |
| `nod_number` | Number of nod positions in the pattern (identical in every row) |
| `dither_number`, `dither_type`, `nod_type` | Position of the exposure in the pattern, and the pattern names |
| `exptime` | Exposure time, s |
| `bkg_nods` | Background exposures subtracted (`excluded` if none) |
| `bkg_override` | Whether the background selection was overridden, manually or automatically |
| `bkg_auto` | Whether the override was automatic (cataloged neighbor or neighbor search) |
| `bkg_why` | Reason for the override: `hand`, `scan`, or the contaminating neighbor; or a flag when all background exposures were contaminated and retained |
| `shutter_state` | Slitlet shutter states, one character per shutter: `1` open, `0` closed, `x` the source's shutter |
| `stuck_shutter_list` | Shutters treated as closed |
| `source_ra`, `source_dec` | Planned source position |
| `source_xpos`, `source_ypos` | Planned position within the shutter, across and along the slit |
| `v3pa` | Telescope position angle |
| `fixed_slit`, `slit_name` | Whether the source was observed through a fixed slit, and which |

### Primary header

The primary header retains the `jwst` keywords, including software versions, all reference files used (`CAL_VER`, `CRDS_CTX`, `R_PTHLOS`, `R_PHOTOM`, ...) and step completion flags (`S_*`). CAMPFIRE adds:

| Keyword | Content |
|---|---|
| `CMPFRVER`, `CMPFRTIM` | CAMPFIRE pipeline version and processing time |
| `EXTRACT`, `EXTRWHY` | Extraction adopted for `fnu` (`profile`, `profile_decontam`, `boxcar3`, `optimal`) and the reason |
| `CMPFROPT` | Whether `fnu_opt` is the optimal extraction (`optimal`) or a boxcar fallback |
| `CONTSTAT` | Contamination status (`off`, `none`, `predicted`) |
| `NEXP`, `NEXPBKG` | Number of exposures combined; minimum number of background exposures subtracted from any one |
| `MBKGPREF`, `MBKGWHY` | Whether the master-background spectrum is likely preferable, and the reason |
| `PFVER` | Extraction model version |
| `PFOPTIC` | Optics model version |
| `PFAPER` | Diameter of the reporting aperture, arcsec |
| `PFSHAPE`, `PFREASON` | Profile adopted (`own`, `prism`, `default`) and the reason |
| `PFWIDTH`, `PFCEN` | Fitted Gaussian σ and center at `PFPIVOT`, in units of the open shutter height (0.46″) |
| `PFSLY`, `PFSLX` | Slit-loss treatment along the slit (`optics` / `jwst`) and across the shutter (`corrected` / `jwst`) |
| `PFPLREF` | `jwst` path-loss reference used as the baseline |
| `PFBAROFF` | Bar offset applied, in the units of `PFWIDTH` |
| `PFCONTAM`, `PFCONSIG` | Predicted contamination relative to the spectrum, and relative to its noise |
| `PFNBR` | Possible uncataloged neighbor in the slitlet (flagged for review) |

The `SPEC1D_MBKG` extension header carries the corresponding keywords for the master-background fit; those in the primary header refer to the nod-subtracted spectrum. Additional `PF*` keywords record fit diagnostics (detection statistic, χ² of each candidate profile, masked shutter fraction).

## Redshift file

| Extension | Content |
|---|---|
| Primary | `ZBEST` (best-fit redshift), `ZBEST_C` (best fit on the coarse grid), `ZCONF` (percentage of the redshift probability within Δz = 0.03), `CHI2MIN`. `ZQUAL` is always 0; quality is assigned at inspection |
| `MODEL` | Best-fit template combination: `wav` (µm), `fnu` (µJy) |
| `CHI2` | χ² over the redshift grid: `z`, `chi2` |

This is the automatic redshift; the inspected redshift is stored in the portal catalog.

## Line-fit file

| Extension | Content |
|---|---|
| Primary | Redshift used (`ZUSED`), its origin and quality, the spectrum's global velocity offset and width, and the input spectrum hash (`SPECHASH`) |
| `LINES` | One row per line, accepted broad component (e.g. `Halpha_broad`) and doublet total (`component`): rest and observed wavelength, flux and uncertainty (erg s⁻¹ cm⁻²), continuum, rest-frame equivalent width, S/N, velocity offset and width, and a flag bitmask (blend, blended, resolved, edge, masked, ...) |
| `MODEL` | Fitted line model and continuum on the spectral grid (erg s⁻¹ cm⁻² Å⁻¹) |
| `COMPLEXES` | Line groups fitted together, with their χ² |

The line fitting is described in [Redshifts and lines](/docs/reduction/nirspec/redshifts).
