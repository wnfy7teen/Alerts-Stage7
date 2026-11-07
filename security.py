"""
Защита: заголовки и CSP с nonce, IP клиента за прокси, лимиты, CSRF, антибот, проверка файлов.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

import payments as pay

log = logging.getLogger("security")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


TRUST_PROXY_HEADERS = _env("TRUST_PROXY_HEADERS", "0") == "1"
FORCE_HTTPS = _env("FORCE_HTTPS", "0") == "1"
CSP_REPORT_ONLY = _env("CSP_REPORT_ONLY", "0") == "1"
ADMIN_ALLOWED = [x.strip() for x in _env("ADMIN_ALLOWED_CIDRS").split(",") if x.strip()]
TURNSTILE_SITE_KEY = _env("TURNSTILE_SITE_KEY")
TURNSTILE_SECRET = _env("TURNSTILE_SECRET_KEY")
CSRF_HEADER = "donation-admin"


# ───────────────────────── IP клиента ─────────────────────────
def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(req: Request) -> str:
    """За доверенным прокси берём адрес из его заголовка. Для X-Forwarded-For это ПОСЛЕДНЯЯ запись:
    её дописал наш прокси, а всё левее клиент мог подделать."""
    if TRUST_PROXY_HEADERS:
        ip = _valid_ip(req.headers.get("cf-connecting-ip", ""))
        if ip:
            return ip
        xff = req.headers.get("x-forwarded-for", "")
        if xff:
            ip = _valid_ip(xff.split(",")[-1])
            if ip:
                return ip
    return req.client.host if req.client else "?"


def ip_in(ip: str, cidrs: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in ipaddress.ip_network(c, strict=False) for c in cidrs)
    except ValueError:
        return False


def forwarded_proto(req: Request) -> str:
    if TRUST_PROXY_HEADERS:
        return req.headers.get("x-forwarded-proto", "").split(",")[-1].strip().lower() or req.url.scheme
    return req.url.scheme


def is_https(req: Request) -> bool:
    return pay.PUBLIC_URL.startswith("https://") or forwarded_proto(req) == "https"


# ───────────────────────── лимиты ─────────────────────────
class FailureLimiter:
    """Считает неудачные попытки (вход в админку): после `limit` ошибок за `window` секунд — блокировка."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._fails: dict[str, deque] = defaultdict(deque)

    def _trim(self, key: str) -> deque:
        now, q = time.monotonic(), self._fails[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._trim(key)) >= self.limit

    def fail(self, key: str) -> None:
        self._trim(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


# ───────────────────────── заголовки и CSP ─────────────────────────
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,255}$")


def csp(kind: str, nonce: str, host: str) -> str:
    """Политика для страницы. Inline-скрипты и стили допускаются только с nonce этого ответа."""
    parts = ["default-src 'none'", "base-uri 'none'", "form-action 'self'", "frame-ancestors 'none'", "object-src 'none'"]
    script, style = f"'nonce-{nonce}'", f"'self' 'nonce-{nonce}'"
    if kind == "donate":
        yk = "https://yookassa.ru https://*.yookassa.ru https://yoomoney.ru https://*.yoomoney.ru"
        parts += [
            f"script-src {script} {yk} https://challenges.cloudflare.com",
            f"style-src {style} https://fonts.googleapis.com",
            "font-src https://fonts.gstatic.com",
            "img-src 'self' data: https:",
            f"connect-src 'self' {yk} https://challenges.cloudflare.com",
            "frame-src https:",  # платёжные формы и 3-D Secure банков открываются во фреймах разных доменов
        ]
    elif kind == "overlay":
        ws = f"wss://{host} ws://{host}" if _HOST_RE.match(host) else ""
        parts += [f"script-src {script}", f"style-src {style}", "img-src 'self'", "media-src 'self'",
                  f"connect-src 'self' {ws}".strip()]
    elif kind == "admin":
        parts += [f"script-src {script}", f"style-src {style}", "img-src 'self' data:", "media-src 'self'", "connect-src 'self'"]
    else:
        parts += [f"style-src {style}"]
    parts.append("report-uri /api/csp-report")
    return "; ".join(parts)


