"""Подбор фотографий к статье.

Модель, когда пишет текст, отмечает места под иллюстрации маркером
`[ИЛЛЮСТРАЦИЯ] описание нужной картинки` — описание на русском и своими словами.
Здесь эти описания превращаются в поисковые запросы (на английском — так
у фотостоков в разы больше материала), фотографии ищутся, скачиваются
и прикрепляются к блокам вместе с подписью и атрибуцией.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..models import Article, BlockType
from .images import ImageResult, ImageSearcher, download

log = logging.getLogger(__name__)


QUERY_PROMPT = """\
Ниже — описания иллюстраций к статье о классической мужской одежде, на русском языке.

Для каждого описания составь поисковый запрос НА АНГЛИЙСКОМ для фотостока.

Правила:
- 2–5 слов, конкретные существительные, без предлогов и артиклей
- используй профессиональную лексику: tailoring, lapel, worsted wool, brogue,
  goodyear welt, herringbone, savile row, bespoke tailor, fabric swatch
- не используй абстракции («elegance», «lifestyle», «success») — фотосток отдаст
  на них рекламный мусор
- если описание про схему, чертёж или диаграмму — всё равно дай запрос
  на ближайший предметный объект (фото детали, ткани, инструмента)

Верни ровно по одной строке на описание, в том же порядке, в формате:
<номер>. <запрос>

Описания:
{items}
"""


def build_queries(captions: list[str], backend=None) -> list[str]:
    """Превращает русские описания иллюстраций в поисковые запросы.

    Если бэкенд Claude не передан или дал сбой, работает запасной
    словарный перевод по ключевым словам.
    """
    if not captions:
        return []

    if backend is not None:
        items = "\n".join(f"{n}. {c}" for n, c in enumerate(captions, 1))
        try:
            answer = backend.ask(QUERY_PROMPT.format(items=items), fresh=True)
            queries = _parse_numbered(answer, len(captions))
            if all(queries):
                return queries
            log.warning("Часть запросов не разобрана — дополняю запасным способом")
            return [q or _fallback_query(c) for q, c in zip(queries, captions)]
        except Exception as exc:
            log.warning("Не удалось составить запросы через Claude: %s", exc)

    return [_fallback_query(c) for c in captions]


def illustrate(
    article: Article,
    dest_dir: Path,
    *,
    searcher: ImageSearcher | None = None,
    backend=None,
    max_images: int = 4,
) -> int:
    """Находит и прикрепляет фотографии ко всем блокам-иллюстрациям.

    Меняет блоки на месте: в `meta['path']` появляется локальный файл,
    в `caption` — подпись с атрибуцией.

    Returns:
        число прикреплённых фотографий.
    """
    searcher = searcher or ImageSearcher()
    dest_dir = Path(dest_dir)

    slots = [b for b in article.blocks if b.type is BlockType.IMAGE][:max_images]
    if not slots:
        log.info("В статье нет мест под иллюстрации")
        return 0

    active = searcher.active_providers()
    if not active:
        log.warning("Нет доступных источников фотографий")
        return 0
    log.info("Подбираю %s иллюстраций (источники: %s)", len(slots), ", ".join(active))

    queries = build_queries([b.caption for b in slots], backend)
    used_urls: set[str] = set()
    attached = 0

    for n, (block, query) in enumerate(zip(slots, queries), 1):
        block.meta["query"] = query
        candidates = searcher.search(query, limit=6)
        picked: ImageResult | None = next(
            (c for c in candidates if c.url not in used_urls), None
        )
        if picked is None:
            log.warning("[%s/%s] Ничего не найдено по запросу «%s»", n, len(slots), query)
            continue

        path = download(picked, dest_dir / f"{article.id}-img{n}.jpg")
        if path is None:
            continue

        used_urls.add(picked.url)
        block.meta.update(
            path=str(path),
            source=picked.source,
            source_url=picked.page_url,
            license=picked.license_name,
            author=picked.author,
        )
        block.caption = _compose_caption(block.caption, picked)
        attached += 1
        log.info("[%s/%s] «%s» → %s", n, len(slots), query, picked.source)

    log.info("Прикреплено фотографий: %s из %s", attached, len(slots))
    return attached


# ----------------------------------------------------------------------


def _compose_caption(original: str, img: ImageResult) -> str:
    """Осмысленная подпись + атрибуция.

    Подпись — отдельный смысловой слой, а не «фото сверху»: исходное описание
    от модели сохраняется, к нему добавляется указание авторства.
    """
    text = original.strip().rstrip(".")
    # описание для генератора картинок бывает техническим — обрезаем хвост в скобках
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    if len(text) > 180:
        text = text[:180].rsplit(" ", 1)[0] + "…"
    return f"{text}. {img.attribution()}" if text else img.attribution()


def _parse_numbered(answer: str, expected: int) -> list[str]:
    """Разбирает ответ вида `1. query` в список нужной длины."""
    out: list[str] = [""] * expected
    for line in answer.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^\s*(\d+)[.)]\s*(.+?)\s*$", line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if 0 <= idx < expected:
            out[idx] = m.group(2).strip().strip('"«»')
    return out


# Запасной перевод: ключевое слово в описании → английский запрос.
# Порядок важен — более специфичные записи идут первыми.
_FALLBACK_MAP: list[tuple[str, str]] = [
    ("рант", "goodyear welted shoe sole"),
    ("оксфорд", "oxford leather shoes"),
    ("дерби", "derby leather shoes"),
    ("ботин", "leather dress shoes"),
    ("обув", "leather dress shoes"),
    ("лацкан", "suit jacket lapel closeup"),
    ("плеч", "suit jacket shoulder tailoring"),
    ("рукав", "suit sleeve cuff buttons"),
    ("пройм", "jacket armhole tailoring"),
    ("подклад", "suit jacket lining"),
    ("пиджак", "tailored jacket menswear"),
    ("костюм", "mens tailored suit"),
    ("рубаш", "dress shirt collar detail"),
    ("галстук", "silk necktie knot"),
    ("брюк", "tailored trousers menswear"),
    ("пальто", "wool overcoat menswear"),
    ("твид", "tweed fabric texture"),
    ("фланел", "flannel wool fabric"),
    ("шерст", "worsted wool fabric closeup"),
    ("ткан", "wool fabric swatches"),
    ("ателье", "bespoke tailor workshop"),
    ("портн", "tailor sewing workshop"),
    ("закрой", "tailor cutting pattern"),
    ("мерк", "tailor measuring tape"),
    ("примерк", "suit fitting tailor"),
    ("гардероб", "menswear wardrobe rail"),
    ("уход", "shoe care brushes polish"),
    ("хранен", "wooden hangers suits"),
    ("глад", "ironing dress shirt"),
]


def _fallback_query(caption: str) -> str:
    """Подбирает запрос по ключевым словам, когда Claude недоступен."""
    low = caption.lower()
    for key, query in _FALLBACK_MAP:
        if key in low:
            return query
    return "classic menswear tailoring"
