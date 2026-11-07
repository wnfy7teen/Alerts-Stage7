"""
Платежи: БД, идемпотентность, провайдеры, webhook-и и опрос статусов.

Принцип: webhook и опрос — только «триггеры». Решение «оплачено» принимается по ответу
API провайдера (источник истины), а не по телу входящего запроса. Донат засчитывается
ровно один раз: атомарный UPDATE ... WHERE status != 'paid' + уникальный индекс
по (provider, provider_txn_id).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import sqlite3
import time
import uuid
from urllib.parse import urlsplit
from collections import defaultdict, deque
from contextlib import closing
from pathlib import Path
from typing import Awaitable, Callable

import httpx
import segno
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("payments")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


DB_PATH = Path(_env("DB_PATH") or Path(__file__).parent / "donations.db")
PUBLIC_URL = _env("PUBLIC_URL").rstrip("/")

CRYPTOPAY_TOKEN = _env("CRYPTO_PAY_TOKEN")
CRYPTOPAY_API = (
    "https://testnet-pay.crypt.bot/api" if _env("TESTNET", "0") == "1" else "https://pay.crypt.bot/api"
)

YK_SHOP_ID = _env("YOOKASSA_SHOP_ID")
YK_SECRET = _env("YOOKASSA_SECRET_KEY")
YK_API = "https://api.yookassa.ru/v3"
# Необязательно: список IP/подсетей ЮKassa через запятую (берите из документации ЮKassa).
YK_WEBHOOK_IPS = [x.strip() for x in _env("YOOKASSA_WEBHOOK_IPS").split(",") if x.strip()]

NP_API_KEY = _env("NOWPAYMENTS_API_KEY")
NP_IPN_SECRET = _env("NOWPAYMENTS_IPN_SECRET")
NP_API = "https://api-sandbox.nowpayments.io/v1" if _env("NOWPAYMENTS_SANDBOX", "0") == "1" else "https://api.nowpayments.io/v1"
NP_PAY_CURRENCY = _env("NOWPAYMENTS_PAY_CURRENCY", "usdttrc20").lower()

HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None  # подменяется в тестах

PENDING_TTL = 3 * 3600  # через сколько неоплаченный донат считаем просроченным


GENERIC_FAIL = "Не удалось создать платёж, попробуйте позже"


def provider_fail(provider: str, detail) -> "PaymentError":
    """Подробности ошибки провайдера уходят в лог, пользователю — общий текст (внутренности не раскрываем)."""
    log.error("%s: %s", provider, str(detail)[:300])
    return PaymentError(GENERIC_FAIL, 502)


class PaymentError(Exception):
    """Ошибка, текст которой безопасно показать пользователю."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ───────────────────────── валидация ─────────────────────────
def _num(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def validate_amount(value, min_amount: float, max_amount: float, symbol: str = "₽") -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise PaymentError("Введите сумму числом") from None
    if not math.isfinite(amount):
        raise PaymentError("Введите сумму числом")
    amount = round(amount, 2)
    if amount < min_amount:
        raise PaymentError(f"Минимальная сумма — {_num(min_amount)} {symbol}")
    if amount > max_amount:
        raise PaymentError(f"Максимальная сумма — {_num(max_amount)} {symbol}")
    return amount


def sanitize_text(value: str, limit: int) -> str:
    """Убирает HTML-теги и управляющие символы. Вывод на страницах всё равно идёт через textContent."""
    value = re.sub(r"<[^>]*>", "", value or "")
    value = "".join(" " if ch in "\r\n\t" else ch for ch in value if ch >= " " and ch != "\x7f" or ch in "\r\n\t")
    value = re.sub(r" {2,}", " ", value)
    return value.strip()[:limit]


class RateLimiter:
    """Скользящее окно в памяти процесса: не более `limit` событий за `window` секунд на ключ."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self._hits) > 10_000:
            self._prune(now)
        return True

    def _prune(self, now: float) -> None:
        """Удаляет ключи, у которых полностью истекло скользящее окно."""
        for key, hits in list(self._hits.items()):
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if not hits:
                del self._hits[key]


# ───────────────────────── база ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS donations(
    id TEXT PRIMARY KEY,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,              -- created | pending | paid | canceled | failed | expired
    donor_name TEXT NOT NULL,
    message TEXT NOT NULL,
    provider TEXT NOT NULL,            -- yookassa | nowpayments | cryptopay
    provider_txn_id TEXT,
    idem_key TEXT UNIQUE,
    checkout TEXT,                     -- JSON для повторной выдачи при двойном клике
    created_at REAL NOT NULL,
    paid_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_donations_provider_txn
    ON donations(provider, provider_txn_id) WHERE provider_txn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_donations_status ON donations(status, created_at);
"""


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db() -> None:
    with closing(db()) as c:
        cols = [r["name"] for r in c.execute("PRAGMA table_info(donations)")]
        legacy = "invoice_id" in cols
        if legacy:  # старая схема (до этапа 1): переносим данные
            c.execute("ALTER TABLE donations RENAME TO donations_legacy")
            c.commit()
        c.executescript(SCHEMA)
        if legacy:
            for r in c.execute("SELECT * FROM donations_legacy").fetchall():
                status = r["status"] if r["status"] in ("paid", "pending", "expired") else "pending"
                c.execute(
                    "INSERT OR IGNORE INTO donations(id,amount,currency,status,donor_name,message,provider,"
                    "provider_txn_id,created_at,paid_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, r["amount"], r["fiat"], status, r["name"], r["message"], "cryptopay",
                     str(r["invoice_id"]), r["created"], r["created"] if status == "paid" else None),
                )
            c.commit()
            log.info("Данные старой таблицы donations перенесены в новую схему (копия: donations_legacy)")


