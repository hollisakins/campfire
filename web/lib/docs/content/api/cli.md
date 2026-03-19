# CLI Reference

The `campfire` command-line tool manages authentication, bulk downloads, and local data. After installing the Python package, the `campfire` command is available in your terminal.

## Quick Start

```bash
campfire login                     # Authenticate
campfire add ember_uds_p4          # Track an observation
campfire sync                      # Download everything
campfire status                    # Check what you have
```

---

## Observation Management

### `campfire observations`

List all available observations with stats.

```bash
campfire observations              # List all observations
campfire observations --tracked    # Only show tracked observations
campfire observations --json       # JSON output for scripting
```

**Output:**

```
  OBSERVATION               PROGRAM      FIELD       OBJECTS  SPECTRA       SIZE   STATUS
  ember_uds_p4              EMBER-UDS    UDS             450     1350     2.1 GB   tracked (synced)
  capers_cosmos_p1          CAPERS       COSMOS          320      960     1.5 GB   not tracked
```

### `campfire add`

Track observations for syncing. Tracked observations are downloaded when you run `campfire sync`.

```bash
campfire add ember_uds_p4                    # Track one observation
campfire add ember_uds_p4 capers_cosmos_p1   # Track multiple
campfire add --all                            # Track everything
```

### `campfire remove`

Stop tracking an observation.

```bash
campfire remove ember_uds_p4                 # Stop tracking (keep files)
campfire remove ember_uds_p4 --delete        # Stop tracking and delete files
campfire remove ember_uds_p4 --delete --yes  # Skip confirmation
```

---

## Syncing Data

### `campfire sync`

Download and update all tracked observations. Sync is incremental: only new or changed files are downloaded.

```bash
campfire sync                                # Download everything
campfire sync --dry-run                      # Show what would be downloaded
campfire sync --observation ember_uds_p4     # Sync specific observation
campfire sync --workers 8                    # Use 8 parallel download workers
campfire sync --yes                          # Skip confirmation prompt
```

**What sync does:**

1. Fetches a download manifest for each tracked observation
2. Compares against your local files (using SHA-256 hashes)
3. Downloads new and updated FITS files in parallel
4. Updates the local metadata database (SQLite)
5. Regenerates `objects.csv` and `spectra.csv` catalogs

**Output:**

```
Checking tracked observations...
  ember_uds_p4: 12 new (450.2 MB)
  capers_cosmos_p1: up to date

Download 12 files (450.2 MB)?
Proceed? [Y/n]: y

ember_uds_p4: 100%|██████████| 12/12 [00:45<00:00]

Updating catalog...
  Catalog updated: objects.csv (770 objects), spectra.csv (2310 spectra)

✓ Sync complete
  Files downloaded: 12
  Total size: 450.2 MB
```

### `campfire status`

Check credentials, tracked observations, and disk usage.

```bash
campfire status
```

**Output:**

```
✓ Credentials valid
  User: user@example.com

Data directory: /Users/you/.campfire/data

Tracked observations:
  OBSERVATION               SYNCED       SIZE         LAST SYNC
  ember_uds_p4              1350         2.1 GB       2026-03-15 14:30
  capers_cosmos_p1          960          1.5 GB       2026-03-14 09:15

Catalog: objects.csv (770 objects), spectra.csv (2310 spectra)
Disk usage: 3.6 GB
```

---

## Local Data Layout

After syncing, your data directory looks like:

```
~/.campfire/
├── credentials              # OAuth tokens or API key (0600 permissions)
├── config.toml              # Settings (base_url, data_dir, tracked observations)
└── data/
    ├── .campfire_meta/
    │   ├── campfire.db      # SQLite database (objects + spectra metadata)
    │   ├── objects.csv      # Object catalog (for pandas/astropy)
    │   └── spectra.csv      # Spectra catalog (for pandas/astropy)
    ├── ember_uds_p4/
    │   ├── ember_uds_p4_PRISM_CLEAR_123456_spec.fits
    │   ├── ember_uds_p4_G395M_F290LP_123456_spec.fits
    │   └── ...
    └── capers_cosmos_p1/
        └── ...
```

### CSV Catalogs

The CSV catalogs are regenerated after each sync. They're designed for direct use with astropy or pandas:

```python
from astropy.table import Table

objects = Table.read('~/.campfire/data/.campfire_meta/objects.csv')
spectra = Table.read('~/.campfire/data/.campfire_meta/spectra.csv')

# Filter locally
high_z = objects[objects['redshift'] > 3.0]
print(f"Found {len(high_z)} high-z objects")
```

**objects.csv columns:**

| Column | Type | Description |
|--------|------|-------------|
| `object_id` | str | Unique identifier |
| `program_slug` | str | Program identifier |
| `program_name` | str | Program display name |
| `field` | str | Field name (COSMOS, UDS, EGS) |
| `observation` | str | Observation name |
| `ra`, `dec` | float | Coordinates (J2000, degrees) |
| `redshift` | float | Best redshift (inspected or auto) |
| `redshift_auto` | float | Pipeline redshift |
| `redshift_inspected` | float | Manually inspected redshift |
| `redshift_quality` | int | Quality (0=none, 1=impossible, 2=tentative, 3=probable) |
| `spectral_features` | int | Bitmask (see [Flags](/docs/inspection/flags)) |
| `object_flags` | int | Bitmask (see [Flags](/docs/inspection/flags)) |
| `dq_flags` | int | Bitmask (see [Flags](/docs/inspection/flags)) |
| `max_snr` | float | Maximum signal-to-noise ratio |

**spectra.csv columns:**

| Column | Type | Description |
|--------|------|-------------|
| `spectra_id` | int | Unique spectrum ID |
| `object_id` | str | Parent object ID |
| `grating` | str | Grating (PRISM, G140M, G235M, G395M, etc.) |
| `fits_path` | str | Remote FITS file path |
| `file_hash` | str | SHA-256 hash for integrity |
| `file_size` | int | File size in bytes |
| `signal_to_noise` | float | Spectrum S/N |
| `local_path` | str | Relative path to local FITS file |

---

## Global Options

All commands support these options:

| Option | Description |
|--------|-------------|
| `--base-url URL` | Override API URL (for development) |
| `--version` | Show version |
| `--help` | Show help |

## Configuration

Settings are stored in `~/.campfire/config.toml`:

```toml
[settings]
base_url = "https://campfire.hollisakins.com/api/v1"
data_dir = "/Users/you/.campfire/data"

[observations]
tracked = ["ember_uds_p4", "capers_cosmos_p1"]
```

You can edit this file directly or use the CLI commands to manage it.
