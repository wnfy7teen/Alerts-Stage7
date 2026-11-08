"""
Донат-алерты для Twitch через Crypto Bot (Crypto Pay API).

Поток: страница /donate -> счёт в Crypto Pay -> оплата -> алерт на /overlay (OBS).
Оплату ловим двумя способами: опросом getInvoices (работает всегда) и webhook (мгновенно, по желанию).
"""
import asyncio
import csv
import io
import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path

import edge_tts
import httpx
import uvicorn
from dotenv import load_dotenv
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

import payments as pay
import security as sec
import seo

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("donations")

BASE = Path(__file__).parent
MEDIA = Path(os.getenv("MEDIA_DIR") or BASE / "media")
TTS_DIR = MEDIA / "tts"
STATIC = BASE / "static"
CFG_PATH = Path(os.getenv("CONFIG_PATH") or BASE / "config.json")
BACKUP_STATUS_PATH = Path(os.getenv("BACKUP_STATUS_PATH") or BASE / "backup-status.json")
_DEPLOY_HISTORY_CACHE: dict = {"fetched_at": 0.0, "payload": None}
MEDIA.mkdir(exist_ok=True)
TTS_DIR.mkdir(exist_ok=True)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
OVERLAY_KEY = os.getenv("OVERLAY_KEY", "")
PORT = int(os.getenv("PORT", "8000"))

if not ADMIN_PASSWORD or not OVERLAY_KEY:
    raise SystemExit("Заполните ADMIN_PASSWORD и OVERLAY_KEY в файле .env")
if os.getenv("ALLOW_WEAK_SECRETS") != "1" and (len(ADMIN_PASSWORD) < 12 or len(OVERLAY_KEY) < 16):
    raise SystemExit("ADMIN_PASSWORD должен быть не короче 12 символов, OVERLAY_KEY — не короче 16")
if not pay.enabled_methods():
    log.warning("Не настроен ни один способ оплаты (ЮKassa / NOWPayments / Crypto Pay): тестовые алерты работают, приём донатов — нет")

SYMBOLS = {"RUB": "₽", "USD": "$", "EUR": "€", "UAH": "₴", "KZT": "₸"}


# ───────────────────────── настройки ─────────────────────────
class Tier(BaseModel):
    """Оформление алерта. Применяется самый «высокий» тир, чей min_amount <= суммы доната."""
    min_amount: float = Field(default=0, ge=0)
    image: str = ""            # файл из папки media (gif/png/webp/webm/mp4)
    sound: str = ""            # файл из папки media (mp3/wav/ogg); пусто = встроенный сигнал
    animation: str = "bounce"  # bounce | slide | zoom | fade | shake | flip
    duration: int = Field(default=8, ge=2, le=60)  # минимум секунд на экране
    color: str = "#0593C6"
    tts: bool = True

    @field_validator("image", "sound")
    @classmethod
    def safe_name(cls, v: str) -> str:
        if v and Path(v).name != v:
            raise ValueError("недопустимое имя файла")
        return v

    @field_validator("color")
    @classmethod
    def safe_color(cls, v: str) -> str:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError("цвет должен быть вида #RRGGBB")
        return v


