from jira import JIRA
from atlassian import Confluence
import os
import sys
import re
import time
import html
import json
import urllib3
import logging
import requests
from html.parser import HTMLParser
from pathlib import Path
from collections import defaultdict
from typing import Optional

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

ALLOWED_TESTERS = {
    "Абдуллаев Магомедэмин Шамильевич",
    "Годунова Дарья Алексеевна", "Гулиев Руслан Иса оглы",
    "Калашников Андрей Романович", "Канаев Леонид Олегович",
    "Меркуленков Дмитрий Игоревич",
    "Метляев Игорь Андреевич", "Петрунин Никита Анатольевич",
    "Приколотина Евгения Александровна",
    "Симиник Даниил Григорьевич", "Федоров Никита Андреевич",
    "Чиж Мария Михайловна", "Синица Захар Алексеевич", "Абдулгалимов Гамзат Абусуньянович"
}

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
ZEPHYR_ENABLE_FULL_CYCLE_SCAN = os.getenv("ZEPHYR_ENABLE_FULL_CYCLE_SCAN", "0").lower() in (
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
    os.getenv("ZEPHYR_CYCLE_CACHE_PATH", str(parent_dir / "trash" / "zephyr_cycle_cache.json"))
)
ZEPHYR_REQUEST_TIMEOUT_SECONDS = int(os.getenv("ZEPHYR_REQUEST_TIMEOUT_SECONDS", "10"))
ZEPHYR_MAX_RETRIES_PER_REQUEST = int(os.getenv("ZEPHYR_MAX_RETRIES_PER_REQUEST", "3"))
ZEPHYR_MAX_FAILED_REQUESTS = int(os.getenv("ZEPHYR_MAX_FAILED_REQUESTS", "20"))

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
BACK_REGRESS_CYCLE_ALIASES = ('Регресс', 'Regress', 'Regression')
BACK_NF_CYCLE_ALIASES = ('НФ', 'NF', 'Новый функционал', 'Новая функциональность')
BACK_API_CHANNEL_ALIASES = ('api',)
BACK_DEVICE_BROWSER_CHANNEL_ALIASES = ('ipad/pwa/safari/sberbrowser',)
WEB_DEVICE_BROWSER_NF_CHANNEL_ALIASES = ('ipad/pwa/safari/sberbrowser',)
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


