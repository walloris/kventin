#!/usr/bin/env python3
"""
Выгружает tubNum по UUID из addressbook API.

Скрипт:
* берет UUID из второго столбца входного CSV;
* извлекает URL, заголовки и cookies из сохраненного curl в RTF;
* пишет CSV строго с колонками uuid,tubNum;
* безопасно возобновляет работу, пропуская уже записанные UUID;
* не печатает cookies, заголовки авторизации или тела ответов.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from uuid import UUID

try:
    import requests
    from requests import Response, Session
    from requests.exceptions import ConnectionError as RequestsConnectionError
    from requests.exceptions import RequestException, Timeout
    from requests.exceptions import SSLError as RequestsSSLError
except ImportError:
    requests = None  # type: ignore[assignment]
    Response = Any  # type: ignore[misc,assignment]
    Session = Any  # type: ignore[misc,assignment]
    RequestsConnectionError = OSError  # type: ignore[assignment]
    RequestException = OSError  # type: ignore[assignment]
    RequestsSSLError = OSError  # type: ignore[assignment]
    Timeout = TimeoutError  # type: ignore[assignment]


DEFAULT_EXPECTED_HOST = "addressbook.sigma.sbrf.ru"
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
AUTH_STATUSES = {401, 403}


class ConfigError(RuntimeError):
    pass


class AuthExpired(RuntimeError):
    pass


class FetchError(RuntimeError):
    pass


class MissingTubNum(FetchError):
    pass


class AmbiguousTubNum(FetchError):
    pass


@dataclass(frozen=True)
class CurlRequest:
    url: str
    method: str
    headers: Dict[str, str]
    cookies: Dict[str, str]


@dataclass(frozen=True)
class Endpoint:
    url_without_query: str
    static_query: List[Tuple[str, str]]
    uuid_parameter: str


class RateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.interval = 0.0 if requests_per_second <= 0 else 1.0 / requests_per_second
        self._next_start = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        delay = self._next_start - now
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
        self._next_start = now + self.interval


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Выгрузить CSV uuid,tubNum из addressbook API."
    )
    parser.add_argument("--input", required=True, type=Path, help="Исходный CSV.")
    parser.add_argument(
        "--request-rtf",
        required=True,
        type=Path,
        help="RTF с рабочей curl-командой из браузера.",
    )
    parser.add_argument("--output", required=True, type=Path, help="Итоговый CSV.")
    parser.add_argument(
        "--id-column",
        type=int,
        default=2,
        help="Номер колонки UUID, начиная с 1 (по умолчанию: 2).",
    )
    parser.add_argument(
        "--uuid-parameter",
        default="empId",
        help="Query-параметр UUID (по умолчанию: empId).",
    )
    parser.add_argument(
        "--expected-host",
        default=DEFAULT_EXPECTED_HOST,
        help="Разрешенный API-хост; защищает cookies от отправки не туда.",
    )
    parser.add_argument(
        "--ca-bundle",
        type=Path,
        help="PEM-файл с доверенными CA для внутреннего TLS.",
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
        help="Таймаут одного HTTP-запроса в секундах (по умолчанию: 30).",
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
        "--dry-run",
        action="store_true",
        help="Проверить входные файлы и curl без HTTP-запросов.",
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
    return args


def rtf_to_text(path: Path) -> str:
    if not path.is_file():
        raise ConfigError(f"RTF-файл не найден: {path}")
    try:
        result = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ConfigError(
            "Не найден textutil. На macOS он встроен; в другой ОС заранее "
            "сохраните curl как обычный текстовый файл с расширением .txt."
        ) from exc
    if result.returncode != 0:
        raise ConfigError("Не удалось преобразовать RTF через textutil.")
    return result.stdout.decode("utf-8", errors="replace")


def request_text(path: Path) -> str:
    if path.suffix.lower() == ".rtf":
        return rtf_to_text(path)
    if not path.is_file():
        raise ConfigError(f"Файл запроса не найден: {path}")
    return path.read_text(encoding="utf-8-sig")


def parse_curl(text: str) -> CurlRequest:
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise ConfigError("Не удалось разобрать curl-команду.") from exc
    if not tokens:
        raise ConfigError("Файл запроса пуст.")

    url: Optional[str] = None
    cookie_string: Optional[str] = None
    method = "GET"
    raw_headers: List[str] = []

    index = 1 if Path(tokens[0]).name == "curl" else 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-H", "--header"}:
            index += 1
            if index >= len(tokens):
                raise ConfigError("После -H/--header нет значения.")
            raw_headers.append(tokens[index])
        elif token.startswith("--header="):
            raw_headers.append(token.split("=", 1)[1])
        elif token.startswith("-H") and token != "-H":
            raw_headers.append(token[2:])
        elif token in {"-b", "--cookie"}:
            index += 1
            if index >= len(tokens):
                raise ConfigError("После -b/--cookie нет значения.")
            cookie_string = tokens[index]
        elif token.startswith("--cookie="):
            cookie_string = token.split("=", 1)[1]
        elif token.startswith("-b") and token != "-b":
            cookie_string = token[2:]
        elif token in {"-X", "--request"}:
            index += 1
            if index >= len(tokens):
                raise ConfigError("После -X/--request нет значения.")
            method = tokens[index].upper()
        elif token.startswith("--request="):
            method = token.split("=", 1)[1].upper()
        elif token == "--url":
            index += 1
            if index >= len(tokens):
                raise ConfigError("После --url нет значения.")
            url = tokens[index]
        elif token.startswith("--url="):
            url = token.split("=", 1)[1]
        elif token.startswith(("https://", "http://")):
            url = token
        index += 1

    if not url:
        raise ConfigError("В curl-команде не найден URL.")
    if method != "GET":
        raise ConfigError(f"Ожидался GET-запрос, найден метод {method}.")
    if not cookie_string:
        raise ConfigError("В curl-команде не найден аргумент -b/--cookie.")

    headers: Dict[str, str] = {}
    for raw_header in raw_headers:
        if ":" not in raw_header:
            continue
        name, value = raw_header.split(":", 1)
        name = name.strip()
        value = value.strip()
        if name.lower() in {"cookie", "host", "content-length", "connection"}:
            continue
        if name:
            headers[name] = value

    parsed_cookies = SimpleCookie()
    try:
        parsed_cookies.load(cookie_string)
    except Exception as exc:
        raise ConfigError("Не удалось разобрать cookies из curl.") from exc
    cookies = {name: morsel.value for name, morsel in parsed_cookies.items()}
    if not cookies:
        raise ConfigError("Cookie-набор из curl оказался пустым.")

    return CurlRequest(url=url, method=method, headers=headers, cookies=cookies)


def validate_request(curl_request: CurlRequest, expected_host: str) -> Endpoint:
    split = urlsplit(curl_request.url)
    if split.scheme.lower() != "https":
        raise ConfigError("URL запроса должен использовать HTTPS.")
    if split.hostname != expected_host:
        raise ConfigError(
            f"Хост запроса {split.hostname!r} не совпадает с разрешенным "
            f"{expected_host!r}. Cookies не отправлены."
        )
    if not split.path:
        raise ConfigError("В URL запроса отсутствует API-путь.")
    return Endpoint(
        url_without_query=urlunsplit((split.scheme, split.netloc, split.path, "", "")),
        static_query=[],
        uuid_parameter="empId",
    )


def endpoint_from_request(
    curl_request: CurlRequest, expected_host: str, uuid_parameter: str
) -> Endpoint:
    validated = validate_request(curl_request, expected_host)
    split = urlsplit(curl_request.url)
    static_query = [
        (name, value)
        for name, value in parse_qsl(split.query, keep_blank_values=True)
        if name != uuid_parameter
    ]
    return Endpoint(
        url_without_query=validated.url_without_query,
        static_query=static_query,
        uuid_parameter=uuid_parameter,
    )


def resolve_tls_verify(
    ca_bundle: Optional[Path],
) -> Union[bool, str]:
    if ca_bundle is not None:
        resolved = ca_bundle.expanduser().resolve()
        if not resolved.is_file():
            raise ConfigError(f"CA bundle не найден: {resolved}")
        return str(resolved)
    return True


def create_session(
    curl_request: CurlRequest,
    expected_host: str,
    tls_verify: Union[bool, str] = True,
) -> Session:
    if requests is None:
        raise ConfigError(
            "Не установлен пакет requests. Выполните: "
            "python3 -m pip install -r requirements.txt"
        )
    session = requests.Session()
    session.verify = tls_verify
    session.headers.update(curl_request.headers)
    for name, value in curl_request.cookies.items():
        session.cookies.set(name, value, domain=expected_host, path="/")
    return session


def canonical_uuid(value: str) -> Optional[str]:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = UUID(cleaned)
    except ValueError:
        return None
    canonical = str(parsed)
    if cleaned.lower() != canonical:
        return None
    return canonical


def _tubnum_candidates(
    value: Any,
    requested_uuid: Optional[str] = None,
    ancestor_uuid_match: bool = False,
) -> List[Tuple[str, bool]]:
    candidates: List[Tuple[str, bool]] = []
    if isinstance(value, dict):
        uuid_match = ancestor_uuid_match
        if requested_uuid is not None:
            direct_uuids = {
                canonical
                for item in value.values()
                if isinstance(item, str)
                for canonical in [canonical_uuid(item)]
                if canonical is not None
            }
            if requested_uuid in direct_uuids:
                uuid_match = True
            elif direct_uuids:
                # Вложенный объект с другим UUID считается новой сущностью:
                # UUID родительского объекта на него не распространяется.
                uuid_match = False
        if "tubNum" in value and value["tubNum"] is not None:
            result = str(value["tubNum"]).strip()
            if result:
                candidates.append((result, uuid_match))
        for nested in value.values():
            candidates.extend(
                _tubnum_candidates(nested, requested_uuid, uuid_match)
            )
    elif isinstance(value, list):
        for nested in value:
            candidates.extend(
                _tubnum_candidates(nested, requested_uuid, ancestor_uuid_match)
            )
    return candidates


def _contains_requested_uuid(value: Any, requested_uuid: str) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == requested_uuid.lower()
    if isinstance(value, dict):
        return any(
            _contains_requested_uuid(nested, requested_uuid)
            for nested in value.values()
        )
    if isinstance(value, list):
        return any(
            _contains_requested_uuid(nested, requested_uuid) for nested in value
        )
    return False


def find_tubnum(value: Any, requested_uuid: Optional[str] = None) -> Optional[str]:
    candidates = _tubnum_candidates(value, requested_uuid)
    if not candidates:
        return None

    matched = {result for result, is_match in candidates if is_match}
    if len(matched) == 1:
        return next(iter(matched))
    if len(matched) > 1:
        raise AmbiguousTubNum(
            "в ответе несколько tubNum рядом с запрошенным UUID"
        )

    if requested_uuid is not None and _contains_requested_uuid(
        value, requested_uuid
    ):
        raise MissingTubNum(
            "ответ содержит запрошенный UUID, но рядом с ним нет tubNum"
        )

    unique = {result for result, _ in candidates}
    if len(unique) == 1:
        return next(iter(unique))
    raise AmbiguousTubNum("в ответе найдено несколько разных tubNum")


def retry_after_seconds(response: Response, max_backoff: float) -> Optional[float]:
    raw_value = response.headers.get("Retry-After")
    if not raw_value:
        return None
    try:
        return min(max(float(raw_value), 0.0), max_backoff)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw_value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        return min(max(seconds, 0.0), max_backoff)
    except (TypeError, ValueError, OverflowError):
        return None


def backoff_seconds(attempt: int, max_backoff: float) -> float:
    base = min(2**attempt, max_backoff)
    return min(base + random.uniform(0.0, min(1.0, base / 4)), max_backoff)


def safe_transport_error(exc: BaseException) -> str:
    if requests is not None and isinstance(exc, RequestsSSLError):
        message = str(exc).casefold()
        if "certificate_verify_failed" in message:
            reason = "CERTIFICATE_VERIFY_FAILED"
        elif "self signed certificate" in message:
            reason = "SELF_SIGNED_CERTIFICATE"
        elif "hostname" in message and (
            "mismatch" in message or "doesn" in message
        ):
            reason = "HOSTNAME_MISMATCH"
        elif "certificate required" in message:
            reason = "CLIENT_CERTIFICATE_REQUIRED"
        elif "pem lib" in message:
            reason = "PEM_ERROR"
        elif "tlsv1 alert" in message or "ssl alert" in message:
            reason = "TLS_ALERT"
        elif "eof" in message:
            reason = "UNEXPECTED_EOF"
        else:
            reason = "TLS_ERROR"
        return f"SSLError[{reason}]"
    return exc.__class__.__name__


def fetch_tubnum(
    session: Session,
    endpoint: Endpoint,
    user_uuid: str,
    timeout: float,
    retries: int,
    max_backoff: float,
    rate_limiter: RateLimiter,
) -> str:
    params = endpoint.static_query + [(endpoint.uuid_parameter, user_uuid)]
    last_error = "неизвестная ошибка"

    for attempt in range(retries + 1):
        rate_limiter.wait()
        try:
            response = session.get(
                endpoint.url_without_query,
                params=params,
                timeout=(10.0, timeout),
                allow_redirects=False,
            )
        except (Timeout, RequestsConnectionError) as exc:
            last_error = safe_transport_error(exc)
            if attempt >= retries:
                break
            time.sleep(backoff_seconds(attempt, max_backoff))
            continue
        except RequestException as exc:
            raise FetchError(f"HTTP-клиент: {exc.__class__.__name__}") from exc

        if response.status_code in AUTH_STATUSES:
            raise AuthExpired(f"HTTP {response.status_code}")
        if response.status_code in REDIRECT_STATUSES:
            raise AuthExpired(f"HTTP {response.status_code}, получен redirect")
        if response.status_code in {204, 404}:
            raise MissingTubNum(f"HTTP {response.status_code}: записи нет")
        if response.status_code in RETRYABLE_STATUSES:
            last_error = f"HTTP {response.status_code}"
            if attempt >= retries:
                break
            wait_for = retry_after_seconds(response, max_backoff)
            if wait_for is None:
                wait_for = backoff_seconds(attempt, max_backoff)
            time.sleep(wait_for)
            continue
        if not 200 <= response.status_code < 300:
            raise FetchError(f"HTTP {response.status_code}")

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            content_type = response.headers.get("Content-Type", "").lower()
            if "html" in content_type:
                raise AuthExpired("вместо JSON получен HTML") from exc
            raise FetchError("ответ API не является JSON") from exc
        tub_num = find_tubnum(payload, user_uuid)
        if tub_num is None:
            raise MissingTubNum("в JSON-ответе отсутствует tubNum")
        return tub_num

    raise FetchError(f"после {retries + 1} попыток: {last_error}")


def load_processed(output_path: Path) -> Set[str]:
    processed: Set[str] = set()
    if not output_path.exists() or output_path.stat().st_size == 0:
        return processed
    with output_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["uuid", "tubNum"]:
            raise ConfigError(
                f"У существующего файла {output_path} ожидаются колонки uuid,tubNum."
            )
        for line_number, row in enumerate(reader, start=2):
            value = canonical_uuid(row.get("uuid", ""))
            tub_num = (row.get("tubNum") or "").strip()
            if value is None or not tub_num:
                raise ConfigError(
                    f"В существующем результате {output_path} строка "
                    f"{line_number} неполная или повреждена. Основной CSV "
                    "должен содержать только непустые пары uuid,tubNum."
                )
            if value in processed:
                raise ConfigError(
                    f"В существующем результате {output_path} UUID "
                    f"дублируется (строка {line_number})."
                )
            processed.add(value)
    return processed


def open_csv_append(
    path: Path, fieldnames: Sequence[str]
) -> Tuple[Any, csv.DictWriter]:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists_with_data = path.exists() and path.stat().st_size > 0
    encoding = "utf-8" if exists_with_data else "utf-8-sig"
    handle = path.open("a", encoding=encoding, newline="")
    writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
    if not exists_with_data:
        writer.writeheader()
        handle.flush()
    return handle, writer


def default_errors_path(output_path: Path) -> Path:
    suffix = output_path.suffix or ".csv"
    return output_path.with_name(f"{output_path.stem}.errors{suffix}")


def input_rows(
    path: Path, id_column: int
) -> Iterable[Tuple[int, Optional[str], Optional[str]]]:
    if not path.is_file():
        raise ConfigError(f"Исходный CSV не найден: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            next(reader)
        except StopIteration:
            return
        for source_row, row in enumerate(reader, start=2):
            if len(row) < id_column:
                yield source_row, None, "недостаточно колонок"
                continue
            value = canonical_uuid(row[id_column - 1])
            if value is None:
                yield source_row, None, "пустой или некорректный UUID"
                continue
            yield source_row, value, None


def scan_input(path: Path, id_column: int) -> Tuple[int, int]:
    valid = 0
    invalid = 0
    for _, user_uuid, error in input_rows(path, id_column):
        if user_uuid is None or error:
            invalid += 1
        else:
            valid += 1
    return valid, invalid


def run(args: argparse.Namespace) -> int:
    curl_request = parse_curl(request_text(args.request_rtf))
    endpoint = endpoint_from_request(
        curl_request, args.expected_host, args.uuid_parameter
    )

    if args.dry_run:
        valid, invalid = scan_input(args.input, args.id_column)
        print(
            "Проверка завершена: "
            f"host={args.expected_host}, cookies={len(curl_request.cookies)}, "
            f"валидных UUID={valid}, некорректных/пустых={invalid}. "
            "HTTP-запросы не выполнялись."
        )
        return 0

    processed = load_processed(args.output)
    errors_path = default_errors_path(args.output)
    tls_verify = resolve_tls_verify(args.ca_bundle)
    session = create_session(
        curl_request, args.expected_host, tls_verify=tls_verify
    )
    try:
        output_handle, output_writer = open_csv_append(
            args.output, ["uuid", "tubNum"]
        )
        try:
            error_handle, error_writer = open_csv_append(
                errors_path, ["uuid", "source_row", "error"]
            )
        except Exception:
            output_handle.close()
            raise
    except Exception:
        session.close()
        raise
    rate_limiter = RateLimiter(args.rate)

    attempted = 0
    written = 0
    no_tubnum = 0
    invalid = 0
    failed = 0
    skipped = 0
    consecutive_errors = 0
    stopped_for_auth = False

    try:
        for source_row, user_uuid, input_error in input_rows(
            args.input, args.id_column
        ):
            if input_error or user_uuid is None:
                invalid += 1
                error_writer.writerow(
                    {"uuid": "", "source_row": source_row, "error": input_error}
                )
                continue
            if user_uuid in processed:
                skipped += 1
                continue
            if args.limit is not None and attempted >= args.limit:
                break

            attempted += 1
            try:
                tub_num = fetch_tubnum(
                    session=session,
                    endpoint=endpoint,
                    user_uuid=user_uuid,
                    timeout=args.timeout,
                    retries=args.retries,
                    max_backoff=args.max_backoff,
                    rate_limiter=rate_limiter,
                )
            except AuthExpired as exc:
                stopped_for_auth = True
                print(
                    "Сессия addressbook истекла или ведет на страницу входа "
                    f"({exc}). Обновите RTF с curl и запустите ту же команду: "
                    "существующий CSV будет продолжен.",
                    file=sys.stderr,
                )
                break
            except FetchError as exc:
                if isinstance(exc, MissingTubNum):
                    no_tubnum += 1
                    consecutive_errors = 0
                else:
                    failed += 1
                    consecutive_errors += 1
                error_writer.writerow(
                    {
                        "uuid": user_uuid,
                        "source_row": source_row,
                        "error": str(exc),
                    }
                )
                error_handle.flush()
                if (
                    not isinstance(exc, MissingTubNum)
                    and consecutive_errors >= args.max_consecutive_errors
                ):
                    print(
                        f"Остановка после {consecutive_errors} ошибок подряд. "
                        "Неуспешные UUID не добавлены в результат и будут "
                        "повторены при следующем запуске.",
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
            processed.add(user_uuid)
            written += 1

            if written % args.flush_every == 0:
                output_handle.flush()
                error_handle.flush()
            if attempted % args.progress_every == 0:
                print(
                    f"Обработано новых UUID: {attempted}; записано: {written}; "
                    f"ошибок: {failed}; без tubNum: {no_tubnum}.",
                    file=sys.stderr,
                )
    finally:
        output_handle.flush()
        error_handle.flush()
        output_handle.close()
        error_handle.close()
        session.close()

    print(
        f"Итог: новых попыток={attempted}, записано={written}, "
        f"без tubNum={no_tubnum}, ошибок={failed}, "
        f"некорректных строк={invalid}, ранее готовых={skipped}. "
        f"Результат: {args.output}. Ошибки: {errors_path}."
    )
    return 3 if stopped_for_auth else (1 if failed or no_tubnum else 0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = parse_args(argv)
        return run(args)
    except ConfigError as exc:
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
