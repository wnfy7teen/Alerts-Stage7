#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

BASE_URL="${BASE_URL:-https://vanesso4ka.site}"
HEALTH_URL="$BASE_URL/api/health"
SERVICE_NAME="${SERVICE_NAME:-donation-alerts.service}"
BACKUP_STATUS="${BACKUP_STATUS:-/var/lib/donation-alerts/backup-status.json}"
DB_PATH="${DB_PATH:-/var/lib/donation-alerts/donations.db}"
MIN_FREE_KB="${MIN_FREE_KB:-1048576}"
MIN_MEM_AVAILABLE_KB="${MIN_MEM_AVAILABLE_KB:-262144}"
BACKUP_MAX_AGE_SECONDS="${BACKUP_MAX_AGE_SECONDS:-172800}"
STUCK_PAYMENT_SECONDS="${STUCK_PAYMENT_SECONDS:-3600}"
MAX_OUTBOX_AGE_SECONDS="${MAX_OUTBOX_AGE_SECONDS:-300}"
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
mem_available_kb="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
if [[ -z "$mem_available_kb" || "$mem_available_kb" -lt "$MIN_MEM_AVAILABLE_KB" ]]; then
  failures+=("memory_available_below_threshold")
fi

backup_state="$(python3 - "$BACKUP_STATUS" "$BACKUP_MAX_AGE_SECONDS" <<'PY'
import json, sys, time
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    age = time.time() - float(data.get("last_success_at") or 0)
    ok = data.get("status") == "success" and 0 <= age <= int(sys.argv[2])
    print("ok" if ok else "stale_or_failed")
except Exception:
    print("missing_or_invalid")
PY
)"
if [[ "$backup_state" != "ok" ]]; then
  failures+=("backup_$backup_state")
fi

if [[ -r "$DB_PATH" ]]; then
  db_state="$(python3 - "$DB_PATH" "$STUCK_PAYMENT_SECONDS" "$MAX_OUTBOX_AGE_SECONDS" <<'PY'
import sqlite3, sys, time
path, payment_age, outbox_age = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    now = time.time()
    pending = db.execute(
        "SELECT COUNT(*) FROM donations WHERE status IN ('created','pending') AND created_at < ?",
        (now - payment_age,),
    ).fetchone()[0]
    oldest = db.execute(
        "SELECT MIN(created_at) FROM alert_outbox WHERE delivered_at IS NULL"
    ).fetchone()[0]
    queue_age = int(now - oldest) if oldest is not None else 0
    db.close()
    print(f"{pending}:{queue_age}")
except Exception:
    print("db_error")
PY
)"
  if [[ "$db_state" == "db_error" ]]; then
    failures+=("database_monitor_query_failed")
  else
    stale_payments="${db_state%%:*}"
    outbox_age="${db_state##*:}"
    if [[ "$stale_payments" -gt 0 ]]; then failures+=("stale_pending_payments:$stale_payments"); fi
    if [[ "$outbox_age" -gt "$MAX_OUTBOX_AGE_SECONDS" ]]; then failures+=("stale_obs_outbox:$outbox_age"); fi
  fi
else
  failures+=("database_file_unreadable")
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
if [[ -f "$ALERT_FILE" ]]; then
  send_alert "Donation Alerts recovery: all health checks passed."
  rm -f "$ALERT_FILE"
fi
echo "healthcheck=ok"
