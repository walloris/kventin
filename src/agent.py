"""
AI-агент тестировщик: активно ходит по сайту, кликает, заполняет формы,
скринит экран и отправляет в GigaChat за советом. Многофазный цикл:
1) Скриншот + контекст → GigaChat (что вижу, что делать?)
2) Выполняем действие (click, type, scroll, hover)
3) Скриншот после действия → GigaChat (что произошло, есть баг?)
4) Если баг → Jira. Если нет → следующее действие.
Все действия видимы. Память действий — не повторяемся.

Архитектура: pipeline с фоновым пулом потоков.
- Main thread: Playwright (действия, скриншоты) — sync only
- Background pool: GigaChat, Jira, a11y, perf — параллельно
"""
import base64
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import Future
from datetime import datetime
from typing import List, Dict, Any, Optional

from playwright.sync_api import sync_playwright, Page

from config import (
    START_URL,
    BROWSER_SLOW_MO,
    HEADLESS,
    BROWSER_USER_DATA_DIR,
    CHECKLIST_STEP_DELAY_MS,
    VIEWPORT_WIDTH,
    VIEWPORT_HEIGHT,
    ENABLE_TEST_PLAN_START,
    ENABLE_ORACLE_AFTER_ACTION,
    ENABLE_SECOND_PASS_BUG,
    ACTION_RETRY_COUNT,
    SESSION_REPORT_EVERY_N,
    SESSION_REPORT_PATH,
    SESSION_REPORT_HTML_PATH,
    SESSION_REPORT_JSONL,
    SESSION_REPORT_SAVE_EVERY_N,
    SAVE_STEP_SCREENSHOTS_DIR,
    ORACLE_ON_VISUAL_OR_ERROR,
    CRITICAL_FLOW_STEPS,
    MAX_STEPS,
    SCROLL_PIXELS,
    MAX_ACTIONS_IN_MEMORY,
    MAX_SCROLLS_IN_ROW,
    CONSOLE_LOG_LIMIT,
    NETWORK_LOG_LIMIT,
    POST_ACTION_DELAY,
    PHASE_STEPS_TO_ADVANCE,
    GIGACHAT_RESPONSE_TIMEOUT_SEC,
    GIGACHAT_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS,
    GIGACHAT_CIRCUIT_BREAKER_COOLDOWN_SEC,
    ACTION_TIMEOUT_MS,
    A11Y_CHECK_EVERY_N,
    PERF_CHECK_EVERY_N,
    ENABLE_RESPONSIVE_TEST,
    RESPONSIVE_VIEWPORTS,
    SESSION_PERSIST_CHECK_EVERY_N,
    SELF_HEAL_AFTER_FAILURES,
    ENABLE_SCENARIO_CHAINS,
    SCENARIO_CHAIN_LENGTH,
    ENABLE_IFRAME_TESTING,
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
    BROWSER_SUPPRESS_CERT_PROMPT,
    BROWSER_CHROMIUM_ARGS,
    BROWSER_CLIENT_CERT_ORIGIN,
    BROWSER_CLIENT_CERT_ORIGINS,
    BROWSER_CLIENT_CERT_PFX_PATH,
    BROWSER_CLIENT_CERT_PASSPHRASE,
    BROWSER_CLIENT_CERT_CERT_PATH,
    BROWSER_CLIENT_CERT_KEY_PATH,
    BROWSER_AUTO_SELECT_CERT_PATTERNS,
    PLAYWRIGHT_EXPORT_PATH,
    ENABLE_API_INTERCEPT,
    API_LOG_MAX,
    ENABLE_DOM_DIFF_AFTER_ACTION,
    VISUAL_BASELINE_DIR,
    VISUAL_REGRESSION_THRESHOLD_PCT,
    TEST_SPEC_YAML_PATH,
    FLAKINESS_RERUN_COUNT,
)
from src.gigachat_client import (
    consult_agent_with_screenshot,
    consult_agent,
    get_test_plan_from_screenshot,
    ask_is_this_really_bug,
    init_gigachat_connection,
)
from src.llm_parser import parse_llm_action, validate_llm_action
from src.form_strategies import detect_field_type, get_test_value, get_form_fill_strategy
from src.accessibility import check_accessibility, format_a11y_issues
from src.visual_diff import (
    compute_screenshot_diff,
    compare_with_baseline,
    save_baseline,
    load_baseline,
)
from src.performance import check_performance, format_performance_issues

import html as html_module
import logging

LOG = logging.getLogger("Agent")
if not LOG.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[Agent] %(levelname)s %(message)s"))
    LOG.addHandler(h)

# Текущая память агента в основном цикле (для self-healing в _find_element)
_current_agent_memory: Optional["AgentMemory"] = None

# Фоновый пул для параллельных задач (GigaChat, Jira, a11y, perf).
# Playwright НЕ thread-safe → только main thread. Всё остальное — в пул.
# Реализация и сам инстанс пула живут в src/bg_pool.py.
from src.bg_pool import (
    bg_result as _bg_result,
    bg_submit as _bg_submit,
    get_bg_pool as _get_bg_pool,
    shutdown_bg_pool as _shutdown_bg_pool,
)

from src.jira_client import create_jira_issue, reset_session_defects
from src.page_analyzer import (
    build_context,
    get_dom_summary,
    get_page_modules,
    get_page_resource_urls,
    detect_active_overlays,
    format_overlays_context,
    detect_cookie_banner,
    detect_page_type,
    detect_form_fields,
    detect_table_structure,
)
from src.visible_actions import (
    inject_cursor,
    move_cursor_to,
    highlight_and_click,
    safe_highlight,
    scroll_to_center,
    inject_llm_overlay,
    update_llm_overlay,
    show_highlight_label,
)
from src.wait_utils import smart_wait_after_goto
from src.checklist import run_checklist, checklist_results_to_context, build_checklist
from src.defect_builder import (
    build_defect_summary,
    build_defect_description,
    collect_evidence,
    infer_defect_severity,
)
from src.locators import url_pattern as _url_pattern

# Бюджет на URL — единый источник правды в config.py.
from config import URL_BUDGET_NO_PROGRESS  # noqa: E402,F401


# Резолверы и обогащение действий вынесены в src/element_resolver.py
from src.element_resolver import (
    enrich_action,
    norm_key as _norm_key,
    resolve_canonical_locator,
    resolve_stable_key,
)


# AgentMemory вынесен в src/agent_memory.py — здесь только реэкспорт
# для обратной совместимости (run_agent и кучу других мест ссылаются
# именно на agent.AgentMemory).
from src.agent_memory import AgentMemory  # noqa: E402,F401


# --- Скриншот в base64 ---
def _hide_agent_ui(page: Page):
    """Скрыть UI агента перед скриншотом (Shadow DOM host)."""
    try:
        page.evaluate("""() => {
            if (window.__agentShadow && window.__agentShadow.host) {
                window.__agentShadow.host.style.display = 'none';
            }
            // Временные элементы (label, ripple)
            document.querySelectorAll('[data-agent-host]').forEach(el => el.style.display = 'none');
        }""")
    except Exception:
        pass


def _show_agent_ui(page: Page):
    """Вернуть UI агента после скриншота."""
    try:
        page.evaluate("""() => {
            if (window.__agentShadow && window.__agentShadow.host) {
                window.__agentShadow.host.style.display = '';
            }
            document.querySelectorAll('[data-agent-host]').forEach(el => el.style.display = '');
        }""")
    except Exception:
        pass


def take_screenshot_b64(page: Page) -> Optional[str]:
    """Сделать скриншот (без UI агента) и вернуть base64-строку."""
    try:
        if page.is_closed():
            return None
        _hide_agent_ui(page)
        raw = page.screenshot(type="png")
        return base64.b64encode(raw).decode("ascii")
    except Exception as e:
        if "closed" in str(e).lower() or "Target page" in str(e):
            return None
        print(f"[Agent] Ошибка скриншота: {e}")
        return None
    finally:
        try:
            if not page.is_closed():
                _show_agent_ui(page)
        except Exception:
            pass


def describe_element_for_report(page: Page, selector: str) -> str:
    """
    Построить человекочитаемое описание элемента по селектору (ref:N или CSS)
    для подробных шагов воспроизведения и описания дефекта.
    Пример: 'button «Войти» id=#login-btn data-testid="login-submit" aria-label="Вход"'.
    """
    if not selector:
        return ""
    sel = selector.strip()
    try:
        # ref:N — достаём элемент из window.__agentRefs
        ref = None
        if sel.startswith("ref:"):
            try:
                ref = int(sel[4:])
            except ValueError:
                ref = None
        elif sel.isdigit():
            ref = int(sel)
        if ref is not None:
            desc = page.evaluate(
                """(ref) => {
                    const el = (window.__agentRefs && window.__agentRefs[ref])
                        || document.querySelector('[data-agent-ref="'+ref+'"]');
                    if (!el) return '';
                    const tag = el.tagName ? el.tagName.toLowerCase() : '';
                    const role = el.getAttribute && el.getAttribute('role') || '';
                    const type = el.type || '';
                    const text = (el.innerText || el.textContent || el.value || el.placeholder || '')
                        .trim().replace(/\\s+/g,' ').slice(0, 100);
                    const aria = (el.getAttribute && el.getAttribute('aria-label')) || '';
                    const title = (el.getAttribute && el.getAttribute('title')) || '';
                    const id = el.id || '';
                    const name = el.name || '';
                    const href = (el.getAttribute && el.getAttribute('href')) || '';
                    const placeholder = el.placeholder || '';
                    const testId = (el.getAttribute && (el.getAttribute('data-testid')
                        || el.getAttribute('data-test-id')
                        || el.getAttribute('data-test')
                        || el.getAttribute('data-qa'))) || '';
                    const parts = [];
                    let head = tag + (type ? ':' + type : '');
                    if (role) head += '[role=' + role + ']';
                    parts.push(head);
                    if (text) parts.push('«' + text + '»');
                    if (testId) parts.push('data-testid="' + testId + '"');
                    if (id) parts.push('#' + id);
                    if (name) parts.push('name=' + name);
                    if (aria) parts.push('aria-label="' + aria.slice(0,80) + '"');
                    if (title) parts.push('title="' + title.slice(0,60) + '"');
                    if (placeholder) parts.push('placeholder="' + placeholder.slice(0,60) + '"');
                    if (href) parts.push('href=' + href.slice(0, 120));
                    // CSS-локатор как дополнительная подсказка
                    const css = id ? '#' + id
                        : (testId ? '[data-testid="' + testId + '"]'
                        : (name ? tag + '[name="' + name + '"]'
                        : (aria ? tag + '[aria-label="' + aria.slice(0,60) + '"]'
                        : (href ? tag + '[href="' + href.slice(0,100) + '"]'
                        : tag))));
                    parts.push('css=' + css);
                    return parts.join(' ');
                }""",
                ref,
            )
            if desc:
                return str(desc)[:400]
    except Exception:
        pass
    # Fallback: использовать сам селектор
    return sel[:120]


# --- Выполнение действия ---
def execute_action(page: Page, action: Dict[str, Any], memory: AgentMemory) -> str:
    """Выполнить действие на странице. Возвращает текстовый результат."""
    act = action.get("action", "").lower()
    selector = action.get("selector", "").strip()
    value = action.get("value", "").strip()
    reason = action.get("reason", "")

    print(f"[Agent] Действие: {act} -> {selector[:60]} | {reason[:60]}")

    if act == "click":
        result = _do_click(page, selector, reason)
        # Записываем клик в покрытие
        if memory and "clicked" in (result or "").lower():
            memory.record_page_element(page.url, f"click:{_norm_key(selector)}")
        return result
    elif act == "fill_form":
        # Умное заполнение формы
        form_strat = action.get("_form_strategy", "happy")
        result = _fill_form_smart(page, form_strategy=form_strat, memory=memory)
        # Записываем заполнение формы в покрытие
        if memory and "form_filled" in (result or "").lower():
            memory.record_page_element(page.url, "fill_form:all_fields")
        return result
    elif act == "type":
        form_strat = action.get("_form_strategy", "happy")
        result = _do_type(page, selector, value, form_strategy=form_strat)
        # Записываем в покрытие
        if memory and "typed" in (result or "").lower():
            memory.record_page_element(page.url, f"type:{_norm_key(selector)}")
        return result
    elif act == "scroll":
        return _do_scroll(page, selector)
    elif act == "hover":
        return _do_hover(page, selector)
    elif act == "explore":
        return _do_scroll(page, "down")
    elif act == "close_modal":
        return _do_close_modal(page, selector)
    elif act == "select_option":
        return _do_select_option(page, selector, value)
    elif act == "press_key":
        return _do_press_key(page, selector or value or "Escape")
    elif act == "upload_file":
        result = _do_upload_file(page, selector, value)
        if memory and "uploaded" in (result or "").lower():
            memory.record_page_element(page.url, f"type:{_norm_key(selector)}")
        return result
    elif act == "check_defect":
        return "defect_found"
    else:
        print(f"[Agent] Неизвестное действие: {act}, пробую клик")
        return _do_click(page, selector, reason) if selector else "no_action"


