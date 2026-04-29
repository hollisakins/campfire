# Python Client

Reference for the `Campfire` class and its companion dataclasses. New here? Start with [Getting Started](/docs/api/getting-started) for a guided walk, or browse the [Recipes](/docs/api/recipes) for end-to-end task examples.

The two primary query surfaces:

- **`query_objects`** — one row per sky position. Carries inspection state (`redshift`, `redshift_quality`, `tags`) and cross-program aggregates (`max_snr`, `n_spectra`). Use this for science selection.
- **`query_spectra`** — one row per spectrum. Carries per-spectrum metadata (`spectrum_id`, `grating`, `fits_path`, `dq_flags`, `redshift_auto`). Use this when you need to reach individual FITS files.

For a single object with spectra and photometry attached, use `cf.get_object(object_id)`, which returns a typed [`Object`](#object) dataclass.

This page is organised by topic:

1. [Initialization](#initialization)
2. [Sync and download](#sync-and-download)
3. [Querying](#querying)
4. [Working with an Object](#working-with-an-object)
5. [Opening spectra](#opening-spectra)
6. [Imaging — cutouts and shutters](#imaging--cutouts-and-shutters)
7. [Calibration and stacking](#calibration-and-stacking)
8. [Plotting helpers](#plotting-helpers)
9. [Flag filtering](#flag-filtering)
10. [Metadata accessors](#metadata-accessors)
11. [Error handling](#error-handling)

---

## Initialization

```python
Campfire(base_url=None, data_dir=None, auto_refresh=True)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `base_url` | str | None | API URL. Uses `CAMPFIRE_API_URL` env var or production server. |
| `data_dir` | str/Path | None | Root data directory. Defaults to `$CAMPFIRE_ROOT` or `~/campfire`. |
| `auto_refresh` | bool | True | Automatically refresh OAuth tokens. |

The client auto-detects locally synced data. If `<data_dir>/meta/campfire.db` exists, queries are served from SQLite. When `$CAMPFIRE_ROOT` is set, the client uses the same `products/` directory as the pipeline, so already-reduced spectra are found without re-downloading.

```python
cf = Campfire()
print(cf.is_local)       # True if local database found
print(cf.last_synced)    # ISO timestamp of last sync
```

| Attribute | Description |
|---|---|
| `is_local` | `True` when `<data_dir>/meta/campfire.db` exists and is valid |
| `last_synced` | ISO timestamp of the last successful sync (or `None`) |

Pass `remote=True` to any query to force the API path, useful for verifying that local data is current.

---

## Sync and download

### `sync()`

Pull the full object/spectra catalog from the server. Equivalent to `campfire sync`. Metadata only — no FITS files.

```python
result = cf.sync()
# {'observations': 8, 'objects': 2450, 'spectra': 7200, 'stale_count': 0}
```

After syncing, all queries are served from the local SQLite database.

### `download()`

Download FITS files. Equivalent to `campfire download`. Requires a prior `sync()`.

```python
cf.download(observations=['ember_uds_p4'])               # By observation
cf.download(programs=['EMBER-UDS'], gratings=['PRISM'])   # By program + grating
cf.download(fields=['COSMOS'])                            # By field
cf.download(stale_only=True)                              # Re-download updated files
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `observations` | list[str] | None | Filter by observation name |
| `programs` | list[str] | None | Filter by program slug |
| `fields` | list[str] | None | Filter by field |
| `gratings` | list[str] | None | Filter by grating |
| `stale_only` | bool | False | Only re-download changed files |
| `max_workers` | int | 4 | Parallel download workers |

### Staleness detection

When spectra are reprocessed on the server, `sync()` detects the change by comparing server-side file hashes against your local copies:

```python
result = cf.sync()
if result['stale_count'] > 0:
    print(f"{result['stale_count']} files updated on server")
    cf.download(stale_only=True)
```

See [recipe 4](/docs/api/recipes#4-iterate-over-a-large-sample-efficiently) for an iteration-friendly pattern.

---

## Querying

`query_objects` and `query_spectra` share filter vocabulary; they differ in granularity. `iter_*` variants stream results so you don't have to think about pagination.

### `query_objects()`

Query objects (one row per sky position). Returns an `astropy.table.Table`.

```python
cf.query_objects(
    fields=None,             # list[str]: e.g., ['cosmos', 'uds']
    programs=None,           # list[int|str]: Program IDs or slugs
    gratings=None,           # list[str]: e.g., ['PRISM', 'G395M']
    observations=None,       # list[str]: Observation names
    redshift_range=None,     # tuple[float, float]: (min, max)
    redshift_quality=None,   # list[int|str]: e.g. [3, 4] or ['probable', 'secure']
    max_snr_range=None,      # tuple[float, float]: (min, max) best-SNR
    dq_flags=None,           # Per-spectrum DQ bitmask filter (see Flag Filtering)
    tags=None,               # list[str]: Tag slugs (e.g., ['lrd', 'blagn'])
    inspected_only=None,     # bool
    staleness=None,          # bool: Only objects with stale spectra
    has_photometry=None,     # bool
    search=None,             # str: Text search on object_id
    cone_search=None,        # tuple[float, float, float]: (ra, dec, radius_arcsec)
    limit=None,              # int: Max results (unlimited locally; 1000 default remote)
    offset=0,                # int: Pagination offset
    sort='object_id',        # str: Sort column
    sort_dir='asc',          # 'asc' or 'desc'
    remote=False,            # bool: Force remote API
)
```

Examples:

```python
# High-z galaxies in COSMOS with inspected redshifts
results = cf.query_objects(
    fields=['cosmos'],
    redshift_range=(4.0, 8.0),
    redshift_quality=[3, 4],
    inspected_only=True,
)

# Tag filter
lrds = cf.query_objects(tags=['lrd', 'blagn'])

# Cone search
results = cf.query_objects(cone_search=(150.0832, 2.3511, 30.0))
```

### `query_spectra()`

One row per spectrum. Inspection filters (`redshift_range`, `redshift_quality`, `inspected_only`, `tags`) join through the parent object. Per-spectrum DQ bitmask lives directly on this view.

```python
cf.query_spectra(
    # ... same vocabulary as query_objects, plus dq_flags applied per-spectrum
    sort='spectrum_id',
    ...
)
```

Returned columns: `spectrum_id`, `target_id`, `object_id`, `grating`, `fits_path`, `signal_to_noise`, `exposure_time`, `reduction_version`, `redshift_auto`, `dq_flags`, `local_path`.

```python
from campfire.flags import DQFlags

good = cf.query_spectra(
    gratings=['PRISM'],
    inspected_only=True,
    dq_flags=~DQFlags.CONTAMINATION & ~DQFlags.LOW_SNR,
)
spec = cf.open_spectrum(good[0]['spectrum_id'])
```

### `iter_objects()` and `iter_spectra()`

Auto-paginating iterators. Same filters as their `query_*` siblings. Use them when the result set is large or when you want to short-circuit. See [recipe 4](/docs/api/recipes#4-iterate-over-a-large-sample-efficiently).

```python
for row in cf.iter_objects(redshift_range=(2.0, 4.0)):
    print(row['object_id'], row['redshift'])

# Or collect
all_lrds = list(cf.iter_objects(tags=['lrd']))
```

Locally, these query SQLite directly. Remotely, they auto-paginate through the API.

### `get_object()` and `get_spectrum()`

Fetch a single record by ID. Returns `None` if not found.

```python
obj = cf.get_object('ember_uds_p4_123456')   # → Object dataclass with .spectra + .photometry
row = cf.get_spectrum('ember_uds_p4_prism_clear_123456')   # → dict (catalog row)
```

### Tags

Tags are object-level string slugs. System tags (e.g., `lrd`, `blagn`, `lae`, `hae`, `qg`) are seeded for everyone; users can also create private or shared tags via the [web portal](/nirspec/tags). Tags replace the old `object_flags` bitmask system.

```python
tags = cf.get_tags()
# Table with columns: slug, name, member_count

# Filter by tag (object-level)
results = cf.query_objects(tags=['lrd', 'blagn'])
```

Tags also surface as `Object.tags` on the dataclass returned by `cf.get_object()`.

---

## Working with an Object

`cf.get_object()` returns an `Object` — a typed dataclass with the spectra collection and photometry pre-attached. The dataclass tree is `Object → SpectrumCollection → Spectrum → SpectrumData`, with `Photometry` hanging off `Object`.

### `Object`

```python
obj = cf.get_object('ember_uds_p4_123456')
print(obj)
# Object(ember_uds_p4_123456, z=3.4210, cosmos)
#   3 spectra (G140M, G395M, PRISM)
#   tags: lrd
#   Photometry(8 bands, UVCANDELS, photo_z=3.51)
```

| Attribute | Type | Description |
|---|---|---|
| `object_id`, `field`, `ra`, `dec` | — | Identification and position |
| `redshift`, `redshift_auto`, `redshift_inspected` | float | Best, automated, and inspected redshifts |
| `redshift_quality` | int | 0 (not inspected), 1 (impossible), 2 (tentative), 3 (probable), 4 (secure) |
| `programs`, `tags` | list[str] | Program slugs and tag slugs |
| `n_spectra`, `max_snr`, `max_exposure_time` | — | Cross-spectrum aggregates |
| `has_photometry`, `photo_z`, `photo_z_err_lo/hi` | — | Photometry summary |
| `spectra` | `SpectrumCollection` | All spectra for this object |
| `photometry` | `Photometry` or `None` | Broadband photometric measurements |

### `SpectrumCollection`

Numpy-style filterable container of `Spectrum` objects.

```python
# Integer indexing → single Spectrum
first = obj.spectra[0]

# Boolean masking on attribute arrays
prism = obj.spectra[obj.spectra.grating == 'PRISM']
high_snr = obj.spectra[obj.spectra.signal_to_noise > 10]

# Properties return numpy arrays (or list[str] for the unique-gratings helper)
obj.spectra.spectrum_id      # np.ndarray[str]
obj.spectra.signal_to_noise  # np.ndarray[float]
obj.spectra.gratings         # sorted list of unique gratings

# Convert / iterate
tbl = obj.spectra.to_table()
for s in obj.spectra:
    print(s.spectrum_id, s.grating, s.downloaded)
```

### `Spectrum`

A single catalog row plus an attached opener. Call `.open()` (or `.data`) to load the FITS arrays as a [`SpectrumData`](#spectrumdata).

```python
s = obj.spectra[0]

s.spectrum_id, s.grating, s.signal_to_noise, s.dq_flags, s.downloaded

spec = s.open()       # SpectrumData
s.plot()              # quick-look matplotlib (alias for s.open().plot())
```

### `Photometry`

Parallel arrays for broadband photometry, with single-band lookup by name.

```python
phot = obj.photometry

phot.bands                 # ['f115w', 'f150w', 'f277w', 'f444w', ...]
phot.flux                  # np.ndarray[float], μJy
phot.flux_err              # np.ndarray[float], μJy
phot.wavelength            # np.ndarray[float], μm
phot.catalog               # source catalog name
phot.photo_z               # photometric redshift (or None)

# Single band → namedtuple(flux, flux_err, wavelength)
band = phot['f444w']
band.flux, band.flux_err, band.wavelength

phot.to_table()
```

---

## Opening spectra

### `open_spectrum()`

Open a spectrum as a `SpectrumData` with wavelength/flux arrays. Uses local FITS first; falls back to the API and caches the file in the data directory so subsequent calls are instant.

```python
spec = cf.open_spectrum(spectrum_id)
```

### `SpectrumData`

| Attribute | Type | Description |
|---|---|---|
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

```python
spec = cf.open_spectrum('ember_uds_p4_prism_clear_123456')
print(spec)
# SpectrumData(ember_uds_p4_prism_clear_123456, PRISM, 1024 pixels, 0.60-5.30 μm)

import matplotlib.pyplot as plt
spec.plot(flux_unit='fnu')      # or flux_unit='flam'

# Or plot by hand
plt.step(spec.wavelength, spec.fnu, where='mid')

# Header access
spec.header.get('EXPTIME')
```

You can also open any pipeline-format FITS file directly:

```python
from campfire import SpectrumData
spec = SpectrumData.from_fits('/path/to/local/file.fits')
```

---

## Imaging — cutouts and shutters

`plot_cutout()` is the high-level entry point; for full control, fetch the cutout PNG and shutter geometry yourself and pass them to `campfire.imaging.plot_cutout`. Cutout images and shutter JSON are cached locally after the first fetch.

### `plot_cutout()`

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(5, 5))
cf.plot_cutout('ember_uds_p4_123456', fov=3.2, ax=ax)
fig.savefig('cutout.pdf')   # vector shutter overlay preserved in PDF
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `object_id` | str | — | Object ID |
| `fov` | float | 5.0 | Field of view in arcseconds |
| `size` | int | None | Output size in pixels (default: native resolution) |
| `shutters` | bool/str | True | `True` or `'all'`: all shutters. `'target'`: target only. `False`: none. |
| `ax` | Axes | None | Matplotlib axes (uses `plt.gca()` if None) |
| `shutter_style` | dict | None | Per-category style overrides |
| `scalebar` | bool | True | Draw a scalebar |

Per-category styling — categories are `'target'`, `'other'`, `'stuck_closed'`. Partial overrides are merged with defaults. `marker` controls shape: `'box'` (default) or `'corners'` (JADES-style L-shaped marks).

```python
cf.plot_cutout('obj_id', fov=3.2, ax=ax, shutter_style={
    "target": {"marker": "corners", "edgecolor": "cyan"},
    "other":  {"marker": "corners", "edgecolor": "white", "linewidth": 0.5},
})
```

See [recipe 5](/docs/api/recipes#5-publication-quality-cutout) for a worked example with three styling variants side by side.

### `get_cutout()` and `get_shutters()`

Return cached file paths / dicts without plotting.

```python
from campfire.imaging import plot_cutout

path = cf.get_cutout('obj_id', fov=3.2)       # cached PNG path
data = cf.get_shutters('obj_id', fov=3.2)     # cached JSON dict
plot_cutout(path, shutters=data, object_id='obj_id', fov=3.2, ax=ax)
```

---

## Calibration and stacking

`campfire.calibration` flux-calibrates spectra against broadband photometry and combines multiple spectra onto a common wavelength grid. Requires the extras from `pip install "campfire[deploy]"` (scipy, matplotlib).

### `calibrate_to_photometry()`

Fit a smooth correction curve so synthetic photometry from the spectrum matches the observed bands.

```python
from campfire import calibrate_to_photometry

result = calibrate_to_photometry(
    spec,                     # Spectrum or SpectrumData
    obj.photometry,           # Photometry
    method='chebyshev',       # 'chebyshev' (default) or 'flat'
    # bands=['f150w', 'f277w', 'f444w'],   # optional; auto-selected by default
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

Resample multiple spectra onto a common wavelength grid and combine.

```python
from campfire import stack_spectra

stacked = stack_spectra(
    [s.open() for s in prism],
    method='weighted_mean',   # 'weighted_mean' | 'median' | 'mean'
    # wavelength_grid=None,   # default: grid from the input with the most pixels
    object_id=obj.object_id,
)
stacked.plot()
```

### `calibrate_and_stack()`

Convenience wrapper: per-spectrum calibration → resample → stack, in one call. Accepts a `SpectrumCollection`, a list of `Spectrum`, or a list of `SpectrumData`. See [recipe 3](/docs/api/recipes#3-calibrate-and-stack-a-sample) for a worked example.

```python
from campfire import calibrate_and_stack

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

## Plotting helpers

CAMPFIRE includes Plotly-based plotting functions for interactive use. Requires the `plotting` extra (`pip install "campfire[plotting]"`).

```python
from campfire import plot_spectrum, plot_redshift_fit, plot_spectrum_simple

cf = Campfire()
spectrum_id = 'ember_uds_p4_prism_clear_123456'
data = cf.get_spectrum_data(spectrum_id)

# Multi-panel: 2D heatmap + profile + 1D spectrum
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

`get_spectrum_data(spectrum_id)` returns `{wave, fnu, fnu_err, snr_2d, n_spatial, n_wave, profile, profile_fit, profile_pix}`. `get_redshift_fit_data(spectrum_id)` returns `{redshift, chi2_min, confidence, z_grid, chi2_grid, model_wave, model_fnu}`.

### Helper functions

```python
from campfire import convert_flux_units, get_emission_lines, EMISSION_LINES

flambda = convert_flux_units(fnu, wavelength, to_unit='flambda')
lines = get_emission_lines(redshift=2.5, wave_min=1.0, wave_max=5.0)
```

---

## Flag filtering

CAMPFIRE uses bitmask flags for data quality. The Python client provides numpy-style operators for intuitive filtering.

### Operators

```python
from campfire.flags import DQFlags

# OR: match any of these flags
DQFlags.CHIP_GAP | DQFlags.LOW_SNR

# AND: must have all of these flags
DQFlags.CHIP_GAP & DQFlags.LOW_SNR

# NOT: exclude this flag
~DQFlags.CONTAMINATION
```

To exclude multiple flags, AND the negations: `~DQFlags.CONTAMINATION & ~DQFlags.LOW_SNR`. The form `~(A | B)` is not supported.

### Examples

```python
# Clean spectra only (dq_flags is per-spectrum)
results = cf.query_spectra(
    dq_flags=~DQFlags.CONTAMINATION & ~DQFlags.LOW_SNR,
)

# Or, filter objects by their spectra's DQ flags
objects = cf.query_objects(
    dq_flags=~DQFlags.CONTAMINATION & ~DQFlags.LOW_SNR,
)
```

See the [Flags documentation](/docs/inspection/flags) for full flag definitions and values.

### Utilities

```python
from campfire import list_flags, decode_flags, encode_flags

list_flags()                                          # Print all flags
list_flags('dq_flags')                                # Print specific type
decode_flags(3, 'dq_flags')                           # ['CHIP_GAP', 'CONTAMINATION']
encode_flags(['CHIP_GAP', 'CONTAMINATION'], 'dq_flags')  # 3
```

---

## Metadata accessors

```python
cf.get_metadata()      # {'programs': [...], 'fields': [...], 'gratings': [...], 'observations': [...]}
cf.get_programs()      # astropy Table: slug, program_name, pi_name, is_public
cf.get_fields()        # ['cosmos', 'uds', ...]
cf.get_gratings()      # ['PRISM', 'G395M', ...]
cf.get_observations()  # ['ember_uds_p4', ...]
cf.get_tags()          # astropy Table: slug, name, member_count
```

---

## Error handling

```python
from campfire import (
    CampfireError,       # Base exception
    AuthenticationError, # Invalid/expired credentials
    NotFoundError,       # Object or spectrum not found
    DownloadError,       # File download failed
    ValidationError,     # Invalid parameters
    APIError,            # Unexpected API error
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
