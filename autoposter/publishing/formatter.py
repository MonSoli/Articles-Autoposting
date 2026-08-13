"""Преобразование блоков статьи в то, что понимает редактор vc.ru."""

from __future__ import annotations

import html

from ..models import Article, Block, BlockType


def blocks_to_html(article: Article) -> str:
    """Собирает HTML для вставки через буфер обмена.

    Блочный редактор Osnova разбирает вставленный rich-text в свои блоки:
    <h2> становится заголовком, <ul>/<ol> — списками, <blockquote> — цитатой.
    Это самый устойчивый способ переноса структуры: одна вставка вместо
    сотни кликов по меню блоков.
    """
    out: list[str] = []
    for b in article.blocks:
        e = html.escape
        if b.type is BlockType.HEADING:
            out.append(f"<h2>{e(b.text)}</h2>")
        elif b.type is BlockType.SUBHEADING:
            out.append(f"<h3>{e(b.text)}</h3>")
        elif b.type is BlockType.BULLET_LIST:
            items = "".join(f"<li>{e(i)}</li>" for i in b.items)
            out.append(f"<ul>{items}</ul>")
        elif b.type is BlockType.NUMBER_LIST:
            items = "".join(f"<li>{e(i)}</li>" for i in b.items)
            out.append(f"<ol>{items}</ol>")
        elif b.type is BlockType.QUOTE:
            out.append(f"<blockquote>{e(b.text)}</blockquote>")
        elif b.type is BlockType.NUMBER_CARD:
            # «Цифра» вставляется отдельным блоком вручную; в потоке оставляем
            # читаемый заменитель, который затем заменяется на настоящий блок
            out.append(f"<p><strong>{e(b.text)}</strong> — {e(b.caption)}</p>")
        elif b.type is BlockType.DELIMITER:
            out.append("<hr>")
        elif b.type is BlockType.IMAGE:
            # плейсхолдер: картинку загружает publisher отдельным шагом
            out.append(f"<p>{IMAGE_MARK} {e(b.caption)}</p>")
        elif b.type is BlockType.POLL:
            opts = "".join(f"<li>{e(i)}</li>" for i in b.items)
            out.append(f"<p>{POLL_MARK} {e(b.text)}</p><ul>{opts}</ul>")
        elif b.type is BlockType.CODE:
            out.append(f"<pre>{e(b.text)}</pre>")
        else:
            out.append(f"<p>{e(b.text)}</p>")
    return "\n".join(out)


IMAGE_MARK = "[[IMG]]"
POLL_MARK = "[[POLL]]"


def blocks_to_plain(article: Article) -> str:
    """Простой текст — запасной вариант, если вставка HTML не сработала.

    Каждый блок с новой строки: редактор создаст из них отдельные абзацы.
    Разметка теряется, но текст доходит целиком.
    """
    lines: list[str] = []
    for b in article.blocks:
        if b.type in (BlockType.HEADING, BlockType.SUBHEADING):
            lines.append(b.text)
        elif b.type in (BlockType.BULLET_LIST, BlockType.NUMBER_LIST):
            lines.extend(
                f"— {i}" if b.type is BlockType.BULLET_LIST else f"{n}. {i}"
                for n, i in enumerate(b.items, 1)
            )
        elif b.type is BlockType.QUOTE:
            lines.append(b.text)
        elif b.type is BlockType.NUMBER_CARD:
            lines.append(f"{b.text} — {b.caption}")
        elif b.type is BlockType.DELIMITER:
            lines.append("***")
        elif b.type is BlockType.IMAGE:
            continue
        elif b.type is BlockType.POLL:
            lines.append(b.text)
            lines.extend(f"— {i}" for i in b.items)
        else:
            lines.append(b.text)
    return "\n".join(l for l in lines if l.strip())


def validate(article: Article) -> list[str]:
    """Проверка соответствия регламенту площадки перед публикацией."""
    problems: list[str] = []

    if not article.title:
        problems.append("нет заголовка")
    elif len(article.title) > 90:
        problems.append(f"заголовок {len(article.title)} знаков — обрежется в ленте (макс. 90)")

    if not article.subtitle:
        problems.append("нет подзаголовка — он же meta description")

    chars = article.char_count
    if chars < 4000:
        problems.append(f"объём {chars} знаков — мало для попадания в «Популярное» (нужно от 6000)")
    elif chars > 18000:
        problems.append(f"объём {chars} знаков — дочитываемость упадёт")

    if article.heading_count < 3:
        problems.append(f"только {article.heading_count} H2-подзаголовка — текст будет монолитным")

    if not article.tags:
        problems.append("нет тегов")
    elif len(article.tags) > 5:
        problems.append(f"{len(article.tags)} тегов — рекомендуется 3–5")

    if not article.cover_path:
        problems.append("нет обложки — материал уйдёт в ленту без картинки, CTR упадёт в разы")

    # длинные абзацы
    long_paras = [
        b.text[:60] for b in article.blocks
        if b.type is BlockType.PARAGRAPH and len(b.text) > 600
    ]
    for p in long_paras:
        problems.append(f"слишком длинный абзац: «{p}…»")

    # признаки ИИ-текста
    ai_markers = [
        "в современном мире", "важно отметить", "играет ключевую роль",
        "стоит отметить", "в заключение", "не только", "динамично развива",
    ]
    body = article.body_text.lower()
    hits = [m for m in ai_markers if m in body]
    if hits:
        problems.append(f"штампы ИИ-текста: {', '.join(hits)}")

    return problems
