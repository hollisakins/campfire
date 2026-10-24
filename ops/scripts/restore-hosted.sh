#!/usr/bin/env bash
# Restore a dump-hosted.sh dump into the stack's db container and verify it
# (ops/README.md, "Database cutover checklist", steps 3–4).
#
#   ops/scripts/restore-hosted.sh <dump dir>            restore, then verify
#   ops/scripts/restore-hosted.sh <dump dir> --verify   verify only
#
# Preconditions: `docker compose up -d db` on a FRESHLY INITIALISED cluster
# (README "First boot": vault/graphql/realtime schemas present, auth.uid()
# owned by supabase_auth_admin). The script refuses to restore into a database
# that already has tables in public; re-initialise (move db/data aside) first.
#
# Order, and why:
#   1. start auth: GoTrue creates and migrates the auth schema itself
#      (schema.sql carries no auth.* DDL; data.sql has the auth rows).
#   2. drop the image's default privileges for postgres in public. pg_dump
#      expresses ACLs relative to the built-in defaults; the supabase/postgres
#      image pre-sets ALTER DEFAULT PRIVILEGES granting anon/authenticated/
#      service_role on every new table, so without this step the restore hands
#      anon every table (rehearsal 2026-10-24: 490 grants, link_password
#      readable). The dump re-creates the hosted default privileges at its end.
#   3. roles.sql, schema.sql, migrations_history.sql, then data.sql with
#      triggers off (session_replication_role = replica) and the storage.*
#      blocks removed (no Storage service here; the tables do not exist).
#   4. REFRESH the materialized views (restored WITH NO DATA; the nightly
#      refresh_all_matviews() uses CONCURRENTLY, which needs a populated view),
#      re-create the pg_cron jobs, ANALYZE.
# Verification: counts vs reference.txt, `supabase migration list` vs the repo,
# the SQL guards (the six that reference seed.sql's DR-seed release fail on
# production data: expected, counted separately; check_dr_freeze.sql is
# skipped, see below), and ops/sql/fingerprint.sql
# vs the hosted fingerprint (grants, RLS, policies, triggers, indexes,
# extensions, default privileges must hash identically).
# Rehearsal 2026-10-24: restore 2 min, verification 1 min, 2.2 GB.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib.sh
source "${OPS_DIR}/scripts/lib.sh"; load_env "${OPS_DIR}/.env"
REPO_DIR="${REPO_DIR:-/srv/campfire/repo}"
SUPABASE_CLI_VERSION="${SUPABASE_CLI_VERSION:-2.120.0}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-postgres}"

DIR="${1:-}"; MODE="${2:-restore}"
[[ -d "$DIR" ]] || { echo "usage: restore-hosted.sh <dump dir> [--verify]" >&2; exit 2; }
for f in roles.sql schema.sql migrations_history.sql data.sql; do
  [[ -f "$DIR/$f" ]] || { echo "missing $DIR/$f" >&2; exit 2; }
done

T0=$(date +%s); step() { printf '[restore %4ss] %s\n' "$(( $(date +%s) - T0 ))" "$*"; }
compose() { docker compose --project-directory "$OPS_DIR" "$@"; }
# psql_db reads its input from stdin (docker exec forwards none without -i);
# q runs one query and must NOT take stdin, or it eats the file a surrounding
# `while read` loop is iterating.
psql_db() { docker exec -i campfire-db psql -U postgres -d "$POSTGRES_DB" "$@"; }
q() { docker exec campfire-db psql -U postgres -d "$POSTGRES_DB" -Atc "$1"; }

[[ "$(docker inspect --format '{{.State.Health.Status}}' campfire-db 2>/dev/null)" == healthy ]] \
  || { echo "campfire-db is not healthy; docker compose up -d db first" >&2; exit 1; }

