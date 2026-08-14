"""Тесты SEO-анализа: нормализация словоформ, подсчёт вхождений, проверки."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoposter.models import Article, Block, BlockType, Topic  # noqa: E402
from autoposter.seo import (  # noqa: E402
    analyze, keyword_report, normalize, phrase_hits, stems,
)


# ------------------------------------------------------- нормализация

def test_normalize_collapses_word_forms():
    forms = ["костюм", "костюма", "костюмы", "костюмом", "костюмах", "костюмами"]
    assert len({normalize(f) for f in forms}) == 1


def test_normalize_keeps_short_words_intact():
    assert normalize("шёлк") == "шелк"


def test_normalize_collapses_tkan_forms():
    assert normalize("ткань") == normalize("ткани") == normalize("тканью")


def test_normalize_handles_yo():
    assert normalize("шёлковый") == normalize("шелковый")


def test_stems_drops_stopwords():
    assert "для" not in stems("костюм для работы")
    assert "и" not in stems("пиджак и брюки")


# ------------------------------------------------------ подсчёт вхождений

def test_phrase_hits_counts_inflections():
    text = "Мужской костюм стоит дорого. У мужского костюма своя цена."
    assert phrase_hits(text, "мужской костюм") == 2


def test_phrase_hits_single_word():
    assert phrase_hits("Костюм, костюма, костюмы", "костюм") == 3


def test_phrase_hits_tolerates_one_word_gap():
    # «как выбрать костюм» находится и в «как правильно выбрать костюм»
    assert phrase_hits("Как правильно выбрать костюм", "как выбрать костюм") == 1


def test_phrase_hits_rejects_large_gap():
    assert phrase_hits("Выбрать хороший недорогой качественный костюм", "выбрать костюм") == 0


def test_phrase_hits_ignores_stopwords_in_query():
    assert phrase_hits("Уход за костюмом", "уход за костюмом") == 1


def test_phrase_hits_empty_query():
    assert phrase_hits("любой текст", "  ") == 0


def test_phrase_hits_absent():
    assert phrase_hits("Про обувь и ранты", "мужской костюм") == 0


# -------------------------------------------------------------- анализ

def _article(**kw) -> Article:
    para = "Как выбрать костюм, чтобы он служил долго и не терял вид со временем. " * 5
    blocks = [Block(BlockType.PARAGRAPH, para)]
    for n in range(4):
        blocks.append(Block(BlockType.HEADING, f"Как выбрать костюм по критерию {n}?"))
        blocks.append(Block(BlockType.PARAGRAPH, "Короткий прямой ответ на вопрос."))
        blocks.append(Block(BlockType.PARAGRAPH, para))
    blocks.append(Block(BlockType.BULLET_LIST, items=["плечо", "рукав"]))

    base = dict(
        title="Как выбрать костюм и не переплатить",
        subtitle="Разбираем девять контрольных точек посадки, которые проверяются "
                 "прямо в примерочной за пару минут без помощи консультанта.",
        blocks=blocks,
    )
    base.update(kw)
    return Article(**base)


def test_analyze_finds_query_in_title_and_lead():
    rep = analyze(_article(), primary="как выбрать костюм")
    joined = " ".join(rep.good)
    assert "в заголовке" in joined
    assert "в первом абзаце" in joined


def test_analyze_flags_missing_query():
    rep = analyze(_article(title="Про обувь и ранты"), primary="как выбрать костюм")
    assert any("нет в заголовке" in p for p in rep.problems)


def test_analyze_detects_keyword_stuffing():
    stuffed = _article(
        blocks=[Block(BlockType.PARAGRAPH, "Как выбрать костюм. " * 40)]
    )
    rep = analyze(stuffed, primary="как выбрать костюм")
    assert any("переспам" in p for p in rep.problems)


def test_analyze_detects_too_few_occurrences():
    thin = _article(
        blocks=[Block(BlockType.PARAGRAPH, "Ткани бывают разные. " * 200)]
    )
    rep = analyze(thin, primary="как выбрать костюм")
    assert any("не встречается" in p or "мало вхождений" in p for p in rep.problems)


def test_analyze_rewards_question_headings():
    rep = analyze(_article(), primary="как выбрать костюм")
    assert any("подзаголовков-вопросов" in g for g in rep.good)


def test_analyze_flags_missing_question_headings():
    flat = _article()
    for b in flat.blocks:
        if b.type is BlockType.HEADING:
            b.text = b.text.rstrip("?")
    rep = analyze(flat, primary="как выбрать костюм")
    assert any("форме вопроса" in p for p in rep.problems)


def test_analyze_flags_long_quick_answer():
    a = _article()
    for i, b in enumerate(a.blocks):
        if b.type is BlockType.HEADING:
            a.blocks[i + 1].text = "слово " * 200
            break
    rep = analyze(a, primary="как выбрать костюм")
    assert any("длиннее 600 знаков" in p for p in rep.problems)


def test_analyze_checks_subtitle_length():
    short = analyze(_article(subtitle="Коротко"), primary="как выбрать костюм")
    assert any("коротко для description" in p for p in short.problems)

    long = analyze(_article(subtitle="а" * 250), primary="как выбрать костюм")
    assert any("обрежется в выдаче" in p for p in long.problems)


def test_analyze_flags_long_title():
    rep = analyze(_article(title="Как выбрать костюм " * 5), primary="как выбрать костюм")
    assert any("в выдаче обрежется" in p for p in rep.problems)


def test_analyze_checks_image_captions():
    a = _article()
    a.blocks.append(Block(BlockType.IMAGE, caption="хм"))
    rep = analyze(a, primary="как выбрать костюм")
    assert any("без содержательной подписи" in p for p in rep.problems)


def test_analyze_flags_missing_lists():
    a = _article()
    a.blocks = [b for b in a.blocks if b.type is not BlockType.BULLET_LIST]
    rep = analyze(a, primary="как выбрать костюм")
    assert any("нет ни одного списка" in p for p in rep.problems)


def test_analyze_secondary_queries_in_headings():
    a = _article()
    a.blocks.insert(1, Block(BlockType.HEADING, "Какая плотность ткани нужна?"))
    a.blocks.insert(2, Block(BlockType.PARAGRAPH, "Плотность ткани измеряется в граммах."))
    rep = analyze(a, primary="как выбрать костюм", secondary=["плотность ткани"])
    assert any("вынесены в подзаголовки" in g for g in rep.good)


def test_analyze_takes_queries_from_topic():
    a = _article(topic=Topic(title="t", primary_query="как выбрать костюм"))
    rep = analyze(a)
    assert any("в заголовке" in g for g in rep.good)


def test_analyze_without_query_reports_problem():
    rep = analyze(_article())
    assert any("не задан главный поисковый запрос" in p for p in rep.problems)


def test_score_bounds():
    rep = analyze(_article(), primary="как выбрать костюм")
    assert 0 <= rep.score <= 100


def test_keyword_report_sorted_by_hits():
    a = _article()
    rows = keyword_report(a, ["как выбрать костюм", "рант"])
    assert rows[0][0] == "как выбрать костюм"
    assert rows[0][1] > rows[-1][1]
    assert rows[0][2] > 0
