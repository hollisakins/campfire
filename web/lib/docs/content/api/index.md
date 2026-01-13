# API Reference

CAMPFIRE provides programmatic access to NIRSpec spectroscopic data through a Python client library and REST API.

## Installation

Install the Python client using pip:

```bash
pip install campfire
```

For plotting functionality, install with optional dependencies:

```bash
pip install campfire[plotting]
```

---

## Authentication

Before using the API, you must authenticate. CAMPFIRE supports two authentication methods:

### Browser Login (Recommended)

The recommended method opens a browser for secure OAuth authentication:

```bash
campfire login
```

This will:
1. Open your browser to the CAMPFIRE login page
2. After you log in, credentials are saved to `~/.campfire/credentials`
3. Tokens are automatically refreshed when they expire

### API Key Login

For headless environments (servers, HPC clusters), use an API key:

```bash
campfire login --api-key
```

Generate API keys from your [Profile page](/profile/api-keys). Keys start with `sk_`.

### CLI Commands

| Command | Description |
|---------|-------------|
| `campfire login` | Authenticate with CAMPFIRE |
| `campfire logout` | Remove stored credentials |
| `campfire whoami` | Show current authenticated user |
| `campfire status` | Check if credentials are valid |

---

## Python Client

### Quick Start

```python
from campfire import Campfire

# Initialize client (uses stored credentials)
cf = Campfire()

# Query high-redshift galaxies
results = cf.query_objects(
    redshift_range=(3.0, 6.0),
    redshift_quality=[2, 3],
    limit=100
)

# Download FITS files
for row in results:
    for spectrum in row['spectra']:
        cf.download_spectrum(spectrum['fits_path'])
```

### Campfire Class

```python
class Campfire(base_url=None, auto_refresh=True)
```

The main client for interacting with the CAMPFIRE API.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | str | None | API base URL. Uses `CAMPFIRE_API_URL` env var or production server. |
| `auto_refresh` | bool | True | Automatically refresh OAuth tokens when they expire. |

**Example:**

```python
from campfire import Campfire

# Default: production server with auto-refresh
cf = Campfire()

# Custom server (for development)
cf = Campfire(base_url="http://localhost:3000/api/v1")
```

---

### Querying Objects

#### `query_objects()`

Query the spectroscopic database with filters.

```python
cf.query_objects(
    programs=None,           # list[int|str]: Program IDs or names
    fields=None,             # list[str]: Field names (e.g., ['COSMOS', 'UDS'])
    gratings=None,           # list[str]: Grating types (e.g., ['PRISM', 'G395M'])
    observations=None,       # list[str]: Observation names
    redshift_range=None,     # tuple[float, float]: (min, max) redshift
    redshift_quality=None,   # list[int]: Quality codes to include
    max_snr_range=None,      # tuple[float, float]: (min, max) SNR range
    spectral_features=None,  # Flag filter (see Flag Filtering section)
    object_flags=None,       # Flag filter (see Flag Filtering section)
    dq_flags=None,           # Flag filter (see Flag Filtering section)
    inspected_only=None,     # bool: Only return inspected objects
    search=None,             # str: Text search on object_id
    cone_search=None,        # tuple[float, float, float]: (ra, dec, radius_arcsec)
    limit=1000,              # int: Maximum results
    offset=0,                # int: Pagination offset
    sort='object_id',        # str: Sort column
    sort_dir='asc'           # str: 'asc' or 'desc'
)
```

**Returns:** `astropy.table.Table` with columns including:
- `object_id`: Unique identifier
- `ra`, `dec`: Coordinates (degrees)
- `redshift`: Best redshift estimate
- `redshift_quality`: Quality code (0-3)
- `field`: Field name
- `program_id`: Program ID
- `spectra`: List of available spectra with `grating`, `fits_path`
- `spectral_features`, `object_flags`, `dq_flags`: Bitmask flags

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

# Text search by object ID
results = cf.query_objects(search='ember_uds')

# Pagination for large queries
all_results = []
offset = 0
while True:
    batch = cf.query_objects(limit=1000, offset=offset)
    if len(batch) == 0:
        break
    all_results.append(batch)
    offset += 1000
