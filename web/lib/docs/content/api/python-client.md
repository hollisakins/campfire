# Python Client

The `Campfire` class provides an interactive interface for querying metadata, accessing spectra, and plotting. When locally synced data is available (from `campfire sync`), queries run against your local SQLite database for instant results.

The API has two primary query surfaces:

- **`query_objects`** — one row per sky position. Carries inspection state (`redshift`, `redshift_quality`, `tags`) and cross-program aggregates (`max_snr`, `n_spectra`). Use this for science selection.
- **`query_spectra`** — one row per spectrum. Carries per-spectrum metadata (`spectrum_id`, `grating`, `fits_path`, `dq_flags`, `redshift_auto`). Use this when you need to reach individual FITS files.

For a single object with its spectra and photometry attached, use `cf.get_object(object_id)`, which returns a typed [`Object`](#working-with-objects) dataclass.

## Quick Start

```python
from campfire import Campfire

cf = Campfire()

# Query objects (sky positions)
results = cf.query_objects(
    redshift_range=(3.0, 6.0),
    redshift_quality=[3, 4],
    limit=100,
)

# Load one object with its spectra + photometry
obj = cf.get_object('ember_uds_p4_123456')
print(obj)                       # repr shows gratings, tags, photometry
spec = obj.spectra[0].open()     # SpectrumData with wavelength, fnu, flam
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

Query sky-objects — one row per unique (ra, dec) grouping. Returns an `astropy.table.Table`. Inspection state (redshift, quality, tags) lives at this level.

```python
cf.query_objects(
    fields=None,             # list[str]: e.g., ['cosmos', 'uds']
    programs=None,           # list[int|str]: Program IDs or slugs
    gratings=None,           # list[str]: e.g., ['PRISM', 'G395M']
    observations=None,       # list[str]: Observation names
    redshift_range=None,     # tuple[float, float]: (min, max)
    redshift_quality=None,   # list[int]: Quality codes (0, 1, 2, 3, 4)
    max_snr_range=None,      # tuple[float, float]: (min, max) best-SNR
    dq_flags=None,           # Per-spectrum DQ bitmask filter (see Flag Filtering)
    tags=None,               # list[str]: Tag slugs (e.g., ['lrd', 'blagn'])
    inspected_only=None,     # bool: Only inspected objects
    staleness=None,          # bool: Only objects with stale spectra
    has_photometry=None,     # bool: Only objects with matched photometry
    search=None,             # str: Text search on object_id
    cone_search=None,        # tuple[float, float, float]: (ra, dec, radius_arcsec)
    limit=None,              # int: Max results (default: unlimited locally, 1000 remote)
    offset=0,                # int: Pagination offset
    sort='object_id',        # str: Sort column
    sort_dir='asc',          # str: 'asc' or 'desc'
    remote=False,            # bool: Force remote API (skip local)
)
```

**Examples:**

```python
# High-z galaxies in COSMOS with inspected redshifts
results = cf.query_objects(
    fields=['cosmos'],
    redshift_range=(4.0, 8.0),
    redshift_quality=[3, 4],
    inspected_only=True,
)

# Filter by tags
lrds = cf.query_objects(tags=['lrd', 'blagn'])

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
# Iterate over ALL matching objects without pagination bookkeeping
for row in cf.iter_objects(redshift_range=(2.0, 4.0)):
    print(row['object_id'], row['redshift'])

# Collect into a list
all_lrds = list(cf.iter_objects(tags=['lrd']))
```

When local data is available, `iter_objects()` queries SQLite directly. Otherwise, it auto-paginates through the remote API.

---

## Querying Spectra

### `query_spectra()`

Query the flat per-spectrum view — one row per spectrum. Each row has a unique `spectrum_id` plus its parent `object_id`. Per-spectrum `dq_flags` live here, and inspection filters (`redshift_range`, `redshift_quality`, `inspected_only`) join through the parent object.

```python
cf.query_spectra(
    fields=None,             # list[str]
    programs=None,           # list[int|str]
    gratings=None,           # list[str]: e.g., ['PRISM']
    observations=None,       # list[str]
    redshift_range=None,     # tuple[float, float]  (object-level)
    redshift_quality=None,   # list[int]            (object-level)
    max_snr_range=None,      # tuple[float, float]
    dq_flags=None,           # Per-spectrum DQ bitmask filter
    tags=None,               # list[str]            (object-level)
    inspected_only=None,     # bool                 (object-level)
    has_photometry=None,     # bool
    search=None,             # str: on spectrum_id
    cone_search=None,        # tuple[float, float, float]
    limit=None,
    offset=0,
    sort='spectrum_id',
    sort_dir='asc',
    remote=False,
)
```

**Returned columns** include: `spectrum_id`, `target_id`, `object_id`, `grating`, `fits_path`, `signal_to_noise`, `exposure_time`, `reduction_version`, `redshift_auto`, `dq_flags`, `local_path`.

```python
from campfire.flags import DQFlags

# Clean PRISM spectra belonging to inspected objects
good = cf.query_spectra(
    gratings=['PRISM'],
    inspected_only=True,
    dq_flags=~DQFlags.CONTAMINATION & ~DQFlags.LOW_SNR,
)

# Open the first one
spec = cf.open_spectrum(good[0]['spectrum_id'])
```

### `iter_spectra()`

Auto-paginating iterator over the spectra view. Accepts the same filters as `query_spectra()`.

```python
for row in cf.iter_spectra(gratings=['G395M']):
    print(row['spectrum_id'], row['signal_to_noise'])
```

### `get_spectrum()`

Return a single spectrum catalog row by ID (or `None`).

```python
row = cf.get_spectrum('ember_uds_p4_prism_clear_123456')
```

---

## Tags

Objects can be tagged with user-defined or system-seeded tags. Tags replace the old `object_flags` bitmask system.

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
results = cf.query_objects(tags=['lrd', 'blagn'])

# Iterate over tagged objects
for row in cf.iter_objects(tags=['lrd']):
    print(row['object_id'], row['redshift'])
```

Tags also surface as `Object.tags` on the dataclass returned by `cf.get_object()`.

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
# Clean spectra only (dq_flags is per-spectrum)
results = cf.query_spectra(
    dq_flags=~(DQFlags.CONTAMINATION | DQFlags.LOW_SNR),
)

# Or, filter objects by their spectra's DQ flags
objects = cf.query_objects(
    dq_flags=~(DQFlags.CONTAMINATION | DQFlags.LOW_SNR),
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

## Working with Objects

### `get_object()`

Return a single `Object` dataclass, with its spectra and photometry attached. Returns `None` if not found.

```python
obj = cf.get_object('ember_uds_p4_123456')

print(obj)
# Object(ember_uds_p4_123456, z=3.4210, cosmos)
#   3 spectra (G140M, G395M, PRISM)
#   tags: lrd
#   Photometry(8 bands, UVCANDELS, photo_z=3.51)
```

Attributes available on `Object`:

| Attribute | Type | Description |
|-----------|------|-------------|
| `object_id`, `field`, `ra`, `dec` | — | Identification and position |
| `redshift`, `redshift_auto`, `redshift_inspected` | float | Best, automated, and inspected redshifts |
| `redshift_quality` | int | 0 (not inspected), 1 (impossible), 2 (tentative), 3 (probable), 4 (secure) |
| `programs`, `tags` | list[str] | Program slugs and tag slugs |
| `n_spectra`, `max_snr`, `max_exposure_time` | — | Cross-spectrum aggregates |
| `has_photometry`, `photo_z`, `photo_z_err_lo/hi` | — | Photometry summary |
| `spectra` | `SpectrumCollection` | All spectra for this object |
| `photometry` | `Photometry` or `None` | Broadband photometric measurements |

### `SpectrumCollection`

`Object.spectra` is a `SpectrumCollection` — a filterable, numpy-style container of `Spectrum` objects.

```python
# Integer indexing → single Spectrum
first = obj.spectra[0]

# Numpy-style boolean masking
prism = obj.spectra[obj.spectra.grating == 'PRISM']
high_snr = obj.spectra[obj.spectra.signal_to_noise > 10]

# Properties return numpy arrays
obj.spectra.spectrum_id      # np.ndarray[str]
obj.spectra.signal_to_noise  # np.ndarray[float]
obj.spectra.gratings         # sorted list of unique gratings

# Convert to astropy Table
tbl = obj.spectra.to_table()

# Iterate
for s in obj.spectra:
    print(s.spectrum_id, s.grating, s.downloaded)
```

### `Spectrum`

Each entry in a `SpectrumCollection` is a `Spectrum` — the catalog row for one spectrum. Call `.open()` (or use `.data`) to load the FITS arrays as a [`SpectrumData`](#accessing-spectra).

```python
s = obj.spectra[0]

s.spectrum_id         # stable per-spectrum ID
s.grating             # 'PRISM', 'G395M', …
s.signal_to_noise     # peak SNR
s.dq_flags            # per-spectrum DQ bitmask
s.downloaded          # True if local FITS is available

spec = s.open()       # SpectrumData — reads local FITS, falls back to API
s.plot()              # shortcut: quick-look matplotlib plot
```

### `Photometry`

`Object.photometry` exposes broadband photometry for the object (`None` if unmatched).

```python
phot = obj.photometry
phot.bands                 # ['f115w', 'f150w', 'f277w', 'f444w', ...]
phot.flux                  # np.ndarray[float], μJy
phot.flux_err              # np.ndarray[float], μJy
phot.wavelength            # np.ndarray[float], μm
phot.catalog               # source catalog name
phot.photo_z               # photometric redshift (or None)

# Access a single band
band = phot['f444w']
band.flux, band.flux_err, band.wavelength

# Convert to astropy Table
phot.to_table()
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
data = cf.get_spectrum_data('ember_uds_p4_prism_clear_123456')
```

Takes a `spectrum_id`. Returns a dict with: `wave`, `fnu`, `fnu_err`, `snr_2d`, `n_spatial`, `n_wave`, `profile`, `profile_fit`, `profile_pix`.

### `get_redshift_fit_data()`

```python
fit = cf.get_redshift_fit_data('ember_uds_p4_prism_clear_123456')
```

Takes a `spectrum_id`. Returns a dict with: `redshift`, `chi2_min`, `confidence`, `z_grid`, `chi2_grid`, `model_wave`, `model_fnu`.

---

## Plotting

CAMPFIRE includes Plotly-based plotting functions. Requires installing with the `plotting` extra (see [Installation](/docs/api#installation)).

```python
from campfire import plot_spectrum, plot_redshift_fit, plot_spectrum_simple

cf = Campfire()
spectrum_id = 'ember_uds_p4_prism_clear_123456'
data = cf.get_spectrum_data(spectrum_id)

# Multi-panel plot (2D heatmap + profile + 1D spectrum)
fig = plot_spectrum(data, redshift=2.5, show_emission_lines=True)
fig.show()

# Redshift fit visualization
fit = cf.get_redshift_fit_data(spectrum_id)
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

## Calibration & Stacking

`campfire.calibration` provides flux-calibration of spectra against broadband photometry and multi-spectrum stacking. These routines require the extras from `pip install "campfire[deploy]"` (scipy, matplotlib).

### `calibrate_to_photometry()`

Flux-calibrate a spectrum against broadband photometry by fitting a smooth correction curve (Chebyshev polynomial or a single scalar) so that synthetic photometry from the spectrum matches the observed bands.

```python
from campfire import calibrate_to_photometry

obj = cf.get_object('ember_uds_p4_123456')
spec = obj.spectra[obj.spectra.grating == 'PRISM'][0]

result = calibrate_to_photometry(
    spec,                     # Spectrum or SpectrumData
    obj.photometry,           # Photometry
    method='chebyshev',       # 'chebyshev' (default) or 'flat'
    # bands=['f150w', 'f277w', 'f444w'],  # optional; auto-selected by default
    # degree=3,                            # Chebyshev degree
    # min_snr=0.5,                         # per-band SNR threshold
)

result.spectrum      # SpectrumData, calibrated
result.original      # SpectrumData, uncalibrated input
result.multiplier    # correction curve (same length as wavelength)
result.bands_used    # bands that contributed to the fit
result.plot()        # diagnostic matplotlib plot
```

### `stack_spectra()`

Resample multiple spectra onto a common wavelength grid and combine them.

```python
from campfire import stack_spectra

obj = cf.get_object('ember_uds_p4_123456')
prism = [s.open() for s in obj.spectra[obj.spectra.grating == 'PRISM']]

stacked = stack_spectra(
    prism,
    method='weighted_mean',   # 'weighted_mean' | 'median' | 'mean'
    # wavelength_grid=None,   # default: grid from the input with the most pixels
    object_id=obj.object_id,  # sets stacked.spectrum_id = f'stack:{object_id}:{grating}'
)

stacked.plot()
```

### `calibrate_and_stack()`

Convenience wrapper: per-spectrum calibration → resample → stack, in one call. Accepts a `SpectrumCollection`, a list of `Spectrum`, or a list of `SpectrumData`.

```python
from campfire import calibrate_and_stack

obj = cf.get_object('ember_uds_p4_123456')

result = calibrate_and_stack(
    obj.spectra[obj.spectra.grating == 'PRISM'],
    photometry=obj.photometry,
    method='chebyshev',
    stacking_method='weighted_mean',
    object_id=obj.object_id,
)

result.spectrum         # SpectrumData, final stacked spectrum
result.calibrations     # list[CalibrationResult], per-input
result.input_spectra    # list[SpectrumData], calibrated inputs before stacking
result.plot()           # 3-panel diagnostic
```

### `synthetic_photometry()`

Compute AB synthetic photometry for a single band from a spectrum.

```python
from campfire import synthetic_photometry

flux, err = synthetic_photometry(
    spec.wavelength, spec.fnu, spec.fnu_err, 'f444w',
)
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
    obj = cf.get_object('ember_uds_p4_123456')
    if obj is None:
        print("No such object")
    else:
        spec = obj.spectra[0].open()
except AuthenticationError:
    print("Run: campfire login")
except NotFoundError as e:
    print(f"Not found: {e}")
```