class Config(BaseModel):
    # приём донатов
    min_amount: float = Field(default=50, ge=1)
    max_amount: float = Field(default=100000, ge=1)
    fiat: str = Field(default="RUB", pattern=r"^[A-Z]{3}$")
    accepted_assets: str = Field(default="USDT,TON", pattern=r"^[A-Z, ]{0,80}$")
    max_message_len: int = Field(default=500, ge=0, le=500)
    # внешний вид алерта
    title_template: str = Field(default="{name} задонатил(а) {amount}", max_length=200)
    text_color: str = "#003D55"
    font_size: int = Field(default=28, ge=12, le=80)
    sound_volume: float = Field(default=0.8, ge=0, le=1)
    # озвучка
    tts_enabled: bool = True
    tts_voice: str = Field(default="ru-RU-SvetlanaNeural", pattern=r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural$")
    tts_rate: int = Field(default=0, ge=-50, le=100)
    tts_volume: float = Field(default=1.0, ge=0, le=1)
    tts_min_amount: float = Field(default=100, ge=0)
    tts_max_chars: int = Field(default=200, ge=20, le=500)
    tts_template: str = Field(default="{name} задонатил {amount}. {message}", max_length=300)
    tiers: list[Tier] = Field(
        max_length=20,
        default_factory=lambda: [
            Tier(min_amount=0),
            Tier(min_amount=1000, animation="zoom", color="#005A7A", duration=12),
        ]
    )

    @field_validator("text_color")
    @classmethod
    def safe_color(cls, v: str) -> str:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError("цвет должен быть вида #RRGGBB")
        return v


_cfg: Config | None = None


def save_cfg(c: Config) -> None:
    global _cfg
    CFG_PATH.write_text(c.model_dump_json(indent=2), encoding="utf-8")
    _cfg = c


def load_cfg() -> Config:
    global _cfg
    if _cfg is None:
        if CFG_PATH.exists():
            try:
                raw = json.loads(CFG_PATH.read_text(encoding="utf-8"))
                migrated = False
                if raw.get("text_color", "").upper() == "#FFFFFF":
                    raw["text_color"] = "#003D55"
                    migrated = True
                for tier in raw.get("tiers", []):
                    old_color = str(tier.get("color", "")).upper()
                    if old_color == "#9146FF":
                        tier["color"] = "#0593C6"
                        migrated = True
                    elif old_color == "#F5A623":
                        tier["color"] = "#005A7A"
                        migrated = True
                _cfg = Config(**raw)
                if migrated:
                    save_cfg(_cfg)
            except Exception as e:
                log.error("config.json повреждён (%s), использую значения по умолчанию", e)
                _cfg = Config()
        else:
            save_cfg(Config())
    return _cfg  # type: ignore[return-value]


# ───────────────────────── алерты ─────────────────────────
clients: set[WebSocket] = set()


async def broadcast(alert: dict) -> bool:
    delivered = False
    for ws in list(clients):
        try:
            await ws.send_json(alert)
            delivered = True
        except Exception:
            clients.discard(ws)
    return delivered


def num_text(amount: float) -> str:
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def amount_text(amount: float, fiat: str) -> str:
    return f"{num_text(amount)} {SYMBOLS.get(fiat, fiat)}"


def amount_words(amount: float, fiat: str) -> str:
    if fiat == "RUB" and float(amount).is_integer():
        n = int(amount)
        if n % 100 in range(11, 15):
            w = "рублей"
        elif n % 10 == 1:
            w = "рубль"
        elif n % 10 in (2, 3, 4):
            w = "рубля"
        else:
            w = "рублей"
        return f"{n} {w}"
    return f"{num_text(amount)} {fiat}"


def fill(template: str, **kw: str) -> str:
    for k, v in kw.items():
        template = template.replace("{" + k + "}", v)
    return template


def pick_tier(cfg: Config, amount: float) -> Tier:
    tiers = sorted(cfg.tiers, key=lambda t: t.min_amount) or [Tier()]
    chosen = tiers[0]
    for t in tiers:
        if amount >= t.min_amount:
            chosen = t
    return chosen


async def synth(text: str, cfg: Config) -> str:
    name = f"{uuid.uuid4().hex}.mp3"
    comm = edge_tts.Communicate(text, cfg.tts_voice, rate=f"{cfg.tts_rate:+d}%")
    await asyncio.wait_for(comm.save(str(TTS_DIR / name)), timeout=20)
    return f"/media/tts/{name}"


def prune_tts_files(max_age_seconds: int = 86400, limit: int = 500, scan_limit: int = 2000) -> int:
    """Удаляет старые автоматически созданные MP3 с ограничением удаления и объёма сканирования."""
    cutoff = time.time() - max(0, max_age_seconds)
    removed = 0
    scanned = 0
    for path in TTS_DIR.glob("*.mp3"):
        if removed >= max(1, limit) or scanned >= max(1, scan_limit):
            break
        scanned += 1
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("Не удалось удалить старый TTS-файл %s: %s", path.name, type(exc).__name__)
    return removed


async def make_alert(name: str, message: str, amount: float, fiat: str, alert_id: str | None = None) -> dict:
    cfg = load_cfg()
    tier = pick_tier(cfg, amount)
    amt = amount_text(amount, fiat)

    tts_text, tts_url = "", ""
    if cfg.tts_enabled and tier.tts and amount >= cfg.tts_min_amount:
        clean_msg = re.sub(r"https?://\S+|www\.\S+", "", message).strip()
        tts_text = fill(cfg.tts_template, name=name, amount=amount_words(amount, fiat), message=clean_msg)
        tts_text = tts_text.strip()[: cfg.tts_max_chars]
        try:
            tts_url = await synth(tts_text, cfg)
        except Exception as e:
            log.warning("Не удалось сгенерировать озвучку (%s) — оверлей попробует голос браузера", type(e).__name__)

    return {
        "id": alert_id or uuid.uuid4().hex,
        "title": fill(cfg.title_template, name=name, amount=amt),
        "message": message,
        "image": f"/media/{tier.image}" if tier.image else "",
        "sound": f"/media/{tier.sound}" if tier.sound else "",
        "animation": tier.animation,
        "duration": tier.duration,
        "color": tier.color,
        "text_color": cfg.text_color,
        "font_size": cfg.font_size,
        "sound_volume": cfg.sound_volume,
        "tts_text": tts_text,
        "tts_url": tts_url,
        "tts_volume": cfg.tts_volume,
        "tts_rate": cfg.tts_rate,
    }


async def on_paid(row) -> bool:
    """Отправляет алерт с постоянным ID, чтобы повторную доставку можно было дедуплицировать."""
    # Не создаём TTS-файл и не тратим время на подготовку алерта, пока OBS офлайн.
    if not clients:
        return False
    alert = await make_alert(row["donor_name"], row["message"], row["amount"], row["currency"], row["id"])
    return await broadcast(alert)


async def poller() -> None:
    tick = 0
    while True:
        try:
            await pay.poll_once(tick)
            if tick > 0 and tick % 720 == 0:  # раз в час при интервале 5 секунд
                prune_tts_files()
        except Exception as e:
            log.warning("Опрос платежей не удался: %s", e)
        tick += 1
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    pay.init_db()
    pay.set_paid_handler(on_paid)
    load_cfg()
    prune_tts_files()  # чистим старую озвучку при запуске
    task = asyncio.create_task(poller())
    yield
    task.cancel()


sec.init_sentry()
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# CORS намеренно не включён: браузер не даст чужим сайтам читать ответы API.

api_limiter = pay.RateLimiter(limit=300, window=60)
hook_limiter = pay.RateLimiter(limit=120, window=60)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path, ip = request.url.path, sec.client_ip(request)

    def reject(detail: str, status: int) -> JSONResponse:
        response = JSONResponse({"detail": detail}, status_code=status)
        sec.apply_common_headers(response, request, path)
        return response

    if sec.FORCE_HTTPS and pay.PUBLIC_URL.startswith("https://") and sec.forwarded_proto(request) == "http" and path != "/api/health":
        target = pay.PUBLIC_URL + path + ("?" + request.url.query if request.url.query else "")
        return RedirectResponse(target, status_code=308)

    limit = 21 * 1024 * 1024 if path == "/api/admin/upload" else 1024 * 1024
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > limit:
        return reject("Слишком большой запрос", 413)
    if path.startswith("/api/webhooks") or path == "/crypto-webhook":
        if not hook_limiter.allow(ip):
            return reject("Слишком часто", 429)
    elif path.startswith("/api/") and not api_limiter.allow(ip):
        return reject("Слишком много запросов", 429)
    if (path == "/admin" or path.startswith("/api/admin")) and sec.ADMIN_ALLOWED and not sec.ip_in(ip, sec.ADMIN_ALLOWED):
        return reject("Not Found", 404)

    response = await call_next(request)
    sec.apply_common_headers(response, request, path)
    return response

security = HTTPBasic()


auth_failures = sec.FailureLimiter(limit=10, window=900)  # 10 неверных паролей за 15 минут -> блокировка по IP


def admin(req: Request, creds: HTTPBasicCredentials = Depends(security)) -> None:
    ip = sec.client_ip(req)
    if auth_failures.blocked(ip):
        raise HTTPException(429, "Слишком много неверных попыток, подождите 15 минут")
    ok = secrets.compare_digest(creds.username.encode(), b"admin") & secrets.compare_digest(
        creds.password.encode(), ADMIN_PASSWORD.encode()
    )
    if not ok:
        auth_failures.fail(ip)
        raise HTTPException(401, "Неверный пароль", headers={"WWW-Authenticate": "Basic"})
    auth_failures.reset(ip)


ADMIN = [Depends(admin), Depends(sec.csrf_guard)]  # вход + защита от CSRF для изменяющих запросов


# ───────────────────────── публичные страницы ─────────────────────────


donate_limiter = pay.RateLimiter(limit=15, window=600)  # 5 попыток за 10 минут на IP
IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class DonationIn(BaseModel):
    method: Literal["yookassa", "nowpayments", "cryptopay"]
    amount: float = Field(gt=0, lt=10_000_000)
    name: str = Field(default="", max_length=200)
    message: str = Field(default="", max_length=5000)
    website: str = Field(default="", max_length=200)  # honeypot: человек это поле не видит и не заполняет
    turnstile_token: str = Field(default="", max_length=2048)


def html_page(name: str, kind: str, req: Request, transform=None) -> HTMLResponse:
    """Отдаёт страницу, подставляя одноразовый nonce в <script> и <style> (inline-код без nonce браузер не выполнит)."""
    nonce = secrets.token_urlsafe(16)
    html = (STATIC / name).read_text(encoding="utf-8")
    if transform:
        html = transform(html)
    html = html.replace("<script>", f'<script nonce="{nonce}">').replace("<style>", f'<style nonce="{nonce}">')
    header = "Content-Security-Policy-Report-Only" if sec.CSP_REPORT_ONLY else "Content-Security-Policy"
    return HTMLResponse(html, headers={header: sec.csp(kind, nonce, req.headers.get("host", ""))})


@app.get("/")
def home(req: Request):
    """Главная и есть страница доната: текст, способы оплаты и FAQ отрисовываются на сервере (для поисковиков и ИИ-ботов)."""
    return html_page("donate.html", "donate", req, lambda tpl: seo.render_donate(tpl, req, load_cfg()))


@app.get("/donate")
def donate_old_address(req: Request):
    """Старый адрес: постоянный редирект на главную, чтобы у страницы был один канонический адрес."""
    query = req.url.query
    return RedirectResponse("/" + ("?" + query if query else ""), status_code=301)


@app.get("/robots.txt")
def robots(req: Request):
    return Response(seo.robots_txt(req), media_type="text/plain; charset=utf-8")


@app.get("/sitemap.xml")
def sitemap(req: Request):
    return Response(seo.sitemap_xml(req), media_type="application/xml; charset=utf-8")


@app.get("/llms.txt")
def llms(req: Request):
    return Response(seo.llms_txt(req, load_cfg()), media_type="text/plain; charset=utf-8")


def static_asset(name: str, media_type: str) -> Response:
    return Response((STATIC / name).read_bytes(), media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/og-cover.png")
def og_cover():
    return static_asset("og-cover.png", "image/png")


@app.get("/theme.css")
def theme_css():
    return static_asset("theme.css", "text/css; charset=utf-8")


@app.get("/favicon.svg")
def favicon_svg():
    return static_asset("favicon.svg", "image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico():
    return static_asset("favicon.ico", "image/x-icon")


@app.get("/yandex_f635b5b1368c3ec6.html")
def yandex_verification():
    """Публичный файл подтверждения прав в Яндекс Вебмастере."""
    return static_asset("yandex_f635b5b1368c3ec6.html", "text/html; charset=utf-8")


@app.get("/overlay")
def overlay_page(req: Request):
    return html_page("overlay.html", "overlay", req)


@app.get("/thanks")
def thanks(req: Request):
    return html_page("thanks.html", "thanks", req)


csp_limiter = pay.RateLimiter(limit=30, window=60)
WEBHOOK_BODY_LIMIT = 64 * 1024
CSP_REPORT_BODY_LIMIT = 8 * 1024


async def read_limited_body(req: Request, limit: int) -> bytes:
    """Читает тело потоково и прекращает чтение при превышении лимита."""
    body = bytearray()
    async for chunk in req.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "Слишком большой запрос")
        body.extend(chunk)
    return bytes(body)


@app.post("/api/csp-report", status_code=204)
async def csp_report(req: Request):
    """Сюда браузер присылает нарушения CSP: помогает отладить политику на реальной странице оплаты."""
    if csp_limiter.allow(sec.client_ip(req)):
        text = (await read_limited_body(req, CSP_REPORT_BODY_LIMIT))[:1500].decode("utf-8", "replace")
        log.warning("CSP: %s", re.sub(r"[\r\n\t]+", " ", text))
    return Response(status_code=204)


@app.get("/api/public-config")
def public_config():
    c = load_cfg()
    return {
        "min_amount": c.min_amount,
        "max_amount": c.max_amount,
        "symbol": SYMBOLS.get(c.fiat, c.fiat),
        "max_message_len": c.max_message_len,
        "streamer": os.getenv("STREAMER_NAME", "стримера"),
        "methods": pay.enabled_methods(),
        "turnstile_site_key": sec.TURNSTILE_SITE_KEY,
    }


def _err(e: pay.PaymentError) -> HTTPException:
    return HTTPException(e.status, str(e))


@app.post("/api/donations")
async def create_donation(req: Request):
    """Создаёт платёж. Заголовок Idempotency-Key защищает от двойного списания при повторной отправке."""
    if not donate_limiter.allow(sec.client_ip(req)):
        raise HTTPException(429, "Слишком много попыток, подождите несколько минут")
    sec.check_origin(req)
    try:
        body = DonationIn.model_validate_json(await read_limited_body(req, 1024 * 1024))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from None
    idem = req.headers.get("idempotency-key", "")
    if not IDEM_RE.match(idem):
        raise HTTPException(400, "Нужен заголовок Idempotency-Key (16–64 символа: буквы, цифры, - и _)")
    if body.website.strip():
        raise HTTPException(400, "Не удалось отправить форму")
    if not pay.get_by_idem(idem):  # повтор того же запроса (двойной клик) повторной проверки не требует
        if not await sec.verify_turnstile(body.turnstile_token, sec.client_ip(req)):
            raise HTTPException(400, "Не удалось подтвердить, что вы не робот. Обновите страницу и попробуйте снова")
    cfg = load_cfg()
    try:
        amount = pay.validate_amount(body.amount, cfg.min_amount, cfg.max_amount, SYMBOLS.get(cfg.fiat, cfg.fiat))
        return await pay.create_donation(
            method=body.method, amount=amount, currency=cfg.fiat,
            name=pay.sanitize_text(body.name, 30) or "Аноним",
            message=pay.sanitize_text(body.message, cfg.max_message_len),
            idem_key=idem, accepted_assets=cfg.accepted_assets,
        )
    except pay.PaymentError as e:
        raise _err(e) from None


status_limiter = pay.RateLimiter(limit=120, window=60)


@app.get("/api/payments/status/{donation_id}")
def payment_status(donation_id: str, req: Request):
    if not status_limiter.allow(sec.client_ip(req)):
        raise HTTPException(429, "Слишком часто")
    st = pay.public_status(donation_id)
    if not st:
        raise HTTPException(404, "Платёж не найден")
    return st


@app.post("/api/webhooks/yookassa")
async def webhook_yookassa(req: Request):
    try:
        body = json.loads(await read_limited_body(req, WEBHOOK_BODY_LIMIT))
    except ValueError:
        raise HTTPException(400, "bad request") from None
    try:
        await pay.handle_yookassa_webhook(body if isinstance(body, dict) else {}, sec.client_ip(req))
    except pay.PaymentError as e:
        raise _err(e) from None
    return {"ok": True}


@app.post("/api/webhooks/nowpayments")
async def webhook_nowpayments(req: Request):
    try:
        await pay.handle_nowpayments_webhook(
            await read_limited_body(req, WEBHOOK_BODY_LIMIT), req.headers.get("x-nowpayments-sig", "")
        )
    except pay.PaymentError as e:
        raise _err(e) from None
    return {"ok": True}


@app.post("/api/webhooks/cryptopay")
@app.post("/crypto-webhook")  # прежний адрес оставлен, чтобы не перенастраивать Crypto Pay
async def webhook_cryptopay(req: Request):
    try:
        await pay.handle_cryptopay_webhook(
            await read_limited_body(req, WEBHOOK_BODY_LIMIT), req.headers.get("crypto-pay-api-signature", "")
        )
    except pay.PaymentError as e:
        raise _err(e) from None
    return {"ok": True}


@app.get("/api/health")
def health():
    try:
        pay.recent(1)
    except Exception:
        raise HTTPException(503, "db unavailable") from None
    return {"status": "ok"}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not secrets.compare_digest(ws.query_params.get("key", "").encode(), OVERLAY_KEY.encode()):
        await ws.close(code=1008)
        return
    if len(clients) >= 10:
        await ws.close(code=1013)
        return
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except Exception as e:  # обрыв связи с оверлеем — обычное дело
        log.debug("WebSocket закрыт: %s", type(e).__name__)
    finally:
        clients.discard(ws)


# ───────────────────────── админка ─────────────────────────
ALLOWED_EXT = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".webm", ".mp4", ".mp3", ".wav", ".ogg", ".m4a"}
MAX_UPLOAD = 20 * 1024 * 1024


class TestIn(BaseModel):
    name: str = "Тестовый донатер"
    message: str = "Привет! Это тестовое сообщение."
    amount: float = 100


@app.get("/admin", dependencies=ADMIN)
def admin_page(req: Request):
    return html_page("admin.html", "admin", req)


@app.get("/api/admin/config", dependencies=ADMIN)
def get_config():
    return load_cfg()


@app.put("/api/admin/config", dependencies=ADMIN)
def put_config(cfg: Config):
    save_cfg(cfg)
    pay.record_admin_action("config.updated", details={"fields": sorted(cfg.model_fields_set)})
    return {"ok": True}


@app.get("/api/admin/links", dependencies=ADMIN)
def links():
    return {"overlay": f"{pay.PUBLIC_URL}/overlay?key={OVERLAY_KEY}"}


@app.get("/api/admin/media", dependencies=ADMIN)
def list_media():
    return sorted(p.name for p in MEDIA.iterdir() if p.is_file())


@app.post("/api/admin/upload", dependencies=ADMIN)
async def upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, "Допустимы: gif, png, jpg, webp, webm, mp4, mp3, wav, ogg, m4a")
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(file.filename or "file").stem)[:30] or "file"
    name = f"{uuid.uuid4().hex[:6]}_{stem}{ext}"
    path = MEDIA / name
    head = await file.read(16)
    if not sec.looks_like(ext, head):
        raise HTTPException(400, "Содержимое файла не соответствует его расширению")
    size, too_big = len(head), False
    with open(path, "wb") as f:
        f.write(head)
        while chunk := await file.read(256 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                too_big = True
                break
            f.write(chunk)
    if too_big:
        path.unlink(missing_ok=True)
        raise HTTPException(413, "Файл больше 20 МБ")
    pay.record_admin_action("media.uploaded", target=name, details={"size_bytes": size, "extension": ext})
    return {"name": name}


@app.post("/api/admin/test", dependencies=ADMIN)
async def send_test(t: TestIn):
    alert = await make_alert(t.name.strip()[:30] or "Аноним", t.message[:250], round(t.amount, 2), load_cfg().fiat)
    delivered = await broadcast(alert)
    pay.record_admin_action("test_alert.sent", details={"delivered": delivered, "overlay_connections": len(clients)})
    return {"ok": True, "overlays_connected": len(clients)}


@app.get("/api/admin/delivery", dependencies=ADMIN)
def delivery_status():
    return pay.alert_delivery_stats()


@app.get("/api/admin/history", dependencies=ADMIN)
def history():
    return [
        {"invoice_id": r["id"], "name": r["donor_name"], "message": r["message"], "amount": r["amount"],
         "fiat": r["currency"], "status": r["status"], "created": r["created_at"], "provider": r["provider"]}
        for r in pay.recent(30)
    ]


@app.get("/api/admin/analytics", dependencies=ADMIN)
def admin_analytics(days: int = 30, currency: str = ""):
    return pay.analytics(days=days, currency=currency or load_cfg().fiat)


def _backup_health(now: float) -> dict:
    try:
        data = json.loads(BACKUP_STATUS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "unknown", "last_success_at": None, "age_seconds": None, "file": None}
    except (OSError, ValueError, TypeError):
        return {"status": "error", "last_success_at": None, "age_seconds": None, "file": None}
    try:
        last_success = float(data["last_success_at"]) if data.get("last_success_at") is not None else None
    except (TypeError, ValueError):
        return {"status": "error", "last_success_at": None, "age_seconds": None, "file": None}
    age = max(0, int(now - last_success)) if last_success is not None else None
    status = data.get("status", "unknown")
    if status == "success" and (age is None or age > 48 * 3600):
        status = "stale" if age is not None else "error"
    return {
        "status": status,
        "last_success_at": last_success,
        "last_attempt_at": data.get("last_attempt_at"),
        "age_seconds": age,
        "file": data.get("backup_file"),
        "size_bytes": data.get("size_bytes"),
        "integrity": data.get("db_integrity"),
        "config_included": data.get("config_included"),
        "error": data.get("error"),
    }


@app.get("/api/admin/system-health", dependencies=ADMIN)
def system_health():
    now = time.time()
    cfg = load_cfg()
    try:
        with closing(pay.db()) as db:
            integrity = db.execute("PRAGMA quick_check").fetchone()[0]
            pending = db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN created_at<? THEN 1 ELSE 0 END) AS stale, "
                "MIN(created_at) AS oldest FROM donations WHERE status='pending'",
                (now - 600,),
            ).fetchone()
            donation_count = db.execute("SELECT COUNT(*) FROM donations").fetchone()[0]
            paid_count = db.execute(
                "SELECT COUNT(*) FROM donations WHERE status IN ('paid','partially_refunded','refunded')"
            ).fetchone()[0]
            refund_count = db.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
            alert_count = db.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0]
        db_size = pay.DB_PATH.stat().st_size if pay.DB_PATH.exists() else 0
        wal_path = Path(str(pay.DB_PATH) + "-wal")
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0
        database = {
            "status": "ok" if integrity == "ok" else "error",
            "integrity": integrity,
            "size_bytes": db_size,
            "wal_size_bytes": wal_size,
            "donations_total": donation_count,
            "donations_paid": paid_count,
            "refunds_total": refund_count,
            "alerts_total": alert_count,
        }
    except Exception as exc:
        log.warning("Проверка базы данных не удалась (%s)", type(exc).__name__)
        pending = {"total": 0, "stale": 0, "oldest": None}
        database = {"status": "error", "integrity": "unavailable", "size_bytes": 0, "wal_size_bytes": 0}

    try:
        usage = shutil.disk_usage(pay.DB_PATH.parent)
        free_percent = round(usage.free / usage.total * 100, 1) if usage.total else 0
        disk_status = "critical" if free_percent < 5 else "warning" if free_percent < 15 else "ok"
        disk = {"status": disk_status, "free_bytes": usage.free, "total_bytes": usage.total,
                "free_percent": free_percent}
    except OSError:
        disk = {"status": "unknown", "free_bytes": None, "total_bytes": None, "free_percent": None}

    outbox = pay.alert_delivery_stats()
    backup = _backup_health(now)
    pending_total = int(pending["total"] or 0)
    stale_pending = int(pending["stale"] or 0)
    oldest_pending = pending["oldest"]
    issues = []
    if database["status"] != "ok":
        issues.append("database_error")
    if disk["status"] == "critical":
        issues.append("disk_critical")
    elif disk["status"] == "warning":
        issues.append("low_disk_space")
    if backup["status"] != "success":
        issues.append("backup_" + str(backup["status"]))
    if stale_pending:
        issues.append("stale_payments")
    if outbox["pending"] and outbox["oldest_pending_seconds"] and outbox["oldest_pending_seconds"] > 300:
        issues.append("stale_alert_queue")
    if not pay.enabled_methods():
        issues.append("no_payment_provider")
    overall = "critical" if database["status"] != "ok" or disk["status"] == "critical" else "warning" if issues else "ok"

    return {
        "status": overall,
        "generated_at": now,
        "database": database,
        "disk": disk,
        "backup": backup,
        "payments": {
            "providers": [m["id"] for m in pay.enabled_methods()],
            "pending": pending_total,
            "stale_pending": stale_pending,
            "oldest_pending_at": oldest_pending,
            "oldest_pending_seconds": max(0, int(now - oldest_pending)) if oldest_pending is not None else None,
        },
        "alerts": outbox,
        "obs": {"status": "connected" if clients else "offline", "connections": len(clients)},
        "tts": {"enabled": bool(cfg.tts_enabled)},
        "issues": issues,
    }


