# CAMPFIRE Python Client

Python package for querying, downloading, and analyzing NIRSpec spectroscopic data from the CAMPFIRE archive. Includes a **CLI** for catalog sync and bulk downloads, and a **Python client** for interactive analysis.

## Installation

```bash
cd python
pip install -e .

# With plotting support (plotly)
pip install -e ".[plotting]"
```

## Authentication

```bash
campfire login              # Browser-based OAuth (recommended)
campfire login --api-key    # Paste an API key (for headless systems)
```

## CLI Workflow

### 1. Sync the catalog

```bash
campfire sync               # Pulls full object/spectra metadata (~seconds)
```

This downloads the complete catalog into a local SQLite database and exports `objects.csv` + `spectra.csv`. No FITS files are downloaded. Safe to run often — refreshes inspection results, redshifts, and flags.

### 2. Download spectra

```bash
campfire download --obs ember_uds_p4              # By observation
campfire download --program EMBER-UDS             # By program
campfire download --field COSMOS --grating PRISM   # By field + grating
campfire download --stale                          # Re-download reprocessed files
campfire download --all                            # Everything accessible
```

### 3. Check status

```bash
campfire status             # Credentials, catalog stats, downloads, disk usage
campfire download           # List available observations and download status
```

### CSV-only workflow

After `campfire sync`, the CSV catalogs are ready for pandas/astropy:

```python
from astropy.table import Table
objects = Table.read('~/campfire/meta/objects.csv')
high_z = objects[objects['redshift'] > 3.0]
```

## Python Client

The `Campfire` class queries the local catalog when available, falling back to the remote API.

```python
from campfire import Campfire

cf = Campfire()

# Sync the catalog (same as CLI)
cf.sync()

# Query objects (one row per sky position; inspection state lives here)
objects = cf.query_objects(
    redshift_range=(3.0, 6.0),
    redshift_quality=[2, 3],
    inspected_only=True,
)

# Query spectra (flat, one row per spectrum; includes dq_flags per spectrum)
spectra = cf.query_spectra(gratings=['PRISM'])

# Filter by tags (object-level)
lrds = cf.query_objects(tags=['lrd', 'blagn'])

# Download FITS files
cf.download(observations=['ember_uds_p4'], gratings=['PRISM'])

# Open a spectrum by spectrum_id (local FITS if downloaded, API fallback otherwise)
spec = cf.open_spectrum('ember_uds_p4_prism_clear_123456')
print(spec.wavelength.shape, spec.flux.shape)

# Iterate over all matching objects (auto-pagination)
for obj in cf.iter_objects(tags=['lrd']):
    print(obj['object_id'], obj['redshift'])
```

### Spectra view

`query_spectra` is the flat per-spectrum query. Each row has
`spectrum_id`, `target_id`, `object_id`, `grating`, `fits_path`,
`dq_flags`, and `redshift_auto`. Inspection filters
(`redshift_range`, `redshift_quality`, `inspected_only`) join through
the parent object.

```python
from campfire.flags import DQFlags

# Clean PRISM spectra with inspected redshifts
good = cf.query_spectra(
    gratings=['PRISM'],
    inspected_only=True,
    dq_flags=~DQFlags.CONTAMINATION & ~DQFlags.LOW_SNR,
)

# Open one
spec = cf.open_spectrum(good[0]['spectrum_id'])
```

### Flag Filtering

```python
from campfire.flags import DQFlags

# Numpy-style operators for per-spectrum DQ bitmask flags
clean = cf.query_spectra(dq_flags=~DQFlags.CONTAMINATION & ~DQFlags.LOW_SNR)
```

### Spectrum Access

```python
from campfire import SpectrumData

spec = cf.open_spectrum('ember_uds_p4_prism_clear_123456')
spec.wavelength   # np.ndarray, microns
spec.flux         # np.ndarray, microjansky
spec.flux_err     # np.ndarray
spec.header       # FITS header as dict

# Or open any FITS file directly
spec = SpectrumData.from_fits('/path/to/file.fits')
```

### Staleness Detection

When spectra are reprocessed on the server, `sync()` detects the change:

```python
result = cf.sync()
if result['stale_count'] > 0:
    print(f"{result['stale_count']} files updated on server")
    cf.download(stale_only=True)
```

### Plotting

```python
from campfire import plot_spectrum, plot_redshift_fit

data = cf.get_spectrum_data('ember_uds_p4_prism_clear_123456')
fig = plot_spectrum(data, redshift=2.5, show_emission_lines=True)
fig.show()
```

### NIRCam Cutouts

Generate publication-quality RGB cutout images with vector shutter overlays. Cutout images and shutter geometry are cached locally after the first fetch.

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(5, 5))
cf.plot_cutout('ember_uds_p4_123456', fov=3.2, ax=ax)
fig.savefig('cutout.pdf')  # vector shutter overlay in PDF
```

Control which shutters are shown:

```python
cf.plot_cutout('obj_id', fov=3.2, shutters='target', ax=ax)  # target only
cf.plot_cutout('obj_id', fov=3.2, shutters=False, ax=ax)     # no shutters
```

Customize shutter style — use `"box"` (default) or `"corners"` (JADES-style L-shaped marks):

```python
cf.plot_cutout('obj_id', fov=3.2, ax=ax, shutter_style={
    "target": {"marker": "corners", "edgecolor": "cyan"},
    "other": {"marker": "corners", "edgecolor": "white", "linewidth": 0.5},
})
```

For full control, fetch data separately and use `plot_cutout` directly:

```python
from campfire.imaging import plot_cutout

path = cf.get_cutout('obj_id', fov=3.2)       # cached PNG
data = cf.get_shutters('obj_id', fov=3.2)     # cached JSON
plot_cutout(path, shutters=data, object_id='obj_id', fov=3.2, ax=ax)
```

## Architecture

```
campfire sync       → pulls full catalog into SQLite + CSVs (no FITS)
campfire download   → downloads FITS files by obs/program/field/grating

Campfire.sync()         → same as campfire sync
Campfire.download()     → same as campfire download
Campfire.query_objects()→ object-level queries (local SQLite or API fallback)
Campfire.query_spectra()→ spectrum-level queries (local SQLite or API fallback)
Campfire.open_spectrum()→ opens local FITS by spectrum_id (or downloads on demand)
Campfire.plot_cutout()  → RGB cutout with vector shutter overlay
Campfire.get_cutout()   → cached PNG cutout
Campfire.get_shutters() → cached shutter geometry JSON
```

## Full Documentation

See the [CAMPFIRE docs](https://campfire.hollisakins.com/docs/api) for the complete reference.
