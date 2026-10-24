# CAMPFIRE self-hosting runbook

Infrastructure-as-code for running the CAMPFIRE portal and its database on one
Jetstream2 VM, replacing Vercel ($20/mo) and hosted Supabase ($40/mo). Status
and the agreed order of operations live in this file; the application itself is
documented in the repo-root `AGENTS.md`.

Everything here is a compose file, a Caddyfile and a few shell scripts. If the
Jetstream2 allocation ever lapses, the same directory runs on any Ubuntu box.

## Architecture

```
browser / campfire CLI
        │ https
        ▼
Cloudflare edge (DNS, TLS, static cache, rate limits)   hollisakins.com is already proxied
        │ https, security group admits 443 from Cloudflare ranges only
        ▼
Caddy :443 (ops/Caddyfile), routes by hostname
        ├── $WEB_HOST  campfire.hollisakins.com      → web  :3000  (Next.js, web/Dockerfile)
        └── $API_HOST  campfire-api.hollisakins.com  → kong :8000
                                /auth/v1 → auth (GoTrue)     ← browsers call these directly,
                                /rest/v1 → rest (PostgREST)    exactly as they call *.supabase.co today
                                                   │
                                            db :5432 (supabase/postgres 17)
                                            127.0.0.1 only; pg_cron runs the 2 scheduled jobs
127.0.0.1-only extras: Studio via kong:8000 (SSH tunnel), web:3000 (health, cron timer)
Host timers (ops/systemd): backup 03:30 UTC, sync-snapshot 05:00 UTC, restore-check Sun 06:00 UTC
Unchanged: Cloudflare download Worker, OSN buckets, R2 tiles, campfire-etc on Fly.io
```

What the stack does **not** run, because CAMPFIRE never used it: realtime,
storage API, imgproxy, edge functions, analytics/logflare, supavisor.

Dependencies on the old platform that this replaces, found by audit
(2026-10-24): one Vercel cron (`web/vercel.json`), `@vercel/analytics`
(removed), `maxDuration` exports (inert under `next start`). The Python client
only ever talks to `campfire.hollisakins.com/api/v1`, so no client release is
tied to the move.

## Sizing

| | |
|---|---|
| Flavor | m3.medium (8 vCPU / 30 GB), ~5,800 SU per month; m3.quad is the fallback size |
| Root disk | 60 GB: OS, Docker images, logs |
| Volume | 200 GB at `/srv/campfire`: Postgres (2.2 GB today), dumps, certs, templates. Volumes grow online; shrinking means a new volume |
| Postgres | `PG_*` in `.env`; defaults sized for 30 GB RAM |

## Migration order

Each step names an owner and the gate that closes it.

1. **Phase 0 PR (this directory).** Dockerfile + `output: 'standalone'`,
   `/api/health`, Vercel Analytics removed, `ops/` scaffold. Gate: web CI green;
   `docker compose config` validates with a filled `.env`. Merging changes
   nothing on Vercel.
2. **Create the box** (Exosphere): Ubuntu 24.04, m3.medium, public IP, SSH
   key, 200 GB volume attached. Horizon: application credential for the
   OpenStack CLI. Gate: SSH works.
3. **Base system** (`scripts/bootstrap.sh`), stack up under the staging hostnames
   `campfire-staging.hollisakins.com` (already an allowed origin on the download
   Worker) and `campfire-api.hollisakins.com`, web still pointed at hosted
   Supabase. Security group → 22 (your IPs) and 443 (Cloudflare ranges) only.
   Gate: staging renders against hosted Supabase.
4. **Restore rehearsal.** Read-only dump of production into the VM Postgres,
   staging repointed at the local API. Compare counts, run the SQL guards, log in,
   open an object page, run `campfire sync` against staging. Gate: a cutover
   checklist with measured timings (below, filled in).
5. **Deploy workflow** (`.github/workflows/deploy.yml`, not yet written): on push
   to `main` build the image on a GitHub runner, push to GHCR `:<sha>`, SSH with a
   forced-command deploy key, `scripts/deploy.sh <sha>`. The key's
   `authorized_keys` line is
   `restrict,command="/srv/campfire/repo/ops/scripts/deploy.sh" ssh-ed25519 …`
   (`restrict` disables forwarding, pty and agent); the script accepts only
   `deploy <full 40-hex sha>` and only a sha already on `origin/main`. Gate: a
   merge to main lands on staging unattended. No self-hosted runner: the repo
   is public.
