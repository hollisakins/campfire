# Scale DB: a local Postgres shaped like production

P-1 tooling (`docs/design-data-releases-infra-proposal.md` §3.1). A copy of production's
*data* loaded into the local stack built from the migration chain, with user identities
and free text scrubbed. It is for timings, query plans, freeze rehearsals, backfill
rehearsals and the needs-review work over real distributions. It lives on your machine
only: the dump contains proprietary-program rows and is never committed or shared.

```
bash scripts/scale_db/dump_prod.sh     # 1. read-only pg_dump of production → ~/campfire-scale-db/
bash scripts/scale_db/load.sh          # 2. db reset (no seed) → restore → scrub → ANALYZE
```

## Prerequisites

- `~/.config/campfire/prod_db_url` (`chmod 600`) holding the **session-pooler** connection
  string from the Supabase dashboard (Connect → Session pooler, port 5432;
  `postgresql://postgres.<ref>:<password>@aws-1-us-east-1.pooler.supabase.com:5432/postgres`).
  The direct host is IPv6-only; the transaction pooler (6543) does not support `pg_dump`.
  `CAMPFIRE_PROD_DB_URL` overrides the file.
- `pg_dump` / `pg_restore` / `psql` ≥ 17 on `PATH` (`~/miniforge3/envs/pgvalidate/bin`
  has 18.x).
- The native stack running for the branch you want to load into
  (`SUPABASE_EXPERIMENTAL_STACK=1 npx -y supabase@2.120.0 start --runtime native`). Each
  git branch is its own local project; `load.sh` wipes the current one.

## What the dump contains

`public` schema data only (schema comes from the migration chain). Excluded outright:
`api_keys`, `refresh_tokens`, `device_codes`, `access_codes`, `code_redemptions`,
`password_reset_log`, `pending_invites`, `inspection_access_requests`, `share_links`,
`download_log`, `sync_snapshots`, `sync_deletions`. `auth.*` is never dumped.

`scrub.sql` then: creates an `auth.users` row (`user-<8 hex>@campfire.dev`) for every
uuid the catalog references, rewrites `user_profiles` usernames and names (flags such as
`is_admin` / `can_inspect` are kept so RLS behaves), blanks `comments.content`, creates the
three fixed test accounts from `seed.sql` (`admin` / `user` / `viewer`, uuids
`1111…` / `2222…` / `3333…`), and refuses to commit if any non-synthetic email survives.

## Expectations

Production on 2026-10-11: 1.9 GB database, of which ~1.1 GB is data-bearing heap
(`storage_objects` 313 MB, `spectra` 202 MB mostly SVG thumbnails). Dump size and the
restore time are recorded here after the first run; the budget is "minutes, not an hour".
Load with triggers disabled (`session_replication_role = replica`), so derived tables that
triggers maintain (`spectrum_lines`, `object_photometry_bands`) come from the dump as-is.

## Refresh

Re-run both scripts before a measuring session; monthly otherwise. Dumps are timestamped;
delete old ones by hand (`~/campfire-scale-db/`). Avoid 04:30–05:30 UTC, when the
nightly sync-snapshot cron reads the same tables.

## Do not

- commit, upload or copy the dump anywhere (`.gitignore` covers `*.dump` under
  `scripts/scale_db/`, and `dump_prod.sh` refuses an output directory inside the repo);
- point `load.sh` at anything but a local stack;
- run the dump against production more than needed; it is one pass over ~1 GB.