def get_row(donation_id: str) -> sqlite3.Row | None:
    with closing(db()) as c:
        return c.execute("SELECT * FROM donations WHERE id=?", (donation_id,)).fetchone()


def get_by_idem(idem_key: str) -> sqlite3.Row | None:
    with closing(db()) as c:
        return c.execute("SELECT * FROM donations WHERE idem_key=?", (idem_key,)).fetchone()


def find_by_txn(provider: str, txn: str) -> sqlite3.Row | None:
    with closing(db()) as c:
        return c.execute(
            "SELECT * FROM donations WHERE provider=? AND provider_txn_id=?", (provider, txn)
        ).fetchone()


def recent(limit: int = 30) -> list[dict]:
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM donations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def public_status(donation_id: str) -> dict | None:
    """Только то, что можно отдавать анониму: без имени и сообщения."""
    if not re.fullmatch(r"[0-9a-f]{32}", donation_id or ""):
        return None
    row = get_row(donation_id)
    if not row:
        return None
    return {"id": row["id"], "status": row["status"], "amount": row["amount"],
            "currency": row["currency"], "provider": row["provider"]}


# ───────────────────────── доступные способы оплаты ─────────────────────────
def enabled_methods() -> list[dict]:
    methods = []
    if YK_SHOP_ID and YK_SECRET:
        methods.append({"id": "yookassa", "label": "Карта или СБП", "hint": "Оплата прямо на сайте"})
    if NP_API_KEY:
        methods.append({"id": "nowpayments", "label": "Криптовалюта", "hint": "Адрес и QR на сайте"})
    if CRYPTOPAY_TOKEN:
        methods.append({"id": "cryptopay", "label": "Crypto Bot", "hint": "Оплата откроется в Telegram"})
    return methods


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, transport=HTTP_TRANSPORT)


def _qr_data_uri(text: str) -> str:
    return segno.make(text, error="m").svg_data_uri(scale=6, border=2)


# ───────────────────────── Crypto Pay (Telegram) ─────────────────────────
async def _cp_call(method: str, params: dict | None = None, post: bool = False) -> dict:
    headers = {"Crypto-Pay-API-Token": CRYPTOPAY_TOKEN}
    async with _client() as c:
        if post:
            r = await c.post(f"{CRYPTOPAY_API}/{method}", headers=headers, json=params or {})
        else:
            r = await c.get(f"{CRYPTOPAY_API}/{method}", headers=headers, params=params or {})
    data = r.json()
    if not data.get("ok"):
        raise provider_fail("Crypto Pay " + method, data.get("error"))
    return data


async def _cp_create(donation_id: str, amount: float, currency: str, assets: str) -> tuple[str, dict]:
    params: dict = {
        "currency_type": "fiat", "fiat": currency, "amount": f"{amount:.2f}",
        "description": "Донат стримеру", "allow_comments": False, "expires_in": 3600,
    }
    if assets.strip():
        params["accepted_assets"] = assets.replace(" ", "")
    if PUBLIC_URL.startswith("https://"):  # адрес только из настроек сервера, не от клиента
        params.update(paid_btn_name="callback", paid_btn_url=f"{PUBLIC_URL}/?donation={donation_id}")
    inv = (await _cp_call("createInvoice", params, post=True))["result"]
    url = inv.get("bot_invoice_url") or inv.get("pay_url")
    parts = urlsplit(str(url))
    if parts.scheme != "https" or parts.hostname not in ("t.me", "telegram.me"):  # не отдаём клиенту произвольные ссылки
        raise provider_fail("Crypto Pay", "неожиданный адрес счёта")
    return str(inv["invoice_id"]), {"type": "link", "url": url, "label": "Открыть Crypto Bot"}