def _find_element(page: Page, selector: str):
    """
    Поиск элемента по ref-id (мгновенный) с fallback по атрибутам.
    Self-healing: при успехе fallback кешируем селектор в memory._selector_heal_cache;
    при следующем вызове сначала пробуем кешированный вариант.

    Стратегии (по приоритету):
      0) кеш self-healing (если ref ранее найден через getByRole/getByText)
      1) ref:N — мгновенный поиск через data-agent-ref
      2) CSS/XPath/ID, семантика, getByRole/getByText
    """
    global _current_agent_memory
    if not selector:
        return None

    selector = selector.strip()

    # --- 0) Self-healing: попробовать кешированный селектор ---
    mem = _current_agent_memory
    if mem and getattr(mem, "_selector_heal_cache", None) and selector in mem._selector_heal_cache:
        c = mem._selector_heal_cache[selector]
        try:
            strat = c.get("strategy") or ""
            role = c.get("role")
            name = (c.get("name") or "").strip()
            if strat == "getByRole" and role and name:
                loc = page.get_by_role(role, name=name, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            elif strat == "getByLabel" and name:
                loc = page.get_by_label(name, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            elif strat == "getByText" and name:
                loc = page.get_by_text(name, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            elif strat == "getByPlaceholder" and name:
                loc = page.get_by_placeholder(name, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
        except Exception:
            pass

    # --- 1) ref:N — основной путь (мгновенный) ---
    ref_num = None
    if selector.startswith("ref:"):
        try:
            ref_num = int(selector[4:])
        except ValueError:
            pass
    elif selector.isdigit():
        ref_num = int(selector)

    if ref_num is not None:
        try:
            # Сначала пробуем через data-agent-ref (надёжный CSS-селектор)
            loc = page.locator(f'[data-agent-ref="{ref_num}"]').first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            pass
        try:
            # Fallback: через сохранённую JS-ссылку (если DOM изменился, но ссылка жива)
            exists = page.evaluate(f"() => !!window.__agentRefs && !!window.__agentRefs[{ref_num}] && document.contains(window.__agentRefs[{ref_num}])")
            if exists:
                loc = page.locator(f'[data-agent-ref="{ref_num}"]').first
                if loc.count() > 0:
                    return loc
        except Exception:
            pass
        LOG.debug(f"_find_element ref:{ref_num} not found, falling back to text strategies")

    safe_text = selector.replace('"', '\\"').replace("'", "\\'")[:100]

    # --- 1) Явные CSS/XPath/ID селекторы ---
    if selector.startswith(("#", ".", "[", "//")):
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            pass

    # --- 2) Семантические атрибуты (быстрые) ---
    attr_strategies = [
        f'[data-testid="{safe_text}"]',
        f'[data-testid*="{safe_text}"]',
        f'[aria-label="{safe_text}"]',
        f'[aria-label*="{safe_text}"]',
        f'[placeholder="{safe_text}"]',
        f'[name="{safe_text}"]',
        f'[title="{safe_text}"]',
    ]
    for css in attr_strategies:
        try:
            loc = page.locator(css).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue

    # --- 3) Playwright getBy* методы ---
    getby_strategies = [
        ("getByRole:button", "getByRole", "button", lambda: page.get_by_role("button", name=safe_text, exact=False).first),
        ("getByRole:link", "getByRole", "link", lambda: page.get_by_role("link", name=safe_text, exact=False).first),
        ("getByRole:tab", "getByRole", "tab", lambda: page.get_by_role("tab", name=safe_text, exact=False).first),
        ("getByRole:menuitem", "getByRole", "menuitem", lambda: page.get_by_role("menuitem", name=safe_text, exact=False).first),
        ("getByLabel", "getByLabel", None, lambda: page.get_by_label(safe_text, exact=False).first),
        ("getByPlaceholder", "getByPlaceholder", None, lambda: page.get_by_placeholder(safe_text, exact=False).first),
        ("getByText", "getByText", None, lambda: page.get_by_text(safe_text, exact=True).first),
    ]
    for _label, strat, role, get_loc in getby_strategies:
        try:
            loc = get_loc()
            if loc.count() > 0 and loc.is_visible():
                if mem and selector:
                    mem._selector_heal_cache[selector] = {"strategy": strat, "role": role, "name": safe_text}
                return loc
        except Exception:
            continue

    # --- 4) Текстовый has-text fallback ---
    text_strategies = [
        f'button:has-text("{safe_text}")',
        f'a:has-text("{safe_text}")',
        f'[role="button"]:has-text("{safe_text}")',
    ]
    for css in text_strategies:
        try:
            loc = page.locator(css).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue

    return None


def _do_click(page: Page, selector: str, reason: str = "") -> str:
    if not selector:
        return "no_selector"
    loc = _find_element(page, selector)
    if loc:
        try:
            # ПРОВЕРКА: кликаем только по внутренним ссылкам
            try:
                tag = loc.evaluate("el => el.tagName.toLowerCase()")
                if tag == "a":
                    href = loc.evaluate("el => el.getAttribute('href') || ''")
                    if href and not href.startswith("javascript:") and href != "#":
                        # Проверяем что это внутренняя ссылка (на том же домене)
                        is_internal = False
                        try:
                            current_url = page.url
                            if href.startswith("/") or href.startswith("./") or href.startswith("../") or not href.startswith("http"):
                                is_internal = True  # Относительный путь — всегда внутренний
                            elif href.startswith("http"):
                                # Абсолютный URL — проверяем домен
                                from urllib.parse import urlparse
                                current_domain = urlparse(current_url).netloc
                                href_domain = urlparse(href).netloc
                                is_internal = (href_domain == current_domain or href_domain == "")
                        except Exception:
                            is_internal = True  # При ошибке разрешаем клик
                        
                        if not is_internal:
                            print(f"[Agent] ⚠️ Пропускаю внешнюю ссылку: {selector[:50]}")
                            return f"skipped_external_link: {selector[:50]}"
            except Exception:
                pass
            
            print(f"[Agent] КЛИК: {selector[:50]} ({reason[:30]})")
            scroll_to_center(loc, page)
            loc.click()
            print(f"[Agent] Клик выполнен: {selector[:50]}")
            return f"clicked: {selector[:50]}"
        except Exception as e:
            print(f"[Agent] ❌ Ошибка клика: {e}")
            return f"click_error: {e}"
    print(f"[Agent] ⚠️ Элемент не найден: {selector[:50]}")
    return f"not_found: {selector[:50]}"


def _fill_form_smart(page: Page, form_strategy: str = "happy", memory: Optional[AgentMemory] = None) -> str:
    """
    Умное заполнение формы: найти все поля формы и заполнить их за раз.
    Возвращает результат заполнения.
    """
    try:
        fields = detect_form_fields(page)
        if not fields:
            return "no_form_fields"
        
        filled_count = 0
        from src.form_strategies import detect_field_type, get_test_value
        
        for field in fields:
            selector = field.get("selector") or field.get("id") or field.get("name") or field.get("placeholder", "")
            if not selector:
                continue
            
            # Определяем тип поля для правильной проверки уже протестированных элементов
            field_type_str = field.get("type", "").lower()
            is_select = field_type_str == "select"
            
            # Проверяем, не заполняли ли уже это поле (используем правильный префикс)
            if memory:
                field_key_prefix = "select" if is_select else "type"
                field_key = f"{field_key_prefix}:{_norm_key(selector)}"
                if memory.is_element_tested(page.url, field_key):
                    continue
            
            # Определяем тип поля и генерируем значение
            field_type = detect_field_type(
                input_type=field.get("type", ""),
                placeholder=field.get("placeholder", ""),
                name=field.get("name", ""),
                aria_label=field.get("ariaLabel", ""),
            )
            
            # Для SELECT элементов используем специальную функцию
            if is_select:
                # Выбираем первую доступную опцию
                options = field.get("options", [])
                if not options:
                    continue  # Пропускаем если нет опций
                value = options[0]
                result = _do_select_option(page, selector, value)
                if "selected" in (result or "").lower():
                    filled_count += 1
                    if memory:
                        memory.record_page_element(page.url, f"select:{_norm_key(selector)}")
            else:
                # Для обычных input/textarea используем _do_type
                value = get_test_value(field_type, form_strategy)
                result = _do_type(page, selector, value, form_strategy)
                if "typed" in (result or "").lower():
                    filled_count += 1
                    if memory:
                        memory.record_page_element(page.url, f"type:{_norm_key(selector)}")
            
            time.sleep(0.2)  # Небольшая пауза между полями
        
        if filled_count > 0:
            return f"form_filled: {filled_count} fields"
        return "form_fill_failed"
    except Exception as e:
        return f"form_fill_error: {e}"


def _do_type(page: Page, selector: str, value: str, form_strategy: str = "happy") -> str:
    """
    Улучшенный ввод в поле с валидацией, умным подбором значения и проверкой результата.
    """
    # Smart value: если value пустой — подобрать по типу поля и стратегии
    if not value and selector:
        field_type = detect_field_type(placeholder=selector, name=selector, aria_label=selector)
        value = get_test_value(field_type, form_strategy)
    if not selector or not value:
        return "no_selector_or_value"
    
    loc = _find_element(page, selector)
    if not loc:
        # Попробуем найти ближайший input / textarea по приоритету
        input_selectors = [
            "input[type='email']",  # email часто важнее
            "input[type='text']",
            "input[type='search']",
            "textarea",
            "input:not([type='hidden']):not([type='submit']):not([type='button'])",
        ]
        for inp_sel in input_selectors:
            try:
                loc = page.locator(inp_sel).first
                if loc.count() > 0 and loc.is_visible():
                    break
                loc = None
            except Exception:
                loc = None
    if loc:
        try:
            print(f"[Agent] ВВОД: {selector[:50]} = {value[:30]}")
            scroll_to_center(loc, page)
            loc.click()
            loc.fill(value)
            # Верификация: значение действительно попало в поле
            try:
                current_val = (loc.input_value() or "").strip()
                val_stripped = (value or "").strip()
                if val_stripped and current_val != val_stripped and val_stripped not in current_val:
                    return f"typed_but_value_mismatch: expected '{val_stripped[:30]}', got '{current_val[:30]}'"
            except Exception:
                pass
            print(f"[Agent] ✅ Ввод выполнен: {value[:30]}")
            
            # Проверка валидации: есть ли сообщение об ошибке после ввода?
            # Используем loc.evaluate() чтобы работать напрямую с найденным элементом
            try:
                # Проверяем наличие сообщений об ошибке рядом с полем
                validation_error = loc.evaluate("""(input) => {
                    if (!input) return null;
                    // Ищем сообщения об ошибке: aria-invalid, aria-describedby, .error, .invalid
                    if (input.getAttribute('aria-invalid') === 'true') {
                        const descId = input.getAttribute('aria-describedby');
                        if (descId) {
                            const desc = document.getElementById(descId);
                            if (desc) return desc.textContent.trim().slice(0, 100);
                        }
                    }
                    // Проверяем родительский контейнер на наличие .error, .invalid
                    let parent = input.parentElement;
                    for (let i = 0; i < 3 && parent; i++) {
                        const errorEl = parent.querySelector('.error, .invalid, [class*="error"], [class*="invalid"]');
                        if (errorEl && errorEl.textContent) {
                            return errorEl.textContent.trim().slice(0, 100);
                        }
                        parent = parent.parentElement;
                    }
                    return null;
                }""")
                
                if validation_error:
                    return f"typed_with_validation_error: {value[:30]} -> {validation_error[:50]}"
            except Exception:
                pass
            
            return f"typed: {value[:30]} into {selector[:30]}"
        except Exception as e:
            return f"type_error: {e}"
    return f"input_not_found: {selector[:50]}"


def _do_scroll(page: Page, direction: str) -> str:
    try:
        if direction.lower() in ("down", "вниз", ""):
            page.evaluate(f"window.scrollBy(0, {SCROLL_PIXELS})")
            return "scrolled_down"
        elif direction.lower() in ("up", "вверх"):
            page.evaluate(f"window.scrollBy(0, -{SCROLL_PIXELS})")
            return "scrolled_up"
        else:
            loc = _find_element(page, direction)
            if loc:
                loc.scroll_into_view_if_needed()
                safe_highlight(loc, page, 0.3)
                return f"scrolled_to: {direction[:30]}"
            page.evaluate(f"window.scrollBy(0, {SCROLL_PIXELS})")
            return "scrolled_down"
    except Exception as e:
        return f"scroll_error: {e}"


def _do_hover(page: Page, selector: str) -> str:
    if not selector:
        return "no_selector"
    loc = _find_element(page, selector)
    if loc:
        try:
            safe_highlight(loc, page, 0.3)
            loc.hover()
            time.sleep(1.0)  # Ждём появления тултипа/дропдауна после ховера
            return f"hovered: {selector[:50]}"
        except Exception as e:
            return f"hover_error: {e}"
    return f"not_found: {selector[:50]}"


def _do_close_modal(page: Page, selector: str = "") -> str:
    """
    Закрыть модалку / оверлей. Стратегии (по приоритету):
    1) Клик по переданному селектору (крестик закрытия)
    2) Поиск крестика закрытия по стандартным селекторам
    3) Нажатие Escape
    4) Клик по бэкдропу (за пределами модалки)
    """
    # Стратегия 1: переданный селектор
    if selector:
        loc = _find_element(page, selector)
        if loc:
            try:
                safe_highlight(loc, page, 0.3)
                highlight_and_click(loc, page, description="Закрываю")
                time.sleep(0.5)
                return f"modal_closed_by_selector: {selector[:40]}"
            except Exception:
                pass

    # Стратегия 2: стандартные кнопки закрытия
    close_selectors = [
        '[aria-label*="close" i]',
        '[aria-label*="закрыть" i]',
        '[aria-label*="Close" i]',
        'button.close',
        '.modal-close',
        '[data-dismiss="modal"]',
        '[data-bs-dismiss="modal"]',
        '[class*="close"][class*="button"]',
        '[class*="close"][class*="btn"]',
        '[class*="dialog"] [class*="close"]',
        '[class*="modal"] [class*="close"]',
        '[role="dialog"] button:has-text("×")',
        '[role="dialog"] button:has-text("✕")',
        '[role="dialog"] button:has-text("✖")',
        '[role="dialog"] button:has-text("Закрыть")',
        '[role="dialog"] button:has-text("Close")',
        '[role="dialog"] button:has-text("Отмена")',
        '[role="dialog"] button:has-text("Cancel")',
    ]
    for cs in close_selectors:
        try:
            loc = page.locator(cs).first
            if loc.count() > 0 and loc.is_visible():
                safe_highlight(loc, page, 0.3)
                highlight_and_click(loc, page, description="Закрываю")
                time.sleep(0.5)
                return f"modal_closed_by_standard: {cs[:40]}"
        except Exception:
            continue

    # Стратегия 3: Escape
    try:
        page.keyboard.press("Escape")
        time.sleep(0.5)
        return "modal_closed_by_escape"
    except Exception:
        pass

    # Стратегия 4: клик за пределами модалки (по backdrop)
    try:
        page.mouse.click(5, 5)
        time.sleep(0.5)
        return "modal_closed_by_backdrop_click"
    except Exception as e:
        return f"modal_close_failed: {e}"


def _do_select_option(page: Page, selector: str, value: str) -> str:
    """Выбрать опцию в дропдауне / select / listbox."""
    if not selector or not value:
        return "no_selector_or_value"

    # Стратегия 1: нативный <select>
    loc = _find_element(page, selector)
    if loc:
        try:
            scroll_to_center(loc, page)
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                loc.select_option(label=value)
                time.sleep(0.5)
                return f"selected_native: {value[:30]} in {selector[:30]}"
        except Exception:
            pass

    # Стратегия 2: кастомный дропдаун — кликнуть по пункту с текстом value
    try:
        option_selectors = [
            f'[role="option"]:has-text("{value}")',
            f'[role="menuitem"]:has-text("{value}")',
            f'li:has-text("{value}")',
            f'.dropdown-item:has-text("{value}")',
            f'[class*="option"]:has-text("{value}")',
            f'[class*="item"]:has-text("{value}")',
        ]
        for os_sel in option_selectors:
            try:
                opt = page.locator(os_sel).first
                if opt.count() > 0 and opt.is_visible():
                    safe_highlight(opt, page, 0.3)
                    highlight_and_click(opt, page, description=f"Выбираю: {value[:20]}")
                    time.sleep(0.5)
                    return f"selected_custom: {value[:30]}"
            except Exception:
                continue
    except Exception:
        pass

    return f"select_not_found: {selector[:30]} / {value[:30]}"


def _do_upload_file(page: Page, selector: str, file_path: str) -> str:
    """Загрузить файл в input[type=file] по селектору (ref:N или иной)."""
    if not file_path or not os.path.isfile(file_path):
        return f"upload_error: file not found {file_path[:50]}"
    loc = _find_element(page, selector)
    if not loc:
        return f"upload_error: element not_found {selector[:30]}"
    try:
        loc.set_input_files(file_path)
        return f"uploaded: {os.path.basename(file_path)[:40]}"
    except Exception as e:
        return f"upload_error: {e}"


def _do_press_key(page: Page, key: str) -> str:
    """Нажать клавишу (Escape, Enter, Tab и т.д.)."""
    try:
        page.keyboard.press(key)
        time.sleep(0.5)
        return f"key_pressed: {key}"
    except Exception as e:
        return f"key_error: {e}"


# --- Автологин ---
def _do_auth_login(page: Page, auth_url: str, username: str, password: str, submit_selector: str) -> bool:
    """Выполнить вход на auth_url (заполнить логин/пароль, нажать кнопку). Возвращает True при успехе."""
    if not auth_url or not username or not password:
        return False
    try:
        page.goto(auth_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_load_state("domcontentloaded", timeout=5000)
        # Ищем поля логина и пароля по type, name, placeholder
        login_sel = 'input[type="email"], input[type="text"]:not([type="search"]), input[name*="login" i], input[name*="user" i], input[name*="email" i], input[placeholder*="логин" i], input[placeholder*="email" i], input[id*="login" i], input[id*="email" i]'
        pass_sel = 'input[type="password"]'
        try:
            page.locator(login_sel).first.fill(username, timeout=5000)
            page.locator(pass_sel).first.fill(password, timeout=5000)
        except Exception:
            # Пробуем по первому text/email и второму password
            inputs = page.query_selector_all("input[type='text'], input[type='email'], input:not([type])")
            for inp in inputs:
                if inp.get_attribute("type") == "password":
                    continue
                inp.fill(username)
                break
            pw = page.query_selector("input[type='password']")
            if pw:
                pw.fill(password)
        # Кнопка отправки
        if submit_selector:
            try:
                page.locator(submit_selector).first.click(timeout=3000)
            except Exception:
                page.get_by_role("button", name=submit_selector).first.click(timeout=3000)
        else:
            page.locator('button[type="submit"], input[type="submit"], button:has-text("Войти"), button:has-text("Вход"), button:has-text("Login"), button:has-text("Sign in")').first.click(timeout=3000)
        time.sleep(2)
        print("[Agent] Автологин выполнен")
        return True
    except Exception as e:
        LOG.warning("Автологин не удался: %s", e)
        return False


# --- Cookie/баннер согласия ---
def try_accept_cookie_banner(page: Page) -> bool:
    """Если на странице баннер cookies/согласия — кликнуть по кнопке принять. Возвращает True если кликнули."""
    try:
        info = detect_cookie_banner(page)
        if not info or not info.get("text"):
            return False
        text = info.get("text", "").strip()
        if not text:
            return False
        loc = _find_element(page, text)
        if loc:
            safe_highlight(loc, page, 0.3)
            highlight_and_click(loc, page, description="Принять")
            time.sleep(1.0)
            print(f"[Agent] Закрыт баннер: {text[:50]}")
            return True
    except Exception as e:
        print(f"[Agent] Ошибка закрытия баннера: {e}")
    return False


# --- Test spec YAML (сценарии до автономного прохода) ---
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
    for scenario in scenarios:
        steps = scenario.get("steps") or scenario.get("step") or []
        if isinstance(steps, dict):
            steps = [steps]
        name = scenario.get("name", "")
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
                        LOG.warning("test_spec navigate %s: %s", url[:50], e)
            elif action == "click":
                sel = (step.get("selector") or step.get("element") or "").strip()
                if sel:
                    loc = _find_element(page, sel)
                    if loc:
                        try:
                            loc.click(timeout=5000)
                            time.sleep(0.5)
                        except Exception as e:
                            LOG.warning("test_spec click %s: %s", sel[:30], e)
            elif action == "type":
                sel = (step.get("selector") or step.get("element") or "").strip()
                val = (step.get("value") or step.get("text") or "").strip()
                if sel and val is not None:
                    loc = _find_element(page, sel)
                    if loc:
                        try:
                            loc.fill(val, timeout=5000)
                            time.sleep(0.3)
                        except Exception as e:
                            LOG.warning("test_spec type %s: %s", sel[:30], e)
        if name:
            print(f"[Agent] Test spec сценарий выполнен: {name[:50]}")


# --- Инициализация страницы ---
def _inject_all(page: Page):
    """Инжектировать все визуальные элементы."""
    inject_cursor(page)
    inject_llm_overlay(page)


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
    - Если загрузка успешна → лог, скриншот для визуала, закрыть вкладку
    - Если загрузка неуспешна (таймаут, краш, ошибка) → завести дефект, закрыть вкладку
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
                # Загрузка неуспешна → дефект
                bug_desc = f"Ссылка открыла новую вкладку с ошибкой.\nURL: {tab_url}\nTitle: {title}\nОшибки JS: {', '.join(tab_errors[:3])}"
                print(f"[Agent] #{step} Новая вкладка: ОШИБКА → дефект. URL: {tab_url[:60]}")
                update_llm_overlay(main_page, prompt=f"Новая вкладка: ошибка!", response=bug_desc[:200], loading=False)
                _create_defect(main_page, bug_desc, tab_url, [], console_log, network_failures, memory)
                memory.add_action({"action": "new_tab_error", "selector": tab_url}, result="defect_reported")
            else:
                # Загрузка успешна
                load_ok = True
                print(f"[Agent] #{step} Новая вкладка OK: {tab_url[:60]} → закрываю")
                memory.add_action({"action": "new_tab_ok", "selector": tab_url}, result=f"tab_loaded: {title[:40]}")

        except Exception as e:
            # Таймаут загрузки или краш → дефект
            try:
                tab_url = new_tab.url or tab_url
            except Exception:
                pass
            bug_desc = f"Новая вкладка не загрузилась (таймаут/ошибка).\nURL: {tab_url}\nОшибка: {str(e)[:200]}"
            print(f"[Agent] #{step} Новая вкладка: ТАЙМАУТ/КРАШ → дефект. URL: {tab_url[:60]}")
            update_llm_overlay(main_page, prompt="Новая вкладка: не загрузилась!", response=bug_desc[:200], loading=False)
            _create_defect(main_page, bug_desc, tab_url, [], console_log, network_failures, memory)
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


# --- Основной цикл ---
def run_agent(start_url: str = None):
    """
    Запуск умного агента. Многофазный цикл:
    Phase 1: Скриншот + контекст → GigaChat (что делать?)
    Phase 2: Выполнение действия
    Phase 3: Скриншот после действия → GigaChat (анализ)
    Phase 4: Если дефект → Jira с фактурой
    """
    global _current_agent_memory
    start_url = start_url or START_URL
    if not start_url.startswith("http"):
        start_url = "https://" + start_url

    console_log: List[Dict[str, Any]] = []
    network_failures: List[Dict[str, Any]] = []
    memory = AgentMemory()
    reset_session_defects()  # сбросить локальный кеш дефектов

    # Инициализация соединения с GigaChat до запуска браузера
    if not init_gigachat_connection():
        print("[Agent] GigaChat недоступен. Проверьте настройки (токен, URL). Браузер не запускается.")
        return {"defects": 0, "steps": 0, "error": "GigaChat недоступен"}

    print("[Agent] GigaChat готов. Запуск браузера…")
    result = {"defects": 0, "steps": 0, "error": None}

    with sync_playwright() as p:
        browser = None
        engine = getattr(p, BROWSER_ENGINE, p.chromium)
        # Аргументы Chromium: подавить диалог выбора сертификата в headless/CI.
        use_chromium = BROWSER_ENGINE == "chromium" or bool(BROWSER_USER_DATA_DIR)
        chromium_args = list(BROWSER_CHROMIUM_ARGS)
        if use_chromium and BROWSER_SUPPRESS_CERT_PROMPT:
            chromium_args.append("--ignore-certificate-errors")
            if sys.platform == "darwin":
                chromium_args.append("--use-mock-keychain")
        launch_kw = {"headless": HEADLESS, "slow_mo": BROWSER_SLOW_MO}
        if use_chromium and chromium_args:
            launch_kw["args"] = chromium_args

        # Клиентский сертификат: один и тот же сертификат для всех origin (браузер подставляет сам).
        origins = ([BROWSER_CLIENT_CERT_ORIGIN] if BROWSER_CLIENT_CERT_ORIGIN else []) + list(BROWSER_CLIENT_CERT_ORIGINS)
        origins = [o for o in origins if o]
        client_certs = []
        if origins:
            if BROWSER_CLIENT_CERT_CERT_PATH and BROWSER_CLIENT_CERT_KEY_PATH:
                cert_path = os.path.abspath(BROWSER_CLIENT_CERT_CERT_PATH)
                key_path = os.path.abspath(BROWSER_CLIENT_CERT_KEY_PATH)
                if os.path.isfile(cert_path) and os.path.isfile(key_path):
                    for origin in origins:
                        client_certs.append({"origin": origin, "certPath": cert_path, "keyPath": key_path})
            elif BROWSER_CLIENT_CERT_PFX_PATH and os.path.isfile(BROWSER_CLIENT_CERT_PFX_PATH):
                pfx_path = os.path.abspath(BROWSER_CLIENT_CERT_PFX_PATH)
                for origin in origins:
                    entry = {"origin": origin, "pfxPath": pfx_path}
                    if BROWSER_CLIENT_CERT_PASSPHRASE:
                        entry["passphrase"] = BROWSER_CLIENT_CERT_PASSPHRASE
                    client_certs.append(entry)
        # Политика авто-выбора сертификата по URL (без файла сертификата): пишем в профиль при persistent context.
        if BROWSER_USER_DATA_DIR and BROWSER_AUTO_SELECT_CERT_PATTERNS and use_chromium:
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
        try:
            page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            smart_wait_after_goto(page, timeout=15000)
            _inject_all(page)
        except Exception as e:
            print(f"[Agent] Ошибка загрузки {start_url}: {e}")
            if browser:
                browser.close()
            else:
                context.close()
            result["error"] = str(e)[:500]
            return result

        memory.session_start = datetime.now()
        memory.set_start_url_for_nav(start_url)
        # Закрыть баннер cookies/согласия, если есть
        if try_accept_cookie_banner(page):
            time.sleep(1.5)
            smart_wait_after_goto(page, timeout=3000)

        # Спецификация теста (YAML): выполнить сценарии до автономного прохода
        if TEST_SPEC_YAML_PATH:
            get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
            _run_test_spec_yaml(page, memory, TEST_SPEC_YAML_PATH)
            time.sleep(1)

        # Тест-план в начале сессии (GigaChat по скриншоту предлагает 5–7 шагов)
        if ENABLE_TEST_PLAN_START:
            plan_screenshot = take_screenshot_b64(page)
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
        if MAX_STEPS > 0:
            print(f"[Agent] Лимит: {MAX_STEPS} шагов.")
        else:
            print(f"[Agent] Бесконечный цикл. Ctrl+C для остановки.")

        # ========== PIPELINE: Асинхронный GigaChat + мгновенные действия ==========
        # GigaChat работает в фоне. Пока ждём ответ — агент кликает по ref-id.
        # Когда GigaChat отвечает — берём его действие следующим.
        _gigachat_future: Optional[Future] = None
        _gigachat_future_started_at: float = 0.0
        _gigachat_action: Optional[Dict[str, Any]] = None
        _gigachat_meta: Dict[str, Any] = {}  # has_overlay, screenshot_b64
        _gigachat_circuit_open_until: float = 0.0  # Circuit breaker: не вызывать GigaChat до этого времени
        _gigachat_consecutive_timeouts: int = 0

        def _start_gigachat_async(page_, step_, memory_, console_log_, network_failures_, checklist_results_, context_):
            """Запустить GigaChat в фоновом потоке. Возвращает Future."""
            nonlocal _gigachat_future
            # Проверка: страница закрыта — не запускаем GigaChat
            if page_.is_closed():
                return
            
            # Собираем всё что нужно ДО отправки в фон (Playwright — только main thread)
            dom_max = 5000
            history_n = 15
            
            try:
                overlay_info = detect_active_overlays(page_)
                has_overlay = overlay_info.get("has_overlay", False)
                screenshot_b64 = take_screenshot_b64(page_)
                screenshot_changed = memory_.is_screenshot_changed(screenshot_b64 or "")
                current_url_ = page_.url
                dom_summary = get_dom_summary(page_, max_length=dom_max, include_shadow_dom=ENABLE_SHADOW_DOM)
                history_text = memory_.get_history_text(last_n=history_n)
                overlay_context = format_overlays_context(overlay_info)
                page_type = detect_page_type(page_)
            except Exception as e:
                # Страница закрылась во время сбора данных
                LOG.debug("_start_gigachat_async: страница закрыта во время сбора данных: %s", e)
                return
            coverage_hint = ""
            if current_url_ in memory_._page_coverage:
                tested_count = len(memory_._page_coverage[current_url_])
                if tested_count > 0:
                    coverage_hint = f"\nПротестировано: {tested_count}. Выбери НОВЫЙ элемент.\n"

            _gigachat_meta["has_overlay"] = has_overlay
            _gigachat_meta["screenshot_b64"] = screenshot_b64

            # Формируем контекст и вопрос
            ctx = build_context(page_, current_url_, console_log_, network_failures_)
            if checklist_results_:
                ctx = checklist_results_to_context(checklist_results_) + "\n\n" + ctx
            if overlay_context:
                ctx = overlay_context + "\n\n" + ctx

            type_strategies = {
                "landing": "Landing page: CTA, формы", "form": "Form: заполни поля",
                "dashboard": "Dashboard: таблицы, фильтры", "catalog": "Catalog: карточки, фильтры",
            }
            ptype_hint = f"\nТип: {page_type}. {type_strategies.get(page_type, '')}\n" if page_type != "unknown" else ""

            module_ctx = memory_.get_module_context_text()

            if has_overlay:
                question = f"""Скриншот. АКТИВНЫЙ ОВЕРЛЕЙ.
{overlay_context}
{module_ctx}
ЭЛЕМЕНТЫ: {dom_summary[:2500]}
{history_text}
Используй selector="ref:N". Тестируй оверлей или закрой (close_modal)."""
            else:
                plan_hint = ""
                if memory_.test_plan:
                    plan_hint = memory_.get_test_plan_progress() + "\n"
                critical_hint = ""
                if CRITICAL_FLOW_STEPS:
                    critical_hint = f"\nКритический сценарий (сделай в первую очередь): {', '.join(CRITICAL_FLOW_STEPS[:5])}.\n"
                stuck_w = "\n🚨 ЗАЦИКЛИВАНИЕ! Выбери НОВЫЙ элемент!\n" if memory_.is_stuck() else ""
                question = f"""Скриншот и контекст.
{module_ctx}
{ptype_hint}{coverage_hint}{critical_hint}
ЭЛЕМЕНТЫ СТРАНИЦЫ (только видимые на экране, формат: [N] тип "текст" атрибуты):
{dom_summary[:2500]}
{history_text}
{plan_hint}{stuck_w}
Используй selector="ref:N". Выбери КОНКРЕТНОЕ действие в рамках текущего модуля."""

            phase_instruction = memory_.get_phase_instruction()
            send_screenshot = screenshot_b64 if screenshot_changed else None

            def _call_gigachat():
                raw = consult_agent_with_screenshot(
                    ctx, question, screenshot_b64=send_screenshot,
                    phase_instruction=phase_instruction, tester_phase=memory_.tester_phase,
                    has_overlay=has_overlay,
                )
                if raw:
                    action = parse_llm_action(raw)
                    if action:
                        return validate_llm_action(action)
                    # Один retry с запросом только валидного JSON
                    retry_q = "Ответь ТОЛЬКО валидным JSON с полями action, selector, value, reason, test_goal, expected_outcome. Без markdown и пояснений."
                    retry_raw = consult_agent_with_screenshot(
                        ctx, retry_q, screenshot_b64=send_screenshot,
                        phase_instruction=phase_instruction, tester_phase=memory_.tester_phase,
                        has_overlay=has_overlay,
                    )
                    if retry_raw:
                        action = parse_llm_action(retry_raw)
                        if action:
                            return validate_llm_action(action)
                return None

            nonlocal _gigachat_future_started_at
            _gigachat_future_started_at = time.time()
            _gigachat_future = _bg_submit(_call_gigachat)

        def _poll_gigachat() -> Optional[Dict[str, Any]]:
            """Проверить готов ли GigaChat (не блокирует). При таймауте — отменить и вернуть None."""
            nonlocal _gigachat_future, _gigachat_action, _gigachat_future_started_at, _gigachat_consecutive_timeouts, _gigachat_circuit_open_until
            if _gigachat_future is None:
                return _gigachat_action
            if _gigachat_future.done():
                try:
                    result = _gigachat_future.result(timeout=0)
                    _gigachat_action = result
                    if result is not None:
                        _gigachat_consecutive_timeouts = 0  # успех — сброс счётчика
                except Exception:
                    _gigachat_action = None
                _gigachat_future = None
                return _gigachat_action
            if GIGACHAT_RESPONSE_TIMEOUT_SEC > 0 and (time.time() - _gigachat_future_started_at) > GIGACHAT_RESPONSE_TIMEOUT_SEC:
                try:
                    _gigachat_future.cancel()
                except Exception:
                    pass
                _gigachat_future = None
                if GIGACHAT_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS > 0:
                    _gigachat_consecutive_timeouts += 1
                    if _gigachat_consecutive_timeouts >= GIGACHAT_CIRCUIT_BREAKER_AFTER_N_TIMEOUTS:
                        _gigachat_circuit_open_until = time.time() + GIGACHAT_CIRCUIT_BREAKER_COOLDOWN_SEC
                        print(f"[Agent] Circuit breaker: GigaChat не отвечает {_gigachat_consecutive_timeouts} раз подряд. Только fast action следующие {GIGACHAT_CIRCUIT_BREAKER_COOLDOWN_SEC} сек.")
                return None
            return None  # ещё думает

        try:
            while True:
                memory.iteration += 1
                step = memory.iteration
                memory.defects_on_current_step = 0
                _current_agent_memory = memory

                if MAX_STEPS > 0 and step > MAX_STEPS:
                    print(f"[Agent] Лимит {MAX_STEPS} шагов. Завершаю.")
                    break

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
                    pass

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

                # ========== ВЫБОР ДЕЙСТВИЯ: GigaChat (если готов) или быстрое локальное ==========
                # Проверка: страница закрыта — выходим из цикла
                if page.is_closed():
                    print(f"[Agent] #{step} Страница закрыта. Завершаю.")
                    break
                
                try:
                    overlay_info_fast = detect_active_overlays(page)
                    has_overlay = overlay_info_fast.get("has_overlay", False)
                except Exception as e:
                    LOG.debug("detect_active_overlays: страница закрыта: %s", e)
                    break

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

                # Ref-id для быстрого выбора (и для GigaChat)
                if not page.is_closed():
                    try:
                        get_dom_summary(page, max_length=4000, include_shadow_dom=ENABLE_SHADOW_DOM)
                    except Exception:
                        pass

                gc_action = _poll_gigachat()

                if gc_action is not None:
                    action = gc_action
                    _gigachat_action = None
                    has_overlay = _gigachat_meta.get("has_overlay", has_overlay)
                    screenshot_b64 = _gigachat_meta.get("screenshot_b64")
                    source = "GigaChat"
                else:
                    action = _get_fast_action(page, memory, has_overlay)
                    screenshot_b64 = None
                    source = "Fast"

                # Обогащаем action stable_key + url_pattern для надёжной памяти
                enrich_action(page, memory, action)

                # Circuit breaker: не вызывать GigaChat пока открыт контур
                if _gigachat_future is None and not page.is_closed():
                    if time.time() < _gigachat_circuit_open_until:
                        pass  # только fast action
                    else:
                        try:
                            _start_gigachat_async(page, step, memory, console_log, network_failures, checklist_results, context)
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
                    break
                
                try:
                    result = _step_execute(page, action, step, memory, context)
                except Exception as e:
                    if "closed" in str(e).lower() or "Target page" in str(e):
                        print(f"[Agent] #{step} Страница закрыта во время выполнения: {e}")
                        break
                    raise

                # Success/failure tracking
                if "error" in (result or "").lower() or "not_found" in (result or "").lower():
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
                    "result": (result or "")[:200],
                    "source": source,
                    "screenshot_path": screenshot_path_rel,
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
                            _bg_submit(_check_broken_links_bg, urls_to_check[:50], memory)  # не более 50 за раз
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

                # Пост-анализ ОТКЛЮЧЕН — агент должен активно кликать
                # Периодические проверки ОТКЛЮЧЕНЫ — только клики!

                if SESSION_REPORT_EVERY_N > 0 and step % SESSION_REPORT_EVERY_N == 0:
                    report = memory.get_session_report_text()
                    print(report)

                # Сохранять отчёт после каждого шага
                if SESSION_REPORT_SAVE_EVERY_N > 0 and step >= 1:
                    _save_report_now(step, f"конец шага {step}")

                time.sleep(0.3)

        except KeyboardInterrupt:
            print("\n[Agent] Остановлен по Ctrl+C.")
        finally:
            # Отменить фоновые задачи GigaChat
            if '_gigachat_future' in locals() and _gigachat_future is not None:
                try:
                    _gigachat_future.cancel()
                except Exception:
                    pass
            
            # Дождаться фоновых задач (если страница ещё жива)
            try:
                if not page.is_closed():
                    _flush_pending_analysis(page, memory, console_log, network_failures)
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

            # wait=True — гарантируем, что воркеры успели завершить отправки.
            _shutdown_bg_pool(wait=True)
            
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
                    from src.playwright_export import build_playwright_script
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

            result["defects"] = len(getattr(memory, "defects_created", []))
            result["steps"] = getattr(memory, "iteration", 0)

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

    return result


def _collect_browser_metrics(page: Page, memory: AgentMemory, step: int) -> None:
    """Собрать метрики: загрузка страницы, ресурсы по типам, время отклика, память."""
    try:
        url = page.url[:200] if page.url else ""
        metrics = page.evaluate("""() => {
            const out = { url: window.location.href ? window.location.href.slice(0, 200) : '' };
            out.page = {};
            out.resources = {};
            out.response = {};
            try {
                const t = performance.timing || {};
                const nav = performance.getEntriesByType('navigation')[0];
                const toMs = (a, b) => (a > 0 && b >= 0) ? Math.round(a - b) : null;
                if (nav) {
                    out.page.ttfb = toMs(nav.responseStart, nav.fetchStart);
                    out.page.domInteractive = toMs(nav.domInteractive, nav.fetchStart);
                    out.page.domContentLoaded = toMs(nav.domContentLoadedEventEnd, nav.fetchStart);
                    out.page.loadComplete = toMs(nav.loadEventEnd, nav.fetchStart);
                    out.page.domComplete = toMs(nav.domComplete, nav.fetchStart);
                    out.loadEventEnd = out.page.loadComplete;
                    out.domContentLoaded = out.page.domContentLoaded;
                    out.domComplete = out.page.domComplete;
                    out.responseStart = out.page.ttfb;
                } else if (t.loadEventEnd) {
                    const start = t.navigationStart;
                    out.page.ttfb = t.responseStart - start;
                    out.page.domInteractive = t.domInteractive - start;
                    out.page.domContentLoaded = t.domContentLoadedEventEnd - start;
                    out.page.loadComplete = t.loadEventEnd - start;
                    out.page.domComplete = t.domComplete - start;
                    out.loadEventEnd = out.page.loadComplete;
                    out.domContentLoaded = out.page.domContentLoaded;
                    out.domComplete = out.page.domComplete;
                    out.responseStart = out.page.ttfb;
                }
                const paint = performance.getEntriesByType('paint');
                paint.forEach(p => {
                    if (p.name === 'first-paint') out.page.firstPaint = Math.round(p.startTime);
                    if (p.name === 'first-contentful-paint') out.page.firstContentfulPaint = Math.round(p.startTime);
                });
            } catch (e) {}
            try {
                out.scrollHeight = document.documentElement ? document.documentElement.scrollHeight : 0;
                out.scrollWidth = document.documentElement ? document.documentElement.scrollWidth : 0;
                out.bodyChildren = document.body ? document.body.childElementCount : 0;
                out.readyState = document.readyState || '';
            } catch (e) {}
            try {
                const resources = performance.getEntriesByType('resource');
                const byType = {};
                let xhrFetch = [];
                resources.forEach(r => {
                    const type = (r.initiatorType || 'other').toLowerCase();
                    if (!byType[type]) byType[type] = { count: 0, durationSum: 0, durationMax: 0, transferSum: 0, items: [] };
                    const d = Math.round(r.duration || 0);
                    const sz = r.transferSize || 0;
                    byType[type].count++;
                    byType[type].durationSum += d;
                    byType[type].durationMax = Math.max(byType[type].durationMax, d);
                    byType[type].transferSum += sz;
                    if (d > 0) byType[type].items.push({ n: (r.name || '').slice(-80), d, sz });
                    if ((type === 'xmlhttprequest' || type === 'fetch') && r.responseStart > 0) {
                        const resp = Math.round((r.responseEnd || r.startTime) - r.responseStart);
                        xhrFetch.push({ n: (r.name || '').slice(-60), ms: resp });
                    }
                });
                Object.keys(byType).forEach(k => {
                    const x = byType[k];
                    x.avgDuration = x.count ? Math.round(x.durationSum / x.count) : 0;
                    x.slowest = x.items.sort((a, b) => b.d - a.d).slice(0, 3).map(i => ({ name: i.n, duration: i.d, size: i.sz }));
                });
                out.resources = byType;
                xhrFetch.sort((a, b) => b.ms - a.ms);
                out.response.xhrFetch = xhrFetch.slice(0, 10);
                if (xhrFetch.length) {
                    out.response.avgMs = Math.round(xhrFetch.reduce((s, i) => s + i.ms, 0) / xhrFetch.length);
                    out.response.maxMs = xhrFetch[0] ? xhrFetch[0].ms : 0;
                }
                const lcp = performance.getEntriesByType('largest-contentful-paint');
                if (lcp.length) out.page.lcp = Math.round(lcp[lcp.length - 1].startTime);
            } catch (e) {}
            try {
                if (performance.memory) {
                    out.usedJSHeapSize = performance.memory.usedJSHeapSize;
                    out.totalJSHeapSize = performance.memory.totalJSHeapSize;
                }
            } catch (e) {}
            return out;
        }""")
        if isinstance(metrics, dict):
            metrics["step"] = step
            metrics["url"] = metrics.get("url") or url
            memory._browser_metrics_latest = metrics
            memory._browser_metrics_history.append(dict(metrics))
            if len(memory._browser_metrics_history) > 50:
                memory._browser_metrics_history.pop(0)
    except Exception as e:
        LOG.debug("collect_browser_metrics: %s", e)


def _write_junit_report(memory: AgentMemory, path: str) -> None:
    """Записать отчёт в формате JUnit XML."""
    step_log = getattr(memory, "_step_log", None) or []
    failures = sum(1 for e in step_log if "error" in (e.get("result") or "").lower() or "not_found" in (e.get("result") or "").lower())
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    duration_sec = 0.0
    if getattr(memory, "session_start", None):
        duration_sec = (datetime.now() - memory.session_start).total_seconds()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="Kventin" tests="{len(step_log)}" failures="{failures}" errors="0" skipped="0" time="{duration_sec:.2f}" timestamp="{ts}">',
    ]
    for e in step_log:
        step = e.get("step", 0)
        result = (e.get("result") or "")
        fail = "error" in result.lower() or "not_found" in result.lower()
        name = html_module.escape(f"step_{step}_{e.get('action', '')}")
        res_esc = html_module.escape(result[:500])
        if fail:
            lines.append(f'  <testcase name="{name}"><failure message="{res_esc}"/></testcase>')
        else:
            lines.append(f'  <testcase name="{name}"><system-out>{res_esc}</system-out></testcase>')
    lines.append("</testsuite>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _build_html_report(memory: AgentMemory, report_text: str, start_url: str = "", video_dir: str = "") -> str:
    """Собрать красивый HTML-отчёт сессии."""
    def esc(s: str) -> str:
        return html_module.escape(str(s) if s else "", quote=True)

    if not memory.session_start:
        duration_sec = 0
    else:
        duration_sec = (datetime.now() - memory.session_start).total_seconds()
    step_log = getattr(memory, "_step_log", None) or []
    defects = getattr(memory, "defects_created", None) or []
    coverage = ", ".join(str(z) for z in memory.coverage_zones) if memory.coverage_zones else "—"

    steps_rows = []
    for e in step_log:
        sp = e.get("screenshot_path") or ""
        img_cell = ""
        if sp and not os.path.isabs(sp) and "screenshots/" in sp:
            img_cell = f'<a href="{esc(sp)}" target="_blank"><img src="{esc(sp)}" alt="шаг" class="step-thumb"/></a>'
        fok, ftot = e.get("flakiness_ok"), e.get("flakiness_total")
        flak_cell = f"{fok}/{ftot}" if (fok is not None and ftot) else "—"
        step_num = e.get("step", "")
        steps_rows.append(
            f"<tr id=\"step-{esc(str(step_num))}\"><td>{step_num}</td><td class=\"url\">{esc((e.get('url') or '')[:80])}</td>"
            f"<td><span class=\"act act-{esc(e.get('action', ''))}\">{esc(e.get('action', ''))}</span></td>"
            f"<td class=\"sel\">{esc((e.get('selector') or '')[:50])}</td>"
            f"<td class=\"result\">{esc((e.get('result') or '')[:80])}</td>"
            f"<td><span class=\"src src-{esc(e.get('source', ''))}\">{esc(e.get('source', ''))}</span></td>"
            f"<td class=\"sub\">{flak_cell}</td><td class=\"thumb\">{img_cell}</td></tr>"
        )
    steps_body = "\n".join(steps_rows) if steps_rows else "<tr><td colspan=\"8\">Нет данных</td></tr>"

    defects_rows = []
    for d in defects:
        key = d.get("key", "")
        summary = d.get("summary", "")[:120]
        sev = d.get("severity", "major")
        defects_rows.append(f"<tr><td class=\"key\">{esc(key)}</td><td><span class=\"sev sev-{esc(sev)}\">{esc(sev)}</span></td><td>{esc(summary)}</td></tr>")
    defects_body = "\n".join(defects_rows) if defects_rows else "<tr><td colspan=\"3\">Нет</td></tr>"

    nav_graph = getattr(memory, "_nav_graph", None) or []
    nav_rows = []
    for edge in nav_graph[-100:]:
        frm = (edge.get("from_url") or "")[:60]
        to = (edge.get("to_url") or "")[:60]
        step = edge.get("step", "")
        nav_rows.append(f"<tr><td>{step}</td><td class=\"url\">{esc(frm)}</td><td class=\"url\">→ {esc(to)}</td></tr>")
    nav_body = "\n".join(nav_rows) if nav_rows else "<tr><td colspan=\"3\">Нет переходов</td></tr>"

    broken_links = getattr(memory, "_broken_links", None) or []
    broken_rows = []
    for b in broken_links[-80:]:
        url_short = (b.get("url") or "")[:100]
        status = b.get("status") or ""
        err = (b.get("error") or "")[:80]
        broken_rows.append(f"<tr><td class=\"url\">{esc(url_short)}</td><td>{status}</td><td class=\"result\">{esc(err)}</td></tr>")
    broken_body = "\n".join(broken_rows) if broken_rows else "<tr><td colspan=\"3\">Нет</td></tr>"
    broken_section = (
        "<section><h2>Битые ссылки</h2><table><thead><tr><th>URL</th><th>Статус</th><th>Ошибка</th></tr></thead>"
        "<tbody>" + broken_body + "</tbody></table></section>"
    ) if broken_rows else ""

    console_warnings = getattr(memory, "_session_console_warnings", None) or []
    console_errors = [c for c in console_warnings if (c.get("type") or "").lower() == "error"]
    cw_rows = []
    for c in console_errors[-50:]:
        cw_rows.append(f"<tr><td><span class=\"sev sev-{esc(c.get('type', 'error'))}\">{esc(c.get('type', ''))}</span></td><td class=\"result\">{esc((c.get('text') or '')[:150])}</td></tr>")
    cw_body = "\n".join(cw_rows) if cw_rows else "<tr><td colspan=\"2\">Нет ошибок</td></tr>"
    console_section = (
        "<section><h2>Консоль (ошибки)</h2><table><thead><tr><th>Тип</th><th>Текст</th></tr></thead>"
        "<tbody>" + cw_body + "</tbody></table></section>"
    ) if cw_rows else ""

    mixed_content = getattr(memory, "_mixed_content", None) or []
    mc_body = "<br/>".join(esc((m.get("url") or "")[:80]) for m in mixed_content[-20:]) if mixed_content else "—"
    ws_issues = getattr(memory, "_websocket_issues", None) or []
    ws_body = "<br/>".join(f"{esc((w.get('url') or '')[:60])} ({w.get('event', '')})" for w in ws_issues[-20:]) if ws_issues else "—"
    mixed_section = (
        "<section><h2>Mixed content / WebSocket</h2>"
        f"<p><strong>Mixed content:</strong> {mc_body}</p><p><strong>WebSocket:</strong> {ws_body}</p></section>"
    ) if (mixed_content or ws_issues) else ""
    api_log = getattr(memory, "_api_log", None) or []
    def _status_code(x):
        try:
            return int(x.get("status") or 0)
        except (TypeError, ValueError):
            return 0
    api_failed = [a for a in api_log if _status_code(a) >= 400 or not a.get("ok", True)]
    api_rows = []
    for a in api_failed[-50:]:
        method = a.get("method", "")
        url_short = (a.get("url") or "")[:80]
        status = a.get("status", "") or ("—" if not a.get("ok", True) else "")
        cls = "sev sev-major"
        api_rows.append(f"<tr><td>{esc(method)}</td><td class=\"url\">{esc(url_short)}</td><td class=\"{cls}\">{esc(str(status))}</td></tr>")
    api_body = "\n".join(api_rows) if api_rows else "<tr><td colspan=\"3\">Нет запросов с ошибками</td></tr>"
    api_section = (
        "<section><details class=\"api-section\" id=\"api-section\">"
        f"<summary><h2 class=\"api-section-title\">API (XHR/fetch) — только ошибки</h2><span class=\"sub\">{len(api_failed)} запросов с ошибками (4xx, 5xx)</span></summary>"
        "<table class=\"api-table\"><thead><tr><th>Метод</th><th>URL</th><th>Статус</th></tr></thead>"
        "<tbody>" + api_body + "</tbody></table></details></section>"
    ) if api_failed else ""
    visual_regressions = getattr(memory, "_visual_regressions", None) or []
    vr_rows = []
    for v in visual_regressions:
        vr_rows.append(f"<tr><td class=\"url\">{esc((v.get('url') or '')[:80])}</td><td>{v.get('change_percent', 0)}%</td><td class=\"result\">{esc((v.get('detail') or '')[:100])}</td></tr>")
    vr_body = "\n".join(vr_rows) if vr_rows else "<tr><td colspan=\"3\">Нет</td></tr>"
    vr_section = (
        "<section><h2>Visual regression (baseline)</h2><table><thead><tr><th>URL</th><th>Изменение %</th><th>Детали</th></tr></thead>"
        "<tbody>" + vr_body + "</tbody></table></section>"
    ) if vr_rows else ""

    browser_metrics = getattr(memory, "_browser_metrics_latest", None) or {}
    metrics_rows = []
    if browser_metrics:
        m = browser_metrics
        page = m.get("page") or {}
        for key, label in [
            ("ttfb", "TTFB (время до первого байта)"),
            ("domInteractive", "DOM interactive"),
            ("domContentLoaded", "DOM Content Loaded"),
            ("loadComplete", "Полная загрузка (load)"),
            ("firstPaint", "First Paint"),
            ("firstContentfulPaint", "First Contentful Paint"),
            ("lcp", "LCP (Largest Contentful Paint)"),
        ]:
            val = page.get(key)
            if val is not None:
                metrics_rows.append(f"<tr><td>{esc(label)}</td><td>{val} мс</td></tr>")
        res = m.get("resources") or {}
        if res:
            metrics_rows.append("<tr><td colspan=\"2\"><strong>Ресурсы по типам</strong></td></tr>")
            for rtype, data in sorted(res.items(), key=lambda x: (str(x[0]),)):
                count = data.get("count", 0)
                avg = data.get("avgDuration")
                mx = data.get("durationMax")
                kb = (data.get("transferSum") or 0) / 1024
                metrics_rows.append(
                    f"<tr><td class=\"sub\">{esc(rtype)}</td><td>n={count}, avg={avg or '—'} мс, max={mx or '—'} мс, {kb:.0f} КБ</td></tr>"
                )
                for s in (data.get("slowest") or [])[:2]:
                    metrics_rows.append(f"<tr><td></td><td class=\"sub\">↳ {esc(str(s.get('duration', 0)))} мс {esc((s.get('name') or '')[-50:])}</td></tr>")
        resp = m.get("response") or {}
        if resp.get("xhrFetch"):
            metrics_rows.append("<tr><td colspan=\"2\"><strong>XHR/fetch отклик</strong></td></tr>")
            if resp.get("avgMs") is not None:
                metrics_rows.append(f"<tr><td>Среднее / макс</td><td>{resp['avgMs']} / {resp.get('maxMs', '—')} мс</td></tr>")
            for x in (resp.get("xhrFetch") or [])[:3]:
                metrics_rows.append(f"<tr><td></td><td class=\"sub\">↳ {x.get('ms', 0)} мс {esc((x.get('n') or '')[-40:])}</td></tr>")
        if m.get("scrollHeight") is not None:
            metrics_rows.append(f"<tr><td>scrollHeight / scrollWidth</td><td>{m.get('scrollHeight')} / {m.get('scrollWidth', '—')}</td></tr>")
        if m.get("bodyChildren") is not None:
            metrics_rows.append(f"<tr><td>body child elements</td><td>{m['bodyChildren']}</td></tr>")
        if m.get("usedJSHeapSize") is not None:
            used_mb = round(m["usedJSHeapSize"] / 1024 / 1024, 2)
            total_mb = round(m.get("totalJSHeapSize", 0) / 1024 / 1024, 2)
            metrics_rows.append(f"<tr><td>JS heap</td><td>{used_mb} / {total_mb} МБ</td></tr>")
        if m.get("readyState"):
            metrics_rows.append(f"<tr><td>readyState</td><td>{esc(m['readyState'])}</td></tr>")
    metrics_body = "\n".join(metrics_rows) if metrics_rows else "<tr><td colspan=\"2\">Не собраны (открой отчёт после шага с загруженной страницей)</td></tr>"

    # Карточки метрик для сводки (красивое оформление)
    summary_metrics_cards = []
    if browser_metrics:
        page = browser_metrics.get("page") or {}
        for key, label in [
            ("ttfb", "TTFB"), ("domContentLoaded", "DCL"), ("loadComplete", "Load"),
            ("firstContentfulPaint", "FCP"), ("lcp", "LCP"),
        ]:
            val = page.get(key)
            if val is not None:
                summary_metrics_cards.append(f'<div class="card card-metric"><div class="val">{esc(str(val))}</div><div class="lbl">{esc(label)}</div></div>')
        if browser_metrics.get("usedJSHeapSize") is not None:
            used_mb = round(browser_metrics["usedJSHeapSize"] / 1024 / 1024, 1)
            summary_metrics_cards.append(f'<div class="card card-metric"><div class="val">{used_mb}</div><div class="lbl">JS heap МБ</div></div>')
    summary_metrics_html = "\n".join(summary_metrics_cards) if summary_metrics_cards else "<p class=\"sub\">Метрики не собраны</p>"

    total_steps = len(step_log)
    timeline_bars = ""
    if total_steps > 0:
        for e in step_log[-60:]:
            s = e.get("step", 0)
            act = e.get("action", "")
            is_fail = "error" in (e.get("result") or "").lower() or "not_found" in (e.get("result") or "").lower()
            pct = 100 * s / max(total_steps, 1)
            cls = "timeline-fail" if is_fail else "timeline-ok"
            timeline_bars += f'<span class="timeline-bar {cls}" style="width:{max(2, 100/60)}%" title="#{s} {act}"/>'

    # Данные для Session Replay (без json.dumps, чтобы избежать проблем с несериализуемыми типами)
    steps_js_items: List[str] = []
    for e in step_log:
        step_val = e.get("step")
        if isinstance(step_val, dict):
            step_num = 0
        else:
            try:
                step_num = int(step_val or 0)
            except (TypeError, ValueError):
                step_num = 0
        sp = e.get("screenshot_path") or ""
        thumb = os.path.basename(sp) if sp else ""
        thumb_safe = thumb.replace("\\", "\\\\").replace('"', '\\"')
        steps_js_items.append(f'{{"step": {step_num}, "thumb": "{thumb_safe}"}}')
    steps_js = "[" + ",".join(steps_js_items) + "]"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta http-equiv="refresh" content="15"/>
<title>Kventin — отчёт сессии</title>
<style>
:root {{
  --bg: #0c0c10;
  --bg2: #12121a;
  --surface: #18181f;
  --surface2: #22222e;
  --surface3: #2a2a38;
  --text: #f0f0f5;
  --text2: #a0a0b0;
  --accent: #6366f1;
  --accent2: #818cf8;
  --accent-dim: rgba(99,102,241,0.12);
  --success: #22c55e;
  --warn: #eab308;
  --danger: #ef4444;
  --radius: 14px;
  --radius-sm: 8px;
  --shadow: 0 4px 24px rgba(0,0,0,0.35);
  --font: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 0;
  font-family: var(--font);
  font-size: 15px;
  line-height: 1.55;
  background: var(--bg);
  color: var(--text);
  background-image: radial-gradient(ellipse 100% 60% at 50% -30%, rgba(99,102,241,0.12), transparent 55%);
  min-height: 100vh;
}}
.container {{ max-width: 1280px; margin: 0 auto; padding: 0 1.5rem 2rem; }}
.header-bar {{
  background: var(--surface);
  border-bottom: 1px solid var(--surface2);
  padding: 1rem 1.5rem;
  margin-bottom: 1.5rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
}}
.header-bar h1 {{
  margin: 0;
  font-size: 1.5rem;
  font-weight: 700;
  background: linear-gradient(135deg, var(--accent2), var(--accent));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}}
.live-badge {{
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.35rem 0.75rem;
  border-radius: 999px;
  background: var(--accent-dim);
  color: var(--accent2);
  font-size: 0.8rem;
  font-weight: 500;
}}
.live-badge::before {{
  content: "";
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--success);
  animation: pulse 2s ease-in-out infinite;
}}
@keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}
.sub {{
  color: var(--text2);
  font-size: 0.9rem;
  margin-bottom: 0.5rem;
}}
.cards {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1rem;
  margin-bottom: 2rem;
}}
.card {{
  background: var(--surface);
  border: 1px solid var(--surface2);
  border-radius: var(--radius);
  padding: 1.25rem 1.5rem;
  text-align: center;
  box-shadow: var(--shadow);
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}}
.card:hover {{ transform: translateY(-2px); box-shadow: 0 8px 32px rgba(0,0,0,0.4); }}
.card .val {{
  font-size: 1.75rem;
  font-weight: 700;
  color: var(--accent2);
  letter-spacing: -0.02em;
}}
.card .lbl {{ font-size: 0.8rem; color: var(--text2); margin-top: 0.35rem; text-transform: uppercase; letter-spacing: 0.04em; }}
section {{
  background: var(--surface);
  border: 1px solid var(--surface2);
  border-radius: var(--radius);
  padding: 1.5rem 1.75rem;
  margin-bottom: 1.25rem;
  box-shadow: var(--shadow);
}}
section h2 {{
  font-size: 0.95rem;
  margin: 0 0 1rem;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--surface2);
}}
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
  border-radius: var(--radius-sm);
  overflow: hidden;
}}
th {{
  text-align: left;
  padding: 0.75rem 1rem;
  color: var(--text2);
  font-weight: 600;
  font-size: 0.8rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  background: var(--surface2);
}}
td {{
  padding: 0.7rem 1rem;
  border-bottom: 1px solid rgba(255,255,255,0.04);
  background: var(--surface);
}}
tr:nth-child(even) td {{ background: rgba(255,255,255,0.02); }}
tr:hover td {{ background: rgba(99,102,241,0.06); }}
tr:last-child td {{ border-bottom: none; }}
.url {{ max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.sel {{ max-width: 140px; overflow: hidden; text-overflow: ellipsis; }}
.result {{ max-width: 260px; overflow: hidden; text-overflow: ellipsis; }}
.act {{ padding: 0.25em 0.6em; border-radius: var(--radius-sm); font-weight: 500; background: var(--surface3); color: var(--text); font-size: 0.85em; }}
.act-click {{ background: rgba(99,102,241,0.28); color: var(--accent2); }}
.act-type {{ background: rgba(34,197,94,0.22); color: var(--success); }}
.act-scroll {{ background: rgba(234,179,8,0.22); color: var(--warn); }}
.act-hover {{ background: rgba(129,140,248,0.22); color: var(--accent2); }}
.act-close_modal {{ background: rgba(239,68,68,0.22); color: var(--danger); }}
.act-fill_form {{ background: rgba(34,197,94,0.28); color: var(--success); }}
.src {{ padding: 0.2em 0.45em; border-radius: 4px; font-size: 0.78em; }}
.src-gigachat {{ background: rgba(99,102,241,0.22); color: var(--accent2); }}
.src-fast {{ background: var(--surface3); color: var(--text2); }}
.step-thumb {{ width: 88px; height: 50px; object-fit: cover; border-radius: var(--radius-sm); display: block; }}
.thumb {{ width: 100px; }}
.key {{ font-family: ui-monospace, monospace; color: var(--accent2); }}
.sev {{ padding: 0.25em 0.55em; border-radius: var(--radius-sm); font-size: 0.82em; font-weight: 500; }}
.sev-critical {{ background: rgba(239,68,68,0.28); color: var(--danger); }}
.sev-major {{ background: rgba(234,179,8,0.28); color: var(--warn); }}
.sev-minor {{ background: var(--surface3); color: var(--text2); }}
pre {{ margin: 0; font-size: 0.85rem; color: var(--text2); white-space: pre-wrap; line-height: 1.5; }}
.timeline-wrap {{ display: flex; flex-wrap: wrap; gap: 2px; margin-top: 0.5rem; }}
.timeline-bar {{ height: 22px; border-radius: 5px; display: inline-block; min-width: 5px; }}
.timeline-ok {{ background: linear-gradient(180deg, var(--accent), var(--accent2)); opacity: 0.9; }}
.timeline-fail {{ background: var(--danger); }}
.replay-wrap {{ display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem; flex-wrap: wrap; }}
.replay-btn {{ padding: 0.5rem 1rem; border-radius: var(--radius-sm); background: var(--surface2); color: var(--text); border: 1px solid var(--surface3); cursor: pointer; font-size: 0.9rem; }}
.replay-btn:hover {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.replay-strip {{ display: flex; flex-wrap: wrap; gap: 6px; max-height: 130px; overflow-y: auto; }}
.replay-thumb {{ width: 84px; height: 47px; border-radius: var(--radius-sm); overflow: hidden; border: 2px solid transparent; cursor: pointer; }}
.replay-thumb.active {{ border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }}
.replay-thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
.metrics-summary-wrap {{ margin-bottom: 1rem; }}
.cards-inline {{ display: flex; flex-wrap: wrap; gap: 0.75rem; }}
.card-metric .val {{ font-size: 1.25rem; }}
.report-full-text {{ margin-top: 1rem; }}
.report-full-text summary {{ cursor: pointer; color: var(--text2); }}
.api-section-title {{ display: inline; margin-right: 0.5rem; }}
.api-section summary {{ cursor: pointer; list-style: none; }}
.api-section summary::-webkit-details-marker {{ display: none; }}
.api-filter-wrap {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.75rem 0; }}
.api-filter {{ font-size: 0.85rem; }}
.api-filter.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.api-table tr[data-status].hidden {{ display: none; }}
@media (max-width: 768px) {{ .url, .result {{ max-width: 120px; }} .header-bar {{ flex-direction: column; align-items: flex-start; }} }}
</style>
</head>
<body>
<div class="header-bar">
<div>
<h1>Kventin</h1>
<p class="sub">Отчёт сессии · {esc(start_url or "—")[:70]}</p>
</div>
<span class="live-badge">Обновлено {esc(datetime.now().strftime("%H:%M:%S"))}</span>
</div>
<div class="container">
<div class="cards">
<div class="card"><div class="val">{len(step_log)}</div><div class="lbl">Шагов</div></div>
<div class="card"><div class="val">{int(duration_sec)}</div><div class="lbl">Секунд</div></div>
<div class="card"><div class="val">{esc(memory.tester_phase)}</div><div class="lbl">Фаза</div></div>
<div class="card"><div class="val">{len(defects)}</div><div class="lbl">Дефектов</div></div>
<div class="card"><div class="val">{len(memory.done_click)}</div><div class="lbl">Кликов</div></div>
<div class="card"><div class="val">{len(memory.done_type)}</div><div class="lbl">Вводов</div></div>
</div>
{f'<p class="sub">Видео сессии: <code>{esc(video_dir)}</code></p>' if video_dir else ''}
<section>
<h2>Сводка</h2>
<div class="metrics-summary-wrap">
<h3 class="sub">Метрики браузера</h3>
<div class="cards cards-inline">{summary_metrics_html}</div>
</div>
<details class="report-full-text"><summary>Полный текст отчёта</summary>
<pre>{esc(report_text)}</pre>
</details>
</section>
<section>
<h2>Покрытие</h2>
<p>{esc(coverage)}</p>
</section>
<section>
<details class="nav-details"><summary><h2>Навигация</h2></summary>
<table>
<thead><tr><th>Шаг</th><th>От</th><th>Куда</th></tr></thead>
<tbody>
{nav_body}
</tbody>
</table>
</details>
</section>
<section>
<h2>Timeline</h2>
<div class="timeline-wrap">{timeline_bars}</div>
</section>
<section>
<h2>Session Replay</h2>
<div class="replay-wrap" id="replay-wrap">
<button type="button" class="replay-btn" id="replay-prev">◀ Prev</button>
<button type="button" class="replay-btn" id="replay-play">Play</button>
<button type="button" class="replay-btn" id="replay-next">Next ▶</button>
<span class="sub" id="replay-info">Шаг 0 / {total_steps}</span>
</div>
<div class="replay-strip" id="replay-strip"></div>
<script>
(function(){{
var steps = {steps_js};
var idx = 0, total = steps.length, playing = false, t;
if (!total) {{ document.getElementById("replay-info").textContent = "Нет шагов"; }}
else {{
var strip = document.getElementById("replay-strip");
steps.forEach(function(s, i){{
 var a = document.createElement("a");
 a.href = "#step-" + s.step;
 a.className = "replay-thumb" + (i===0 ? " active" : "");
 a.dataset.step = i;
 a.innerHTML = s.thumb ? "<img src=\\"screenshots/" + s.thumb + "\\" alt=\\"#"+s.step+"\\"/>" : "<span>#"+s.step+"</span>";
 strip.appendChild(a);
}});
function go(i){{
 idx = Math.max(0, Math.min(i, total-1));
 strip.querySelectorAll(".replay-thumb").forEach(function(el, j){{ el.classList.toggle("active", j===idx); }});
 document.getElementById("replay-info").textContent = "Шаг " + (steps[idx]&&steps[idx].step) + " / " + total;
 var stepNum = steps[idx] && steps[idx].step;
 if(stepNum) {{ var row = document.getElementById("step-" + stepNum); if(row) row.scrollIntoView({{block:"center"}}); }}
}}
document.getElementById("replay-prev").onclick = function(){{ go(idx-1); }};
document.getElementById("replay-next").onclick = function(){{ go(idx+1); }};
document.getElementById("replay-play").onclick = function(){{
 playing = !playing;
 this.textContent = playing ? "Pause" : "Play";
 if(playing) t = setInterval(function(){{ go(idx+1); if(idx>=total-1) clearInterval(t); }}, 2000);
 else clearInterval(t);
}};
strip.querySelectorAll(".replay-thumb").forEach(function(el){{ el.onclick = function(e){{ e.preventDefault(); go(parseInt(this.dataset.step,10)); }}; }});
}}
}})();
</script>
</section>
{broken_section}
{console_section}
{vr_section}
<section>
<h2>Метрики браузера (последний сбор)</h2>
<table>
<thead><tr><th>Метрика</th><th>Значение</th></tr></thead>
<tbody>{metrics_body}</tbody>
</table>
<p class="sub">Шаг: {browser_metrics.get('step', '—')}, URL: {esc((browser_metrics.get('url') or '')[:120])}</p>
</section>
{api_section}
{mixed_section}
<section>
<h2>Шаги</h2>
<table id="report-steps-table">
<thead><tr><th>#</th><th>URL</th><th>Действие</th><th>Селектор</th><th>Результат</th><th>Источник</th><th>Flakiness</th><th>Скрин</th></tr></thead>
<tbody>
{steps_body}
</tbody>
</table>
</section>
<section>
<h2>Созданные дефекты</h2>
<table>
<thead><tr><th>Ключ</th><th>Severity</th><th>Описание</th></tr></thead>
<tbody>
{defects_body}
</tbody>
</table>
</section>
</div>
</body>
</html>"""

def _should_create_new_checklist(page: Page, current_url: str, memory: AgentMemory, has_overlay: bool, overlay_types: List[str], checklist_key: str) -> bool:
    """
    Определить, нужно ли создать новый чеклист:
    - Первое посещение страницы (нет чеклиста для URL)
    - Появилась новая модалка/оверлей (если основной чеклист завершён)
    """
    # Если чеклиста для этого ключа (страница или страница+оверлей) ещё нет — проверить приоритеты
    if checklist_key not in memory._page_checklists:
        # Если это оверлей — сначала убедиться, что основной чеклист страницы существует и завершён
        if has_overlay:
            main_checklist = memory._page_checklists.get(current_url)
            # Если основного чеклиста страницы нет — не создавать чеклист оверлея, сначала нужен основной
            if main_checklist is None:
                return False  # Сначала создадим основной чеклист страницы
            # Если основной чеклист существует но не завершён — не создавать чеклист оверлея
            if not main_checklist.get("completed", False):
                return False  # Сначала завершим основной чеклист
            # Основной чеклист завершён — можно создать чеклист для оверлея
            return True
        # Если это не оверлей — создать основной чеклист страницы
        return True
    
    checklist_info = memory._page_checklists[checklist_key]
    
    # Если чеклист уже завершён — не создавать заново
    if checklist_info.get("completed", False):
        return False
    
    # Если чеклист не завершён — продолжать выполнять существующий
    return False


def _step_checklist_incremental(
    page: Page, 
    step: int, 
    current_url: str,
    console_log: List[Dict[str, Any]], 
    network_failures: List[Dict[str, Any]], 
    memory: AgentMemory
) -> List[Dict[str, Any]]:
    """
    Инкрементальный чеклист: создаётся один раз для страницы, выполняется постепенно.
    Возвращает результаты выполненных пунктов.
    """
    from src.checklist import build_checklist
    
    overlay_info = detect_active_overlays(page)
    has_overlay = overlay_info.get("has_overlay", False)
    overlay_types = [o.get("type", "?") for o in overlay_info.get("overlays", [])]
    
    # Определяем ключ для чеклиста (страница или страница+оверлей)
    if has_overlay and overlay_types:
        checklist_key = f"{current_url}::overlay::{','.join(sorted(overlay_types))}"
    else:
        checklist_key = current_url
    
    # Проверяем, нужно ли создать новый чеклист
    should_create = _should_create_new_checklist(page, current_url, memory, has_overlay, overlay_types, checklist_key)
    
    # Если это оверлей, но основной чеклист страницы ещё не создан — создать основной сначала
    if has_overlay and not should_create:
        main_checklist = memory._page_checklists.get(current_url)
        if main_checklist is None:
            # Создаём основной чеклист страницы вместо чеклиста оверлея
            checklist_items = build_checklist()
            memory._page_checklists[current_url] = {
                "items": checklist_items,
                "index": 0,
                "completed": False,
                "results": [],
            }
            print(f"[Agent] #{step} Создан основной чеклист для страницы (оверлей будет позже)")
            # Используем основной чеклист для выполнения
            checklist_key = current_url
    
    if should_create:
        # Создаём новый чеклист
        checklist_items = build_checklist()
        memory._page_checklists[checklist_key] = {
            "items": checklist_items,
            "index": 0,
            "completed": False,
            "results": [],
        }
        context_desc = f"оверлей ({', '.join(overlay_types)})" if has_overlay else "страницы"
        print(f"[Agent] #{step} Создан новый чеклист для {context_desc} ({len(checklist_items)} пунктов)")
    
    # Получаем текущий чеклист
    checklist_info = memory._page_checklists.get(checklist_key)
    if not checklist_info:
        return []
    
    # Если чеклист уже завершён — возвращаем результаты
    if checklist_info.get("completed", False):
        return checklist_info.get("results", [])
    
    # Выполняем следующий пункт чеклиста (по одному за шаг)
    current_index = checklist_info["index"]
    items = checklist_info["items"]
    
    if current_index < len(items):
        item = items[current_index]
        try:
            ok, detail = item["check"](page, console_log, network_failures)
        except Exception as e:
            ok, detail = False, str(e)
        
        result = {
            "id": item["id"],
            "title": item["title"],
            "ok": ok,
            "detail": detail,
        }
        
        checklist_info["results"].append(result)
        checklist_info["index"] = current_index + 1
        
        # Обновляем UI
        st = "+" if ok else "X"
        total = len(items)
        update_llm_overlay(page, prompt=f"Чеклист: {item['id']}", response=f"{st} {detail[:120]}", loading=False)
        
        # Если выполнили все пункты — помечаем как завершённый
        if checklist_info["index"] >= len(items):
            checklist_info["completed"] = True
            context_desc = f"оверлей ({', '.join(overlay_types)})" if has_overlay else "страницы"
            print(f"[Agent] #{step} Чеклист для {context_desc} завершён")
    
    return checklist_info.get("results", []) if checklist_info else []


def _step_checklist(page, step, console_log, network_failures, memory):
    """LEGACY: Старый способ (полный запуск чеклиста). Оставлен для совместимости."""
    checklist_every = 5
    checklist_results = []
    if step % checklist_every == 1:
        smart_wait_after_goto(page, timeout=5000)
        def on_step(step_id, ok, detail, step_index, total):
            st = "+" if ok else "X"
            pct = round(100 * step_index / total) if total else 0
            update_llm_overlay(page, prompt=f"Чеклист: {step_id}", response=f"{st} {detail[:120]}", loading=False)
        checklist_results = run_checklist(page, console_log, network_failures, step_delay_ms=CHECKLIST_STEP_DELAY_MS, on_step=on_step)
    return checklist_results


def _get_fast_action(
    page: Page,
    memory: AgentMemory,
    has_overlay: bool = False,
) -> Dict[str, Any]:
    """
    Мгновенный выбор действия БЕЗ LLM — по ref-id из DOM.
    Учитывает текущий модуль (если задан): только элементы внутри модуля.
    """
    try:
        if page.is_closed():
            return {"action": "scroll", "selector": "down", "reason": "Страница закрыта"}
        
        if has_overlay:
            return {"action": "close_modal", "selector": "", "reason": "Закрываю оверлей"}

        current_url = page.url
        cur_module = memory.get_current_module()
        scope_selector = (cur_module.get("selector") or "").strip() if cur_module else ""

        def _collect_js(scope_sel: str) -> str:
            scope_check = ""
            if scope_sel:
                scope_check = """
                const scopeEl = document.querySelector(scopeSel);
                if (scopeEl && !scopeEl.contains(el)) return;"""
            return """
            (scopeSel) => {
            const scopeEl = scopeSel ? document.querySelector(scopeSel) : null;
            if (scopeSel && !scopeEl) return [];
            const result = [];
            const isAgent = (el) => {
                let c = el;
                while (c && c !== document.body) {
                    if (c.hasAttribute && c.hasAttribute('data-agent-host')) return true;
                    c = c.parentElement;
                }
                return false;
            };
            const inViewport = (el) => {
                const r = el.getBoundingClientRect();
                const vw = window.innerWidth, vh = window.innerHeight;
                return r.top < vh && r.bottom > 0 && r.left < vw && r.right > 0;
            };
            const ancestorsVisible = (el) => {
                let cur = el.parentElement;
                while (cur && cur !== document.body) {
                    const s = getComputedStyle(cur);
                    if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
                    cur = cur.parentElement;
                }
                return true;
            };
            const vis = (el) => {
                const r = el.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) return false;
                const s = getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
                if (!inViewport(el) || !ancestorsVisible(el)) return false;
                return true;
            };
            // Кнопки (приоритет 1)
            document.querySelectorAll('button:not([disabled]), [role="button"]:not([disabled]), input[type="submit"]').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                """ + ("if (scopeEl && !scopeEl.contains(el)) return;" if scope_sel else "") + """
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                const text = (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 50);
                result.push({ref: 'ref:' + ref, type: 'click', text, priority: 1});
            });
            // Инпуты (приоритет 2)
            document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([disabled]), textarea:not([disabled])').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                """ + ("if (scopeEl && !scopeEl.contains(el)) return;" if scope_sel else "") + """
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                const text = (el.placeholder || el.name || el.getAttribute('aria-label') || '').trim().slice(0, 50);
                result.push({ref: 'ref:' + ref, type: 'input', text, priority: 2});
            });
            // Ссылки (приоритет 3)
            document.querySelectorAll('a[href]:not([disabled])').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                """ + ("if (scopeEl && !scopeEl.contains(el)) return;" if scope_sel else "") + """
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                const text = (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 50);
                const href = (el.getAttribute('href') || '');
                if (href.startsWith('javascript:') || href === '#') return;
                if (href.startsWith('http')) {
                    try {
                        const url = new URL(href, window.location.href);
                        if (url.hostname !== window.location.hostname && url.hostname !== '') return;
                    } catch(e) { return; }
                }
                result.push({ref: 'ref:' + ref, type: 'link', text, priority: 3});
            });
            // Select, табы
            document.querySelectorAll('select:not([disabled])').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                """ + ("if (scopeEl && !scopeEl.contains(el)) return;" if scope_sel else "") + """
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                const opts = Array.from(el.options).slice(0,3).map(o => o.text.trim()).join(',');
                result.push({ref: 'ref:' + ref, type: 'select', text: opts, priority: 2});
            });
            document.querySelectorAll('[role="tab"]').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                """ + ("if (scopeEl && !scopeEl.contains(el)) return;" if scope_sel else "") + """
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                const text = (el.textContent || '').trim().slice(0, 50);
                result.push({ref: 'ref:' + ref, type: 'tab', text, priority: 2});
            });
            document.querySelectorAll('input[type="file"]').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                """ + ("if (scopeEl && !scopeEl.contains(el)) return;" if scope_sel else "") + """
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                result.push({ref: 'ref:' + ref, type: 'file', text: 'file', priority: 2});
            });
            return result;
            }
            """

        elements = page.evaluate(_collect_js(scope_selector), scope_selector) or []

        # Если задан тестовый файл для загрузки — предпочитаем input[type=file]
        if TEST_UPLOAD_FILE_PATH and os.path.isfile(TEST_UPLOAD_FILE_PATH):
            file_elems = [e for e in elements if e.get("type") == "file" and _norm_key(e.get("ref", "")) not in memory.done_type]
            if file_elems:
                elem = file_elems[0]
                return {
                    "action": "upload_file",
                    "selector": elem.get("ref", ""),
                    "value": TEST_UPLOAD_FILE_PATH,
                    "reason": "Загрузка тестового файла",
                    "test_goal": "Проверка загрузки файла",
                    "expected_outcome": "Файл принят",
                }

        # Старая логика без scope — один большой evaluate (оставляем запасной вариант)
        if scope_selector and not elements:
            elements = page.evaluate("""() => {
            const result = [];
            const isAgent = (el) => {
                let c = el;
                while (c && c !== document.body) {
                    if (c.hasAttribute && c.hasAttribute('data-agent-host')) return true;
                    c = c.parentElement;
                }
                return false;
            };
            const inViewport = (el) => {
                const r = el.getBoundingClientRect();
                const vw = window.innerWidth, vh = window.innerHeight;
                return r.top < vh && r.bottom > 0 && r.left < vw && r.right > 0;
            };
            const ancestorsVisible = (el) => {
                let cur = el.parentElement;
                while (cur && cur !== document.body) {
                    const s = getComputedStyle(cur);
                    if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
                    cur = cur.parentElement;
                }
                return true;
            };
            const vis = (el) => {
                const r = el.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) return false;
                const s = getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
                if (!inViewport(el) || !ancestorsVisible(el)) return false;
                return true;
            };
            // Кнопки (приоритет 1)
            document.querySelectorAll('button:not([disabled]), [role="button"]:not([disabled]), input[type="submit"]').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                const text = (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 50);
                result.push({ref: 'ref:' + ref, type: 'click', text, priority: 1});
            });
            // Инпуты (приоритет 2)
            document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([disabled]), textarea:not([disabled])').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                const text = (el.placeholder || el.name || el.getAttribute('aria-label') || '').trim().slice(0, 50);
                result.push({ref: 'ref:' + ref, type: 'input', text, priority: 2});
            });
            // Ссылки (приоритет 3) — только внутренние (на том же домене)
            document.querySelectorAll('a[href]:not([disabled])').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                const text = (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 50);
                const href = (el.getAttribute('href') || '');
                if (href.startsWith('javascript:') || href === '#') return;
                // Проверяем что это внутренняя ссылка (относительный путь или тот же домен)
                if (href.startsWith('http')) {
                    try {
                        const url = new URL(href, window.location.href);
                        const currentDomain = window.location.hostname;
                        if (url.hostname !== currentDomain && url.hostname !== '') return; // Внешняя ссылка — пропускаем
                    } catch(e) { return; }
                }
                result.push({ref: 'ref:' + ref, type: 'link', text, priority: 3});
            });
            // Select (приоритет 2)
            document.querySelectorAll('select:not([disabled])').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                const opts = Array.from(el.options).slice(0,3).map(o => o.text.trim()).join(',');
                result.push({ref: 'ref:' + ref, type: 'select', text: opts, priority: 2});
            });
            // Табы (приоритет 2)
            document.querySelectorAll('[role="tab"]').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                const text = (el.textContent || '').trim().slice(0, 50);
                result.push({ref: 'ref:' + ref, type: 'tab', text, priority: 2});
            });
            document.querySelectorAll('input[type="file"]').forEach(el => {
                if (!vis(el) || isAgent(el)) return;
                let ref = el.getAttribute('data-agent-ref');
                if (!ref) return;
                result.push({ref: 'ref:' + ref, type: 'file', text: 'file', priority: 2});
            });
            return result;
        }""") or []

        # Резолвим stable_key для всех refs одним evaluate (для устойчивой дедупликации).
        # Возвращает {ref_str_without_prefix: stable_key}
        try:
            ref_keys = page.evaluate(
                "() => { const out = {}; const m = window.__agentRefMeta || {}; for (const k of Object.keys(m)) out[k] = m[k] || ''; return out; }"
            ) or {}
        except Exception:
            ref_keys = {}
        url_pat = memory.current_url_pattern or _url_pattern(current_url)

        def _stable_key_for(elem_ref: str) -> str:
            r = elem_ref or ""
            if r.startswith("ref:"):
                r = r[4:]
            return str(ref_keys.get(r, "") or "")

        def _is_already_done_in_memory(act_type: str, stable_key: str) -> bool:
            if not (url_pat and stable_key):
                return False
            return stable_key in memory.done_by_url.get(url_pat, {}).get(act_type, set())

        # Фильтруем: убираем уже протестированные элементы (по stable_key + url_pattern)
        for elem in elements:
            ref = elem.get("ref", "")
            etype = elem.get("type", "")
            if etype == "file":
                continue  # file уже обработан выше (TEST_UPLOAD_FILE_PATH) или пропускаем
            act = "click" if etype in ("click", "link", "tab") else ("type" if etype == "input" else "select_option")
            stable_key = _stable_key_for(ref)
            if _is_already_done_in_memory(act, stable_key):
                continue
            text = elem.get("text", "?")[:30]
            if True:
                if etype == "input":
                    from src.form_strategies import detect_field_type, get_test_value
                    ftype = detect_field_type(placeholder=text, name=text)
                    val = get_test_value(ftype, "happy")
                    return {
                        "action": "type", "selector": ref, "value": val,
                        "reason": f"Ввод в '{text}'",
                        "test_goal": f"Заполнить поле {text}",
                        "expected_outcome": "Поле принимает значение",
                        "_stable_key": stable_key,
                        "_url_pattern": url_pat,
                    }
                elif etype == "select":
                    return {
                        "action": "select_option", "selector": ref, "value": text.split(",")[0] if text else "",
                        "reason": "Выбор опции",
                        "test_goal": "Выбрать опцию в дропдауне",
                        "expected_outcome": "Опция выбирается",
                        "_stable_key": stable_key,
                        "_url_pattern": url_pat,
                    }
                else:
                    return {
                        "action": "click", "selector": ref,
                        "reason": f"Клик: {text}",
                        "test_goal": f"Проверить '{text}'",
                        "expected_outcome": "Элемент реагирует",
                        "_stable_key": stable_key,
                        "_url_pattern": url_pat,
                    }

        # Не уходить в бесконечный скролл: если уже много скроллов подряд — тыкать первый элемент (даже повторно)
        if memory.should_avoid_scroll() and elements:
            elem = elements[0]
            ref = elem.get("ref", "")
            etype = elem.get("type", "")
            text = elem.get("text", "?")[:30]
            if etype == "input":
                try:
                    from src.form_strategies import detect_field_type, get_test_value
                    val = get_test_value(detect_field_type(placeholder=text, name=text), "happy")
                    return {"action": "type", "selector": ref, "value": val, "reason": f"Повтор ввода: {text}", "test_goal": "Поле", "expected_outcome": "OK"}
                except Exception:
                    pass
            if etype == "select":
                return {"action": "select_option", "selector": ref, "value": (text.split(",")[0] if text else ""), "reason": "Повтор выбора", "test_goal": "Опция", "expected_outcome": "OK"}
            return {"action": "click", "selector": ref, "reason": f"Повтор клика: {text}", "test_goal": "Клик", "expected_outcome": "OK"}

        # Last resort: нет элементов с ref — клик по первой кнопке/ссылке на странице
        if memory.should_avoid_scroll():
            return {"action": "click", "selector": "button, a[href]", "reason": "Last resort — первая кнопка/ссылка", "test_goal": "Клик", "expected_outcome": "OK"}

        return {"action": "scroll", "selector": "down", "reason": "Все видимые элементы протестированы, ищу новые"}

    except Exception as e:
        LOG.debug("_get_fast_action error: %s", e)
        return {"action": "scroll", "selector": "down", "reason": "Fast action error"}


def _step_get_action(page, step, memory, console_log, network_failures, checklist_results, context):
    """STEP 2: Скриншот + контекст → GigaChat → получить действие."""
    dom_max = 4000
    history_n = 15

    overlay_info = detect_active_overlays(page)
    overlay_context = format_overlays_context(overlay_info)
    has_overlay = overlay_info.get("has_overlay", False)

    if has_overlay:
        overlay_types = [o.get("type", "?") for o in overlay_info.get("overlays", [])]
        print(f"[Agent] #{step} Оверлеи: {', '.join(overlay_types)}")

    screenshot_b64 = take_screenshot_b64(page)
    screenshot_changed = memory.is_screenshot_changed(screenshot_b64 or "")

    current_url = page.url
    context_str = build_context(page, current_url, console_log, network_failures)
    if checklist_results:
        context_str = checklist_results_to_context(checklist_results) + "\n\n" + context_str
    if overlay_context:
        context_str = overlay_context + "\n\n" + context_str
    
    # Детекция типа страницы для адаптивной стратегии
    page_type = detect_page_type(page)
    page_type_hint = ""
    if page_type != "unknown":
        type_strategies = {
            "landing": "Landing page: приоритет на CTA кнопки, формы регистрации, hero-секция",
            "form": "Form page: заполни ВСЕ поля формы, проверь валидацию, отправь форму",
            "dashboard": "Dashboard: проверь таблицы, фильтры, навигацию, данные",
            "catalog": "Catalog: кликай по карточкам товаров, фильтры, сортировка, пагинация",
            "article": "Article: проверь читаемость, ссылки, комментарии, навигацию",
        }
        page_type_hint = f"\nТип страницы: {page_type}. {type_strategies.get(page_type, '')}\n"
    
    dom_summary = get_dom_summary(page, max_length=dom_max, include_shadow_dom=ENABLE_SHADOW_DOM)
    history_text = memory.get_history_text(last_n=history_n)
    
    # Проверяем покрытие элементов на текущей странице
    coverage_hint = ""
    if current_url in memory._page_coverage:
        tested_count = len(memory._page_coverage[current_url])
        if tested_count > 0:
            coverage_hint = f"\nНа этой странице уже протестировано элементов: {tested_count}. Выбери НОВЫЙ элемент.\n"

    if has_overlay:
        stuck_warning = ""
        if memory.is_stuck():
            stuck_warning = "\n🚨 КРИТИЧНО: Агент зациклился! Выбери действие, которого НЕТ в списке выше.\n"
        question = f"""Вот скриншот. На странице АКТИВНЫЙ ОВЕРЛЕЙ (модалка/дропдаун/тултип/попап).
{overlay_context}
ЭЛЕМЕНТЫ СТРАНИЦЫ (только видимые на экране, формат: [N] тип "текст" атрибуты):
{dom_summary[:3000]}
{history_text}{stuck_warning}
🚀 Используй selector="ref:N" (N из [N] выше).
1) Тестируй содержимое оверлея, 2) Если уже тестировал — закрой (close_modal), 3) Баг — check_defect.
⚠️ НЕ ПОВТОРЯЙ действия. Выбери КОНКРЕТНОЕ ДЕЙСТВИЕ."""
    else:
        plan_hint = ""
        if memory.test_plan:
            plan_progress = memory.get_test_plan_progress()
            plan_hint = f"\n{plan_progress}\n"
        if CRITICAL_FLOW_STEPS:
            plan_hint += "\nКритический сценарий: " + ", ".join(CRITICAL_FLOW_STEPS[:5]) + "\n"
        form_strategy = get_form_fill_strategy(memory.tester_phase, memory.form_strategy_iteration)
        form_hint = ""
        if form_strategy != "happy":
            form_hint = f"\nСтратегия заполнения форм: {form_strategy} (негативные/граничные/security значения).\n"
        # Предупреждение о зацикливании
        stuck_warning = ""
        if memory.is_stuck():
            stuck_warning = "\n🚨🚨🚨 КРИТИЧНО: Агент зациклился! Выбери действие, которого ТОЧНО НЕТ в списке 'УЖЕ СДЕЛАНО' выше. 🚨🚨🚨\n"
        
        # Проверяем наличие формы для умного заполнения (реже проверяем чтобы не замедлять)
        form_hint_smart = ""
        if page_type == "form" and step % 10 == 0:  # Каждые 10 шагов проверяем форму
            form_fields = detect_form_fields(page)
            if form_fields and len(form_fields) > 2:
                # Проверяем непротестированные поля с правильным префиксом (type: или select:)
                untested_fields = []
                for f in form_fields:
                    selector = f.get('selector', '')
                    if not selector:
                        continue
                    field_type_str = f.get('type', '').lower()
                    is_select = field_type_str == 'select'
                    field_key_prefix = "select" if is_select else "type"
                    field_key = f"{field_key_prefix}:{_norm_key(selector)}"
                    if not memory.is_element_tested(current_url, field_key):
                        untested_fields.append(f)
                if untested_fields:
                    form_hint_smart = f"\n💡 На странице форма с {len(form_fields)} полями. Рекомендуется заполнить все поля формы за раз (action='fill_form').\n"
        
        # Проверяем наличие таблиц для умного тестирования (реже)
        table_hint = ""
        if page_type == "dashboard" and step % 15 == 0:  # Каждые 15 шагов проверяем таблицы
            tables = detect_table_structure(page)
            if tables:
                table_hint = f"\n📊 На странице {len(tables)} таблиц. Рекомендуется протестировать фильтры, сортировку, пагинацию.\n"
        
        question = f"""Вот скриншот и контекст страницы.
{page_type_hint}{coverage_hint}{form_hint_smart}{table_hint}
ЭЛЕМЕНТЫ СТРАНИЦЫ (только видимые на экране, формат: [N] тип "текст" атрибуты):
{dom_summary[:3000]}
{history_text}
{plan_hint}{form_hint}{stuck_warning}
🚀 Выбери ОДНО КОНКРЕТНОЕ действие. Используй selector="ref:N" (N из [N] выше).
⚠️ НЕ ПОВТОРЯЙ уже сделанные действия.
✅ Приоритет: CTA кнопки → формы (fill_form) → таблицы → навигация → остальное.
🎯 ДЕЙСТВУЙ: кликай, заполняй, тестируй. Укажи test_goal и expected_outcome."""

    phase_instruction = memory.get_phase_instruction()
    update_llm_overlay(page, prompt=f"#{step} [{memory.tester_phase}]", loading=True)

    # Скриншот для GigaChat: если не изменился — можно не отправлять (экономия токенов)
    send_screenshot = screenshot_b64 if screenshot_changed else None

    # Scenario chains: в critical_path каждый 3-й шаг запрашиваем цепочку
    if ENABLE_SCENARIO_CHAINS and memory.tester_phase == "critical_path" and step % 3 == 0 and not has_overlay:
        chain = _request_scenario_chain(page, memory, context_str, send_screenshot)
        if chain and len(chain) > 1:
            # Выполнить все действия из цепочки кроме первого (первый вернём как основной)
            print(f"[Agent] #{step} Scenario chain: {len(chain)} действий")
            # Первое действие вернём, остальные сохраним в memory для последующих шагов
            if not hasattr(memory, '_scenario_queue'):
                memory._scenario_queue = []
            memory._scenario_queue = chain[1:]
            return chain[0], has_overlay, screenshot_b64

    # Проверить, есть ли действия из scenario chain в очереди
    if hasattr(memory, '_scenario_queue') and memory._scenario_queue:
        action = memory._scenario_queue[0]  # Не pop пока не проверим
        enrich_action(page, memory, action)
        act_check = (action.get("action") or "").lower()
        sel_check = (action.get("selector") or "").strip()
        # Если это повтор — очистить очередь и запросить новое действие
        if act_check != "check_defect" and memory.is_already_done_action(action):
            print(f"[Agent] #{step} ⚠️ Scenario chain содержит повтор: {act_check} -> {sel_check[:40]}. Очищаю очередь.")
            memory._scenario_queue = []
            # Продолжить к обычному запросу к GigaChat
        else:
            action = memory._scenario_queue.pop(0)
            enrich_action(page, memory, action)
            print(f"[Agent] #{step} Scenario chain (из очереди): {action.get('action')} -> {action.get('selector', '')[:40]}")
            return action, has_overlay, screenshot_b64

    # Если застряли — попробовать более агрессивную стратегию
    if memory.is_stuck():
        print(f"[Agent] #{step} 🚨 Зацикливание обнаружено, применяю анти-цикл стратегию...")
        # Принудительно сменить фазу
        memory.advance_tester_phase(force=True)
        memory.reset_repeats()
        # Попробовать прокрутку вверх для поиска новых элементов
        return {"action": "scroll", "selector": "up", "reason": "Анти-цикл: прокрутка вверх"}, has_overlay, screenshot_b64

    raw_answer = consult_agent_with_screenshot(
        context_str, question, screenshot_b64=send_screenshot,
        phase_instruction=phase_instruction, tester_phase=memory.tester_phase,
        has_overlay=has_overlay,
    )
    update_llm_overlay(page, prompt=f"#{step} Ответ", response=raw_answer or "Нет ответа", loading=False, error="Нет ответа" if not raw_answer else None)

    if not raw_answer:
        print(f"[Agent] #{step} GigaChat недоступен после retry, возвращаю None для fallback")
        return None, has_overlay, screenshot_b64

    action = parse_llm_action(raw_answer)
    if not action:
        print(f"[Agent] #{step} Не удалось распарсить JSON: {raw_answer[:120]}")
        action = _get_fast_action(page, memory, has_overlay)
    # Валидация и нормализация
    action = validate_llm_action(action)
    enrich_action(page, memory, action)

    # ПРЕДВАРИТЕЛЬНАЯ проверка повтора ПЕРЕД выполнением
    act_precheck = (action.get("action") or "").lower()
    sel_precheck = (action.get("selector") or "").strip()
    if act_precheck != "check_defect" and memory.is_already_done_action(action):
        print(f"[Agent] #{step} ⚠️ GigaChat предложил повтор: {act_precheck} -> {sel_precheck[:40]} (key={action.get('_stable_key', '')[:40]}). Игнорирую и выбираю альтернативу.")
        memory.record_repeat()
        # Выбрать альтернативное действие
        if has_overlay:
            action = {"action": "close_modal", "selector": "", "reason": "GigaChat предложил повтор — закрываю оверлей"}
        elif not memory.should_avoid_scroll():
            action = {"action": "scroll", "selector": "down", "reason": "GigaChat предложил повтор — прокрутка"}
        else:
            action = {"action": "hover", "selector": "body", "reason": "GigaChat предложил повтор — hover для поиска"}
        enrich_action(page, memory, action)
    # layout_issue → possible_bug
    if action.get("layout_issue") and not action.get("possible_bug"):
        action["possible_bug"] = action.get("layout_issue")

    act_type = (action.get("action") or "").lower()
    sel = (action.get("selector") or "").strip()
    val = (action.get("value") or "").strip()

    # Дедупликация действий: строгая проверка
    is_repeat = act_type != "check_defect" and memory.is_already_done_action(action)
    if is_repeat:
        memory.record_repeat()
        print(f"[Agent] #{step} ⚠️ ПОВТОР: {act_type} -> {sel[:40]} (повторов подряд: {memory._consecutive_repeats})")
        
        # Если застряли (3+ повтора) — принудительная смена стратегии
        if memory.is_stuck():
            print(f"[Agent] #{step} 🚨 ЗАЦИКЛИВАНИЕ! Принудительная смена фазы и стратегии.")
            memory.advance_tester_phase(force=True)
            memory.reset_repeats()  # Сбросить после смены фазы
            # Попробовать прокрутку вверх или переход на другую часть страницы
            action = {"action": "scroll", "selector": "up", "reason": "Зацикливание — смена фазы, прокрутка вверх"}
        elif has_overlay:
            action = {"action": "close_modal", "selector": "", "reason": "Повтор — закрываю оверлей"}
        elif not memory.should_avoid_scroll():
            action = {"action": "scroll", "selector": "down", "reason": "Повтор — прокрутка вниз"}
        else:
            # Много прокруток уже было — попробовать hover на новый элемент
            action = {"action": "hover", "selector": "body", "reason": "Повтор — hover для поиска новых элементов"}
    else:
        # Успешное новое действие — сбросить счётчик повторов
        memory.reset_repeats()

    # Логирование
    test_goal = action.get("test_goal", "")
    expected_outcome = action.get("expected_outcome", "")
    if test_goal:
        print(f"[Agent] #{step} Цель: {test_goal[:80]}")
    if expected_outcome:
        print(f"[Agent] #{step} Ожидаемый: {expected_outcome[:80]}")
    print(f"[Agent] #{step} Действие: {action.get('action')} -> {action.get('selector', '')[:40]} | {action.get('reason', '')[:50]}")

    return action, has_overlay, screenshot_b64


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


def _step_execute(page, action, step, memory, context):
    """STEP 3: Выполнение действия с retry."""
    act_type = (action.get("action") or "").lower()
    sel = (action.get("selector") or "").strip()
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
    # Один быстрый retry при неудаче
    if "error" in (result or "").lower() or "not_found" in (result or "").lower():
        time.sleep(0.15)
        result = execute_action(page, action, memory)

    # Flakiness: при сбое перезапустить действие ещё N раз и записать долю успехов
    memory._last_step_flakiness = None
    if FLAKINESS_RERUN_COUNT >= 2 and act_type in ("click", "type"):
        ok = 1 if ("not_found" not in (result or "").lower() and "error" not in (result or "").lower()) else 0
        for _ in range(FLAKINESS_RERUN_COUNT - 1):
            time.sleep(0.2)
            r2 = execute_action(page, action, memory)
            if "not_found" not in (r2 or "").lower() and "error" not in (r2 or "").lower():
                ok += 1
        memory._last_step_flakiness = (ok, FLAKINESS_RERUN_COUNT)

    memory.add_action(action, result=result)
    memory.tick_phase_step()
    print(f"[Agent] #{step} Результат: {result}")

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
            if getattr(memory, "_dom_hash_before", None) is not None and h == memory._dom_hash_before:
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

    return result


def _collect_post_data(page, has_overlay, memory):
    """
    Собрать данные после действия ИЗ MAIN THREAD (Playwright).
    Возвращает dict с данными, которые потом можно анализировать в фоне.
    """
    # Проверка: страница закрыта
    if page.is_closed():
        return {
            "new_overlay": False,
            "overlay_types": [],
            "post_screenshot_b64": None,
        }
    
    try:
        # Детекция нового оверлея
        post_overlay = detect_active_overlays(page)
        new_overlay = post_overlay.get("has_overlay") and not has_overlay
        overlay_types = []
        if new_overlay:
            overlay_types = [o.get("type", "?") for o in post_overlay.get("overlays", [])]

        # Скриншот после действия
        post_screenshot_b64 = take_screenshot_b64(page)

        return {
            "new_overlay": new_overlay,
            "overlay_types": overlay_types,
            "post_screenshot_b64": post_screenshot_b64,
        }
    except Exception as e:
        if "closed" in str(e).lower() or "Target page" in str(e):
            return {
                "new_overlay": False,
                "overlay_types": [],
                "post_screenshot_b64": None,
            }
        raise


def _analyze_in_background(
    post_data, step, action, result, act_type, sel, val, expected_outcome, possible_bug,
    current_url, checklist_results, console_log_snapshot, network_snapshot, memory,
    before_screenshot,
):
    """
    Фоновый анализ (без Playwright!): visual diff, оракул, определение багов.
    Возвращает dict с результатами для main thread.
    """
    findings = {"oracle_error": False, "bug_to_report": None, "five_xx_bug": None, "new_console_errors": []}
    post_screenshot_b64 = post_data.get("post_screenshot_b64")

    # Visual diff
    visual_diff_info = compute_screenshot_diff(before_screenshot, post_screenshot_b64)
    if visual_diff_info.get("changed") and visual_diff_info.get("change_percent", 0) > 0:
        LOG.info("#{step} Visual diff: %s (%.1f%%)", visual_diff_info.get("diff_zone", "?"), visual_diff_info.get("change_percent", 0))

    # Берём только новые записи консоли/сети (появившиеся после действия).
    pre_lens = (action or {}).get("_pre_action_lens") or {}
    console_before_len = int(pre_lens.get("console") or 0)
    network_before_len = int(pre_lens.get("network") or 0)
    new_console = console_log_snapshot[console_before_len:] if console_before_len <= len(console_log_snapshot) else console_log_snapshot[-10:]
    new_network = network_snapshot[network_before_len:] if network_before_len <= len(network_snapshot) else network_snapshot[-5:]
    # Применяем шумовой фильтр (favicon, аналитика, расширения, ResizeObserver…)
    from src.defect_rules import is_noise_url, is_noise_console_text, rule_pageerror, rule_4xx_on_main
    new_errors = [
        c for c in new_console
        if (c.get("type") or "").lower() in ("error", "pageerror")
        and not is_noise_console_text(c.get("text") or "")
    ]
    new_network_fails = [
        n for n in new_network
        if n.get("status") and n.get("status") >= 500
        and not is_noise_url(n.get("url") or "")
    ]
    # Сохраним новые ошибки консоли в finding — пригодится для описания дефекта (стек + путь к JS).
    findings["new_console_errors"] = new_errors

    # Короткая сводка по новым ошибкам консоли (с путём до JS-файла) — подкладываем в багрепорт
    def _fmt_console_brief(errs: list) -> str:
        if not errs:
            return ""
        lines = []
        for e in errs[-5:]:
            et = (e.get("type") or "log").lower()
            txt = (e.get("text") or "").strip().replace("\n", " ")[:200]
            src = e.get("source_url") or e.get("url") or ""
            line_no = e.get("line")
            col_no = e.get("column")
            loc = ""
            if src:
                loc = src
                if line_no is not None and col_no is not None:
                    loc = f"{src}:{line_no}:{col_no}"
                elif line_no is not None:
                    loc = f"{src}:{line_no}"
            stack = (e.get("stack") or "")
            first_stack = ""
            if stack:
                for s_line in str(stack).splitlines()[:3]:
                    s_line = s_line.strip()
                    if s_line:
                        first_stack = s_line
                        break
            extra = f" | at {loc}" if loc else ""
            stack_line = f"\n    stack: {first_stack}" if first_stack else ""
            lines.append(f"  - [{et}] {txt}{extra}{stack_line}")
        return "\n".join(lines)

    console_brief = _fmt_console_brief(new_errors)

    # 5xx
    if new_network_fails:
        five_xx_detail = "\n".join(
            f"- {n.get('status')} {n.get('method', 'GET')} {n.get('url', '')[:120]}"
            for n in new_network_fails[-10:]
        )
        findings["five_xx_bug"] = (
            f"HTTP 5xx после действия агента.\n\n"
            f"Действие: {act_type} | selector: {sel[:100]} | value: {val[:50]}\n\n"
            f"Неуспешные запросы:\n{five_xx_detail}"
            + (f"\n\nНовые ошибки консоли после действия:\n{console_brief}" if console_brief else "")
        )

    # Оракул (GigaChat — thread-safe). Lazy: только при изменении экрана или новых ошибках (ORACLE_ON_VISUAL_OR_ERROR)
    run_oracle = ENABLE_ORACLE_AFTER_ACTION and act_type in ("type", "click") and post_screenshot_b64 and not new_network_fails
    if run_oracle and ORACLE_ON_VISUAL_OR_ERROR:
        run_oracle = visual_diff_info.get("changed") or bool(new_errors)
    if run_oracle:
        expected_text = f"Ожидалось: {expected_outcome[:200]}" if expected_outcome else "Ожидался успешный результат."
        vdiff_text = ""
        if visual_diff_info.get("changed"):
            vdiff_text = f" Visual diff: {visual_diff_info.get('detail', '')}."
        oracle_context = f"Действие: {act_type} -> {sel[:60]}. Результат: {result}. {expected_text}{vdiff_text}"
        oracle_ans = consult_agent_with_screenshot(
            oracle_context,
            "Произошло ли ожидаемое? Ответь: успех / ошибка / неясно.",
            screenshot_b64=post_screenshot_b64,
        )
        if oracle_ans and "ошибка" in oracle_ans.lower():
            findings["oracle_error"] = True

    # Пост-анализ ошибок с улучшенной классификацией
    if not new_network_fails and (new_errors or possible_bug or findings["oracle_error"]):
        # Улучшенный контекст для классификации бага
        error_summary = ""
        if new_errors:
            error_types = {}
            for e in new_errors[-5:]:
                err_type = e.get("type", "unknown")
                error_types[err_type] = error_types.get(err_type, 0) + 1
            error_summary = f"Типы ошибок: {', '.join(f'{k}({v})' for k, v in error_types.items())}. "
        
        post_context = f"""Действие: {action.get('action')} -> {action.get('selector', '')}.
