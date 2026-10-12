# Data-release export format, version 1

The on-disk shape of a CAMPFIRE data release (DR), as written by the export
builder (`/api/v1/dr/<tag>/export`, looped by `campfire dr export`) and read by
`campfire pull --dr` and `Campfire(dr=...)`. `dr_releases.dr_format_version`
records which version a release was exported with; a reader refuses versions it
does not know. Design: `docs/design-data-releases.md` §4.3, plan §6 P4b.

## Files

```
dr/<tag>/exports/<stream>/<partition>.jsonl.gz
```

on the private data bucket (OSN). One file per (stream, partition); each line is
one JSON object; the file is gzip bytes (content type `application/gzip`, not
`Content-Encoding`), hashed as stored. The keys are **not layout products**:
never registered in `storage_objects`, never presignable through the storage
routes. Readers obtain presigned urls from `/api/v1/dr/<tag>/partitions`, which
returns only the partitions the caller holds, each with `sha256`, `size` and
`rows` to verify against.

`<partition>` in the key is the partition name made key-safe (every character
outside `[A-Za-z0-9._-]` becomes `_`); the partitions route carries the real
name alongside the url, so a reader never inverts the mapping.

## Streams and partitions

| stream | partition | rows |
|---|---|---|
| `objects` | program slug | every frozen object whose `programs` contains the slug |
| `spectra` | program slug | frozen published spectra of that program |
| `line_fits` | program slug | frozen line fits of that program's spectra |
| `photometry` | program slug | frozen photometry rows whose object's `programs` contains the slug |
| `members` | program slug | NIRSpec finals + sidecars of that program |
| `members` | `nircam/<field>` | the published mosaics of the field component |
| `members` | `photometry/<field>/<catalog>` | the P(z) sidecars of the photometry component |

A partition is produced only when it has rows. Program partitions are
visible to a viewer holding that program (the same `accessible_program_slugs()`
scope as live); the two field-level member partitions are visible to every
account, as published field deployments are live. Share-link accounts get
nothing (the `/api/v1` bearer routes refuse them).

An object (or photometry row) that belongs to several programs appears in each
of those programs' `objects` (`photometry`) partitions. The **aggregates on an
objects row are scoped to the partition's program** (`n_targets`, `n_spectra`,
`gratings`, `observations`, `max_snr`, `max_exposure_time` count only that
program's frozen spectra), exactly what the live scoped sync returns to a caller
holding that one program. A reader holding several programs merges rows by `id`
and recomputes the aggregates from the spectra it holds; `programs` and
`evidence_programs` are the full frozen arrays in every copy.

Within a partition rows are ordered by the stream's key (`objects`: `object_id`
name; `spectra`: `spectrum_id`; `line_fits`: `spectrum_id`; `photometry`: `id`;
`members`: `storage_key`), the order `dr_export_rows` pages in.

## Row shapes

Rows mirror the live `/api/v1/sync/*` rows of the same stream where a field
exists on both sides, so the client's sqlite loader applies with few changes.
Frozen-only fields are added; live-only operational fields (`updated_at`,
`created_at`, `lists`, tombstones) are absent.

### objects

`id` (the live `objects.id` at the freeze), `object_id` (the public name),
`field`, `ra`, `dec`, `programs`, `evidence_programs` (programs of the
published members), `observations`, `gratings`, `n_targets`, `n_spectra`,
`max_snr`, `max_exposure_time` (the six scoped to the partition program),
`photo_z`, `photo_z_err_lo`, `photo_z_err_hi`, `has_photometry`,
`z_source_spectrum_id` (the `spectra.id` whose auto fit anchors
`redshift_auto`), `redshift_auto`, `redshift_inspected`, `redshift`,
`redshift_quality`, `inspected_used_auto`, `last_inspected_at`,
`last_inspected_by`, `version`, `is_active` (always true), `needs_review`,
`reviewed_spectrum_ids` (the review state at the freeze).

### spectra

`id` (live `spectra.id`), `spectrum_id` (the text key), `target_id`,
`object_id` (the object's public name, null when the spectrum's target had no
active object), `object_db_id`, `grating`, `fits_path`, `file_hash`,
`science_hash`, `file_size`, `signal_to_noise`, `exposure_time`,
`cfpipe_version`, `crds_context`, `jwst_version`, `date_obs`, `reduced_at`,
`redshift_auto`, `chi2_min`, `confidence`, `dq_flags`, `program_slug`,
`observation`, `field`.

### line_fits

`spectrum_id` (live `spectra.id`), `spectrum_name` (the text key), `target_id`,
`grating`, `program_slug`, `observation`, `object_id` (name), `object_version`,
`z_used`, `z_source`, `z_quality`, `z_fit`, `z_fit_err`, `dv`, `dv_err`,
`sigma_v`, `sigma_v_err`, `kin_source`, `n_lines`, `n_detected`, `n_broad`,
`chi2`, `dof`, `lines`, `fit_version`, `cfpipe_version`, `f_lsf`,
`spectrum_hash`, `spectrum_science_hash`, `fitted_at`, `stale_redshift`,
`stale_spectrum` (both evaluated inside the freeze snapshot).

### photometry

`id` (live `object_photometry.id`), `object_id` (live `objects.id`),
`programs` (the object's at the freeze), `field`, `catalog_name`, `catalog_id`,
`ra`, `dec`, `match_distance_arcsec`, `photometry`, `photo_z`,
`photo_z_err_lo`, `photo_z_err_hi`, `has_pz`.

### members

`storage_key` (the live key at the freeze), `component`, `program_slug`,
`field`, `product_type`, `spectrum_id` (text key; null for field-level rows),
`backend`, `bucket`, `content_hash`, `science_hash`, `size_bytes`,
`archive_key` (the content-addressed archive copy, set by the byte pass),
`byte_state` (`pending` | `copied` | `stamped` | `failed`), `stamped_hash`
(whole-file hash of the stamped archive bytes). A reader downloads
`archive_key` when `byte_state` is `copied` or `stamped`, else the live key.

## Ledger

`dr_exports` holds one row per (tag, stream, partition): `storage_key`,
`sha256` ("sha256:<hex>" of the gzip bytes), `size_bytes`, `rows`, `state`
(`pending` | `building` | `ready` | `failed`), `built_at`, `error`. The builder
is resumable: a run rebuilds only rows that are not `ready`, and the last
`ready` row moves the release from `exporting` to `exported`.