async def _cp_verdict(row: sqlite3.Row) -> str:
    data = await _cp_call("getInvoices", {"invoice_ids": row["provider_txn_id"]})
    res = data["result"]
    items = res["items"] if isinstance(res, dict) else res
    inv = next((i for i in items if str(i.get("invoice_id")) == row["provider_txn_id"]), None)
    if not inv:
        return "pending"
    if inv.get("status") == "paid":
        if abs(float(inv.get("amount", 0)) - row["amount"]) > 0.01:
            log.error("Crypto Pay: сумма счёта %s не совпадает с донатом %s", inv.get("amount"), row["id"])
            return "pending"
        return "paid"
    return "expired" if inv.get("status") == "expired" else "pending"


# ───────────────────────── ЮKassa (карты, СБП) ─────────────────────────
async def _yk_create(donation_id: str, amount: float, currency: str) -> tuple[str, dict]:
    body = {
        "amount": {"value": f"{amount:.2f}", "currency": currency},
        "capture": True,
        "confirmation": {"type": "embedded"},
        "description": "Донат стримеру",
        "metadata": {"donation_id": donation_id},
    }
    async with _client() as c:
        r = await c.post(f"{YK_API}/payments", json=body, auth=(YK_SHOP_ID, YK_SECRET),
                         headers={"Idempotence-Key": donation_id})
    data = r.json()
    if r.status_code >= 300:
        raise provider_fail("ЮKassa", data.get("description") or r.status_code)
    token = (data.get("confirmation") or {}).get("confirmation_token")
    if not token:
        raise provider_fail("ЮKassa", "нет confirmation_token")
    return data["id"], {
        "type": "widget", "confirmation_token": token,
        "return_url": f"{PUBLIC_URL}/?donation={donation_id}",  # только из настроек сервера
    }


async def _yk_verdict(row: sqlite3.Row) -> str:
    async with _client() as c:
        r = await c.get(f"{YK_API}/payments/{row['provider_txn_id']}", auth=(YK_SHOP_ID, YK_SECRET))
    if r.status_code != 200:
        raise PaymentError("ЮKassa недоступна", 502)
    data = r.json()
    if (data.get("metadata") or {}).get("donation_id") != row["id"]:
        log.error("ЮKassa: metadata.donation_id не совпадает для %s", row["id"])
        return "pending"
    if data.get("status") == "succeeded" and data.get("paid"):
        if (data.get("amount") or {}).get("value") != f"{row['amount']:.2f}":
            log.error("ЮKassa: сумма платежа не совпадает для %s", row["id"])
            return "pending"
        return "paid"
    return "canceled" if data.get("status") == "canceled" else "pending"


# ───────────────────────── NOWPayments (крипта на сайте) ─────────────────────────
async def _np_create(donation_id: str, amount: float, currency: str) -> tuple[str, dict]:
    body = {
        "price_amount": amount, "price_currency": currency.lower(), "pay_currency": NP_PAY_CURRENCY,
        "order_id": donation_id, "order_description": "Донат стримеру",
    }
    if PUBLIC_URL.startswith("https://"):
        body["ipn_callback_url"] = f"{PUBLIC_URL}/api/webhooks/nowpayments"
    async with _client() as c:
        r = await c.post(f"{NP_API}/payment", json=body, headers={"x-api-key": NP_API_KEY})
    data = r.json()
    if r.status_code >= 300:
        msg = str(data.get("message") or "")
        log.error("NOWPayments create: %s %s", r.status_code, msg)
        if "minimal" in msg.lower() or "amount" in msg.lower():
            raise PaymentError("Сумма слишком мала для оплаты криптой, выберите больше", 400)
        raise provider_fail("NOWPayments", msg)
    address = str(data["pay_address"])
    if not re.fullmatch(r"[A-Za-z0-9:_.\-]{10,200}", address):
        raise provider_fail("NOWPayments", "некорректный адрес в ответе")
    return str(data["payment_id"]), {
        "type": "crypto_address", "address": address, "amount": data["pay_amount"],
        "currency": str(data["pay_currency"]).upper(), "network": data.get("network"),
        "memo": data.get("payin_extra_id"), "expires_at": data.get("expiration_estimate_date"),
        "qr": _qr_data_uri(address),
    }


