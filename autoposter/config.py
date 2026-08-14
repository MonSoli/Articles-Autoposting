"""Конфигурация из YAML с разумными значениями по умолчанию."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


@dataclass
class ClaudeConfig:
    binary: str = "claude"
    model: str = "opus"
    timeout: int = 900
    retries: int = 3
    session: bool = True


@dataclass
class ContentConfig:
    target_chars: int = 9000
    critique: bool = True
    seo: bool = True             # этап SEO-доводки по замечаниям анализатора
    default_subsite: str = "fashion"
    # запрет на повтор тем: сколько последних заголовков показывать модели
    recent_window: int = 30


@dataclass
class CoverConfig:
    enabled: bool = True
    width: int = 1920
    height: int = 1080
    # путь к .ttf; если пусто — ищем системный
    font_path: str = ""
    palette: list[str] = field(
        default_factory=lambda: ["#1c1c1e", "#2c2416", "#1a2620", "#241a26", "#1a1f2e"]
    )
    accent: str = "#c9a227"


@dataclass
class ImagesConfig:
    """Фотографии-иллюстрации внутри статьи."""

    enabled: bool = True
    max_per_article: int = 4
    min_width: int = 1200
    max_width: int = 1600
    landscape_only: bool = True
    # Met, Wikimedia и Openverse работают без ключей.
    # Unsplash намеренно не в умолчаниях: его условия запрещают хранить файлы
    # у себя, а мы загружаем их в редактор vc.ru.
    order: list[str] = field(
        default_factory=lambda: ["met", "pexels", "wikimedia", "openverse"]
    )
    # ключи можно задать здесь либо переменными окружения
    pexels_key: str = ""            # PEXELS_API_KEY
    pixabay_key: str = ""           # PIXABAY_API_KEY
    unsplash_key: str = ""          # UNSPLASH_ACCESS_KEY, только для просмотра
    openverse_client_id: str = ""       # OPENVERSE_CLIENT_ID
    openverse_client_secret: str = ""   # OPENVERSE_CLIENT_SECRET


@dataclass
class PublishConfig:
    # каталог профиля браузера — там живёт сессия vc.ru после разового логина
    profile_dir: str = "data/browser-profile"
    headless: bool = False
    base_url: str = "https://vc.ru"
    slow_mo_ms: int = 120
    timeout_ms: int = 45000
    # окна публикации по МСК (см. 00_platform_vcru.md)
    good_hours: list[int] = field(default_factory=lambda: [10, 11, 15, 16])
    screenshot_dir: str = "data/screenshots"
    # если True — заполнить редактор, но не нажимать «Опубликовать»
    dry_run: bool = True


@dataclass
class Config:
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    content: ContentConfig = field(default_factory=ContentConfig)
    cover: CoverConfig = field(default_factory=CoverConfig)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    data_dir: str = "data"
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else DEFAULT_CONFIG
        if not p.exists():
            return cls()
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(
            claude=ClaudeConfig(**raw.get("claude", {})),
            content=ContentConfig(**raw.get("content", {})),
            cover=CoverConfig(**raw.get("cover", {})),
            images=ImagesConfig(**raw.get("images", {})),
            publish=PublishConfig(**raw.get("publish", {})),
            data_dir=raw.get("data_dir", "data"),
            log_level=raw.get("log_level", "INFO"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # удобные пути
    @property
    def articles_dir(self) -> Path:
        return (ROOT / self.data_dir / "articles").resolve()

    @property
    def covers_dir(self) -> Path:
        return (ROOT / self.data_dir / "covers").resolve()

    @property
    def photos_dir(self) -> Path:
        return (ROOT / self.data_dir / "photos").resolve()

    @property
    def db_path(self) -> Path:
        return (ROOT / self.data_dir / "state.sqlite3").resolve()

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (ROOT / p).resolve()
