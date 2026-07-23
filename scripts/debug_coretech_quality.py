#!/usr/bin/env python3
"""Read-only reconciliation of CoreTech quality metrics against a CSV export.

The script intentionally reuses quarterly_quality_metrics.py, so release search,
exact consist-of loading, period boundaries, and Story/Bug filters stay identical
to the production report. It never writes to Confluence.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import quarterly_quality_metrics as metrics


CORETECH_PROJECT = "HRC"
CORETECH_TEAM = "HRP Core Tech"
DEFAULT_REFERENCE_CSV = Path(
    "/Users/walloris/Downloads/885d7863-8c12-48ea-9a56-291e4a72e76e.csv"
)
REFERENCE_KEY_COLUMN = "Ключ задачи"
REFERENCE_TYPE_COLUMN = "Тип задачи"
REFERENCE_SUMMARY_COLUMN = "Название задачи"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Сверяет Story/Bug CoreTech из CSV с задачами, найденными через "
            "установленные релизы и точные связи consist of. Confluence не меняет."
        )
    )
    parser.add_argument(
        "--reference-csv",
        default=str(DEFAULT_REFERENCE_CSV),
        help="CSV-выгрузка с эталонными Story/Bug CoreTech.",
    )
    parser.add_argument(
        "--quarter",
        help="Квартал YYYY-QN. По умолчанию текущий квартал.",
    )
    parser.add_argument(
        "--output",
        help=(
            "Путь итогового JSON. По умолчанию создаётся "
            "coretech_debug_YYYYMMDD_HHMMSS.json в текущей папке."
        ),
    )
    return parser.parse_args(argv)


def normalize_reference_key(value: Any) -> str:
    text = str(value or "").strip().upper()
    match = re.search(r"\bHRC-\d+\b", text)
    return match.group(0) if match else text.removeprefix("S-")


def load_reference_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise RuntimeError(f"Не найдена CSV-выгрузка: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise RuntimeError("CSV-выгрузка не содержит заголовков.")
        required = {
            REFERENCE_KEY_COLUMN,
            REFERENCE_TYPE_COLUMN,
            REFERENCE_SUMMARY_COLUMN,
        }
        missing_columns = sorted(required - set(reader.fieldnames))
        if missing_columns:
            raise RuntimeError(
                "В CSV нет обязательных колонок: " + ", ".join(missing_columns)
            )

        rows: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for row_number, raw_row in enumerate(reader, start=2):
            key = normalize_reference_key(raw_row.get(REFERENCE_KEY_COLUMN))
            if not key:
                raise RuntimeError(f"CSV, строка {row_number}: пустой ключ задачи.")
            if key in seen_keys:
                raise RuntimeError(f"CSV содержит повторный ключ {key}.")
            seen_keys.add(key)
            rows.append(
                {
                    "key": key,
                    "export_type": str(
                        raw_row.get(REFERENCE_TYPE_COLUMN) or ""
                    ).strip(),
                    "export_summary": str(
                        raw_row.get(REFERENCE_SUMMARY_COLUMN) or ""
                    ).strip(),
                    "csv_row": row_number,
                    "raw": dict(raw_row),
                }
            )
    return rows


def period_bounds(
    quarter_arg: str,
) -> tuple[date, date, int, date, date, date, date]:
    today = datetime.now().astimezone().date()
    if quarter_arg:
        quarter_start, quarter_end, quarter = metrics.parse_quarter(quarter_arg)
    else:
        quarter_start, quarter_end, quarter = metrics.quarter_bounds(today)
    rolling_end = today + timedelta(days=1)
    rolling_start = rolling_end - timedelta(days=90)
    collection_start = min(rolling_start, quarter_start)
    collection_end = max(rolling_end, quarter_end)
    return (
        rolling_start,
        rolling_end,
        quarter,
        quarter_start,
        quarter_end,
        collection_start,
        collection_end,
    )


def issue_status_category(issue: Mapping[str, Any]) -> str:
    raw_status = metrics.issue_field(issue, "status")
    if not isinstance(raw_status, Mapping):
        return ""
    return metrics.named_value(raw_status.get("statusCategory"))


def issue_snapshot(
    issue: Mapping[str, Any],
    detection_stage_field_id: str,
    ke_field_id: str,
) -> dict[str, Any]:
    fields = issue.get("fields")
    raw_fields = dict(fields) if isinstance(fields, Mapping) else {}
    detection_stage_values = metrics.named_values(
        metrics.issue_field(issue, detection_stage_field_id)
    )
    snapshot = {
        "key": str(issue.get("key") or "").strip().upper(),
        "summary": metrics.named_value(metrics.issue_field(issue, "summary")),
        "project": metrics.issue_project_key(issue),
        "issue_type": metrics.named_value(metrics.issue_field(issue, "issuetype")),
        "status": metrics.named_value(metrics.issue_field(issue, "status")),
        "status_category": issue_status_category(issue),
        "priority": metrics.named_value(metrics.issue_field(issue, "priority")),
        "detection_stage": detection_stage_values,
        "resolution": metrics.named_value(metrics.issue_field(issue, "resolution")),
        "resolution_date": metrics.issue_field(issue, "resolutiondate"),
        "created": metrics.issue_field(issue, "created"),
        "updated": metrics.issue_field(issue, "updated"),
        "labels": metrics.issue_field(issue, "labels") or [],
        "components": metrics.issue_field(issue, "components") or [],
        "fix_versions": metrics.issue_field(issue, "fixVersions") or [],
        "parent": metrics.issue_field(issue, "parent"),
        "selected_raw_fields": raw_fields,
    }
    if ke_field_id:
        snapshot["ke_field_id"] = ke_field_id
        snapshot["ke"] = metrics.issue_field(issue, ke_field_id)
    return snapshot


def classify_for_period(
    issue: Optional[Mapping[str, Any]],
    period_release_keys: Sequence[str],
    rules: metrics.MetricRules,
    detection_stage_field_id: str,
) -> dict[str, Any]:
    if issue is None:
        return {
            "counted": False,
            "bucket": None,
            "reason_code": "JIRA_ISSUE_NOT_RETURNED",
            "reason": "Jira не вернула задачу по ключу.",
        }
    if not period_release_keys:
        return {
            "counted": False,
            "bucket": None,
            "reason_code": "NO_INSTALLED_RELEASE_IN_PERIOD",
            "reason": (
                "Задача не найдена в consist of релиза со статусом "
                "«Установлен на ПРОМ» в этом периоде."
            ),
        }

    project_key = metrics.issue_project_key(issue)
    if project_key != CORETECH_PROJECT:
        return {
            "counted": False,
            "bucket": None,
            "reason_code": "OTHER_PROJECT",
            "reason": f"Jira project={project_key or 'пусто'}, ожидался HRC.",
        }

    issue_type = metrics.normalized(
        metrics.named_value(metrics.issue_field(issue, "issuetype"))
    )
    raw_status = metrics.issue_field(issue, "status")
    status = metrics.normalized(metrics.named_value(raw_status))

    if issue_type in rules.story_types:
        if not metrics.story_has_done_status(raw_status, rules):
            return {
                "counted": False,
                "bucket": None,
                "reason_code": "STORY_NOT_DONE",
                "reason": (
                    "Story не имеет допустимый Done-статус/Done status category: "
                    f"{metrics.named_value(raw_status) or 'пусто'}."
                ),
            }
        return {
            "counted": True,
            "bucket": "Story",
            "reason_code": "COUNTED_STORY",
            "reason": "Story проекта HRC в Done и в составе установленного релиза.",
        }

    if issue_type not in rules.bug_types:
        return {
            "counted": False,
            "bucket": None,
            "reason_code": "UNSUPPORTED_ISSUE_TYPE",
            "reason": (
                "Тип не относится к Story/Bug: "
                f"{metrics.named_value(metrics.issue_field(issue, 'issuetype')) or 'пусто'}."
            ),
        }
    if status not in rules.bug_statuses:
        return {
            "counted": False,
            "bucket": None,
            "reason_code": "BUG_NOT_CLOSED",
            "reason": (
                "Bug не Closed/Закрыт: "
                f"{metrics.named_value(raw_status) or 'пусто'}."
            ),
        }

    priority = metrics.normalized(
        metrics.named_value(metrics.issue_field(issue, "priority"))
    )
    if priority not in rules.bug_priorities:
        return {
            "counted": False,
            "bucket": None,
            "reason_code": "BUG_PRIORITY_NOT_ELIGIBLE",
            "reason": (
                "Приоритет Bug не входит в Critical/Crytical/Blocker/"
                f"Высокий/Блокирующий: {metrics.named_value(metrics.issue_field(issue, 'priority')) or 'пусто'}."
            ),
        }

    detection_stage = metrics.classify_eligible_detection_stage(
        metrics.issue_field(issue, detection_stage_field_id),
        rules,
    )
    if detection_stage is None:
        return {
            "counted": False,
            "bucket": None,
            "reason_code": "BUG_DETECTION_STAGE_NOT_ELIGIBLE",
            "reason": (
                "Этап обнаружения Bug не PSI/ПСИ/PROM/ПРОМ: "
                f"{metrics.named_values(metrics.issue_field(issue, detection_stage_field_id)) or ['пусто']}."
            ),
        }
    return {
        "counted": True,
        "bucket": "Bug PROM" if detection_stage == "prom" else "Bug PSI",
        "reason_code": "COUNTED_BUG",
        "reason": (
            "Bug проекта HRC, Closed, с допустимым приоритетом и этапом "
            f"обнаружения {detection_stage.upper()}."
        ),
    }


def reverse_release_links(
    issue_keys_by_release: Mapping[str, set[str]],
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for release_key, issue_keys in issue_keys_by_release.items():
        for issue_key in issue_keys:
            result[issue_key].append(release_key)
    return {
        issue_key: sorted(set(release_keys))
        for issue_key, release_keys in result.items()
    }


def release_keys_for_period(
    releases: Iterable[Mapping[str, Any]],
    release_date_field_id: str,
    start: date,
    end: date,
) -> set[str]:
    result: set[str] = set()
    for release in releases:
        installed = metrics.parse_jira_date(
            metrics.issue_field(release, release_date_field_id)
        )
        key = str(release.get("key") or "").strip().upper()
        if key and installed is not None and start <= installed < end:
            result.add(key)
    return result


def counts_dict(counts: metrics.TeamCounts) -> dict[str, Any]:
    return {
        "stories": counts.stories,
        "bugs": counts.bugs,
        "psi_bugs": counts.psi_bugs,
        "prom_bugs": counts.prom_bugs,
        "story_keys": sorted(counts.story_keys),
        "bug_keys": sorted(counts.bug_keys),
    }


def safe_find_ke_field_id(jira: metrics.JiraClient) -> str:
    try:
        return metrics.find_jira_field_id(
            jira,
            "",
            "КЭ",
            ("Конфигурационная единица",),
        )
    except Exception as exc:
        metrics.execution_log(
            f"Debug: поле КЭ не определено, продолжаю без него: {exc}",
            error=True,
        )
        return ""


def load_issue_details(
    jira: metrics.JiraClient,
    issue_keys: set[str],
    fields: Sequence[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    loaded = jira.issues_by_keys(
        issue_keys,
        fields,
        progress_label="Debug: подробные поля Story/Bug",
    )
    missing_keys = sorted(issue_keys - set(loaded))
    fallback, errors = jira.issues_individually(
        missing_keys,
        fields,
        max_workers=1,
        progress_label="Debug: fallback Story/Bug",
    )
    loaded.update(fallback)
    unresolved = sorted(issue_keys - set(loaded))
    for key in unresolved:
        errors.setdefault(key, "Jira не вернула задачу")
    return loaded, errors


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    started_at = time.perf_counter()
    try:
        reference_path = Path(args.reference_csv).expanduser().resolve()
        reference_rows = load_reference_rows(reference_path)
        reference_by_key = {row["key"]: row for row in reference_rows}
        reference_keys = set(reference_by_key)
        export_counts = Counter(row["export_type"] for row in reference_rows)
        metrics.execution_log(
            f"Debug CoreTech: CSV строк={len(reference_rows)}, "
            f"Story={export_counts.get('Story', 0)}, "
            f"Bug={export_counts.get('Bug', 0)}"
        )

        (
            rolling_start,
            rolling_end,
            quarter,
            quarter_start,
            quarter_end,
            collection_start,
            collection_end,
        ) = period_bounds(args.quarter or "")

        jira = metrics.JiraClient(metrics.load_jira_settings())
        release_date_field_id = metrics.find_jira_field_id(
            jira,
            metrics.configured_jira_field_id(
                "prod_installed_date_id",
                metrics.DEFAULT_RELEASE_DATE_FIELD_ID,
            ),
            metrics.DEFAULT_RELEASE_DATE_FIELD_NAME,
            (
                "Дата установки на пром",
                "Дата установки в ПРОМ",
                "Дата установки в пром",
                "Дата установки (ПРОМ)",
                "Дата установки PROD",
            ),
        )
        detection_stage_field_id = metrics.DEFAULT_DETECTION_STAGE_FIELD_ID
        release_type_field_id = metrics.DEFAULT_RELEASE_TYPE_FIELD_ID

        releases, basic_issues, release_jql, issue_keys_by_release = (
            metrics.collect_released_issues(
                jira,
                start=collection_start,
                end=collection_end,
                release_project=metrics.DEFAULT_RELEASE_PROJECT,
                release_issue_type=metrics.DEFAULT_RELEASE_ISSUE_TYPE,
                release_ke_field_name=metrics.DEFAULT_RELEASE_KE_FIELD_NAME,
                release_ke_ids=metrics.RELEASE_KE_IDS,
                release_created_since=metrics.DEFAULT_RELEASE_CREATED_SINCE,
                release_date_field_id=release_date_field_id,
                release_type_field_id=release_type_field_id,
                detection_stage_field_id=detection_stage_field_id,
                link_keywords=metrics.DEFAULT_RELEASE_LINK_KEYWORDS,
                verbose=False,
            )
        )

        basic_by_key = {
            str(issue.get("key") or "").strip().upper(): issue
            for issue in basic_issues
            if str(issue.get("key") or "").strip()
        }
        linked_coretech_keys = {
            key
            for key, issue in basic_by_key.items()
            if metrics.issue_project_key(issue) == CORETECH_PROJECT
        }
        diagnostic_keys = reference_keys | linked_coretech_keys
        ke_field_id = safe_find_ke_field_id(jira)
        diagnostic_fields = [
            "summary",
            "project",
            "issuetype",
            "status",
            "priority",
            detection_stage_field_id,
            "resolution",
            "resolutiondate",
            "created",
            "updated",
            "labels",
            "components",
            "fixVersions",
            "parent",
        ]
        if ke_field_id:
            diagnostic_fields.append(ke_field_id)
        detailed_by_key, detail_errors = load_issue_details(
            jira,
            diagnostic_keys,
            diagnostic_fields,
        )

        rolling_releases, rolling_issues = metrics.select_period_data(
            releases,
            basic_issues,
            issue_keys_by_release,
            start=rolling_start,
            end=rolling_end,
            release_date_field_id=release_date_field_id,
        )
        quarter_releases, quarter_issues = metrics.select_period_data(
            releases,
            basic_issues,
            issue_keys_by_release,
            start=quarter_start,
            end=quarter_end,
            release_date_field_id=release_date_field_id,
        )
        rolling_release_keys = release_keys_for_period(
            releases,
            release_date_field_id,
            rolling_start,
            rolling_end,
        )
        quarter_release_keys = release_keys_for_period(
            releases,
            release_date_field_id,
            quarter_start,
            quarter_end,
        )
        issue_release_keys = reverse_release_links(issue_keys_by_release)
        rules = metrics.MetricRules.defaults()
        team_spec = next(
            spec
            for spec in metrics.load_team_specs()
            if spec.team == CORETECH_TEAM
        )
        rolling_counts = metrics.aggregate_issues(
            (team_spec,),
            rolling_issues,
            rules,
            detection_stage_field_id,
        )[CORETECH_TEAM]
        quarter_counts = metrics.aggregate_issues(
            (team_spec,),
            quarter_issues,
            rules,
            detection_stage_field_id,
        )[CORETECH_TEAM]

        issue_records: dict[str, dict[str, Any]] = {}
        for key in sorted(diagnostic_keys):
            issue = detailed_by_key.get(key)
            all_release_keys = issue_release_keys.get(key, [])
            rolling_links = sorted(set(all_release_keys) & rolling_release_keys)
            quarter_links = sorted(set(all_release_keys) & quarter_release_keys)
            issue_records[key] = {
                "key": key,
                "in_reference_csv": key in reference_by_key,
                "reference": reference_by_key.get(key),
                "jira": (
                    issue_snapshot(
                        issue,
                        detection_stage_field_id,
                        ke_field_id,
                    )
                    if issue is not None
                    else None
                ),
                "all_installed_release_keys": all_release_keys,
                "rolling_90_days": {
                    "release_keys": rolling_links,
                    "classification": classify_for_period(
                        issue,
                        rolling_links,
                        rules,
                        detection_stage_field_id,
                    ),
                },
                "quarter": {
                    "release_keys": quarter_links,
                    "classification": classify_for_period(
                        issue,
                        quarter_links,
                        rules,
                        detection_stage_field_id,
                    ),
                },
                "detail_error": detail_errors.get(key),
            }

        release_records = []
        for release in sorted(
            releases,
            key=lambda item: str(item.get("key") or ""),
        ):
            release_key = str(release.get("key") or "").strip().upper()
            linked_keys = sorted(issue_keys_by_release.get(release_key, set()))
            installed = metrics.parse_jira_date(
                metrics.issue_field(release, release_date_field_id)
            )
            release_records.append(
                {
                    "key": release_key,
                    "summary": metrics.named_value(
                        metrics.issue_field(release, "summary")
                    ),
                    "status": metrics.named_value(
                        metrics.issue_field(release, "status")
                    ),
                    "installed_date": installed.isoformat() if installed else None,
                    "hotfix": metrics.is_hotfix_release(
                        release,
                        release_type_field_id,
                        metrics.DEFAULT_HOTFIX_VALUES,
                    ),
                    "linked_total": len(linked_keys),
                    "linked_coretech_keys": sorted(
                        set(linked_keys) & linked_coretech_keys
                    ),
                    "linked_reference_keys": sorted(
                        set(linked_keys) & reference_keys
                    ),
                }
            )

        rolling_reference_buckets = Counter(
            record["rolling_90_days"]["classification"]["bucket"] or "Excluded"
            for record in issue_records.values()
            if record["in_reference_csv"]
        )
        quarter_reference_buckets = Counter(
            record["quarter"]["classification"]["bucket"] or "Excluded"
            for record in issue_records.values()
            if record["in_reference_csv"]
        )
        rolling_reason_counts = Counter(
            record["rolling_90_days"]["classification"]["reason_code"]
            for record in issue_records.values()
            if record["in_reference_csv"]
        )
        quarter_reason_counts = Counter(
            record["quarter"]["classification"]["reason_code"]
            for record in issue_records.values()
            if record["in_reference_csv"]
        )

        generated_at = datetime.now().astimezone()
        output_path = (
            Path(args.output).expanduser().resolve()
            if args.output
            else (
                Path.cwd()
                / f"coretech_debug_{generated_at.strftime('%Y%m%d_%H%M%S')}.json"
            )
        )
        report = {
            "generated_at": generated_at.isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            "read_only": True,
            "source_csv": str(reference_path),
            "parameters": {
                "team": CORETECH_TEAM,
                "jira_project": CORETECH_PROJECT,
                "required_release_status": "Установлен на ПРОМ",
                "rolling_start": rolling_start.isoformat(),
                "rolling_end_inclusive": (
                    rolling_end - timedelta(days=1)
                ).isoformat(),
                "quarter": f"{quarter_start.year}-Q{quarter}",
                "quarter_start": quarter_start.isoformat(),
                "quarter_end_inclusive": (
                    quarter_end - timedelta(days=1)
                ).isoformat(),
                "release_date_field_id": release_date_field_id,
                "detection_stage_field_id": detection_stage_field_id,
                "ke_field_id": ke_field_id or None,
                "release_jql": release_jql,
            },
            "summary": {
                "reference_csv": {
                    "total": len(reference_rows),
                    "types": dict(export_counts),
                },
                "jira_collection": {
                    "installed_releases_total": len(releases),
                    "linked_issues_all_projects": len(basic_issues),
                    "linked_coretech_issues": len(linked_coretech_keys),
                    "reference_keys_returned_by_jira": len(
                        reference_keys & set(detailed_by_key)
                    ),
                    "reference_keys_missing_in_jira": sorted(
                        reference_keys - set(detailed_by_key)
                    ),
                    "coretech_keys_not_in_reference_csv": sorted(
                        linked_coretech_keys - reference_keys
                    ),
                },
                "rolling_90_days": {
                    "method_counts": counts_dict(rolling_counts),
                    "reference_classification_buckets": dict(
                        rolling_reference_buckets
                    ),
                    "reference_reason_counts": dict(rolling_reason_counts),
                },
                "quarter": {
                    "method_counts": counts_dict(quarter_counts),
                    "reference_classification_buckets": dict(
                        quarter_reference_buckets
                    ),
                    "reference_reason_counts": dict(quarter_reason_counts),
                },
            },
            "reference_comparison": [
                issue_records[row["key"]]
                for row in reference_rows
            ],
            "linked_coretech_not_in_reference": [
                issue_records[key]
                for key in sorted(linked_coretech_keys - reference_keys)
            ],
            "releases": release_records,
            "detail_errors": detail_errors,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as target:
            json.dump(report, target, ensure_ascii=False, indent=2, default=str)
            target.write("\n")

        metrics.execution_log(
            f"Debug CoreTech завершён за {metrics.elapsed_seconds(started_at)}"
        )
        print(
            "CSV: "
            f"{len(reference_rows)} задач "
            f"(Story={export_counts.get('Story', 0)}, "
            f"Bug={export_counts.get('Bug', 0)})"
        )
        print(
            "Метод, 90 дней: "
            f"Story={rolling_counts.stories}, Bug={rolling_counts.bugs}, "
            f"PSI={rolling_counts.psi_bugs}, PROM={rolling_counts.prom_bugs}"
        )
        print(
            f"Метод, {quarter_start.year} Q{quarter}: "
            f"Story={quarter_counts.stories}, Bug={quarter_counts.bugs}, "
            f"PSI={quarter_counts.psi_bugs}, PROM={quarter_counts.prom_bugs}"
        )
        print(f"DEBUG_REPORT={output_path}")
        print("Пришлите JSON из DEBUG_REPORT целиком.")
        return 0
    except KeyboardInterrupt:
        metrics.execution_log("Debug остановлен пользователем.", error=True)
        return 130
    except Exception as exc:
        metrics.execution_log(f"Ошибка debug-скрипта: {exc}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
