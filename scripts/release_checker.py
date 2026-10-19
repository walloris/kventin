from jira import JIRA
from atlassian import Confluence
import os
import sys
import re
import time
import html
import hashlib
import json
import urllib3
import logging
import requests
import tempfile
from itertools import count
from html.parser import HTMLParser
from pathlib import Path
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote, urlsplit

script_dir = Path(__file__).resolve().parent
parent_dir = script_dir.parent
if str(parent_dir) not in sys.path:
    sys.path.insert(0, str(parent_dir))
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

try:
    from gigachat import get_gigachat_token, check_summary_description_match
except ImportError:
    print("⚠️ Внимание: не удалось импортировать функции из gigachat.py")
    def get_gigachat_token(env): return None
    def check_summary_description_match(env, batch, access_token, max_retries=3): return None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Глушим все WARNING логи (jira rate-limit, confluence, requests и т.д.)
logging.getLogger("jira").setLevel(logging.ERROR)
logging.getLogger("jira.client").setLevel(logging.ERROR)
logging.getLogger("jira.resilientsession").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("atlassian").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)

try:
    from config import config
except ImportError:
    import config as env_config

    jira_url = getattr(env_config, "JIRA_URL", os.getenv("JIRA_URL", "")).rstrip("/")
    jira_verify_ssl = getattr(
        env_config,
        "JIRA_VERIFY_SSL",
        os.getenv("JIRA_VERIFY_SSL", "1").lower() in ("1", "true", "yes"),
    )
    config = {
        "jira": {
            "url": jira_url,
            "token": getattr(env_config, "JIRA_API_TOKEN", os.getenv("JIRA_API_TOKEN", "")),
            "options": {"server": jira_url, "verify": jira_verify_ssl},
        },
        "confluence": {
            "url": os.getenv("CONFLUENCE_URL", "").rstrip("/"),
            "token": os.getenv("CONFLUENCE_TOKEN", ""),
            "verify_ssl": os.getenv("CONFLUENCE_VERIFY_SSL", "1").lower() in ("1", "true", "yes"),
            "parent_page_id": os.getenv("CONFLUENCE_PARENT_PAGE_ID", ""),
        },
    }

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)

# Промежуточная версия для Jenkins: True — подробная диагностика, False — обычные логи.
# Команду запуска менять не нужно. RELEASE_CHECKER_DEBUG_HTTP=1/0 переопределяет этот параметр.
HTTP_DEBUG_ENABLED = True
# Обычная проверка включена. Для read-only эксперимента: True или
# RELEASE_CHECKER_LOOKUP_DIAGNOSTICS=1 (матрица API вместо отчёта).
LOOKUP_DIAGNOSTICS_ENABLED = False
LOOKUP_DIAGNOSTIC_FOCUS = 'cycles'  # both / pr / cycles
# Из лога №1562: известный цикл для независимой проверки прямого чтения.
LOOKUP_DIAGNOSTIC_CYCLE = ''

ALLOWED_TESTERS = {
    "Абдуллаев Магомедэмин Шамильевич",
    "Годунова Дарья Алексеевна", "Гулиев Руслан Иса оглы",
    "Калашников Андрей Романович", "Канаев Леонид Олегович",
    "Меркуленков Дмитрий Игоревич",
    "Лакутин Роман Денисович",
    "Метляев Игорь Андреевич", "Петрунин Никита Анатольевич",
    "Приколотина Евгения Александровна",
    "Симиник Даниил Григорьевич", "Федоров Никита Андреевич",
    "Чиж Мария Михайловна", "Синица Захар Алексеевич", "Абдулгалимов Гамзат Абусуньянович"
}
WORKLOG_EXEMPT_COMMENT_AUTHORS = {
    "Метляев Игорь Андреевич",
    "Никонов Александр Алексеевич",
}
WORKLOG_EXEMPT_EXCLUDED_PROJECTS = {"HRC"}

ALLOWED_ARCHITECTURE_CLOSERS = {
    "Черешнев Антон Олегович", "Абдуллин Нурали Нуриевич",
    "Базаева Тамара Виталиевна", "Калашникова Екатерина Рашидовна",
    "Куданов Александр Андреевич", "Куренков Николай Николаевич",
    "Матвеев Антон Анатольевич", "Махмудов Джамбулат Рамизович",
    "Абдуллаев Магомед Русланович", "Плохотнюк Анна Дмитриевна",
}
ARCHITECTURE_CLOSED_STATUSES = {
    'closed', 'закрыта', 'закрыто', 'done', 'готово', 'выполнено',
    'resolved', 'решена', 'решено',
}
PR_MAX_ATTEMPTS = 3
PR_RETRY_DELAY_SECONDS = 0.5
PR_LOOKUP_TIMEOUT_SECONDS = 30
PR_REQUEST_TIMEOUT_SECONDS = 3
PR_SUMMARY_TIMEOUT_SECONDS = 5
PR_DETAIL_TIMEOUT_SECONDS = 12
PR_FAILURE_COOLDOWN_SECONDS = 15
PR_LEGACY_PROVIDERS = ('stash', 'bitbucket', 'bitbucket-server')
PR_RELEASE_TIMEOUT_SECONDS = 120
PR_ENDPOINT_FAILURE_LIMIT = 3

# Статус ТК, который считается утверждённым (Approved)
ZEPHYR_APPROVED_STATUS = "Approved"

# Пространство Zephyr, в котором лежат тест-кейсы и тест-циклы.
# Ключ ТЦ выглядит как HRPQA-C133028.
ZEPHYR_TEST_CASE_PROJECT_KEY = "HRPQA"
ZEPHYR_TC_PROJECT_KEY = ZEPHYR_TEST_CASE_PROJECT_KEY
ZEPHYR_TEST_CYCLE_PROJECT_KEYS = tuple(
    dict.fromkeys(
        project_key.strip()
        for project_key in os.getenv("ZEPHYR_TEST_CYCLE_PROJECT_KEYS", ZEPHYR_TEST_CASE_PROJECT_KEY).split(",")
        if project_key.strip()
    )
)
ZEPHYR_ENABLE_FULL_CYCLE_SCAN = os.getenv("ZEPHYR_ENABLE_FULL_CYCLE_SCAN", "1").lower() in (
    "1",
    "true",
    "yes",
)
ZEPHYR_EXTENDED_CYCLE_DIAG = os.getenv("ZEPHYR_EXTENDED_CYCLE_DIAG", "0").lower() in (
    "1",
    "true",
    "yes",
)
ZEPHYR_DIRECT_CYCLE_SCAN_ENABLED = os.getenv("ZEPHYR_DIRECT_CYCLE_SCAN_ENABLED", "1").lower() in (
    "1",
    "true",
    "yes",
)
ZEPHYR_DIRECT_CYCLE_SCAN_LIMIT = int(os.getenv("ZEPHYR_DIRECT_CYCLE_SCAN_LIMIT", "300"))
ZEPHYR_DIRECT_CYCLE_SCAN_FORWARD_LIMIT = int(os.getenv("ZEPHYR_DIRECT_CYCLE_SCAN_FORWARD_LIMIT", "300"))
ZEPHYR_DIRECT_CYCLE_SCAN_FORWARD_MISS_LIMIT = int(os.getenv("ZEPHYR_DIRECT_CYCLE_SCAN_FORWARD_MISS_LIMIT", "20"))
ZEPHYR_DIRECT_CYCLE_SCAN_START = os.getenv("ZEPHYR_DIRECT_CYCLE_SCAN_START", "133028").strip()
ZEPHYR_CYCLE_CACHE_PATH = Path(
    os.getenv("ZEPHYR_CYCLE_CACHE_PATH", str(
        Path(tempfile.gettempdir()) / f"kventin-release-checker-{os.getuid()}" / "zephyr_cycle_cache.json"))
)
ZEPHYR_REQUEST_TIMEOUT_SECONDS = int(os.getenv("ZEPHYR_REQUEST_TIMEOUT_SECONDS", "10"))
ZEPHYR_MAX_RETRIES_PER_REQUEST = int(os.getenv("ZEPHYR_MAX_RETRIES_PER_REQUEST", "3"))
ZEPHYR_MAX_FAILED_REQUESTS = int(os.getenv("ZEPHYR_MAX_FAILED_REQUESTS", "20"))
ZEPHYR_LOOKUP_TIMEOUT_SECONDS = 45
ZEPHYR_REQUEST_BUDGET_SECONDS = 32
# Reserve fallback time inside the existing 45-second cycle lookup budget.
ZEPHYR_CYCLE_DISCOVERY_SECONDS = 25
ZEPHYR_CYCLE_METADATA_SECONDS = 8
# Jenkins №1570: 5000 items/page read the full 23059-cycle index within discovery budget.
ZEPHYR_CYCLE_INDEX_PAGE_SIZE = int(os.getenv("ZEPHYR_CYCLE_INDEX_PAGE_SIZE", "5000"))
ZEPHYR_CASE_FIELDS = 'key,name,status,customFields,issueLinks'
CHECK_TABLE_HEADER = '|| Проверка || Результат || Время, с || Объекты || Успех / ошибки / предупреждения || Замечания ||'
LEGACY_COMMENT_TABLE_HEADER = '|| Объект релиза || Замечания ||'
COMMENT_TABLE_HEADER = '|| Объект релиза || Статус || Что требует внимания ||'

# Имя кастомного поля "Вид тестирования" в Zephyr Scale ТК/ТЦ.
# Поле ищется в customFields ответа API GET /rest/atm/1.0/testcase/{key}
# и GET /rest/atm/1.0/testrun/{key}.
# Если на вашем инстансе поле называется иначе — подставьте нужное значение.
ZEPHYR_TESTING_TYPE_FIELD = "Вид тестирования"

# Допустимые значения "Вида тестирования"
ZEPHYR_TESTING_TYPE_NEW = "Новый функционал"
ZEPHYR_TESTING_TYPE_REGRESSION = "Регресс"

# В релизе и каждой Story/Bug должен быть указан технический контур.
REQUIRED_PLATFORM_LABELS = {'web', 'back', 'mobile'}
TEST_CYCLE_KEY_RE = re.compile(r'\b[A-Z][A-Z0-9]+-C\d+\b')

# Название Jira-поля с КЭ в разных инсталляциях может отличаться, поэтому
# ищем field id по metadata Jira, а не хардкодим customfield_*.
SERVICE_KE_FIELD_NAME_CANDIDATES = (
    'КЭ',
    'КЕ',
    'КЭ сервиса',
    'КЕ сервиса',
    'Конфигурационная единица',
    'Configuration item',
)
BUG_SKIP_REASON_FIELD_ID = 'customfield_16801'
BUG_SKIP_REASON_ALLOWED_VALUES = {
    'неполнота тестовой модели',
    'не выполнены требования кибербезопасности',
    'тест-кейс не запланирован',
    'тест-кейс не выполнен',
    'некорректные тестовые данные',
    'нарушение процедуры тестирования',
    'несоответствие тестового окружения',
    'ошибка при установке',
    'неакцептованные изменения в ппо/спо',
    'пропущен этап тестирования',
    'ошибка в согласованных требованиях',
    'несогласованные изменения требований',
    'отсутствие требований',
    'изменение/проблема на стороне партнеров/провайдеров',
    'ошибка была известна в релизе',
}
BACK_REGRESS_CYCLE_ALIASES = ('Регресс', 'Regress', 'Regression')
BACK_NF_CYCLE_ALIASES = ('НФ', 'NF', 'Новый функционал', 'Новая функциональность')
BACK_API_CHANNEL_ALIASES = ('api',)
BACK_DEVICE_BROWSER_CHANNEL_ALIASES = ('ipad/pwa/safari/sberbrowser',)
WEB_DEVICE_BROWSER_NF_CHANNEL_ALIASES = ('ipad/pwa/safari/sberbrowser',)
WEB_DEVICE_BROWSER_NF_CHANNEL_ALIASES_NEUROUI = ('ipad/safari/sberbrowser',)
WEB_REGRESS_CHANNELS = (
    ('ipad', ('ipad',)),
    ('pwa', ('pwa',)),
    ('safari', ('safari',)),
    ('sberbrowser', ('sberbrowser',)),
)
WEB_REGRESS_CHANNELS_NEUROUI = (
    ('ipad', ('ipad',)),
    ('safari', ('safari',)),
    ('sberbrowser', ('sberbrowser',)),
)


def is_allowed_tester(author_name):
    if not author_name or author_name == 'Неизвестно':
        return False
    author_lower = author_name.lower().strip()
    for allowed in ALLOWED_TESTERS:
        allowed_lower = allowed.lower().strip()
        allowed_parts = allowed_lower.split()
        author_parts = author_lower.split()
        matched_parts = 0
        for allowed_part in allowed_parts:
            for author_part in author_parts:
                if (allowed_part == author_part or
                        (len(allowed_part) > 3 and allowed_part in author_part) or
                        (len(author_part) > 3 and author_part in allowed_part)):
                    matched_parts += 1
                    break
        if matched_parts >= 2:
            return True
    return False


def extract_issue_key_from_url(url_or_key):
    pattern = r'([A-Z][A-Z0-9]+-\d+)'
    match = re.search(pattern, url_or_key)
    if match:
        return match.group(1)
    return url_or_key


def normalize_status_name(status: str) -> str:
    return str(status or '').strip().casefold()


def normalize_field_text(value: object) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def clean_report_text(value: object) -> str:
    """Декодировать HTML-энтити из Jira/рендеренных полей перед выводом отчета."""
    text = normalize_field_text(value)
    previous = None
    while text and text != previous:
        previous = text
        text = html.unescape(text)
    return text


def trim_report_text(value: object, limit: int) -> str:
    text = clean_report_text(value)
    if len(text) <= limit:
        return text
    return text[:limit]