Результат: {result}
{error_summary}Последние ошибки: {', '.join(e.get('text', '')[:60] for e in new_errors[-3:])}
Visual diff: {visual_diff_info.get('change_percent', 0):.1f}% изменений.
Ожидалось: {expected_outcome[:100] if expected_outcome else 'успешное выполнение'}.
Классифицируй проблему: критический баг / некритический баг / не баг (ожидаемое поведение) / флак (нестабильный)."""
        
        post_answer = consult_agent_with_screenshot(
            post_context,
            "Это баг или нет? Если критический/некритический баг — JSON с action=check_defect и possible_bug (укажи тип: функциональный/UI/производительность/безопасность).",
            screenshot_b64=post_screenshot_b64,
        )
        if post_answer:
            post_action = parse_llm_action(post_answer)
            if post_action and post_action.get("action") == "check_defect" and post_action.get("possible_bug"):
                bug_text = post_action["possible_bug"]
                if console_brief:
                    bug_text = f"{bug_text}\n\nНовые ошибки консоли после действия:\n{console_brief}"
                findings["bug_to_report"] = bug_text
        else:
            LOG.warning(
                "#%s оракул не ответил (LLM пуст) — fallback на правила без LLM",
                step,
            )

    # Фолбэк: даже если LLM ничего не вернул — pageerror и 4xx на основном/API
    # это надёжный сигнал, заводим дефект независимо.
    if not findings["bug_to_report"] and not findings["five_xx_bug"]:
        rule_bug = rule_pageerror(new_errors)
        if rule_bug:
            findings["bug_to_report"] = (
                f"{rule_bug['title']}\n\n{rule_bug['details']}"
                + (f"\n\nНовые ошибки консоли после действия:\n{console_brief}" if console_brief else "")
            )
            LOG.info("#%s правило rule_pageerror → дефект", step)
        else:
            rule_bug4 = rule_4xx_on_main(new_network, current_url)
            if rule_bug4:
                findings["bug_to_report"] = (
                    f"{rule_bug4['title']}\n\n{rule_bug4['details']}"
                    + (f"\n\nНовые ошибки консоли после действия:\n{console_brief}" if console_brief else "")
                )
                LOG.info("#%s правило rule_4xx_on_main → дефект", step)

    if not findings["bug_to_report"] and not findings["five_xx_bug"]:
        LOG.debug(
            "#%s дефекта нет: new_errors=%d, new_network=%d, oracle_error=%s, possible_bug=%s",
            step, len(new_errors), len(new_network),
            findings["oracle_error"], bool(possible_bug),
        )

    return findings


def _step_post_analysis(
    page, step, action, result, act_type, sel, val, expected_outcome, possible_bug,
    has_overlay, current_url, checklist_results, console_log, network_failures, memory,
):
    """STEP 4: Пост-анализ — быстрый сбор данных + фоновый анализ."""
    # Проверка: страница закрыта — не запускаем анализ
    if page.is_closed():
        return

    # Быстрый сбор из Playwright (main thread)
    try:
        post_data = _collect_post_data(page, has_overlay, memory)
    except Exception as e:
        if "closed" in str(e).lower() or "Target page" in str(e):
            return
        raise

    # Новый оверлей — обработать сразу
    if post_data["new_overlay"]:
        print(f"[Agent] #{step} Появился оверлей: {', '.join(post_data['overlay_types'])}")
        memory.add_action(
            {"action": "overlay_detected", "selector": ", ".join(post_data["overlay_types"])},
            result="new_overlay_appeared"
        )
        return

    # Снимки логов: берём ПОЛНЫЙ срез — в фоне выделим именно новые записи после действия.
    console_snapshot = list(console_log)
    network_snapshot = list(network_failures)
    before_screenshot = memory.screenshot_before_action
    # Запомним границы (сколько записей было ДО действия), чтобы в фоне брать именно «новые».
    action["_pre_action_lens"] = {
        "console": memory.console_len_before_action,
        "network": memory.network_len_before_action,
    }

    # Запускаем анализ В ФОНЕ — main thread свободен для следующего шага
    future = _bg_submit(
        _analyze_in_background,
        post_data, step, action, result, act_type, sel, val, expected_outcome, possible_bug,
        current_url, checklist_results, console_snapshot, network_snapshot, memory,
        before_screenshot,
    )

    # Сохраняем future — main thread проверит результат в начале следующего шага
    memory._pending_analysis = {
        "future": future,
        "step": step,
        "current_url": current_url,
        "checklist_results": checklist_results,
    }


def _flush_pending_analysis(page, memory, console_log, network_failures):
    """
    Проверить результат фонового анализа предыдущего шага.
    Вызывается в начале следующего шага — к этому моменту фон уже готов.
    """
    pending = getattr(memory, '_pending_analysis', None)
    if not pending:
        return
    memory._pending_analysis = None

    future = pending["future"]
    findings = _bg_result(future, timeout=10.0, default={})
    if not findings:
        return

    step = pending["step"]
    current_url = pending["current_url"]
    checklist_results = pending["checklist_results"]

    # 5xx дефект
    if findings.get("five_xx_bug") and memory.defects_on_current_step == 0:
        _create_defect(page, findings["five_xx_bug"], current_url, checklist_results, console_log, network_failures, memory)
        memory.defects_on_current_step += 1

    # Баг от пост-анализа
    if findings.get("bug_to_report") and memory.defects_on_current_step == 0:
        pbug = findings["bug_to_report"]
        if ENABLE_SECOND_PASS_BUG and not ask_is_this_really_bug(pbug, None):
            LOG.info("#{step} Фоновый анализ: не баг.")
        else:
            _create_defect(page, pbug, current_url, checklist_results, console_log, network_failures, memory)
            memory.defects_on_current_step += 1


def _step_post_analysis_LEGACY(
    page, step, action, result, act_type, sel, val, expected_outcome, possible_bug,
    has_overlay, current_url, checklist_results, console_log, network_failures, memory,
):
    """LEGACY: синхронный пост-анализ (fallback если пул сломан)."""

    post_overlay = detect_active_overlays(page)
    if post_overlay.get("has_overlay") and not has_overlay:
        overlay_types = [o.get("type", "?") for o in post_overlay.get("overlays", [])]
        print(f"[Agent] #{step} Появился оверлей: {', '.join(overlay_types)}")
        memory.add_action(
            {"action": "overlay_detected", "selector": ", ".join(overlay_types)},
            result="new_overlay_appeared"
        )
        time.sleep(0.5)
        return

    post_screenshot_b64 = take_screenshot_b64(page)

    visual_diff_info = compute_screenshot_diff(memory.screenshot_before_action, post_screenshot_b64)
    if visual_diff_info.get("changed"):
        diff_pct = visual_diff_info.get("change_percent", 0)
        diff_zone = visual_diff_info.get("diff_zone", "?")
        if diff_pct > 0:
            LOG.info("#{step} Visual diff: %s (%.1f%%)", diff_zone, diff_pct)

    from src.defect_rules import is_noise_url, is_noise_console_text
    new_errors = [
        c for c in console_log[-10:]
        if c.get("type") == "error" and not is_noise_console_text(c.get("text") or "")
    ]
    new_network_fails = [
        n for n in network_failures[-5:]
        if n.get("status") and n.get("status") >= 500 and not is_noise_url(n.get("url") or "")
    ]

    if new_network_fails and memory.defects_on_current_step == 0:
        five_xx_detail = "\n".join(
            f"- {n.get('status')} {n.get('method', 'GET')} {n.get('url', '')[:120]}"
            for n in new_network_fails[-10:]
        )
        bug_5xx = (
            f"HTTP 5xx после действия агента.\n\n"
            f"Действие: {act_type} | selector: {sel[:100]} | value: {val[:50]}\n\n"
            f"Неуспешные запросы:\n{five_xx_detail}"
        )
        _create_defect(page, bug_5xx, current_url, checklist_results, console_log, network_failures, memory)
        memory.defects_on_current_step += 1

    oracle_says_error = False
    if ENABLE_ORACLE_AFTER_ACTION and act_type in ("type", "click") and post_screenshot_b64 and memory.defects_on_current_step == 0:
        update_llm_overlay(page, prompt=f"#{step} Оракул", loading=True)
        expected_text = f"Ожидалось: {expected_outcome[:200]}" if expected_outcome else "Ожидался успешный результат."
        vdiff_text = ""
        if visual_diff_info.get("changed"):
            vdiff_text = f" Visual diff: {visual_diff_info.get('detail', '')}."
        oracle_context = f"Действие: {act_type} -> {sel[:60]}. Результат: {result}. {expected_text}{vdiff_text}"
        oracle_ans = consult_agent_with_screenshot(
            oracle_context,
            "Произошло ли ожидаемое? Ответь: успех / ошибка / неясно.",
            screenshot_b64=post_screenshot_b64,
        )
        update_llm_overlay(page, prompt=f"#{step} Оракул", response=oracle_ans or "—", loading=False)
        if oracle_ans and "ошибка" in oracle_ans.lower():
            oracle_says_error = True

    if memory.defects_on_current_step == 0 and (new_errors or possible_bug or oracle_says_error):
        post_context = f"""Действие: {action.get('action')} -> {action.get('selector', '')}.
