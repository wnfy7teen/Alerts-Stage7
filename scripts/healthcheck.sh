#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

BASE_URL="${BASE_URL:-https://vanesso4ka.site}"
HEALTH_URL="$BASE_URL/api/health"
SERVICE_NAME="${SERVICE_NAME:-donation-alerts.service}"
BACKUP_STATUS="${BACKUP_STATUS:-/var/lib/donation-alerts/backup-status.json}"
MIN_FREE_KB="${MIN_FREE_KB:-1048576}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
ALERT_FILE="${ALERT_FILE:-/var/lib/donation-alerts/health-alert-state}"

send_alert() {
  local message="$1"
  if [[ -n "$TELEGRAM_BOT_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN" |
      curl --config - --silent --show-error --max-time 10 \
        --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" \
        --data-urlencode "text=$message" >/dev/null || true
  fi
}

failures=()
if ! curl --fail --silent --show-error --max-time 10 "$HEALTH_URL" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
  failures+=("public_health_failed")
fi
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  failures+=("service_inactive")
fi
free_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
if [[ -z "$free_kb" || "$free_kb" -lt "$MIN_FREE_KB" ]]; then
  failures+=("disk_free_below_threshold")
fi
if [[ -f "$BACKUP_STATUS" ]] && ! grep -q '"status"[[:space:]]*:[[:space:]]*"success"' "$BACKUP_STATUS"; then
  failures+=("latest_backup_failed")
elif [[ ! -f "$BACKUP_STATUS" ]]; then
  failures+=("backup_status_missing")
fi

if (("${#failures[@]}" > 0)); then
  message="Donation Alerts health alert: ${failures[*]}"
  echo "$message" >&2
  current="$(printf '%s' "${failures[*]}")"
  previous="$(cat "$ALERT_FILE" 2>/dev/null || true)"
  if [[ "$current" != "$previous" ]]; then
    send_alert "$message"
    printf '%s' "$current" > "$ALERT_FILE"
    chmod 600 "$ALERT_FILE"
  fi
  exit 1
fi
rm -f "$ALERT_FILE"
echo "healthcheck=ok"
