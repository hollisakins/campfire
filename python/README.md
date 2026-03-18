# CAMPFIRE Python API

Python client for querying and downloading NIRSpec spectroscopic data from the CAMPFIRE archive.
*Under construction!*

## Installation

```bash
cd python
pip install -e .

# With plotting support
pip install -e ".[plotting]"

# With all optional dependencies
pip install -e ".[all]"
```

## Quick Start

```python
from campfire import Campfire

# Initialize with API key
cf = Campfire(api_key='sk_live_...')
# Or set CAMPFIRE_API_KEY environment variable

# Query objects
results = cf.query_objects(
    programs=['EMBER-UDS'],
    redshift_range=(2.0, 4.0),
    redshift_quality=[2, 3],
    inspected_only=True
)

print(f"Found {len(results)} objects")
print(results['object_id', 'ra', 'dec', 'redshift'])

# Download spectra
paths = cf.download_spectra(
    table=results,
    download_dir='./spectra/',
    gratings=['PRISM']
)
```

## Features

- **Query objects** with flexible filters:
  - Programs, fields, gratings, observations
  - Redshift ranges and quality
  - SNR ranges
  - Spectral features, object flags, DQ flags
  - Visual inspection status
  - Cone search around coordinates

- **Download spectra** with:
  - Automatic file caching (skip re-downloads)
  - Progress bars
  - Batch downloads

- **Metadata discovery**:
  - List available programs, fields, gratings, observations
  - Explore the archive before querying

- **Interactive plotting** (requires `plotly`):
  - Multi-panel spectrum viewer (1D + 2D heatmap)
  - Redshift fitting visualization
  - Emission line overlays
  - Flux unit conversion (fν ↔ fλ)

- **Astropy integration**:
  - Returns `astropy.table.Table` objects
  - Works seamlessly with astropy ecosystem

## Authentication

Get your API key from your CAMPFIRE profile settings. Then either:

1. Pass it directly: `Campfire(api_key='sk_live_...')`
2. Set environment variable: `export CAMPFIRE_API_KEY=sk_live_...`

## Custom Base URL

By default, the client connects to `https://campfire.hollisakins.com/api/v1`. You can override this for development or testing:

```python
# Connect to local development server
cf = Campfire(base_url="http://localhost:3000/api/v1")

# Connect to staging environment
cf = Campfire(base_url="https://dev.campfire.hollisakins.com/api/v1")
```

## Examples

### Query high-redshift galaxies

```python
cf = Campfire()

# Find z>3 galaxies with high-quality redshifts
high_z = cf.query_objects(
    redshift_range=(3.0, 10.0),
    redshift_quality=[2, 3],  # Good quality
    inspected_only=True
)
```

### Cone search

```python
# Find objects within 5 arcsec of coordinates
nearby = cf.query_objects(
    cone_search=(150.0, 2.5, 5.0)  # RA, Dec (degrees), radius (arcsec)
)
```

### Explore available metadata

```python
cf = Campfire()

# List all programs you have access to
programs = cf.get_programs()
print(programs)

# Get available fields and gratings
print("Fields:", cf.get_fields())
print("Gratings:", cf.get_gratings())
print("Observations:", cf.get_observations())

# Or get everything at once
metadata = cf.get_metadata()
```

### Download all PRISM spectra for a program

```python
results = cf.query_objects(programs=['CAPERS-COSMOS'])

paths = cf.download_spectra(
    table=results,
    download_dir='./capers_data/',
    gratings=['PRISM'],
    show_progress=True
)
```

### Plot a spectrum (interactive Plotly)

```python
from campfire import Campfire
from campfire.plotting import plot_spectrum, plot_redshift_fit

cf = Campfire()

# Fetch spectrum data for plotting
spec_data = cf.get_spectrum_data('ember_uds_p4_123456', 'PRISM')

# Create multi-panel plot with 2D heatmap
fig = plot_spectrum(
    spec_data,
    redshift=2.5,
    show_emission_lines=True,
    flux_unit='fnu',  # or 'flambda'
)
fig.show()

# Plot redshift fitting results
fit_data = cf.get_redshift_fit_data('ember_uds_p4_123456', 'PRISM')
fig = plot_redshift_fit(fit_data, spectrum_data=spec_data)
fig.show()
```

### Simple 1D spectrum plot

```python
from campfire.plotting import plot_spectrum_simple

# Lightweight plot without 2D heatmap
fig = plot_spectrum_simple(
    spec_data,
    redshift=2.5,
    show_emission_lines=True
)
fig.show()
```

### Cross-match with your catalog

```python
from astropy.coordinates import SkyCoord
import astropy.units as u

# Get all CAMPFIRE objects
campfire_objects = cf.query_objects(limit=10000)

# Your catalog coordinates
my_coords = SkyCoord(ra=[150.1, 150.2]*u.deg, dec=[2.5, 2.6]*u.deg)

# Convert CAMPFIRE coords
cf_coords = SkyCoord(
    ra=campfire_objects['ra']*u.deg,
    dec=campfire_objects['dec']*u.deg
)

# Find matches within 1 arcsec
idx, sep, _ = my_coords.match_to_catalog_sky(cf_coords)
matches = campfire_objects[idx][sep < 1*u.arcsec]
```

