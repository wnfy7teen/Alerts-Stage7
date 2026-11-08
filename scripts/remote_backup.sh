#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/var/backups/donation-alerts}"
PROJECT_PATH="${PROJECT_PATH:-/opt/donation-alerts}"
BACKUP_MAX_AGE_SECONDS="${BACKUP_MAX_AGE_SECONDS:-172800}"
REMOTE_PATH="${REMOTE_PATH:-donation-backups-crypt:donation-alerts}"
RCLONE_CONFIG="${RCLONE_CONFIG:-/etc/rclone/rclone.conf}"
LOCK_FILE="${LOCK_FILE:-/run/lock/donation-alerts-remote-backup.lock}"

exec 9>"$LOCK_FILE"
flock -n 9 || { echo "remote backup already running"; exit 0; }

command -v rclone >/dev/null
command -v sha256sum >/dev/null
test -r "$RCLONE_CONFIG"
test -d "$BACKUP_DIR"

ARCHIVE="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'donation-alerts-*.tar.gz' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
test -n "$ARCHIVE" && test -s "$ARCHIVE"
gzip -t "$ARCHIVE"
archive_age="$(( $(date +%s) - $(stat -c %Y "$ARCHIVE") ))"
if (( archive_age < 0 || archive_age > BACKUP_MAX_AGE_SECONDS )); then
  echo "latest local archive is stale" >&2
  exit 1
fi
VERIFY_DIR="$(mktemp -d)"
trap 'rm -rf "$VERIFY_DIR"' EXIT
python3 "$PROJECT_PATH/scripts/restore_drill.py" "$ARCHIVE" "$VERIFY_DIR/restore"
LOCAL_SHA="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
REMOTE_FILE="$REMOTE_PATH/$(basename "$ARCHIVE")"

rclone copyto "$ARCHIVE" "$REMOTE_FILE" \
  --config "$RCLONE_CONFIG" --immutable --checksum --transfers 1 --checkers 1
rclone cryptcheck "$ARCHIVE" "$REMOTE_FILE" \
  --config "$RCLONE_CONFIG" --one-way

printf 'remote_backup=verified archive=%s sha256=%s\n' "$(basename "$ARCHIVE")" "$LOCAL_SHA"
