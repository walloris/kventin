"""Lifecycle orchestration for the autonomous browser-testing agent.

Playwright stays on the main thread. Deterministic observation, overlay scope,
preflight, execution, and rule signals keep working without the optional LLM;
isolated background pools handle model analysis, Jira delivery, and I/O.
"""
import json
import os
import queue
import re
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from typing import List, Dict, Any, Optional

from playwright.sync_api import sync_playwright, Page

from config import (
    START_URL,
    START_URL_TRY_REDIRECT_FALLBACKS,
    BROWSER_USER_DATA_DIR,
    VIEWPORT_WIDTH,
    VIEWPORT_HEIGHT,
    ENABLE_TEST_PLAN_START,
    ENABLE_SECOND_PASS_BUG,
    ACTION_RETRY_COUNT,
    SESSION_REPORT_EVERY_N,
    SESSION_REPORT_PATH,
    SESSION_REPORT_HTML_PATH,
    SESSION_REPORT_JSONL,
    SESSION_REPORT_SAVE_EVERY_N,
    SAVE_STEP_SCREENSHOTS_DIR,
    CRITICAL_FLOW_STEPS,
    MAX_STEPS,
    CONSOLE_LOG_LIMIT,
    NETWORK_LOG_LIMIT,
    PHASE_STEPS_TO_ADVANCE,
    LLM_RESPONSE_TIMEOUT_SEC,
    LLM_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS,
    LLM_CIRCUIT_BREAKER_COOLDOWN_SEC,
    ACTION_TIMEOUT_MS,
    MAX_NAVIGATION_DEPTH,
    AUTH_URL,
    AUTH_USERNAME,
    AUTH_PASSWORD,
    AUTH_SUBMIT_SELECTOR,
    SESSION_STATE_SAVE_PATH,
    SESSION_STATE_RESTORE_PATH,
    RECORD_VIDEO_DIR,
    SESSION_BASELINE_JSONL,
    JUNIT_REPORT_PATH,
    BROKEN_LINKS_CHECK_EVERY_N,
    ENABLE_CONSOLE_WARNINGS_IN_REPORT,
    ENABLE_MIXED_CONTENT_CHECK,
    ENABLE_WEBSOCKET_MONITOR,
    TEST_UPLOAD_FILE_PATH,
    ENABLE_SHADOW_DOM,
    BROWSER_ENGINE,
    BROWSER_AUTO_SELECT_CERT_PATTERNS,
    PLAYWRIGHT_EXPORT_PATH,
    ENABLE_API_INTERCEPT,
    API_LOG_MAX,
    ENABLE_DOM_DIFF_AFTER_ACTION,
    VISUAL_BASELINE_DIR,
    VISUAL_REGRESSION_THRESHOLD_PCT,
    TEST_SPEC_YAML_PATH,
    FLAKINESS_RERUN_COUNT,
    AGENT_MEMORY_PATH,
    AGENT_MEMORY_SAVE_EVERY_N,
    ENABLE_QA_RETEST_MONITOR,
    JIRA_RETEST_MONITOR_INTERVAL_SEC,
    JIRA_RETEST_STATUS_QA,
    AGENT_CONTINUOUS_RESTART,
    AGENT_RESTART_BASE_DELAY_SEC,
    AGENT_RESTART_MAX_DELAY_SEC,
)
from agent.llm.llm_client import (
    consult_agent_with_screenshot,
    get_structured_test_plan,
    get_test_plan_from_screenshot,
    ask_is_this_really_bug,
    init_llm_connection,
)
from agent.llm.llm_parser import parse_llm_action, validate_llm_action
from agent.actions.form_strategies import get_form_fill_strategy
from agent.checks.visual_diff import (
    compare_with_baseline,
    save_baseline,
    load_baseline,
)

import html as html_module
import logging

LOG = logging.getLogger("Agent")
if not LOG.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[Agent] %(levelname)s %(message)s"))
    LOG.addHandler(h)

# Фоновый пул для параллельных задач (LLM, Jira, a11y, perf).
# Playwright НЕ thread-safe → только main thread. Всё остальное — в пул.
# Реализация и сам инстанс пула живут в agent/bg_pool.py.
from agent.core.bg_pool import (
    bg_submit as _bg_submit,
    cancel_bg_tasks as _cancel_bg_tasks,
    shutdown_bg_pool as _shutdown_bg_pool,
)

from agent.defects.jira_client import reset_session_defects
from agent.browser.page_analyzer import (
    get_dom_summary,
    get_page_modules,
    get_page_resource_urls,
    detect_active_overlays,
)
from agent.actions.visible_actions import update_llm_overlay
from agent.actions.wait_utils import smart_wait_after_goto
from agent.browser.network_capture import NetworkCapture
from agent.browser.browser_options import (
    build_browser_launch_options,
    build_client_certificates,
    build_start_url_candidates,
    is_too_many_redirects_error,
    should_write_auto_select_cert_policy,
)
from agent.actions.action_policy import action_from_llm_candidate_choice
from agent.actions.action_result import action_failed
from agent.actions.action_retry import prepare_action_retry
from agent.actions.action_selection import apply_preflight_or_fallback
from agent.core.observation import collect_page_observation
from agent.browser.page_objects import capture_page_state
from agent.core.local_policy import choose_local_action
from agent.core.reporting import (
    _build_html_report,
    _collect_browser_metrics,
    _write_junit_report,
)
from agent.core.post_analysis import _flush_pending_analysis, _step_post_analysis
from agent.checks.scheduler import run_periodic_checks
from agent.checks.agent_checks import check_page_load_and_report as _check_page_load_and_report
from agent.defects.defect_pipeline import (
    check_broken_links_bg as _check_broken_links_bg,
    create_defect as _create_defect,
)

# Бюджет на URL — единый источник правды в config.py.
from config import URL_BUDGET_NO_PROGRESS  # noqa: E402,F401


# AgentMemory вынесен в core/agent_memory.py — здесь только реэкспорт
# для обратной совместимости (run_agent и кучу других мест ссылаются
# именно на agent.AgentMemory).
from agent.core.agent_memory import AgentMemory  # noqa: E402,F401
from agent.actions.browser_actions import (  # noqa: E402,F401
    _do_auth_login,
    _do_close_modal,
    _find_element,
    _inject_all,
    describe_element_for_report,
    execute_action,
    set_current_agent_memory,
    try_accept_cookie_banner,
)
from agent.browser.screenshot import take_screenshot_b64


