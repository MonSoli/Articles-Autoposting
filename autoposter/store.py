"""Хранилище состояния: что сгенерировано, что опубликовано."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import Article, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    topic_title  TEXT,
    subsite      TEXT,
    status       TEXT NOT NULL,
    path         TEXT,
    url          TEXT,
    chars        INTEGER,
    created_at   TEXT,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------

    def record(self, article: Article, path: Path | None = None) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO articles
                     (id, title, topic_title, subsite, status, path, url, chars, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=excluded.title, subsite=excluded.subsite,
                     status=excluded.status, path=excluded.path,
                     url=excluded.url, chars=excluded.chars""",
                (
                    article.id,
                    article.title,
                    article.topic.title if article.topic else "",
                    article.subsite,
                    article.status.value,
                    str(path) if path else "",
                    article.published_url,
                    article.char_count,
                    article.created_at,
                ),
            )

    def mark_published(self, article_id: str, url: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE articles SET status=?, url=?, published_at=? WHERE id=?",
                (
                    Status.PUBLISHED.value,
                    url,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    article_id,
                ),
            )

    def mark_failed(self, article_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE articles SET status=? WHERE id=?", (Status.FAILED.value, article_id)
            )

    # ------------------------------------------------------------------

    def used_topic_titles(self) -> set[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT topic_title FROM articles WHERE topic_title <> ''"
            ).fetchall()
        return {r["topic_title"] for r in rows}

    def recent_titles(self, limit: int = 30) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT title FROM articles ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r["title"] for r in rows]

    def pending(self) -> list[sqlite3.Row]:
        """Сгенерированные, но не опубликованные."""
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM articles WHERE status IN (?,?) ORDER BY created_at",
                (Status.GENERATED.value, Status.REVIEWED.value),
            ).fetchall()

    def all_rows(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM articles ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def get(self, article_id: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM articles WHERE id=?", (article_id,)
            ).fetchone()
