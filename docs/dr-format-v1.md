# Data-release export format, version 1

The on-disk shape of a CAMPFIRE data release (DR), as written by `campfire dr
export` (on the admin's machine, `python/campfire/dr_export.py`) and read by
`campfire pull --dr` and `Campfire(dr=...)`. `dr_releases.dr_format_version`
records which version a release was exported with; a reader refuses versions it
does not know. Design: `docs/design-data-releases.md` §4.3, plan §6 P4b.

## Files

```
dr/<tag>/exports/<stream>/<scope>.<sha16>.<claim8>.jsonl.gz
```

on the private data bucket (OSN). One file per (stream, scope); the scope is a
program slug, or a field-level component for the manifest. Each line is one JSON
object; the file is gzip bytes (content type `application/gzip`, not
`Content-Encoding`), hashed as stored. The same rows always give the same bytes.

`<sha16>` is the first 16 hex digits of the file's sha256 and `<claim8>` the
first 8 hex digits of the build's claim token: every build has its own key, so a
rebuild never overwrites the file a reader was handed, and an export run that
lost its build claim can only write its own key. The ledger (`dr_exports`)
records the key of the ready file; readers use the url they are given and never
construct keys. A build that loses its claim deletes its own upload; an export
run that finds every file ready deletes the other keys under the prefix the
ledger does not name (superseded builds). A release is only visible to readers
once it is published, and a published release's ledger never changes, so no
reader ever holds a url to a superseded file. Nothing else may be written under
`dr/<tag>/exports/`. `campfire dr purge` (refused while a build claim is live)
deletes the whole prefix (never with `--local`, whose database is local but
whose bucket is the shared one; `dr export --local` is refused for the same
reason).

The keys are **not layout products**: never registered in `storage_objects`,
never presignable through the storage routes. Readers obtain presigned urls from
`/api/v1/dr/<tag>/files`, which returns only the files the caller may read, each
with `stream`, `scope`, `sha256`, `size` and `rows` to verify against. A client
merges a stream's files into one table (one row per id; an object in several of
the reader's programs appears in each program's file).

`<scope>` in the key is the scope name when it is already key-safe
(`[A-Za-z0-9._-]+`, every program slug); any other name (component names carry
`/`, catalog names spaces) is sanitised to that alphabet **and suffixed with
`-` plus the first 8 hex digits of its md5**, so names that sanitise alike
(`CAT v1` / `CAT_v1`) still get distinct keys. The files route carries the
real name alongside the url, so a reader never inverts the mapping.

## Streams and scopes

| stream | scope | rows |
|---|---|---|
| `objects` | program slug | every frozen object whose `programs` contains the slug |
| `spectra` | program slug | frozen published spectra of that program |
| `line_fits` | program slug | frozen line fits of that program's spectra |
| `photometry` | program slug | frozen photometry rows whose object's `programs` contains the slug |
| `manifest` | program slug | NIRSpec finals + sidecars of that program |
| `manifest` | `nircam/<field>` | the published mosaics of the field component |

P(z) sidecars are not in the manifest (they are key-only products the layout cannot
archive; the `photometry` rows carry the catalog values).

A file is produced only when it has rows. Program files are
visible to a viewer holding that program (the same `accessible_program_slugs()`
scope as live); the field-level manifest files are visible to every
account, as published field deployments are live. Share-link accounts get
nothing (the `/api/v1` bearer routes refuse them).

An object (or photometry row) that belongs to several programs appears in each
of those programs' `objects` (`photometry`) files. The **aggregates on an
objects row are scoped to the file's program** (`n_targets`, `n_spectra`,
`gratings`, `observations`, `max_snr`, `max_exposure_time` count only that
program's frozen spectra), exactly what the live scoped sync returns to a caller
holding that one program. A reader holding several programs merges rows by `id`
and recomputes the aggregates from the spectra it holds. Nothing in a program's
file names another program or another program's spectra: `programs` and
`evidence_programs` are `[<file program>]`, `reviewed_spectrum_ids` keeps
that program's spectra only, and `z_source_spectrum_id` is null when the anchor
spectrum belongs to another program (a reader holding several programs unions
the arrays across its copies).

Within a file rows are ordered by the stream's key (`objects`: `object_id`
name; `spectra`: `spectrum_id`; `line_fits`: `spectrum_id`; `photometry`: `id`;
`manifest`: `storage_key`), the order `dr_export_rows` pages in.

## Row shapes

Rows mirror the live `/api/v1/sync/*` rows of the same stream where a field
exists on both sides, so the client's sqlite loader applies with few changes.
Frozen-only fields are added; live-only operational fields (`updated_at`,
`created_at`, `lists`, tombstones) are absent.

### objects

