# Donation Alerts — Production hardening runbook

Repository: `[GITHUB_REPO]`  
Production host: `[VPS_HOST]`  
Application directory: `[PROJECT_PATH]`  
Persistent data: `[DATA_PATH]`  
Database: `[DB_PATH]`  
Backup directory: `[BACKUP_DIR]`

> This document separates verified repository facts from operations that require production access. Never use real payment/refund operations as smoke tests. Use provider sandbox credentials and a staging callback URL.

## 0. PR #88 — terminal refund state

### Verified in source
- `record_refund()` uses `BEGIN IMMEDIATE`, validates provider and currency, and deduplicates on the unique key `(provider, provider_refund_id)`.
- The original PR changed the accepted status list so a fully `refunded` donation is terminal.
- A unit/regression test covers known and unknown refund amounts.
- An HTTP integration test was added: it calls the YooKassa refund webhook for a full refund, then a distinct late refund webhook, and asserts that status, count, and refunded total remain unchanged.

### Exact release sequence
1. PR #88 is now merged. Tested head SHA: `e0c69f2912ee4f1e2696b3996d27bffbd77d3d0c`; CI run `42505346624` succeeded (142 tests, including the new HTTP refund regression, plus browser tests).
2. Merge SHA: `6d46c7fe41631776f22d98edaac09531e4268c3c`.
3. The production push/deploy workflow result has not been independently confirmed through the available GitHub status interface; do not infer deployment from merge success.
4. Verify the `push` workflow on `main`; both test job and **Deploy production** job must succeed. A skipped deploy is not a successful deployment.
5. Run non-financial smoke checks:
   - `curl -fsS https://[DOMAIN]/api/health`
   - `curl -fsSL -o /dev/null -w '%{http_code}\n' https://[DOMAIN]/`
   - `systemctl is-active donation-alerts.service`
   - `journalctl -u donation-alerts.service --since '-10 min' --no-pager`
7. Refund regression smoke must run in provider sandbox/staging only: create sandbox payment → refund full amount → send the same refund event again → send a distinct late refund event → verify status remains `refunded`, exactly one accepted refund row per provider refund ID, and cumulative amount does not exceed original amount. Never run this against a real donor payment.

### Post-deploy checklist
- [ ] Merge SHA matches the deploy workflow SHA.
- [ ] Test job succeeded on main.
- [ ] Deploy production job succeeded (not skipped).
- [ ] Both apex and www homepage return 200 after redirects.
- [ ] `/api/health` returns JSON status ok.
- [ ] Service active; no new traceback or database errors.
- [ ] OBS overlay connects and ACKs a test alert.
- [ ] Sandbox refund regression passes.
- [ ] Existing pending donations and outbox rows remain present.

## 1. Payment audit

| Provider | Authentication / verification | Source finding | Status | Fix / ticket |
|---|---|---|---|---|
| YooKassa | YooKassa webhook does not carry an HMAC signature in this implementation. Optional `YOOKASSA_WEBHOOK_IPS` allowlist is checked. The webhook is only a trigger: payment/refund is fetched from YooKassa API using shop credentials; payment metadata donation ID, amount and currency are compared. | Strong API re-verification. IP allowlist is optional; with an empty list, webhook origin is not restricted at the network layer. Refund IDs are validated and provider refund is fetched. | Partially verified in code; production env/IP ranges require host and provider-console review. | P1: configure official YooKassa webhook IP ranges in `YOOKASSA_WEBHOOK_IPS`; add tests for missing/invalid IP list behavior and unknown refund/payment IDs. |
| NOWPayments | HMAC-SHA512 in `x-nowpayments-sig`, constant-time compare. Canonical sorted compact JSON is supported; raw-body HMAC compatibility is also accepted. Webhook payment ID is used to locate local row; actual status is fetched from provider API and order ID, price amount and currency checked. | Invalid signatures return 403. Duplicate webhook does not directly credit: provider status is re-read and `mark_paid` is conditional. No durable event-ID ledger exists. | Code verified; live credential/callback verification not performed. | P1: persist provider event/delivery ID where available; add replay/concurrency tests; test mismatched payment ID/order ID/price currency. |
| Crypto Pay | HMAC-SHA256 over raw request body using SHA256(bot token) as key; constant-time compare. Invoice ID maps to local payment; API `getInvoices` is queried and fiat type, amount, fiat currency checked. | Invalid signature returns 403. Signed malformed payload returns 400. No durable event-ID ledger exists; provider invoice ID is unique locally. | Code verified; production token/API status not tested here. | P1: add durable webhook event ledger and replay/concurrency tests; verify bot token and production API reachability from VPS without exposing token. |

