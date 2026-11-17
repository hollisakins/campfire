# CLI Reference

The `campfire` command-line tool manages authentication, catalog sync, and FITS downloads. After installing the Python package, the `campfire` command is available in your terminal.

## Quick Start

```bash
campfire login                        # Authenticate
campfire sync                         # Sync the full catalog (metadata only)
campfire pull spec --obs ember_uds_p4  # Download spectra
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
  Run: campfire pull --stale
```

Sync is fast and safe to run often — it refreshes inspection results, redshifts, flags, and any new objects added to the database.

---

## Downloading Files

### `campfire pull`

Download data products into your local tree. Pick a modality first, then what to download. `pull` syncs the catalog before it plans; `campfire download` is the same command.

```bash
campfire pull spectroscopy ...   # short: campfire pull spec ...
campfire pull imaging ...        # short: campfire pull im ...
```

**Spectroscopy** (spectra and their exposures):

```bash
campfire pull spec --obs ember_uds_p4                       # By observation
campfire pull spec --program EMBER-UDS                      # By program
campfire pull spec --field cosmos --grating PRISM           # By field, one grating
campfire pull spec --obs ember_uds_p4 --products spectra,exposures
campfire pull spec --all                                    # Every observation you can read
campfire pull spec --obs ember_uds_p4 --dry-run             # Preview without downloading
```

**Imaging** (mosaics, exposures and exposure maps):

```bash
campfire pull im --field cosmos                             # Mosaics of a field
campfire pull im --field cosmos --filters f277w f444w --tile A1
campfire pull im --field nep --products exposures,expmaps   # Inputs for your own mosaics
```

**Across both:**

```bash
campfire pull --stale                                       # Re-download files updated on the server
```

**Options:**

| Option | Applies to | Description |
|--------|------------|-------------|
| `--obs NAME` | spec | Observation name(s) |
| `--program NAME` | spec | Program slug(s) |
| `--field NAME` | spec, im | Field name(s) |
| `--all` | spec, im | Everything you can read |
| `--products A,B` | spec, im | spec: `spectra` (default), `exposures`; im: `mosaics` (default), `exposures`, `expmaps` |
| `--instrument NAME` | spec, im | Only these instruments (default: all) |
| `--grating NAME` | spec | Narrow spectra to these gratings |
| `--filters NAME` | im | Narrow to these filters |
| `--tile NAME` | im | Narrow mosaics to these tiles |
| `--workers N` | all | Parallel download workers (default: 4) |
| `--yes` | all | Skip confirmation prompt |
| `--dry-run` | all | Show plan without downloading |

A narrowing option that matches nothing is an error that lists what is available. Downloads are incremental (only new or changed files) and verified with SHA-256 hashes.

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

## Listing What's Available

`campfire pull spec` or `campfire pull im` with nothing selected lists what you can download and how much is already local:

```bash
campfire pull spec
```

**Output:**

```
  OBSERVATION               PROGRAM         FIELD       SPECTRA   LOCAL
  ember_uds_p4              EMBER-UDS       UDS            1350   1350 (complete)
  capers_cosmos_p1          CAPERS          COSMOS          960   480/960
  rubies_egs_p2             RUBIES          EGS             600
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

⚠ 3 local file(s) updated on server. Run: campfire pull --stale

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
| `needs_review` | bool | Needs-review badge: a quality 1–3 object whose published member spectra changed since its last sign-off |
| `last_data_change_at`, `staleness_reason` | str | Legacy staleness columns (no longer written; removed in a later release) |

Tags are not denormalized into `objects.csv` — use `cf.query_objects(tags=[...])` or `cf.get_tags()` from the Python client.

**spectra.csv columns:**

| Column | Type | Description |
|--------|------|-------------|
| `spectrum_id` | str | Stable per-spectrum identifier |
| `target_id` | str | Per-program target ID |
| `object_id` | str | Parent sky-object ID |
| `grating` | str | Grating (PRISM, G140M, G235M, G395M, …) |
| `filter` | str | NIRSpec filter paired with the grating (CLEAR, F070LP, F100LP, F170LP, F290LP) |
| `fits_path` | str | Remote FITS file path |
| `file_hash`, `file_size` | — | Remote file metadata |
| `signal_to_noise`, `exposure_time` | float | Per-spectrum quality |
| `cfpipe_version` | str | campfire-pipeline version (FITS `CMPFRVER`) |
| `crds_context`, `jwst_version` | str | CRDS pmap + `jwst` version that pinned the calibration |
| `date_obs`, `reduced_at` | str | Observation date + reduction time (FITS `CMPFRTIM`) |
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

The `campfire deploy` subgroup provides archive-maintainer tooling — deploying reduced products to Supabase + R2, generating RGB cutouts and map tiles, and reconciling object associations. These commands are intended for archive maintainers only:

```bash
campfire deploy --help
```

If you're not a maintainer, you can ignore this subgroup.
