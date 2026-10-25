#!/usr/bin/env bash
# First-boot setup of the CAMPFIRE VM (Ubuntu 24.04). Idempotent: re-run freely.
#
#   sudo ops/scripts/bootstrap.sh [--volume-device /dev/sdb]
#
# Paths are fixed (the systemd units hard-code them): repo at
# /srv/campfire/repo, data root at /srv/campfire.
#
# Installs Docker (compose plugin), Node 20 (the Supabase CLI via npx and
# Claude Code), rclone and the psql client; turns on unattended security
# upgrades; caps journald and the Docker json-file logs; mounts the data
# volume; prepares the data root; installs the systemd timers. It changes
# nothing about the firewall: the Jetstream2 security group owns 22/80/443.
#
# Data volume. Exosphere auto-mounts an attached volume at
# /media/volume/<name> through its own unit. Rather than fight that, the data
# root ADOPTS the existing mount: /srv/campfire becomes a symlink to it (Docker
# resolves bind-mount paths through symlinks). Only when no such mount exists
# and --volume-device is given does the script format (if blank) and mount the
# device at /srv/campfire itself, by UUID, with nofail.
set -euo pipefail

DATA_ROOT=/srv/campfire
REPO_DIR=/srv/campfire/repo
VOLUME_DEVICE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --volume-device) VOLUME_DEVICE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "run as root (sudo)" >&2
  exit 1
fi
OPS_USER="${SUDO_USER:-exouser}"
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

log "data volume -> ${DATA_ROOT}"
if mountpoint -q "$DATA_ROOT" || { [[ -L "$DATA_ROOT" ]] && mountpoint -q "$(readlink -f "$DATA_ROOT")"; }; then
  echo "${DATA_ROOT} already backed by a mount: $(findmnt -no SOURCE,TARGET "$(readlink -f "$DATA_ROOT")")"
else
  EXO_MOUNT="$(findmnt -rno TARGET | grep -m1 '^/media/volume/' || true)"
  if [[ -n "$EXO_MOUNT" ]]; then
    echo "adopting Exosphere mount ${EXO_MOUNT}"
    if [[ -d "$DATA_ROOT" && ! -L "$DATA_ROOT" ]]; then
      rmdir "$DATA_ROOT" 2>/dev/null || { echo "${DATA_ROOT} exists and is not empty; move it aside first" >&2; exit 1; }
    fi
    ln -sfn "$EXO_MOUNT" "$DATA_ROOT"
  elif [[ -n "$VOLUME_DEVICE" ]]; then
    [[ -b "$VOLUME_DEVICE" ]] || { echo "${VOLUME_DEVICE} is not a block device" >&2; exit 1; }
    if ! blkid -s TYPE -o value "$VOLUME_DEVICE" >/dev/null 2>&1; then
      echo "no filesystem on ${VOLUME_DEVICE}; creating ext4"
      mkfs.ext4 -q -L campfire "$VOLUME_DEVICE"
    fi
    UUID="$(blkid -s UUID -o value "$VOLUME_DEVICE")"
    install -d -m 0750 "$DATA_ROOT"
    if ! grep -q "UUID=${UUID}" /etc/fstab; then
      echo "UUID=${UUID} ${DATA_ROOT} ext4 defaults,nofail 0 2" >> /etc/fstab
    fi
    systemctl daemon-reload
    mount "$DATA_ROOT"
  else
    echo "WARNING: no volume mounted under /media/volume and no --volume-device given;" >&2
    echo "         ${DATA_ROOT} will live on the root disk until you re-run with --volume-device." >&2
    install -d -m 0750 "$DATA_ROOT"
  fi
fi

log "data root layout"
# The ops user must be able to traverse the data root itself (it holds the
# repo checkout, and systemd units cd into it): root-owned, ops group, 0750.
# Applied to the resolved path so the Exosphere-mount symlink case is covered.
REAL_ROOT="$(readlink -f "$DATA_ROOT")"
chown root:"$OPS_USER" "$REAL_ROOT"
chmod 0750 "$REAL_ROOT"
for d in db/data backups auth-templates certs logs/caddy; do
  install -d -m 0750 "$DATA_ROOT/$d"
done
# db/data is chowned by the supabase/postgres entrypoint on
# first start (it runs as root, then drops to postgres).
chown "$OPS_USER":"$OPS_USER" "$DATA_ROOT/auth-templates" "$DATA_ROOT/certs"
# backup.sh runs pg_dump INSIDE the db container, so the backups dir must be
# writable by the image's postgres uid; the ops user keeps group access.
PG_IMAGE="$(grep -m1 -oE 'supabase/postgres:[0-9.]+' "$REPO_DIR/ops/docker-compose.yml" 2>/dev/null || true)"
if [[ -n "$PG_IMAGE" ]]; then
  PG_UID="$(docker run --rm --entrypoint id "$PG_IMAGE" -u postgres 2>/dev/null || true)"
fi
if [[ -n "${PG_UID:-}" ]]; then
  chown "$PG_UID":"$(id -g "$OPS_USER")" "$DATA_ROOT/backups"
  chmod 2770 "$DATA_ROOT/backups"
else
  echo "could not resolve the postgres uid from ${PG_IMAGE:-the compose file}; backups/ left to ${OPS_USER}" >&2
  chown "$OPS_USER":"$OPS_USER" "$DATA_ROOT/backups"
fi

log "systemd units"
if [[ -d "$REPO_DIR/ops/systemd" ]]; then
  install -m 0644 "$REPO_DIR"/ops/systemd/*.service "$REPO_DIR"/ops/systemd/*.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now campfire-backup.timer campfire-sync-snapshot.timer campfire-restore-check.timer campfire-site-check.timer
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
