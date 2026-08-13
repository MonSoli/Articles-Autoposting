"""Генератор обложек 1920×1080 под требования vc.ru.

Без внешних сервисов: градиент + типографика. Требования площадки —
горизонтальная картинка не менее 1800 px по широкой стороне.
"""

from __future__ import annotations

import hashlib
import logging
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]


def _find_font(explicit: str = "") -> str | None:
    if explicit and Path(explicit).exists():
        return explicit
    for c in FONT_CANDIDATES:
        if Path(c).exists():
            return c
    return None


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _pick_color(seed: str, palette: list[str]) -> str:
    h = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16)
    return palette[h % len(palette)]


def _gradient(w: int, h: int, base: tuple[int, int, int]) -> Image.Image:
    """Диагональный градиент от base к его осветлённой версии."""
    top = tuple(min(255, int(v * 1.9) + 18) for v in base)
    small = Image.new("RGB", (w // 8, h // 8))
    px = small.load()
    for y in range(small.height):
        for x in range(small.width):
            t = (x / max(small.width - 1, 1) * 0.45) + (y / max(small.height - 1, 1) * 0.55)
            px[x, y] = tuple(int(base[i] + (top[i] - base[i]) * (1 - t)) for i in range(3))  # type: ignore[index]
    return small.resize((w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(6))


def _fit_text(text: str, font_path: str | None, max_w: int, max_h: int, start: int):
    """Подбирает размер шрифта и перенос так, чтобы текст влез в блок."""
    size = start
    while size > 28:
        font = (
            ImageFont.truetype(font_path, size)
            if font_path
            else ImageFont.load_default()
        )
        # эмпирическая ширина символа для подбора переноса
        approx_char_w = max(font.getlength("н"), 1)
        wrap_at = max(int(max_w / approx_char_w), 8)
        lines = textwrap.wrap(text, width=wrap_at)
        line_h = int(size * 1.22)
        if len(lines) * line_h <= max_h and all(
            font.getlength(l) <= max_w for l in lines
        ):
            return font, lines, line_h
        if font_path is None:
            return font, textwrap.wrap(text, width=40), int(size * 1.22)
        size -= 4
    font = ImageFont.truetype(font_path, 28) if font_path else ImageFont.load_default()
    return font, textwrap.wrap(text, width=40), 34


def make_cover(
    title: str,
    out_path: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    palette: list[str] | None = None,
    accent: str = "#c9a227",
    font_path: str = "",
    kicker: str = "",
) -> Path:
    """Рисует обложку и сохраняет в JPEG.

    Args:
        title: текст на обложке — не более 5–7 слов (см. регламент площадки).
        kicker: маленькая надпись сверху (рубрика).
    """
    palette = palette or ["#1c1c1e", "#2c2416", "#1a2620", "#241a26", "#1a1f2e"]
    base = _hex(_pick_color(title, palette))
    img = _gradient(width, height, base)
    draw = ImageDraw.Draw(img)

    fp = _find_font(font_path)
    if fp is None:
        log.warning("TTF-шрифт не найден — обложка будет с системным шрифтом")

    margin = int(width * 0.08)
    box_w = width - margin * 2
    box_h = int(height * 0.52)

    # виньетка снизу для контраста
    overlay = Image.new("L", (width, height), 0)
    ov = ImageDraw.Draw(overlay)
    ov.rectangle([0, int(height * 0.35), width, height], fill=90)
    img = Image.composite(Image.new("RGB", (width, height), (0, 0, 0)), img, overlay.filter(ImageFilter.GaussianBlur(120)))
    draw = ImageDraw.Draw(img)

    font, lines, line_h = _fit_text(title, fp, box_w, box_h, start=int(height * 0.11))

    # блок «рубрика + заголовок + черта» центрируется по вертикали
    text_h = len(lines) * line_h
    y = int((height - text_h) / 2)

    if kicker:
        kf = ImageFont.truetype(fp, 34) if fp else ImageFont.load_default()
        draw.text((margin, y - 70), kicker.upper(), font=kf, fill=_hex(accent))

    for line in lines:
        draw.text((margin, y), line, font=font, fill=(245, 243, 238))
        y += line_h

    # акцентная черта
    draw.rectangle(
        [margin, y + 28, margin + int(box_w * 0.16), y + 36], fill=_hex(accent)
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=90, optimize=True)
    log.info("Обложка: %s (%s×%s)", out_path, width, height)
    return out_path