if [[ "$MODE" != "--verify" ]]; then
  # ---- preconditions -------------------------------------------------------
  [[ "$(q "select count(*) from pg_namespace where nspname in ('vault','graphql','realtime')")" == 3 ]] \
    || { echo "image init did not complete (vault/graphql/realtime missing); re-initialise the cluster" >&2; exit 1; }
  ntab="$(q "select count(*) from pg_tables where schemaname='public'")"
  [[ "$ntab" == 0 ]] || { echo "refusing: public already has ${ntab} tables (re-initialise the cluster for a restore)" >&2; exit 1; }

  # ---- 1. auth ---------------------------------------------------------------
  step "starting auth (GoTrue migrates the auth schema)"
  compose up -d auth >/dev/null
  for _ in $(seq 1 90); do
    compose logs --no-log-prefix auth 2>&1 | grep -q "API started" && break
    sleep 2
  done
  compose logs --no-log-prefix auth 2>&1 | grep -q "API started" || { echo "GoTrue did not start; docker compose logs auth" >&2; exit 1; }
  step "auth.schema_migrations: $(q "select count(*)||' max='||max(version) from auth.schema_migrations")"

  # ---- 2. default privileges -------------------------------------------------
  step "clearing the image default privileges for postgres in public"
  psql_db -v ON_ERROR_STOP=1 -q <<'SQL'
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES    FROM postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON SEQUENCES FROM postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM postgres, anon, authenticated, service_role;
SQL

  # ---- 3. roles, schema, history, data ----------------------------------------
  step "roles.sql";              psql_db -v ON_ERROR_STOP=1 -q < "$DIR/roles.sql"
  step "schema.sql";             psql_db -v ON_ERROR_STOP=1 -q < "$DIR/schema.sql"
  step "migrations_history.sql"; psql_db -v ON_ERROR_STOP=1 -q < "$DIR/migrations_history.sql"
  step "data.sql (storage.* blocks dropped, triggers off)"
  # Each pg_dump data block is "-- Data for Name: <table>; Type: TABLE DATA; Schema: <schema>; ..."
  # up to the next block header; the sequence SET section follows the last one.
  awk '/^-- Data for Name: .*; Schema: storage;/{skip=1} /^-- Data for Name: .*; Schema: (public|auth);/{skip=0} /^-- Name: .*; Type: SEQUENCE SET;/{skip=0} !skip' "$DIR/data.sql" \
    | psql_db -v ON_ERROR_STOP=1 -q -c 'set session_replication_role = replica' -f -
  step "rows loaded"

  # ---- 4. matviews, cron, analyze --------------------------------------------
  step "refreshing materialized views"
  q "select format('REFRESH MATERIALIZED VIEW %I.%I;', schemaname, matviewname) from pg_matviews where not ispopulated" \
    | psql_db -v ON_ERROR_STOP=1 -q -f -
  if [[ -f "$DIR/cron_jobs.sql" ]]; then
    step "pg_cron jobs"; psql_db -v ON_ERROR_STOP=1 -q < "$DIR/cron_jobs.sql"
  else
    echo "WARNING: no cron_jobs.sql; re-create the pg_cron jobs by hand (select jobname, schedule, command from cron.job on the hosted project)" >&2
  fi
  step "ANALYZE"; psql_db -q -c 'ANALYZE' 2>/dev/null
  step "restore done"
fi

# ---- verification ------------------------------------------------------------
fail=0
echo "== counts (restored | reference)"
while IFS='|' read -r name ref; do
  case "$name" in
    users) got="$(q "select count(*) from auth.users")" ;;
    migrations) got="$(q "select count(*) from supabase_migrations.schema_migrations")" ;;
    auth_migrations_max) got="$(q "select max(version) from auth.schema_migrations")" ;;
    *) got="$(q "select count(*) from public.\"$name\"")" ;;
  esac
  if [[ "$got" == "$ref" ]]; then printf '  ok   %-20s %s\n' "$name" "$got"; else printf '  FAIL %-20s %s | %s\n' "$name" "$got" "$ref"; fail=1; fi
done < <(if [[ -f "$DIR/reference.txt" ]]; then cat "$DIR/reference.txt"; else echo "WARNING: no reference.txt" >&2; fi)

