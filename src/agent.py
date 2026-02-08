"""
AI-агент тестировщик: активно ходит по сайту, кликает, заполняет формы,
скринит экран и отправляет в GigaChat за советом. Многофазный цикл:
1) Скриншот + контекст → GigaChat (что вижу, что делать?)
2) Выполняем действие (click, type, scroll, hover)
3) Скриншот после действия → GigaChat (что произошло, есть баг?)
4) Если баг → Jira. Если нет → следующее действие.
Все действия видимы. Память действий — не повторяемся.
"""
import base64
import json
import os
import re
import shutil
import time
from datetime import datetime
from typing import List, Dict, Any, Optional

from playwright.sync_api import sync_playwright, Page

from config import (
    START_URL,
    BROWSER_SLOW_MO,
    HEADLESS,
    CHECKLIST_STEP_DELAY_MS,
    VIEWPORT_WIDTH,
    VIEWPORT_HEIGHT,
)
from src.gigachat_client import consult_agent_with_screenshot, consult_agent
from src.jira_client import create_jira_issue
from src.page_analyzer import build_context, get_dom_summary, detect_active_overlays, format_overlays_context
from src.visible_actions import (
    inject_cursor,
    move_cursor_to,
    highlight_and_click,
    safe_highlight,
    inject_llm_overlay,
    update_llm_overlay,
    inject_demo_banner,
    update_demo_banner,
    show_highlight_label,
)
from src.wait_utils import smart_wait_after_goto
from src.checklist import run_checklist, checklist_results_to_context
from src.defect_builder import build_defect_summary, build_defect_description, collect_evidence


# --- Память агента ---
class AgentMemory:
    """Хранит историю действий, чтобы не повторяться и давать GigaChat контекст."""

    def __init__(self, max_actions: int = 50):
        self.actions: List[Dict[str, Any]] = []
        self.max_actions = max_actions
        self.defects_reported: List[str] = []
        self.elements_clicked: set = set()
        self.iteration = 0

    def add_action(self, action: Dict[str, Any], result: str = ""):
        self.iteration += 1
        entry = {
            "step": self.iteration,
            "time": datetime.now().strftime("%H:%M:%S"),
            "action": action.get("action", ""),
            "selector": action.get("selector", ""),
            "reason": action.get("reason", ""),
            "result": result[:200],
        }
        self.actions.append(entry)
        if len(self.actions) > self.max_actions:
            self.actions = self.actions[-self.max_actions:]
        sel = action.get("selector", "")
        if sel and action.get("action") == "click":
            self.elements_clicked.add(sel[:100])

    def get_history_text(self, last_n: int = 15) -> str:
        if not self.actions:
            return "История пуста — это первое действие."
        lines = ["Последние действия агента:"]
        for a in self.actions[-last_n:]:
            lines.append(f"  #{a['step']} [{a['time']}] {a['action']} -> {a['selector'][:50]} | {a['result'][:60]}")
        if self.elements_clicked:
            lines.append(f"Уже кликнуто ({len(self.elements_clicked)}): {', '.join(list(self.elements_clicked)[-10:])}")
        return "\n".join(lines)


# --- Скриншот в base64 ---
def take_screenshot_b64(page: Page) -> Optional[str]:
    """Сделать скриншот и вернуть base64-строку."""
    try:
        raw = page.screenshot(type="png")
        return base64.b64encode(raw).decode("ascii")
    except Exception as e:
        print(f"[Agent] Ошибка скриншота: {e}")
        return None


