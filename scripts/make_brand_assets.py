"""
Рисует обложку для ссылок (1200x630, для Telegram и соцсетей) и иконку сайта в фирменных цветах.

    python scripts/make_brand_assets.py --name vanesso4ka --pink "#F8C8DC"

Нужна библиотека Pillow (есть в requirements-dev.txt). Результат: static/og-cover.png, static/favicon.ico, static/favicon.svg.
"""
import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

STATIC = Path(__file__).resolve().parent.parent / "static"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD_WIN, FONT_REG_WIN = "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"


def rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def luminance(c) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def contrast(a, b) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def darken_until(color, background, ratio: float):
    """Затемняет цвет, пока контраст с фоном не достигнет нужного значения (оттенок сохраняется)."""
    c = list(color)
    while contrast(c, background) < ratio:
        c = [max(0, int(v * 0.97)) for v in c]
    return tuple(c)


def font(path_linux: str, path_win: str, size: int):
    for p in (path_linux, path_win):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    raise SystemExit("Не найден шрифт с кириллицей (DejaVu Sans или Arial)")


def heart_points(cx, cy, size, n=120):
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x * size / 17, cy - y * size / 17))
    return pts


def make_cover(name: str, blue, white, pink, chips: list[str], out: Path) -> None:
    S = 2  # рисуем в двойном размере и уменьшаем: края получаются гладкими
    W, H = 1200 * S, 630 * S
    img = Image.new("RGB", (W, H), white)
    d = ImageDraw.Draw(img)
    navy = darken_until(blue, white, 12.0)   # основной текст
    deep = darken_until(blue, white, 4.5)    # имя: тот же голубой, но читаемый

    # декор справа: розовый и голубой круги, белое сердце
    d.ellipse((760 * S, -140 * S, 1340 * S, 440 * S), fill=pink)
    d.ellipse((800 * S, 190 * S, 1240 * S, 630 * S), fill=blue)
    d.polygon(heart_points(1020 * S, 400 * S, 190 * S), fill=white)

    f_big, f_mid = font(FONT_BOLD, FONT_BOLD_WIN, 92 * S), font(FONT_REG, FONT_REG_WIN, 38 * S)
    f_chip = font(FONT_BOLD, FONT_BOLD_WIN, 32 * S)
    x = 70 * S
    d.text((x, 90 * S), "Поддержать", font=f_big, fill=navy)
    name_font_size = 92
    while d.textlength(name, font=font(FONT_BOLD, FONT_BOLD_WIN, name_font_size * S)) > 660 * S and name_font_size > 40:
        name_font_size -= 4
    d.text((x, 205 * S), name, font=font(FONT_BOLD, FONT_BOLD_WIN, name_font_size * S), fill=deep)
    d.text((x, 340 * S), "Донат с сообщением", font=f_mid, fill=navy)
    d.text((x, 390 * S), "прямо на стриме", font=f_mid, fill=navy)

    cx = x
    for label in chips:
        w = int(d.textlength(label, font=f_chip)) + 56 * S
        d.rounded_rectangle((cx, 480 * S, cx + w, 480 * S + 74 * S), radius=37 * S, fill=pink)
        d.text((cx + 28 * S, 480 * S + 17 * S), label, font=f_chip, fill=navy)
        cx += w + 18 * S

    img = img.resize((1200, 630), Image.LANCZOS)
    img.save(out, optimize=True)
    if out.stat().st_size > 300 * 1024:  # лимит из ТЗ: до 300 КБ
        img.quantize(colors=128).save(out, optimize=True)
    print(f"{out.name}: {out.stat().st_size // 1024} КБ; контраст текста: основной {contrast(navy, white):.1f}:1, имя {contrast(deep, white):.1f}:1")
    print("Тёмно-голубой для текста и кнопок (контраст 4.5:1 на белом): #%02x%02x%02x" % deep)


def make_favicon(blue, white, out: Path) -> None:
    S = 8
    img = Image.new("RGBA", (64 * S, 64 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 64 * S - 1, 64 * S - 1), radius=14 * S, fill=blue)
    d.polygon(heart_points(32 * S, 34 * S, 30 * S), fill=white)
    img = img.resize((64, 64), Image.LANCZOS)
    img.save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in heart_points(32, 34, 30))  # тот же рисунок для SVG-иконки
    hexc = lambda c: "#%02x%02x%02x" % c  # noqa: E731
    (out.parent / "favicon.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="{hexc(blue)}"/>'
        f'<polygon points="{pts}" fill="{hexc(white)}"/></svg>\n', encoding="utf-8")
    print(f"{out.name}: {out.stat().st_size // 1024 or 1} КБ")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="vanesso4ka")
    ap.add_argument("--blue", default="#0593C6")
    ap.add_argument("--white", default="#FCFFFF")
    ap.add_argument("--pink", default="#F8C8DC")
    ap.add_argument("--chips", default="Карта,СБП,Крипта")
    a = ap.parse_args()
    make_cover(a.name, rgb(a.blue), rgb(a.white), rgb(a.pink), [c.strip() for c in a.chips.split(",") if c.strip()], STATIC / "og-cover.png")
    make_favicon(rgb(a.blue), rgb(a.white), STATIC / "favicon.ico")
