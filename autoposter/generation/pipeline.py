"""Пайплайн генерации: угол → структура → черновик → критика → правка → SEO → мета."""

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
    do_seo: bool = True

    @property
    def total_steps(self) -> int:
        return 4 + int(self.do_critique) * 2 + int(self.do_seo)

    def _step(self, n: int) -> str:
        """Человекочитаемый номер шага с учётом отключённых этапов."""
        return f"{n}/{self.total_steps}"

    def _seo_pass(self, text: str, primary: str, secondary: list[str]) -> str:
        """Доводит текст по замечаниям SEO-анализатора.

        Анализатор работает по готовому тексту, поэтому промпт получает
        конкретный список проблем, а не общие требования.
        """
        from ..seo import analyze

        probe = Article(blocks=parse_markdown_to_blocks(text))
        report = analyze(probe, primary, secondary)
        if not report.problems:
            log.info("SEO: замечаний нет (оценка %s)", report.score)
            return text

        log.info("SEO: оценка %s, замечаний %s — правлю", report.score, len(report.problems))
        for p in report.problems:
            log.debug("  %s", p)
        try:
            fixed = _clean_output(
                self.backend.ask(
                    prompts.stage_seo(text, primary, secondary, report.problems),
                    system=prompts.SYSTEM,
                )
            )
        except Exception as exc:
            log.warning("SEO-правка не удалась, оставляю как есть: %s", exc)
            return text

        after = analyze(Article(blocks=parse_markdown_to_blocks(fixed)), primary, secondary)
        # правка не должна ухудшать текст: если стало хуже, откатываемся
        if after.score < report.score:
            log.warning("После правки оценка упала (%s → %s) — откат",
                        report.score, after.score)
            return text
        log.info("SEO: оценка %s → %s", report.score, after.score)
        return fixed

    def run(self, topic: Topic, recent_titles: list[str] | None = None) -> Article:
        recent_titles = recent_titles or []
        self.backend.reset()

        log.info("[%s] Угол и заголовки: %s", self._step(1), topic.title)
        brief = self.backend.ask(
            prompts.stage_angle(topic.title, topic.category, recent_titles),
            system=prompts.SYSTEM,
        )
        parsed = _parse_kv(brief)

        log.info("[%s] Структура", self._step(2))
        outline = self.backend.ask(
            prompts.stage_outline(brief, self.target_chars), system=prompts.SYSTEM
        )

        log.info("[%s] Черновик (~%s знаков)", self._step(3), self.target_chars)
        draft = self.backend.ask(
            prompts.stage_draft(brief, outline, self.target_chars), system=prompts.SYSTEM
        )
        draft = _clean_output(draft)

        final = draft
        if self.do_critique:
            log.info("[%s] Редакторская критика", self._step(4))
            critique = self.backend.ask(prompts.stage_critique(draft), system=prompts.SYSTEM)

            log.info("[%s] Финальная правка", self._step(5))
            final = _clean_output(
                self.backend.ask(
                    prompts.stage_polish(draft, critique, self.target_chars),
                    system=prompts.SYSTEM,
                )
            )

        primary = parsed.get("ГЛАВНЫЙ_ЗАПРОС", "").strip()
        secondary = [
            q.strip() for q in parsed.get("ДОП_ЗАПРОСЫ", "").split(";") if q.strip()
        ]

        if self.do_seo and primary:
            final = self._seo_pass(final, primary, secondary)

        log.info("[%s] Метаданные и факт-чек", self._step(6))
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
        if not topic.primary_query:
            topic.primary_query = primary
        if not topic.secondary_queries:
            topic.secondary_queries = secondary

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


# Служебные реплики, которые модель иногда добавляет перед текстом статьи,
# несмотря на запрет в промпте: «Вот финальный текст:», «Final text below» и т.п.
_PREAMBLE_RE = re.compile(
    r"\b("
    r"final text|text below|target range|as requested|here'?s the|word count"
    r"|вот (?:финальн|итогов|готов|текст)|финальн\w+ (?:текст|версия)"
    r"|итогов\w+ текст|готов\w+ текст|ниже\s+текст|текст\s+статьи\s+ниже"
    r")\b",
    re.I,
)


def _strip_preamble(text: str, *, max_lines: int = 3) -> str:
    """Отрезает служебные реплики модели в начале и в конце ответа.

    Проверяются только первые и последние строки: внутри статьи такие
    формулировки — обычный текст, и трогать их нельзя.
    """
    lines = text.split("\n")

    def is_meta(line: str) -> bool:
        s = line.strip()
        if not s or len(s) > 200:
            return False
        if s.startswith("#"):  # заголовок — уже содержимое
            return False
        return bool(_PREAMBLE_RE.search(s))

    start = 0
    for _ in range(max_lines):
        while start < len(lines) and not lines[start].strip():
            start += 1
        if start < len(lines) and is_meta(lines[start]):
            start += 1
        else:
            break

    end = len(lines)
    for _ in range(max_lines):
        while end > start and not lines[end - 1].strip():
            end -= 1
        if end > start and is_meta(lines[end - 1]):
            end -= 1
        else:
            break

    return "\n".join(lines[start:end]).strip()


def _clean_output(text: str) -> str:
    """Полная очистка ответа модели перед разбором в блоки."""
    return _strip_preamble(_strip_fences(text))
