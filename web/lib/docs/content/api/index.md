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
    redshift_quality=[2, 3],  # 0 none, 1 tentative, 2 probable, 3 secure, 4 gold
)

# Download FITS for specific observations
cf.download(observations=['ember_uds_p4'], gratings=['PRISM'])

# Load an object with its spectra + photometry
obj = cf.get_object('ember_uds_p4_123456')
spec = obj.spectra[0].open()   # SpectrumData

# Or open a spectrum directly by spectrum_id
spec = cf.open_spectrum('ember_uds_p4_prism_clear_123456')
```

See the [Python Client](/docs/api/python-client) for the full API reference.

---

## Architecture

```
campfire sync       → full catalog into SQLite + CSVs (no FITS, fast)
campfire download   → FITS files by observation/program/field/grating

Campfire.sync()          → same as campfire sync
Campfire.download()      → same as campfire download
Campfire.query_objects() → one row per sky position (inspection state)
Campfire.query_spectra() → one row per spectrum (FITS paths, dq_flags)
Campfire.get_object()    → Object with .spectra + .photometry attached
Campfire.open_spectrum() → local FITS if downloaded, API fallback
Campfire.plot_cutout()   → NIRCam RGB cutout with vector shutter overlay
```

The CLI and Python client share the same local data store (defaults to `$CAMPFIRE_ROOT` or `~/campfire`). FITS files go in `products/` (matching the pipeline layout), metadata in `meta/`. Sync the catalog often (it's fast), download spectra only when you need them.