```

---

### Flag Filtering

CAMPFIRE uses bitmask flags for spectral features, object classifications, and data quality. The Python client provides a powerful query interface using numpy-style operators.

#### Flag Types

**SpectralFeatures** - Features used for redshift determination:

| Flag | Value | Description |
|------|-------|-------------|
| `CONTINUUM_BREAK` | 1 | Redshift from continuum shape |
| `LYMAN_BREAK` | 2 | Clear Lyman break |
| `BALMER_BREAK` | 4 | Clear Balmer break |
| `ABSORPTION_FEATURES` | 8 | Absorption lines identified |
| `SINGLE_EMISSION` | 16 | Single emission line |
| `MULTI_EMISSION` | 32 | Multiple emission lines |

**ObjectFlags** - Object classifications:

| Flag | Value | Description |
|------|-------|-------------|
| `LRD` | 1 | Little Red Dot |
| `BROAD_LINE` | 2 | Broad emission line (AGN) |
| `LYA_EMITTER` | 4 | Strong Lyman-alpha emission |
| `BALMER_BREAK_GALAXY` | 8 | Strong Balmer break |
| `OIII_EMITTER` | 16 | Strong [OIII] emission |
| `HA_EMITTER` | 32 | Strong H-alpha emission |
| `PASSIVE` | 64 | Quiescent galaxy |
| `DUSTY` | 128 | Dust-attenuated |
| `STAR` | 256 | Stellar spectrum |

**DQFlags** - Data quality issues:

| Flag | Value | Description |
|------|-------|-------------|
| `CHIP_GAP` | 1 | Affected by detector gap |
| `CONTAMINATION` | 2 | Nearby source contamination |
| `STUCK_SHUTTER` | 4 | Possible stuck shutter |
| `MULTIPLE_SOURCES` | 8 | Multiple sources in slitlet |
| `NO_DETECTION` | 16 | No source detected |
| `LOW_SNR` | 32 | Low signal-to-noise |
| `SPECTRAL_OVERLAP` | 64 | Spectral overlap |
| `PRISM_CORRUPTED` | 128 | PRISM data corrupted |
| `GRATING_CORRUPTED` | 256 | Grating data corrupted |

#### Query Operators

```python
from campfire.flags import ObjectFlags, DQFlags, SpectralFeatures

# OR: Match any of these flags
ObjectFlags.LRD | ObjectFlags.LYA_EMITTER

# AND: Must have all these flags
ObjectFlags.LRD & ObjectFlags.BROAD_LINE

# NOT: Exclude this flag
~DQFlags.CONTAMINATION

# Combined expressions
(ObjectFlags.LRD | ObjectFlags.LYA_EMITTER) & ~ObjectFlags.BROAD_LINE
```

**Examples:**

```python
from campfire import Campfire
from campfire.flags import ObjectFlags, DQFlags, SpectralFeatures

cf = Campfire()

# Find LRDs or LAEs, excluding broad-line AGN
results = cf.query_objects(
    object_flags=(ObjectFlags.LRD | ObjectFlags.LYA_EMITTER) & ~ObjectFlags.BROAD_LINE
)

# Objects with multiple emission lines and good data quality
results = cf.query_objects(
    spectral_features=SpectralFeatures.MULTI_EMISSION,
    dq_flags=~(DQFlags.CONTAMINATION | DQFlags.LOW_SNR)
)

# Simple string-based filtering (matches web interface behavior)
results = cf.query_objects(object_flags=['LRD', 'LYA_EMITTER'])  # Match any
```

#### Utility Functions

```python
from campfire import list_flags, decode_flags, encode_flags

# Print all available flags
list_flags()

# Print flags of specific type
list_flags('object_flags')

# Decode bitmask to flag names
decode_flags(5, 'object_flags')  # ['LRD', 'LYA_EMITTER']

# Encode flag names to bitmask
encode_flags(['LRD', 'LYA_EMITTER'], 'object_flags')  # 5
```

---

### Downloading Spectra

#### `download_spectrum()`

Download a single FITS file.

```python
cf.download_spectrum(
    fits_path,              # str: FITS path from query results
    output_path=None,       # str|Path: Local save path (default: basename)
    overwrite=False,        # bool: Overwrite existing files
    show_progress=True      # bool: Show progress bar
)
```

**Returns:** `str` - Path to downloaded file.

**Example:**

```python
results = cf.query_objects(search='ember_uds_p4_123')

