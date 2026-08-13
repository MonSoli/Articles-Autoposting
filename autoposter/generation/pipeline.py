"""Пайплайн генерации статьи: угол → структура → черновик → критика → правка → мета."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..models import Article, Status, Topic, parse_markdown_to_blocks
from . import prompts
from .claude_backend import ClaudeBackend

log = logging.getLogger(__name__)

VALID_SUBSITES = {"fashion", "life", "business", "marketing", "tribuna", "future"}


@dataclass
class Pipeline:
    """Многоэтапная генерация. Каждый этап — отдельный вызов Claude в одной цепочке."""

    backend: ClaudeBackend
    target_chars: int = 9000
    do_critique: bool = True

    def run(self, topic: Topic, recent_titles: list[str] | None = None) -> Article:
        recent_titles = recent_titles or []
        self.backend.reset()

        log.info("[1/6] Угол и заголовки: %s", topic.title)
        brief = self.backend.ask(
            prompts.stage_angle(topic.title, topic.category, recent_titles),
            system=prompts.SYSTEM,
        )
        parsed = _parse_kv(brief)

        log.info("[2/6] Структура")
        outline = self.backend.ask(
            prompts.stage_outline(brief, self.target_chars), system=prompts.SYSTEM
        )

        log.info("[3/6] Черновик (~%s знаков)", self.target_chars)
        draft = self.backend.ask(
            prompts.stage_draft(brief, outline, self.target_chars), system=prompts.SYSTEM
        )
        draft = _strip_fences(draft)

        final = draft
        if self.do_critique:
            log.info("[4/6] Редакторская критика")
            critique = self.backend.ask(prompts.stage_critique(draft), system=prompts.SYSTEM)

            log.info("[5/6] Финальная правка")
            final = _strip_fences(
                self.backend.ask(
                    prompts.stage_polish(draft, critique, self.target_chars),
                    system=prompts.SYSTEM,
                )
            )

        log.info("[6/6] Метаданные и факт-чек")
        meta_raw = self.backend.ask(prompts.stage_meta(final, brief), system=prompts.SYSTEM)
        meta = _parse_kv(meta_raw)

        article = Article(
            title=_clean_title(meta.get("ЗАГОЛОВОК") or _best_headline(parsed) or topic.title),
            subtitle=meta.get("ПОДЗАГОЛОВОК", parsed.get("ПОДЗАГОЛОВОК", "")).strip(),
            blocks=parse_markdown_to_blocks(final),
            subsite=_pick_subsite(meta.get("ПОДСАЙТ", "")),
            tags=_split_tags(meta.get("ТЕГИ", "")),
            cover_prompt=meta.get("ОБЛОЖКА_ОПИСАНИЕ", "").strip(),
            topic=topic,
            status=Status.GENERATED,
            fact_checks=_parse_list(meta_raw, "ФАКТЧЕК"),
        )
        article.cover_prompt = article.cover_prompt or article.title
        cover_text = meta.get("ОБЛОЖКА_ТЕКСТ", "").strip()
        if cover_text:
            article.notes = f"cover_text={cover_text}"

        # запросы для SEO кладём в тему
        if topic.primary_query == "":
            topic.primary_query = parsed.get("ГЛАВНЫЙ_ЗАПРОС", "").strip()
        if not topic.secondary_queries:
            topic.secondary_queries = [
                q.strip() for q in parsed.get("ДОП_ЗАПРОСЫ", "").split(";") if q.strip()
            ]

        log.info(
            "Готово: «%s» — %s знаков, %s H2-блоков, %s пунктов на факт-чек",
            article.title, article.char_count, article.heading_count, len(article.fact_checks),
        )
        return article


# ----------------------------------------------------------------------
# разбор ответов
# ----------------------------------------------------------------------

_KV_RE = re.compile(r"^([А-ЯЁA-Z_]{3,}):\s*(.*)$")


def _parse_kv(text: str) -> dict[str, str]:
    """Разбирает ответ вида `КЛЮЧ: значение` (значение может быть многострочным)."""
    out: dict[str, str] = {}
    key: str | None = None
    buf: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        m = _KV_RE.match(line.strip())
        if m:
            if key:
                out[key] = "\n".join(buf).strip()
            key, first = m.group(1), m.group(2)
            buf = [first]
        elif key:
            buf.append(line)
    if key:
        out[key] = "\n".join(buf).strip()
    return out


def _parse_list(text: str, key: str) -> list[str]:
    """Достаёт пункты списка, идущие после строки `КЛЮЧ:`."""
    items: list[str] = []
    started = False
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.upper().startswith(f"{key}:"):
            started = True
            tail = s.split(":", 1)[1].strip()
            if tail:
                items.append(tail)
            continue
        if started:
            if re.match(r"^[-*•]\s+", s):
                items.append(re.sub(r"^[-*•]\s+", "", s))
            elif _KV_RE.match(s):
                break
            elif not s:
                continue
            else:
                break
    return [i for i in items if i and i.lower() not in {"нет", "none", "-"}]


def _best_headline(parsed: dict[str, str]) -> str:
    """Берёт заголовок, отмеченный моделью как лучший."""
    best = parsed.get("ЛУЧШИЙ", "")
    headlines = parsed.get("ЗАГОЛОВКИ", "")
    if not headlines:
        return ""
    options = {}
    for line in headlines.split("\n"):
        m = re.match(r"^\s*(\d+)[.)]\s*(.+)$", line.strip())
        if m:
            options[m.group(1)] = m.group(2).strip()
    m = re.search(r"\d+", best)
    if m and m.group(0) in options:
        return options[m.group(0)]
    return next(iter(options.values()), "")


def _clean_title(title: str) -> str:
    title = title.strip().strip('"«»').strip()
    title = re.sub(r"\s+", " ", title)
    if len(title) > 90:
        cut = title[:90].rsplit(" ", 1)[0]
        title = cut.rstrip(",.:;—-")
    return title


def _split_tags(raw: str) -> list[str]:
    tags = [t.strip().lstrip("#").lower() for t in re.split(r"[,;]", raw) if t.strip()]
    return tags[:5]


def _pick_subsite(raw: str) -> str:
    raw = raw.strip().lower().lstrip("/")
    for s in VALID_SUBSITES:
        if s in raw:
            return s
    return "fashion"


def _strip_fences(text: str) -> str:
    """Убирает обёртку ```markdown ... ``` вокруг всего ответа, если модель её добавила."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines[-1].strip().startswith("```"):
            return "\n".join(lines[1:-1]).strip()
    return t
