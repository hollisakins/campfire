# CLI Reference

The `campfire` command-line tool manages authentication, catalog sync, and FITS downloads. After installing the Python package, the `campfire` command is available in your terminal.

## Quick Start

```bash
campfire login                        # Authenticate
campfire sync                         # Sync the full catalog (metadata only)
campfire download --obs ember_uds_p4  # Download FITS files
campfire status                       # Check catalog and download status
```

---

## Catalog Sync

### `campfire sync`

Sync the full object/spectra catalog from the server. This is a **metadata-only** operation — it pulls all accessible observations' metadata into a local SQLite database and regenerates CSV catalogs. No FITS files are downloaded.

```bash
campfire sync
```

**What it does:**

1. Fetches all objects and spectra metadata you have access to
2. Upserts into the local SQLite database
3. Exports `objects.csv` and `spectra.csv` catalogs
4. Detects stale local files (FITS files that have been reprocessed on the server)

**Output:**

```
Syncing catalog...
✓ Synced 8 observations, 2450 objects, 7200 spectra

⚠ 3 local file(s) have been updated on the server.
  Run: campfire download --stale
```

Sync is fast and safe to run often — it refreshes inspection results, redshifts, flags, and any new objects added to the database.

---

## Downloading FITS Files

### `campfire download`

Download FITS spectrum files. Requires a prior `campfire sync` to populate the local catalog.

```bash
campfire download --obs ember_uds_p4              # By observation
campfire download --program EMBER-UDS             # By program
campfire download --field COSMOS                  # By field
campfire download --program EMBER-UDS --grating PRISM  # With grating filter
campfire download --stale                         # Re-download reprocessed files
campfire download --all                           # Everything accessible
campfire download --obs ember_uds_p4 --dry-run    # Preview without downloading
```

At least one filter is required (or `--all` / `--stale`).

**Options:**

| Option | Description |
|--------|-------------|
| `--obs NAME` | Download by observation name (repeatable) |
| `--program NAME` | Download by program slug (repeatable) |
| `--field NAME` | Download by field name (repeatable) |
| `--grating NAME` | Filter by grating type (repeatable) |
| `--stale` | Re-download files updated on the server |
| `--all` | Download everything accessible |
| `--workers N` | Parallel download workers (default: 4) |
| `--yes` | Skip confirmation prompt |
| `--dry-run` | Show plan without downloading |

Downloads are incremental — only new or changed files are downloaded. Files are verified with SHA-256 hashes.

**Output:**

```
Checking files...
  ember_uds_p4: 12 new (450.2 MB)
  capers_cosmos_p1: up to date

Download 12 files (450.2 MB)?
Proceed? [Y/n]: y

ember_uds_p4: 100%|██████████| 12/12 [00:45<00:00]

✓ Download complete
  Files downloaded: 12
  Total size: 450.2 MB
```

---

## Listing Observations

Running `campfire download` with no filters shows all available observations and their download status:

```bash
campfire download
```

**Output:**

```
  OBSERVATION               PROGRAM         FIELD       SPECTRA   LOCAL
  ember_uds_p4              EMBER-UDS       UDS            1350   1350 (complete)
  capers_cosmos_p1          CAPERS          COSMOS          960   480/960
  rubies_egs_p2             RUBIES          EGS             600

Use --obs, --program, or --field to download, or --all for everything.
```

---

## Status

### `campfire status`

Check credentials, catalog, and download status.

```bash
campfire status
```

**Output:**

```
✓ Credentials valid
  User: user@example.com

Data directory: /Users/you/.campfire/data
Catalog: 8 observations (last synced 2026-03-15 14:30)

  OBSERVATION               DOWNLOADED     SIZE
  ember_uds_p4              1350           2.1 GB
  capers_cosmos_p1          480            750 MB

⚠ 3 local file(s) updated on server. Run: campfire download --stale

Disk usage: 2.9 GB
```

---

## Authentication

