from jira import JIRA
from atlassian import Confluence
import os
import sys
import re
import time
import urllib3
import logging
import requests
from html.parser import HTMLParser
from pathlib import Path
from collections import defaultdict
from typing import Optional

try:
    from bugsAnalyse import analyze_bugs_standalone
except ImportError:
    print("⚠️ Внимание: не удалось импортировать analyze_bugs_standalone из bugsAnalyse.py")
    def analyze_bugs_standalone(*args, **kwargs): return []

try:
    from gigachat import get_gigachat_token, check_summary_description_match
except ImportError:
    print("⚠️ Внимание: не удалось импортировать функции из gigachat.py")
    def get_gigachat_token(env): return None
    def check_summary_description_match(env, batch, access_token, max_retries=3): return None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Глушим все WARNING логи (jira rate-limit, confluence, requests и т.д.)
logging.getLogger("jira").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("atlassian").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)

script_dir = Path(__file__).parent
parent_dir = script_dir.parent
sys.path.insert(0, str(parent_dir))

from config import config

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

# Пространство проекта, в котором лежат тест-кейсы
ZEPHYR_TC_PROJECT_KEY = "HRPQA"

# Имя кастомного поля "Вид тестирования" в Zephyr Scale ТК.
# Поле ищется в customFields ответа API GET /rest/atm/1.0/testcase/{key}.
# Если на вашем инстансе поле называется иначе — подставьте нужное значение.
ZEPHYR_TESTING_TYPE_FIELD = "Вид тестирования"

# Допустимые значения "Вида тестирования"
ZEPHYR_TESTING_TYPE_NEW = "Новый функционал"
ZEPHYR_TESTING_TYPE_REGRESSION = "Регресс"


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


