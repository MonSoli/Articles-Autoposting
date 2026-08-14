"""Тесты поиска фотографий: разбор ответов API, отбор, атрибуция."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoposter.media import images as im  # noqa: E402
from autoposter.media.illustrator import (  # noqa: E402
    _compose_caption, _fallback_query, _parse_numbered, build_queries, illustrate,
)
from autoposter.models import Article, Block, BlockType  # noqa: E402


# ------------------------------------------------------------ разбор ответов

OPENVERSE_JSON = {
    "results": [
        {
            "url": "https://ex.org/a.jpg",
            "title": "Tailor at work",
            "creator": "Ivan Petrov",
            "creator_url": "https://ex.org/u",
            "foreign_landing_url": "https://ex.org/p/1",
            "license": "by-sa",
            "license_version": "4.0",
            "license_url": "https://cc.org/by-sa/4.0",
            "width": 2000,
            "height": 1300,
            "thumbnail": "https://ex.org/a-s.jpg",
        },
        {"title": "без url"},  # должен быть отброшен
    ]
}

WIKIMEDIA_JSON = {
    "query": {
        "pages": [
            {
                "title": "File:Suit.jpg",
                "imageinfo": [
                    {
                        "url": "https://up.wikimedia.org/Suit.jpg",
                        "thumburl": "https://up.wikimedia.org/Suit_1600.jpg",
                        "thumbwidth": 1600,
                        "thumbheight": 1000,
                        "descriptionurl": "https://commons.wikimedia.org/Suit",
                        "extmetadata": {
                            "Artist": {"value": '<a href="/u">Анна</a> Смирнова'},
                            "LicenseShortName": {"value": "CC BY 4.0"},
                            "LicenseUrl": {"value": "https://cc.org/by/4.0"},
                            "Categories": {"value": "Men's suits|Tailoring"},
                        },
                    }
                ],
            }
        ]
    }
}

MET_SEARCH_JSON = {"objectIDs": [101, 102]}
MET_OBJECT_JSON = {
    "isPublicDomain": True,
    "primaryImage": "https://met.org/1.jpg",
    "primaryImageSmall": "https://met.org/1s.jpg",
    "title": "Frock coat",
    "artistDisplayName": "",
    "culture": "British",
    "objectURL": "https://met.org/o/101",
    "objectDate": "1890",
}

UNSPLASH_JSON = {
    "results": [
        {
            "urls": {"raw": "https://un.com/raw", "small": "https://un.com/s"},
            "description": "Wool fabric",
            "user": {"name": "Jane Doe", "links": {"html": "https://un.com/@jane"}},
            "links": {"html": "https://un.com/photos/1"},
            "width": 3000,
            "height": 2000,
        }
    ]
}

PEXELS_JSON = {
    "photos": [
        {
            "src": {"original": "https://px.com/o.jpg", "medium": "https://px.com/m.jpg"},
            "alt": "Leather shoes",
            "photographer": "Max Mustermann",
            "photographer_url": "https://px.com/@max",
            "url": "https://px.com/photo/1",
            "width": 2500,
            "height": 1600,
        }
    ]
}


@pytest.fixture
def fake_get(monkeypatch):
    """Подменяет HTTP-слой; возвращает список запрошенных URL."""
    calls: list[str] = []

    def _make(mapping):
        def fake(url, headers=None):
            calls.append(url)
            for frag, payload in mapping.items():
                if frag in url:
                    return payload
            raise AssertionError(f"неожиданный запрос: {url}")

        monkeypatch.setattr(im, "_get_json", fake)
        return calls

    return _make


def test_openverse_parsing(fake_get):
    fake_get({"openverse": OPENVERSE_JSON})
    res = im.OpenverseProvider().search("tailor", limit=5)
    assert len(res) == 1                       # запись без url отброшена
    r = res[0]
    assert r.url == "https://ex.org/a.jpg"
    assert r.author == "Ivan Petrov"
    assert r.license_name == "CC BY-SA 4.0"
    assert r.width == 2000 and r.is_landscape
    assert r.needs_attribution


def test_wikimedia_parsing_strips_html(fake_get):
    fake_get({"commons.wikimedia.org": WIKIMEDIA_JSON})
    res = im.WikimediaProvider().search("suit", limit=5)
    assert len(res) == 1
    r = res[0]
    assert r.author == "Анна Смирнова"          # теги вычищены
    assert r.title == "Suit.jpg"
    assert r.url.endswith("Suit_1600.jpg")      # берётся уменьшенная версия
    assert r.license_name == "CC BY 4.0"


def test_met_parsing_is_public_domain(fake_get):
    fake_get({"/search": MET_SEARCH_JSON, "/objects/": MET_OBJECT_JSON})
    res = im.MetMuseumProvider().search("frock coat", limit=1)
    assert len(res) == 1
    r = res[0]
    assert r.author == "British"                # culture как запасное поле автора
    assert not r.needs_attribution              # public domain
    assert "The Met" in r.attribution()


def test_unsplash_requires_key(fake_get):
    fake_get({"unsplash": UNSPLASH_JSON})
    assert im.UnsplashProvider(key="").search("wool") == []
    res = im.UnsplashProvider(key="k").search("wool", limit=1)
    assert res[0].author == "Jane Doe"
    assert not res[0].needs_attribution


def test_pexels_parsing(fake_get):
    fake_get({"pexels": PEXELS_JSON})
    res = im.PexelsProvider(key="k").search("shoes", limit=1)
    assert res[0].url == "https://px.com/o.jpg"
    assert res[0].author == "Max Mustermann"


def test_provider_survives_network_error(monkeypatch):
    def boom(url, headers=None):
        raise ConnectionError("нет сети")

    monkeypatch.setattr(im, "_get_json", boom)
    # сбой источника не должен ронять поиск
    assert im.OpenverseProvider().search("x") == []
    assert im.WikimediaProvider().search("x") == []
    assert im.MetMuseumProvider().search("x") == []


# ------------------------------------------------------------------ отбор

def _res(**kw):
    base = dict(url="u", source="openverse", width=2000, height=1200,
                title="wool suit jacket", tags=["suit", "wool"])
    base.update(kw)
    return im.ImageResult(**base)


def test_technical_gate_filters_small_and_portrait():
    g = im.Scorer(min_width=1200).technical_gate
    assert g(_res())
    assert not g(_res(width=800))
    assert not g(_res(width=1300, height=2000))
    # неизвестные размеры пропускаем, а не отбрасываем
    assert g(_res(width=0, height=0))


def test_searcher_dedupes(monkeypatch):
    same = [_res(url="dup"), _res(url="dup"), _res(url="other")]
    monkeypatch.setattr(im.OpenverseProvider, "search", lambda self, q, limit=10: same)
    monkeypatch.setattr(im.WikimediaProvider, "search", lambda self, q, limit=10: same)
    found = im.ImageSearcher(order=["openverse", "wikimedia"]).search("q", limit=5)
    assert sorted(f.url for f in found) == ["dup", "other"]


def test_active_providers_without_keys():
    """Met, Wikimedia и Openverse работают без ключей; Pexels — нет."""
    active = im.ImageSearcher(pexels_key="").active_providers()
    assert active == ["met", "wikimedia", "openverse"]


def test_active_providers_with_pexels_key():
    active = im.ImageSearcher(pexels_key="b").active_providers()
    assert "pexels" in active


def test_unsplash_excluded_when_files_must_be_stored():
    """Условия Unsplash запрещают хранение файлов — источник отбрасывается."""
    order = ["unsplash", "met"]
    assert "unsplash" not in im.ImageSearcher(
        order=order, unsplash_key="k", require_downloadable=True
    ).active_providers()
    assert "unsplash" in im.ImageSearcher(
        order=order, unsplash_key="k", require_downloadable=False
    ).active_providers()


def test_download_refuses_unsplash(tmp_path):
    assert im.download(_res(source="unsplash"), tmp_path / "a.jpg") is None


# ------------------------------------------------------------- атрибуция

def test_attribution_formats():
    r = _res(source="wikimedia", author="Иван", license_name="CC BY-SA 4.0")
    assert r.attribution() == "Фото: Иван · Wikimedia Commons · CC BY-SA 4.0"


def test_cc0_needs_no_attribution():
    assert not _res(source="met", license_name="CC0").needs_attribution
    assert not _res(source="wikimedia", license_name="Public Domain").needs_attribution


def test_unknown_license_defaults_to_attributing():
    assert _res(source="wikimedia", license_name="").needs_attribution


def test_cc_label():
    assert im._cc_label("by", "4.0") == "CC BY 4.0"
    assert im._cc_label("cc0") == "CC0"
    assert im._cc_label("pdm") == "Public Domain"
    assert im._cc_label("") == ""


# ------------------------------------------------------ составление запросов

def test_fallback_query_matches_keywords():
    assert _fallback_query("Крупный план лацкана") == "suit jacket lapel closeup"
    assert _fallback_query("Рант ботинка") == "goodyear welted shoe sole"
    assert _fallback_query("Нечто непонятное") == "classic menswear tailoring"


def test_parse_numbered_handles_gaps():
    assert _parse_numbered("1. one\n3. three", 3) == ["one", "", "three"]


def test_build_queries_uses_backend():
    class Fake:
        def ask(self, prompt, **kw):
            return "1. suit lapel closeup\n2. wool fabric texture"

    assert build_queries(["лацкан", "ткань"], Fake()) == [
        "suit lapel closeup", "wool fabric texture",
    ]


def test_build_queries_falls_back_on_error():
    class Broken:
        def ask(self, prompt, **kw):
            raise RuntimeError("нет связи")

    assert build_queries(["Крупный план лацкана"], Broken()) == [
        "suit jacket lapel closeup"
    ]


def test_build_queries_patches_partial_answer():
    class Partial:
        def ask(self, prompt, **kw):
            return "1. suit lapel closeup"       # второй пропущен

    out = build_queries(["лацкан", "Рант ботинка"], Partial())
    assert out == ["suit lapel closeup", "goodyear welted shoe sole"]


def test_build_queries_empty_input():
    assert build_queries([]) == []


# -------------------------------------------------------------- подписи

def test_caption_keeps_meaning_and_adds_credit():
    r = _res(source="pexels", author="Max", license_name="Pexels License")
    out = _compose_caption("Крупный план лацкана (фото сверху)", r)
    assert out.startswith("Крупный план лацкана")
    assert "фото сверху" not in out              # техническая скобка убрана
    assert "Max" in out


def test_caption_truncates_long_description():
    out = _compose_caption("слово " * 100, _res())
    assert len(out) < 260 and "…" in out


def test_caption_without_description():
    assert _compose_caption("", _res(author="Иван")).startswith("Фото: Иван")


# ------------------------------------------------------------- illustrate

def test_illustrate_attaches_and_skips_duplicates(monkeypatch, tmp_path):
    article = Article(
        blocks=[
            Block(BlockType.PARAGRAPH, "текст"),
            Block(BlockType.IMAGE, caption="Крупный план лацкана"),
            Block(BlockType.IMAGE, caption="Рант ботинка"),
        ]
    )

    monkeypatch.setattr(
        im.ImageSearcher, "active_providers", lambda self: ["openverse"]
    )
    monkeypatch.setattr(
        im.ImageSearcher, "search",
        lambda self, q, limit=5: [_res(url="same", author="Автор")],
    )

    saved: list[Path] = []

    def fake_download(img, dest, max_width=1600):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x")
        saved.append(Path(dest))
        return Path(dest)

    monkeypatch.setattr("autoposter.media.illustrator.download", fake_download)

    n = illustrate(article, tmp_path, backend=None)
    # обе картинки нашли один и тот же url — второй отбрасывается как дубль
    assert n == 1
    assert len(saved) == 1
    first = article.blocks[1]
    assert first.meta["path"] == str(saved[0])
    assert first.meta["query"] == "suit jacket lapel closeup"
    assert "Автор" in first.caption


def test_illustrate_without_slots(tmp_path):
    article = Article(blocks=[Block(BlockType.PARAGRAPH, "текст")])
    assert illustrate(article, tmp_path, backend=None) == 0


def test_illustrate_without_providers(monkeypatch, tmp_path):
    article = Article(blocks=[Block(BlockType.IMAGE, caption="лацкан")])
    monkeypatch.setattr(im.ImageSearcher, "active_providers", lambda self: [])
    assert illustrate(article, tmp_path, backend=None) == 0


def test_illustrate_respects_max_images(monkeypatch, tmp_path):
    article = Article(blocks=[Block(BlockType.IMAGE, caption=f"фото {i}") for i in range(6)])
    monkeypatch.setattr(im.ImageSearcher, "active_providers", lambda self: ["openverse"])
    counter = {"n": 0}

    def search(self, q, limit=5):
        counter["n"] += 1
        return [_res(url=f"u{counter['n']}")]

    monkeypatch.setattr(im.ImageSearcher, "search", search)
    monkeypatch.setattr(
        "autoposter.media.illustrator.download",
        lambda img, dest, max_width=1600: Path(dest),
    )
    assert illustrate(article, tmp_path, backend=None, max_images=2) == 2
    assert counter["n"] == 2


def test_illustrate_survives_download_failure(monkeypatch, tmp_path):
    article = Article(blocks=[Block(BlockType.IMAGE, caption="лацкан")])
    monkeypatch.setattr(im.ImageSearcher, "active_providers", lambda self: ["openverse"])
    monkeypatch.setattr(im.ImageSearcher, "search", lambda self, q, limit=5: [_res()])
    monkeypatch.setattr(
        "autoposter.media.illustrator.download", lambda img, dest, max_width=1600: None
    )
    assert illustrate(article, tmp_path, backend=None) == 0
    assert not article.blocks[0].meta.get("path")