def _run_test_spec_yaml(page: Page, memory: AgentMemory, spec_path: str) -> None:
    """Выполнить сценарии из YAML: navigate, click, type. Селектор — ref:N или текст."""
    if not spec_path or not os.path.isfile(spec_path):
        return
    try:
        import yaml
        with open(spec_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        LOG.warning("test_spec YAML: не удалось загрузить %s: %s", spec_path, e)
        return
    scenarios = data.get("scenarios") or data.get("steps") or []
    if isinstance(scenarios, dict):
        scenarios = [scenarios]

    def live_selector(raw_selector: str) -> str:
        locator = _find_element(page, raw_selector)
        if not locator:
            return ""
        try:
            ref = locator.evaluate(
                """(el) => {
                    window.__agentRefs = window.__agentRefs || {};
                    window.__agentRefMeta = window.__agentRefMeta || {};
                    let ref = el.getAttribute('data-agent-ref');
                    if (!ref) {
                        const ids = Object.keys(window.__agentRefs).map(Number).filter(Number.isFinite);
                        ref = String((ids.length ? Math.max(...ids) : 0) + 1);
                        el.setAttribute('data-agent-ref', ref);
                    }
                    window.__agentRefs[ref] = el;
                    return ref;
                }"""
            )
            return f"ref:{ref}" if ref else ""
        except Exception:
            return ""

    for scenario in scenarios:
        steps = scenario.get("steps") or scenario.get("step") or []
        if isinstance(steps, dict):
            steps = [steps]
        name = scenario.get("name", "")
        scenario_ok = True
        for idx, step in enumerate(steps):
            if isinstance(step, str):
                step = {"navigate": step}
            action = step.get("action") or ("navigate" if step.get("navigate") else "click")
            if action == "navigate" or step.get("navigate"):
                url = (step.get("url") or step.get("navigate") or "").strip()
                if url:
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        smart_wait_after_goto(page, timeout=5000)
                        get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
                    except Exception as e:
                        scenario_ok = False
                        LOG.warning("test_spec navigate %s: %s", url[:50], e)
            elif action == "click":
                sel = (step.get("selector") or step.get("element") or "").strip()
                if sel:
                    resolved = live_selector(sel)
                    result = execute_action(
                        page,
                        {"action": "click", "selector": resolved, "reason": "test_spec"},
                        memory,
                    ) if resolved else "preflight_rejected:not_found"
                    if action_failed(result):
                        scenario_ok = False
                        LOG.warning("test_spec click %s: %s", sel[:30], result[:200])
                    time.sleep(0.5)
            elif action == "type":
                sel = (step.get("selector") or step.get("element") or "").strip()
                val = (step.get("value") or step.get("text") or "").strip()
                if sel and val is not None:
                    resolved = live_selector(sel)
                    result = execute_action(
                        page,
                        {
                            "action": "type",
                            "selector": resolved,
                            "value": val,
                            "reason": "test_spec",
                        },
                        memory,
                    ) if resolved else "preflight_rejected:not_found"
                    if action_failed(result):
                        scenario_ok = False
                        LOG.warning("test_spec type %s: %s", sel[:30], result[:200])
                    time.sleep(0.3)
        if name:
            status = "выполнен" if scenario_ok else "завершён с ошибкой"
            print(f"[Agent] Test spec сценарий {status}: {name[:50]}")


# --- Инициализация страницы ---


def _same_page(start_url: str, current_url: str) -> bool:
    """Сравнить только домен/протокол, чтобы не блокировать навигацию внутри сайта."""
    try:
        from urllib.parse import urlparse
        s = urlparse(start_url or "")
        c = urlparse(current_url or "")
        return (s.scheme, s.netloc) == (c.scheme, c.netloc)
    except Exception:
        return True


# --- Обработка новых вкладок ---
def _handle_new_tabs(
    new_tabs_queue: List[Any],
    main_page: Page,
    start_url: str,
    step: int,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
    memory: AgentMemory,
):
    """
    Обработать все новые вкладки из очереди:
    - Дождаться загрузки (domcontentloaded, таймаут 15с)
    - Зафиксировать состояние вкладки в долговременной памяти
    - Закрыть вкладку и вернуться в рабочую вкладку
    """
    while new_tabs_queue:
        new_tab = new_tabs_queue.pop(0)
        tab_url = "(пустая)"
        load_ok = False

        try:
            # Ждём, пока вкладка начнёт загружаться
            new_tab.wait_for_load_state("domcontentloaded", timeout=15000)
            tab_url = new_tab.url or "(пустая)"
            print(f"[Agent] #{step} Новая вкладка загрузилась: {tab_url[:80]}")

            # Проверяем, что страница не пустая/ошибочная
            title = ""
            try:
                title = new_tab.title() or ""
            except Exception:
                pass

            # Попробуем дождаться networkidle (но не больше 5 сек)
            try:
                new_tab.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            # Скриншот новой вкладки для лога
            try:
                _inject_all(new_tab)
                time.sleep(0.5)
                screenshot_b64 = take_screenshot_b64(new_tab)
            except Exception:
                screenshot_b64 = None

            # Проверяем на ошибки: пустая страница, about:blank, chrome-error://
            is_error_page = (
                not tab_url
                or tab_url in ("about:blank", "about:blank#blocked")
                or "chrome-error://" in tab_url
                or "err_" in tab_url.lower()
            )

            # Проверяем: есть ли ошибки JS в новой вкладке
            tab_errors = []
            try:
                tab_errors_raw = new_tab.evaluate("""
                    () => {
                        const errs = [];
                        if (window.__pageErrors) errs.push(...window.__pageErrors);
                        return errs.map(e => String(e)).slice(0, 5);
                    }
                """)
                if tab_errors_raw:
                    tab_errors = tab_errors_raw
            except Exception:
                pass

            # Проверяем HTTP-статус (если страница отдала ошибку)
            is_http_error = False
            try:
                body_text = new_tab.text_content("body") or ""
                for err_pattern in ["404", "500", "502", "503", "This page isn", "не найдена", "Server Error", "Bad Gateway"]:
                    if err_pattern.lower() in body_text[:500].lower() and len(body_text.strip()) < 2000:
                        is_http_error = True
                        break
            except Exception:
                pass

            if is_error_page or is_http_error:
                detail = f"title={title[:80]} errors={', '.join(tab_errors[:3])}"
                print(f"[Agent] #{step} Новая вкладка открылась с ошибочным состоянием: {tab_url[:60]} → закрываю")
                memory.record_external_tab(tab_url, "opened_error_state", title=title, detail=detail)
                memory.add_action({"action": "new_tab_checked", "selector": tab_url}, result=f"opened_error_state: {detail[:80]}")
            else:
                # Загрузка успешна
                load_ok = True
                print(f"[Agent] #{step} Новая вкладка OK: {tab_url[:60]} → закрываю")
                memory.record_external_tab(tab_url, "opened_ok", title=title)
                memory.add_action({"action": "new_tab_ok", "selector": tab_url}, result=f"tab_loaded: {title[:40]}")

        except Exception as e:
            # Таймаут загрузки или краш: для внешней вкладки достаточно запомнить и вернуться.
            try:
                tab_url = new_tab.url or tab_url
            except Exception:
                pass
            print(f"[Agent] #{step} Новая вкладка: таймаут/ошибка. URL: {tab_url[:60]} → закрываю")
            memory.record_external_tab(tab_url, "opened_timeout_or_error", detail=str(e)[:200])
            memory.add_action({"action": "new_tab_timeout", "selector": tab_url}, result=f"error: {str(e)[:60]}")

        finally:
            # Всегда закрываем новую вкладку
            try:
                if not new_tab.is_closed():
                    new_tab.close()
                    print(f"[Agent] #{step} Вкладка закрыта: {tab_url[:60]}")
            except Exception as close_err:
                print(f"[Agent] #{step} Ошибка закрытия вкладки: {close_err}")

    # Убедиться, что фокус на основной вкладке
    try:
        main_page.bring_to_front()
    except Exception:
        pass


def _is_too_many_redirects_error(exc: Exception) -> bool:
    return is_too_many_redirects_error(exc)


def _build_start_url_candidates(primary: str) -> List[str]:
    return build_start_url_candidates(primary)


def _print_redirect_loop_hints() -> None:
    print(
        "[Agent] Подсказка по петле редиректов (ERR_TOO_MANY_REDIRECTS):\n"
        "  — Часто на корп. порталах: нет сессии → SSO крутит A→B→A, или /platform/ "
        "не для «холодного» браузера.\n"
        "  — Попробуй: залогиниться вручную в Chrome с BROWSER_USER_DATA_DIR, "
        "или AUTH_URL + креды, или SESSION_STATE_RESTORE_PATH с валидными cookies,\n"
        "  или задай другой START_URL / START_URL_FALLBACKS= (корень сайта, /login, и т.д.)."
    )


def _goto_start_page_with_redirect_fallbacks(page: Page, start_url: str) -> str:
    """
    Первый удачный page.goto. При только ERR_TOO_MANY_REDIRECTS перебирает кандидатов.
    Возвращает URL, на котором сработал goto (строка кандидата, не page.url).
    """
    candidates = _build_start_url_candidates(start_url)
    last_exc: Optional[Exception] = None
    for u in candidates:
        try:
            page.goto(u, wait_until="domcontentloaded", timeout=30000)
            if u != start_url:
                print(f"[Agent] Старт с альтернативного URL (из-за редиректов): {u}")
            return u
        except Exception as e:
            last_exc = e
            if not _is_too_many_redirects_error(e):
                raise
            LOG.info("goto %r: петля редиректов, следующий вариант…", u[:120])
    if last_exc:
        _print_redirect_loop_hints()
        raise last_exc
    raise RuntimeError("no candidates for start URL")


# --- Основной цикл ---
def _run_agent_session(
    start_url: str = None,
    *,
    max_steps: Optional[int] = None,
    enable_qa_retests: bool = True,
):
    """
    Запуск умного агента. Многофазный цикл:
    Phase 1: Скриншот + контекст → LLM (что делать?)
    Phase 2: Выполнение действия
    Phase 3: Скриншот после действия → LLM (анализ)
    Phase 4: Если дефект → Jira с фактурой
    """
    start_url = start_url or START_URL
    session_max_steps = MAX_STEPS if max_steps is None else max(0, int(max_steps))
    if not start_url.startswith("http"):
        start_url = "https://" + start_url

    console_log: List[Dict[str, Any]] = []
    network_failures: List[Dict[str, Any]] = []
    memory = AgentMemory()
    if AGENT_MEMORY_PATH and memory.load_from_file(AGENT_MEMORY_PATH):
        print(f"[Agent] Восстановлена долговременная память: {AGENT_MEMORY_PATH}")
        # ``iteration`` is a session counter. Persisted coverage/history should
        # not make a bounded MAX_STEPS run finish before its first action.
        memory.iteration = 0
    initial_defect_count = len(memory.defects_created)
    reset_session_defects()  # сбросить локальный кеш дефектов

    # LLM enriches decisions but is not a single point of failure. The local
    # deterministic policy keeps testing while the client circuit breaker waits
    # for the endpoint to recover.
    llm_ready = init_llm_connection()
    if llm_ready:
        print("[Agent] Локальная LLM готова. Запуск браузера…")
    else:
        print("[Agent] LLM временно недоступна. Запускаю браузер в локальном режиме; подключение восстановится автоматически.")
    session_result = {"defects": 0, "steps": 0, "error": None, "termination": ""}

    with sync_playwright() as p:
        browser = None
        engine = getattr(p, BROWSER_ENGINE, p.chromium)
        launch_kw = build_browser_launch_options(engine_name=BROWSER_ENGINE)
        client_certs = build_client_certificates()
        # Политика авто-выбора сертификата по URL (без файла сертификата): пишем в профиль при persistent context.
        if should_write_auto_select_cert_policy(BROWSER_ENGINE):
            try:
                policy_entries = [json.dumps({"pattern": p, "filter": {}}) for p in BROWSER_AUTO_SELECT_CERT_PATTERNS]
                policy_json = json.dumps({"AutoSelectCertificateForUrls": policy_entries})
                policy_dir = os.path.join(BROWSER_USER_DATA_DIR, "Default", "Managed Preferences")
                os.makedirs(policy_dir, exist_ok=True)
                policy_file = os.path.join(policy_dir, "auto_select_certificate_for_urls.json")
                with open(policy_file, "w", encoding="utf-8") as f:
                    f.write(policy_json)
                LOG.info("Политика авто-выбора сертификата записана в %s", policy_file)
            except Exception as e:
                LOG.debug("Не удалось записать политику сертификата: %s", e)
        ctx_common = {
            "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            "ignore_https_errors": True,
        }
        if client_certs:
            ctx_common["client_certificates"] = client_certs

        if BROWSER_USER_DATA_DIR:
            # Профиль на диске — поддерживается только Chromium
            context = p.chromium.launch_persistent_context(
                BROWSER_USER_DATA_DIR,
                **ctx_common,
                **launch_kw,
            )
        else:
            browser = engine.launch(**launch_kw)
            ctx_opts = dict(ctx_common)
            if RECORD_VIDEO_DIR:
                os.makedirs(RECORD_VIDEO_DIR, exist_ok=True)
                ctx_opts["record_video_dir"] = RECORD_VIDEO_DIR
            context = browser.new_context(**ctx_opts)
        page = context.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)

        # Восстановление состояния (cookies) из предыдущей сессии
        if SESSION_STATE_RESTORE_PATH and os.path.isfile(SESSION_STATE_RESTORE_PATH):
            try:
                with open(SESSION_STATE_RESTORE_PATH, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                if isinstance(cookies, list) and cookies:
                    context.add_cookies(cookies)
                    print(f"[Agent] Восстановлено {len(cookies)} cookies из {SESSION_STATE_RESTORE_PATH}")
            except Exception as e:
                LOG.debug("Восстановление состояния: %s", e)

        # Параметры в localStorage на каждой загружаемой странице
        context.add_init_script("""
            localStorage.setItem('onboarding_is_passed', 'true');
            localStorage.setItem('hrp-core-app/app-mode', '"neuro"');
        """)

        # --- Обработка новых вкладок (target="_blank" и т.п.) ---
        new_tabs_queue: List[Any] = []   # очередь вкладок для обработки

        def _on_new_page(new_page):
            """Перехватываем открытие новой вкладки."""
            print(f"[Agent] Новая вкладка обнаружена")
            new_tabs_queue.append(new_page)

        context.on("page", _on_new_page)

        def on_console(msg):
            entry: Dict[str, Any] = {"type": msg.type, "text": msg.text}
            try:
                loc = msg.location or {}
                if isinstance(loc, dict):
                    url_l = loc.get("url") or ""
                    line_l = loc.get("lineNumber")
                    col_l = loc.get("columnNumber")
                    if url_l:
                        entry["source_url"] = url_l
                    if line_l is not None:
                        entry["line"] = line_l
                    if col_l is not None:
                        entry["column"] = col_l
            except Exception:
                pass
            # Для ошибок — попытаться вытащить стек из аргументов (Error.stack)
            if msg.type == "error":
                try:
                    stacks = []
                    for arg in (msg.args or [])[:3]:
                        try:
                            s = arg.evaluate("e => (e && typeof e === 'object' && e.stack) ? String(e.stack) : ''")
                            if s:
                                stacks.append(s)
                        except Exception:
                            pass
                    if stacks:
                        entry["stack"] = "\n".join(stacks)[:4000]
                except Exception:
                    pass
            console_log.append(entry)
        page.on("console", on_console)

        def on_page_error(err):
            """Необработанные JS-исключения: всегда содержат полный стек-трейс."""
            try:
                name = getattr(err, "name", None) or "Error"
                message = getattr(err, "message", None) or str(err)
                stack = getattr(err, "stack", None) or ""
            except Exception:
                name, message, stack = "Error", str(err), ""
            entry = {
                "type": "pageerror",
                "text": f"{name}: {message}"[:2000],
                "stack": str(stack)[:4000],
                "name": name,
            }
            # Попробуем извлечь путь к JS-файлу из первой строки стека
            try:
                for line in str(stack).splitlines():
                    line = line.strip()
                    m = re.search(r"(https?://\S+?\.js(?:\?\S*)?):(\d+):(\d+)", line)
                    if m:
                        entry["source_url"] = m.group(1)
                        entry["line"] = int(m.group(2))
                        entry["column"] = int(m.group(3))
                        break
            except Exception:
                pass
            console_log.append(entry)
        page.on("pageerror", on_page_error)

        page._agent_console_log = console_log

        def on_response(response):
            if not response.ok and response.url:
                try:
                    network_failures.append({
                        "url": response.url,
                        "status": response.status,
                        "method": response.request.method,
                    })
                except Exception:
                    pass
            if ENABLE_MIXED_CONTENT_CHECK and response.url and page.url.startswith("https://") and response.url.startswith("http://"):
                try:
                    memory._mixed_content.append({"url": response.url[:300], "page": page.url[:200]})
                except Exception:
                    pass
            if ENABLE_API_INTERCEPT and response.request.resource_type in ("xhr", "fetch"):
                try:
                    req = response.request
                    entry = {
                        "method": req.method,
                        "url": (req.url or "")[:500],
                        "status": response.status,
                        "ok": response.ok,
                    }
                    memory._api_log.append(entry)
                    if len(memory._api_log) > API_LOG_MAX:
                        memory._api_log.pop(0)
                except Exception:
                    pass
        page.on("response", on_response)
        page._agent_network_failures = network_failures

        # HAR: захватываем все запросы (request+response+timing+headers/sizes),
        # чтобы прикреплять к дефекту окно по времени или последние N запросов.
        net_capture = NetworkCapture()
        net_capture.attach(page)
        page._agent_net_capture = net_capture
        memory.net_capture = net_capture

        # On-load диагностика для каждой смены URL:
        # помечаем флаг при framenavigated → проверяем в начале следующего шага.
        memory._pending_page_load_check_url = ""

        def _on_main_frame_navigated(frame):
            try:
                if frame == page.main_frame:
                    new_url = frame.url or ""
                    if new_url:
                        memory._pending_page_load_check_url = new_url
            except Exception:
                pass

        try:
            page.on("framenavigated", _on_main_frame_navigated)
        except Exception:
            LOG.debug("framenavigated подписка не установлена", exc_info=True)

        if ENABLE_WEBSOCKET_MONITOR:
            def on_websocket(ws):
                url_ws = ws.url or ""
                def on_close():
                    try:
                        memory._websocket_issues.append({"url": url_ws[:200], "event": "close"})
                    except Exception:
                        pass
                def on_error(err):
                    try:
                        memory._websocket_issues.append({"url": url_ws[:200], "event": "error", "error": str(err)[:150]})
                    except Exception:
                        pass
                try:
                    ws.on("close", on_close)
                    ws.on("socketerror", on_error)
                except Exception:
                    pass
            page.on("websocket", on_websocket)

        # Автологин перед стартом (если задан AUTH_URL)
        if AUTH_URL and AUTH_USERNAME and AUTH_PASSWORD:
            _do_auth_login(page, AUTH_URL, AUTH_USERNAME, AUTH_PASSWORD, AUTH_SUBMIT_SELECTOR)
            time.sleep(1)

        # Загрузка начальной страницы
        # По умолчанию — как раньше: один page.goto(START_URL). Многошаговый обход
        # петли редиректов — только при START_URL_TRY_REDIRECT_FALLBACKS=1.
        try:
            if START_URL_TRY_REDIRECT_FALLBACKS:
                effective_start = _goto_start_page_with_redirect_fallbacks(page, start_url)
                if effective_start != start_url:
                    start_url = effective_start
            else:
                page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            smart_wait_after_goto(page, timeout=15000)
            _inject_all(page)
            try:
                collect_page_observation(
                    page,
                    memory,
                    console_log,
                    network_failures,
                    [],
                    screenshot_func=lambda _page: None,
                    include_shadow_dom=ENABLE_SHADOW_DOM,
                    dom_max=5000,
                    history_n=0,
                    max_candidates=80,
                )
                print(f"[Agent] PageObject создан/обновлен для {page.url[:100]}")
            except Exception:
                LOG.debug("initial page object update failed", exc_info=True)
        except Exception as e:
            print(f"[Agent] Ошибка загрузки {start_url}: {e}")
            if browser:
                browser.close()
            else:
                context.close()
            session_result["error"] = str(e)[:500]
            session_result["termination"] = "startup_error"
            return session_result

        memory.session_start = datetime.now()
        memory.set_start_url_for_nav(start_url)
        # Закрыть баннер cookies/согласия, если есть
        if try_accept_cookie_banner(page):
            time.sleep(1.5)
            smart_wait_after_goto(page, timeout=3000)

        # On-load диагностика: ошибки в консоли/сети при ОТКРЫТИИ страницы
        # заводятся отдельным дефектом (kind=page_load) — без привязки к действию.
        try:
            _check_page_load_and_report(
                page, memory, page.url or start_url, console_log, network_failures,
            )
        except Exception:
            LOG.exception("check_page_load_and_report (start): ошибка")

        # Спецификация теста (YAML): выполнить сценарии до автономного прохода
        if TEST_SPEC_YAML_PATH:
            get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
            _run_test_spec_yaml(page, memory, TEST_SPEC_YAML_PATH)
            time.sleep(1)

        # Тест-план в начале сессии: пытаемся получить структурированный план;
        # если LLM вернул кривой JSON — фоллбек уже встроен (плоский → обёрнут в dict).
        if ENABLE_TEST_PLAN_START:
            plan_screenshot = take_screenshot_b64(page)
            try:
                plan_modules = get_page_modules(page)
            except Exception:
                plan_modules = []
            structured_plan = get_structured_test_plan(
                plan_screenshot,
                start_url,
                modules=plan_modules,
            )
            if structured_plan:
                memory.set_structured_test_plan(structured_plan)
                titles = [it.get("title", "") for it in structured_plan]
                print(
                    f"[Agent] Тест-план структурированный ({len(structured_plan)} пунктов): "
                    + "; ".join(t[:50] for t in titles[:3]) + "…"
                )
                update_llm_overlay(page, prompt="Тест-план", response="; ".join(titles[:4]), loading=False)
            else:
                test_plan_steps = get_test_plan_from_screenshot(plan_screenshot, start_url)
                if test_plan_steps:
                    memory.set_test_plan(test_plan_steps)
                    memory.set_test_plan_tracking()
                    print(f"[Agent] Тест-план ({len(test_plan_steps)} шагов): " + "; ".join(test_plan_steps[:3]) + "…")
                    update_llm_overlay(page, prompt="Тест-план", response="; ".join(test_plan_steps[:4]), loading=False)

        # Абсолютные пути к отчётам (чтобы знать, куда они пишутся)
        _report_abs_path = os.path.abspath(SESSION_REPORT_PATH) if SESSION_REPORT_PATH else ""
        _report_html_abs_path = os.path.abspath(SESSION_REPORT_HTML_PATH) if SESSION_REPORT_HTML_PATH else ""
        _report_first_save_done = False

        def _save_report_now(step_: int, label: str = "") -> None:
            """Сохранить HTML и текстовый отчёт на диск (вызывается из разных мест цикла)."""
            nonlocal _report_first_save_done
            try:
                if not page.is_closed():
                    _collect_browser_metrics(page, memory, step_)
                report = memory.get_session_report_text()
                if SESSION_REPORT_PATH:
                    with open(_report_abs_path, "w", encoding="utf-8") as f:
                        f.write(report)
                        f.flush()
                        os.fsync(f.fileno())
                if SESSION_REPORT_HTML_PATH:
                    html_content = _build_html_report(memory, report, start_url or "", video_dir=RECORD_VIDEO_DIR or "")
                    with open(_report_html_abs_path, "w", encoding="utf-8") as f:
                        f.write(html_content)
                        f.flush()
                        os.fsync(f.fileno())
                if not _report_first_save_done:
                    _report_first_save_done = True
                    if _report_html_abs_path:
                        print(f"[Agent] HTML-отчёт: {_report_html_abs_path}")
                    if _report_abs_path:
                        print(f"[Agent] Текстовый отчёт: {_report_abs_path}")
            except TypeError as e:
                if "unhashable" in str(e):
                    import traceback
                    print(f"[Agent] Ошибка сохранения отчёта ({label}): unhashable type — возможно dict в set. Попытка упрощённого отчёта.")
                    traceback.print_exc()
                    try:
                        _report_fallback = f"Шаг {step_}\nВремя: {getattr(memory, 'session_start', '')}\nОшибка: {e}"
                        if SESSION_REPORT_PATH:
                            with open(_report_abs_path, "w", encoding="utf-8") as f:
                                f.write(_report_fallback)
                        if SESSION_REPORT_HTML_PATH:
                            with open(_report_html_abs_path, "w", encoding="utf-8") as f:
                                f.write(f"<html><body><pre>{html_module.escape(_report_fallback)}</pre></body></html>")
                    except Exception:
                        pass
                else:
                    raise
            except Exception as e:
                import traceback
                print(f"[Agent] Ошибка сохранения отчёта ({label}): {e}")
                traceback.print_exc()

        print(f"[Agent] Старт тестирования: {start_url}")
        if SESSION_REPORT_HTML_PATH:
            print(f"[Agent] Отчёт будет обновляться в: {_report_html_abs_path}")
        if session_max_steps > 0:
            print(f"[Agent] Лимит: {session_max_steps} шагов.")
        else:
            print(f"[Agent] Бесконечный цикл. Ctrl+C для остановки.")

        retest_queue: "queue.Queue[str]" = queue.Queue()
        retest_stop_event = threading.Event()
        retest_seen_keys: set[str] = set()
        retest_thread: Optional[threading.Thread] = None

        def _qa_retest_monitor_loop() -> None:
            """Фоново ищет kventin-дефекты в QA. Playwright здесь не трогаем."""
            from agent.defects.defect_retest import collect_qa_retest_issue_keys
            from agent.defects.jira_client import is_jira_rest_configured

            if not is_jira_rest_configured():
                LOG.info("QA retest monitor disabled: Jira REST is not configured")
                return
            interval = max(60, int(JIRA_RETEST_MONITOR_INTERVAL_SEC or 2400))
            while not retest_stop_event.is_set():
                try:
                    keys = collect_qa_retest_issue_keys()
                    for key in keys:
                        if key and key not in retest_seen_keys:
                            retest_seen_keys.add(key)
                            retest_queue.put(key)
                            print(f"[Agent] Ретест: найден дефект в {JIRA_RETEST_STATUS_QA}: {key}")
                except Exception:
                    LOG.exception("QA retest monitor: ошибка поиска дефектов")
                retest_stop_event.wait(interval)

        def _process_qa_retests(step_: int) -> bool:
            """Синхронно обработать очередь ретестов в main thread. Возвращает True, если был ретест."""
            nonlocal _llm_future, _llm_action
            processed_any = False
            while True:
                try:
                    key = retest_queue.get_nowait()
                except queue.Empty:
                    return processed_any
                processed_any = True
                print(f"[Agent] #{step_} Пауза тестирования: ретест дефекта {key}")
                try:
                    if _llm_future is not None:
                        try:
                            _llm_future.cancel()
                        except Exception:
                            pass
                        _llm_future = None
                        _llm_action = None
                    from agent.defects.defect_retest import process_retest_issue_on_current_page

                    process_retest_issue_on_current_page(
                        key,
                        page,
                        memory,
                        console_log,
                        network_failures,
                        fallback_start_url=start_url,
                    )
                    if AGENT_MEMORY_PATH:
                        memory.save_to_file(AGENT_MEMORY_PATH)
                    if not page.is_closed():
                        try:
                            page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                            smart_wait_after_goto(page, timeout=5000)
                            _inject_all(page)
                            memory.set_current_url_pattern(start_url)
                        except Exception:
                            LOG.exception("QA retest: не удалось вернуть рабочую страницу")
                except Exception:
                    LOG.exception("QA retest %s: ошибка обработки", key)
                finally:
                    retest_seen_keys.discard(key)
                    retest_queue.task_done()

        if ENABLE_QA_RETEST_MONITOR and enable_qa_retests:
            retest_thread = threading.Thread(
                target=_qa_retest_monitor_loop,
                name="kventin-qa-retest-monitor",
                daemon=True,
            )
            retest_thread.start()
            print(
                f"[Agent] QA-ретест монитор включён: статус '{JIRA_RETEST_STATUS_QA}', "
                f"интервал {max(60, int(JIRA_RETEST_MONITOR_INTERVAL_SEC or 2400))} сек."
            )

        # ========== PIPELINE: Асинхронная LLM + мгновенные действия ==========
        # LLM работает в фоне. Пока ждём ответ — агент кликает по ref-id.
        # Когда LLM отвечает — берём её действие следующим.
        _llm_future: Optional[Future] = None
        _llm_future_started_at: float = 0.0
        _llm_action: Optional[Dict[str, Any]] = None
        _llm_meta: Dict[str, Any] = {}  # has_overlay, screenshot_b64
        _llm_circuit_open_until: float = 0.0  # Circuit breaker: не вызывать LLM до этого времени
        _llm_consecutive_timeouts: int = 0

        def _start_llm_async(page_, step_, memory_, console_log_, network_failures_, checklist_results_, context_):
            """Запустить LLM в фоновом потоке. Возвращает Future."""
            nonlocal _llm_future
            # Проверка: страница закрыта — не запускаем LLM
            if page_.is_closed():
                return
            
            # Собираем всё что нужно ДО отправки в фон (Playwright — только main thread)
            dom_max = 5000
            history_n = 15
            
            try:
                observation = collect_page_observation(
                    page_,
                    memory_,
                    console_log_,
                    network_failures_,
                    checklist_results_,
                    screenshot_func=take_screenshot_b64,
                    include_shadow_dom=ENABLE_SHADOW_DOM,
                    dom_max=dom_max,
                    history_n=history_n,
                    max_candidates=60,
                )
            except Exception as e:
                # Страница закрылась во время сбора данных
                LOG.debug("_start_llm_async: страница закрыта во время сбора данных: %s", e)
                return
            coverage_hint = ""
            if observation.current_url in memory_._page_coverage:
                tested_count = len(memory_._page_coverage[observation.current_url])
                if tested_count > 0:
                    coverage_hint = f"\nПротестировано: {tested_count}. Выбери НОВЫЙ элемент.\n"

            _llm_meta["has_overlay"] = observation.has_overlay
            _llm_meta["screenshot_b64"] = observation.screenshot_b64
            _llm_meta["ref_meta"] = observation.ref_meta
            _llm_meta["candidates"] = observation.candidates

            # Формируем контекст и вопрос
            ctx = observation.context

            type_strategies = {
                "landing": "Landing page: CTA, формы", "form": "Form: заполни поля",
                "dashboard": "Dashboard: таблицы, фильтры", "catalog": "Catalog: карточки, фильтры",
            }
            ptype_hint = f"\nТип: {observation.page_type}. {type_strategies.get(observation.page_type, '')}\n" if observation.page_type != "unknown" else ""

            module_ctx = memory_.get_module_context_text()

            if observation.has_overlay:
                question = f"""Скриншот. АКТИВНЫЙ ОВЕРЛЕЙ.
{observation.overlay_context}
{module_ctx}
КАНДИДАТЫ ДЕЙСТВИЙ (выбери candidate_id из списка):
{observation.candidates_prompt}

ЭЛЕМЕНТЫ: {observation.dom_summary[:1800]}
{observation.history_text}
Верни JSON: {{"candidate_id":"cN","reason":"почему"}}. Не придумывай selector."""
            else:
                plan_hint = ""
                if memory_.test_plan or getattr(memory_, "structured_test_plan", None):
                    next_focus = memory_.get_next_plan_focus()
                    if next_focus:
                        prio = next_focus.get("priority", "")
                        title = next_focus.get("title", "")[:120]
                        intent = next_focus.get("intent", "")[:140]
                        expected = next_focus.get("expected", "")[:140]
                        module = next_focus.get("module") or next_focus.get("area") or ""
                        mod_part = f" (модуль: {module})" if module else ""
                        plan_hint = (
                            f"СЛЕДУЮЩИЙ ПУНКТ ПЛАНА [{prio}]{mod_part}: {title}\n"
                        )
                        if intent:
                            plan_hint += f"  Зачем: {intent}\n"
                        if expected:
                            plan_hint += f"  Ожидаемый результат: {expected}\n"
                    else:
                        plan_hint = memory_.get_test_plan_progress() + "\n"
                critical_hint = ""
                if CRITICAL_FLOW_STEPS:
                    critical_hint = f"\nКритический сценарий (сделай в первую очередь): {', '.join(CRITICAL_FLOW_STEPS[:5])}.\n"
                stuck_w = "\n🚨 ЗАЦИКЛИВАНИЕ! Выбери НОВЫЙ элемент!\n" if memory_.is_stuck() else ""
                question = f"""Скриншот и контекст.
{module_ctx}
{ptype_hint}{coverage_hint}{critical_hint}
КАНДИДАТЫ ДЕЙСТВИЙ (выбери candidate_id из списка):
{observation.candidates_prompt}

ЭЛЕМЕНТЫ СТРАНИЦЫ (только видимые на экране, формат: [N] тип "текст" атрибуты):
{observation.dom_summary[:1800]}
{observation.history_text}
{plan_hint}{stuck_w}
Верни JSON: {{"candidate_id":"cN","reason":"почему"}}. Если кандидатов нет — верни action=scroll."""

            phase_instruction = memory_.get_phase_instruction()
            send_screenshot = observation.screenshot_b64 if observation.screenshot_changed else None

            def _call_llm():
                raw = consult_agent_with_screenshot(
                    ctx, question, screenshot_b64=send_screenshot,
                    phase_instruction=phase_instruction, tester_phase=memory_.tester_phase,
                    has_overlay=observation.has_overlay,
                )
                if raw:
                    candidate_action = action_from_llm_candidate_choice(raw, observation.candidates)
                    if candidate_action:
                        return candidate_action
                    action = parse_llm_action(raw)
                    if action:
                        return validate_llm_action(action)
                    # Один retry с запросом только валидного JSON
                    retry_q = (
                        "Ответь ТОЛЬКО валидным JSON. Предпочтительно: "
                        "{\"candidate_id\":\"cN\",\"reason\":\"...\"}. "
                        "Если кандидатов нет: action/selector/value/reason. Без markdown."
                    )
                    retry_raw = consult_agent_with_screenshot(
                        ctx, retry_q, screenshot_b64=send_screenshot,
                        phase_instruction=phase_instruction, tester_phase=memory_.tester_phase,
                        has_overlay=observation.has_overlay,
                    )
                    if retry_raw:
                        candidate_action = action_from_llm_candidate_choice(retry_raw, observation.candidates)
                        if candidate_action:
                            return candidate_action
                        action = parse_llm_action(retry_raw)
                        if action:
                            return validate_llm_action(action)
                return None

            nonlocal _llm_future_started_at
            _llm_future_started_at = time.time()
            _llm_future = _bg_submit(_call_llm, pool_name="llm")

        def _poll_llm() -> Optional[Dict[str, Any]]:
            """Проверить готова ли LLM (не блокирует). При таймауте — отменить и вернуть None."""
            nonlocal _llm_future, _llm_action, _llm_future_started_at, _llm_consecutive_timeouts, _llm_circuit_open_until
            if _llm_future is None:
                return _llm_action
            if _llm_future.done():
                try:
                    result = _llm_future.result(timeout=0)
                    _llm_action = result
                    if result is not None:
                        _llm_consecutive_timeouts = 0  # успех — сброс счётчика
                except Exception:
                    _llm_action = None
                _llm_future = None
                return _llm_action
            if LLM_RESPONSE_TIMEOUT_SEC > 0 and (time.time() - _llm_future_started_at) > LLM_RESPONSE_TIMEOUT_SEC:
                try:
                    _llm_future.cancel()
                except Exception:
                    pass
                _llm_future = None
                if LLM_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS > 0:
                    _llm_consecutive_timeouts += 1
                    if _llm_consecutive_timeouts >= LLM_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS:
                        _llm_circuit_open_until = time.time() + LLM_CIRCUIT_BREAKER_COOLDOWN_SEC
                        print(f"[Agent] Circuit breaker: LLM не отвечает {_llm_consecutive_timeouts} раз подряд. Только fast action следующие {LLM_CIRCUIT_BREAKER_COOLDOWN_SEC} сек.")
                return None
            return None  # ещё думает

        try:
            while True:
                step = memory.begin_step()
                memory.defects_on_current_step = 0
                set_current_agent_memory(memory)

                if session_max_steps > 0 and step > session_max_steps:
                    print(f"[Agent] Лимит {session_max_steps} шагов. Завершаю.")
                    session_result["termination"] = "max_steps"
                    break

                if _process_qa_retests(step):
                    if SESSION_REPORT_SAVE_EVERY_N > 0:
                        _save_report_now(step, "QA-ретест")
                    continue

                # Сохранять отчёт в начале каждого шага
                if SESSION_REPORT_SAVE_EVERY_N > 0 and step >= 1:
                    _save_report_now(step, f"начало шага {step}")

                current_url = page.url

                # Visual regression baseline: один раз на URL — сравнить с baseline или сохранить
                if VISUAL_BASELINE_DIR and current_url and current_url not in memory._visual_baseline_checked:
                    try:
                        b64 = take_screenshot_b64(page)
                        if b64:
                            baseline = load_baseline(VISUAL_BASELINE_DIR, current_url, "")
                            if not baseline:
                                save_baseline(VISUAL_BASELINE_DIR, current_url, b64, "")
                            else:
                                res = compare_with_baseline(
                                    VISUAL_BASELINE_DIR, current_url, b64, "",
                                    threshold_pct=VISUAL_REGRESSION_THRESHOLD_PCT,
                                )
                                if res and res.get("regression"):
                                    memory._visual_regressions.append({
                                        "url": current_url[:200],
                                        "change_percent": res.get("change_percent", 0),
                                        "detail": (res.get("detail") or "")[:200],
                                    })
                            memory._visual_baseline_checked.add(current_url)
                    except Exception as e:
                        LOG.debug("visual baseline check: %s", e)

                # НАВИГАЦИЯ ВКЛЮЧЕНА — агент активно переходит по страницам приложения
                # Новые вкладки — обрабатываем
                _handle_new_tabs(new_tabs_queue, page, start_url, step, console_log, network_failures, memory)

                # Обновить URL-паттерн (для дедупликации и бюджета)
                memory.set_current_url_pattern(page.url if not page.is_closed() else current_url)

                # Если ушли на другой домен — возвращаемся на start_url
                if not _same_page(start_url, page.url):
                    print(f"[Agent] #{step} Навигация на {page.url[:60]}. Возврат на {start_url[:60]}")
                    try:
                        page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                        smart_wait_after_goto(page, timeout=5000)
                        _inject_all(page)
                        memory.set_current_url_pattern(start_url)
                    except Exception as e:
                        LOG.warning("Ошибка возврата: %s", e)
                    if SESSION_REPORT_SAVE_EVERY_N > 0:
                        _save_report_now(step, "навигация-возврат")
                    continue

                # Бюджет: если на текущем url_pattern много шагов без новых
                # элементов и без дефектов — принудительно вернуться на старт.
                if memory.should_force_back_to_start() and not page.is_closed():
                    pat = memory.current_url_pattern
                    print(f"[Agent] #{step} Бюджет исчерпан (≥{URL_BUDGET_NO_PROGRESS} шагов без прогресса) на {pat[:80]}. Возврат на {start_url[:60]}.")
                    try:
                        page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                        smart_wait_after_goto(page, timeout=5000)
                        _inject_all(page)
                        memory.reset_url_budget(pat)
                        memory.set_current_url_pattern(start_url)
                    except Exception as e:
                        LOG.warning("Бюджет: возврат на start_url: %s", e)
                    if SESSION_REPORT_SAVE_EVERY_N > 0:
                        _save_report_now(step, "бюджет URL — возврат")
                    continue

                try:
                    _flush_pending_analysis(page, memory, console_log, network_failures)
                except Exception:
                    LOG.exception("flush_pending_analysis: исключение проглочено")

                # ===== Anti-Loop Guard =====
                # Реакция на «застой» сессии: лестница diversify → goto_start → hard_stop.
                # Подробности в AgentMemory.loop_guard_action / config.LOOP_GUARD_*.
                guard = memory.loop_guard_action()
                if guard == "hard_stop":
                    if session_max_steps <= 0 and not page.is_closed():
                        print(
                            f"[Agent] #{step} Anti-loop: бесконечный режим — вместо HARD STOP возвращаюсь на старт"
                        )
                        try:
                            page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                            smart_wait_after_goto(page, timeout=5000)
                            _inject_all(page)
                            memory.set_current_url_pattern(start_url)
                            memory.advance_tester_phase(force=True)
                            memory.reset_repeats()
                            memory.reset_session_progress()
                            memory.session_should_stop = False
                            memory.session_stop_reason = ""
                            memory.save_to_file(AGENT_MEMORY_PATH)
                        except Exception:
                            LOG.exception("infinite loop hard-stop recovery failed")
                        continue
                    print(
                        f"[Agent] #{step} Anti-loop: HARD STOP — {memory.session_stop_reason}"
                    )
                    LOG.warning("loop-guard hard_stop: %s", memory.session_stop_reason)
                    session_result["termination"] = "loop_guard"
                    break
                if guard == "goto_start" and not page.is_closed():
                    print(
                        f"[Agent] #{step} Anti-loop: GOTO_START "
                        f"({memory.steps_without_progress} шагов без прогресса)"
                    )
                    try:
                        page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                        smart_wait_after_goto(page, timeout=5000)
                        _inject_all(page)
                        memory.set_current_url_pattern(start_url)
                        memory.advance_tester_phase(force=True)
                        memory.reset_repeats()
                        memory.reset_session_progress()
                    except Exception:
                        LOG.exception("loop-guard goto_start failed")
                    if SESSION_REPORT_SAVE_EVERY_N > 0:
                        _save_report_now(step, "anti-loop: возврат на старт")
                    continue
                if guard == "diversify":
                    print(
                        f"[Agent] #{step} Anti-loop: DIVERSIFY "
                        f"({memory.steps_without_progress} шагов без прогресса)"
                    )
                    if memory.advance_module():
                        m = memory.get_current_module()
                        print(f"[Agent] Anti-loop: смена модуля: {(m or {}).get('name', '')[:50]}")
                    memory.advance_tester_phase(force=True)
                    memory.reset_repeats()
                    # Подменяем «следующее действие» на разнообразящее: либо назад в
                    # истории, либо просто scroll вниз — это и сбивает паттерн повторов,
                    # и часто открывает невидимые ранее модули.
                    forced_diversify_action = {
                        "action": "scroll",
                        "selector": "down",
                        "reason": "anti-loop diversify",
                    }
                    # Помечаем флагом, чтобы не давать LLM перебивать решение в этом шаге.
                    memory._diversify_step = step
                    memory._forced_action = forced_diversify_action

                # On-load: если за последний шаг произошла навигация — проверим
                # консоль/сеть на дефекты ЗАГРУЗКИ страницы (отдельный класс).
                pending_url = getattr(memory, "_pending_page_load_check_url", "") or ""
                if pending_url:
                    memory._pending_page_load_check_url = ""
                    try:
                        _check_page_load_and_report(
                            page, memory, pending_url, console_log, network_failures,
                            settle_seconds=1.0,
                        )
                    except Exception:
                        LOG.exception("check_page_load_and_report (navigation): ошибка")

                # Лимит логов
                if len(console_log) > CONSOLE_LOG_LIMIT:
                    del console_log[:len(console_log) - CONSOLE_LOG_LIMIT + 50]
                if len(network_failures) > NETWORK_LOG_LIMIT:
                    del network_failures[:len(network_failures) - NETWORK_LOG_LIMIT + 30]

                # Фаза
                if step > 1:
                    memory.advance_tester_phase()

                # Чеклист ОТКЛЮЧЕН — агент должен активно кликать, а не проверять
                checklist_results = []

                # ========== ВЫБОР ДЕЙСТВИЯ: LLM (если готов) или быстрое локальное ==========
                # Проверка: страница закрыта — выходим из цикла
                if page.is_closed():
                    print(f"[Agent] #{step} Страница закрыта. Завершаю.")
                    session_result["termination"] = "page_closed"
                    break
                
                try:
                    overlay_info_fast = detect_active_overlays(page)
                    has_overlay = overlay_info_fast.get("has_overlay", False)
                except Exception as e:
                    LOG.debug("detect_active_overlays: страница закрыта: %s", e)
                    break

                run_periodic_checks(
                    page,
                    memory,
                    step=step,
                    current_url=current_url,
                    console_log=console_log,
                    network_failures=network_failures,
                    has_overlay=has_overlay,
                )

                # Обновить модули страницы при смене URL (шапка, нав, main, секции)
                if not page.is_closed() and memory._modules_page_url != current_url:
                    try:
                        modules = get_page_modules(page)
                        if not modules:
                            modules = [{"id": "page", "name": "Страница", "selector": "body", "in_viewport": True}]
                        memory.set_page_modules(modules, current_url)
                        print(f"[Agent] Модули страницы: {len(modules)} — {[m.get('name', '')[:20] for m in modules]}")
                    except Exception:
                        pass

                # Ref-id для быстрого выбора (и для LLM)
                if not page.is_closed():
                    try:
                        get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
                    except Exception:
                        pass

                llm_action = _poll_llm()

                forced = getattr(memory, "_forced_action", None)
                if forced and getattr(memory, "_diversify_step", -1) == step:
                    action = forced
                    memory._forced_action = None
                    screenshot_b64 = None
                    source = "AntiLoop"
                    decision_candidates = []
                    if llm_action is not None:
                        # LLM предложил действие, но мы под антилупом — игнорируем
                        # его на этом шаге и не теряем зря: сбрасываем future, а не кладём
                        # «отложенное решение для потом» (контекст ушёл).
                        _llm_action = None
                elif llm_action is not None:
                    action = llm_action
                    _llm_action = None
                    screenshot_b64 = _llm_meta.get("screenshot_b64")
                    source = "LLM"
                    decision_candidates = _llm_meta.get("candidates") or []
                else:
                    action = choose_local_action(
                        page,
                        memory,
                        has_overlay=has_overlay,
                        overlay_info=overlay_info_fast,
                        upload_file_path=TEST_UPLOAD_FILE_PATH,
                    )
                    screenshot_b64 = None
                    source = "Fast"
                    decision_candidates = getattr(memory, "_last_action_candidates", []) or []

                expected_ref_meta = _llm_meta.get("ref_meta") if source == "LLM" else None
                action, source, decision_trace = apply_preflight_or_fallback(
                    page=page,
                    memory=memory,
                    action=action,
                    source=source,
                    has_overlay=has_overlay,
                    decision_candidates=decision_candidates,
                    fallback_factory=lambda: choose_local_action(
                        page,
                        memory,
                        has_overlay=has_overlay,
                        overlay_info=overlay_info_fast,
                        upload_file_path=TEST_UPLOAD_FILE_PATH,
                    ),
                    expected_ref_meta=expected_ref_meta,
                    step=step,
                )

                # Circuit breaker: не вызывать LLM пока открыт контур
                if _llm_future is None and not page.is_closed():
                    if time.time() < _llm_circuit_open_until:
                        pass  # только fast action
                    else:
                        try:
                            _start_llm_async(page, step, memory, console_log, network_failures, checklist_results, context)
                        except Exception:
                            pass

                act_type = (action.get("action") or "").lower()
                sel = (action.get("selector") or "").strip()
                val = (action.get("value") or "").strip()
                possible_bug = action.get("possible_bug")
                expected_outcome = action.get("expected_outcome", "")

                print(f"[Agent] #{step} [{source}] {act_type.upper()}: {sel[:40]} | {action.get('reason', '')[:40]}")

                # Дефект
                if act_type == "check_defect" and possible_bug:
                    if not page.is_closed():
                        _step_handle_defect(page, action, possible_bug, current_url, checklist_results, console_log, network_failures, memory)
                    if SESSION_REPORT_SAVE_EVERY_N > 0:
                        _save_report_now(step, "после дефекта")
                    continue

                # Anti-loop: серия неудач → reset
                if memory.is_stuck():
                    if memory.advance_module():
                        m = memory.get_current_module()
                        print(f"[Agent] Зацикливание — смена модуля: {(m or {}).get('name', '')[:50]}")
                    memory.advance_tester_phase(force=True)
                    memory.reset_repeats()
                    action = {"action": "scroll", "selector": "down", "reason": "Anti-loop reset"}
                    act_type, sel, val = "scroll", "down", ""

                # Запомнить скриншот до действия
                memory.screenshot_before_action = screenshot_b64
                memory.snapshot_logs_before_action(console_log, network_failures)

                # ========== ВЫПОЛНИТЬ ДЕЙСТВИЕ ==========
                # Проверка перед выполнением
                if page.is_closed():
                    print(f"[Agent] #{step} Страница закрыта перед выполнением действия. Завершаю.")
                    session_result["termination"] = "page_closed"
                    break
                
                try:
                    action_result = _step_execute(page, action, step, memory, context)
                except Exception as e:
                    if "closed" in str(e).lower() or "Target page" in str(e):
                        print(f"[Agent] #{step} Страница закрыта во время выполнения: {e}")
                        session_result["termination"] = "page_closed"
                        break
                    raise

                # Success/failure tracking
                if action_failed(action_result):
                    memory.record_action_failure()
                else:
                    memory.record_action_success()

                # Опционально: скриншот после шага для отчёта
                screenshot_path_rel = ""
                if not page.is_closed():
                    if SESSION_REPORT_HTML_PATH:
                        screenshot_dir = os.path.join(os.path.dirname(SESSION_REPORT_HTML_PATH), "screenshots")
                        try:
                            os.makedirs(screenshot_dir, exist_ok=True)
                            path = os.path.join(screenshot_dir, f"step_{step:04d}.png")
                            page.screenshot(path=path)
                            screenshot_path_rel = f"screenshots/step_{step:04d}.png"
                        except Exception as e:
                            LOG.debug("Скриншот шага: %s", e)
                    elif SAVE_STEP_SCREENSHOTS_DIR:
                        try:
                            os.makedirs(SAVE_STEP_SCREENSHOTS_DIR, exist_ok=True)
                            path = os.path.join(SAVE_STEP_SCREENSHOTS_DIR, f"step_{step:04d}.png")
                            page.screenshot(path=path)
                            screenshot_path_rel = path
                        except Exception as e:
                            LOG.debug("Скриншот шага: %s", e)

                flak = getattr(memory, "_last_step_flakiness", None)
                step_entry = {
                    "step": step,
                    "url": (current_url or "")[:200],
                    "action": act_type,
                    "selector": sel[:80] if sel else "",
                    "value": (action.get("value") or "")[:200],
                    "result": (action_result or "")[:200],
                    "source": source,
                    "screenshot_path": screenshot_path_rel,
                    "decision_trace": decision_trace,
                }
                if flak:
                    step_entry["flakiness_ok"], step_entry["flakiness_total"] = flak[0], flak[1]
                memory.append_step_log(step_entry)

                # Граф навигации и лимит глубины
                url_after = page.url if not page.is_closed() else current_url
                if url_after and url_after != (current_url or ""):
                    memory.record_navigation(current_url or "", url_after, step, sel or "")
                if MAX_NAVIGATION_DEPTH > 0 and not page.is_closed():
                    depth = memory.get_navigation_depth(page.url)
                    if depth > MAX_NAVIGATION_DEPTH:
                        print(f"[Agent] Глубина {depth} > {MAX_NAVIGATION_DEPTH}, возврат на {start_url[:60]}")
                        try:
                            page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                            smart_wait_after_goto(page, timeout=5000)
                            _inject_all(page)
                        except Exception as e:
                            LOG.warning("Возврат на start_url: %s", e)

                # Проверка битых ссылок каждые N шагов (в фоне)
                if BROKEN_LINKS_CHECK_EVERY_N > 0 and step % BROKEN_LINKS_CHECK_EVERY_N == 0 and not page.is_closed():
                    try:
                        urls_to_check = get_page_resource_urls(page, current_url or page.url)
                        if urls_to_check:
                            _bg_submit(
                                _check_broken_links_bg,
                                urls_to_check[:50],
                                memory,
                                pool_name="io",
                            )
                    except Exception as e:
                        LOG.debug("Broken links collect: %s", e)

                # Шаги по модулю: после N шагов переключаемся на следующий модуль
                memory.tick_module_step()
                if memory.get_current_module() and memory.steps_in_current_module >= PHASE_STEPS_TO_ADVANCE:
                    if memory.advance_module():
                        next_mod = memory.get_current_module()
                        if next_mod:
                            print(f"[Agent] Переход к модулю: {next_mod.get('name', '')[:50]}")

                _track_test_plan(memory, action)

                # Пост-анализ — в ФОНЕ. Main thread свободен для следующего шага,
                # но JS-ошибки/5xx/4xx/визуальные регрессии после действия мы по-
                # прежнему видим (без него дефекты вообще не создаются).
                # Исключения логируем громко: иначе вся часть с заведением дефектов
                # будет тихо ломаться, а в выводе будет «всё ок».
                try:
                    _step_post_analysis(
                        page, step, action, action_result, act_type, sel, val,
                        expected_outcome, possible_bug,
                        has_overlay, current_url, checklist_results,
                        console_log, network_failures, memory,
                    )
                except Exception:
                    LOG.exception("#%s post-analysis: исключение проглочено", step)

                if SESSION_REPORT_EVERY_N > 0 and step % SESSION_REPORT_EVERY_N == 0:
                    report = memory.get_session_report_text()
                    print(report)

                # Сохранять отчёт после каждого шага
                if SESSION_REPORT_SAVE_EVERY_N > 0 and step >= 1:
                    _save_report_now(step, f"конец шага {step}")
                if AGENT_MEMORY_PATH and AGENT_MEMORY_SAVE_EVERY_N > 0 and step % AGENT_MEMORY_SAVE_EVERY_N == 0:
                    memory.save_to_file(AGENT_MEMORY_PATH)

                time.sleep(0.3)

        except KeyboardInterrupt:
            print("\n[Agent] Остановлен по Ctrl+C.")
            session_result["termination"] = "interrupted"
        finally:
            try:
                retest_stop_event.set()
                if retest_thread is not None:
                    retest_thread.join(timeout=2)
            except Exception:
                pass

            # Отменить фоновые задачи LLM
            if '_llm_future' in locals() and _llm_future is not None:
                try:
                    _llm_future.cancel()
                except Exception:
                    pass
            
            # Дождаться фоновых задач (если страница ещё жива)
            try:
                if not page.is_closed():
                    _flush_pending_analysis(
                        page,
                        memory,
                        console_log,
                        network_failures,
                        wait_for_all=True,
                    )
            except Exception:
                pass
            
            # КРИТИЧНО: дождаться отправки всех дефектов в Jira (иначе будут теряться).
            try:
                pending = list(getattr(memory, "pending_defect_futures", []) or [])
                if pending:
                    print(f"[Agent] Дожидаемся отправки в Jira: {len(pending)} дефектов…")
                    for fut in pending:
                        try:
                            fut.result(timeout=60)
                        except Exception as e:
                            print(f"[Agent] Дефект не доставлен: {e}")
                    print("[Agent] Все Jira-задачи завершены.")
            except Exception as e:
                print(f"[Agent] Ошибка ожидания фоновых дефектов: {e}")

            # Jira delivery is durable work and must finish. Optional pools are
            # reused by the next supervised session; cancel only queued work so
            # uninterruptible HTTP calls cannot cause a new executor per restart.
            _shutdown_bg_pool(wait=True, pool_names=("jira",))
            _cancel_bg_tasks(
                pool_names=("llm", "analysis", "io", "default"),
            )

            if AGENT_MEMORY_PATH:
                memory.save_to_file(AGENT_MEMORY_PATH)
            
            # Закрыть transient UI, которое агент открыл как отдельное состояние страницы.
            try:
                registry = getattr(memory, "page_objects", None)
                if registry and registry.has_open_transient_states() and not page.is_closed():
                    print("[Agent] Закрываю transient UI перед завершением сессии...")
                    for _ in range(5):
                        overlay_info = detect_active_overlays(page)
                        has_overlay_now = bool(overlay_info.get("has_overlay"))
                        focus_now = capture_page_state(page, overlay_info=overlay_info).get("focus", "")
                        if not has_overlay_now and not focus_now:
                            registry.mark_current_closed()
                            break
                        if has_overlay_now:
                            _do_close_modal(page, "")
                        else:
                            _do_press_key(page, "Escape")
                        time.sleep(0.3)
                    final_overlay = detect_active_overlays(page)
                    final_focus = capture_page_state(page, overlay_info=final_overlay).get("focus", "")
                    if not final_overlay.get("has_overlay") and not final_focus:
                        registry.mark_current_closed()
                        print("[Agent] Transient UI закрыт и проверен по DOM.")
                    else:
                        print("[Agent] Не удалось полностью закрыть transient UI по DOM-проверке.")
            except Exception:
                LOG.debug("final transient UI cleanup failed", exc_info=True)
            
            if ENABLE_CONSOLE_WARNINGS_IN_REPORT:
                try:
                    memory._session_console_warnings = [c for c in console_log if c.get("type") in ("warning", "error")][-100:]
                except Exception:
                    memory._session_console_warnings = []

            # Финальный отчёт
            report = memory.get_session_report_text()
            plan_progress = memory.get_test_plan_progress()
            if plan_progress:
                report += "\n" + plan_progress
            if memory.reported_a11y_rules:
                report += f"\nA11y: проверено {len(memory.reported_a11y_rules)} правил"
            if memory.reported_perf_rules:
                report += f"\nPerf: обнаружено {len(memory.reported_perf_rules)} проблем"
            if memory.responsive_done:
                report += f"\nResponsive: проверены viewports {', '.join(memory.responsive_done)}"
            if ENABLE_CONSOLE_WARNINGS_IN_REPORT and getattr(memory, "_session_console_warnings", None):
                report += f"\nКонсоль (warnings/errors): {len(memory._session_console_warnings)}"
            if getattr(memory, "_mixed_content", None):
                report += f"\nMixed content: {len(memory._mixed_content)}"
            if getattr(memory, "_websocket_issues", None):
                report += f"\nWebSocket issues: {len(memory._websocket_issues)}"
            if getattr(memory, "_api_log", None):
                api_fail = sum(1 for a in memory._api_log if not a.get("ok", True))
                report += f"\nAPI (XHR/fetch): {len(memory._api_log)} записей, с ошибкой: {api_fail}"
            if getattr(memory, "_visual_regressions", None):
                report += f"\nVisual regressions: {len(memory._visual_regressions)}"
            if getattr(memory, "_step_log", None):
                report += "\n--- Лог шагов ---"
                for e in memory._step_log[-50:]:
                    report += f"\n  #{e.get('step')} [{e.get('source')}] {e.get('action')} -> {e.get('result', '')[:60]}"
            print(report)
            if SESSION_REPORT_PATH:
                try:
                    with open(SESSION_REPORT_PATH, "w", encoding="utf-8") as f:
                        f.write(report)
                    print(f"[Agent] Отчёт записан в {SESSION_REPORT_PATH}")
                except Exception as e:
                    LOG.warning("Не удалось записать отчёт в файл %s: %s", SESSION_REPORT_PATH, e)
            if SESSION_REPORT_HTML_PATH:
                try:
                    html_content = _build_html_report(memory, report, start_url or "", video_dir=RECORD_VIDEO_DIR or "")
                    with open(SESSION_REPORT_HTML_PATH, "w", encoding="utf-8") as f:
                        f.write(html_content)
                    print(f"[Agent] HTML-отчёт записан в {SESSION_REPORT_HTML_PATH}")
                except Exception as e:
                    LOG.warning("Не удалось записать HTML-отчёт %s: %s", SESSION_REPORT_HTML_PATH, e)
            if SESSION_REPORT_JSONL and getattr(memory, "_step_log", None):
                try:
                    with open(SESSION_REPORT_JSONL, "w", encoding="utf-8") as f:
                        for e in memory._step_log:
                            line = json.dumps(e, ensure_ascii=False) + "\n"
                            f.write(line)
                    print(f"[Agent] JSONL-лог записан в {SESSION_REPORT_JSONL}")
                except Exception as e:
                    LOG.warning("Не удалось записать JSONL %s: %s", SESSION_REPORT_JSONL, e)
            if PLAYWRIGHT_EXPORT_PATH and getattr(memory, "_step_log", None):
                try:
                    from agent.browser.playwright_export import build_playwright_script
                    script = build_playwright_script(memory._step_log, start_url or "")
                    with open(PLAYWRIGHT_EXPORT_PATH, "w", encoding="utf-8") as f:
                        f.write(script)
                    print(f"[Agent] Playwright-скрипт записан в {PLAYWRIGHT_EXPORT_PATH}")
                except Exception as e:
                    LOG.warning("Не удалось записать Playwright-скрипт %s: %s", PLAYWRIGHT_EXPORT_PATH, e)
            if SESSION_BASELINE_JSONL and getattr(memory, "_step_log", None):
                try:
                    with open(SESSION_BASELINE_JSONL, "w", encoding="utf-8") as f:
                        for e in memory._step_log:
                            f.write(json.dumps(e, ensure_ascii=False) + "\n")
                    print(f"[Agent] Baseline сохранён в {SESSION_BASELINE_JSONL}")
                except Exception as e:
                    LOG.warning("Не удалось сохранить baseline %s: %s", SESSION_BASELINE_JSONL, e)
            if SESSION_STATE_SAVE_PATH and "context" in locals():
                try:
                    cookies = context.cookies()
                    with open(SESSION_STATE_SAVE_PATH, "w", encoding="utf-8") as f:
                        json.dump(cookies, f, ensure_ascii=False, indent=0)
                    print(f"[Agent] Состояние (cookies) сохранено в {SESSION_STATE_SAVE_PATH}")
                except Exception as e:
                    LOG.warning("Не удалось сохранить состояние %s: %s", SESSION_STATE_SAVE_PATH, e)
            if JUNIT_REPORT_PATH and getattr(memory, "_step_log", None):
                try:
                    _write_junit_report(memory, JUNIT_REPORT_PATH)
                    print(f"[Agent] JUnit-отчёт записан в {JUNIT_REPORT_PATH}")
                except Exception as e:
                    LOG.warning("Не удалось записать JUnit %s: %s", JUNIT_REPORT_PATH, e)

            session_result["defects"] = max(
                0,
                len(getattr(memory, "defects_created", [])) - initial_defect_count,
            )
            session_result["steps"] = getattr(memory, "iteration", 0)

            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            else:
                try:
                    context.close()
                except Exception:
                    pass

    if not session_result.get("termination"):
        session_result["termination"] = "completed"
    return session_result


def run_agent(
    start_url: str = None,
    *,
    max_steps: Optional[int] = None,
    enable_qa_retests: bool = True,
):
    """Run the agent under a process-level browser session supervisor."""
    from agent.core.supervisor import AgentSupervisor, SupervisorConfig

    effective_max_steps = MAX_STEPS if max_steps is None else max(0, int(max_steps))
    supervisor = AgentSupervisor(
        SupervisorConfig(
            continuous=AGENT_CONTINUOUS_RESTART,
            bounded_run=effective_max_steps > 0,
            base_delay=AGENT_RESTART_BASE_DELAY_SEC,
            max_delay=AGENT_RESTART_MAX_DELAY_SEC,
        )
    )
    return supervisor.run(
        lambda start_url=None: _run_agent_session(
            start_url=start_url,
            max_steps=effective_max_steps,
            enable_qa_retests=enable_qa_retests,
        ),
        start_url=start_url,
    )



def _step_handle_defect(page, action, possible_bug, current_url, checklist_results, console_log, network_failures, memory):
    """Обработка явного check_defect."""
    if ENABLE_SECOND_PASS_BUG:
        post_b64 = take_screenshot_b64(page)
        if not ask_is_this_really_bug(possible_bug, post_b64):
            print(f"[Agent] Второй проход: не баг, пропускаем.")
            update_llm_overlay(page, prompt="Ревью", response="Не баг", loading=False)
            memory.add_action(action, result="defect_skipped_second_pass")
            time.sleep(0.3)
            return
    _create_defect(page, possible_bug, current_url, checklist_results, console_log, network_failures, memory)
    memory.add_action(action, result="defect_reported")
    time.sleep(1)


def _capture_click_target_state(page, selector: str) -> Optional[Dict[str, Any]]:
    """Capture properties that can change without changing serialized HTML."""
    if not selector:
        return None
    try:
        locator = _find_element(page, selector)
        if not locator:
            return None
        return locator.evaluate(
            """(el) => ({
                checked: ('checked' in el) ? Boolean(el.checked) : null,
                value: ('value' in el) ? String(el.value || '').slice(0, 200) : null,
                selectedIndex: ('selectedIndex' in el) ? el.selectedIndex : null,
                open: ('open' in el) ? Boolean(el.open) : null,
                ariaExpanded: el.getAttribute && el.getAttribute('aria-expanded'),
                ariaPressed: el.getAttribute && el.getAttribute('aria-pressed'),
                ariaSelected: el.getAttribute && el.getAttribute('aria-selected'),
                disabled: ('disabled' in el) ? Boolean(el.disabled) : null
            })"""
        )
    except Exception:
        return None


def _step_execute(page, action, step, memory, context):
    """STEP 3: Выполнение действия с retry."""
    act_type = (action.get("action") or "").lower()
    sel = (action.get("selector") or "").strip()
    click_target_state_before = (
        _capture_click_target_state(page, sel)
        if act_type == "click" and not page.is_closed()
        else None
    )
    page_state_before = {}
    if not page.is_closed():
        try:
            page_state_before = capture_page_state(
                page,
                dom_summary=str(page.evaluate("() => document.body ? document.body.innerHTML.length : 0")),
                overlay_info=detect_active_overlays(page),
            )
        except Exception:
            page_state_before = {}
    if ENABLE_DOM_DIFF_AFTER_ACTION and not page.is_closed():
        try:
            memory._dom_hash_before = page.evaluate("() => document.body ? document.body.innerHTML.length : 0")
        except Exception:
            memory._dom_hash_before = None

    # Зафиксировать контекст шага ДО выполнения: URL и человекочитаемый локатор элемента.
    try:
        url_before = page.url if not page.is_closed() else ""
    except Exception:
        url_before = ""
    element_desc = ""
    if sel and act_type in ("click", "type", "hover", "select_option", "upload_file", "press_key"):
        try:
            element_desc = describe_element_for_report(page, sel)
        except Exception:
            element_desc = ""
    action["_step_context"] = {
        "url_before": url_before,
        "element_desc": element_desc,
        "selector": sel,
    }

    # Передаём стратегию заполнения формы
    if act_type == "type":
        strategy = get_form_fill_strategy(memory.tester_phase, memory.form_strategy_iteration)
        action["_form_strategy"] = strategy
        memory.form_strategy_iteration += 1

    result = execute_action(page, action, memory)
    for _ in range(max(0, ACTION_RETRY_COUNT)):
        if not action_failed(result):
            break
        retry = prepare_action_retry(page, memory, action)
        if not retry.allowed:
            result = f"{result} retry_aborted:{retry.reason}"
            break
        action = retry.action
        time.sleep(0.15)
        result = execute_action(page, action, memory)

    # Flakiness: при сбое перезапустить действие ещё N раз и записать долю успехов
    memory._last_step_flakiness = None
    if (
        action_failed(result)
        and FLAKINESS_RERUN_COUNT >= 2
        and act_type in ("type", "hover")
    ):
        ok = 0
        attempted = 1
        for _ in range(FLAKINESS_RERUN_COUNT - 1):
            retry = prepare_action_retry(page, memory, action)
            if not retry.allowed:
                break
            time.sleep(0.2)
            r2 = execute_action(page, retry.action, memory)
            attempted += 1
            if not action_failed(r2):
                ok += 1
        memory._last_step_flakiness = (ok, attempted)

    # Карта покрытия
    if act_type == "scroll" and not page.is_closed():
        try:
            y = page.evaluate("() => window.scrollY")
            h = page.evaluate("() => Math.max(0, document.documentElement.scrollHeight - window.innerHeight)")
            if h <= 0:
                zone = "top"
            elif y < h * 0.3:
                zone = "top"
            elif y < h * 0.7:
                zone = "middle"
            else:
                zone = "bottom"
            memory.record_coverage_zone(zone)
        except Exception:
            pass

    # DOM diff: после клика DOM не изменился — возможный мёртвый клик
    if ENABLE_DOM_DIFF_AFTER_ACTION and act_type == "click" and not page.is_closed():
        try:
            h = page.evaluate("() => document.body ? document.body.innerHTML.length : 0")
            target_after = _capture_click_target_state(page, sel)
            target_unchanged = (
                click_target_state_before is None
                or click_target_state_before == target_after
            )
            if (
                getattr(memory, "_dom_hash_before", None) is not None
                and h == memory._dom_hash_before
                and target_unchanged
            ):
                result = (result or "") + " possible_dead_click"
        except Exception:
            pass

    # Минимальная пауза: только чтобы DOM обновился
    time.sleep(0.3)
    # Быстрый wait (не 3 секунды!)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=2000)
    except Exception:
        pass

    if not page.is_closed() and getattr(memory, "page_objects", None):
        try:
            overlay_info = detect_active_overlays(page)
            dom_len = page.evaluate("() => document.body ? document.body.innerHTML.length : 0")
            page_state = memory.page_objects.update_from_observation(
                page,
                dom_summary=str(dom_len),
                overlay_info=overlay_info,
                candidates=[],
                ref_meta={},
            )
            if page_state_before and page_state_before.get("state_key") != page_state.state_key:
                result = (result or "") + f" page_state_changed:{page_state.state_name[:80]}"
        except Exception:
            LOG.debug("page object state update after action failed", exc_info=True)

    memory.add_action(action, result=result)
    memory.tick_phase_step()
    print(f"[Agent] #{step} Результат: {result}")
    return result


def _track_test_plan(memory: AgentMemory, action: Dict):
    """Отследить, какой пункт тест-плана закрыт текущим действием.

    Поддерживает оба формата плана:
      • структурированный (memory.structured_test_plan: {title, intent, module, ...})
      • плоский (memory.test_plan: List[str])
    Совпадение определяем простой эвристикой: ≥2 «значимых» слова (>3 символов) из
    title/intent встречаются в reason/test_goal/element_desc/selector текущего действия.
    """
    reason = (action.get("reason") or "").lower()
    test_goal = (action.get("test_goal") or "").lower()
    sel = (action.get("selector") or "").lower()
    elem_desc = ""
    step_ctx = action.get("_step_context") or {}
    if isinstance(step_ctx, dict):
        elem_desc = (step_ctx.get("element_desc") or "").lower()
    combined = f"{reason} {test_goal} {sel} {elem_desc}"

    structured = getattr(memory, "structured_test_plan", None) or []
    if structured:
        for i, item in enumerate(structured):
            if item.get("done"):
                continue
            haystack = " ".join([
                (item.get("title") or "").lower(),
                (item.get("intent") or "").lower(),
                (item.get("module") or "").lower(),
                (item.get("area") or "").lower(),
            ])
            words = [w for w in haystack.split() if len(w) > 3]
            matches = sum(1 for w in words if w in combined)
            if matches >= 2 or (len(words) <= 2 and matches >= 1):
                memory.mark_structured_test_plan_step(i, result=action.get("expected_outcome", ""))
                print(f"[Agent] Тест-план: закрыт пункт {i+1}: {(item.get('title') or '')[:50]}")
                return

    if not memory.test_plan or not memory.test_plan_completed:
        return
    for i, step in enumerate(memory.test_plan):
        if memory.test_plan_completed[i]:
            continue
        step_lower = step.lower()
        words = [w for w in step_lower.split() if len(w) > 3]
        matches = sum(1 for w in words if w in combined)
        if matches >= 2 or (len(words) <= 2 and matches >= 1):
            memory.mark_test_plan_step(i)
            print(f"[Agent] Тест-план: закрыт пункт {i+1}: {step[:50]}")
            break
