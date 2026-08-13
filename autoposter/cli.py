"""Командный интерфейс."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import topics as topics_mod
from .config import Config, ROOT
from .generation.claude_backend import ClaudeBackend, GenerationError
from .generation.pipeline import Pipeline
from .media.cover import make_cover
from .models import Article, Status, Topic
from .publishing.formatter import validate
from .store import Store

log = logging.getLogger("autoposter")

MSK = timezone(timedelta(hours=3))


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ----------------------------------------------------------------------
# команды
# ----------------------------------------------------------------------


def cmd_doctor(args, cfg: Config) -> int:
    print("\nПроверка окружения\n" + "─" * 60)
    ok = True

    backend = ClaudeBackend(**cfg.claude.__dict__)
    good, msg = backend.check()
    print(f"  Claude CLI      {'✓' if good else '✗'}  {msg}")
    ok &= good

    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            try:
                b = p.chromium.launch(headless=True)
                b.close()
                print("  Playwright      ✓  Chromium готов")
            except Exception as exc:
                print(f"  Playwright      ✗  Chromium не запускается: {exc}")
                print("                     → python -m playwright install chromium")
                ok = False
    except ImportError:
        print("  Playwright      ✗  не установлен → pip install playwright")
        ok = False

    try:
        from PIL import Image  # noqa: F401

        print("  Pillow          ✓  генератор обложек готов")
    except ImportError:
        print("  Pillow          ✗  не установлен → pip install pillow")
        ok = False

    profile = cfg.resolve(cfg.publish.profile_dir)
    print(f"  Профиль vc.ru   {'✓' if profile.exists() else '—'}  {profile}"
          + ("" if profile.exists() else "  (выполните `login`)"))

    print("─" * 60)
    print("Всё готово.\n" if ok else "Есть проблемы — см. выше.\n")
    return 0 if ok else 1


def cmd_topics(args, cfg: Config) -> int:
    store = Store(cfg.db_path)
    used = store.used_topic_titles()
    all_topics = topics_mod.load_topics(cfg.resolve("data/topics.json"))
    planned = topics_mod.plan(all_topics, used, args.count)

    print(f"\nКонтент-план на {len(planned)} материалов\n" + "─" * 78)
    print(f"{'#':<3} {'Приор':<6} {'Категория':<11} Тема")
    print("─" * 78)
    for n, t in enumerate(planned, 1):
        print(f"{n:<3} {t.priority:<6.0f} {t.category:<11} {t.title[:52]}")
    print("─" * 78)
    print(f"Использовано ранее: {len(used)} · В банке всего: {len(all_topics)}\n")
    return 0


def cmd_generate(args, cfg: Config) -> int:
    store = Store(cfg.db_path)
    backend = ClaudeBackend(**cfg.claude.__dict__)

    good, msg = backend.check()
    if not good:
        print(f"\n✗ {msg}\n")
        return 1

    if args.topic:
        topic = Topic(title=args.topic, category="custom")
    else:
        all_topics = topics_mod.load_topics(cfg.resolve("data/topics.json"))
        topic = topics_mod.pick_next(all_topics, store.used_topic_titles())
        if topic is None:
            print("Все темы из банка использованы. Добавьте свои в data/topics.json")
            return 1

    pipeline = Pipeline(
        backend=backend,
        target_chars=args.chars or cfg.content.target_chars,
        do_critique=not args.fast,
    )

    print(f"\nГенерирую: {topic.title}\n")
    try:
        article = pipeline.run(topic, store.recent_titles(cfg.content.recent_window))
    except GenerationError as exc:
        print(f"\n✗ Ошибка генерации: {exc}\n")
        return 1

    if cfg.cover.enabled:
        cover_text = ""
        if article.notes.startswith("cover_text="):
            cover_text = article.notes.split("=", 1)[1]
        make_cover(
            cover_text or article.title,
            cfg.covers_dir / f"{article.id}.jpg",
            width=cfg.cover.width,
            height=cfg.cover.height,
            palette=cfg.cover.palette,
            accent=cfg.cover.accent,
            font_path=cfg.cover.font_path,
            kicker=topic.category,
        )
        article.cover_path = str(cfg.covers_dir / f"{article.id}.jpg")

    json_path = cfg.articles_dir / f"{article.id}.json"
    article.save(json_path)
    md_path = cfg.articles_dir / f"{article.id}.md"
    md_path.write_text(article.to_markdown(), encoding="utf-8")
    store.record(article, json_path)

    problems = validate(article)
    print("\n" + "─" * 70)
    print(f"  «{article.title}»")
    print(f"  {article.subtitle}")
    print(f"  {article.char_count} знаков · {article.heading_count} H2 · "
          f"подсайт {article.subsite} · теги: {', '.join(article.tags)}")
    print(f"  Черновик:  {md_path}")
    print(f"  Данные:    {json_path}")
    if article.cover_path:
        print(f"  Обложка:   {article.cover_path}")
    if problems:
        print("\n  Замечания:")
        for p in problems:
            print(f"    ! {p}")
    if article.fact_checks:
        print("\n  Проверить перед публикацией:")
        for f in article.fact_checks:
            print(f"    · {f}")
    print("─" * 70)
    print(f"\n  Публикация:  python run.py publish {article.id}\n")
    return 0


def cmd_batch(args, cfg: Config) -> int:
    """Генерирует несколько материалов подряд."""
    store = Store(cfg.db_path)
    all_topics = topics_mod.load_topics(cfg.resolve("data/topics.json"))
    planned = topics_mod.plan(all_topics, store.used_topic_titles(), args.count)

    ok = 0
    for n, topic in enumerate(planned, 1):
        print(f"\n{'═' * 70}\n  [{n}/{len(planned)}] {topic.title}\n{'═' * 70}")
        sub = argparse.Namespace(topic=topic.title, chars=args.chars, fast=args.fast)
        if cmd_generate(sub, cfg) == 0:
            ok += 1
    print(f"\nГотово: {ok} из {len(planned)}\n")
    return 0 if ok else 1


def cmd_review(args, cfg: Config) -> int:
    path = cfg.articles_dir / f"{args.article_id}.json"
    if not path.exists():
        print(f"Не найдено: {path}")
        return 1
    article = Article.load(path)
    print("\n" + article.to_markdown() + "\n")

    problems = validate(article)
    if problems:
        print("Замечания:")
        for p in problems:
            print(f"  ! {p}")
    else:
        print("Замечаний нет.")

    if args.approve:
        article.status = Status.REVIEWED
        article.save(path)
        Store(cfg.db_path).record(article, path)
        print("\n✓ Отмечено как проверенное.\n")
    return 0


def cmd_login(args, cfg: Config) -> int:
    from .publishing.vcru import VcRuPublisher

    with VcRuPublisher(cfg, headless=False) as pub:
        return 0 if pub.interactive_login() else 1


def cmd_calibrate(args, cfg: Config) -> int:
    from .publishing.vcru import VcRuPublisher

    with VcRuPublisher(cfg, headless=False) as pub:
        if not pub.is_logged_in():
            print("Сначала выполните `python run.py login`")
            return 1
        report = pub.calibrate()

    print("\nКалибровка селекторов\n" + "─" * 70)
    for key in ("title", "subtitle", "body"):
        print(f"  {key:<12} {report[key]}")
    print("\n  Найденные редактируемые поля:")
    for el in report["contenteditable"]:
        print(f"    placeholder={el['placeholder']!r} test={el['testid']!r} class={el['cls'][:60]!r}")
    print("\n  Кнопки на странице:")
    print("    " + " | ".join(report["buttons"][:30]))
    print(f"\n  Скриншот: {report['screenshot']}")
    print(f"  HTML:     {report['html_dump']}")
    print("─" * 70)
    print("  Обновите autoposter/publishing/selectors.yaml по этим данным.\n")
    return 0


def cmd_publish(args, cfg: Config) -> int:
    from .publishing.vcru import VcRuPublisher, PublishError

    path = cfg.articles_dir / f"{args.article_id}.json"
    if not path.exists():
        print(f"Не найдено: {path}")
        return 1
    article = Article.load(path)

    problems = validate(article)
    if problems and not args.force:
        print("\nМатериал не прошёл проверку:")
        for p in problems:
            print(f"  ! {p}")
        print("\nИсправьте или запустите с --force\n")
        return 1

    live = args.live
    if live and not args.yes:
        print(f"\n  Публикуется в ленту vc.ru:")
        print(f"    «{article.title}»")
        print(f"    подсайт: {article.subsite} · теги: {', '.join(article.tags)}")
        if article.fact_checks:
            print(f"\n  Не проверено человеком ({len(article.fact_checks)} утверждений):")
            for f in article.fact_checks[:5]:
                print(f"    · {f}")
        if input("\n  Публиковать? [y/N] ").strip().lower() not in {"y", "yes", "д", "да"}:
            print("  Отменено.\n")
            return 0

    now = datetime.now(MSK)
    if live and now.hour not in cfg.publish.good_hours:
        print(f"  Сейчас {now:%H:%M} МСК — не лучшее окно публикации "
              f"(рекомендуется {cfg.publish.good_hours} ч).")

    try:
        with VcRuPublisher(cfg) as pub:
            url = pub.publish(article, live=live)
    except PublishError as exc:
        print(f"\n✗ {exc}\n")
        Store(cfg.db_path).mark_failed(article.id)
        return 1

    if url:
        article.published_url = url
        article.status = Status.PUBLISHED
        article.save(path)
        Store(cfg.db_path).mark_published(article.id, url)
        print(f"\n✓ Опубликовано: {url}\n")
    return 0


def cmd_status(args, cfg: Config) -> int:
    rows = Store(cfg.db_path).all_rows(args.limit)
    if not rows:
        print("\nПока ничего нет. Начните с `python run.py generate`\n")
        return 0
    print(f"\n{'ID':<14}{'Статус':<12}{'Знаков':<9}{'Подсайт':<11}Заголовок")
    print("─" * 92)
    for r in rows:
        print(f"{r['id']:<14}{r['status']:<12}{r['chars'] or 0:<9}"
              f"{r['subsite'] or '':<11}{(r['title'] or '')[:44]}")
    print("─" * 92 + "\n")
    return 0


def cmd_export(args, cfg: Config) -> int:
    """Выгружает базу знаний одним файлом — удобно для ручной работы в чате."""
    from .generation.prompts import KNOWLEDGE_DIR

    parts = [p.read_text(encoding="utf-8") for p in sorted(KNOWLEDGE_DIR.glob("*.md"))]
    out = Path(args.out)
    out.write_text("\n\n---\n\n".join(parts), encoding="utf-8")
    print(f"\nБаза знаний выгружена: {out} ({out.stat().st_size // 1024} КБ)\n")
    return 0


# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Генерация и автопубликация статей о классической одежде на vc.ru",
    )
    p.add_argument("--config", default=None, help="путь к config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="проверить окружение").set_defaults(fn=cmd_doctor)

    t = sub.add_parser("topics", help="показать контент-план")
    t.add_argument("-n", "--count", type=int, default=10)
    t.set_defaults(fn=cmd_topics)

    g = sub.add_parser("generate", help="сгенерировать статью")
    g.add_argument("--topic", help="своя тема (иначе берётся из банка)")
    g.add_argument("--chars", type=int, help="целевой объём в знаках")
    g.add_argument("--fast", action="store_true", help="без этапа критики (быстрее, слабее)")
    g.set_defaults(fn=cmd_generate)

    b = sub.add_parser("batch", help="сгенерировать несколько статей")
    b.add_argument("-n", "--count", type=int, default=3)
    b.add_argument("--chars", type=int)
    b.add_argument("--fast", action="store_true")
    b.set_defaults(fn=cmd_batch)

    r = sub.add_parser("review", help="показать статью и проверить её")
    r.add_argument("article_id")
    r.add_argument("--approve", action="store_true", help="отметить как проверенную")
    r.set_defaults(fn=cmd_review)

    sub.add_parser("login", help="разовый вход в vc.ru").set_defaults(fn=cmd_login)
    sub.add_parser("calibrate", help="обновить селекторы редактора").set_defaults(fn=cmd_calibrate)

    pub = sub.add_parser("publish", help="опубликовать статью")
    pub.add_argument("article_id")
    pub.add_argument("--live", action="store_true", help="реально опубликовать (иначе только заполнить)")
    pub.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    pub.add_argument("--force", action="store_true", help="игнорировать замечания проверки")
    pub.set_defaults(fn=cmd_publish)

    s = sub.add_parser("status", help="список материалов")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.set_defaults(fn=cmd_status)

    e = sub.add_parser("export-knowledge", help="выгрузить базу знаний в один файл")
    e.add_argument("--out", default="knowledge-export.md")
    e.set_defaults(fn=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config)
    setup_logging(cfg.log_level)
    cfg.articles_dir.mkdir(parents=True, exist_ok=True)
    cfg.covers_dir.mkdir(parents=True, exist_ok=True)
    try:
        return args.fn(args, cfg)
    except KeyboardInterrupt:
        print("\nПрервано.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
