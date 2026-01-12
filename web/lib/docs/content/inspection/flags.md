# Flags

In addition to redshift quality and spectral features, you can tag objects with classification flags and data quality flags. These help categorize the catalog and document issues.

## Object Type Flags

These flags classify the physical nature of the object. Select all that apply.

### Little Red Dot

Compact, red sources that are candidate high-redshift AGN. Characteristics:

- Very red in rest-frame optical (steep red continuum)
- Compact/unresolved in imaging
- May show broad emission lines
- Typically at z > 4

### Broad Line

Objects showing broad emission lines indicative of AGN activity:

- FWHM > 1000 km/s
- Common in Hα, Hβ, MgII, CIV
- Indicates Type 1 AGN / quasar
- May be combined with "Little Red Dot" if both apply

### Lyα Emitter (LAE)

Galaxies identified primarily by Lyman-alpha emission:

- Strong Lyα emission line
- Often asymmetric line profile
- Usually at z > 2
- May show Lyman break

### Balmer Break Galaxy (BBG)

Galaxies with prominent Balmer break:

- Clear 4000 Å / Balmer break
- Indicates intermediate-age stellar population
- Often at z ~ 1-3
- May show Balmer absorption lines

### [OIII] Emitter

Galaxies with strong [OIII] emission:

- Prominent [OIII] 4959, 5007 Å doublet
- High equivalent width
- Common at z ~ 1-3
- Often indicates high ionization / low metallicity

### Hα Emitter

Galaxies with strong Hα emission:

- Prominent Hα 6563 Å emission
- Star-forming indicator
- May show [NII] companions
- Common at z ~ 0.5-2.5 in NIRSpec

### Quiescent

Galaxies with little or no ongoing star formation:

- Red continuum
- Absorption-dominated spectrum
- Strong 4000 Å break
- No or weak emission lines

### Dusty

Galaxies with significant dust content:

- Very red continuum
- May show silicate absorption
- Attenuated UV/optical emission
- Can obscure emission lines

### Star

Stellar contaminants in the sample:

- Point source in imaging
- Stellar absorption features
- No redshift (z = 0)
- Mark as "Impossible" for redshift quality

## Data Quality Flags

These flags document issues with the data that may affect the reliability of the spectrum or redshift.

### Chip Gap

The spectrum falls in a detector chip gap:

- Missing wavelength coverage
- May lose key emission lines
- Check other gratings for coverage
- Note which features are affected

![Example of chip gap](./images/flag-chip-gap.png)

### Contamination

Another source contaminates the spectrum:

- Flux from nearby object in extraction
- Check the 2D spectrum for contamination
- May affect continuum level or add spurious features
- Common in crowded fields

### Stuck Closed Shutter

A MSA shutter failed to open:

- Missing or significantly reduced flux
- Affects slit let positions
- Check for systematic patterns
- May require re-observation

### Multiple Sources

The extraction includes flux from multiple objects:

- More than one source in the slit
- Blended spectra
- Redshift may be ambiguous
- Check imaging for source positions

### No Detection

No significant flux detected:

- Object not detected in spectrum
- Very faint or outside slit
- Mark as "Impossible" for redshift
- Check acquisition imaging

### Low S/N

Signal-to-noise ratio is insufficient for reliable analysis:

- Features near noise level
- High uncertainty on measurements
- Consider downgrading redshift quality
- May improve with different binning

### Spectral Overlap

Spectra from adjacent slitlets overlap:

- Common in crowded MSA configurations
- Can cause confusion in feature identification
- Check 2D spectrum for overlap
- May affect wavelength calibration

### PRISM Corrupted

Data quality issues specific to PRISM spectrum:

- Bad pixels, artifacts, or reduction issues
- May not affect grating spectra
- Document the nature of the problem
- Check if other gratings are usable

### Grating Corrupted

Data quality issues in one or more grating spectra:

- Bad pixels, artifacts, or reduction issues
- May not affect PRISM
- Specify which grating in comments
- Check other gratings for alternative

## Best Practices

1. **Be thorough** - Flag all relevant issues, not just the most obvious
2. **Check the 2D spectrum** - Many issues are clearer in 2D
3. **Use comments** - Explain unusual or ambiguous situations
4. **Consider impact** - Does this flag affect redshift reliability?
5. **Update as needed** - Return to add flags if you notice issues later

## Combining Quality and Flags

Data quality flags should influence your redshift quality rating:

| Situation | Typical Impact |
|-----------|----------------|
| Minor chip gap | May not affect quality if key lines visible |
| Chip gap on key feature | Downgrade quality or mark Impossible |
| Light contamination | Consider quality downgrade |
| Severe contamination | Mark Impossible |
| Low S/N | Be conservative with quality |
| No detection | Mark Impossible |

Remember: flags document what you observed; quality reflects your confidence in the redshift.
