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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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

def _amount_matches(actual, expected) -> bool:
    """Сравнивает фиатные суммы с точностью до копейки без допуска в один цент."""
    try:
        actual_value = Decimal(str(actual))
        expected_value = Decimal(str(expected))
        if not actual_value.is_finite() or not expected_value.is_finite():
            return False
        cent = Decimal("0.01")
        return actual_value.quantize(cent, rounding=ROUND_HALF_UP) == expected_value.quantize(
            cent, rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, TypeError, ValueError):
        return False



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
        self._next_prune_at = 0.0

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if now >= self._next_prune_at:
            self._prune(now)
            self._next_prune_at = now + min(self.window, 60.0)
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
    status TEXT NOT NULL,              -- created | pending | paid | partially_refunded | refunded | canceled | failed | expired
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
CREATE TABLE IF NOT EXISTS refunds(
    id TEXT PRIMARY KEY,
    donation_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_refund_id TEXT NOT NULL,
    amount REAL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(provider, provider_refund_id),
    FOREIGN KEY(donation_id) REFERENCES donations(id)
);
CREATE INDEX IF NOT EXISTS ix_refunds_donation ON refunds(donation_id, status);
CREATE TABLE IF NOT EXISTS alert_outbox(
    donation_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    claimed_until REAL,
    delivered_at REAL,
    last_error TEXT,
    FOREIGN KEY(donation_id) REFERENCES donations(id)
);
CREATE INDEX IF NOT EXISTS ix_alert_outbox_pending
    ON alert_outbox(delivered_at, next_attempt_at, claimed_until);
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


def record_refund(
    *,
    donation_id: str,
    provider: str,
    provider_refund_id: str,
    amount: float | None,
    currency: str,
) -> bool:
    """Сохраняет подтверждённый провайдером возврат идемпотентно."""
    if not provider_refund_id or len(provider_refund_id) > 200:
        return False
    refund_value: float | None = None
    if amount is not None:
        try:
            value = Decimal(str(amount))
            if not value.is_finite() or value <= 0:
                return False
            refund_value = float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, TypeError, ValueError):
            return False

    with closing(db()) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM donations WHERE id=?", (donation_id,)).fetchone()
        if not row or row["provider"] != provider or row["currency"].upper() != currency.upper():
            c.rollback()
            return False
        if row["status"] not in ("created", "pending", "paid", "partially_refunded", "refunded"):
            c.rollback()
            return False
        exists = c.execute(
            "SELECT 1 FROM refunds WHERE provider=? AND provider_refund_id=?",
            (provider, provider_refund_id),
        ).fetchone()
        if exists:
            c.rollback()
            return False
        refunded_before = Decimal(str(c.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM refunds WHERE donation_id=? AND status='succeeded'",
            (donation_id,),
        ).fetchone()[0]))
        if refund_value is not None:
            new_total = refunded_before + Decimal(str(refund_value))
            original = Decimal(str(row["amount"]))
            if new_total - original > Decimal("0.01"):
                log.error("Возврат превышает сумму доната %s", donation_id)
                c.rollback()
                return False
            new_status = "refunded" if new_total >= original - Decimal("0.01") else "partially_refunded"
        else:
            new_status = "refunded"
        c.execute(
            "INSERT INTO refunds(id,donation_id,provider,provider_refund_id,amount,currency,status,created_at) "
            "VALUES(?,?,?,?,?,?, 'succeeded', ?)",
            (uuid.uuid4().hex, donation_id, provider, provider_refund_id, refund_value, currency, time.time()),
        )
        c.execute("UPDATE donations SET status=? WHERE id=?", (new_status, donation_id))
        c.commit()
    return True


def recent(limit: int = 30) -> list[dict]:
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM donations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def list_transactions(*, limit: int = 50, offset: int = 0, status: str = "", provider: str = "", query: str = "") -> dict:
    """Возвращает страницу финансового журнала и общее число совпадений."""
    allowed_statuses = {"created", "pending", "paid", "canceled", "failed", "expired", "refunded"}
    allowed_providers = {"yookassa", "nowpayments", "cryptopay"}
    status_filter = status if status in allowed_statuses else ""
    provider_filter = provider if provider in allowed_providers else ""
    term = query.strip()[:100]
    pattern = f"%{term}%" if term else ""
    params = (
        status_filter, status_filter, provider_filter, provider_filter,
        term, pattern, pattern, pattern, pattern,
    )
    page_size = min(100, max(1, int(limit)))
    page_offset = max(0, int(offset))
    with closing(db()) as c:
        total = c.execute(
            "SELECT COUNT(*) FROM donations "
            "WHERE (?='' OR status=?) AND (?='' OR provider=?) "
            "AND (?='' OR id LIKE ? OR provider_txn_id LIKE ? OR donor_name LIKE ? OR message LIKE ?)",
            params,
        ).fetchone()[0]
        rows = c.execute(
            "SELECT id, amount, currency, status, donor_name, message, provider, provider_txn_id, "
            "created_at, paid_at FROM donations "
            "WHERE (?='' OR status=?) AND (?='' OR provider=?) "
            "AND (?='' OR id LIKE ? OR provider_txn_id LIKE ? OR donor_name LIKE ? OR message LIKE ?) "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, page_size, page_offset),
        ).fetchall()
    return {"total": int(total), "limit": page_size, "offset": page_offset, "items": [dict(r) for r in rows]}