6. **Phase 1, web cutover.** Cloudflare DNS: `campfire.hollisakins.com` → VM,
   web still on hosted Supabase. Vercel stays deployed as the rollback. Expect
   +20 ms per query (Indiana ↔ us-east-1) for the duration. Run one to two weeks.
   Gate: a quiet week; trailing-slash and `/api/v1` paths confirmed through the proxy.
7. **Phase 2, database cutover** (checklist below). Pause, never delete, the
   hosted project.
8. **Decommission** after two quiet weeks: delete the hosted project, cancel Pro,
   Vercel → Hobby or delete, rewrite the Infrastructure section of `AGENTS.md`.

## First boot (step 3)

```bash
# as the exouser user on the VM (the Jetstream2 image's login user). Exosphere has already mounted the volume at
# /media/volume/campfire-prod (device /dev/sdb); bootstrap.sh adopts that mount
# by making /srv/campfire a symlink to it. Only on a box without the Exosphere
# mount pass --volume-device /dev/sdb and it formats/mounts by UUID itself.
sudo git clone https://github.com/hollisakins/campfire.git /tmp/campfire-repo
sudo /tmp/campfire-repo/ops/scripts/bootstrap.sh            # creates /srv/campfire first
sudo mv /tmp/campfire-repo /srv/campfire/repo && sudo chown -R exouser:exouser /srv/campfire/repo
sudo /srv/campfire/repo/ops/scripts/bootstrap.sh            # second pass installs the timers
# log out and in (docker group), then:
cd /srv/campfire/repo/ops
cp .env.example .env && chmod 600 .env          # fill every blank
touch web.env && chmod 600 web.env              # block at the bottom of .env.example
# Cloudflare origin certificate (dashboard → SSL/TLS → Origin Server, 15 years,
# hostnames hollisakins.com, *.hollisakins.com):
#   /srv/campfire/certs/origin.pem, origin.key   (chmod 600)
docker compose up -d db && docker compose logs -f db        # wait for "database system is ready"
# The health check passes even when the image's init aborted half-way. Verify
# the init really completed before anything else touches the cluster:
docker exec campfire-db psql -U postgres -d postgres -Atc \
  "select count(*) from pg_namespace where nspname in ('vault','graphql','realtime')"   # must print 3
docker exec campfire-db psql -U postgres -d postgres -Atc \
  "select proowner::regrole from pg_proc where proname='uid' and pronamespace='auth'::regnamespace"  # supabase_auth_admin
# Anything else: stop db, move db/data aside, fix the cause, start again.
docker compose up -d
docker compose ps
curl -s http://127.0.0.1:3000/api/health
```

Hostnames are deliberately **first-level** (`campfire-<thing>.hollisakins.com`):
the free Universal SSL edge certificate covers `hollisakins.com` and
`*.hollisakins.com` only, so a proxied `x.campfire.hollisakins.com` fails the TLS
handshake (alert 40) before reaching the VM. The origin certificate on the VM is
issued for the same two names, so any first-level host works without reissuing.

Cloudflare, via the API token (DNS Edit, Cache Rules, WAF, SSL): A records for
`campfire-staging` and `campfire-api` → VM floating IP, proxied; SSL mode Full
(strict); a cache rule for `/_next/static/*`; the one free rate-limiting rule on
`campfire-api.hollisakins.com/auth/v1/*`; "Normalize incoming URLs" rewriting
off (it broke paths on the earlier proxy attempt).

TLS alternative: Let's Encrypt via DNS challenge needs Caddy built with the
Cloudflare DNS module (`caddy:2-builder` + `github.com/caddy-dns/cloudflare`) and
a scoped token in the container. The origin-certificate path above needs neither
and is what `Caddyfile` implements.

Web image: until step 5 exists, build it on the VM and tag it locally:

```bash
cd /srv/campfire/repo && docker build -f web/Dockerfile \
  --build-arg NEXT_PUBLIC_SUPABASE_URL=https://campfire-api.hollisakins.com \
  --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY="$ANON_KEY" \
  --build-arg NEXT_PUBLIC_APP_URL=https://campfire-staging.hollisakins.com \
  --build-arg NEXT_PUBLIC_WORKER_DOWNLOAD_URL=https://campfire-download.hollisakins.com \
  --build-arg GIT_SHA="$(git rev-parse HEAD)" \
  -t ghcr.io/hollisakins/campfire-web:"$(git rev-parse HEAD)" web/
```