def normalize_test_cycle_name(value: object) -> str:
    """Нормализация имени ТЦ для проверки масок: регистр, пробелы и точки."""
    text = normalize_field_text(value).casefold()
    text = re.sub(r'\s*\.\s*', '.', text)
    text = re.sub(r'\s*/\s*', '/', text)
    text = re.sub(r'\.{2,}', '.', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip(' .')


def normalize_test_cycle_name_without_parenthesized_ids(value: object) -> str:
    """
    Дополнительная нормализация для КЭ вида SmartProfile(2295205).
    ID в скобках в имени КЭ не должен ломать матчинг маски ТЦ.
    """
    text = normalize_test_cycle_name(value)
    text = re.sub(r'\s*\([^)]*\)', '', text)
    text = re.sub(r'\s*\.\s*', '.', text)
    text = re.sub(r'\.{2,}', '.', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip(' .')


def test_cycle_name_matches_expected_variant(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    if not actual.startswith(expected):
        return False

    suffix = actual[len(expected):].strip(' .')
    if not suffix:
        return True

    return bool(re.match(r'^(\d{1,2}[./-]\d{1,2}([./-]\d{2,4})?|от\s+\d{1,2}[./-]\d{1,2}([./-]\d{2,4})?)\b', suffix))


def test_cycle_name_matches_mask(
    cycle_name: object,
    release_key: str,
    service_ke: str,
    channel_aliases: tuple[str, ...],
    cycle_type_aliases: tuple[str, ...],
) -> bool:
    """Проверить имя ТЦ по маске {release}.{КЭ}.{канал}.{тип} с учетом пробелов/регистра."""
    actual_variants = {
        normalize_test_cycle_name(cycle_name),
        normalize_test_cycle_name_without_parenthesized_ids(cycle_name),
    }
    for channel in channel_aliases:
        for cycle_type in cycle_type_aliases:
            expected_raw = f"{release_key}.{service_ke}.{channel}.{cycle_type}"
            expected_variants = {
                normalize_test_cycle_name(expected_raw),
                normalize_test_cycle_name_without_parenthesized_ids(expected_raw),
            }
            for actual in actual_variants:
                for expected in expected_variants:
                    if test_cycle_name_matches_expected_variant(actual, expected):
                        return True
    return False


def build_cycle_mask_hint(
    release_key: str,
    service_aliases: list[str],
    channel_aliases: tuple[str, ...],
    cycle_type_hint: str,
) -> str:
    masks = []
    for service_alias in service_aliases:
        for channel_alias in channel_aliases:
            masks.append(f"{release_key}.{service_alias}.{channel_alias}.{cycle_type_hint}")
    return " / ".join(masks)


def service_ke_name_aliases(service_ke: str) -> list[str]:
    """Варианты имени КЭ для ТЦ: полное значение Jira и имя без хвостового ID в скобках."""
    original = normalize_field_text(service_ke)
    aliases = [original] if original else []
    without_parenthesized_id = normalize_field_text(re.sub(r'\s*\([^)]*\)\s*$', '', original))
    if without_parenthesized_id and without_parenthesized_id.casefold() not in {a.casefold() for a in aliases}:
        aliases.append(without_parenthesized_id)
    return aliases


def extract_jira_field_value(raw_val: object) -> Optional[str]:
    if raw_val is None:
        return None
    if hasattr(raw_val, 'value'):
        return normalize_field_text(raw_val.value)
    if isinstance(raw_val, dict):
        for key in ('value', 'name', 'displayName'):
            if raw_val.get(key) is not None:
                return normalize_field_text(raw_val.get(key))
        values = [extract_jira_field_value(item) for item in raw_val.values()]
        return normalize_field_text(' '.join(value for value in values if value))
    if isinstance(raw_val, list):
        values = [extract_jira_field_value(item) for item in raw_val]
        return ', '.join(value for value in values if value)
    return normalize_field_text(raw_val)


def extract_jira_field_values(raw_val: object) -> list[str]:
    """Извлекает список человекочитаемых значений Jira field/select/multiselect."""
    if raw_val is None:
        return []
    if isinstance(raw_val, list):
        values = []
        for item in raw_val:
            values.extend(extract_jira_field_values(item))
        return [value for value in values if value]
    value = extract_jira_field_value(raw_val)
    return [value] if value else []


class JiraTableFieldParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self._current_row = None
        self._current_cell = None
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._current_row = []
        elif tag == 'td' and self._current_row is not None:
            self._in_cell = True
            self._current_cell = {'text': [], 'links': []}
        elif tag == 'a' and self._in_cell and self._current_cell is not None:
            href = dict(attrs).get('href')
            if href:
                self._current_cell['links'].append(href)

    def handle_data(self, data):
        if self._in_cell and self._current_cell is not None:
            text = normalize_field_text(data)
            if text:
                self._current_cell['text'].append(text)

    def handle_endtag(self, tag):
        if tag == 'td' and self._current_row is not None and self._current_cell is not None:
            self._current_row.append({
                'text': normalize_field_text(' '.join(self._current_cell['text'])),
                'links': self._current_cell['links'],
            })
            self._current_cell = None
            self._in_cell = False
        elif tag == 'tr' and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None


class ZephyrScaleClient:
    """
    Клиент для работы с Zephyr Scale REST API (Server/DC версия).

    Используемые эндпоинты:
      GET /rest/atm/1.0/issuelink/{issueKey}/testcases
          — список ТК, прилинкованных к задаче Jira.
      GET /rest/atm/1.0/testrun/search?query=...
          — поиск ТЦ по проекту и issueKeys.
      GET /rest/atm/1.0/issuelink/{issueKey}/testruns
          — список ТЦ, прилинкованных к задаче Jira (fallback).
      GET /rest/atm/1.0/testcase/{testCaseKey}
          — полная информация о ТК, включая customFields.
      GET /rest/atm/1.0/testrun/{testRunKey}
          — детали ТЦ, включая customFields.
      GET /rest/atm/1.0/testrun/{testRunKey}/testresults
          — список ТК (результатов) внутри ТЦ.
    """

    def __init__(self, base_url: str, token: str, verify_ssl: bool = False):
        self.base_url = base_url.rstrip('/')
        self.verify_ssl = verify_ssl
        self.max_retries = ZEPHYR_MAX_RETRIES_PER_REQUEST
        self.request_timeout_seconds = ZEPHYR_REQUEST_TIMEOUT_SECONDS
        self.max_failed_requests = ZEPHYR_MAX_FAILED_REQUESTS
        self.failed_requests = 0
        self.fatal_error = None
        self.retry_state = {}
        self.retry_status_codes = {408, 429, 500, 502, 503, 504}
        self.session = ReleaseHttpSession()
        self.session.verify = verify_ssl
        self.session.headers.update({
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        })
        self.last_test_cycle_search_stats: list[dict] = []

    def _raise_if_failed_request_limit_reached(self) -> None:
        if self.fatal_error:
            raise RuntimeError(self.fatal_error)
        if self.failed_requests >= self.max_failed_requests:
            raise RuntimeError(
                "Zephyr API: лимит неуспешных запросов исчерпан "
                f"({self.failed_requests}/{self.max_failed_requests}); "
                "дальнейшие Zephyr-запросы остановлены"
            )

    def is_failed_request_limit_reached(self) -> bool:
        return bool(self.fatal_error) or self.failed_requests >= self.max_failed_requests

    def _mark_failed_request(self, reason: str) -> None:
        self.failed_requests += 1
        print(
            f"   ⚠️ Zephyr API: неуспешный запрос {self.failed_requests}/{self.max_failed_requests}"
            f" ({reason})"
        )

    @contextmanager
    def lookup_budget(self, seconds):
        previous = getattr(self, '_lookup_deadline', None)
        deadline = time.monotonic() + seconds
        self._lookup_deadline = min(previous, deadline) if previous is not None else deadline
        try:
            yield
        finally:
            self._lookup_deadline = previous

    def lookup_budget_exhausted(self):
        deadline = getattr(self, '_lookup_deadline', None)
        return deadline is not None and time.monotonic() >= deadline

    def requests_paused(self):
        return self.retry_state.get('until', 0) > time.monotonic()

    def _get_with_retries(self, url: str, **kwargs) -> requests.Response:
        self._raise_if_failed_request_limit_reached()
        lookup_deadline = getattr(self, '_lookup_deadline', None)
        deadline = min(kwargs.pop('deadline', float('inf')),
                       lookup_deadline if lookup_deadline is not None else float('inf'),
                       time.monotonic() + ZEPHYR_REQUEST_BUDGET_SECONDS)
        validate = kwargs.pop('validate', validate_json_payload)
        try:
            response = bounded_get(self.session, url, source='Zephyr',
                                   attempts=self.max_retries,
                                   timeout=kwargs.pop('timeout', self.request_timeout_seconds),
                                   deadline=deadline, validate=validate, retry_state=self.retry_state, **kwargs)
        except requests.exceptions.SSLError as exc:
            self.fatal_error = request_failure_kind(exc)
            raise RuntimeError(self.fatal_error) from exc
        except RequestNotSent:
            raise  # Budget/cooldown skips are not failures of the Zephyr service.
        except (requests.RequestException, ValueError) as exc:
            self._mark_failed_request(request_failure_kind(exc))
            raise
        if response.status_code in {401, 403}:
            self.fatal_error = f'Zephyr: HTTP {response.status_code}; проверьте права доступа'
            raise RuntimeError(self.fatal_error)
        if response.status_code in self.retry_status_codes:
            self._mark_failed_request(f'HTTP {response.status_code}')
        return response

    def _get_once(self, url: str, **kwargs) -> requests.Response:
        """Один быстрый GET без ретраев для диагностических/спекулятивных endpoint'ов."""
        self._raise_if_failed_request_limit_reached()
        kwargs.setdefault('timeout', self.request_timeout_seconds)
        lookup_deadline = getattr(self, '_lookup_deadline', None)
        try:
            response = bounded_get(self.session, url, source='Zephyr', attempts=1,
                timeout=kwargs.pop('timeout'), deadline=min(time.monotonic() + self.request_timeout_seconds,
                    lookup_deadline if lookup_deadline is not None else float('inf')),
                retry_state=self.retry_state, **kwargs)
        except requests.exceptions.SSLError as e:
            self.fatal_error = request_failure_kind(e)
            raise RuntimeError(self.fatal_error) from e
        except RequestNotSent:
            raise
        except requests.RequestException as e:
            self._mark_failed_request(request_failure_kind(e))
            self._raise_if_failed_request_limit_reached()
            raise
        if response.status_code in {401, 403}:
            self.fatal_error = f'Zephyr: HTTP {response.status_code}; проверьте права доступа'
            raise RuntimeError(self.fatal_error)
        if response.status_code in self.retry_status_codes:
            self._mark_failed_request(f"HTTP {response.status_code}")
            self._raise_if_failed_request_limit_reached()
        return response

    def get_test_cases_for_issue(self, issue_key: str) -> tuple[list[dict], Optional[str]]:
        """
        Возвращает (list_of_tcs, error_message).
          - error_message = None — ответ получен успешно
          - error_message = str  — АПИ вернул ошибку (статус код / exception)
        Эндпоинт: GET /rest/atm/1.0/issuelink/{issueKey}/testcases
        """
        deadline = time.monotonic() + ZEPHYR_LOOKUP_TIMEOUT_SECONDS
        errors = []
        url = f"{self.base_url}/rest/atm/1.0/issuelink/{quote(issue_key, safe='')}/testcases"
        try:
            # Restore the request proven to work before 08.10: no projection.
            response = self._get_with_retries(url, deadline=deadline, validate=self._test_case_page)
            if response.status_code == 200:
                cases, more = self._test_case_page(response.json())
                if not more:
                    return cases, None
                errors.append('issuelink вернул неполную страницу')
            else:
                errors.append(f'issuelink HTTP {response.status_code}')
        except Exception as exc:
            errors.append(str(exc))
        if self.is_failed_request_limit_reached():
            return [], '; '.join(errors)
        # Documented Server/DC query. Do not scan the whole project or use Xray.
        query = f'projectKey = "{ZEPHYR_TC_PROJECT_KEY}" AND issueKeys IN ({json.dumps(issue_key)})'
        url = f'{self.base_url}/rest/atm/1.0/testcase/search'
        found = {}
        start = 0
        try:
            for _ in range(50):
                response = self._get_with_retries(url, deadline=deadline, validate=self._test_case_page,
                    params={'query': query, 'fields': ZEPHYR_CASE_FIELDS, 'startAt': start, 'maxResults': 100})
                if response.status_code != 200:
                    raise RuntimeError(f'testcase/search HTTP {response.status_code}')
                payload = response.json()
                cases, more = self._test_case_page(payload, start=start)
                before = len(found)
                for case in cases:
                    found[self.get_test_case_key(case)] = case
                if cases and len(found) - before != len(cases):
                    raise ValueError('Zephyr: пагинация не продвигается')
                if more is False or (more is None and len(cases) < 100):
                    return list(found.values()), None
                if not cases:
                    raise ValueError('Zephyr: пустая промежуточная страница ТК')
                start += len(cases)
            raise ValueError('Zephyr: превышен лимит страниц ТК')
        except Exception as exc:
            errors.append(str(exc))
            return list(found.values()), '; '.join(errors)

    @staticmethod
    def _test_case_page(payload, start=0):
        """Validate instead of treating an unknown JSON envelope as zero coverage."""
        validate_json_payload(payload)
        more = None
        if isinstance(payload, list):
            cases = payload
        else:
            cases = next((payload[k] for k in ('testCases', 'results', 'values', 'items')
                          if isinstance(payload.get(k), list)), None)
            if cases is None:
                raise ValueError('Zephyr: неизвестный формат списка ТК')
            if isinstance(payload.get('total'), int) and not isinstance(payload['total'], bool):
                if payload['total'] < start + len(cases):
                    raise ValueError('Zephyr: противоречивое число ТК в ответе')
                more = start + len(cases) < payload['total']
            elif isinstance(payload.get('isLast'), bool):
                more = not payload['isLast']
        if any(not isinstance(c, dict) or not re.fullmatch(r'[A-Z][A-Z0-9]*-T\d+',
                ZephyrScaleClient.get_test_case_key(c)) for c in cases):
            raise ValueError('Zephyr: ТК без корректного ключа')
        return cases, more

    def get_test_case_details(self, tc_key: str) -> Optional[dict]:
        """
        Возвращает полные данные тест-кейса, включая customFields.
        Эндпоинт: GET /rest/atm/1.0/testcase/{testCaseKey}

        Пример ответа:
        {
          "key": "HRPQA-T118396",
          "name": "Отправка SMS с валидным номером телефона",
          "status": "Approved",
          "projectKey": "HRPQA",
          "customFields": {
            "Вид тестирования": "Новый функционал",
            ...
          },
          ...
        }

        :param tc_key: Ключ тест-кейса (например, "HRPQA-T118396")
        :return: Словарь с данными ТК или None при ошибке.
        """
        url = f"{self.base_url}/rest/atm/1.0/testcase/{tc_key}"
        def validate_case(data):
            validate_json_payload(data)
            key = self.get_test_case_key(data) if isinstance(data, dict) else ''
            if not isinstance(data, dict) or (key and key != tc_key) or not any(
                    field in data for field in ('status', 'workflowStatus', 'customFields', 'testCase')):
                raise ValueError('Zephyr: ответ деталей ТК не соответствует запрошенному ключу')
        try:
            response = self._get_with_retries(url, validate=validate_case)
            if response.status_code == 200:
                data = response.json()
                return data
            return None
        except Exception:
            return None

    def get_test_case_custom_field(self, tc_details: dict, field_name: str) -> Optional[str]:
        """
        Извлекает значение кастомного поля из данных ТК.
        Zephyr Scale Server хранит кастомные поля в объекте 'customFields' (dict).
        Ключ — имя поля, значение — строка или объект.
        """
        custom_fields = tc_details.get('customFields', {})
        if isinstance(custom_fields, list):
            custom_fields = {str(f.get('name', '')): f.get('value')
                             for f in custom_fields if isinstance(f, dict)}
        if not custom_fields:
            return None
        if not isinstance(custom_fields, dict):
            return None

        val = custom_fields.get(field_name)
        if val is None:
            # Пробуем регистронезависимый поиск
            for k, v in custom_fields.items():
                if k.strip().lower() == field_name.strip().lower():
                    val = v
                    break

        if val is None:
            return None
        if isinstance(val, dict):
            return self._extract_zephyr_named_value(val) or None
        if isinstance(val, list):
            return ', '.join(self._extract_zephyr_named_value(item) for item in val) or None
        return str(val).strip()

    @staticmethod
    def _extract_zephyr_collection(data: object) -> list[dict]:
        """Достать список сущностей из разных форматов ответа Zephyr Scale."""
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        if ZephyrScaleClient.get_test_cycle_key(data):
            return [data]
        collection_keys = (
            'results',
            'testRuns',
            'testRunLinks',
            'testCycles',
            'testCycleLinks',
            'values',
            'items',
            'data',
        )
        for collection_key in collection_keys:
            collection = data.get(collection_key)
            if isinstance(collection, list):
                return [item for item in collection if isinstance(item, dict)]
            if isinstance(collection, dict):
                nested = ZephyrScaleClient._extract_zephyr_collection(collection)
                if nested:
                    return nested
        for collection in data.values():
            if isinstance(collection, list) and any(isinstance(item, dict) for item in collection):
                return [item for item in collection if isinstance(item, dict)]
            if isinstance(collection, dict):
                nested = ZephyrScaleClient._extract_zephyr_collection(collection)
                if nested:
                    return nested
        return []

    @staticmethod
    def get_test_cycle_key(cycle: dict) -> str:
        for key_field in ('key', 'testRunKey', 'testCycleKey'):
            value = cycle.get(key_field)
            if value:
                return str(value)
        for nested_field in ('testRun', 'testCycle', 'cycle', 'target', 'entity', 'object'):
            nested = cycle.get(nested_field)
            if isinstance(nested, dict):
                nested_key = ZephyrScaleClient.get_test_cycle_key(nested)
                if nested_key:
                    return nested_key
        return ''

    @staticmethod
    def get_test_cycle_name(cycle: dict) -> str:
        for name_field in ('name', 'testRunName', 'testCycleName'):
            value = cycle.get(name_field)
            if value:
                return str(value)
        for nested_field in ('testRun', 'testCycle', 'cycle', 'target', 'entity', 'object'):
            nested = cycle.get(nested_field)
            if isinstance(nested, dict):
                nested_name = ZephyrScaleClient.get_test_cycle_name(nested)
                if nested_name:
                    return nested_name
        return ''

    @staticmethod
    def _dedupe_project_keys(project_keys: list[object]) -> list[str]:
        result = []
        seen = set()
        for project_key in project_keys:
            normalized = str(project_key or '').strip()
            if not normalized:
                continue
            dedupe_key = normalized.casefold()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            result.append(normalized)
        return result

    @staticmethod
    def _cycle_mentions_issue(cycle: dict, issue_key: str) -> bool:
        # REL-1 must not match REL-10; names and both legacy/new link fields count.
        text = json.dumps(cycle, ensure_ascii=False).casefold()
        return bool(re.search(r'(?<![a-z0-9-])' + re.escape(issue_key.casefold())
                              + r'(?![a-z0-9-])', text))

    @staticmethod
    def _issue_link_endpoint_paths(issue_key: str) -> list[tuple[str, str]]:
        primary_paths = [
            ('issuelink/testruns', f'/rest/atm/1.0/issuelink/{issue_key}/testruns'),
        ]
        if not ZEPHYR_EXTENDED_CYCLE_DIAG:
            return primary_paths
        return primary_paths + [
            ('issuelink/testRuns', f'/rest/atm/1.0/issuelink/{issue_key}/testRuns'),
            ('issuelink/testrun', f'/rest/atm/1.0/issuelink/{issue_key}/testrun'),
            ('issuelink/testcycles', f'/rest/atm/1.0/issuelink/{issue_key}/testcycles'),
            ('issuelink/testCycles', f'/rest/atm/1.0/issuelink/{issue_key}/testCycles'),
            ('issuelink/testcycle', f'/rest/atm/1.0/issuelink/{issue_key}/testcycle'),
            ('issue/testruns', f'/rest/atm/1.0/issue/{issue_key}/testruns'),
            ('issue/testRuns', f'/rest/atm/1.0/issue/{issue_key}/testRuns'),
            ('issue/testcycles', f'/rest/atm/1.0/issue/{issue_key}/testcycles'),
            ('issue/testCycles', f'/rest/atm/1.0/issue/{issue_key}/testCycles'),
        ]

    def get_test_cycles_for_issue(
        self,
        issue_key: str,
        issue_id: Optional[str] = None,
        cycle_project_keys: Optional[list[str]] = None,
    ) -> list[dict]:
        """
        Возвращает список ТЦ, привязанных к релизу.

        ТЦ и ТК лежат в Zephyr-пространстве HRPQA. Ключ ТЦ выглядит как
        HRPQA-C133028; связь с релизом проверяем по issueKey внутри ТЦ.
        """
        del issue_id  # Zephyr Server/DC на этом инстансе ищем по ключу релиза.
        self.last_test_cycle_search_stats = []

        seen_keys: set[str] = set()
        result: list[dict] = []

        project_keys_to_try = self._dedupe_project_keys(
            list(cycle_project_keys or [])
            + list(ZEPHYR_TEST_CYCLE_PROJECT_KEYS)
        )

        for item in self._get_test_cycles_by_issue_link(issue_key):
            key = self.get_test_cycle_key(item)
            dedupe_key = key or f"name:{self.get_test_cycle_name(item)}"
            if dedupe_key and dedupe_key not in seen_keys:
                seen_keys.add(dedupe_key)
                result.append(item)

        if result:
            return result

        # issueKey is not a documented testrun/search filter. Servers may ignore
        # it and serialize the entire project, including all execution results.
        legacy_items = (self._get_test_cycles_by_issue_query_params(issue_key, project_keys_to_try)
                        if ZEPHYR_EXTENDED_CYCLE_DIAG else [])
        for item in legacy_items:
            key = self.get_test_cycle_key(item)
            dedupe_key = key or f"name:{self.get_test_cycle_name(item)}"
            if dedupe_key and dedupe_key not in seen_keys:
                seen_keys.add(dedupe_key)
                result.append(item)

        if result:
            return result

        for proj in project_keys_to_try:
            found = self._search_test_cycles_by_name(proj, issue_key)
            for item in found:
                key = self.get_test_cycle_key(item)
                dedupe_key = key or f"name:{self.get_test_cycle_name(item)}"
                if dedupe_key and dedupe_key not in seen_keys:
                    seen_keys.add(dedupe_key)
                    result.append(item)

        if not result:
            self._print_test_cycle_search_debug(issue_key, project_keys_to_try)

        return result

    def _get_test_cycles_by_issue_link(self, issue_key: str) -> list[dict]:
        """Быстрый поиск ТЦ, связанных с Jira-задачей, через Zephyr issue endpoints."""
        result = []
        for endpoint_name, endpoint_path in self._issue_link_endpoint_paths(issue_key):
            url = f"{self.base_url}{endpoint_path}"
            try:
                # Retry transient failures, but spend no more than the old single GET.
                response = self._get_with_retries(
                    url, deadline=time.monotonic() + self.request_timeout_seconds)
                if response.status_code != 200:
                    self.last_test_cycle_search_stats.append({
                        'mode': 'issuelink',
                        'endpoint': endpoint_name,
                        'status': response.status_code,
                        'count': 0,
                        'body': response.text[:200] if response.status_code not in (401, 403, 404) else '',
                    })
                    continue
                items = self._extract_zephyr_collection(response.json())
                self.last_test_cycle_search_stats.append({
                    'mode': 'issuelink',
                    'endpoint': endpoint_name,
                    'status': response.status_code,
                    'count': len(items),
                })
                if items:
                    result.extend(items)
            except Exception as e:
                self.last_test_cycle_search_stats.append({
                    'mode': 'issuelink',
                    'endpoint': endpoint_name,
                    'status': 'exception',
                    'count': 0,
                    'error': str(e),
                })
        return result

    def _get_test_cycles_by_issue_query_params(self, issue_key: str, project_keys: list[str]) -> list[dict]:
        """Пробует O(1)-варианты, где issueKey передается отдельным query-параметром."""
        result = []
        seen_keys = set()

        for project_key in project_keys:
            candidates = self._issue_query_param_candidates(issue_key, project_key)
            for endpoint_name, endpoint_path, params in candidates:
                url = f"{self.base_url}{endpoint_path}"
                try:
                    response = self._get_once(url, params=params)
                    if response.status_code == 200:
                        try:
                            items = self._extract_zephyr_collection(response.json())
                        except Exception:
                            items = []
                        matched = [item for item in items if self._cycle_mentions_issue(item, issue_key)]
                        if not matched and 0 < len(items) <= 10:
                            for item in items:
                                cycle_key = self.get_test_cycle_key(item)
                                if not cycle_key:
                                    continue
                                details = self.get_test_cycle_details(cycle_key)
                                if details and self._cycle_mentions_issue(details, issue_key):
                                    matched.append(details)
                    else:
                        items = []
                        matched = []

                    self.last_test_cycle_search_stats.append({
                        'mode': 'issue-query',
                        'endpoint': endpoint_name,
                        'project_key': project_key,
                        'status': response.status_code,
                        'seen': len(items),
                        'count': len(matched),
                        'body': response.text[:200] if response.status_code not in (200, 401, 403, 404) else '',
                    })

                    for item in matched:
                        key = self.get_test_cycle_key(item)
                        dedupe_key = key or f"name:{self.get_test_cycle_name(item)}"
                        if dedupe_key and dedupe_key not in seen_keys:
                            seen_keys.add(dedupe_key)
                            result.append(item)
                except Exception as e:
                    self.last_test_cycle_search_stats.append({
                        'mode': 'issue-query',
                        'endpoint': endpoint_name,
                        'project_key': project_key,
                        'status': 'exception',
                        'seen': 0,
                        'count': 0,
                        'error': str(e),
                    })
        return result

    @staticmethod
    def _issue_query_param_candidates(issue_key: str, project_key: str) -> list[tuple[str, str, dict]]:
        project_query = f'projectKey = "{project_key}"'
        primary_candidates = [
            (
                'testrun/search issueKey',
                '/rest/atm/1.0/testrun/search',
                {'query': project_query, 'issueKey': issue_key, 'maxResults': 50, 'startAt': 0},
            ),
        ]
        if not ZEPHYR_EXTENDED_CYCLE_DIAG:
            return primary_candidates
        return primary_candidates + [
            (
                'testrun/search issueKeys',
                '/rest/atm/1.0/testrun/search',
                {'query': project_query, 'issueKeys': issue_key, 'maxResults': 50, 'startAt': 0},
            ),
            (
                'testrun/search jiraIssueKey',
                '/rest/atm/1.0/testrun/search',
                {'query': project_query, 'jiraIssueKey': issue_key, 'maxResults': 50, 'startAt': 0},
            ),
            (
                'testrun issueKey',
                '/rest/atm/1.0/testrun',
                {'projectKey': project_key, 'issueKey': issue_key, 'maxResults': 50, 'startAt': 0},
            ),
            (
                'testrun issueKeys',
                '/rest/atm/1.0/testrun',
                {'projectKey': project_key, 'issueKeys': issue_key, 'maxResults': 50, 'startAt': 0},
            ),
            (
                'testcycle/search issueKey',
                '/rest/atm/1.0/testcycle/search',
                {'query': project_query, 'issueKey': issue_key, 'maxResults': 50, 'startAt': 0},
            ),
            (
                'testcycle issueKey',
                '/rest/atm/1.0/testcycle',
                {'projectKey': project_key, 'issueKey': issue_key, 'maxResults': 50, 'startAt': 0},
            ),
        ]

    def _search_test_cycles_by_name(self, project_key: str, issue_key: str) -> list[dict]:
        """Read a bounded, lightweight index using the documented Server API.

        Server only supports projectKey/folder filters for cycles, not name or
        issueKey. Projecting out execution items avoids the expensive full payload.
        """
        if ZEPHYR_EXTENDED_CYCLE_DIAG:
            targeted = self._search_test_cycles_by_name_query(project_key, issue_key)
            if targeted:
                return targeted
        if not ZEPHYR_ENABLE_FULL_CYCLE_SCAN:
            self.last_test_cycle_search_stats.append({
                'mode': 'search', 'project_key': project_key, 'status': 'exception',
                'pages': 0, 'total_seen': 0, 'count': 0, 'error': 'project index disabled'})
            return []

        url = f"{self.base_url}/rest/atm/1.0/testrun/search"
        matched, seen_keys = [], set()
        start_at, pages, status, error = 0, 0, None, ''
        complete = False
        with self.lookup_budget(30):
            try:
                for _ in range(500):
                    response = self._get_with_retries(url, params={
                        'query': f'projectKey = "{project_key}"',
                        'fields': 'key,name,issueKey,issueLinks',
                        'maxResults': ZEPHYR_CYCLE_INDEX_PAGE_SIZE, 'startAt': start_at})
                    status = response.status_code
                    if status != 200:
                        raise ValueError(f'cycle index HTTP {status}')
                    payload = response.json()
                    if isinstance(payload, list):
                        items = payload
                    elif isinstance(payload, dict):
                        items = next((payload[k] for k in ('results', 'testRuns', 'testCycles', 'values', 'items')
                                      if isinstance(payload.get(k), list)), None)
                    else:
                        items = None
                    if items is None:
                        raise ValueError('Unknown cycle index response')
                    pages += 1
                    for item in items:
                        key = self.get_test_cycle_key(item) if isinstance(item, dict) else ''
                        if not re.fullmatch(r'[A-Z][A-Z0-9_]*-C\d+', key) or key in seen_keys:
                            raise ValueError('Invalid cycle key or repeated index page')
                        seen_keys.add(key)
                        if self._cycle_mentions_issue(item, issue_key):
                            matched.append(item)
                    start_at += len(items)
                    total = payload.get('total') if isinstance(payload, dict) else None
                    is_last = payload.get('isLast') if isinstance(payload, dict) else None
                    if type(total) is int and (total < start_at or (not items and start_at < total)):
                        raise ValueError('Inconsistent cycle index total')
                    if not items and is_last is False:
                        raise ValueError('Empty intermediate cycle index page')
                    if is_last is True or (type(total) is int and start_at >= total) or not items:
                        complete = True
                        break
                    # A plain list may be capped by the server. Continue until
                    # an empty page instead of assuming a short page is final.
                if not complete:
                    raise ValueError('Cycle index page limit reached')
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                status, error = 'exception', str(exc)
        stat = {'mode': 'search', 'project_key': project_key, 'status': status,
                'pages': pages, 'total_seen': start_at, 'count': len(matched),
                'complete': complete, 'error': error}
        self.last_test_cycle_search_stats.append(stat)
        print(f"   Zephyr cycle index: project={project_key} pages={pages} "
              f"seen={start_at} matched={len(matched)} complete={complete}"
              + (f" error={error}" if error else ''))
        # Positive matches remain useful even if a later page failed. The caller
        # must report incomplete discovery and never cache this as a full list.
        return matched

    def _search_test_cycles_by_name_query(self, project_key: str, issue_key: str) -> list[dict]:
        url = f"{self.base_url}/rest/atm/1.0/testrun/search"
        query = f'projectKey = "{project_key}" AND name ~ "{issue_key}"'
        matched, seen_pages, seen_keys = [], set(), set()
        start_at, total_seen, status, error = 0, 0, None, ''
        with self.lookup_budget(ZEPHYR_LOOKUP_TIMEOUT_SECONDS):
            try:
                while True:
                    response = self._get_with_retries(url, params={
                        'query': query, 'maxResults': 50, 'startAt': start_at})
                    status = response.status_code
                    if status != 200:
                        break
                    payload = response.json()
                    items = self._extract_zephyr_collection(payload)
                    fingerprint = json.dumps(items, sort_keys=True)
                    if items and fingerprint in seen_pages:
                        raise ValueError('Zephyr повторил страницу ТЦ; полнота списка не подтверждена')
                    seen_pages.add(fingerprint)
                    total_seen += len(items)
                    for item in items:
                        name = self.get_test_cycle_name(item)
                        key = self.get_test_cycle_key(item) or name
                        if key not in seen_keys and (not name or issue_key.casefold() in name.casefold()):
                            matched.append(item)
                            seen_keys.add(key)
                    if isinstance(payload, dict) and payload.get('isLast') is True:
                        break
                    total = payload.get('total') if isinstance(payload, dict) else None
                    if type(total) is int and start_at + len(items) >= total:
                        break
                    if not items or (len(items) < 50 and total is None):
                        break
                    start_at += len(items)
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                status, error = 'exception', str(exc)
        self.last_test_cycle_search_stats.append({
            'mode': 'search-name', 'project_key': project_key, 'query': query,
            'status': status, 'seen': total_seen, 'count': len(matched), 'error': error})
        if error or (status != 200 and matched):
            # A partially read list must not become a successful cached lookup.
            return []
        return matched

    def _print_test_cycle_search_debug(self, issue_key: str, project_keys_to_try: list[str]) -> None:
        checked_projects = ", ".join(project_keys_to_try) if project_keys_to_try else "—"
        print(f"   ⚠️ Zephyr: ТЦ по ключу {issue_key} не найдены. Пространства ТЦ: {checked_projects}")
        for stat in self.last_test_cycle_search_stats:
            mode = stat.get('mode', 'unknown')
            if mode == 'issuelink':
                suffix = f", error={stat.get('error')}" if stat.get('error') else ""
                body = f", body={stat.get('body')}" if stat.get('body') else ""
                print(
                    f"      {stat.get('endpoint', 'issuelink')}: "
                    f"status={stat.get('status')}, found={stat.get('count')}{suffix}{body}"
                )
            elif mode == 'issue-query':
                suffix = f", error={stat.get('error')}" if stat.get('error') else ""
                body = f", body={stat.get('body')}" if stat.get('body') else ""
                print(
                    f"      {stat.get('endpoint', 'issue-query')}: "
                    f"status={stat.get('status')}, seen={stat.get('seen', 0)}, "
                    f"matched={stat.get('count')}{suffix}{body}"
                )
            elif mode == 'search-name':
                suffix = f", error={stat.get('error')}" if stat.get('error') else ""
                body = f", body={stat.get('body')}" if stat.get('body') else ""
                print(
                    f"      search-name projectKey={stat.get('project_key')}: "
                    f"status={stat.get('status')}, seen={stat.get('seen', 0)}, "
                    f"matched={stat.get('count')}{suffix}{body}"
                )
            elif mode == 'search':
                suffix = f", error={stat.get('error')}" if stat.get('error') else ""
                print(
                    f"      search projectKey={stat.get('project_key')}: "
                    f"status={stat.get('status')}, pages={stat.get('pages')}, "
                    f"seen={stat.get('total_seen')}, matched={stat.get('count')}{suffix}"
                )

    def get_test_cycle_details(self, tc_key: str) -> Optional[dict]:
        """
        Возвращает полные данные ТЦ, включая customFields.
        Эндпоинт: GET /rest/atm/1.0/testrun/{testRunKey}
        """
        url = f"{self.base_url}/rest/atm/1.0/testrun/{tc_key}"
        try:
            response = self._get_with_retries(url)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception:
            return None

    def get_test_cycle_test_results(self, tc_details: dict) -> list[dict]:
        """
        Возвращает список ТК внутри ТЦ.
        Данные берём из поля 'items' уже загруженных деталей ТЦ —
        отдельный HTTP-запрос не нужен, items приходят в GET /testrun/{key}.
        Каждый элемент содержит: testCaseKey, status (результат выполнения).
        """
        if not tc_details:
            return []
        return tc_details.get('items', [])

    def _extract_zephyr_named_value(self, value: object) -> str:
        """Извлекает человекочитаемое значение из вложенных объектов Zephyr."""
        if value is None:
            return ''
        if isinstance(value, dict):
            for key in ('name', 'value', 'displayName', 'key'):
                nested_value = value.get(key)
                extracted = self._extract_zephyr_named_value(nested_value)
                if extracted:
                    return extracted
            return str(value).strip()
        return str(value).strip()

    def get_test_case_status(self, tc: dict) -> str:
        """
        Возвращает статус ТК из разных форматов ответа Zephyr Scale.

        Короткий issuelink endpoint и детальный testcase endpoint могут отдавать
        статус в разных местах: status, workflowStatus, latestVersion.status и т.д.
        """
        direct_status_fields = ('status', 'workflowStatus', 'testCaseStatus')
        for field_name in direct_status_fields:
            status = self._extract_zephyr_named_value(tc.get(field_name))
            if status:
                return status

        nested_status_paths = (
            ('latestVersion', 'status'),
            ('version', 'status'),
            ('testCase', 'status'),
            ('testcase', 'status'),
        )
        for parent_key, status_key in nested_status_paths:
            parent_value = tc.get(parent_key)
            if not isinstance(parent_value, dict):
                continue
            status = self._extract_zephyr_named_value(parent_value.get(status_key))
            if status:
                return status

        return ''

    @staticmethod
    def get_test_case_key(tc: dict) -> str:
        nested = tc.get('testCase') or tc.get('testcase') or {}
        key = tc.get('key') or tc.get('testCaseKey') or (nested.get('key') if isinstance(nested, dict) else '')
        # Older responses used the textual id as the case key.
        return str(key or (tc.get('id') if isinstance(tc.get('id'), str) else '') or '')

    def get_test_case_name(self, tc: dict) -> str:
        return tc.get('name', tc.get('summary', '—'))


def release_tls_policy():
    """Keep the existing Jira TLS policy unless explicitly configured by the job."""
    ca_bundle = os.getenv('RELEASE_CHECKER_CA_BUNDLE', '').strip()
    if ca_bundle:
        return ca_bundle
    return os.getenv('RELEASE_CHECKER_VERIFY_SSL', '0').lower() in {'1', 'true', 'yes'}


class ReleaseHttpSession(requests.Session):
    """Pass the chosen verify explicitly: REQUESTS_CA_BUNDLE must not override False."""
    def request(self, method, url, **kwargs):
        kwargs.setdefault('verify', self.verify)
        return super().request(method, url, **kwargs)


def request_failure_kind(exc):
    if isinstance(exc, requests.exceptions.SSLError):
        return 'TLS: ошибка сертификата; проверьте RELEASE_CHECKER_CA_BUNDLE/VERIFY_SSL'
    if isinstance(exc, requests.Timeout):
        return 'timeout'
    if isinstance(exc, requests.ConnectionError):
        return 'connection error'
    if isinstance(exc, ValueError):
        return 'invalid JSON/API error'
    return type(exc).__name__


_http_request_ids = count(1)
http_debug_logger = logging.getLogger('release_checker.http')
http_debug_logger.setLevel(logging.INFO)
http_debug_logger.propagate = False


def http_debug_enabled():
    override = os.getenv('RELEASE_CHECKER_DEBUG_HTTP')
    return HTTP_DEBUG_ENABLED if override is None else override.strip().lower() in {'1', 'true', 'yes', 'on'}


def configure_http_debug(path):
    """Optional dedicated JSONL log; never enable third-party wire logging."""
    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    http_debug_logger.addHandler(handler)


def response_shape(payload, depth=0):
    """Bounded schema sample, with no free-text values or unknown field names."""
    if isinstance(payload, list):
        result = {'type': 'list', 'count': len(payload)}
        if payload and depth < 3:
            result['first'] = response_shape(payload[0], depth + 1)
        return result
    if isinstance(payload, dict):
        known = {'summary', 'pullrequest', 'overall', 'count', 'byInstanceType',
                 'detail', 'pullRequests', 'repositories', 'commits', 'errors',
                 'errorMessages', 'results', 'values', 'items', 'testRuns',
                 'testCycles', 'testCases', 'total', 'isLast', 'key', 'id',
                 'name', 'status', 'customFields', 'issueKey', 'issueLinks'}
        result = {'type': 'object', 'fields': len(payload)}
        if depth < 3:
            result['known'] = {key: (value if key in {'total', 'count', 'isLast'}
                                     and type(value) in (int, bool) else response_shape(value, depth + 1))
                               for key, value in payload.items() if key in known}
        return result
    return {'type': type(payload).__name__}


def trace_http(source, url, outcome, started, attempt=1, params=None, **details):
    elapsed = time.monotonic() - started
    debug = http_debug_enabled()
    safe_params = {key: value for key, value in (params or {}).items()
                   if key in {'issueId', 'applicationType', 'dataType', 'startAt', 'maxResults', 'fields'}}
    if debug:
        event = {'time': datetime.now(timezone.utc).isoformat(), 'source': source,
                 'path': urlsplit(url).path, 'params': safe_params, 'attempt': attempt,
                 'result': outcome, 'elapsed_s': round(elapsed, 3), **details}
        line = json.dumps(event, ensure_ascii=False)
        if http_debug_logger.handlers:
            http_debug_logger.info(line)
        else:
            print('[HTTP DEBUG] ' + line, flush=True)
        return
    if source in {'check', 'cycle-lookup'} or outcome in {'request', 'retry', 'cache-hit', 'circuit-open'}:
        return
    if not str(outcome).startswith('2') or elapsed >= 2:
        # No headers, tokens, response bodies or arbitrary query strings in CI logs.
        print(f"[HTTP {source}] {urlsplit(url).path} {safe_params or ''} "
              f"attempt={attempt} result={outcome} elapsed={elapsed:.2f}s", flush=True)


def retry_after_seconds(response):
    """Honor both Retry-After forms; never retry earlier than the server requests."""
    raw = (getattr(response, 'headers', None) or {}).get('Retry-After')
    if raw is None:
        return 0.0
    try:
        seconds = float(raw)
        return max(0.0, seconds) if seconds == seconds else 0.0
    except (ValueError, TypeError):
        try:
            return max(0.0, (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


class RequestNotSent(requests.Timeout):
    """A local deadline/Retry-After prevented sending any HTTP request."""


def bounded_get(session, url, *, source, attempts, timeout, deadline, validate=None, retry_state=None, **kwargs):
    """Retry safe reads only, with one shared deadline for requests and backoff.

    Timeout bounds socket waits; the monotonic deadline prevents further requests
    or sleeps. No retries for TLS/authentication/permanent HTTP failures.
    """
    request_id = next(_http_request_ids)
    sent = 0
    def trace(outcome, started, attempt, **details):
        trace_http(source, url, outcome, started, attempt, kwargs.get('params'),
                   request_id=request_id, remaining_s=round(max(0, deadline - time.monotonic()), 3),
                   **details)

    for attempt in range(1, attempts + 1):
        cooldown = max(0, (retry_state or {}).get('until', 0) - time.monotonic())
        if cooldown:
            if cooldown >= deadline - time.monotonic():
                trace('budget-exhausted', time.monotonic(), attempt, reason='retry-after')
                error = RequestNotSent if sent == 0 else requests.Timeout
                raise error(f'{source}: Retry-After превышает лимит времени')
            trace('retry', time.monotonic(), attempt, sleep_s=cooldown, reason='retry-after')
            time.sleep(cooldown)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            trace('budget-exhausted', time.monotonic(), attempt, reason='deadline')
            error = RequestNotSent if sent == 0 else requests.Timeout
            raise error(f'{source}: исчерпан лимит времени запроса')
        started = time.monotonic()
        response = None
        trace('request', started, attempt, timeout_s=min(timeout, remaining))
        try:
            # A total urllib3 timeout shares time between connect and read.
            sent += 1
            response = session.get(url, timeout=urllib3.util.Timeout(
                total=min(timeout, remaining), connect=min(5.0, timeout, remaining)), **kwargs)
            trace(response.status_code, started, attempt,
                  bytes=len(response.content) if isinstance(getattr(response, 'content', None), bytes) else None,
                  content_type=str((getattr(response, 'headers', None) or {}).get('Content-Type', '')).split(';')[0][:80],
                  retry_after_s=retry_after_seconds(response))
            if response.status_code in {429, 503} and retry_state is not None:
                retry_state['until'] = time.monotonic() + retry_after_seconds(response)
            if response.status_code == 200 and validate is not None:
                payload = response.json()
                if http_debug_enabled():
                    trace('json', started, attempt, shape=response_shape(payload))
                validate(payload)
            if response.status_code not in {408, 429, 500, 502, 503, 504} or attempt == attempts:
                return response
        except requests.exceptions.SSLError:
            trace('TLS', started, attempt)
            raise
        except (requests.RequestException, ValueError) as exc:
            trace(request_failure_kind(exc), started, attempt, exception_type=type(exc).__name__)
            if attempt == attempts:
                raise
        delay = max(PR_RETRY_DELAY_SECONDS * 2 ** (attempt - 1), retry_after_seconds(response))
        if delay >= deadline - time.monotonic():
            trace('budget-exhausted', started, attempt, reason='backoff', sleep_s=delay)
            raise requests.Timeout(f'{source}: повтор не помещается в лимит времени (Retry-After/backoff)')
        trace('retry', started, attempt, sleep_s=delay)
        time.sleep(delay)
    raise requests.Timeout(f'{source}: не выполнен запрос')


def validate_json_payload(payload):
    if not isinstance(payload, (dict, list)):
        raise ValueError('Expected JSON object or list')
    if isinstance(payload, dict) and (payload.get('errors') or payload.get('errorMessages')):
        raise ValueError('API returned application errors')


def wiki_cell(value):
    text = html.escape(normalize_field_text(clean_report_text(value)), quote=False)
    return text.translate(str.maketrans({char: f'&#{ord(char)};' for char in '|\\{}[]*_#'}))


class CachedJiraReads:
    """Reuse identical successful reads within a release; writes always go through."""
    READ_METHODS = {'issue', 'search_issues', 'comments', 'worklogs', 'fields'}

    def __init__(self, client):
        self.client = client
        self.cache = {}

    def clear(self):
        self.cache.clear()

    def __getattr__(self, name):
        method = getattr(self.client, name)
        if name not in self.READ_METHODS:
            return method

        def read(*args, **kwargs):
            key = (name, json.dumps([args, kwargs], sort_keys=True, default=str))
            if key not in self.cache:
                started = time.monotonic()
                self.cache[key] = method(*args, **kwargs)
                if http_debug_enabled():
                    result = self.cache[key]
                    size = len(result) if isinstance(result, (list, dict)) else 1
                    print(f"[JIRA {name}] objects={size} elapsed={time.monotonic()-started:.2f}s", flush=True)
            return self.cache[key]
        return read


class ReleaseValidator:
    def __init__(self):
        self.jira_main = CachedJiraReads(JIRA(
            options={**config['jira']['options'], 'verify': release_tls_policy()},
            token_auth=config['jira']['token'], timeout=(5, 30), max_retries=1
        ))
        self.confluence = Confluence(
            url=config['confluence']['url'],
            token=config['confluence']['token'],
            verify_ssl=config['confluence']['verify_ssl']
        )
        self.confluence_parent_page = config['confluence']['parent_page_id']

        self.zephyr = ZephyrScaleClient(
            base_url=config['jira']['url'],
            token=config['jira']['token'],
            verify_ssl=release_tls_policy()
        )
        self.jira_http = ReleaseHttpSession()
        self.jira_http.verify = release_tls_policy()
        self.jira_http.headers.update({
            'Authorization': f"Bearer {config['jira']['token']}",
            'Content-Type': 'application/json',
        })

        self.report_data = defaultdict(lambda: {
            'summary': '',
            'assignee': 'Не назначен',
            'url': '',
            'errors': [],
            'warnings': [],
            'success': []
        })
        self._jira_field_name_to_id_cache: Optional[dict[str, str]] = None
        self._zephyr_release_cycles_cache: dict[str, list[tuple[str, str]]] = {}
        self._consist_of_cache: dict[str, list[str]] = {}
        self._release_service_infos_cache: dict[str, Optional[dict[str, dict[str, object]]]] = {}
        self._zephyr_issue_test_cases_cache: dict[str, tuple[list[dict], Optional[str]]] = {}
        self._zephyr_cycle_details_cache: dict[str, Optional[dict]] = {}
        self._zephyr_test_case_details_cache: dict[str, Optional[dict]] = {}
        self._zephyr_cycle_approved_checked: set[str] = set()
        self._zephyr_cycle_testing_type_checked: set[tuple[str, str]] = set()
        self._zephyr_cycle_search_debug_logged: set[str] = set()
        self._dev_status_payload_cache: dict[tuple[str, tuple[str, ...]], list[tuple[str, str, dict]]] = {}
        self._pull_request_evidence_cache: dict[str, Optional[str]] = {}
        self._pull_request_only_evidence_cache: dict[str, Optional[str]] = {}
        self._release_pr_targets_cache: dict[str, list[tuple[object, list[object]]]] = {}
        self._merged_pull_request_evidence_cache: dict[str, Optional[str]] = {}
        self._zephyr_cycle_cache = self._load_zephyr_cycle_cache()

        try:
            self.myself = self.jira_main.myself()
            self.my_account_id = self.myself.get('accountId') or self.myself.get('name')
            print(f"🤖 Авторизован как: {self.myself.get('displayName')} ({self.my_account_id})")
        except Exception as e:
            print(f"⚠️ Не удалось получить информацию о пользователе: {e}")
            self.my_account_id = None

    def _log_issue(self, issue_obj_or_key, status, message):
        active = getattr(self, '_active_check', None)
        if active is not None:
            technical_markers = ('не удалось', 'проверка не выполнена', 'неполную историю',
                                 'проверка ТЦ пропущена', 'лимит неуспешных', 'недоступна сводка',
                                 'ошибка получения', 'ошибка поиска', 'ошибка проверки', 'нет токена',
                                 'пустой ответ от модели', 'не вернул вердикт')
            if status != 'success' and any(marker.casefold() in message.casefold() for marker in technical_markers):
                active['incomplete'] = True
                if active['required']:
                    status = 'error'
            active[status if status in {'error', 'warning'} else 'success'] += 1
            active['issues'].add(getattr(issue_obj_or_key, 'key', str(issue_obj_or_key)))
            if status in {'error', 'warning'}:
                active.setdefault('findings', []).append((
                    getattr(issue_obj_or_key, 'key', str(issue_obj_or_key)), status, message))
        issue_key = "GENERAL"
        assignee = "—"
        summary = "Общие проверки"
        url = ""

        if isinstance(issue_obj_or_key, str):
            issue_key = issue_obj_or_key
            if issue_key != "GENERAL":
                url = f"{config['jira']['url']}/browse/{issue_key}"
        elif issue_obj_or_key is not None:
            issue_key = issue_obj_or_key.key
            summary = issue_obj_or_key.fields.summary
            url = f"{config['jira']['url']}/browse/{issue_key}"
            if issue_obj_or_key.fields.assignee:
                assignee = issue_obj_or_key.fields.assignee.displayName

        if self.report_data[issue_key]['summary'] == '' and summary:
            self.report_data[issue_key]['summary'] = summary
        if self.report_data[issue_key]['assignee'] == 'Не назначен' and assignee != '—':
            self.report_data[issue_key]['assignee'] = assignee
        if self.report_data[issue_key]['url'] == '':
            self.report_data[issue_key]['url'] = url

        if status == 'error':
            self.report_data[issue_key]['errors'].append(message)
        elif status == 'warning':
            self.report_data[issue_key]['warnings'].append(message)
        else:
            self.report_data[issue_key]['success'].append(message)

    def _issue_labels_casefold(self, issue_obj) -> set[str]:
        labels = getattr(issue_obj.fields, 'labels', []) or []
        return {str(label).strip().casefold() for label in labels if str(label).strip()}

    def _check_required_platform_label(self, issue_obj, issue_kind: str) -> bool:
        """Проверить наличие одного из обязательных лейблов web/back/mobile без учёта регистра."""
        normalized_labels = self._issue_labels_casefold(issue_obj)
        if normalized_labels & REQUIRED_PLATFORM_LABELS:
            matched = sorted(normalized_labels & REQUIRED_PLATFORM_LABELS)[0]
            self._log_issue(
                issue_obj,
                "success",
                f"{issue_kind}: лейбл контура '{matched}' указан ✓"
            )
            return True

        expected = "/".join(sorted(REQUIRED_PLATFORM_LABELS))
        self._log_issue(
            issue_obj,
            "error",
            f"{issue_kind}: отсутствует обязательный лейбл контура. Должен быть один из: {expected}"
        )
        return False

    def _get_jira_field_id_by_names(self, names: tuple[str, ...]) -> Optional[str]:
        """Найти id Jira-поля по имени через metadata, с кэшем на запуск."""
        if self._jira_field_name_to_id_cache is None:
            try:
                # fields() used to time out once and poison this cache for the run.
                response = bounded_get(self.jira_http,
                    f"{config['jira']['url'].rstrip('/')}/rest/api/2/field",
                    source='Jira fields', attempts=3, timeout=15,
                    deadline=time.monotonic() + 35, validate=validate_json_payload)
                response.raise_for_status()
                fields = response.json()
                if not isinstance(fields, list):
                    raise ValueError('Jira: неизвестный формат metadata полей')
                loaded = {}
                for field in fields:
                    field_name = normalize_field_text(field.get('name', '')).casefold()
                    field_id = field.get('id')
                    if field_name and field_id:
                        loaded[field_name] = field_id
                self._jira_field_name_to_id_cache = loaded
            except Exception as e:
                self._log_issue("GENERAL", "warning", f"Не удалось загрузить metadata полей Jira: {e}")

        if not self._jira_field_name_to_id_cache:
            return None

        wanted = [normalize_field_text(name).casefold() for name in names]
        for name in wanted:
            field_id = self._jira_field_name_to_id_cache.get(name)
            if field_id:
                return field_id

        # Fallback: иногда поле называется "КЭ сервиса (новое)" или похоже.
        for name in sorted((x for x in wanted if len(x) > 2), key=len, reverse=True):
            for field_name, field_id in self._jira_field_name_to_id_cache.items():
                if name in field_name:
                    return field_id
        for name in (x for x in wanted if len(x) <= 2):
            for field_name, field_id in self._jira_field_name_to_id_cache.items():
                if field_name == name or field_name.startswith(f"{name} ") or field_name.startswith(f"{name}("):
                    return field_id
        return None

    def _extract_issue_service_ke_values(self, issue, service_ke_field_id: str) -> list[str]:
        raw_val = getattr(issue.fields, service_ke_field_id, None)
        if raw_val is None and hasattr(issue, 'raw'):
            raw_val = issue.raw.get('fields', {}).get(service_ke_field_id)
        values = extract_jira_field_values(raw_val)
        cleaned = []
        seen = set()
        for value in values:
            value_clean = normalize_field_text(value)
            if not value_clean:
                continue
            key = value_clean.casefold()
            if key not in seen:
                seen.add(key)
                cleaned.append(value_clean)
        return cleaned

    def _story_has_new_functionality(self, issue) -> bool:
        raw_val = getattr(issue.fields, 'customfield_24000', None)
        if raw_val is None and hasattr(issue, 'raw'):
            raw_val = issue.raw.get('fields', {}).get('customfield_24000')
        value = extract_jira_field_value(raw_val)
        return normalize_field_text(value).casefold() == 'да'

    def _get_cycle_name_from_zephyr_item(self, cycle: dict) -> tuple[str, str]:
        cycle_key = self.zephyr.get_test_cycle_key(cycle)
        cycle_name = self.zephyr.get_test_cycle_name(cycle)
        if cycle_key and not cycle_name:
            details = self._get_test_cycle_details_cached(cycle_key)
            if details:
                cycle_name = details.get('name', '')
        return cycle_key, cycle_name

    def _get_release_test_cycles(self, release_key: str) -> list[tuple[str, str]]:
        """Один кэшированный Zephyr-поиск ТЦ по ключу релиза."""
        started = time.monotonic()
        with self.zephyr.lookup_budget(ZEPHYR_LOOKUP_TIMEOUT_SECONDS):
            cycles = self._get_release_test_cycles_bounded(release_key)
        trace_http('cycle-lookup', '', 'finished', started, issue=release_key, matches=len(cycles),
                   partial=any(stat.get('complete') is False for stat in self.zephyr.last_test_cycle_search_stats))
        return cycles

    def _get_release_test_cycles_bounded(self, release_key: str) -> list[tuple[str, str]]:
        if release_key not in self._zephyr_release_cycles_cache:
            with self.zephyr.lookup_budget(ZEPHYR_CYCLE_DISCOVERY_SECONDS):
                raw_cycles = self.zephyr.get_test_cycles_for_issue(release_key)
            if not raw_cycles and not self.zephyr.is_failed_request_limit_reached():
                with self.zephyr.lookup_budget(ZEPHYR_CYCLE_METADATA_SECONDS):
                    raw_cycles = self._get_test_cycles_from_jira_release_metadata(release_key)
            if not raw_cycles and not self.zephyr.is_failed_request_limit_reached():
                raw_cycles = self._get_test_cycles_from_direct_cache_or_scan(release_key)
            cycles = [self._get_cycle_name_from_zephyr_item(cycle) for cycle in raw_cycles]
            resolved = [(key, name) for key, name in cycles if key or name]
            incomplete = any(stat.get('complete') is False
                             for stat in self.zephyr.last_test_cycle_search_stats)
            if resolved and incomplete:
                self._log_issue(release_key, 'warning',
                                'Zephyr: не удалось проверить полный список ТЦ релиза')
                return resolved
            if not resolved:
                self._log_test_cycle_search_debug(release_key)
                if self._zephyr_last_cycle_search_had_technical_failure():
                    return []
            self._zephyr_release_cycles_cache[release_key] = resolved
        return self._zephyr_release_cycles_cache[release_key]

    def _get_test_cycles_from_jira_release_metadata(self, release_key: str) -> list[dict]:
        """Попробовать найти ключи ТЦ в Jira metadata релиза и открыть ТЦ напрямую."""
        cycle_keys = self._collect_test_cycle_keys_from_jira_metadata(release_key)
        if not cycle_keys:
            return []

        result = []
        for cycle_key in sorted(cycle_keys):
            details = self._get_test_cycle_details_cached(cycle_key)
            if not details:
                continue
            if self._test_cycle_belongs_to_release(details, release_key):
                result.append(details)

        if result:
            self._log_issue(
                release_key,
                "success",
                f"Zephyr: ТЦ найдены через Jira metadata релиза: "
                + ", ".join(sorted(self.zephyr.get_test_cycle_key(cycle) for cycle in result))
            )
        return result

    def _get_test_cycles_from_direct_cache_or_scan(self, release_key: str) -> list[dict]:
        with self.zephyr.lookup_budget(30):
            return self._get_test_cycles_from_direct_cache_or_scan_bounded(release_key)

    def _get_test_cycles_from_direct_cache_or_scan_bounded(self, release_key: str) -> list[dict]:
        """Найти ТЦ через локальный кэш или bounded direct scan по ключам HRPQA-C..."""
        cached_cycles = self._get_cached_test_cycles_for_release(release_key)
        if self.zephyr.requests_paused():
            self.zephyr.last_test_cycle_search_stats.append({
                'mode': 'direct-scan', 'status': 'exception', 'complete': False,
                'error': 'Retry-After: direct scan deferred'})
            return cached_cycles
        if not ZEPHYR_DIRECT_CYCLE_SCAN_ENABLED:
            if cached_cycles:
                self._log_issue(
                    release_key,
                    "success",
                    "Zephyr: ТЦ найдены в локальном кэше direct-lookup: "
                    + ", ".join(sorted(self.zephyr.get_test_cycle_key(cycle) for cycle in cached_cycles))
                )
            return cached_cycles

        project_key = self._primary_test_cycle_project_key()
        if not project_key:
            return cached_cycles

        high_watermark = self._get_direct_scan_high_watermark(project_key)
        if not high_watermark:
            if cached_cycles:
                self._log_issue(
                    release_key,
                    "success",
                    "Zephyr: ТЦ найдены в локальном кэше direct-lookup: "
                    + ", ".join(sorted(self.zephyr.get_test_cycle_key(cycle) for cycle in cached_cycles))
                )
                self._save_zephyr_cycle_cache()
                return cached_cycles
            self._log_issue(
                release_key,
                "warning",
                "Zephyr direct scan: нет стартового C-номера. "
                "Задай ZEPHYR_DIRECT_CYCLE_SCAN_START, например номер из HRPQA-C133028."
            )
            return []

        # Search the known window before advancing it. Previously forward lookup
        # consumed the entire budget, so even the configured start was never read.
        min_number = max(1, high_watermark - ZEPHYR_DIRECT_CYCLE_SCAN_LIMIT + 1)
        found = []

        with self.zephyr.lookup_budget(20):
            for cycle_number in range(high_watermark, min_number - 1, -1):
                if (self.zephyr.is_failed_request_limit_reached() or self.zephyr.lookup_budget_exhausted()
                        or self.zephyr.requests_paused()):
                    break
                cycle_key = f"{project_key}-C{cycle_number}"
                details = self._get_cached_or_fetch_cycle_details(project_key, cycle_key)
                if details and self._test_cycle_belongs_to_release(details, release_key):
                    found.append(details)
        high_watermark = self._refresh_direct_scan_high_watermark(project_key, high_watermark)
        # Forward reads already populate release_map. Keep those matches even
        # when the final read exhausts the remaining budget.
        found.extend(self._get_cached_test_cycles_for_release(release_key))
        self.zephyr.last_test_cycle_search_stats.append({
            'mode': 'direct-scan', 'status': 'exception', 'complete': False,
            'error': 'bounded numeric window does not prove a complete release cycle list'})

        combined_by_key = {}
        for cycle in cached_cycles + found:
            cycle_key = self.zephyr.get_test_cycle_key(cycle)
            if cycle_key:
                combined_by_key[cycle_key] = cycle
        combined = list(combined_by_key.values())

        if combined:
            self._cache_release_cycles(release_key, combined)
            self._save_zephyr_cycle_cache()
            source_note = "кэш + обновление" if cached_cycles and found else ("кэш" if cached_cycles else "обновление")
            self._log_issue(
                release_key,
                "success",
                f"Zephyr direct scan: ТЦ найдены ({source_note}), всего={len(combined)}, "
                f"окно {project_key}-C{min_number}..{project_key}-C{high_watermark}: "
                + ", ".join(sorted(combined_by_key.keys()))
            )
        else:
            self._save_zephyr_cycle_cache()
            self._log_issue(
                release_key,
                "warning",
                f"Zephyr direct scan: в окне {project_key}-C{min_number}..{project_key}-C{high_watermark} "
                "ТЦ релиза не найдены"
            )
        return combined

    def _get_cached_test_cycles_for_release(self, release_key: str) -> list[dict]:
        cycle_keys = self._zephyr_cycle_cache.get('release_map', {}).get(release_key, [])
        result = []
        for cycle_key in cycle_keys:
            if not self._is_allowed_test_cycle_key(cycle_key):
                continue
            details = self._get_cached_or_fetch_cycle_details(cycle_key.split('-C', 1)[0], cycle_key)
            if details and self._test_cycle_belongs_to_release(details, release_key):
                result.append(details)
        return result

    @staticmethod
    def _primary_test_cycle_project_key() -> str:
        return ZEPHYR_TEST_CYCLE_PROJECT_KEYS[0] if ZEPHYR_TEST_CYCLE_PROJECT_KEYS else ZEPHYR_TEST_CASE_PROJECT_KEY

    @staticmethod
    def _is_allowed_test_cycle_key(cycle_key: str) -> bool:
        allowed_prefixes = {f"{project_key}-c".casefold() for project_key in ZEPHYR_TEST_CYCLE_PROJECT_KEYS}
        return not allowed_prefixes or any(str(cycle_key).casefold().startswith(prefix) for prefix in allowed_prefixes)

    def _get_direct_scan_high_watermark(self, project_key: str) -> int:
        project_cache = self._zephyr_cycle_cache.get('projects', {}).get(project_key, {})
        cached_max = project_cache.get('max_seen')
        return max(int(cached_max) if str(cached_max).isdigit() else 0,
                   int(ZEPHYR_DIRECT_CYCLE_SCAN_START) if ZEPHYR_DIRECT_CYCLE_SCAN_START.isdigit() else 0)

    def _refresh_direct_scan_high_watermark(self, project_key: str, high_watermark: int) -> int:
        current = high_watermark
        misses = 0
        for cycle_number in range(high_watermark + 1, high_watermark + ZEPHYR_DIRECT_CYCLE_SCAN_FORWARD_LIMIT + 1):
            if (self.zephyr.is_failed_request_limit_reached() or self.zephyr.lookup_budget_exhausted()
                    or self.zephyr.requests_paused()):
                break
            cycle_key = f"{project_key}-C{cycle_number}"
            details = self.zephyr.get_test_cycle_details(cycle_key)
            if details:
                self._cache_cycle_details(project_key, details)
                current = cycle_number
                misses = 0
                continue
            misses += 1
            if misses >= ZEPHYR_DIRECT_CYCLE_SCAN_FORWARD_MISS_LIMIT:
                break
        return current

    def _get_cached_or_fetch_cycle_details(self, project_key: str, cycle_key: str) -> Optional[dict]:
        project_cache = self._zephyr_cycle_cache.setdefault('projects', {}).setdefault(project_key, {})
        cycles_cache = project_cache.setdefault('cycles', {})
        cached = cycles_cache.get(cycle_key)
        if isinstance(cached, dict):
            return cached

        details = self.zephyr.get_test_cycle_details(cycle_key)
        if details:
            self._cache_cycle_details(project_key, details)
        return details

    def _cache_release_cycles(self, release_key: str, cycles: list[dict]) -> None:
        release_map = self._zephyr_cycle_cache.setdefault('release_map', {})
        cycle_keys = []
        for cycle in cycles:
            cycle_key = self.zephyr.get_test_cycle_key(cycle)
            project_key = str(cycle.get('projectKey') or cycle_key.split('-C', 1)[0])
            if not cycle_key:
                continue
            if not self._is_allowed_test_cycle_key(cycle_key):
                continue
            cycle_keys.append(cycle_key)
            self._cache_cycle_details(project_key, cycle)
        release_map[release_key] = sorted(set(cycle_keys))

    def _cache_cycle_details(self, project_key: str, cycle_details: dict) -> None:
        cycle_key = self.zephyr.get_test_cycle_key(cycle_details)
        if not cycle_key:
            return
        project_cache = self._zephyr_cycle_cache.setdefault('projects', {}).setdefault(project_key, {})
        cycles_cache = project_cache.setdefault('cycles', {})
        cycles_cache[cycle_key] = cycle_details
        match = re.search(r'-C(\d+)$', cycle_key)
        if match:
            project_cache['max_seen'] = max(int(project_cache.get('max_seen') or 0), int(match.group(1)))
        issue_keys = re.findall(r'\b[A-Z][A-Z0-9_]*-\d+\b', json.dumps({
            'issueKey': cycle_details.get('issueKey'),
            'issueLinks': cycle_details.get('issueLinks'),
            'name': self.zephyr.get_test_cycle_name(cycle_details)}, ensure_ascii=False))
        for issue_key in set(issue_keys):
            release_map = self._zephyr_cycle_cache.setdefault('release_map', {})
            current = set(release_map.get(issue_key, []))
            current.add(cycle_key)
            release_map[issue_key] = sorted(current)

    def _load_zephyr_cycle_cache(self) -> dict:
        try:
            if ZEPHYR_CYCLE_CACHE_PATH.exists():
                data = json.loads(ZEPHYR_CYCLE_CACHE_PATH.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    data.setdefault('projects', {})
                    data.setdefault('release_map', {})
                    return data
        except Exception:
            pass
        return {'projects': {}, 'release_map': {}}

    def _save_zephyr_cycle_cache(self) -> None:
        try:
            ZEPHYR_CYCLE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            ZEPHYR_CYCLE_CACHE_PATH.write_text(
                json.dumps(self._zephyr_cycle_cache, ensure_ascii=False, indent=2, sort_keys=True),
                encoding='utf-8',
            )
        except Exception as e:
            # Cache persistence is an optimization, never a release defect.
            print(f"   Zephyr cache: не удалось сохранить {ZEPHYR_CYCLE_CACHE_PATH}: {e}")

    def _collect_test_cycle_keys_from_jira_metadata(self, release_key: str) -> set[str]:
        cycle_keys: set[str] = set()
        for keys in self._collect_test_cycle_keys_from_jira_metadata_sources(release_key).values():
            cycle_keys.update(keys)

        allowed_prefixes = {f"{project_key}-c".casefold() for project_key in ZEPHYR_TEST_CYCLE_PROJECT_KEYS}
        return {
            cycle_key for cycle_key in cycle_keys
            if not allowed_prefixes or any(cycle_key.casefold().startswith(prefix) for prefix in allowed_prefixes)
        }

    def _collect_test_cycle_keys_from_jira_metadata_sources(self, release_key: str) -> dict[str, set[str]]:
        return {
            'remote_links': self._collect_test_cycle_keys_from_remote_links(release_key),
            'properties': self._collect_test_cycle_keys_from_issue_properties(release_key),
            'issue_raw': self._collect_test_cycle_keys_from_issue_raw(release_key),
            'issue_html': self._collect_test_cycle_keys_from_issue_html(release_key),
        }

    def _get_cycle_metadata(self, url, **kwargs):
        """Jira fallbacks share the cycle discovery deadline, including properties."""
        deadline = getattr(self.zephyr, '_lookup_deadline', None) or time.monotonic() + 12
        validate = None if '/browse/' in urlsplit(url).path else validate_json_payload
        return bounded_get(self.jira_http, url, source='Jira cycle metadata',
                           attempts=1, timeout=12, deadline=deadline, validate=validate, **kwargs)

    def _collect_test_cycle_keys_from_remote_links(self, release_key: str) -> set[str]:
        url = f"{config['jira']['url'].rstrip('/')}/rest/api/2/issue/{release_key}/remotelink"
        try:
            response = self._get_cycle_metadata(url)
            if response.status_code != 200:
                return set()
            return self._extract_test_cycle_keys(response.json())
        except Exception:
            return set()

    def _collect_test_cycle_keys_from_issue_properties(self, release_key: str) -> set[str]:
        base_url = config['jira']['url'].rstrip('/')
        url = f"{base_url}/rest/api/2/issue/{release_key}/properties"
        try:
            response = self._get_cycle_metadata(url)
            if response.status_code != 200:
                return set()
            data = response.json()
        except Exception:
            return set()

        cycle_keys = self._extract_test_cycle_keys(data)
        property_keys = []
        for item in data.get('keys', []) if isinstance(data, dict) else []:
            property_key = item.get('key', '') if isinstance(item, dict) else ''
            if property_key and self._is_relevant_jira_property_key(property_key):
                property_keys.append(property_key)

        for property_key in property_keys[:30]:
            if self.zephyr.lookup_budget_exhausted():
                break
            property_url = f"{url}/{property_key}"
            try:
                property_response = self._get_cycle_metadata(property_url)
                if property_response.status_code == 200:
                    cycle_keys.update(self._extract_test_cycle_keys(property_response.json()))
            except Exception:
                continue
        return cycle_keys

    def _collect_test_cycle_keys_from_issue_raw(self, release_key: str) -> set[str]:
        try:
            url = f"{config['jira']['url'].rstrip('/')}/rest/api/2/issue/{release_key}"
            response = self._get_cycle_metadata(url, params={
                'fields': '*all', 'expand': 'renderedFields,names,schema'})
            return self._extract_test_cycle_keys(response.json()) if response.status_code == 200 else set()
        except Exception:
            return set()

    def _collect_test_cycle_keys_from_issue_html(self, release_key: str) -> set[str]:
        url = f"{config['jira']['url'].rstrip('/')}/browse/{release_key}"
        try:
            response = self._get_cycle_metadata(
                url,
                headers={'Accept': 'text/html,application/xhtml+xml'},
            )
            if response.status_code != 200:
                return set()
            return self._extract_test_cycle_keys(response.text)
        except Exception:
            return set()

    @staticmethod
    def _is_relevant_jira_property_key(property_key: str) -> bool:
        property_key_lower = property_key.casefold()
        return any(
            marker in property_key_lower
            for marker in (
                'zephyr', 'tm4j', 'atm', 'test', 'cycle', 'run',
                'dev', 'development', 'stash', 'bitbucket', 'pull', 'vcs',
            )
        )

    @staticmethod
    def _extract_test_cycle_keys(payload: object) -> set[str]:
        try:
            text = json.dumps(payload, ensure_ascii=False)
        except Exception:
            text = str(payload)
        return set(TEST_CYCLE_KEY_RE.findall(text))

    @staticmethod
    def _test_cycle_belongs_to_release(cycle_details: dict, release_key: str) -> bool:
        return ZephyrScaleClient._cycle_mentions_issue(cycle_details, release_key)

    def _log_test_cycle_search_debug(self, release_key: str) -> None:
        if release_key in self._zephyr_cycle_search_debug_logged:
            return
        self._zephyr_cycle_search_debug_logged.add(release_key)
        stats = self._format_test_cycle_search_stats()
        if stats:
            self._log_issue(
                release_key,
                "warning",
                f"Zephyr debug: ТЦ по ключу релиза не найдены. {stats}"
            )

    def _format_test_cycle_search_stats(self) -> str:
        parts = []
        for stat in self.zephyr.last_test_cycle_search_stats:
            mode = stat.get('mode')
            if mode == 'issuelink':
                parts.append(
                    f"{stat.get('endpoint', 'issuelink')} "
                    f"status={stat.get('status')} found={stat.get('count')}"
                )
            elif mode == 'issue-query':
                parts.append(
                    f"{stat.get('endpoint', 'issue-query')} "
                    f"status={stat.get('status')} seen={stat.get('seen', 0)} matched={stat.get('count')}"
                )
            elif mode == 'search-name':
                parts.append(
                    f"search-name projectKey={stat.get('project_key')} "
                    f"status={stat.get('status')} seen={stat.get('seen', 0)} matched={stat.get('count')}"
                )
            elif mode == 'search':
                parts.append(
                    f"search projectKey={stat.get('project_key')} status={stat.get('status')} "
                    f"pages={stat.get('pages')} seen={stat.get('total_seen')} matched={stat.get('count')}"
                    + (f" error={stat.get('error')}" if stat.get('error') else "")
                )
        return "; ".join(parts)

    def _zephyr_last_cycle_search_had_technical_failure(self) -> bool:
        technical_statuses = {401, 403, 429, 500, 502, 503, 504}
        for stat in self.zephyr.last_test_cycle_search_stats:
            status = stat.get('status')
            if status == 'exception':
                return True
            if isinstance(status, int) and status in technical_statuses:
                return True
            if isinstance(status, str) and status.isdigit() and int(status) in technical_statuses:
                return True
        return self.zephyr.is_failed_request_limit_reached()

    def _log_missing_release_cycles(self, release_key: str, check_label: str) -> None:
        if self._zephyr_last_cycle_search_had_technical_failure():
            stats = self._format_test_cycle_search_stats()
            self._log_issue(
                release_key,
                "warning",
                f"{check_label}: Zephyr временно не отдал ТЦ по ключу релиза {release_key}; "
                "проверка ТЦ пропущена, отсутствие ТЦ не зафиксировано как ошибка"
                + (f". Диагностика: {stats}" if stats else "")
            )
            return

        self._log_issue(
            release_key,
            "error",
            f"{check_label}: Zephyr не вернул ТЦ по ключу релиза {release_key}"
        )
        self._log_test_cycle_search_debug(release_key)

    def _get_test_cycle_details_cached(self, cycle_key: str) -> Optional[dict]:
        if cycle_key not in self._zephyr_cycle_details_cache:
            if self.zephyr.requests_paused():
                return None
            details = self.zephyr.get_test_cycle_details(cycle_key)
            if details:
                self._zephyr_cycle_details_cache[cycle_key] = details
        return self._zephyr_cycle_details_cache.get(cycle_key)

    def _get_test_case_details_cached(self, tc_key: str) -> Optional[dict]:
        if tc_key not in self._zephyr_test_case_details_cache:
            if self.zephyr.requests_paused():
                return None
            details = self.zephyr.get_test_case_details(tc_key)
            if details:
                self._zephyr_test_case_details_cache[tc_key] = details
        return self._zephyr_test_case_details_cache.get(tc_key)

    def _get_issue_test_cases_cached(self, issue_key: str) -> tuple[list[dict], Optional[str]]:
        if issue_key not in self._zephyr_issue_test_cases_cache:
            cases, error = self.zephyr.get_test_cases_for_issue(issue_key)
            if error is not None:
                return cases, error
            self._zephyr_issue_test_cases_cache[issue_key] = (cases, None)
            # The fields projection returns everything needed; detail GET is a
            # fallback only when a server omits status/customFields/issueLinks.
            for case in cases:
                if self.zephyr.get_test_case_status(case) and 'customFields' in case and 'issueLinks' in case:
                    self._zephyr_test_case_details_cache[self.zephyr.get_test_case_key(case)] = case
        return self._zephyr_issue_test_cases_cache[issue_key]

    @staticmethod
    def _extract_test_case_key_from_cycle_result(test_result: dict) -> str:
        direct_key = test_result.get('testCaseKey') or test_result.get('key') or ''
        if direct_key:
            return str(direct_key)
        test_case = test_result.get('testCase') or test_result.get('testcase')
        if isinstance(test_case, dict):
            return str(test_case.get('key') or test_case.get('testCaseKey') or '')
        if isinstance(test_case, str):
            return test_case
        return ''

    @staticmethod
    def _payload_mentions_issue(payload: object, issue_key: str) -> bool:
        if not payload:
            return False
        try:
            payload_text = json.dumps(payload, ensure_ascii=False).casefold()
        except Exception:
            payload_text = str(payload).casefold()
        return bool(re.search(r'(?<![a-z0-9-])' + re.escape(issue_key.casefold()) + r'(?![a-z0-9-])', payload_text))

    @staticmethod
    def _payload_mentions_issue_in_direct_link_fields(payload: dict, issue_key: str) -> bool:
        if not isinstance(payload, dict):
            return False

        direct_fields = (
            'issueKey',
            'jiraIssueKey',
            'issueKeys',
            'jiraIssueKeys',
            'issues',
            'jiraIssues',
            'issueLinks',
            'jiraIssueLinks',
            'linkedIssues',
            'links',
            'requirements',
            'requirementKeys',
            'testCaseIssueLinks',
        )
        for field_name in direct_fields:
            if field_name in payload and ReleaseValidator._payload_mentions_issue(payload.get(field_name), issue_key):
                return True
        return False

    def _is_directly_linked_test_case_for_issue(
        self,
        issue_key: str,
        test_case_link: dict,
        test_case_details: Optional[dict],
    ) -> bool:
        """
        Zephyr issue-link endpoint может вернуть ТК из контекста ТЦ/результата
        выполнения. Для проверки задачи нам нужны только ТК, прямо связанные
        с этой Story/Bug/Defect, а не ТК из релизного ТЦ.
        """
        return (
            self._payload_mentions_issue_in_direct_link_fields(test_case_link, issue_key)
            or self._payload_mentions_issue_in_direct_link_fields(test_case_details or {}, issue_key)
        )

    def _get_issue_type_map(self, issue_keys: list[str]) -> dict[str, str]:
        if not issue_keys:
            return {}
        keys_str = ",".join(issue_keys)
        try:
            issues = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields='issuetype',
                maxResults=False
            )
        except Exception as exc:
            raise RuntimeError("не удалось получить типы задач") from exc
        return {
            issue.key: issue.fields.issuetype.name.casefold()
            for issue in issues
            if getattr(issue.fields, 'issuetype', None)
        }

    def _check_cycle_case_batches(self, release_key, cycles):
        # Metadata for every required cycle is checked before any bulk case reads.
        # Small cycles first: a large regression must not starve all other cycles.
        cache = getattr(self, '_zephyr_cycle_details_cache', {})
        ordered = sorted(dict.fromkeys(cycles), key=lambda item: len(
            self.zephyr.get_test_cycle_test_results(cache.get(item[0], {}))))
        for cycle_key, cycle_name in ordered:
            self._check_test_cycle_cases_approved(release_key, cycle_key, cycle_name)

    def _check_test_cycle_cases_approved(self, release_key: str, cycle_key: str, cycle_name: str) -> None:
        """Проверить, что все ТК внутри ТЦ находятся в статусе Approved."""
        check_key = cycle_key or normalize_test_cycle_name(cycle_name)
        if check_key in self._zephyr_cycle_approved_checked:
            return
        self._zephyr_cycle_approved_checked.add(check_key)

        if not cycle_key:
            self._log_issue(
                release_key,
                "warning",
                f"ТЦ '{cycle_name}': нет key, невозможно проверить статусы ТК внутри ТЦ"
            )
            return

        details = self._get_test_cycle_details_cached(cycle_key)
        if not details:
            self._log_issue(
                release_key,
                "error",
                f"ТЦ [{cycle_key}] '{cycle_name}': не удалось получить детали ТЦ для проверки статусов ТК"
            )
            return

        test_results = self.zephyr.get_test_cycle_test_results(details)
        if not test_results:
            self._log_issue(
                release_key,
                "error",
                f"ТЦ [{cycle_key}] '{cycle_name}': внутри ТЦ не найдены ТК"
            )
            return

        not_approved = []
        unresolved = []
        seen_tc_keys = set()
        for test_result in test_results:
            tc_key = self._extract_test_case_key_from_cycle_result(test_result)
            if not tc_key:
                unresolved.append('ТК без ключа')
                continue
            if tc_key in seen_tc_keys:
                continue
            seen_tc_keys.add(tc_key)

            tc_details = self._get_test_case_details_cached(tc_key)
            if not tc_details:
                unresolved.append(tc_key)
                continue

            tc_status = self.zephyr.get_test_case_status(tc_details)
            if not tc_status:
                unresolved.append(tc_key)
            elif tc_status.casefold() != ZEPHYR_APPROVED_STATUS.casefold():
                tc_name = self.zephyr.get_test_case_name(tc_details)
                not_approved.append((tc_key, tc_name, tc_status or '—'))

        for tc_key, tc_name, tc_status in not_approved:
            self._log_issue(
                release_key,
                "error",
                f"ТЦ [{cycle_key}] '{cycle_name}': ТК [{tc_key}] '{tc_name}' "
                f"не в статусе '{ZEPHYR_APPROVED_STATUS}' (текущий: '{tc_status}')"
            )

        if unresolved:
            unresolved = sorted(set(unresolved))
            print(f"[CYCLE unresolved] {cycle_key}: {', '.join(unresolved)}", flush=True)
            preview = ', '.join(unresolved[:10])
            remainder = len(unresolved) - 10
            if remainder > 0:
                preview += f"; ещё {remainder} — полный список в логе Jenkins"
            self._log_issue(
                release_key,
                "warning",
                f"ТЦ [{cycle_key}] '{cycle_name}': не удалось получить детали ТК ({len(unresolved)}): "
                + preview
            )

        if not seen_tc_keys:
            self._log_issue(release_key, 'error',
                            f"ТЦ [{cycle_key}] '{cycle_name}': не удалось определить ключи ТК")
        elif not not_approved and not unresolved:
            self._log_issue(
                release_key,
                "success",
                f"ТЦ [{cycle_key}] '{cycle_name}': все ТК внутри ТЦ в статусе '{ZEPHYR_APPROVED_STATUS}' ✓"
            )

    def _check_test_cycle_testing_type(
        self,
        release_key: str,
        cycle_key: str,
        cycle_name: str,
        expected_testing_type: str,
    ) -> None:
        """Проверить поле 'Вид тестирования' у ТЦ."""
        check_key = (cycle_key or normalize_test_cycle_name(cycle_name), expected_testing_type.casefold())
        if check_key in self._zephyr_cycle_testing_type_checked:
            return
        self._zephyr_cycle_testing_type_checked.add(check_key)

        if not cycle_key:
            self._log_issue(
                release_key,
                "warning",
                f"ТЦ '{cycle_name}': нет key, невозможно проверить поле '{ZEPHYR_TESTING_TYPE_FIELD}'"
            )
            return

        details = self._get_test_cycle_details_cached(cycle_key)
        if not details:
            self._log_issue(
                release_key,
                "error",
                f"ТЦ [{cycle_key}] '{cycle_name}': не удалось получить детали ТЦ "
                f"для проверки поля '{ZEPHYR_TESTING_TYPE_FIELD}'"
            )
            return

        actual_testing_type = self.zephyr.get_test_case_custom_field(details, ZEPHYR_TESTING_TYPE_FIELD)
        if actual_testing_type is None:
            self._log_issue(
                release_key,
                "error",
                f"ТЦ [{cycle_key}] '{cycle_name}': поле '{ZEPHYR_TESTING_TYPE_FIELD}' не найдено, "
                f"ожидается '{expected_testing_type}'"
            )
            return

        if normalize_field_text(actual_testing_type).casefold() != expected_testing_type.casefold():
            self._log_issue(
                release_key,
                "error",
                f"ТЦ [{cycle_key}] '{cycle_name}': '{ZEPHYR_TESTING_TYPE_FIELD}' = "
                f"'{actual_testing_type}', ожидается '{expected_testing_type}'"
            )
            return

        self._log_issue(
            release_key,
            "success",
            f"ТЦ [{cycle_key}] '{cycle_name}': {ZEPHYR_TESTING_TYPE_FIELD} = '{actual_testing_type}' ✓"
        )

    def _find_matching_cycle(
        self,
        cycles: list[tuple[str, str]],
        release_key: str,
        service_ke: str,
        channel_aliases: tuple[str, ...],
        cycle_type_aliases: tuple[str, ...],
    ) -> Optional[tuple[str, str]]:
        for service_alias in service_ke_name_aliases(service_ke):
            for cycle_key, cycle_name in cycles:
                if test_cycle_name_matches_mask(
                    cycle_name,
                    release_key,
                    service_alias,
                    channel_aliases,
                    cycle_type_aliases,
                ):
                    return cycle_key, cycle_name
        return None

    def _collect_release_service_infos(
        self,
        release_key: str,
        check_label: str,
    ) -> Optional[dict[str, dict[str, object]]]:
        """Собрать КЭ из Story/Bug состава релиза и отметить, где есть НФ Story."""
        if release_key in self._release_service_infos_cache:
            return self._release_service_infos_cache[release_key]

        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            self._log_issue(
                release_key,
                "warning",
                f"{check_label}: нет задач со связью 'consist of' — проверка ТЦ по КЭ пропущена"
            )
            self._release_service_infos_cache[release_key] = None
            return None

        service_ke_field_id = self._get_jira_field_id_by_names(SERVICE_KE_FIELD_NAME_CANDIDATES)
        if not service_ke_field_id:
            self._log_issue(
                release_key,
                "error",
                f"{check_label}: не удалось определить Jira field id для поля КЭ/КЭ сервиса"
            )
            return None

        keys_str = ",".join(linked_keys)
        issue_types_to_check = {'story', 'bug', 'defect', 'ошибка'}
        try:
            release_items = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields=f'summary,issuetype,assignee,{service_ke_field_id},customfield_24000',
                maxResults=False
            )
        except Exception as e:
            self._log_issue(
                release_key,
                "error",
                f"{check_label}: ошибка получения задач состава релиза для проверки КЭ: {e}"
            )
            return None

        services: dict[str, dict[str, object]] = {}
        for issue in release_items:
            issue_type = issue.fields.issuetype.name.casefold() if issue.fields.issuetype else ''
            if issue_type not in issue_types_to_check:
                continue

            service_values = self._extract_issue_service_ke_values(issue, service_ke_field_id)
            if not service_values:
                self._log_issue(
                    issue,
                    "error",
                    f"{check_label}: не заполнено поле КЭ сервиса ({service_ke_field_id}); "
                    "невозможно проверить обязательные ТЦ"
                )
                continue

            issue_project_key = issue.key.split('-', 1)[0].casefold()
            for service_ke in service_values:
                service_key = normalize_field_text(service_ke).casefold()
                service_info = services.setdefault(service_key, {
                    'display': service_ke,
                    'issue_keys': set(),
                    'project_keys': set(),
                    'has_nf_story': False,
                })
                service_info['issue_keys'].add(issue.key)
                service_info['project_keys'].add(issue_project_key)
                if issue_type == 'story' and self._story_has_new_functionality(issue):
                    service_info['has_nf_story'] = True

        if not services:
            self._log_issue(
                release_key,
                "error",
                f"{check_label}: в составе релиза не найдено ни одной Story/Bug с заполненной КЭ сервиса"
            )
            self._release_service_infos_cache[release_key] = None
            return None

        self._release_service_infos_cache[release_key] = services
        return services

    def _run_check(self, check_id, name, callback, *, required=True, skip_reason='', budget=None):
        record = {'id': check_id, 'name': name, 'required': required, 'status': 'RUNNING',
                  'seconds': 0.0, 'success': 0, 'error': 0, 'warning': 0,
                  'issues': set(), 'incomplete': False, 'reason': skip_reason, 'findings': []}
        self.check_results.append(record)
        if skip_reason:
            record['status'] = 'SKIP'
            print(f"[CHECK {check_id}] SKIP {name}: {skip_reason}", flush=True)
            return
        started = time.monotonic()
        self._active_check = record
        print(f"[CHECK {check_id}] START {name}", flush=True)
        try:
            if budget is None:
                callback()
            else:
                self._begin_pr_stage()
                with self._pr_lookup_budget(budget):
                    callback()
        except Exception as exc:
            record['incomplete'] = True
            self._log_issue('GENERAL', 'error' if required else 'warning',
                            f"{name}: проверка не выполнена ({type(exc).__name__}: {exc})")
        finally:
            record['seconds'] = time.monotonic() - started
            if record['incomplete']:
                record['status'] = 'INCOMPLETE'
            elif record['error']:
                record['status'] = 'FAIL'
            elif record['warning']:
                record['status'] = 'WARN'
            else:
                record['status'] = 'PASS' if record['success'] else 'NO_FINDINGS'
            self._active_check = None
            print(f"[CHECK {check_id}] {record['status']} {record['seconds']:.2f}s "
                  f"issues={len(record['issues'])} ok={record['success']} "
                  f"errors={record['error']} warnings={record['warning']}", flush=True)
            trace_http('check', '', record['status'], started, check_id=check_id,
                       issues=len(record['issues']), errors=record['error'], warnings=record['warning'])

    def _reset_release_run(self):
        self.report_data.clear()
        self.check_results = []
        self._active_check = None
        self._reset_pr_run_state()
        if isinstance(self.jira_main, CachedJiraReads):
            self.jira_main.clear()
        for name, value in vars(self).items():
            if name.startswith('_zephyr_') and name != '_zephyr_cycle_cache' and isinstance(value, (dict, set)):
                value.clear()
        self._jira_field_name_to_id_cache = None
        self._consist_of_cache.clear()
        self._release_service_infos_cache.clear()
        self.zephyr.failed_requests = 0
        self.zephyr.fatal_error = None
        self.zephyr.last_test_cycle_search_stats = []

    def _checks_wiki_table(self):
        rows = [CHECK_TABLE_HEADER]
        reported = set()
        for result in getattr(self, 'check_results', []):
            name = wiki_cell(result['name'])
            status = result['status'] + (': ' + result['reason'] if result['reason'] else '')
            findings = result.get('findings', [])
            reported.update(findings)
            details = ' \\\\ '.join(self._finding_wiki(*finding) for finding in dict.fromkeys(findings)) or '—'
            rows.append(f"| {name} | {wiki_cell(status)} | {result['seconds']:.2f} | {len(result['issues'])} | "
                        f"{result['success']} / {result['error']} / {result['warning']} | {details} |")
        # Keep failures before the first stage (e.g. inaccessible release ticket).
        for key, data in self.report_data.items():
            for field, status in (('errors', 'error'), ('warnings', 'warning')):
                for message in data[field]:
                    if (key, status, message) not in reported:
                        rows.append(f"| Общая проверка | {'FAIL' if status == 'error' else 'WARN'} | — | 1 | — | "
                                    + self._finding_wiki(key, status, message) + ' |')
        if len(rows) == 1:
            rows.append('| Проверки не выполнены | INCOMPLETE | — | 0 | 0 / 0 / 0 | Нет результатов проверок |')
        return '\n'.join(rows)

    @staticmethod
    def _is_testing_time_deviation(message):
        return 'суммарное время в тест-статусах' in message and 'больше' in message

    def _finding_wiki(self, key, status, message):
        target = wiki_cell(key)
        if key != 'GENERAL' and re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', key):
            target = f"[{key}|{config['jira']['url'].rstrip('/')}/browse/{key}]"
        label = 'Отклонение' if self._is_testing_time_deviation(message) else ('Ошибка' if status == 'error' else 'Предупреждение')
        return f"{target}: {label} — {wiki_cell(message)}"

    @staticmethod
    def _comment_message(message):
        """Keep the action needed by the release owner; diagnostics stay in logs."""
        text = clean_report_text(message)
        lower = text.casefold()
        if any(marker in lower for marker in ('zephyr debug:', 'zephyr direct scan:', 'gigachat',
                                              'сохранить кэш zephyr', 'баги не найдены в составе')):
            return ''
        if 'нет прилинкованных тк' in lower:
            return 'Нет связанных тест-кейсов.'
        if 'не удалось проверить полный список тц' in lower:
            return 'Не удалось проверить все тест-циклы релиза.'
        technical = any(marker in lower for marker in (
            'не удалось', 'проверка не выполнена', 'лимит неуспешных',
            'ошибка получения', 'ошибка поиска', 'ошибка проверки', 'timeout', 'httppool', 'httpsconnectionpool',
            'временно не отдал', 'проверка тц пропущена', 'диагностика:', '/rest/', 'status=', 'traceback'))
        if technical:
            if 'aifixed' in lower or 'gigacode' in lower:
                return 'Не удалось проверить необходимость лейбла AIFIXED.'
            if 'pull request' in lower or re.search(r'\bpr\b', lower):
                return 'Не удалось проверить ПР. Повторите проверку.'
            if 'metadata' in lower or re.search(r'\bкэ\b', lower):
                return 'Не удалось проверить тест-циклы: недоступны данные КЭ.'
            if 'тц' in lower or 'тест-цикл' in lower:
                return 'Не удалось проверить тест-циклы в Zephyr.'
            if 'zephyr' in lower or 'тк' in lower or 'покрытие' in lower:
                return 'Не удалось проверить тест-кейсы в Zephyr.'
            if 'баг' in lower:
                return 'Не удалось проверить баги релиза.'
            if 'истори' in lower:
                return 'Не удалось проверить историю изменений задачи.'
            if 'анализ архитектуры' in lower:
                return 'Не удалось проверить закрытие задачи «Анализ архитектуры».'
            return 'Не удалось завершить проверку. Повторите запуск.'
        text = re.sub(r'^(?:Story|Bug|Web release|Back release):\s*', '', text)
        text = re.sub(r"sub-task 'Анализ архитектуры':\s*", 'Анализ архитектуры — ', text)
        text = re.sub(r'^Zephyr\s+(ТК|ТЦ)', r'\1', text)
        text = re.sub(r'«[^»]+»\s*', '', text)
        text = re.sub(r'\s*\(customfield_\d+\)', '', text)
        text = re.sub(r'^Pull Request не найден\..*', 'Не найден ПР, связанный с задачей.', text)
        text = re.sub(r'^Отлинкованная задача:.*?merged.*',
                      'У исключённой из релиза задачи есть влитый ПР.', text, flags=re.I)
        return normalize_field_text(text)

    def _release_comment(self, release_key, is_success):
        grouped = {}
        labels = {'error': '(x) Исправить', 'unavailable': '(?) Нет данных',
                  'warning': '(!) Проверить', 'deviation': '(!) Отклонение', 'success': '(/) Без замечаний'}
        has_errors = any(data['errors'] for data in self.report_data.values())
        success = is_success and not has_errors
        # PR lookup observations are the explicit exception: they keep a green result.
        other_remarks = any(self._comment_message(message) and not (
            self._is_testing_time_deviation(message) or 'pull request' in message.casefold() or re.search(r'\bpr\b', message, re.I))
            for data in self.report_data.values() for message in data['warnings'])
        green = success and not other_remarks
        for key, data in self.report_data.items():
            messages = {}
            for severity, values in (('error', data['errors']), ('warning', data['warnings'])):
                for original in values:
                    message = self._comment_message(original)
                    if message:
                        kind = ('deviation' if self._is_testing_time_deviation(original) else
                                'unavailable' if message.startswith('Не удалось') else severity)
                        messages.setdefault(message, kind)
            if not messages:
                continue
            object_key = release_key if key == 'GENERAL' else key
            entry = grouped.setdefault(object_key, {'summary': data['summary'], 'messages': {}})
            if key == release_key and data['summary']:
                entry['summary'] = data['summary']
            for message, kind in messages.items():
                entry['messages'].setdefault(message, kind)
        if not grouped:
            grouped[release_key] = {'summary': '', 'messages': {
                'Ошибок нет.' if success else 'Проверка не завершена. Повторите запуск.':
                'success' if success else 'unavailable'}}
        color, background = ('#14892c', '#e3fcef') if green else ('#de350b', '#ffebe6')
        title = 'Проверка релиза — успешно' if green else 'Проверка релиза — есть замечания'
        if green and any(data['warnings'] for data in self.report_data.values()):
            title += ' (есть неблокирующие замечания)'
        counts = {kind: sum(value == kind for entry in grouped.values() for value in entry['messages'].values())
                  for kind in ('error', 'unavailable', 'warning', 'deviation')}
        rows = [f'{{panel:title={title}|titleBGColor={background}|titleColor={color}'
                f'|borderColor={color}|bgColor=#ffffff}}',
                f"*Исправить: {counts['error']}* · *Нет данных: {counts['unavailable']}* · "
                f"*Проверить: {counts['warning']}* · *Отклонения: {counts['deviation']}*", '']
        if counts['unavailable']:
            rows.extend(['(?) *Нет данных* — источник не ответил или вернул неполный результат. '
                         'Это не подтверждает отсутствие ПР или тестов; повторите проверку.', ''])
        rows.append(COMMENT_TABLE_HEADER)
        for key, entry in sorted(grouped.items(), key=lambda item: (item[0] != release_key, item[0])):
            target = wiki_cell(key)
            if re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', key):
                target = f"[{key}|{config['jira']['url'].rstrip('/')}/browse/{key}]"
            if entry['summary'] and entry['summary'] != 'Общие проверки':
                target += ' \\\\ ' + wiki_cell(trim_report_text(entry['summary'], 100))
            messages = ' \\\\ \\\\ '.join(
                f'*{number}.* {wiki_cell(message)}'
                for number, message in enumerate(entry['messages'], 1))
            statuses = ' \\\\ '.join(labels[kind] for kind in labels if kind in entry['messages'].values())
            rows.append(f'| {target} | {statuses} | {messages} |')
        rows.append('{panel}')
        return '\n'.join(rows)

    def check_release(self, release_key):
        self._reset_release_run()
        print(f"🔍 Проверка релиза: {release_key}")
        print("⏳ Выполняется анализ...\n")

        try:
            release = self.jira_main.issue(release_key, expand='subtasks')
            self._log_issue(release, "success", "Релизный тикет найден")
        except Exception as e:
            self._log_issue("GENERAL", "error", f"Критическая ошибка: Не удалось получить тикет релиза: {str(e)}")
            return False

        self._run_check("release", "Контур релиза", lambda: self._check_required_platform_label(release, "Release"))

        # Определяем тип релиза: Hotfix или обычный
        release_type_raw = getattr(release.fields, 'customfield_23500', None)
        if release_type_raw is None and hasattr(release, 'raw'):
            release_type_raw = release.raw['fields'].get('customfield_23500')
        if hasattr(release_type_raw, 'value'):
            release_type_str = release_type_raw.value
        elif isinstance(release_type_raw, dict):
            release_type_str = release_type_raw.get('value', '')
        elif isinstance(release_type_raw, str):
            release_type_str = release_type_raw.strip()
        else:
            release_type_str = ''
        is_hotfix = release_type_str.strip().lower() == 'hotfix'
        if is_hotfix:
            print("   🔥 Тип релиза: Hotfix — применяются дополнительные проверки")

        release_consist_of = self._extract_consist_of_issues(release)
        if release_consist_of is not None:
            self._consist_of_cache[release_key] = release_consist_of

        labels = self._issue_labels_casefold(release)
        checks = [
            ('test_subtask', 'Списание времени в тестировании релиза', lambda: self._check_test_subtask(release), True, ''),
            ('artifacts', 'Комментарии тестировщиков и worklog', lambda: self._check_artifacts(release_key), True, ''),
            ('coverage', 'Связь задач с тест-кейсами', lambda: self._check_release_coverage(release_key), True, ''),
            ('zephyr_cases', 'ТК Zephyr: Approved и вид тестирования', lambda: self._check_zephyr_test_cases(release_key, is_hotfix=is_hotfix), True, ''),
            ('back_cycles', 'Back: обязательные ТЦ по КЭ', lambda: self._check_back_release_service_test_cycles(release_key), True,
             'Hotfix' if is_hotfix else ('' if 'back' in labels else 'нет лейбла back')),
            ('web_cycles', 'Web: обязательные ТЦ по КЭ', lambda: self._check_web_release_service_test_cycles(release_key), True,
             'Hotfix' if is_hotfix else ('' if 'web' in labels else 'нет лейбла web')),
            ('bugs', 'Баги: поля, статусы, контур, причины пропуска и Confluence', lambda: self._check_bugs(release_key, is_hotfix=is_hotfix), True, ''),
            ('stories', 'Story: поля, архитектура, сроки тестирования и Task', lambda: self._check_stories(release_key), True, ''),
            ('required_pr', 'Наличие связи PR', lambda: self._check_required_pull_requests(release_key), False, ''),
            ('unlinked_pr', 'Merged PR у отлинкованных задач', lambda: self._check_unlinked_release_items_pull_requests(release_key), False, ''),
            ('gigacode', 'GigaCode и AIFIXED', lambda: self._check_gigacode_aifixed_labels(release_key), False, ''),
            ('cloud', 'Cloud: лейбл внешнего рынка', lambda: self._check_cloud_label(release_key, release.fields.summary), True,
             '' if 'cloud' in release.fields.summary.casefold() else 'релиз не Cloud'),
            ('third_party', 'SBRPPL: лейбл третьих лиц', lambda: self._check_sbrppl_third_party_label(release_key), True, ''),
            ('story_points', 'SBRPPL: Story Points', lambda: self._check_sbrppl_story_points(release_key), True, ''),
            ('gigachat', 'GigaChat: соответствие описания', lambda: self._check_summary_description_match(release_key), False, ''),
        ]
        for check_id, name, callback, required, reason in checks:
            # Independent budgets: PR existence cannot starve merged/GigaCode checks.
            budget = PR_RELEASE_TIMEOUT_SECONDS if check_id in {'required_pr', 'unlinked_pr', 'gigacode'} else None
            self._run_check(check_id, name, callback, required=required, skip_reason=reason, budget=budget)
        print("\n[CHECK SUMMARY]\n" + self._checks_wiki_table(), flush=True)

        total_errors = sum(len(d['errors']) for d in self.report_data.values())
        return total_errors == 0

    def _check_zephyr_test_cases(self, release_key: str, is_hotfix: bool = False):
        """
        Проверяет для каждой задачи внутри релиза:
          1. Все прилинкованные ТК из HRPQA в статусе 'Approved'.
          2. «Вид тестирования» у прилинкованных ТК:
             - Bug/Defect/Ошибка → «Регресс»
             - Story с customfield_24000 = «Да» → «Новый функционал»
             - Story с customfield_24000 = «Нет» → «Регресс»
             - Прочие типы задач → проверка не выполняется
        """
        print(f"\n🧪 Проверка статусов ТК Zephyr Scale (проект {ZEPHYR_TC_PROJECT_KEY})...")

        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            self._log_issue(
                "GENERAL", "warning",
                "Zephyr: нет задач со связью 'consist of' — проверка ТК пропущена"
            )
            return

        # Маппинг issue_key → ожидаемый «Вид тестирования»
        expected_type_map = self._build_expected_testing_type_map(linked_keys)
        issue_type_map = self._get_issue_type_map(linked_keys)
        bug_types = {'bug', 'defect', 'ошибка'}

        total_tc_checked = 0
        total_not_approved = 0

        for issue_key in linked_keys:
            if self.zephyr.is_failed_request_limit_reached():
                self._log_issue(
                    "GENERAL",
                    "warning",
                    "Zephyr: лимит неуспешных запросов исчерпан, дальнейшая проверка ТК пропущена"
                )
                break

            test_cases, tc_error = self._get_issue_test_cases_cached(issue_key)

            if tc_error is not None:
                if self.zephyr.is_failed_request_limit_reached():
                    self._log_issue(
                        "GENERAL",
                        "warning",
                        "Zephyr: лимит неуспешных запросов исчерпан, дальнейшая проверка ТК пропущена"
                    )
                    break
                self._log_issue(
                    issue_key, "warning",
                    f"Zephyr: не удалось получить ТК ({tc_error})"
                )
                continue

            hrpqa_test_cases = [
                tc for tc in test_cases
                if self.zephyr.get_test_case_key(tc).startswith(ZEPHYR_TC_PROJECT_KEY + '-T')
            ]

            if not hrpqa_test_cases:
                # АПИ ответил успешно, ТК просто нет
                self._log_issue(
                    issue_key, "error",
                    f"Zephyr: задача есть в Jira, но нет прилинкованных ТК в пространстве {ZEPHYR_TC_PROJECT_KEY}"
                )
                continue

            expected_testing_type = expected_type_map.get(issue_key)
            issue_type = issue_type_map.get(issue_key, '')
            require_direct_test_case_link = issue_type == 'story' or issue_type in bug_types

            for tc in hrpqa_test_cases:
                tc_key = self.zephyr.get_test_case_key(tc)
                tc_name = self.zephyr.get_test_case_name(tc)
                tc_details = self._get_test_case_details_cached(tc_key)
                if tc_details:
                    tc_name = self.zephyr.get_test_case_name(tc_details)
                    tc_status = self.zephyr.get_test_case_status(tc_details)
                else:
                    tc_status = self.zephyr.get_test_case_status(tc)
                    self._log_issue(
                        issue_key, "warning",
                        f"Zephyr ТК [{tc_key}] «{tc_name}»: не удалось получить детали ТК, "
                        "статус проверяется по короткому ответу issuelink"
                    )

                if require_direct_test_case_link and not self._is_directly_linked_test_case_for_issue(issue_key, tc, tc_details):
                    self._log_issue(
                        issue_key,
                        "warning",
                        f"Zephyr ТК [{tc_key}] «{tc_name}» пропущен: в данных ТК нет прямой связи "
                        f"с задачей {issue_key}; вероятно, ТК пришел из ТЦ/результата выполнения"
                    )
                    continue

                total_tc_checked += 1

                # --- Проверка 1: статус Approved ---
                if not tc_status:
                    self._log_issue(issue_key, 'error',
                                    f"Zephyr ТК [{tc_key}]: не удалось определить статус ТК")
                elif tc_status.casefold() != ZEPHYR_APPROVED_STATUS.casefold():
                    total_not_approved += 1
                    self._log_issue(
                        issue_key, "error",
                        f"Zephyr ТК [{tc_key}] «{tc_name}» не в статусе "
                        f"'{ZEPHYR_APPROVED_STATUS}' (текущий: '{tc_status}')"
                    )
                else:
                    self._log_issue(
                        issue_key, "success",
                        f"Zephyr ТК [{tc_key}] в статусе '{ZEPHYR_APPROVED_STATUS}' ✓"
                    )

                # --- Проверка 2: «Вид тестирования» ---
                if expected_testing_type:
                    if tc_details:
                        actual_type = self.zephyr.get_test_case_custom_field(
                            tc_details, ZEPHYR_TESTING_TYPE_FIELD
                        )
                        if actual_type is None:
                            self._log_issue(
                                issue_key, "warning",
                                f"Zephyr ТК [{tc_key}]: не удалось проверить поле '{ZEPHYR_TESTING_TYPE_FIELD}' "
                                f"— не найдено в кастомных полях ТК"
                            )
                        else:
                            if actual_type.strip().lower() != expected_testing_type.strip().lower():
                                self._log_issue(
                                    issue_key, "error",
                                    f"Zephyr ТК [{tc_key}] «{tc_name}»: '{ZEPHYR_TESTING_TYPE_FIELD}' = "
                                    f"'{actual_type}', ожидается '{expected_testing_type}'"
                                )
                            else:
                                self._log_issue(
                                    issue_key, "success",
                                    f"Zephyr ТК [{tc_key}]: {ZEPHYR_TESTING_TYPE_FIELD} = "
                                    f"'{actual_type}' ✓"
                                )
                    else:
                        self._log_issue(
                            issue_key, "warning",
                            f"Zephyr ТК [{tc_key}]: не удалось получить детали ТК "
                            f"для проверки поля '{ZEPHYR_TESTING_TYPE_FIELD}'"
                        )

        print(
            f"   Zephyr: проверено {total_tc_checked} ТК, "
            f"не утверждено: {total_not_approved}"
        )

    def _build_expected_testing_type_map(self, linked_keys: list[str]) -> dict[str, str]:
        """
        Для Story/Bug/Defect из состава релиза определяет ожидаемый
        «Вид тестирования» у прилинкованных ТК.

        Правила:
          Bug / Defect / Ошибка  → 'Регресс'
          Story, customfield_24000 = 'Да'  → 'Новый функционал'
          Story, customfield_24000 = 'Нет' → 'Регресс'
          Прочие типы → не попадают в маппинг.

        Возвращает dict: issue_key → ожидаемый вид тестирования.
        Если задача не попала в словарь — проверка вида тестирования не выполняется.
        """
        result = {}
        keys_str = ",".join(linked_keys)
        bug_types = {'bug', 'defect', 'ошибка'}

        try:
            issues = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields='summary,issuetype,customfield_24000',
                maxResults=False
            )
        except Exception as exc:
            raise RuntimeError("не удалось получить вид тестирования задач") from exc

        for issue in issues:
            issue_type = issue.fields.issuetype.name.lower() if issue.fields.issuetype else ''

            if issue_type in bug_types:
                result[issue.key] = ZEPHYR_TESTING_TYPE_REGRESSION
                continue

            # Story — смотрим customfield_24000
            if issue_type == 'story':
                raw_val = getattr(issue.fields, 'customfield_24000', None)
                if raw_val is None:
                    field_value = None
                elif hasattr(raw_val, 'value'):
                    field_value = raw_val.value
                elif isinstance(raw_val, dict):
                    field_value = raw_val.get('value')
                elif isinstance(raw_val, str):
                    field_value = raw_val.strip()
                else:
                    field_value = str(raw_val).strip()

                if not field_value:
                    continue

                normalized = field_value.strip().lower()
                if normalized == 'да':
                    result[issue.key] = ZEPHYR_TESTING_TYPE_NEW
                elif normalized == 'нет':
                    result[issue.key] = ZEPHYR_TESTING_TYPE_REGRESSION

        return result

    def _check_back_release_service_test_cycles(self, release_key: str) -> None:
        """
        Для back-релизов проверяет наличие Zephyr ТЦ по каждой КЭ:
          - {release}.{КЭ}.ipad/pwa/safari/sberbrowser.Регресс — обязательно;
          - {release}.{КЭ}.api.Регресс — обязательно;
          - {release}.{КЭ}.api.НФ — дополнительно, если внутри этой КЭ есть Story
            с "Новая функциональность" = "Да".

        Zephyr ищем один раз по ключу релиза, затем матчим названия локально.
        """
        print(f"\n📋 Проверка API ТЦ Zephyr Scale для back-релиза...")

        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            self._log_issue(
                release_key,
                "warning",
                "Back release: нет задач со связью 'consist of' — проверка API ТЦ по КЭ пропущена"
            )
            return

        service_ke_field_id = self._get_jira_field_id_by_names(SERVICE_KE_FIELD_NAME_CANDIDATES)
        if not service_ke_field_id:
            self._log_issue(
                release_key,
                "error",
                "Back release: не удалось определить Jira field id для поля КЭ/КЭ сервиса"
            )
            return

        keys_str = ",".join(linked_keys)
        issue_types_to_check = {'story', 'bug', 'defect', 'ошибка'}
        try:
            release_items = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields=f'summary,issuetype,assignee,{service_ke_field_id},customfield_24000',
                maxResults=False
            )
        except Exception as e:
            self._log_issue(
                release_key,
                "error",
                f"Back release: ошибка получения задач состава релиза для проверки КЭ: {e}"
            )
            return

        services: dict[str, dict[str, object]] = {}
        for issue in release_items:
            issue_type = issue.fields.issuetype.name.casefold() if issue.fields.issuetype else ''
            if issue_type not in issue_types_to_check:
                continue

            service_values = self._extract_issue_service_ke_values(issue, service_ke_field_id)
            if not service_values:
                self._log_issue(
                    issue,
                    "error",
                    f"Back release: не заполнено поле КЭ сервиса ({service_ke_field_id}); "
                    "невозможно проверить обязательные API ТЦ"
                )
                continue

            for service_ke in service_values:
                service_key = normalize_field_text(service_ke).casefold()
                service_info = services.setdefault(service_key, {
                    'display': service_ke,
                    'issue_keys': set(),
                    'has_nf_story': False,
                })
                service_info['issue_keys'].add(issue.key)
                if issue_type == 'story' and self._story_has_new_functionality(issue):
                    service_info['has_nf_story'] = True

        if not services:
            self._log_issue(
                release_key,
                "error",
                "Back release: в составе релиза не найдено ни одной Story/Bug с заполненной КЭ сервиса"
            )
            return

        cycles = self._get_release_test_cycles(release_key)

        if not cycles:
            self._log_missing_release_cycles(release_key, "Back release")
            return

        print(f"   Back release: найдено КЭ={len(services)}, ТЦ по ключу релиза={len(cycles)}")

        pending_case_checks = []
        for service_info in services.values():
            service_ke = str(service_info['display'])
            issue_list = ', '.join(sorted(service_info['issue_keys']))
            service_aliases = service_ke_name_aliases(service_ke)

            device_regress_mask_hint = build_cycle_mask_hint(
                release_key,
                service_aliases,
                BACK_DEVICE_BROWSER_CHANNEL_ALIASES,
                'Регресс',
            )
            api_regress_mask_hint = build_cycle_mask_hint(
                release_key,
                service_aliases,
                BACK_API_CHANNEL_ALIASES,
                'Регресс',
            )
            api_nf_mask_hint = build_cycle_mask_hint(
                release_key,
                service_aliases,
                BACK_API_CHANNEL_ALIASES,
                'НФ',
            )

            device_regress_match = self._find_matching_cycle(
                cycles,
                release_key,
                service_ke,
                BACK_DEVICE_BROWSER_CHANNEL_ALIASES,
                BACK_REGRESS_CYCLE_ALIASES,
            )
            if device_regress_match:
                tc_key, tc_name = device_regress_match
                self._log_issue(
                    release_key,
                    "success",
                    f"Back release: для КЭ '{service_ke}' найден iPad/PWA/Safari/SberBrowser Регресс ТЦ "
                    f"[{tc_key}] '{tc_name}' ✓"
                )
                self._check_test_cycle_testing_type(release_key, tc_key, tc_name, 'Регресс')
                pending_case_checks.append((tc_key, tc_name))
            else:
                self._log_issue(
                    release_key,
                    "error",
                    f"Back release: для КЭ '{service_ke}' ({issue_list}) отсутствует ТЦ Zephyr "
                    f"по маске '{device_regress_mask_hint}'"
                )

            api_regress_match = self._find_matching_cycle(
                cycles,
                release_key,
                service_ke,
                BACK_API_CHANNEL_ALIASES,
                BACK_REGRESS_CYCLE_ALIASES,
            )
            if api_regress_match:
                tc_key, tc_name = api_regress_match
                self._log_issue(
                    release_key,
                    "success",
                    f"Back release: для КЭ '{service_ke}' найден API Регресс ТЦ "
                    f"[{tc_key}] '{tc_name}' ✓"
                )
                self._check_test_cycle_testing_type(release_key, tc_key, tc_name, 'Регресс')
                pending_case_checks.append((tc_key, tc_name))
            else:
                self._log_issue(
                    release_key,
                    "error",
                    f"Back release: для КЭ '{service_ke}' ({issue_list}) отсутствует ТЦ Zephyr "
                    f"по маске '{api_regress_mask_hint}'"
                )

            if service_info['has_nf_story']:
                nf_match = self._find_matching_cycle(
                    cycles,
                    release_key,
                    service_ke,
                    BACK_API_CHANNEL_ALIASES,
                    BACK_NF_CYCLE_ALIASES,
                )
                if nf_match:
                    tc_key, tc_name = nf_match
                    self._log_issue(
                        release_key,
                        "success",
                        f"Back release: для КЭ '{service_ke}' найден API НФ ТЦ "
                        f"[{tc_key}] '{tc_name}' ✓"
                    )
                    self._check_test_cycle_testing_type(release_key, tc_key, tc_name, ZEPHYR_TESTING_TYPE_NEW)
                    pending_case_checks.append((tc_key, tc_name))
                else:
                    self._log_issue(
                        release_key,
                        "error",
                        f"Back release: для КЭ '{service_ke}' есть Story с 'Новая функциональность' = 'Да', "
                        f"но отсутствует ТЦ Zephyr по маске '{api_nf_mask_hint}'"
                    )

        self._check_cycle_case_batches(release_key, pending_case_checks)

    def _check_web_release_service_test_cycles(self, release_key: str) -> None:
        """
        Для web-релизов проверяет наличие Zephyr ТЦ по каждой КЭ:
          - {release}.{КЭ}.ipad/pwa/safari/sberbrowser.НФ — если внутри КЭ есть Story
            с "Новая функциональность" = "Да";
          - для NEUROUI вместо НФ-маски с PWA используется
            {release}.{КЭ}.ipad/safari/sberbrowser.НФ;
          - {release}.{КЭ}.ipad.Регресс — обязательно;
          - {release}.{КЭ}.pwa.Регресс — обязательно, кроме проекта NEUROUI;
          - {release}.{КЭ}.safari.Регресс — обязательно;
          - {release}.{КЭ}.sberbrowser.Регресс — обязательно.
        """
        print(f"\n📋 Проверка Web ТЦ Zephyr Scale по КЭ...")

        services = self._collect_release_service_infos(release_key, "Web release")
        if not services:
            return

        cycles = self._get_release_test_cycles(release_key)
        if not cycles:
            self._log_missing_release_cycles(release_key, "Web release")
            return

        print(f"   Web release: найдено КЭ={len(services)}, ТЦ по ключу релиза={len(cycles)}")

        pending_case_checks = []
        for service_info in services.values():
            service_ke = str(service_info['display'])
            issue_list = ', '.join(sorted(service_info['issue_keys']))
            service_aliases = service_ke_name_aliases(service_ke)
            project_keys = {str(project_key).casefold() for project_key in service_info.get('project_keys', set())}
            is_neuroui = bool(project_keys) and project_keys <= {'neuroui'}

            if service_info['has_nf_story']:
                nf_channel_aliases = (
                    WEB_DEVICE_BROWSER_NF_CHANNEL_ALIASES_NEUROUI
                    if is_neuroui else WEB_DEVICE_BROWSER_NF_CHANNEL_ALIASES
                )
                nf_channel_name = 'iPad/Safari/SberBrowser' if is_neuroui else 'iPad/PWA/Safari/SberBrowser'
                nf_mask_hint = build_cycle_mask_hint(
                    release_key,
                    service_aliases,
                    nf_channel_aliases,
                    'НФ',
                )
                nf_match = self._find_matching_cycle(
                    cycles,
                    release_key,
                    service_ke,
                    nf_channel_aliases,
                    BACK_NF_CYCLE_ALIASES,
                )
                if nf_match:
                    tc_key, tc_name = nf_match
                    self._log_issue(
                        release_key,
                        "success",
                        f"Web release: для КЭ '{service_ke}' найден {nf_channel_name} НФ ТЦ "
                        f"[{tc_key}] '{tc_name}' ✓"
                    )
                    self._check_test_cycle_testing_type(release_key, tc_key, tc_name, ZEPHYR_TESTING_TYPE_NEW)
                    pending_case_checks.append((tc_key, tc_name))
                else:
                    self._log_issue(
                        release_key,
                        "error",
                        f"Web release: для КЭ '{service_ke}' есть Story с 'Новая функциональность' = 'Да', "
                        f"но отсутствует ТЦ Zephyr по маске '{nf_mask_hint}'"
                    )

            regress_channels = WEB_REGRESS_CHANNELS_NEUROUI if is_neuroui else WEB_REGRESS_CHANNELS
            for channel_name, channel_aliases in regress_channels:
                regress_mask_hint = build_cycle_mask_hint(
                    release_key,
                    service_aliases,
                    channel_aliases,
                    'Регресс',
                )
                regress_match = self._find_matching_cycle(
                    cycles,
                    release_key,
                    service_ke,
                    channel_aliases,
                    BACK_REGRESS_CYCLE_ALIASES,
                )
                if regress_match:
                    tc_key, tc_name = regress_match
                    self._log_issue(
                        release_key,
                        "success",
                        f"Web release: для КЭ '{service_ke}' найден {channel_name} Регресс ТЦ "
                        f"[{tc_key}] '{tc_name}' ✓"
                    )
                    self._check_test_cycle_testing_type(release_key, tc_key, tc_name, 'Регресс')
                    pending_case_checks.append((tc_key, tc_name))
                else:
                    self._log_issue(
                        release_key,
                        "error",
                        f"Web release: для КЭ '{service_ke}' ({issue_list}) отсутствует ТЦ Zephyr "
                        f"по маске '{regress_mask_hint}'"
                    )

            if is_neuroui:
                self._log_issue(
                    release_key,
                    "success",
                    f"Web release: для КЭ '{service_ke}' проект NEUROUI — pwa.Регресс не требуется ✓"
                )

        self._check_cycle_case_batches(release_key, pending_case_checks)

    def _check_test_cycles(self, release_key: str):
        """
        Проверка тест-циклов (ТЦ) в составе релиза.

        Правила:
          1. ТЦ получаем через Zephyr API (поиск + issuelink, все проекты).
          2. Если есть Story с 'Новая функциональность' = 'Да' -> должен быть
             ТЦ с 'НФ' или 'NF' в названии,
             кастомное поле 'Вид тестирования' = 'Новый функционал',
             в составе ТЦ только ТК из этих Story (не более).
          3. Обязательно должен быть ТЦ с 'Регресс' или 'Regress' в названии,
             кастомное поле 'Вид тестирования' = 'Регресс'.
          4. Если есть ТЦ с 'Web' или 'ВЭБ' в названии,
             то обязательно должны быть ТЦ с 'PWA' и 'IPAD' в названии.
          5. Во всех ТЦ все ТК должны быть в статусе 'Approved'.
          6. Сравнение названий регистронезависимое.
        """
        print(f"\n📋 Проверка тест-циклов Zephyr Scale...")

        # Получаем числовой id тикета релиза — Zephyr иногда хранит связь по id, а не по key
        release_id = None
        try:
            release_issue = self.jira_main.issue(release_key, fields='id')
            release_id = str(release_issue.id)
        except Exception:
            pass

        # --- Получаем ТЦ, привязанные к релизу ---
        cycles = self.zephyr.get_test_cycles_for_issue(release_key, issue_id=release_id)
        if not cycles:
            cycles = self._get_test_cycles_from_jira_release_metadata(release_key)
        if not cycles:
            cycles = self._get_test_cycles_from_direct_cache_or_scan(release_key)

        if not cycles:
            self._log_issue(
                "GENERAL", "error",
                "Zephyr: нет тест-циклов, привязанных к релизу"
            )
            return

        print(f"   Найдено {len(cycles)} ТЦ")

        # Собираем детали каждого ТЦ
        cycle_details = []  # список (key, name, name_casefold, details)
        for tc_raw in cycles:
            tc_key = self.zephyr.get_test_cycle_key(tc_raw)
            details = self.zephyr.get_test_cycle_details(tc_key) if tc_key else None
            if details:
                name = self.zephyr.get_test_cycle_name(details) or self.zephyr.get_test_cycle_name(tc_raw)
            else:
                name = self.zephyr.get_test_cycle_name(tc_raw)
            cycle_details.append((tc_key, name, name.casefold(), details))

        # --- Проверяем Story с НФ для правила 2 ---
        linked_keys = self._get_consist_of_issues(release_key)
        nf_story_keys = set()
        if linked_keys:
            expected_type_map = self._build_expected_testing_type_map(linked_keys)
            for key, expected in expected_type_map.items():
                if expected == ZEPHYR_TESTING_TYPE_NEW:
                    nf_story_keys.add(key)

        # Собираем ТК, прилинкованные к NF Story
        nf_story_tc_keys = set()
        for story_key in nf_story_keys:
            tcs, _ = self.zephyr.get_test_cases_for_issue(story_key)
            for tc in tcs:
                tc_key = self.zephyr.get_test_case_key(tc)
                if tc_key.startswith(ZEPHYR_TC_PROJECT_KEY):
                    nf_story_tc_keys.add(tc_key)

        # --- Классифицируем ТЦ по названиям ---
        has_nf_cycle = False
        has_regress_cycle = False
        has_web_cycle = False
        has_pwa_cycle = False
        has_ipad_cycle = False

        for tc_key, name, name_casefold, details in cycle_details:

            # Классификация
            is_nf = ('нф' in name_casefold or 'nf' in name_casefold)
            is_regress = ('регресс' in name_casefold or 'regress' in name_casefold)
            is_web = ('web' in name_casefold or 'вэб' in name_casefold)
            is_pwa = ('pwa' in name_casefold)
            is_ipad = ('ipad' in name_casefold)

            if is_nf:
                has_nf_cycle = True
            if is_regress:
                has_regress_cycle = True
            if is_web:
                has_web_cycle = True
            if is_pwa:
                has_pwa_cycle = True
            if is_ipad:
                has_ipad_cycle = True

            # --- Правило 5: все ТК в каждом ТЦ должны быть в статусе Approved ---
            test_results = self.zephyr.get_test_cycle_test_results(details)
            not_approved_in_cycle = []
            for tr in test_results:
                # testCaseKey может быть в разных полях
                tr_tc_key = tr.get('testCaseKey', tr.get('testCase', {}).get('key', '') if isinstance(tr.get('testCase'), dict) else tr.get('testCase', ''))
                # Статус ТК из результата — берём из самого ТК, а не из результата выполнения
                if tr_tc_key and tr_tc_key.startswith(ZEPHYR_TC_PROJECT_KEY):
                    tc_detail = self.zephyr.get_test_case_details(tr_tc_key)
                    if tc_detail:
                        tc_status = self.zephyr.get_test_case_status(tc_detail)
                        if tc_status.casefold() != ZEPHYR_APPROVED_STATUS.casefold():
                            tc_name = tc_detail.get('name', tr_tc_key)
                            not_approved_in_cycle.append((tr_tc_key, tc_name, tc_status))

            if not_approved_in_cycle:
                for bad_key, bad_name, bad_status in not_approved_in_cycle:
                    self._log_issue(
                        release_key, "error",
                        f"ТЦ [{tc_key}] '{name}': ТК [{bad_key}] '{bad_name}' "
                        f"не в статусе '{ZEPHYR_APPROVED_STATUS}' (текущий: '{bad_status}')"
                    )
            else:
                if test_results:
                    self._log_issue(
                        release_key, "success",
                        f"ТЦ [{tc_key}] '{name}': все ТК в статусе '{ZEPHYR_APPROVED_STATUS}' ✓"
                    )

            # --- Правило 2: ТЦ с НФ — проверяем 'Вид тестирования' и состав ТК ---
            if is_nf and details:
                vt = self.zephyr.get_test_case_custom_field(details, ZEPHYR_TESTING_TYPE_FIELD)
                if vt is None:
                    self._log_issue(
                        release_key, "warning",
                        f"ТЦ [{tc_key}] '{name}': поле '{ZEPHYR_TESTING_TYPE_FIELD}' не найдено"
                    )
                elif vt.strip().casefold() != ZEPHYR_TESTING_TYPE_NEW.casefold():
                    self._log_issue(
                        release_key, "error",
                        f"ТЦ [{tc_key}] '{name}': '{ZEPHYR_TESTING_TYPE_FIELD}' = '{vt}', ожидается '{ZEPHYR_TESTING_TYPE_NEW}'"
                    )
                else:
                    self._log_issue(
                        release_key, "success",
                        f"ТЦ [{tc_key}] '{name}': {ZEPHYR_TESTING_TYPE_FIELD} = '{vt}' ✓"
                    )

                # Проверяем состав: только ТК из НФ Story, не более
                if nf_story_tc_keys:
                    cycle_tc_keys = set()
                    for tr in test_results:  # test_results уже загружены выше
                        tr_tc_key = tr.get('testCaseKey', tr.get('testCase', {}).get('key', '') if isinstance(tr.get('testCase'), dict) else tr.get('testCase', ''))
                        if tr_tc_key:
                            cycle_tc_keys.add(tr_tc_key)

                    extra_tcs = cycle_tc_keys - nf_story_tc_keys
                    if extra_tcs:
                        extra_list = ', '.join(sorted(extra_tcs))
                        self._log_issue(
                            release_key, "error",
                            f"ТЦ [{tc_key}] '{name}': содержит ТК не из Story НФ релиза: {extra_list}"
                        )
                    else:
                        self._log_issue(
                            release_key, "success",
                            f"ТЦ [{tc_key}] '{name}': все ТК входят в Story НФ релиза ✓"
                        )

            # --- Правило 3: ТЦ с Регресс — проверяем 'Вид тестирования' ---
            if is_regress and details:
                vt = self.zephyr.get_test_case_custom_field(details, ZEPHYR_TESTING_TYPE_FIELD)
                if vt is None:
                    self._log_issue(
                        release_key, "warning",
                        f"ТЦ [{tc_key}] '{name}': поле '{ZEPHYR_TESTING_TYPE_FIELD}' не найдено"
                    )
                elif vt.strip().casefold() != 'регресс':
                    self._log_issue(
                        release_key, "error",
                        f"ТЦ [{tc_key}] '{name}': '{ZEPHYR_TESTING_TYPE_FIELD}' = '{vt}', ожидается 'Регресс'"
                    )
                else:
                    self._log_issue(
                        release_key, "success",
                        f"ТЦ [{tc_key}] '{name}': {ZEPHYR_TESTING_TYPE_FIELD} = '{vt}' ✓"
                    )

        # --- Правило 2: наличие ТЦ НФ если есть Story с НФ ---
        if nf_story_keys and not has_nf_cycle:
            self._log_issue(
                release_key, "error",
                f"Есть Story с 'Новая функциональность' = 'Да', но нет ТЦ с 'НФ'/'NF' в названии"
            )

        # --- Правило 3: обязательный ТЦ Регресс ---
        if not has_regress_cycle:
            self._log_issue(
                release_key, "error",
                "Отсутствует ТЦ с 'Регресс'/'Regress' в названии"
            )

        # --- Правило 4: если есть Web/ВЭБ — должны быть PWA и IPAD ---
        if has_web_cycle:
            if not has_pwa_cycle:
                self._log_issue(
                    release_key, "error",
                    "Есть ТЦ с 'Web'/'ВЭБ', но отсутствует ТЦ с 'PWA' в названии"
                )
            if not has_ipad_cycle:
                self._log_issue(
                    release_key, "error",
                    "Есть ТЦ с 'Web'/'ВЭБ', но отсутствует ТЦ с 'IPAD' в названии"
                )

        print(f"   ТЦ: Регресс={'✓' if has_regress_cycle else '✗'}, "
              f"НФ={'✓' if has_nf_cycle else ('—' if not nf_story_keys else '✗')}, "
              f"Web={'✓' if has_web_cycle else '—'}, "
              f"PWA={'✓' if has_pwa_cycle else ('—' if not has_web_cycle else '✗')}, "
              f"IPAD={'✓' if has_ipad_cycle else ('—' if not has_web_cycle else '✗')}")

    def _manage_jira_comment(self, release_key, is_success):
        if getattr(self, 'dry_run', False):
            preview_path = os.getenv('RELEASE_CHECKER_COMMENT_PREVIEW', '').strip()
            if preview_path:
                Path(preview_path).write_text(self._release_comment(release_key, is_success), encoding='utf-8')
                print(f"[DRY RUN] Jira wiki preview: {preview_path}", flush=True)
            print("[DRY RUN] Комментарий Jira не публикуется", flush=True)
            return
        if not self.my_account_id:
            return

        print(f"\n💬 Управление комментариями для {release_key}...")

        try:
            comments = self.jira_main.comments(release_key)
            is_non_editable = False
            for comment in comments:
                if is_non_editable:
                    break
                author = comment.author
                current_author_id = getattr(author, 'accountId', getattr(author, 'name', ''))
                if current_author_id == self.my_account_id:
                    if ("Автоматическая проверка релиза" in comment.body
                            or "Результат проверки" in comment.body
                            or comment.body.startswith(CHECK_TABLE_HEADER)
                            or LEGACY_COMMENT_TABLE_HEADER in comment.body
                            or COMMENT_TABLE_HEADER in comment.body):
                        try:
                            comment.delete()
                        except Exception as del_err:
                            if "non-editable workflow state" in str(del_err):
                                print(f"   ⚠️ Задача в финальном статусе — старые комментарии не удалить")
                                is_non_editable = True
                            else:
                                print(f"   ⚠️ Не удалось удалить комментарий {comment.id}: {del_err}")
        except Exception as e:
            print(f"   ⚠️ Ошибка при попытке удалить комментарии: {e}")

        comment_body = self._release_comment(release_key, is_success)

        try:
            print("   📝 Публикую новый комментарий...")
            self.jira_main.add_comment(release_key, comment_body)
            print("   ✅ Комментарий опубликован.")
        except Exception as e:
            print(f"   ❌ Ошибка публикации комментария: {e}")

    def _check_test_subtask(self, release):
        test_subtask = None
        for subtask in release.fields.subtasks:
            if 'тестирование' in subtask.fields.summary.lower():
                test_subtask = subtask
                break

        if not test_subtask:
            self._log_issue(release, "error", "Сабтаска с 'Тестирование' не найдена")
            return

        full_subtask = self.jira_main.issue(test_subtask.key)
        worklogs = self.jira_main.worklogs(full_subtask.key)

        if not worklogs:
            self._log_issue(full_subtask, "error", "Нет затреканного времени")
        else:
            total_time = sum(wl.timeSpentSeconds for wl in worklogs)
            self._log_issue(full_subtask, "success", f"Затрекано {total_time / 3600:.2f}ч")

    def _get_author_name(self, author_obj: object) -> str:
        """Нормализует автора Jira/Zephyr объекта к строковому имени."""
        if hasattr(author_obj, 'displayName'):
            return author_obj.displayName
        elif hasattr(author_obj, 'name'):
            return author_obj.name
        return str(author_obj)

    @staticmethod
    def _issue_project_key(issue: object) -> str:
        issue_key = str(getattr(issue, 'key', '') or '').strip().upper()
        if '-' in issue_key:
            return issue_key.split('-', 1)[0]
        return ''

    @staticmethod
    def _is_worklog_exempt_comment_author(author_name: str, project_key: str) -> bool:
        if project_key.strip().upper() in WORKLOG_EXEMPT_EXCLUDED_PROJECTS:
            return False
        author_normalized = normalize_field_text(author_name).casefold()
        return author_normalized in {
            normalize_field_text(author).casefold()
            for author in WORKLOG_EXEMPT_COMMENT_AUTHORS
        }

    def _get_tester_worklog_authors(self, issue: object) -> set[str]:
        """Возвращает множество тестировщиков со списанием времени > 0 в задаче."""
        authors: set[str] = set()
        worklogs = self.jira_main.worklogs(getattr(issue, 'key', issue))

        for wl in worklogs:
            author_name = self._get_author_name(wl.author)
            if is_allowed_tester(author_name) and wl.timeSpentSeconds > 0:
                authors.add(author_name)
        return authors

    def _get_linked_testing_tasks(self, issue: object, errors: Optional[list[str]] = None) -> list[str]:
        """
        Возвращает ключи связанных Task с признаком тестирования.
        Нужны для кейса, когда время списано не в Story, а в тестовой Task.
        """
        task_keys: list[str] = []
        if not hasattr(issue.fields, 'issuelinks') or not issue.fields.issuelinks:
            return task_keys

        for link in issue.fields.issuelinks:
            linked_issue = getattr(link, 'outwardIssue', None) or getattr(link, 'inwardIssue', None)
            if not linked_issue:
                continue
            try:
                linked_full = self.jira_main.issue(linked_issue.key, fields='summary,issuetype')
            except Exception as exc:
                self._log_issue(issue, "warning", f"Не удалось проверить связанную задачу {linked_issue.key}: {exc}")
                if errors is not None:
                    errors.append(linked_issue.key)
                continue

            issue_type = linked_full.fields.issuetype.name.lower() if linked_full.fields.issuetype else ''
            if issue_type != 'task':
                continue
            summary = (linked_full.fields.summary or '').lower()
            if 'тестирован' in summary:
                task_keys.append(linked_full.key)
        return task_keys

    def _extract_consist_of_issues(self, release) -> Optional[list[str]]:
        if not hasattr(release.fields, 'issuelinks'):
            return None
        consist_of_issues = []
        for link in release.fields.issuelinks or []:
            link_type_name = link.type.name.lower() if hasattr(link.type, 'name') else ''
            if 'consist' in link_type_name or 'part' in link_type_name:
                if hasattr(link, 'outwardIssue'):
                    consist_of_issues.append(link.outwardIssue.key)
                elif hasattr(link, 'inwardIssue'):
                    consist_of_issues.append(link.inwardIssue.key)
        return list(dict.fromkeys(consist_of_issues))

    def _get_consist_of_issues(self, release_key):
        if release_key in self._consist_of_cache:
            return list(self._consist_of_cache[release_key])
        try:
            release = self.jira_main.issue(release_key, fields='issuelinks')
            consist_of_issues = self._extract_consist_of_issues(release) or []
            self._consist_of_cache[release_key] = consist_of_issues
            return list(consist_of_issues)
        except Exception as e:
            self._log_issue("GENERAL", "error", f"Ошибка получения связей: {str(e)}")
            return []

    def _check_artifacts(self, release_key: str) -> None:
        """
        Проверяет артефакты релиза:
          1) есть комментарии от тестировщиков;
          2) у каждого тестировщика из комментариев есть списание времени.
        Для Story время может быть как в самой Story, так и в связанной тестовой Task.
        """
        artifact_keys = self._get_consist_of_issues(release_key)
        if not artifact_keys:
            self._log_issue("GENERAL", "error", "Нет задач со связью 'consist of' / 'is part of'")
            return

        for artifact_key in artifact_keys:
            artifact = artifact_key
            metadata_ok = True
            try:
                artifact = self.jira_main.issue(artifact_key)
            except Exception as exc:
                metadata_ok = False
                self._log_issue(artifact_key, "warning", f"Не удалось получить данные задачи для проверки связанных списаний: {exc}")

            comment_authors: set[str] = set()
            required_authors: set[str] = set()
            comments_ok = True
            try:
                comments = self.jira_main.comments(artifact_key)
                for comment in comments:
                    author = self._get_author_name(comment.author)
                    exempt = self._is_worklog_exempt_comment_author(author, artifact_key.split('-')[0])
                    if is_allowed_tester(author) or exempt:
                        comment_authors.add(author)
                    if is_allowed_tester(author) and not exempt:
                        required_authors.add(author)
                if comment_authors:
                    self._log_issue(artifact, "success", f"Есть комментарии от: {', '.join(sorted(comment_authors))}")
                else:
                    self._log_issue(artifact, "error", "Нет комментариев от тестировщика")
            except Exception as exc:
                comments_ok = False
                self._log_issue(artifact, "warning", f"Не удалось проверить комментарии тестировщика: {exc}")

            # Независимо от комментариев и PR читаем все доступные списания.
            worklog_authors: set[str] = set()
            worklogs_ok = metadata_ok
            sources = [artifact]
            if metadata_ok and getattr(artifact.fields.issuetype, 'name', '').lower() == 'story':
                try:
                    discovery_errors: list[str] = []
                    sources.extend(self._get_linked_testing_tasks(artifact, errors=discovery_errors))
                    worklogs_ok = worklogs_ok and not discovery_errors
                except Exception as exc:
                    worklogs_ok = False
                    self._log_issue(artifact, "warning", f"Не удалось проверить связанные тестовые Task: {exc}")
            for source in sources:
                try:
                    worklog_authors.update(self._get_tester_worklog_authors(source))
                except Exception as exc:
                    worklogs_ok = False
                    self._log_issue(artifact, "warning", f"Не удалось получить списанное время из {getattr(source, 'key', source)}: {exc}")

            missing = sorted(required_authors - worklog_authors)
            if comments_ok and comment_authors and not missing:
                self._log_issue(artifact, "success", "У всех тестировщиков с комментариями, для которых требуется worklog, есть списанное время ✓")
            elif missing and worklogs_ok:
                self._log_issue(artifact, "error", "Нет затреканного времени от тестировщиков с комментариями: " + ", ".join(missing))
            elif missing:
                self._log_issue(artifact, "warning", "Не удалось подтвердить списанное время: " + ", ".join(missing))
            elif worklog_authors:
                self._log_issue(artifact, "success", "Есть списанное время от: " + ", ".join(sorted(worklog_authors)))
            elif worklogs_ok:
                self._log_issue(artifact, "error", "Нет затреканного времени от тестировщика")

    def _check_release_coverage(self, release_key):
        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            return
        for key in linked_keys:
            if key == release_key:
                continue
            cases, error = self._get_issue_test_cases_cached(key)
            if error is not None:
                self._log_issue(key, 'error', f'Zephyr: не удалось проверить покрытие ({error})')
                continue
            confirmed = []
            unresolved = []
            for case in cases:
                tc_key = self.zephyr.get_test_case_key(case)
                if not tc_key.startswith(ZEPHYR_TC_PROJECT_KEY + '-T'):
                    continue
                details = None
                if not self._is_directly_linked_test_case_for_issue(key, case, None):
                    details = self._get_test_case_details_cached(tc_key)
                if self._is_directly_linked_test_case_for_issue(key, case, details):
                    confirmed.append(tc_key)
                elif not details or not any(f in details or f in case for f in ('issueLinks', 'links', 'issues')):
                    unresolved.append(tc_key)
            if confirmed:
                self._log_issue(key, 'success', 'Покрытие Zephyr: ' + ', '.join(sorted(set(confirmed))))
            elif unresolved:
                self._log_issue(key, 'error', 'Zephyr: не удалось подтвердить прямую связь с ТК: ' + ', '.join(unresolved))
            else:
                self._log_issue(key, "error", "Отсутствует тестовое покрытие (нет прилинкованных ТК)")

    @staticmethod
    def _parse_jira_datetime(ts_str: str):
        from datetime import datetime

        if not ts_str:
            return None
        try:
            return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        except Exception:
            try:
                return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S.%f%z")
            except Exception:
                return None

    @staticmethod
    def _calc_status_days(changelog, monitored_statuses: list[str]) -> float:
        """
        По ченщикам changelog вычисляет суммарное время (в днях) нахождения
        задачи в статусах из monitored_statuses (регистронезависимо).
        """
        from datetime import datetime, timezone

        monitored_lower = {s.lower() for s in monitored_statuses}

        # Собираем все переходы статуса в хронологическом порядке
        transitions: list[tuple[datetime, str]] = []
        for history in changelog.histories:
            for item in history.items:
                if item.field == 'status':
                    ts_str = history.created  # формат "2024-04-01T12:34:56.000+0300"
                    ts = ReleaseValidator._parse_jira_datetime(ts_str)
                    if ts is None:
                        continue
                    new_status = (item.toString or '').lower()
                    transitions.append((ts, new_status))

        # Сортируем по времени
        transitions.sort(key=lambda x: x[0])

        total_seconds = 0.0
        for i, (ts, new_status) in enumerate(transitions):
            if new_status not in monitored_lower:
                continue
            # Время выхода из этого статуса = момент следующего перехода
            if i + 1 < len(transitions):
                exit_ts = transitions[i + 1][0]
            else:
                # Текущий статус — считаем до сейчас
                exit_ts = datetime.now(timezone.utc)
            total_seconds += (exit_ts - ts).total_seconds()

        return total_seconds / 86400.0

    # Целевое суммарное время 2026 (в днях) нахождения Story
    # в статусах тестирования по проектной области.
    STORY_MONITORED_STATUSES = ["READY FOR IFT", "IFT", "READY FOR UAT", "UAT"]
    STORY_MAX_TESTING_DAYS: dict[str, float] = {
        "HRM":        6.0,
        "HRC":        6.0,
        "NEUROUI":    5.5,
        "SFILE":      6.0,
        "HRPPA":      3.6,
        "PERFREVIEW": 5.2,
        "HRPASSIST": 10.0,
    }
    STORY_MAX_TESTING_RATIO = 1.15

    def _extract_story_requirements_rows(self, raw_val: object) -> dict[str, dict]:
        field_value = extract_jira_field_value(raw_val)
        if not field_value:
            return {}

        rows_by_label = {}
        parser = JiraTableFieldParser()
        try:
            parser.feed(field_value)
        except Exception:
            pass

        for row in parser.rows:
            if len(row) < 2:
                continue
            label = normalize_field_text(row[0]['text']).casefold()
            if label:
                rows_by_label[label] = row[1]

        if rows_by_label:
            return rows_by_label

        return self._extract_story_requirements_rows_from_text(field_value)

    def _extract_story_requirements_rows_from_text(self, field_value: str) -> dict[str, dict]:
        labels = ('Бизнес-требования', 'Функциональное решение', 'Архитектура')
        text = normalize_field_text(field_value)
        text_casefolded = text.casefold()
        positions = []

        for label in labels:
            idx = text_casefolded.find(label.casefold())
            if idx >= 0:
                positions.append((idx, label))

        rows_by_label = {}
        positions.sort()

        for index, (start, label) in enumerate(positions):
            value_start = start + len(label)
            value_end = positions[index + 1][0] if index + 1 < len(positions) else len(text)
            value_fragment = normalize_field_text(text[value_start:value_end])
            links = re.findall(r'https?://[^\s\])"]+', value_fragment)
            value_text = normalize_field_text(re.sub(r'https?://[^\s\])"]+', '', value_fragment))

            for possible_value in ('Зафиксировано', 'Не меняется', 'Утверждена'):
                possible_value_normalized = possible_value.casefold()
                if possible_value_normalized in value_text.casefold():
                    value_text = possible_value
                    break

            rows_by_label[label.casefold()] = {
                'text': value_text,
                'links': links,
            }

        return rows_by_label

    def _check_story_requirements_block(self, story) -> bool:
        REQUIREMENTS_FIELD = 'customfield_21100'
        ARCHITECTURE_IMPACT_FIELD = 'customfield_23700'
        ARCHITECTURE_IMPACT_FIELD_NAME = 'Архитектурные изменения'
        ARCHITECTURE_NO_CHANGE = 'не меняется'
        ARCHITECTURE_APPROVED = 'утверждена'
        ARCHITECTURE_NO_IMPACT = 'не влияет на архитектуру'
        ARCHITECTURE_IMPACT_ALLOWED_VALUES = {
            'не влияет на архитектуру',
            'это первое внедрение для кэ',
            'в результате доработки этот кэ будет отправлен в архив или выведен',
            'в результате доработки зависимый кэ будет отправлен в архив или выведен',
            'реализуется новая интеграция с другой ас',
            'реализуется выгрузка данных на фир',
            'реализуется загрузка данных с фир',
            'реализуется механизм получения и обработки электронных писем',
            'реализуется механизм отправки электронных писем',
            'осуществляется миграция на другой тех.стек',
            'создается или изменяется dblink',
            'создание или расширение реплики данных внутри ас или в другую ас',
            'реализуется новый ui/ арм',
            'один из компонентов кэ будет размещен в отличном от текущего сегменте сети',
            'создается новый rpa-алгоритм (робот)',
            'вносятся изменения в интерфейсы, с которыми работают rpa',
            'доработки привели к нарушению одного из архитектурных стандартов',
            'после внедрения ожидается закрытие ранее выставленного атд',
            'вносятся изменения в существующую интеграцию с другой ас',
            'к существующем сервису подключается новый потребитель',
            'реализуется возможность выгрузки данных/отчетов на рабочую станцию пользователя',
            'вносятся изменения в существующий процесс выгрузки данных/отчетов на рабочую станцию пользователя',
            'создание новых хранимых процедур или триггеров в бд',
            'реализуется новая схема данных в бд или отдельный инстанс бд',
            'вносятся изменения в существующую интеграцию внутри одной ас',
            'создается новая интеграция внутри одной ас',
            'создается или изменяется api',
        }

        rows = self._extract_story_requirements_rows(getattr(story.fields, REQUIREMENTS_FIELD, None))
        architecture_impact_values = extract_jira_field_values(getattr(story.fields, ARCHITECTURE_IMPACT_FIELD, None))
        architecture_impact_display = ', '.join(architecture_impact_values) if architecture_impact_values else 'None'
        architecture_impact_normalized_values = {
            normalize_field_text(value).casefold()
            for value in architecture_impact_values
            if normalize_field_text(value)
        }

        def get_row(label: str) -> Optional[dict]:
            return rows.get(label.casefold())

        block_is_valid = True

        def check_fixed_with_link(label: str) -> bool:
            row = get_row(label)
            if not row:
                self._log_issue(story, "error", f"Story: в {REQUIREMENTS_FIELD} не найдена строка '{label}'")
                return False

            row_text = normalize_field_text(row['text']).casefold()
            if row_text != 'зафиксировано':
                self._log_issue(
                    story, "error",
                    f"Story: {label} = '{row['text']}', ожидается 'Зафиксировано'"
                )
                return False

            if not row['links']:
                self._log_issue(story, "error", f"Story: {label} = 'Зафиксировано', но ссылка не указана")
                return False

            self._log_issue(story, "success", f"Story: {label} зафиксированы, ссылка есть ✓")
            return True

        if not check_fixed_with_link('Бизнес-требования'):
            block_is_valid = False
        if not check_fixed_with_link('Функциональное решение'):
            block_is_valid = False

        architecture_row = get_row('Архитектура')
        if not architecture_row:
            self._log_issue(story, "error", f"Story: в {REQUIREMENTS_FIELD} не найдена строка 'Архитектура'")
            return False

        architecture_status = normalize_field_text(architecture_row['text']).casefold()
        if architecture_status == ARCHITECTURE_NO_CHANGE:
            if architecture_impact_normalized_values != {ARCHITECTURE_NO_IMPACT}:
                block_is_valid = False
                self._log_issue(
                    story, "error",
                    f"Story: Архитектура = 'Не меняется', но поле '{ARCHITECTURE_IMPACT_FIELD_NAME}' = "
                    f"'{architecture_impact_display}'. Ожидается 'Не влияет на архитектуру'"
                )
            else:
                self._log_issue(
                    story, "success",
                    "Story: Архитектура не меняется, влияние на архитектуру заполнено корректно ✓"
                )
        elif architecture_status == ARCHITECTURE_APPROVED:
            if not architecture_row['links']:
                block_is_valid = False
                self._log_issue(story, "error", "Story: Архитектура = 'Утверждена', но ссылка не указана")
            elif not architecture_impact_normalized_values:
                block_is_valid = False
                self._log_issue(
                    story, "error",
                    f"Story: Архитектура = 'Утверждена', но поле '{ARCHITECTURE_IMPACT_FIELD_NAME}' = "
                    f"'{architecture_impact_display}'. Ожидается одно или несколько архитектурных изменений"
                )
            elif ARCHITECTURE_NO_IMPACT in architecture_impact_normalized_values:
                block_is_valid = False
                self._log_issue(
                    story, "error",
                    f"Story: Архитектура = 'Утверждена', но поле '{ARCHITECTURE_IMPACT_FIELD_NAME}' = "
                    f"'{architecture_impact_display}'. Значение 'Не влияет на архитектуру' допустимо только "
                    "для статуса 'Не меняется'"
                )
            elif not architecture_impact_normalized_values.issubset(ARCHITECTURE_IMPACT_ALLOWED_VALUES):
                block_is_valid = False
                unexpected_values = sorted(architecture_impact_normalized_values - ARCHITECTURE_IMPACT_ALLOWED_VALUES)
                self._log_issue(
                    story, "error",
                    f"Story: Архитектура = 'Утверждена', но поле '{ARCHITECTURE_IMPACT_FIELD_NAME}' = "
                    f"'{architecture_impact_display}' содержит недопустимые значения: "
                    f"{', '.join(unexpected_values)}"
                )
            else:
                self._log_issue(
                    story, "success",
                    "Story: Архитектура утверждена, ссылка и влияние на архитектуру корректны ✓"
                )
        else:
            block_is_valid = False
            self._log_issue(
                story, "error",
                f"Story: Архитектура = '{architecture_row['text']}', ожидается 'Не меняется' или 'Утверждена'"
            )

        return block_is_valid

    def _check_story_requirements_timing(self, story, changelog) -> None:
        REQUIREMENTS_FIELD = 'customfield_21100'
        ARCHITECTURE_IMPACT_FIELD = 'customfield_23700'
        ARCHITECTURE_IMPACT_FIELD_NAME = 'Архитектурные изменения'
        late_statuses = {'ready for uat', 'uat', 'done'}
        tracked_fields = {
            REQUIREMENTS_FIELD: 'Бизнес-требования / Функциональное решение / Архитектура',
            ARCHITECTURE_IMPACT_FIELD: ARCHITECTURE_IMPACT_FIELD_NAME,
        }

        first_late_status_at = None
        late_status_name = ''
        field_changes_after_late_status: list[tuple[str, object]] = []

        histories = sorted(
            getattr(changelog, 'histories', []) or [],
            key=lambda history: getattr(history, 'created', '') or ''
        )

        for history in histories:
            changed_at = self._parse_jira_datetime(getattr(history, 'created', ''))
            if changed_at is None:
                continue

            for item in getattr(history, 'items', []) or []:
                item_field = normalize_field_text(getattr(item, 'field', ''))
                item_field_id = normalize_field_text(getattr(item, 'fieldId', ''))
                if item_field == 'status':
                    new_status = normalize_status_name(getattr(item, 'toString', ''))
                    if new_status in late_statuses and first_late_status_at is None:
                        first_late_status_at = changed_at
                        late_status_name = getattr(item, 'toString', '') or new_status
                    continue

                if first_late_status_at is None or changed_at <= first_late_status_at:
                    continue

                for field_id, field_name in tracked_fields.items():
                    if item_field_id == field_id or item_field == field_id or item_field.casefold() == field_name.casefold():
                        field_changes_after_late_status.append((field_name, changed_at))

        if first_late_status_at is None:
            return

        seen_messages = set()
        for field_name, changed_at in field_changes_after_late_status:
            message_key = (field_name, changed_at)
            if message_key in seen_messages:
                continue
            seen_messages.add(message_key)
            self._log_issue(
                story,
                "error",
                f"Story: поле '{field_name}' было изменено после перехода в '{late_status_name}' "
                f"({changed_at.strftime('%Y-%m-%d %H:%M')}). Требования и архитектура должны быть привязаны до UAT/Done"
            )

    def _check_story_architecture_subtask(self, story) -> None:
        """Проверяем именно автора закрытия, а не текущего исполнителя."""
        try:
            subtasks = getattr(story.fields, 'subtasks', None)
            if subtasks is None:
                subtasks = self.jira_main.issue(story.key, fields='subtasks').fields.subtasks
            candidates = [task for task in subtasks
                          if normalize_field_text(task.fields.summary).casefold() == 'анализ архитектуры']
            if not candidates:
                self._log_issue(story, "error", "Story: отсутствует sub-task 'Анализ архитектуры'")
                return
            failures = []
            allowed = {normalize_field_text(name).casefold() for name in ALLOWED_ARCHITECTURE_CLOSERS}
            for task in candidates:
                try:
                    full = self.jira_main.issue(task.key, expand='changelog')
                    status = full.fields.status
                    current = normalize_status_name(status.name)
                    if current not in ARCHITECTURE_CLOSED_STATUSES:
                        failures.append(f"{task.key}: не закрыта (статус '{status.name}')")
                        continue
                    changelog = full.changelog
                    histories = list(getattr(changelog, 'histories', []) or [])
                    # Не принимаем решение по обрезанной истории: в ней может не быть повторного закрытия.
                    if getattr(changelog, 'total', len(histories)) > len(histories):
                        raise ValueError(f"{task.key}: Jira вернула неполную историю закрытия")
                    transitions = [(history, item) for history in histories for item in history.items
                                   if getattr(item, 'field', '').casefold() == 'status']
                    transitions.sort(key=lambda pair: self._parse_jira_datetime(pair[0].created))
                    if not transitions:
                        failures.append(f"{task.key}: в истории нет автора закрытия")
                        continue
                    history, transition = transitions[-1]
                    author = self._get_author_name(history.author)
                    if (normalize_status_name(getattr(transition, 'toString', '')) == current
                            and normalize_field_text(author).casefold() in allowed):
                        self._log_issue(story, "success", f"Story: sub-task 'Анализ архитектуры' [{task.key}] закрыта: {author} ✓")
                        return
                    failures.append(f"{task.key}: закрытие не подтверждено разрешенным автором (последний переход: {author})")
                except Exception as exc:
                    failures.append(f"{task.key}: не удалось проверить закрытие: {exc}")
            self._log_issue(story, "error", "Story: sub-task 'Анализ архитектуры': " + '; '.join(failures))
        except Exception as exc:
            self._log_issue(story, "error", f"Story: не удалось проверить sub-task 'Анализ архитектуры': {exc}")

    def _run_story_check(self, story, name, callback):
        """Ошибка одной проверки не отменяет остальные проверки Story."""
        try:
            return callback()
        except Exception as exc:
            self._log_issue(story, "warning", f"Story: не удалось выполнить проверку '{name}': {exc}")
            return None

    def _check_stories(self, release_key: str) -> None:
        """Проверка Story: описание + обязательные поля + Epic Link + время в тест-статусах + Task внутри Story"""
        STORY_FIELDS = {
            'customfield_24000': 'Новая функциональность',
            'customfield_18400': 'Требуется НТ',
        }
        # Epic Link: customfield_10006
        EPIC_LINK_FIELD = 'customfield_10006'
        REQUIREMENTS_FIELD = 'customfield_21100'
        ARCHITECTURE_IMPACT_FIELD = 'customfield_23700'
        CLOSED_STATUSES = {'closed', 'закрыта', 'закрыто', 'resolved', 'решена', 'решено', 'cancelled', 'отменён', 'отменен'}
        CORRUPTED_STORY_STATUSES = {'ready for uat', 'uat', 'done'}

        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            return

        keys_str = ",".join(linked_keys)
        fields_req = (
            f"summary,description,issuetype,status,assignee,labels,issuelinks,subtasks,"
            f"{','.join(STORY_FIELDS.keys())},{EPIC_LINK_FIELD},"
            f"{REQUIREMENTS_FIELD},{ARCHITECTURE_IMPACT_FIELD}"
        )

        try:
            story_issues = self.jira_main.search_issues(
                f'key in ({keys_str}) AND issuetype = Story',
                fields=fields_req,
                maxResults=False
            )
        except Exception as e:
            self._log_issue("GENERAL", "error", f"Ошибка поиска Story задач: {e}")
            return

        if not story_issues:
            print("   ℹ️ Story задачи не найдены в составе релиза")
            return

        print(f"   Проверка {len(story_issues)} Story задач...")

        for story in story_issues:
            story_project = story.key.split('-')[0]
            self._run_story_check(story, 'Анализ архитектуры', lambda: self._check_story_architecture_subtask(story))

            # --- 0. Проверка обязательного лейбла контура ---
            self._run_story_check(story, 'Контур', lambda: self._check_required_platform_label(story, "Story"))

            # --- 1. Проверка описания Story ---
            description = getattr(story.fields, 'description', None)
            if description is None or (isinstance(description, str) and not description.strip()):
                self._log_issue(story, "error", "Story: отсутствует описание")
            else:
                self._log_issue(story, "success", "Story: описание заполнено ✓")

            # --- 2. Проверка полей customfield_24000 и customfield_18400 ---
            for field_id, field_name in STORY_FIELDS.items():
                raw_val = getattr(story.fields, field_id, None)

                if raw_val is None:
                    field_value = None
                elif hasattr(raw_val, 'value'):
                    field_value = raw_val.value
                elif isinstance(raw_val, dict):
                    field_value = raw_val.get('value')
                elif isinstance(raw_val, str):
                    field_value = raw_val.strip()
                else:
                    field_value = str(raw_val).strip()

                normalized = field_value.strip().lower() if field_value else None

                if normalized is None:
                    self._log_issue(
                        story, "error",
                        f"Story: Поле '{field_name}' ({field_id}) не заполнено"
                    )
                elif normalized not in ('да', 'нет'):
                    self._log_issue(
                        story, "error",
                        f"Story: Поле '{field_name}' ({field_id}) содержит недопустимое значение '{field_value}'. Допустимо: 'Да' / 'Нет'"
                    )
                else:
                    self._log_issue(story, "success", f"Story: {field_name} = {field_value} ✓")

            # --- 2. Проверка Epic Link у Story ---
            epic_link = getattr(story.fields, EPIC_LINK_FIELD, None)
            if not epic_link:
                self._log_issue(
                    story, "error",
                    f"Story: не заполнен Epic Link ({EPIC_LINK_FIELD})"
                )
            else:
                self._log_issue(story, "success", f"Story: Epic Link = {epic_link} ✓")

            story_full = None
            try:
                story_full = self.jira_main.issue(story.key, expand='changelog')
            except Exception as e:
                self._log_issue(
                    story,
                    "warning",
                    f"Story: не удалось получить changelog для проверки порядка привязки требований: {e}"
                )

            # --- 3. Проверка блока требований и архитектуры ---
            requirements_block_is_valid = self._run_story_check(story, 'Требования', lambda: self._check_story_requirements_block(story))
            if story_full is not None:
                self._run_story_check(story, 'Порядок привязки требований', lambda: self._check_story_requirements_timing(story, story_full.changelog))
            if requirements_block_is_valid is False:
                story_status = story.fields.status.name if getattr(story.fields, 'status', None) else ''
                if normalize_status_name(story_status) in CORRUPTED_STORY_STATUSES:
                    self._log_issue(
                        story, "error",
                        f"Story испорчена и влияет на метрику модели зрелости: "
                        f"проверка блока требований/архитектуры не выполнена, текущий статус '{story_status}'"
                    )

            # --- 4. Проверка времени в тест-статусах ---
            target_days = self.STORY_MAX_TESTING_DAYS.get(story_project)
            if target_days is not None:
                max_days = round(target_days * self.STORY_MAX_TESTING_RATIO, 10)
                max_percent = round(self.STORY_MAX_TESTING_RATIO * 100)
                try:
                    if story_full is None:
                        story_full = self.jira_main.issue(story.key, expand='changelog')
                    actual_days = self._calc_status_days(
                        story_full.changelog,
                        self.STORY_MONITORED_STATUSES
                    )
                    actual_days_rounded = round(actual_days, 2)
                    max_days_rounded = round(max_days, 2)
                    target_days_rounded = round(target_days, 2)
                    if actual_days > max_days:
                        self._log_issue(
                            story, "warning",
                            f"Story: суммарное время в тест-статусах "
                            f"({actual_days_rounded} д.) больше {max_percent}% норматива "
                            f"({max_days_rounded} д. из {target_days_rounded} д.) "
                            f"для проектной области {story_project}"
                        )
                    else:
                        self._log_issue(
                            story, "success",
                            f"Story: время в тест-статусах {actual_days_rounded} д. "
                            f"не превышает {max_percent}% норматива ({max_days_rounded} д.) ✓"
                        )
                except Exception as e:
                    self._log_issue(
                        story, "warning",
                        f"Story: не удалось получить changelog для проверки времени в статусах: {e}"
                    )

            # --- 5. Проверка Task внутри Story через связь "consists of" ---
            task_keys = []
            if hasattr(story.fields, 'issuelinks') and story.fields.issuelinks:
                for link in story.fields.issuelinks:
                    link_type = link.type.name.lower() if hasattr(link.type, 'name') else ''
                    if 'consist' in link_type or 'part' in link_type:
                        linked_issue = getattr(link, 'outwardIssue', None) or getattr(link, 'inwardIssue', None)
                        if linked_issue:
                            task_keys.append(linked_issue.key)

            if not task_keys:
                continue

            print(f"   Проверка {len(task_keys)} Task задач внутри {story.key}...")

            for task_key in task_keys:
                try:
                    task = self.jira_main.issue(
                        task_key,
                        fields=f'summary,issuetype,status,assignee,{EPIC_LINK_FIELD}'
                    )

                    if task.fields.issuetype.name.lower() != 'task':
                        continue

                    # --- 3а. Статус Task ---
                    status_name = task.fields.status.name if task.fields.status else ''
                    if status_name.lower() not in CLOSED_STATUSES:
                        self._log_issue(
                            task, "error",
                            f"Task внутри Story [{story.key}] не закрыта. Текущий статус: '{status_name}'"
                        )
                    else:
                        self._log_issue(
                            task, "success",
                            f"Task внутри Story [{story.key}] закрыта ✓"
                        )

                    # --- 3б. Epic Link у Task (только если та же проектная область) ---
                    task_project = task.key.split('-')[0]
                    if task_project == story_project:
                        task_epic = getattr(task.fields, EPIC_LINK_FIELD, None)
                        if not task_epic:
                            self._log_issue(
                                task, "error",
                                f"Task внутри Story [{story.key}]: не заполнен Epic Link ({EPIC_LINK_FIELD})"
                            )
                        else:
                            self._log_issue(
                                task, "success",
                                f"Task внутри Story [{story.key}]: Epic Link = {task_epic} ✓"
                            )

                except Exception as e:
                    self._log_issue(task_key, "error", f"Ошибка проверки Task {task_key}: {e}")

    @staticmethod
    def _normalize_gigacode_marker_text(value: str) -> str:
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', value)
        text = re.sub(r'<([^>]+)>', r'\1', text)
        return normalize_field_text(text)

    def _find_gigacode_marker(self, value: object) -> Optional[str]:
        marker_patterns = (
            r'co-authored-by\s*:\s*gigacode\s+assistant\b',
            r'co-authored-by\s*:\s*gigacode\b',
            r'co-authorer-by\s*:\s*gigacode\s+assistant\b',
            r'co-authorer-by\s*:\s*gigacode\b',
            r'#\s*gigacode\s+agent\b',
        )
        if isinstance(value, str):
            value_normalized = self._normalize_gigacode_marker_text(value)
            matches = []
            match_spans = []
            for pattern in marker_patterns:
                match = re.search(pattern, value_normalized, flags=re.IGNORECASE)
                if match:
                    span = match.span()
                    if any(span[0] >= existing[0] and span[1] <= existing[1] for existing in match_spans):
                        continue
                    match_spans.append(span)
                    matches.append(match.group(0))
            return ', '.join(dict.fromkeys(matches)) if matches else None
        if isinstance(value, dict):
            markers = []
            for item in value.values():
                marker = self._find_gigacode_marker(item)
                if marker:
                    markers.extend(part.strip() for part in marker.split(',') if part.strip())
            return ', '.join(dict.fromkeys(markers)) if markers else None
        if isinstance(value, list):
            markers = []
            for item in value:
                marker = self._find_gigacode_marker(item)
                if marker:
                    markers.extend(part.strip() for part in marker.split(',') if part.strip())
            return ', '.join(dict.fromkeys(markers)) if markers else None
        return None

    @staticmethod
    def _is_pull_request_key(key: object) -> bool:
        key_compact = re.sub(r'[^a-zа-я0-9]+', '', str(key).casefold())
        return key_compact in {
            'pullrequest',
            'pullrequests',
            'pullrequestid',
            'pullrequesturl',
            'pullrequeststatus',
            'pullrequeststate',
            'pullrequestname',
            'pullrequesttitle',
        } or key_compact.startswith('pullrequest')

    @staticmethod
    def _is_pull_request_url(value: object) -> bool:
        text = str(value or '').casefold()
        return bool(
            re.search(r'/(pull-requests|pullrequests|pull)/\d+(?:\b|[/?#])', text)
        )

    def _pull_request_evidence_from_mapping(self, value: dict) -> Optional[str]:
        pr_id = (
            value.get('id')
            or value.get('pullRequestId')
            or value.get('pullrequestId')
            or value.get('number')
            or value.get('displayId')
            or value.get('key')
        )
        title = value.get('title') or value.get('name') or value.get('summary')
        url = value.get('url') or value.get('link') or value.get('href') or value.get('self')

        if url and self._is_pull_request_url(url):
            return str(url)
        if pr_id:
            return f"PR {pr_id}"
        if title:
            return str(title)
        return None

    def _extract_pull_request_evidence(
        self, value: object, *, in_pull_request_context: bool = False,
    ) -> Optional[str]:
        """Recognize PR objects, positive counters, JSON fields and explicit PR URLs."""
        if isinstance(value, str):
            text = html.unescape(value).strip()
            if text.startswith(('{', '[')):
                try:
                    return self._extract_pull_request_evidence(
                        json.loads(text), in_pull_request_context=in_pull_request_context)
                except ValueError:
                    pass
            # Jira Server may serialize Development as a Java map, not JSON.
            count = re.search(r'\bpullrequests?\s*=\s*\{[^{}]*\b(?:stateCount|count)\s*=\s*([1-9]\d*)\b', text, re.I)
            if count:
                return f"PR count={count.group(1)}"
            match = re.search(r'https?://[^\s<>"\[\]{}]+/(?:pull-requests|pullrequests|pull|merge_requests)/\d+\b[^\s<>"\[\]{}]*', text, re.I)
            return match.group(0) if match else None
        if isinstance(value, list):
            for item in value:
                evidence = self._extract_pull_request_evidence(item, in_pull_request_context=in_pull_request_context)
                if evidence:
                    return evidence
        elif isinstance(value, dict):
            if in_pull_request_context:
                for counter in ('count', 'total', 'overall', 'stateCount', 'open', 'merged', 'declined'):
                    count = value.get(counter)
                    if isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0:
                        return f"PR {counter}={count}"
                # Provider/repository metadata is not a PR object.
                if not any(key in value for key in ('pullRequests', 'pullrequests', 'repositories', '_instance', 'applicationType')):
                    if any(value.get(key) for key in ('id', 'number', 'displayId', 'pullRequestId')):
                        return self._pull_request_evidence_from_mapping(value)
            for key, item in value.items():
                compact_key = re.sub(r'[^a-z0-9]', '', str(key).casefold())
                if compact_key in {'pullrequestid', 'pullrequestcount'} and str(item).isdigit() and int(item) > 0:
                    return f"PR {key}={item}"
                if in_pull_request_context and key == 'byInstanceType' and isinstance(item, dict):
                    for provider_data in item.values():
                        evidence = self._extract_pull_request_evidence(provider_data, in_pull_request_context=True)
                        if evidence:
                            return evidence
                evidence = self._extract_pull_request_evidence(
                    item, in_pull_request_context=self._is_pull_request_key(key)
                    or (in_pull_request_context and key in ('overall', 'byInstanceType', 'stateCount', 'summary')))
                if evidence:
                    return evidence
        return None

    def _json_contains_pull_request(self, value: object) -> bool:
        return self._extract_pull_request_evidence(value) is not None

    def _json_contains_commit(self, value: object) -> bool:
        if isinstance(value, str):
            value_normalized = value.casefold()
            value_compact = re.sub(r'[^a-zа-я0-9/#-]+', '', value_normalized)
            return (
                '/commits/' in value_normalized
                or bool(re.search(r'\bcommit\s+[0-9a-f]{7,40}\b', value_normalized))
                or bool(re.search(r'\b[0-9a-f]{40}\b', value_normalized))
                or 'commits' in value_compact
            )
        if isinstance(value, dict):
            for key, item in value.items():
                key_normalized = str(key).casefold()
                key_compact = re.sub(r'[^a-zа-я0-9]+', '', key_normalized)
                if key_compact in {'commit', 'commits'} and item:
                    return True
                if self._json_contains_commit(item):
                    return True
        elif isinstance(value, list):
            return any(self._json_contains_commit(item) for item in value)
        return False

    @contextmanager
    def _pr_lookup_budget(self, seconds=PR_LOOKUP_TIMEOUT_SECONDS):
        previous = getattr(self, '_pr_request_deadline', None)
        deadline = time.monotonic() + seconds
        self._pr_request_deadline = min(previous, deadline) if previous is not None else deadline
        try:
            yield
        finally:
            self._pr_request_deadline = previous

    def _reset_pr_run_state(self):
        # Successful snapshots last for one release; transient failures expire.
        self._pr_http_cache = {}
        self._pr_endpoint_failures = {}
        self._pr_endpoint_last_error = {}
        self._pr_endpoint_retry_at = {}
        self._pr_retry_state = {}
        self._pr_lookup_failures = set()
        self._dev_status_payload_cache = {}
        self._pull_request_only_evidence_cache = {}
        self._pull_request_evidence_cache = {}
        self._merged_pull_request_evidence_cache = {}
        self._release_pr_targets_cache = {}

    def _pr_get_json(self, url: str, **kwargs):
        """At most three bounded attempts; share snapshots and outage protection."""
        if not hasattr(self, '_pr_http_cache'):
            self._pr_http_cache = {}
            self._pr_endpoint_failures = {}
            self._pr_endpoint_last_error = {}
        if not hasattr(self, '_pr_endpoint_retry_at'):
            self._pr_endpoint_retry_at = {}
            self._pr_retry_state = {}
        if not hasattr(self, '_pr_lookup_failures'):
            self._pr_lookup_failures = set()
        cache_key = (url, json.dumps(kwargs, sort_keys=True))
        if cache_key in self._pr_http_cache:
            payload, failure, expires = self._pr_http_cache[cache_key]
            if not failure or time.monotonic() < expires:
                if failure:
                    self._pr_lookup_failures.add(failure)
                trace_http('PR', url, 'cache-hit', time.monotonic(), params=kwargs.get('params'),
                           cached_failure=bool(failure))
                return payload
            del self._pr_http_cache[cache_key]
        cooldown_reason = self._pr_cooldown_reason()
        if cooldown_reason:
            self._pr_lookup_failures.add(cooldown_reason)
            return None
        outer_deadline = getattr(self, '_pr_request_deadline', None)
        if outer_deadline is not None and time.monotonic() >= outer_deadline:
            self._pr_lookup_failures.add('исчерпан лимит времени проверки PR')
            trace_http('PR', url, 'budget-exhausted', time.monotonic(), params=kwargs.get('params'))
            # No request was attempted: do not poison the endpoint circuit/cache.
            return None
        # Group issue-specific paths, so an outage is not retried for every Story.
        endpoint = re.sub(r'/issue/[^/]+/(remotelink)$', r'/issue/*/\1', url)
        if url.endswith('/detail'):
            params = kwargs.get('params') or {}
            endpoint += ':' + str(params.get('applicationType', '')) + ':' + str(params.get('dataType', ''))
        if self._pr_endpoint_failures.get(endpoint, 0) >= PR_ENDPOINT_FAILURE_LIMIT:
            if time.monotonic() < self._pr_endpoint_retry_at.get(endpoint, 0):
                cause = self._pr_endpoint_last_error.get(endpoint, 'сбой запроса')
                self._pr_lookup_failures.add(f'источник временно недоступен после повторных сбоев: {cause}')
                trace_http('PR', url, 'circuit-open', time.monotonic(), params=kwargs.get('params'))
                return None
            self._pr_endpoint_failures[endpoint] = 0
        is_detail = url.endswith('/detail')
        deadline = min(outer_deadline if outer_deadline is not None else float('inf'),
                       time.monotonic() + (25 if is_detail else 12))
        permanent = False
        try:
            request_timeout = (PR_DETAIL_TIMEOUT_SECONDS if is_detail else
                               PR_SUMMARY_TIMEOUT_SECONDS if url.endswith('/summary') else PR_REQUEST_TIMEOUT_SECONDS)
            response = bounded_get(self.jira_http, url, source='PR', attempts=PR_MAX_ATTEMPTS,
                                   timeout=request_timeout,
                                   deadline=deadline, retry_state=self._pr_retry_state,
                                   validate=validate_json_payload, **kwargs)
            if response.status_code == 200:
                payload = response.json()
                self._pr_endpoint_failures[endpoint] = 0
                self._pr_http_cache[cache_key] = (payload, None, float('inf'))
                return payload
            failure = f'HTTP {response.status_code}'
            if response.status_code in {401, 403}:
                permanent = True
                self._pr_endpoint_failures[endpoint] = PR_ENDPOINT_FAILURE_LIMIT
        except requests.exceptions.SSLError as exc:
            failure = request_failure_kind(exc)
            permanent = True
            self._pr_endpoint_failures[endpoint] = PR_ENDPOINT_FAILURE_LIMIT
        except RequestNotSent as exc:
            self._pr_lookup_failures.add(str(exc))
            return None
        except (requests.RequestException, ValueError) as exc:
            failure = request_failure_kind(exc)
            if isinstance(exc, requests.Timeout) and 'лимит' in str(exc):
                failure += ': исчерпан лимит времени проверки PR'
        self._pr_endpoint_failures[endpoint] = self._pr_endpoint_failures.get(endpoint, 0) + 1
        self._pr_endpoint_last_error[endpoint] = failure
        expires = float('inf') if permanent else time.monotonic() + PR_FAILURE_COOLDOWN_SECONDS
        self._pr_endpoint_retry_at[endpoint] = expires
        self._pr_lookup_failures.add(failure)
        self._pr_http_cache[cache_key] = (None, failure, expires)
        return None

    def _pr_cooldown_reason(self):
        """Stop source fan-out during a server-requested pause; log it only once."""
        state = getattr(self, '_pr_retry_state', {})
        until = state.get('until', 0)
        remaining = until - time.monotonic()
        if remaining <= 0:
            return ''
        if state.get('reported_until') != until:
            trace_http('PR', '', 'rate-limited', time.monotonic(), retry_after_s=round(remaining, 2))
            state['reported_until'] = until
        return f'PR: сервер ограничил запросы; Retry-After, осталось {remaining:.1f} с'

    def _begin_pr_stage(self):
        """A transient failure in PR presence must not disable merged/GigaCode reads."""
        cache = getattr(self, '_pr_http_cache', {})
        for key, (_, failure, expires) in list(cache.items()):
            if failure and expires != float('inf'):
                del cache[key]
        for endpoint, expires in getattr(self, '_pr_endpoint_retry_at', {}).items():
            if expires != float('inf'):
                self._pr_endpoint_failures[endpoint] = 0
        # Keep the shared Retry-After state and permanent TLS/auth failures.

    @staticmethod
    def _summary_pr_count(payload) -> Optional[int]:
        if not isinstance(payload, dict) or payload.get('errors') or payload.get('errorMessages'):
            return None
        summary = payload.get('summary')
        section = summary.get('pullrequest') if isinstance(summary, dict) else None
        overall = section.get('overall') if isinstance(section, dict) else None
        count = overall.get('count') if isinstance(overall, dict) else None
        return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None

    def _get_dev_summary(self, issue_id):
        base = config['jira']['url'].rstrip('/')
        fallback = (None, None)
        # Summary is an optimization, not a prerequisite. Reserve time for detail.
        with self._pr_lookup_budget(5):
            for version in ('latest', '1.0'):
                payload = self._pr_get_json(f"{base}/rest/dev-status/{version}/issue/summary",
                                            params={'issueId': issue_id})
                if isinstance(payload, dict) and isinstance(payload.get('summary'), dict):
                    fallback = (version, payload)
                    if self._summary_pr_count(payload) is not None:
                        return version, payload
        return fallback

    def _get_dev_status_payloads(self, issue_id: str, *, data_types=('pullrequest', 'repository', 'commit'),
                                 find_evidence=None) -> list[tuple[str, str, dict]]:
        """Use summary when available; retain pre-08.10 direct integration reads."""
        with self._pr_lookup_budget():
            return self._get_dev_status_payloads_with_retries(issue_id, data_types, find_evidence=find_evidence)

    def _get_dev_status_payloads_with_retries(self, issue_id: str, data_types, *, find_evidence=None) -> list[tuple[str, str, dict]]:
        cache_key = (issue_id, tuple(data_types))
        if cache_key in self._dev_status_payload_cache:
            return self._dev_status_payload_cache[cache_key]
        cooldown_reason = self._pr_cooldown_reason()
        if cooldown_reason:
            raise RuntimeError(cooldown_reason)
        version, summary = self._get_dev_summary(issue_id)
        cooldown_reason = self._pr_cooldown_reason()
        if cooldown_reason:
            raise RuntimeError(cooldown_reason)
        sections = (summary or {}).get('summary', {})
        payloads, errors = [], []
        base = config['jira']['url'].rstrip('/')
        for type_index, data_type in enumerate(data_types):
            section = sections.get(data_type) or {}
            if not isinstance(section, dict):
                section = {}
            overall = section.get('overall') or {}
            count = overall.get('count') if isinstance(overall, dict) else None
            if type(count) is int and count == 0:
                continue
            reported = section.get('byInstanceType') or {}
            if not isinstance(reported, dict):
                reported = {}
            providers = tuple(reported) or PR_LEGACY_PROVIDERS
            type_payloads = []
            # A slow first source must not consume the repository/commit fallback.
            deadline = getattr(self, '_pr_request_deadline', None)
            type_seconds = (max(0, deadline - time.monotonic()) / (len(data_types) - type_index)
                            if deadline is not None else PR_LOOKUP_TIMEOUT_SECONDS)
            type_deadline = time.monotonic() + type_seconds
            for provider in providers:
                provider_info = reported.get(provider) or {}
                provider_count = provider_info.get('count') if isinstance(provider_info, dict) else None
                if type(provider_count) is int and provider_count == 0 and count == 0:
                    continue
                obtained = None
                for api_version in dict.fromkeys((version or 'latest', '1.0')):
                    with self._pr_lookup_budget(max(0, type_deadline - time.monotonic())):
                        payload = self._pr_get_json(f"{base}/rest/dev-status/{api_version}/issue/detail", params={
                            'issueId': issue_id, 'applicationType': provider, 'dataType': data_type})
                    cooldown_reason = self._pr_cooldown_reason()
                    if payload is None and cooldown_reason:
                        raise RuntimeError(cooldown_reason)
                    if not isinstance(payload, dict) or not isinstance(payload.get('detail'), list):
                        continue
                    # An empty response for an unrecognized alias proves nothing.
                    if not payload['detail']:
                        continue
                    if count and data_type == 'pullrequest' and not self._extract_pull_request_evidence(payload):
                        continue
                    obtained = (provider, data_type, payload)
                    type_payloads.append(obtained)
                    if find_evidence and find_evidence(payload):
                        return [obtained]
                    break
                if obtained is None and reported:
                    errors.append(f'не удалось проверить {provider}/{data_type}: пустой или недоступный detail')
                if obtained is not None and not reported:
                    # Historical providers are aliases; a working alias is enough.
                    break
            payloads.extend(type_payloads)
            if not type_payloads:
                errors.append(f'не удалось получить development {data_type}')
        if errors:
            raise RuntimeError('; '.join(dict.fromkeys(errors)))
        self._dev_status_payload_cache[cache_key] = payloads
        return payloads

    def _dev_status_pull_request_evidence(self, data_type: str, payload: dict) -> Optional[str]:
        return self._extract_pull_request_evidence(payload)

    def _dev_status_payload_has_pull_request(self, data_type: str, payload: dict) -> bool:
        return self._dev_status_pull_request_evidence(data_type, payload) is not None

    def _dev_status_payload_has_commit(self, data_type: str, payload: dict) -> bool:
        if self._json_contains_commit(payload):
            return True

        details = payload.get('detail') if isinstance(payload, dict) else None
        if isinstance(details, list):
            return any(self._json_contains_commit(item) for item in details)
        if isinstance(details, dict):
            return self._json_contains_commit(details)

        if data_type in {'commit', 'repository'}:
            return bool(details)

        return False

    def _extract_merged_pull_request_evidence_from_payload(self, value: object, in_pr=False) -> Optional[str]:
        """Найти в dev-status payload PR со статусом merged/merged=true."""
        if isinstance(value, list):
            for item in value:
                evidence = self._extract_merged_pull_request_evidence_from_payload(item, in_pr=in_pr)
                if evidence:
                    return evidence
            return None

        if not isinstance(value, dict):
            return None

        key_text = " ".join(str(key) for key in value.keys()).casefold()
        raw_text = json.dumps(value, ensure_ascii=False, default=str)
        raw_casefold = raw_text.casefold()
        raw_compact = re.sub(r'[^a-zа-я0-9/-]+', '', raw_casefold)
        looks_like_pr = (
            in_pr or 'pullrequest' in re.sub(r'[^a-zа-я0-9]+', '', key_text)
            or 'pullrequest' in raw_compact
            or '/pull-requests/' in raw_casefold
            or bool(re.search(r'\bpull\s*request\b', raw_casefold))
        )

        if looks_like_pr:
            status_values = []
            for status_key in ('status', 'state', 'pullRequestStatus', 'pullrequestStatus'):
                status_value = value.get(status_key)
                if status_value is not None:
                    status_text = extract_jira_field_value(status_value) or normalize_field_text(status_value)
                    status_values.append(status_text.casefold())

            merged_flag = value.get('merged')
            is_merged = (
                any(status in {'merged', 'merge', 'влит', 'смёржен', 'смержен'} for status in status_values)
                or merged_flag is True
                or normalize_field_text(merged_flag).casefold() == 'true'
            )

            if is_merged:
                pr_id = (
                    value.get('id')
                    or value.get('pullRequestId')
                    or value.get('pullrequestId')
                    or value.get('number')
                    or value.get('displayId')
                    or value.get('name')
                    or value.get('title')
                    or value.get('url')
                    or 'PR'
                )
                status_display = ", ".join(status_values) if status_values else "merged=true"
                return f"{pr_id} ({status_display})"

        for key, item in value.items():
            evidence = self._extract_merged_pull_request_evidence_from_payload(item, in_pr=self._is_pull_request_key(key))
            if evidence:
                return evidence
        return None

    def _find_issue_merged_pull_request_evidence(self, issue) -> Optional[str]:
        issue_key = str(getattr(issue, 'key', '') or '')
        issue_id = str(getattr(issue, 'id', '') or '')
        cache_key = issue_key or issue_id
        if cache_key and cache_key in self._merged_pull_request_evidence_cache:
            return self._merged_pull_request_evidence_cache[cache_key]

        evidence = None
        if not issue_id:
            raise RuntimeError('нет Jira ID для проверки merged PR')
        if issue_id:
            for application_type, data_type, payload in self._get_dev_status_payloads(issue_id, data_types=('pullrequest',)):
                payload_evidence = self._extract_merged_pull_request_evidence_from_payload(payload)
                if payload_evidence:
                    evidence = f"dev-status {application_type}/{data_type}: {payload_evidence}"
                    break

        if cache_key and evidence:
            self._merged_pull_request_evidence_cache[cache_key] = evidence
        return evidence

    def _find_issue_pull_request_evidence(self, issue) -> Optional[str]:
        issue_key = str(getattr(issue, 'key', '') or '')
        issue_id = str(getattr(issue, 'id', '') or '')
        cache_key = issue_key or issue_id
        if cache_key and cache_key in self._pull_request_evidence_cache:
            return self._pull_request_evidence_cache[cache_key]

        evidence = self._find_issue_pull_request_evidence_uncached(issue, issue_key, issue_id)
        if cache_key and evidence:
            self._pull_request_evidence_cache[cache_key] = evidence
        return evidence

    def _find_issue_pull_request_only_evidence(self, issue) -> Optional[str]:
        issue_key = str(getattr(issue, 'key', '') or '')
        issue_id = str(getattr(issue, 'id', '') or '')
        cache_key = issue_key or issue_id
        if cache_key and cache_key in self._pull_request_only_evidence_cache:
            return self._pull_request_only_evidence_cache[cache_key]

        self._pr_lookup_failures = set()
        with self._pr_lookup_budget():
            evidence = self._find_issue_pull_request_only_evidence_uncached(issue_key, issue_id)
        if cache_key and evidence:
            self._pull_request_only_evidence_cache[cache_key] = evidence
        return evidence

    def _find_issue_pull_request_only_evidence_uncached(
        self, issue_key: str, issue_id: str,
    ) -> Optional[str]:
        if not hasattr(self, '_pr_lookup_failures'):
            self._pr_lookup_failures = set()
        count = None
        cooldown_reason = self._pr_cooldown_reason()
        if cooldown_reason:
            self._pr_lookup_failures.add(cooldown_reason)
            return None
        if issue_id:
            _, summary = self._get_dev_summary(issue_id)
            count = self._summary_pr_count(summary)
            if count is not None and count > 0:
                return f"Jira development: связанных PR — {count}"
        cooldown_reason = self._pr_cooldown_reason()
        if cooldown_reason:
            self._pr_lookup_failures.add(cooldown_reason)
            return None
        links = None
        base = config['jira']['url'].rstrip('/')
        if issue_key:
            with self._pr_lookup_budget(4):
                links = self._pr_get_json(f"{base}/rest/api/2/issue/{quote(issue_key, safe='')}/remotelink")
            if isinstance(links, list):
                evidence = self._extract_pull_request_evidence(links)
                if evidence:
                    return f"Jira remote links: {evidence}"
        if count == 0 and isinstance(links, list):
            self._pr_lookup_failures = set()
            return None
        # f38c5e1 removed both of these sources. Restore them without treating
        # commits, repository ids or a PR on a child Task as evidence for a Story.
        if issue_id:
            try:
                with self._pr_lookup_budget(16):
                    for provider, data_type, payload in self._get_dev_status_payloads(
                            issue_id, data_types=('pullrequest', 'repository'), find_evidence=self._extract_pull_request_evidence):
                        evidence = self._dev_status_pull_request_evidence(data_type, payload)
                        if evidence:
                            self._pr_lookup_failures = set()
                            return f"dev-status {provider}/{data_type}: {evidence}"
            except RuntimeError as exc:
                self._pr_lookup_failures.add(str(exc))
        if issue_key:
            properties = self._pr_get_json(f"{base}/rest/api/2/issue/{quote(issue_key, safe='')}/properties")
            keys = (properties.get('keys') or []) if isinstance(properties, dict) else []
            relevant = [item['key'] for item in keys if isinstance(item, dict) and isinstance(item.get('key'), str)
                        and any(word in item['key'].casefold() for word in ('dev', 'stash', 'bitbucket', 'pull', 'vcs'))]
            for key in relevant[:3]:
                payload = self._pr_get_json(f"{base}/rest/api/2/issue/{quote(issue_key, safe='')}/properties/{quote(key, safe='')}")
                evidence = self._extract_pull_request_evidence(payload)
                if evidence:
                    self._pr_lookup_failures = set()
                    return f"Jira issue property {key}: {evidence}"
        self._pr_lookup_failures.add('не получены полные данные о связях PR')
        return None

    def _find_issue_pull_request_evidence_uncached(
        self, issue, issue_key: str, issue_id: str,
    ) -> Optional[str]:
        with self._pr_lookup_budget():
            return self._find_issue_pull_request_only_evidence_uncached(issue_key, issue_id)

    def _issue_has_pull_request(self, issue) -> bool:
        return self._find_issue_pull_request_evidence(issue) is not None

    def _issue_has_gigacode_pull_request(self, issue) -> Optional[str]:
        issue_id = str(getattr(issue, 'id', '') or '')
        if not issue_id:
            raise RuntimeError('нет Jira ID для проверки GigaCode')

        for _, _, payload in self._get_dev_status_payloads(issue_id, find_evidence=self._find_gigacode_marker):
            gigacode_marker = self._find_gigacode_marker(payload)
            if gigacode_marker:
                return gigacode_marker

        return None

    def _add_label_if_missing(self, issue, label: str) -> bool:
        labels = list(getattr(issue.fields, 'labels', []) or [])
        if label.casefold() in {existing_label.casefold() for existing_label in labels}:
            return False

        issue_key = getattr(issue, 'key', None)
        if not issue_key:
            raise RuntimeError("У задачи нет key для обновления labels")
        response = self.jira_http.put(
            f"{config['jira']['url'].rstrip('/')}/rest/api/2/issue/{issue_key}",
            json={'fields': {'labels': labels + [label]}},
            timeout=12,
        )
        if response.status_code not in (200, 204):
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:250]}")
        issue.fields.labels = labels + [label]
        return True

    def _get_story_task_keys(self, story) -> list[str]:
        task_keys = []
        if not hasattr(story.fields, 'issuelinks') or not story.fields.issuelinks:
            return task_keys

        for link in story.fields.issuelinks:
            link_type = link.type.name.casefold() if hasattr(link.type, 'name') else ''
            if 'consist' not in link_type and 'part' not in link_type:
                continue
            linked_issue = getattr(link, 'outwardIssue', None) or getattr(link, 'inwardIssue', None)
            if linked_issue:
                task_keys.append(linked_issue.key)
        return task_keys

    def _get_release_pr_targets(self, release_key: str) -> list[tuple[object, list[object]]]:
        if release_key in self._release_pr_targets_cache:
            return self._release_pr_targets_cache[release_key]

        issue_types = {'story', 'bug', 'defect', 'ошибка'}

        linked_keys = self._get_consist_of_issues(release_key)
        if linked_keys:
            keys_str = ",".join(linked_keys)
            jql = f'parent = {release_key} OR key in ({keys_str})'
        else:
            jql = f'parent = {release_key}'

        try:
            release_issues = self.jira_main.search_issues(
                jql,
                fields='summary,issuetype,assignee,labels,issuelinks',
                maxResults=False
            )
        except Exception as e:
            self._log_issue("GENERAL", "warning", f"Ошибка получения Story/Bug для проверки PR: {e}")
            self._release_pr_targets_cache[release_key] = []
            return []

        target_issues = [
            issue for issue in release_issues
            if issue.fields.issuetype and issue.fields.issuetype.name.casefold() in issue_types
        ]

        task_keys_by_story: dict[str, list[str]] = {}
        all_task_keys: list[str] = []
        for target_issue in target_issues:
            if target_issue.fields.issuetype.name.casefold() != 'story':
                continue
            task_keys = self._get_story_task_keys(target_issue)
            task_keys_by_story[target_issue.key] = task_keys
            all_task_keys.extend(task_keys)

        tasks_by_key = {}
        unique_task_keys = sorted(set(all_task_keys))
        if unique_task_keys:
            try:
                task_keys_str = ",".join(unique_task_keys)
                task_issues = self.jira_main.search_issues(
                    f'key in ({task_keys_str})',
                    fields='summary,issuetype,assignee,labels',
                    maxResults=False
                )
                tasks_by_key = {task.key: task for task in task_issues}
            except Exception as e:
                self._log_issue("GENERAL", "warning", f"PR/AIFIXED: не удалось bulk-получить Task: {e}")

        targets = []
        for target_issue in target_issues:
            issues_to_check = [target_issue]

            if target_issue.fields.issuetype.name.casefold() == 'story':
                for task_key in task_keys_by_story.get(target_issue.key, []):
                    task = tasks_by_key.get(task_key)
                    if task is None:
                        self._log_issue(target_issue, "warning", f"PR/AIFIXED: не удалось получить Task {task_key}")
                        continue
                    if task.fields.issuetype and task.fields.issuetype.name.casefold() == 'task':
                        issues_to_check.append(task)

            targets.append((target_issue, issues_to_check))

        self._release_pr_targets_cache[release_key] = targets
        return targets

    def _check_required_pull_requests(self, release_key: str) -> None:
        targets = self._get_release_pr_targets(release_key)
        if not targets:
            return

        print(f"   Проверка Pull Request для {len(targets)} Story/Bug...")

        for target_issue, _issues_to_check in targets:
            pr_evidence = None
            try:
                pr_evidence = self._find_issue_pull_request_only_evidence(target_issue)
            except Exception as e:
                self._log_issue(
                    target_issue,
                    "warning",
                    f"Pull Request: не удалось проверить {target_issue.key}: {e}"
                )
                continue

            if pr_evidence:
                self._log_issue(
                    target_issue,
                    "success",
                    f"Pull Request найден в {target_issue.key} ({pr_evidence}) ✓"
                )
            else:
                self._log_issue(
                    target_issue,
                    "warning",
                    ("Pull Request не удалось подтвердить: " + "; ".join(sorted(self._pr_lookup_failures))
                     if getattr(self, '_pr_lookup_failures', None) else
                     "Pull Request не найден. Для каждой Story/Bug в составе релиза должен быть PR на самой задаче")
                )

    @staticmethod
    def _extract_issue_keys_from_changelog_value(value: object) -> set[str]:
        return set(re.findall(r'\b[A-Z][A-Z0-9]+-\d+\b', str(value or '')))

    def _get_unlinked_release_issue_keys(self, release_key: str) -> set[str]:
        try:
            release = self.jira_main.issue(release_key, fields='issuelinks', expand='changelog')
        except Exception as e:
            self._log_issue(
                release_key,
                "warning",
                f"Отлинкованные задачи: не удалось получить changelog релиза для проверки merged PR: {e}"
            )
            return set()

        current_linked_keys = set(self._extract_consist_of_issues(release) or [])
        removed_keys = set()

        changelog = getattr(release, 'changelog', None)
        histories = getattr(changelog, 'histories', []) or []
        if changelog is None or getattr(changelog, 'total', len(histories)) > len(histories):
            raise RuntimeError('не удалось получить полную историю связей релиза')
        for history in histories:
            for item in getattr(history, 'items', []) or []:
                field_name = normalize_field_text(getattr(item, 'field', '')).casefold()
                field_id = normalize_field_text(getattr(item, 'fieldId', '')).casefold()
                if 'link' not in field_name and 'link' not in field_id:
                    continue

                from_keys = self._extract_issue_keys_from_changelog_value(getattr(item, 'fromString', ''))
                to_keys = self._extract_issue_keys_from_changelog_value(getattr(item, 'toString', ''))
                if not from_keys:
                    from_keys = self._extract_issue_keys_from_changelog_value(getattr(item, 'from', ''))
                if not to_keys:
                    to_keys = self._extract_issue_keys_from_changelog_value(getattr(item, 'to', ''))

                removed_keys.update(from_keys - to_keys)

        return removed_keys - current_linked_keys - {release_key}

    def _get_release_story_bug_keys(self, release_key: str) -> set[str]:
        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            return set()

        keys_str = ",".join(linked_keys)
        try:
            issues = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields='issuetype',
                maxResults=False,
            )
        except Exception as e:
            self._log_issue(
                release_key,
                "warning",
                f"Отлинкованные задачи: не удалось получить состав релиза для проверки связей: {e}"
            )
            return set()

        issue_types_to_check = {'story', 'bug', 'defect', 'ошибка'}
        return {
            issue.key
            for issue in issues
            if issue.fields.issuetype and issue.fields.issuetype.name.casefold() in issue_types_to_check
        }

    @staticmethod
    def _extract_linked_issue_keys(issue) -> set[str]:
        linked_keys = set()
        for link in getattr(issue.fields, 'issuelinks', []) or []:
            linked_issue = getattr(link, 'outwardIssue', None) or getattr(link, 'inwardIssue', None)
            if linked_issue and getattr(linked_issue, 'key', None):
                linked_keys.add(linked_issue.key)
        return linked_keys

    def _check_unlinked_release_items_pull_requests(self, release_key: str) -> None:
        unlinked_keys = sorted(self._get_unlinked_release_issue_keys(release_key))
        if not unlinked_keys:
            return

        print(f"   Проверка merged PR у {len(unlinked_keys)} отлинкованных задач...")
        current_release_story_bug_keys = self._get_release_story_bug_keys(release_key)

        keys_str = ",".join(unlinked_keys)
        try:
            issues = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields='summary,issuetype,assignee,labels,issuelinks',
                maxResults=False
            )
        except Exception as e:
            self._log_issue(
                release_key,
                "warning",
                f"Отлинкованные задачи: не удалось получить задачи для проверки merged PR: {e}"
            )
            return

        issue_types_to_check = {'story', 'task', 'задача', 'bug', 'defect', 'ошибка'}
        issues_by_key = {
            issue.key: issue
            for issue in issues
            if issue.fields.issuetype and issue.fields.issuetype.name.casefold() in issue_types_to_check
        }

        for issue_key in unlinked_keys:
            issue = issues_by_key.get(issue_key)
            if not issue:
                continue

            linked_release_keys = self._extract_linked_issue_keys(issue) & current_release_story_bug_keys
            if linked_release_keys:
                self._log_issue(
                    issue,
                    "success",
                    "Отлинкованная от релиза задача связана с задачей/багом в составе релиза "
                    f"({', '.join(sorted(linked_release_keys))}) — проверка merged PR пропущена ✓"
                )
                continue

            issue_labels = {str(label).strip().casefold() for label in (getattr(issue.fields, 'labels', []) or [])}
            if 'review_confirmed' in issue_labels:
                self._log_issue(
                    issue,
                    "success",
                    "Отлинкованная от релиза задача имеет лейбл review_confirmed — "
                    "проверка merged PR пропущена ✓"
                )
                continue

            try:
                merged_pr = self._find_issue_merged_pull_request_evidence(issue)
            except Exception as e:
                self._log_issue(
                    issue,
                    "warning",
                    f"Отлинкованная задача: не удалось проверить статус PR: {e}"
                )
                continue

            if merged_pr:
                self._log_issue(
                    issue,
                    "error",
                    f"Отлинкованная от релиза задача содержит PR в статусе merged ({merged_pr}). "
                    "Если PR уже влит, задача/баг не должна быть отлинкована от релизного объекта"
                )
            else:
                self._log_issue(
                    issue,
                    "success",
                    "Отлинкованная от релиза задача: merged PR не найден ✓"
                )

    def _check_gigacode_aifixed_labels(self, release_key: str) -> None:
        AIFIXED_LABEL = 'AIFIXED'
        targets = self._get_release_pr_targets(release_key)
        if not targets:
            return

        print(f"   Проверка AIFIXED по GigaCode PR/commit для {len(targets)} Story/Bug...")

        for target_issue, issues_to_check in targets:
            if AIFIXED_LABEL.casefold() in self._issue_labels_casefold(target_issue):
                self._log_issue(target_issue, 'success', 'AIFIXED: лейбл уже установлен ✓')
                continue
            matched_issue = None
            matched_marker = None
            check_failed = False
            for issue_to_check in issues_to_check:
                try:
                    gigacode_marker = self._issue_has_gigacode_pull_request(issue_to_check)
                    if gigacode_marker and matched_issue is None:
                        matched_issue = issue_to_check
                        matched_marker = gigacode_marker
                        break
                except Exception as e:
                    check_failed = True
                    self._log_issue(
                        target_issue,
                        "warning",
                        f"AIFIXED: не удалось проверить {issue_to_check.key}: {e}"
                    )

            if not matched_issue:
                if check_failed:
                    continue
                self._log_issue(target_issue, "success", "AIFIXED: GigaCode PR/commit не найден — лейбл AIFIXED не требуется ✓")
                continue

            if getattr(self, 'dry_run', False):
                self._log_issue(target_issue, "warning", "AIFIXED: найден GigaCode; требуется добавить лейбл (dry-run, запись отключена)")
                continue
            try:
                added = self._add_label_if_missing(target_issue, AIFIXED_LABEL)
                if added:
                    self._log_issue(
                        target_issue,
                        "success",
                        f"GigaCode PR/commit найден в {matched_issue.key} "
                        f"(маркер: '{matched_marker}'); лейбл {AIFIXED_LABEL} добавлен ✓"
                    )
                else:
                    self._log_issue(
                        target_issue,
                        "success",
                        f"GigaCode PR/commit найден в {matched_issue.key} "
                        f"(маркер: '{matched_marker}'); лейбл {AIFIXED_LABEL} уже есть ✓"
                    )
            except Exception as e:
                self._log_issue(
                    target_issue,
                    "error",
                    f"GigaCode PR/commit найден в {matched_issue.key} "
                    f"(маркер: '{matched_marker}'), но не удалось добавить лейбл {AIFIXED_LABEL}: {e}"
                )

    def _check_cloud_label(self, release_key: str, release_summary: str):
        """
        Для каждой Story и Бага в составе релиза (consist of, не дети):
          Если в названии релиза есть 'Cloud' — проверяем наличие
          лейбла 'Пульс_внешний_рынок' в каждой Story и Баге.
        """
        CLOUD_LABEL = 'Пульс_внешний_рынок'
        is_cloud = 'cloud' in (release_summary or '').lower()

        if not is_cloud:
            return

        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            return

        keys_str = ",".join(linked_keys)
        try:
            all_issues = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields='summary,issuetype,assignee,labels',
                maxResults=False
            )
        except Exception as e:
            self._log_issue("GENERAL", "error", f"Ошибка проверки Cloud-лейбла: {e}")
            return

        issues = [
            i for i in all_issues
            if i.fields.issuetype.name.lower() in {'story', 'bug', 'defect', 'ошибка'}
        ]

        for issue in issues:
            labels = getattr(issue.fields, 'labels', []) or []
            if CLOUD_LABEL not in labels:
                self._log_issue(
                    issue, "error",
                    f"Отсутствует лейбл '{CLOUD_LABEL}' в Cloud-релизе"
                )
            else:
                self._log_issue(
                    issue, "success",
                    f"Лейбл '{CLOUD_LABEL}' присутствует ✓"
                )

    def _check_sbrppl_third_party_label(self, release_key: str) -> None:
        """
        Проверяет обязательный лейбл #Пульс_3лица для Story/Bug проекта SBRPPL.

        В Jira labels обычно хранятся без символа #, поэтому сравнение
        нормализует оба варианта: Пульс_3лица и #Пульс_3лица.
        """
        project_key = 'SBRPPL'
        required_label = 'Пульс_3лица'
        issue_types = {'story', 'bug', 'defect', 'ошибка'}

        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            return

        keys_str = ",".join(linked_keys)
        try:
            all_issues = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields='summary,issuetype,assignee,labels',
                maxResults=False
            )
        except Exception as e:
            self._log_issue("GENERAL", "error", f"Ошибка проверки лейбла #Пульс_3лица для SBRPPL: {e}")
            return

        target_issues = [
            issue for issue in all_issues
            if issue.key.split('-')[0] == project_key
            and issue.fields.issuetype.name.lower() in issue_types
        ]

        if not target_issues:
            return

        for issue in target_issues:
            labels = getattr(issue.fields, 'labels', []) or []
            normalized_labels = {str(label).lstrip('#') for label in labels}
            if required_label not in normalized_labels:
                self._log_issue(
                    issue,
                    "error",
                    "Для задач Story/Bug проектной области SBRPPL "
                    "обязателен лейбл '#Пульс_3лица'"
                )
            else:
                self._log_issue(
                    issue,
                    "success",
                    "Лейбл '#Пульс_3лица' присутствует ✓"
                )

    def _check_sbrppl_story_points(self, release_key: str) -> None:
        """
        Проверяет обязательную оценку Story Points для Story/Bug проекта SBRPPL.

        Поле Story Points в Jira: customfield_10002.
        """
        project_key = 'SBRPPL'
        story_points_field = 'customfield_10002'
        issue_types = {'story', 'bug', 'defect', 'ошибка'}

        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            return

        keys_str = ",".join(linked_keys)
        try:
            all_issues = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields=f'summary,issuetype,assignee,{story_points_field}',
                maxResults=False
            )
        except Exception as e:
            self._log_issue(
                "GENERAL",
                "error",
                f"Ошибка проверки Story Points для SBRPPL: {e}"
            )
            return

        target_issues = [
            issue for issue in all_issues
            if issue.key.split('-')[0] == project_key
            and issue.fields.issuetype.name.lower() in issue_types
        ]

        if not target_issues:
            return

        for issue in target_issues:
            story_points = getattr(issue.fields, story_points_field, None)
            is_empty_string = isinstance(story_points, str) and not story_points.strip()
            if story_points is None or is_empty_string:
                self._log_issue(
                    issue,
                    "error",
                    "Для задач Story/Bug проектной области SBRPPL "
                    "обязательна оценка в Story Points (customfield_10002)"
                )
            else:
                self._log_issue(
                    issue,
                    "success",
                    f"Story Points заполнены: {story_points} ✓"
                )

    def _check_summary_description_match(self, release_key: str) -> None:
        """
        Проверяет через GigaChat соответствие summary↔description у Story/Bug/Defect/Ошибка.

        Сначала пробуем env='ift', при недоступности — fallback на env='dev'.
        Любая проблема (нет токена, нет ответа, mismatch) логируется как warning,
        чтобы не блокировать релиз.
        """
        issue_types_to_check = {'story', 'bug', 'defect', 'ошибка'}

        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            return

        keys_str = ",".join(linked_keys)
        try:
            issues = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields='summary,description,issuetype,assignee',
                maxResults=False,
            )
        except Exception as e:
            self._log_issue(
                "GENERAL",
                "warning",
                f"GigaChat: не удалось получить задачи для проверки summary↔description: {e}",
            )
            return

        candidates = []
        candidate_issue_by_key: dict = {}
        for issue in issues:
            issue_type = (issue.fields.issuetype.name or '').lower() if issue.fields.issuetype else ''
            if issue_type not in issue_types_to_check:
                continue
            description = getattr(issue.fields, 'description', None) or ''
            summary = issue.fields.summary or ''
            if not summary.strip() or not description.strip():
                continue
            candidates.append({
                'key': issue.key,
                'summary': summary,
                'description': description,
            })
            candidate_issue_by_key[issue.key] = issue

        if not candidates:
            return

        token = None
        used_env = None
        for env_candidate in ('ift', 'dev'):
            try:
                token = get_gigachat_token(env_candidate)
            except Exception as e:
                self._log_issue(
                    "GENERAL",
                    "warning",
                    f"GigaChat: ошибка получения токена ({env_candidate}): {e}",
                )
                token = None
            if token:
                used_env = env_candidate
                break

        if not token or not used_env:
            self._log_issue(
                "GENERAL",
                "warning",
                "GigaChat: не удалось получить токен ни на ift, ни на dev — проверка summary↔description пропущена",
            )
            return

        try:
            analysis = check_summary_description_match(used_env, candidates, token)
        except Exception as e:
            self._log_issue(
                "GENERAL",
                "warning",
                f"GigaChat: сбой при проверке summary↔description ({used_env}): {e}",
            )
            return

        if analysis is None:
            self._log_issue(
                "GENERAL",
                "warning",
                f"GigaChat: пустой ответ от модели на {used_env} — проверка summary↔description пропущена",
            )
            return

        analyzed_keys = set()
        for item in analysis:
            issue_key = item.get('key', '')
            issue_obj = candidate_issue_by_key.get(issue_key)
            if not issue_obj:
                continue
            analyzed_keys.add(issue_key)
            reason = item.get('reason') or 'без причины'
            if item.get('is_match'):
                self._log_issue(
                    issue_obj,
                    "success",
                    f"GigaChat: summary соответствует description ✓ ({reason})",
                )
            else:
                self._log_issue(
                    issue_obj,
                    "warning",
                    f"GigaChat: summary не соответствует description — {reason}",
                )

        for candidate in candidates:
            if candidate['key'] in analyzed_keys:
                continue
            issue_obj = candidate_issue_by_key.get(candidate['key'])
            if not issue_obj:
                continue
            self._log_issue(
                issue_obj,
                "warning",
                "GigaChat: не вернул вердикт по этой задаче — проверка summary↔description пропущена",
            )

    def _check_prod_bug_skip_reason(self, bug, skip_reason_field_id: Optional[str]) -> None:
        """Для ПРОМ-багов причина пропуска обязательна и должна быть из согласованного списка."""
        if not skip_reason_field_id:
            self._log_issue(
                bug,
                "error",
                "ПРОМ Bug: не задан Jira field id для поля причины пропуска"
            )
            return

        raw_val = getattr(bug.fields, skip_reason_field_id, None)
        if raw_val is None and hasattr(bug, 'raw'):
            raw_val = bug.raw.get('fields', {}).get(skip_reason_field_id)

        values = extract_jira_field_values(raw_val)
        normalized_values = {
            normalize_field_text(value).casefold()
            for value in values
            if normalize_field_text(value)
        }
        display_value = ", ".join(values) if values else "None"

        if not normalized_values:
            self._log_issue(
                bug,
                "error",
                f"ПРОМ Bug: поле 'Причина пропуска' ({skip_reason_field_id}) не заполнено"
            )
            return

        invalid_values = sorted(normalized_values - BUG_SKIP_REASON_ALLOWED_VALUES)
        if invalid_values:
            allowed_display = "; ".join(sorted(BUG_SKIP_REASON_ALLOWED_VALUES))
            self._log_issue(
                bug,
                "error",
                f"ПРОМ Bug: поле 'Причина пропуска' ({skip_reason_field_id}) = "
                f"'{display_value}', содержит недопустимые значения: {', '.join(invalid_values)}. "
                f"Допустимо: {allowed_display}"
            )
            return

        self._log_issue(
            bug,
            "success",
            f"ПРОМ Bug: причина пропуска заполнена корректно ({display_value}) ✓"
        )

    def _check_bugs(self, release_key, is_hotfix: bool = False):
        VALID_DETECTION_METHODS = {"АТ НФ", "АТ Регресс", "Регрессионное тестирование", "Тестирование НФ"}
        SKIP_DETECTION_CHECK_STANDS = {"ПСИ", "ПРОМ", "ПРОМ (Stand-in)"}
        STAND_FIELD_ID = 'customfield_17500'
        DISCOVERY_STAGE_FIELD = 'customfield_11507'
        HOTFIX_REQUIRED_STAGE = 'ПРОМ'
        HOTFIX_REQUIRED_PRIORITIES = {'blocker', 'блокирующий', 'critical', 'критичный'}
        REGULAR_RELEASE_BUG_STATUSES = {normalize_status_name(status) for status in {'закрыт', 'closed'}}
        HOTFIX_BUG_STATUS = normalize_status_name('подтверждение исправления')
        # Хотя бы один из этих лейблов должен быть у каждого бага
        REQUIRED_CONTOUR_LABELS = {'sigma', 'cloud', 'mobile'}
        skip_reason_field_id = BUG_SKIP_REASON_FIELD_ID

        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            jql = f'parent = {release_key}'
        else:
            keys_str = ",".join(linked_keys)
            jql = f'parent = {release_key} OR key in ({keys_str})'

        try:
            extra_fields = [field_id for field_id in (skip_reason_field_id,) if field_id]
            fields_req = (
                "summary,description,priority,labels,issuetype,status,assignee,"
                f"customfield_16901,customfield_11507,{STAND_FIELD_ID},"
                + ",".join(extra_fields + ["*all"])
            )
            all_issues = self.jira_main.search_issues(jql, fields=fields_req, maxResults=False)
        except Exception as e:
            self._log_issue("GENERAL", "error", f"Ошибка поиска багов: {e}")
            return

        bugs = [i for i in all_issues if i.fields.issuetype.name in ['Bug', 'Defect', 'Ошибка']]

        if not bugs:
            self._log_issue(release_key, "success", "Баги не найдены в составе релиза")
            return

        print(f"   Проверка {len(bugs)} багов...")

        prod_bugs = []

        for bug in bugs:
            self._check_required_platform_label(bug, "Bug")

            bug_status = bug.fields.status.name if getattr(bug.fields, 'status', None) else ''
            bug_status_normalized = normalize_status_name(bug_status)
            if is_hotfix:
                if bug_status_normalized != HOTFIX_BUG_STATUS:
                    self._log_issue(
                        bug, "error",
                        f"Hotfix: Bug в статусе '{bug_status}', ожидается '{HOTFIX_BUG_STATUS}'"
                    )
                else:
                    self._log_issue(
                        bug, "success",
                        f"Hotfix: Bug в статусе '{bug_status}' ✓"
                    )
            elif bug_status_normalized not in REGULAR_RELEASE_BUG_STATUSES:
                expected_statuses = "', '".join(sorted(REGULAR_RELEASE_BUG_STATUSES))
                self._log_issue(
                    bug, "error",
                    f"Bug в статусе '{bug_status}', ожидается один из: '{expected_statuses}'"
                )
            else:
                self._log_issue(
                    bug, "success",
                    f"Bug в статусе '{bug_status}' ✓"
                )

            if not bug.fields.description or not bug.fields.description.strip():
                self._log_issue(bug, "error", "Отсутствует описание")

            # --- Проверка лейбла контура ---
            bug_labels = {lbl.lower() for lbl in (getattr(bug.fields, 'labels', []) or [])}
            if not bug_labels & REQUIRED_CONTOUR_LABELS:
                self._log_issue(
                    bug, "error",
                    f"Отсутствует лейбл контура. Должен быть один из: sigma, cloud, mobile"
                )

            stand_type = ""
            val = getattr(bug.fields, STAND_FIELD_ID, None)
            if not val and hasattr(bug, 'raw'):
                val = bug.raw['fields'].get(STAND_FIELD_ID)

            if val:
                if hasattr(val, 'value'):
                    stand_type = val.value
                elif isinstance(val, dict):
                    stand_type = val.get('value', '')
                elif isinstance(val, str):
                    stand_type = val

            stand_type = str(stand_type).strip()

            if stand_type not in SKIP_DETECTION_CHECK_STANDS:
                det_val = getattr(bug.fields, 'customfield_16901', None)
                actual_method = ""
                if det_val:
                    if hasattr(det_val, 'value'):
                        actual_method = det_val.value
                    elif isinstance(det_val, dict):
                        actual_method = det_val.get('value', '')
                    elif isinstance(det_val, str):
                        actual_method = det_val

                actual_method = str(actual_method).strip()

                if actual_method not in VALID_DETECTION_METHODS:
                    self._log_issue(bug, "error",
                                    f"Некорректный метод обнаружения: '{actual_method}'. Стенд: '{stand_type}'")

            # --- Проверки для Hotfix-релиза ---
            if is_hotfix:
                # Этап обнаружения должен быть ПРОМ
                disc_val = getattr(bug.fields, DISCOVERY_STAGE_FIELD, None)
                if disc_val is None and hasattr(bug, 'raw'):
                    disc_val = bug.raw['fields'].get(DISCOVERY_STAGE_FIELD)
                if hasattr(disc_val, 'value'):
                    disc_stage = disc_val.value
                elif isinstance(disc_val, dict):
                    disc_stage = disc_val.get('value', '')
                elif isinstance(disc_val, str):
                    disc_stage = disc_val.strip()
                else:
                    disc_stage = ''
                disc_stage = str(disc_stage).strip()
                if disc_stage.upper() != HOTFIX_REQUIRED_STAGE.upper():
                    self._log_issue(
                        bug, "error",
                        f"Hotfix: Этап обнаружения (customfield_11507) = '{disc_stage}', "
                        f"ожидается '{HOTFIX_REQUIRED_STAGE}'"
                    )

                # Приоритет должен быть Blocker или Critical
                priority_obj = getattr(bug.fields, 'priority', None)
                priority_name = ''
                if priority_obj:
                    priority_name = getattr(priority_obj, 'name', '') or ''
                if priority_name.strip().lower() not in HOTFIX_REQUIRED_PRIORITIES:
                    self._log_issue(
                        bug, "error",
                        f"Hotfix: Приоритет = '{priority_name}', "
                        f"ожидается Блокирующий (Blocker) или Критичный (Critical)"
                    )

            if stand_type in ["ПРОМ", "ПРОМ (Stand-in)"]:
                self._check_prod_bug_skip_reason(bug, skip_reason_field_id)
                prod_bugs.append(bug)

        if prod_bugs:
            self._check_confluence(prod_bugs)

    def _check_confluence(self, prod_bugs):
        project_keys = set(bug.key.split('-')[0] for bug in prod_bugs)
        try:
            child_pages = list(self.confluence.get_child_pages(self.confluence_parent_page))

            relevant_pages = []
            for page in child_pages:
                for p_key in project_keys:
                    if p_key in page['title']:
                        relevant_pages.append(page)
                        break

            for page in relevant_pages:
                try:
                    page_content = self.confluence.get_page_by_id(page['id'], expand='body.storage')
                    content = page_content['body']['storage']['value']

                    confluence_base = config['confluence']['url'].rstrip('/')
                    page_url = f"{confluence_base}/pages/viewpage.action?pageId={page['id']}"

                    for bug in prod_bugs:
                        if bug.key in content:
                            self._log_issue(
                                bug, "error",
                                f"Баг найден на Confluence: [{page['title']}|{page_url}]"
                            )
                except Exception:
                    pass

        except Exception as e:
            self._log_issue("GENERAL", "error", f"Ошибка проверки Confluence: {e}")

    def generate_report(self, release_key_or_url):
        release_key = extract_issue_key_from_url(release_key_or_url)

        print(f"\n{'=' * 80}")
        print(f"🚀 ОТЧЕТ ПО РЕЛИЗУ: {release_key}")
        print(f"🔗 {config['jira']['url']}/browse/{release_key}")
        print(f"{'=' * 80}\n")

        self.check_release(release_key)

        total_errors = sum(len(d['errors']) for d in self.report_data.values())

        sorted_items = sorted(
            self.report_data.items(),
            key=lambda x: (0 if x[0] == 'GENERAL' else 1, 0 if x[1]['errors'] else 1, x[0])
        )

        for key, data in sorted_items:
            if not data['errors'] and not data['warnings'] and key != 'GENERAL':
                continue

            status_icon = "❌" if data['errors'] else ("⚠️" if data['warnings'] else "✅")
            assignee = clean_report_text(data['assignee'])

            if key == "GENERAL":
                print(f"{status_icon} ОБЩИЕ ПРОВЕРКИ:")
            else:
                print(f"{status_icon} [{key}] {trim_report_text(data['summary'], 60)}... ({assignee})")
                print(f"   🔗 {data['url']}")

            for err in data['errors']:
                print(f"   🔴 {clean_report_text(err)}")

            for warn in data['warnings']:
                print(f"   ⚠️  {clean_report_text(warn)}")

            print("-" * 40)

        is_success = (total_errors == 0)

        print("=" * 80)
        if is_success:
            print("✅ РЕЛИЗ ГОТОВ К ВЫПУСКУ! Ошибок не найдено.")
        else:
            print(f"❌ РЕЛИЗ НЕ ГОТОВ. Найдено {total_errors} ошибок.")
        print("=" * 80)

        self._manage_jira_comment(release_key, is_success)
        return is_success


