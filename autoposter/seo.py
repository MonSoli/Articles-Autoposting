"""SEO-анализ статьи под Яндекс и Google.

Проверяется то, что реально влияет на ранжирование в 2026 году: размещение
запроса, структура заголовков, пригодность под быстрые ответы, длина мета-полей,
переспам. Плотность считается по словоформам — русский язык флективный,
и точные вхождения ключа в тексте почти не встречаются.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Article, BlockType

# Окончания, которые отсекаются при грубой нормализации словоформ.
# Это не морфологический анализ, а достаточное приближение для подсчёта вхождений.
_ENDINGS = (
    "ами", "ями", "ого", "ему", "ому", "ыми", "ими", "ей", "ой", "ый", "ий",
    "ая", "яя", "ое", "ее", "ые", "ие", "ам", "ям", "ах", "ях", "ом", "ем",
    "ов", "ев", "ью", "ия", "ии", "а", "я", "о", "е", "у", "ю", "ы", "и", "ь",
)

_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.I)

# Стоп-слова: в ключевых фразах они не несут смысла для сопоставления
_STOP = {
    "и", "в", "во", "не", "на", "с", "со", "как", "что", "это", "для", "по",
    "от", "до", "из", "за", "к", "у", "о", "об", "а", "но", "или", "же", "ли",
    "бы", "то", "все", "так", "уже", "если", "чем", "тем", "при", "без",
}


def normalize(word: str) -> str:
    """Грубо приводит слово к основе, отсекая частотное окончание."""
    w = word.lower().replace("ё", "е")
    if len(w) <= 4:
        return w
    for end in _ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 4:
            return w[: -len(end)]
    return w


def stems(text: str) -> list[str]:
    """Список основ значимых слов текста."""
    return [
        normalize(w) for w in _WORD_RE.findall(text.lower()) if w not in _STOP
    ]


def phrase_hits(text: str, phrase: str) -> int:
    """Сколько раз ключевая фраза встречается в тексте в любых словоформах.

    Считается вхождение всех значимых слов фразы подряд, с допуском
    в одно постороннее слово между ними — так ищутся «костюм мужской»
    и «мужского костюма» одновременно.
    """
    target = [normalize(w) for w in _WORD_RE.findall(phrase.lower()) if w not in _STOP]
    if not target:
        return 0

    body = stems(text)
    if len(target) == 1:
        return body.count(target[0])

    hits = 0
    i = 0
    while i < len(body):
        if body[i] != target[0]:
            i += 1
            continue
        pos, matched, gaps = i + 1, 1, 0
        while pos < len(body) and matched < len(target):
            if body[pos] == target[matched]:
                matched += 1
            elif gaps < 1:
                gaps += 1
            else:
                break
            pos += 1
        if matched == len(target):
            hits += 1
            i = pos
        else:
            i += 1
    return hits


@dataclass
class SeoReport:
    """Результат анализа."""

    score: int = 0                              # 0..100
    density: float = 0.0                        # вхождений на 1000 знаков
    problems: list[str] = field(default_factory=list)
    good: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def analyze(article: Article, primary: str = "", secondary: list[str] | None = None) -> SeoReport:
    """Проверяет статью по факторам ранжирования.

    Args:
        article: статья.
        primary: главный поисковый запрос. Если пуст, берётся из темы.
        secondary: сопутствующие запросы.
    """
    topic = article.topic
    primary = primary or (topic.primary_query if topic else "")
    secondary = secondary or (topic.secondary_queries if topic else []) or []

    rep = SeoReport()
    body = article.body_text
    chars = max(article.char_count, 1)

    headings = [b.text for b in article.blocks if b.type is BlockType.HEADING]
    first_para = next(
        (b.text for b in article.blocks if b.type is BlockType.PARAGRAPH and b.text), ""
    )

    # --- главный запрос -------------------------------------------------
    if not primary:
        rep.problems.append("не задан главный поисковый запрос — нечего оптимизировать")
    else:
        if phrase_hits(article.title, primary):
            rep.good.append("главный запрос в заголовке")
        else:
            rep.problems.append(f"главного запроса «{primary}» нет в заголовке (H1)")

        if phrase_hits(first_para, primary):
            rep.good.append("главный запрос в первом абзаце")
        else:
            rep.problems.append("главного запроса нет в первом абзаце")

        if phrase_hits(article.subtitle, primary):
            rep.good.append("главный запрос в подзаголовке (meta description)")

        total = phrase_hits(body, primary)
        rep.density = total / chars * 1000
        if total == 0:
            rep.problems.append("главный запрос не встречается в тексте")
        elif rep.density > 2.5:
            rep.problems.append(
                f"переспам: {total} вхождений, {rep.density:.1f} на 1000 знаков "
                "(безопасно до 2,5) — риск фильтра"
            )
        elif rep.density < 0.4:
            rep.problems.append(
                f"слишком мало вхождений: {total} ({rep.density:.1f} на 1000 знаков)"
            )
        else:
            rep.good.append(f"плотность запроса в норме: {rep.density:.1f} на 1000 знаков")

    # --- сопутствующие запросы ------------------------------------------
    if secondary:
        covered = [q for q in secondary if phrase_hits(body, q)]
        in_headings = [
            q for q in secondary if any(phrase_hits(h, q) for h in headings)
        ]
        if len(covered) < len(secondary) / 2:
            rep.problems.append(
                f"раскрыто {len(covered)} из {len(secondary)} сопутствующих запросов"
            )
        else:
            rep.good.append(f"сопутствующие запросы раскрыты: {len(covered)}/{len(secondary)}")
        if in_headings:
            rep.good.append(f"{len(in_headings)} запросов вынесены в подзаголовки")
        else:
            rep.problems.append("сопутствующие запросы не вынесены в H2")

    # --- структура под быстрые ответы -----------------------------------
    questions = [h for h in headings if h.rstrip().endswith("?")]
    if len(questions) >= 2:
        rep.good.append(f"{len(questions)} подзаголовков-вопросов — заявка на быстрые ответы")
    else:
        rep.problems.append(
            "меньше двух подзаголовков в форме вопроса — теряете шанс на быстрый ответ Яндекса"
        )

    for q in questions:
        answer = _paragraph_after(article, q)
        if answer and len(answer) > 600:
            rep.problems.append(
                f"ответ на вопрос «{q[:40]}…» длиннее 600 знаков — "
                "в быстрый ответ попадает короткий абзац"
            )

    # --- списки и таблицы -----------------------------------------------
    lists = sum(
        1 for b in article.blocks
        if b.type in (BlockType.BULLET_LIST, BlockType.NUMBER_LIST)
    )
    if lists:
        rep.good.append(f"{lists} списков — хорошо индексируются")
    else:
        rep.problems.append("нет ни одного списка — теряется структурная разметка")

    # --- мета -----------------------------------------------------------
    if article.subtitle:
        n = len(article.subtitle)
        if n < 100:
            rep.problems.append(f"подзаголовок {n} знаков — коротко для description (нужно 140–160)")
        elif n > 200:
            rep.problems.append(f"подзаголовок {n} знаков — обрежется в выдаче (нужно 140–160)")
        else:
            rep.good.append(f"длина подзаголовка подходит под description ({n} знаков)")

    n = len(article.title)
    if n > 70:
        rep.problems.append(f"заголовок {n} знаков — в выдаче обрежется (оптимум до 70)")

    # --- подписи к иллюстрациям (они же alt) -----------------------------
    images = [b for b in article.blocks if b.type is BlockType.IMAGE]
    if images:
        empty = [b for b in images if len(b.caption.strip()) < 15]
        if empty:
            rep.problems.append(f"{len(empty)} иллюстраций без содержательной подписи (это alt)")
        else:
            rep.good.append("у всех иллюстраций есть подписи")

    # --- итоговая оценка -------------------------------------------------
    checks = len(rep.good) + len(rep.problems)
    rep.score = int(len(rep.good) / checks * 100) if checks else 0
    return rep


def _paragraph_after(article: Article, heading: str) -> str:
    """Первый абзац после указанного подзаголовка."""
    found = False
    for b in article.blocks:
        if b.type is BlockType.HEADING and b.text == heading:
            found = True
            continue
        if found:
            if b.type is BlockType.PARAGRAPH and b.text:
                return b.text
            if b.type is BlockType.HEADING:
                return ""
    return ""


def keyword_report(article: Article, queries: list[str]) -> list[tuple[str, int, float]]:
    """Таблица «запрос → вхождений → плотность на 1000 знаков»."""
    chars = max(article.char_count, 1)
    body = article.body_text
    out = []
    for q in queries:
        hits = phrase_hits(body, q)
        out.append((q, hits, hits / chars * 1000))
    return sorted(out, key=lambda r: -r[1])
