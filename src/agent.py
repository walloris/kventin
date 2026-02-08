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
    ENABLE_TEST_PLAN_START,
    ENABLE_ORACLE_AFTER_ACTION,
    ENABLE_SECOND_PASS_BUG,
    ACTION_RETRY_COUNT,
    SESSION_REPORT_EVERY_N,
    CRITICAL_FLOW_STEPS,
)
from src.gigachat_client import (
    consult_agent_with_screenshot,
    consult_agent,
    get_test_plan_from_screenshot,
    ask_is_this_really_bug,
)
from src.jira_client import create_jira_issue
from src.page_analyzer import (
    build_context,
    get_dom_summary,
    detect_active_overlays,
    format_overlays_context,
    detect_cookie_banner,
)
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


# --- Нормализация ключа для дедупликации ---
def _norm_key(s: str, max_len: int = 80) -> str:
    """Единый ключ для сравнения: без повторов из-за пробелов/регистра."""
    if not s:
        return ""
    return s.strip().lower().replace("\n", " ").replace("\r", " ")[:max_len]


# --- Память агента ---
class AgentMemory:
    """
    Хранит всё, что агент уже делал, чтобы не ходить по циклу.
    Учитываются: клики, ховеры, ввод в поля, закрытие модалок, выбор опций, прокрутки.
    """

    def __init__(self, max_actions: int = 80):
        self.actions: List[Dict[str, Any]] = []
        self.max_actions = max_actions
        self.defects_reported: List[str] = []
        self.iteration = 0
        # Ключи (normalized) уже выполненных действий — НЕ ПОВТОРЯТЬ
        self.done_click: set = set()       # selector/text по которому кликали
        self.done_hover: set = set()      # selector/text по которому наводили
        self.done_type: set = set()       # placeholder/name поля или "selector" куда вводили
        self.done_close_modal: int = 0    # сколько раз закрывали модалку (не дублируем бесконечно)
        self.done_select_option: set = set()  # ("selector", "value") или просто value
        self.done_scroll_down: int = 0
        self.done_scroll_up: int = 0
        # Лимиты, чтобы не зациклиться на одном типе действия
        self.max_scrolls_in_row = 5
        self.last_actions_sequence: List[str] = []
        # Карта покрытия: какие зоны страницы уже обходили (top/middle/bottom)
        self.coverage_zones: List[str] = []
        # Тест-план от GigaChat в начале сессии (список шагов)
        self.test_plan: List[str] = []
        # Критические шаги: индексы выполненных (из config CRITICAL_FLOW_STEPS)
        self.critical_flow_done: set = set()
        # Созданные дефекты за сессию (для отчёта)
        self.defects_created: List[Dict[str, Any]] = []
        # Время старта сессии
        self.session_start: Optional[datetime] = None

    def add_action(self, action: Dict[str, Any], result: str = ""):
        act = (action.get("action") or "").lower()
        sel = _norm_key(action.get("selector", ""))
        val = _norm_key(action.get("value", ""))

        self.iteration += 1
        entry = {
            "step": self.iteration,
            "time": datetime.now().strftime("%H:%M:%S"),
            "action": act,
            "selector": action.get("selector", ""),
            "reason": action.get("reason", ""),
            "result": result[:200],
        }
        self.actions.append(entry)
        if len(self.actions) > self.max_actions:
            self.actions = self.actions[-self.max_actions:]

        # Запоминаем для дедупликации
        if act == "click" and sel:
            self.done_click.add(sel)
        elif act == "hover" and sel:
            self.done_hover.add(sel)
        elif act == "type" and (sel or val):
            self.done_type.add(sel or val)
        elif act == "close_modal":
            self.done_close_modal += 1
        elif act == "select_option" and (sel or val):
            self.done_select_option.add((sel, val) if sel and val else (sel or val,))
        elif act == "scroll":
            if sel in ("down", "вниз", ""):
                self.done_scroll_down += 1
            elif sel in ("up", "вверх"):
                self.done_scroll_up += 1

        self.last_actions_sequence.append(act)
        if len(self.last_actions_sequence) > 10:
            self.last_actions_sequence = self.last_actions_sequence[-10:]

    def is_already_done(self, action: str, selector: str = "", value: str = "") -> bool:
        """Проверить, не делали ли мы уже это действие (чтобы не повторять)."""
        act = (action or "").lower()
        sel = _norm_key(selector)
        val = _norm_key(value)
        if act == "click" and sel and sel in self.done_click:
            return True
        if act == "hover" and sel and sel in self.done_hover:
            return True
        if act == "type" and (sel or val) and (sel in self.done_type or val in self.done_type):
            return True
        if act == "select_option" and (sel or val):
            if (sel, val) in self.done_select_option:
                return True
            if val and (val,) in self.done_select_option:
                return True
            if sel and (sel,) in self.done_select_option:
                return True
        if act == "close_modal" and self.done_close_modal > 0:
            # Не считаем повторным, если модалка появилась снова — но можно ограничить подряд
            pass
        return False

    def should_avoid_scroll(self) -> bool:
        """Не прокручивать бесконечно: если недавно много scroll — предложить другое действие."""
        recent = self.last_actions_sequence[-5:] if self.last_actions_sequence else []
        scroll_count = sum(1 for a in recent if a == "scroll")
        return scroll_count >= self.max_scrolls_in_row

    def get_history_text(self, last_n: int = 20) -> str:
        """Текст для GigaChat: что уже сделано. НЕ ПОВТОРЯТЬ эти действия."""
        lines = [
            "——— УЖЕ СДЕЛАНО (НЕ ПОВТОРЯТЬ, выбирай другое действие!) ———",
        ]
        if self.done_click:
            items = sorted(self.done_click)[-25:]
            lines.append(f"Кликнуто ({len(self.done_click)}): " + ", ".join(f'"{x[:40]}"' for x in items))
        if self.done_hover:
            items = sorted(self.done_hover)[-15:]
            lines.append(f"Наведено (hover) ({len(self.done_hover)}): " + ", ".join(f'"{x[:40]}"' for x in items))
        if self.done_type:
            items = sorted(self.done_type)[-15:]
            lines.append(f"Ввод в поля ({len(self.done_type)}): " + ", ".join(f'"{x[:40]}"' for x in items))
        if self.done_close_modal:
            lines.append(f"Закрыто модалок: {self.done_close_modal}")
        if self.done_select_option:
            items = list(self.done_select_option)[:15]
            lines.append(f"Выбрано опций: " + ", ".join(str(x)[:50] for x in items))
        if self.done_scroll_down or self.done_scroll_up:
            lines.append(f"Прокручено: вниз {self.done_scroll_down}, вверх {self.done_scroll_up}")
        if self.should_avoid_scroll():
            lines.append("Внимание: недавно много прокруток — выбери клик/hover/type/close_modal, а не scroll.")
        lines.append("——— Конец списка. Выбери действие, которого ещё НЕТ в списке выше. ———")
        lines.append("")
        lines.append("Последние шаги:")
        for a in self.actions[-last_n:]:
            lines.append(f"  #{a['step']} {a['action']} -> {a['selector'][:45]} | {a['result'][:50]}")
        return "\n".join(lines)

    def record_coverage_zone(self, zone: str):
        """Учесть, что эту зону страницы уже обходили (top/middle/bottom)."""
        if zone and zone not in self.coverage_zones:
            self.coverage_zones.append(zone)
            if len(self.coverage_zones) > 20:
                self.coverage_zones = self.coverage_zones[-20:]

    def set_test_plan(self, steps: List[str]):
        self.test_plan = list(steps)[:15]

    def get_steps_to_reproduce(self, max_steps: int = 15) -> List[str]:
        """Шаги для воспроизведения дефекта (последние действия до бага)."""
        steps = []
        for a in self.actions[-max_steps:]:
            act = a.get("action", "")
            sel = (a.get("selector") or "").strip()
            if act == "click" and sel:
                steps.append(f"Клик по элементу: {sel[:60]}")
            elif act == "type" and sel:
                steps.append(f"Ввод в поле: {sel[:60]}")
            elif act == "hover" and sel:
                steps.append(f"Наведение на: {sel[:60]}")
            elif act == "close_modal":
                steps.append("Закрыть модальное окно")
            elif act == "select_option" and sel:
                steps.append(f"Выбрать опцию: {sel[:60]}")
            elif act == "scroll":
                steps.append("Прокрутка страницы")
        return steps

    def record_defect_created(self, key: str, summary: str):
        self.defects_created.append({"key": key, "summary": summary[:200]})

    def get_session_report_text(self) -> str:
        """Краткий отчёт сессии: шаги, покрытие, дефекты."""
        if not self.session_start:
            self.session_start = datetime.now()
        duration = (datetime.now() - self.session_start).total_seconds() if self.session_start else 0
        lines = [
            "=== Отчёт сессии AI-тестировщика Kventin ===",
            f"Шагов выполнено: {len(self.actions)}",
            f"Время: {duration:.0f} с",
            f"Зоны покрытия: {', '.join(self.coverage_zones) if self.coverage_zones else '—'}",
        ]
        if self.test_plan:
            lines.append("Тест-план: " + "; ".join(self.test_plan[:5]))
        if self.defects_created:
            lines.append(f"Создано дефектов: {len(self.defects_created)}")
            for d in self.defects_created[-10:]:
                lines.append(f"  - {d.get('key', '')}: {d.get('summary', '')[:60]}")
        lines.append("=== Конец отчёта ===")
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
                _create_defect(main_page, bug_desc, tab_url, [], console_log, network_failures, memory)
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

        memory.session_start = datetime.now()
        # Закрыть баннер cookies/согласия, если есть
        if try_accept_cookie_banner(page):
            time.sleep(1.5)
            smart_wait_after_goto(page, timeout=3000)

        # Тест-план в начале сессии (GigaChat по скриншоту предлагает 5–7 шагов)
        if ENABLE_TEST_PLAN_START:
            update_demo_banner(page, step_text="Получение тест-плана от GigaChat…", progress_pct=10)
            plan_screenshot = take_screenshot_b64(page)
            test_plan_steps = get_test_plan_from_screenshot(plan_screenshot, start_url)
            if test_plan_steps:
                memory.set_test_plan(test_plan_steps)
                print(f"[Agent] Тест-план ({len(test_plan_steps)} шагов): " + "; ".join(test_plan_steps[:3]) + "…")
                update_llm_overlay(page, prompt="Тест-план", response="; ".join(test_plan_steps[:4]), loading=False)

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
            history_text = memory.get_history_text(last_n=15)

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
                plan_hint = ""
                if memory.test_plan:
                    plan_hint = f"\nТест-план сессии: " + "; ".join(memory.test_plan[:6]) + "\nПо возможности выполняй шаги из плана по порядку.\n"
                if CRITICAL_FLOW_STEPS:
                    plan_hint += "\nКритический сценарий (приоритет): " + ", ".join(CRITICAL_FLOW_STEPS[:5]) + "\n"
                question = f"""Вот скриншот и контекст страницы.

DOM (кнопки, ссылки, формы):
{dom_summary[:3000]}

{history_text}
{plan_hint}

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

            act_type = (action.get("action") or "").lower()
            sel = (action.get("selector") or "").strip()
            val = (action.get("value") or "").strip()

            # Проверка: это действие уже делали? Не ходим по циклу.
            if act_type != "check_defect" and memory.is_already_done(act_type, sel, val):
                print(f"[Agent] #{step} Повтор действия (уже делали): {act_type} -> {sel[:40]}")
                update_llm_overlay(page, prompt=f"#{step} Повтор!", response=f"Уже делали: {act_type} {sel[:30]}. Запасное действие.", loading=False)
                if has_overlay:
                    action = {"action": "close_modal", "selector": "", "reason": "Повтор — закрываю оверлей"}
                elif not memory.should_avoid_scroll():
                    action = {"action": "scroll", "selector": "down", "reason": "Повтор — прокручиваю вниз"}
                else:
                    action = {"action": "scroll", "selector": "up", "reason": "Повтор — прокручиваю вверх"}
                act_type = action.get("action", "").lower()
                sel = action.get("selector", "")
                val = action.get("value", "")

            observation = action.get("observation", "")
            reason = action.get("reason", "")
            possible_bug = action.get("possible_bug")
            print(f"[Agent] #{step} Наблюдение: {observation[:80]}")
            print(f"[Agent] #{step} Действие: {act_type} -> {sel[:40]} | {reason[:50]}")

            # ========== PHASE 3: Выполнение действия ==========
            update_demo_banner(page, step_text=f"#{step} {act_type.upper()}: {sel[:30]}…", progress_pct=80)

            if act_type == "check_defect" and possible_bug:
                # Второй проход: это точно баг?
                if ENABLE_SECOND_PASS_BUG:
                    post_b64 = take_screenshot_b64(page)
                    if not ask_is_this_really_bug(possible_bug, post_b64):
                        print(f"[Agent] #{step} Второй проход: не баг, пропускаем тикет.")
                        update_llm_overlay(page, prompt="Ревью дефекта", response="Не баг (ожидаемое поведение)", loading=False)
                        memory.add_action(action, result="defect_skipped_second_pass")
                        time.sleep(1)
                        continue
                _create_defect(page, possible_bug, current_url, checklist_results, console_log, network_failures, memory)
                memory.add_action(action, result="defect_reported")
                time.sleep(3)
                continue

            # Запомним кол-во вкладок ДО действия
            pages_before = len(context.pages)

            # Выполнение с повтором при сбое (таймаут, not_found)
            result = ""
            for attempt in range(1 + max(0, ACTION_RETRY_COUNT)):
                result = execute_action(page, action, memory)
                if "error" not in result.lower() and "not_found" not in result.lower() and "no_selector" not in result.lower():
                    break
                if attempt < max(0, ACTION_RETRY_COUNT):
                    print(f"[Agent] #{step} Повтор попытки {attempt + 1}…")
                    time.sleep(1.0)

            memory.add_action(action, result=result)
            print(f"[Agent] #{step} Результат: {result}")

            # Карта покрытия: запомнить зону после прокрутки
            if act_type == "scroll":
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

            # Оракул: после ввода или клика — достигнут ли ожидаемый результат?
            oracle_says_error = False
            if ENABLE_ORACLE_AFTER_ACTION and act_type in ("type", "click") and post_screenshot_b64:
                update_llm_overlay(page, prompt=f"#{step} Оракул: успех или ошибка?", loading=True)
                oracle_ans = consult_agent_with_screenshot(
                    f"Последнее действие: {act_type} -> {sel[:60]}. Результат: {result}",
                    "На скриншоте после этого действия виден успех (всё ок, форма отправилась, данные отобразились) или ошибка (сообщение об ошибке, пустая страница, что-то сломалось)? Ответь одним словом: успех / ошибка / неясно.",
                    screenshot_b64=post_screenshot_b64,
                )
                update_llm_overlay(page, prompt=f"#{step} Оракул", response=oracle_ans or "—", loading=False)
                if oracle_ans and "ошибка" in oracle_ans.lower():
                    oracle_says_error = True

            if new_errors or new_network_fails or possible_bug or oracle_says_error:
                post_context = f"""Я выполнил действие: {action.get('action')} -> {action.get('selector', '')}.