def alert_delivery_stats() -> dict:
    """Сводка очереди без имён, сообщений и идентификаторов донатов."""
    now = time.time()
    with closing(db()) as c:
        row = c.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN delivered_at IS NULL THEN 1 ELSE 0 END) AS pending, "
            "SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS delivered, "
            "SUM(CASE WHEN delivered_at IS NULL THEN attempts ELSE 0 END) AS attempts, "
            "MIN(CASE WHEN delivered_at IS NULL THEN created_at END) AS oldest_pending_at "
            "FROM alert_outbox"
        ).fetchone()
        errors = c.execute(
            "SELECT last_error, COUNT(*) AS count FROM alert_outbox "
            "WHERE delivered_at IS NULL AND last_error IS NOT NULL "
            "GROUP BY last_error ORDER BY count DESC LIMIT 5"
        ).fetchall()
    oldest = row["oldest_pending_at"]
    return {
        "pending": int(row["pending"] or 0),
        "delivered": int(row["delivered"] or 0),
        "pending_attempts": int(row["attempts"] or 0),
        "oldest_pending_at": oldest,
        "oldest_pending_seconds": max(0, int(now - oldest)) if oldest is not None else None,
        "errors": [{"code": r["last_error"], "count": int(r["count"])} for r in errors],
    }


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
        if not _amount_matches(inv.get("amount"), row["amount"]):
            log.error("Crypto Pay: сумма счёта не совпадает с донатом %s", row["id"])
            return "pending"
        if str(inv.get("fiat", "")).upper() != row["currency"].upper():
            log.error("Crypto Pay: валюта счёта не совпадает с донатом %s", row["id"])
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
        amount = data.get("amount") or {}
        if amount.get("value") != f"{row['amount']:.2f}":
            log.error("ЮKassa: сумма платежа не совпадает для %s", row["id"])
            return "pending"
        if str(amount.get("currency", "")).upper() != row["currency"].upper():
            log.error("ЮKassa: валюта платежа не совпадает для %s", row["id"])
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
        if not _amount_matches(data.get("price_amount"), row["amount"]):
            log.error("NOWPayments: price_amount не совпадает для %s", row["id"])
            return "pending"
        if str(data.get("price_currency", "")).lower() != row["currency"].lower():
            log.error("NOWPayments: price_currency не совпадает для %s", row["id"])
            return "pending"
        return "paid"
    if st in ("failed", "refunded"):
        return "failed"
    return "expired" if st == "expired" else "pending"  # partially_paid и прочее ждут


def np_signature(payload: dict, secret: str) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode(), canon.encode(), hashlib.sha512).hexdigest()


# ───────────────────────── применение статуса ─────────────────────────
_paid_handler: Callable[[sqlite3.Row], Awaitable[bool]] | None = None


def set_paid_handler(fn: Callable[[sqlite3.Row], Awaitable[bool]]) -> None:
    global _paid_handler
    _paid_handler = fn


async def mark_paid(donation_id: str) -> bool:
    """Переводит донат в paid и атомарно ставит алерт в устойчивую очередь."""
    now = time.time()
    with closing(db()) as c:
        cur = c.execute("UPDATE donations SET status='paid', paid_at=? WHERE id=? AND status!='paid'",
                        (now, donation_id))
        first = cur.rowcount == 1
        row = c.execute("SELECT * FROM donations WHERE id=?", (donation_id,)).fetchone()
        if first and row:
            c.execute(
                "INSERT OR IGNORE INTO alert_outbox(donation_id,created_at,next_attempt_at) VALUES(?,?,?)",
                (donation_id, now, now),
            )
        c.commit()
    if first:
        await dispatch_pending_alerts()
    return first


