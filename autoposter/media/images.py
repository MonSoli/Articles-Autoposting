"""Поиск и загрузка фотографий со свободной лицензией.

Источники подключаются по принципу «сначала то, что работает без ключа»:
Openverse, Wikimedia Commons и Met Museum не требуют регистрации, Unsplash
и Pexels подключаются, если вы завели ключи — у них лучше качество съёмки.

Все источники отдают материалы, пригодные для публикации, но условия разные:
часть требует указания автора. Атрибуция собирается автоматически и попадает
в подпись под фотографией — не отключайте её для лицензий, где она обязательна.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

log = logging.getLogger(__name__)

USER_AGENT = "vcru-autoposter/1.0 (article illustration; contact via repo)"
TIMEOUT = 25


# ----------------------------------------------------------------------
# модель результата
# ----------------------------------------------------------------------


@dataclass
class ImageResult:
    """Найденная фотография."""

    url: str                      # ссылка на полноразмерный файл
    source: str                   # openverse / wikimedia / met / unsplash / pexels
    title: str = ""
    author: str = ""
    author_url: str = ""
    page_url: str = ""            # страница фото на сайте источника
    license_name: str = ""
    license_url: str = ""
    width: int = 0
    height: int = 0
    thumb_url: str = ""
    local_path: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_attribution(self) -> bool:
        """Требует ли лицензия указания автора."""
        lic = self.license_name.lower()
        if not lic:
            return True                       # неизвестна — указываем на всякий случай
        if "cc0" in lic or "public domain" in lic or "pdm" in lic:
            return False
        if self.source in {"unsplash", "pexels"}:
            return False                      # не обязательна, но мы всё равно ставим
        return True

    @property
    def is_landscape(self) -> bool:
        return self.width > 0 and self.height > 0 and self.width >= self.height

    def attribution(self) -> str:
        """Строка атрибуции для подписи под фотографией."""
        parts: list[str] = []
        if self.author:
            parts.append(f"Фото: {self.author}")
        elif self.title:
            parts.append(self.title)
        else:
            parts.append("Фото")

        src_names = {
            "openverse": "Openverse",
            "wikimedia": "Wikimedia Commons",
            "met": "The Met",
            "unsplash": "Unsplash",
            "pexels": "Pexels",
        }
        if src := src_names.get(self.source):
            parts.append(src)
        if self.license_name:
            parts.append(self.license_name)
        return " · ".join(parts)


class Provider(Protocol):
    name: str

    def available(self) -> bool: ...

    def search(self, query: str, limit: int = 10) -> list[ImageResult]: ...


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------


def _get_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    import requests

    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    resp = requests.get(url, headers=h, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ----------------------------------------------------------------------
# источники без ключа
# ----------------------------------------------------------------------


class OpenverseProvider:
    """Openverse — агрегатор материалов под Creative Commons. Ключ не нужен."""

    name = "openverse"
    BASE = "https://api.openverse.org/v1/images/"

    def available(self) -> bool:
        return True

    def search(self, query: str, limit: int = 10) -> list[ImageResult]:
        url = (
            f"{self.BASE}?q={quote(query)}&page_size={limit}"
            f"&license_type=commercial,modification&aspect_ratio=wide&mature=false"
        )
        try:
            data = _get_json(url)
        except Exception as exc:
            log.warning("Openverse недоступен: %s", exc)
            return []

        out: list[ImageResult] = []
        for it in data.get("results", []):
            if not it.get("url"):
                continue
            out.append(
                ImageResult(
                    url=it["url"],
                    source=self.name,
                    title=it.get("title", ""),
                    author=it.get("creator", ""),
                    author_url=it.get("creator_url", ""),
                    page_url=it.get("foreign_landing_url", ""),
                    license_name=_cc_label(it.get("license", ""), it.get("license_version", "")),
                    license_url=it.get("license_url", ""),
                    width=int(it.get("width") or 0),
                    height=int(it.get("height") or 0),
                    thumb_url=it.get("thumbnail", ""),
                )
            )
        return out


class WikimediaProvider:
    """Wikimedia Commons. Ключ не нужен, много исторического материала."""

    name = "wikimedia"
    BASE = "https://commons.wikimedia.org/w/api.php"

    def available(self) -> bool:
        return True

    def search(self, query: str, limit: int = 10) -> list[ImageResult]:
        url = (
            f"{self.BASE}?action=query&format=json&generator=search"
            f"&gsrsearch={quote(query)}&gsrnamespace=6&gsrlimit={limit}"
            f"&prop=imageinfo&iiprop=url|size|extmetadata&iiurlwidth=1600"
        )
        try:
            data = _get_json(url)
        except Exception as exc:
            log.warning("Wikimedia недоступна: %s", exc)
            return []

        out: list[ImageResult] = []
        for page in (data.get("query", {}).get("pages", {}) or {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            if not info.get("url"):
                continue
            ext = info.get("extmetadata", {}) or {}

            def field_(key: str) -> str:
                return _strip_html(str(ext.get(key, {}).get("value", "")))

            out.append(
                ImageResult(
                    url=info.get("thumburl") or info["url"],
                    source=self.name,
                    title=page.get("title", "").removeprefix("File:"),
                    author=field_("Artist"),
                    page_url=info.get("descriptionurl", ""),
                    license_name=field_("LicenseShortName"),
                    license_url=field_("LicenseUrl"),
                    width=int(info.get("thumbwidth") or info.get("width") or 0),
                    height=int(info.get("thumbheight") or info.get("height") or 0),
                )
            )
        return out


class MetMuseumProvider:
    """Met Museum Open Access. Ключ не нужен, всё в public domain.

    Полезен для исторического костюма: коллекция Costume Institute.
    Ищет в два шага — сначала id объектов, затем карточки.
    """

    name = "met"
    SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1/search"
    OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects"

    def available(self) -> bool:
        return True

    def search(self, query: str, limit: int = 6) -> list[ImageResult]:
        try:
            found = _get_json(
                f"{self.SEARCH}?hasImages=true&isPublicDomain=true&q={quote(query)}"
            )
        except Exception as exc:
            log.warning("Met Museum недоступен: %s", exc)
            return []

        ids = (found.get("objectIDs") or [])[: limit * 2]
        out: list[ImageResult] = []
        for oid in ids:
            if len(out) >= limit:
                break
            try:
                obj = _get_json(f"{self.OBJECT}/{oid}")
            except Exception:
                continue
            img = obj.get("primaryImage") or ""
            if not img:
                continue
            out.append(
                ImageResult(
                    url=img,
                    source=self.name,
                    title=obj.get("title", ""),
                    author=obj.get("artistDisplayName", "") or obj.get("culture", ""),
                    page_url=obj.get("objectURL", ""),
                    license_name="Public Domain (CC0)",
                    license_url="https://creativecommons.org/publicdomain/zero/1.0/",
                    thumb_url=obj.get("primaryImageSmall", ""),
                    meta={"date": obj.get("objectDate", "")},
                )
            )
        return out


# ----------------------------------------------------------------------
# источники с ключом
# ----------------------------------------------------------------------


class UnsplashProvider:
    """Unsplash. Нужен бесплатный Access Key в UNSPLASH_ACCESS_KEY."""

    name = "unsplash"
    BASE = "https://api.unsplash.com/search/photos"

    def __init__(self, key: str = "") -> None:
        self.key = key or os.environ.get("UNSPLASH_ACCESS_KEY", "")

    def available(self) -> bool:
        return bool(self.key)

    def search(self, query: str, limit: int = 10) -> list[ImageResult]:
        if not self.available():
            return []
        url = f"{self.BASE}?query={quote(query)}&per_page={limit}&orientation=landscape"
        try:
            data = _get_json(url, headers={"Authorization": f"Client-ID {self.key}"})
        except Exception as exc:
            log.warning("Unsplash недоступен: %s", exc)
            return []

        out: list[ImageResult] = []
        for it in data.get("results", []):
            urls = it.get("urls", {})
            user = it.get("user", {})
            out.append(
                ImageResult(
                    url=urls.get("raw", "") or urls.get("full", "") or urls.get("regular", ""),
                    source=self.name,
                    title=it.get("description") or it.get("alt_description") or "",
                    author=user.get("name", ""),
                    author_url=user.get("links", {}).get("html", ""),
                    page_url=it.get("links", {}).get("html", ""),
                    license_name="Unsplash License",
                    license_url="https://unsplash.com/license",
                    width=int(it.get("width") or 0),
                    height=int(it.get("height") or 0),
                    thumb_url=urls.get("small", ""),
                )
            )
        return out


class PexelsProvider:
    """Pexels. Нужен бесплатный ключ в PEXELS_API_KEY."""

    name = "pexels"
    BASE = "https://api.pexels.com/v1/search"

    def __init__(self, key: str = "") -> None:
        self.key = key or os.environ.get("PEXELS_API_KEY", "")

    def available(self) -> bool:
        return bool(self.key)

    def search(self, query: str, limit: int = 10) -> list[ImageResult]:
        if not self.available():
            return []
        url = f"{self.BASE}?query={quote(query)}&per_page={limit}&orientation=landscape"
        try:
            data = _get_json(url, headers={"Authorization": self.key})
        except Exception as exc:
            log.warning("Pexels недоступен: %s", exc)
            return []

        out: list[ImageResult] = []
        for it in data.get("photos", []):
            src = it.get("src", {})
            out.append(
                ImageResult(
                    url=src.get("original", "") or src.get("large2x", ""),
                    source=self.name,
                    title=it.get("alt", ""),
                    author=it.get("photographer", ""),
                    author_url=it.get("photographer_url", ""),
                    page_url=it.get("url", ""),
                    license_name="Pexels License",
                    license_url="https://www.pexels.com/license/",
                    width=int(it.get("width") or 0),
                    height=int(it.get("height") or 0),
                    thumb_url=src.get("medium", ""),
                )
            )
        return out


# ----------------------------------------------------------------------
# поиск по нескольким источникам
# ----------------------------------------------------------------------

DEFAULT_ORDER = ["unsplash", "pexels", "openverse", "wikimedia", "met"]


class ImageSearcher:
    """Обходит источники по порядку и собирает подходящие фотографии."""

    def __init__(
        self,
        order: list[str] | None = None,
        *,
        min_width: int = 1200,
        landscape_only: bool = True,
        unsplash_key: str = "",
        pexels_key: str = "",
    ) -> None:
        self.min_width = min_width
        self.landscape_only = landscape_only
        registry: dict[str, Provider] = {
            "unsplash": UnsplashProvider(unsplash_key),
            "pexels": PexelsProvider(pexels_key),
            "openverse": OpenverseProvider(),
            "wikimedia": WikimediaProvider(),
            "met": MetMuseumProvider(),
        }
        self.providers = [
            registry[n] for n in (order or DEFAULT_ORDER) if n in registry
        ]

    def active_providers(self) -> list[str]:
        return [p.name for p in self.providers if p.available()]

    def search(self, query: str, limit: int = 5) -> list[ImageResult]:
        """Ищет по всем доступным источникам, пока не наберёт limit."""
        found: list[ImageResult] = []
        seen: set[str] = set()

        for provider in self.providers:
            if len(found) >= limit:
                break
            if not provider.available():
                continue
            log.debug("Поиск «%s» в %s", query, provider.name)
            for img in provider.search(query, limit=limit * 2):
                if len(found) >= limit:
                    break
                if not img.url or img.url in seen:
                    continue
                if not self._acceptable(img):
                    continue
                seen.add(img.url)
                found.append(img)

        log.info("«%s» — найдено %s фото (%s)", query, len(found),
                 ", ".join(sorted({i.source for i in found})) or "нет источников")
        return found

    def _acceptable(self, img: ImageResult) -> bool:
        # размеры известны не у всех источников — тогда пропускаем проверку
        if img.width and img.width < self.min_width:
            return False
        if self.landscape_only and img.width and img.height and not img.is_landscape:
            return False
        return True


# ----------------------------------------------------------------------
# скачивание
# ----------------------------------------------------------------------


def download(img: ImageResult, dest: Path, *, max_width: int = 1600) -> Path | None:
    """Скачивает фотографию, приводит к JPEG и ограничивает ширину.

    Returns:
        путь к файлу либо None, если скачать не удалось.
    """
    import requests
    from PIL import Image

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(
            img.url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True
        )
        resp.raise_for_status()
        tmp = dest.with_suffix(".download")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)

        with Image.open(tmp) as im:
            im = im.convert("RGB")
            if im.width > max_width:
                ratio = max_width / im.width
                im = im.resize((max_width, int(im.height * ratio)), Image.LANCZOS)
            im.save(dest, "JPEG", quality=88, optimize=True)
        tmp.unlink(missing_ok=True)

        img.local_path = str(dest)
        log.info("Скачано: %s ← %s", dest.name, img.source)
        return dest
    except Exception as exc:
        log.warning("Не удалось скачать %s: %s", img.url[:80], exc)
        return None


# ----------------------------------------------------------------------
# вспомогательное
# ----------------------------------------------------------------------


def _cc_label(code: str, version: str = "") -> str:
    """`by-sa` + `4.0` → `CC BY-SA 4.0`."""
    if not code:
        return ""
    code = code.strip().lower()
    if code in {"cc0", "zero"}:
        return "CC0"
    if code == "pdm":
        return "Public Domain"
    label = "CC " + code.upper()
    return f"{label} {version}".strip()


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Wikimedia отдаёт часть полей размеченными — вычищаем теги."""
    import html as html_mod

    return html_mod.unescape(_TAG_RE.sub("", text)).strip()