for row in results:
    for spectrum in row['spectra']:
        path = cf.download_spectrum(
            spectrum['fits_path'],
            output_path=f"./data/{spectrum['grating']}_{row['object_id']}.fits"
        )
        print(f"Downloaded: {path}")
```

#### `download_spectra()`

Download multiple spectra from query results.

```python
cf.download_spectra(
    object_ids=None,        # str|list[str]: Filter by object IDs
    table=None,             # Table: Query results table (required)
    download_dir='.',       # str|Path: Output directory
    gratings=None,          # list[str]: Only download specific gratings
    overwrite=False,        # bool: Overwrite existing files
    show_progress=True      # bool: Show progress
)
```

**Returns:** `dict` - Mapping of `{object_id: {grating: filepath}}`.

**Example:**

```python
# Query and download all PRISM spectra
results = cf.query_objects(
    programs=['EMBER-UDS'],
    redshift_range=(2.0, 4.0),
    limit=50
)

paths = cf.download_spectra(
    table=results,
    download_dir='./ember_spectra/',
    gratings=['PRISM']
)

# Download specific objects
paths = cf.download_spectra(
    object_ids=['ember_uds_p4_123', 'ember_uds_p4_456'],
    table=results,
    download_dir='./selected/'
)
```

---

### Metadata Methods

#### `get_metadata()`

Get all available filter options in a single call.

```python
meta = cf.get_metadata()
# Returns: {'programs': [...], 'fields': [...], 'gratings': [...], 'observations': [...]}
```

#### `get_programs()`

List available programs with metadata.

```python
programs = cf.get_programs()
# Returns: Table with columns: program_id, program_name, pi_name, is_public
```

#### `get_fields()`

List available field names.

```python
fields = cf.get_fields()
# Returns: ['COSMOS', 'UDS', ...]
```

#### `get_gratings()`

List available grating types.

```python
gratings = cf.get_gratings()
# Returns: ['PRISM', 'G395M', ...]
```

#### `get_observations()`

List available observation names.

```python
observations = cf.get_observations()
# Returns: ['ember_uds_p4', 'capers_cosmos_p1', ...]
```

---

### Spectrum Data Methods

These methods return JSON data suitable for plotting, without downloading FITS files.

#### `get_spectrum_data()`

Fetch spectrum data for plotting.

```python
data = cf.get_spectrum_data(
    object_id,    # str: Object ID
    grating       # str: Grating type (e.g., 'PRISM')
)
```

**Returns:** `dict` with keys:
- `wave`: Wavelength array (microns)
- `fnu`: Flux density (microJy)
- `fnu_err`: Flux uncertainty
- `snr_2d`: 2D S/N array [spatial, wavelength]
- `n_spatial`, `n_wave`: Array dimensions
- `profile`, `profile_fit`, `profile_pix`: Cross-dispersion profile data

#### `get_redshift_fit_data()`

Fetch redshift fitting results.

```python
fit = cf.get_redshift_fit_data(
    object_id,    # str: Object ID
    grating       # str: Grating type
)
```

**Returns:** `dict` with keys:
- `redshift`: Best-fit redshift
- `chi2_min`: Minimum chi-squared
- `confidence`: Confidence percentage
- `z_grid`: Redshift grid
- `chi2_grid`: Chi-squared values
- `model_wave`, `model_fnu`: Best-fit model spectrum

---

### Plotting

CAMPFIRE includes Plotly-based plotting functions that match the web interface.

```python
from campfire import Campfire, plot_spectrum, plot_redshift_fit, plot_spectrum_simple
```

#### `plot_spectrum()`

Create a multi-panel spectrum plot with 2D S/N heatmap and cross-dispersion profile.

```python
from campfire import Campfire, plot_spectrum

cf = Campfire()
data = cf.get_spectrum_data('ember_uds_p4_123456', 'PRISM')