def _diag_tc(validator: 'ReleaseValidator', tc_key: str):
    """Диагностика: вывести сырой JSON ответа для ТЦ."""
    import json
    url = f"{validator.zephyr.base_url}/rest/atm/1.0/testrun/{tc_key}"
    print(f"\n=== GET {url} ===")
    resp = validator.zephyr._get_with_retries(url)
    print(f"Status: {resp.status_code}")
    try:
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    except Exception:
        print(resp.text[:2000])


def _diag_search(validator: 'ReleaseValidator', release_key: str, expected_cycle_key: str = ''):
    """Диагностика: показать реальный алгоритм поиска ТЦ по ключу релиза."""
    import json

    try:
        ri = validator.jira_main.issue(release_key, fields='id,summary')
        print(f"Jira issue id: {ri.id}  summary: {ri.fields.summary}")
    except Exception as e:
        print(f"Failed to get issue id: {e}")

    if expected_cycle_key:
        url = f"{validator.zephyr.base_url}/rest/atm/1.0/testrun/{expected_cycle_key}"
        print(f"\nDirect cycle lookup: {url}")
        try:
            response = validator.zephyr._get_with_retries(url)
            print(f"  status={response.status_code}")
            if response.status_code == 200:
                data = response.json()
                raw = json.dumps(data, ensure_ascii=False)
                if validator._test_cycle_belongs_to_release(data, release_key):
                    validator._cache_release_cycles(release_key, [data])
                    validator._save_zephyr_cycle_cache()
                    print("  cached=yes")
                print(f"  key={validator.zephyr.get_test_cycle_key(data) or data.get('key', '')}")
                print(f"  name={validator.zephyr.get_test_cycle_name(data) or data.get('name', '')}")
                print(f"  projectKey={data.get('projectKey', data.get('project', ''))}")
                print(f"  top-level keys={', '.join(sorted(data.keys()))}")
                print(f"  contains release key in raw json={'yes' if release_key in raw else 'no'}")
                custom_fields = data.get('customFields')
                if isinstance(custom_fields, dict):
                    print(f"  customFields={', '.join(sorted(custom_fields.keys()))}")
            else:
                print(f"  body={response.text[:1000]}")
        except Exception as e:
            print(f"  direct lookup failed: {e}")

    cycles = validator.zephyr.get_test_cycles_for_issue(release_key)
    print("\nZephyr search stats:")
    for stat in validator.zephyr.last_test_cycle_search_stats:
        mode = stat.get('mode', 'unknown')
        if mode == 'issuelink':
            suffix = f", error={stat.get('error')}" if stat.get('error') else ""
            body = f", body={stat.get('body')}" if stat.get('body') else ""
            print(
                f"  {stat.get('endpoint', 'issuelink')}: "
                f"status={stat.get('status')}, found={stat.get('count')}{suffix}{body}"
            )
        elif mode == 'issue-query':
            suffix = f", error={stat.get('error')}" if stat.get('error') else ""
            body = f", body={stat.get('body')}" if stat.get('body') else ""
            print(
                f"  {stat.get('endpoint', 'issue-query')}: "
                f"status={stat.get('status')}, seen={stat.get('seen', 0)}, "
                f"matched={stat.get('count')}{suffix}{body}"
            )
        elif mode == 'search-name':
            suffix = f", error={stat.get('error')}" if stat.get('error') else ""
            body = f", body={stat.get('body')}" if stat.get('body') else ""
            print(
                f"  search-name projectKey={stat.get('project_key')}: "
                f"status={stat.get('status')}, seen={stat.get('seen', 0)}, "
                f"matched={stat.get('count')}{suffix}{body}"
            )
        elif mode == 'search':
            suffix = f", error={stat.get('error')}" if stat.get('error') else ""
            print(
                f"  search projectKey={stat.get('project_key')}: "
                f"status={stat.get('status')}, pages={stat.get('pages')}, "
                f"seen={stat.get('total_seen')}, matched={stat.get('count')}{suffix}"
            )

    print(f"\nFound cycles: {len(cycles)}")
    for cycle in cycles[:50]:
        cycle_key = validator.zephyr.get_test_cycle_key(cycle)
        cycle_name = validator.zephyr.get_test_cycle_name(cycle)
        print(f"  -> {cycle_key} | {cycle_name}")

    effective_cycles = validator._get_release_test_cycles(release_key)
    print(f"\nEffective release cycles: {len(effective_cycles)}")
    for cycle_key, cycle_name in effective_cycles[:50]:
        print(f"  -> {cycle_key} | {cycle_name}")

    metadata_sources = validator._collect_test_cycle_keys_from_jira_metadata_sources(release_key)
    for source_name, source_keys in metadata_sources.items():
        print(f"Jira metadata source {source_name}: {len(source_keys)}")

    metadata_cycle_keys = validator._collect_test_cycle_keys_from_jira_metadata(release_key)
    print(f"\nJira metadata cycle keys: {len(metadata_cycle_keys)}")
    for cycle_key in sorted(metadata_cycle_keys)[:50]:
        details = validator._get_test_cycle_details_cached(cycle_key)
        if details and validator._test_cycle_belongs_to_release(details, release_key):
            print(f"  -> {cycle_key} | {validator.zephyr.get_test_cycle_name(details)} | belongs=yes")
        elif details:
            print(f"  -> {cycle_key} | {validator.zephyr.get_test_cycle_name(details)} | belongs=no")
        else:
            print(f"  -> {cycle_key} | details=not found")

    cached_cycles = validator._get_cached_test_cycles_for_release(release_key)
    print(f"\nLocal Zephyr cycle cache: {len(cached_cycles)}")
    for cycle in cached_cycles[:50]:
        print(f"  -> {validator.zephyr.get_test_cycle_key(cycle)} | {validator.zephyr.get_test_cycle_name(cycle)}")