`campfire login` for browser OAuth, `campfire login --api-key` for headless environments. `campfire whoami` shows the current user; `campfire logout` clears credentials. See [Getting Started](/docs/api/getting-started#2-authenticate) for the full walkthrough.

---

## Local Data Layout

The data directory defaults to `$CAMPFIRE_ROOT` if set, otherwise `~/campfire`. This matches the pipeline's directory structure, so pipeline users can access reduced spectra without re-downloading.

```
$CAMPFIRE_ROOT/            # or ~/campfire/
├── meta/
│   ├── campfire.db        # SQLite database (full catalog + download tracking)
│   ├── objects.csv        # Object catalog (for pandas/astropy)
│   └── spectra.csv        # Spectra catalog (for pandas/astropy)
└── products/
    ├── ember_uds_p4/
    │   ├── ember_uds_p4_prism_clear_123456_spec.fits
    │   └── ...
    └── capers_cosmos_p1/
        └── ...
```

Credentials are stored separately in `~/.campfire/credentials`.

### CSV Catalogs

The CSV catalogs are regenerated after each `campfire sync`:

```python
from astropy.table import Table

objects = Table.read('~/campfire/meta/objects.csv')
spectra = Table.read('~/campfire/meta/spectra.csv')

high_z = objects[objects['redshift'] > 3.0]
```

Three CSVs are generated: `objects.csv` (one row per sky-object, with inspection state), `spectra.csv` (one row per spectrum, with FITS paths), and `photometry.csv` (wide-format broadband photometry).

**objects.csv columns:**

| Column | Type | Description |
|--------|------|-------------|
| `object_id` | str | Unique sky-object identifier |
| `field` | str | Field name (cosmos, uds, egs, …) |
| `ra`, `dec` | float | Coordinates (J2000, degrees) |
| `redshift` | float | Best redshift (inspected > auto) |
| `redshift_auto` | float | Automated (zfit) redshift |
| `redshift_inspected` | float | Manually inspected redshift |
| `redshift_quality` | int | 0 (not inspected), 1 (impossible), 2 (tentative), 3 (probable), 4 (secure) |
| `n_targets`, `n_spectra` | int | Cross-program counts |
| `programs`, `gratings`, `observations` | str | Semicolon-separated lists |
| `member_target_ids` | str | Semicolon-separated per-program target IDs |
| `max_snr`, `max_exposure_time` | float | Aggregates across spectra |
| `has_photometry` | bool | Whether a photometry match exists |
| `photo_z`, `photo_z_err_lo`, `photo_z_err_hi` | float | Photo-z (if matched) |
| `last_inspected_at`, `last_inspected_by` | str | Inspection metadata |
| `last_data_change_at`, `staleness_reason` | str | Staleness tracking |

Tags are not denormalized into `objects.csv` — use `cf.query_objects(tags=[...])` or `cf.get_tags()` from the Python client.

**spectra.csv columns:**

| Column | Type | Description |
|--------|------|-------------|
| `spectrum_id` | str | Stable per-spectrum identifier |
| `target_id` | str | Per-program target ID |
| `object_id` | str | Parent sky-object ID |
| `grating` | str | Grating (PRISM, G140M, G235M, G395M, …) |
| `fits_path` | str | Remote FITS file path |
| `file_hash`, `file_size` | — | Remote file metadata |
| `signal_to_noise`, `exposure_time` | float | Per-spectrum quality |
| `reduction_version` | str | Pipeline version |
| `redshift_auto` | float | Per-spectrum automated redshift |
| `dq_flags` | int | Per-spectrum DQ bitmask (see [Flags](/docs/inspection/flags)) |
| `program_slug`, `observation`, `field` | str | Provenance |
| `local_path` | str | Relative path to downloaded FITS |

**photometry.csv** is wide-format: one row per `(object_id, catalog_name)` with identification columns (`object_id`, `field`, `catalog_name`, `catalog_id`, `match_distance_arcsec`), photo-z columns (`photo_z`, `photo_z_err_lo`, `photo_z_err_hi`), and per-band `f_<band>` / `e_<band>` pairs in μJy.

---

## Global Options

| Option | Description |
|--------|-------------|
| `--base-url URL` | Override API URL (for development) |
| `--version` | Show version |
| `--help` | Show help |

---

## Admin Commands

The `campfire deploy` subgroup provides archive-maintainer tooling — deploying reduced products to Supabase + R2, generating RGB cutouts and map tiles, and reconciling object associations. These commands are intended for archive maintainers only and are gated behind the `deploy` extra:

```bash
pip install -e ".[deploy]"
campfire deploy --help
```

If you're not a maintainer, you can ignore this subgroup.