`NEXT_PUBLIC_*` values are baked at build. For step 3 (web on hosted Supabase)
build with the hosted URL and anon key; Phase 2 is a rebuild with the new ones.

## Phase 1 operating notes

- `DEPLOY_MIGRATIONS=skip` in `.env` while the web runs against the hosted
  project: `deploy.sh` swaps the image only; migrations keep landing through the
  Supabase GitHub integration on merge. The first automatic deploy (run
  40119771568) failed exactly here before the switch existed.
- The GHCR package created by the workflow inherits the repository's public
  visibility, so the VM pulls anonymously; no registry login on the host.
- The web container's `WORKER_JWT_SECRET` / `CDN_FRONT_URL` stay unset until the
  Phase 1 cutover rotates the Worker secret; sidecars stream directly meanwhile.

## Database cutover checklist (Phase 2)

Prepare in the days before:

- [ ] Export from the hosted dashboard, by hand (none of it is in the repo):
      Auth → URL Configuration (site url, redirect list) → `.env`;
      Auth → Rate Limits → `RATE_LIMIT_*`; Auth → Email Templates → five HTML
      files in `/srv/campfire/auth-templates/`; Auth → SMTP (Resend,
      `team@campfire.hollisakins.com`) → `SMTP_*`; Settings → API → JWT secret →
      `JWT_SECRET`, anon + service keys, Max rows → `PGRST_DB_MAX_ROWS`.
      **Every exported value lives in two places: the 1Password vault and the
      VM's `ops/.env`.** The hosted project is deleted at decommission, so
      1Password is the only copy that outlives it.
- [ ] GoTrue version: hosted shows it at `https://<ref>.supabase.co/auth/v1/health`;
      the `auth` image tag in `docker-compose.yml` must be ≥ it (migrations only
      run forward). Record the hosted version in 1Password with the rest.
- [ ] Hosted Postgres version (17.6.1.025 on 2026-10-24) ≤ the `db` image tag.
- [ ] Rehearsal (step 4) done on a fresh dump, with timings written here:
      dump ____ min, restore ____ min, verification ____ min.
      (2026-10-24 rehearsal, 2.2 GB / 183 users / 81,313 spectra: dump 70 s,
      restore 2 min, verification 1 min; `scripts/restore-hosted.sh` ended with
      the fingerprint identical to the hosted project.)
- [ ] Updates page notice with the window; `dr status` shows no freeze and no deploy lease.

The window (target under 1 h):

1. **Take the old API down** so nothing written after the dump is lost.
   A `default_transaction_read_only` setting is *not* a freeze: PostgREST opens
   every POST / PATCH / PUT / DELETE and every volatile RPC as an explicit
   `READ WRITE` transaction, which overrides the role default. The only
   enforceable gate is to stop the two roles the API logs in as, then kill
   their pooled sessions (which never log in again on their own):
   ```sql
   alter role authenticator nologin;         -- PostgREST
   alter role supabase_auth_admin nologin;   -- GoTrue
   select pg_terminate_backend(pid) from pg_stat_activity
     where usename in ('authenticator', 'supabase_auth_admin');
   ```
   Reads stop too; the site shows errors for the window, which the Updates
   notice announced. **Verify before dumping** with a real mutation that must
   be rejected, for example with the service key against a scratch row:
   ```bash
   curl -s -X PATCH "$OLD_URL/rest/v1/user_profiles?id=eq.00000000-0000-0000-0000-000000000000" \
     -H "apikey: $SERVICE_KEY" -H "Authorization: Bearer $SERVICE_KEY" \
     -H "Content-Type: application/json" -d '{"display_name":"x"}'
   # expect an auth/connection error from PostgREST, never 204
   ```
   Also confirm `select count(*) from pg_stat_activity where usename in
   ('authenticator','supabase_auth_admin')` is 0 and stays 0 for a minute.
   Reverts with `alter role authenticator login; alter role supabase_auth_admin
   login;` (sessions reconnect on the next request).
