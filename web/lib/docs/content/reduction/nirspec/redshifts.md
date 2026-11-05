# Redshifts and lines

Every spectrum gets an automatic redshift from a template fit. Those redshifts are then [inspected by eye](/docs/inspection), and emission lines are measured only at the inspected redshift, so no line flux is measured at a redshift nobody has checked.

## Template redshifts

At every redshift on a grid, the spectrum is fitted as a sum of templates, each with a coefficient that cannot be negative. The redshift where the fit is best is the pipeline's redshift.

![Two PRISM spectra (black, with their errors in gray) and the best-fit template combination (orange), with the main emission lines marked at the fitted redshift. Right: how much worse the fit is at every other redshift; the dot marks the best one. The first source's redshift is set by the shape of its continuum, the second's by its emission lines.](/docs/nirspec/redshift_fit.png)

The templates:

| Kind | What they describe |
|---|---|
| 16 stellar populations | Galaxies of a range of ages, star-formation histories, metallicities and dust (made with `bagpipes`). They are rebuilt for each range of redshift: ages are fractions of the age of the Universe at that redshift, metallicity falls toward high redshift, and dust is strongest near z ≈ 3 |
| Emission lines | 21 lines and line groups from Lyα to Paα. Doublets with a fixed atomic ratio ([OIII], [NII], [SIII]) and the Balmer and Paschen lines at their recombination ratios are tied together |
| Broad lines | Hα and Hβ with a FWHM of 1,500 and 3,000 km/s, for broad-line active galaxies |
| Blackbodies | 500, 2,500 and 5,000 K, and a modified blackbody, for featureless or dust-dominated sources |

The fit runs in two passes. The first covers 0 < z < 20 (PRISM) or 0 < z < 15 (gratings) in steps of 500 km/s for the PRISM, 100 km/s for the M gratings and 30 km/s for the H gratings. The second refines within 3,000 km/s of the best redshift, in steps of 30 km/s (PRISM), 10 km/s (M gratings) or 5 km/s (H gratings). The templates are blurred to each disperser's resolution, taken as the nominal resolution curve times 1.3 for the PRISM and 1.5 for the gratings, and absorbed by the intergalactic medium at each redshift. Before the fit, errors smaller than 10 % of the flux are raised to 10 %, so that a few bright pixels cannot decide the redshift on their own.

The redshift file (`<spectrum>_zfit.fits`) holds the best redshift (`ZBEST`), the best-fit model and the fit at every redshift. A confidence (`ZCONF`, a percentage) measures how much of the fit's probability lies within 0.03 of the best redshift: low when two redshifts fit about equally well.

When an object has spectra in several dispersers, the automatic redshift the inspector starts from combines them: the PRISM sets it, and a grating's redshift close to it refines it. The automatic redshift is only a starting point: every redshift is inspected before it is used, and the catalog's redshift is the inspected one.

The fit uses `fnu`, which by default includes the light of other sources whose spectra overlap this one (see [Overlapping spectra](/docs/reduction/nirspec/extraction#overlapping-spectra)). On the gratings, a spectrum dominated by a neighbor's light can give the neighbor's redshift; the inspection is where this is caught.

## Emission lines

Line fluxes are measured after inspection, at the inspected redshift, for the spectra of objects whose redshift quality is "probable" or better (`min_quality = 3`).

```bash
campfire pull --obs <observation>           # the inspected redshifts, into reference/nirspec/<observation>/redshifts.toml
cfpipe nirspec linefit --obs <observation>  # fit the lines at those redshifts
```

How the lines are fitted:

1. **Where to look.** Each of the 43 lines in the line list that falls in the spectrum's wavelength range gets a window around its expected position. Lines whose windows overlap are fitted together.
2. **The model.** In each window: a Gaussian per line, blurred by the disperser's resolution, on a straight-line local continuum. Doublets with a fixed ratio are tied.
3. **Line widths and velocities.** First every group of lines is fitted with its own velocity and width. The lines detected at a signal-to-noise of 5 or more then set one velocity and width for the whole spectrum, and the other lines are refitted with them held fixed. The velocity may shift by at most 1,000 km/s from the inspected redshift (2,500 km/s for the PRISM), and the width ranges from 10 to 600 km/s. With no strong line, the width is held at 100 km/s.
4. **Lines that cannot be separated.** Lines closer than about twice their width are fitted as one, reported under the stronger line and flagged.
5. **Doublets with a free ratio** ([OII], CIII], [SII] and others) are also reported as a total. Near the resolution limit the two components trade flux with each other, but their sum is well measured, and it means the same thing in every disperser; use it for selections.
6. **Broad lines.** On the gratings, a second, broad Gaussian is tried on the permitted lines (Hα, Hβ, Paα, Paβ, MgII, CIV and others) and kept when it improves the fit clearly and is itself detected. It is reported as its own row (for example `Halpha_broad`).

Each line has a flux (erg s⁻¹ cm⁻²) and a rest-frame equivalent width. Fluxes may be negative: a line that is not detected carries an honest flux and error, from which a limit can be formed. The errors come from the fit itself. Results are written per spectrum as `<spectrum>_lines.fits`, with the redshift used, its quality and source, and a hash of the spectrum, so a line fit is redone only when the redshift, its quality, the spectrum or the fitter changes.

Like the redshift fit, the line fit uses `fnu`, with other sources' light included by default.

## Which redshift to cite

Cite the inspected redshift and its quality, from the portal or the catalog (`Campfire.query_objects()` in the Python client). `ZBEST` in the redshift file is the automatic fit of one spectrum; the redshift in the line-fit file is the inspected one plus the velocity the line fit measured.

> Redshifts were measured by fitting each spectrum with a non-negative combination of stellar-population, emission-line and blackbody templates, and every redshift was inspected visually; we use the inspected redshifts of quality probable or better. Emission-line fluxes were measured at the inspected redshift with Gaussian fits on a local linear continuum, with one velocity and width per spectrum set by the strongly detected lines; close doublets are reported as totals.