def extract_jira_field_value(raw_val: object) -> str | None:
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
        self.max_retries = 3
        self.retry_backoff_seconds = 2
        self.retry_status_codes = {429, 500, 502, 503, 504}
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session.headers.update({
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        })

    def _get_with_retries(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault('timeout', 30)
        last_exception = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, **kwargs)
                if response.status_code not in self.retry_status_codes or attempt == self.max_retries:
                    return response
            except requests.RequestException as e:
                last_exception = e
                if attempt == self.max_retries:
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

    def get_test_cycles_for_issue(self, issue_key: str, issue_id: Optional[str] = None) -> list[dict]:
        """
        Возвращает список ТЦ, привязанных к релизу.

        API testrun/search поддерживает только фильтры 'projectKey' и 'folder'.
        Поэтому получаем все ТЦ проекта постранично и фильтруем по наличию
        issue_key в названии ТЦ — связь хранится только в name.
        """
        issue_project = issue_key.split('-')[0] if '-' in issue_key else ''
        project_keys_to_try = [issue_project] if issue_project else []
        if ZEPHYR_TC_PROJECT_KEY not in project_keys_to_try:
            project_keys_to_try.append(ZEPHYR_TC_PROJECT_KEY)

        seen_keys: set[str] = set()
        result: list[dict] = []

        for proj in project_keys_to_try:
            found = self._search_test_cycles_by_name(proj, issue_key)
            for item in found:
                key = item.get('key', '')
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    result.append(item)

        return result

    def _search_test_cycles_by_name(self, project_key: str, issue_key: str) -> list[dict]:
        """
        GET /rest/atm/1.0/testrun/search?query=projectKey = "{project_key}"
        Пагинация по 50, фильтруем на стороне клиента по issue_key в name.
        API поддерживает только поля projectKey и folder в query.
        """
        url = f"{self.base_url}/rest/atm/1.0/testrun/search"
        query = f'projectKey = "{project_key}"'
        page_size = 50
        start_at = 0
        matched: list[dict] = []
        issue_key_lower = issue_key.lower()

        while True:
            try:
                response = self._get_with_retries(
                    url,
                    params={'query': query, 'maxResults': page_size, 'startAt': start_at},
                )
            except Exception:
                break

            if response.status_code != 200:
                break

            data = response.json()
            items = data if isinstance(data, list) else data.get('results', data.get('testRuns', data.get('values', [])))

            if not items:
                break

            for item in items:
                name = item.get('name', '')
                if issue_key_lower in name.lower():
                    matched.append(item)

            # Если вернулось меньше page_size — достигли конца
            if len(items) < page_size:
                break

            start_at += page_size

        return matched

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

        self._check_test_subtask(release)
        self._check_artifacts(release_key)
        self._check_release_coverage(release_key)
        self._check_zephyr_test_cases(release_key, is_hotfix=is_hotfix)
        self._check_bugs(release_key, is_hotfix=is_hotfix)
        self._check_stories(release_key)
        self._check_gigacode_pull_requests(release_key)
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
          2. «Вид тестирования» в ТК:
             - Bug/Defect/Ошибка → всегда «Регресс»
             - Story с customfield_24000 = «Да» → «Новый функционал»
             - Story с customfield_24000 = «Нет» → «Регресс»
             - Прочие типы задач → проверка не выполняется
          3. Hotfix: ТК к багам не должны иметь «Новый функционал» (явная проверка)
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

        # Для хотфикса собираем множество ключей багов — понадобится в цикле
        hotfix_bug_keys: set[str] = set()
        if is_hotfix:
            BUG_TYPES_LOWER = {'bug', 'defect', 'ошибка'}
            try:
                keys_str = ",".join(linked_keys)
                hotfix_issues = self.jira_main.search_issues(
                    f'key in ({keys_str})',
                    fields='issuetype',
                    maxResults=500
                )
                for hi in hotfix_issues:
                    if hi.fields.issuetype.name.lower() in BUG_TYPES_LOWER:
                        hotfix_bug_keys.add(hi.key)
            except Exception:
                pass

        total_tc_checked = 0
        total_not_approved = 0

        for issue_key in linked_keys:
            test_cases, tc_error = self.zephyr.get_test_cases_for_issue(issue_key)

            if tc_error is not None:
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

            for tc in hrpqa_test_cases:
                total_tc_checked += 1
                tc_key = self.zephyr.get_test_case_key(tc)
                tc_name = self.zephyr.get_test_case_name(tc)
                tc_details = self.zephyr.get_test_case_details(tc_key)
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

                # --- Проверка 1: статус Approved ---
                if tc_status.lower() != ZEPHYR_APPROVED_STATUS.lower():
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
                if expected_testing_type or issue_key in hotfix_bug_keys:
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
                            # Hotfix: ТК бага не должен иметь «Новый функционал»
                            if issue_key in hotfix_bug_keys and actual_type.strip().lower() == ZEPHYR_TESTING_TYPE_NEW.strip().lower():
                                self._log_issue(
                                    issue_key, "error",
                                    f"Hotfix: Zephyr ТК [{tc_key}] «{tc_name}»: "
                                    f"'{ZEPHYR_TESTING_TYPE_FIELD}' = '{actual_type}' — "
                                    f"для хотфикса запрещён '{ZEPHYR_TESTING_TYPE_NEW}', допустим только '{ZEPHYR_TESTING_TYPE_REGRESSION}'"
                                )
                            elif expected_testing_type and actual_type.strip().lower() != expected_testing_type.strip().lower():
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
        Для каждой задачи из состава релиза определяет ожидаемый «Вид тестирования».

        Правила:
          Bug / Defect / Ошибка  → всегда 'Регресс'
          Story, customfield_24000 = 'Да'  → 'Новый функционал'
          Story, customfield_24000 = 'Нет' → 'Регресс'
          Прочие типы (если нет customfield_24000) → не попадают в маппинг

        Возвращает dict: issue_key → ожидаемый вид тестирования.
        Если задача не попала в словарь — проверка вида тестирования не выполняется.
        """
        result = {}
        keys_str = ",".join(linked_keys)
        BUG_TYPES = {'bug', 'defect', 'ошибка'}

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

            # Баги — всегда «Регресс»
            if issue_type in BUG_TYPES:
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
            self._log_issue(
                "GENERAL", "error",
                "Zephyr: нет тест-циклов, привязанных к релизу"
            )
            return

        print(f"   Найдено {len(cycles)} ТЦ")

        # Собираем детали каждого ТЦ
        cycle_details = []  # список (key, name, name_lower, details)
        for tc_raw in cycles:
            tc_key = tc_raw.get('key', '')
            details = self.zephyr.get_test_cycle_details(tc_key)
            if details:
                name = details.get('name', tc_raw.get('name', ''))
            else:
                name = tc_raw.get('name', '')
            cycle_details.append((tc_key, name, name.lower(), details))

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

        for tc_key, name, name_lower, details in cycle_details:

            # Классификация
            is_nf = ('нф' in name_lower or 'nf' in name_lower)
            is_regress = ('регресс' in name_lower or 'regress' in name_lower)
            is_web = ('web' in name_lower or 'вэб' in name_lower)
            is_pwa = ('pwa' in name_lower)
            is_ipad = ('ipad' in name_lower)

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
                        if tc_status.lower() != ZEPHYR_APPROVED_STATUS.lower():
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
                elif vt.strip().lower() != 'нф':
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
                elif vt.strip().lower() != 'регресс':
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
                # В Confluence wiki-разметке:
                # • \\ — перенос строки внутри ячейки таблицы
                # • | внутри текста ячейки ломает таблицу — заменяем на HTML-энтити
                def _escape(text: str) -> str:
                    return text.replace('|', '&#124;')
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

    def _get_consist_of_issues(self, release_key):
        consist_of_issues = []
        try:
            release = self.jira_main.issue(release_key)
            if hasattr(release.fields, 'issuelinks') and release.fields.issuelinks:
                for link in release.fields.issuelinks:
                    link_type_name = link.type.name.lower() if hasattr(link.type, 'name') else ''
                    if 'consist' in link_type_name or 'part' in link_type_name:
                        if hasattr(link, 'outwardIssue'):
                            consist_of_issues.append(link.outwardIssue.key)
                        elif hasattr(link, 'inwardIssue'):
                            consist_of_issues.append(link.inwardIssue.key)
        except Exception as e:
            self._log_issue("GENERAL", "error", f"Ошибка получения связей: {str(e)}")
        return consist_of_issues

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
                    try:
                        # Парсим с timezone
                        ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    except Exception:
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
        ARCHITECTURE_APPROVED_ALLOWED_IMPACTS = {
            None,
            'это первое внедрение для кэ',
            'в результате доработки этот кэ будет отправлен в архив или выведен',
            'в результате доработки зависимый кэ будет отправлен в архив или выведен',
        }

        rows = self._extract_story_requirements_rows(getattr(story.fields, REQUIREMENTS_FIELD, None))
        architecture_impact = extract_jira_field_value(getattr(story.fields, ARCHITECTURE_IMPACT_FIELD, None))
        architecture_impact_normalized = (
            normalize_field_text(architecture_impact).casefold()
            if architecture_impact
            else None
        )

        def get_row(label: str) -> dict | None:
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
            if architecture_impact_normalized != ARCHITECTURE_NO_IMPACT:
                block_is_valid = False
                self._log_issue(
                    story, "error",
                    f"Story: Архитектура = 'Не меняется', но поле '{ARCHITECTURE_IMPACT_FIELD_NAME}' = "
                    f"'{architecture_impact or 'None'}'. Ожидается 'Не влияет на архитектуру'"
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
            elif architecture_impact_normalized not in ARCHITECTURE_APPROVED_ALLOWED_IMPACTS:
                block_is_valid = False
                self._log_issue(
                    story, "error",
                    f"Story: Архитектура = 'Утверждена', но поле '{ARCHITECTURE_IMPACT_FIELD_NAME}' = "
                    f"'{architecture_impact or 'None'}' содержит недопустимое значение"
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
            f"summary,description,issuetype,status,assignee,issuelinks,"
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

            # --- 3. Проверка блока требований и архитектуры ---
            requirements_block_is_valid = self._check_story_requirements_block(story)
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

    def _json_contains_gigacode_marker(self, value: object) -> bool:
        markers = (
            '#gigacode agent',
            'co-authored-by: gigacode',
            'co-authorer-by: gigacode',
        )
        if isinstance(value, str):
            value_normalized = value.casefold()
            return any(marker in value_normalized for marker in markers)
        if isinstance(value, dict):
            return any(self._json_contains_gigacode_marker(item) for item in value.values())
        if isinstance(value, list):
            return any(self._json_contains_gigacode_marker(item) for item in value)
        return False

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

    def _get_dev_status_payloads(self, issue_id: str) -> list[dict]:
        base_url = config['jira']['url'].rstrip('/')
        payloads = []
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
                        payloads.append(response.json())
                except Exception:
                    continue

        return payloads

    def _issue_has_gigacode_pull_request(self, issue) -> bool:
        issue_id = str(getattr(issue, 'id', '') or '')
        if not issue_id:
            return False

        payloads = self._get_dev_status_payloads(issue_id)
        has_pull_request = any(self._json_contains_pull_request(payload) for payload in payloads)
        has_commit = any(self._json_contains_commit(payload) for payload in payloads)
        has_gigacode_marker = any(self._json_contains_gigacode_marker(payload) for payload in payloads)
        return has_gigacode_marker and (has_pull_request or has_commit)

    def _add_label_if_missing(self, issue, label: str) -> bool:
        labels = list(getattr(issue.fields, 'labels', []) or [])
        if label.casefold() in {existing_label.casefold() for existing_label in labels}:
            return False

        issue.update(fields={'labels': labels + [label]})
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

    def _check_gigacode_pull_requests(self, release_key: str) -> None:
        AIFIXED_LABEL = 'AIFIXED'
        issue_types = {'story', 'bug', 'defect', 'ошибка'}

        linked_keys = self._get_consist_of_issues(release_key)
        if not linked_keys:
            return

        keys_str = ",".join(linked_keys)
        try:
            release_issues = self.jira_main.search_issues(
                f'key in ({keys_str})',
                fields='summary,issuetype,assignee,labels,issuelinks',
                maxResults=500
            )
        except Exception as e:
            self._log_issue("GENERAL", "error", f"Ошибка проверки GigaCode PR/commit: {e}")
            return

        target_issues = [
            issue for issue in release_issues
            if issue.fields.issuetype and issue.fields.issuetype.name.casefold() in issue_types
        ]

        print(f"   Проверка Pull Request с GigaCode для {len(target_issues)} Story/Bug...")

        for target_issue in target_issues:
            issues_to_check = [target_issue]

            if target_issue.fields.issuetype.name.casefold() == 'story':
                for task_key in self._get_story_task_keys(target_issue):
                    try:
                        task = self.jira_main.issue(
                            task_key,
                            fields='summary,issuetype,assignee,labels'
                        )
                    except Exception as e:
                        self._log_issue(target_issue, "warning", f"GigaCode PR/commit: не удалось получить Task {task_key}: {e}")
                        continue
                    if task.fields.issuetype and task.fields.issuetype.name.casefold() == 'task':
                        issues_to_check.append(task)

            matched_issue = None
            for issue_to_check in issues_to_check:
                try:
                    if self._issue_has_gigacode_pull_request(issue_to_check):
                        matched_issue = issue_to_check
                        break
                except Exception as e:
                    self._log_issue(
                        target_issue,
                        "warning",
                        f"GigaCode PR/commit: не удалось проверить {issue_to_check.key}: {e}"
                    )

            if not matched_issue:
                self._log_issue(target_issue, "success", "GigaCode PR/commit не найден — лейбл AIFIXED не требуется ✓")
                continue

            try:
                added = self._add_label_if_missing(target_issue, AIFIXED_LABEL)
                if added:
                    self._log_issue(
                        target_issue,
                        "success",
                        f"GigaCode PR/commit найден в {matched_issue.key}; лейбл {AIFIXED_LABEL} добавлен ✓"
                    )
                else:
                    self._log_issue(
                        target_issue,
                        "success",
                        f"GigaCode PR/commit найден в {matched_issue.key}; лейбл {AIFIXED_LABEL} уже есть ✓"
                    )
            except Exception as e:
                self._log_issue(
                    target_issue,
                    "error",
                    f"GigaCode PR/commit найден в {matched_issue.key}, но не удалось добавить лейбл {AIFIXED_LABEL}: {e}"
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
        bug_keys = []

        for bug in bugs:
            bug_keys.append(bug.key)

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

        if bug_keys:
            try:
                analysis_results = analyze_bugs_standalone(self.jira_main, bug_keys, env='ift')
                for res in analysis_results:
                    bk = res['key']
                    bug_obj = next((b for b in bugs if b.key == bk), None)
                    if not bug_obj:
                        continue

                    if not res['is_priority_correct']:
                        self._log_issue(bug_obj, "error",
                                        f"AI: Некорректный приоритет. {res['recommendations'].get('priority_analysis', '')}")

                    if not res['is_description_good']:
                        self._log_issue(bug_obj, "error",
                                        f"AI: Плохое описание. {res['recommendations'].get('description_analysis', '')}")

                    is_prod = any(b.key == bk for b in prod_bugs)
                    if is_prod:
                        rec_cause = res['recommendations'].get('missing_cause', '')
                        if '[ERROR]' in rec_cause or '[WARN]' in rec_cause:
                            self._log_issue(bug_obj, "error", f"AI: Проблема с причиной пропуска. {rec_cause}")

            except Exception as e:
                self._log_issue("GENERAL", "error", f"Сбой AI анализатора: {e}")

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


def _diag_search(validator: 'ReleaseValidator', release_key: str):
    """Диагностика: перебрать все варианты поиска ТЦ с детальным логом."""
    import json
    import re

    issue_project = release_key.split('-')[0]
    release_id = None
    try:
        ri = validator.jira_main.issue(release_key, fields='id,summary')
        release_id = str(ri.id)
        print(f"Jira issue id: {release_id}  summary: {ri.fields.summary}")
    except Exception as e:
        print(f"Failed to get issue id: {e}")

    base = validator.zephyr.base_url

    queries = [
        f'projectKey = "{issue_project}" AND issueId = "{release_id}"',
        f'projectKey = "{issue_project}" AND issueKey = "{release_key}"',
        f'projectKey = "{issue_project}" AND issueKeys IN ("{release_key}")',
        f'projectKey = "{issue_project}" AND relatedIssues IN ("{release_key}")',
        f'projectKey = "{issue_project}"',
    ]

    for q in queries:
        url = f"{base}/rest/atm/1.0/testrun/search"
        resp = validator.zephyr._get_with_retries(url, params={'query': q, 'maxResults': 10})
        data = None
        try:
            data = resp.json()
        except Exception:
            pass
        count = len(data) if isinstance(data, list) else (
            len(data.get('results', data.get('testRuns', []))) if isinstance(data, dict) else '?'
        )
        print(f"  [{resp.status_code}] count={count}  query: {q}")
        if isinstance(count, int) and count > 0:
            items = data if isinstance(data, list) else data.get('results', data.get('testRuns', []))
            for item in items[:3]:
                print(f"    -> {item.get('key','')} | {item.get('name','')[:60]}")

    # Также пробуем issuelink
    url2 = f"{base}/rest/atm/1.0/issuelink/{release_key}/testruns"
    resp2 = validator.zephyr._get_with_retries(url2)
    try:
        d2 = resp2.json()
    except Exception:
        d2 = resp2.text
    c2 = len(d2) if isinstance(d2, list) else '?'
    print(f"  [{resp2.status_code}] count={c2}  issuelink: {url2}")


if __name__ == "__main__":
    validator = ReleaseValidator()

    # Диагностика: python release_checker.py --diag-tc HRPRELEASE-C967
    if len(sys.argv) >= 3 and sys.argv[1] == '--diag-tc':
        _diag_tc(validator, sys.argv[2])
        sys.exit(0)

    # Диагностика: python release_checker.py --diag-search HRPRELEASE-120111
    if len(sys.argv) >= 3 and sys.argv[1] == '--diag-search':
        _diag_search(validator, sys.argv[2])
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
