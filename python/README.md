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
campfire observations       # List all observations with download status
```

### CSV-only workflow

After `campfire sync`, the CSV catalogs are ready for pandas/astropy:

```python
from astropy.table import Table
objects = Table.read('~/.campfire/data/.campfire_meta/objects.csv')
high_z = objects[objects['redshift'] > 3.0]
```

## Python Client

The `Campfire` class queries the local catalog when available, falling back to the remote API.

```python
from campfire import Campfire, ObjectFlags

cf = Campfire()

# Sync the catalog (same as CLI)
cf.sync()

# Query locally — instant, no network
results = cf.query_objects(
    redshift_range=(3.0, 6.0),
    redshift_quality=[2, 3],
    inspected_only=True
)

# Download FITS files
cf.download(observations=['ember_uds_p4'], gratings=['PRISM'])

# Open a spectrum (local FITS if downloaded, API fallback otherwise)
spec = cf.open_spectrum('ember_uds_p4_123456', 'PRISM')
print(spec.wavelength.shape, spec.flux.shape)

# Iterate over all matching objects (auto-pagination)
for obj in cf.iter_objects(object_flags=ObjectFlags.LRD):
    print(obj['object_id'], obj['redshift'])
```

### Flag Filtering

```python
from campfire.flags import ObjectFlags, DQFlags, SpectralFeatures

# Numpy-style operators
results = cf.query_objects(
    object_flags=(ObjectFlags.LRD | ObjectFlags.LYA_EMITTER) & ~ObjectFlags.BROAD_LINE
)

# Or simple string lists
results = cf.query_objects(object_flags=['LRD', 'LYA_EMITTER'])
```

### Spectrum Access

```python
from campfire import SpectrumData

spec = cf.open_spectrum('ember_uds_p4_123456', 'PRISM')
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

data = cf.get_spectrum_data('ember_uds_p4_123456', 'PRISM')
fig = plot_spectrum(data, redshift=2.5, show_emission_lines=True)
fig.show()
```

## Architecture

```
campfire sync       → pulls full catalog into SQLite + CSVs (no FITS)
campfire download   → downloads FITS files by obs/program/field/grating

Campfire.sync()     → same as campfire sync
Campfire.download() → same as campfire download
Campfire.query_objects() → queries local SQLite (or API fallback)
Campfire.open_spectrum() → opens local FITS (or downloads on demand)
```

## Full Documentation

See the [CAMPFIRE docs](https://campfire.hollisakins.com/docs/api) for the complete reference.
