"""
Ретест тикетов, заведённых агентом Kventin (лейбл kventin).

Поток:
1. JQL: статус «Ready for QA» (имя настраивается в .env).
2. Перевод в статус QA (JIRA_RETEST_STATUS_QA).
3. Воспроизведение шагов из описания тикета (Playwright).
4. Успех → Resolved + resolution Fixed.
5. Неуспех → In Progress + назначение на автора перевода в Ready for QA (из changelog).

Описание тикета ожидается в формате, который пишет defect_builder (wiki, блок h3. Шаги воспроизведения).
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

import src.agent as agent_mod
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
from src.agent import AgentMemory, _do_auth_login, _inject_all, execute_action, smart_wait_after_goto
from src.jira_client import (
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
from src.page_analyzer import get_dom_summary

LOG = logging.getLogger("kventin.retest")


def extract_canonical_locator(description: str) -> str:
    """Вытащить строку канонического локатора из wiki-описания (блок «Затронутый элемент»)."""
    if not description:
        return ""
    for line in description.splitlines():
        low = line.lower()
        if "локатор" in low and ":" in line:
            part = line.split(":", 1)[1].strip()
            part = re.sub(r"^\{\{\s*|\s*\}\}$", "", part).strip()
            return part[:600]
    return ""


def canonical_to_selector_hint(canon: str) -> str:
    """
    Превратить человекочитаемый canonical (getByTestId('x'), #id, …) в строку,
    которую поймёт _find_element (CSS / текст / ref).
    """
    c = (canon or "").strip()
    c = re.sub(r"^\{\{\s*|\s*\}\}$", "", c).strip()
    if not c:
        return ""
    m = re.match(r"getByTestId\(['\"]([^'\"]+)['\"]\)", c, re.I)
    if m:
        return f'[data-testid="{m.group(1)}"]'
    m = re.match(
        r"getByRole\(['\"]([^'\"]+)['\"]\s*,\s*\{\s*name:\s*['\"]([^'\"]+)['\"]",
        c,
        re.I,
    )
    if m:
        return m.group(2).strip()
    m = re.match(r"getByLabel\(['\"]([^'\"]+)['\"]\)", c, re.I)
    if m:
        return m.group(1).strip()
    m = re.match(r"getByPlaceholder\(['\"]([^'\"]+)['\"]\)", c, re.I)
    if m:
        return m.group(1).strip()
    m = re.match(r"getByText\(['\"]([^'\"]+)['\"]\)", c, re.I)
    if m:
        return m.group(1).strip()
    if c.startswith("#") or c.startswith("[") or c.startswith("."):
        return c
    return c


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


def run_retest_playwright(description: str, fallback_start_url: str) -> Tuple[bool, str]:
    """
    Открыть браузер и выполнить шаги из описания. Возвращает (успех, сообщение).
    """
    canon_raw = extract_canonical_locator(description)
    canon_sel = canonical_to_selector_hint(canon_raw)
    start_url, steps = parse_reproduction_steps(description)
    base_url = start_url or fallback_start_url or START_URL
    if not base_url.startswith("http"):
        base_url = "https://" + base_url

    memory = AgentMemory()
    agent_mod._current_agent_memory = memory

    with sync_playwright() as p:
        context, page, browser = _retest_launch_browser(p)
        try:
            if AUTH_URL and AUTH_USERNAME and AUTH_PASSWORD:
                _do_auth_login(page, AUTH_URL, AUTH_USERNAME, AUTH_PASSWORD, AUTH_SUBMIT_SELECTOR)

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


def run_kventin_defect_retests() -> int:
    """
    CLI: обработать тикеты Kventin в статусе Ready for QA.

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

    max_n = JIRA_RETEST_MAX_ISSUES if JIRA_RETEST_MAX_ISSUES > 0 else 100
    issue_summaries = search_kventin_issues_by_status(
        JIRA_RETEST_STATUS_READY_FOR_QA,
        max_results=max_n,
    )
    if not issue_summaries:
        print(f"[retest] Нет задач с лейблом kventin в статусе «{JIRA_RETEST_STATUS_READY_FOR_QA}».")
        return 0

    print(
        f"[retest] Найдено {len(issue_summaries)} задач(и) в «{JIRA_RETEST_STATUS_READY_FOR_QA}». "
        f"Ретест (макс. {max_n})…"
    )

    processed = 0
    for item in issue_summaries:
        key = item.get("key") or "?"
        code, full, raw_tail = get_issue_with_changelog(key)
        if code != 200 or not full:
            print(f"[retest] {key}: не удалось загрузить issue ({code}): {raw_tail[:200]}")
            continue

        fields = full.get("fields") or {}
        changelog = full.get("changelog")
        author = find_author_who_moved_to_status(changelog, JIRA_RETEST_STATUS_READY_FOR_QA)
        assign_back = author_to_assignee_value(author) or JIRA_RETEST_FALLBACK_ASSIGNEE

        if not start_qa_transition(key):
            add_issue_comment(
                key,
                "h3. Kventin retest\nНе удалось перевести задачу в QA — проверьте workflow и JIRA_RETEST_STATUS_QA.",
            )
            continue

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

        processed += 1

    print(f"[retest] Готово, обработано задач: {processed}.")
    return 0
