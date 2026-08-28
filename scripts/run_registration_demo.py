#!/usr/bin/env python3
"""Run a real discover -> fix -> retest acceptance cycle locally."""

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from examples.registration_demo.server import BUGGY_RELEASE, FIXED_RELEASE, RegistrationDemoServer


def _configure_demo_environment(base_url: str, artifact_dir: Path, max_steps: int) -> None:
    values = {
        "START_URL": base_url,
        "HEADLESS": "true",
        "BROWSER_SLOW_MO": "0",
        "HIGHLIGHT_DURATION_MS": "0",
        "BROWSER_CHANNEL": "chrome",
        "BROWSER_EXECUTABLE_PATH": "",
        "BROWSER_USER_DATA_DIR": "",
        "BROWSER_CHROMIUM_ARGS": "",
        "BROWSER_SUPPRESS_CERT_PROMPT": "0",
        "ACTION_TIMEOUT_MS": "5000",
        "POST_ACTION_DELAY": "0.2",
        "WAIT_NETWORK_IDLE_MS": "500",
        "CHECKLIST_STEP_DELAY_MS": "0",
        "MAX_STEPS": str(max_steps),
        "AGENT_CONTINUOUS_RESTART": "false",
        "AGENT_MEMORY_PATH": "",
        "ENABLE_QA_RETEST_MONITOR": "0",
        "ENABLE_TEST_PLAN_START": "false",
        "ENABLE_ORACLE_AFTER_ACTION": "false",
        "ENABLE_SECOND_PASS_BUG": "false",
        "ORACLE_ON_VISUAL_OR_ERROR": "false",
        "ENABLE_SCENARIO_CHAINS": "false",
        "ENABLE_RESPONSIVE_TEST": "false",
        "A11Y_CHECK_EVERY_N": "0",
        "PERF_CHECK_EVERY_N": "0",
        "IFRAME_CHECK_EVERY_N": "0",
        "RESPONSIVE_CHECK_EVERY_N": "0",
        "SESSION_PERSIST_CHECK_EVERY_N": "0",
        "BROKEN_LINKS_CHECK_EVERY_N": "0",
        "TEST_SPEC_YAML_PATH": "",
        "SESSION_STATE_SAVE_PATH": "",
        "SESSION_STATE_RESTORE_PATH": "",
        "RECORD_VIDEO_DIR": "",
        "VISUAL_BASELINE_DIR": "",
        "PLAYWRIGHT_EXPORT_PATH": "",
        "JUNIT_REPORT_PATH": "",
        "SESSION_REPORT_SAVE_EVERY_N": "0",
        "SESSION_REPORT_EVERY_N": "0",
        "SESSION_REPORT_PATH": str(artifact_dir / "agent-session.txt"),
        "SESSION_REPORT_HTML_PATH": str(artifact_dir / "agent-session.html"),
        "LOCAL_LLM_API_URL": base_url + "/llm/v1",
        "LOCAL_LLM_MODEL": "demo-disabled-model",
        "LLM_RETRY_COUNT": "1",
        "LLM_RETRY_BASE_DELAY": "0",
        "LLM_RETRY_MAX_DELAY": "0",
        "LLM_RESPONSE_TIMEOUT_SEC": "1",
        "LLM_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS": "1",
        "LLM_CIRCUIT_BREAKER_COOLDOWN_SEC": "3600",
        "JIRA_URL": base_url,
        "JIRA_USERNAME": "",
        "JIRA_EMAIL": "",
        "JIRA_API_TOKEN": "demo-bearer-token-for-local-debug-jira",
        "JIRA_PROJECT_KEY": "DEMO",
        "JIRA_ISSUE_TYPE": "Bug",
        "JIRA_ASSIGNEE": "kventin-agent",
        "JIRA_PRIORITY_CRITICAL": "Critical",
        "JIRA_PRIORITY_MAJOR": "Major",
        "JIRA_PRIORITY_MINOR": "Minor",
        "JIRA_RETRY_COUNT": "1",
        "JIRA_RETRY_BASE_DELAY": "0",
        "JIRA_RETRY_MAX_DELAY": "0",
        "JIRA_RETEST_STATUS_READY_FOR_QA": "Ready for QA",
        "JIRA_RETEST_STATUS_QA": "QA",
        "JIRA_RETEST_STATUS_IN_PROGRESS": "In Progress",
        "JIRA_RETEST_STATUS_RESOLVED": "Closed",
        "JIRA_RETEST_RESOLUTION_FIXED": "Fixed",
    }
    os.environ.update(values)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _find_registration_issue(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    for issue in snapshot.get("issues") or []:
        text = "%s\n%s" % (issue.get("summary") or "", issue.get("description") or "")
        if "/api/register" in text and "500" in text:
            return issue
    raise AssertionError("Агент не завёл известный дефект HTTP 500 на /api/register")


def _capture_layout_screenshots(base_url: str, artifact_dir: Path) -> Dict[str, Any]:
    from playwright.sync_api import sync_playwright

    from agent.browser.browser_options import build_browser_launch_options

    layouts: Dict[str, Any] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            **build_browser_launch_options(engine_name="chromium")
        )
        try:
            for name, width, height in (
                ("desktop", 1440, 900),
                ("mobile", 390, 844),
            ):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
                metrics = page.evaluate(
                    """() => {
                        const form = document.querySelector('.form-panel');
                        const rect = form.getBoundingClientRect();
                        return {
                            viewportWidth: window.innerWidth,
                            scrollWidth: document.documentElement.scrollWidth,
                            formLeft: rect.left,
                            formRight: rect.right,
                            formWidth: rect.width
                        };
                    }"""
                )
                if metrics["scrollWidth"] > metrics["viewportWidth"] + 1:
                    raise AssertionError("%s layout имеет горизонтальный overflow" % name)
                if metrics["formLeft"] < -1 or metrics["formRight"] > metrics["viewportWidth"] + 1:
                    raise AssertionError("%s form выходит за пределы viewport" % name)
                screenshot = artifact_dir / ("layout-%s.png" % name)
                page.screenshot(path=str(screenshot), full_page=True)
                layouts[name] = metrics
                page.close()
        finally:
            browser.close()
    return layouts


