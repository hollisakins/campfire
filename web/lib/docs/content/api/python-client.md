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

# Open a spectrum directly (spectrum_id from the catalog)
spec = cf.open_spectrum('ember_uds_p4_prism_clear_123456')
print(spec.wavelength.shape, spec.fnu.shape)
```

---

## Initialization

```python
Campfire(base_url=None, data_dir=None, auto_refresh=True)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | str | None | API URL. Uses `CAMPFIRE_API_URL` env var or production server. |
| `data_dir` | str/Path | None | Root data directory. Defaults to `$CAMPFIRE_ROOT` or `~/campfire`. |
| `auto_refresh` | bool | True | Automatically refresh OAuth tokens. |

The client auto-detects locally synced data. If `<data_dir>/meta/campfire.db` exists, queries are served from SQLite. When `$CAMPFIRE_ROOT` is set, the client uses the same `products/` directory as the pipeline, so already-reduced spectra are found without re-downloading.

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
cf.query_targets(
    programs=None,           # list[int|str]: Program IDs or slugs
    fields=None,             # list[str]: e.g., ['COSMOS', 'UDS']
    gratings=None,           # list[str]: e.g., ['PRISM', 'G395M']
    observations=None,       # list[str]: Observation names
    redshift_range=None,     # tuple[float, float]: (min, max)
    redshift_quality=None,   # list[int]: Quality codes
    max_snr_range=None,      # tuple[float, float]: (min, max) SNR
    dq_flags=None,           # Flag filter (see Flag Filtering)
    tags=None,               # list[str]: Tag slugs (e.g., ['lrd', 'blagn'])
    inspected_only=None,     # bool: Only inspected targets
    search=None,             # str: Text search on target_id
    cone_search=None,        # tuple[float, float, float]: (ra, dec, radius_arcsec)
    limit=1000,              # int: Max results
    offset=0,                # int: Pagination offset
    sort='target_id',        # str: Sort column
    sort_dir='asc',          # str: 'asc' or 'desc'
    remote=False,            # bool: Force remote API (skip local)
)
```

**Examples:**

```python
# High-z galaxies in COSMOS with good redshifts
results = cf.query_targets(
    fields=['COSMOS'],
    redshift_range=(4.0, 8.0),
    redshift_quality=[2, 3],
    inspected_only=True
)

# Filter by tags
lrds = cf.query_targets(tags=['lrd', 'blagn'])

# Cone search around a coordinate
results = cf.query_targets(
    cone_search=(150.0832, 2.3511, 30.0)  # RA, Dec, radius in arcsec
)

# Force remote API (bypass local data)
results = cf.query_targets(remote=True)
```

### `iter_targets()`

Auto-paginating iterator over all matching targets. Accepts the same filters as `query_targets()`.

```python
# Iterate over ALL matching targets without worrying about pagination
for obj in cf.iter_targets(redshift_range=(2.0, 4.0)):
    print(obj['target_id'], obj['redshift'])

# Collect into a list
all_lrds = list(cf.iter_targets(tags=['lrd']))
```

When local data is available, `iter_targets()` queries SQLite directly. Otherwise, it auto-paginates through the remote API.

---

## Tags

Targets can be tagged with user-defined or system-seeded tags. Tags replace the old `object_flags` bitmask system.

### `get_tags()`

List all available tags (system and user-created).

```python
>>> tags = cf.get_tags()
>>> print(tags['slug', 'name', 'member_count'])
slug   name              member_count
------ ----------------- ------------
lrd    Little Red Dots            142
blagn  Broad Line AGN              87
...
```

### Filtering by tags

```python
# Filter by tag slugs
results = cf.query_targets(tags=['lrd', 'blagn'])

# Iterate over tagged targets
for obj in cf.iter_targets(tags=['lrd']):
    print(obj['target_id'], obj['redshift'])
```

System tags (e.g., `lrd`, `blagn`, `lae`) are available to all users. Users can also create private or shared tags via the [web portal](/nirspec/tags).

---

## Flag Filtering

CAMPFIRE uses bitmask flags for data quality. The Python client provides numpy-style operators for intuitive filtering.

### Operators

```python
from campfire.flags import DQFlags

# OR: Match any of these flags
DQFlags.CHIP_GAP | DQFlags.LOW_SNR

# AND: Must have all these flags
DQFlags.CHIP_GAP & DQFlags.LOW_SNR

# NOT: Exclude this flag
~DQFlags.CONTAMINATION
```

### Examples

```python
# Clean data only
results = cf.query_targets(
    dq_flags=~(DQFlags.CONTAMINATION | DQFlags.LOW_SNR)
)
```

### Flag Reference

See the [Flags documentation](/docs/inspection/flags) for full flag definitions and values.

### Utility Functions

```python
from campfire import list_flags, decode_flags, encode_flags