def normalize_test_cycle_name(value: object) -> str:
    """Нормализация имени ТЦ для проверки масок: регистр, пробелы и точки."""
    text = normalize_field_text(value).casefold()
    text = re.sub(r'\s*\.\s*', '.', text)
    text = re.sub(r'\s*/\s*', '/', text)
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
    text = re.sub(r'\s+', ' ', text)
    return text.strip(' .')


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
            if actual_variants & expected_variants:
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
        self.retry_backoff_seconds = 2
        self.request_timeout_seconds = ZEPHYR_REQUEST_TIMEOUT_SECONDS
        self.max_failed_requests = ZEPHYR_MAX_FAILED_REQUESTS
        self.failed_requests = 0
        self.retry_status_codes = {429, 500, 502, 503, 504}
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session.headers.update({
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        })
        self.last_test_cycle_search_stats: list[dict] = []

    def _raise_if_failed_request_limit_reached(self) -> None:
        if self.failed_requests >= self.max_failed_requests:
            raise RuntimeError(
                "Zephyr API: лимит неуспешных запросов исчерпан "
                f"({self.failed_requests}/{self.max_failed_requests}); "
                "дальнейшие Zephyr-запросы остановлены"
            )

    def is_failed_request_limit_reached(self) -> bool:
        return self.failed_requests >= self.max_failed_requests

    def _mark_failed_request(self, reason: str) -> None:
        self.failed_requests += 1
        print(
            f"   ⚠️ Zephyr API: неуспешный запрос {self.failed_requests}/{self.max_failed_requests}"
            f" ({reason})"
        )

    def _get_with_retries(self, url: str, **kwargs) -> requests.Response:
        self._raise_if_failed_request_limit_reached()
        kwargs.setdefault('timeout', self.request_timeout_seconds)
        last_exception = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, **kwargs)
                if response.status_code not in self.retry_status_codes:
                    return response
                if attempt == self.max_retries:
                    self._mark_failed_request(f"HTTP {response.status_code}")
                    self._raise_if_failed_request_limit_reached()
                    return response
            except requests.RequestException as e:
                last_exception = e
                if attempt == self.max_retries:
                    self._mark_failed_request(str(e))
                    self._raise_if_failed_request_limit_reached()
                    raise

            sleep_seconds = self.retry_backoff_seconds * attempt
            print(
                f"   ⚠️ Zephyr API: повтор запроса {attempt + 1}/{self.max_retries} "
                f"через {sleep_seconds} сек."
            )
            time.sleep(sleep_seconds)

        if last_exception:
            raise last_exception
        raise RuntimeError("Zephyr API: запрос не выполнен")

    def _get_once(self, url: str, **kwargs) -> requests.Response:
        """Один быстрый GET без ретраев для диагностических/спекулятивных endpoint'ов."""
        self._raise_if_failed_request_limit_reached()
        kwargs.setdefault('timeout', self.request_timeout_seconds)
        try:
            response = self.session.get(url, **kwargs)
        except requests.RequestException as e:
            self._mark_failed_request(str(e))
            self._raise_if_failed_request_limit_reached()
            raise
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
        url = f"{self.base_url}/rest/atm/1.0/issuelink/{issue_key}/testcases"
        try:
            response = self._get_with_retries(url)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    return data, None
                elif isinstance(data, dict):
                    return data.get('testCases', data.get('results', [])), None
                return [], None
            else:
                return [], f"HTTP {response.status_code}"
        except Exception as e:
            return [], f"{e}"

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
        try:
            response = self._get_with_retries(url)
            if response.status_code == 200:
                return response.json()
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
        if not custom_fields:
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
            return val.get('name', val.get('value', str(val)))
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
        issue_key_lower = issue_key.casefold()
        direct_issue_key = str(cycle.get('issueKey') or cycle.get('jiraIssueKey') or '').casefold()
        if direct_issue_key == issue_key_lower:
            return True
        cycle_name = ZephyrScaleClient.get_test_cycle_name(cycle).casefold()
        if issue_key_lower in cycle_name:
            return True
        try:
            return issue_key_lower in json.dumps(cycle, ensure_ascii=False).casefold()
        except Exception:
            return False

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

        for item in self._get_test_cycles_by_issue_query_params(issue_key, project_keys_to_try):
            key = self.get_test_cycle_key(item)
            dedupe_key = key or f"name:{self.get_test_cycle_name(item)}"
            if dedupe_key and dedupe_key not in seen_keys:
                seen_keys.add(dedupe_key)
                result.append(item)

        if result:
            return result

        for item in self._get_test_cycles_by_issue_link(issue_key):
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
                response = self._get_once(url)
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
        """
        GET /rest/atm/1.0/testrun/search?query=projectKey = "{project_key}"
        Пагинация по 50, фильтруем на стороне клиента по issue_key в name.
        API поддерживает только поля projectKey и folder в query.
        """
        targeted = self._search_test_cycles_by_name_query(project_key, issue_key)
        if targeted:
            return targeted
        if not ZEPHYR_ENABLE_FULL_CYCLE_SCAN:
            self.last_test_cycle_search_stats.append({
                'mode': 'search',
                'project_key': project_key,
                'status': 'skipped',
                'pages': 0,
                'total_seen': 0,
                'count': 0,
                'error': 'full project scan disabled',
            })
            return []

        url = f"{self.base_url}/rest/atm/1.0/testrun/search"
        query = f'projectKey = "{project_key}"'
        page_size = 50
        start_at = 0
        matched: list[dict] = []
        issue_key_lower = issue_key.lower()
        pages = 0
        total_seen = 0
        status = None

        while True:
            try:
                response = self._get_with_retries(
                    url,
                    params={'query': query, 'maxResults': page_size, 'startAt': start_at},
                )
            except Exception as e:
                status = 'exception'
                search_error = str(e)
                break

            status = response.status_code
            if response.status_code != 200:
                break

            data = response.json()
            items = self._extract_zephyr_collection(data)

            if not items:
                break

            pages += 1
            total_seen += len(items)
            for item in items:
                name = self.get_test_cycle_name(item)
                if issue_key_lower in name.casefold():
                    matched.append(item)

            # Если вернулось меньше page_size — достигли конца
            if len(items) < page_size:
                break

            start_at += page_size

        self.last_test_cycle_search_stats.append({
            'mode': 'search',
            'project_key': project_key,
            'status': status,
            'pages': pages,
            'total_seen': total_seen,
            'count': len(matched),
            'error': locals().get('search_error', ''),
        })
        return matched

    def _search_test_cycles_by_name_query(self, project_key: str, issue_key: str) -> list[dict]:
        """
        Быстрый targeted-поиск по имени. На части инсталляций Zephyr Scale это
        работает, на части возвращает 400 — тогда ниже остается полный fallback
        по projectKey.
        """
        url = f"{self.base_url}/rest/atm/1.0/testrun/search"
        query = f'projectKey = "{project_key}" AND name ~ "{issue_key}"'
        try:
            response = self._get_with_retries(
                url,
                params={'query': query, 'maxResults': 50, 'startAt': 0},
            )
        except Exception as e:
            self.last_test_cycle_search_stats.append({
                'mode': 'search-name',
                'project_key': project_key,
                'query': query,
                'status': 'exception',
                'count': 0,
                'error': str(e),
            })
            return []

        body_snippet = ''
        if response.status_code == 200:
            try:
                items = self._extract_zephyr_collection(response.json())
            except Exception:
                items = []
                body_snippet = response.text[:300]
        else:
            items = []
            body_snippet = response.text[:300]

        issue_key_lower = issue_key.lower()
        matched = []
        for item in items:
            name = self.get_test_cycle_name(item)
            if not name or issue_key_lower in name.casefold():
                matched.append(item)

        self.last_test_cycle_search_stats.append({
            'mode': 'search-name',
            'project_key': project_key,
            'query': query,
            'status': response.status_code,
            'seen': len(items),
            'count': len(matched),
            'body': body_snippet,
        })
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

    def get_test_case_key(self, tc: dict) -> str:
        return tc.get('key', tc.get('id', 'unknown'))

    def get_test_case_name(self, tc: dict) -> str:
        return tc.get('name', tc.get('summary', '—'))


