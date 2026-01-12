# FITS File Reference

This page documents the structure and contents of CAMPFIRE FITS files.

## 1D Spectrum Files

*Documentation coming soon!*

### File Naming Convention

```
{object_id}_{grating}_1d.fits
```

### HDU Structure

*Documentation coming soon!*

| HDU | Name | Type | Description |
|-----|------|------|-------------|
| 0 | PRIMARY | Header | Metadata |
| 1 | SPEC1D | BinTable | Spectral data |

### Column Definitions

*Documentation coming soon!*

| Column | Unit | Description |
|--------|------|-------------|
| WAVE | micron | Wavelength |
| FLUX | microJy | Flux density |
| ERR | microJy | Flux uncertainty |
| ... | ... | ... |

## 2D Spectrum Files

*Documentation coming soon!*

### File Naming Convention

```
{object_id}_{grating}_2d.fits
```

### HDU Structure

*Documentation coming soon!*

Lorem ipsum dolor sit amet...

## Header Keywords

*Documentation coming soon!*

### Required Keywords

| Keyword | Description |
|---------|-------------|
| OBJECT | Object identifier |
| RA | Right Ascension |
| DEC | Declination |
| ... | ... |

### Pipeline Keywords

*Documentation coming soon!*

Lorem ipsum dolor sit amet...

## Examples

*Documentation coming soon!*

### Reading a Spectrum with Python

```python
from astropy.io import fits

# Open the file
with fits.open('spectrum_1d.fits') as hdul:
    data = hdul['SPEC1D'].data
    wave = data['WAVE']
    flux = data['FLUX']
    err = data['ERR']
```

### Plotting a Spectrum

```python
import matplotlib.pyplot as plt

plt.figure(figsize=(10, 4))
plt.plot(wave, flux, 'k-', lw=0.5)
plt.fill_between(wave, flux-err, flux+err, alpha=0.3)
plt.xlabel('Wavelength (micron)')
plt.ylabel('Flux (microJy)')
plt.show()
```