def run_cycle(artifact_dir: Path, max_steps: int = 7) -> Dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    server = RegistrationDemoServer().start()
    base_url = server.base_url
    _configure_demo_environment(base_url, artifact_dir, max_steps)

    result: Dict[str, Any] = {
        "success": False,
        "application_url": base_url,
        "debug_jira_url": base_url + "/debug/issues",
        "stages": [],
    }

    try:
        # Imports are intentionally delayed until the deterministic demo
        # environment is complete because config.py reads values at import time.
        from agent.core.agent import run_agent
        from agent.defects.defect_builder import RETEST_JSON_MARKER
        from agent.defects.defect_retest import process_retest_issue

        layouts = _capture_layout_screenshots(base_url, artifact_dir)
        result["stages"].append({
            "name": "layout_verified",
            "ok": True,
            "viewports": layouts,
        })

        print("[demo] 1/4 Buggy release: %s" % base_url)
        discovery = run_agent(
            start_url=base_url,
            max_steps=max_steps,
            enable_qa_retests=False,
        )
        discovered_snapshot = server.state.snapshot()
        issue = _find_registration_issue(discovered_snapshot)
        description = issue.get("description") or ""
        if RETEST_JSON_MARKER not in description:
            raise AssertionError("В debug Jira нет машинного Kventin-сценария ретеста")
        required_sections = (
            "h3. Описание проблемы",
            "h3. Ожидаемый результат (ОР)",
            "h3. Фактический результат (ФР)",
            "h3. Шаги воспроизведения",
            "h3. Окружение",
        )
        missing_sections = [section for section in required_sections if section not in description]
        if missing_sections:
            raise AssertionError(
                "В описании дефекта отсутствуют секции: %s" % ", ".join(missing_sections)
            )
        expected_attributes = {
            "issue_type": "Bug",
            "priority": "Critical",
            "assignee": "kventin-agent",
            "release": BUGGY_RELEASE,
        }
        wrong_attributes = {
            name: issue.get(name)
            for name, expected in expected_attributes.items()
            if issue.get(name) != expected
        }
        if wrong_attributes:
            raise AssertionError("Некорректные атрибуты debug issue: %s" % wrong_attributes)
        if "severity-critical" not in (issue.get("labels") or []):
            raise AssertionError("Severity дефекта не сохранена в labels")
        if len(issue.get("attachments") or []) < 4:
            raise AssertionError("К дефекту не приложен полный набор evidence")
        if int(discovery.get("defects") or 0) < 1:
            raise AssertionError("run_agent не подтвердил создание дефекта в summary")

        _write_json(artifact_dir / "debug-jira-discovered.json", discovered_snapshot)
        result["stages"].append({
            "name": "defect_discovered",
            "ok": True,
            "issue_key": issue["key"],
            "status": issue["status"],
            "agent": discovery,
        })
        print("[demo] 2/4 Defect filed: %s (%s)" % (issue["key"], issue["status"]))

        server.state.deploy_fix()
        fixed_status, fixed_response = server.state.register({
            "fullName": "Иван Тестов",
            "email": "test@example.com",
            "password": "TestPass123!",
            "terms": True,
        })
        if fixed_status != 201 or server.state.snapshot()["release"] != FIXED_RELEASE:
            raise AssertionError("Исправленный release не принимает валидную регистрацию")
        result["stages"].append({
            "name": "fix_deployed",
            "ok": True,
            "release": FIXED_RELEASE,
            "probe": fixed_response,
        })
        print("[demo] 3/4 Fix deployed: %s" % FIXED_RELEASE)

        if not server.state.mark_ready_for_qa(issue["key"]):
            raise AssertionError("Не удалось перевести debug issue в Ready for QA")
        if not process_retest_issue(issue["key"]):
            raise AssertionError("Штатный процесс ретеста не обработал debug issue")

        final_snapshot = server.state.snapshot()
        final_issue = next(
            item for item in final_snapshot["issues"] if item["key"] == issue["key"]
        )
        comments = "\n".join(final_issue.get("comments") or [])
        if final_issue.get("status") != "Closed":
            raise AssertionError("После успешного ретеста дефект не перешёл в Closed")
        if final_issue.get("resolution") != "Fixed":
            raise AssertionError("После успешного ретеста не выставлена resolution=Fixed")
        if "ретест" not in comments.casefold() or "пройден" not in comments.casefold():
            raise AssertionError("В debug issue нет подтверждения успешного ретеста")

        result["stages"].append({
            "name": "retest_passed",
            "ok": True,
            "issue_key": final_issue["key"],
            "status": final_issue["status"],
            "resolution": final_issue["resolution"],
        })
        result["success"] = True
        result["issue"] = {
            "key": final_issue["key"],
            "summary": final_issue["summary"],
            "description": final_issue["description"],
            "issue_type": final_issue["issue_type"],
            "priority": final_issue["priority"],
            "assignee": final_issue["assignee"],
            "labels": final_issue["labels"],
            "attachments": final_issue["attachments"],
            "status": final_issue["status"],
            "resolution": final_issue["resolution"],
            "comments": final_issue["comments"],
        }
        _write_json(artifact_dir / "debug-jira-final.json", final_snapshot)
        print("[demo] 4/4 Retest passed: %s -> Closed / Fixed" % issue["key"])
        print("[demo] SUCCESS")
        return result
    except Exception as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        result["traceback"] = traceback.format_exc()
        _write_json(artifact_dir / "debug-jira-failure.json", server.state.snapshot())
        raise
    finally:
        _write_json(artifact_dir / "result.json", result)
        server.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kventin registration demo: discover, fix and retest a known bug"
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=REPO_ROOT / "artifacts" / "registration-demo",
    )
    parser.add_argument("--max-steps", type=int, default=7)
    args = parser.parse_args()
    try:
        result = run_cycle(args.artifacts_dir.resolve(), max_steps=max(5, args.max_steps))
    except Exception as exc:
        print("[demo] FAILED: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
