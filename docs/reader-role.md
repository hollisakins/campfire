# Direct SQL access to the public catalog (`campfire_reader`)

A read-only Postgres login for the public CAMPFIRE catalog: the same tables
the portal and the Python client read, as SQL, for psql, pandas, DuckDB or
TOPCAT. Catalog Read Model phase 4. The tables are the contract; the role
sees what an anonymous reader of the portal could see, and nothing else.

## Connecting

```
postgresql://campfire_reader:<password>@db.campfire-jwst.org:6432/postgres?sslmode=require
```

The password is published with the connection string (the data is public;
the credential only selects the role). The connection is TLS-encrypted; the
server presents a self-signed certificate, so `sslmode=require` rather than
`verify-full` until a publicly trusted certificate is wired to the pooler.

```bash
psql "postgresql://campfire_reader:<password>@db.campfire-jwst.org:6432/postgres?sslmode=require"
```

```python
import pandas as pd, sqlalchemy as sa
eng = sa.create_engine("postgresql://campfire_reader:<password>@db.campfire-jwst.org:6432/postgres?sslmode=require")
df = pd.read_sql("SELECT object_id, ra, dec, redshift, redshift_quality FROM objects WHERE redshift > 6", eng)
```

```python
import duckdb
con = duckdb.connect()
con.sql("INSTALL postgres; LOAD postgres;")
con.sql("ATTACH 'dbname=postgres user=campfire_reader password=<password> host=db.campfire-jwst.org port=6432 sslmode=require' AS cf (TYPE postgres, READ_ONLY)")
con.sql("SELECT count(*) FROM cf.public.spectra").show()
```

Limits, set on the role: 30 s per statement, 60 s idle in a transaction,
read-only transactions, 20 connections in all through a transaction-mode
pooler (PgBouncer). Postgres has no per-query row cap; the statement timeout
and `work_mem` bound a query instead. Page a large pull with keyset
predicates (`WHERE id > :last ORDER BY id LIMIT 10000`), not `OFFSET`.
Session state does not survive a transaction in a transaction-mode pooler,
and the pooler resets the server connection after every transaction
(`DISCARD ALL`): no `SET` that outlives a transaction, no temp tables, no
prepared statements across statements, no `LISTEN`. Every reader shares the
one role, so nothing you do in a session is visible to another reader.

Since the credential is public, Postgres cannot stop a holder from changing
the role's own password (`SET TRANSACTION READ WRITE` lifts the read-only
default for that transaction). Doing so only breaks the pooler's next server
connection; a timer on the host re-applies the published password within two
minutes, so it is a nuisance, not a way in. Please do not.

## What the role sees

Live catalog, the rows a signed-out reader of the portal sees:

| table | rows |
|---|---|
| `programs` | public programs |
| `observations` | observations of public programs |
| `objects` | public, active, with a published spectrum (one row per astrophysical source); the member aggregates (`programs`, `observations`, `gratings`, `n_targets`, `n_spectra`, `max_snr`, `max_exposure_time`, `search_text`) count public published members only |
| `targets` | public, with a published spectrum (one row per MSA target per observation) |
| `spectra` | public and published (one row per reduced spectrum; `fits_path` is the storage key) |
| `line_fits` | the fit run of each visible spectrum (provenance: `z_used`, `stale_redshift`, versions) |
| `lines` | one row per visible spectrum × line (`line`, `component`, `flux`, `snr`, `ew_rest`, kinematics); `object_id` is the owning object |
| `catalog_matches` | the photometry cross-match of each visible object |
| `photometry` | one row per match × band (`band`, `flux`, `flux_err` in µJy, `mag`, `snr`); `object_id` is the owning object |
| `nircam_images` | published mosaics |
| `fields` | fields with a published mosaic |

