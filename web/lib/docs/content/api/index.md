# Programmatic Access

CAMPFIRE provides a Python package for querying, downloading, and analyzing NIRSpec spectroscopic data. It includes a **CLI** for bulk data management and a **Python client** for interactive analysis in notebooks.

## Installation

Install the Python client directly from GitHub:

```bash
pip install "git+https://github.com/hollisakins/campfire.git#subdirectory=python/"
```

For plotting functionality, install with optional dependencies:

```bash
pip install "campfire[plotting] @ git+https://github.com/hollisakins/campfire.git#subdirectory=python/"
```

## Authentication

Before using the CLI or Python client, you must authenticate:

### Browser Login (Recommended)

```bash
campfire login
```

This opens your browser for secure OAuth authentication. Credentials are saved to `~/.campfire/credentials` and tokens are automatically refreshed when they expire.

### API Key Login

For headless environments (servers, HPC clusters):

```bash
campfire login --api-key
```

Generate API keys from your [Profile page](/profile/api-keys). Keys start with `sk_`.

### Auth Commands

| Command | Description |
|---------|-------------|
| `campfire login` | Authenticate with CAMPFIRE |
| `campfire logout` | Remove stored credentials |
| `campfire whoami` | Show current authenticated user |
| `campfire status` | Check credentials, sync status, and disk usage |

---

## Two Ways to Access Data

### CLI: Catalog Sync + Bulk Download

For astronomers who want to download data and work with their own tools.

```bash
campfire sync                         # Pull full catalog (metadata only, fast)
campfire download --obs ember_uds_p4  # Download FITS files
```

After syncing, you have `objects.csv` and `spectra.csv` catalogs ready for pandas/astropy. FITS downloads are separate — download only what you need.

See the [CLI Reference](/docs/api/cli) for the full command reference.

### Python Client: Interactive Notebook Workflows

For exploratory analysis, filtering, and plotting. The client queries the local catalog after sync, falling back to the remote API.

```python
from campfire import Campfire

cf = Campfire()
cf.sync()  # Pull full catalog

# Query locally — instant, no network
results = cf.query_objects(
    redshift_range=(3.0, 6.0),
    redshift_quality=['probable', 'secure']
)

# Download FITS for specific observations
cf.download(observations=['ember_uds_p4'], gratings=['PRISM'])

# Open spectrum (local FITS if downloaded, API fallback)
spec = cf.open_spectrum('ember_uds_p4_123456', 'PRISM')
```

See the [Python Client](/docs/api/python-client) for the full API reference.

---

## Architecture

```
campfire sync       → full catalog into SQLite + CSVs (no FITS, fast)
campfire download   → FITS files by observation/program/field/grating

Campfire.sync()     → same as campfire sync
Campfire.download() → same as campfire download
Campfire.query_objects() → queries local SQLite (instant after sync)
Campfire.open_spectrum() → local FITS if downloaded, API fallback
```

The CLI and Python client share the same local data store (defaults to `$CAMPFIRE_ROOT` or `~/campfire`). FITS files go in `products/` (matching the pipeline layout), metadata in `meta/`. Sync the catalog often (it's fast), download spectra only when you need them.
