# Programmatic Access

CAMPFIRE provides a Python package for querying, downloading, and analyzing NIRSpec spectroscopic data. It includes a **CLI** for bulk data management and a **Python client** for interactive analysis in notebooks.

## Installation

Install the Python client using pip:

```bash
pip install campfire
```

For plotting functionality, install with optional dependencies:

```bash
pip install campfire[plotting]
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

### CLI: Bulk Download and Local Catalog

For astronomers who want to download data and work with their own tools. The CLI maintains a local mirror of tracked observations with FITS files and CSV catalogs.

```bash
campfire add ember_uds_p4         # Track an observation
campfire sync                      # Download all tracked data
```

After syncing, you have:
- FITS files organized by observation in `~/.campfire/data/`
- `objects.csv` and `spectra.csv` catalogs ready for pandas/astropy
- A SQLite database for the Python client to query locally

See the [CLI Reference](/docs/api/cli) for the full command reference.

### Python Client: Interactive Notebook Workflows

For exploratory analysis, filtering, and plotting. The client automatically uses locally synced data when available, falling back to the remote API.

```python
from campfire import Campfire

cf = Campfire()
results = cf.query_objects(
    redshift_range=(3.0, 6.0),
    redshift_quality=[2, 3]
)

spec = cf.open_spectrum('ember_uds_p4_123456', 'PRISM')
print(spec.wavelength.shape, spec.flux.shape)
```

See the [Python Client](/docs/api/python-client) for the full API reference.

---

## Architecture

```
campfire sync (CLI)
  Downloads FITS files + populates local SQLite database + exports CSVs

Campfire() (Python client)
  Queries local SQLite when data is synced, falls back to remote API
  Opens FITS files from local disk when available
```

The CLI and Python client share the same local data store. When you `campfire sync`, the Python client automatically detects the local data and serves queries from SQLite instead of hitting the remote API. This means instant queries over your synced catalog.