echo "== matviews: $(q "select string_agg(matviewname||'='||ispopulated, ', ') from pg_matviews")"
echo "== cron jobs: $(q "select coalesce(string_agg(jobname||' ['||schedule||']', ', '), 'NONE') from cron.job")"
echo "== authenticator: $(q "select array_to_string(rolconfig, ' | ') from pg_roles where rolname='authenticator'")"

echo "== supabase migration list vs ${REPO_DIR}/supabase/migrations"
DB_URL="postgresql://postgres:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT}/${POSTGRES_DB}?sslmode=disable"
ml="$(cd "$REPO_DIR" && npx -y "supabase@${SUPABASE_CLI_VERSION}" migration list --db-url "$DB_URL" 2>&1 || true)"
both=$(grep -cE '^ *`?[0-9]{14}`? *\| *`?[0-9]{14}' <<<"$ml" || true)
repo_only=$(grep -cE '^ *`?[0-9]{14}`? *\| *\|' <<<"$ml" || true)
db_only=$(grep -cE '^ *\| *`?[0-9]{14}' <<<"$ml" || true)
files=$(ls -1 "$REPO_DIR"/supabase/migrations/*.sql | wc -l | tr -d ' ')
echo "  applied on both: ${both}  repo-only: ${repo_only}  db-only: ${db_only}  repo files: ${files}"
# repo-only: deploy.sh would replay the chain; db-only: the checkout is behind
# the database (deploying it could reintroduce what a newer migration changed).
[[ "$both" == "$files" && "$repo_only" == 0 && "$db_only" == 0 ]] || { echo "  FAIL: migration history differs from the repo (repo-only → supabase migration repair --status applied <version>; db-only → check out the commit the hosted project was migrated to)"; fail=1; }

echo "== SQL guards (BEGIN … ROLLBACK; guards that reference seed.sql's DR-seed release fail on production data)"
# check_dr_freeze.sql freezes the entire catalog inside its transaction: on the
# seed that is instant, on 81k spectra it ran for 40 min in the rehearsal. It
# tests the release machinery, not the restore, so it is skipped here.
SKIP_GUARDS="check_dr_freeze.sql"
gpass=0; gfail=0; seed_fail=0
for f in "$REPO_DIR"/supabase/tests/check_*.sql; do
  if [[ " $SKIP_GUARDS " == *" $(basename "$f") "* ]]; then
    echo "  skip $(basename "$f") (whole-catalog freeze; not a restore check)"
  elif out="$(psql_db -v ON_ERROR_STOP=1 -q < "$f" 2>&1)"; then
    gpass=$((gpass + 1))
  else
    err="$(grep -m1 -E '^(psql:.*)?ERROR:' <<<"$out" | cut -c1-200)"
    # Only the assertion that trips on the missing seed release is expected
    # ("DR-seed lacks …", "no release DR-seed", "… got PT404" = tag not found).
    # Any earlier assertion in the same file is a real failure.
    if grep -q 'DR-seed' "$f" && grep -qE 'DR-seed|got PT404' <<<"$err"; then
      seed_fail=$((seed_fail + 1)); echo "  seed $(basename "$f"): ${err}"
    else
      gfail=$((gfail + 1)); fail=1
      echo "  FAIL $(basename "$f"): ${err:-$(tail -n1 <<<"$out" | cut -c1-200)}"
    fi
  fi
done
echo "  ok ${gpass}, failed ${gfail}, seed-dependent (DR-seed) failures ${seed_fail}"

if [[ -f "$DIR/fingerprint-hosted.txt" ]]; then
  echo "== fingerprint vs hosted (ops/sql/fingerprint.sql)"
  psql_db -At -F'|' < "${OPS_DIR}/sql/fingerprint.sql" > "$DIR/fingerprint-restored.txt"
  if diff <(sort "$DIR/fingerprint-hosted.txt") <(sort "$DIR/fingerprint-restored.txt"); then
    echo "  identical"
  else
    echo "  FAIL: differs (hosted <, restored >)"; fail=1
  fi
else
  echo "WARNING: no fingerprint-hosted.txt; grants not compared" >&2
fi

step "verification done"
if (( fail )); then echo "RESTORE VERIFICATION FAILED"; exit 1; fi
echo "RESTORE VERIFIED"