def _diag_gigacode(validator: 'ReleaseValidator', issue_key: str):
    try:
        issue = validator.jira_main.issue(issue_key, fields='summary,issuetype,labels')
    except Exception as e:
        print(f"Не удалось получить issue {issue_key}: {e}")
        return

    issue_id = str(getattr(issue, 'id', '') or '')
    print(f"Jira issue id: {issue_id}  type: {issue.fields.issuetype.name}  summary: {issue.fields.summary}")
    print(f"Labels: {', '.join(getattr(issue.fields, 'labels', []) or []) or 'None'}")

    base_url = config['jira']['url'].rstrip('/')
    application_types = ('stash', 'bitbucket', 'bitbucket-server')
    data_types = ('pullrequest', 'commit', 'repository')

    for application_type in application_types:
        for data_type in data_types:
            url = f"{base_url}/rest/dev-status/latest/issue/detail"
            params = {
                'issueId': issue_id,
                'applicationType': application_type,
                'dataType': data_type,
            }
            print(f"\nGET dev-status applicationType={application_type} dataType={data_type}")
            try:
                response = validator.jira_http.get(url, params=params, timeout=12)
            except Exception as e:
                print(f"  exception={e}")
                continue

            print(f"  status={response.status_code} bytes={len(response.text)}")
            if response.status_code != 200:
                print(f"  body={response.text[:500]}")
                continue

            try:
                payload = response.json()
            except Exception as e:
                print(f"  json_error={e}")
                print(f"  body={response.text[:1000]}")
                continue

            raw = json.dumps(payload, ensure_ascii=False)
            raw_casefold = raw.casefold()
            marker = validator._find_gigacode_marker(payload)
            print(f"  top-level={', '.join(sorted(payload.keys())) if isinstance(payload, dict) else type(payload).__name__}")
            print(f"  contains GigaCode={'yes' if 'gigacode' in raw_casefold else 'no'}")
            print(f"  contains Co-authored={'yes' if 'co-authored' in raw_casefold or 'co-authorer' in raw_casefold else 'no'}")
            print(f"  contains #GigaCode={'yes' if '#gigacode' in raw_casefold else 'no'}")
            print(f"  parser marker={marker or 'None'}")
            if marker or 'gigacode' in raw_casefold:
                for matched in re.finditer(r'.{0,80}(gigacode|co-authored|co-authorer|#gigacode).{0,160}', raw, flags=re.IGNORECASE):
                    print(f"  snippet={matched.group(0)}")


