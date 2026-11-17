"""Проверки дизайна: фирменные цвета, контраст WCAG, шкала шрифтов, доступность вёрстки, свои шрифты и стили."""
import math
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "static" / "assets"
STATIC = ASSETS.parent
TOKENS = (ASSETS / "tokens.css").read_text(encoding="utf-8")
APP_CSS = (ASSETS / "app.css").read_text(encoding="utf-8")


def variables(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})\b", block))


LIGHT = variables(TOKENS.split("@media (prefers-color-scheme: dark)")[0])
DARK = LIGHT  # публичный интерфейс всегда остаётся светлым, независимо от системной темы


def luminance(h: str) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# (текст или значок, фон, минимум) по ролям токенов
PAIRS = [
    ("c-ink", "c-bg", 7), ("c-ink", "c-surface", 7), ("c-ink", "c-surface-soft", 7), ("c-ink", "c-pink", 7), ("c-ink", "c-pink-soft", 7),
    ("c-ink-muted", "c-bg", 4.5), ("c-ink-muted", "c-surface", 4.5), ("c-ink-muted", "c-surface-soft", 4.5), ("c-ink-muted", "c-pink-soft", 4.5),
    ("c-on-action", "c-action", 4.5), ("c-on-action", "c-action-hover", 4.5),
    ("c-link", "c-bg", 4.5), ("c-link", "c-surface", 4.5), ("c-link", "c-pink-soft", 4.5), ("c-link", "c-surface-soft", 4.5),
    ("c-success", "c-success-bg", 4.5), ("c-error", "c-error-bg", 4.5), ("c-warning", "c-warning-bg", 4.5),
    ("c-success", "c-surface", 4.5), ("c-error", "c-surface", 4.5), ("c-warning", "c-surface", 4.5),
    # рамки полей, значки и кольцо фокуса: 3:1 (WCAG 1.4.11)
    ("c-outline", "c-bg", 3), ("c-outline", "c-surface", 3),
    ("c-night-ink", "c-night", 7), ("c-night-muted", "c-night", 4.5), ("c-night-accent", "c-night", 4.5), ("c-night-pink", "c-night", 4.5),
    ("c-control", "c-surface", 3), ("c-control", "c-bg", 3), ("c-focus", "c-bg", 3), ("c-focus", "c-surface", 3), ("c-brand", "c-bg", 3),
]


@pytest.mark.parametrize("theme,palette", [("светлая", LIGHT), ("тёмная", DARK)])
def test_contrast_wcag(theme, palette):
    for fg, bg, need in PAIRS:
        ratio = contrast(palette[fg], palette[bg])
        assert ratio >= need, f"{theme} тема: {fg} на {bg} = {ratio:.2f}:1, нужно {need}:1"


def test_brand_colors_are_used_exactly():
    assert LIGHT["brand-blue"].upper() == "#0593C6" and LIGHT["brand-white"].upper() == "#FCFFFF" and LIGHT["brand-pink"].upper() == "#F8C8DC"
    assert LIGHT["c-bg"].upper() == "#FCFFFF" and LIGHT["c-brand"].upper() == "#0593C6" and LIGHT["c-pink"].upper() == "#F8C8DC"


def test_pure_brand_blue_is_not_used_for_text_or_buttons():
    """#0593C6 на белом даёт около 3,5:1: для обычного текста и кнопок с белым текстом берётся более тёмный голубой."""
    assert contrast(LIGHT["brand-blue"], LIGHT["brand-white"]) < 4.5
    assert LIGHT["c-action"].upper() != "#0593C6" and LIGHT["c-link"].upper() != "#0593C6"


def test_type_scale_is_a_major_third():
    steps = [float(v) for v in re.findall(r"--fs-(?:-1|0|1|2|3|4|5):\s*([\d.]+)rem", TOKENS)]
    assert len(steps) == 7
    for a, b in zip(steps, steps[1:]):
        assert math.isclose(b / a, 1.25, rel_tol=0.01), (a, b)