Результат: {result}
Новые ошибки консоли: {', '.join(e.get('text', '')[:60] for e in new_errors[-3:])} 
Новые 5xx ответы: {', '.join(f"{n.get('status')} {n.get('url', '')[:40]}" for n in new_network_fails[-3:])}
{"Оракул: на скриншоте после действия видна ошибка (не успех)." if oracle_says_error else ""}

Это баг приложения или нормальное поведение? Если баг — ответь JSON с action=check_defect и possible_bug."""
                update_llm_overlay(page, prompt=f"#{step} Есть ошибки: анализ…", loading=True)
                post_answer = consult_agent_with_screenshot(post_context, "Проанализируй: это баг или нет?", screenshot_b64=post_screenshot_b64)
                update_llm_overlay(page, prompt=f"#{step} Анализ ошибок", response=post_answer or "", loading=False)

                if post_answer:
                    post_action = parse_llm_action(post_answer)
                    if post_action and post_action.get("action") == "check_defect" and post_action.get("possible_bug"):
                        pbug = post_action["possible_bug"]
                        if ENABLE_SECOND_PASS_BUG and not ask_is_this_really_bug(pbug, post_screenshot_b64):
                            print(f"[Agent] #{step} Второй проход: не баг, пропускаем.")
                            continue
                        _create_defect(page, pbug, current_url, checklist_results, console_log, network_failures, memory)

            update_demo_banner(page, step_text=f"#{step} Готово. Следующий шаг…", progress_pct=100)

            # Отчёт сессии каждые N шагов
            if SESSION_REPORT_EVERY_N > 0 and step % SESSION_REPORT_EVERY_N == 0:
                print(memory.get_session_report_text())

            time.sleep(1)

        browser.close()


def _create_defect(
    page: Page,
    bug_description: str,
    current_url: str,
    checklist_results: List[Dict[str, Any]],
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
    memory: Optional[AgentMemory] = None,
):
    """Создать дефект в Jira с полной фактурой и путём воспроизведения."""
    steps_to_reproduce = memory.get_steps_to_reproduce() if memory else None
    summary = build_defect_summary(bug_description, current_url)
    description = build_defect_description(
        bug_description, current_url,
        checklist_results=checklist_results,
        console_log=console_log,
        network_failures=network_failures,
        steps_to_reproduce=steps_to_reproduce,
    )
    attachment_paths = collect_evidence(page, console_log, network_failures)
    key = create_jira_issue(summary=summary, description=description, attachment_paths=attachment_paths or None)
    if key:
        print(f"[Agent] Дефект создан: {key}")
        if memory:
            memory.record_defect_created(key, summary)
        update_llm_overlay(page, prompt="Дефект создан!", response=f"{key}: {summary[:80]}", loading=False)
    if attachment_paths:
        try:
            d = os.path.dirname(attachment_paths[0])
            if d and os.path.isdir(d) and "kventin_defect_" in d:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
