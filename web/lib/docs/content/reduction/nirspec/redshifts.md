# Redshifts and lines

Every spectrum gets an automatic redshift from a template fit. Those redshifts are then [inspected by eye](/docs/inspection), and emission lines are measured only at the inspected redshift, so the catalog never carries line fluxes measured at a wrong redshift.

## Template redshifts

At every redshift on a grid, the spectrum is fitted as a sum of templates, each with a coefficient that cannot be negative. The redshift where the fit is best is the pipeline's redshift.

![Two PRISM spectra (black, with their errors in gray) and the best-fit template combination (orange), with the main emission lines marked at the fitted redshift. Right: how much worse the fit is at every other redshift. The first source's redshift is fixed by the shape of its continuum, the second's by its emission lines.](/docs/nirspec/redshift_fit.png)

The templates:

| Kind | What they describe |
|---|---|
| 16 stellar populations | Galaxies of a range of ages, star-formation histories, metallicities and dust (made with `bagpipes`). Metallicity falls and dust changes with redshift, following the average galaxy at each epoch |
| Emission lines | 20 lines and line groups from Lyα to Paα. Lines whose ratios are fixed by physics are tied together (for example the [OIII] doublet, or Hβ with Hα) |
| Broad lines | Hα and Hβ at widths of 1,500 and 3,000 km/s, for broad-line active galaxies |
| Blackbodies | 500, 2,500 and 5,000 K, and a modified blackbody, for featureless or dust-dominated sources |

The fit runs in two passes: a coarse grid over the whole redshift range (steps of 500 km/s for the PRISM, 100 km/s for the medium-resolution gratings), then a fine grid within 3,000 km/s of the best coarse redshift (30 km/s for the PRISM, 10 km/s for the M gratings, 5 km/s for the H gratings). The templates are blurred to each disperser's resolution, and absorption by the intergalactic medium is applied at each redshift.

The redshift file (`<spectrum>_zfit.fits`) holds the best redshift (`ZBEST`), the best-fit model and the fit quality at every redshift. A confidence (`ZCONF`) measures how much of the fit's probability lies within 0.03 of the best redshift: low when two redshifts fit about equally well. The automatic redshift is a starting point; every redshift is inspected before it is used.

The fit uses `fnu`, which by default includes the light of other sources whose spectra overlap this one (see [Overlapping spectra](/docs/reduction/nirspec/extraction#overlapping-spectra)). On the gratings, a spectrum dominated by a neighbor's light can give the neighbor's redshift; the inspection is where this is caught.

## Emission lines

Line fluxes are measured after inspection, at the inspected redshift, for spectra whose redshift quality is "probable" or better.

```bash
campfire pull --obs <observation>           # the inspected redshifts, into reference/nirspec/<observation>/redshifts.toml
cfpipe nirspec linefit --obs <observation>  # fit the lines at those redshifts
```

How the lines are fitted:

1. **Where to look.** Every line in the line list that falls in the spectrum's wavelength range gets a window around its expected position. Lines whose windows overlap are fitted together.
2. **The model.** In each window: a Gaussian per line, blurred by the disperser's resolution, on a low-order polynomial continuum. Doublets with a fixed ratio are tied.
3. **Line widths and velocities.** First every group of lines is fitted with its own velocity and width. The strongly detected lines then set one velocity and width for the whole spectrum, and the faint lines are refitted with them held fixed.
4. **Lines that cannot be separated.** Lines closer than the resolution are fitted as one, reported under the stronger line and flagged.
5. **Doublets with a free ratio** ([OII], CIII], [SII] and others) are also reported as a total. Near the resolution limit the two components trade flux with each other, but their sum is well measured, and it means the same thing in every disperser; use it for selections.

Fluxes may be negative: a line that is not detected carries an honest flux and error, from which a limit can be formed. The errors come from the fit itself. Results are written per spectrum as `<spectrum>_lines.fits`, with the redshift used, its quality and source, and a hash of the spectrum, so a line fit is redone only when the redshift, its quality or the spectrum changes.

Like the redshift fit, the line fit uses `fnu`, with other sources' light included by default.