def test_tap_target_and_measure_tokens():
    assert float(re.search(r"--tap:\s*([\d.]+)rem", TOKENS).group(1)) * 16 >= 44
    assert 60 <= int(re.search(r"--measure:\s*(\d+)ch", TOKENS).group(1)) <= 75
    for sel in (r"\.btn \{[^}]*min-height: 3\.25rem", r"\.chip \{[^}]*min-height: var\(--tap\)", r"\.input \{[^}]*min-height: 3rem"):
        assert re.search(sel, APP_CSS), sel


def test_css_has_accessibility_features():
    for needle in ("prefers-reduced-motion: reduce", ":focus-visible", "forced-colors: active", ".sr-only", ".skip"):
        assert needle in APP_CSS, needle
    assert "prefers-color-scheme: dark" not in TOKENS
    assert "color-scheme: light;" in TOKENS


def test_fonts_are_self_hosted_and_swap():
    assert "font-display: swap" in TOKENS and TOKENS.count("@font-face") == 2
    for f in ("onest-v1.woff2", "unbounded-v1.woff2"):
        data = (ASSETS / "fonts" / f).read_bytes()
        assert data[:4] == b"wOF2" and len(data) < 80 * 1024
    for f in (ASSETS / "fonts").glob("OFL-*.txt"):
        assert "SIL OPEN FONT LICENSE" in f.read_text(encoding="utf-8").upper()
    for page in (STATIC / "donate.html", STATIC / "thanks.html"):
        assert not re.search(r"googleapis|gstatic|cdn\.", page.read_text(encoding="utf-8")), page.name


def test_no_inline_styles_or_handlers():
    for page in (STATIC / "donate.html", STATIC / "thanks.html"):
        text = page.read_text(encoding="utf-8")
        assert not re.search(r'\sstyle="|\son(click|load|error|change|submit)=', text), page.name


class Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs, self.label_for, self.wrapped, self.aria, self._in_label = [], set(), set(), set(), 0
        self.first_link = None
        self.landmarks = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and self.first_link is None:
            self.first_link = a
        if tag in ("main", "header", "footer", "nav"):
            self.landmarks.append(tag)
        if tag == "label":
            self._in_label += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        if tag in ("input", "textarea", "select") and a.get("type") not in ("hidden", "radio"):
            self.inputs.append(a)
            if self._in_label:
                self.wrapped.add(a.get("id"))
            if a.get("aria-label"):
                self.aria.add(a.get("id"))

    def handle_endtag(self, tag):
        if tag == "label":
            self._in_label -= 1


def test_rendered_page_a11y_structure(client):
    html = client.get("/").text
    p = Controls()
    p.feed(html)
    assert p.first_link["href"] == "#main" and "skip" in p.first_link["class"]       # первая ссылка: «К форме доната»
    assert {"main", "header", "footer"} <= set(p.landmarks)
    for field in p.inputs:
        assert field.get("id") in p.label_for | p.wrapped | p.aria, f"у поля {field.get('id')} нет подписи"
    assert 'lang="ru"' in html and 'role="alert"' in html and 'aria-live="polite"' in html
    assert len(re.findall(r'<input[^>]+type="radio"', html)) == 3 and "<fieldset>" in html and "<legend>" in html


def test_assets_are_versioned_and_cached(client):
    html = client.get("/").text
    css_href = re.search(r'href="(/assets/app\.css\?v=([0-9a-f]{10}))"', html)
    assert css_href and "{{" not in html
    r = client.get(css_href.group(1))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/css") and "immutable" in r.headers["cache-control"]
    font = client.get("/assets/fonts/onest-v1.woff2")
    assert font.status_code == 200 and font.headers["content-type"] == "font/woff2" and font.content[:4] == b"wOF2"
    assert "max-age=3600" in font.headers["cache-control"]
    assert "immutable" not in font.headers["cache-control"]
    assert 'rel="preload"' in html and 'as="font"' in html and "crossorigin" in html
    csp = client.get("/").headers["content-security-policy"]
    assert "font-src 'self'" in csp and "googleapis" not in csp and "fonts.gstatic" not in csp