## API Reference

### `Campfire`

Main client class for interacting with CAMPFIRE.

#### Query Methods

##### `query_objects(**filters)`

Query objects with optional filters.

**Parameters:**
- `programs` (list): Program IDs or names
- `fields` (list): Field names (e.g., `['COSMOS', 'UDS']`)
- `gratings` (list): Grating names (e.g., `['PRISM', 'G395M']`)
- `observations` (list): Observation names
- `redshift_range` (tuple): `(min, max)` redshift
- `redshift_quality` (list): Quality codes (0=auto, 1=low, 2=medium, 3=high)
- `max_snr_range` (tuple): `(min, max)` SNR
- `spectral_features` (int): Bit mask for features
- `object_flags` (int): Bit mask for object flags
- `dq_flags` (int): Bit mask for DQ flags
- `inspected_only` (bool): Only visually inspected objects
- `search` (str): Text search on object_id
- `cone_search` (tuple): `(ra, dec, radius_arcsec)` for cone search
- `limit` (int): Max results (default: 1000)
- `offset` (int): Pagination offset
- `sort` (str): Sort column
- `sort_dir` (str): `'asc'` or `'desc'`

**Returns:** `astropy.table.Table`

#### Metadata Methods

##### `get_metadata()`

Get all available metadata in one call.

**Returns:** `dict` with keys: `programs`, `fields`, `gratings`, `observations`

##### `get_programs()`

List available programs with metadata.

**Returns:** `astropy.table.Table` with columns: `program_id`, `program_name`, `pi_name`, `is_public`

##### `get_fields()`

List available field names.

**Returns:** `list[str]`

##### `get_gratings()`

List available grating types.

**Returns:** `list[str]`

##### `get_observations()`

List available observation names.

**Returns:** `list[str]`

#### Download Methods

##### `download_spectrum(fits_path, output_path=None, overwrite=False)`

Download a single FITS file.

**Parameters:**
- `fits_path` (str): FITS path from query results
- `output_path` (str/Path): Local save path
- `overwrite` (bool): Overwrite existing file

**Returns:** `str` (path to downloaded file)

##### `download_spectra(table, download_dir='.', gratings=None, overwrite=False)`

Download multiple spectra.

**Parameters:**
- `table` (Table): Results from `query_objects()`
- `download_dir` (str/Path): Directory for downloads
- `gratings` (list): Filter by gratings
- `overwrite` (bool): Overwrite existing files

**Returns:** `dict` mapping `object_id` to `{grating: filepath}`

#### Plotting Data Methods

##### `get_spectrum_data(object_id, grating)`

Fetch spectrum JSON data for plotting.

**Parameters:**
- `object_id` (str): Object ID
- `grating` (str): Grating type (e.g., 'PRISM', 'G395M')

**Returns:** `dict` with keys: `wave`, `fnu`, `fnu_err`, `snr_2d`, `profile`, etc.

##### `get_redshift_fit_data(object_id, grating)`

Fetch redshift fitting results for plotting.

**Parameters:**
- `object_id` (str): Object ID
- `grating` (str): Grating type

**Returns:** `dict` with keys: `redshift`, `chi2_min`, `confidence`, `z_grid`, `chi2_grid`, `model_wave`, `model_fnu`

### Plotting Functions

These require installing with `pip install -e ".[plotting]"`.

##### `plot_spectrum(spectrum_data, ...)`

Create multi-panel spectrum plot with 2D S/N heatmap.

**Parameters:**
- `spectrum_data` (dict): From `get_spectrum_data()`
- `redshift` (float): Redshift for emission lines (default: 0.0)
- `flux_unit` (str): `'fnu'` or `'flambda'` (default: 'fnu')
- `show_errors` (bool): Show error band (default: True)
- `show_emission_lines` (bool): Show emission line markers (default: False)
- `colormap` (str): Heatmap colormap (default: 'viridis')
- `snr_range` (tuple): S/N range for heatmap (default: (-5, 10))

**Returns:** `plotly.graph_objects.Figure`

##### `plot_redshift_fit(fit_data, spectrum_data=None, ...)`

Create redshift fitting plot with chi-squared curve.

**Parameters:**
- `fit_data` (dict): From `get_redshift_fit_data()`
- `spectrum_data` (dict): Optional observed spectrum overlay
- `flux_unit` (str): `'fnu'` or `'flambda'`
- `show_emission_lines` (bool): Show emission line markers

**Returns:** `plotly.graph_objects.Figure`

##### `plot_spectrum_simple(spectrum_data, ...)`

Create simple 1D spectrum plot (no 2D heatmap).

**Parameters:** Same as `plot_spectrum()` but without heatmap options.

**Returns:** `plotly.graph_objects.Figure`

### Helper Functions

##### `convert_flux_units(fnu, wavelength, to_unit='flambda')`

Convert between flux units.

##### `get_emission_lines(redshift, wave_min=None, wave_max=None)`

Get emission lines with observed wavelengths at given redshift.

##### `EMISSION_LINES`

List of common emission lines with rest wavelengths.

## License

MIT