fig = plot_spectrum(
    data,
    redshift=2.5,              # float: Redshift for emission lines
    flux_unit='fnu',           # 'fnu' (microJy) or 'flambda' (erg/s/cm2/A)
    show_errors=True,          # Show 1-sigma error band
    show_emission_lines=True,  # Show emission line markers
    colormap='viridis',        # 2D heatmap colormap
    snr_range=(-5, 10),        # S/N colorbar range
    title='My Spectrum'
)
fig.show()
```

#### `plot_redshift_fit()`

Plot redshift fitting results with chi-squared curve.

```python
from campfire import Campfire, plot_redshift_fit

cf = Campfire()
fit = cf.get_redshift_fit_data('ember_uds_p4_123456', 'PRISM')
spec = cf.get_spectrum_data('ember_uds_p4_123456', 'PRISM')

fig = plot_redshift_fit(
    fit,
    spectrum_data=spec,        # Optional: overlay observed spectrum
    show_emission_lines=True
)
fig.show()
```

#### `plot_spectrum_simple()`

Lightweight 1D spectrum plot without 2D heatmap.

```python
from campfire import Campfire, plot_spectrum_simple

cf = Campfire()
data = cf.get_spectrum_data('ember_uds_p4_123456', 'PRISM')

fig = plot_spectrum_simple(
    data,
    redshift=2.5,
    show_emission_lines=True
)
fig.show()
```

#### Helper Functions

```python
from campfire import convert_flux_units, get_emission_lines, EMISSION_LINES

# Convert flux units
flambda = convert_flux_units(fnu, wavelength, to_unit='flambda')

# Get emission lines at a redshift
lines = get_emission_lines(redshift=2.5, wave_min=1.0, wave_max=5.0)
# Returns: [{'name': 'Hα', 'rest_wave': 0.6563, 'observed_wave': 2.297, 'color': '#eab308'}, ...]

# Access all emission line definitions
print(EMISSION_LINES)
```

---

## REST API

For direct HTTP access without the Python client.

### Authentication

All API requests require authentication via the `Authorization` header:

```bash
# Using API key
curl -H "Authorization: Bearer sk_your_api_key" \
  https://campfire.hollisakins.com/api/v1/objects

# Using JWT access token (from device flow)
curl -H "Authorization: Bearer eyJ..." \
  https://campfire.hollisakins.com/api/v1/objects
