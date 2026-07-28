#!/usr/bin/env python3
"""90-day and quarterly released Story/Bug quality metrics for Jira/Confluence.

The script finds Release 2.0 issues installed to production, collects Story/Bug
issues included in those releases, calculates independent rolling-90-day and
quarter-to-date ratios for each team, and publishes both Confluence tables.

Run locally without publishing:
    python scripts/quarterly_quality_metrics.py --no-publish

Publish to the page embedded in DEFAULT_CONFLUENCE_PAGE_ID, using config.py:
    python scripts/quarterly_quality_metrics.py
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from threading import local
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_JIRA_URL = "https://jira.sberbank.ru"
DEFAULT_CONFLUENCE_URL = "https://confluence.sberbank.ru"
HTTP_TIMEOUT_SECONDS = 60
JIRA_KEY_BATCH_SIZE = 200
# Jira ограничивает частоту одиночных GET /issue. Одна переиспользуемая
# Session работает стабильнее нескольких параллельных сессий и соответствует
# способу загрузки issuelinks в release_checker.py.
JIRA_LINK_WORKERS = 1
JIRA_ISSUE_REQUEST_INTERVAL_SECONDS = 0.35
JIRA_RATE_LIMIT_RECOVERY_SECONDS = 30
# Корпоративные Jira/Confluence используют self-signed CA. release_checker.py
# также принудительно отключает verify для этих подключений.
VERIFY_SSL = False
DEFAULT_RELEASE_PROJECT = "HRPRELEASE"
DEFAULT_RELEASE_ISSUE_TYPE = "Release 2.0"
DEFAULT_RELEASE_CREATED_SINCE = "2025-09-01"
DEFAULT_RELEASE_KE_FIELD_NAME = "КЭ"
DEFAULT_RELEASE_DATE_FIELD_NAME = "Дата установки на ПРОМ"
DEFAULT_RELEASE_DATE_FIELD_ID = "customfield_19400"
DEFAULT_RELEASE_TYPE_FIELD_ID = "customfield_23500"
DEFAULT_DETECTION_STAGE_FIELD_ID = "customfield_11507"
DEFAULT_CONFLUENCE_PAGE_ID = "24517214638"

DEFAULT_STORY_TYPES = ("Story",)
DEFAULT_STORY_STATUSES = ("Done", "Сделано", "Готово")
DEFAULT_BUG_TYPES = ("Bug", "Defect", "Ошибка")
DEFAULT_BUG_STATUSES = ("Closed", "Закрыт", "Закрыто")
DEFAULT_BUG_PRIORITIES = (
    "Critical",
    "Crytical",
    "Blocker",
    "Высокий",
    "Блокирующий",
)
DEFAULT_PSI_DETECTION_STAGES = ("PSI", "ПСИ")
DEFAULT_PROM_DETECTION_STAGES = ("PROM", "ПРОМ")
DEFAULT_RELEASE_LINK_KEYWORDS = ("consist", "part")
DEFAULT_HOTFIX_VALUES = ("Hotfix",)
DEFAULT_INSTALLED_RELEASE_STATUSES = (
    "Установлен на ПРОМ",
)
RELEASE_KE_IDS = tuple(
    dict.fromkeys(
        (
            2298599,
            8553253,
            3589425,
            3304476,
            3303802,
            3191860,
            2288712,
            2257858,
            2935717,
            3521872,
            6355438,
            5452084,
            5452083,
            5452082,
            5452085,
            3303802,
            3304476,
            7993288,
            2435236,
            5374387,
            2257817,
            2644268,
            2298078,
            5366436,
            2503797,
            3930742,
            2836020,
            3173847,
            2295205,
            2288712,
            9643400,
            9643401,
            9644023,
            9644025,
            9362600,
            9535069,
            8553253,
            9644020,
            10743713,
        )
    )
)

RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
AS_IS_FLOOR_MULTIPLIER = Decimal("0.8")

# target_ratio — уже рассчитанный коэффициент A × AS IS + B из целевой
# таблицы. Итоговый коэффициент дополнительно ограничивается снизу значением
# as_is_ratio × AS_IS_FLOOR_MULTIPLIER.
TEAM_SETTINGS_JSON = r"""
[
  {
    "team": "HRP Core UI",
    "project_keys": ["HRM"],
    "as_is_ratio": 0.11,
    "target_ratio": 0.12
  },
  {
    "team": "HRP Core Tech",
    "project_keys": ["HRC"],
    "as_is_ratio": 0.13,
    "target_ratio": 0.13
  },
  {
    "team": "Core UI 2.0 / Neuro UI",
    "project_keys": ["NEUROUI"],
    "as_is_ratio": 0.29,
    "target_ratio": 0.24
  },
  {
    "team": "Профиль сотрудника",
    "project_keys": ["SFILE"],
    "as_is_ratio": 0.21,
    "target_ratio": 0.24
  },
  {
    "team": "Продуктовая аналитика",
    "project_keys": ["HRPPA"],
    "as_is_ratio": 0.80,
    "target_ratio": 0.10
  },
  {
    "team": "Люди Сбера",
    "project_keys": ["SBRPPL"],
    "as_is_ratio": null,
    "target_ratio": 0.10
  },
  {
    "team": "Задачи и уведомления",
    "project_keys": ["PERFREVIEW"],
    "as_is_ratio": 0.34,
    "target_ratio": 0.33
  },
  {
    "team": "Ассистент HR",
    "project_keys": ["HRPASSIST"],
    "as_is_ratio": 0.09,
    "target_ratio": 0.09
  }
]
"""
TEAM_SETTINGS: tuple[dict[str, Any], ...] = tuple(json.loads(TEAM_SETTINGS_JSON))


def execution_log(message: str, *, error: bool = False) -> None:
    """Print a timestamped job log line immediately."""

    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    stream = sys.stderr if error else sys.stdout
    print(f"[{timestamp}] {message}", file=stream, flush=True)


def elapsed_seconds(started_at: float) -> str:
    return f"{time.perf_counter() - started_at:.1f} с"


def normalized(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", text)


def normalized_set(values: Iterable[str]) -> frozenset[str]:
    return frozenset(normalized(value) for value in values if str(value).strip())


def jql_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def issue_keys_jql(keys: Iterable[str]) -> str:
    cleaned = sorted({str(key).strip().upper() for key in keys if str(key).strip()})
    return ",".join(
        key if re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", key) else jql_value(key)
        for key in cleaned
    )


def batches(values: Sequence[str], size: int = 100) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def quarter_bounds(day: date) -> tuple[date, date, int]:
    quarter = (day.month - 1) // 3 + 1
    start_month = (quarter - 1) * 3 + 1
    start = date(day.year, start_month, 1)
    if quarter == 4:
        end = date(day.year + 1, 1, 1)
    else:
        end = date(day.year, start_month + 3, 1)
    return start, end, quarter


def parse_quarter(value: str) -> tuple[date, date, int]:
    match = re.fullmatch(r"(\d{4})-?[QqКк]([1-4])", value.strip())
    if not match:
        raise ValueError("Квартал должен быть в формате YYYY-QN, например 2026-Q3.")
    year = int(match.group(1))
    quarter = int(match.group(2))
    month = (quarter - 1) * 3 + 1
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if quarter == 4 else date(year, month + 3, 1)
    return start, end, quarter


def parse_jira_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, Mapping):
        for key in ("value", "date", "name"):
            parsed = parse_jira_date(value.get(key))
            if parsed is not None:
                return parsed
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def named_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(named_values(item))
        return result
    if isinstance(value, Mapping):
        for key in ("value", "name", "displayName", "key"):
            nested = value.get(key)
            if nested not in (None, ""):
                return named_values(nested)
        return []
    text = str(value).strip()
    return [text] if text else []


def named_value(value: Any) -> str:
    values = named_values(value)
    return values[0] if values else ""


@dataclass(frozen=True)
class TeamSpec:
    team: str
    project_keys: tuple[str, ...]
    as_is_ratio: Optional[Decimal]
    target_ratio: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TeamSpec":
        team = str(raw.get("team") or "").strip()
        projects_raw = raw.get("project_keys")
        if not isinstance(projects_raw, list):
            raise ValueError(f"{team or 'Команда'}: project_keys должен быть массивом.")
        project_keys = tuple(
            dict.fromkeys(str(key).strip().upper() for key in projects_raw if str(key).strip())
        )
        try:
            target_ratio = Decimal(str(raw.get("target_ratio")))
        except Exception as exc:
            raise ValueError(f"{team or 'Команда'}: неверный target_ratio.") from exc
        as_is_raw = raw.get("as_is_ratio")
        try:
            as_is_ratio = (
                None if as_is_raw in (None, "") else Decimal(str(as_is_raw))
            )
        except Exception as exc:
            raise ValueError(f"{team or 'Команда'}: неверный as_is_ratio.") from exc
        if not team:
            raise ValueError("У команды не задано имя.")
        if not project_keys:
            raise ValueError(f"{team}: не задан ни один Jira project key.")
        if target_ratio <= 0 or target_ratio >= 1:
            raise ValueError(f"{team}: target_ratio должен быть больше 0 и меньше 1.")
        if as_is_ratio is not None and as_is_ratio < 0:
            raise ValueError(f"{team}: as_is_ratio не может быть отрицательным.")
        return cls(
            team=team,
            project_keys=project_keys,
            as_is_ratio=as_is_ratio,
            target_ratio=target_ratio,
        )


def load_team_specs() -> list[TeamSpec]:
    raw = TEAM_SETTINGS
    specs = [TeamSpec.from_dict(item) for item in raw if isinstance(item, Mapping)]
    if len(specs) != len(raw):
        raise ValueError("Каждый элемент TEAM_SETTINGS должен быть словарём.")

    seen_projects: dict[str, str] = {}
    for spec in specs:
        for project_key in spec.project_keys:
            previous = seen_projects.get(project_key)
            if previous:
                raise ValueError(
                    f"Jira project {project_key} одновременно назначен командам "
                    f"'{previous}' и '{spec.team}'."
                )
            seen_projects[project_key] = spec.team
    return specs


@dataclass(frozen=True)
class MetricRules:
    story_types: frozenset[str]
    story_statuses: frozenset[str]
    bug_types: frozenset[str]
    bug_statuses: frozenset[str]
    bug_priorities: frozenset[str]
    psi_detection_stages: frozenset[str]
    prom_detection_stages: frozenset[str]

    @classmethod
    def defaults(cls) -> "MetricRules":
        return cls(
            story_types=normalized_set(DEFAULT_STORY_TYPES),
            story_statuses=normalized_set(DEFAULT_STORY_STATUSES),
            bug_types=normalized_set(DEFAULT_BUG_TYPES),
            bug_statuses=normalized_set(DEFAULT_BUG_STATUSES),
            bug_priorities=normalized_set(DEFAULT_BUG_PRIORITIES),
            psi_detection_stages=normalized_set(DEFAULT_PSI_DETECTION_STAGES),
            prom_detection_stages=normalized_set(DEFAULT_PROM_DETECTION_STAGES),
        )


@dataclass
class TeamCounts:
    stories: int = 0
    bugs: int = 0
    psi_bugs: int = 0
    prom_bugs: int = 0
    story_keys: list[str] = field(default_factory=list)
    bug_keys: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TeamMetric:
    team: str
    project_keys: tuple[str, ...]
    as_is_ratio: Optional[Decimal]
    calculated_target_ratio: Decimal
    minimum_target_ratio: Optional[Decimal]
    target_ratio: Decimal
    stories: int
    bugs: int
    psi_bugs: int
    prom_bugs: int
    actual_ratio: Optional[Decimal]
    target_attainment_percent: Optional[Decimal]
    infinite_attainment: bool
    additional_bugs_allowed: int
    additional_stories_required: int
    state: str
    story_keys: tuple[str, ...]
    bug_keys: tuple[str, ...]


def calculate_metric(spec: TeamSpec, counts: TeamCounts) -> TeamMetric:
    calculated_target = spec.target_ratio
    minimum_target = (
        spec.as_is_ratio * AS_IS_FLOOR_MULTIPLIER
        if spec.as_is_ratio is not None
        else None
    )
    target = (
        max(calculated_target, minimum_target)
        if minimum_target is not None
        else calculated_target
    )
    stories = counts.stories
    bugs = counts.bugs

    if stories > 0:
        # В эталонной таблице сначала округляется факт до сотых, а уже затем
        # считается выполнение цели: target / rounded_fact * 100.
        actual_ratio: Optional[Decimal] = (
            Decimal(bugs) / Decimal(stories)
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        actual_ratio = None

    if stories == 0 and bugs == 0:
        attainment = Decimal(100)
        infinite_attainment = False
        state = "Цель выполнена"
    elif bugs == 0 or actual_ratio == 0:
        attainment = Decimal(100)
        infinite_attainment = False
        state = "Цель выполнена"
    elif stories == 0:
        attainment = Decimal(0)
        infinite_attainment = False
        state = "Ниже цели"
    else:
        attainment = (target / actual_ratio) * Decimal(100) if actual_ratio else None
        infinite_attainment = False
        state = "Цель выполнена" if actual_ratio <= target else "Ниже цели"

    # Запас багов и Story до цели считаются по точному неравенству
    # bugs / stories <= target. Округление выше нужно только для отображения
    # и процента выполнения по правилам эталонной таблицы.
    bug_budget = int(
        (Decimal(stories) * target).to_integral_value(rounding=ROUND_FLOOR)
    )
    additional_bugs_allowed = max(0, bug_budget - bugs)

    if bugs > 0:
        stories_needed_total = int(
            (Decimal(bugs) / target).to_integral_value(rounding=ROUND_CEILING)
        )
    else:
        stories_needed_total = 0
    additional_stories_required = max(0, stories_needed_total - stories)

    return TeamMetric(
        team=spec.team,
        project_keys=spec.project_keys,
        as_is_ratio=spec.as_is_ratio,
        calculated_target_ratio=calculated_target,
        minimum_target_ratio=minimum_target,
        target_ratio=target,
        stories=stories,
        bugs=bugs,
        psi_bugs=counts.psi_bugs,
        prom_bugs=counts.prom_bugs,
        actual_ratio=actual_ratio,
        target_attainment_percent=attainment,
        infinite_attainment=infinite_attainment,
        additional_bugs_allowed=additional_bugs_allowed,
        additional_stories_required=additional_stories_required,
        state=state,
        story_keys=tuple(sorted(counts.story_keys)),
        bug_keys=tuple(sorted(counts.bug_keys)),
    )


def issue_field(issue: Mapping[str, Any], key: str) -> Any:
    fields = issue.get("fields")
    if not isinstance(fields, Mapping):
        return None
    return fields.get(key)


def issue_project_key(issue: Mapping[str, Any]) -> str:
    project = issue_field(issue, "project")
    if isinstance(project, Mapping):
        key = str(project.get("key") or "").strip()
        if key:
            return key.upper()
    return named_value(project).upper()


def story_has_done_status(raw_status: Any, rules: MetricRules) -> bool:
    if normalized(named_value(raw_status)) in rules.story_statuses:
        return True
    if not isinstance(raw_status, Mapping):
        return False
    status_category = raw_status.get("statusCategory")
    category_values = {
        normalized(value)
        for value in named_values(status_category)
    }
    if isinstance(status_category, Mapping):
        category_values.update(
            normalized(status_category.get(key))
            for key in ("key", "name")
            if status_category.get(key)
        )
    return "done" in category_values


def classify_eligible_detection_stage(
    raw_value: Any,
    rules: MetricRules,
) -> Optional[str]:
    stages = {normalized(value) for value in named_values(raw_value)}
    if stages & rules.prom_detection_stages:
        return "prom"
    if stages & rules.psi_detection_stages:
        return "psi"
    return None


def aggregate_issues(
    team_specs: Sequence[TeamSpec],
    issues: Iterable[Mapping[str, Any]],
    rules: MetricRules,
    detection_stage_field_id: str,
) -> dict[str, TeamCounts]:
    counts = {spec.team: TeamCounts() for spec in team_specs}
    project_to_team = {
        project_key: spec.team for spec in team_specs for project_key in spec.project_keys
    }
    seen_keys: set[str] = set()

    for issue in issues:
        key = str(issue.get("key") or "").strip().upper()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)

        project_key = issue_project_key(issue)
        team = project_to_team.get(project_key)
        if team is None:
            continue

        issue_type = normalized(named_value(issue_field(issue, "issuetype")))
        raw_status = issue_field(issue, "status")
        status = normalized(named_value(raw_status))

        if issue_type in rules.story_types and story_has_done_status(raw_status, rules):
            counts[team].stories += 1
            counts[team].story_keys.append(key)
            continue

        if issue_type not in rules.bug_types or status not in rules.bug_statuses:
            continue

        priority = normalized(named_value(issue_field(issue, "priority")))
        if priority not in rules.bug_priorities:
            continue

        detection_stage = classify_eligible_detection_stage(
            issue_field(issue, detection_stage_field_id),
            rules,
        )
        if detection_stage is None:
            continue

        counts[team].bugs += 1
        counts[team].bug_keys.append(key)
        if detection_stage == "prom":
            counts[team].prom_bugs += 1
        else:
            counts[team].psi_bugs += 1

    return counts


@dataclass(frozen=True)
class ConnectionSettings:
    url: str
    token: str
    username: str
    auth_mode: str
    verify_ssl: bool
    timeout_seconds: int = 60

    def auth(self) -> tuple[dict[str, str], Optional[tuple[str, str]]]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.auth_mode == "basic":
            return headers, (self.username, self.token)
        headers["Authorization"] = f"Bearer {self.token}"
        return headers, None


class HttpRequestError(RuntimeError):
    def __init__(self, service: str, status_code: int, response_text: str):
        self.service = service
        self.status_code = status_code
        self.response_text = response_text
        super().__init__(
            f"{service}: HTTP {status_code}; ответ: {response_text[:700]}"
        )


class RestClient:
    def __init__(self, service: str, settings: ConnectionSettings):
        try:
            import requests
            import urllib3
        except ImportError as exc:
            raise RuntimeError(
                "Не установлены requests/urllib3. Выполните: pip install -r requirements.txt"
            ) from exc
        if not settings.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.service = service
        self.settings = settings
        self.session = requests.Session()
        headers, auth = settings.auth()
        self.session.headers.update(headers)
        self.session.auth = auth

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        payload: Optional[Mapping[str, Any]] = None,
        safe_to_retry: bool = False,
    ) -> Any:
        url = f"{self.settings.url.rstrip('/')}/{path.lstrip('/')}"
        attempts = 4 if method.upper() == "GET" or safe_to_retry else 1
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=payload,
                    timeout=self.settings.timeout_seconds,
                    verify=self.settings.verify_ssl,
                )
            except Exception as exc:
                if attempt >= attempts:
                    raise RuntimeError(f"{self.service}: ошибка запроса {url}: {exc}") from exc
                delay = min(2 ** (attempt - 1), 8)
                execution_log(
                    f"{self.service}: ошибка сети, повтор "
                    f"{attempt + 1}/{attempts} через {delay} с: {exc}",
                    error=True,
                )
                time.sleep(delay)
                continue

            if 200 <= response.status_code < 300:
                if not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError as exc:
                    raise RuntimeError(
                        f"{self.service}: сервер вернул не JSON для {url}."
                    ) from exc

            if (
                response.status_code in RETRYABLE_HTTP_STATUSES
                and attempt < attempts
            ):
                retry_after = response.headers.get("Retry-After", "").strip()
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = min(2 ** (attempt - 1), 8)
                if response.status_code == 429:
                    # SynGX/Jira иногда присылает Retry-After: 0. Немедленный
                    # повтор только добивает лимит и расходует все попытки.
                    delay = max(delay, min(2 ** (attempt - 1), 8))
                delay = max(0, min(delay, 30))
                execution_log(
                    f"{self.service}: HTTP {response.status_code}, повтор "
                    f"{attempt + 1}/{attempts} через {delay:g} с",
                    error=True,
                )
                time.sleep(delay)
                continue
            raise HttpRequestError(self.service, response.status_code, response.text)
        raise AssertionError("unreachable")


class JiraClient:
    def __init__(self, settings: ConnectionSettings):
        self.settings = settings
        self.rest = RestClient("Jira", settings)

    def search(
        self,
        jql: str,
        fields: Sequence[str],
        *,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        start_at = 0
        result: list[dict[str, Any]] = []
        while True:
            data = self.rest.request_json(
                "POST",
                "/rest/api/2/search",
                payload={
                    "jql": jql,
                    "startAt": start_at,
                    "maxResults": page_size,
                    "fields": list(dict.fromkeys(fields)),
                },
                safe_to_retry=True,
            )
            if not isinstance(data, Mapping):
                raise RuntimeError("Jira search вернул неожиданный ответ.")
            issues = data.get("issues") or []
            if not isinstance(issues, list):
                raise RuntimeError("Jira search: поле issues не является массивом.")
            result.extend(issue for issue in issues if isinstance(issue, dict))
            start_at += len(issues)
            total = int(data.get("total") or 0)
            if not issues or start_at >= total:
                break
        return result

    def fields(self) -> list[dict[str, Any]]:
        data = self.rest.request_json("GET", "/rest/api/2/field")
        if not isinstance(data, list):
            raise RuntimeError("Jira /field вернул неожиданный ответ.")
        return [item for item in data if isinstance(item, dict)]

    def issue(self, issue_key: str, fields: Sequence[str]) -> dict[str, Any]:
        data = self.rest.request_json(
            "GET",
            f"/rest/api/2/issue/{issue_key}",
            params={"fields": ",".join(dict.fromkeys(fields))},
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"Jira issue {issue_key} вернул неожиданный ответ.")
        return data

    def issues_individually(
        self,
        issue_keys: Iterable[str],
        fields: Sequence[str],
        *,
        max_workers: int = JIRA_LINK_WORKERS,
        progress_label: str = "",
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        keys = sorted(
            {
                str(issue_key).strip().upper()
                for issue_key in issue_keys
                if str(issue_key).strip()
            }
        )
        if not keys:
            return {}, {}

        if max_workers <= 1:
            result: dict[str, dict[str, Any]] = {}
            errors: dict[str, str] = {}
            previous_request_started = 0.0
            for index, key in enumerate(keys, start=1):
                since_previous_start = (
                    time.perf_counter() - previous_request_started
                    if previous_request_started
                    else JIRA_ISSUE_REQUEST_INTERVAL_SECONDS
                )
                pacing_delay = max(
                    0.0,
                    JIRA_ISSUE_REQUEST_INTERVAL_SECONDS - since_previous_start,
                )
                if pacing_delay:
                    time.sleep(pacing_delay)
                previous_request_started = time.perf_counter()
                try:
                    result[key] = self.issue(key, fields)
                except Exception as exc:
                    errors[key] = str(exc)
                if progress_label and (index % 10 == 0 or index == len(keys)):
                    execution_log(
                        f"{progress_label}: {index}/{len(keys)}, "
                        f"ошибок={len(errors)}"
                    )
            return result, errors

        worker_state = local()

        def load_issue(key: str) -> tuple[str, dict[str, Any]]:
            worker_client = getattr(worker_state, "jira_client", None)
            if worker_client is None:
                worker_client = JiraClient(self.settings)
                worker_state.jira_client = worker_client
            return key, worker_client.issue(key, fields)

        result = {}
        errors = {}
        worker_count = min(max_workers, len(keys))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="jira-links",
        ) as executor:
            futures = {
                executor.submit(load_issue, key): key
                for key in keys
            }
            completed = 0
            for future in as_completed(futures):
                key = futures[future]
                try:
                    loaded_key, issue = future.result()
                except Exception as exc:
                    errors[key] = str(exc)
                else:
                    result[loaded_key] = issue
                completed += 1
                if progress_label and (
                    completed % 10 == 0 or completed == len(keys)
                ):
                    execution_log(
                        f"{progress_label}: {completed}/{len(keys)}, "
                        f"ошибок={len(errors)}"
                    )
        return result, errors

    def issues_by_keys(
        self,
        issue_keys: Iterable[str],
        fields: Sequence[str],
        *,
        batch_size: int = JIRA_KEY_BATCH_SIZE,
        progress_label: str = "",
    ) -> dict[str, dict[str, Any]]:
        if batch_size <= 0:
            raise ValueError("Jira batch_size должен быть больше нуля.")
        keys = sorted(
            {
                str(issue_key).strip().upper()
                for issue_key in issue_keys
                if str(issue_key).strip()
            }
        )
        result: dict[str, dict[str, Any]] = {}
        batch_count = (len(keys) + batch_size - 1) // batch_size
        for batch_index, key_batch in enumerate(
            batches(keys, size=batch_size),
            start=1,
        ):
            batch_started = time.perf_counter()
            jql = f"key in ({issue_keys_jql(key_batch)})"
            batch_issues = self.search(
                jql,
                fields,
                page_size=min(max(len(key_batch), 100), 1000),
            )
            for issue in batch_issues:
                key = str(issue.get("key") or "").strip().upper()
                if key:
                    result[key] = issue
            if progress_label:
                execution_log(
                    f"{progress_label}: пакет {batch_index}/{batch_count}, "
                    f"ключей={len(key_batch)}, Jira вернула={len(batch_issues)}, "
                    f"всего загружено={len(result)} ({elapsed_seconds(batch_started)})"
                )
        return result


def find_jira_field_id(
    jira: JiraClient,
    explicit_id: str,
    requested_name: str,
    candidates: Sequence[str],
) -> str:
    explicit_id = explicit_id.strip()
    wanted = normalized_set((requested_name, *candidates))
    fields = jira.fields()

    explicit_field = next(
        (
            item
            for item in fields
            if str(item.get("id") or "").strip() == explicit_id
        ),
        None,
    )

    default_field = next(
        (
            item
            for item in fields
            if str(item.get("id") or "").strip() == DEFAULT_RELEASE_DATE_FIELD_ID
            and normalized(item.get("name")) in wanted
        ),
        None,
    )
    if default_field is not None:
        if explicit_id and explicit_id != DEFAULT_RELEASE_DATE_FIELD_ID:
            configured_name = (
                str(explicit_field.get("name") or "").strip()
                if explicit_field is not None
                else "поле с таким ID не найдено"
            )
            execution_log(
                f"Jira: настроенный ID даты {explicit_id} указывает на "
                f"«{configured_name}»; использую проверенный "
                f"{DEFAULT_RELEASE_DATE_FIELD_ID} "
                f"«{default_field.get('name')}»",
                error=True,
            )
        return DEFAULT_RELEASE_DATE_FIELD_ID

    if explicit_field is not None:
        explicit_name = str(explicit_field.get("name") or "").strip()
        if normalized(explicit_name) in wanted:
            return explicit_id

    exact = [
        item
        for item in fields
        if normalized(item.get("name")) in wanted
        and str(item.get("id") or "").strip()
    ]
    if len(exact) == 1:
        resolved_id = str(exact[0].get("id") or "").strip()
        if explicit_id and resolved_id != explicit_id:
            configured_name = (
                str(explicit_field.get("name") or "").strip()
                if explicit_field is not None
                else "поле с таким ID не найдено"
            )
            execution_log(
                f"Jira: настроенный ID даты {explicit_id} указывает на "
                f"«{configured_name}»; использую {resolved_id} "
                f"«{exact[0].get('name')}»",
                error=True,
            )
        return resolved_id
    if len(exact) > 1:
        available = ", ".join(
            f"{item.get('name')} ({item.get('id')})" for item in exact
        )
        raise RuntimeError(
            f"Для Jira-поля '{requested_name}' найдено несколько точных "
            f"совпадений: {available}. Укажите правильный id в "
            "config['jira']['fields']['prod_installed_date_id']."
        )

    partial = [
        item
        for item in fields
        if any(candidate in normalized(item.get("name")) for candidate in wanted)
    ]
    if len(partial) == 1:
        resolved_id = str(partial[0].get("id") or "").strip()
        if resolved_id:
            return resolved_id
    available = ", ".join(
        sorted(
            f"{item.get('name')} ({item.get('id')})"
            for item in partial
            if item.get("name")
        )
    )
    suffix = f" Похожие поля: {available}." if available else ""
    explicit_suffix = (
        f" Настроенный ID {explicit_id} не соответствует этому имени."
        if explicit_id
        else ""
    )
    raise RuntimeError(
        f"Не найдено Jira-поле '{requested_name}'. "
        "Укажите его id в config['jira']['fields']['prod_installed_date_id'] "
        "или DEFAULT_RELEASE_DATE_FIELD_ID внутри скрипта."
        + explicit_suffix
        + suffix
    )


def build_release_jql(
    *,
    project: str,
    issue_type: str,
    ke_field_name: str,
    ke_ids: Sequence[int],
    created_since: str,
) -> str:
    ke_ids_jql = ", ".join(str(value) for value in dict.fromkeys(ke_ids))
    return (
        f"project={jql_value(project)} "
        f"AND {ke_field_name} in ({ke_ids_jql}) "
        f"AND type = {jql_value(issue_type)} "
        f'AND created >= "{created_since}"'
    )


def issue_link_type_text(link: Mapping[str, Any]) -> str:
    link_type = link.get("type")
    if isinstance(link_type, Mapping):
        # У разных Jira название связи встречается в name либо только в
        # направлении inward/outward. Для consist-of учитываем все варианты.
        values: list[str] = []
        for key in ("name", "inward", "outward"):
            values.extend(named_values(link_type.get(key)))
        return " / ".join(dict.fromkeys(values))
    return " / ".join(named_values(link_type))


def release_linked_keys(
    release: Mapping[str, Any],
    link_keywords: Sequence[str],
) -> set[str]:
    keywords = normalized_set(link_keywords)
    fields = release.get("fields")
    if not isinstance(fields, Mapping):
        return set()
    links = fields.get("issuelinks") or []
    result: set[str] = set()
    if not isinstance(links, list):
        return result
    for link in links:
        if not isinstance(link, Mapping):
            continue
        link_type_text = issue_link_type_text(link)
        normalized_type = normalized(link_type_text)
        if not any(keyword in normalized_type for keyword in keywords):
            continue
        for direction in ("outwardIssue", "inwardIssue"):
            linked = link.get(direction)
            if isinstance(linked, Mapping):
                key = str(linked.get("key") or "").strip().upper()
                if key:
                    result.add(key)
    return result


def is_hotfix_release(
    release: Mapping[str, Any],
    release_type_field_id: str,
    hotfix_values: Sequence[str],
) -> bool:
    """Return whether a Release 2.0 ticket represents a Hotfix."""

    expected = normalized_set(hotfix_values)
    actual_values = {
        normalized(value)
        for value in named_values(issue_field(release, release_type_field_id))
    }
    if actual_values & expected:
        return True

    # Старые Hotfix-релизы в HRPRELEASE встречаются без заполненного типа, но с
    # маркером Hotfix в summary. Такой fallback уже использует dpm2.py.
    summary = normalized(issue_field(release, "summary"))
    return any(value and value in summary for value in expected)


def release_is_installed_on_prom(
    release: Mapping[str, Any],
    allowed_statuses: Sequence[str] = DEFAULT_INSTALLED_RELEASE_STATUSES,
) -> bool:
    status = normalized(named_value(issue_field(release, "status")))
    # Только точный allowlist: substring-проверка ошибочно принимала статусы
    # вроде «Не установлен на ПРОМ» и «Будет установлен на ПРОМ».
    return status in normalized_set(allowed_statuses)


def collect_released_issues(
    jira: JiraClient,
    *,
    start: date,
    end: date,
    release_project: str,
    release_issue_type: str,
    release_ke_field_name: str,
    release_ke_ids: Sequence[int],
    release_created_since: str,
    release_date_field_id: str,
    release_type_field_id: str,
    detection_stage_field_id: str,
    link_keywords: Sequence[str],
    verbose: bool = False,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    str,
    dict[str, set[str]],
]:
    collection_started = time.perf_counter()
    release_fields = (
        "summary",
        "status",
        release_date_field_id,
        release_type_field_id,
    )
    release_jql = build_release_jql(
        project=release_project,
        issue_type=release_issue_type,
        ke_field_name=release_ke_field_name,
        ke_ids=release_ke_ids,
        created_since=release_created_since,
    )
    if verbose:
        print(f"JQL релизов (как в dpm2.py): {release_jql}")
    release_search_started = time.perf_counter()
    execution_log(
        "Jira: поиск Release 2.0 по КЭ "
        f"для диапазона {start.isoformat()} — "
        f"{(end - timedelta(days=1)).isoformat()}"
    )
    release_candidates = jira.search(
        release_jql,
        release_fields,
        page_size=1000,
    )
    execution_log(
        f"Jira: найдено кандидатов релизов={len(release_candidates)} "
        f"({elapsed_seconds(release_search_started)})"
    )

    releases_with_install_date = [
        release
        for release in release_candidates
        if parse_jira_date(issue_field(release, release_date_field_id)) is not None
    ]
    if release_candidates and not releases_with_install_date:
        raise RuntimeError(
            f"Jira вернула {len(release_candidates)} кандидатов релизов, "
            f"но поле даты {release_date_field_id} пусто у всех. "
            "Проверьте config['jira']['fields']['prod_installed_date_id']; "
            f"для текущей Jira ожидается {DEFAULT_RELEASE_DATE_FIELD_ID}."
        )

    range_releases = [
        release
        for release in releases_with_install_date
        if (
            (installed := parse_jira_date(issue_field(release, release_date_field_id)))
            is not None
            and start <= installed < end
        )
    ]
    releases = [
        release
        for release in range_releases
        if release_is_installed_on_prom(release)
    ]
    execution_log(
        f"Jira: в диапазоне={len(range_releases)}, "
        f"в статусе «Установлен на ПРОМ»={len(releases)}"
    )
    if verbose:
        print(
            f"Jira вернула релизов: {len(release_candidates)}; "
            f"с датой установки: {len(releases_with_install_date)}; "
            f"в объединённом диапазоне: {len(range_releases)}; "
            f"в статусе «Установлен на ПРОМ»: {len(releases)}"
        )
        status_counts = Counter(
            named_value(issue_field(release, "status")) or "Без статуса"
            for release in range_releases
        )
        if status_counts:
            print("Статусы релизов объединённого диапазона:")
            for status_name, count in status_counts.most_common():
                print(f"  {status_name}: {count}")

    issue_fields = (
        "project",
        "issuetype",
        "status",
        "priority",
        detection_stage_field_id,
    )
    issues_by_key: dict[str, dict[str, Any]] = {}
    all_linked_keys: set[str] = set()
    failed_releases: list[str] = []
    link_types: Counter[str] = Counter()
    issue_keys_by_release: dict[str, set[str]] = {}
    release_keys = sorted(
        {
            str(release.get("key") or "").strip().upper()
            for release in releases
            if str(release.get("key") or "").strip()
        }
    )
    # Jira Search на практике может вернуть урезанный issuelinks даже при
    # явном fields=issuelinks. Как и release_checker.py, источником истины
    # оставляем последовательный GET /issue/{key} через одну Session.
    release_links_started = time.perf_counter()
    execution_log(
        f"Jira: загружаю точный consist-of для {len(release_keys)} релизов, "
        f"параллельность={JIRA_LINK_WORKERS}, "
        f"интервал GET≥{JIRA_ISSUE_REQUEST_INTERVAL_SECONDS:.2f} с"
    )
    release_details_by_key, release_errors = jira.issues_individually(
        release_keys,
        ("issuelinks",),
        max_workers=JIRA_LINK_WORKERS,
        progress_label="Jira: связи релизов",
    )
    incomplete_release_keys = [
        key
        for key, release in release_details_by_key.items()
        if (
            not isinstance(release.get("fields"), Mapping)
            or "issuelinks" not in release["fields"]
        )
    ]
    for key in incomplete_release_keys:
        release_details_by_key.pop(key, None)
        release_errors[key] = "в ответе отсутствует fields.issuelinks"

    retry_release_keys = sorted(
        set(release_keys) - set(release_details_by_key)
    )
    if retry_release_keys:
        rate_limited = any(
            "429" in release_errors.get(key, "")
            for key in retry_release_keys
        )
        if rate_limited:
            execution_log(
                f"Jira: rate limit затронул {len(retry_release_keys)} релизов; "
                f"жду {JIRA_RATE_LIMIT_RECOVERY_SECONDS} с перед recovery",
                error=True,
            )
            time.sleep(JIRA_RATE_LIMIT_RECOVERY_SECONDS)
        else:
            execution_log(
                f"Jira: повторно загружаю связи {len(retry_release_keys)} релизов "
                "через последовательный recovery"
            )

        recovered_releases, recovery_errors = jira.issues_individually(
            retry_release_keys,
            ("issuelinks",),
            max_workers=1,
            progress_label="Jira: recovery связей",
        )
        incomplete_recovery_keys = [
            key
            for key, release in recovered_releases.items()
            if (
                not isinstance(release.get("fields"), Mapping)
                or "issuelinks" not in release["fields"]
            )
        ]
        for key in incomplete_recovery_keys:
            recovered_releases.pop(key, None)
            recovery_errors[key] = "в ответе отсутствует fields.issuelinks"
        release_details_by_key.update(recovered_releases)
        for key in recovered_releases:
            release_errors.pop(key, None)
        for key, error in recovery_errors.items():
            release_errors[key] = error

    failed_releases.extend(
        sorted(set(release_keys) - set(release_details_by_key))
    )

    for index, release in enumerate(releases, start=1):
        release_key = str(release.get("key") or "").strip().upper()
        if not release_key:
            continue
        full_release = release_details_by_key.get(release_key)
        if full_release is None:
            issue_keys_by_release[release_key] = set()
            if verbose:
                print(
                    f"Не удалось получить связи {release_key}: "
                    f"{release_errors.get(release_key, 'пустой ответ Jira')}",
                    file=sys.stderr,
                )
            continue

        linked_keys = release_linked_keys(full_release, link_keywords)
        issue_keys_by_release[release_key] = set(linked_keys)
        all_linked_keys.update(linked_keys)

        if verbose:
            links = issue_field(full_release, "issuelinks") or []
            if isinstance(links, list):
                for link in links:
                    if isinstance(link, Mapping):
                        link_types[issue_link_type_text(link) or "без названия"] += 1

        if verbose and (index % 10 == 0 or index == len(releases)):
            print(
                f"Обработано релизов: {index}/{len(releases)}; "
                f"последний {release_key}: consist-of задач={len(linked_keys)}"
            )

    if failed_releases:
        details = "; ".join(
            f"{key}: {release_errors.get(key, 'пустой ответ Jira')}"
            for key in failed_releases[:5]
        )
        suffix = (
            f"; ещё {len(failed_releases) - 5}"
            if len(failed_releases) > 5
            else ""
        )
        raise RuntimeError(
            "Jira не вернула полный состав установленных релизов; "
            "публикация остановлена. "
            f"{details}{suffix}"
        )
    execution_log(
        f"Jira: связи релизов загружены, "
        f"уникальных consist-of ключей={len(all_linked_keys)} "
        f"({elapsed_seconds(release_links_started)})"
    )

    # Сначала загружаем абсолютно весь состав consist of без фильтра типа,
    # проекта, статуса, приоритета или этапа обнаружения. Все
    # бизнес-фильтры применяются только позже в aggregate_issues().
    issue_load_started = time.perf_counter()
    execution_log(
        f"Jira: загружаю {len(all_linked_keys)} уникальных задач состава "
        f"пакетами по {JIRA_KEY_BATCH_SIZE}"
    )
    issues_by_key = jira.issues_by_keys(
        all_linked_keys,
        issue_fields,
        progress_label="Jira: задачи состава",
    )
    initially_missing_keys = sorted(all_linked_keys - set(issues_by_key))
    if initially_missing_keys:
        execution_log(
            f"Jira: пакетный поиск не вернул {len(initially_missing_keys)} задач; "
            "запускаю точный GET fallback"
        )
    fallback_issues, issue_errors = jira.issues_individually(
        initially_missing_keys,
        issue_fields,
        max_workers=JIRA_LINK_WORKERS,
        progress_label="Jira: fallback задач",
    )
    issues_by_key.update(fallback_issues)
    inaccessible_keys = sorted(
        set(initially_missing_keys) - set(fallback_issues)
    )

    if inaccessible_keys:
        details = "; ".join(
            f"{key}: {issue_errors.get(key, 'пустой ответ Jira')}"
            for key in inaccessible_keys[:5]
        )
        suffix = (
            f"; ещё {len(inaccessible_keys) - 5}"
            if len(inaccessible_keys) > 5
            else ""
        )
        raise RuntimeError(
            "Jira не вернула все задачи из consist of; "
            "публикация остановлена. "
            f"{details}{suffix}"
        )
    execution_log(
        f"Jira: задачи состава загружены={len(issues_by_key)} "
        f"({elapsed_seconds(issue_load_started)}); "
        f"весь сбор={elapsed_seconds(collection_started)}"
    )

    if verbose:
        issue_batch_requests = (
            (len(all_linked_keys) + JIRA_KEY_BATCH_SIZE - 1)
            // JIRA_KEY_BATCH_SIZE
            if all_linked_keys
            else 0
        )
        print(
            f"Полный состав consist of: релизов={len(releases)}, "
            f"ошибок={len(failed_releases)}, "
            f"уникальных ключей={len(all_linked_keys)}, "
            f"загружено задач без фильтров={len(issues_by_key)}, "
            f"недоступно в Jira={len(inaccessible_keys)}"
        )
        print(
            "Jira-запросы состава: "
            f"точные GET релизов={len(release_keys)} "
            f"(параллельность≤{JIRA_LINK_WORKERS}), "
            f"пакеты задач≈{issue_batch_requests}, "
            f"GET fallback задач={len(initially_missing_keys)}"
        )
        if link_types:
            print("Типы связей релизов:")
            for label, count in link_types.most_common():
                print(f"  {label}: {count}")

    return releases, list(issues_by_key.values()), release_jql, issue_keys_by_release


def select_period_data(
    releases: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    issue_keys_by_release: Mapping[str, set[str]],
    *,
    start: date,
    end: date,
    release_date_field_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    period_releases: list[dict[str, Any]] = []
    period_issue_keys: set[str] = set()

    for release in releases:
        installed = parse_jira_date(issue_field(release, release_date_field_id))
        if installed is None or not (start <= installed < end):
            continue
        if not isinstance(release, dict):
            continue
        period_releases.append(release)
        release_key = str(release.get("key") or "").strip().upper()
        period_issue_keys.update(issue_keys_by_release.get(release_key, set()))

    period_issues = [
        issue
        for issue in issues
        if str(issue.get("key") or "").strip().upper() in period_issue_keys
    ]
    return period_releases, period_issues


def print_issue_inventory(issues: Sequence[Mapping[str, Any]]) -> None:
    """Print raw released-issue distribution before metric filters."""

    inventory: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    for issue in issues:
        project_key = issue_project_key(issue) or "БЕЗ ПРОЕКТА"
        issue_type = named_value(issue_field(issue, "issuetype")) or "Без типа"
        status = named_value(issue_field(issue, "status")) or "Без статуса"
        inventory[project_key][(issue_type, status)] += 1

    print("Состав релизов до фильтров метрики:")
    if not inventory:
        print("  задач не загружено")
        return
    for project_key in sorted(inventory):
        total = sum(inventory[project_key].values())
        details = "; ".join(
            f"{issue_type}/{status}={count}"
            for (issue_type, status), count in inventory[project_key].most_common()
        )
        print(f"  {project_key}: всего={total}; {details}")


def print_filter_audit(
    team_specs: Sequence[TeamSpec],
    issues: Sequence[Mapping[str, Any]],
    rules: MetricRules,
    detection_stage_field_id: str,
) -> None:
    """Explain how Story/Bug candidates pass through metric filters."""

    project_to_team = {
        project_key: spec.team for spec in team_specs for project_key in spec.project_keys
    }
    audit: dict[str, Counter[str]] = {
        spec.team: Counter() for spec in team_specs
    }
    rejected_values: dict[str, dict[str, Counter[str]]] = {
        spec.team: {
            "story_status": Counter(),
            "bug_status": Counter(),
            "bug_priority": Counter(),
            "bug_detection_stage": Counter(),
        }
        for spec in team_specs
    }
    outside_projects: Counter[str] = Counter()

    for issue in issues:
        project_key = issue_project_key(issue) or "БЕЗ ПРОЕКТА"
        team = project_to_team.get(project_key)
        if team is None:
            outside_projects[project_key] += 1
            continue

        issue_type = normalized(named_value(issue_field(issue, "issuetype")))
        raw_status = issue_field(issue, "status")
        status_name = named_value(raw_status) or "Без статуса"
        status = normalized(status_name)

        if issue_type in rules.story_types:
            audit[team]["story_total"] += 1
            if story_has_done_status(raw_status, rules):
                audit[team]["story_done"] += 1
            else:
                rejected_values[team]["story_status"][status_name] += 1
            continue

        if issue_type not in rules.bug_types:
            continue
        audit[team]["bug_total"] += 1
        if status not in rules.bug_statuses:
            rejected_values[team]["bug_status"][status_name] += 1
            continue
        audit[team]["bug_closed"] += 1

        priority_name = named_value(issue_field(issue, "priority")) or "Без приоритета"
        if normalized(priority_name) not in rules.bug_priorities:
            rejected_values[team]["bug_priority"][priority_name] += 1
            continue
        audit[team]["bug_high_plus"] += 1

        stage_values = named_values(issue_field(issue, detection_stage_field_id))
        stage_name = ", ".join(stage_values) or "Без этапа обнаружения"
        if classify_eligible_detection_stage(
            issue_field(issue, detection_stage_field_id),
            rules,
        ) is None:
            rejected_values[team]["bug_detection_stage"][stage_name] += 1
            continue
        audit[team]["bug_eligible"] += 1

    print("Аудит фильтров метрики:")
    for spec in team_specs:
        values = audit[spec.team]
        print(
            f"  {spec.team}: "
            f"Story всего={values['story_total']}, Done={values['story_done']}; "
            f"Bug всего={values['bug_total']}, Closed={values['bug_closed']}, "
            f"High+={values['bug_high_plus']}, PSI/ПРОМ={values['bug_eligible']}"
        )
        details: list[str] = []
        for field_name, label in (
            ("story_status", "Story-статусы"),
            ("bug_status", "Bug-статусы"),
            ("bug_priority", "приоритеты"),
            ("bug_detection_stage", "этапы обнаружения"),
        ):
            counter = rejected_values[spec.team][field_name]
            if counter:
                rendered = ", ".join(
                    f"{value}={count}" for value, count in counter.most_common()
                )
                details.append(f"{label}: {rendered}")
        if details:
            print("    Отсев — " + "; ".join(details))

    if outside_projects:
        rendered = ", ".join(
            f"{project_key}={count}"
            for project_key, count in outside_projects.most_common()
        )
        print("  Вне настроенных команд: " + rendered)


def decimal_text(value: Decimal, places: int = 3) -> str:
    quantizer = Decimal(1).scaleb(-places)
    return format(value.quantize(quantizer), "f").replace(".", ",")


def ratio_text(value: Optional[Decimal], bugs: int, stories: int) -> str:
    if value is None:
        return "∞" if bugs > 0 and stories == 0 else "—"
    return f"{decimal_text(value, 2)} ({decimal_text(value * 100, 1)}%)"


def target_text(value: Decimal) -> str:
    return f"{decimal_text(value, 2)} ({decimal_text(value * 100, 0)}%)"


def target_formula_text(metric: TeamMetric) -> str:
    if metric.as_is_ratio is None or metric.minimum_target_ratio is None:
        return (
            f"AS IS — · расчёт {decimal_text(metric.calculated_target_ratio, 3)}"
        )
    return (
        f"AS IS {decimal_text(metric.as_is_ratio, 3)} · "
        f"расчёт {decimal_text(metric.calculated_target_ratio, 3)} · "
        f"80% AS IS {decimal_text(metric.minimum_target_ratio, 3)}"
    )


def as_is_floor_applied(metric: TeamMetric) -> bool:
    return (
        metric.minimum_target_ratio is not None
        and metric.calculated_target_ratio < metric.minimum_target_ratio
    )


def attainment_text(metric: TeamMetric) -> str:
    if metric.infinite_attainment:
        return "∞"
    if metric.target_attainment_percent is None:
        return "—"
    return f"{decimal_text(metric.target_attainment_percent, 1)}%"


def render_console(metrics: Sequence[TeamMetric]) -> str:
    headers = (
        "Команда",
        "Jira",
        "AS IS",
        "A×AS IS+B",
        "Цель",
        "Story",
        "Баги",
        "PSI",
        "ПРОМ",
        "Факт",
        "Цель, %",
        "Ещё ПРОМ-багов",
        "Story до цели",
    )
    rows = [
        (
            metric.team,
            ",".join(metric.project_keys),
            (
                decimal_text(metric.as_is_ratio, 2)
                if metric.as_is_ratio is not None
                else "—"
            ),
            decimal_text(metric.calculated_target_ratio, 2),
            decimal_text(metric.target_ratio, 2),
            str(metric.stories),
            str(metric.bugs),
            str(metric.psi_bugs),
            str(metric.prom_bugs),
            ratio_text(metric.actual_ratio, metric.bugs, metric.stories),
            attainment_text(metric),
            str(metric.additional_bugs_allowed),
            str(metric.additional_stories_required),
        )
        for metric in metrics
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def render_row(row: Sequence[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))

    separator = "-+-".join("-" * width for width in widths)
    return "\n".join((render_row(headers), separator, *(render_row(row) for row in rows)))


def state_cell_style(metric: TeamMetric) -> str:
    if metric.state == "Цель выполнена":
        return "background-color:#E3FCEF;color:#006644;"
    if metric.state == "Ниже цели":
        return "background-color:#FFEBE6;color:#BF2600;"
    return "background-color:#F4F5F7;color:#5E6C84;"


def state_badge(metric: TeamMetric) -> str:
    if metric.state == "Цель выполнена":
        icon = "✓"
    elif metric.state == "Ниже цели":
        icon = "!"
    else:
        icon = "·"
    return (
        f'<span style="{state_cell_style(metric)}border-radius:12px;display:inline-block;'
        f'font-weight:bold;padding:4px 9px;">{icon} {html.escape(metric.state)}</span>'
    )


def attainment_visual(metric: TeamMetric) -> str:
    if metric.infinite_attainment:
        percent_for_bar = 100
        color = "#00A3BF"
    elif metric.target_attainment_percent is None:
        percent_for_bar = 0
        color = "#B3BAC5"
    else:
        percent_for_bar = max(
            0,
            min(100, int(metric.target_attainment_percent)),
        )
        color = "#36B37E" if metric.target_attainment_percent >= 100 else "#FF5630"
    return (
        f'<div style="font-weight:bold;color:#172B4D;">{html.escape(attainment_text(metric))}</div>'
        '<div style="background-color:#DFE1E6;border-radius:4px;height:6px;'
        'margin-top:5px;min-width:78px;">'
        f'<div style="background-color:{color};border-radius:4px;height:6px;'
        f'width:{percent_for_bar}%;">&#160;</div></div>'
    )


def kpi_cell(label: str, value: str, background: str, color: str = "#FFFFFF") -> str:
    return (
        f'<td style="background-color:{background};border:6px solid #FFFFFF;'
        'border-radius:10px;padding:14px;text-align:center;width:20%;">'
        f'<div style="color:{color};font-size:11px;font-weight:bold;'
        f'letter-spacing:.04em;text-transform:uppercase;">{html.escape(label)}</div>'
        f'<div style="color:{color};font-size:24px;font-weight:bold;'
        f'margin-top:4px;">{html.escape(value)}</div></td>'
    )


def render_metric_section_html(
    metrics: Sequence[TeamMetric],
    *,
    title: str,
    start: date,
    end: date,
    release_count: int,
    hotfix_count: int,
    accent: str,
) -> str:
    planned_release_count = max(0, release_count - hotfix_count)
    story_count = sum(metric.stories for metric in metrics)
    bug_count = sum(metric.bugs for metric in metrics)
    psi_bug_count = sum(metric.psi_bugs for metric in metrics)
    prom_bug_count = sum(metric.prom_bugs for metric in metrics)
    teams_on_target = sum(metric.state == "Цель выполнена" for metric in metrics)
    period = (
        f"{start.strftime('%d.%m')}–"
        f"{(end - timedelta(days=1)).strftime('%d.%m.%Y')}"
    )

    parts = [
        f'<div style="background-color:{accent};border-radius:9px;color:#FFFFFF;'
        'font-size:20px;font-weight:bold;margin:18px 0 10px;padding:13px 16px;">',
        html.escape(title),
        f'<div style="color:#DEEBFF;font-size:12px;font-weight:normal;margin-top:4px;">'
        f"{html.escape(period)}</div>",
        "</div>",
        '<table style="border-collapse:separate;border-spacing:0;margin:0 0 14px 0;'
        'table-layout:fixed;width:100%;"><tbody><tr>',
        kpi_cell("Период", period, "#0052CC"),
        kpi_cell("Плановые релизы", str(planned_release_count), "#6554C0"),
        kpi_cell("Hotfix", str(hotfix_count), "#FF8B00"),
        kpi_cell("Story Done", str(story_count), "#00875A"),
        kpi_cell("Баги PSI / ПРОМ", f"{bug_count} · {psi_bug_count}/{prom_bug_count}", "#DE350B"),
        "</tr></tbody></table>",
        '<div style="background-color:#EAE6FF;border-left:5px solid #6554C0;'
        'border-radius:6px;color:#403294;margin-bottom:15px;padding:10px 13px;">',
        f'<strong>Цель выполняют {teams_on_target} из {len(metrics)} команд.</strong> '
        "Факт = баги / Story и округляется до сотых; выполнение = целевой "
        "коэффициент / факт × 100%. Эффективная цель = максимум из "
        "расчёта A × AS IS + B и 80% от AS IS. При нуле багов выполнение равно 100%. "
        "Чем выше процент выполнения, тем лучше.",
        "</div>",
        '<table class="confluenceTable" style="border-collapse:collapse;'
        'border:1px solid #C1C7D0;font-size:13px;width:100%;"><thead><tr>',
    ]
    headers = (
        "Команда",
        "Jira",
        "Цель",
        "Story Done",
        "Баги Closed PSI/ПРОМ",
        "PSI",
        "ПРОМ",
        "Факт",
        "Выполнение цели",
        "Состояние",
        "Ещё ПРОМ-багов до лимита",
        "Story до цели",
    )
    for header in headers:
        parts.append(
            '<th style="background-color:#F4F5F7;border:1px solid #C1C7D0;'
            'color:#172B4D;font-size:12px;font-weight:bold;padding:9px 7px;'
            'text-align:center;vertical-align:middle;">'
            f'<span style="color:#172B4D;font-weight:bold;">'
            f"{html.escape(header)}</span></th>"
        )
    parts.append("</tr></thead><tbody>")

    for index, metric in enumerate(metrics):
        row_background = "#FFFFFF" if index % 2 == 0 else "#F7F9FC"
        base_cell = (
            f"background-color:{row_background};border:1px solid #DFE1E6;"
            "padding:9px 7px;text-align:center;vertical-align:middle;"
        )
        parts.append(
            f'<tr><td style="{base_cell}color:#172B4D;font-weight:bold;'
            f'text-align:left;">{html.escape(metric.team)}</td>'
        )
        parts.append(
            f'<td style="{base_cell}"><span style="background-color:#DEEBFF;'
            'border-radius:10px;color:#0747A6;font-weight:bold;padding:3px 7px;">'
            f'{html.escape(", ".join(metric.project_keys))}</span></td>'
        )
        target_background = "#FFF0B3" if as_is_floor_applied(metric) else "#EAE6FF"
        target_color = "#974F0C" if as_is_floor_applied(metric) else "#403294"
        floor_note = " · применён минимум" if as_is_floor_applied(metric) else ""
        parts.append(
            f'<td style="{base_cell}background-color:{target_background};'
            f'color:{target_color};font-weight:bold;">'
            f'<div>{html.escape(target_text(metric.target_ratio))}</div>'
            f'<div style="font-size:10px;font-weight:normal;margin-top:3px;">'
            f'{html.escape(target_formula_text(metric) + floor_note)}</div></td>'
        )
        parts.append(
            f'<td style="{base_cell}color:#006644;font-size:16px;'
            f'font-weight:bold;">{metric.stories}</td>'
        )
        parts.append(
            f'<td style="{base_cell}color:#BF2600;font-size:16px;'
            f'font-weight:bold;">{metric.bugs}</td>'
        )
        parts.append(
            f'<td style="{base_cell}background-color:#E6FCFF;color:#0065FF;'
            f'font-weight:bold;">{metric.psi_bugs}</td>'
        )
        parts.append(
            f'<td style="{base_cell}background-color:#FFF0B3;color:#974F0C;'
            f'font-weight:bold;">{metric.prom_bugs}</td>'
        )
        ratio_background = "#E3FCEF" if metric.state == "Цель выполнена" else "#FFEBE6"
        ratio_color = "#006644" if metric.state == "Цель выполнена" else "#BF2600"
        parts.append(
            f'<td style="{base_cell}background-color:{ratio_background};'
            f'color:{ratio_color};font-weight:bold;">'
            f'{html.escape(ratio_text(metric.actual_ratio, metric.bugs, metric.stories))}</td>'
        )
        parts.append(f'<td style="{base_cell}">{attainment_visual(metric)}</td>')
        parts.append(f'<td style="{base_cell}">{state_badge(metric)}</td>')
        capacity_background = "#E3FCEF" if metric.additional_bugs_allowed > 0 else row_background
        capacity_color = "#006644" if metric.additional_bugs_allowed > 0 else "#5E6C84"
        parts.append(
            f'<td style="{base_cell}background-color:{capacity_background};'
            f'color:{capacity_color};font-size:16px;font-weight:bold;">'
            f"{metric.additional_bugs_allowed}</td>"
        )
        stories_background = "#FFEBE6" if metric.additional_stories_required > 0 else "#E3FCEF"
        stories_color = "#BF2600" if metric.additional_stories_required > 0 else "#006644"
        parts.append(
            f'<td style="{base_cell}background-color:{stories_background};'
            f'color:{stories_color};font-size:16px;font-weight:bold;">'
            f"{metric.additional_stories_required}</td>"
        )
        parts.append("</tr>")

    parts.append("</tbody></table>")
    return "".join(parts)


def render_confluence_html(
    *,
    rolling_metrics: Sequence[TeamMetric],
    quarter_metrics: Sequence[TeamMetric],
    rolling_start: date,
    rolling_end: date,
    quarter_start: date,
    quarter_end: date,
    quarter: int,
    rolling_release_count: int,
    rolling_hotfix_count: int,
    quarter_release_count: int,
    quarter_hotfix_count: int,
    generated_at: datetime,
) -> str:
    parts = [
        '<div style="background-color:#172B4D;border-radius:12px;'
        'color:#FFFFFF;margin-bottom:14px;padding:20px 22px;">',
        '<div style="color:#79F2C0;font-size:12px;font-weight:bold;'
        'letter-spacing:.08em;text-transform:uppercase;">Quality Radar</div>',
        '<div style="font-size:25px;font-weight:bold;margin-top:5px;">'
        "Качество релизов команд · квартал и 90 дней</div>",
        '<div style="color:#B3D4FF;font-size:13px;margin-top:7px;">'
        "Два независимых среза Story и критичных PSI/ПРОМ-багов из "
        "установленных на ПРОМ плановых релизов и Hotfix</div>",
        "</div>",
        render_metric_section_html(
            quarter_metrics,
            title=f"Текущий квартал · {quarter_start.year} Q{quarter}",
            start=quarter_start,
            end=quarter_end,
            release_count=quarter_release_count,
            hotfix_count=quarter_hotfix_count,
            accent="#6554C0",
        ),
        render_metric_section_html(
            rolling_metrics,
            title="Последние 90 дней",
            start=rolling_start,
            end=rolling_end,
            release_count=rolling_release_count,
            hotfix_count=rolling_hotfix_count,
            accent="#0052CC",
        ),
        '<div style="background-color:#F4F5F7;border-radius:7px;color:#5E6C84;'
        'font-size:12px;margin-top:14px;padding:11px 13px;">',
        "В баги входят только Closed/Закрыт с приоритетом "
        "Critical/Crytical, Blocker, Высокий или Блокирующий "
        "и этапом обнаружения PSI/ПСИ или PROM/ПРОМ. "
        "Эффективная цель = max(A × AS IS + B; AS IS × 0,8). "
        "Лимит дефектов = floor(эффективная цель × Story). "
        "«Ещё ПРОМ-багов» — целый запас до этого лимита; «Story до цели» — "
        "минимальное число дополнительных Story для возвращения к 100%.",
        f"<br/>Обновлено автоматически: "
        f"{html.escape(generated_at.isoformat(timespec='seconds'))}.",
        "</div>",
    ]
    return "".join(parts)


class ConfluencePublisher:
    def __init__(self, settings: ConnectionSettings):
        self.rest = RestClient("Confluence", settings)
        self.base_url = settings.url.rstrip("/")

    def get_page(self, page_id: str) -> dict[str, Any]:
        page = self.rest.request_json(
            "GET",
            f"/rest/api/content/{page_id}",
            params={"expand": "version,space,title"},
        )
        if not isinstance(page, dict):
            raise RuntimeError("Confluence вернул неожиданные данные страницы.")
        return page

    def update_page(self, page: Mapping[str, Any], title: str, body: str) -> dict[str, Any]:
        page_id = str(page.get("id") or "").strip()
        version = page.get("version")
        version_number = int(version.get("number") or 0) if isinstance(version, Mapping) else 0
        if not page_id or version_number <= 0:
            fresh = self.get_page(page_id)
            page_id = str(fresh.get("id") or "").strip()
            fresh_version = fresh.get("version")
            version_number = (
                int(fresh_version.get("number") or 0)
                if isinstance(fresh_version, Mapping)
                else 0
            )
        if not page_id or version_number <= 0:
            raise RuntimeError("Confluence: не удалось определить id/version страницы.")
        result = self.rest.request_json(
            "PUT",
            f"/rest/api/content/{page_id}",
            payload={
                "id": page_id,
                "type": "page",
                "title": title,
                "version": {
                    "number": version_number + 1,
                    "message": "Quarterly quality metrics refresh",
                },
                "body": {
                    "storage": {
                        "value": body,
                        "representation": "storage",
                    }
                },
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("Confluence update page вернул неожиданный ответ.")
        return result

    def publish(
        self,
        *,
        body: str,
        page_id: str = DEFAULT_CONFLUENCE_PAGE_ID,
    ) -> str:
        page = self.get_page(page_id)
        actual_title = str(page.get("title") or "").strip()
        if not actual_title:
            raise RuntimeError(
                f"Confluence: у страницы {page_id} не удалось прочитать текущее название."
            )
        result = self.update_page(page, actual_title, body)
        result_id = str(result.get("id") or page_id).strip()
        if not result_id:
            raise RuntimeError("Confluence не вернул id опубликованной страницы.")
        return f"{self.base_url}/pages/viewpage.action?pageId={result_id}"


def load_repository_config() -> tuple[Any, Mapping[str, Any]]:
    """Load the same config.py sources that release_checker.py uses."""

    try:
        import config as config_module
    except ImportError as exc:
        raise RuntimeError(
            f"Не найден {PROJECT_ROOT / 'config.py'} с настройками Jira/Confluence."
        ) from exc
    nested = getattr(config_module, "config", {})
    return config_module, nested if isinstance(nested, Mapping) else {}


def configured_jira_field_id(field_name: str, default: str = "") -> str:
    """Read config['jira']['fields'][field_name] exactly like dpm2.py."""

    _, nested = load_repository_config()
    jira_config = nested.get("jira") if isinstance(nested.get("jira"), Mapping) else {}
    fields = (
        jira_config.get("fields")
        if isinstance(jira_config.get("fields"), Mapping)
        else {}
    )
    return first_text(fields.get(field_name), default=default)


def first_text(*values: Any, default: str = "") -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return default


def load_jira_settings() -> ConnectionSettings:
    config_module, nested = load_repository_config()
    jira_config = nested.get("jira") if isinstance(nested.get("jira"), Mapping) else {}
    options = (
        jira_config.get("options")
        if isinstance(jira_config.get("options"), Mapping)
        else {}
    )
    url = first_text(
        jira_config.get("url"),
        options.get("server"),
        getattr(config_module, "JIRA_URL", ""),
        default=DEFAULT_JIRA_URL,
    ).rstrip("/")
    token = first_text(
        jira_config.get("token"),
        getattr(config_module, "JIRA_API_TOKEN", ""),
    )
    username = first_text(
        jira_config.get("username"),
        jira_config.get("email"),
        getattr(config_module, "JIRA_USERNAME", ""),
        getattr(config_module, "JIRA_EMAIL", ""),
    )
    auth_mode = first_text(
        jira_config.get("auth_mode"),
        getattr(config_module, "JIRA_AUTH_MODE", ""),
        default="token",
    ).casefold()
    missing: list[str] = []
    if not token:
        missing.append("config['jira']['token'] или config.JIRA_API_TOKEN")
    if auth_mode == "basic" and not username:
        missing.append("Jira username/email в config.py")
    if missing:
        raise RuntimeError(f"Не заданы настройки Jira в config.py: {', '.join(missing)}.")
    if auth_mode not in {"token", "basic"}:
        raise RuntimeError("Jira auth_mode в config.py должен быть token или basic.")
    return ConnectionSettings(
        url=url,
        token=token,
        username=username,
        auth_mode=auth_mode,
        verify_ssl=VERIFY_SSL,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
    )


def load_confluence_settings() -> ConnectionSettings:
    config_module, nested = load_repository_config()
    confluence_config = (
        nested.get("confluence")
        if isinstance(nested.get("confluence"), Mapping)
        else {}
    )
    jira_config = nested.get("jira") if isinstance(nested.get("jira"), Mapping) else {}
    url = first_text(
        confluence_config.get("url"),
        getattr(config_module, "CONFLUENCE_URL", ""),
        default=DEFAULT_CONFLUENCE_URL,
    ).rstrip("/")
    token = first_text(
        confluence_config.get("token"),
        getattr(config_module, "CONFLUENCE_TOKEN", ""),
    )
    username = first_text(
        confluence_config.get("username"),
        getattr(config_module, "CONFLUENCE_USERNAME", ""),
        jira_config.get("username"),
        jira_config.get("email"),
        getattr(config_module, "JIRA_USERNAME", ""),
        getattr(config_module, "JIRA_EMAIL", ""),
    )
    auth_mode = first_text(
        confluence_config.get("auth_mode"),
        getattr(config_module, "CONFLUENCE_AUTH_MODE", ""),
        default="token",
    ).casefold()
    missing: list[str] = []
    if not token:
        missing.append("config['confluence']['token'] или config.CONFLUENCE_TOKEN")
    if auth_mode == "basic" and not username:
        missing.append("Confluence username в config.py")
    if missing:
        raise RuntimeError(
            f"Не заданы настройки Confluence в config.py: {', '.join(missing)}."
        )
    if auth_mode not in {"token", "basic"}:
        raise RuntimeError("Confluence auth_mode в config.py должен быть token или basic.")
    return ConnectionSettings(
        url=url,
        token=token,
        username=username,
        auth_mode=auth_mode,
        verify_ssl=VERIFY_SSL,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Собирает за последние 90 дней и отдельно за квартал отношение "
            "закрытых Critical/Blocker/Высокий/Блокирующий PSI/ПРОМ-багов "
            "к зарелизенным Story и публикует таблицы в Confluence."
        )
    )
    parser.add_argument(
        "--quarter",
        help="Квартал YYYY-QN. По умолчанию текущий квартал.",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Только собрать и вывести метрики, не менять Confluence.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Показать JQL и ключи учтённых задач.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    job_started = time.perf_counter()
    execution_log(
        "Старт quarterly_quality_metrics "
        f"(публикация={'выключена' if args.no_publish else 'включена'}, "
        f"verbose={'да' if args.verbose else 'нет'})"
    )
    try:
        team_specs = load_team_specs()
        rules = MetricRules.defaults()
        today = datetime.now().astimezone().date()
        if args.quarter:
            quarter_start, quarter_calendar_end, quarter = parse_quarter(args.quarter)
        else:
            quarter_start, quarter_calendar_end, quarter = quarter_bounds(today)

        quarter_end = quarter_calendar_end
        rolling_end = today + timedelta(days=1)
        rolling_start = rolling_end - timedelta(days=90)
        collection_start = min(rolling_start, quarter_start)
        collection_end = max(rolling_end, quarter_end)
        execution_log(
            f"Периоды: 90 дней {rolling_start.isoformat()} — "
            f"{(rolling_end - timedelta(days=1)).isoformat()}; "
            f"{quarter_start.year} Q{quarter} {quarter_start.isoformat()} — "
            f"{(quarter_end - timedelta(days=1)).isoformat()}; "
            f"команд={len(team_specs)}"
        )

        execution_log("Загружаю настройки Jira из config.py")
        jira = JiraClient(load_jira_settings())
        release_date_name = DEFAULT_RELEASE_DATE_FIELD_NAME
        field_lookup_started = time.perf_counter()
        execution_log(
            f"Jira: определяю поле «{release_date_name}»"
        )
        release_date_field_id = find_jira_field_id(
            jira,
            configured_jira_field_id(
                "prod_installed_date_id",
                DEFAULT_RELEASE_DATE_FIELD_ID,
            ),
            release_date_name,
            (
                "Дата установки на пром",
                "Дата установки в ПРОМ",
                "Дата установки в пром",
                "Дата установки (ПРОМ)",
                "Дата установки PROD",
            ),
        )
        execution_log(
            f"Jira: поле даты релиза={release_date_field_id} "
            f"({elapsed_seconds(field_lookup_started)})"
        )
        detection_stage_field_id = DEFAULT_DETECTION_STAGE_FIELD_ID
        release_type_field_id = DEFAULT_RELEASE_TYPE_FIELD_ID

        releases, issues, release_jql, issue_keys_by_release = collect_released_issues(
            jira,
            start=collection_start,
            end=collection_end,
            release_project=DEFAULT_RELEASE_PROJECT,
            release_issue_type=DEFAULT_RELEASE_ISSUE_TYPE,
            release_ke_field_name=DEFAULT_RELEASE_KE_FIELD_NAME,
            release_ke_ids=RELEASE_KE_IDS,
            release_created_since=DEFAULT_RELEASE_CREATED_SINCE,
            release_date_field_id=release_date_field_id,
            release_type_field_id=release_type_field_id,
            detection_stage_field_id=detection_stage_field_id,
            link_keywords=DEFAULT_RELEASE_LINK_KEYWORDS,
            verbose=args.verbose,
        )

        rolling_releases, rolling_issues = select_period_data(
            releases,
            issues,
            issue_keys_by_release,
            start=rolling_start,
            end=rolling_end,
            release_date_field_id=release_date_field_id,
        )
        quarter_releases, quarter_issues = select_period_data(
            releases,
            issues,
            issue_keys_by_release,
            start=quarter_start,
            end=quarter_end,
            release_date_field_id=release_date_field_id,
        )
        execution_log(
            "Разбиение по периодам завершено: "
            f"90 дней — релизов={len(rolling_releases)}, задач={len(rolling_issues)}; "
            f"квартал — релизов={len(quarter_releases)}, задач={len(quarter_issues)}"
        )

        rolling_hotfix_count = sum(
            is_hotfix_release(release, release_type_field_id, DEFAULT_HOTFIX_VALUES)
            for release in rolling_releases
        )
        quarter_hotfix_count = sum(
            is_hotfix_release(release, release_type_field_id, DEFAULT_HOTFIX_VALUES)
            for release in quarter_releases
        )
        if args.verbose:
            print("\n=== Последние 90 дней: аудит ===")
            print_issue_inventory(rolling_issues)
            print_filter_audit(
                team_specs,
                rolling_issues,
                rules,
                detection_stage_field_id,
            )
            print(f"\n=== {quarter_start.year} Q{quarter}: аудит ===")
            print_issue_inventory(quarter_issues)
            print_filter_audit(
                team_specs,
                quarter_issues,
                rules,
                detection_stage_field_id,
            )

        metrics_started = time.perf_counter()
        execution_log("Рассчитываю метрики команд и формирую таблицы")
        rolling_counts = aggregate_issues(
            team_specs,
            rolling_issues,
            rules,
            detection_stage_field_id,
        )
        quarter_counts = aggregate_issues(
            team_specs,
            quarter_issues,
            rules,
            detection_stage_field_id,
        )
        rolling_metrics = [
            calculate_metric(spec, rolling_counts[spec.team]) for spec in team_specs
        ]
        quarter_metrics = [
            calculate_metric(spec, quarter_counts[spec.team]) for spec in team_specs
        ]
        generated_at = datetime.now().astimezone()
        report_html = render_confluence_html(
            rolling_metrics=rolling_metrics,
            quarter_metrics=quarter_metrics,
            rolling_start=rolling_start,
            rolling_end=rolling_end,
            quarter_start=quarter_start,
            quarter_end=quarter_end,
            quarter=quarter,
            rolling_release_count=len(rolling_releases),
            rolling_hotfix_count=rolling_hotfix_count,
            quarter_release_count=len(quarter_releases),
            quarter_hotfix_count=quarter_hotfix_count,
            generated_at=generated_at,
        )
        execution_log(
            f"Метрики и таблицы сформированы ({elapsed_seconds(metrics_started)})"
        )

        print(
            f"\nПоследние 90 дней: {rolling_start.isoformat()} — "
            f"{(rolling_end - timedelta(days=1)).isoformat()}"
        )
        print(
            f"Релизов: {len(rolling_releases)} "
            f"(плановых: {len(rolling_releases) - rolling_hotfix_count}, "
            f"Hotfix: {rolling_hotfix_count}); "
            f"задач consist of до фильтров: {len(rolling_issues)}"
        )
        print(render_console(rolling_metrics))

        print(
            f"\n{quarter_start.year} Q{quarter}: {quarter_start.isoformat()} — "
            f"{(quarter_end - timedelta(days=1)).isoformat()}"
        )
        print(
            f"Релизов: {len(quarter_releases)} "
            f"(плановых: {len(quarter_releases) - quarter_hotfix_count}, "
            f"Hotfix: {quarter_hotfix_count}); "
            f"задач consist of до фильтров: {len(quarter_issues)}"
        )
        print(render_console(quarter_metrics))

        if args.verbose:
            print(f"\nФактический JQL релизов: {release_jql}")
            print("Последние 90 дней:")
            for metric in rolling_metrics:
                print(
                    f"{metric.team}: Story={','.join(metric.story_keys) or '—'}; "
                    f"Bug={','.join(metric.bug_keys) or '—'}"
                )
            print(f"{quarter_start.year} Q{quarter}:")
            for metric in quarter_metrics:
                print(
                    f"{metric.team}: Story={','.join(metric.story_keys) or '—'}; "
                    f"Bug={','.join(metric.bug_keys) or '—'}"
                )

        if args.no_publish:
            execution_log(
                f"Готово за {elapsed_seconds(job_started)}; "
                "Confluence не изменён (--no-publish)"
            )
            return 0

        publish_started = time.perf_counter()
        execution_log(
            f"Confluence: публикую отчёт на страницу {DEFAULT_CONFLUENCE_PAGE_ID}"
        )
        publisher = ConfluencePublisher(load_confluence_settings())
        page_url = publisher.publish(
            body=report_html,
            page_id=DEFAULT_CONFLUENCE_PAGE_ID,
        )
        print(f"CONFLUENCE_PAGE_URL={page_url}")
        execution_log(
            f"Confluence: публикация завершена ({elapsed_seconds(publish_started)}); "
            f"весь запуск={elapsed_seconds(job_started)}"
        )
        return 0
    except KeyboardInterrupt:
        execution_log(
            f"Остановлено пользователем через {elapsed_seconds(job_started)}",
            error=True,
        )
        return 130
    except Exception as exc:
        execution_log(
            f"Ошибка через {elapsed_seconds(job_started)}: {exc}",
            error=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
