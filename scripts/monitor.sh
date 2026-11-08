#!/usr/bin/env bash
set -Eeuo pipefail
ENV_FILE="${MONITOR_ENV_FILE:-/etc/donation-alerts-monitor.env}"
[[ -r "$ENV_FILE" ]] && source "$ENV_FILE"
STATE_FILE="${MONITOR_STATE_FILE:-/var/lib/donation-alerts/monitor-state}"
SITE_URL="${SITE_URL:-https://vanesso4ka.site}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/health}"
DISK_PATH="${DISK_PATH:-/var/lib/donation-alerts}"
BACKUP_STATUS_PATH="${BACKUP_STATUS_PATH:-/var/lib/donation-alerts/backup-status.json}"
issues=()
systemctl is-active --quiet "${SERVICE_NAME:-donation-alerts.service}" || issues+=("donation-alerts.service inactive")
systemctl is-active --quiet "${NGINX_NAME:-nginx.service}" || issues+=("nginx.service inactive")
curl -fsSL --max-time 12 "$SITE_URL/" >/dev/null 2>&1 || issues+=("public homepage unavailable")
health="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null || true)"
[[ "$health" == *'"status":"ok"'* ]] || issues+=("local API health failed")
ws_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  "http://127.0.0.1:8000/ws?key=healthcheck-invalid" 2>/dev/null || true)"
[[ "$ws_code" == "403" ]] || issues+=("WebSocket endpoint handshake failed")
if journalctl -u "${SERVICE_NAME:-donation-alerts.service}" --since "5 minutes ago" -o cat --no-pager 2>/dev/null \
  | grep -Eiq 'ЮKassa:|NOWPayments:|Crypto Pay |Не удалось проверить'; then
  issues+=("payment provider verification errors in recent application logs")
fi
python3 - "$BACKUP_STATUS_PATH" <<'PY' || issues+=("backup status missing, failed or stale")
import json, sys, time
from pathlib import Path
try:
    data = json.loads(Path(sys.argv[1]).read_text())
    ok = data.get("status") == "success" and float(data.get("last_success_at") or 0) >= time.time() - 48 * 3600
except (OSError, ValueError, TypeError):
    ok = False
raise SystemExit(0 if ok else 1)
PY
free_pct="$(df -P "$DISK_PATH" | awk 'NR==2 {gsub("%","",$5); print 100-$5}')"
[[ -n "$free_pct" && "$free_pct" -ge 10 ]] || issues+=("disk free below 10%")
avail="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
total="$(awk '/MemTotal:/ {print $2}' /proc/meminfo)"
if [[ -n "$avail" && -n "$total" && "$total" -gt 0 ]]; then
  (( avail * 100 / total >= 10 )) || issues+=("available RAM below 10%")
fi
load="$(awk '{print $1}' /proc/loadavg)"; cpus="$(getconf _NPROCESSORS_ONLN)"
awk -v l="$load" -v c="$cpus" 'BEGIN {exit !(l < c*2)}' || issues+=("load average above 2x CPU count")
send_alert() {
  local message="$1"
  if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -fsS --max-time 10 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$message" >/dev/null
  else
    logger -t donation-alerts-monitor "$message"
  fi
}
if ((${#issues[@]} > 0)); then
  fingerprint="$(printf '%s\n' "${issues[@]}" | sha256sum | awk '{print $1}')"
  previous="$(cat "$STATE_FILE" 2>/dev/null || true)"
  if [[ "$fingerprint" != "$previous" ]]; then
    send_alert "Donation Alerts WARNING: $(printf '%s; ' "${issues[@]}")"
    printf '%s' "$fingerprint" > "$STATE_FILE"
  fi
else
  previous="$(cat "$STATE_FILE" 2>/dev/null || true)"
  if [[ -n "$previous" ]]; then
    send_alert "Donation Alerts RECOVERED: all health checks are healthy."
    : > "$STATE_FILE"
  fi
fi
