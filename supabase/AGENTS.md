# supabase/ — database schema

The commands (start the native stack, generate a migration, run the guards,
regenerate the seed) are in `README.md`. This file holds the rules.

## Source of truth

- **`supabase/schemas/` defines the whole database**: tables, functions,
  triggers, views, indexes, policies, in that file order (`schema_paths` in
  `config.toml`). Read the schema files, never the migrations, to learn the
  current definition of anything.
- **Migrations are the deployment mechanism.** Generate them from the schema
  files with pg-delta (`supabase db schema declarative sync -f <name> --no-apply`)
  and commit both together. `ops/scripts/deploy.sh` applies them to production
  when the PR merges to `main`.
- Every new migration starts with exactly these two lines (CI checks the first two
  non-comment lines):
  ```sql
  SET lock_timeout = '5s';
  SET statement_timeout = '60s';
  ```
- **Never edit an applied migration.** Fix forward with a new one.
- **No bulk backfills in migrations** over `spectra`, `objects` or
  `storage_objects`. A backfill is a resumable script under `scripts/`
  (template: `scripts/backfill_zfit_scalars.py`).
- Not declarative (write a plain migration under `supabase/migrations/`): casts,
  operators, text-search objects, DML, and **renames**. pg-delta turns a rename
  into DROP + CREATE, so rename with a hand-written `ALTER … RENAME` migration,
  then generate a second migration for the function bodies and comments that change with it.
- Read generated migrations before committing: pg-delta may emit `GRANT` / `REVOKE`
  lines from default privileges that you did not write.
- If a migration adds, removes or renames a seeded column or table, regenerate
  `seed.sql` (`scripts/generate_seed.py`, then `scripts/check_seed_privacy.py`).
  `seed_admin_fixtures.sql` is hand-maintained and builds on the rows `seed.sql` inserts.

## Tests

- `supabase/tests/check_*.sql` are self-contained guards (`BEGIN … ROLLBACK`)
  that CI runs after `db reset`. Each guards one invariant. When you change
  something a guard covers (its name says which: `check_reader_role`,
  `check_catalog_query`, `check_is_public_scoping`, …), update the guard in the
  same PR. A new invariant gets a new guard.
- The Python SQL harnesses (`python/tests/sql/`, `python/tests/test_*_rls.py`)
  are seed-anchored and need `CAMPFIRE_LOCAL_DB_URL`. They skip silently without
  a local database or without `psql` on `PATH`, so a green local run doesn't prove they ran.
- Test users: `admin@campfire.dev`, `user@campfire.dev`, `viewer@campfire.dev`, password `password123`.

## Access-control invariants

Access to catalog rows follows program membership: a row is visible when its
program is public or the viewer holds access to it. When adding or changing tables:

- **Trigger-maintained columns are never written by hand**: the `is_public`
  copies on `objects` / `targets` / `spectra`, `lines.object_id`,
  `photometry.object_id`, `line_fits.stale_redshift`. A client-supplied value is
  recomputed away. `lines` / `photometry` rows are written only through
  `line_fits_upsert` / `catalog_matches_upsert`, which replace a parent's rows wholesale.
- **A new program-gated catalog table** gets the same shape as `spectra`: a
  trigger-maintained `is_public` column and a read policy of the form
  `(NOT is_link_account() AND is_public) OR <scope array>`. Then extend
  `check_is_public_scoping.sql`.
- **A new catalog table readable by the public SQL login** (`campfire_reader`)
  needs a grant in `tables.sql`, a `reader_select_*` policy in `policies.sql`
  that uses column predicates only (the role has no JWT, so no helper
  functions), and a row in the allowlist of `check_reader_role.sql`. A new
  aggregate column on `objects` must join `reader.objects` in `views.sql` and the
  excluded-column list. Contract: `reader-role.md` (this directory).
- **SECURITY DEFINER functions scope their own rows.** Several filter helpers
  (`object_line_values`, `object_band_values`, `identify_files`, …) bypass RLS for
  performance. They take explicit `p_program_slugs` / `p_include_unpublished`
  arguments, because the bearer API routes call them as service role with no
  `auth.uid()`. When there is a user identity, they also apply a verbatim copy
  of the `select_objects_by_access` predicate. Keep those copies in step with the
  policy, and extend the matching `check_*_scoping.sql` guard.
- **New catalog filter dimensions are whitelist entries** in
  `catalog_query_whitelist()` (and `catalog_query_correlations()` for a child
  table), not new RPC parameters. Identifiers come only from the whitelist;
  values are always literals.
- Every trigger that copies a parent value onto a row reads the parent `FOR SHARE`
  (`python/tests/test_target_scope_race.py` checks this with two sessions).
- The sync RPCs run with `statement_timeout = 120s`; service role otherwise
  inherits the authenticator's 8 s.

## Local stack

- `supabase start --runtime native` runs Postgres and the services as host
  processes without Docker (Linux, Apple-silicon macOS; pin the CLI version CI
  uses, `npx -y supabase@2.120.0 …`). `pg_cron` jobs don't run natively.
- Each git branch is its own local project with its own data and ports.
