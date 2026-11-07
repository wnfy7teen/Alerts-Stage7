"""
SEO и AEO: мета-теги, Open Graph, JSON-LD, FAQ, robots.txt, sitemap.xml, llms.txt.

Страница доната отрисовывается на сервере: роботы поисковиков и ИИ (GPTBot, ClaudeBot, PerplexityBot и др.)
не выполняют JavaScript, поэтому весь текст, способы оплаты и FAQ должны быть в самом HTML.
Все значения берутся из настроек сайта, ничего не выдумывается.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request

import payments as pay
from security import TURNSTILE_SITE_KEY

STATIC = Path(__file__).parent / "static"
SYMBOLS = {"RUB": "₽", "USD": "$", "EUR": "€", "UAH": "₴", "KZT": "₸"}
COVER_ALT = "Страница доната: поддержать стримера картой, СБП или криптовалютой, сообщение появится на стриме"

AI_BOTS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot", "Claude-User",
           "anthropic-ai", "PerplexityBot", "Google-Extended", "Bingbot", "Applebot-Extended"]
PRIVATE_PREFIXES = ["/admin", "/overlay", "/api/", "/thanks", "/media/", "/crypto-webhook"]
SITE_LABELS = {"twitch.tv": "Twitch", "t.me": "Telegram", "telegram.me": "Telegram", "youtube.com": "YouTube", "vk.com": "VK"}


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:—- ")
    return cut + "…"


def num(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


# ───────────────────────── факты о сайте ─────────────────────────
def base_url(req: Request) -> str:
    return pay.PUBLIC_URL or str(req.base_url).rstrip("/")


def streamer_name() -> str:
    return os.getenv("STREAMER_NAME", "").strip()


def who() -> str:
    n = streamer_name()
    return f"стримера {n}" if n else "стримера"


def same_as() -> list[str]:
    """Ссылки на профили (Twitch, Telegram и т.д.) из SAME_AS. Только https."""
    urls = []
    for item in os.getenv("SAME_AS", "").split(","):
        item = item.strip()
        parts = urlsplit(item)
        if parts.scheme == "https" and parts.hostname:
            urls.append(item)
    return urls


def link_label(url: str) -> str:
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return SITE_LABELS.get(host, host)


def method_phrases() -> list[str]:
    names = {"yookassa": "банковская карта и СБП", "nowpayments": "криптовалюта (адрес и QR-код на сайте)",
             "cryptopay": "Crypto Bot (оплата откроется в Telegram)"}
    return [names[m["id"]] for m in pay.enabled_methods()]


def methods_short() -> str:
    ids = {m["id"] for m in pay.enabled_methods()}
    parts = []
    if "yookassa" in ids:
        parts.append("карта, СБП")
    if ids & {"nowpayments", "cryptopay"}:
        parts.append("криптовалюта")
    return ", ".join(parts)


def intro_text() -> str:
    custom = os.getenv("ABOUT_TEXT", "").strip()
    if custom:
        return custom
    n = streamer_name()
    if n:
        return (f"{n} — стример на Twitch. Здесь зрители могут поддержать {n} донатом: выберите сумму и способ оплаты, "
                "напишите сообщение, и оно появится на стриме.")
    return "Здесь зрители могут поддержать стримера донатом: выберите сумму и способ оплаты, напишите сообщение, и оно появится на стриме."


def faq(cfg) -> list[tuple[str, str]]:
    sym = SYMBOLS.get(cfg.fiat, cfg.fiat)
    items = [
        (f"Как поддержать {who()}?",
         f"Выберите сумму (от {num(cfg.min_amount)} {sym}) и способ оплаты, при желании напишите имя и сообщение и нажмите кнопку оплаты. "
         "После подтверждения платежа алерт появится на стриме."),
    ]
    phrases = method_phrases()
    items.append(("Какие способы оплаты доступны?",
                  ("Доступно: " + "; ".join(phrases) + ".") if phrases else "Способы оплаты пока не подключены."))
    items.append(("Какая минимальная сумма доната?",
                  f"Минимальная сумма — {num(cfg.min_amount)} {sym}, максимальная — {num(cfg.max_amount)} {sym}."))
    items.append(("Когда сообщение появится на стриме?",
                  "Сразу после подтверждения платежа: при оплате картой или СБП обычно в течение нескольких секунд. "
                  "Для криптовалюты нужно дождаться подтверждения платежа в сети."))
    if cfg.tts_enabled:
        voice = f"Сообщения от {num(cfg.tts_min_amount)} {sym} озвучиваются голосом в эфире."
    else:
        voice = "Сообщение показывается на экране текстом."
    big = [t for t in cfg.tiers if t.min_amount > 0]
    if big:
        voice += f" Донаты от {num(min(t.min_amount for t in big))} {sym} показываются с особым оформлением."
    items.append(("Будет ли сообщение озвучено?", voice))
    items.append(("Нужна ли регистрация?",
                  "Нет. Имя и сообщение указывать необязательно: без имени донат подписывается как «Аноним»."))
    safe = []
    ids = {m["id"] for m in pay.enabled_methods()}
    if "yookassa" in ids:
        safe.append("Данные карты вводятся в форме платёжного сервиса ЮKassa, сайт их не получает и не хранит.")
    if "nowpayments" in ids:
        safe.append("Криптовалюту вы отправляете со своего кошелька, доступа к нему у сайта нет.")
    if "cryptopay" in ids:
        safe.append("Оплата через Crypto Bot проходит в Telegram.")
    safe.append("Сайт хранит только сумму, имя, сообщение и номер платежа.")
    items.append(("Безопасно ли платить?", " ".join(safe)))
    return items


# ───────────────────────── мета-данные страницы ─────────────────────────
def page_meta(req: Request, cfg) -> dict:
    base, name = base_url(req), streamer_name()
    sym = SYMBOLS.get(cfg.fiat, cfg.fiat)
    title = clip(f"Донат стримеру {name}: {methods_short()}" if name and methods_short() else
                 (f"Донат стримеру {name}" if name else "Донат стримеру"), 60)
    parts = [f"Поддержать {who()} донатом"]
    if methods_short():
        parts.append(f": {methods_short()}")
    desc = "".join(parts) + f". Сообщение появится на стриме. От {num(cfg.min_amount)} {sym}."
    return {
        "base": base, "url": base + "/", "title": title, "description": clip(desc, 160),
        "og_title": clip(f"Поддержать {name} донатом" if name else "Поддержать стримера донатом", 90),
        "image": base + "/og-cover.png", "site_name": name or "Донаты",
    }


def jsonld(meta: dict, cfg, questions: list[tuple[str, str]], modified: str) -> str:
    base, name = meta["base"], streamer_name() or "Стример"
    sym_code = cfg.fiat
    person = {
        "@type": "Person", "@id": base + "/#streamer", "name": name, "url": base + "/",
        "potentialAction": {
            "@type": "DonateAction", "name": "Поддержать донатом",
            "target": {"@type": "EntryPoint", "urlTemplate": base + "/",
                       "actionPlatform": ["https://schema.org/DesktopWebPlatform", "https://schema.org/MobileWebPlatform"]},
            "priceSpecification": {"@type": "PriceSpecification", "minPrice": cfg.min_amount, "maxPrice": cfg.max_amount,
                                   "priceCurrency": sym_code},
        },
    }
    links = same_as()
    if links:
        person["sameAs"] = links
    graph = [
        {"@type": "WebSite", "@id": base + "/#website", "url": base + "/", "name": meta["site_name"], "inLanguage": "ru-RU",
         "publisher": {"@id": base + "/#streamer"}},
        person,
        {"@type": "WebPage", "@id": base + "/#page", "url": base + "/", "name": meta["title"], "description": meta["description"],
         "inLanguage": "ru-RU", "isPartOf": {"@id": base + "/#website"}, "about": {"@id": base + "/#streamer"},
         "primaryImageOfPage": {"@type": "ImageObject", "url": meta["image"], "width": 1200, "height": 630},
         "dateModified": modified},
        {"@type": "FAQPage", "@id": base + "/#faq", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in questions]},
    ]
    # «<» экранируется, чтобы текст из настроек не мог закрыть тег <script>
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("<", "\\u003c")


def last_modified() -> str:
    files = [STATIC / "donate.html", Path(os.getenv("CONFIG_PATH") or Path(__file__).parent / "config.json")]
    ts = max((f.stat().st_mtime for f in files if f.exists()), default=0)
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def head_html(meta: dict, noindex: bool, ld: str) -> str:
    robots = "noindex, nofollow" if noindex else "index, follow, max-image-preview:large"
    t, d, u, img = esc(meta["title"]), esc(meta["description"]), esc(meta["url"]), esc(meta["image"])
    og_t, alt, site = esc(meta["og_title"]), esc(COVER_ALT), esc(meta["site_name"])
    lines = [
        f"<title>{t}</title>",
        f'<meta name="description" content="{d}">',
        f'<meta name="robots" content="{robots}">',
        f'<link rel="canonical" href="{u}">',
        '<meta name="theme-color" content="#0593C6">',
        '<link rel="icon" href="/favicon.svg" type="image/svg+xml">',
        '<link rel="icon" href="/favicon.ico" sizes="any">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:title" content="{og_t}">',
        f'<meta property="og:description" content="{d}">',
        f'<meta property="og:url" content="{u}">',
        f'<meta property="og:image" content="{img}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        f'<meta property="og:image:alt" content="{alt}">',
        f'<meta property="og:site_name" content="{site}">',
        '<meta property="og:locale" content="ru_RU">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{og_t}">',
        f'<meta name="twitter:description" content="{d}">',
        f'<meta name="twitter:image" content="{img}">',
        f'<meta name="twitter:image:alt" content="{alt}">',
        f'<script type="application/ld+json">{ld}</script>',
    ]
    return "\n".join(lines)


# ───────────────────────── серверная отрисовка страницы доната ─────────────────────────
def render_donate(template: str, req: Request, cfg) -> str:
    meta = page_meta(req, cfg)
    sym = SYMBOLS.get(cfg.fiat, cfg.fiat)
    methods = pay.enabled_methods()
    questions = faq(cfg)
    noindex = "donation" in req.query_params  # адреса возврата после оплаты в поиск не попадают
    name = streamer_name()
    default_amount = max(cfg.min_amount, 300)
    presets = [v for v in (100, 300, 500, 1000) if v >= cfg.min_amount]

    chips = "".join(
        f'<button type="button" data-v="{v}" aria-pressed="{"true" if v == default_amount else "false"}">{v} {esc(sym)}</button>'
        for v in presets)
    method_html = "".join(
        f'<label class="method"><input type="radio" name="method" value="{esc(m["id"])}"{" checked" if i == 0 else ""}>'
        f'<span class="card"><span>{esc(m["label"])}</span><small>{esc(m["hint"])}</small></span></label>'
        for i, m in enumerate(methods))
    faq_html = "".join(
        f'<details class="faq-item"><summary>{esc(q)}<span class="faq-chevron" aria-hidden="true"></span></summary>'
        f'<div class="faq-answer"><div><p>{esc(a)}</p></div></div></details>'
        for q, a in questions)
    links = same_as()
    links_html = ""
    if links:
        links_html = ('<nav aria-label="Профили стримера"><ul class="links">'
                      + "".join(f'<li><a href="{esc(u)}" rel="noopener noreferrer">{esc(link_label(u))}</a></li>' for u in links)
                      + "</ul></nav>")
    cfg_json = json.dumps({
        "min_amount": cfg.min_amount, "max_amount": cfg.max_amount, "symbol": sym, "max_message_len": cfg.max_message_len,
        "streamer": name, "methods": methods, "turnstile_site_key": TURNSTILE_SITE_KEY,
    }, ensure_ascii=False).replace("<", "\\u003c")

    btn_text = f"Поддержать на {num(default_amount)} {sym}" if methods else "Поддержать"
    subs = {
        "{{HEAD}}": head_html(meta, noindex, jsonld(meta, cfg, questions, last_modified())),
        "{{H1}}": esc(f"Поддержать {name}" if name else "Поддержать стримера"),
        "{{INTRO}}": esc(intro_text()),
        "{{AMOUNT}}": esc(num(default_amount)), "{{MIN}}": esc(num(cfg.min_amount)), "{{SYMBOL}}": esc(sym),
        "{{MINHINT}}": esc(f"Минимум {num(cfg.min_amount)} {sym}"),
        "{{CHIPS}}": chips, "{{METHODS}}": method_html, "{{MAXLEN}}": esc(cfg.max_message_len),
        "{{COUNT}}": esc(f"0 / {cfg.max_message_len}"), "{{BTN}}": esc(btn_text),
        "{{BTN_DISABLED}}": "" if methods else " disabled",
        "{{ERR}}": "" if methods else "Приём донатов пока не настроен.",
        "{{TS_CLASS}}": ' class="ts-on"' if TURNSTILE_SITE_KEY else "",
        "{{FAQ}}": faq_html, "{{LINKS}}": links_html, "{{CFG}}": cfg_json,
    }
    out = template
    for token, value in subs.items():
        out = out.replace(token, value)
    return out


# ───────────────────────── robots.txt, sitemap.xml, llms.txt ─────────────────────────
def robots_txt(req: Request) -> str:
    rules = "\n".join(f"Disallow: {p}" for p in PRIVATE_PREFIXES if p != "/crypto-webhook")
    blocks = ["User-agent: *\nAllow: /\n" + rules]
    for bot in AI_BOTS:  # у каждого именованного робота своя группа, поэтому закрытые разделы повторяются
        blocks.append(f"User-agent: {bot}\nAllow: /\n{rules}")
    return "\n\n".join(blocks) + f"\n\nSitemap: {base_url(req)}/sitemap.xml\n"


def sitemap_xml(req: Request) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"  <url><loc>{esc(base_url(req))}/</loc><lastmod>{last_modified()}</lastmod></url>\n</urlset>\n")


def llms_txt(req: Request, cfg) -> str:
    base, name = base_url(req), streamer_name()
    sym = SYMBOLS.get(cfg.fiat, cfg.fiat)
    title = f"Донаты стримера {name}" if name else "Донаты стримера"
    lines = [f"# {title}", "", f"> {intro_text()}", "",
             f"Сайт: {base}/. Язык: русский. Валюта: {cfg.fiat}. Обновлено: {last_modified()}.", "",
             f"- Минимальная сумма: {num(cfg.min_amount)} {sym}, максимальная: {num(cfg.max_amount)} {sym}.",
             "- Способы оплаты: " + ("; ".join(method_phrases()) if method_phrases() else "пока не подключены") + ".",
             "- Регистрация не нужна, имя и сообщение указывать необязательно.", ""]
    lines += [f"- **{q}** {a}" for q, a in faq(cfg)]
    lines += ["", "## Страницы", "",
              f"- [Поддержать {name or 'стримера'} донатом]({base}/): форма доната (сумма, способ оплаты, имя, сообщение)",
              f"- [sitemap.xml]({base}/sitemap.xml): карта сайта"]
    links = same_as()
    if links:
        lines += ["", "## Профили", ""] + [f"- [{link_label(u)}]({u})" for u in links]
    return "\n".join(lines) + "\n"