def _diag_pr(validator: 'ReleaseValidator', issue_key: str):
    try:
        issue = validator.jira_main.issue(issue_key, fields='summary,issuetype,labels')
    except Exception as e:
        print(f"Не удалось получить issue {issue_key}: {e}")
        return

    issue_id = str(getattr(issue, 'id', '') or '')
    print(f"Jira issue id: {issue_id}  type: {issue.fields.issuetype.name}  summary: {issue.fields.summary}")
    print(f"Labels: {', '.join(getattr(issue.fields, 'labels', []) or []) or 'None'}")
    strict_evidence = validator._find_issue_pull_request_only_evidence(issue)
    broad_evidence = validator._find_issue_pull_request_evidence(issue)
    print(f"Report parser verdict: has_pr={'yes' if strict_evidence else 'no'}")
    print(f"Report evidence: {strict_evidence or 'None'}")
    print(f"Broad legacy evidence: {broad_evidence or 'None'}")

    for application_type, data_type, payload in validator._get_dev_status_payloads(issue_id):
        raw = json.dumps(payload, ensure_ascii=False)
        raw_casefold = raw.casefold()
        pr_evidence = validator._dev_status_pull_request_evidence(data_type, payload)
        has_pr = pr_evidence is not None
        print(f"\nPayload applicationType={application_type} dataType={data_type}")
        print(f"  has_pr={'yes' if has_pr else 'no'}")
        print(f"  evidence={pr_evidence or 'None'}")
        print(f"  top-level={', '.join(sorted(payload.keys())) if isinstance(payload, dict) else type(payload).__name__}")
        print(f"  contains pullrequest-ish={'yes' if 'pullrequest' in re.sub(r'[^a-z0-9]+', '', raw_casefold) else 'no'}")
        print(f"  contains url={'yes' if 'url' in raw_casefold else 'no'}")
        print(f"  detail_type={type(payload.get('detail')).__name__ if isinstance(payload, dict) else 'n/a'}")
        print(f"  detail_len={len(payload.get('detail') or []) if isinstance(payload, dict) and isinstance(payload.get('detail'), list) else 'n/a'}")
        print(f"  contains files={'yes' if 'files' in raw_casefold else 'no'}")
        if has_pr or 'pullrequest' in re.sub(r'[^a-z0-9]+', '', raw_casefold):
            for matched in re.finditer(r'.{0,100}(pullrequest|pull_request|pull request|url|files).{0,180}', raw, flags=re.IGNORECASE):
                print(f"  snippet={matched.group(0)}")