async def _np_verdict(row: sqlite3.Row) -> str:
    async with _client() as c:
        r = await c.get(f"{NP_API}/payment/{row['provider_txn_id']}", headers={"x-api-key": NP_API_KEY})
    if r.status_code != 200:
        raise PaymentError("NOWPayments недоступна", 502)
    data = r.json()
    if str(data.get("order_id")) != row["id"]:
        log.error("NOWPayments: order_id не совпадает для %s", row["id"])
        return "pending"
    st = data.get("payment_status")
    if st == "finished":
        if abs(float(data.get("price_amount", 0)) - row["amount"]) > 0.01:
            log.error("NOWPayments: price_amount не совпадает для %s", row["id"])
            return "pending"
        return "paid"
    if st in ("failed", "refunded"):
        return "failed"
    return "expired" if st == "expired" else "pending"  # partially_paid и прочее ждут


def np_signature(payload: dict, secret: str) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode(), canon.encode(), hashlib.sha512).hexdigest()


# ───────────────────────── применение статуса ─────────────────────────
_paid_handler: Callable[[sqlite3.Row], Awaitable[None]] | None = None


def set_paid_handler(fn: Callable[[sqlite3.Row], Awaitable[None]]) -> None:
    global _paid_handler
    _paid_handler = fn


async def mark_paid(donation_id: str) -> bool:
    """True, если именно этот вызов перевёл донат в paid (значит, алерт нужно показать)."""
    with closing(db()) as c:
        cur = c.execute("UPDATE donations SET status='paid', paid_at=? WHERE id=? AND status!='paid'",
                        (time.time(), donation_id))
        c.commit()
        first = cur.rowcount == 1
        row = c.execute("SELECT * FROM donations WHERE id=?", (donation_id,)).fetchone()
    if first and _paid_handler and row:
        try:
            await _paid_handler(row)
        except Exception:
            log.error("Донат %s оплачен, но алерт не отправился (можно повторить в админке)", donation_id, exc_info=False)
    return first


