# Python Client

The `Campfire` class provides an interactive interface for querying metadata, accessing spectra, and plotting. When locally synced data is available (from `campfire sync`), queries run against your local SQLite database for instant results.

## Quick Start

```python
from campfire import Campfire

cf = Campfire()

# Query objects
results = cf.query_objects(
    redshift_range=(3.0, 6.0),
    redshift_quality=[2, 3],
    limit=100
)

# Open a spectrum directly
spec = cf.open_spectrum('ember_uds_p4_123456', 'PRISM')
print(spec.wavelength.shape, spec.flux.shape)
```

---

## Initialization

```python
Campfire(base_url=None, data_dir=None, auto_refresh=True)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | str | None | API URL. Uses `CAMPFIRE_API_URL` env var or production server. |
| `data_dir` | str/Path | None | Local data directory. Auto-detected from `~/.campfire/config.toml`. |
| `auto_refresh` | bool | True | Automatically refresh OAuth tokens. |

The client auto-detects locally synced data. If `~/.campfire/data/.campfire_meta/campfire.db` exists, queries are served from SQLite.

```python
# Check if local data is available
cf = Campfire()
print(cf.is_local)       # True if local database found
print(cf.last_synced)    # Timestamp of last sync
```

---

## Sync and Download

### `sync()`

Sync the full object/spectra catalog from the server. Equivalent to `campfire sync`. Metadata only — no FITS files.

```python
result = cf.sync()
# {'observations': 8, 'objects': 2450, 'spectra': 7200, 'stale_count': 0}
```

After syncing, all queries via `query_objects()` are served from the local SQLite database.

### `download()`

Download FITS files. Equivalent to `campfire download`. Requires a prior `sync()`.

```python
cf.download(observations=['ember_uds_p4'])                    # By observation
cf.download(programs=['EMBER-UDS'], gratings=['PRISM'])        # By program + grating
cf.download(fields=['COSMOS'])                                 # By field
cf.download(stale_only=True)                                   # Re-download updated files
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `observations` | list[str] | None | Filter by observation name |
| `programs` | list[str] | None | Filter by program slug |
| `fields` | list[str] | None | Filter by field |
| `gratings` | list[str] | None | Filter by grating |
| `stale_only` | bool | False | Only re-download changed files |
| `max_workers` | int | 4 | Parallel download workers |

### Staleness Detection

When spectra are reprocessed on the server, `sync()` detects the change by comparing server-side file hashes against your local copies:

```python
result = cf.sync()
if result['stale_count'] > 0:
    print(f"{result['stale_count']} files updated on server")
    cf.download(stale_only=True)
```

---

## Querying Objects

### `query_objects()`

Query the spectroscopic database with filters. Returns an `astropy.table.Table`.

```python
cf.query_objects(
    programs=None,           # list[int|str]: Program IDs or slugs
    fields=None,             # list[str]: e.g., ['COSMOS', 'UDS']
    gratings=None,           # list[str]: e.g., ['PRISM', 'G395M']
    observations=None,       # list[str]: Observation names
    redshift_range=None,     # tuple[float, float]: (min, max)
    redshift_quality=None,   # list[int]: Quality codes
    max_snr_range=None,      # tuple[float, float]: (min, max) SNR
    spectral_features=None,  # Flag filter (see Flag Filtering)
    object_flags=None,       # Flag filter (see Flag Filtering)
    dq_flags=None,           # Flag filter (see Flag Filtering)
    inspected_only=None,     # bool: Only inspected objects
    search=None,             # str: Text search on object_id
    cone_search=None,        # tuple[float, float, float]: (ra, dec, radius_arcsec)
    limit=1000,              # int: Max results
    offset=0,                # int: Pagination offset
    sort='object_id',        # str: Sort column
    sort_dir='asc',          # str: 'asc' or 'desc'
    remote=False,            # bool: Force remote API (skip local)
)
```

**Examples:**

```python
# High-z galaxies in COSMOS with good redshifts
results = cf.query_objects(
    fields=['COSMOS'],
    redshift_range=(4.0, 8.0),
    redshift_quality=[2, 3],
    inspected_only=True
)

# Cone search around a coordinate
results = cf.query_objects(
    cone_search=(150.0832, 2.3511, 30.0)  # RA, Dec, radius in arcsec
)

# Force remote API (bypass local data)
results = cf.query_objects(remote=True)
```

### `iter_objects()`

Auto-paginating iterator over all matching objects. Accepts the same filters as `query_objects()`.

```python
# Iterate over ALL matching objects without worrying about pagination
for obj in cf.iter_objects(redshift_range=(2.0, 4.0)):
    print(obj['object_id'], obj['redshift'])

# Collect into a list
all_lrds = list(cf.iter_objects(object_flags=ObjectFlags.LRD))
```

When local data is available, `iter_objects()` queries SQLite directly. Otherwise, it auto-paginates through the remote API.

---

## Flag Filtering

CAMPFIRE uses bitmask flags for spectral features, object classifications, and data quality. The Python client provides numpy-style operators for intuitive filtering.

### Operators

```python
from campfire.flags import ObjectFlags, DQFlags, SpectralFeatures

# OR: Match any of these flags
ObjectFlags.LRD | ObjectFlags.LYA_EMITTER

# AND: Must have all these flags
ObjectFlags.LRD & ObjectFlags.BROAD_LINE

# NOT: Exclude this flag
~DQFlags.CONTAMINATION

# Complex expressions
(ObjectFlags.LRD | ObjectFlags.LYA_EMITTER) & ~ObjectFlags.BROAD_LINE
```

### Examples

