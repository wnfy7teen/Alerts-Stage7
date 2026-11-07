import asyncio
import hashlib
import hmac
import json
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


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"