list_flags()                                          # Print all flags
list_flags('dq_flags')                                # Print specific type
decode_flags(3, 'dq_flags')                           # ['CHIP_GAP', 'CONTAMINATION']
encode_flags(['CHIP_GAP', 'CONTAMINATION'], 'dq_flags') # 3
```

---

## Accessing Spectra

### `open_spectrum()`

Open a spectrum as a `SpectrumData` object with wavelength, flux, and error arrays. Checks for locally downloaded FITS files first. If not found locally, downloads from the API and caches in the managed data directory so subsequent calls are instant.

```python
spec = cf.open_spectrum(spectrum_id)
```

**Returns:** `SpectrumData` with attributes:

| Attribute | Type | Description |
|-----------|------|-------------|
| `wavelength` | np.ndarray | Wavelength in microns |
| `fnu` | np.ndarray | Flux density f_ν in microjansky (μJy) |
| `fnu_err` | np.ndarray | Flux error f_ν in microjansky (μJy) |
| `flam` | np.ndarray | Flux density f_λ in erg/s/cm²/Å (auto-computed from fnu if not in FITS) |
| `flam_err` | np.ndarray | Flux error f_λ in erg/s/cm²/Å |
| `fnu_units` / `flam_units` / `wave_units` | str | Unit strings |
| `header` | dict | FITS primary header |
| `grating` | str | Grating name |
| `spectrum_id` | str | Stable per-spectrum identifier |
| `fits_path` | str/None | Local file path if from disk |

**Example:**

```python
spec = cf.open_spectrum('ember_uds_p4_prism_clear_123456')

print(spec)
# SpectrumData(ember_uds_p4_prism_clear_123456, PRISM, 1024 pixels, 0.60-5.30 μm)

# Access arrays directly (fnu in μJy; flam auto-computed in erg/s/cm²/Å)
import matplotlib.pyplot as plt
spec.plot(flux_unit='fnu')   # or flux_unit='flam'

# Or plot by hand
plt.step(spec.wavelength, spec.fnu, where='mid')
plt.xlabel('Wavelength (μm)')
plt.ylabel('f_ν (μJy)')

# Access FITS header
print(spec.header.get('EXPTIME'))

# Second call is instant — file is cached locally
spec2 = cf.open_spectrum('ember_uds_p4_prism_clear_123456')
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

CAMPFIRE includes Plotly-based plotting functions. Requires installing with the `plotting` extra (see [Installation](/docs/api#installation)).

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

### NIRCam Cutouts

Generate publication-quality RGB cutout images with vector shutter overlays. Cutout images and shutter geometry are cached locally after the first fetch.

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(5, 5))
cf.plot_cutout('ember_uds_p4_123456', fov=3.2, ax=ax)
fig.savefig('cutout.pdf')  # vector shutter overlay in PDF
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `object_id` | str | — | Object ID |
| `fov` | float | 5.0 | Field of view in arcseconds |
| `size` | int | None | Output size in pixels (default: native resolution) |
| `shutters` | bool/str | True | `True` or `'all'`: all shutters. `'target'`: target only. `False`: none. |
| `ax` | Axes | None | Matplotlib axes (uses `plt.gca()` if None) |
| `shutter_style` | dict | None | Per-category style overrides (see below) |
| `scalebar` | bool | True | Draw a scalebar |

**Shutter filtering:**

```python
cf.plot_cutout('obj_id', fov=3.2, shutters='target', ax=ax)  # target only
cf.plot_cutout('obj_id', fov=3.2, shutters=False, ax=ax)     # no shutters
```

**Custom shutter style** — override per category (`'target'`, `'other'`, `'stuck_closed'`). Partial overrides are merged with defaults. The `marker` key controls shape: `'box'` (default) or `'corners'` (JADES-style L-shaped marks).

```python
cf.plot_cutout('obj_id', fov=3.2, ax=ax, shutter_style={
    "target": {"marker": "corners", "edgecolor": "cyan"},
    "other": {"marker": "corners", "edgecolor": "white", "linewidth": 0.5},
})
```

**Low-level access** — fetch data separately for full control:

```python
from campfire.imaging import plot_cutout

path = cf.get_cutout('obj_id', fov=3.2)       # cached PNG
data = cf.get_shutters('obj_id', fov=3.2)     # cached JSON
plot_cutout(path, shutters=data, object_id='obj_id', fov=3.2, ax=ax)
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