class ReleaseValidator:
    def __init__(self):
        self.jira_main = JIRA(
            options={**config['jira']['options'], 'verify': False},
            token_auth=config['jira']['token']
        )
        self.confluence = Confluence(
            url=config['confluence']['url'],
            token=config['confluence']['token'],
            verify_ssl=config['confluence']['verify_ssl']
        )
        self.confluence_parent_page = config['confluence']['parent_page_id']

        self.zephyr = ZephyrScaleClient(
            base_url=config['jira']['url'],
            token=config['jira']['token'],
            verify_ssl=False
        )
        self.jira_http = requests.Session()
        self.jira_http.verify = False
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
        self._dev_status_payload_cache: dict[str, list[tuple[str, str, dict]]] = {}
        self._release_pr_targets_cache: dict[str, list[tuple[object, list[object]]]] = {}
        self._zephyr_cycle_cache = self._load_zephyr_cycle_cache()

        try:
            self.myself = self.jira_main.myself()
            self.my_account_id = self.myself.get('accountId') or self.myself.get('name')
            print(f"🤖 Авторизован как: {self.myself.get('displayName')} ({self.my_account_id})")
        except Exception as e:
            print(f"⚠️ Не удалось получить информацию о пользователе: {e}")
            self.my_account_id = None

    def _log_issue(self, issue_obj_or_key, status, message):
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
            self._jira_field_name_to_id_cache = {}
            try:
                for field in self.jira_main.fields():
                    field_name = normalize_field_text(field.get('name', '')).casefold()
                    field_id = field.get('id')
                    if field_name and field_id:
                        self._jira_field_name_to_id_cache[field_name] = field_id
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
        if release_key not in self._zephyr_release_cycles_cache:
            raw_cycles = self.zephyr.get_test_cycles_for_issue(release_key)
            if not raw_cycles:
                raw_cycles = self._get_test_cycles_from_jira_release_metadata(release_key)
            if not raw_cycles:
                raw_cycles = self._get_test_cycles_from_direct_cache_or_scan(release_key)
            cycles = [self._get_cycle_name_from_zephyr_item(cycle) for cycle in raw_cycles]
            self._zephyr_release_cycles_cache[release_key] = [
                (key, name) for key, name in cycles if key or name
            ]
            if not self._zephyr_release_cycles_cache[release_key]:
                self._log_test_cycle_search_debug(release_key)
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
        """Найти ТЦ через локальный кэш или bounded direct scan по ключам HRPQA-C..."""
        cached_cycles = self._get_cached_test_cycles_for_release(release_key)
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

        high_watermark = self._refresh_direct_scan_high_watermark(project_key, high_watermark)
        min_number = max(1, high_watermark - ZEPHYR_DIRECT_CYCLE_SCAN_LIMIT + 1)
        found = []

        for cycle_number in range(high_watermark, min_number - 1, -1):
            cycle_key = f"{project_key}-C{cycle_number}"
            details = self._get_cached_or_fetch_cycle_details(project_key, cycle_key)
            if not details:
                continue
            if self._test_cycle_belongs_to_release(details, release_key):
                found.append(details)

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
        if ZEPHYR_DIRECT_CYCLE_SCAN_START.isdigit():
            return int(ZEPHYR_DIRECT_CYCLE_SCAN_START)
        project_cache = self._zephyr_cycle_cache.get('projects', {}).get(project_key, {})
        cached_max = project_cache.get('max_seen')
        return int(cached_max) if str(cached_max).isdigit() else 0

    def _refresh_direct_scan_high_watermark(self, project_key: str, high_watermark: int) -> int:
        current = high_watermark
        misses = 0
        for cycle_number in range(high_watermark + 1, high_watermark + ZEPHYR_DIRECT_CYCLE_SCAN_FORWARD_LIMIT + 1):
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
        issue_key = str(cycle_details.get('issueKey') or '')
        if issue_key:
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
            self._log_issue("GENERAL", "warning", f"Не удалось сохранить кэш Zephyr ТЦ: {e}")

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

    def _collect_test_cycle_keys_from_remote_links(self, release_key: str) -> set[str]:
        url = f"{config['jira']['url'].rstrip('/')}/rest/api/2/issue/{release_key}/remotelink"
        try:
            response = self.jira_http.get(url, timeout=12)
            if response.status_code != 200:
                return set()
            return self._extract_test_cycle_keys(response.json())
        except Exception:
            return set()

    def _collect_test_cycle_keys_from_issue_properties(self, release_key: str) -> set[str]:
        base_url = config['jira']['url'].rstrip('/')
        url = f"{base_url}/rest/api/2/issue/{release_key}/properties"
        try:
            response = self.jira_http.get(url, timeout=12)
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
            property_url = f"{url}/{property_key}"
            try:
                property_response = self.jira_http.get(property_url, timeout=12)
                if property_response.status_code == 200:
                    cycle_keys.update(self._extract_test_cycle_keys(property_response.json()))
            except Exception:
                continue
        return cycle_keys

    def _collect_test_cycle_keys_from_issue_raw(self, release_key: str) -> set[str]:
        try:
            issue = self.jira_main.issue(
                release_key,
                fields='*all',
                expand='renderedFields,names,schema',
            )
            return self._extract_test_cycle_keys(issue.raw)
        except Exception:
            return set()

    def _collect_test_cycle_keys_from_issue_html(self, release_key: str) -> set[str]:
        url = f"{config['jira']['url'].rstrip('/')}/browse/{release_key}"
        try:
            response = self.jira_http.get(
                url,
                headers={'Accept': 'text/html,application/xhtml+xml'},
                timeout=12,
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
            for marker in ('zephyr', 'tm4j', 'atm', 'test', 'cycle', 'run')
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
        if str(cycle_details.get('issueKey', '')).casefold() == release_key.casefold():
            return True
        try:
            return release_key.casefold() in json.dumps(cycle_details, ensure_ascii=False).casefold()
        except Exception:
            return False

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
        technical_statuses = {429, 500, 502, 503, 504}
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
            self._zephyr_cycle_details_cache[cycle_key] = self.zephyr.get_test_cycle_details(cycle_key)
        return self._zephyr_cycle_details_cache[cycle_key]

    def _get_test_case_details_cached(self, tc_key: str) -> Optional[dict]:
        if tc_key not in self._zephyr_test_case_details_cache:
            self._zephyr_test_case_details_cache[tc_key] = self.zephyr.get_test_case_details(tc_key)
        return self._zephyr_test_case_details_cache[tc_key]

    def _get_issue_test_cases_cached(self, issue_key: str) -> tuple[list[dict], Optional[str]]:
        if issue_key not in self._zephyr_issue_test_cases_cache:
            self._zephyr_issue_test_cases_cache[issue_key] = self.zephyr.get_test_cases_for_issue(issue_key)
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
        return issue_key.casefold() in payload_text

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
                maxResults=500
            )
        except Exception:
            return {}
        return {
            issue.key: issue.fields.issuetype.name.casefold()
            for issue in issues
            if getattr(issue.fields, 'issuetype', None)
        }

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
            if not tc_key or tc_key in seen_tc_keys:
                continue
            seen_tc_keys.add(tc_key)

            tc_details = self._get_test_case_details_cached(tc_key)
            if not tc_details:
                unresolved.append(tc_key)
                continue

            tc_status = self.zephyr.get_test_case_status(tc_details)
            if tc_status.casefold() != ZEPHYR_APPROVED_STATUS.casefold():
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
            self._log_issue(
                release_key,
                "warning",
                f"ТЦ [{cycle_key}] '{cycle_name}': не удалось получить детали ТК: "
                + ", ".join(sorted(unresolved))
            )

        if not not_approved:
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
            self._release_service_infos_cache[release_key] = None
            return None

        keys_str = ",".join(linked_keys)
        issue_types_to_check = {'story', 'bug', 'defect', 'ошибка'}
        try:
            release_items = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields=f'summary,issuetype,assignee,{service_ke_field_id},customfield_24000',
                maxResults=500
            )
        except Exception as e:
            self._log_issue(
                release_key,
                "error",
                f"{check_label}: ошибка получения задач состава релиза для проверки КЭ: {e}"
            )
            self._release_service_infos_cache[release_key] = None
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

    def check_release(self, release_key):
        self.report_data.clear()
        print(f"🔍 Проверка релиза: {release_key}")
        print("⏳ Выполняется анализ...\n")

        try:
            release = self.jira_main.issue(release_key, expand='subtasks')
            self._log_issue(release, "success", "Релизный тикет найден")
        except Exception as e:
            self._log_issue("GENERAL", "error", f"Критическая ошибка: Не удалось получить тикет релиза: {str(e)}")
            return False

        self._check_required_platform_label(release, "Release")

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

        self._check_test_subtask(release)
        self._check_artifacts(release_key)
        self._check_release_coverage(release_key)
        self._check_zephyr_test_cases(release_key, is_hotfix=is_hotfix)
        if not is_hotfix:
            release_labels = self._issue_labels_casefold(release)
            if 'back' in release_labels:
                self._check_back_release_service_test_cycles(release_key)
            if 'web' in release_labels:
                self._check_web_release_service_test_cycles(release_key)
        self._check_bugs(release_key, is_hotfix=is_hotfix)
        self._check_stories(release_key)
        self._check_required_pull_requests(release_key)
        self._check_gigacode_aifixed_labels(release_key)
        self._check_cloud_label(release_key, release.fields.summary)
        self._check_sbrppl_third_party_label(release_key)
        self._check_sbrppl_story_points(release_key)
        self._check_summary_description_match(release_key)

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
                if self.zephyr.get_test_case_key(tc).startswith(ZEPHYR_TC_PROJECT_KEY)
            ]

            if not hrpqa_test_cases:
                # АПИ ответил успешно, ТК просто нет
                self._log_issue(
                    issue_key, "warning",
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
                if tc_status.casefold() != ZEPHYR_APPROVED_STATUS.casefold():
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
                                f"Zephyr ТК [{tc_key}]: поле '{ZEPHYR_TESTING_TYPE_FIELD}' "
                                f"не найдено в кастомных полях ТК"
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
                maxResults=500
            )
        except Exception:
            return result

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
                maxResults=500
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
                self._check_test_cycle_cases_approved(release_key, tc_key, tc_name)
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
                self._check_test_cycle_cases_approved(release_key, tc_key, tc_name)
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
                    self._check_test_cycle_testing_type(release_key, tc_key, tc_name, 'НФ')
                    self._check_test_cycle_cases_approved(release_key, tc_key, tc_name)
                else:
                    self._log_issue(
                        release_key,
                        "error",
                        f"Back release: для КЭ '{service_ke}' есть Story с 'Новая функциональность' = 'Да', "
                        f"но отсутствует ТЦ Zephyr по маске '{api_nf_mask_hint}'"
                    )

    def _check_web_release_service_test_cycles(self, release_key: str) -> None:
        """
        Для web-релизов проверяет наличие Zephyr ТЦ по каждой КЭ:
          - {release}.{КЭ}.ipad/pwa/safari/sberbrowser.НФ — если внутри КЭ есть Story
            с "Новая функциональность" = "Да";
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

        for service_info in services.values():
            service_ke = str(service_info['display'])
            issue_list = ', '.join(sorted(service_info['issue_keys']))
            service_aliases = service_ke_name_aliases(service_ke)
            project_keys = {str(project_key).casefold() for project_key in service_info.get('project_keys', set())}
            is_neuroui = bool(project_keys) and project_keys <= {'neuroui'}

            if service_info['has_nf_story']:
                nf_mask_hint = build_cycle_mask_hint(
                    release_key,
                    service_aliases,
                    WEB_DEVICE_BROWSER_NF_CHANNEL_ALIASES,
                    'НФ',
                )
                nf_match = self._find_matching_cycle(
                    cycles,
                    release_key,
                    service_ke,
                    WEB_DEVICE_BROWSER_NF_CHANNEL_ALIASES,
                    BACK_NF_CYCLE_ALIASES,
                )
                if nf_match:
                    tc_key, tc_name = nf_match
                    self._log_issue(
                        release_key,
                        "success",
                        f"Web release: для КЭ '{service_ke}' найден iPad/PWA/Safari/SberBrowser НФ ТЦ "
                        f"[{tc_key}] '{tc_name}' ✓"
                    )
                    self._check_test_cycle_testing_type(release_key, tc_key, tc_name, 'НФ')
                    self._check_test_cycle_cases_approved(release_key, tc_key, tc_name)
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
                    self._check_test_cycle_cases_approved(release_key, tc_key, tc_name)
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

    def _check_test_cycles(self, release_key: str):
        """
        Проверка тест-циклов (ТЦ) в составе релиза.

        Правила:
          1. ТЦ получаем через Zephyr API (поиск + issuelink, все проекты).
          2. Если есть Story с 'Новая функциональность' = 'Да' -> должен быть
             ТЦ с 'НФ' или 'NF' в названии,
             кастомное поле 'Вид тестирования' = 'НФ',
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
                elif vt.strip().casefold() != 'нф':
                    self._log_issue(
                        release_key, "error",
                        f"ТЦ [{tc_key}] '{name}': '{ZEPHYR_TESTING_TYPE_FIELD}' = '{vt}', ожидается 'НФ'"
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
                    if "Автоматическая проверка релиза" in comment.body or "Результат проверки" in comment.body:
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

        if is_success:
            comment_body = (
                "{panel:title=Автоматическая проверка релиза|borderStyle=solid|borderColor=#14892c|titleBGColor=#14892c|titleColor=#ffffff}\n"
                "✅ *Релиз готов к выпуску!*\n\n"
                "Все обязательные проверки качества пройдены успешно.\n"
                "Анализ выполнен автоматически."
                "{panel}"
            )
        else:
            table_rows = []
            sorted_items = sorted(self.report_data.items(), key=lambda x: (0 if x[1]['errors'] else 1, x[0]))
            for key, data in sorted_items:
                if not data['errors']:
                    continue
                assignee = data['assignee']
                summary = data['summary'][:50] if data['summary'] else "—"
                url = data['url']
                # В Jira wiki-разметке:
                # • \\ — перенос строки внутри ячейки таблицы
                # • | внутри текста ячейки ломает таблицу — заменяем на HTML-энтити
                def _escape(text: str) -> str:
                    safe_text = normalize_field_text(text)
                    safe_text = html.escape(safe_text, quote=False)
                    replacements = {
                        '|': '&#124;',
                        '{': '&#123;',
                        '}': '&#125;',
                        '[': '&#91;',
                        ']': '&#93;',
                        '*': '&#42;',
                        '_': '&#95;',
                        '#': '&#35;',
                    }
                    for raw_char, escaped_char in replacements.items():
                        safe_text = safe_text.replace(raw_char, escaped_char)
                    return safe_text
                error_lines = [f"• {_escape(e)}" for e in data['errors']]
                error_text = " \\\\ ".join(error_lines)
                safe_summary = _escape(summary)
                safe_assignee = _escape(assignee)
                key_cell = "ОБЩЕЕ" if key == "GENERAL" else f"[{key}|{url}]"
                table_rows.append(f"| {key_cell} | {safe_summary} | {safe_assignee} | {error_text} |")

            table_body = "\n".join(table_rows) if table_rows else "| — | — | — | Проблем не найдено |"
            comment_body = (
                "{panel:title=Результат проверки: НАЙДЕНЫ ОШИБКИ|borderStyle=solid|borderColor=#de350b|titleBGColor=#de350b|titleColor=#ffffff}\n"
                "❌ *Релиз не готов к выпуску*\n"
                "Необходимо исправить следующие замечания:\n\n"
                "|| Задача || Тема || Ответственный || Ошибки ||\n"
                f"{table_body}\n\n"
                "Пожалуйста, исправьте ошибки и перезапустите проверку."
                "{panel}"
            )

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

    def _get_tester_worklog_authors(self, issue: object) -> set[str]:
        """Возвращает множество тестировщиков со списанием времени > 0 в задаче."""
        authors: set[str] = set()
        try:
            worklogs = self.jira_main.worklogs(issue.key)
        except Exception:
            return authors

        for wl in worklogs:
            author_name = self._get_author_name(wl.author)
            if is_allowed_tester(author_name) and wl.timeSpentSeconds > 0:
                authors.add(author_name)
        return authors

    def _get_linked_testing_tasks(self, issue: object) -> list[str]:
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
            except Exception:
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
            try:
                artifact = self.jira_main.issue(artifact_key)
                comments = self.jira_main.comments(artifact.key)
                comment_authors: set[str] = set()
                for comment in comments:
                    author_name = self._get_author_name(comment.author)
                    if is_allowed_tester(author_name):
                        comment_authors.add(author_name)

                if not comment_authors:
                    self._log_issue(artifact, "error", "Нет комментариев от тестировщика")
                else:
                    self._log_issue(artifact, "success", f"Есть комментарии от: {', '.join(comment_authors)}")

                # Обязательная проверка:
                # у тестировщика, оставившего комментарий, должно быть списанное время
                # либо в самой Story/Bug, либо (для Story) в связанной тестовой Task.
                direct_worklog_authors = self._get_tester_worklog_authors(artifact)
                worklog_authors = set(direct_worklog_authors)

                issue_type = artifact.fields.issuetype.name.lower() if artifact.fields.issuetype else ''
                if issue_type == 'story':
                    for task_key in self._get_linked_testing_tasks(artifact):
                        try:
                            task_issue = self.jira_main.issue(task_key)
                        except Exception:
                            continue
                        worklog_authors.update(self._get_tester_worklog_authors(task_issue))

                missing_time_authors = sorted(comment_authors - worklog_authors)
                if missing_time_authors:
                    self._log_issue(
                        artifact,
                        "error",
                        "Нет затреканного времени от тестировщиков с комментариями: "
                        + ", ".join(missing_time_authors)
                    )
                else:
                    self._log_issue(
                        artifact,
                        "success",
                        "У всех тестировщиков с комментариями есть списанное время ✓"
                    )
            except Exception as e:
                self._log_issue(artifact_key, "error", f"Ошибка проверки артефактов: {e}")

    def _check_release_coverage(self, release_key):
        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            return

        keys_str = ",".join(linked_keys)
        jql_covered = f'key in ({keys_str}) AND issue in hasTestCoverage()'

        try:
            covered_issues = self.jira_main.search_issues(jql_covered, fields='key', maxResults=1000)
            covered_keys = {issue.key for issue in covered_issues}
        except Exception as e:
            self._log_issue("GENERAL", "warning", f"Не удалось проверить покрытие (плагин Xray?): {e}")
            return

        for key in linked_keys:
            if key == release_key:
                continue
            if key not in covered_keys:
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

    # Целевое максимальное суммарное время (в днях) нахождения Story
    # в статусах тестирования по проектной области.
    STORY_MONITORED_STATUSES = ["READY FOR IFT", "IFT", "READY FOR UAT", "UAT"]
    STORY_MAX_TESTING_DAYS: dict[str, float] = {
        "HRM":        8.9,
        "HRC":        9.9,
        "NEUROUI":    5.5,
        "SFILE":      6.0,
        "SBRPPL":     6.0,
        "PERFREVIEW": 5.2,
        "HRPASSIST": 10.0,
    }
    STORY_MIN_TESTING_RATIO = 0.5

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
            f"summary,description,issuetype,status,assignee,labels,issuelinks,"
            f"{','.join(STORY_FIELDS.keys())},{EPIC_LINK_FIELD},"
            f"{REQUIREMENTS_FIELD},{ARCHITECTURE_IMPACT_FIELD}"
        )

        try:
            story_issues = self.jira_main.search_issues(
                f'key in ({keys_str}) AND issuetype = Story',
                fields=fields_req,
                maxResults=100
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

            # --- 0. Проверка обязательного лейбла контура ---
            self._check_required_platform_label(story, "Story")

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
            requirements_block_is_valid = self._check_story_requirements_block(story)
            if story_full is not None:
                self._check_story_requirements_timing(story, story_full.changelog)
            if not requirements_block_is_valid:
                story_status = story.fields.status.name if getattr(story.fields, 'status', None) else ''
                if normalize_status_name(story_status) in CORRUPTED_STORY_STATUSES:
                    self._log_issue(
                        story, "error",
                        f"Story испорчена и влияет на метрику модели зрелости: "
                        f"проверка блока требований/архитектуры не выполнена, текущий статус '{story_status}'"
                    )

            # --- 4. Проверка времени в тест-статусах ---
            max_days = self.STORY_MAX_TESTING_DAYS.get(story_project)
            if max_days is not None:
                min_days = max_days * self.STORY_MIN_TESTING_RATIO
                try:
                    if story_full is None:
                        story_full = self.jira_main.issue(story.key, expand='changelog')
                    actual_days = self._calc_status_days(
                        story_full.changelog,
                        self.STORY_MONITORED_STATUSES
                    )
                    actual_days_rounded = round(actual_days, 2)
                    min_days_rounded = round(min_days, 2)
                    if actual_days > max_days:
                        self._log_issue(
                            story, "error",
                            f"Story: суммарное время в тест-статусах "
                            f"({actual_days_rounded} д.) превышает целевое ({max_days} д.) "
                            f"для проектной области {story_project}"
                        )
                    elif actual_days < min_days:
                        self._log_issue(
                            story, "error",
                            f"Story: суммарное время в тест-статусах "
                            f"({actual_days_rounded} д.) меньше 50% норматива "
                            f"({min_days_rounded} д. из {max_days} д.) "
                            f"для проектной области {story_project}"
                        )
                    else:
                        self._log_issue(
                            story, "success",
                            f"Story: время в тест-статусах {actual_days_rounded} д. "
                            f"в пределах 50–100% норматива ({min_days_rounded}–{max_days} д.) ✓"
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

    def _json_contains_pull_request(self, value: object) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                key_normalized = str(key).casefold()
                if 'pullrequest' in key_normalized or 'pull_request' in key_normalized or 'pull request' in key_normalized:
                    if item:
                        return True
                if self._json_contains_pull_request(item):
                    return True
        elif isinstance(value, list):
            return any(self._json_contains_pull_request(item) for item in value)
        return False

    def _json_contains_commit(self, value: object) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                key_normalized = str(key).casefold()
                if key_normalized in {'commit', 'commits'} and item:
                    return True
                if self._json_contains_commit(item):
                    return True
        elif isinstance(value, list):
            return any(self._json_contains_commit(item) for item in value)
        return False

    def _get_dev_status_payloads(self, issue_id: str) -> list[tuple[str, str, dict]]:
        if issue_id in self._dev_status_payload_cache:
            return self._dev_status_payload_cache[issue_id]

        base_url = config['jira']['url'].rstrip('/')
        payloads: list[tuple[str, str, dict]] = []
        application_types = ('stash', 'bitbucket', 'bitbucket-server')
        data_types = ('pullrequest', 'repository', 'commit')

        for application_type in application_types:
            for data_type in data_types:
                url = f"{base_url}/rest/dev-status/latest/issue/detail"
                try:
                    response = self.jira_http.get(
                        url,
                        params={
                            'issueId': issue_id,
                            'applicationType': application_type,
                            'dataType': data_type,
                        },
                        timeout=30,
                    )
                    if response.status_code == 200:
                        payloads.append((application_type, data_type, response.json()))
                except Exception:
                    continue

        self._dev_status_payload_cache[issue_id] = payloads
        return payloads

    def _dev_status_payload_has_pull_request(self, data_type: str, payload: dict) -> bool:
        if self._json_contains_pull_request(payload):
            return True
        if data_type != 'pullrequest':
            return False

        details = payload.get('detail') if isinstance(payload, dict) else None
        if isinstance(details, list):
            return any(bool(item) for item in details)
        if isinstance(details, dict):
            return bool(details)
        return False

    def _issue_has_pull_request(self, issue) -> bool:
        issue_id = str(getattr(issue, 'id', '') or '')
        if not issue_id:
            return False

        for _, data_type, payload in self._get_dev_status_payloads(issue_id):
            if self._dev_status_payload_has_pull_request(data_type, payload):
                return True

        return False

    def _issue_has_gigacode_pull_request(self, issue) -> Optional[str]:
        issue_id = str(getattr(issue, 'id', '') or '')
        if not issue_id:
            return None

        for _, _, payload in self._get_dev_status_payloads(issue_id):
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
        if not linked_keys:
            self._release_pr_targets_cache[release_key] = []
            return []

        keys_str = ",".join(linked_keys)
        try:
            release_issues = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields='summary,issuetype,assignee,labels,issuelinks',
                maxResults=500
            )
        except Exception as e:
            self._log_issue("GENERAL", "error", f"Ошибка получения Story/Bug для проверки PR: {e}")
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
                    maxResults=500
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

        for target_issue, issues_to_check in targets:
            pr_issue = None
            for issue_to_check in issues_to_check:
                try:
                    if pr_issue is None and self._issue_has_pull_request(issue_to_check):
                        pr_issue = issue_to_check
                except Exception as e:
                    self._log_issue(
                        target_issue,
                        "warning",
                        f"Pull Request: не удалось проверить {issue_to_check.key}: {e}"
                    )

            if pr_issue:
                self._log_issue(
                    target_issue,
                    "success",
                    f"Pull Request найден в {pr_issue.key} ✓"
                )
            else:
                self._log_issue(
                    target_issue,
                    "error",
                    "Pull Request не найден. Для каждой Story/Bug в составе релиза должен быть PR"
                )

    def _check_gigacode_aifixed_labels(self, release_key: str) -> None:
        AIFIXED_LABEL = 'AIFIXED'
        targets = self._get_release_pr_targets(release_key)
        if not targets:
            return

        print(f"   Проверка AIFIXED по GigaCode PR/commit для {len(targets)} Story/Bug...")

        for target_issue, issues_to_check in targets:
            matched_issue = None
            matched_marker = None
            for issue_to_check in issues_to_check:
                try:
                    gigacode_marker = self._issue_has_gigacode_pull_request(issue_to_check)
                    if gigacode_marker and matched_issue is None:
                        matched_issue = issue_to_check
                        matched_marker = gigacode_marker
                except Exception as e:
                    self._log_issue(
                        target_issue,
                        "warning",
                        f"AIFIXED: не удалось проверить {issue_to_check.key}: {e}"
                    )

            if not matched_issue:
                self._log_issue(target_issue, "success", "AIFIXED: GigaCode PR/commit не найден — лейбл AIFIXED не требуется ✓")
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
                maxResults=200
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
                maxResults=200
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
                maxResults=200
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
                maxResults=500,
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

        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            jql = f'parent = {release_key}'
        else:
            keys_str = ",".join(linked_keys)
            jql = f'parent = {release_key} OR key in ({keys_str})'

        try:
            fields_req = f"summary,description,priority,labels,issuetype,status,assignee,customfield_16901,customfield_11507,{STAND_FIELD_ID},*all"
            all_issues = self.jira_main.search_issues(jql, fields=fields_req, maxResults=100)
        except Exception as e:
            self._log_issue("GENERAL", "error", f"Ошибка поиска багов: {e}")
            return

        bugs = [i for i in all_issues if i.fields.issuetype.name in ['Bug', 'Defect', 'Ошибка']]

        if not bugs:
            self._log_issue("GENERAL", "warning", "Баги не найдены в составе релиза")
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
            assignee = data['assignee']

            if key == "GENERAL":
                print(f"{status_icon} ОБЩИЕ ПРОВЕРКИ:")
            else:
                print(f"{status_icon} [{key}] {data['summary'][:60]}... ({assignee})")
                print(f"   🔗 {data['url']}")

            for err in data['errors']:
                print(f"   🔴 {err}")

            for warn in data['warnings']:
                print(f"   ⚠️  {warn}")

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


if __name__ == "__main__":
    validator = ReleaseValidator()

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

    if len(sys.argv) > 1:
        release_input = sys.argv[1]
    else:
        release_input = "https://jira.sberbank.ru/browse/HRPRELEASE-120486"
        print(f"⚠️ Используем дефолтный ключ: {release_input}")

    if not validator.generate_report(release_input):
        sys.exit(1)
    else:
        sys.exit(0)
