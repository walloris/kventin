"""
Клиент LLM поверх агентного CLI gigacode (LLM_PROVIDER=gigacode_cli).

Вместо прямого REST к GigaChat «мозгом» становится gigacode CLI: он запускается
как подпроцесс в неинтерактивном режиме (по умолчанию ``gigacode -p ... --output-format json``),
читает промпт из stdin/arg и возвращает ответ в stdout. Скриншоты передаются ссылкой
на временный PNG-файл в тексте промпта (CLI читает изображение по пути).

Интерфейс совместим с остальными провайдерами (см. src/llm_provider.py и src/jan_client.py):
методы query(), chat_with_screenshot(), _get_token().
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shlex
import subprocess
import tempfile
from typing import Optional

LOG = logging.getLogger("gigacode-cli")


class GigacodeCliClient:
    """Запуск gigacode CLI как подпроцесса для получения ответов модели."""

    def __init__(self) -> None:
        try:
            from config import (
                GIGACODE_CLI_ARGS,
                GIGACODE_CLI_BIN,
                GIGACODE_CLI_CWD,
                GIGACODE_CLI_JSON_RESULT_KEY,
                GIGACODE_CLI_PASS_PROMPT,
                GIGACODE_CLI_SEND_IMAGE,
                GIGACODE_CLI_SYSTEM_FLAG,
                GIGACODE_CLI_TIMEOUT_SEC,
            )
        except ImportError:
            GIGACODE_CLI_BIN = os.getenv("GIGACODE_CLI_BIN", "gigacode")
            GIGACODE_CLI_ARGS = os.getenv("GIGACODE_CLI_ARGS", "-p --output-format json")
            GIGACODE_CLI_TIMEOUT_SEC = int(os.getenv("GIGACODE_CLI_TIMEOUT_SEC", "180"))
            GIGACODE_CLI_CWD = os.getenv("GIGACODE_CLI_CWD", "")
            GIGACODE_CLI_PASS_PROMPT = os.getenv("GIGACODE_CLI_PASS_PROMPT", "stdin")
            GIGACODE_CLI_SEND_IMAGE = os.getenv("GIGACODE_CLI_SEND_IMAGE", "true").lower() in ("1", "true", "yes")
            GIGACODE_CLI_SYSTEM_FLAG = os.getenv("GIGACODE_CLI_SYSTEM_FLAG", "--append-system-prompt")
            GIGACODE_CLI_JSON_RESULT_KEY = os.getenv("GIGACODE_CLI_JSON_RESULT_KEY", "result")

        self.bin = GIGACODE_CLI_BIN or "gigacode"
        self.extra_args = shlex.split(GIGACODE_CLI_ARGS or "")
        self.timeout = max(10, int(GIGACODE_CLI_TIMEOUT_SEC))
        self.cwd = GIGACODE_CLI_CWD or None
        self.pass_prompt = (GIGACODE_CLI_PASS_PROMPT or "stdin").lower()
        self.send_image = bool(GIGACODE_CLI_SEND_IMAGE)
        self.system_flag = (GIGACODE_CLI_SYSTEM_FLAG or "").strip()
        self.json_result_key = (GIGACODE_CLI_JSON_RESULT_KEY or "result").strip()
        LOG.info("gigacode CLI client: bin=%s args=%s", self.bin, self.extra_args)

    def _get_token(self) -> Optional[str]:
        """Заглушка для init_gigachat_connection: проверяем, что бинарь доступен."""
        from shutil import which

        if which(self.bin) or os.path.isfile(self.bin):
            return "gigacode-cli"
        LOG.warning("gigacode CLI '%s' не найден в PATH", self.bin)
        return None

    def _build_argv(self, system: Optional[str], prompt_for_arg: Optional[str]) -> list:
        argv = [self.bin, *self.extra_args]
        if system and self.system_flag:
            argv += [self.system_flag, system]
        if prompt_for_arg is not None:
            argv.append(prompt_for_arg)
        return argv

    def _run(self, prompt: str, system: Optional[str]) -> str:
        use_stdin = self.pass_prompt != "arg"
        argv = self._build_argv(system, None if use_stdin else prompt)
        try:
            proc = subprocess.run(
                argv,
                input=prompt if use_stdin else None,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.cwd,
            )
        except FileNotFoundError:
            LOG.error("gigacode CLI '%s' не найден", self.bin)
            return ""
        except subprocess.TimeoutExpired:
            LOG.warning("gigacode CLI timeout %.0fs", self.timeout)
            return ""
        except Exception as exc:  # noqa: BLE001
            LOG.exception("gigacode CLI запуск: %s", exc)
            return ""

        if proc.returncode != 0:
            LOG.error("gigacode CLI rc=%s stderr=%s", proc.returncode, (proc.stderr or "")[:500])
            # некоторые CLI пишут ответ в stdout даже при ненулевом коде — пробуем распарсить
        return self._parse_output(proc.stdout or "")

    def _parse_output(self, stdout: str) -> str:
        text = (stdout or "").strip()
        if not text:
            return ""
        # Пытаемся распарсить JSON-вывод (--output-format json) и достать поле result.
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            for key in (self.json_result_key, "result", "text", "content", "output", "response"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            # вложенный structured_output
            so = data.get("structured_output")
            if isinstance(so, str) and so.strip():
                return so.strip()
            if isinstance(so, (dict, list)):
                return json.dumps(so, ensure_ascii=False)
        return text

    def query(self, prompt: str, system: Optional[str] = None) -> str:
        return self._run(prompt, system)

    def chat_with_screenshot(
        self,
        text_prompt: str,
        screenshot_b64: Optional[str] = None,
        system: Optional[str] = None,
    ) -> str:
        if not screenshot_b64 or not self.send_image:
            return self._run(text_prompt, system)

        tmp_path = None
        try:
            raw = base64.b64decode(screenshot_b64)
            fd, tmp_path = tempfile.mkstemp(prefix="kventin_shot_", suffix=".png")
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            prompt = (
                f"{text_prompt}\n\n"
                f"Скриншот текущего экрана (PNG): {tmp_path}\n"
                f"Проанализируй изображение по этому пути."
            )
            return self._run(prompt, system)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("gigacode CLI screenshot: %s — без изображения", exc)
            return self._run(text_prompt, system)
        finally:
            if tmp_path and os.path.isfile(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


def _smoke_test() -> int:
    """
    Проверка клиента из консоли (не запускайте файл как скрипт без этого блока).

    Из корня репозитория:
        python3 -m src.gigacode_cli_client
    или:
        cd /Users/walloris/Documents/kventin && python3 -m src.gigacode_cli_client
    """
    logging.basicConfig(level=logging.INFO, format="[gigacode-cli] %(levelname)s %(message)s")
    client = GigacodeCliClient()
    print(f"bin={client.bin!r} extra_args={client.extra_args!r} cwd={client.cwd!r} timeout={client.timeout}s")
    token = client._get_token()
    if not token:
        print("FAIL: gigacode не найден в PATH. Задайте GIGACODE_CLI_BIN в .env на полный путь к бинарю.")
        return 2
    print(f"OK: CLI доступен (_get_token -> {token!r})")
    print("Запрос к CLI (может занять до GIGACODE_CLI_TIMEOUT_SEC сек)…")
    answer = client.query("Ответь одним словом: ок", system="Отвечай только одним словом, без пояснений.")
    if not (answer or "").strip():
        print("FAIL: пустой ответ. Проверьте GIGACODE_CLI_ARGS (one-shot: -p --output-format json) и gigacode --help.")
        return 1
    print("--- ответ CLI ---")
    print(answer.strip()[:2000])
    print("--- конец ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(_smoke_test())
