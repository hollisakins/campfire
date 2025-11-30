# CAMPFIRE Python API

Python client for querying and downloading NIRSpec spectroscopic data from the CAMPFIRE archive.

## Installation

```bash
cd python
pip install -e .
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

- **Astropy integration**:
  - Returns `astropy.table.Table` objects
  - Works seamlessly with astropy ecosystem

## Authentication

Get your API key from your CAMPFIRE profile settings. Then either:

1. Pass it directly: `Campfire(api_key='sk_live_...')`
2. Set environment variable: `export CAMPFIRE_API_KEY=sk_live_...`

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

#### Methods

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

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black campfire/
```

## License

MIT
