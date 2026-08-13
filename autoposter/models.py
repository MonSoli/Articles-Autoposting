"""Доменные модели: тема, статья, блоки контента."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class BlockType(str, Enum):
    """Типы блоков редактора Osnova (vc.ru)."""

    PARAGRAPH = "paragraph"
    HEADING = "heading"          # H2
    SUBHEADING = "subheading"    # H3
    BULLET_LIST = "bullet_list"
    NUMBER_LIST = "number_list"
    QUOTE = "quote"
    NUMBER_CARD = "number_card"  # врезка «Цифра»
    IMAGE = "image"
    DELIMITER = "delimiter"
    POLL = "poll"
    CODE = "code"


@dataclass
class Block:
    """Один блок статьи."""

    type: BlockType
    text: str = ""
    items: list[str] = field(default_factory=list)
    caption: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Block":
        return cls(
            type=BlockType(d["type"]),
            text=d.get("text", ""),
            items=list(d.get("items", [])),
            caption=d.get("caption", ""),
            meta=dict(d.get("meta", {})),
        )

    @property
    def char_count(self) -> int:
        return len(self.text) + sum(len(i) for i in self.items)


class Status(str, Enum):
    NEW = "new"
    GENERATED = "generated"
    REVIEWED = "reviewed"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclass
class Topic:
    """Тема из банка тем, с оценками приоритета."""

    title: str
    category: str = "general"
    angle: str = ""
    primary_query: str = ""
    secondary_queries: list[str] = field(default_factory=list)
    # оценки 1..5
    virality: int = 3
    seo: int = 3
    teaching: int = 3
    uniqueness: int = 3

    @property
    def priority(self) -> float:
        """Приоритет по формуле из 03_domain_research.md."""
        return self.virality * 2 + self.seo + self.teaching + self.uniqueness * 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Topic":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Article:
    """Готовая статья со всеми метаданными для публикации."""

    title: str = ""
    subtitle: str = ""
    blocks: list[Block] = field(default_factory=list)
    subsite: str = "fashion"
    tags: list[str] = field(default_factory=list)
    cover_path: str | None = None
    cover_prompt: str = ""

    topic: Topic | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: Status = Status.NEW
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    published_url: str = ""
    # утверждения, требующие проверки человеком
    fact_checks: list[str] = field(default_factory=list)
    notes: str = ""

    # ---------- метрики ----------

    @property
    def char_count(self) -> int:
        return sum(b.char_count for b in self.blocks)

    @property
    def heading_count(self) -> int:
        return sum(1 for b in self.blocks if b.type is BlockType.HEADING)

    @property
    def body_text(self) -> str:
        parts: list[str] = []
        for b in self.blocks:
            if b.text:
                parts.append(b.text)
            parts.extend(b.items)
        return "\n".join(parts)

    # ---------- сериализация ----------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "subtitle": self.subtitle,
            "blocks": [b.to_dict() for b in self.blocks],
            "subsite": self.subsite,
            "tags": self.tags,
            "cover_path": self.cover_path,
            "cover_prompt": self.cover_prompt,
            "topic": self.topic.to_dict() if self.topic else None,
            "status": self.status.value,
            "created_at": self.created_at,
            "published_url": self.published_url,
            "fact_checks": self.fact_checks,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Article":
        return cls(
            id=d.get("id", uuid.uuid4().hex[:12]),
            title=d.get("title", ""),
            subtitle=d.get("subtitle", ""),
            blocks=[Block.from_dict(b) for b in d.get("blocks", [])],
            subsite=d.get("subsite", "fashion"),
            tags=list(d.get("tags", [])),
            cover_path=d.get("cover_path"),
            cover_prompt=d.get("cover_prompt", ""),
            topic=Topic.from_dict(d["topic"]) if d.get("topic") else None,
            status=Status(d.get("status", "new")),
            created_at=d.get("created_at", ""),
            published_url=d.get("published_url", ""),
            fact_checks=list(d.get("fact_checks", [])),
            notes=d.get("notes", ""),
        )

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "Article":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---------- markdown ----------

    def to_markdown(self) -> str:
        """Человекочитаемый вид для ревью."""
        out = [f"# {self.title}", "", f"_{self.subtitle}_", ""]
        for b in self.blocks:
            if b.type is BlockType.HEADING:
                out += [f"## {b.text}", ""]
            elif b.type is BlockType.SUBHEADING:
                out += [f"### {b.text}", ""]
            elif b.type is BlockType.BULLET_LIST:
                out += [f"- {i}" for i in b.items] + [""]
            elif b.type is BlockType.NUMBER_LIST:
                out += [f"{n}. {i}" for n, i in enumerate(b.items, 1)] + [""]
            elif b.type is BlockType.QUOTE:
                out += [f"> {b.text}", ""]
            elif b.type is BlockType.NUMBER_CARD:
                out += [f"**{b.text}** — {b.caption}", ""]
            elif b.type is BlockType.IMAGE:
                out += [f"![{b.caption}]({b.meta.get('path', b.text)})", ""]
            elif b.type is BlockType.DELIMITER:
                out += ["---", ""]
            elif b.type is BlockType.POLL:
                out += [f"**Опрос: {b.text}**"] + [f"- [ ] {i}" for i in b.items] + [""]
            elif b.type is BlockType.CODE:
                out += ["```", b.text, "```", ""]
            else:
                out += [b.text, ""]
        meta = [
            "",
            "---",
            f"Подсайт: `{self.subsite}` · Теги: {', '.join(self.tags)} "
            f"· Знаков: {self.char_count}",
        ]
        if self.fact_checks:
            meta += ["", "**Проверить перед публикацией:**"]
            meta += [f"- {f}" for f in self.fact_checks]
        return "\n".join(out + meta)


# --------------------------------------------------------------------------
# Разбор markdown-ответа модели в блоки
# --------------------------------------------------------------------------

_NUM_CARD_RE = re.compile(r"^\[ЦИФРА\]\s*(.+?)\s*\|\s*(.+)$", re.I)
_IMG_RE = re.compile(r"^\[ИЛЛЮСТРАЦИЯ\]\s*(.+)$", re.I)
_POLL_RE = re.compile(r"^\[ОПРОС\]\s*(.+)$", re.I)


def parse_markdown_to_blocks(md: str) -> list[Block]:
    """Превращает markdown, сгенерированный моделью, в блоки редактора.

    Поддерживает служебные маркеры, которые модель ставит по инструкции:
      [ЦИФРА] 84 000 ₽ | средняя цена костюма на заказ
      [ИЛЛЮСТРАЦИЯ] подпись к картинке
      [ОПРОС] вопрос   + следующие за ним пункты списка
    """
    blocks: list[Block] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    n = len(lines)

    def flush_list(marker: str) -> None:
        nonlocal i
        items: list[str] = []
        while i < n:
            s = lines[i].strip()
            if marker == "bullet" and re.match(r"^[-*•]\s+", s):
                items.append(re.sub(r"^[-*•]\s+", "", s))
            elif marker == "number" and re.match(r"^\d+[.)]\s+", s):
                items.append(re.sub(r"^\d+[.)]\s+", "", s))
            else:
                break
            i += 1
        if items:
            blocks.append(
                Block(
                    type=BlockType.BULLET_LIST
                    if marker == "bullet"
                    else BlockType.NUMBER_LIST,
                    items=items,
                )
            )

    while i < n:
        raw = lines[i]
        s = raw.strip()

        if not s:
            i += 1
            continue

        if s.startswith("### "):
            blocks.append(Block(BlockType.SUBHEADING, s[4:].strip()))
            i += 1
        elif s.startswith("## "):
            blocks.append(Block(BlockType.HEADING, s[3:].strip()))
            i += 1
        elif s.startswith("# "):
            # H1 в теле не нужен — это заголовок статьи
            blocks.append(Block(BlockType.HEADING, s[2:].strip()))
            i += 1
        elif s in {"---", "***", "___"}:
            blocks.append(Block(BlockType.DELIMITER))
            i += 1
        elif s.startswith("> "):
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(Block(BlockType.QUOTE, " ".join(quote)))
        elif m := _NUM_CARD_RE.match(s):
            blocks.append(Block(BlockType.NUMBER_CARD, m.group(1), caption=m.group(2)))
            i += 1
        elif m := _IMG_RE.match(s):
            blocks.append(Block(BlockType.IMAGE, caption=m.group(1).strip()))
            i += 1
        elif m := _POLL_RE.match(s):
            question = m.group(1).strip()
            i += 1
            options: list[str] = []
            while i < n and re.match(r"^[-*•]\s+", lines[i].strip()):
                options.append(re.sub(r"^[-*•]\s+", "", lines[i].strip()))
                i += 1
            blocks.append(Block(BlockType.POLL, question, items=options))
        elif re.match(r"^[-*•]\s+", s):
            flush_list("bullet")
        elif re.match(r"^\d+[.)]\s+", s):
            flush_list("number")
        elif s.startswith("```"):
            i += 1
            code: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append(Block(BlockType.CODE, "\n".join(code)))
        elif s.startswith("|") and s.endswith("|"):
            # markdown-таблица -> маркированный список (в редакторе нет таблиц)
            rows: list[str] = []
            while i < n and lines[i].strip().startswith("|"):
                row = lines[i].strip().strip("|")
                if not re.fullmatch(r"[\s|:-]+", row):
                    cells = [c.strip() for c in row.split("|")]
                    rows.append(" — ".join(c for c in cells if c))
                i += 1
            if rows:
                header, *body = rows
                blocks.append(Block(BlockType.PARAGRAPH, header))
                blocks.append(Block(BlockType.BULLET_LIST, items=body))
        else:
            para = []
            while i < n and lines[i].strip() and not _is_special(lines[i].strip()):
                para.append(lines[i].strip())
                i += 1
            blocks.append(Block(BlockType.PARAGRAPH, " ".join(para)))

    return blocks


def _is_special(s: str) -> bool:
    return bool(
        s.startswith(("#", ">", "```", "|"))
        or re.match(r"^[-*•]\s+", s)
        or re.match(r"^\d+[.)]\s+", s)
        or s in {"---", "***", "___"}
        or _NUM_CARD_RE.match(s)
        or _IMG_RE.match(s)
        or _POLL_RE.match(s)
    )
