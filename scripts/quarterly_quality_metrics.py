#!/usr/bin/env python3
"""Quarterly released Story/Bug quality metrics for Jira and Confluence.

The script finds Release 2.0 issues installed to production during a quarter,
collects Story/Bug issues included in those releases, calculates the eligible
bug-to-story ratio for each team, and publishes a Confluence table.

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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_JIRA_URL = "https://jira.sberbank.ru"
DEFAULT_CONFLUENCE_URL = "https://confluence.sberbank.ru"
HTTP_TIMEOUT_SECONDS = 60
# Корпоративные Jira/Confluence используют self-signed CA. release_checker.py
# также принудительно отключает verify для этих подключений.
VERIFY_SSL = False
DEFAULT_RELEASE_PROJECT = "HRPRELEASE"
DEFAULT_RELEASE_ISSUE_TYPE = "Release 2.0"
DEFAULT_RELEASE_CREATED_SINCE = "2025-09-01"
DEFAULT_RELEASE_KE_FIELD_NAME = "КЭ"
DEFAULT_RELEASE_DATE_FIELD_NAME = "Дата установки на ПРОМ"
DEFAULT_RELEASE_DATE_FIELD_ID = ""
DEFAULT_RELEASE_TYPE_FIELD_ID = "customfield_23500"
DEFAULT_DETECTION_STAND_FIELD_ID = "customfield_17500"
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
DEFAULT_PSI_STANDS = ("PSI", "ПСИ")
DEFAULT_PROM_STANDS = ("PROM", "ПРОМ")
DEFAULT_RELEASE_LINK_KEYWORDS = ("consist", "part")
DEFAULT_HOTFIX_VALUES = ("Hotfix",)
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

TEAM_SETTINGS_JSON = r"""
[
  {"team": "HRP Core UI", "project_keys": ["HRM"], "target_ratio": 0.12},
  {"team": "HRP Core Tech", "project_keys": ["HRC"], "target_ratio": 0.13},
  {
    "team": "Core UI 2.0 / Neuro UI",
    "project_keys": ["NEUROUI"],
    "target_ratio": 0.24
  },
  {
    "team": "Профиль сотрудника",
    "project_keys": ["SFILE"],
    "target_ratio": 0.24
  },
  {
    "team": "Продуктовая аналитика",
    "project_keys": ["SEARCHCS"],
    "target_ratio": 0.10
  },
  {"team": "Люди Сбера", "project_keys": ["SBRPPL"], "target_ratio": 0.10},
  {
    "team": "Управление эффективностью",
    "project_keys": ["PERFREVIEW"],
    "target_ratio": 0.33
  },
  {
    "team": "Ассистент HR",
    "project_keys": ["HRPASSIST"],
    "target_ratio": 0.09
  }
]
"""
TEAM_SETTINGS: tuple[dict[str, Any], ...] = tuple(json.loads(TEAM_SETTINGS_JSON))


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
    return ", ".join(jql_value(key) for key in cleaned)


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
        if not team:
            raise ValueError("У команды не задано имя.")
        if not project_keys:
            raise ValueError(f"{team}: не задан ни один Jira project key.")
        if target_ratio <= 0 or target_ratio >= 1:
            raise ValueError(f"{team}: target_ratio должен быть больше 0 и меньше 1.")
        return cls(team=team, project_keys=project_keys, target_ratio=target_ratio)


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
    psi_stands: frozenset[str]
    prom_stands: frozenset[str]

    @classmethod
    def defaults(cls) -> "MetricRules":
        return cls(
            story_types=normalized_set(DEFAULT_STORY_TYPES),
            story_statuses=normalized_set(DEFAULT_STORY_STATUSES),
            bug_types=normalized_set(DEFAULT_BUG_TYPES),
            bug_statuses=normalized_set(DEFAULT_BUG_STATUSES),
            bug_priorities=normalized_set(DEFAULT_BUG_PRIORITIES),
            psi_stands=normalized_set(DEFAULT_PSI_STANDS),
            prom_stands=normalized_set(DEFAULT_PROM_STANDS),
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
    target = spec.target_ratio
    stories = counts.stories
    bugs = counts.bugs

    if stories > 0:
        actual_ratio: Optional[Decimal] = Decimal(bugs) / Decimal(stories)
    else:
        actual_ratio = None

    if stories == 0 and bugs == 0:
        attainment = None
        infinite_attainment = False
        state = "Нет релизов"
    elif bugs == 0:
        attainment = None
        infinite_attainment = True
        state = "Цель выполнена"
    elif stories == 0:
        attainment = Decimal(0)
        infinite_attainment = False
        state = "Ниже цели"
    else:
        attainment = (target / actual_ratio) * Decimal(100) if actual_ratio else None
        infinite_attainment = False
        state = "Цель выполнена" if actual_ratio <= target else "Ниже цели"

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


def classify_eligible_stand(raw_value: Any, rules: MetricRules) -> Optional[str]:
    stands = {normalized(value) for value in named_values(raw_value)}
    if stands & rules.prom_stands:
        return "prom"
    if stands & rules.psi_stands:
        return "psi"
    return None


def aggregate_issues(
    team_specs: Sequence[TeamSpec],
    issues: Iterable[Mapping[str, Any]],
    rules: MetricRules,
    detection_stand_field_id: str,
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

        project = issue_field(issue, "project")
        project_key = named_value(project).upper()
        team = project_to_team.get(project_key)
        if team is None:
            continue

        issue_type = normalized(named_value(issue_field(issue, "issuetype")))
        status = normalized(named_value(issue_field(issue, "status")))

        if issue_type in rules.story_types and status in rules.story_statuses:
            counts[team].stories += 1
            counts[team].story_keys.append(key)
            continue

        if issue_type not in rules.bug_types or status not in rules.bug_statuses:
            continue

        priority = normalized(named_value(issue_field(issue, "priority")))
        if priority not in rules.bug_priorities:
            continue

        stand = classify_eligible_stand(
            issue_field(issue, detection_stand_field_id),
            rules,
        )
        if stand is None:
            continue

        counts[team].bugs += 1
        counts[team].bug_keys.append(key)
        if stand == "prom":
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
                time.sleep(min(2 ** (attempt - 1), 8))
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
                time.sleep(max(0, min(delay, 30)))
                continue
            raise HttpRequestError(self.service, response.status_code, response.text)
        raise AssertionError("unreachable")


class JiraClient:
    def __init__(self, settings: ConnectionSettings):
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


def find_jira_field_id(
    jira: JiraClient,
    explicit_id: str,
    requested_name: str,
    candidates: Sequence[str],
) -> str:
    if explicit_id.strip():
        return explicit_id.strip()
    wanted = normalized_set((requested_name, *candidates))
    fields = jira.fields()
    for item in fields:
        if normalized(item.get("name")) in wanted:
            field_id = str(item.get("id") or "").strip()
            if field_id:
                return field_id
    partial = [
        item
        for item in fields
        if any(candidate in normalized(item.get("name")) for candidate in wanted)
    ]
    if len(partial) == 1:
        return str(partial[0].get("id") or "").strip()
    available = ", ".join(
        sorted(str(item.get("name")) for item in partial if item.get("name"))
    )
    suffix = f" Похожие поля: {available}." if available else ""
    raise RuntimeError(
        f"Не найдено Jira-поле '{requested_name}'. "
        "Укажите его id в константе DEFAULT_RELEASE_DATE_FIELD_ID внутри скрипта."
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
        link_type = link.get("type")
        link_type_text = " ".join(named_values(link_type))
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
    detection_stand_field_id: str,
    link_keywords: Sequence[str],
    verbose: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    release_fields = (
        "summary",
        "status",
        "issuelinks",
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
    release_candidates = jira.search(release_jql, release_fields)

    releases = [
        release
        for release in release_candidates
        if (
            (installed := parse_jira_date(issue_field(release, release_date_field_id)))
            is not None
            and start <= installed < end
        )
    ]
    if verbose:
        with_install_date = sum(
            parse_jira_date(issue_field(release, release_date_field_id)) is not None
            for release in release_candidates
        )
        print(
            f"Jira вернула релизов: {len(release_candidates)}; "
            f"с датой установки: {with_install_date}; "
            f"в выбранном квартале: {len(releases)}"
        )

    release_keys = sorted(
        str(release.get("key") or "").strip().upper()
        for release in releases
        if str(release.get("key") or "").strip()
    )
    linked_keys = sorted(
        {
            key
            for release in releases
            for key in release_linked_keys(release, link_keywords)
        }
    )

    issue_fields = (
        "summary",
        "project",
        "issuetype",
        "status",
        "priority",
        detection_stand_field_id,
    )
    issues_by_key: dict[str, dict[str, Any]] = {}
    for key_batch in batches(linked_keys):
        jql = f"key in ({issue_keys_jql(key_batch)})"
        for issue in jira.search(jql, issue_fields):
            key = str(issue.get("key") or "").strip().upper()
            if key:
                issues_by_key[key] = issue

    for release_batch in batches(release_keys, size=50):
        if not release_batch:
            continue
        parent_jql = f"parent in ({issue_keys_jql(release_batch)})"
        try:
            child_issues = jira.search(parent_jql, issue_fields)
        except HttpRequestError as exc:
            if exc.status_code != 400:
                raise
            child_issues = []
            if verbose:
                print(
                    "Jira не поддержала parent in (...) для релизов; "
                    "учтены задачи из связей consist of / is part of.",
                    file=sys.stderr,
                )
        for issue in child_issues:
            key = str(issue.get("key") or "").strip().upper()
            if key:
                issues_by_key[key] = issue

    return releases, list(issues_by_key.values()), release_jql


def decimal_text(value: Decimal, places: int = 3) -> str:
    quantizer = Decimal(1).scaleb(-places)
    return format(value.quantize(quantizer), "f").replace(".", ",")


def ratio_text(value: Optional[Decimal], bugs: int, stories: int) -> str:
    if value is None:
        return "∞" if bugs > 0 and stories == 0 else "—"
    return f"{decimal_text(value)} ({decimal_text(value * 100, 1)}%)"


def target_text(value: Decimal) -> str:
    return f"{decimal_text(value, 2)} ({decimal_text(value * 100, 0)}%)"


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


def render_confluence_html(
    metrics: Sequence[TeamMetric],
    *,
    start: date,
    end: date,
    quarter: int,
    release_count: int,
    hotfix_count: int,
    generated_at: datetime,
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
        '<div style="background-color:#172B4D;border-radius:12px;'
        'color:#FFFFFF;margin-bottom:14px;padding:20px 22px;">',
        '<div style="color:#79F2C0;font-size:12px;font-weight:bold;'
        'letter-spacing:.08em;text-transform:uppercase;">Quality Radar</div>',
        f'<div style="font-size:25px;font-weight:bold;margin-top:5px;">'
        f"Качество релизов команд · {start.year} Q{quarter}</div>",
        '<div style="color:#B3D4FF;font-size:13px;margin-top:7px;">'
        "Story и критичные PSI/ПРОМ-баги из установленных на ПРОМ "
        "плановых релизов и Hotfix</div>",
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
        "Факт = баги / Story, выполнение = целевой коэффициент / факт × 100%. "
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
            '<th style="background-color:#253858;border:1px solid #42526E;'
            'color:#FFFFFF;font-size:12px;font-weight:bold;padding:9px 7px;'
            'text-align:center;vertical-align:middle;">'
            f"{html.escape(header)}</th>"
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
        parts.append(
            f'<td style="{base_cell}background-color:#EAE6FF;color:#403294;'
            f'font-weight:bold;">{html.escape(target_text(metric.target_ratio))}</td>'
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

    parts.extend(
        [
            "</tbody></table>",
            '<div style="background-color:#F4F5F7;border-radius:7px;color:#5E6C84;'
            'font-size:12px;margin-top:14px;padding:11px 13px;">',
            "В баги входят только Closed/Закрыт с приоритетом "
            "Critical/Crytical, Blocker, Высокий или Блокирующий "
            "и стендом обнаружения PSI/ПСИ или PROM/ПРОМ. "
            "«Ещё ПРОМ-багов» — целый запас до коэффициента; «Story до цели» — "
            "минимальное число дополнительных Story для возвращения к 100%.",
            f"<br/>Обновлено автоматически: {html.escape(generated_at.isoformat(timespec='seconds'))}.",
            "</div>",
        ]
    )
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
            "Собирает за квартал отношение закрытых Critical/Blocker/Высокий/"
            "Блокирующий PSI/ПРОМ-багов к зарелизенным Story и публикует "
            "таблицу в Confluence."
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
    try:
        team_specs = load_team_specs()
        rules = MetricRules.defaults()
        if args.quarter:
            start, end, quarter = parse_quarter(args.quarter)
        else:
            start, end, quarter = quarter_bounds(datetime.now().astimezone().date())

        jira = JiraClient(load_jira_settings())
        release_date_name = DEFAULT_RELEASE_DATE_FIELD_NAME
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
        detection_stand_field_id = DEFAULT_DETECTION_STAND_FIELD_ID
        release_type_field_id = DEFAULT_RELEASE_TYPE_FIELD_ID

        releases, issues, release_jql = collect_released_issues(
            jira,
            start=start,
            end=end,
            release_project=DEFAULT_RELEASE_PROJECT,
            release_issue_type=DEFAULT_RELEASE_ISSUE_TYPE,
            release_ke_field_name=DEFAULT_RELEASE_KE_FIELD_NAME,
            release_ke_ids=RELEASE_KE_IDS,
            release_created_since=DEFAULT_RELEASE_CREATED_SINCE,
            release_date_field_id=release_date_field_id,
            release_type_field_id=release_type_field_id,
            detection_stand_field_id=detection_stand_field_id,
            link_keywords=DEFAULT_RELEASE_LINK_KEYWORDS,
            verbose=args.verbose,
        )
        hotfix_count = sum(
            is_hotfix_release(release, release_type_field_id, DEFAULT_HOTFIX_VALUES)
            for release in releases
        )
        counts = aggregate_issues(
            team_specs,
            issues,
            rules,
            detection_stand_field_id,
        )
        metrics = [calculate_metric(spec, counts[spec.team]) for spec in team_specs]
        generated_at = datetime.now().astimezone()
        report_html = render_confluence_html(
            metrics,
            start=start,
            end=end,
            quarter=quarter,
            release_count=len(releases),
            hotfix_count=hotfix_count,
            generated_at=generated_at,
        )

        print(
            f"\n{start.year} Q{quarter}: {start.isoformat()} — "
            f"{(end - timedelta(days=1)).isoformat()}"
        )
        print(
            f"Релизов: {len(releases)} "
            f"(плановых: {len(releases) - hotfix_count}, Hotfix: {hotfix_count}); "
            f"задач состава релизов: {len(issues)}"
        )
        print(render_console(metrics))

        if args.verbose:
            print(f"\nФактический JQL релизов: {release_jql}")
            for metric in metrics:
                print(
                    f"{metric.team}: Story={','.join(metric.story_keys) or '—'}; "
                    f"Bug={','.join(metric.bug_keys) or '—'}"
                )

        if args.no_publish:
            print("Confluence не изменён (--no-publish).")
            return 0

        publisher = ConfluencePublisher(load_confluence_settings())
        page_url = publisher.publish(
            body=report_html,
            page_id=DEFAULT_CONFLUENCE_PAGE_ID,
        )
        print(f"CONFLUENCE_PAGE_URL={page_url}")
        return 0
    except KeyboardInterrupt:
        print("Остановлено пользователем.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
