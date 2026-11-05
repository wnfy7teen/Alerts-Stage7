import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest

import payments as pay
import seo
from helpers import donate

PAGE_KEYS = ("title", "description")


class Page(HTMLParser):
    """Разбирает HTML так же, как это сделал бы робот без JavaScript."""

    def __init__(self):
        super().__init__()
        self.h = []
        self.meta, self.links, self.ld, self.text = {}, {}, [], []
        self._tag, self._ld = None, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self._tag = tag
        if tag == "meta":
            self.meta[a.get("property") or a.get("name")] = a.get("content")
        elif tag == "link":
            self.links[a.get("rel")] = a.get("href")
        elif tag == "script" and a.get("type") == "application/ld+json":
            self._ld = True
        if tag in ("h1", "h2", "h3"):
            self.h.append([tag, ""])

    def handle_endtag(self, tag):
        self._tag = None
        if tag == "script":
            self._ld = False

    def handle_data(self, data):
        if self._ld:
            self.ld.append(data)
        elif self._tag in ("h1", "h2", "h3") and self.h:
            self.h[-1][1] += data
        elif self._tag == "title":
            self.meta["title"] = data
        self.text.append(data)


@pytest.fixture()
def page(client, monkeypatch):
    monkeypatch.setenv("STREAMER_NAME", "vanesso4ka")
    monkeypatch.setenv("SAME_AS", "https://www.twitch.tv/vanesso4ka, http://insecure.example, javascript:alert(1)")
    r = client.get("/", headers={"user-agent": "GPTBot"})
    p = Page()
    p.feed(r.text)
    return r, p


def test_html_is_complete_without_javascript(page):
    r, p = page
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    body = " ".join(p.text)
    assert "{{" not in r.text, "в страницу попали неподставленные значения"
    assert "vanesso4ka — стример на Twitch" in body                      # сущность описана в первом абзаце
    assert "Карта или СБП" in body and "Криптовалюта" in body              # способы оплаты видны роботу
    assert "Минимум 50 ₽" in body and "Вопросы и ответы" in body


def test_title_description_canonical(page):
    _, p = page
    assert len(p.meta["title"]) <= 60 and "vanesso4ka" in p.meta["title"]
    assert len(p.meta["description"]) <= 160
    assert p.links["canonical"] == "https://example.test/"
    assert p.meta["robots"].startswith("index")


def test_exactly_one_h1_and_ordered_headings(page):
    _, p = page
    levels = [h[0] for h in p.h]
    assert levels.count("h1") == 1 and levels[0] == "h1"
    assert p.h[0][1] == "Поддержать vanesso4ka"
    assert "h2" in levels and "h3" in levels and levels.index("h2") < levels.index("h3")


def test_open_graph_and_twitter_card(page):
    _, p = page
    m = p.meta
    assert m["og:type"] == "website" and m["og:locale"] == "ru_RU" and m["og:site_name"] == "vanesso4ka"
    assert m["og:url"] == "https://example.test/" and m["og:image"] == "https://example.test/og-cover.png"
    assert m["og:image:width"] == "1200" and m["og:image:height"] == "630" and m["og:image:alt"]
    assert m["og:title"] and m["og:description"] == m["description"]
    assert m["twitter:card"] == "summary_large_image" and m["twitter:image"] == m["og:image"]


def test_json_ld_matches_visible_content(page):
    _, p = page
    graph = json.loads("".join(p.ld))["@graph"]
    kinds = {n["@type"] for n in graph}
    assert {"WebSite", "Person", "WebPage", "FAQPage"} <= kinds
    person = next(n for n in graph if n["@type"] == "Person")
    assert person["potentialAction"]["@type"] == "DonateAction"
    assert person["sameAs"] == ["https://www.twitch.tv/vanesso4ka"]           # только https-ссылки
    faq = next(n for n in graph if n["@type"] == "FAQPage")
    visible = [h[1] for h in p.h if h[0] == "h3"]
    assert [q["name"] for q in faq["mainEntity"]] == visible                  # разметка совпадает с тем, что видит человек