Результат: {result}
Ошибки консоли: {', '.join(e.get('text', '')[:60] for e in new_errors[-3:])}
{"Оракул: ошибка." if oracle_says_error else ""}
Баг или нормальное поведение? Если баг — JSON с action=check_defect и possible_bug."""
        update_llm_overlay(page, prompt=f"#{step} Анализ…", loading=True)
        post_answer = consult_agent_with_screenshot(post_context, "Это баг или нет?", screenshot_b64=post_screenshot_b64)
        update_llm_overlay(page, prompt=f"#{step} Анализ", response=post_answer or "", loading=False)

        if post_answer:
            post_action = parse_llm_action(post_answer)
            if post_action and post_action.get("action") == "check_defect" and post_action.get("possible_bug"):
                pbug = post_action["possible_bug"]
                if ENABLE_SECOND_PASS_BUG and not ask_is_this_really_bug(pbug, post_screenshot_b64):
                    print(f"[Agent] #{step} Второй проход: не баг.")
                    return
                _create_defect(page, pbug, current_url, checklist_results, console_log, network_failures, memory)
                memory.defects_on_current_step += 1
                return
        else:
            LOG.warning("#%s оракул не ответил (LLM пуст) — fallback на правила", step)

        from src.defect_rules import rule_pageerror as _rule_pe
        rb = _rule_pe(new_errors)
        if rb and memory.defects_on_current_step == 0:
            pbug = f"{rb['title']}\n\n{rb['details']}"
            print(f"[Agent] #{step} Дефект по правилу rule_pageerror (LLM не ответил)")
            _create_defect(page, pbug, current_url, checklist_results, console_log, network_failures, memory)
            memory.defects_on_current_step += 1


# Создание дефекта вынесено в src/defect_pipeline.py (вместе с фоновой
# отправкой в Jira и семантической дедупликацией). Здесь — реэкспорт под
# прежними приватными именами, чтобы не править все вызовы.
from src.defect_pipeline import (
    check_broken_links_bg as _check_broken_links_bg,
    create_defect as _create_defect,
    is_semantic_duplicate as _is_semantic_duplicate,
)


# ===== Продвинутые проверки =====
# A11y, perf, responsive, iframe, scenario chains вынесены в src/agent_checks.py.
# Self-heal остаётся здесь — он использует execute_action из этого же файла
# и слишком сильно завязан на внутренний state.
from src.agent_checks import (
    request_scenario_chain as _request_scenario_chain,
    run_a11y_check as _run_a11y_check,
    run_iframe_check as _run_iframe_check,
    run_perf_check as _run_perf_check,
    run_responsive_check as _run_responsive_check,
    run_session_persistence_check as _run_session_persistence_check,
)


def _self_heal(page: Page, memory: AgentMemory, console_log, network_failures):
    """
    Self-healing: после серии неудач ИЛИ зацикливания — мета-рефлексия.
    Спрашиваем GigaChat «что пошло не так и что делать?».
    """
    is_stuck = memory.is_stuck()
    reason = f"{memory._consecutive_repeats} повторов подряд" if is_stuck else f"{memory.consecutive_failures} неудач подряд"
    print(f"[Agent] 🚨 Self-healing: {reason}")
    
    screenshot_b64 = take_screenshot_b64(page)
    recent_actions = "\n".join(
        f"  #{a['step']} {a['action']} -> {a['selector'][:40]} => {a['result'][:40]}"
        for a in memory.actions[-8:]
    )
    done_list = memory.get_history_text(last_n=10)
    
    prompt = f"""Агент {'зациклился (повторяет одни и те же действия)' if is_stuck else 'не может выполнить действия (ошибки)'}.
