"""
Донат-алерты для Twitch через Crypto Bot (Crypto Pay API).

Поток: страница /donate -> счёт в Crypto Pay -> оплата -> алерт на /overlay (OBS).
Оплату ловим двумя способами: опросом getInvoices (работает всегда) и webhook (мгновенно, по желанию).
"""
import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import edge_tts
import uvicorn
from dotenv import load_dotenv
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import payments as pay
import security as sec

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("donations")

BASE = Path(__file__).parent
MEDIA = Path(os.getenv("MEDIA_DIR") or BASE / "media")
TTS_DIR = MEDIA / "tts"
STATIC = BASE / "static"
CFG_PATH = Path(os.getenv("CONFIG_PATH") or BASE / "config.json")
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
    color: str = "#9146FF"
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
    text_color: str = "#FFFFFF"
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
            Tier(min_amount=1000, animation="zoom", color="#F5A623", duration=12),
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
                _cfg = Config(**json.loads(CFG_PATH.read_text(encoding="utf-8")))
            except Exception as e:
                log.error("config.json повреждён (%s), использую значения по умолчанию", e)
                _cfg = Config()
        else:
            save_cfg(Config())
    return _cfg  # type: ignore[return-value]


# ───────────────────────── алерты ─────────────────────────
clients: set[WebSocket] = set()


async def broadcast(alert: dict) -> None:
    for ws in list(clients):
        try:
            await ws.send_json(alert)
        except Exception:
            clients.discard(ws)


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


async def make_alert(name: str, message: str, amount: float, fiat: str) -> dict:
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
        "id": uuid.uuid4().hex,
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


async def on_paid(row) -> None:
    """Вызывается ровно один раз, когда платёж подтверждён провайдером."""
    await broadcast(await make_alert(row["donor_name"], row["message"], row["amount"], row["currency"]))


async def poller() -> None:
    tick = 0
    while True:
        try:
            await pay.poll_once(tick)
        except Exception as e:
            log.warning("Опрос платежей не удался: %s", e)
        tick += 1
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    pay.init_db()
    pay.set_paid_handler(on_paid)
    load_cfg()
    for f in TTS_DIR.glob("*.mp3"):  # чистим старую озвучку
        if time.time() - f.stat().st_mtime > 86400:
            f.unlink(missing_ok=True)
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
    if sec.FORCE_HTTPS and pay.PUBLIC_URL.startswith("https://") and sec.forwarded_proto(request) == "http" and path != "/api/health":
        target = pay.PUBLIC_URL + path + ("?" + request.url.query if request.url.query else "")  # адрес из настроек, не из Host
        return RedirectResponse(target, status_code=308)
    limit = 21 * 1024 * 1024 if path == "/api/admin/upload" else 1024 * 1024
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > limit:
        return JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)
    if path.startswith("/api/webhooks") or path == "/crypto-webhook":
        if not hook_limiter.allow(ip):
            return JSONResponse({"detail": "Слишком часто"}, status_code=429)
    elif path.startswith("/api/") and not api_limiter.allow(ip):
        return JSONResponse({"detail": "Слишком много запросов"}, status_code=429)
    if (path == "/admin" or path.startswith("/api/admin")) and sec.ADMIN_ALLOWED and not sec.ip_in(ip, sec.ADMIN_ALLOWED):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
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


donate_limiter = pay.RateLimiter(limit=5, window=600)  # 5 попыток за 10 минут на IP
IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class DonationIn(BaseModel):
    method: Literal["yookassa", "nowpayments", "cryptopay"]
    amount: float = Field(gt=0, lt=10_000_000)
    name: str = Field(default="", max_length=200)
    message: str = Field(default="", max_length=5000)
    website: str = Field(default="", max_length=200)  # honeypot: человек это поле не видит и не заполняет
    turnstile_token: str = Field(default="", max_length=2048)


@app.get("/")
def root():
    return RedirectResponse("/donate")


def html_page(name: str, kind: str, req: Request) -> HTMLResponse:
    """Отдаёт страницу, подставляя одноразовый nonce в <script> и <style> (inline-код без nonce браузер не выполнит)."""
    nonce = secrets.token_urlsafe(16)
    html = (STATIC / name).read_text(encoding="utf-8")
    html = html.replace("<script>", f'<script nonce="{nonce}">').replace("<style>", f'<style nonce="{nonce}">')
    header = "Content-Security-Policy-Report-Only" if sec.CSP_REPORT_ONLY else "Content-Security-Policy"
    return HTMLResponse(html, headers={header: sec.csp(kind, nonce, req.headers.get("host", ""))})


@app.get("/donate")
def donate_page(req: Request):
    return html_page("donate.html", "donate", req)


@app.get("/overlay")
def overlay_page(req: Request):
    return html_page("overlay.html", "overlay", req)


@app.get("/thanks")
def thanks(req: Request):
    return html_page("thanks.html", "thanks", req)


csp_limiter = pay.RateLimiter(limit=30, window=60)


@app.post("/api/csp-report", status_code=204)
async def csp_report(req: Request):
    """Сюда браузер присылает нарушения CSP: помогает отладить политику на реальной странице оплаты."""
    if csp_limiter.allow(sec.client_ip(req)):
        text = (await req.body())[:1500].decode("utf-8", "replace")
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
async def create_donation(body: DonationIn, req: Request):
    """Создаёт платёж. Заголовок Idempotency-Key защищает от двойного списания при повторной отправке."""
    if not donate_limiter.allow(sec.client_ip(req)):
        raise HTTPException(429, "Слишком много попыток, подождите несколько минут")
    sec.check_origin(req)
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
        body = await req.json()
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
        await pay.handle_nowpayments_webhook(await req.body(), req.headers.get("x-nowpayments-sig", ""))
    except pay.PaymentError as e:
        raise _err(e) from None
    return {"ok": True}


@app.post("/api/webhooks/cryptopay")
@app.post("/crypto-webhook")  # прежний адрес оставлен, чтобы не перенастраивать Crypto Pay
async def webhook_cryptopay(req: Request):
    try:
        await pay.handle_cryptopay_webhook(await req.body(), req.headers.get("crypto-pay-api-signature", ""))
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
    return {"ok": True}


@app.get("/api/admin/links", dependencies=ADMIN)
def links():
    return {"overlay": f"http://localhost:{PORT}/overlay?key={OVERLAY_KEY}"}


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
    return {"name": name}


@app.post("/api/admin/test", dependencies=ADMIN)
async def send_test(t: TestIn):
    alert = await make_alert(t.name.strip()[:30] or "Аноним", t.message[:250], round(t.amount, 2), load_cfg().fiat)
    await broadcast(alert)
    return {"ok": True, "overlays_connected": len(clients)}


@app.get("/api/admin/history", dependencies=ADMIN)
def history():
    return [
        {"invoice_id": r["id"], "name": r["donor_name"], "message": r["message"], "amount": r["amount"],
         "fiat": r["currency"], "status": r["status"], "created": r["created_at"], "provider": r["provider"]}
        for r in pay.recent(30)
    ]


@app.post("/api/admin/replay/{donation_id}", dependencies=ADMIN)
async def replay(donation_id: str):
    row = pay.get_row(donation_id)
    if not row or row["status"] != "paid":
        raise HTTPException(404, "Оплаченный донат не найден")
    await broadcast(await make_alert(row["donor_name"], row["message"], row["amount"], row["currency"]))
    return {"ok": True}


app.mount("/media", StaticFiles(directory=MEDIA), name="media")

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=PORT, access_log=False)  # в адресах бывают ключи оверлея и id платежей