def test_template_injection_is_escaped(client, monkeypatch):
    monkeypatch.setenv("STREAMER_NAME", '</script><img src=x onerror=alert(1)>')
    html = client.get("/").text
    assert "<img src=x" not in html and "</script><img" not in html


def test_donation_return_page_is_noindex(client):
    r = client.get("/?donation=" + "a" * 32)
    assert 'content="noindex, nofollow"' in r.text and 'rel="canonical" href="https://example.test/"' in r.text


def test_old_address_redirects_permanently(client):
    r = client.get("/donate?donation=abc", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/?donation=abc"


def test_robots_txt(client):
    txt = client.get("/robots.txt").text
    for bot in seo.AI_BOTS:
        group = re.search(rf"User-agent: {re.escape(bot)}\n(.*?)(?:\n\n|$)", txt, re.S).group(1)
        assert "Allow: /" in group and "Disallow: /admin" in group and "Disallow: /overlay" in group, bot
    assert "Disallow: /api/" in txt and "Sitemap: https://example.test/sitemap.xml" in txt


def test_sitemap_xml(client):
    root = ET.fromstring(client.get("/sitemap.xml").text)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    assert [u.text for u in root.findall("s:url/s:loc", ns)] == ["https://example.test/"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", root.find("s:url/s:lastmod", ns).text)


def test_llms_txt_follows_the_standard(client, monkeypatch):
    monkeypatch.setenv("STREAMER_NAME", "vanesso4ka")
    r = client.get("/llms.txt")
    lines = r.text.splitlines()
    assert "text/plain" in r.headers["content-type"] and lines[0].startswith("# ") and lines[2].startswith("> ")
    assert "## Страницы" in r.text and "(https://example.test/)" in r.text and "Минимальная сумма: 50 ₽" in r.text


def test_facts_follow_the_settings(client):
    import server
    cfg = server.load_cfg()
    old = (cfg.min_amount, cfg.tts_enabled)
    cfg.min_amount, cfg.tts_enabled = 120, False
    try:
        html = client.get("/").text
        assert "Минимум 120 ₽" in html and "Сообщение показывается на экране текстом." in html
    finally:
        cfg.min_amount, cfg.tts_enabled = old


def test_private_pages_are_not_indexable(client):
    auth = ("admin", "pw-test-1234567")
    for path, kw in (("/overlay", {}), ("/thanks", {}), ("/admin", {"auth": auth}), ("/api/public-config", {})):
        r = client.get(path, **kw)
        assert r.headers["x-robots-tag"] == "noindex, nofollow", path
    assert "x-robots-tag" not in client.get("/").headers
    assert 'name="robots" content="noindex, nofollow"' in client.get("/overlay").text


def test_brand_assets():
    static = Path(seo.STATIC)
    png = (static / "og-cover.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) <= 300 * 1024
    assert int.from_bytes(png[16:20], "big") == 1200 and int.from_bytes(png[20:24], "big") == 630
    ET.fromstring((static / "favicon.svg").read_text(encoding="utf-8"))


def test_asset_routes(client):
    cover = client.get("/og-cover.png")
    assert cover.status_code == 200 and cover.headers["content-type"] == "image/png" and "max-age" in cover.headers["cache-control"]
    assert client.get("/favicon.svg").headers["content-type"].startswith("image/svg+xml")
    assert client.get("/favicon.ico").status_code == 200


def test_bots_get_html_not_a_block(client):
    for ua in ("GPTBot", "ClaudeBot", "PerplexityBot", "TelegramBot (like TwitterBot)", "Googlebot"):
        r = client.get("/", headers={"user-agent": ua})
        assert r.status_code == 200 and "<h1" in r.text, ua


def test_no_payment_state_leaks_into_seo_pages(client, fake):
    did = donate(client).json()["donation_id"]
    assert did not in client.get("/sitemap.xml").text + client.get("/llms.txt").text + client.get("/robots.txt").text
    assert pay.get_row(did)