```python
# Find LRDs or LAEs, excluding broad-line AGN
results = cf.query_objects(
    object_flags=(ObjectFlags.LRD | ObjectFlags.LYA_EMITTER) & ~ObjectFlags.BROAD_LINE
)

# Objects with multiple emission lines and clean data
results = cf.query_objects(
    spectral_features=SpectralFeatures.MULTI_EMISSION,
    dq_flags=~(DQFlags.CONTAMINATION | DQFlags.LOW_SNR)
)

# Simple string-based filtering
results = cf.query_objects(object_flags=['LRD', 'LYA_EMITTER'])
```

### Flag Reference

See the [Flags documentation](/docs/inspection/flags) for full flag definitions and values.

### Utility Functions

```python
from campfire import list_flags, decode_flags, encode_flags

list_flags()                                          # Print all flags
list_flags('object_flags')                            # Print specific type
decode_flags(5, 'object_flags')                       # [LRD, LYA_EMITTER]
encode_flags(['LRD', 'LYA_EMITTER'], 'object_flags') # 5
```

---

## Accessing Spectra

### `open_spectrum()`

Open a spectrum as a `SpectrumData` object with wavelength, flux, and error arrays. Checks for locally downloaded FITS files first. If not found locally, downloads from the API and caches in the managed data directory so subsequent calls are instant.

```python
spec = cf.open_spectrum(object_id, grating)
```

**Returns:** `SpectrumData` with attributes:

| Attribute | Type | Description |
|-----------|------|-------------|
| `wavelength` | np.ndarray | Wavelength in microns |
| `flux` | np.ndarray | Flux density f_nu in microjansky |
| `flux_err` | np.ndarray | Flux error in microjansky |
| `header` | dict | FITS primary header |
| `grating` | str | Grating name |
| `object_id` | str | Object ID |
| `fits_path` | str/None | Local file path if from disk |

**Example:**

```python
spec = cf.open_spectrum('ember_uds_p4_123456', 'PRISM')

print(spec)
# SpectrumData(ember_uds_p4_123456, PRISM, 1024 pixels, 0.60-5.30 μm)

# Access arrays directly
import matplotlib.pyplot as plt
plt.plot(spec.wavelength, spec.flux)
plt.xlabel('Wavelength (μm)')
plt.ylabel('f_ν (μJy)')

# Access FITS header
print(spec.header.get('EXPTIME'))

# Second call is instant — file is cached locally
spec2 = cf.open_spectrum('ember_uds_p4_123456', 'PRISM')
```

You can also create a `SpectrumData` from any FITS file:

```python
from campfire import SpectrumData
spec = SpectrumData.from_fits('/path/to/local/file.fits')
```

---

## Metadata

### `get_metadata()`

Get all available filter options in a single call.

```python
meta = cf.get_metadata()
# {'programs': [...], 'fields': [...], 'gratings': [...], 'observations': [...]}
```

### `get_programs()`

List programs as an astropy Table.

```python
programs = cf.get_programs()
# Table with: program_id, program_name, pi_name, is_public
```

### `get_fields()`, `get_gratings()`, `get_observations()`

```python
cf.get_fields()        # ['COSMOS', 'UDS', ...]
cf.get_gratings()      # ['PRISM', 'G395M', ...]
cf.get_observations()  # ['ember_uds_p4', ...]
```

---

## Spectrum Data for Plotting

These methods return JSON data for visualization, without downloading FITS files.

### `get_spectrum_data()`

```python
data = cf.get_spectrum_data('ember_uds_p4_123456', 'PRISM')
```

Returns a dict with: `wave`, `fnu`, `fnu_err`, `snr_2d`, `n_spatial`, `n_wave`, `profile`, `profile_fit`, `profile_pix`.

### `get_redshift_fit_data()`

```python
fit = cf.get_redshift_fit_data('ember_uds_p4_123456', 'PRISM')
```

Returns a dict with: `redshift`, `chi2_min`, `confidence`, `z_grid`, `chi2_grid`, `model_wave`, `model_fnu`.

---

## Plotting

CAMPFIRE includes Plotly-based plotting functions. Requires `pip install campfire[plotting]`.

```python
from campfire import plot_spectrum, plot_redshift_fit, plot_spectrum_simple

cf = Campfire()
data = cf.get_spectrum_data('ember_uds_p4_123456', 'PRISM')

# Multi-panel plot (2D heatmap + profile + 1D spectrum)
fig = plot_spectrum(data, redshift=2.5, show_emission_lines=True)
fig.show()

# Redshift fit visualization
fit = cf.get_redshift_fit_data('ember_uds_p4_123456', 'PRISM')
fig = plot_redshift_fit(fit, spectrum_data=data)
fig.show()

# Simple 1D spectrum (lightweight)
fig = plot_spectrum_simple(data, redshift=2.5)
fig.show()
```

### Helper Functions

```python
from campfire import convert_flux_units, get_emission_lines, EMISSION_LINES

flambda = convert_flux_units(fnu, wavelength, to_unit='flambda')
lines = get_emission_lines(redshift=2.5, wave_min=1.0, wave_max=5.0)
```

---

## Error Handling

```python
from campfire import (
    CampfireError,       # Base exception
    AuthenticationError, # Invalid/expired credentials
    NotFoundError,       # Object or spectrum not found
    DownloadError,       # File download failed
    ValidationError,     # Invalid parameters
    APIError             # Unexpected API error
)

try:
    results = cf.query_objects()
except AuthenticationError:
    print("Run: campfire login")
except NotFoundError as e:
    print(f"Not found: {e}")
```
