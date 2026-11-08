# Production reliability runbook

## Payment audit
- YooKassa: webhook has no signature in this integration; event is verified by authenticated YooKassa API lookup. Optional IP allowlist: YOOKASSA_WEBHOOK_IPS.
- NOWPayments: IPN HMAC-SHA512 validation.
- Crypto Pay: HMAC-SHA256 validation derived from API token.
- All providers: provider API is the source of truth; order/payment ID, amount and currency are checked before credit.
- Idempotency-Key, unique (provider, provider_txn_id), atomic paid transition and durable alert_outbox protect against duplicate credits.
- Poller reconciles pending provider transactions; never locally expire an invoice once provider_txn_id exists.
- Alert delivery to OBS is at-least-once retry with a stable donation ID, but WebSocket send does not prove the browser rendered it.

## Backup environment
Set in /etc/donation-alerts.env:
```ini
DB_PATH=/var/lib/donation-alerts/donations.db
CONFIG_PATH=/var/lib/donation-alerts/config.json
MEDIA_DIR=/var/lib/donation-alerts/media
BACKUP_DIR=/var/lib/donation-alerts/backups
BACKUP_STATUS_PATH=/var/lib/donation-alerts/backup-status.json
BACKUP_KEEP=14
```
Create service:
```ini
# /etc/systemd/system/donation-alerts-backup.service
[Unit]
Description=Verified Donation Alerts backup
After=local-fs.target
[Service]
Type=oneshot
User=donationapp
Group=donationapp
WorkingDirectory=/opt/donation-alerts
EnvironmentFile=/etc/donation-alerts.env
ExecStart=/opt/donation-alerts/venv/bin/python /opt/donation-alerts/scripts/backup.py
UMask=0077
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/var/lib/donation-alerts
```
Timer:
```ini
# /etc/systemd/system/donation-alerts-backup.timer
[Unit]
Description=Daily Donation Alerts backup
[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true
RandomizedDelaySec=10m
[Install]
WantedBy=timers.target
```
After ensuring the backups directory is writable by donationapp:
```bash
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/donation-alerts-backup.service /etc/systemd/system/donation-alerts-backup.timer
systemctl enable --now donation-alerts-backup.timer
systemctl start donation-alerts-backup.service
systemctl status donation-alerts-backup.service --no-pager
```

## Isolated restore test
Dry-run:
```bash
sudo -u donationapp /opt/donation-alerts/venv/bin/python /opt/donation-alerts/scripts/restore_backup.py /var/lib/donation-alerts/backups/[ARCHIVE].tar.gz --target /var/lib/donation-alerts/restore-test-[STAMP]
```
Actual restore to isolated directory (not production):
```bash
sudo -u donationapp /opt/donation-alerts/venv/bin/python /opt/donation-alerts/scripts/restore_backup.py /var/lib/donation-alerts/backups/[ARCHIVE].tar.gz --target /var/lib/donation-alerts/restore-test-[STAMP] --apply
```
Never target the live data directory in a routine restore test.

## Telegram monitor
Create /etc/donation-alerts-monitor.env mode 0600:
```ini
TELEGRAM_BOT_TOKEN=[TELEGRAM_BOT_TOKEN]
TELEGRAM_CHAT_ID=[TELEGRAM_CHAT_ID]
SITE_URL=https://vanesso4ka.site
HEALTH_URL=http://127.0.0.1:8000/api/health
DISK_PATH=/var/lib/donation-alerts
```
Install monitor.sh as /usr/local/sbin/donation-alerts-monitor, plus supplied systemd service/timer under /etc/systemd/system:
```bash
chmod 0750 /usr/local/sbin/donation-alerts-monitor
chmod 0600 /etc/donation-alerts-monitor.env
systemd-analyze verify /etc/systemd/system/donation-alerts-monitor.service /etc/systemd/system/donation-alerts-monitor.timer
systemctl daemon-reload
systemctl enable --now donation-alerts-monitor.timer
systemctl start donation-alerts-monitor.service
journalctl -u donation-alerts-monitor.service -n 30 --no-pager
```
Checks public homepage, local API, app service, Nginx, disk, available RAM and load. Alerts on changed failure state and recovery. It does not perform a real payment, call authenticated provider APIs, or inspect the private admin health endpoint.

## Payment charged but donation missing
1. Do not ask the donor to pay again.
2. Search the admin transaction journal by provider transaction ID, donation ID, name and amount.
3. Check provider dashboard/API for exact ID, amount, currency and final state.
4. Inspect service logs: journalctl -u donation-alerts.service --since "[TIME]". Never paste secrets or full webhook bodies.
5. Confirm the provider ID maps to the correct donation and metadata/order ID, amount and currency match.
6. Allow poller reconciliation; inspect stale pending count and provider API errors.
7. If DB says paid but OBS alert is absent, inspect alert_outbox and OBS connection; replay only after confirming paid state.
8. Never mark paid based only on a screenshot or unverified webhook.

## Provider outage
Keep pending provider-backed payments pending; check provider status, DNS/network, TLS, system clock and logs. Do not locally expire a transaction with provider_txn_id. After recovery verify poller reconciliation and no duplicate alert.

## Database loss/corruption
Preserve copies of DB plus -wal/-shm before attempting repair. Run integrity checks on a copy. Restore the verified archive into a new directory; compare provider transaction history for payments since backup. Cut over only after checking restored DB, config and media. Never delete live WAL/SHM as a repair shortcut.

## Website outage
Check DNS, TLS, nginx -t, Nginx/app status, local health, disk and memory. Inspect journalctl for both services. Never reload Nginx before nginx -t passes.

## Backup failure
Check service status/logs, free space, permissions and DB/config/media paths. Do not delete older backups before an external copy is verified. Rerun backup and validate with restore script dry-run plus isolated --apply restore.
