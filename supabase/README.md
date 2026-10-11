# Local database

`supabase/schemas/` is the source of truth for the schema; `supabase/migrations/` is how
it is deployed; `seed.sql` is the sample dataset preview branches and CI load. This page is
the local workflow after P-1 (`docs/design-data-releases-infra-proposal.md`).

## Start the stack (no Docker needed)

`config.toml` turns on the CLI's experimental `stack` commands. On Linux or Apple-silicon
macOS the whole stack runs as host processes:

```bash
npx -y supabase@2.120.0 start --runtime native     # first run downloads the services (~35 s here)
npx -y supabase@2.120.0 status --output-format json  # endpoints and env (DB_URL, API_URL, keys)
npx -y supabase@2.120.0 db reset                    # migration chain + seed.sql (~5 s)
npx -y supabase@2.120.0 stop
```

Pin the version you use (`2.120.0` at the time of writing; CI pins the same): the stack
commands and the pg-delta engine are experimental and outside the CLI compatibility
promise. **Each git branch is its own local project** (own data, own ports in
20000–32767; `config.toml` deliberately fixes no ports): `supabase stack list` shows them,
`supabase stop --all` stops them all, `supabase stack destroy` deletes one. A project keeps
the runtime it was first started with; data lives under `~/.supabase/stacks/<id>/`. With Docker installed, plain `supabase start` keeps working.
Known limits of the native runtime: `pg_cron` jobs do not run; Windows and Intel Macs are
not supported.

## Change the schema

1. Edit `supabase/schemas/*.sql`.
2. `npx -y supabase@2.120.0 db schema declarative sync -f <name> --no-apply`
   writes `supabase/migrations/<ts>_<name>.sql` with the difference between the schema
   files and the migration chain (pg-delta, in a shadow database). "No schema changes
   found" means the files and the chain already agree.
3. Prepend the header: `SET lock_timeout = '5s'; SET statement_timeout = '60s';`
   (the `database` CI check rejects new migrations without it).
4. `npx -y supabase@2.120.0 db reset`, read the SQL, run the guards (below).
5. Commit the schema change and the migration together.

pg-delta models materialized views, comments, publications and security-invoker views.
Casts, operators, text-search objects and any DML are not declarative: write those as a
plain migration. Generated migrations may use `GRANT`/`REVOKE` lines you did not write
(default privileges); read them.

## Tests

```bash
DB_URL=$(npx -y supabase@2.120.0 status --output-format json | python3 -c 'import sys,json; print(json.load(sys.stdin)["env"]["DB_URL"])')
for f in supabase/tests/check_*.sql; do psql "$DB_URL" -v ON_ERROR_STOP=1 -f "$f"; done
CAMPFIRE_LOCAL_DB_URL=$DB_URL pytest python/tests -k "rls or lifecycle or field_deploy_gate"
```

`supabase/tests/check_*.sql` are self-contained guards (`BEGIN … ROLLBACK`). The Python
harnesses are seed-anchored (user ids `1111…`, `2222…`, `3333…`, admin / user / viewer
@campfire.dev, password `password123`) and skip when no local database answers
(`python/tests/_localdb.py` resolves `CAMPFIRE_LOCAL_DB_URL`, then the Docker container).

## Regenerate the seed

`seed.sql` is a stratified sample of **public** production data plus synthetic
accounts, written by `scripts/generate_seed.py` (read-only against production:
`CAMPFIRE_SUPABASE_URL` + `CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY`, or the `[supabase]`
block of `$CAMPFIRE_ROOT/config/deploy.toml`). Regenerate it whenever a migration
changes a seeded table, or when a new table needs rows:

```bash
conda run -n campfire python scripts/generate_seed.py      # ~2 min, a few thousand rows read
python scripts/check_seed_privacy.py                        # must print OK
npx -y supabase@2.120.0 db reset --yes                      # then the guards + harnesses above
```

What the generator guarantees, and what the checker re-verifies on the file alone:
only `is_public = true` programs and rows that reference them (the run aborts on
the first violation; the checker also walks every `programs` / `program_slugs`
array); only `deploy_status = 'published'` spectra are read from production (the
draft and the revoked fixture are two of those demoted in the seed, never
production's own drafts); no production identities — every real `auth.users` uuid is
mapped onto `admin@` / `user@`, comment text is synthetic, the only emails are
`@campfire.dev` and `@shared.invalid`. Besides the catalog it seeds the lifecycle
surface: real `storage_objects` rows (hashes, sizes) for every sampled spectrum and
its sidecars, `spectrum_line_fits` (+ `spectrum_lines` by trigger), `object_photometry`
(+ bands by trigger), one published deployment per observation plus a draft and a
revoked one each with one spectrum attached, a few published `nircam_images` of one
real field plus a draft one, the `fields` rows, and two share links (one live, one
revoked; password `seed-link-password`). The objects of `castellano3073` are numbered
first so the seed-anchored harnesses keep objects 1-3. Keep the file under 2 MB
(`--objects-per-program` controls the sample size).

## CI

`.github/workflows/database.yml` runs on every PR and, for PRs touching the database
surface: starts the native stack, fails on schema↔migration drift
(`declarative sync --strict-coverage` must create nothing), resets with the seed, runs the
guards and harnesses, and checks the header of every new migration. It is a required check
(`database`).
