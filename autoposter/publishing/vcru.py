"""Публикация на vc.ru через Playwright.

Модель работы
-------------
1. Один раз запускается `python run.py login` — открывается настоящий браузер,
   вы входите в свой аккаунт руками. Сессия сохраняется в постоянный профиль
   (`data/browser-profile`), дальше вход не требуется.
2. `publish` открывает редактор, заполняет заголовок, подзаголовок и тело,
   грузит обложку, выбирает подсайт и теги.
3. По умолчанию включён `dry_run`: редактор заполняется, но кнопка «Опубликовать»
   не нажимается — можно проверить глазами. Публикация — только с `--live`.

Вёрстка площадки меняется. Все селекторы вынесены в `selectors.yaml`,
а `python run.py calibrate` помогает их обновить.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from ..models import Article, BlockType
from .formatter import blocks_to_html, blocks_to_plain

log = logging.getLogger(__name__)

SELECTORS_PATH = Path(__file__).resolve().parent / "selectors.yaml"


class PublishError(RuntimeError):
    pass


def load_selectors() -> dict[str, Any]:
    return yaml.safe_load(SELECTORS_PATH.read_text(encoding="utf-8"))


class VcRuPublisher:
    """Драйвер редактора vc.ru."""

    def __init__(self, cfg, *, headless: bool | None = None) -> None:
        self.cfg = cfg
        self.sel = load_selectors()
        self.headless = cfg.publish.headless if headless is None else headless
        self.profile_dir = cfg.resolve(cfg.publish.profile_dir)
        self.shots_dir = cfg.resolve(cfg.publish.screenshot_dir)
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self._pw = None
        self._ctx = None

    # ------------------------------------------------------------------
    # контекст браузера
    # ------------------------------------------------------------------

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            slow_mo=self.cfg.publish.slow_mo_ms,
            viewport={"width": 1440, "height": 960},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._ctx.set_default_timeout(self.cfg.publish.timeout_ms)
        try:
            self._ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:  # не критично, есть запасной путь ввода
            log.debug("Не удалось выдать права на буфер обмена")
        return self

    def __exit__(self, *exc) -> None:
        if self._ctx:
            self._ctx.close()
        if self._pw:
            self._pw.stop()

    @property
    def page(self):
        if not self._ctx:
            raise PublishError("браузер не запущен — используйте контекстный менеджер")
        return self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    # ------------------------------------------------------------------
    # вспомогательное
    # ------------------------------------------------------------------

    def _first(self, candidates: list[str], *, timeout: int = 8000, required: bool = True):
        """Возвращает первый видимый локатор из списка кандидатов."""
        page = self.page
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            for css in candidates:
                try:
                    loc = page.locator(css).first
                    if loc.count() and loc.is_visible():
                        return loc
                except Exception:
                    continue
            page.wait_for_timeout(250)
        if required:
            raise PublishError(
                f"не найден ни один из селекторов: {candidates}\n"
                "Вёрстка vc.ru изменилась — обновите selectors.yaml "
                "(поможет `python run.py calibrate`)."
            )
        return None

    def shot(self, name: str) -> Path:
        p = self.shots_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.png"
        try:
            self.page.screenshot(path=str(p), full_page=False)
            log.info("Скриншот: %s", p)
        except Exception as exc:
            log.warning("Не удалось снять скриншот: %s", exc)
        return p

    # ------------------------------------------------------------------
    # авторизация
    # ------------------------------------------------------------------

    def is_logged_in(self) -> bool:
        self.page.goto(self.cfg.publish.base_url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2500)
        marker = self._first(
            self.sel["login"]["logged_in_marker"], timeout=6000, required=False
        )
        return marker is not None

    def interactive_login(self) -> bool:
        """Открывает браузер и ждёт, пока вы войдёте руками."""
        page = self.page
        page.goto(self.cfg.publish.base_url, wait_until="domcontentloaded")
        print("\n" + "=" * 70)
        print("  Войдите в свой аккаунт vc.ru в открывшемся окне браузера.")
        print("  Сессия сохранится в профиль, повторный вход не потребуется.")
        print("  Когда закончите — вернитесь сюда и нажмите Enter.")
        print("=" * 70 + "\n")
        input("  Нажмите Enter после входа... ")
        ok = self._first(
            self.sel["login"]["logged_in_marker"], timeout=8000, required=False
        ) is not None
        print("  ✓ Вход выполнен, сессия сохранена." if ok else
              "  ✗ Признак авторизации не найден. Проверьте selectors.yaml → login.")
        return ok

    # ------------------------------------------------------------------
    # заполнение редактора
    # ------------------------------------------------------------------

    def open_editor(self) -> None:
        self.page.goto(self.sel["editor_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(3000)

    def _type_into(self, loc, text: str) -> None:
        loc.click()
        self.page.wait_for_timeout(200)
        loc.type(text, delay=12)

    def _paste_html(self, loc, html_body: str) -> bool:
        """Кладёт HTML в буфер обмена и вставляет в поле.

        Блочный редактор разбирает вставленный rich-text в свои блоки —
        так структура (заголовки, списки, цитаты) переносится за одну операцию.
        """
        page = self.page
        try:
            page.evaluate(
                """(html) => {
                    const holder = document.createElement('div');
                    holder.setAttribute('contenteditable', 'true');
                    holder.style.position = 'fixed';
                    holder.style.left = '-10000px';
                    holder.innerHTML = html;
                    document.body.appendChild(holder);
                    const range = document.createRange();
                    range.selectNodeContents(holder);
                    const sel = window.getSelection();
                    sel.removeAllRanges();
                    sel.addRange(range);
                    document.execCommand('copy');
                    sel.removeAllRanges();
                    holder.remove();
                }""",
                html_body,
            )
            loc.click()
            page.wait_for_timeout(300)
            page.keyboard.press("Control+V")
            page.wait_for_timeout(2000)
            return True
        except Exception as exc:
            log.warning("Вставка HTML не удалась (%s) — перехожу на посимвольный ввод", exc)
            return False

    def fill_article(self, article: Article, *, strategy: str = "paste") -> None:
        page = self.page

        log.info("Заголовок")
        self._type_into(self._first(self.sel["title"]), article.title)

        if article.subtitle:
            log.info("Подзаголовок")
            sub = self._first(self.sel["subtitle"], timeout=5000, required=False)
            if sub:
                self._type_into(sub, article.subtitle)
            else:
                log.warning("Поле подзаголовка не найдено — пропускаю")

        log.info("Тело статьи (%s знаков)", article.char_count)
        body = self._first(self.sel["body"])
        pasted = False
        if strategy == "paste":
            pasted = self._paste_html(body, blocks_to_html(article))
        if not pasted:
            self._type_into(body, blocks_to_plain(article))
        page.wait_for_timeout(1500)

    def upload_cover(self, cover_path: Path) -> bool:
        """Грузит обложку и включает «Вывести в ленте»."""
        if not cover_path or not Path(cover_path).exists():
            log.warning("Обложка не найдена: %s", cover_path)
            return False
        log.info("Загружаю обложку: %s", cover_path)
        inp = self._first(self.sel["cover"]["file_input"], timeout=6000, required=False)
        if inp is None:
            # input[type=file] часто скрыт — ищем без проверки видимости
            try:
                inp = self.page.locator("input[type='file']").first
                inp.set_input_files(str(cover_path))
            except Exception as exc:
                log.warning("Не удалось загрузить обложку: %s", exc)
                return False
        else:
            inp.set_input_files(str(cover_path))

        self.page.wait_for_timeout(4000)

        toggle = self._first(
            self.sel["cover"]["show_in_feed_toggle"], timeout=6000, required=False
        )
        if toggle:
            try:
                toggle.click()
                log.info("Включено «Вывести в ленте»")
            except Exception as exc:
                log.warning("Не удалось включить «Вывести в ленте»: %s", exc)
        else:
            log.warning(
                "Кнопка «Вывести в ленте» не найдена. Включите её вручную — "
                "без неё материал уйдёт в ленту без картинки."
            )
        return True

    def set_subsite(self, subsite: str) -> bool:
        name = self.sel["subsite_names"].get(subsite, subsite)
        picker = self._first(self.sel["subsite"]["picker"], timeout=6000, required=False)
        if not picker:
            log.warning("Селектор подсайта не найден — выберите подсайт «%s» вручную", name)
            return False
        try:
            picker.click()
            self.page.wait_for_timeout(800)
            search = self._first(
                self.sel["subsite"]["search_input"], timeout=4000, required=False
            )
            if search:
                search.type(name, delay=40)
                self.page.wait_for_timeout(1200)
            option = self.page.locator(
                self.sel["subsite"]["option_template"].format(name=name)
            ).first
            option.click(timeout=6000)
            log.info("Подсайт: %s", name)
            return True
        except Exception as exc:
            log.warning("Не удалось выбрать подсайт: %s", exc)
            return False

    def set_tags(self, tags: list[str]) -> bool:
        if not tags:
            return True
        field = self._first(self.sel["tags"]["input"], timeout=5000, required=False)
        if not field:
            log.warning("Поле тегов не найдено — добавьте теги вручную: %s", ", ".join(tags))
            return False
        try:
            field.click()
            for t in tags:
                field.type(t, delay=40)
                self.page.wait_for_timeout(500)
                self.page.keyboard.press("Enter")
                self.page.wait_for_timeout(400)
            log.info("Теги: %s", ", ".join(tags))
            return True
        except Exception as exc:
            log.warning("Не удалось проставить теги: %s", exc)
            return False

    def submit(self) -> str:
        """Нажимает «Опубликовать» и возвращает URL материала."""
        nxt = self._first(
            self.sel["publish"]["open_settings_button"], timeout=4000, required=False
        )
        if nxt:
            nxt.click()
            self.page.wait_for_timeout(1500)

        btn = self._first(self.sel["publish"]["publish_button"])
        btn.click()
        log.info("Нажата кнопка публикации, жду подтверждения")
        self.page.wait_for_timeout(6000)
        try:
            self.page.wait_for_url("**/vc.ru/**", timeout=20000)
        except Exception:
            pass
        url = self.page.url
        self.shot("published")
        return url

    # ------------------------------------------------------------------
    # сценарий целиком
    # ------------------------------------------------------------------

    def publish(self, article: Article, *, live: bool = False) -> str:
        """Заполняет редактор и (если live=True) публикует.

        Returns:
            URL опубликованного материала либо пустую строку в режиме проверки.
        """
        if not self.is_logged_in():
            raise PublishError(
                "нет активной сессии vc.ru. Выполните `python run.py login`."
            )

        self.open_editor()
        self.fill_article(article)

        if article.cover_path:
            self.upload_cover(Path(article.cover_path))

        self.set_subsite(article.subsite)
        self.set_tags(article.tags)
        self.shot(f"filled-{article.id}")

        if not live:
            log.info(
                "Режим проверки: редактор заполнен, публикация не выполнена. "
                "Проверьте окно браузера. Для реальной публикации добавьте --live."
            )
            input("  Нажмите Enter, чтобы закрыть браузер... ")
            return ""

        url = self.submit()
        log.info("Опубликовано: %s", url)
        return url

    # ------------------------------------------------------------------
    # калибровка селекторов
    # ------------------------------------------------------------------

    def calibrate(self) -> dict[str, Any]:
        """Открывает редактор и собирает данные для обновления selectors.yaml."""
        self.open_editor()
        page = self.page
        report: dict[str, Any] = {}

        for key in ("title", "subtitle", "body"):
            found = None
            for css in self.sel[key]:
                try:
                    if page.locator(css).first.count():
                        found = css
                        break
                except Exception:
                    continue
            report[key] = found or "НЕ НАЙДЕНО"

        # все contenteditable и их плейсхолдеры — подсказка для новых селекторов
        report["contenteditable"] = page.evaluate(
            """() => [...document.querySelectorAll('[contenteditable="true"]')]
                 .map(el => ({
                     placeholder: el.getAttribute('data-placeholder') || el.dataset.placeholder || '',
                     cls: el.className ? String(el.className).slice(0, 120) : '',
                     testid: el.getAttribute('data-test') || '',
                 }))"""
        )
        report["buttons"] = page.evaluate(
            """() => [...document.querySelectorAll('button')]
                 .map(b => (b.innerText || '').trim())
                 .filter(t => t && t.length < 40).slice(0, 60)"""
        )

        shot = self.shot("calibrate")
        dump = self.shots_dir / f"{datetime.now():%Y%m%d-%H%M%S}-editor.html"
        dump.write_text(page.content(), encoding="utf-8")
        report["screenshot"] = str(shot)
        report["html_dump"] = str(dump)
        return report