# --- Парсинг JSON-ответа от GigaChat ---
def parse_llm_action(raw: str) -> Optional[Dict[str, Any]]:
    """Попытаться распарсить JSON-действие из ответа GigaChat."""
    if not raw:
        return None
    # Убираем markdown code block если есть
    cleaned = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r'```\s*$', '', cleaned.strip(), flags=re.MULTILINE)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and "action" in obj:
            return obj
    except json.JSONDecodeError:
        pass
    # Попробуем найти JSON в тексте
    m = re.search(r'\{[^{}]*"action"\s*:\s*"[^"]+?"[^{}]*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


# --- Выполнение действия ---
def execute_action(page: Page, action: Dict[str, Any], memory: AgentMemory) -> str:
    """Выполнить действие на странице. Возвращает текстовый результат."""
    act = action.get("action", "").lower()
    selector = action.get("selector", "").strip()
    value = action.get("value", "").strip()
    reason = action.get("reason", "")

    print(f"[Agent] Действие: {act} -> {selector[:60]} | {reason[:60]}")

    if act == "click":
        return _do_click(page, selector, reason)
    elif act == "type":
        return _do_type(page, selector, value)
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
    elif act == "check_defect":
        return "defect_found"
    else:
        print(f"[Agent] Неизвестное действие: {act}, пробую клик")
        return _do_click(page, selector, reason) if selector else "no_action"


def _find_element(page: Page, selector: str):
    """Попытаться найти элемент по разным стратегиям."""
    strategies = []
    if selector.startswith((".", "#", "[", "//", "button", "a", "input", "div", "span")):
        strategies.append(("css/xpath", lambda: page.locator(selector).first))
    # По тексту (основная стратегия)
    safe_text = selector.replace('"', '\\"')[:80]
    strategies.extend([
        ("button:text", lambda: page.locator(f'button:has-text("{safe_text}")').first),
        ("a:text", lambda: page.locator(f'a:has-text("{safe_text}")').first),
        ("role=button", lambda: page.locator(f'[role="button"]:has-text("{safe_text}")').first),
        ("input:text", lambda: page.locator(f'input:has-text("{safe_text}")').first),
        ("any:text", lambda: page.locator(f'text="{safe_text}"').first),
        ("getByText", lambda: page.get_by_text(safe_text, exact=False).first),
        ("getByRole", lambda: page.get_by_role("button", name=safe_text).first),
    ])
    for name, get_loc in strategies:
        try:
            loc = get_loc()
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
            safe_highlight(loc, page, 0.4)
            highlight_and_click(loc, page, description=reason[:30] or "Клик")
            return f"clicked: {selector[:50]}"
        except Exception as e:
            return f"click_error: {e}"
    return f"not_found: {selector[:50]}"


def _do_type(page: Page, selector: str, value: str) -> str:
    if not selector or not value:
        return "no_selector_or_value"
    loc = _find_element(page, selector)
    if not loc:
        # Попробуем найти ближайший input / textarea
        for inp_sel in ["input[type='text']", "input[type='email']", "input[type='search']", "textarea", "input:not([type='hidden'])"]:
            try:
                loc = page.locator(inp_sel).first
                if loc.count() > 0 and loc.is_visible():
                    break
                loc = None
            except Exception:
                loc = None
    if loc:
        try:
            safe_highlight(loc, page, 0.3)
            box = loc.bounding_box()
            if box:
                move_cursor_to(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                show_highlight_label(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, f"Ввожу: {value[:20]}")
            loc.click()
            loc.fill(value)
            time.sleep(0.5)
            return f"typed: {value[:30]} into {selector[:30]}"
        except Exception as e:
            return f"type_error: {e}"
    return f"input_not_found: {selector[:50]}"


def _do_scroll(page: Page, direction: str) -> str:
    try:
        if direction.lower() in ("down", "вниз", ""):
            page.evaluate("window.scrollBy(0, 600)")
            return "scrolled_down"
        elif direction.lower() in ("up", "вверх"):
            page.evaluate("window.scrollBy(0, -600)")
            return "scrolled_up"
        else:
            loc = _find_element(page, direction)
            if loc:
                loc.scroll_into_view_if_needed()
                safe_highlight(loc, page, 0.3)
                return f"scrolled_to: {direction[:30]}"
            page.evaluate("window.scrollBy(0, 600)")
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


def _do_press_key(page: Page, key: str) -> str:
    """Нажать клавишу (Escape, Enter, Tab и т.д.)."""
    try:
        page.keyboard.press(key)
        time.sleep(0.5)
        return f"key_pressed: {key}"
    except Exception as e:
        return f"key_error: {e}"


# --- Инициализация страницы ---
def _inject_all(page: Page):
    """Инжектировать все визуальные элементы."""
    inject_cursor(page)
    inject_llm_overlay(page)
    inject_demo_banner(page)


def _same_page(start_url: str, current_url: str) -> bool:
    def norm(u):
        return (u or "").split("#")[0].rstrip("/").lower()
    return norm(current_url) == norm(start_url)


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
            update_demo_banner(main_page, step_text=f"Новая вкладка: {tab_url[:40]}…", progress_pct=50)

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
                _create_defect(main_page, bug_desc, tab_url, [], console_log, network_failures)
                memory.add_action({"action": "new_tab_error", "selector": tab_url}, result="defect_reported")
            else:
                # Загрузка успешна
                load_ok = True
                print(f"[Agent] #{step} Новая вкладка OK: {tab_url[:60]} → закрываю")
                update_demo_banner(main_page, step_text=f"Вкладка OK: {tab_url[:30]}. Закрываю.", progress_pct=70)
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
            _create_defect(main_page, bug_desc, tab_url, [], console_log, network_failures)
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
    start_url = start_url or START_URL
    if not start_url.startswith("http"):
        start_url = "https://" + start_url

    console_log: List[Dict[str, Any]] = []
    network_failures: List[Dict[str, Any]] = []
    memory = AgentMemory()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=BROWSER_SLOW_MO)
        context = browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            ignore_https_errors=True,
        )
        page = context.new_page()

        # --- Обработка новых вкладок (target="_blank" и т.п.) ---
        new_tabs_queue: List[Any] = []   # очередь вкладок для обработки

        def _on_new_page(new_page):
            """Перехватываем открытие новой вкладки."""
            print(f"[Agent] Новая вкладка обнаружена")
            new_tabs_queue.append(new_page)

        context.on("page", _on_new_page)

        def on_console(msg):
            console_log.append({"type": msg.type, "text": msg.text})
        page.on("console", on_console)
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
        page.on("response", on_response)
        page._agent_network_failures = network_failures

        # Загрузка начальной страницы
        try:
            page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            smart_wait_after_goto(page, timeout=15000)
            _inject_all(page)
        except Exception as e:
            print(f"[Agent] Ошибка загрузки {start_url}: {e}")
            browser.close()
            return

        print(f"[Agent] Старт тестирования: {start_url}")
        print(f"[Agent] Бесконечный цикл. Ctrl+C для остановки.")

        while True:
            memory.iteration += 1
            step = memory.iteration
            current_url = page.url

            # ========== Обработка новых вкладок ==========
            _handle_new_tabs(
                new_tabs_queue, page, start_url, step,
                console_log, network_failures, memory,
            )

            # Проверка: ушли на другую страницу (навигация в той же вкладке) → вернуться
            if not _same_page(start_url, page.url):
                print(f"[Agent] #{step} Навигация: {page.url[:60]}. Возврат на {start_url}")
                update_demo_banner(page, step_text="Возврат на основную страницу…", progress_pct=0)
                try:
                    page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                    smart_wait_after_goto(page, timeout=10000)
                    _inject_all(page)
                except Exception as e:
                    print(f"[Agent] Ошибка возврата: {e}")
                continue

            # Лимит логов
            if len(console_log) > 150:
                del console_log[:-100]
            if len(network_failures) > 80:
                del network_failures[:-50]

            # ========== PHASE 1: Чеклист (каждые 5 итераций) ==========
            checklist_results = []
            if step % 5 == 1:
                smart_wait_after_goto(page, timeout=5000)
                def on_step(step_id, ok, detail, step_index, total):
                    st = "✅" if ok else "❌"
                    pct = round(100 * step_index / total) if total else 0
                    update_demo_banner(page, step_text=f"Чеклист {step_index}/{total}: {step_id}", progress_pct=pct)
                    update_llm_overlay(page, prompt=f"Чеклист: {step_id}", response=f"{st} {detail[:120]}", loading=False)
                checklist_results = run_checklist(page, console_log, network_failures, step_delay_ms=CHECKLIST_STEP_DELAY_MS, on_step=on_step)

            # ========== PHASE 2: Обнаружение оверлеев + Скриншот + контекст → GigaChat ==========
            update_demo_banner(page, step_text=f"#{step} Анализ страницы…", progress_pct=25)

            # Детекция модалок, тултипов, дропдаунов
            overlay_info = detect_active_overlays(page)
            overlay_context = format_overlays_context(overlay_info)
            has_overlay = overlay_info.get("has_overlay", False)

            if has_overlay:
                overlay_types = [o.get("type", "?") for o in overlay_info.get("overlays", [])]
                print(f"[Agent] #{step} Обнаружены оверлеи: {', '.join(overlay_types)}")
                update_demo_banner(page, step_text=f"#{step} Оверлей: {', '.join(overlay_types)}!", progress_pct=30)

            update_demo_banner(page, step_text=f"#{step} Скриншот для GigaChat…", progress_pct=35)
            screenshot_b64 = take_screenshot_b64(page)

            context_str = build_context(page, current_url, console_log, network_failures)
            if checklist_results:
                context_str = checklist_results_to_context(checklist_results) + "\n\n" + context_str
            if overlay_context:
                context_str = overlay_context + "\n\n" + context_str
            dom_summary = get_dom_summary(page, max_length=4000)
            history_text = memory.get_history_text(last_n=10)

            if has_overlay:
                question = f"""Вот скриншот. На странице есть АКТИВНЫЙ ОВЕРЛЕЙ (модалка/дропдаун/тултип/попап).

{overlay_context}

DOM (все элементы):
{dom_summary[:3000]}

{history_text}

ВАЖНО: Сейчас на экране оверлей! Действуй так:
1) Если ещё НЕ протестировал содержимое оверлея — тестируй его (кликай кнопки внутри, заполняй поля, проверяй ссылки)
2) Если уже протестировал — закрой оверлей (action=close_modal) и переходи к другим элементам
3) Если видишь баг в оверлее — action=check_defect
Выбери ОДНО действие."""
            else:
                question = f"""Вот скриншот и контекст страницы.

DOM (кнопки, ссылки, формы):
{dom_summary[:3000]}

{history_text}

Выбери ОДНО следующее действие. Не повторяй те элементы, на которые уже кликнул. Ищи новые кнопки, формы, ссылки. Будь активным тестировщиком!
Попробуй hover на элементы с подменю/тултипами. Открывай дропдауны и выбирай опции.
Если видишь реальный баг — укажи action=check_defect."""

            update_demo_banner(page, step_text=f"#{step} Консультация с GigaChat…", progress_pct=60)
            update_llm_overlay(page, prompt=f"#{step} Что делать дальше?", loading=True)

            raw_answer = consult_agent_with_screenshot(context_str, question, screenshot_b64=screenshot_b64)
            update_llm_overlay(page, prompt=f"#{step} Что делать дальше?", response=raw_answer or "Нет ответа", loading=False, error="Нет ответа" if not raw_answer else None)

            if not raw_answer:
                print(f"[Agent] #{step} GigaChat недоступен, пауза 10с")
                time.sleep(10)
                continue

            action = parse_llm_action(raw_answer)
            if not action:
                print(f"[Agent] #{step} Не удалось распарсить JSON. Ответ: {raw_answer[:120]}")
                action = {"action": "scroll", "selector": "down", "reason": "GigaChat не дал JSON, прокрутка"}

            observation = action.get("observation", "")
            reason = action.get("reason", "")
            possible_bug = action.get("possible_bug")
            print(f"[Agent] #{step} Наблюдение: {observation[:80]}")
            print(f"[Agent] #{step} Действие: {action.get('action')} -> {action.get('selector', '')[:40]} | {reason[:50]}")

            # ========== PHASE 3: Выполнение действия ==========
            update_demo_banner(page, step_text=f"#{step} {action.get('action', '').upper()}: {action.get('selector', '')[:30]}…", progress_pct=80)

            if action.get("action") == "check_defect" and possible_bug:
                _create_defect(page, possible_bug, current_url, checklist_results, console_log, network_failures)
                memory.add_action(action, result="defect_reported")
                time.sleep(3)
                continue

            # Запомним кол-во вкладок ДО действия
            pages_before = len(context.pages)

            result = execute_action(page, action, memory)
            memory.add_action(action, result=result)
            print(f"[Agent] #{step} Результат: {result}")

            # Пауза после действия для загрузки
            time.sleep(1.5)
            smart_wait_after_goto(page, timeout=3000)

            # Обработать вкладки, которые могли открыться из-за этого действия
            _handle_new_tabs(
                new_tabs_queue, page, start_url, step,
                console_log, network_failures, memory,
            )

            # ========== PHASE 4: Пост-анализ после действия ==========
            update_demo_banner(page, step_text=f"#{step} Анализ результата…", progress_pct=90)

            # Проверяем: появился ли оверлей после действия?
            post_overlay = detect_active_overlays(page)
            if post_overlay.get("has_overlay") and not has_overlay:
                # Оверлей появился! Следующая итерация займётся им
                overlay_types = [o.get("type", "?") for o in post_overlay.get("overlays", [])]
                print(f"[Agent] #{step} После действия появился оверлей: {', '.join(overlay_types)}")
                update_demo_banner(page, step_text=f"#{step} Появился оверлей! Тестирую…", progress_pct=95)
                memory.add_action(
                    {"action": "overlay_detected", "selector": ", ".join(overlay_types)},
                    result="new_overlay_appeared"
                )
                # Не ждём — сразу к следующей итерации, чтобы протестировать оверлей
                time.sleep(0.5)
                continue

            update_demo_banner(page, step_text=f"#{step} Анализ результата…", progress_pct=95)
            post_screenshot_b64 = take_screenshot_b64(page)

            new_errors = [c for c in console_log[-10:] if c.get("type") == "error"]
            new_network_fails = [n for n in network_failures[-5:] if n.get("status") and n.get("status") >= 500]

            if new_errors or new_network_fails or possible_bug:
                post_context = f"""Я выполнил действие: {action.get('action')} -> {action.get('selector', '')}.
Результат: {result}
Новые ошибки консоли: {', '.join(e.get('text', '')[:60] for e in new_errors[-3:])} 
Новые 5xx ответы: {', '.join(f"{n.get('status')} {n.get('url', '')[:40]}" for n in new_network_fails[-3:])}

Это баг приложения или нормальное поведение? Если баг — ответь JSON с action=check_defect и possible_bug."""
                update_llm_overlay(page, prompt=f"#{step} Есть ошибки: анализ…", loading=True)
                post_answer = consult_agent_with_screenshot(post_context, "Проанализируй: это баг или нет?", screenshot_b64=post_screenshot_b64)
                update_llm_overlay(page, prompt=f"#{step} Анализ ошибок", response=post_answer or "", loading=False)

                if post_answer:
                    post_action = parse_llm_action(post_answer)
                    if post_action and post_action.get("action") == "check_defect" and post_action.get("possible_bug"):
                        _create_defect(page, post_action["possible_bug"], current_url, checklist_results, console_log, network_failures)

            update_demo_banner(page, step_text=f"#{step} Готово. Следующий шаг…", progress_pct=100)
            time.sleep(1)

        browser.close()


def _create_defect(
    page: Page,
    bug_description: str,
    current_url: str,
    checklist_results: List[Dict[str, Any]],
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
):
    """Создать дефект в Jira с полной фактурой."""
    summary = build_defect_summary(bug_description, current_url)
    description = build_defect_description(
        bug_description, current_url,
        checklist_results=checklist_results,
        console_log=console_log,
        network_failures=network_failures,
    )
    attachment_paths = collect_evidence(page, console_log, network_failures)
    key = create_jira_issue(summary=summary, description=description, attachment_paths=attachment_paths or None)
    if key:
        print(f"[Agent] Дефект создан: {key}")
        update_llm_overlay(page, prompt="Дефект создан!", response=f"{key}: {summary[:80]}", loading=False)
    if attachment_paths:
        try:
            d = os.path.dirname(attachment_paths[0])
            if d and os.path.isdir(d) and "kventin_defect_" in d:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
