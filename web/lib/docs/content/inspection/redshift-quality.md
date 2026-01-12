# Redshift Quality

The redshift quality rating is the most important field in visual inspection. It indicates your confidence that the redshift measurement is correct, enabling downstream science to appropriately weight or filter the catalog.

## The Five Quality Levels

| Quality | Color | Confidence | When to Use |
|---------|-------|------------|-------------|
| **Not Inspected** | Gray | N/A | Default state - no human has reviewed this object |
| **Impossible** | Red | 0% | Cannot determine a redshift from available data |
| **Tentative** | Orange | ~50% | Uncertain - could be this redshift or something else |
| **Probable** | Yellow | ~80% | Likely correct, but some doubt remains |
| **Secure** | Green | >95% | Highly confident - multiple features confirm |

## Detailed Guidance

### Not Inspected (Gray)

This is the default state for all objects before human review. You should never explicitly select this - instead, always assign one of the other four levels.

### Impossible (Red)

Use this when you **cannot determine any redshift** from the available data. Common reasons:

- **No spectral features** - Featureless continuum or pure noise
- **Signal-to-noise too low** - Cannot distinguish real features from noise
- **Severe contamination** - Another source dominates the extraction
- **Data quality issues** - Chip gaps, stuck shutters, or other problems affecting key wavelengths

> **Note:** "Impossible" means you can't measure a redshift, not that the automated fit is wrong. If you can identify features but they suggest a different redshift, override the value and mark as Tentative or higher.

### Tentative (Orange, ~50% confidence)

Use this when you have **some evidence** for the redshift but significant uncertainty remains:

- **Single emission line** - Could be multiple identifications (e.g., [OII] vs Hα)
- **Weak features** - Features near the noise level
- **Ambiguous break** - Could be Lyman or Balmer break
- **Conflicting information** - Some features support the redshift, others don't fit

![Example of a tentative spectrum](./images/quality-tentative.png)

### Probable (Yellow, ~80% confidence)

Use this when the redshift is **likely correct** but you have some remaining doubt:

- **Clear single line with supporting evidence** - e.g., line shape, continuum color
- **Break with some confirmation** - Lyman break with weak emission
- **Multiple weak features** - Several features align but individually marginal
- **Reasonable but not perfect fit** - Most features work, minor discrepancies

### Secure (Green, >95% confidence)

Use this when you are **highly confident** in the redshift:

- **Multiple emission lines** - Two or more clearly identified lines at consistent redshift
- **Strong break with confirmation** - Clear Lyman/Balmer break plus emission lines
- **Absorption features match** - Stellar absorption lines at consistent redshift
- **Excellent chi-squared fit** - Automated fit clearly correct with high confidence

![Example of a secure spectrum](./images/quality-secure.png)

## Decision Tree

```
Can you identify any spectral features?
├── No → IMPOSSIBLE
└── Yes → Do multiple features confirm the same redshift?
    ├── Yes (>95% confident) → SECURE
    └── No → Is there strong evidence for this specific redshift?
        ├── Yes (~80% confident) → PROBABLE
        └── No (~50% confident) → TENTATIVE
```

## Common Scenarios

### Single Emission Line

A single emission line is one of the most challenging cases:

1. **Consider the line wavelength** - What are the possible identifications?
2. **Check the line profile** - Is it broad (AGN) or narrow?
3. **Look at the continuum** - Does the color support a particular redshift?
4. **Check for asymmetry** - Lyα often shows characteristic asymmetric profile
5. **Look for faint companions** - Are there any other weak features?

If you cannot distinguish between identifications, mark as **Tentative**.

### Break Identification

Distinguishing Lyman break (z > 5) from Balmer break (z ~ 1-2):

- **Lyman break** - Complete flux suppression blueward, often with Lyα emission
- **Balmer break** - Gradual break, often with absorption features

If the break is clear but you're unsure which type, check the photometry for additional constraints.

### Low S/N Spectra

For low signal-to-noise spectra:

- Be conservative - use lower quality ratings
- Check the 2D spectrum for contamination
- Consider whether apparent "features" could be noise spikes
- Look at multiple gratings if available

## Overriding the Automated Redshift

If you identify a different redshift than the automated fit:

1. Enter your redshift value in the override field
2. Select an appropriate quality rating for your new redshift
3. Add a comment explaining your reasoning

The override field accepts values to 4 decimal places (e.g., 2.3456).

## Team Consistency

To maintain catalog quality:

- When uncertain, discuss with colleagues using the comments feature
- Periodically review each other's inspections for calibration
- Document any systematic issues (e.g., pipeline problems) in comments
- Flag objects needing second opinions
