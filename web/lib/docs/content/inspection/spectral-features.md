# Spectral Features

When inspecting a spectrum, tag the features that informed your redshift assessment. This helps document the evidence behind each measurement and enables filtering the catalog by feature type.

## Available Feature Tags

| Feature | Description |
|---------|-------------|
| **Continuum Shape** | The overall spectral shape constrains the redshift |
| **Lyman Break** | Sharp flux discontinuity at rest-frame 1216 Å |
| **Balmer Break** | Gradual flux discontinuity at rest-frame 3646 Å |
| **Absorption Features** | Stellar or interstellar absorption lines |
| **Single Emission Line** | One clearly detected emission line |
| **Multiple Emission Lines** | Two or more emission lines detected |

You can select multiple tags - choose all that apply to your assessment.

## Continuum Shape

The overall shape of the spectral continuum can constrain the redshift even without discrete features:

- **Blue slope** - Young stellar populations, low dust
- **Red slope** - Old stellar populations or dusty
- **Flat continuum** - AGN or specific stellar population mix

Use this tag when the continuum color or shape significantly influenced your redshift determination, even if combined with other features.

## Lyman Break

The Lyman break occurs at rest-frame 1216 Å due to absorption by neutral hydrogen in the intergalactic medium (IGM). It appears as a sharp cutoff in flux blueward of Lyman-alpha.

**Key characteristics:**
- Complete or near-complete flux suppression blueward
- Sharp transition (not gradual like Balmer break)
- Often accompanied by Lyman-alpha emission
- Indicates high redshift (z > 5 for NIRSpec observations)

![Example of Lyman break](./images/feature-lyman-break.png)

**Distinguishing from Balmer break:**
- Lyman break is sharper
- Check for Lyman-alpha emission (often asymmetric)
- Balmer break shows absorption features redward

## Balmer Break

The Balmer break at rest-frame 3646 Å arises from hydrogen absorption in stellar atmospheres. It's prominent in A-type stars and stellar populations with ages ~0.5-2 Gyr.

**Key characteristics:**
- Gradual flux discontinuity (not as sharp as Lyman break)
- Flux is higher at longer wavelengths
- Often accompanied by Balmer absorption lines (Hδ, Hγ, Hβ)
- Associated with "Balmer break galaxies" (BBGs) at z ~ 1-3

**Distinguishing from Lyman break:**
- More gradual transition
- Look for Balmer absorption lines
- Check for [OII] emission at 3727 Å

## Absorption Features

Tag this when stellar or interstellar absorption lines help constrain the redshift:

**Common stellar absorption features:**
- **Balmer series** (Hα, Hβ, Hγ, Hδ) - A-type stars
- **Ca II H&K** (3934, 3969 Å) - Solar-type and cooler stars
- **Mg II** (2796, 2803 Å) - Common in galaxies
- **Na I D** (5890, 5896 Å) - Cool stars, interstellar medium

**Interstellar absorption:**
- **Lyα absorption** - Damped Lyman-alpha systems
- **Metal lines** (CIV, SiIV, etc.) - Outflows, ISM

## Single Emission Line

Use this tag when you detect **exactly one** emission line. Single-line identifications are inherently uncertain because multiple lines can appear at similar observed wavelengths.

**Common identifications for a single line:**

| Observed λ | Possible IDs |
|------------|--------------|
| 1.0-1.3 μm | [OII] 3727 (z~2-2.5), Hβ (z~1-1.5), [OIII] (z~1-1.5) |
| 1.3-1.8 μm | Hα (z~1-1.7), [OIII] (z~1.6-2.6) |
| 1.8-2.5 μm | [OIII] (z~2.6-4), Hα (z~1.7-2.8), Lyα (z~13-20) |

**Tips for single-line identification:**
1. Check line profile - Lyα often asymmetric, Hα often has [NII] wings
2. Check continuum color - supports certain redshift ranges
3. Look for faint companion lines
4. Consider line equivalent width

> **Important:** Single emission lines typically warrant a "Tentative" quality rating unless other evidence strongly supports the identification.

## Multiple Emission Lines

Use this tag when you detect **two or more** emission lines. Multiple lines significantly constrain the redshift and usually warrant "Probable" or "Secure" quality ratings.

**Common emission line pairs:**

| Lines Detected | Typical Redshift |
|----------------|------------------|
| [OII] + [OIII] | z ~ 1-3 |
| [OIII] doublet (4959, 5007 Å) | z ~ 1-4 |
| Hα + [NII] | z ~ 0.5-2.5 |
| Hβ + [OIII] | z ~ 1-3 |
| Lyα + CIV | z > 5 |

**Key line ratios:**
- [OIII] 5007/4959 ≈ 3:1 (fixed by atomic physics)
- [NII] 6583/6548 ≈ 3:1
- [SII] doublet ratio varies (density diagnostic)

![Example of multiple emission lines](./images/feature-multiple-lines.png)

## Emission Line Reference

Common emission lines covered by NIRSpec (rest wavelengths):

| Line | λ_rest (Å) | Notes |
|------|------------|-------|
| Lyα | 1216 | Often asymmetric, IGM absorbed |
| CIV | 1549 | AGN indicator if broad |
| CIII] | 1909 | Semi-forbidden |
| MgII | 2800 | Doublet |
| [OII] | 3727 | Doublet (unresolved in PRISM) |
| Hδ | 4102 | Balmer series |
| Hγ | 4340 | Balmer series |
| Hβ | 4861 | Balmer series |
| [OIII] | 4959, 5007 | Strong doublet, 3:1 ratio |
| Hα | 6563 | Strongest Balmer line |
| [NII] | 6548, 6583 | Flanks Hα |
| [SII] | 6717, 6731 | Density diagnostic |
| Paβ | 12820 | Paschen series |
| Paα | 18751 | Paschen series |

## Tips for Feature Identification

1. **Enable emission line overlay** - Use the "Show emission lines" toggle in the plot controls
2. **Adjust the redshift slider** - See if lines align at different redshifts
3. **Check multiple gratings** - High-res gratings can resolve doublets
4. **Examine the 2D spectrum** - Verify features are real, not artifacts
5. **Compare to templates** - The REDSHIFT tab shows template fits
