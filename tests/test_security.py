import json
import logging
import re
from contextlib import ExitStack, closing

import httpx
import pytest
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

import payments as pay
import security as sec
import server
from helpers import KEY, donate, route_through

AUTH = ("admin", "pw-test-1234567")
H = {"X-Requested-With": "donation-admin"}
GIF = b"GIF89a" + b"\x00" * 30


def test_failure_limiter_prunes_expired_unique_ips(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(sec.time, "monotonic", lambda: clock[0])
    limiter = sec.FailureLimiter(10, 900)

    for i in range(10_001):
        limiter.fail(f"ip-{i}")

    assert len(limiter._fails) == 10_001
    clock[0] = 901.0
    limiter.fail("fresh-ip")
    assert list(limiter._fails) == ["fresh-ip"]


# ───── заголовки и CSP ─────
def test_headers_and_csp_nonce(client):
    a, b = client.get("/"), client.get("/")
    csp = a.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'nonce="{nonce}"' in a.text and nonce not in b.headers["content-security-policy"]
    assert "<script>" not in a.text and "<style>" not in a.text  # весь inline-код с nonce
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp and "frame-ancestors 'none'" in csp
    assert a.headers["x-content-type-options"] == "nosniff" and a.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" in a.headers and "permissions-policy" in a.headers
    assert client.get("/thanks").headers["content-security-policy"].startswith("default-src 'none'")


def test_private_pages_are_strict(client):
    ov = client.get("/overlay")
    assert "ws://testserver" in ov.headers["content-security-policy"] and ov.headers["referrer-policy"] == "no-referrer"
    adm = client.get("/admin", auth=AUTH)
    assert "yookassa" not in adm.headers["content-security-policy"] and adm.headers["cache-control"] == "no-store"
    assert client.get("/api/public-config").headers["cache-control"] == "no-store"


def test_no_cors_headers(client):
    r = client.options("/api/donations", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_health_has_no_internals(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_csp_report_endpoint(client):
    assert client.post("/api/csp-report", content=b'{"csp-report":{}}').status_code == 204


# ───── админка: вход, перебор, CSRF, список IP ─────
def test_admin_requires_auth(client):
    assert client.get("/api/admin/config").status_code == 401
    assert client.get("/api/admin/config", auth=("admin", "wrong")).status_code == 401

def test_admin_delivery_stats_is_private_and_reports_queue(client):
    assert client.get("/api/admin/delivery").status_code == 401
    donation_id = donate(client).json()["donation_id"]
    with closing(pay.db()) as db:
        db.execute(
            "INSERT INTO alert_outbox(donation_id,created_at,attempts,next_attempt_at,last_error) "
            "VALUES(?,?,?,?,?)", (donation_id, pay.time.time() - 120, 3, pay.time.time(), "no_overlay_clients")
        )
        db.commit()
    response = client.get("/api/admin/delivery", auth=AUTH)
    assert response.status_code == 200
    stats = response.json()
    assert stats["pending"] == 1 and stats["delivered"] == 0
    assert stats["pending_attempts"] == 3 and stats["oldest_pending_seconds"] >= 120
    assert stats["errors"] == [{"code": "no_overlay_clients", "count": 1}]




def test_admin_bruteforce_lockout(client, monkeypatch):
    monkeypatch.setattr(server, "auth_failures", sec.FailureLimiter(10, 900))
    assert [client.get("/admin", auth=("admin", f"bad{i}")).status_code for i in range(10)] == [401] * 10
    assert client.get("/admin", auth=AUTH).status_code == 429  # даже верный пароль после блокировки


def test_admin_csrf(client):
    body = {"name": "x", "message": "y", "amount": 100}
    assert client.post("/api/admin/test", auth=AUTH, json=body).status_code == 403                       # нет заголовка
    assert client.post("/api/admin/test", auth=AUTH, json=body,
                       headers={**H, "Origin": "https://evil.example"}).status_code == 403               # чужой Origin
    assert client.post("/api/admin/test", auth=AUTH, json=body,
                       headers={**H, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/admin/test", auth=AUTH, json=body,
                       headers={**H, "Origin": "http://testserver"}).status_code == 200                  # свой сайт
    assert client.get("/api/admin/config", auth=AUTH).status_code == 200                                  # чтение без заголовка можно


def test_admin_ip_allowlist(client, monkeypatch):
    monkeypatch.setattr(sec, "ADMIN_ALLOWED", ["10.0.0.0/8"])
    assert client.get("/admin", auth=AUTH).status_code == 404
    assert client.get("/").status_code == 200


def test_client_ip_behind_proxy(monkeypatch):
    def req(**headers):
        return Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                        "client": ("127.0.0.1", 1)})
    assert sec.client_ip(req(**{"x-forwarded-for": "1.1.1.1"})) == "127.0.0.1"     # без доверия заголовки игнорируются
    monkeypatch.setattr(sec, "TRUST_PROXY_HEADERS", True)
    assert sec.client_ip(req(**{"x-forwarded-for": "6.6.6.6, 9.9.9.9"})) == "9.9.9.9"  # левые записи подделывает клиент
    assert sec.client_ip(req(**{"cf-connecting-ip": "5.6.7.8", "x-forwarded-for": "9.9.9.9"})) == "5.6.7.8"
    assert sec.client_ip(req(**{"cf-connecting-ip": "not-an-ip"})) == "127.0.0.1"


def test_https_redirect_uses_configured_host(client, monkeypatch):
    monkeypatch.setattr(sec, "FORCE_HTTPS", True)
    monkeypatch.setattr(sec, "TRUST_PROXY_HEADERS", True)
    r = client.get("/donate?x=1", headers={"x-forwarded-proto": "http", "host": "evil.example"}, follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "https://example.test/donate?x=1"
    assert client.get("/api/health", headers={"x-forwarded-proto": "http"}, follow_redirects=False).status_code == 200


# ───── лимиты и размеры ─────
def test_global_api_limit(client, monkeypatch):
    monkeypatch.setattr(server, "api_limiter", pay.RateLimiter(3, 60))
    assert [client.get("/api/public-config").status_code for _ in range(4)] == [200, 200, 200, 429]


def test_body_size_limit(client):
    assert client.post("/api/donations", content=b"x" * (1024 * 1024 + 10)).status_code == 413


def test_webhook_and_csp_body_limits(client):
    oversized = b"x" * (server.WEBHOOK_BODY_LIMIT + 1)
    for path in ("/api/webhooks/yookassa", "/api/webhooks/nowpayments",
                 "/api/webhooks/cryptopay", "/crypto-webhook"):
        assert client.post(path, content=oversized).status_code == 413, path
    assert client.post("/api/csp-report", content=b"x" * (server.CSP_REPORT_BODY_LIMIT + 1)).status_code == 413


def test_websocket_limit(client):
    server.clients.clear()  # Remove the fake overlay registered by the shared test fixture.
    with ExitStack() as stack:
        for _ in range(10):
            stack.enter_context(client.websocket_connect("/ws?key=overlay-key-test-0123456789"))
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws?key=overlay-key-test-0123456789") as ws:
                ws.receive_text()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?key=wrong") as ws:
            ws.receive_text()


# ───── антибот ─────
def test_honeypot(client):
    r = client.post("/api/donations", headers={"Idempotency-Key": KEY},
                    json={"method": "yookassa", "amount": 300, "website": "http://spam.example"})
    assert r.status_code == 400 and pay.recent(5) == []


def test_turnstile(client, fake, monkeypatch):
    monkeypatch.setattr(sec, "TURNSTILE_SECRET", "ts-secret")
    verdict = {"ok": True}
    route_through(monkeypatch, fake, lambda r: httpx.Response(200, json={"success": verdict["ok"]}) if "turnstile" in str(r.url) else None)

    def post(token=None, key=KEY):
        body = {"method": "yookassa", "amount": 300}
        if token:
            body["turnstile_token"] = token
        return client.post("/api/donations", headers={"Idempotency-Key": key}, json=body)

    assert post().status_code == 400                      # нет токена
    verdict["ok"] = False
    assert post("tok").status_code == 400                 # Cloudflare не подтвердил
    verdict["ok"] = True
    first = post("tok")
    assert first.status_code == 200
    verdict["ok"] = False                                 # токен одноразовый, но повтор того же запроса проходит без него
    assert post().json()["donation_id"] == first.json()["donation_id"]


# ───── вход данных: XSS, SQL, утечки ─────
def test_xss_and_sql_injection_payloads_are_inert(client):
    assert pay.sanitize_text("<img src=x onerror=alert(1)>Привет<script>x</script>", 50) == "Приветx"
    evil = "x'); DROP TABLE donations;--"
    did = donate(client, name=evil).json()["donation_id"]
    assert pay.get_row(did)["donor_name"] == evil and len(pay.recent(10)) == 1  # записано как текст, таблица цела


def test_provider_errors_are_not_exposed(client, fake, monkeypatch):
    route_through(monkeypatch, fake, lambda r: httpx.Response(401, json={"description": "Invalid credentials shop_id=shop1"})
                  if r.method == "POST" and "yookassa" in str(r.url) else None)
    r = donate(client)
    assert r.status_code == 502 and r.json()["detail"] == pay.GENERIC_FAIL and "shop" not in r.text


def test_cryptopay_link_must_be_telegram(client, fake, monkeypatch):
    route_through(monkeypatch, fake, lambda r: httpx.Response(200, json={"ok": True, "result": {
        "invoice_id": 9, "bot_invoice_url": "https://evil.example/phish"}}) if "createInvoice" in str(r.url) else None)
    assert donate(client, "cryptopay").status_code == 502


def test_no_personal_data_in_logs(client, fake, caplog):
    caplog.set_level(logging.DEBUG)
    donate(client, "nowpayments", name="СекретноеИмя", message="СекретноеСообщение")
    fake.np_status = "finished"
    import asyncio
    asyncio.run(pay.poll_once(0))
    donate(client, key="z" * 24, name="СекретноеИмя", message="СекретноеСообщение")
    fake.yk_status, fake.yk_amount = "succeeded", "1.00"   # провоцируем ошибочную ветку
    client.post("/api/webhooks/yookassa", json={"event": "payment.succeeded", "object": {"id": "yk-1"}})
    assert "Секретное" not in caplog.text and len(fake.alerts) == 1


# ───── настройки и файлы ─────
def test_config_validation(client):
    cfg = client.get("/api/admin/config", auth=AUTH).json()
    assert client.put("/api/admin/config", auth=AUTH, headers=H, json=cfg).status_code == 200
    for patch in ({"tts_voice": "../../etc/passwd"}, {"title_template": "x" * 500},
                  {"tiers": [cfg["tiers"][0]] * 21}, {"accepted_assets": "<b>"}, {"text_color": "red"}):
        assert client.put("/api/admin/config", auth=AUTH, headers=H, json={**cfg, **patch}).status_code == 422, patch


def test_upload_checks_content_and_serves_sandboxed(client):
    up = lambda name, data: client.post("/api/admin/upload", auth=AUTH, headers=H, files={"file": (name, data, "x/y")})  # noqa: E731
    assert up("x.svg", b"<svg onload=alert(1)>").status_code == 400
    assert up("evil.gif", b"<html><script>alert(1)</script>").status_code == 400   # внутри не гиф
    ok = up("my pic.gif", GIF)
    assert ok.status_code == 200
    r = client.get("/media/" + ok.json()["name"])
    assert r.status_code == 200 and "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/media/..%2Fserver.py").status_code in (400, 404)


# ───── Sentry ─────
def test_sentry_event_is_scrubbed():
    event = {
        "request": {"method": "POST", "url": "https://x.test/overlay?key=SECRET", "data": {"name": "Вася", "message": "Привет"},
                    "headers": {"Authorization": "Basic abc", "x-nowpayments-sig": "sig"}, "query_string": "key=SECRET"},
        "user": {"ip_address": "1.2.3.4"}, "server_name": "host", "breadcrumbs": {"values": [{"message": "Вася"}]},
        "exception": {"values": [{"stacktrace": {"frames": [{"function": "f", "vars": {"message": "Привет"}}]}}]},
    }
    out = sec.scrub_sentry_event(event)
    dumped = json.dumps(out, ensure_ascii=False)
    for secret in ("SECRET", "Вася", "Привет", "Basic", "1.2.3.4", "sig"):
        assert secret not in dumped, secret
    assert out["request"] == {"method": "POST", "url": "https://x.test/overlay"}


def test_sentry_is_off_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert sec.init_sentry() is False



def test_shared_brand_theme_is_available_and_allowed_by_csp(client):
    theme = client.get("/theme.css")
    assert theme.status_code == 200
    assert "color-scheme: dark" in theme.text
    assert "--bg: #080B12" in theme.text
    assert "--accent: #A5B4FC" in theme.text
    assert "--glow-violet:" in theme.text

    for path, auth in [("/", None), ("/admin", AUTH), ("/thanks", None), ("/overlay", None)]:
        response = client.get(path, auth=auth)
        csp = response.headers["content-security-policy"]
        assert "style-src 'self'" in csp
        assert 'href="/theme.css"' in response.text

def test_donation_page_uses_short_payment_heading(client):
    for path in ("/", "/?donation=" + "a" * 32):
        response = client.get(path)
        assert response.status_code == 200
        assert '<p class="sub">Поддержать меня^^</p>' in response.text


def test_donation_amount_field_hides_native_number_spinner(client):
    response = client.get("/")
    assert response.status_code == 200
    assert '<input id="amount" type="text" inputmode="decimal"' in response.text
    assert ".amount input{" in response.text
    assert "background:transparent!important" in response.text
    assert "border:0!important" in response.text
    assert "font-size:clamp(36px,9vw,48px)" in response.text
    assert "line-height:1.1" in response.text
    assert response.text.index(".amount input#amount{") > response.text.index("input[type=text],input[type=number],textarea{")
    assert "font-family:\"Unbounded\",sans-serif;font-size:clamp(36px,9vw,48px)" in response.text