@app.get("/api/admin/deployments", dependencies=ADMIN)
async def deployment_history():
    now = time.time()
    cached = _DEPLOY_HISTORY_CACHE.get("payload")
    if cached is not None and now - _DEPLOY_HISTORY_CACHE.get("fetched_at", 0) < 300:
        return cached
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            response = await client.get(
                "https://api.github.com/repos/wnfy7teen/Alerts-Stage7/actions/runs?per_page=30",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "DonationAlerts-Monitor"},
            )
        if response.status_code != 200:
            raise RuntimeError("github_api_unavailable")
        runs = response.json().get("workflow_runs", [])
        items = []
        for run in runs:
            if run.get("event") != "push" or run.get("head_branch") != "main":
                continue
            run_id = int(run.get("id") or 0)
            if not run_id:
                continue
            items.append({
                "title": str(run.get("display_title") or "Production update")[:160],
                "sha": str(run.get("head_sha") or "")[:7],
                "status": str(run.get("status") or "unknown"),
                "conclusion": run.get("conclusion"),
                "updated_at": run.get("updated_at"),
                "url": f"https://github.com/wnfy7teen/Alerts-Stage7/actions/runs/{run_id}",
            })
            if len(items) >= 10:
                break
        payload = {"available": True, "stale": False, "items": items, "fetched_at": now}
        _DEPLOY_HISTORY_CACHE.update({"fetched_at": now, "payload": payload})
        return payload
    except Exception as exc:
        log.warning("История production-деплоев недоступна (%s)", type(exc).__name__)
        if cached is not None:
            return {**cached, "stale": True}
        return {"available": False, "stale": False, "items": [], "fetched_at": now}