def _diag_cycle_index(probe, release_key, deadline):
    """Bounded compact index experiment; empty next page proves completion, not page size."""
    query = f'projectKey = "{ZEPHYR_TC_PROJECT_KEY}"'
    def page(size, offset):
        payload = probe(f'cycle index size={size} offset={offset}', '/rest/atm/1.0/testrun/search',
                        {'query': query, 'fields': 'key,name,issueKey,issueLinks',
                         'maxResults': size, 'startAt': offset}, deadline=deadline, kind='cycles')
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return next((payload[field] for field in ('values', 'results', 'testRuns', 'testCycles')
                         if isinstance(payload.get(field), list)), None)
        return None

    result = {'complete': False, 'reason': 'request_failed', 'pages': 0, 'unique_cycles': 0, 'matches': []}
    small = page(1000, 0)
    if small is None:
        return result
    result['pages'] = 1
    large = page(5000, 0) if small and time.monotonic() < deadline else None
    size = 5000 if large is not None and len(large) >= len(small) else 1000
    items = large if size == 5000 else small
    result.update(requested_page_size=size, initial_count=len(items))
    if large is not None:
        result['pages'] += 1
    seen, matched = set(), set()
    offset = 0
    for _ in range(32):
        if items is None:
            result['reason'] = 'request_failed_or_throttled'
            break
        if not items:
            result.update(complete=True, reason='empty_next_page')
            break
        keys = {str(item.get('key')) for item in items if isinstance(item, dict) and item.get('key')}
        if len(keys) != len(items):
            result['reason'] = 'invalid_or_duplicate_keys'
            break
        if not keys - seen:
            result['reason'] = 'pagination_not_advancing'
            break
        if keys & seen:
            result['reason'] = 'overlapping_pages'
            break
        seen.update(keys)
        for item in items:
            if ZephyrScaleClient._cycle_mentions_issue(
                    {field: item.get(field) for field in ('name', 'issueKey', 'issueLinks', 'links')}, release_key):
                matched.add(item['key'])
        offset += len(items)  # Servers may silently cap maxResults.
        if time.monotonic() >= deadline:
            result['reason'] = 'budget'
            break
        items = page(size, offset)
        result['pages'] += 1
    else:
        result['reason'] = 'page_limit'
    result.update(unique_cycles=len(seen), matches=sorted(matched))
    return result