2. **Dump** on the VM (the dump dir is under the mounted `backups/` volume):
   ```bash
   ops/scripts/dump-hosted.sh "$OLD" /srv/campfire/backups/cutover-$(date +%F)
   ```
   `$OLD` is the hosted connection string **through the IPv4 session pooler**
   (`aws-1-<region>.pooler.supabase.com:5432`, user `postgres.<ref>`); the
   direct `db.<ref>.supabase.co` host is IPv6-only and the VM has no IPv6.
   The script runs the CLI dumps (`roles.sql`, `schema.sql`, `data.sql` as COPY)
   plus `pg_dump --schema=supabase_migrations` (the CLI dump **excludes the
   `auth` schema DDL** — its rows are in `data.sql` — and **excludes
   `supabase_migrations` entirely**), writes `cron_jobs.sql` (the pg_cron jobs
   are not dumped), `reference.txt` (row counts) and `fingerprint-hosted.txt`
   (`ops/sql/fingerprint.sql`: grants, RLS, policies, triggers, indexes,
   extensions, default privileges). It warns if `roles.sql` lacks the
   `ALTER ROLE authenticator SET pgrst.db_pre_request` setting the release
   header hook depends on.
3. **Restore** into a **freshly initialised** cluster (see "First boot": the
   init must have completed; the script refuses a database that already has
   tables in `public`):
   ```bash
   docker compose up -d db
   ops/scripts/restore-hosted.sh /srv/campfire/backups/cutover-<date>
   ```
   What it does, and the traps each step exists for (all hit in the 2026-10-24
   rehearsal):
   - starts `auth` first and waits for GoTrue, which creates and migrates the
     `auth` schema itself (`schema.sql` has no `auth.*` DDL; loading `data.sql`
     before GoTrue is done fails on a missing column);
   - **clears the image's default privileges for `postgres` in `public`** before
     loading the schema. pg_dump writes ACLs relative to the built-in defaults,
     but the supabase/postgres image pre-sets `ALTER DEFAULT PRIVILEGES … GRANT
     ALL … TO anon, authenticated, service_role`, so a plain restore handed
     `anon` every table (490 grants; `share_links.link_password` readable by
     `authenticated`). The dump re-creates the hosted default privileges at its
     end, so the result is exact;
   - loads `roles.sql`, `schema.sql`, `migrations_history.sql`, then `data.sql`
     with triggers off (`session_replication_role = replica`) and the empty
     `storage.*` blocks removed (no Storage service, the tables do not exist;
     with them in, `ON_ERROR_STOP` aborts before the sequence resets at the end
     of the file);
   - `REFRESH`es the three materialized views (restored `WITH NO DATA`; the
     nightly `refresh_all_matviews()` uses `CONCURRENTLY`, which needs a
     populated view), re-creates the pg_cron jobs from `cron_jobs.sql`, `ANALYZE`.
4. **Verify** (the same script continues; `--verify` re-runs this part alone):
   row counts vs `reference.txt`; `supabase migration list` must show every
   file in `supabase/migrations/` applied and nothing db-only (otherwise
   `deploy.sh` replays the chain; `supabase migration repair --status applied
   <version>` fixes a gap); the `supabase/tests/check_*.sql` guards — the six
   that reference the seed's `DR-seed` release fail on production data and are
   counted separately, `check_dr_freeze.sql` is skipped (it freezes the whole
   catalog inside its transaction: instant on the seed, 40 min on 81k spectra),
   every other guard must pass; the fingerprint must be
   **identical** to `fingerprint-hosted.txt`. The script exits non-zero on any
   failure. Spot checks by hand: `select jobname from cron.job` (two jobs),
   `select matviewname, ispopulated from pg_matviews` (all true).
5. **Bring up the stack**, rebuild the web image with
   `NEXT_PUBLIC_SUPABASE_URL=https://campfire-api.hollisakins.com` and the same
   anon key, `deploy.sh <sha>`. Update `web.env`: `SUPABASE_SERVICE_ROLE_KEY`,
   `SUPABASE_JWT_SECRET` unchanged (same secret), nothing else moves.
6. **Smoke test**: sign in with a real account (existing cookies remain valid:
   same JWT secret), open an object page, `campfire sync` from a laptop, send
   a password reset to yourself (Resend path), check `/api/health`.
7. **Pause** the hosted project (Dashboard → Settings → General → Pause).
   Delete it after two quiet weeks. Then cancel the subscriptions.

Rollback at any point before step 7: `alter role authenticator login;`
`alter role supabase_auth_admin login;` on the old database and redeploy
the previous image (built against the hosted URL).

**Known follow-up (not in Phase 0):** the web container's server-side Supabase
calls go out through `NEXT_PUBLIC_SUPABASE_URL`, i.e. back in through
Cloudflare, whose 100 s edge timeout is shorter than the 120 s
`statement_timeout` the sync RPCs run under. A server-only internal URL
(`http://kong:8000`) for `createServiceClient()` / the server clients removes
the round trip and the timeout; until then the first page of a sync stream can
524 under load.

## Rollback

