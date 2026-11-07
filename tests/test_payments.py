import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing

import pytest

import payments as pay
from helpers import KEY, donate  # noqa: F401


# ───── unit: валидация ─────
def test_validate_amount():
    assert pay.validate_amount("300.456", 50, 1000) == 300.46
    for bad in ("abc", None, float("nan"), float("inf"), 10, 5000):
        with pytest.raises(pay.PaymentError):
            pay.validate_amount(bad, 50, 1000)


def test_sanitize_text():
    assert pay.sanitize_text("<script>alert(1)</script>Привет\n\nмир\x00", 50) == "alert(1)Привет мир"
    assert len(pay.sanitize_text("a" * 900, 500)) == 500


def test_rate_limiter_prunes_expired_unique_keys(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(pay.time, "monotonic", lambda: clock[0])
    limiter = pay.RateLimiter(5, 60)

    for i in range(10_001):
        assert limiter.allow(f"ip-{i}")

    assert len(limiter._hits) == 10_001
    clock[0] = 61.0
    assert limiter.allow("fresh-ip")
    assert list(limiter._hits) == ["fresh-ip"]


# ───── создание платежа ─────
def test_yookassa_widget_checkout_and_idempotency(client, fake):
    r = donate(client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["checkout"]["type"] == "widget" and d["checkout"]["confirmation_token"] == "ct-1"
    assert d["checkout"]["return_url"] == f"https://example.test/?donation={d['donation_id']}"
    again = donate(client)  # двойной клик с тем же ключом
    assert again.json()["donation_id"] == d["donation_id"] and len(fake.yk_created) == 1
    assert donate(client, amount=999).status_code == 409  # тот же ключ, другие параметры


def test_idempotency_key_rejects_changed_donor_payload(client):
    original = donate(client, name="Вася", message="Привет")
    assert original.status_code == 200
    assert donate(client, name="Другой", message="Привет").status_code == 409
    assert donate(client, name="Вася", message="Другое сообщение").status_code == 409


def test_idempotency_key_required(client):
    assert client.post("/api/donations", json={"method": "yookassa", "amount": 300}).status_code == 400


def test_amount_limits_and_unknown_method(client):
    assert donate(client, amount=10).status_code == 400
    assert donate(client, amount=10**7 - 1, key="x" * 24).status_code == 400
    assert client.post("/api/donations", headers={"Idempotency-Key": KEY},
                       json={"method": "paypal", "amount": 300}).status_code == 422


def test_rate_limit_5_per_window(client):
    codes = [donate(client, amount=10, key=f"key-{i:020d}").status_code for i in range(6)]
    assert codes[:5] == [400] * 5 and codes[5] == 429  # считаются все попытки, даже неудачные


def test_duplicate_provider_txn_is_rejected_gracefully(client):
    assert donate(client, "nowpayments", key="a" * 24).status_code == 200
    r = donate(client, "nowpayments", key="b" * 24)  # мок отдаёт тот же payment_id 777
    assert r.status_code == 502


def test_crypto_checkout_has_address_and_qr(client):
    d = donate(client, "nowpayments").json()["checkout"]
    assert d["type"] == "crypto_address" and d["address"].startswith("TXyz")
    assert d["currency"] == "USDTTRC20" and d["qr"].startswith("data:image/svg+xml")


def test_cryptopay_checkout_is_explicit_link(client):
    d = donate(client, "cryptopay").json()["checkout"]
    assert d["type"] == "link" and d["url"].startswith("https://t.me/")


def test_status_endpoint_hides_personal_data(client):
    did = donate(client, name="Секрет", message="Тайна").json()["donation_id"]
    st = client.get(f"/api/payments/status/{did}").json()
    assert st["status"] == "pending" and "Секрет" not in json.dumps(st, ensure_ascii=False)
    assert client.get("/api/payments/status/not-an-id").status_code == 404


# ───── webhook-и: зачисление ровно один раз ─────
def test_yookassa_webhook_credits_once(client, fake):
    did = donate(client).json()["donation_id"]
    fake.yk_status = "succeeded"
    hook = {"event": "payment.succeeded", "object": {"id": "yk-1"}}
    assert client.post("/api/webhooks/yookassa", json=hook).status_code == 200
    assert client.post("/api/webhooks/yookassa", json=hook).status_code == 200
    assert len(fake.alerts) == 1 and fake.alerts[0]["title"].startswith("Вася")
    assert client.get(f"/api/payments/status/{did}").json()["status"] == "paid"


def test_yookassa_partial_refund_webhook_is_verified_and_idempotent(client, fake):
    did = donate(client, key="y" * 24).json()["donation_id"]
    fake.yk_status = "succeeded"
    payment_hook = {"event": "payment.succeeded", "object": {"id": "yk-1"}}
    assert client.post("/api/webhooks/yookassa", json=payment_hook).status_code == 200

    fake.yk_refund_data = {
        "id": "rf-1", "status": "succeeded", "payment_id": "yk-1",
        "amount": {"value": "100.00", "currency": "RUB"},
    }
    refund_hook = {"event": "refund.succeeded", "object": {"id": "rf-1"}}
    assert client.post("/api/webhooks/yookassa", json=refund_hook).status_code == 200
    assert client.post("/api/webhooks/yookassa", json=refund_hook).status_code == 200

    row = pay.get_row(did)
    assert row["status"] == "partially_refunded"
    journal = pay.list_transactions(status="partially_refunded")
    assert journal["total"] == 1
    assert journal["items"][0]["refund_count"] == 1
    assert journal["items"][0]["refunded_amount"] == 100.0


def test_nowpayments_refunded_is_not_recorded_as_failed(client, fake):
    did = donate(client, "nowpayments", key="n" * 24).json()["donation_id"]
    fake.np_status = "finished"
    assert asyncio.run(pay.refresh(pay.get_row(did))) == "paid"

    fake.np_status = "refunded"
    assert asyncio.run(pay.refresh(pay.get_row(did))) == "refunded"
    row = pay.get_row(did)
    assert row["status"] == "refunded"
    journal = pay.list_transactions(status="refunded")
    assert journal["total"] == 1
    assert journal["items"][0]["refund_count"] == 1
    assert journal["items"][0]["refunded_amount"] is None


def test_yookassa_forged_webhook_does_not_credit(client, fake):
    donate(client)  # платёж не оплачен: ЮKassa отвечает pending
    client.post("/api/webhooks/yookassa", json={"event": "payment.succeeded", "object": {"id": "yk-1"}})
    client.post("/api/webhooks/yookassa", json={"event": "payment.succeeded", "object": {"id": "unknown"}})
    assert fake.alerts == []


def test_yookassa_amount_mismatch_is_not_credited(client, fake):
    donate(client)
    fake.yk_status, fake.yk_amount = "succeeded", "1.00"
    client.post("/api/webhooks/yookassa", json={"event": "payment.succeeded", "object": {"id": "yk-1"}})
    assert fake.alerts == []



def test_nowpayments_one_cent_amount_mismatch_is_not_credited(client, fake):
    donate(client, "nowpayments")
    fake.np_status, fake.np_price = "finished", "299.99"
    asyncio.run(pay.poll_once(0))
    assert fake.alerts == []


def test_cryptopay_one_cent_amount_mismatch_is_not_credited(client, fake):
    donate(client, "cryptopay")
    fake.cp_status, fake.cp_amount = "paid", "299.99"
    raw = json.dumps({"update_type": "invoice_paid", "payload": {"invoice_id": 555}}).encode()
    good = hmac.new(hashlib.sha256(b"cp-token").digest(), raw, hashlib.sha256).hexdigest()
    client.post("/api/webhooks/cryptopay", content=raw, headers={"crypto-pay-api-signature": good})
    assert fake.alerts == []


def test_amount_matches_uses_cent_precision():
    assert pay._amount_matches("300.004", 300)
    assert pay._amount_matches("300.00", 300)
    assert not pay._amount_matches("299.99", 300)
    assert not pay._amount_matches("invalid", 300)
    assert not pay._amount_matches("NaN", 300)


def test_yookassa_currency_mismatch_is_not_credited(client, fake):
    donate(client)
    fake.yk_status, fake.yk_currency = "succeeded", "USD"
    client.post("/api/webhooks/yookassa", json={"event": "payment.succeeded", "object": {"id": "yk-1"}})
    assert fake.alerts == []


def test_nowpayments_currency_mismatch_is_not_credited(client, fake):
    donate(client, "nowpayments")
    fake.np_status, fake.np_price_currency = "finished", "usd"
    asyncio.run(pay.poll_once(0))
    assert fake.alerts == []


def test_cryptopay_currency_mismatch_is_not_credited(client, fake):
    donate(client, "cryptopay")
    fake.cp_status, fake.cp_fiat = "paid", "USD"
    raw = json.dumps({"update_type": "invoice_paid", "payload": {"invoice_id": 555}}).encode()
    good = hmac.new(hashlib.sha256(b"cp-token").digest(), raw, hashlib.sha256).hexdigest()
    client.post("/api/webhooks/cryptopay", content=raw, headers={"crypto-pay-api-signature": good})
    assert fake.alerts == []


def test_nowpayments_ipn_signature_and_credit(client, fake):
    did = donate(client, "nowpayments").json()["donation_id"]
    fake.np_status = "finished"
    payload = {"payment_id": 777, "payment_status": "finished", "fee": {"z": 1, "a": 2}, "order_id": did}
    raw = json.dumps(payload).encode()
    assert client.post("/api/webhooks/nowpayments", content=raw, headers={"x-nowpayments-sig": "bad"}).status_code == 403
    assert fake.alerts == []
    sig = pay.np_signature(payload, "np-ipn-secret")
    for _ in range(2):
        assert client.post("/api/webhooks/nowpayments", content=raw, headers={"x-nowpayments-sig": sig}).status_code == 200
    assert len(fake.alerts) == 1


def test_cryptopay_webhook_rejects_non_object_payload(client):
    for payload in ([], {"update_type": "invoice_paid", "payload": []}):
        raw = json.dumps(payload).encode()
        good = hmac.new(hashlib.sha256(b"cp-token").digest(), raw, hashlib.sha256).hexdigest()
        assert client.post("/api/webhooks/cryptopay", content=raw,
                           headers={"crypto-pay-api-signature": good}).status_code == 400


def test_cryptopay_webhook(client, fake):
    donate(client, "cryptopay")
    fake.cp_status = "paid"
    raw = json.dumps({"update_type": "invoice_paid", "payload": {"invoice_id": 555}}).encode()
    good = hmac.new(hashlib.sha256(b"cp-token").digest(), raw, hashlib.sha256).hexdigest()
    assert client.post("/api/webhooks/cryptopay", content=raw, headers={"crypto-pay-api-signature": "0" * 64}).status_code == 403
    assert client.post("/api/webhooks/cryptopay", content=raw, headers={"crypto-pay-api-signature": good}).status_code == 200
    assert len(fake.alerts) == 1


def test_poller_credits_without_webhook(client, fake):
    donate(client, "nowpayments")
    fake.np_status = "finished"
    asyncio.run(pay.poll_once(0))
    asyncio.run(pay.poll_once(0))
    assert len(fake.alerts) == 1


def test_old_provider_pending_payment_is_reconciled_instead_of_locally_expired(client, fake):
    response = donate(client, "nowpayments")
    donation_id = response.json()["donation_id"]
    with closing(pay.db()) as db:
        db.execute("UPDATE donations SET created_at=? WHERE id=?", (pay.time.time() - pay.PENDING_TTL - 60, donation_id))
        db.commit()

    fake.np_status = "finished"
    asyncio.run(pay.poll_once(0))

    row = pay.get_row(donation_id)
    assert row["status"] == "paid"
    assert row["paid_at"] is not None
    assert len(fake.alerts) == 1


def test_prune_delivered_alerts_preserves_pending_and_respects_batch(client, fake):
    first, second, third = "1" * 32, "2" * 32, "3" * 32
    now = pay.time.time()
    with closing(pay.db()) as db:
        db.executemany(
            "INSERT INTO donations(id,amount,currency,status,donor_name,message,provider,created_at) "
            "VALUES(?,100,'RUB','paid','test','test','yookassa',?)",
            [(first, now - 100000), (second, now - 100000), (third, now - 100000)],
        )
        db.executemany(
            "INSERT INTO alert_outbox(donation_id,created_at,attempts,next_attempt_at,delivered_at) "
            "VALUES(?,?,?,?,?)",
            [
                (first, now - 100000, 1, now, now - 90000),
                (second, now - 100000, 1, now, now - 80000),
                (third, now - 100000, 1, now, None),
            ],
        )
        db.commit()

    assert pay.prune_delivered_alerts(retention_seconds=3600, limit=1) == 1
    with closing(pay.db()) as db:
        rows = db.execute(
            "SELECT donation_id, delivered_at FROM alert_outbox ORDER BY donation_id"
        ).fetchall()
    remaining = {row["donation_id"]: row["delivered_at"] for row in rows}
    assert first not in remaining
    assert second in remaining and remaining[second] is not None
    assert third in remaining and remaining[third] is None


def test_outbox_survives_restart_and_recovers_expired_lease(client, fake):
    donation_id = donate(client).json()["donation_id"]
    now = pay.time.time()
    with closing(pay.db()) as db:
        db.execute(
            "INSERT INTO alert_outbox(donation_id,created_at,attempts,next_attempt_at,claimed_until,last_error) "
            "VALUES(?,?,?,?,?,?)",
            (donation_id, now - 180, 2, now - 1, now - 1, "handler_error"),
        )
        db.commit()

    # Startup initialization must not discard pending durable delivery records.
    pay.init_db()
    with closing(pay.db()) as db:
        persisted = db.execute(
            "SELECT * FROM alert_outbox WHERE donation_id=?", (donation_id,)
        ).fetchone()
    assert persisted is not None
    assert persisted["delivered_at"] is None
    assert persisted["attempts"] == 2

    # The next poll after a process restart can reclaim an expired lease.
    asyncio.run(pay.poll_once(0))
    with closing(pay.db()) as db:
        recovered = db.execute(
            "SELECT * FROM alert_outbox WHERE donation_id=?", (donation_id,)
        ).fetchone()
    assert recovered["delivered_at"] is not None
    assert recovered["claimed_until"] is None
    assert recovered["last_error"] is None
    assert recovered["attempts"] == 3
    assert [alert["id"] for alert in fake.alerts].count(donation_id) == 1


def test_paid_alert_waits_for_overlay_and_retries(client, fake, monkeypatch):
    import server

    donation_id = donate(client).json()["donation_id"]
    fake.yk_status = "succeeded"

    async def offline(_alert):
        return False

    monkeypatch.setattr(server, "broadcast", offline)
    hook = {"event": "payment.succeeded", "object": {"id": "yk-1"}}
    assert client.post("/api/webhooks/yookassa", json=hook).status_code == 200
    with closing(pay.db()) as db:
        pending = db.execute("SELECT * FROM alert_outbox WHERE donation_id=?", (donation_id,)).fetchone()
        assert pending["delivered_at"] is None and pending["attempts"] == 1
        db.execute("UPDATE alert_outbox SET next_attempt_at=0 WHERE donation_id=?", (donation_id,))
        db.commit()

    async def online(alert):
        fake.alerts.append(alert)
        return True

    monkeypatch.setattr(server, "broadcast", online)
    assert asyncio.run(pay.dispatch_pending_alerts()) == 1
    with closing(pay.db()) as db:
        delivered = db.execute("SELECT * FROM alert_outbox WHERE donation_id=?", (donation_id,)).fetchone()
        assert delivered["delivered_at"] is not None and delivered["last_error"] is None
    assert fake.alerts[0]["id"] == donation_id
    assert asyncio.run(pay.dispatch_pending_alerts()) == 0


# ───── БД ─────
def test_unique_provider_txn_index(client):
    donate(client, "nowpayments")
    with closing(pay.db()) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO donations(id,amount,currency,status,donor_name,message,provider,provider_txn_id,created_at)"
                   " VALUES('dup',1,'RUB','pending','a','b','nowpayments','777',0)")


def test_legacy_schema_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(pay, "DB_PATH", tmp_path / "old.db")
    with closing(sqlite3.connect(pay.DB_PATH)) as c:
        c.execute("CREATE TABLE donations(invoice_id INTEGER PRIMARY KEY,name TEXT,message TEXT,amount REAL,fiat TEXT,"
                  "status TEXT,created REAL,paid_asset TEXT,paid_amount TEXT)")
        c.execute("INSERT INTO donations VALUES(42,'Аня','hi',300,'RUB','paid',1000,NULL,NULL)")
        c.commit()
    pay.init_db()
    row = pay.find_by_txn("cryptopay", "42")
    assert row["donor_name"] == "Аня" and row["status"] == "paid" and row["currency"] == "RUB"


def test_refresh_ignores_malformed_provider_response(client, monkeypatch):
    donation_id = donate(client).json()["donation_id"]
    row = pay.get_row(donation_id)

    async def malformed(_row):
        raise AttributeError("provider returned a list instead of an object")

    monkeypatch.setattr(pay, "_yk_verdict", malformed)
    assert asyncio.run(pay.refresh(row)) == "pending"
    assert pay.get_row(donation_id)["status"] == "pending"


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_admin_transaction_journal_filters_and_paginates(client, fake):
    donate(client, key="a" * 24, name="Alpha")
    donate(client, key="b" * 24, name="Beta")
    with closing(pay.db()) as db:
        db.execute("UPDATE donations SET status='paid' WHERE donor_name='Alpha'")
        db.commit()

    first = client.get("/api/admin/transactions?page=1&limit=1", auth=("admin", "pw-test-1234567"))
    second = client.get("/api/admin/transactions?page=2&limit=1", auth=("admin", "pw-test-1234567"))
    filtered = client.get(
        "/api/admin/transactions?status=paid&q=Alpha",
        auth=("admin", "pw-test-1234567"),
    )

    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert len(first.json()["items"]) == 1
    assert second.json()["items"][0]["donor_name"] != first.json()["items"][0]["donor_name"]
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["donor_name"] == "Alpha"


def test_admin_transaction_csv_neutralizes_spreadsheet_formulas(client, fake):
    donate(client, key="c" * 24, name='=HYPERLINK("https://example.com","click")')
    response = client.get("/api/admin/transactions.csv", auth=("admin", "pw-test-1234567"))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "'=HYPERLINK" in response.text




def test_paid_handler_skips_alert_generation_when_overlay_is_offline(monkeypatch):
    import server

    monkeypatch.setattr(server, "clients", set())

    async def unexpected_alert(*args, **kwargs):
        raise AssertionError("alert generation must be skipped while OBS is offline")

    monkeypatch.setattr(server, "make_alert", unexpected_alert)
    row = {"donor_name": "Test", "message": "Test", "amount": 100, "currency": "RUB", "id": "a" * 32}
    assert asyncio.run(server.on_paid(row)) is False



def test_prune_tts_files_bounds_scan_work(tmp_path, monkeypatch):
    import server

    files = []
    for index in range(6):
        path = tmp_path / f"recent-{index}.mp3"
        path.write_bytes(b"test")
        os.utime(path, (900, 900))
        files.append(path)
    old_audio = tmp_path / "old.mp3"
    old_audio.write_bytes(b"test")
    os.utime(old_audio, (100, 100))
    files.append(old_audio)

    monkeypatch.setattr(server, "TTS_DIR", tmp_path)
    monkeypatch.setattr(server.time, "time", lambda: 1000)
    monkeypatch.setattr(server.Path, "glob", lambda self, pattern: iter(files))

    assert server.prune_tts_files(max_age_seconds=300, scan_limit=5) == 0
    assert old_audio.exists()


def test_prune_tts_files_removes_only_expired_mp3(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "TTS_DIR", tmp_path)
    old_audio = tmp_path / "old.mp3"
    recent_audio = tmp_path / "recent.mp3"
    unrelated_file = tmp_path / "keep.wav"
    for path in (old_audio, recent_audio, unrelated_file):
        path.write_bytes(b"test")
    os.utime(old_audio, (100, 100))
    os.utime(recent_audio, (900, 900))
    monkeypatch.setattr(server.time, "time", lambda: 1000)

    assert server.prune_tts_files(max_age_seconds=300) == 1
    assert not old_audio.exists()
    assert recent_audio.exists()
    assert unrelated_file.exists()


def test_admin_replay_reports_offline_overlay(client, monkeypatch):
    import server

    donation_id = donate(client).json()["donation_id"]
    with closing(pay.db()) as db:
        db.execute("UPDATE donations SET status='paid' WHERE id=?", (donation_id,))
        db.commit()

    async def offline(_alert):
        return False

    monkeypatch.setattr(server, "broadcast", offline)
    response = client.post(
        f"/api/admin/replay/{donation_id}",
        auth=("admin", "pw-test-1234567"), headers={"X-Requested-With": "donation-admin"}
    )
    assert response.status_code == 409
    assert "Оверлей не подключён" in response.json()["detail"]
