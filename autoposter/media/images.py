"""Поиск и загрузка фотографий со свободной лицензией.

Порядок источников выбран по итогам разбора условий каждого API:

1. **Met Museum** — основной. Ключ не нужен вообще, всё под CC0, изображения
   2000–4000 px. Главное: у музея **контролируемая онтология** (`objectName`,
   `classification`, `medium`), поэтому точность автоматического отбора
   несопоставимо выше, чем по пользовательским тегам фотостоков.
2. **Pexels** — современные кадры там, где музей бессилен. Разрешает хранить
   файлы у себя.
3. **Wikimedia Commons** — исторический и справочный слой, ключ не нужен.
4. **Openverse** — добор через агрегатор.

**Unsplash сознательно исключён из умолчаний.** Его условия требуют показывать
изображения **только по их ссылкам** (хотлинк) и запрещают раздавать со своей
стороны. Мы файлы скачиваем и загружаем в редактор vc.ru — то есть неизбежно
рехостим. Провайдер оставлен в коде, но помечен `allows_download = False`
и при скачивании пропускается.

Pixabay, наоборот, **требует** хранить файлы у себя и запрещает постоянный хотлинк.
Единой политики хранения не существует — отсюда флаг у каждого источника.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

log = logging.getLogger(__name__)

# Wikimedia требует осмысленный User-Agent с контактом, иначе отдаёт 403.
DEFAULT_CONTACT = os.environ.get("AUTOPOSTER_CONTACT", "https://github.com/")
USER_AGENT = f"vcru-autoposter/1.0 (иллюстрации к статьям; {DEFAULT_CONTACT})"
TIMEOUT = 25


# ----------------------------------------------------------------------
# модель результата
# ----------------------------------------------------------------------


@dataclass
class ImageResult:
    """Найденная фотография."""

    url: str
    source: str
    title: str = ""
    author: str = ""
    author_url: str = ""
    page_url: str = ""
    license_name: str = ""
    license_url: str = ""
    width: int = 0
    height: int = 0
    thumb_url: str = ""
    local_path: str = ""
    tags: list[str] = field(default_factory=list)
    popularity: int = 0
    # готовая строка атрибуции, если источник её отдаёт (Openverse)
    ready_attribution: str = ""
    score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_attribution(self) -> bool:
        lic = self.license_name.lower()
        if "cc0" in lic or "public domain" in lic or lic == "pdm":
            return False
        if self.source in {"unsplash", "pexels", "pixabay"}:
            return False        # юридически не обязательна, но мы её всё равно ставим
        return True

    @property
    def is_landscape(self) -> bool:
        return self.width > 0 and self.height > 0 and self.width >= self.height

    def attribution(self) -> str:
        """Атрибуция по схеме TASL: название — автор — источник — лицензия."""
        if self.ready_attribution:
            return self.ready_attribution

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
            "pixabay": "Pixabay",
        }
        if src := src_names.get(self.source):
            parts.append(src)
        if self.license_name:
            parts.append(self.license_name)
        return " · ".join(parts)


class Provider(Protocol):
    name: str
    allows_download: bool

    def available(self) -> bool: ...

    def search(self, query: str, limit: int = 10) -> list[ImageResult]: ...


# ----------------------------------------------------------------------
# HTTP с ограничением частоты
# ----------------------------------------------------------------------

_last_call: dict[str, float] = {}


def _throttle(key: str, min_interval: float) -> None:
    """Выдерживает паузу между обращениями к одному источнику."""
    prev = _last_call.get(key, 0.0)
    wait = min_interval - (time.monotonic() - prev)
    if wait > 0:
        time.sleep(wait)
    _last_call[key] = time.monotonic()


def _get_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    import requests

    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    resp = requests.get(url, headers=h, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ----------------------------------------------------------------------
# источники
# ----------------------------------------------------------------------


class MetMuseumProvider:
    """Met Museum Open Access — основной источник.

    Ключ не нужен. Всё, что помечено `isPublicDomain`, доступно под CC0:
    можно скачивать, хранить у себя, изменять, использовать коммерчески.

    Отдел 8 — Costume Institute, крупнейшее открытое собрание костюма.
    Поиск двухшаговый: сначала идентификаторы, затем карточки, поэтому
    ограничиваем число добираемых объектов.
    """

    name = "met"
    allows_download = True
    SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1/search"
    OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects"
    COSTUME_INSTITUTE = 8

    def __init__(self, *, department: int | None = COSTUME_INSTITUTE,
                 date_begin: int = 1850, date_end: int = 1980) -> None:
        self.department = department
        self.date_begin = date_begin
        self.date_end = date_end

    def available(self) -> bool:
        return True

    def search(self, query: str, limit: int = 6) -> list[ImageResult]:
        url = (
            f"{self.SEARCH}?hasImages=true&isPublicDomain=true&q={quote(query)}"
            f"&dateBegin={self.date_begin}&dateEnd={self.date_end}"
        )
        if self.department is not None:
            url += f"&departmentId={self.department}"
        try:
            found = _get_json(url)
        except Exception as exc:
            log.warning("Met Museum недоступен: %s", exc)
            return []

        ids = (found.get("objectIDs") or [])[: limit * 2]
        out: list[ImageResult] = []
        for oid in ids:
            if len(out) >= limit:
                break
            _throttle("met", 0.05)          # музей просит не превышать ~80 rps
            try:
                obj = _get_json(f"{self.OBJECT}/{oid}")
            except Exception:
                continue
            if not obj.get("isPublicDomain"):
                continue                     # часть карточек с ограничениями
            img = obj.get("primaryImage") or ""
            if not img:
                continue

            tags = [t.get("term", "") for t in (obj.get("tags") or []) if t.get("term")]
            for key in ("objectName", "classification", "medium", "culture"):
                if val := obj.get(key):
                    tags.append(str(val))

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
                    tags=tags,
                    meta={"date": obj.get("objectDate", ""),
                          "classification": obj.get("classification", "")},
                )
            )
        return out


class PexelsProvider:
    """Pexels. Нужен бесплатный ключ в PEXELS_API_KEY. Хранение у себя разрешено."""

    name = "pexels"
    allows_download = True
    BASE = "https://api.pexels.com/v1/search"

    def __init__(self, key: str = "") -> None:
        self.key = key or os.environ.get("PEXELS_API_KEY", "")

    def available(self) -> bool:
        return bool(self.key)

    def search(self, query: str, limit: int = 10) -> list[ImageResult]:
        if not self.available():
            return []
        _throttle("pexels", 0.2)             # лимит 200 запросов в час
        url = (
            f"{self.BASE}?query={quote(query)}&per_page={min(limit, 80)}"
            f"&orientation=landscape&size=large"
        )
        try:
            data = _get_json(url, headers={"Authorization": self.key})
        except Exception as exc:
            log.warning("Pexels недоступен: %s", exc)
            return []

        out: list[ImageResult] = []
        for it in data.get("photos", []):
            src = it.get("src", {})
            alt = it.get("alt", "") or ""
            out.append(
                ImageResult(
                    url=src.get("original", "") or src.get("large2x", ""),
                    source=self.name,
                    title=alt,
                    author=it.get("photographer", ""),
                    author_url=it.get("photographer_url", ""),
                    page_url=it.get("url", ""),
                    license_name="Pexels License",
                    license_url="https://www.pexels.com/license/",
                    width=int(it.get("width") or 0),
                    height=int(it.get("height") or 0),
                    thumb_url=src.get("medium", ""),
                    tags=alt.lower().split(),
                    meta={"avg_color": it.get("avg_color", "")},
                )
            )
        return out


class WikimediaProvider:
    """Wikimedia Commons. Ключ не нужен, но обязателен User-Agent с контактом.

    Поддерживает два режима: полнотекстовый поиск и обход по категории.
    Для нашей темы категорийный обход точнее — это ручная классификация.
    """

    name = "wikimedia"
    allows_download = True
    BASE = "https://commons.wikimedia.org/w/api.php"

    def available(self) -> bool:
        return True

    def search(self, query: str, limit: int = 10) -> list[ImageResult]:
        _throttle("wikimedia", 0.5)
        url = (
            f"{self.BASE}?action=query&format=json&formatversion=2"
            f"&generator=search&gsrsearch={quote('filetype:bitmap ' + query)}"
            f"&gsrnamespace=6&gsrlimit={limit}"
            f"&prop=imageinfo&iiprop=url|size|mime|extmetadata&iiurlwidth=1600"
            f"&maxlag=5"
        )
        try:
            data = _get_json(url)
        except Exception as exc:
            log.warning("Wikimedia недоступна: %s", exc)
            return []

        out: list[ImageResult] = []
        for page in data.get("query", {}).get("pages", []) or []:
            info = (page.get("imageinfo") or [{}])[0]
            if not info.get("url"):
                continue
            ext = info.get("extmetadata", {}) or {}

            def field_(key: str) -> str:
                return _strip_html(str(ext.get(key, {}).get("value", "")))

            # права личности и товарные знаки — для коммерческого блога рискованно
            restrictions = field_("Restrictions").lower()
            if any(flag in restrictions for flag in ("personality", "trademark")):
                log.debug("Пропускаю из-за ограничений: %s", page.get("title"))
                continue

            out.append(
                ImageResult(
                    url=info.get("thumburl") or info["url"],
                    source=self.name,
                    title=str(page.get("title", "")).removeprefix("File:"),
                    author=field_("Artist"),
                    page_url=info.get("descriptionurl", ""),
                    license_name=field_("LicenseShortName"),
                    license_url=field_("LicenseUrl"),
                    width=int(info.get("thumbwidth") or info.get("width") or 0),
                    height=int(info.get("thumbheight") or info.get("height") or 0),
                    tags=[t for t in field_("Categories").split("|") if t],
                )
            )
        return out


class OpenverseProvider:
    """Openverse — агрегатор материалов под Creative Commons.

    Без токена лимит смехотворный (порядка сотни запросов в сутки), поэтому
    при наличии client_id и client_secret получаем токен автоматически.
    Ценная особенность: поле `attribution` — готовая корректная строка.
    """

    name = "openverse"
    allows_download = True
    BASE = "https://api.openverse.org/v1/images/"
    TOKEN_URL = "https://api.openverse.org/v1/auth_tokens/token/"

    def __init__(self, client_id: str = "", client_secret: str = "") -> None:
        self.client_id = client_id or os.environ.get("OPENVERSE_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("OPENVERSE_CLIENT_SECRET", "")
        self._token: str | None = None

    def available(self) -> bool:
        return True

    def _auth_header(self) -> dict[str, str]:
        if not (self.client_id and self.client_secret):
            return {}
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        try:
            import requests

            resp = requests.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            self._token = resp.json().get("access_token")
            if self._token:
                log.debug("Openverse: токен получен")
                return {"Authorization": f"Bearer {self._token}"}
        except Exception as exc:
            log.warning("Openverse: не удалось получить токен (%s), работаю анонимно", exc)
        return {}

    def search(self, query: str, limit: int = 10) -> list[ImageResult]:
        _throttle("openverse", 1.0)
        url = (
            f"{self.BASE}?q={quote(query)}&page_size={limit}"
            f"&license_type=commercial,modification&aspect_ratio=wide"
            f"&category=photograph&filter_dead=true&mature=false"
        )
        try:
            data = _get_json(url, headers=self._auth_header())
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
                    tags=[t.get("name", "") for t in (it.get("tags") or [])],
                    ready_attribution=it.get("attribution", ""),
                )
            )
        return out


class PixabayProvider:
    """Pixabay. Требует скачивания к себе и кэширования ответов на сутки.

    Держим как резерв: много изображений, сгенерированных нейросетями,
    а для темы кроя достоверность критична — такие кадры отсеиваются
    скорингом по стоп-словам.
    """

    name = "pixabay"
    allows_download = True
    BASE = "https://pixabay.com/api/"

    def __init__(self, key: str = "") -> None:
        self.key = key or os.environ.get("PIXABAY_API_KEY", "")

    def available(self) -> bool:
        return bool(self.key)

    def search(self, query: str, limit: int = 10) -> list[ImageResult]:
        if not self.available():
            return []
        _throttle("pixabay", 0.6)            # 100 запросов за 60 секунд
        url = (
            f"{self.BASE}?key={self.key}&q={quote(query)}&image_type=photo"
            f"&orientation=horizontal&min_width=1200&safesearch=true"
            f"&order=popular&per_page={max(3, min(limit, 200))}"
        )
        try:
            data = _get_json(url)
        except Exception as exc:
            log.warning("Pixabay недоступен: %s", exc)
            return []

        out: list[ImageResult] = []
        for it in data.get("hits", []):
            out.append(
                ImageResult(
                    url=it.get("largeImageURL", "") or it.get("webformatURL", ""),
                    source=self.name,
                    title=it.get("tags", ""),
                    author=it.get("user", ""),
                    page_url=it.get("pageURL", ""),
                    license_name="Pixabay Content License",
                    license_url="https://pixabay.com/service/terms/",
                    width=int(it.get("imageWidth") or 0),
                    height=int(it.get("imageHeight") or 0),
                    thumb_url=it.get("previewURL", ""),
                    tags=[t.strip() for t in str(it.get("tags", "")).split(",") if t.strip()],
                    popularity=int(it.get("downloads") or 0),
                )
            )
        return out


class UnsplashProvider:
    """Unsplash — только для просмотра, файлы скачивать нельзя.

    Условия сервиса требуют показывать изображения по возвращённым ссылкам
    и запрещают раздачу со своей стороны. Так как мы загружаем файлы
    в редактор vc.ru, источник по умолчанию не используется: он помечен
    `allows_download = False` и отбрасывается перед скачиванием.
    """

    name = "unsplash"
    allows_download = False
    BASE = "https://api.unsplash.com/search/photos"

    def __init__(self, key: str = "") -> None:
        self.key = key or os.environ.get("UNSPLASH_ACCESS_KEY", "")

    def available(self) -> bool:
        return bool(self.key)

    def search(self, query: str, limit: int = 10) -> list[ImageResult]:
        if not self.available():
            return []
        _throttle("unsplash", 0.8)
        url = (
            f"{self.BASE}?query={quote(query)}&per_page={min(limit, 30)}"
            f"&orientation=landscape&content_filter=high"
        )
        try:
            data = _get_json(
                url,
                headers={"Authorization": f"Client-ID {self.key}", "Accept-Version": "v1"},
            )
        except Exception as exc:
            log.warning("Unsplash недоступен: %s", exc)
            return []

        out: list[ImageResult] = []
        for it in data.get("results", []):
            urls = it.get("urls", {})
            user = it.get("user", {})
            alt = it.get("description") or it.get("alt_description") or ""
            out.append(
                ImageResult(
                    url=urls.get("raw", "") or urls.get("full", ""),
                    source=self.name,
                    title=alt,
                    author=user.get("name", ""),
                    author_url=user.get("links", {}).get("html", ""),
                    page_url=it.get("links", {}).get("html", ""),
                    license_name="Unsplash License",
                    license_url="https://unsplash.com/license",
                    width=int(it.get("width") or 0),
                    height=int(it.get("height") or 0),
                    thumb_url=urls.get("small", ""),
                    tags=[t.get("title", "") for t in (it.get("tags") or [])],
                    popularity=int(it.get("likes") or 0),
                )
            )
        return out


# ----------------------------------------------------------------------
# отбор релевантных
# ----------------------------------------------------------------------

# Английское `suit` — самое коварное слово темы: без стоп-листа треть выдачи
# будет скафандрами, гидрокостюмами, спортивными костюмами и судебными исками.
STOPWORDS = {
    "space suit", "spacesuit", "wetsuit", "wet suit", "tracksuit", "track suit",
    "jumpsuit", "sweatsuit", "swimsuit", "suit of armor", "suit of armour",
    "lawsuit", "bodysuit", "snowsuit", "playing card",
    "woman", "women", "female", "bride", "wedding dress", "gown", "girl",
    "lingerie", "child", "kids", "baby",
    "ai generated", "ai-generated", "midjourney", "3d render", "render",
    "cartoon", "clipart", "mockup", "template", "vector", "illustration",
}

REQUIRED_ANY = {
    "suit", "jacket", "blazer", "tailor", "tailoring", "menswear", "shirt",
    "tie", "necktie", "coat", "overcoat", "shoe", "shoes", "boot", "wool",
    "fabric", "textile", "lapel", "waistcoat", "trousers", "cloth", "leather",
    "brogue", "oxford", "derby", "loafer", "tweed", "flannel", "cufflink",
}


@dataclass
class Scorer:
    """Оценивает релевантность без компьютерного зрения — только по метаданным.

    Веса подобраны под особенность темы: у музейных источников поля заполнены
    кураторами по контролируемому словарю, у фотостоков — пользователями,
    поэтому музейные совпадения весят заметно больше.
    """

    min_width: int = 1200
    landscape_only: bool = True

    FIELD_WEIGHTS = {"tags": 3.0, "title": 2.0}
    CURATED_BONUS = 2.0          # источники с кураторской разметкой
    BIGRAM_BONUS = 3.0
    STOPWORD_PENALTY = 10.0
    MISSING_REQUIRED_PENALTY = 4.0

    def technical_gate(self, img: ImageResult) -> bool:
        """Жёсткие технические отсечки. Неизвестные размеры пропускаем."""
        if img.width and img.width < self.min_width:
            return False
        if self.landscape_only and img.width and img.height:
            ratio = img.width / img.height
            if not 1.2 <= ratio <= 2.2:
                return False
        return True

    def score(self, img: ImageResult, query: str) -> float:
        haystack = " ".join([img.title, " ".join(img.tags)]).lower()
        terms = [t for t in re.findall(r"[a-z]+", query.lower()) if len(t) > 2]

        total = 0.0
        for term in terms:
            if term in " ".join(img.tags).lower():
                total += self.FIELD_WEIGHTS["tags"]
            elif term in img.title.lower():
                total += self.FIELD_WEIGHTS["title"]

        # точная биграмма запроса весит больше, чем два отдельных слова
        for i in range(len(terms) - 1):
            if f"{terms[i]} {terms[i + 1]}" in haystack:
                total += self.BIGRAM_BONUS

        if img.source in {"met", "wikimedia"}:
            total += self.CURATED_BONUS

        for stop in STOPWORDS:
            if stop in haystack:
                total -= self.STOPWORD_PENALTY

        if not any(req in haystack for req in REQUIRED_ANY):
            total -= self.MISSING_REQUIRED_PENALTY

        if not img.needs_attribution:
            total += 1.0                      # CC0 предпочтительнее
        if img.popularity:
            total += min(1.5, img.popularity / 10000)

        return total


# ----------------------------------------------------------------------
# поиск по нескольким источникам
# ----------------------------------------------------------------------

DEFAULT_ORDER = ["met", "pexels", "wikimedia", "openverse"]


class ImageSearcher:
    """Обходит источники, отбирает и ранжирует кандидатов.

    Порог `min_score` подобран так, чтобы попадание стоп-слова (штраф 10)
    дисквалифицировало кандидата, а просто бедные метаданные (штраф 4) —
    лишь опускали его в конец списка. Иначе источники со скупым описанием
    отсеивались бы целиком.
    """

    def __init__(
        self,
        order: list[str] | None = None,
        *,
        min_width: int = 1200,
        landscape_only: bool = True,
        min_score: float = -5.0,
        require_downloadable: bool = True,
        unsplash_key: str = "",
        pexels_key: str = "",
        pixabay_key: str = "",
        openverse_client_id: str = "",
        openverse_client_secret: str = "",
    ) -> None:
        self.scorer = Scorer(min_width=min_width, landscape_only=landscape_only)
        self.min_score = min_score
        self.require_downloadable = require_downloadable

        registry: dict[str, Provider] = {
            "met": MetMuseumProvider(),
            "pexels": PexelsProvider(pexels_key),
            "wikimedia": WikimediaProvider(),
            "openverse": OpenverseProvider(openverse_client_id, openverse_client_secret),
            "pixabay": PixabayProvider(pixabay_key),
            "unsplash": UnsplashProvider(unsplash_key),
        }
        self.providers = [registry[n] for n in (order or DEFAULT_ORDER) if n in registry]

    def active_providers(self) -> list[str]:
        return [
            p.name for p in self.providers
            if p.available() and (p.allows_download or not self.require_downloadable)
        ]

    def search(self, query: str, limit: int = 5) -> list[ImageResult]:
        """Собирает кандидатов из всех источников и возвращает лучших по оценке."""
        candidates: list[ImageResult] = []
        seen: set[str] = set()

        for provider in self.providers:
            if not provider.available():
                continue
            if self.require_downloadable and not provider.allows_download:
                log.debug("%s пропущен: условия запрещают хранение файлов", provider.name)
                continue

            for img in provider.search(query, limit=max(limit * 2, 8)):
                if not img.url or img.url in seen:
                    continue
                if not self.scorer.technical_gate(img):
                    continue
                img.score = self.scorer.score(img, query)
                if img.score < self.min_score:
                    continue
                seen.add(img.url)
                candidates.append(img)

        candidates.sort(key=lambda i: -i.score)
        best = candidates[:limit]
        log.info(
            "«%s» — отобрано %s из %s (%s)",
            query, len(best), len(candidates),
            ", ".join(sorted({i.source for i in best})) or "источников нет",
        )
        return best


# ----------------------------------------------------------------------
# скачивание
# ----------------------------------------------------------------------


def download(img: ImageResult, dest: Path, *, max_width: int = 1600) -> Path | None:
    """Скачивает фотографию, приводит к JPEG и ограничивает ширину."""
    import requests
    from PIL import Image

    if img.source == "unsplash":
        log.warning("Unsplash запрещает хранение файлов у себя — пропускаю")
        return None

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".download")
    try:
        resp = requests.get(
            img.url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True
        )
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)

        with Image.open(tmp) as im:
            im = im.convert("RGB")
            if im.width > max_width:
                ratio = max_width / im.width
                im = im.resize((max_width, int(im.height * ratio)), Image.LANCZOS)
            im.save(dest, "JPEG", quality=88, optimize=True)

        img.local_path = str(dest)
        log.info("Скачано: %s ← %s", dest.name, img.source)
        return dest
    except Exception as exc:
        log.warning("Не удалось скачать %s: %s", img.url[:80], exc)
        return None
    finally:
        tmp.unlink(missing_ok=True)


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
    return f"CC {code.upper()} {version}".strip()


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Wikimedia отдаёт часть полей размеченными — вычищаем теги."""
    import html as html_mod

    return html_mod.unescape(_TAG_RE.sub("", text)).strip()