async def refresh(row: sqlite3.Row) -> str:
    """Спрашивает у провайдера актуальный статус и применяет его."""
    if not row["provider_txn_id"]:
        return row["status"]
    checkers = {"yookassa": _yk_verdict, "nowpayments": _np_verdict, "cryptopay": _cp_verdict}
    try:
        verdict = await checkers[row["provider"]](row)
    except (PaymentError, httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("Не удалось проверить %s/%s: %s", row["provider"], row["id"], e)
        return row["status"]
    if verdict == "paid":
        await mark_paid(row["id"])
    elif verdict in ("canceled", "failed", "expired"):
        with closing(db()) as c:
            c.execute("UPDATE donations SET status=? WHERE id=? AND status IN ('created','pending')",
                      (verdict, row["id"]))
            c.commit()
    return verdict


async def poll_once(tick: int = 0) -> None:
    now = time.time()
    with closing(db()) as c:
        c.execute("UPDATE donations SET status='expired' WHERE status IN ('created','pending') AND created_at<?",
                  (now - PENDING_TTL,))
        c.commit()
        rows = c.execute("SELECT * FROM donations WHERE status='pending' AND provider_txn_id IS NOT NULL").fetchall()
    for row in rows:
        if now - row["created_at"] > 1200 and tick % 12:  # старше 20 минут — раз в минуту
            continue
        await refresh(row)


# ───────────────────────── создание доната ─────────────────────────
def _stored_response(row: sqlite3.Row) -> dict:
    return {"donation_id": row["id"], "status": row["status"], "provider": row["provider"],
            "amount": row["amount"], "currency": row["currency"], "checkout": json.loads(row["checkout"])}


async def create_donation(*, method: str, amount: float, currency: str, name: str, message: str,
                          idem_key: str, accepted_assets: str = "USDT,TON") -> dict:
    if method not in {m["id"] for m in enabled_methods()}:
        raise PaymentError("Этот способ оплаты сейчас недоступен", 400)
    if not PUBLIC_URL.startswith("https://") and method in ("yookassa",):
        raise PaymentError("Оплата не настроена (нужен PUBLIC_URL с https)", 503)

    donation_id = uuid.uuid4().hex
    try:
        with closing(db()) as c:
            c.execute("INSERT INTO donations(id,amount,currency,status,donor_name,message,provider,idem_key,created_at)"
                      " VALUES(?,?,?,'created',?,?,?,?,?)",
                      (donation_id, amount, currency, name, message, method, idem_key, time.time()))
            c.commit()
    except sqlite3.IntegrityError:  # тот же Idempotency-Key: отдаём прежний результат, а не создаём второй платёж
        with closing(db()) as c:
            row = c.execute("SELECT * FROM donations WHERE idem_key=?", (idem_key,)).fetchone()
        if not row or row["provider"] != method or abs(row["amount"] - amount) > 0.001:
            raise PaymentError("Этот ключ уже использован для другого платежа", 409) from None
        if not row["checkout"]:
            raise PaymentError("Платёж ещё создаётся, подождите секунду", 409) from None
        return _stored_response(row)

    try:
        if method == "yookassa":
            txn, checkout = await _yk_create(donation_id, amount, currency)
        elif method == "nowpayments":
            txn, checkout = await _np_create(donation_id, amount, currency)
        else:
            txn, checkout = await _cp_create(donation_id, amount, currency, accepted_assets)
    except Exception as e:
        with closing(db()) as c:
            c.execute("UPDATE donations SET status='failed' WHERE id=?", (donation_id,))
            c.commit()
        if isinstance(e, PaymentError):
            raise
        log.error("Создание платежа %s не удалось: %s", method, type(e).__name__)
        raise PaymentError(GENERIC_FAIL, 502) from None

    try:
        with closing(db()) as c:
            c.execute("UPDATE donations SET provider_txn_id=?, status='pending', checkout=? WHERE id=?",
                      (txn, json.dumps(checkout), donation_id))
            c.commit()
    except sqlite3.IntegrityError:  # провайдер вернул уже известный номер платежа — второй раз не засчитываем
        with closing(db()) as c:
            c.execute("UPDATE donations SET status='failed' WHERE id=?", (donation_id,))
            c.commit()
        log.error("Дубликат provider_txn_id %s у %s", txn, method)
        raise PaymentError("Не удалось создать платёж, попробуйте ещё раз", 502) from None
    saved = get_row(donation_id)
    if saved is None:  # недостижимо при нормальной работе БД
        raise PaymentError(GENERIC_FAIL, 502)
    return _stored_response(saved)


# ───────────────────────── webhook-и ─────────────────────────
def _ip_allowed(ip: str, cidrs: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in ipaddress.ip_network(c, strict=False) for c in cidrs)
    except ValueError:
        return False


async def handle_yookassa_webhook(body: dict, client_ip: str) -> None:
    """У ЮKassa нет подписи: защита — проверка статуса запросом в API + (необязательно) список IP."""
    if YK_WEBHOOK_IPS and not _ip_allowed(client_ip, YK_WEBHOOK_IPS):
        raise PaymentError("forbidden", 403)
    txn = (body.get("object") or {}).get("id")
    row = find_by_txn("yookassa", txn) if isinstance(txn, str) else None
    if row:
        await refresh(row)


async def handle_nowpayments_webhook(raw: bytes, signature: str) -> None:
    if not NP_IPN_SECRET:
        raise PaymentError("forbidden", 403)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise PaymentError("bad request", 400) from None
    if not isinstance(payload, dict):
        raise PaymentError("bad request", 400)
    sig = (signature or "").lower().encode()
    ok = hmac.compare_digest(np_signature(payload, NP_IPN_SECRET).encode(), sig) or hmac.compare_digest(
        hmac.new(NP_IPN_SECRET.encode(), raw, hashlib.sha512).hexdigest().encode(), sig)  # на случай уже отсортированного тела
    if not ok:
        raise PaymentError("forbidden", 403)
    row = find_by_txn("nowpayments", str(payload.get("payment_id", "")))
    if row:
        await refresh(row)


async def handle_cryptopay_webhook(raw: bytes, signature: str) -> None:
    if not CRYPTOPAY_TOKEN:
        raise PaymentError("forbidden", 403)
    secret = hashlib.sha256(CRYPTOPAY_TOKEN.encode()).digest()
    expected = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), (signature or "").lower().encode()):
        raise PaymentError("forbidden", 403)
    try:
        upd = json.loads(raw)
    except ValueError:
        raise PaymentError("bad request", 400) from None
    if upd.get("update_type") == "invoice_paid":
        row = find_by_txn("cryptopay", str((upd.get("payload") or {}).get("invoice_id", "")))
        if row:
            await refresh(row)
