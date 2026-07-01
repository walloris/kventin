"""
Ретест тикетов, заведённых агентом Kventin (лейбл kventin).

Поток:
1. JQL: статус «Ready for QA» (имя настраивается в .env).
2. Перевод в статус QA (JIRA_RETEST_STATUS_QA).
3. Воспроизведение шагов из описания тикета (Playwright).
4. Успех → Resolved + resolution Fixed.
5. Неуспех → In Progress + назначение на автора перевода в Ready for QA (из changelog).

Описание тикета: машинный блок *KVENTIN_RETEST_JSON_V1* (если заводил агент сессии с
действиями) либо текстовый блок «Шаги воспроизведения» (эвристический ретест).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

import agent.core.agent as agent_mod
from config import (
    ACTION_TIMEOUT_MS,
    AUTH_PASSWORD,
    AUTH_SUBMIT_SELECTOR,
    AUTH_URL,
    AUTH_USERNAME,
    BROWSER_CHROMIUM_ARGS,
    BROWSER_ENGINE,
    BROWSER_SLOW_MO,
    BROWSER_SUPPRESS_CERT_PROMPT,
    BROWSER_USER_DATA_DIR,
    ENABLE_SHADOW_DOM,
    HEADLESS,
    JIRA_RETEST_FALLBACK_ASSIGNEE,
    JIRA_RETEST_MAX_ISSUES,
    JIRA_RETEST_STATUS_READY_FOR_QA,
    SESSION_STATE_RESTORE_PATH,
    START_URL,
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
)
from agent.core.agent import AgentMemory, _do_auth_login, _inject_all, execute_action, smart_wait_after_goto
from agent.defects.jira_client import (
    add_issue_comment,
    author_to_assignee_value,
    extract_description_text,
    find_author_who_moved_to_status,
    get_issue_with_changelog,
    reopen_or_move_to_in_progress,
    resolve_issue_fixed,
    search_kventin_issues_by_status,
    start_qa_transition,
    is_jira_rest_configured,
)
from agent.defects.defect_builder import (
    parse_retest_plan_from_description,
    playwright_canonical_to_exec_selector,
)
from agent.browser.page_analyzer import get_dom_summary

LOG = logging.getLogger("kventin.retest")


def _format_retest_lines(items: List[Dict[str, Any]], *, limit: int = 20) -> str:
    lines: List[str] = []
    for item in items[-limit:]:
        if "status" in item:
            lines.append(
                f"* {item.get('status')} {item.get('method', 'GET')} {item.get('url', '')[:500]}"
            )
        else:
            src = item.get("source_url") or ""
            loc = ""
            if src:
                loc = f" ({src[:180]}"
                if item.get("line") is not None:
                    loc += f":{item.get('line')}"
                loc += ")"
            text = (item.get("text") or "").replace("\r", " ").strip()
            stack = (item.get("stack") or "").strip()
            if stack:
                text = f"{text}\n{stack[:1800]}"
            lines.append(f"* {item.get('type', 'console')}: {text[:2200]}{loc}")
    return "\n".join(lines) if lines else "* нет"


def format_retest_failure_comment(
    key: str,
    msg: str,
    *,
    scenario: str,
    start_url: str,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
) -> str:
    action_lines = []
    for a in actions[-30:]:
        action_lines.append(
            f"* #{a.get('step', '?')} {a.get('action', '')} "
            f"{a.get('selector', '')[:220]} -> {(a.get('result') or '')[:500]}"
        )
    return (
        f"h3. Kventin retest — не пройден\n"
        f"Дефект {key} возвращён в работу: исходный сценарий по описанию всё ещё не проходит.\n\n"
        f"*Стартовая страница:* {start_url or '—'}\n"
        f"*Причина:* {{quote}}{msg}{{quote}}\n\n"
        f"h4. Сценарий ретеста\n{scenario or '* нет шагов в описании'}\n\n"
        f"h4. Выполненные действия\n{chr(10).join(action_lines) if action_lines else '* нет'}\n\n"
        f"h4. Консоль / pageerror\n{_format_retest_lines(console_log)}\n\n"
        f"h4. Network >= 400\n{_format_retest_lines(network_failures)}"
    )


def _plan_scenario_text(description: str) -> tuple[str, str]:
    plan = parse_retest_plan_from_description(description or "")
    if plan and plan.get("steps"):
        lines = []
        for idx, step in enumerate(plan.get("steps") or [], 1):
            if not isinstance(step, dict):
                continue
            op = step.get("op") or "step"
            target = step.get("selector") or step.get("url") or step.get("direction") or step.get("key") or ""
            lines.append(f"# {idx}. {op}: {str(target)[:260]}")
        return (plan.get("start_url") or "", "\n".join(lines))
    start_url, steps = parse_reproduction_steps(description or "")
    return start_url, "\n".join(f"# {idx}. {step}" for idx, step in enumerate(steps, 1))


def _short_sig(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _install_retest_observers(
    page: Page,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
) -> None:
    """Collect deterministic signals during retest for the JSON oracle."""

    def on_console(msg):
        try:
            console_log.append({"type": msg.type, "text": msg.text})
        except Exception:
            pass

    def on_pageerror(exc):
        try:
            console_log.append({"type": "pageerror", "text": str(exc)})
        except Exception:
            pass

    def on_response(resp):
        try:
            status = resp.status
            if status >= 400:
                req = resp.request
                network_failures.append({
                    "status": status,
                    "method": req.method,
                    "url": resp.url,
                })
        except Exception:
            pass

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("response", on_response)


def _network_matches(expected: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    exp_status = int(expected.get("status") or 0)
    act_status = int(actual.get("status") or 0)
    if exp_status and exp_status != act_status:
        return False
    exp_method = (expected.get("method") or "").upper()
    act_method = (actual.get("method") or "").upper()
    if exp_method and exp_method != act_method:
        return False
    exp_path = (expected.get("url_path") or "").strip()
    act_url = (actual.get("url") or "").strip()
    if exp_path and exp_path not in act_url:
        return False
    return True


def _assert_retest_oracle(
    plan: Dict[str, Any],
    memory: AgentMemory,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Return False when the original defect signal is still present."""
    oracle = plan.get("oracle") or {}
    if not isinstance(oracle, dict):
        return True, "oracle отсутствует, проверены только шаги"

    action_needles = [
        _short_sig(x)
        for x in (oracle.get("fail_on_action_result_contains") or [])
        if isinstance(x, str) and x.strip()
    ]
    for action in getattr(memory, "actions", []) or []:
        result = _short_sig(action.get("result") or "")
        if result and any(needle in result for needle in action_needles):
            return False, f"повторился action-сигнал: {action.get('action')} -> {action.get('result')}"

    console_needles = [
        _short_sig(x)
        for x in (oracle.get("fail_on_console_contains") or [])
        if isinstance(x, str) and x.strip()
    ]
    for entry in console_log:
        text = _short_sig(entry.get("text") or "")
        if text and any(needle and needle in text for needle in console_needles):
            return False, f"повторился console-сигнал: {entry.get('type')} {entry.get('text')[:180]}"

    expected_network = [
        x for x in (oracle.get("fail_on_network") or [])
        if isinstance(x, dict)
    ]
    for exp in expected_network:
        for actual in network_failures:
            if _network_matches(exp, actual):
                return False, (
                    "повторился network-сигнал: "
                    f"{actual.get('status')} {actual.get('method')} {actual.get('url')[:180]}"
                )

    return True, "шаги выполнены, исходные oracle-сигналы не повторились"


