# ops/ — Claude Code on the CAMPFIRE VM

You are on the production host (Jetstream2, Ubuntu 24.04). Everything that
serves `campfire.hollisakins.com` runs from this directory with Docker
Compose. Read `README.md` here first; the repo-wide `AGENTS.md` describes the
application, not the host.

## Layout

| Path | What |
|---|---|
| `/srv/campfire/repo` | this git checkout; `deploy.sh` moves it between commits (detached HEAD is normal) |
| `/srv/campfire/repo/ops/.env`, `web.env` | the only copies of the secrets; mode 600; never printed, never committed |
| `/srv/campfire/db/data` | the Postgres cluster |
| `/srv/campfire/backups` | nightly dumps, `last-success`, `last-restore-check` |
| `/srv/campfire/certs` | Cloudflare origin certificate |
| `/srv/campfire/auth-templates` | GoTrue email templates |
| `ops/deploys.log` | one line per successful deploy |

Containers: `campfire-db`, `campfire-kong`, `campfire-auth`, `campfire-rest`,
`campfire-meta`, `campfire-studio`, `campfire-web`, `campfire-caddy`.

## Safety rules

- **Never** run `docker compose down -v`, `rm` anything under `/srv/campfire/db`,
  or `pg_restore --clean` against `campfire-db` without a fresh dump you have
  just verified (`scripts/backup.sh` then check `backups/last-success`).
- Do not edit `.env` or `web.env` without appending a dated line to the
  Changelog section of `README.md` saying which variable changed and why.
  Values never go in the changelog.
- Deploys go through `scripts/deploy.sh <sha>`; do not `docker compose pull`
  the web image by hand. Rollback is `deploy.sh <previous sha>` (`deploys.log`).
- Schema changes go through the repo (`supabase/schemas/` → migration → PR);
  `deploy.sh` applies them. Ad-hoc DDL on this host is a last resort and must
  be mirrored into the schema files the same day.
- The security group (Jetstream2) is the firewall. Do not add `ufw` rules or
  publish container ports on `0.0.0.0`; only Caddy listens publicly.
- Read-only investigation is always fine: logs, `psql`, `EXPLAIN`, `docker stats`.

## Common commands

```bash
cd /srv/campfire/repo/ops
docker compose ps                         # container state + health
docker compose logs -f --tail 200 web     # or db, auth, rest, kong, caddy
curl -s http://127.0.0.1:3000/api/health  # {"ok":true,"db":"ok","sha":...}
docker exec -it campfire-db psql -U supabase_admin -d postgres
docker exec campfire-db psql -U supabase_admin -d postgres -Atc \
  "select query, calls, round(mean_exec_time) ms from pg_stat_statements order by total_exec_time desc limit 10"
systemctl list-timers 'campfire-*'        # backup / snapshot / restore-check
journalctl -u campfire-backup -n 50
cat /srv/campfire/backups/last-success
df -h /srv/campfire
scripts/deploy.sh <sha>                   # deploy or roll back
```

Studio (dashboard): from your laptop `ssh -L 8000:127.0.0.1:8000 <vm>` then
open http://localhost:8000 with `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`.