async def dispatch_pending_alerts(limit: int = 50) -> int:
    """Доставляет подтверждённые алерты с lease и повтором при отсутствии оверлея."""
    if _paid_handler is None:
        return 0
    now = time.time()
    claimed: list[tuple[sqlite3.Row, int]] = []
    with closing(db()) as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute(
            "SELECT donation_id, attempts FROM alert_outbox WHERE delivered_at IS NULL "
            "AND next_attempt_at<=? AND (claimed_until IS NULL OR claimed_until<?) "
            "ORDER BY created_at LIMIT ?", (now, now, limit),
        ).fetchall()
        for item in rows:
            cur = c.execute(
                "UPDATE alert_outbox SET claimed_until=?, attempts=attempts+1 "
                "WHERE donation_id=? AND delivered_at IS NULL "
                "AND (claimed_until IS NULL OR claimed_until<?) AND next_attempt_at<=?",
                (now + 60, item["donation_id"], now, now),
            )
            if cur.rowcount:
                donation = c.execute("SELECT * FROM donations WHERE id=?", (item["donation_id"],)).fetchone()
                if donation:
                    claimed.append((donation, int(item["attempts"]) + 1))
        c.commit()

    delivered_count = 0
    for donation, attempts in claimed:
        error = "no_overlay_clients"
        try:
            delivered = await _paid_handler(donation)
            if delivered:
                with closing(db()) as c:
                    c.execute(
                        "UPDATE alert_outbox SET delivered_at=?, claimed_until=NULL, last_error=NULL "
                        "WHERE donation_id=? AND delivered_at IS NULL", (time.time(), donation["id"]),
                    )
                    c.commit()
                delivered_count += 1
                continue
        except Exception as exc:
            error = "handler_error"
            log.error("Не удалось доставить алерт доната %s: %s", donation["id"], type(exc).__name__)
        delay = min(300, 5 * (2 ** min(attempts - 1, 6)))
        with closing(db()) as c:
            c.execute(
                "UPDATE alert_outbox SET claimed_until=NULL, next_attempt_at=?, last_error=? "
                "WHERE donation_id=? AND delivered_at IS NULL",
                (time.time() + delay, error, donation["id"]),
            )
            c.commit()
    return delivered_count


async def refresh(row: sqlite3.Row) -> str:
    """Спрашивает у провайдера актуальный статус и применяет его."""
    if not row["provider_txn_id"]:
        return row["status"]
    checkers = {"yookassa": _yk_verdict, "nowpayments": _np_verdict, "cryptopay": _cp_verdict}
    try:
        verdict = await checkers[row["provider"]](row)
    except (PaymentError, httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError) as e:
        # Ответ провайдера может быть корректным JSON, но иметь неожиданную структуру.
        # Не даём одному повреждённому ответу прервать опрос остальных платежей и outbox.
        log.warning("Не удалось проверить %s/%s: %s", row["provider"], row["id"], type(e).__name__)
        return row["status"]
    if verdict == "paid":
        await mark_paid(row["id"])
    elif verdict in ("canceled", "failed", "expired"):
        with closing(db()) as c:
            c.execute("UPDATE donations SET status=? WHERE id=? AND status IN ('created','pending')",
                      (verdict, row["id"]))
            c.commit()
    return verdict


def prune_delivered_alerts(retention_seconds: int = 30 * 86400, limit: int = 500) -> int:
    """Удаляет только давно доставленные записи, ограничивая объём одной очистки."""
    cutoff = time.time() - max(0, retention_seconds)
    with closing(db()) as c:
        cur = c.execute(
            "DELETE FROM alert_outbox WHERE donation_id IN ("
            "SELECT donation_id FROM alert_outbox WHERE delivered_at IS NOT NULL "
            "AND delivered_at<? ORDER BY delivered_at LIMIT ?)",
            (cutoff, max(1, limit)),
        )
        c.commit()
        return cur.rowcount


async def poll_once(tick: int = 0) -> None:
    now = time.time()
    with closing(db()) as c:
        # Не переводим платежи с provider_txn_id в expired только по локальному таймеру:
        # провайдер остаётся источником истины, а позднее подтверждение должно быть найдено.
        # Локальный TTL применяем лишь к незавершённым записям, для которых счёт не был создан.
        c.execute("UPDATE donations SET status='expired' WHERE status='created' "
                  "AND provider_txn_id IS NULL AND created_at<?", (now - PENDING_TTL,))
        c.commit()
        rows = c.execute("SELECT * FROM donations WHERE status='pending' AND provider_txn_id IS NOT NULL").fetchall()
    for row in rows:
        if now - row["created_at"] > 1200 and tick % 12:  # старше 20 минут — раз в минуту
            continue
        await refresh(row)
    await dispatch_pending_alerts()
    if tick % 720 == 0:  # раз в час при интервале poller 5 секунд
        prune_delivered_alerts()


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
        if (
            not row
            or row["provider"] != method
            or row["currency"] != currency
            or abs(row["amount"] - amount) > 0.001
            or row["donor_name"] != name
            or row["message"] != message
        ):
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
    if not isinstance(upd, dict):
        raise PaymentError("bad request", 400)
    if upd.get("update_type") == "invoice_paid":
        payload = upd.get("payload")
        if not isinstance(payload, dict):
            raise PaymentError("bad request", 400)
        row = find_by_txn("cryptopay", str(payload.get("invoice_id", "")))
        if row:
            await refresh(row)