Published data releases, frozen as they were at the freeze: `dr_releases`
(`tag`, `state`, `doi`, `frozen_at`, `counts`) and the twins `dr_objects`,
`dr_targets`, `dr_spectra`, `dr_line_fits`, `dr_lines`, `dr_catalog_matches`,
`dr_photometry`, `dr_nircam_images`, `dr_fields`, `dr_filter_options`,
`dr_manifest`, each keyed by `tag`, each with the live table's columns. A
release's public rows are the ones public at the freeze (the frozen
`is_public` column), so a release reads the same way forever.

`objects` and `dr_objects` resolve to views in the `reader` schema (the role's
`search_path` is `reader, public`): the same rows and columns as the tables,
with the member aggregates computed over the public published members, since
the tables' stored aggregates count every member, private programs included.
The tables themselves are granted without those columns, so
`SELECT * FROM public.objects` is refused while `SELECT * FROM objects` works;
`public.objects.id` and the other plain columns remain readable for joins.
On `dr_objects` the same applies to `evidence_programs` and `z_source_spectrum_id`.

Nothing else: no functions, no user tables, no storage registry, no
unpublished or private row under any query (`supabase/tests/check_reader_role.sql`
is the guard; the policies are `reader_select_*` in `supabase/schemas/policies.sql`,
the views are in `views.sql`). Proprietary data stays behind the API, where
the JWT is.

## Columns worth knowing

- `objects.programs`, `observations`, `gratings` are arrays: `'PRISM' = ANY(gratings)`,
  `programs && ARRAY['rubies']`.
- `objects.redshift` is the adopted redshift (inspected when there is one, else
  the automatic one); `redshift_quality` 1–4; `redshift_auto`, `redshift_inspected`
  are the two sources.
- `lines.line` names follow the pipeline line list (`Halpha`, `OIII5007`, the
  doublet totals `OII3727`, `CIII1908`, …); `component` is `narrow`, `broad`
  or `doublet`. A fit is `line_fits.stale_redshift` when the object's redshift
  moved since; join it when you care.
- `photometry.mag` is AB (`23.9 - 2.5 log10(flux[µJy])`), NULL for a
  non-positive flux.
- Bookkeeping columns (`version`, `staleness_reason`, `search_text`,
  `reviewed_spectrum_ids`, `last_inspected_by`) are internal and may change
  meaning between releases.

## The example query

Objects above z = 4.9 observed with PRISM or G395M with both C III] and
[O III] detected (the design doc's Figure 5):

```sql
SELECT o.object_id, o.redshift, o.redshift_quality
FROM objects o
WHERE o.redshift >= 4.9
  AND o.gratings && ARRAY['PRISM', 'G395M']
  AND EXISTS (SELECT 1 FROM lines l WHERE l.object_id = o.id AND l.line = 'CIII1908' AND l.snr >= 5)
  AND EXISTS (SELECT 1 FROM lines l WHERE l.object_id = o.id AND l.line = 'OIII5007' AND l.snr >= 3)
ORDER BY o.redshift DESC;
```

The same against a release:

```sql
SELECT o.object_id, o.redshift
FROM dr_objects o
WHERE o.tag = 'DR2026.1' AND o.redshift >= 4.9
  AND EXISTS (SELECT 1 FROM dr_lines l WHERE l.tag = o.tag AND l.object_id = o.id AND l.line = 'CIII1908' AND l.snr >= 5);
```

The portal's Advanced Filters modal copies a query as the same JSON tree the
API takes; the SQL it compiles to runs here unchanged.

## For the operator

`ops/README.md`, "Public read-only SQL": the pooler is a compose profile
(`reader`), `ops/scripts/reader-setup.sh` sets the password, writes the
pooler's SCRAM userlist (that one role only), makes the certificate and
starts the pooler; the security group admits 6432; `db.campfire-jwst.org` is
a DNS-only record. The role itself comes from the migration chain
(`20261028193000_phase4_reader_role.sql`); locally `supabase/roles.sql`
creates it for the stack and the shadow database.
