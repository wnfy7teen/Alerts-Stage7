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


ICONS = {
    "yookassa": '<rect x="2.5" y="5" width="19" height="14" rx="3"/><path d="M2.5 10h19M6.500 15h4"/>',
    "nowpayments": '<circle cx="12" cy="12" r="9"/><path d="M9.500 9.500c.6-1 1.500-1.500 2.500-1.500 1.500 0 2.500.8 2.500 2s-1 1.700-2.500 2-2.500.8-2.500 2 1 2 2.500 2c1 0 1.900-.5 2.500-1.500M12 6v2M12 16v2"/>',
    "cryptopay": '<path d="M21.500 3 2.500 10.500l7 2.800 2.800 7.200z"/><path d="M9.500 13.300 21.500 3"/>',
}


def icon(method_id: str) -> str:
    return ('<svg class="opt-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{ICONS.get(method_id, "")}</svg>')


def steps_html(cfg) -> str:
    sym = SYMBOLS.get(cfg.fiat, cfg.fiat)
    short = methods_short()
    items = [
        ("Выберите сумму", f"От {num(cfg.min_amount)} {sym}. Быстрые суммы под рукой, можно ввести свою."),
        ("Оплатите", (short[:1].upper() + short[1:] + ". Платёж подтверждается автоматически.") if short else "Платёж подтверждается автоматически."),
        ("Появитесь в эфире", "Алерт с вашим именем и сообщением показывается сразу после подтверждения платежа."),
    ]
    return '<ol class="steps-list">' + "".join(f"<li><strong>{esc(t)}</strong><span>{esc(d)}</span></li>" for t, d in items) + "</ol>"


def top_links_html() -> str:
    """Постоянные публичные соцсети стримера в шапке; не зависят от SAME_AS."""
    return (
        '<nav class="social-nav" aria-label="Социальные сети стримера"><ul class="toplinks">'
        '<li><a class="social-link social-twitch" href="https://www.twitch.tv/vanesso4kaaa" '
        'aria-label="Twitch" title="Twitch" target="_blank" rel="noopener noreferrer">'
        '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        '<path d="M4 3 2.5 6.5V20h5v3l3-3h4l7-7V3H4Zm15 9.5-4 4h-4l-2.5 2.5V16H5V5h14v7.5ZM15 7v5h2V7h-2Zm-5 0v5h2V7h-2Z"/></svg>'
        '<span class="social-label">Twitch</span><span class="sr-only">Twitch</span></a></li>'
        '<li><a class="social-link social-telegram" href="https://t.me/vanesso44ka" '
        'aria-label="Telegram" title="Telegram" target="_blank" rel="noopener noreferrer">'
        '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        '<path d="M21.7 3.3 18.5 20c-.2 1.2-.9 1.5-1.8.9l-5-3.7-2.4 2.3c-.3.3-.6.6-1.2.6l.4-5.1 9.3-8.4c.4-.4-.1-.6-.6-.2L5.7 13.7.8 12.2c-1.1-.3-1.1-1 .2-1.5L20.1 3.2c.9-.3 1.8.2 1.6 1.1Z"/></svg>'
        '<span class="social-label">Telegram</span><span class="sr-only">Telegram</span></a></li>'
        '</ul></nav>'
    )


def intro_text() -> str:
    custom = os.getenv("ABOUT_TEXT", "").strip()
    if custom:
        return custom
    n = streamer_name()
    if n:
        return (f"Поддержите стримера {n} :3 выберите сумму и способ оплаты, "
                "напишите сообщение - оно появится на стриме.")
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
        '<meta name="msvalidate.01" content="1656712732A8B6F806D5737CCDB19855">',
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
        f'<button type="button" class="chip" data-v="{v}" aria-pressed="{"true" if v == default_amount else "false"}">{v} {esc(sym)}</button>'
        for v in presets)
    method_html = "".join(
        f'<label class="method"><input type="radio" name="method" value="{esc(m["id"])}"{" checked" if i == 0 else ""}>'
        f'<span class="opt"><span class="dot" aria-hidden="true"></span><span><span class="opt-title">{esc(m["label"])}</span>'
        f'<span class="opt-hint">{esc(m["hint"])}</span></span>{icon(m["id"])}</span></label>'
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
    facts = [f"От {num(cfg.min_amount)} {sym}", "Без регистрации"]
    if methods_short():
        short = methods_short()
        facts.append(short[:1].upper() + short[1:])
    facts_html = '<ul class="facts" aria-label="Коротко о донатах">' + "".join(f"<li>{esc(f)}</li>" for f in facts) + "</ul>"
    cfg_json = json.dumps({
        "min_amount": cfg.min_amount, "max_amount": cfg.max_amount, "symbol": sym, "max_message_len": cfg.max_message_len,
        "streamer": name, "methods": methods, "turnstile_site_key": TURNSTILE_SITE_KEY,
        "title_template": cfg.title_template, "tiers": [{"min_amount": t.min_amount, "color": t.color} for t in cfg.tiers],
    }, ensure_ascii=False).replace("<", "\\u003c")

    btn_text = f"Поддержать на {num(default_amount)} {sym}" if methods else "Поддержать"
    subs = {
        "{{HEAD}}": head_html(meta, noindex, jsonld(meta, cfg, questions, last_modified())),
        "{{H1_A}}": "Поддержать", "{{H1_B}}": esc(name or "стримера"),
        "{{H1_CLASS}}": "n-l" if len(name) > 15 else ("n-m" if len(name) > 10 else ""),
        "{{BRAND}}": esc(name or "Донаты"), "{{TOPLINKS}}": top_links_html(), "{{STEPS}}": steps_html(cfg),
        "{{INTRO}}": esc(intro_text()), "{{FACTS}}": facts_html,
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
    # Replace longer placeholders first: {{MIN}} is a prefix of {{MINHINT}}.
    for token, value in sorted(subs.items(), key=lambda item: len(item[0]), reverse=True):
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