`id` (the live `objects.id` at the freeze), `object_id` (the public name),
`field`, `ra`, `dec`, `programs`, `evidence_programs` (the file program),
`observations`, `gratings`, `n_targets`, `n_spectra`,
`max_snr`, `max_exposure_time` (the six scoped to the file program),
`photo_z`, `photo_z_err_lo`, `photo_z_err_hi`, `has_photometry`,
`z_source_spectrum_id` (the object's **global** redshift anchor: the `spectra.id`
the `compute_object_redshift_auto` rule picks among *all* the object's published
member spectra in the freeze snapshot, whichever program they belong to; it is
written only in the partition of the program that owns that spectrum and is
`null` in the object's other partitions, so a `null` means "the anchor is a
spectrum of another program", not "no anchor"),
`redshift_auto`, `redshift_inspected`, `redshift`, `redshift_quality`,
`inspected_used_auto`, `last_inspected_at`, `last_inspected_by`, `version`,
`is_active` (always true), `needs_review`, `reviewed_spectrum_ids` (the file
program's spectra among the review state at the freeze). Apart from the six
scoped aggregates and the two program arrays, every value is the live
`objects` row as it stood at the freeze (the frozen tables are verbatim copies
since P6b): `photo_z*` / `has_photometry` are the live cross-match fields,
`redshift_auto` the live column, whichever catalogs or spectra produced them.

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

`id` (live `catalog_matches.id`), `object_id` (live `objects.id`),
`programs` (`[<file program>]`, as on the objects stream), `field`, `catalog_name`, `catalog_id`,
`ra`, `dec`, `match_distance_arcsec`, `photometry`, `photo_z`,
`photo_z_err_lo`, `photo_z_err_hi`, `has_pz`.

### manifest

`storage_key` (the live key at the freeze), `component`, `program_slug`,
`field`, `product_type`, `spectrum_id` (text key; null for field-level rows),
`backend`, `bucket`, `content_hash`, `science_hash`, `size_bytes`,
`archive_key` (the content-addressed archive copy, set by the byte pass),
`byte_state` (`pending` | `copied` | `stamped` | `failed`), `stamped_hash`
(whole-file hash of the stamped archive bytes). A reader downloads
`archive_key` when `byte_state` is `copied` or `stamped`, else the live key.
Field-level components also carry the field's exposure maps, their renders, the
layout plot and the mosaic thumbnails / quick-looks (product types
`nircam_expmap`, `nircam_expmap_plot`, `nircam_layout`, `nircam_mosaic_thumbnail`,
`nircam_mosaic_quicklook`; P6b-3). The release page document's figures
(`dr_manifest` component `page`, product type `dr_page_asset`, content-addressed
under `dr/<tag>/page/<first 16 hex of the sha256>/<name>`, never overwritten) are
not exported: they belong to the release page, not the catalog.

## Ledger and lifecycle

`dr_exports` holds one row per (tag, stream, partition = scope): `storage_key`,
`sha256` ("sha256:<hex>" of the gzip bytes), `size_bytes`, `rows`, `state`
(`pending` | `building` | `ready` | `failed`), `built_at`, `error`. The builder
is resumable: a run rebuilds only rows that are not `ready`.

The release state is derived from the ledger and the member byte states after
every write (`dr_derive_state`): `frozen` (nothing built) → `exporting` (files
pending) → `exported` (every file ready) → `copying` (byte pass under way) →
`ready` (every file ready **and** every manifest entry copied or stamped). The
normal order is freeze → archive (`campfire dr archive`, the byte pass) → export
(`campfire dr build` runs the three), so the manifest files are built once,
already carrying the archive keys. A byte pass run after an export changes
manifest rows (`archive_key`, `byte_state`, `stamped_hash`), so it invalidates
the `manifest` files it touches (`dr_export_invalidate`) and the next `campfire
dr export` rebuilds exactly those; a release is published only from `ready`, so
a reader of a published release always sees manifest files that carry the
archive keys. The files route's `complete`
flag is false while any file is pending.

## Local store (the reader's side, P5-1)

`campfire pull --dr <tag>` (`campfire.dr_store.load_release`) lands a release under
`$CAMPFIRE_ROOT/dr/<tag>/`:

```
release.json                       the files route's answer + loaded_at, schema_version, the files
exports/<stream>/<scope>.jsonl.gz  the export files as fetched (scope named by the key rule above)
meta/campfire.db                   a LocalStore (the live mirror's DDL) + release_manifest
products/<relpath>                 the bytes, placed by the layout contract from the live storage_key
```

A file already on disk that verifies (sha256 + size from the files route) is kept;
one that does not is fetched again. The store is derived from the kept files and
nothing else: it is rebuilt from them whenever the set of files changes (a program
granted or lost) or the client's `SCHEMA_VERSION` moved, with the
`storage_objects.local_*` columns (what is on disk) carried across. The live
mirror's purge, tombstone and schema-mismatch-delete logic never touch a release
store. A release of a format version the client does not know is refused before
anything is written.

Merging a stream's files into one table:

- **objects**: one row per `id`; `programs` / `evidence_programs` /
  `reviewed_spectrum_ids` unioned, `z_source_spectrum_id` the non-null copy; the
  six scoped aggregates plus `member_target_ids` recomputed over the `spectra` rows
  held (the reader holds every published spectrum of its programs, so the result
  describes exactly its view of the release).
- **spectra**, **line_fits**: one row per id; a fit takes the `field` of its
  spectrum.
- **photometry**: one row per `id`, keyed by the object's name (the live stream's
  key), `programs` dropped.
- **manifest**: kept whole in `release_manifest` (archive key, byte state, stamped
  hash, for the byte pull), and mirrored into `storage_objects` so the live-shaped
  code (`get_spectrum`, `find_local_path`) resolves a spectrum's file: there
  `content_hash` is the hash of the bytes a reader of the release gets, the
  `stamped_hash` of a stamped member, the live `content_hash` otherwise.

`Campfire(dr=tag)` reads this store and nothing else (design §4.7: it fails
closed, `DRModeError`, on every live branch).

The bytes (`campfire pull --dr <tag> --obs ... | --program ... | --field ... | --all`,
P5-2) go through the same download engine as a live pull, planned from the
release store's `storage_objects`, presigned with `?dr=<tag>` (the storage
presign route answers with the release's archived copies, members the release
does not hold archived omitted), verified against the hash the store carries
(the stamped bytes' hash for a stamped member) and recorded on the release
store, so an interrupted pull resumes at file granularity. The live tree and
the live mirror are never touched. `GET /api/v1/dr` lists the releases a caller
may read (`campfire dr list`); `POST /api/v1/identify` answers what the archive
knows about a file's bytes (`campfire identify`, `identify_files`).