### Cross-provider controls found
- Unique partial index on `(provider, provider_txn_id)` prevents one provider transaction from being attached to multiple donations.
- `refunds` has unique `(provider, provider_refund_id)`; `record_refund()` serializes writes using SQLite `BEGIN IMMEDIATE`.
- `mark_paid()` uses a conditional SQL update and inserts an outbox row in the same DB transaction. The outbox has one row per donation.
- Amount/currency and provider-specific references are checked in provider verdict functions.
- NOWPayments and Crypto Pay duplicate events still cause provider API reads; this is safe for crediting but does not give an auditable event-replay ledger.
- `mark_paid()` currently permits a provider-confirmed success to move some `failed`, `canceled`, or `expired` rows to `paid`. This may be intentional late-settlement recovery, but it is not an explicit documented state machine. Add a policy + tests before changing it. Never silently block a genuinely settled payment.
- Refund event IDs are deduplicated, but generic webhook event IDs are not stored. Introduce a `webhook_events` table only after confirming each provider's event-ID semantics and retention.

### Required transition policy (proposed)
- `created → pending` after a provider invoice/payment ID is safely persisted.
- `created/pending → paid` only after provider API verification and exact local reference/amount/currency match.
- `paid → partially_refunded → refunded` only after provider API confirms each refund.
- `refunded` is terminal for additional refund notifications.
- `failed/canceled/expired → paid` must be treated as a late settlement exception: fetch provider state, record an audit event, and alert staff; do not discard money received. Add explicit transition tests.
- Provider errors, timeouts, malformed JSON and signature failures must never change a payment status.

### Staging-only replay/concurrency tests
- Send identical signed webhook payload concurrently (at least 10 requests); assert one paid transition, one outbox row and one delivered alert ID.
- Send the same refund ID concurrently; assert one refund row.
- Send two distinct partial refunds concurrently; assert sum is at most original amount.
- Send a forged signature, wrong provider payment ID, wrong donation metadata, amount mismatch and currency mismatch; assert no credit.
- Send out-of-order `pending`, `paid`, `refunded` provider states; assert policy above.

## 2. Disaster recovery and off-site backups

### Existing in-app backup
`scripts/backup.py` uses SQLite's online backup API, runs `PRAGMA integrity_check`, hashes DB/config/media, creates a manifest, validates the archive, and applies local retention only after successful verification. It deliberately excludes environment secrets. **It is not encrypted at rest by itself.** The current production DB path must be passed explicitly via the systemd environment; the Python default is under the project directory and is not the known production data path.

### Off-site encrypted copy artifacts
- `scripts/remote_backup.sh`
- `ops/systemd/donation-alerts-remote-backup.service`
- `ops/systemd/donation-alerts-remote-backup.timer`

The script expects an rclone **crypt remote** named by `REMOTE_PATH`; configure the crypt remote to wrap an S3-compatible remote. This encrypts filenames and contents client-side before upload. Keep the rclone config and crypt password/salt outside Git, mode 0600, root-owned. Do not put credentials in command arguments or repository files.

Example `/etc/donation-alerts-remote-backup.env` (replace placeholders):
```ini
BACKUP_DIR=[BACKUP_DIR]
REMOTE_PATH=donation-backups-crypt:donation-alerts
RCLONE_CONFIG=/etc/rclone/rclone.conf
```