@app.get("/api/admin/transactions", dependencies=ADMIN)
def transactions(page: int = 1, limit: int = 50, status: str = "", provider: str = "", q: str = ""):
    page = max(1, page)
    limit = min(100, max(1, limit))
    return pay.list_transactions(
        limit=limit, offset=(page - 1) * limit, status=status, provider=provider, query=q
    )


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


@app.get("/api/admin/transactions.csv", dependencies=ADMIN)
def transactions_csv(status: str = "", provider: str = "", q: str = ""):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "ID доната", "ID платежа провайдера", "Дата создания", "Дата оплаты",
        "Имя", "Сообщение", "Сумма", "Возвращено", "Количество возвратов", "Валюта", "Статус", "Провайдер",
    ])
    offset = 0
    while offset < 10000:
        batch = pay.list_transactions(
            limit=100, offset=offset, status=status, provider=provider, query=q
        )
        items = batch["items"]
        if not items:
            break
        for row in items:
            writer.writerow([
                _csv_cell(row["id"]), _csv_cell(row["provider_txn_id"]),
                _csv_cell(row["created_at"]), _csv_cell(row["paid_at"]),
                _csv_cell(row["donor_name"]), _csv_cell(row["message"]),
                row["amount"], row["refunded_amount"], row["refund_count"],
                _csv_cell(row["currency"]), _csv_cell(row["status"]), _csv_cell(row["provider"]),
            ])
        offset += len(items)
        if offset >= batch["total"]:
            break
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="donation-transactions.csv"'},
    )


@app.post("/api/admin/replay/{donation_id}", dependencies=ADMIN)
async def replay(donation_id: str):
    row = pay.get_row(donation_id)
    if not row or row["status"] not in ("paid", "partially_refunded"):
        raise HTTPException(404, "Оплаченный донат не найден")
    delivered = await broadcast(await make_alert(row["donor_name"], row["message"], row["amount"], row["currency"]))
    if not delivered:
        raise HTTPException(409, "Оверлей не подключён — откройте OBS и повторите отправку")
    pay.record_admin_action("alert.replayed", target=donation_id)
    return {"ok": True}


@app.get("/api/admin/audit", dependencies=ADMIN)
def admin_audit(page: int = 1, limit: int = 50):
    page = max(1, page)
    limit = min(100, max(1, limit))
    return pay.list_admin_audit(limit=limit, offset=(page - 1) * limit)


app.mount("/media", StaticFiles(directory=MEDIA), name="media")

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=PORT, access_log=False)  # в адресах бывают ключи оверлея и id платежей