- **Web** (Phase 1): Cloudflare DNS `campfire.hollisakins.com` back to the
  Vercel CNAME. Vercel stays deployed until decommission.
- **Database** (Phase 2): unpause the hosted project, restore `login` on
  `authenticator` and `supabase_auth_admin`, redeploy the image built against
  the hosted URL. Writes made on the VM between cutover and rollback are
  lost; hence the two quiet weeks before deletion.
- **A bad deploy**: `scripts/deploy.sh <previous full sha>` (`deploys.log` has
  the history; `deploy.sh` itself rolls back when `/api/health` fails).

## Day 2

- Health: `curl -s http://127.0.0.1:3000/api/health`; external monitor on
  `https://campfire.hollisakins.com/api/health`; heartbeat pings from the three
  timers (`HEALTHCHECK_PING_URL*`).
- Logs: `docker compose logs -f --tail 200 <service>`; `journalctl -u campfire-backup`.
- psql: `docker exec -it campfire-db psql -U supabase_admin -d postgres`;
  slow queries in `pg_stat_statements`; statements over 1 s are logged
  (`PG_LOG_MIN_DURATION_MS`).
- Studio: `ssh -L 8000:127.0.0.1:8000 <vm>` → http://localhost:8000
  (`DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`).
- Backups: `/srv/campfire/backups/last-success`, `last-restore-check`;
  restore by hand with `pg_restore -U supabase_admin -d postgres --clean --if-exists <dump>`.
- Resize: Exosphere → instance → Resize (or `openstack server resize`), then
  adjust `PG_*` in `.env` and `docker compose up -d db`.
- Updates: `docker compose pull && docker compose up -d` for the Supabase
  images (bump the tags in `docker-compose.yml` first, via a PR); the OS takes
  security updates unattended and never reboots on its own.
- Image versions pinned here (2026-10-24): postgres 17.6.1.136, gotrue
  v2.197.0, postgrest v14.18, kong 3.9.3, postgres-meta v0.100.0, studio
  2026.10.12, caddy 2.10.

## Jetstream2 specifics

- **Allocation expiry shelves the instance.** Put the ACCESS allocation end
  date in a calendar with reminders at 60 and 30 days. Storage (GB) is a
  separate allocation line from compute (SU).
- Maintenance windows are announced on the Jetstream2 status list; the stack
  comes back on its own after a reboot (`restart: unless-stopped`, Docker
  `live-restore`), but check `docker compose ps` afterwards.
- The floating IP survives reboots and resizes; it does not survive deleting
  the instance. DNS lives in Cloudflare so a new IP is one API call.
- Publications using the portal should acknowledge Jetstream2
  (Hancock et al. 2021, PEARC '21).

## Changelog

Dated, one line per change to `.env` / `web.env` / `docker-compose.yml` on the
host. Variable names only, never values.

- 2026-10-24 — scaffold (Phase 0 PR).
- 2026-10-24 — `.env`: `WEB_HOST`/`API_HOST`/`SITE_URL`/`API_EXTERNAL_URL`/`SUPABASE_PUBLIC_URL` → first-level hostnames; `WEB_TAG` = deployed sha; `DEPLOY_MIGRATIONS=skip` (Phase 1). `web.env` created from the Vercel production env + local creds; `WORKER_JWT_SECRET`/`CDN_FRONT_URL` left unset until the Phase 1 rotation.
- 2026-10-24 — `docker-compose.yml`: `db/config` bind mount → `db-config` named volume (first `db` start failed: empty mount hid the image's include files).
- 2026-10-24 — `volumes/db/roles.sql` guarded (init aborted on the absent `supabase_functions_admin`; cluster re-initialised twice during the rehearsal, `db/data.broken-init-*` kept aside on the volume until the rehearsal passes).
- 2026-10-24 — restore rehearsal passed (third attempt): `scripts/dump-hosted.sh` + `scripts/restore-hosted.sh` + `sql/fingerprint.sql` replace the hand-run dump/restore commands in the cutover checklist. Found and fixed on the way: the image's default privileges leaking `anon` grants into the restore, the `storage.*` data blocks aborting the load before the sequence resets, matviews restored unpopulated, pg_cron jobs not dumped. The VM's `campfire-db` now holds a verified copy of the hosted database as of 2026-10-24 18:17 UTC (not live; Phase 2 redoes the restore from a fresh dump). The five `db/data.broken-init-*` directories (failed inits and the superseded rehearsal clusters, ~8 GB) can be deleted.