Configure rclone:
1. Install rclone from its official package/repository.
2. Create an S3 remote named `s3-primary` with endpoint, bucket and access keys in root's rclone config.
3. Create a crypt remote named `donation-backups-crypt` wrapping `s3-primary:[BUCKET]/[PREFIX]`.
4. Set `/etc/rclone/rclone.conf` owner root, mode 0600. Keep crypt password and salt in that protected file or a supported secret manager.
5. Confirm bucket versioning/object lock and server-side encryption policy; client-side crypt remains the primary confidentiality layer.
6. Create env file mode 0600, then install timer:

```bash
install -o root -g root -m 600 /dev/null /etc/donation-alerts-remote-backup.env
# Populate with the values above using a secure editor; do not paste credentials into shell history.
install -o root -g root -m 644 [PROJECT_PATH]/ops/systemd/donation-alerts-remote-backup.service /etc/systemd/system/
install -o root -g root -m 644 [PROJECT_PATH]/ops/systemd/donation-alerts-remote-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now donation-alerts-remote-backup.timer
systemctl start donation-alerts-remote-backup.service
systemctl status donation-alerts-remote-backup.service --no-pager
journalctl -u donation-alerts-remote-backup.service -n 50 --no-pager
```

### Restore drill (isolated)
Never restore over production as part of a drill. Copy a selected archive to a test location and run:
```bash
install -d -m 700 [RESTORE_PARENT]
[PROJECT_PATH]/venv/bin/python [PROJECT_PATH]/scripts/restore_drill.py \
  [BACKUP_DIR]/[ARCHIVE].tar.gz [RESTORE_PARENT]/restore-$(date +%Y%m%d-%H%M%S)
```
The destination must not already exist. The script rejects unsafe archive members, verifies manifest hashes, runs SQLite integrity check, validates JSON config and reports media file count. Afterward, inspect files and run the application against the restored DB only in a separate staging unit with isolated ports and credentials. This runbook does not claim that a restore drill has already been executed.

### VPS restart test (staging first)
```bash
systemctl is-enabled donation-alerts.service
systemctl is-active donation-alerts.service
systemctl is-enabled donation-alerts-backup.timer
systemctl list-timers --all | grep donation-alerts
```
On a staging VPS, reboot in a maintenance window, then verify service active, local health 200, public HTTPS 200, WebSocket/OBS reconnect, poller resumes and timers are active. Do not reboot production as part of an unapproved test.

### Disk-full and provider-outage behavior
- Simulate on staging only using a loopback filesystem with a strict size limit; never fill the production root filesystem.
- Verify SQLite write errors do not report successful payment/refund state; backup failure is visible in systemd and Telegram alert; Nginx/app remain recoverable.
- Mock provider timeouts/5xx/malformed JSON in tests. Expected: status remains unchanged, poller continues, outbox retries with bounded backoff, ambiguous payment creation is not blindly duplicated.
- Alert at less than 1 GiB free or a project-specific threshold; maintain emergency free space for SQLite WAL and logs.

## 3. Security checklist

