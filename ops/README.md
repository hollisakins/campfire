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
Cloudflare edge (DNS, TLS, static cache, rate limits)   two proxied zones: campfire-jwst.org
        │ https, security group admits 443 from Cloudflare ranges only   (primary) and hollisakins.com
        ▼                                                                (the pre-move names, see "Domain move")
Caddy :443 (ops/Caddyfile), routes by hostname
        ├── $WEB_HOST  campfire-jwst.org             → web  :3000  (Next.js, web/Dockerfile)
        ├── $API_HOST  api.campfire-jwst.org         → kong :8000
        ├── $ALT_WEB_HOST campfire.hollisakins.com   → 301 to $WEB_HOST; /api/v1/* served
        └── $ALT_API_HOST campfire-api.hollisakins.com → kong :8000 (alias)
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

## Public read-only SQL (`campfire_reader`)

`docs/reader-role.md` is the reader's side. The host side is one compose
profile and one script, and it is the one sanctioned exception to "only Caddy
listens publicly" (Catalog Read Model phase 4; the role sees public rows only,
under its own RLS policies, with no function and no other table):

1. The role exists once the migration chain carries `20261028193000_phase4_reader_role.sql`
   (`deploy.sh` applies it). It has no password until step 2.
2. `bash scripts/reader-setup.sh` (from `ops/`): generates `READER_PASSWORD` into
   `.env` when empty, sets it on the role, writes the PgBouncer SCRAM userlist
   (`$CAMPFIRE_DATA_ROOT/pgbouncer/userlist.txt`, that one role only, from the
   verifier in `pg_authid`), makes a self-signed certificate
   (`certs/reader.pem` / `reader.key`) when missing, starts the pooler
   (`docker compose --profile reader up -d pgbouncer`, image
   `edoburu/pgbouncer`, transaction mode, 0.0.0.0:6432) and runs one query
   through it. `rotate` changes the password, `status` reports.
3. Security group: admit TCP 6432 from anywhere (the password is the only
   credential by design, the role's grants are the gate).
4. DNS: `db.$WEB_HOST` as a DNS-only (grey cloud) A record to the VM:
   Cloudflare's proxy carries no Postgres.
5. Publish the connection string the script prints (docs, the portal's docs page).

The pooler is off unless the profile is given, so a stack without a reader
password runs unchanged. Changelog entries for `READER_*` go below like any
other `.env` change.

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

## Web cutover (step 6) — preparation and flip

`campfire.hollisakins.com` is a *proxied* Cloudflare record, so the flip and the
rollback are both one record edit with no propagation wait. Prepared on
2026-10-24; the flip itself is a separate, explicit go.

Preparation (no user impact; all reversible):

1. Caddy serves both hostnames: `WEB_HOST="campfire.hollisakins.com,
   campfire-staging.hollisakins.com"` in `.env`, `docker compose up -d caddy`.
   The admin API shows the match set:
   `docker exec campfire-caddy wget -qO- http://127.0.0.1:2019/config/apps/http/servers/srv0/routes/0/match`.
2. Pre-flight the production hostname against the VM origin **without touching
   DNS**, from a laptop (TLS, Caddy routing and the app under the production Host
   header; `-k` because the origin cert chains to Cloudflare's Origin CA):
   ```bash
   curl -sk --resolve campfire.hollisakins.com:443:149.165.174.96 https://campfire.hollisakins.com/api/health
   ```
3. Production URL baked into the image: GitHub variable
   `DEPLOY_NEXT_PUBLIC_APP_URL=https://campfire.hollisakins.com`, then a manual
   run of the Deploy workflow for the current main sha (it rebuilds). Next.js
   inlines `NEXT_PUBLIC_*` at build time into the client **and** the server
   bundles, so the values in `web.env` are documentation only — a change needs
   a rebuild, never just a container recreate. Server-only runtime overrides
   are the plain names (`SUPABASE_URL` for the server-side Supabase clients).
4. Secrets. `WORKER_JWT_SECRET` and `JWT_SECRET` could not be read back from
   Vercel (sensitive variables are write-only), so:
   - `JWT_SECRET`: the VM's generated value is copied *into* Vercel so both sides
     issue interchangeable tokens. This is a **key change for Vercel**, with two
     user-visible effects (the old value is unrecoverable, so they could only be
     timed, not avoided; done 2026-10-24 22:44 UTC): (a) a CLI session holding a
     1 h access token signed with the old key gets 401s until its *scheduled*
     refresh (`APISession` refreshes on local expiry, not on a 401) — up to 55 min
     — then recovers without a new login (refresh tokens are hashed, not signed;
     API keys are hashed too); (b) bulk-download scripts generated before the
     change embed a 30-day download token signed with the old key and are
     invalid from that moment, with no refresh path: users regenerate them from
     the site. Post an Updates notice saying so.
   - `WORKER_JWT_SECRET`: rotated. Order that keeps the mismatch window to
     seconds: new value into Vercel's production env → Vercel redeploy (the
     *operator* runs it; a production deploy is not something the ops session
     triggers) → the same value onto the Worker (`PUT
     /accounts/<id>/workers/scripts/campfire-download/secrets`, name `JWT_SECRET`)
     → `WORKER_JWT_SECRET` + `CDN_FRONT_URL` into `web.env`, `deploy.sh <sha>`.
     Verify `/api/spectrum/sidecars` on staging now returns
     `campfire-download.hollisakins.com/o/...` urls instead of streaming.
5. Cloudflare checked: Universal SSL covers the first-level name, origin cert
   covers `*.hollisakins.com`, no WAF / rate-limit / cache rule touches the host,
   the apex rewrite rule is scoped to apex + www. Worker `ALLOWED_ORIGINS`
   already lists production and staging. Hosted Auth site url + redirect list
   already name production. Vercel's daily snapshot cron keeps running against
   the Vercel deployment in parallel with the VM timer until decommission
   (duplicate builds; the builder keeps the newest three).

The flip: change the `campfire.hollisakins.com` CNAME
(`8de26addbf5c12e9.vercel-dns-017.com`, proxied) to a proxied `A 149.165.174.96`.
Then: `/api/health` through the production name, sign in (cookies survive: same
host, same `SUPABASE_JWT_SECRET`), an object page, a spectrum download, a small
`campfire sync`, a password-reset email and its link, an OG image preview.
Rollback at any time during the two quiet weeks: restore the CNAME.

## Phase 2 dress rehearsal — staging on the self-hosted stack

Everything in the database cutover except the freeze, the fresh dump and the
production image rebuild can be proven first, with production untouched:
staging becomes the complete self-hosted stack on a restored copy of the real
data, and the operator tests it as a real user.

1. A verified restore in `campfire-db` (`scripts/restore-hosted.sh`), then
   `docker compose up -d kong rest auth`. Check from a laptop with the anon key:
   `https://$API_HOST/auth/v1/health` reports GoTrue's version; `/rest/v1/`
   with the anon key is refused (anon holds no table grants, as on hosted).
2. Build the staging image on the VM against the self-hosted API (the deployed
   image keeps the hosted URL baked in, so production is unaffected):
   ```bash
   cd /srv/campfire/repo
   docker build web -f web/Dockerfile -t campfire-web:staging-selfhosted \
     --build-arg NEXT_PUBLIC_SUPABASE_URL=https://campfire-api.hollisakins.com \
     --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY=<ANON_KEY from .env> \
     --build-arg NEXT_PUBLIC_APP_URL=https://campfire-staging.hollisakins.com \
     --build-arg NEXT_PUBLIC_WORKER_DOWNLOAD_URL=https://campfire-download.hollisakins.com \
     --build-arg GIT_SHA=$(git rev-parse HEAD)
   ```
3. `ops/web-staging.env` = `web.env` with `NEXT_PUBLIC_SUPABASE_URL` and
   `SUPABASE_URL` pointed at `https://campfire-api.hollisakins.com` and
   `NEXT_PUBLIC_APP_URL` at the staging hostname (mode 600, gitignored).
4. `docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d web-staging caddy`
   — Caddy now routes `WEB_STAGING_HOST` to `web-staging`; `deploy.sh` keeps
   managing production alone.
5. Test on staging as a real user: sign in with a real account (the password
   hashes travel with the dump, so this exercises GoTrue against real
   credentials), object page, spectrum download, `campfire sync` against the
   staging host, a password-reset email sent by the self-hosted GoTrue through
   Resend, admin pages, a comment. Then the server-side internal path: set
   `SUPABASE_URL=http://kong:8000` in `web-staging.env` only and repeat. The
   server-side clients prefer `SUPABASE_URL` (runtime, server-only) over
   `NEXT_PUBLIC_SUPABASE_URL`; the latter is inlined at build time into **both**
   the client and the server bundles, so changing it in an env file does
   nothing. This removes Cloudflare's 100 s edge timeout from the sync RPCs and
   is the intended Phase 2 configuration if it holds.
6. Let one real merge land its migration on the copy: `DEPLOY_MIGRATIONS=local`
   is exercised by `deploy.sh` against the local database (production web is
   unaffected: in Phase 1 it still talks to the hosted project).
7. Tear down when the window is scheduled: `docker compose -f docker-compose.yml
   -f docker-compose.staging.yml rm -sf web-staging`, then `up -d --no-deps caddy`.
   The cutover restores from a fresh dump into a re-initialised cluster.

**Results (2026-10-25, 00:33–01:45 UTC):** steps 1–6 passed. Sign-in with real
credentials, browsing, downloads, `campfire sync` and the password-reset email
round trip (self-hosted GoTrue → Resend → staging) all worked; round 2 with
`SUPABASE_URL=http://kong:8000` kept the session (the cookie is named after the
public url, #631) and server renders stopped appearing on the API hostname in
the access log. `deploy.sh` with `DEPLOY_MIGRATIONS=local` reported "Remote
database is up to date" against the copy. Found and fixed on the way: the three
auth-link routes hard-coded the hosted project's verify url (#635 — every reset,
invite and email change would have broken after the cutover); `restore-check`
ran as `--role=postgres` and reported `users=0` (#634). Known, not fixed:
`ResetPasswordForm` snapshots the session right after load and can race the
PKCE code exchange on an uncached page load (same code on production; small web
fix after Phase 2). `web-staging` stays up on the internal path until the window.

## Database cutover checklist (Phase 2) — executed 2026-10-25, kept as the rollback and re-run reference

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

1. **Freeze writes** so nothing written after the dump is lost. On hosted
   Supabase the obvious gate is unavailable: `alter role authenticator nologin`
   fails with "reserved role, only superusers can modify it" (supautils), and a
   `default_transaction_read_only` setting is overridden by PostgREST's explicit
   `READ WRITE` transactions. What works is the dashboard switch: **Project
   Settings → Data API → Enable Data API → off**. PostgREST then answers 503
   (`PGRST002`) to every request, reads included; the site shows errors for the
   window. GoTrue has no such switch and keeps accepting writes to `auth.*`
   (sign-ins, token refreshes, and — via `supabase.auth.updateUser` from an
   already signed-in browser — password changes), none of which reach the
   restored database. Narrow that: **Authentication → Sign In / Providers →
   Email → disable** for the window (no new sign-ins or recovery emails), and
   before going live compare the hosted auth tables against the dump time:
   ```sql
   select count(*) from auth.users where updated_at > '<dump start utc>';
   select count(*) from auth.audit_log_entries where created_at > '<dump start utc>';
   ```
   Anything non-zero is a user to contact (a password changed in the window is
   back to its previous value on the new stack). At the 2026-10-25 cutover both
   were 0. **Verify before dumping** with the service key against a scratch row
   (expect 503, never 204):
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X PATCH "$OLD_URL/rest/v1/user_profiles?user_id=eq.00000000-0000-0000-0000-000000000000" \
     -H "apikey: $SERVICE_KEY" -H "Authorization: Bearer $SERVICE_KEY" \
     -H "Content-Type: application/json" -d '{"display_name":"x"}'
   ```
   Reverts by switching the Data API back on (seconds).
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
7. **Afterwards**: Dashboard → Integrations → GitHub → disconnect (merges must
   no longer push migrations to the old project; `deploy.sh` owns them with
   `DEPLOY_MIGRATIONS=local`). Leave the hosted project running with the Data
   API off for a day or two as the fast rollback, then pause it (Dashboard →
   Settings → General → Pause), delete it after two quiet weeks, then cancel
   the Supabase and Vercel subscriptions. Laptops that deploy in service-role
   mode point `CAMPFIRE_SUPABASE_URL` at the new API host; login mode needs
   nothing.

Rollback at any point before the hosted project is deleted, **in this order**:
switch its Data API back on and re-enable the Email provider if step 1
disabled it (otherwise nobody can sign in or recover a password); revert
`SUPABASE_URL` / `NEXT_PUBLIC_SUPABASE_URL` in `web.env` to the hosted url and
`SITE_URL` in `.env`; *then*
`deploy.sh <last sha built with the hosted url>` (1c6a4eaf at the 2026-10-25
cutover). The env comes first because `deploy.sh` recreates the container with
whatever `web.env` holds, and that image reads `SUPABASE_URL` at runtime — with
`http://kong:8000` still in place it would keep talking to the VM database.
Writes made on the VM in between are lost.

**Known follow-up (not in Phase 0):** the web container's server-side Supabase
calls go out through `NEXT_PUBLIC_SUPABASE_URL`, i.e. back in through
Cloudflare, whose 100 s edge timeout is shorter than the 120 s
`statement_timeout` the sync RPCs run under. A server-only internal URL
(`http://kong:8000`) for `createServiceClient()` / the server clients removes
the round trip and the timeout; until then the first page of a sync stream can
524 under load.

## Domain move (Phase 3) — campfire-jwst.org

The portal moves off the personal domain: Princeton's network classifies
`hollisakins.com` as spyware, and a project should not live under a person's
name anyway. Every old name keeps working as a redirect or an alias, so no
announcement lead time is needed; the one visible effect is that everybody
signs in once more (the session cookie is per host). Nothing verifies the JWT
issuer (`web/lib/auth/supabase-jwt.ts` checks signature, `aud` and `role`), so
changing `API_EXTERNAL_URL` only mints new tokens with the new `iss`.

| Role | Until the move | From the move |
|---|---|---|
| Portal | `campfire.hollisakins.com` | `campfire-jwst.org` (old name → 301 for browsers; `/api/v1/*` keeps being served, because HTTP clients drop the bearer token on a cross-host redirect) |
| Staging | `campfire-staging.hollisakins.com` | `staging.campfire-jwst.org` (old → 301) |
| Supabase API | `campfire-api.hollisakins.com` | `api.campfire-jwst.org` (old stays a live **alias** for months: stored CLI refresh tokens, `CAMPFIRE_SUPABASE_URL` on laptops, `redirect_to` in old emails) |
| Download Worker | `campfire-download.hollisakins.com` | `download.campfire-jwst.org` (old route stays until the 30-day tokens in generated scripts expire) |
| Tiles (R2 custom domain) | `campfire-tiles.hollisakins.com` | `tiles.campfire-jwst.org` (stored in `map_layers.tile_base_url` / `fitsgl_datasets.fitsgl_json_url`: `ops/sql/domain-move.sql`) |
| ETC server (Fly), mail From | `campfire-etc.hollisakins.com`, `team@campfire.hollisakins.com` | unchanged |

**Prepared on 2026-10-25 (no production effect):** zone `campfire-jwst.org`
active on Cloudflare (Registrar, same nameservers), proxied A records `@`,
`api`, `staging` → the VM and `www` → `@`, TLS mode Full (strict), the
`/auth/v1/` rate-limit rule copied for `api.campfire-jwst.org`, and a parking
rule (ruleset `2959130212b842e083bcef971410f6f5`) that answers every request on
the new zone with a **302** to the old portal — 302 on purpose: a cached 301
would loop once the direction reverses. The repo side is the `ALT_*` hostname
set in `Caddyfile` / `docker-compose.yml` / `.env.example` (inert at `*.invalid`
until `.env` sets them), `ops/sql/domain-move.sql`, the Worker's second custom
domain and origins in `wrangler.toml`, and the web fallbacks.

**Operator steps before the window** (none changes what users see):

- [ ] Cloudflare → `campfire-jwst.org` → SSL/TLS → Origin Server → Create
      Certificate (RSA, hostnames `campfire-jwst.org` and `*.campfire-jwst.org`,
      15 years). Save as `/srv/campfire/certs/jwst.pem` + `jwst.key`, mode 600,
      owned by the ops user. The API token cannot do this (Origin CA uses a
      separate user key).
- [ ] Cloudflare → R2 → tiles bucket → Settings → Custom Domains → connect
      `tiles.campfire-jwst.org` (the ops token has no R2 scope). Verify with any
      tile url from `map_layers.tile_base_url` on the new host: 200 and
      `content-type: image/png`.
- [ ] The CI token behind `secrets.CLOUDFLARE_API_TOKEN` (download-worker.yml)
      needs DNS edit on the new zone, or the Worker deploy that attaches
      `download.campfire-jwst.org` fails at the custom-domain step. Do this
      before merging the prep PR.
- [ ] Early alias (optional, lets the API be tested on its new name): with the
      prep PR deployed, `cp jwst.pem alt.pem; cp jwst.key alt.key` in
      `/srv/campfire/certs`, set `ALT_API_HOST=api.campfire-jwst.org` in `.env`
      (changelog line), `docker compose up -d --no-deps caddy`, then
      `curl -s https://api.campfire-jwst.org/auth/v1/health`. Without this step
      the first start of the new Caddyfile still needs `alt.pem`/`alt.key` to
      exist: copy `origin.*` to `alt.*`.

**The window** (target 40 min; the database is not involved):

1. Certificates: `origin.*` must become the new zone's, `alt.*` the old one's.
   In `/srv/campfire/certs`: `mv origin.pem old.pem; mv origin.key old.key;
   cp jwst.pem origin.pem; cp jwst.key origin.key; mv old.pem alt.pem;
   mv old.key alt.key` (so `alt.*` = `*.hollisakins.com` from here on; keep
   `jwst.*` as the spare copy).
2. `.env`: `WEB_HOST`, `WEB_STAGING_HOST`, `API_HOST` → the new names;
   `ALT_WEB_HOST`, `ALT_WEB_STAGING_HOST`, `ALT_API_HOST` → the old ones;
   `SITE_URL`, `API_EXTERNAL_URL`, `SUPABASE_PUBLIC_URL`, the five
   `MAILER_TEMPLATES_*` → the new api/portal urls; `ADDITIONAL_REDIRECT_URLS` =
   new list + old list (`.env.example` has the exact lines). `web.env`:
   `CDN_FRONT_URL` → `https://download.campfire-jwst.org`,
   `S3_TILES_PUBLIC_URL_BASE` → `https://tiles.campfire-jwst.org`, and the
   three commented `NEXT_PUBLIC_*` reference lines. Changelog line.
3. `docker compose up -d --no-deps caddy auth` (`web` is recreated by the
   deploy in step 7 and reads `web.env` then).
4. Cloudflare: delete the parking rule, `cf.sh DELETE
   /zones/acb645a897f706e91975c8581b8f9dcb/rulesets/2959130212b842e083bcef971410f6f5`.
5. Check from a laptop: `curl -sI https://campfire-jwst.org/api/health` → 200
   (still the old image, talking to the old API alias — fine);
   `curl -sI https://campfire.hollisakins.com/nirspec` → 301 to the new host;
   `curl -s https://campfire.hollisakins.com/api/v1/version` → 200 with the
   JSON body (served, not redirected);
   `https://api.campfire-jwst.org/auth/v1/health` and
   `https://campfire-api.hollisakins.com/auth/v1/health` → 200.
6. Tiles: `docker exec -i campfire-db psql -U postgres -d postgres -v
   old=https://campfire-tiles.hollisakins.com -v new=https://tiles.campfire-jwst.org
   -f - < ops/sql/domain-move.sql` (prints the per-host counts; all rows on the
   new host).
7. Rebuild with the new public urls: `gh variable set
   DEPLOY_NEXT_PUBLIC_SUPABASE_URL -b https://api.campfire-jwst.org`, the same
   for `DEPLOY_NEXT_PUBLIC_APP_URL=https://campfire-jwst.org` and
   `DEPLOY_NEXT_PUBLIC_WORKER_DOWNLOAD_URL=https://download.campfire-jwst.org`;
   then merge the flip PR (Python client default url, docs urls, Updates entry)
   — its push to `main` runs `deploy.yml`, which builds and deploys. Watch
   `deploys.log`.
8. Sign in again on the new host and smoke-test: object page with spectra,
   the NIRCam map (tiles from the new host), a download (Worker url on the new
   host), a password-reset email (link → `api.campfire-jwst.org/auth/v1/verify`
   → portal), `campfire login` + `campfire sync` from a laptop, one admin
   write. The access log shows the hosts in use:
   `jq -r '.request.host' /srv/campfire/logs/caddy/access.log | sort | uniq -c`.
9. Monitoring: the site check follows `WEB_HOST`; move the UptimeRobot monitor
   to `https://campfire-jwst.org/api/health` by hand. Laptops that deploy tiles
   or run in service-role mode: `CAMPFIRE_S3_TILES_PUBLIC_URL_BASE` and
   `CAMPFIRE_SUPABASE_URL` → the new hosts.

**Rollback** (any time; nothing is destroyed): steps 1–3 in reverse (`.env` /
`web.env` back, certificates swapped back, `up -d --no-deps caddy auth`),
`domain-move.sql` with `old` and `new` swapped, the three GitHub variables back
and `deploy.sh <last sha built with the old urls>` (`deploys.log`). Re-creating
the parking rule is optional; without it the new names serve the site directly.

**Afterwards:** leave `campfire-api.hollisakins.com` aliased for at least three
months and the old Worker route for at least 30 days; then drop the `ALT_API_HOST`
line, the old route in `wrangler.toml`, and the `campfire-tiles` R2 custom
domain. The redirects on `campfire.hollisakins.com` stay for as long as the
domain exists. Moving the mail From to `team@campfire-jwst.org` is a separate,
optional Resend domain verification.

## Rollback

- **Domain move** (Phase 3): see the end of "Domain move" above.
- **Web** (Phase 1): Cloudflare DNS `campfire.hollisakins.com` back to the
  Vercel CNAME. Vercel stays deployed until decommission.
- **Database** (Phase 2): unpause the hosted project if paused, switch its
  Data API back on and re-enable the Email provider if the cutover disabled
  it, revert the three url lines in `web.env` / `.env` to the
  hosted values **first**, then redeploy the last image built against the
  hosted URL (`deploy.sh 1c6a4eaf…`; it recreates the container with the env
  as it stands). Writes made on the VM between cutover and rollback are lost;
  hence the two quiet weeks before deletion.
- **A bad deploy**: `scripts/deploy.sh <previous full sha>` (`deploys.log` has
  the history; `deploy.sh` itself rolls back when `/api/health` fails).

## Day 2

- Health: `curl -s http://127.0.0.1:3000/api/health`; external monitor on
  `https://campfire.hollisakins.com/api/health`; heartbeat pings from the three
  timers (`HEALTHCHECK_PING_URL*`).
- Logs: `docker compose logs -f --tail 200 <service>`; `journalctl -u campfire-backup`.
- Request log: `/srv/campfire/logs/caddy/access.log`, one JSON line per request
  for all three hostnames, rotated at 50 MB and kept 7 days. For example
  requests per host in the last hour, 5xx responses, slowest routes:
  ```bash
  jq -r 'select(.ts > (now - 3600)) | .request.host' access.log | sort | uniq -c
  jq -c 'select(.status >= 500) | {ts, host: .request.host, uri: .request.uri, status}' access.log | tail
  jq -r '[.duration, .request.host, .request.uri] | @tsv' access.log | sort -rn | head
  ```
- A Caddyfile change that arrived through `deploy.sh` (git writes a new file,
  the container's bind mount still points at the old inode, and compose sees
  nothing to recreate — `caddy reload` then reports "config is unchanged"):
  `docker compose up -d --no-deps --force-recreate caddy` (a one-second
  restart). Only an in-place edit of the mounted file can use the graceful
  `docker exec campfire-caddy caddy reload --config /etc/caddy/Caddyfile
  --adapter caddyfile`. Check what is running:
  `docker exec campfire-caddy wget -qO- http://127.0.0.1:2019/config/ | jq`.
- Restarting Caddy: `docker compose up -d --no-deps caddy`. A plain `up -d caddy`
  also starts Kong, PostgREST and GoTrue through `depends_on`, which in Phase 1
  must stay down (they would serve the API hostname over whatever copy the local
  database holds). The same applies to `up -d web`; `deploy.sh` already uses
  `--no-deps`.
- Outside-in check: `campfire-site-check.timer` fetches the public
  `/api/health` through Cloudflare every 5 min and pings
  `HEALTHCHECK_PING_URL_SITE`; healthchecks.io alerts when the pings stop.
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

- 2026-10-25 ~20:30 UTC — **Domain move executed** (see "Domain move"): certificates swapped (`origin.*` = campfire-jwst.org, `alt.*` = hollisakins.com); `.env` `WEB_HOST`, `WEB_STAGING_HOST`, `API_HOST` → the campfire-jwst.org names, `ALT_WEB_HOST`, `ALT_WEB_STAGING_HOST`, `ALT_API_HOST` → the hollisakins.com names, `SITE_URL`, `API_EXTERNAL_URL`, `SUPABASE_PUBLIC_URL`, `MAILER_TEMPLATES_*` → new urls, `ADDITIONAL_REDIRECT_URLS` = new + old lists; `web.env` `CDN_FRONT_URL`, `S3_TILES_PUBLIC_URL_BASE`, `NEXT_PUBLIC_*` → new hosts; caddy + auth recreated; parking rule deleted; `domain-move.sql` run; `DEPLOY_NEXT_PUBLIC_*` variables → new hosts; this PR's merge rebuilt the image.
- 2026-10-25 — `.env` `ALT_API_HOST` set (early alias: the Supabase API answers on `api.campfire-jwst.org` ahead of the flip); `certs/alt.pem`/`alt.key` = the new zone's origin certificate (`jwst.*` kept as the spare); caddy recreated with the `ALT_*` Caddyfile.
- 2026-10-25 — **Domain move prepared** (see "Domain move"): `campfire-jwst.org` zone, DNS, TLS mode, rate limit and the parking 302 on Cloudflare; `Caddyfile` / `docker-compose.yml` gain the `ALT_*` hostname blocks (inert until `.env` sets them; `certs/alt.pem`/`alt.key` must exist — copy of `origin.*` until the window); `ops/sql/domain-move.sql`; Worker gets `download.campfire-jwst.org` and the new origins. No `.env` change yet.
- 2026-10-26 — Kong `KONG_NGINX_HTTP_LARGE_CLIENT_HEADER_BUFFERS: 4 64k` (#657): a `campfire deploy` plans its push with PostgREST `storage_key=in.(…)` GETs whose request line runs 25–30 KB once URL-encoded; nginx's 8 KB default inside Kong answered 414 since the cutover (the hosted gateway had accepted it). Kong recreated with `compose up -d --no-deps kong`. The Python clients now also size those batches by encoded length (`campfire/deploy/url_batches.py`).
- 2026-10-25 01:52–02:02 UTC — **Phase 2 cutover done.** Freeze = hosted Data API switch (NOLOGIN is impossible on hosted); `dump-hosted.sh` 57 s; cluster re-initialised; `restore-hosted.sh` RESTORE VERIFIED in 2 m 23 s (counts equal, 182/182, fingerprint identical); `.env` `SITE_URL` → production, kong/rest/auth up; `DEPLOY_NEXT_PUBLIC_SUPABASE_URL` → `campfire-api.hollisakins.com`, `web.env` `SUPABASE_URL=http://kong:8000` + `NEXT_PUBLIC_SUPABASE_URL` → api host; workflow run 40152730043 rebuilt and deployed 557483c4 at 02:02:23. Operator smoke test (sign-in, download, `campfire sync`, admin, a write) passed. Rollback image: 1c6a4eaf. Hosted project left running with the Data API off; the previous local cluster is `db/data.broken-init-0158` (rehearsal copy, disposable). Updates notice skipped by decision (few active users).
- 2026-10-25 — dress rehearsal passed (see "Phase 2 dress rehearsal"); `.env` `DEPLOY_MIGRATIONS=local` from now on (applies pending migrations to the local database on every deploy; harmless in Phase 1, required in Phase 2); `web-staging.env` `SUPABASE_URL=http://kong:8000`.
- 2026-10-25 — hosted Auth/API settings copied into `.env` from the dashboard: five email templates (versioned in `ops/auth-templates/`, portal links as `{{ .SiteURL }}`; copied to `/srv/campfire/auth-templates/`), `MAILER_SUBJECTS_*`, `SMTP_MAX_FREQUENCY=60s`, `PGRST_DB_MAX_ROWS=5000`, `ADDITIONAL_REDIRECT_URLS` = hosted list + staging + localhost, `SECURE_EMAIL_CHANGE=false`; the four `HEALTHCHECK_PING_URL*` set (healthchecks.io). `auth` and `rest` recreated. Still to flip at the Phase 2 window: `SITE_URL` → production.
Dated, one line per change to `.env` / `web.env` / `docker-compose.yml` on the
host. Variable names only, never values.

- 2026-10-24 — scaffold (Phase 0 PR).
- 2026-10-24 — `.env`: `WEB_HOST`/`API_HOST`/`SITE_URL`/`API_EXTERNAL_URL`/`SUPABASE_PUBLIC_URL` → first-level hostnames; `WEB_TAG` = deployed sha; `DEPLOY_MIGRATIONS=skip` (Phase 1). `web.env` created from the Vercel production env + local creds; `WORKER_JWT_SECRET`/`CDN_FRONT_URL` left unset until the Phase 1 rotation.
- 2026-10-24 — `docker-compose.yml`: `db/config` bind mount → `db-config` named volume (first `db` start failed: empty mount hid the image's include files).
- 2026-10-24 — `volumes/db/roles.sql` guarded (init aborted on the absent `supabase_functions_admin`; cluster re-initialised twice during the rehearsal, `db/data.broken-init-*` kept aside on the volume until the rehearsal passes).
- 2026-10-24 — Phase 2 preparation: Caddy access log on (`logs/caddy/access.log`, 7 days); `WEB_HOST` back to production only, new `WEB_STAGING_HOST` + `COMPOSE_IGNORE_ORPHANS` in `.env`; `docker-compose.staging.yml` + `web-staging.env` for the dress rehearsal (staging web built against the self-hosted API); `campfire-site-check.timer` (`HEALTHCHECK_PING_URL_SITE`, unset until the healthchecks.io account exists); Kong/PostgREST/GoTrue started on the restored copy for the rehearsal. Found: `docker compose up -d caddy` starts the API containers through `depends_on` (use `--no-deps`).
- 2026-10-24 22:55 UTC — **step 6 flip**: `campfire.hollisakins.com` CNAME → proxied `A 149.165.174.96` (Cloudflare record `db80e4ad…`; the record comment carries the rollback target). Health, root, login, object page, API version and OG image answered 200 from the VM within the minute; Vercel (`campfire-jwst.vercel.app`) kept as the fallback.
- 2026-10-24 — step 6 preparation: `.env` `WEB_HOST` → both hostnames (Caddy recreated); `web.env` `NEXT_PUBLIC_APP_URL` → production (image rebuilt with `DEPLOY_NEXT_PUBLIC_APP_URL` = production, run 40139323751, then `deploy.sh f723aa09`); Vercel production env: `JWT_SECRET` set to the VM's value, `WORKER_JWT_SECRET` rotated (new value also goes to the Worker and `web.env` with `CDN_FRONT_URL` once Vercel has redeployed).
- 2026-10-24 — restore rehearsal passed (third attempt): `scripts/dump-hosted.sh` + `scripts/restore-hosted.sh` + `sql/fingerprint.sql` replace the hand-run dump/restore commands in the cutover checklist. Found and fixed on the way: the image's default privileges leaking `anon` grants into the restore, the `storage.*` data blocks aborting the load before the sequence resets, matviews restored unpopulated, pg_cron jobs not dumped. The VM's `campfire-db` now holds a verified copy of the hosted database as of 2026-10-24 18:17 UTC (not live; Phase 2 redoes the restore from a fresh dump). The five `db/data.broken-init-*` directories (failed inits and the superseded rehearsal clusters, ~8 GB) can be deleted.
