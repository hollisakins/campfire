# Redshifts and lines

Each spectrum receives an automatic redshift from template fitting. Redshifts are then [visually inspected](/docs/inspection), and emission-line fluxes are measured only at the inspected redshift.

## Template redshifts

At each redshift on a grid, the spectrum is fitted with a non-negative linear combination of templates; the redshift of minimum χ² is adopted.

![Two PRISM spectra (black, uncertainties in gray) with the best-fit template combination (orange) and the principal emission lines marked at the fitted redshift. Right: Δχ² as a function of redshift, with the minimum marked. The first redshift is constrained by the continuum shape, the second by emission lines.](/docs/nirspec/redshift_fit.png)

Template set:

| Templates | Description |
|---|---|
| Stellar populations | `bagpipes` models spanning a range of ages, star-formation histories, metallicities and attenuation, regenerated per redshift interval: ages are fractions of the age of the Universe at that redshift, metallicity decreases toward high redshift, and attenuation peaks near z ≈ 3 |
| Emission lines | Lines and line groups from Lyα to Paα; doublets with fixed atomic ratios ([OIII], [NII], [SIII]) and Balmer and Paschen lines at recombination ratios are tied |
| Broad lines | Hα and Hβ with FWHM of 1,500 and 3,000 km/s |
| Blackbodies | 500, 2,500 and 5,000 K, and a modified blackbody |

The fit proceeds in two passes: a coarse grid over 0 < z < 20 (PRISM) or 0 < z < 15 (gratings), with steps of 500 km/s (PRISM), 100 km/s (M gratings) or 30 km/s (H gratings), followed by refinement within ±3,000 km/s of the best coarse solution at 30, 10 or 5 km/s respectively. Templates are convolved to the instrumental resolution, taken as the nominal resolution curve scaled by 1.3 (PRISM) or 1.5 (gratings), and attenuated by the intergalactic medium. Uncertainties below 10 % of the flux are raised to that floor before fitting, to limit the influence of individual high-S/N pixels.

The redshift file (`<spectrum>_zfit.fits`) contains the best-fit redshift (`ZBEST`), the best-fit model, and χ² over the full grid. `ZCONF` is the percentage of the redshift probability distribution within Δz = 0.03 of the best fit; low values indicate competing solutions.

For objects observed with several dispersers, the automatic redshift presented for inspection is anchored by the PRISM and refined by grating redshifts consistent with it. The catalog redshift is the inspected value.

The fit uses `fnu`, which by default includes the flux of overlapping sources (see [Overlapping spectra](/docs/reduction/nirspec/extraction#overlapping-spectra)). A grating spectrum dominated by contamination can therefore return the contaminant's redshift; such cases are identified during inspection.

## Emission lines

Line fluxes are measured after inspection, at the inspected redshift, for objects with redshift quality "probable" or better (`min_quality = 3`):

```bash
campfire pull --obs <observation>           # inspected redshifts, written to reference/nirspec/<observation>/redshifts.toml
cfpipe nirspec linefit --obs <observation>  # line fits at those redshifts
```

1. **Line windows.** Each line in the line list that falls within the spectral range is assigned a window about its expected wavelength; lines with overlapping windows are fitted together.
2. **Model.** Within each window, one Gaussian per line, convolved with the instrumental resolution, on a linear local continuum. Fixed-ratio doublets are tied.
3. **Kinematics.** Each line group is first fitted with its own velocity offset and width. Lines detected at S/N ≥ 5 then define a single velocity and width for the spectrum, with which the remaining lines are refitted. Velocity offsets are bounded to ±1,000 km/s from the inspected redshift (±2,500 km/s for the PRISM) and intrinsic widths to 10–600 km/s; without a strong line, the width is fixed at 100 km/s.
4. **Blends.** Lines separated by less than about twice their width are fitted as a single component, reported under the stronger line and flagged.
5. **Doublet totals.** Doublets with a free ratio ([OII], CIII], [SII] and others) are also reported as a total. Near the resolution limit the components are strongly anti-correlated but their sum is well constrained and comparable across dispersers.
6. **Broad components.** On the gratings, a broad Gaussian is tested on permitted lines (Hα, Hβ, Paα, Paβ, MgII, CIV and others) and retained when it significantly improves the fit and is itself detected; it is reported as a separate entry (e.g. `Halpha_broad`).

![Line fit of a PRISM spectrum at its inspected redshift. Top: the spectrum (black) and the line model (orange); shaded, the three line groups shown below. Bottom: each group's fitted window, with the model (orange), its local continuum (dashed) and the S/N of each detected line; below, the residual in units of the uncertainty. At PRISM resolution Hα includes [NII] and [SII] is fitted as one line. The lines of this compact source are narrower than the assumed instrumental resolution, so the fitted intrinsic widths reach their lower bound and the model falls below the peak of [OIII]λ5007.](/docs/nirspec/line_fit.png)

Each line has a flux (erg s⁻¹ cm⁻²) and rest-frame equivalent width. Fluxes are unconstrained in sign, so non-detections carry a measured flux and uncertainty from which limits can be derived. Uncertainties are derived from the fit covariance. Results are written per spectrum as `<spectrum>_lines.fits`, together with the redshift used, its quality and origin, and a hash of the input spectrum; fits are repeated only when the redshift, its quality, the spectrum or the fitter version changes.

Line fits, like the redshift fits, use `fnu`, which includes contamination by default.

## Adopted redshifts

The adopted redshift is the inspected value and its quality, available from the portal and the catalog (`Campfire.query_objects()` in the Python client). `ZBEST` in the redshift file is the automatic fit of an individual spectrum; the redshift in the line-fit file is the inspected redshift plus the fitted velocity offset.

> Redshifts were measured by fitting each spectrum with a non-negative combination of stellar-population, emission-line and blackbody templates, and were visually inspected; we use inspected redshifts of quality "probable" or better. Emission-line fluxes were measured at the inspected redshift with Gaussian profiles on a local linear continuum, with a common velocity offset and width per spectrum set by the strongest lines; close doublets are reported as totals.