```

### Endpoints

#### GET /api/v1/objects

Query objects with filters.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `programs` | string | Comma-separated program IDs |
| `fields` | string | Comma-separated field names |
| `gratings` | string | Comma-separated grating types |
| `observations` | string | Comma-separated observation names |
| `redshift_min` | float | Minimum redshift |
| `redshift_max` | float | Maximum redshift |
| `redshift_quality` | string | Comma-separated quality codes |
| `max_snr_min` | float | Minimum max SNR |
| `max_snr_max` | float | Maximum max SNR |
| `spectral_features` | int | Bitmask (match any) |
| `spectral_features_include_any` | int | Match any of these flags |
| `spectral_features_include_all` | int | Must have all flags |
| `spectral_features_exclude` | int | Must not have any flags |
| `object_flags` | int | Bitmask (same pattern as above) |
| `object_flags_include_any` | int | |
| `object_flags_include_all` | int | |
| `object_flags_exclude` | int | |
| `dq_flags` | int | Bitmask (same pattern as above) |
| `dq_flags_include_any` | int | |
| `dq_flags_include_all` | int | |
| `dq_flags_exclude` | int | |
| `inspected_only` | boolean | Filter to inspected objects |
| `search` | string | Text search on object_id |
| `ra` | float | RA for cone search (degrees) |
| `dec` | float | Dec for cone search (degrees) |
| `radius` | float | Search radius (arcsec) |
| `limit` | int | Max results (default: 1000) |
| `offset` | int | Pagination offset |
| `sort` | string | Sort column |
| `sort_dir` | string | 'asc' or 'desc' |

**Response:**

```json
{
  "data": [
    {
      "object_id": "ember_uds_p4_123456",
      "ra": 34.1234,
      "dec": -5.4567,
      "redshift": 2.345,
      "redshift_quality": 3,
      "field": "UDS",
      "program_id": 1234,
      "spectra": [
        {"grating": "PRISM", "fits_path": "spectra/ember_uds_p4/..."}
      ]
    }
  ],
  "pagination": {
    "total": 1500,
    "limit": 1000,
    "offset": 0
  }
}
```

#### GET /api/v1/spectra

Get a signed URL for downloading a FITS file.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | FITS file path (required) |
| `redirect` | boolean | If 'true', redirects to signed URL |

**Response:**

```json
{
  "url": "https://..."
}
```

#### GET /api/v1/spectrum

Get spectrum JSON data for plotting.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `object_id` | string | Object ID |
| `grating` | string | Grating type |

**Response:**

```json
{
  "wave": [1.0, 1.1, ...],
  "fnu": [0.5, 0.6, ...],
  "fnu_err": [0.1, 0.1, ...],
  "snr_2d": [[...], [...]],
  "n_spatial": 10,
  "n_wave": 500,
  "profile": [...],
  "profile_fit": [...],
  "profile_pix": [...]
}
```

#### GET /api/v1/redshift-fit

Get redshift fitting results.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `object_id` | string | Object ID |
| `grating` | string | Grating type |

**Response:**

```json
{
  "redshift": 2.345,
  "chi2_min": 1.23,
  "confidence": 95.5,
  "z_grid": [0.0, 0.1, ...],
  "chi2_grid": [100, 95, ...],
  "model_wave": [...],
  "model_fnu": [...]
}
```

#### GET /api/v1/metadata

Get available filter options.

**Response:**

```json
{
  "programs": [
    {"program_id": 1234, "program_name": "EMBER-UDS", "pi_name": "...", "is_public": false}
  ],
  "fields": ["COSMOS", "UDS"],
  "gratings": ["PRISM", "G395M"],
  "observations": ["ember_uds_p4", ...]
}
```

#### GET /api/v1/auth/whoami

Get current user information.

**Response:**

```json
{
  "user_id": "uuid",
  "email": "user@example.com",
  "full_name": "User Name",
  "created_at": "2024-01-01T00:00:00Z"
}
```

### Device Flow Authentication

For CLI/desktop applications:

#### POST /api/v1/auth/device

Initiate device authorization.

**Response:**

```json
{
  "device_code": "...",
  "user_code": "WDJB-MJPQ",
  "verification_uri": "https://campfire.hollisakins.com/cli-auth",
  "verification_uri_complete": "https://campfire.hollisakins.com/cli-auth?code=WDJB-MJPQ",
  "expires_in": 900,
  "interval": 5
}
```

#### POST /api/v1/auth/device/token

Poll for tokens after user authorization.

**Request:**

```json
{
  "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
  "device_code": "..."
}
```

**Response (success):**

```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "rt_..."
}
```

**Response (pending):**

```json
{"error": "authorization_pending"}
```

#### POST /api/v1/auth/refresh

Refresh an access token.

**Request:**

```json
{
  "grant_type": "refresh_token",
  "refresh_token": "rt_..."
}
```

**Response:**

```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "rt_..."
}
```

---

## Error Handling

The Python client raises specific exceptions:

```python
from campfire import (
    CampfireError,      # Base exception
    AuthenticationError, # Invalid/expired credentials
    NotFoundError,       # Resource not found
    DownloadError,       # File download failed
    ValidationError,     # Invalid input
    APIError            # Unexpected API error
)

try:
    cf = Campfire()
    results = cf.query_objects()
except AuthenticationError:
    print("Please run: campfire login")
except NotFoundError as e:
    print(f"Not found: {e}")
except CampfireError as e:
    print(f"API error: {e}")
```

### HTTP Status Codes

| Code | Description |
|------|-------------|
| 200 | Success |
| 400 | Bad request (invalid parameters) |
| 401 | Unauthorized (invalid/missing authentication) |
| 403 | Forbidden (no access to resource) |
| 404 | Not found |
| 429 | Rate limited |
| 500 | Server error |

---

## Rate Limits

API requests are rate-limited to ensure fair usage:

- **Standard users:** 100 requests/minute
- **Burst:** Up to 10 concurrent requests

If rate limited, wait and retry. The Python client does not automatically handle rate limiting.
