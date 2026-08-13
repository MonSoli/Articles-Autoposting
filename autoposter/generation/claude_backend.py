"""Бэкенд генерации через Claude Code CLI — на подписке Pro/Max, без API-ключа.

Почему именно так
-----------------
Требование было: «пишет Claude, но не по API, а через обычную Pro-подписку».
Claude Code CLI авторизуется тем же OAuth-токеном, что и веб-интерфейс, и работает
в счёт лимитов подписки, а не в счёт оплаты за токены. Режим `claude -p` (headless)
принимает промпт на stdin и отдаёт ответ на stdout — то есть это полноценный
программный доступ к чату, без API-ключа и без оплаты за токены.

Это и надёжнее, и честнее, чем эмулировать клики в claude.ai: там Cloudflare,
постоянно меняющаяся вёрстка и прямой запрет на автоматизацию в правилах сервиса.
Результат для пользователя тот же — текст пишет Claude, платит подписка.

Требования
----------
    npm install -g @anthropic-ai/claude-code
    claude          # один раз залогиниться своей учётной записью (Pro/Max)

Проверить, что всё готово: `python run.py doctor`
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


class GenerationError(RuntimeError):
    """Ошибка обращения к Claude."""


@dataclass
class ClaudeBackend:
    """Обёртка над `claude -p`.

    Args:
        binary: путь к исполняемому файлу claude.
        model: алиас модели (`opus`, `sonnet`) либо полный id.
        timeout: таймаут одного вызова, сек.
        retries: число повторов при сбое.
        session: если True, все вызовы идут одной цепочкой (модель помнит контекст).
    """

    binary: str = "claude"
    model: str = "opus"
    timeout: int = 900
    retries: int = 3
    session: bool = True

    _session_id: str | None = None

    # ------------------------------------------------------------------
    # проверка окружения
    # ------------------------------------------------------------------

    def check(self) -> tuple[bool, str]:
        """Возвращает (готов, сообщение)."""
        path = shutil.which(self.binary)
        if not path:
            return False, (
                f"CLI `{self.binary}` не найден в PATH.\n"
                "Установите:  npm install -g @anthropic-ai/claude-code\n"
                "Затем один раз выполните `claude` и войдите в свою учётную запись."
            )
        try:
            res = subprocess.run(
                [self.binary, "--version"],
                capture_output=True, text=True, timeout=60,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f"Не удалось запустить `{self.binary}`: {exc}"

        if res.returncode != 0:
            return False, f"`{self.binary} --version` вернул код {res.returncode}: {res.stderr.strip()}"

        if os.environ.get("ANTHROPIC_API_KEY"):
            log.warning(
                "В окружении задан ANTHROPIC_API_KEY — вызовы могут пойти по API и "
                "тарифицироваться. Уберите переменную, чтобы работать на подписке."
            )
        return True, f"OK: {path} ({res.stdout.strip()})"

    # ------------------------------------------------------------------
    # генерация
    # ------------------------------------------------------------------

    def ask(self, prompt: str, *, system: str | None = None, fresh: bool = False) -> str:
        """Отправляет промпт и возвращает текстовый ответ.

        Args:
            prompt: пользовательское сообщение.
            system: дополнительная системная инструкция.
            fresh: начать новую цепочку, забыв предыдущий контекст.
        """
        if fresh:
            self._session_id = None

        cmd = [self.binary, "-p", "--output-format", "json", "--model", self.model]
        if system:
            cmd += ["--append-system-prompt", system]
        if self.session and self._session_id:
            cmd += ["--resume", self._session_id]

        env = os.environ.copy()
        # гарантируем работу на подписке, а не на API-ключе
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)

        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                log.debug("claude -p (попытка %s/%s, %s знаков)", attempt, self.retries, len(prompt))
                res = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    env=env,
                )
                if res.returncode != 0:
                    raise GenerationError(
                        f"claude вернул код {res.returncode}: {res.stderr.strip()[:500]}"
                    )
                return self._extract(res.stdout)

            except subprocess.TimeoutExpired as exc:
                last_err = GenerationError(f"таймаут {self.timeout} с")
                log.warning("Таймаут вызова Claude (попытка %s)", attempt)
            except GenerationError as exc:
                last_err = exc
                msg = str(exc).lower()
                # лимиты подписки — ждём дольше
                if "rate" in msg or "limit" in msg or "usage" in msg:
                    wait = 60 * attempt
                    log.warning("Похоже на лимит подписки, пауза %s с", wait)
                    time.sleep(wait)
                    continue
                log.warning("Ошибка вызова Claude: %s", exc)

            if attempt < self.retries:
                time.sleep(2 ** attempt)

        raise GenerationError(f"не удалось получить ответ за {self.retries} попыток: {last_err}")

    def _extract(self, stdout: str) -> str:
        """Достаёт текст ответа из JSON-вывода CLI (с запасным вариантом)."""
        stdout = stdout.strip()
        if not stdout:
            raise GenerationError("пустой ответ от claude")

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # CLI мог отдать обычный текст
            return stdout

        if isinstance(data, dict):
            if data.get("is_error"):
                raise GenerationError(f"claude сообщил об ошибке: {data.get('result', '')[:300]}")
            if self.session and data.get("session_id"):
                self._session_id = data["session_id"]
            result = data.get("result") or data.get("text") or ""
            if isinstance(result, list):  # на случай блочного формата
                result = "".join(
                    b.get("text", "") for b in result if isinstance(b, dict)
                )
            if result:
                return str(result).strip()
        raise GenerationError(f"не удалось разобрать ответ: {stdout[:300]}")

    def reset(self) -> None:
        """Сбрасывает цепочку — следующий вызов начнёт новый разговор."""
        self._session_id = None
