#!/usr/bin/env python3
"""
Выгружает tubNum по UUID через авторизованную страницу Addressbook.

Chromium сам хранит клиентский сертификат, cookies и OIDC/SPNEGO-сессию.
Python не извлекает их из браузера: API-запрос выполняется через fetch()
внутри страницы, а наружу возвращается только JSON успешного ответа.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
)
from urllib.parse import urlencode, urlsplit, urlunsplit

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:
    PlaywrightError = RuntimeError  # type: ignore[assignment,misc]
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment,misc]
    sync_playwright = None  # type: ignore[assignment]

try:
    from scripts import uuid_to_tubnum as core
except ImportError:
    import uuid_to_tubnum as core  # type: ignore[no-redef]


ADDRESSBOOK_API_PATH = "/api/home/empInfoFull"
CLIENT_CERT_ORIGINS = (
    f"https://{core.DEFAULT_EXPECTED_HOST}",
    f"https://{core.DEFAULT_PRIMARY_IDP_HOST}",
    f"https://{core.DEFAULT_ALT_IDP_HOST}",
)
AUTH_SERVER_ALLOWLIST = (
    core.DEFAULT_PRIMARY_IDP_HOST,
    core.DEFAULT_ALT_IDP_HOST,
)
SAFE_JSON_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
NUMBER_FIELD_HINT = re.compile(
    r"(tub|tab|tabel|personnel|staff|number|num)",
    re.IGNORECASE,
)
MAX_JSON_SHAPE_PATHS = 40
MAX_JSON_SHAPE_DEPTH = 6

_PAGE_FETCH_JAVASCRIPT = """
async ({url, expectedHost, expectedPath, timeoutMs}) => {
  const target = new URL(url);
  const current = new URL(window.location.href);
  const safePort = (parsed) => parsed.port === "" || parsed.port === "443";

  if (
    target.protocol !== "https:" ||
    target.hostname.toLowerCase() !== expectedHost.toLowerCase() ||
    !safePort(target) ||
    target.pathname !== expectedPath ||
    current.protocol !== "https:" ||
    current.hostname.toLowerCase() !== expectedHost.toLowerCase() ||
    !safePort(current)
  ) {
    return {kind: "invalid_target"};
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(target.href, {
      method: "GET",
      credentials: "same-origin",
      redirect: "manual",
      cache: "no-store",
      headers: {Accept: "application/json"},
      signal: controller.signal,
    });

    if (response.type === "opaqueredirect" || response.status === 0) {
      return {kind: "auth"};
    }

    const finalUrl = new URL(response.url || target.href);
    if (
      finalUrl.protocol !== "https:" ||
      finalUrl.hostname.toLowerCase() !== expectedHost.toLowerCase() ||
      !safePort(finalUrl) ||
      finalUrl.pathname !== expectedPath
    ) {
      return {kind: "auth"};
    }

    const status = response.status;
    if (status === 401 || status === 403) {
      return {kind: "auth", status};
    }
    if (status === 204 || status === 404) {
      return {kind: "missing", status};
    }
    if ([429, 500, 502, 503, 504].includes(status)) {
      const rawRetryAfter = response.headers.get("Retry-After");
      let retryAfter = null;
      if (rawRetryAfter !== null) {
        const numeric = Number(rawRetryAfter);
        if (Number.isFinite(numeric) && numeric >= 0) {
          retryAfter = numeric;
        } else {
          const dateValue = Date.parse(rawRetryAfter);
          if (Number.isFinite(dateValue)) {
            retryAfter = Math.max(0, (dateValue - Date.now()) / 1000);
          }
        }
      }
      return {kind: "retry", status, retryAfter};
    }
    if (status < 200 || status >= 300) {
      return {kind: "http", status};
    }

    const contentType = (
      response.headers.get("Content-Type") || ""
    ).toLowerCase();
    if (contentType.includes("html")) {
      return {kind: "auth"};
    }

    try {
      const payload = await response.json();
      return {kind: "ok", payload};
    } catch (_) {
      return {kind: "invalid_json"};
    }
  } catch (_) {
    if (controller.signal.aborted) {
      return {kind: "timeout"};
    }
    return {kind: "network_error"};
  } finally {
    clearTimeout(timer);
  }
}
"""


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Выгрузить CSV uuid,tubNum через видимый авторизованный Chromium."
        ),
        epilog=(
            "Клиентский .p12 задаётся переменными CLIENT_CERT и "
            "CLIENT_CERT_PASSPHRASE. RTF/curl для этого режима не нужен."
        ),
    )
    parser.add_argument("--input", required=True, type=Path, help="Исходный CSV.")
    parser.add_argument("--output", required=True, type=Path, help="Итоговый CSV.")
    parser.add_argument(
        "--id-column",
        type=int,
        default=2,
        help="Номер колонки UUID, начиная с 1 (по умолчанию: 2).",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=1.0,
        help="Максимум стартов запросов в секунду (по умолчанию: 1).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Таймаут одного browser fetch в секундах (по умолчанию: 30).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Число повторов временной ошибки (по умолчанию: 4).",
    )
    parser.add_argument(
        "--max-backoff",
        type=float,
        default=60.0,
        help="Максимальная пауза между повторами (по умолчанию: 60).",
    )
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=10,
        help="Остановиться после N подряд сетевых/API-ошибок.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Обработать не более N новых UUID; удобно для первого прогона.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Печатать прогресс через N обработанных UUID.",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=25,
        help="Сбрасывать CSV на диск через N строк.",
    )
    parser.add_argument(
        "--login-timeout",
        type=float,
        default=300.0,
        help="Сколько секунд ждать завершения входа (по умолчанию: 300).",
    )
    parser.add_argument(
        "--browser-channel",
        default="chromium",
        help=(
            "Playwright browser channel; chromium запускает встроенный "
            "браузер (по умолчанию: chromium)."
        ),
    )
    parser.add_argument(
        "--browser-executable",
        type=Path,
        help="Явный путь к исполняемому файлу Chromium/корпоративного браузера.",
    )
    parser.add_argument(
        "--ignore-https-errors",
        action="store_true",
        help=(
            "Отключить проверку серверного TLS только для разрешённого "
            "тестового стенда. По умолчанию проверка включена."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Проверить CSV и .p12 без запуска браузера и HTTP-запросов.",
    )
    args = parser.parse_args(argv)

    if args.id_column < 1:
        parser.error("--id-column должен быть не меньше 1")
    if args.rate < 0:
        parser.error("--rate не может быть отрицательным")
    if args.timeout <= 0:
        parser.error("--timeout должен быть больше 0")
    if args.retries < 0:
        parser.error("--retries не может быть отрицательным")
    if args.max_backoff <= 0:
        parser.error("--max-backoff должен быть больше 0")
    if args.max_consecutive_errors < 1:
        parser.error("--max-consecutive-errors должен быть не меньше 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit должен быть не меньше 1")
    if args.progress_every < 1 or args.flush_every < 1:
        parser.error("--progress-every и --flush-every должны быть не меньше 1")
    if args.login_timeout <= 0:
        parser.error("--login-timeout должен быть больше 0")
    if not args.browser_channel.strip() and args.browser_executable is None:
        parser.error("--browser-channel не может быть пустым")
    return args


def build_client_certificates(
    client_certificate: core.ClientCertificate,
) -> List[Dict[str, str]]:
    return [
        {
            "origin": origin,
            "pfxPath": str(client_certificate.path),
            "passphrase": client_certificate.password,
        }
        for origin in CLIENT_CERT_ORIGINS
    ]


def _resolve_browser_executable(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_dir() and resolved.suffix.casefold() == ".app":
        candidate = (
            resolved
            / "Contents"
            / "MacOS"
            / resolved.stem
        )
        if candidate.is_file():
            resolved = candidate
    if not resolved.is_file():
        raise core.ConfigError(f"Исполняемый файл браузера не найден: {resolved}")
    return resolved


def build_launch_options(args: argparse.Namespace) -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "headless": False,
        "args": [
            "--auth-server-allowlist=" + ",".join(AUTH_SERVER_ALLOWLIST)
        ],
    }
    if args.browser_executable is not None:
        options["executable_path"] = str(
            _resolve_browser_executable(args.browser_executable)
        )
    elif args.browser_channel.casefold() != "chromium":
        options["channel"] = args.browser_channel
    return options


def uses_bundled_chromium(args: argparse.Namespace) -> bool:
    return (
        args.browser_executable is None
        and args.browser_channel.casefold() == "chromium"
    )


def ensure_bundled_chromium(
    playwright: Any, args: argparse.Namespace
) -> None:
    if not uses_bundled_chromium(args):
        return
    executable = Path(playwright.chromium.executable_path)
    if executable.is_file():
        return

    print(
        "Playwright Chromium не найден. Устанавливаю его автоматически; "
        "это выполняется только при первом запуске.",
        file=sys.stderr,
    )
    child_env = os.environ.copy()
    child_env.pop(core.CLIENT_CERT_ENV, None)
    child_env.pop(core.CLIENT_CERT_PASSPHRASE_ENV, None)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
            env=child_env,
        )
    except OSError as exc:
        raise core.ConfigError(
            "Не удалось запустить автоматическую установку Chromium."
        ) from exc
    if result.returncode != 0 or not executable.is_file():
        raise core.ConfigError(
            "Автоматическая установка Chromium не завершена. Выполните "
            "вручную: python -m playwright install chromium"
        )


def _allowed_browser_request(url: str) -> bool:
    try:
        split = urlsplit(url)
        port = split.port
    except (TypeError, ValueError):
        return False
    return (
        split.scheme.casefold() == "https"
        and split.hostname
        in {
            core.DEFAULT_EXPECTED_HOST,
            core.DEFAULT_PRIMARY_IDP_HOST,
            core.DEFAULT_ALT_IDP_HOST,
        }
        and port in {None, 443}
        and split.username is None
        and split.password is None
    )


def _install_tls_bypass_request_guard(context: Any) -> None:
    """При TLS bypass не выпускать HTTP(S)-запросы за точный allowlist."""

    def handle(route: Any, request: Any) -> None:
        if _allowed_browser_request(request.url):
            route.continue_()
        else:
            route.abort("blockedbyclient")

    context.route("**/*", handle)


def launch_persistent_browser_context(
    playwright: Any,
    args: argparse.Namespace,
    client_certificates: List[Dict[str, str]],
    user_data_dir: Path,
) -> Any:
    options = build_launch_options(args)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        client_certificates=client_certificates,
        ignore_https_errors=args.ignore_https_errors,
        service_workers="block" if args.ignore_https_errors else "allow",
        **options,
    )
    if args.ignore_https_errors:
        _install_tls_bypass_request_guard(context)
    return context


@contextmanager
def without_client_cert_environment() -> Iterator[None]:
    """Не передавать путь/пароль .p12 процессам Playwright через environment."""

    saved: Dict[str, str] = {}
    for name in (
        core.CLIENT_CERT_ENV,
        core.CLIENT_CERT_PASSPHRASE_ENV,
    ):
        if name in os.environ:
            saved[name] = os.environ.pop(name)
    try:
        yield
    finally:
        for name in (
            core.CLIENT_CERT_ENV,
            core.CLIENT_CERT_PASSPHRASE_ENV,
        ):
            os.environ.pop(name, None)
        os.environ.update(saved)


def _validate_fetch_url(
    url: str, expected_host: str, expected_path: str
) -> None:
    split = urlsplit(url)
    try:
        port = split.port
    except ValueError as exc:
        raise core.ConfigError("Некорректный порт browser fetch.") from exc
    if (
        split.scheme.casefold() != "https"
        or split.hostname != expected_host
        or port not in {None, 443}
        or split.username is not None
        or split.password is not None
        or split.path != expected_path
        or split.fragment
    ):
        raise core.ConfigError(
            "Browser fetch заблокирован: URL вне разрешённого Addressbook API."
        )


def _evaluate_fetch(
    page: Any,
    url: str,
    expected_host: str,
    expected_path: str,
    timeout: float,
) -> Mapping[str, Any]:
    _validate_fetch_url(url, expected_host, expected_path)
    try:
        result = page.evaluate(
            _PAGE_FETCH_JAVASCRIPT,
            {
                "url": url,
                "expectedHost": expected_host,
                "expectedPath": expected_path,
                "timeoutMs": max(1, int(timeout * 1000)),
            },
        )
    except PlaywrightTimeoutError:
        return {"kind": "timeout"}
    except PlaywrightError as exc:
        if _browser_session_lost(page, exc):
            raise core.AuthExpired(
                "окно браузера закрыто или браузерная сессия потеряна"
            ) from exc
        return {"kind": "network_error"}
    if not isinstance(result, Mapping):
        return {"kind": "invalid_result"}
    kind = result.get("kind")
    allowed_kinds = {
        "ok",
        "auth",
        "missing",
        "retry",
        "http",
        "invalid_json",
        "timeout",
        "network_error",
        "invalid_target",
    }
    if kind not in allowed_kinds:
        return {"kind": "invalid_result"}
    return result


def _status(result: Mapping[str, Any]) -> Optional[int]:
    value = result.get("status")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 100 <= value <= 599:
        return None
    return value


def safe_json_shape(value: Any) -> str:
    """Описать только ключи/типы JSON, не включая значения ответа."""

    paths: List[str] = []

    def add(path: str, nested: Any, depth: int) -> None:
        if len(paths) >= MAX_JSON_SHAPE_PATHS:
            return
        if nested is None:
            kind = "null"
        elif isinstance(nested, bool):
            kind = "bool"
        elif isinstance(nested, (int, float)):
            kind = "number"
        elif isinstance(nested, str):
            kind = "string"
        elif isinstance(nested, dict):
            kind = f"object(len={len(nested)})"
        elif isinstance(nested, list):
            kind = f"array(len={len(nested)})"
        else:
            kind = nested.__class__.__name__
        paths.append(f"{path}:{kind}")

        if depth >= MAX_JSON_SHAPE_DEPTH:
            return
        if isinstance(nested, dict):
            for raw_key in sorted(nested, key=lambda item: str(item)):
                if len(paths) >= MAX_JSON_SHAPE_PATHS:
                    return
                key = str(raw_key)
                safe_key = key if SAFE_JSON_KEY.fullmatch(key) else "<redacted-key>"
                add(f"{path}.{safe_key}", nested[raw_key], depth + 1)
        elif isinstance(nested, list) and nested:
            add(f"{path}[]", nested[0], depth + 1)

    add("$", value, 0)
    if len(paths) >= MAX_JSON_SHAPE_PATHS:
        paths.append("...")
    return "; ".join(paths)


def _safe_json_key(raw_key: Any) -> str:
    key = str(raw_key)
    return key if SAFE_JSON_KEY.fullmatch(key) else "<redacted-key>"


def safe_top_level_keys(value: Any) -> str:
    if not isinstance(value, dict):
        return "<not-an-object>"
    return ", ".join(
        sorted({_safe_json_key(raw_key) for raw_key in value})
    )


def safe_number_field_candidates(value: Any) -> str:
    """Найти только пути похожих на номер полей, не читая их значения."""

    candidates: List[str] = []
    queue: List[tuple] = [("$", value, 0)]
    while queue and len(candidates) < MAX_JSON_SHAPE_PATHS:
        path, nested, depth = queue.pop(0)
        if depth >= MAX_JSON_SHAPE_DEPTH:
            continue
        if isinstance(nested, dict):
            for raw_key, child in nested.items():
                safe_key = _safe_json_key(raw_key)
                child_path = f"{path}.{safe_key}"
                if (
                    safe_key != "<redacted-key>"
                    and NUMBER_FIELD_HINT.search(safe_key)
                ):
                    candidates.append(child_path)
                queue.append((child_path, child, depth + 1))
        elif isinstance(nested, list) and nested:
            queue.append((f"{path}[]", nested[0], depth + 1))
    return ", ".join(candidates) if candidates else "<none>"


def missing_tubnum_details(payload: Any) -> str:
    return (
        f"JSON shape: {safe_json_shape(payload)}; "
        f"top-level keys: {safe_top_level_keys(payload)}; "
        "number-like key paths: "
        f"{safe_number_field_candidates(payload)}"
    )


def _retry_after(
    result: Mapping[str, Any], max_backoff: float
) -> Optional[float]:
    value = result.get("retryAfter")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return min(float(value), max_backoff)


def _page_is_addressbook(page: Any, expected_host: str) -> bool:
    try:
        split = urlsplit(page.url)
        port = split.port
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        split.scheme.casefold() == "https"
        and split.hostname == expected_host
        and port in {None, 443}
    )


def _page_is_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except (AttributeError, PlaywrightError):
        return False


def _browser_session_lost(page: Any, exc: BaseException) -> bool:
    if _page_is_closed(page):
        return True
    message = str(exc).casefold()
    markers = (
        "target page, context or browser has been closed",
        "target closed",
        "page has been closed",
        "page crashed",
        "browser has been closed",
        "context has been closed",
    )
    return any(marker in message for marker in markers)


def _safe_navigation_error(exc: BaseException) -> str:
    message = str(exc).casefold()
    if "err_cert" in message or "certificate" in message:
        return "tls_certificate_error"
    if "err_name_not_resolved" in message:
        return "name_resolution_error"
    if "err_connection" in message:
        return "connection_error"
    return "navigation_error"


class BrowserTransport:
    def __init__(
        self,
        page: Any,
        expected_host: str = core.DEFAULT_EXPECTED_HOST,
        timeout: float = 30.0,
        login_timeout: float = 300.0,
    ) -> None:
        if expected_host != core.DEFAULT_EXPECTED_HOST:
            raise core.ConfigError(
                "Браузерный режим разрешён только для Addressbook allowlist."
            )
        self.page = page
        self.expected_host = expected_host
        self.timeout = timeout
        self.login_timeout = login_timeout

    @property
    def root_url(self) -> str:
        return urlunsplit(("https", self.expected_host, "/", "", ""))

    @property
    def probe_url(self) -> str:
        return urlunsplit(
            (
                "https",
                self.expected_host,
                core.ADDRESSBOOK_PROBE_PATH,
                "",
                "",
            )
        )

    def ensure_authenticated(self) -> None:
        if _page_is_closed(self.page):
            raise core.AuthExpired("окно браузера закрыто до завершения входа")
        print(
            "В открытом Chromium завершите корпоративный вход в Addressbook. "
            "Cookies и токены из браузера не копируются.",
            file=sys.stderr,
        )
        try:
            self.page.goto(
                self.root_url,
                wait_until="domcontentloaded",
                timeout=min(self.login_timeout, 30.0) * 1000,
            )
        except PlaywrightTimeoutError:
            pass
        except PlaywrightError as exc:
            if _browser_session_lost(self.page, exc):
                raise core.AuthExpired(
                    "окно браузера закрыто или браузерная сессия потеряна"
                ) from exc
            reason = _safe_navigation_error(exc)
            if reason == "navigation_error" and (
                "err_aborted" in str(exc).casefold()
                or "interrupted by another navigation" in str(exc).casefold()
            ):
                pass
            else:
                raise core.ConfigError(
                    "Chromium не смог открыть Addressbook "
                    f"({reason}). Адрес и секреты не выводятся."
                ) from exc

        deadline = time.monotonic() + self.login_timeout
        while time.monotonic() < deadline:
            if _page_is_closed(self.page):
                raise core.AuthExpired(
                    "окно браузера закрыто до завершения входа"
                )

            if _page_is_addressbook(self.page, self.expected_host):
                result = _evaluate_fetch(
                    self.page,
                    self.probe_url,
                    self.expected_host,
                    core.ADDRESSBOOK_PROBE_PATH,
                    self.timeout,
                )
                if result.get("kind") == "ok":
                    return
            time.sleep(1.0)
        raise core.AuthExpired(
            "вход в Addressbook не завершён за отведённое время"
        )

    def _api_url(self, user_uuid: str) -> str:
        if core.canonical_uuid(user_uuid) != user_uuid:
            raise core.ConfigError("Browser fetch получил некорректный UUID.")
        return urlunsplit(
            (
                "https",
                self.expected_host,
                ADDRESSBOOK_API_PATH,
                urlencode({"empId": user_uuid}),
                "",
            )
        )

    def _session_is_authenticated(self) -> bool:
        result = _evaluate_fetch(
            self.page,
            self.probe_url,
            self.expected_host,
            core.ADDRESSBOOK_PROBE_PATH,
            self.timeout,
        )
        return result.get("kind") == "ok"

    def fetch_tubnum(
        self,
        user_uuid: str,
        retries: int,
        max_backoff: float,
        rate_limiter: core.RateLimiter,
    ) -> str:
        url = self._api_url(user_uuid)
        last_error = "неизвестная ошибка"

        for attempt in range(retries + 1):
            rate_limiter.wait()
            result = _evaluate_fetch(
                self.page,
                url,
                self.expected_host,
                ADDRESSBOOK_API_PATH,
                self.timeout,
            )
            kind = result.get("kind")
            status = _status(result)

            if kind == "ok":
                payload = result.get("payload")
                try:
                    tub_num = core.find_tubnum(payload, user_uuid)
                except core.MissingTubNum as exc:
                    raise core.MissingTubNum(
                        f"{exc}; {missing_tubnum_details(payload)}"
                    ) from exc
                if tub_num is None:
                    raise core.MissingTubNum(
                        "в JSON-ответе отсутствует tabNum/tubNum; "
                        f"{missing_tubnum_details(payload)}"
                    )
                return tub_num
            if kind == "auth":
                detail = f"HTTP {status}" if status is not None else "redirect"
                if self._session_is_authenticated():
                    raise core.MissingTubNum(
                        f"API вернул {detail} только для этого UUID; "
                        "общая сессия Addressbook активна"
                    )
                raise core.AuthExpired(detail)
            if kind == "missing":
                detail = f"HTTP {status}" if status is not None else "записи нет"
                raise core.MissingTubNum(detail)
            if kind == "http":
                if status is None:
                    raise core.FetchError("браузер вернул некорректный HTTP status")
                raise core.FetchError(f"HTTP {status}")
            if kind == "invalid_json":
                raise core.FetchError("ответ API не является JSON")
            if kind in {"invalid_target", "invalid_result"}:
                raise core.FetchError("браузер вернул некорректный безопасный ответ")
            if kind in {"retry", "timeout", "network_error"}:
                if kind == "retry" and status is not None:
                    last_error = f"HTTP {status}"
                else:
                    last_error = f"browser_{kind}"
                if attempt >= retries:
                    break
                wait_for = (
                    _retry_after(result, max_backoff)
                    if kind == "retry"
                    else None
                )
                if wait_for is None:
                    wait_for = core.backoff_seconds(attempt, max_backoff)
                time.sleep(wait_for)
                continue
            raise core.FetchError("браузер вернул неизвестный безопасный ответ")

        raise core.FetchError(
            f"после {retries + 1} попыток: {last_error}"
        )


def run_export(args: argparse.Namespace, transport: BrowserTransport) -> int:
    successful = core.load_processed(args.output)
    errors_path = core.default_errors_path(args.output)
    error_records = core.load_error_records(errors_path, successful)
    core.write_error_records_atomic(errors_path, error_records)
    terminal_error_uuids = {
        record.uuid
        for record in error_records.values()
        if record.uuid and not record.retryable
    }
    terminal_input_rows = {
        record.source_row
        for record in error_records.values()
        if not record.uuid and not record.retryable
    }
    output_handle, output_writer = core.open_csv_append(
        args.output, ["uuid", "tubNum"]
    )

    rate_limiter = core.RateLimiter(args.rate)
    attempted = 0
    written = 0
    no_tubnum = 0
    invalid = 0
    failed = 0
    skipped_success = 0
    skipped_terminal_error = 0
    skipped_duplicate = 0
    consecutive_errors = 0
    stopped_for_auth = False
    reauthentications = 0
    attempted_this_run = set()

    try:
        for source_row, user_uuid, input_error in core.input_rows(
            args.input, args.id_column
        ):
            if input_error or user_uuid is None:
                if source_row in terminal_input_rows:
                    skipped_terminal_error += 1
                    continue
                invalid += 1
                record = core.error_record_for_input(source_row, input_error or "")
                error_records[core.error_record_key(record)] = record
                terminal_input_rows.add(source_row)
                continue
            if user_uuid in successful:
                skipped_success += 1
                continue
            if user_uuid in terminal_error_uuids:
                skipped_terminal_error += 1
                continue
            if user_uuid in attempted_this_run:
                skipped_duplicate += 1
                continue
            if args.limit is not None and attempted >= args.limit:
                break

            attempted += 1
            attempted_this_run.add(user_uuid)
            try:
                try:
                    tub_num = transport.fetch_tubnum(
                        user_uuid,
                        retries=args.retries,
                        max_backoff=args.max_backoff,
                        rate_limiter=rate_limiter,
                    )
                except core.AuthExpired:
                    transport.ensure_authenticated()
                    reauthentications += 1
                    tub_num = transport.fetch_tubnum(
                        user_uuid,
                        retries=args.retries,
                        max_backoff=args.max_backoff,
                        rate_limiter=rate_limiter,
                    )
            except core.AuthExpired:
                stopped_for_auth = True
                print(
                    "Браузерная сессия Addressbook не восстановлена. "
                    "Уже записанный CSV сохранён; повторный запуск продолжит его.",
                    file=sys.stderr,
                )
                break
            except core.FetchError as exc:
                record = core.error_record_for_fetch(user_uuid, source_row, exc)
                error_records[core.error_record_key(record)] = record
                if isinstance(exc, core.MissingTubNum):
                    no_tubnum += 1
                    consecutive_errors = 0
                    terminal_error_uuids.add(user_uuid)
                else:
                    failed += 1
                    consecutive_errors += 1
                if (
                    not isinstance(exc, core.MissingTubNum)
                    and consecutive_errors >= args.max_consecutive_errors
                ):
                    print(
                        f"Остановка после {consecutive_errors} ошибок подряд. "
                        "Неуспешные UUID будут повторены при следующем запуске.",
                        file=sys.stderr,
                    )
                    break
                if attempted % args.progress_every == 0:
                    print(
                        f"Обработано новых UUID: {attempted}; "
                        f"записано: {written}; ошибок: {failed}; "
                        f"без tubNum: {no_tubnum}.",
                        file=sys.stderr,
                    )
                continue

            consecutive_errors = 0
            output_writer.writerow({"uuid": user_uuid, "tubNum": tub_num})
            output_handle.flush()
            successful.add(user_uuid)
            error_records.pop(user_uuid, None)
            written += 1

            if written % args.flush_every == 0:
                output_handle.flush()
            if attempted % args.progress_every == 0:
                print(
                    f"Обработано новых UUID: {attempted}; записано: {written}; "
                    f"ошибок: {failed}; без tubNum: {no_tubnum}.",
                    file=sys.stderr,
                )
    finally:
        output_handle.flush()
        output_handle.close()
        core.write_error_records_atomic(errors_path, error_records)

    print(
        f"Итог: новых попыток={attempted}, записано={written}, "
        f"без tubNum={no_tubnum}, ошибок={failed}, "
        f"некорректных строк={invalid}, ранее готовых={skipped_success}, "
        f"ранее без табельника={skipped_terminal_error}, "
        f"дублей во входе={skipped_duplicate}, "
        f"повторных входов={reauthentications}. "
        f"Результат: {args.output}. Ошибки: {errors_path}."
    )
    return 3 if stopped_for_auth else (1 if failed or no_tubnum else 0)


def run(args: argparse.Namespace) -> int:
    client_certificate = core.resolve_client_certificate()
    if client_certificate is None:
        raise core.ConfigError(
            "Для браузерного режима задайте CLIENT_CERT и "
            "CLIENT_CERT_PASSPHRASE."
        )
    core.validate_client_certificate(client_certificate)
    valid, invalid = core.scan_input(args.input, args.id_column)

    if args.dry_run:
        print(
            "Проверка завершена: browser=chromium, client_cert=да, "
            f"валидных UUID={valid}, некорректных/пустых={invalid}. "
            "Браузер и HTTP-запросы не запускались."
        )
        return 0
    if sync_playwright is None:
        raise core.ConfigError(
            "Не установлен Playwright. Выполните: "
            "python3 -m pip install -r requirements-addressbook.txt"
        )

    if args.ignore_https_errors:
        print(
            "ВНИМАНИЕ: проверка серверного TLS отключена для браузерного "
            "контекста. Используйте это только на разрешённом тестовом стенде.",
            file=sys.stderr,
        )

    client_certificates = build_client_certificates(client_certificate)

    with without_client_cert_environment():
        try:
            with sync_playwright() as playwright:
                ensure_bundled_chromium(playwright, args)
                with tempfile.TemporaryDirectory(
                    prefix="kventin-addressbook-"
                ) as profile_dir:
                    context = launch_persistent_browser_context(
                        playwright,
                        args,
                        client_certificates,
                        Path(profile_dir),
                    )
                    try:
                        page = (
                            context.pages[0]
                            if context.pages
                            else context.new_page()
                        )
                        transport = BrowserTransport(
                            page,
                            timeout=args.timeout,
                            login_timeout=args.login_timeout,
                        )
                        transport.ensure_authenticated()
                        return run_export(args, transport)
                    finally:
                        context.close()
        except core.AuthExpired:
            print(
                "Вход в Addressbook не завершён. CSV не изменён; "
                "запустите команду снова и завершите вход в Chromium.",
                file=sys.stderr,
            )
            return 3
        except PlaywrightError as exc:
            reason = _safe_navigation_error(exc)
            raise core.ConfigError(
                "Playwright Chromium не запущен "
                f"({reason}). Секреты и адреса не выводятся."
            ) from exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except core.ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "Остановлено пользователем. Уже записанные строки сохраняются; "
            "повторный запуск продолжит работу.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
