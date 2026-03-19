# CAMPFIRE Python Client

Python package for querying, downloading, and analyzing NIRSpec spectroscopic data from the CAMPFIRE archive. Includes a **CLI** for bulk data management and a **Python client** for interactive analysis.

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

## CLI: Bulk Download

Track observations and maintain a local mirror with FITS files and CSV catalogs.

```bash
campfire observations           # List available observations
campfire add ember_uds_p4       # Track an observation
campfire sync                   # Download all tracked data
campfire status                 # Check sync status and disk usage
```

After syncing, your data is in `~/.campfire/data/`:
- FITS files organized by observation
- `objects.csv` and `spectra.csv` catalogs for pandas/astropy
- SQLite database (used automatically by the Python client)

## Python Client: Interactive Analysis

The `Campfire` class queries locally synced data when available, falling back to the remote API.

```python
from campfire import Campfire

cf = Campfire()

# Query objects (uses local SQLite if synced, API otherwise)
results = cf.query_objects(
    redshift_range=(3.0, 6.0),
    redshift_quality=[2, 3],
    inspected_only=True
)

# Open a spectrum directly (local FITS if available, downloads if not)
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

# Open from the client (checks local files first)
spec = cf.open_spectrum('ember_uds_p4_123456', 'PRISM')
spec.wavelength   # np.ndarray, microns
spec.flux         # np.ndarray, microjansky
spec.flux_err     # np.ndarray
spec.header       # FITS header as dict

# Or open any FITS file directly
spec = SpectrumData.from_fits('/path/to/file.fits')
```

### Plotting

```python
from campfire import plot_spectrum, plot_redshift_fit

data = cf.get_spectrum_data('ember_uds_p4_123456', 'PRISM')
fig = plot_spectrum(data, redshift=2.5, show_emission_lines=True)
fig.show()

fit = cf.get_redshift_fit_data('ember_uds_p4_123456', 'PRISM')
fig = plot_redshift_fit(fit, spectrum_data=data)
fig.show()
```

### Download Files

```python
# Download a single FITS file (returns local path if already synced)
path = cf.download_spectrum(spectrum['fits_path'])

# Batch download from query results
paths = cf.download_spectra(table=results, download_dir='./spectra/', gratings=['PRISM'])
```

## Architecture

```
campfire sync (CLI)
  └── Downloads FITS → populates SQLite → exports CSVs

Campfire() (Python client)
  └── Queries SQLite when local data available, API otherwise
  └── Opens FITS from disk when synced, downloads on demand
```

Both share the same local data store at `~/.campfire/data/.campfire_meta/campfire.db`.

## Full Documentation

See the [CAMPFIRE docs](https://campfire.hollisakins.com/docs/api) for the complete API reference, CLI command reference, and REST API documentation.
