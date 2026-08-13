"""Тесты разбора, форматирования и проверки материалов."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoposter.generation.pipeline import (  # noqa: E402
    _clean_title, _parse_kv, _parse_list, _best_headline, _pick_subsite,
    _split_tags, _strip_fences,
)
from autoposter.models import (  # noqa: E402
    Article, Block, BlockType, Topic, parse_markdown_to_blocks,
)
from autoposter.publishing.formatter import (  # noqa: E402
    blocks_to_html, blocks_to_plain, validate,
)
from autoposter import topics as topics_mod  # noqa: E402


# ---------------------------------------------------------------- разбор md

def test_parse_headings_and_lists():
    md = """Первый абзац крючка.

## Как проверить ткань

Второй абзац.

- признак один
- признак два

1. шаг первый
2. шаг второй

> Короткий вывод.

[ЦИФРА] 84 000 ₽ | средняя цена костюма на заказ
[ИЛЛЮСТРАЦИЯ] крупный план лацкана
---
[ОПРОС] Сколько вы тратите на костюм?
- до 50 000
- больше
"""
    blocks = parse_markdown_to_blocks(md)
    types = [b.type for b in blocks]

    assert BlockType.HEADING in types
    assert BlockType.BULLET_LIST in types
    assert BlockType.NUMBER_LIST in types
    assert BlockType.QUOTE in types
    assert BlockType.NUMBER_CARD in types
    assert BlockType.IMAGE in types
    assert BlockType.DELIMITER in types
    assert BlockType.POLL in types

    card = next(b for b in blocks if b.type is BlockType.NUMBER_CARD)
    assert card.text == "84 000 ₽"
    assert "средняя цена" in card.caption

    poll = next(b for b in blocks if b.type is BlockType.POLL)
    assert len(poll.items) == 2

    bullets = next(b for b in blocks if b.type is BlockType.BULLET_LIST)
    assert bullets.items == ["признак один", "признак два"]


def test_paragraph_merging():
    blocks = parse_markdown_to_blocks("Строка одна\nстрока два\n\nДругой абзац.")
    paras = [b for b in blocks if b.type is BlockType.PARAGRAPH]
    assert len(paras) == 2
    assert paras[0].text == "Строка одна строка два"


def test_table_becomes_list():
    md = "| Параметр | Норма |\n|---|---|\n| Плечо | по кости |\n| Рукав | 1 см манжеты |"
    blocks = parse_markdown_to_blocks(md)
    lst = next(b for b in blocks if b.type is BlockType.BULLET_LIST)
    assert lst.items == ["Плечо — по кости", "Рукав — 1 см манжеты"]


# ------------------------------------------------------------ разбор ответов

def test_parse_kv_multiline():
    raw = """УГОЛ: дорого не значит хорошо