def _diag_lookup_matrix(release_input, session=None):
    """Read-only comparison; no SDK initialization, retries, pagination or Jira writes."""
    release_key = extract_issue_key_from_url(release_input)
    focus = os.getenv('RELEASE_CHECKER_DIAG_FOCUS', LOOKUP_DIAGNOSTIC_FOCUS).strip().lower()
    if focus not in {'both', 'pr', 'cycles'}:
        raise ValueError('RELEASE_CHECKER_DIAG_FOCUS must be both/pr/cycles')
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', release_key or ''):
        raise ValueError('Expected Jira release key')
    session = session or ReleaseHttpSession()
    session.verify = release_tls_policy()
    session.headers.update({'Authorization': f"Bearer {config['jira']['token']}", 'Accept': 'application/json'})
    base = config['jira']['url'].rstrip('/')
    started = time.monotonic()
    overall_deadline = started + 130
    cooldown = 0.0
    next_request_at = started
    records = []
    parser = ReleaseValidator.__new__(ReleaseValidator)
    print('[LOOKUP MATRIX] read-only; normal report disabled; 130s network budget; one attempt per method', flush=True)

    def probe(name, path, params=None, *, deadline=None, kind='metadata'):
        nonlocal cooldown, next_request_at
        record = {'method': name, 'path': path, 'result': 'SKIP', 'seconds': 0.0}
        records.append(record)
        now = time.monotonic()
        end = min(overall_deadline, deadline or overall_deadline)
        payload = None
        # Brief pacing/cooldowns consume the existing budget; long pauses are not waited out.
        delay = max(cooldown, next_request_at) - now
        if 0 < delay <= 5 and delay < end - now:
            time.sleep(delay)
            record['wait_s'] = round(delay, 3)
            now = time.monotonic()
        if cooldown > now:
            record.update(reason='Retry-After', retry_after_s=round(cooldown - now, 2))
        elif end <= now or next_request_at > now:
            record['reason'] = 'budget'
        else:
            next_request_at = now + 0.5
            try:
                # All probes share server cooldown, including Jira and Zephyr paths.
                response = session.get(base + path, params=params or {}, timeout=urllib3.util.Timeout(
                    total=min(12, end-now), connect=min(5, end-now)))
                record.update(http=response.status_code,
                              content_type=response.headers.get('Content-Type', '').split(';')[0],
                              bytes=len(response.content))
                if response.status_code in (429, 503):
                    pause = retry_after_seconds(response)
                    # Even without Retry-After, don't hammer the server with alternative routes.
                    cooldown = time.monotonic() + max(2, pause)
                    record['retry_after_s'] = pause
                record['result'] = 'HTTP_ERROR'
                if response.status_code == 200:
                    payload = response.json()
                    record['shape'] = response_shape(payload)
                    errors = (payload.get('errors') or payload.get('errorMessages')) if isinstance(payload, dict) else None
                    if errors:
                        record['result'] = 'API_ERROR'
                        # Only fixed categories, never raw service messages, URLs or tokens.
                        error_text = json.dumps(errors, ensure_ascii=False).lower()
                        record['error_categories'] = [label for label, words in {
                            'timeout': ('timeout', 'timed out'), 'permission': ('forbidden', 'permission', 'unauthorized'),
                            'connection': ('connection', 'connectexception'), 'application_link': ('application link', 'applink'),
                        }.items() if any(word in error_text for word in words)] or ['unclassified']
                    elif kind == 'pr':
                        record['result'] = 'PR_EVIDENCE' if parser._extract_pull_request_evidence(payload) else 'NO_PR_EVIDENCE'
                    elif kind == 'cycle_hints':
                        keys = sorted(parser._extract_test_cycle_keys(payload))
                        record.update(cycle_key_count=len(keys), cycle_keys=keys[:30])
                        record['result'] = 'CYCLE_HINTS' if keys else 'NO_CYCLE_HINTS'
                    elif kind == 'cycles':
                        # A keyed cycle is one object; its items are execution results, not cycles.
                        is_cycle = isinstance(payload, dict) and re.fullmatch(
                            r'[A-Z][A-Z0-9_]*-C\d+', str(payload.get('key', '')))
                        items = [payload] if is_cycle else (payload if isinstance(payload, list) else next(
                            (payload[k] for k in ('values', 'results', 'items', 'testRuns', 'testCycles')
                             if isinstance(payload, dict) and isinstance(payload.get(k), list)),
                            [payload] if isinstance(payload, dict) and payload.get('key') else []))
                        cycle_items = [item for item in items if isinstance(item, dict)]
                        matches = [item for item in cycle_items
                                   if parser._test_cycle_belongs_to_release(
                                       {field: item.get(field) for field in ('name', 'issueKey', 'issueLinks', 'links')},
                                       release_key)]
                        if is_cycle:
                            record.update(cycle_key=payload['key'], execution_count=len(payload.get('items') or []))
                        record.update(count=len(items), release_matches=len(matches),
                                      keys_fingerprint=hashlib.sha256(json.dumps(sorted(str(item.get('key', '')) for item in cycle_items)).encode()).hexdigest()[:16],
                                      unrelated=len(cycle_items)-len(matches),
                                      matching_keys=[item.get('key') for item in matches[:20]])
                        record['result'] = 'RELEASE_MATCHES' if matches else 'NO_RELEASE_MATCHES'
                        baseline = next((r for r in records if r['method'] == 'cycle project index baseline'), None)
                        if baseline and baseline is not record and cycle_items:
                            record['same_page_as_baseline'] = baseline.get('keys_fingerprint') == record['keys_fingerprint']
                        if matches and len(matches) < len(cycle_items):
                            record['filter_note'] = 'mixed response: filter may be ignored; no completeness claim'
                    else:
                        record['result'] = 'JSON_OK'
            except Exception as exc:
                record.update(result='EXCEPTION', exception=type(exc).__name__)
            record['seconds'] = round(time.monotonic()-now, 3)
        print('[LOOKUP MATRIX] ' + json.dumps(record, ensure_ascii=False), flush=True)
        return payload if record['result'] not in ('HTTP_ERROR', 'API_ERROR', 'EXCEPTION', 'SKIP') else None

    # Bootstrap through the same bounded HTTP path, without hidden SDK retries.
    release = probe('release-links', f'/rest/api/2/issue/{release_key}', {'fields': 'issuelinks'}, deadline=started+10)
    issue_key = os.getenv('RELEASE_CHECKER_DIAG_ISSUE', '').strip()
    if not issue_key and isinstance(release, dict):
        for link in release.get('fields', {}).get('issuelinks', []):
            if any(word in str(link.get('type', {}).get('name', '')).lower() for word in ('consist', 'part')):
                issue_key = (link.get('outwardIssue') or link.get('inwardIssue') or {}).get('key', '')
                if issue_key:
                    break
    if issue_key and not re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', issue_key):
        raise ValueError('Invalid RELEASE_CHECKER_DIAG_ISSUE')
    pr_deadline = min(overall_deadline, time.monotonic()+60)
    if issue_key and focus != 'cycles':
        issue = probe('PR issue fields', f'/rest/api/2/issue/{issue_key}', {'fields': '*all'}, deadline=pr_deadline, kind='pr')
        issue_id = str((issue or {}).get('id', ''))
        probe('PR remote links', f'/rest/api/2/issue/{issue_key}/remotelink', deadline=pr_deadline, kind='pr')
        if issue_id.isdigit():
            for version in ('latest', '1.0'):
                probe(f'PR summary {version}', f'/rest/dev-status/{version}/issue/summary',
                      {'issueId': issue_id}, deadline=pr_deadline, kind='pr')
            for provider in ('stash', 'bitbucket', 'bitbucket-server'):
                for data_type in ('pullrequest', 'repository', 'commit'):
                    for version in ('latest', '1.0'):
                        probe(f'PR detail {version}/{provider}/{data_type}', f'/rest/dev-status/{version}/issue/detail',
                              {'issueId': issue_id, 'applicationType': provider, 'dataType': data_type},
                              deadline=pr_deadline, kind='pr')
        else:
            print('[LOOKUP MATRIX] PR detail SKIP: issue id unavailable', flush=True)
    elif focus != 'cycles':
        print('[LOOKUP MATRIX] PR SKIP: no linked issue; set RELEASE_CHECKER_DIAG_ISSUE', flush=True)

    if focus != 'pr':
        cycle_deadline = min(overall_deadline, time.monotonic()+60)
        # Optional known key avoids relying on search to test the direct-read endpoint.
        cycle_key = os.getenv('RELEASE_CHECKER_DIAG_CYCLE', LOOKUP_DIAGNOSTIC_CYCLE).strip()
        metadata = probe('cycle Jira fields/links', f'/rest/api/2/issue/{release_key}',
                         {'fields': '*all'}, deadline=cycle_deadline, kind='cycle_hints')
        remote = probe('cycle remote links', f'/rest/api/2/issue/{release_key}/remotelink', deadline=cycle_deadline, kind='cycle_hints')
        sources = [metadata, remote]
        properties = probe('cycle property index', f'/rest/api/2/issue/{release_key}/properties',
                           deadline=cycle_deadline, kind='cycle_hints')
        if isinstance(properties, dict):
            relevant = [item.get('key') for item in properties.get('keys', [])
                        if isinstance(item, dict) and isinstance(item.get('key'), str)
                        and any(word in item['key'].lower() for word in ('zephyr', 'tm4j', 'atm', 'test', 'cycle'))]
            for prop in relevant[:5]:
                sources.append(probe('cycle property value',
                    f'/rest/api/2/issue/{release_key}/properties/{quote(prop, safe="")}',
                    deadline=cycle_deadline, kind='cycle_hints'))
        if cycle_key and not re.fullmatch(r'[A-Z][A-Z0-9_]*-C\d+', cycle_key):
            raise ValueError('Invalid RELEASE_CHECKER_DIAG_CYCLE')
        discovered = sorted(parser._extract_test_cycle_keys(sources))
        candidates_by_key = list(dict.fromkeys(([cycle_key] if cycle_key else []) + discovered))
        print('[LOOKUP MATRIX] cycle candidates ' + json.dumps({
            'discovered_keys': discovered[:30], 'discovered_count': len(discovered),
            'anchor_key': cycle_key, 'selected': candidates_by_key[:5],
            'not_probed': max(0, len(candidates_by_key)-5)}, ensure_ascii=False), flush=True)
        for index, key in enumerate(candidates_by_key[:5]):
            probe('cycle direct key' if index == 0 else 'cycle discovered key ' + key,
                  f'/rest/atm/1.0/testrun/{key}', deadline=cycle_deadline, kind='cycles')
        if not candidates_by_key:
            print('[LOOKUP MATRIX] cycle direct key SKIP: no keys in metadata; set RELEASE_CHECKER_DIAG_CYCLE', flush=True)
        index_result = _diag_cycle_index(probe, release_key, min(cycle_deadline, time.monotonic()+45))
        print('[LOOKUP INDEX SUMMARY] ' + json.dumps(index_result, ensure_ascii=False), flush=True)
    print('[LOOKUP MATRIX SUMMARY] ' + json.dumps({
        'release': release_key, 'issue': issue_key, 'focus': focus, 'seconds': round(time.monotonic()-started, 2),
        'methods': [{'method': r['method'], 'result': r['result'], 'reason': r.get('reason', '')} for r in records],
        'note': 'HTTP 200/empty is not proof of working lookup; no release readiness verdict'}, ensure_ascii=False), flush=True)
    return records


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    debug_path = os.getenv('RELEASE_CHECKER_HTTP_LOG', '').strip()
    if debug_path and http_debug_enabled():
        configure_http_debug(debug_path)
    print(f"[DIAGNOSTICS] HTTP={'ON' if http_debug_enabled() else 'OFF'} "
          f"output={'file' if http_debug_logger.handlers else 'console'}", flush=True)
    matrix_override = os.getenv('RELEASE_CHECKER_LOOKUP_DIAGNOSTICS')
    matrix_enabled = LOOKUP_DIAGNOSTICS_ENABLED if matrix_override is None else matrix_override.lower() in {'1', 'true', 'yes', 'on'}
    explicit_matrix = len(sys.argv) >= 3 and sys.argv[1] == '--diag-lookups'
    if explicit_matrix or (matrix_enabled and len(sys.argv) > 1 and not sys.argv[1].startswith('--')):
        _diag_lookup_matrix(sys.argv[2] if explicit_matrix else sys.argv[1])
        sys.exit(0)  # Diagnostic completion, not a release-readiness verdict.
    validator = ReleaseValidator()
    if len(sys.argv) >= 3 and sys.argv[1] == "--dry-run":
        validator.dry_run = True
        sys.exit(0 if validator.generate_report(sys.argv[2]) else 1)

    # Диагностика: python scripts/release_checker.py --diag-tc HRPQA-C133028
    if len(sys.argv) >= 3 and sys.argv[1] == '--diag-tc':
        _diag_tc(validator, sys.argv[2])
        sys.exit(0)

    # Диагностика: python scripts/release_checker.py --diag-search HRPRELEASE-120111 [HRPQA-C133028]
    if len(sys.argv) >= 3 and sys.argv[1] == '--diag-search':
        _diag_search(validator, sys.argv[2], sys.argv[3] if len(sys.argv) >= 4 else '')
        sys.exit(0)

    # Диагностика: python scripts/release_checker.py --diag-gigacode SFILE-12345
    if len(sys.argv) >= 3 and sys.argv[1] == '--diag-gigacode':
        _diag_gigacode(validator, sys.argv[2])
        sys.exit(0)

    # Диагностика: python scripts/release_checker.py --diag-pr SFILE-12345
    if len(sys.argv) >= 3 and sys.argv[1] == '--diag-pr':
        _diag_pr(validator, sys.argv[2])
        sys.exit(0)

    if len(sys.argv) > 1:
        release_input = sys.argv[1]
    else:
        release_input = "https://jira.sberbank.ru/browse/HRPRELEASE-120486"
        print(f"⚠️ Используем дефолтный ключ: {release_input}")

    if not validator.generate_report(release_input):
        sys.exit(1)
    else:
        sys.exit(0)