def apply_common_headers(resp, req: Request, path: str) -> None:
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    private = path in ("/overlay", "/admin") or path.startswith("/api/admin")
    h.setdefault("Referrer-Policy", "no-referrer" if private else "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=(), interest-cohort=()")
    if is_https(req):
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if path.startswith("/media/"):  # загруженные файлы не должны выполняться как страницы
        h["Content-Security-Policy"] = "default-src 'none'; sandbox"
    elif "content-security-policy" not in h and "content-security-policy-report-only" not in h:
        h["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    if path.startswith("/api/") or private:
        h.setdefault("Cache-Control", "no-store")
    if private or path in ("/thanks", "/crypto-webhook") or path.startswith(("/api/", "/media/")):
        h.setdefault("X-Robots-Tag", "noindex, nofollow")


# ───────────────────────── CSRF ─────────────────────────
def check_origin(req: Request) -> None:
    """Запрос с чужого сайта отклоняется. Браузер сам ставит Origin / Sec-Fetch-Site, подделать их из страницы нельзя."""
    origin = req.headers.get("origin")
    if origin:
        host = urlsplit(origin).netloc
        allowed = {req.headers.get("host", "")}
        if pay.PUBLIC_URL:
            allowed.add(urlsplit(pay.PUBLIC_URL).netloc)
        if host not in allowed:
            raise HTTPException(403, "Запрос с чужого сайта отклонён")
    elif req.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        raise HTTPException(403, "Запрос с чужого сайта отклонён")


def csrf_guard(req: Request) -> None:
    """Для изменяющих запросов админки: нужен свой заголовок (чужая страница не может добавить его без CORS) и верный Origin."""
    if req.method in ("GET", "HEAD", "OPTIONS"):
        return
    if req.headers.get("x-requested-with") != CSRF_HEADER:
        raise HTTPException(403, "Нужен заголовок X-Requested-With")
    check_origin(req)


# ───────────────────────── антибот ─────────────────────────
async def verify_turnstile(token: str, ip: str) -> bool:
    """Если Turnstile не настроен — проверка выключена. Если настроен, то при любой ошибке отказываем."""
    if not TURNSTILE_SECRET:
        return True
    if not token or len(token) > 2048:
        return False
    try:
        async with pay._client() as c:
            r = await c.post("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                             data={"secret": TURNSTILE_SECRET, "response": token, "remoteip": ip})
        return bool(r.json().get("success"))
    except Exception as e:
        log.warning("Turnstile недоступен: %s", type(e).__name__)
        return False


# ───────────────────────── проверка загружаемых файлов ─────────────────────────
def looks_like(ext: str, head: bytes) -> bool:
    """Сверяет начало файла с заявленным расширением (расширение одно не доказывает ничего)."""
    if ext == ".gif":
        return head[:4] == b"GIF8"
    if ext == ".png":
        return head[:8] == b"\x89PNG\r\n\x1a\n"
    if ext in (".jpg", ".jpeg"):
        return head[:3] == b"\xff\xd8\xff"
    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == ".webm":
        return head[:4] == b"\x1a\x45\xdf\xa3"
    if ext in (".mp4", ".m4a"):
        return head[4:8] == b"ftyp"
    if ext == ".mp3":
        return head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if ext == ".wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == ".ogg":
        return head[:4] == b"OggS"
    return False


# ───────────────────────── Sentry без персональных данных ─────────────────────────
def scrub_sentry_event(event: dict, hint=None) -> dict:
    """Оставляет в событии только метод и адрес без параметров. Тело запроса, заголовки (пароль админки, подписи),
    параметры адреса (ключ оверлея), значения переменных и «хлебные крошки» не отправляются."""
    req = event.get("request")
    if req:
        event["request"] = {"method": req.get("method"), "url": str(req.get("url", "")).split("?")[0]}
    for key in ("user", "server_name", "breadcrumbs", "extra", "contexts"):
        event.pop(key, None)
    for ex in (event.get("exception") or {}).get("values") or []:
        for frame in (ex.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
    return event


def init_sentry() -> bool:
    """Включается только если задан SENTRY_DSN."""
    dsn = _env("SENTRY_DSN")
    if not dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn, environment=_env("SENTRY_ENV", "production"), send_default_pii=False, include_local_variables=False,
        max_request_body_size="never", traces_sample_rate=0.0,
        before_send=scrub_sentry_event,  # type: ignore[arg-type]
    )
    log.info("Sentry включён (без персональных данных)")
    return True