| Check | How to verify | Expected | Fix if not |
|---|---|---|---|
| Admin authentication | `curl -i https://[DOMAIN]/admin` without credentials; inspect `ADMIN_PASSWORD` presence/length in root-only env without printing it | 401; strong unique password; TLS only | Rotate password via secure env editor; restart service; restrict admin IP CIDRs if operationally possible |
| Session model | Review `server.py`: admin currently uses HTTP Basic, not server-side sessions | Browser sends credentials on each request; no session cookie to revoke | Document this explicitly; if migrating to sessions, use secure HttpOnly SameSite cookies, CSRF token, expiry and logout/revocation |
| Brute force | Test repeated wrong password from staging IP | 10 failures/15 min per process returns 429 | Add persistent/shared limiter at reverse proxy if multiple workers or restarts are possible |
| CSRF | POST/PUT admin mutation without custom header or with foreign Origin | 403; same-origin mutation succeeds | Keep `csrf_guard` on every state-changing admin route |
| XSS | Review admin/public JS for `innerHTML`; test donor text containing HTML/script in staging | Render via `textContent`/safe templates; CSP nonce | Replace unsafe HTML insertion; sanitize at boundary and encode output |
| SQL injection | Review SQL calls and test quote/wildcard payloads in staging | Parameterized SQL; no string interpolation for values | Parameterize every value; whitelist sort/filter identifiers |
| Rate limiting | Verify API/webhook/admin limiters and nginx limits | 429 at configured thresholds | Add shared edge rate limit for multi-process deployments |
| Python dependencies | `[PROJECT_PATH]/venv/bin/pip-audit -r [PROJECT_PATH]/requirements.txt` and dev/e2e requirements | No known vulnerable pinned/transitive packages; report retained | Upgrade in branch, run tests, PR, deploy |
| Nginx | `nginx -t`; inspect active server blocks; verify TLS, HTTP/2/3 policy, websocket map, body/timeouts, no exposed upstream port | Syntax valid; 80 redirects to 443; app bound to localhost; correct websocket Upgrade map | Back up config before edits; test config; reload only after successful `nginx -t` |
| Secret files | `git check-ignore -v .env`; `git ls-files | grep -E '(^|/)(\.env|.*\.pem|.*\.key|.*\.db|.*\.sqlite)' ` | No live env, private keys, DB or backups tracked | Add ignore rules; rotate any exposed secret immediately |
| Secret history | Run `gitleaks git [GITHUB_REPO]` on a secured clone and scan all refs/history | No credential findings | Rotate first; then purge history only under incident procedure; a new commit does not erase leaked secrets |
| Logs / Sentry | Review logger calls and Sentry scrubber; inspect a sample redacted event | No Authorization headers, webhook signatures, tokens, passwords, donor messages or query secrets | Log exception class/provider/status only; scrub request body/headers/query; disable body capture |
| Backups | Inspect archive manifest and file mode; inspect encrypted remote with rclone cryptcheck | No .env/provider keys; local archive 0600; remote encrypted | Keep secrets in a separate password manager/secret store; rotate if included in old backups |

### Immediate security observations
- Admin currently uses HTTP Basic with a constant-time password comparison and an in-memory per-process failure limiter. This is not a session-based login system.
- State-changing admin routes are grouped under `ADMIN = [Depends(admin), Depends(sec.csrf_guard)]`; verify every mutation route uses this dependency.
- CSP nonce and common security headers are implemented. Verify whether CSP is enforced or report-only via `CSP_REPORT_ONLY`.
- `.gitignore` on `main` excludes `.env`, virtual environments and common local DB names. PR #89 adds explicit rules for `.env.*`, project DB files, SQLite WAL files and backup archives; these rules are not active on `main` until PR #89 is merged. Scan tracked files/history separately.
- Sentry scrubber removes request headers/body and query strings, but ordinary application logs still need a separate audit; `provider_fail()` currently logs a truncated provider detail, which must be reviewed for token/PII leakage.
- Dependency audit and full git-history secret scan have not been run from a secured checkout in this task.

## 4. Monitoring, Telegram alerts and admin journal

### Healthcheck artifacts
- `scripts/healthcheck.sh`
- `ops/systemd/donation-alerts-healthcheck.service`
- `ops/systemd/donation-alerts-healthcheck.timer`

Example `/etc/donation-alerts-healthcheck.env`:
```ini
BASE_URL=https://[DOMAIN]
SERVICE_NAME=donation-alerts.service
BACKUP_STATUS=[BACKUP_STATUS_PATH]
DB_PATH=[DB_PATH]
MIN_FREE_KB=1048576
MIN_MEM_AVAILABLE_KB=262144
BACKUP_MAX_AGE_SECONDS=172800
STUCK_PAYMENT_SECONDS=3600
MAX_OUTBOX_AGE_SECONDS=300
TELEGRAM_BOT_TOKEN=[TELEGRAM_BOT_TOKEN]
TELEGRAM_CHAT_ID=[TELEGRAM_CHAT_ID]
```
Set owner root/mode 0600. Install:
```bash
install -o root -g root -m 600 /dev/null /etc/donation-alerts-healthcheck.env
# Populate securely; never commit this file.
install -o root -g root -m 755 [PROJECT_PATH]/scripts/healthcheck.sh /usr/local/sbin/donation-alerts-healthcheck
install -o root -g root -m 644 [PROJECT_PATH]/ops/systemd/donation-alerts-healthcheck.service /etc/systemd/system/
install -o root -g root -m 644 [PROJECT_PATH]/ops/systemd/donation-alerts-healthcheck.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now donation-alerts-healthcheck.timer
systemctl start donation-alerts-healthcheck.service
journalctl -u donation-alerts-healthcheck.service -n 30 --no-pager
```
The script checks public API health, systemd service, root filesystem free space, available memory, backup success age, old pending payments and oldest pending OBS outbox record. Telegram notification is sent when the failure set changes and a recovery notification is sent when all checks pass again; the alert-state file is stored under persistent data. It does not inspect provider-specific error rates or remote object-store availability directly.