def extract_canonical_locator(description: str) -> str:
    """Вытащить строку канонического локатора из wiki-описания (блок «Затронутый элемент»)."""
    if not description:
        return ""
    for line in description.splitlines():
        low = line.lower()
        if "локатор" in low and ":" in line:
            part = line.split(":", 1)[1].strip()
            part = re.sub(r"^\{\{\s*|\s*\}\}$", "", part).strip()
            # преобразуем в exec-селектор для эвристического пути
            return playwright_canonical_to_exec_selector(part) or part[:600]
    return ""


def parse_reproduction_steps(description: str) -> Tuple[str, List[str]]:
    """
    Извлечь стартовый URL (если есть в первой строке шагов) и список строк шагов.
    """
    desc = description or ""
    start_url = ""
    block = ""
    m = re.search(
        r"h3\.\s*Шаги\s+воспроизведения\s*\n(.*?)(?=\nh3\.|\Z)",
        desc,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        block = m.group(1).strip()
    if not block:
        m2 = re.search(
            r"Шаги\s+воспроизведения\s*\n(.*?)(?=\nh3\.|\n\*?\*?Ожидаемый|\Z)",
            desc,
            re.DOTALL | re.IGNORECASE,
        )
        if m2:
            block = m2.group(1).strip()
    lines_out: List[str] = []
    for raw in block.split("\n"):
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^\#+\s*", "", line).strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("открыть страницу") or low.startswith("открыть url"):
            url_m = re.search(r"https?://[^\s\)\]]+", line)
            if url_m:
                start_url = url_m.group(0).rstrip(".,;)")
        lines_out.append(line)
    return start_url, lines_out


def _interpret_step_to_action(step: str, default_selector: str) -> Optional[Dict[str, Any]]:
    """Сопоставить строку шага из описания с действием execute_action."""
    s = step.strip()
    sl = s.lower()
    url_m = re.search(r"https?://[^\s\)\]]+", s)

    if (sl.startswith("открыть") or sl.startswith("открыть url")) and url_m:
        return {"_navigate": url_m.group(0).rstrip(".,;)")}

    if "прокрутить" in sl or sl.startswith("scroll") or "проскрол" in sl:
        direction = "up" if "вверх" in sl or "up" in sl else "down"
        return {"action": "scroll", "selector": direction, "reason": "retest"}

    if "закрыть модаль" in sl or ("модальн" in sl and "закр" in sl):
        return {"action": "close_modal", "selector": "", "reason": "retest"}

    if "нажать клавиш" in sl:
        key = "Escape" if "escape" in sl or "esc" in sl else "Enter"
        return {"action": "press_key", "selector": "", "value": key, "reason": "retest"}

    if "ввести" in sl or "ввод" in sl:
        val_m = re.search(r"[«\"']([^»\"']{1,200})[»\"']", s)
        val = val_m.group(1) if val_m else "test"
        sel = default_selector
        if not sel:
            return None
        return {"action": "type", "selector": sel, "value": val, "reason": "retest"}

    if any(k in sl for k in ("кликнуть", "нажать", "выбрать опцию", "выбрать")):
        m = re.search(r"[«\"']([^»\"']{1,120})[»\"']", s)
        sel = default_selector or (m.group(1).strip() if m else "")
        if not sel:
            return None
        return {"action": "click", "selector": sel, "reason": "retest"}

    return None


def _retest_launch_browser(p: Playwright) -> Tuple[BrowserContext, Page, Optional[Browser]]:
    engine = getattr(p, BROWSER_ENGINE, p.chromium)
    use_chromium = BROWSER_ENGINE == "chromium" or bool(BROWSER_USER_DATA_DIR)
    chromium_args = list(BROWSER_CHROMIUM_ARGS)
    if use_chromium and BROWSER_SUPPRESS_CERT_PROMPT:
        chromium_args.append("--ignore-certificate-errors")
        if sys.platform == "darwin":
            chromium_args.append("--use-mock-keychain")
    launch_kw: Dict[str, Any] = {"headless": HEADLESS, "slow_mo": BROWSER_SLOW_MO}
    if use_chromium and chromium_args:
        launch_kw["args"] = chromium_args
    ctx_common = {
        "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        "ignore_https_errors": True,
    }
    browser: Optional[Browser] = None
    if BROWSER_USER_DATA_DIR:
        context = p.chromium.launch_persistent_context(
            BROWSER_USER_DATA_DIR,
            **ctx_common,
            **launch_kw,
        )
    else:
        browser = engine.launch(**launch_kw)
        context = browser.new_context(**ctx_common)
    page = context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    if SESSION_STATE_RESTORE_PATH and os.path.isfile(SESSION_STATE_RESTORE_PATH):
        try:
            with open(SESSION_STATE_RESTORE_PATH, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            if isinstance(cookies, list) and cookies:
                context.add_cookies(cookies)
        except Exception as exc:
            LOG.debug("retest: cookies restore: %s", exc)
    return context, page, browser


def _apply_plan_step(
    page: Page,
    memory: AgentMemory,
    step: Dict[str, Any],
    primary_locator: str,
) -> Tuple[bool, str]:
    """Один шаг из JSON-плана defect_builder."""
    op = (step.get("op") or "").lower()
    pl = (primary_locator or "").strip()

    if op == "navigate":
        url = (step.get("url") or "").strip()
        if not url:
            return True, "skip empty navigate"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            smart_wait_after_goto(page, timeout=5000)
            _inject_all(page)
            get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
        except Exception as exc:
            return False, f"navigate {url[:80]}: {exc}"
        return True, "ok"

    act: Dict[str, Any]
    if op == "click":
        sel = (step.get("selector") or pl).strip()
        if not sel:
            return False, "click: нет selector"
        act = {"action": "click", "selector": sel, "reason": "retest"}
    elif op == "hover":
        sel = (step.get("selector") or pl).strip()
        if not sel:
            return False, "hover: нет selector"
        act = {"action": "hover", "selector": sel, "reason": "retest"}
    elif op == "type":
        sel = (step.get("selector") or pl).strip()
        val = step.get("value", "")
        if not sel:
            return False, "type: нет selector"
        act = {"action": "type", "selector": sel, "value": str(val), "reason": "retest"}
    elif op == "scroll":
        direction = (step.get("direction") or "down").lower()
        if direction in ("up", "вверх"):
            direction = "up"
        else:
            direction = "down"
        act = {"action": "scroll", "selector": direction, "reason": "retest"}
    elif op == "close_modal":
        act = {"action": "close_modal", "selector": "", "reason": "retest"}
    elif op == "press_key":
        key = (step.get("key") or "Escape").strip()
        act = {"action": "press_key", "selector": "", "value": key, "reason": "retest"}
    elif op == "select_option":
        sel = (step.get("selector") or pl).strip()
        val = step.get("value", "")
        if not sel:
            return False, "select_option: нет selector"
        act = {"action": "select_option", "selector": sel, "value": str(val), "reason": "retest"}
    elif op == "fill_form":
        act = {"action": "fill_form", "selector": "", "reason": "retest"}
    elif op == "upload_file":
        sel = (step.get("selector") or pl).strip()
        path = (step.get("path") or "").strip()
        if not sel:
            return False, "upload_file: нет selector"
        if not path:
            return False, "upload_file: нет path"
        act = {"action": "upload_file", "selector": sel, "value": path, "reason": "retest"}
    else:
        return True, f"skip unknown op={op!r}"

    result = execute_action(page, act, memory)
    low_res = (result or "").lower()
    if any(x in low_res for x in ("error", "not_found", "not found", "click_error", "no_action")):
        return False, f"{op} → {result}"
    return True, "ok"


def _run_loaded_plan_steps(page: Page, memory: AgentMemory, plan: Dict[str, Any]) -> Tuple[bool, str]:
    primary = (plan.get("primary_locator") or "").strip()
    steps = plan.get("steps") or []
    for idx, st in enumerate(steps):
        if not isinstance(st, dict):
            continue
        ok, msg = _apply_plan_step(page, memory, st, primary)
        if not ok:
            return False, f"План шаг {idx + 1}: {msg}"
        time.sleep(0.28)
    return True, "ok"


def run_retest_on_page(
    page: Page,
    memory: AgentMemory,
    description: str,
    fallback_start_url: str,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Выполнить ретест на уже открытой странице агента.

    Это путь для фонового QA-монитора: Playwright остаётся в main thread,
    а ретест использует тот же executor, память и обработчики консоли/сети.
    """
    plan = parse_retest_plan_from_description(description or "")
    use_plan = bool(plan and plan.get("steps"))
    console_start = len(console_log)
    network_start = len(network_failures)
    base_url_hint, scenario = _plan_scenario_text(description or "")

    if use_plan:
        base_url = (plan.get("start_url") or fallback_start_url or START_URL or "").strip()
        if not base_url:
            return {
                "ok": False,
                "message": "В плане ретеста нет start_url; задайте START_URL в .env или корректный первый navigate.",
                "scenario": scenario,
                "start_url": "",
                "console_log": [],
                "network_failures": [],
                "actions": [],
            }
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
            smart_wait_after_goto(page, timeout=5000)
            _inject_all(page)
            get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
        except Exception as exc:
            ok, msg = False, f"navigate {base_url[:120]}: {exc}"
        else:
            ok, msg = _run_loaded_plan_steps(page, memory, plan)
            if ok:
                ok, msg = _assert_retest_oracle(
                    plan,
                    memory,
                    console_log[console_start:],
                    network_failures[network_start:],
                )
        return {
            "ok": ok,
            "message": msg,
            "scenario": scenario,
            "start_url": base_url,
            "console_log": console_log[console_start:],
            "network_failures": network_failures[network_start:],
            "actions": list(getattr(memory, "actions", [])[-50:]),
        }

    canon_sel = extract_canonical_locator(description)
    start_url, steps = parse_reproduction_steps(description)
    base_url = (start_url or fallback_start_url or START_URL or "").strip()
    if not base_url:
        return {
            "ok": False,
            "message": "Нет стартового URL в описании и не задан START_URL.",
            "scenario": scenario,
            "start_url": "",
            "console_log": [],
            "network_failures": [],
            "actions": [],
        }
    if not base_url.startswith("http"):
        base_url = "https://" + base_url

    ok = True
    msg = "ok"
    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        smart_wait_after_goto(page, timeout=5000)
        _inject_all(page)
        get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
        for step in steps:
            sl = step.lower()
            if sl.startswith("открыть") and re.search(r"https?://", step):
                url_m = re.search(r"https?://[^\s\)\]]+", step)
                if url_m:
                    u = url_m.group(0).rstrip(".,;)")
                    page.goto(u, wait_until="domcontentloaded", timeout=25000)
                    smart_wait_after_goto(page, timeout=5000)
                    _inject_all(page)
                    get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
                continue
            act = _interpret_step_to_action(step, canon_sel)
            if not act:
                continue
            result = execute_action(page, act, memory)
            low_res = (result or "").lower()
            if any(x in low_res for x in ("error", "not_found", "not found", "click_error", "no_action")):
                ok, msg = False, f"Шаг «{step[:100]}» → {result}"
                break
            time.sleep(0.35)
    except Exception as exc:
        ok, msg = False, str(exc)

    return {
        "ok": ok,
        "message": msg,
        "scenario": scenario,
        "start_url": base_url,
        "console_log": console_log[console_start:],
        "network_failures": network_failures[network_start:],
        "actions": list(getattr(memory, "actions", [])[-50:]),
    }


def run_retest_playwright(description: str, fallback_start_url: str) -> Tuple[bool, str]:
    """
    Открыть браузер и выполнить шаги из описания. Возвращает (успех, сообщение).

    Приоритет: JSON *KVENTIN_RETEST_JSON_V1* из defect_builder; иначе эвристика по тексту шагов.
    """
    plan = parse_retest_plan_from_description(description or "")
    use_plan = bool(plan and plan.get("steps"))

    memory = AgentMemory()
    agent_mod._current_agent_memory = memory
    console_log: List[Dict[str, Any]] = []
    network_failures: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        context, page, browser = _retest_launch_browser(p)
        _install_retest_observers(page, console_log, network_failures)
        try:
            if AUTH_URL and AUTH_USERNAME and AUTH_PASSWORD:
                _do_auth_login(page, AUTH_URL, AUTH_USERNAME, AUTH_PASSWORD, AUTH_SUBMIT_SELECTOR)

            if use_plan:
                LOG.info("retest: режим JSON-плана (%d шагов)", len(plan.get("steps") or []))
                base_url = (plan.get("start_url") or fallback_start_url or START_URL or "").strip()
                if not base_url:
                    return False, (
                        "В плане ретеста нет start_url; задайте START_URL в .env или корректный первый navigate."
                    )
                if not base_url.startswith("http"):
                    base_url = "https://" + base_url
                page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                smart_wait_after_goto(page, timeout=5000)
                _inject_all(page)
                get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
                ok, msg = _run_loaded_plan_steps(page, memory, plan)
                if not ok:
                    return False, msg
                return _assert_retest_oracle(plan, memory, console_log, network_failures)

            # --- эвристический путь (старые тикеты без JSON) ---
            LOG.info("retest: режим текстовых шагов (без KVENTIN_RETEST_JSON_V1)")
            canon_sel = extract_canonical_locator(description)
            start_url, steps = parse_reproduction_steps(description)
            base_url = (start_url or fallback_start_url or START_URL or "").strip()
            if not base_url:
                return False, "Нет стартового URL в описании и не задан START_URL."
            if not base_url.startswith("http"):
                base_url = "https://" + base_url

            page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
            smart_wait_after_goto(page, timeout=5000)
            _inject_all(page)
            get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)

            for step in steps:
                sl = step.lower()
                if sl.startswith("открыть") and re.search(r"https?://", step):
                    url_m = re.search(r"https?://[^\s\)\]]+", step)
                    if url_m:
                        u = url_m.group(0).rstrip(".,;)")
                        page.goto(u, wait_until="domcontentloaded", timeout=25000)
                        smart_wait_after_goto(page, timeout=5000)
                        _inject_all(page)
                        get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
                    continue

                act = _interpret_step_to_action(step, canon_sel)
                if not act:
                    continue
                nav = act.pop("_navigate", None)
                if nav:
                    page.goto(nav, wait_until="domcontentloaded", timeout=25000)
                    smart_wait_after_goto(page, timeout=5000)
                    _inject_all(page)
                    get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
                    continue
                if not act:
                    continue

                result = execute_action(page, act, memory)
                low_res = (result or "").lower()
                if any(x in low_res for x in ("error", "not_found", "not found", "click_error", "no_action")):
                    return False, f"Шаг «{step[:100]}» → {result}"
                time.sleep(0.35)

            return True, "ok"
        finally:
            try:
                context.close()
            except Exception:
                pass
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass


def process_retest_issue(key: str) -> bool:
    """
    Ретест одного дефекта Kventin по ключу.

    Поток: changelog → перевод в QA → воспроизведение шагов (Playwright) →
    Resolved/Fixed при успехе либо In Progress + назначение при провале.

    Parameters
    ----------
    key:
        Ключ задачи Jira (например ``KVEN-123``).

    Returns
    -------
    bool
        True, если задача была обработана (любой исход ретеста);
        False, если issue не удалось загрузить или не удалось перевести в QA.
    """
    code, full, raw_tail = get_issue_with_changelog(key)
    if code != 200 or not full:
        print(f"[retest] {key}: не удалось загрузить issue ({code}): {raw_tail[:200]}")
        return False

    fields = full.get("fields") or {}
    changelog = full.get("changelog")
    author = find_author_who_moved_to_status(changelog, JIRA_RETEST_STATUS_READY_FOR_QA)
    assign_back = author_to_assignee_value(author) or JIRA_RETEST_FALLBACK_ASSIGNEE

    if not start_qa_transition(key):
        add_issue_comment(
            key,
            "h3. Kventin retest\nНе удалось перевести задачу в QA — проверьте workflow и JIRA_RETEST_STATUS_QA.",
        )
        return False

    desc_text = extract_description_text(fields)
    ok, msg = run_retest_playwright(desc_text, START_URL)

    if ok:
        if resolve_issue_fixed(key):
            add_issue_comment(
                key,
                f"h3. Kventin retest — пройден\nАвтоматический ретест по шагам из описания выполнен успешно.\n{{quote}}{msg}{{quote}}",
            )
            print(f"[retest] {key}: Fixed (Resolved)")
        else:
            add_issue_comment(
                key,
                "h3. Kventin retest — пройден (статус)\nРетест ОК, но не удалось выставить Resolved/Fixed через API — переведите вручную.",
            )
            print(f"[retest] {key}: ретест OK, переход Resolved не удался")
    else:
        body_comment = (
            f"h3. Kventin retest — не пройден\n{{quote}}{msg}{{quote}}\n"
            f"Задача возвращена в работу (In Progress)."
        )
        if reopen_or_move_to_in_progress(key, assignee_value=assign_back):
            add_issue_comment(key, body_comment)
            print(f"[retest] {key}: In Progress, назначено на {assign_back or '—'}")
        else:
            add_issue_comment(key, body_comment + "\nНе удалось выполнить переход In Progress через API.")
            print(f"[retest] {key}: не удалось In Progress (см. комментарий)")

    return True


def process_retest_issue_on_current_page(
    key: str,
    page: Page,
    memory: AgentMemory,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
    *,
    fallback_start_url: str,
) -> bool:
    """
    Обработать дефект, который уже находится в QA, внутри основного цикла агента.

    Нормальное тестирование должно быть поставлено на паузу вызывающей стороной:
    эта функция синхронно выполняет шаги ретеста на текущем Playwright page.
    """
    code, full, raw_tail = get_issue_with_changelog(key)
    if code != 200 or not full:
        LOG.warning("retest monitor %s: issue load failed %s %s", key, code, raw_tail[:200])
        return False

    fields = full.get("fields") or {}
    changelog = full.get("changelog")
    author = find_author_who_moved_to_status(changelog, JIRA_RETEST_STATUS_QA)
    assign_back = author_to_assignee_value(author) or JIRA_RETEST_FALLBACK_ASSIGNEE
    desc_text = extract_description_text(fields)

    retest = run_retest_on_page(
        page,
        memory,
        desc_text,
        fallback_start_url or START_URL,
        console_log,
        network_failures,
    )
    ok = bool(retest.get("ok"))
    msg = str(retest.get("message") or "")

    if ok:
        if resolve_issue_fixed(key):
            add_issue_comment(
                key,
                (
                    "h3. Kventin retest — пройден\n"
                    "Автоматический ретест по шагам из описания выполнен успешно.\n"
                    f"{{quote}}{msg}{{quote}}\n\n"
                    f"h4. Сценарий\n{retest.get('scenario') or '* нет шагов в описании'}"
                ),
            )
            LOG.info("retest monitor %s: Closed/Fixed", key)
        else:
            add_issue_comment(
                key,
                "h3. Kventin retest — пройден (статус)\n"
                "Ретест ОК, но не удалось закрыть задачу через API — переведите вручную.",
            )
        return True

    comment = format_retest_failure_comment(
        key,
        msg,
        scenario=str(retest.get("scenario") or ""),
        start_url=str(retest.get("start_url") or ""),
        console_log=list(retest.get("console_log") or []),
        network_failures=list(retest.get("network_failures") or []),
        actions=list(retest.get("actions") or []),
    )
    if reopen_or_move_to_in_progress(key, assignee_value=assign_back):
        add_issue_comment(key, comment)
        LOG.info("retest monitor %s: In Progress, assignee=%s", key, assign_back or "—")
    else:
        add_issue_comment(key, comment + "\n\nНе удалось выполнить переход In Progress через API.")
    return True


def collect_retest_issue_keys(max_results: int = 0) -> List[str]:
    """
    Вернуть ключи дефектов Kventin в статусе Ready for QA (для очереди демона).

    max_results=0 — взять JIRA_RETEST_MAX_ISSUES (или 100, если не ограничено).
    """
    max_n = max_results or (JIRA_RETEST_MAX_ISSUES if JIRA_RETEST_MAX_ISSUES > 0 else 100)
    issue_summaries = search_kventin_issues_by_status(
        JIRA_RETEST_STATUS_READY_FOR_QA,
        max_results=max_n,
    )
    return [it.get("key") for it in issue_summaries if it.get("key")]


def collect_qa_retest_issue_keys(max_results: int = 0) -> List[str]:
    """
    Вернуть ключи дефектов Kventin, уже ожидающих ретеста в статусе QA.
    """
    max_n = max_results or (JIRA_RETEST_MAX_ISSUES if JIRA_RETEST_MAX_ISSUES > 0 else 100)
    issue_summaries = search_kventin_issues_by_status(
        JIRA_RETEST_STATUS_QA,
        max_results=max_n,
    )
    return [it.get("key") for it in issue_summaries if it.get("key")]


def run_kventin_defect_retests() -> int:
    """
    CLI: обработать тикеты Kventin в статусе Ready for QA (один прогон).

    Returns
    -------
    int
        0 если конфиг ок и цикл завершён; 2 при ошибке конфигурации/критичном сбое.
    """
    logging.basicConfig(level=logging.INFO, format="[retest] %(levelname)s %(message)s")

    if not is_jira_rest_configured():
        print(
            "[retest] Не заданы JIRA_URL, JIRA_API_TOKEN и JIRA_PROJECT_KEY "
            "(для Basic также нужен JIRA_USERNAME или JIRA_EMAIL)."
        )
        return 2

    keys = collect_retest_issue_keys()
    if not keys:
        print(f"[retest] Нет задач с лейблом kventin в статусе «{JIRA_RETEST_STATUS_READY_FOR_QA}».")
        return 0

    print(f"[retest] Найдено {len(keys)} задач(и) в «{JIRA_RETEST_STATUS_READY_FOR_QA}». Ретест…")

    processed = 0
    for key in keys:
        if process_retest_issue(key):
            processed += 1

    print(f"[retest] Готово, обработано задач: {processed}.")
    return 0