ОБЕЩАНИЕ: читатель научится
проверять ткань руками
ГЛАВНЫЙ_ЗАПРОС: как выбрать костюм"""
    kv = _parse_kv(raw)
    assert kv["УГОЛ"] == "дорого не значит хорошо"
    assert "проверять ткань" in kv["ОБЕЩАНИЕ"]
    assert kv["ГЛАВНЫЙ_ЗАПРОС"] == "как выбрать костюм"


def test_parse_list_stops_at_next_key():
    raw = "ФАКТЧЕК:\n- цена 84 000 ₽\n- срок 11 недель\nТЕГИ: стиль, костюм"
    assert _parse_list(raw, "ФАКТЧЕК") == ["цена 84 000 ₽", "срок 11 недель"]


def test_best_headline_picks_marked():
    parsed = {
        "ЗАГОЛОВКИ": "1. Первый\n2. Второй\n3. Третий",
        "ЛУЧШИЙ": "2 — самый конкретный",
    }
    assert _best_headline(parsed) == "Второй"


def test_clean_title_trims_to_90():
    long = "Очень длинный заголовок " * 10
    out = _clean_title(long)
    assert len(out) <= 90
    assert not out.endswith(("-", ",", " "))


def test_clean_title_strips_quotes():
    assert _clean_title('  «Заголовок в кавычках»  ') == "Заголовок в кавычках"


def test_pick_subsite_falls_back():
    assert _pick_subsite("/life") == "life"
    assert _pick_subsite("Мода") == "fashion"
    assert _pick_subsite("что-то непонятное") == "fashion"


def test_split_tags_limits_to_five():
    tags = _split_tags("#Стиль, костюм; Мода, обувь, ткани, лишний")
    assert tags == ["стиль", "костюм", "мода", "обувь", "ткани"]


def test_strip_fences():
    assert _strip_fences("```markdown\n# Заголовок\n```") == "# Заголовок"
    assert _strip_fences("обычный текст") == "обычный текст"


# ------------------------------------------------------------- форматирование

def _sample_article(**kw) -> Article:
    """Материал, проходящий все проверки: объём, подзаголовки, короткие абзацы."""
    para = "Короткий абзац с конкретикой и цифрой 84 000 рублей внутри него. " * 6  # ~390 знаков
    blocks: list[Block] = [Block(BlockType.PARAGRAPH, para)]
    for n in range(8):
        blocks.append(Block(BlockType.HEADING, f"Содержательный подзаголовок {n}"))
        blocks.append(Block(BlockType.PARAGRAPH, para))
        blocks.append(Block(BlockType.PARAGRAPH, para))
    blocks.append(Block(BlockType.BULLET_LIST, items=["плечо", "рукав", "корпус"]))

    base = dict(
        title="Почему костюм за 200 000 ₽ сидит хуже костюма за 40 000 ₽",
        subtitle="Разбираем на конкретных примерах, за что вы платите и что решает посадку.",
        blocks=blocks,
        tags=["стиль", "костюм", "мода"],
        cover_path="/tmp/c.jpg",
    )
    base.update(kw)
    return Article(**base)


def test_html_conversion_preserves_structure():
    html = blocks_to_html(_sample_article())
    assert "<h2>Содержательный подзаголовок 0</h2>" in html
    assert "<ul><li>плечо</li>" in html


def test_html_escapes_content():
    a = Article(blocks=[Block(BlockType.PARAGRAPH, "<script>alert(1)</script>")])
    assert "<script>" not in blocks_to_html(a)
    assert "&lt;script&gt;" in blocks_to_html(a)


def test_plain_fallback_keeps_text():
    plain = blocks_to_plain(_sample_article())
    assert "Содержательный подзаголовок 0" in plain
    assert "— плечо" in plain


def test_validate_clean_article():
    a = _sample_article()
    assert a.char_count >= 6000
    assert validate(a) == []


def test_validate_catches_problems():
    a = _sample_article(title="К" * 120, subtitle="", tags=[], cover_path=None)
    problems = " ".join(validate(a))
    assert "заголовок 120 знаков" in problems
    assert "подзаголовк" in problems
    assert "нет тегов" in problems
    assert "обложки" in problems


def test_validate_flags_short_article():
    a = _sample_article(blocks=[Block(BlockType.PARAGRAPH, "мало текста")])
    assert any("мало для попадания" in p for p in validate(a))


def test_validate_flags_ai_cliches():
    a = _sample_article()
    a.blocks.append(Block(BlockType.PARAGRAPH, "В современном мире важно отметить это."))
    assert any("штампы" in p for p in validate(a))


def test_validate_flags_long_paragraph():
    a = _sample_article()
    a.blocks.append(Block(BlockType.PARAGRAPH, "слово " * 200))
    assert any("длинный абзац" in p for p in validate(a))


# -------------------------------------------------------------------- темы

def test_priority_formula():
    t = Topic(title="x", virality=5, seo=4, teaching=5, uniqueness=5)
    assert t.priority == 5 * 2 + 4 + 5 + 5 * 2


def test_plan_alternates_categories():
    picked = topics_mod.plan(topics_mod.BUILTIN, set(), 6)
    assert len(picked) == 6
    cats = [t.category for t in picked]
    assert not any(a == b for a, b in zip(cats, cats[1:])), cats


def test_plan_skips_used():
    used = {topics_mod.BUILTIN[0].title}
    assert all(t.title not in used for t in topics_mod.plan(topics_mod.BUILTIN, used, 5))


def test_pick_next_returns_highest_priority():
    best = topics_mod.pick_next(topics_mod.BUILTIN, set())
    assert best is not None
    assert best.priority == max(t.priority for t in topics_mod.BUILTIN)


def test_topic_bank_is_sane():
    assert len(topics_mod.BUILTIN) >= 40
    assert len({t.title for t in topics_mod.BUILTIN}) == len(topics_mod.BUILTIN)


# ------------------------------------------------------------- сериализация

def test_article_roundtrip(tmp_path):
    a = _sample_article()
    a.fact_checks = ["проверить цену 84 000 ₽"]
    p = a.save(tmp_path / "a.json")
    b = Article.load(p)
    assert b.title == a.title
    assert b.char_count == a.char_count
    assert b.fact_checks == a.fact_checks
    assert [x.type for x in b.blocks] == [x.type for x in a.blocks]


def test_markdown_render_includes_factchecks():
    a = _sample_article()
    a.fact_checks = ["цена костюма"]
    md = a.to_markdown()
    assert "## Содержательный подзаголовок 0" in md
    assert "цена костюма" in md


# ------------------------------------------- очистка ответов и штампы

def test_strip_preamble_removes_model_chatter():
    from autoposter.generation.pipeline import _clean_output

    raw = "Good — within target range. Final text below.\n\nЯ слышал эту фразу в примерочной."
    assert _clean_output(raw).startswith("Я слышал")


def test_strip_preamble_removes_russian_chatter_and_trailer():
    from autoposter.generation.pipeline import _clean_output

    raw = "Вот финальный текст:\n\n## Заголовок\n\nТекст.\n\nГотовый текст выше."
    out = _clean_output(raw)
    assert out.startswith("## Заголовок")
    assert "выше" not in out


def test_strip_preamble_keeps_real_content():
    from autoposter.generation.pipeline import _clean_output

    body = "Я слышал эту фразу десятки раз.\n\n## Что это значит\n\nРазбираемся."
    assert _clean_output(body) == body


def test_strip_preamble_ignores_matches_inside_body():
    from autoposter.generation.pipeline import _clean_output

    body = (
        "Первый абзац крючка, достаточно длинный чтобы не выглядеть служебным.\n\n"
        "Вот финальный текст договора, который мне прислали из ателье.\n\n"
        "Последний абзац."
    )
    assert "договора" in _clean_output(body)


def test_ai_cliche_ignores_plain_ne_tolko():
    from autoposter.publishing.formatter import find_ai_cliches

    assert find_ai_cliches("Смотреть надо не только на этикетке магазина?") == []


def test_ai_cliche_catches_full_construction():
    from autoposter.publishing.formatter import find_ai_cliches

    hits = find_ai_cliches("Костюм не только красивый, но и практичный.")
    assert "не только…, но и" in hits


def test_ai_cliche_catches_known_stamps():
    from autoposter.publishing.formatter import find_ai_cliches

    hits = find_ai_cliches("В современном мире это играет ключевую роль. Важно отметить.")
    assert len(hits) == 3
