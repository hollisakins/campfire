#!/usr/bin/env bash
# First-boot setup of a CAMPFIRE VM (Ubuntu 24.04). Idempotent: re-run freely.
#
#   sudo ops/scripts/bootstrap.sh [--data-root /srv/campfire] [--repo-dir /srv/campfire/repo]
#
# Installs Docker (compose plugin), Node 20 (the Supabase CLI via npx and
# Claude Code), rclone and the psql client; turns on unattended security
# upgrades; caps journald and the Docker json-file logs; prepares the data
# root; installs the systemd timers. It changes nothing about the firewall:
# the Jetstream2 security group owns 22/80/443.
set -euo pipefail

DATA_ROOT=/srv/campfire
REPO_DIR=/srv/campfire/repo
while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-root) DATA_ROOT="$2"; shift 2 ;;
    --repo-dir) REPO_DIR="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "run as root (sudo)" >&2
  exit 1
fi
OPS_USER="${SUDO_USER:-ubuntu}"
export DEBIAN_FRONTEND=noninteractive

log() { printf '\n==> %s\n' "$*"; }

log "apt packages"
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg git jq unattended-upgrades rclone

log "Docker engine + compose plugin"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
usermod -aG docker "$OPS_USER"

log "Docker daemon: log rotation defaults"
install -d /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "live-restore": true
}
EOF
systemctl enable --now docker
systemctl reload docker || systemctl restart docker

log "Node 20 (npx supabase, Claude Code)"
if ! command -v node >/dev/null || [[ "$(node -p 'process.versions.node.split(".")[0]')" -lt 20 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y -q nodejs
fi

log "PostgreSQL 17 client (psql / pg_restore for restore-check.sh)"
if ! command -v psql >/dev/null; then
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  . /etc/os-release
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -q
  apt-get install -y -q postgresql-client-17
fi

log "unattended security upgrades"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
# Never reboot on its own; the stack restarts fine, but reboots are ours to schedule.
sed -i 's|^//Unattended-Upgrade::Automatic-Reboot "false";|Unattended-Upgrade::Automatic-Reboot "false";|' /etc/apt/apt.conf.d/50unattended-upgrades
systemctl enable --now unattended-upgrades

log "journald cap"
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/campfire.conf <<'EOF'
[Journal]
SystemMaxUse=500M
MaxRetentionSec=1month
EOF
systemctl restart systemd-journald

log "data root ${DATA_ROOT}"
if ! mountpoint -q "$DATA_ROOT"; then
  echo "WARNING: ${DATA_ROOT} is not a mount point. Attach the volume and mount it there" >&2
  echo "         (Exosphere mounts volumes under /media/volume/<name>; bind-mount or fstab it to ${DATA_ROOT})." >&2
fi
install -d -m 0750 "$DATA_ROOT"
for d in db/data db/config backups auth-templates certs; do
  install -d -m 0750 "$DATA_ROOT/$d"
done
# db/data and db/config are chowned by the supabase/postgres entrypoint on
# first start (it runs as root, then drops to postgres).
chown "$OPS_USER":"$OPS_USER" "$DATA_ROOT/backups" "$DATA_ROOT/auth-templates" "$DATA_ROOT/certs"

log "systemd units"
if [[ -d "$REPO_DIR/ops/systemd" ]]; then
  install -m 0644 "$REPO_DIR"/ops/systemd/*.service "$REPO_DIR"/ops/systemd/*.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now campfire-backup.timer campfire-sync-snapshot.timer campfire-restore-check.timer
  systemctl list-timers 'campfire-*' --no-pager
else
  echo "repo not found at ${REPO_DIR}; clone it there and re-run to install the timers" >&2
fi

log "done"
cat <<EOF
Next:
  1. cp ${REPO_DIR}/ops/.env.example ${REPO_DIR}/ops/.env && chmod 600 ${REPO_DIR}/ops/.env  (fill it in)
  2. create ${REPO_DIR}/ops/web.env (chmod 600) from the block at the bottom of .env.example
  3. Cloudflare origin certificate -> ${DATA_ROOT}/certs/origin.pem + origin.key
  4. cd ${REPO_DIR}/ops && docker compose up -d db && docker compose logs -f db   (first init)
  5. docker compose up -d
  6. log out/in once so '${OPS_USER}' picks up the docker group
EOF
