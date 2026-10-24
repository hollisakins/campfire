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
        └── $API_HOST  api.campfire.hollisakins.com  → kong :8000
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
   `dev.campfire.hollisakins.com` (already an allowed origin on the download
   Worker) and `api.campfire.hollisakins.com`, web still pointed at hosted
   Supabase. Security group → 22 (your IPs) and 443 (Cloudflare ranges) only.
   Gate: staging renders against hosted Supabase.
4. **Restore rehearsal.** Read-only dump of production into the VM Postgres,
   staging repointed at the local API. Compare counts, run the SQL guards, log in,
   open an object page, run `campfire sync` against staging. Gate: a cutover
   checklist with measured timings (below, filled in).
5. **Deploy workflow** (`.github/workflows/deploy.yml`, not yet written): on push
   to `main` build the image on a GitHub runner, push to GHCR `:<sha>`, SSH with a
   forced-command deploy key, `scripts/deploy.sh <sha>`. Gate: a merge to main
   lands on staging unattended. No self-hosted runner: the repo is public.
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
# as the ubuntu user on the VM
sudo mkdir -p /srv/campfire && sudo mount /dev/vdb /srv/campfire      # or bind-mount Exosphere's /media/volume/<name>
echo '/dev/vdb /srv/campfire ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo git clone https://github.com/hollisakins/campfire.git /srv/campfire/repo
sudo chown -R ubuntu:ubuntu /srv/campfire/repo
sudo /srv/campfire/repo/ops/scripts/bootstrap.sh
# log out and in (docker group), then:
cd /srv/campfire/repo/ops
cp .env.example .env && chmod 600 .env          # fill every blank
touch web.env && chmod 600 web.env              # block at the bottom of .env.example
# Cloudflare origin certificate (dashboard → SSL/TLS → Origin Server, 15 years,
# hostnames campfire.hollisakins.com, *.campfire.hollisakins.com):
#   /srv/campfire/certs/origin.pem, origin.key   (chmod 600)
docker compose up -d db && docker compose logs -f db        # wait for "database system is ready"
docker compose up -d
docker compose ps
curl -s http://127.0.0.1:3000/api/health
```

Cloudflare, via the API token (DNS Edit, Cache Rules, WAF, SSL): A records for
`dev.campfire` and `api.campfire` → VM floating IP, proxied; SSL mode Full
(strict); a cache rule for `/_next/static/*`; the one free rate-limiting rule on
`api.campfire.hollisakins.com/auth/v1/*`; "Normalize incoming URLs" rewriting
off (it broke paths on the earlier proxy attempt).

TLS alternative: Let's Encrypt via DNS challenge needs Caddy built with the
Cloudflare DNS module (`caddy:2-builder` + `github.com/caddy-dns/cloudflare`) and
a scoped token in the container. The origin-certificate path above needs neither
and is what `Caddyfile` implements.

Web image: until step 5 exists, build it on the VM and tag it locally:

```bash
cd /srv/campfire/repo && docker build -f web/Dockerfile \
  --build-arg NEXT_PUBLIC_SUPABASE_URL=https://api.campfire.hollisakins.com \
  --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY="$ANON_KEY" \
  --build-arg NEXT_PUBLIC_APP_URL=https://dev.campfire.hollisakins.com \
  --build-arg NEXT_PUBLIC_WORKER_DOWNLOAD_URL=https://campfire-download.hollisakins.com \
  --build-arg GIT_SHA="$(git rev-parse HEAD)" \
  -t ghcr.io/hollisakins/campfire-web:"$(git rev-parse HEAD)" web/
```

`NEXT_PUBLIC_*` values are baked at build. For step 3 (web on hosted Supabase)
build with the hosted URL and anon key; Phase 2 is a rebuild with the new ones.

## Database cutover checklist (Phase 2)

Prepare in the days before:

- [ ] Export from the hosted dashboard, by hand (none of it is in the repo):
      Auth → URL Configuration (site url, redirect list) → `.env`;
      Auth → Rate Limits → `RATE_LIMIT_*`; Auth → Email Templates → five HTML
      files in `/srv/campfire/auth-templates/`; Auth → SMTP (Resend) → `SMTP_*`;
      Settings → API → JWT secret → `JWT_SECRET`, anon + service keys, Max rows
      → `PGRST_DB_MAX_ROWS`.
- [ ] GoTrue version: hosted shows it at `https://<ref>.supabase.co/auth/v1/health`;
      the `auth` image tag in `docker-compose.yml` must be ≥ it (migrations only run forward).
- [ ] Hosted Postgres version (17.6.1.025 on 2026-10-24) ≤ the `db` image tag.
- [ ] Rehearsal (step 4) done on a fresh dump, with timings written here:
      dump ____ min, restore ____ min, verification ____ min.
- [ ] Updates page notice with the window; `dr status` shows no freeze and no deploy lease.

The window (target under 1 h):

1. **Freeze writes on the old database** so nothing written after the dump is lost:
   ```sql
   alter role authenticated set default_transaction_read_only = on;
   alter role anon set default_transaction_read_only = on;
   alter role service_role set default_transaction_read_only = on;
   ```
   (reverts with `reset default_transaction_read_only`; sessions pick it up on reconnect.)
2. **Dump** with the CLI against the hosted connection string:
   ```bash
   supabase db dump --db-url "$OLD" -f roles.sql --role-only
   supabase db dump --db-url "$OLD" -f schema.sql
   supabase db dump --db-url "$OLD" -f data.sql --data-only --use-copy
   ```
   `roles.sql` carries the `ALTER ROLE authenticator SET pgrst.db_pre_request`
   setting the release header hook depends on; check it is there.
3. **Restore** into the VM (`db` up, nothing else):
   ```bash
   psql "$NEW" -f roles.sql
   psql "$NEW" -f schema.sql
   psql "$NEW" -v ON_ERROR_STOP=1 -c 'set session_replication_role = replica' -f data.sql
   ```
   Then `supabase migration list --db-url "$NEW"`: the history table
   `supabase_migrations.schema_migrations` must list every migration in
   `supabase/migrations/`. If the dump left it out, recreate it from the old
   database (`pg_dump -t supabase_migrations.schema_migrations`) or
   `supabase migration repair --status applied <version>` per file. Without it
   the first `deploy.sh` replays the whole chain.
4. **Verify**: `select count(*)` on `spectra`, `objects`, `storage_objects`,
   `auth.users` against the old numbers; `supabase/tests/check_*.sql` guards;
   `select jobname from cron.job` shows the two jobs.
5. **Bring up the stack**, rebuild the web image with
   `NEXT_PUBLIC_SUPABASE_URL=https://api.campfire.hollisakins.com` and the same
   anon key, `deploy.sh <sha>`. Update `web.env`: `SUPABASE_SERVICE_ROLE_KEY`,
   `SUPABASE_JWT_SECRET` unchanged (same secret), nothing else moves.
6. **Smoke test**: sign in with a real account (existing cookies remain valid:
   same JWT secret), open an object page, `campfire sync` from a laptop, send
   a password reset to yourself (Resend path), check `/api/health`.
7. **Pause** the hosted project (Dashboard → Settings → General → Pause).
   Delete it after two quiet weeks. Then cancel the subscriptions.

Rollback at any point before step 7: `reset` the three roles on the old
database and redeploy the previous image (built against the hosted URL).

## Rollback

- **Web** (Phase 1): Cloudflare DNS `campfire.hollisakins.com` back to the
  Vercel CNAME. Vercel stays deployed until decommission.
- **Database** (Phase 2): unpause the hosted project, `reset` the read-only
  roles, redeploy the image built against the hosted URL. Writes made on the VM
  between cutover and rollback are lost; hence the two quiet weeks before deletion.
- **A bad deploy**: `scripts/deploy.sh <previous sha>` (`deploys.log` has the
  history; `deploy.sh` itself rolls back when `/api/health` fails).

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
