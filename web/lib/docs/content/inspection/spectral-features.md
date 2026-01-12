# Spectral Features

When inspecting a spectrum, it can be helpful to tag the features that informed your redshift assessment. 
This documents the evidence behind each measurement and enables more granular filtering for redshift confidence. 
The automatic redshift fitter implicitly considers these features in its fitting routine, but identifying them explicitly in manual inspection ensures that the redshift fit has been properly validated. 

## Available Feature Tags

| Feature | Description |
|---------|-------------|
| **Continuum Shape** | The overall spectral shape constrains the redshift (e.g., 1.6µm bump) |
| **Lyman Break** | Sharp break at rest-frame 1216 Å |
| **Balmer Break** | Break at rest-frame 3646 Å |
| **Absorption Features** | Stellar absorption lines |
| **Single Emission Line** | One clearly detected emission line |
| **Multiple Emission Lines** | Two or more emission lines detected |

You can select multiple tags: choose all that apply to your assessment.

### Note on Continuum Shape

The overall shape of the spectral continuum can constrain the redshift even without discrete features, e.g. a red continuum slope for a dust-obscured stellar population or the 1.6µm stellar bump in low-z galaxies. 

Use this tag when the continuum color or shape significantly influenced your redshift determination, even if combined with other features.

### Common Single-Line Identifications

Use this tag when you detect **exactly one** emission line. Single-line identifications are inherently uncertain due to line confusion. Bright single emission lines are typically Hα, but not always. 

**Tips for single-line identification:**
1. Check line profile - Lyα often asymmetric, Hα often has [NII] wings
2. Check for faint companion lines/doublets (e.g., [OIII]5007,4959 and Hβ, [NII] and [SII] around Hα)
3. Check photometric redshift information, if available

> **Important:** Single emission lines typically warrant a "Tentative" quality rating unless other evidence strongly supports the identification.

## Tips for Feature Identification

1. **Enable emission line overlay** - Use the "Show emission lines" toggle in the plot controls
2. **Adjust the redshift slider** - See if lines align at different redshifts
3. **Check multiple gratings** - High-res gratings can resolve doublets
4. **Examine the 2D spectrum** - Verify features are real, not artifacts
5. **Compare to templates** - The `REDSHIFT` tab shows template fits and chi-sq as a function of redshift