Последние действия:\n{recent_actions}\n\n{done_list}\n
Что идёт не так? Предложи ОДНО действие, которого НЕТ в списке "УЖЕ СДЕЛАНО" выше.
Действие должно быть НОВЫМ (не повторять уже сделанное). JSON с action/selector/value/reason."""
    
    answer = consult_agent_with_screenshot(
        prompt,
        "Предложи одно действие, которое точно сработает и НЕ будет повторением. JSON.",
        screenshot_b64=screenshot_b64,
    )
    
    # Сбросить счётчики
    memory.consecutive_failures = 0
    memory.reset_repeats()
    
    if answer:
        action = parse_llm_action(answer)
        if action:
            action = validate_llm_action(action)
            enrich_action(page, memory, action)
            act = (action.get("action") or "").lower()
            sel = (action.get("selector") or "").strip()
            if act != "check_defect" and memory.is_already_done_action(action):
                print(f"[Agent] Self-heal предложил повтор: {act} -> {sel[:40]}. Игнорирую.")
                action = {"action": "scroll", "selector": "up", "reason": "Self-heal: прокрутка для поиска новых элементов"}
                enrich_action(page, memory, action)
            execute_action(page, action, memory)
            memory.add_action(action, result="self_heal")
    
    # Принудительная смена фазы
    memory.advance_tester_phase(force=True)
    # Очистить scenario queue при зацикливании
    if is_stuck and hasattr(memory, '_scenario_queue'):
        memory._scenario_queue = []
        print("[Agent] Очищена очередь scenario chain из-за зацикливания")


def _check_network_after_action(page: Page, memory: AgentMemory, action: Dict, network_failures: list) -> Optional[str]:
    """
    После click по кнопке формы — проверить, что ушёл сетевой запрос.
    Возвращает описание проблемы или None.
    """
    act = (action.get("action") or "").lower()
    sel = (action.get("selector") or "").lower()
    # Проверяем только после клика по «отправить/сохранить/submit»
    submit_keywords = ["submit", "отправ", "сохран", "save", "send", "войти", "login", "sign", "register", "зарегистр"]
    if act != "click" or not any(kw in sel for kw in submit_keywords):
        return None
    new_after = network_failures[memory.network_len_before_action:]
    # Ищем POST/PUT
    post_put = [n for n in new_after if n.get("method", "").upper() in ("POST", "PUT", "PATCH")]
    if not new_after and not post_put:
        # Вообще нет новых запросов — возможно кнопка не работает
        return f"После нажатия '{sel[:40]}' не обнаружено сетевых запросов. Кнопка может не работать."
    # Есть 4xx/5xx
    errors = [n for n in new_after if n.get("status") and n.get("status") >= 400]
    if errors:
        detail = "; ".join(f"{n.get('status')} {n.get('url', '')[:50]}" for n in errors[:3])
        return f"После нажатия '{sel[:40]}' получены ошибки: {detail}"
    return None


def _track_test_plan(memory: AgentMemory, action: Dict):
    """Отследить, какой пункт тест-плана закрыт текущим действием."""
    if not memory.test_plan or not memory.test_plan_completed:
        return
    reason = (action.get("reason") or "").lower()
    test_goal = (action.get("test_goal") or "").lower()
    sel = (action.get("selector") or "").lower()
    combined = f"{reason} {test_goal} {sel}"
    for i, step in enumerate(memory.test_plan):
        if memory.test_plan_completed[i]:
            continue
        step_lower = step.lower()
        # Простая эвристика: если 2+ слова из пункта плана встречаются в действии
        words = [w for w in step_lower.split() if len(w) > 3]
        matches = sum(1 for w in words if w in combined)
        if matches >= 2 or (len(words) <= 2 and matches >= 1):
            memory.mark_test_plan_step(i)
            print(f"[Agent] Тест-план: закрыт пункт {i+1}: {step[:50]}")
            break