### Suggested read-only SQL monitoring
Run against a consistent read-only copy or with the app's DB connection during low traffic:
```sql
-- Payment state counts
SELECT provider, status, COUNT(*) AS n
FROM donations GROUP BY provider, status ORDER BY provider, status;

-- Pending/created payments older than 20 minutes
SELECT id, provider, provider_txn_id, amount, currency, status,
       datetime(created_at, 'unixepoch') AS created_utc
FROM donations
WHERE status IN ('created','pending') AND created_at < strftime('%s','now') - 1200
ORDER BY created_at ASC LIMIT 100;

-- Pending OBS alerts and oldest age
SELECT COUNT(*) AS pending_alerts,
       MIN(created_at) AS oldest_created_at,
       MAX(attempts) AS max_attempts
FROM alert_outbox WHERE delivered_at IS NULL;

-- Refund totals that exceed original donation amount (must return zero rows)
SELECT d.id, d.amount, d.currency, SUM(r.amount) AS refunded
FROM donations d JOIN refunds r ON r.donation_id=d.id AND r.status='succeeded'
GROUP BY d.id HAVING SUM(COALESCE(r.amount,0)) > d.amount + 0.01;

-- Admin audit
SELECT id, datetime(created_at,'unixepoch') AS at_utc, actor, action, target, details_json
FROM admin_audit ORDER BY id DESC LIMIT 100;
```

### Existing journal capability
- `donations` and `refunds` store payment/refund records.
- `list_transactions()` provides paginated financial journal fields including refund count and cumulative refund amount.
- `admin_audit` and `record_admin_action()` already exist; configuration changes record an audit event. Verify every administrative mutation is logged, especially media upload/delete, test alert, payment settings, and manual actions.
- Keep donor messages out of audit metadata. Store actor identity and action/target only; details must be allowlisted and secrets redacted.

### Existing admin journal and audit APIs
- Financial journal already exists: `GET /api/admin/transactions?page=1&limit=50&status=&provider=&q=`.
- CSV export already exists: `GET /api/admin/transactions.csv?status=&provider=&q=`; CSV cells are protected against formula injection.
- Audit API already exists: `GET /api/admin/audit?page=1&limit=50`.
- The admin HTML already renders journal, analytics and audit views. Review every `innerHTML` insertion for context-appropriate escaping; current inspected rows use `esc()` for donor-controlled values.
- UI columns: date/time, donation ID (shortened), provider, donor, amount/currency, status, refund total/count; details drawer shows provider transaction ID and refund history with sensitive data masked.
- Audit view: timestamp, actor, action, target; never render untrusted fields as HTML.
- Monitoring dashboard: service health, latest backup timestamp/result, free disk, pending outbox count/oldest age, pending payment count/oldest age, provider error counts.

## 5. Final operational acceptance checklist

- [ ] PR #88 latest head CI green, merged and deployed.
- [ ] Refund webhook integration test passes in CI and sandbox.
- [ ] Webhook signature/verification failures do not mutate state.
- [ ] Duplicate/concurrent payment and refund tests pass.
- [ ] Explicit state transition policy implemented and tested.
- [ ] Local backup timer succeeds with production DB/config/media paths.
- [ ] Encrypted off-site backup timer installed; remote cryptcheck passes.
- [ ] Isolated restore drill completed and evidence retained.
- [ ] Staging reboot test completed.
- [ ] pip-audit and Gitleaks scans completed and findings remediated.
- [ ] Healthcheck and Telegram alerts installed and tested.
- [ ] All admin mutations audited; financial journal and audit UI reviewed.
