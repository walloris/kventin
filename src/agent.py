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
import time
from concurrent.futures import ThreadPoolExecutor, Future
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
    CRITICAL_FLOW_STEPS,
    MAX_STEPS,
    SCROLL_PIXELS,
    MAX_ACTIONS_IN_MEMORY,
    MAX_SCROLLS_IN_ROW,
    CONSOLE_LOG_LIMIT,
    NETWORK_LOG_LIMIT,
    POST_ACTION_DELAY,
    PHASE_STEPS_TO_ADVANCE,
    A11Y_CHECK_EVERY_N,
    PERF_CHECK_EVERY_N,
    ENABLE_RESPONSIVE_TEST,
    RESPONSIVE_VIEWPORTS,
    SESSION_PERSIST_CHECK_EVERY_N,
    SELF_HEAL_AFTER_FAILURES,
    ENABLE_SCENARIO_CHAINS,
    SCENARIO_CHAIN_LENGTH,
    ENABLE_IFRAME_TESTING,
)
from src.gigachat_client import (
    consult_agent_with_screenshot,
    consult_agent,
    get_test_plan_from_screenshot,
    ask_is_this_really_bug,
    init_gigachat_connection,
    validate_llm_action,
)
from src.form_strategies import detect_field_type, get_test_value, get_form_fill_strategy
from src.accessibility import check_accessibility, format_a11y_issues
from src.visual_diff import compute_screenshot_diff
from src.performance import check_performance, format_performance_issues

import hashlib
import logging

LOG = logging.getLogger("Agent")
if not LOG.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[Agent] %(levelname)s %(message)s"))
    LOG.addHandler(h)

# Фоновый пул для параллельных задач (GigaChat, Jira, a11y, perf)
# Playwright НЕ thread-safe → только main thread. Всё остальное — в пул.
_bg_pool: Optional[ThreadPoolExecutor] = None


def _get_bg_pool() -> ThreadPoolExecutor:
    """Ленивая инициализация фонового пула."""
    global _bg_pool
    if _bg_pool is None:
        _bg_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="agent-bg")
    return _bg_pool


def _bg_submit(fn, *args, **kwargs) -> Future:
    """Отправить задачу в фоновый пул."""
    return _get_bg_pool().submit(fn, *args, **kwargs)


def _bg_result(future: Optional[Future], timeout: float = 15.0, default=None):
    """Получить результат фоновой задачи (с таймаутом и fallback)."""
    if future is None:
        return default
    try:
        return future.result(timeout=timeout)
    except Exception as e:
        LOG.debug("Background task error: %s", e)
        return default

from src.jira_client import create_jira_issue, reset_session_defects
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

    def __init__(self, max_actions: int = None):
        self.actions: List[Dict[str, Any]] = []
        self.max_actions = max_actions or MAX_ACTIONS_IN_MEMORY
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
        self.max_scrolls_in_row = MAX_SCROLLS_IN_ROW
        self.last_actions_sequence: List[str] = []
        # Хеш последнего скриншота (для дедупликации)
        self.last_screenshot_hash: str = ""
        # Сколько дефектов создано на текущем шаге (защита от дублей 5xx + оракул)
        self.defects_on_current_step: int = 0
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
        # Фаза тестирования (как у реального тестировщика): orient → smoke → critical_path → exploratory
        self.tester_phase: str = "orient"
        self.steps_in_phase: int = 0
        # Корреляция: длина console_log до действия (для детекции новых ошибок именно от действия)
        self.console_len_before_action: int = 0
        self.network_len_before_action: int = 0
        # Tracking тест-плана: какие пункты закрыты
        self.test_plan_completed: List[bool] = []
        # Consecutive failures (для self-healing)
        self.consecutive_failures: int = 0
        # Стратегия заполнения форм
        self.form_strategy_iteration: int = 0
        # Accessibility и performance дефекты (чтобы не дублировать)
        self.reported_a11y_rules: set = set()
        self.reported_perf_rules: set = set()
        # Responsive: уже протестированные viewports
        self.responsive_done: set = set()
        # Скриншот до действия (для visual diff)
        self.screenshot_before_action: Optional[str] = None
        # Pipeline: фоновый анализ предыдущего шага
        self._pending_analysis: Optional[Dict[str, Any]] = None
        # Pipeline: очередь сценариев от GigaChat
        self._scenario_queue: List[Dict[str, Any]] = []

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

    def advance_tester_phase(self, force: bool = False) -> str:
        """
        Переход к следующей фазе тестирования.
        orient → smoke → critical_path → exploratory.
        Переход если force=True или steps_in_phase >= PHASE_STEPS_TO_ADVANCE.
        """
        if not force and self.steps_in_phase < PHASE_STEPS_TO_ADVANCE:
            return self.tester_phase
        next_phase = {
            "orient": "smoke",
            "smoke": "critical_path",
            "critical_path": "exploratory",
            "exploratory": "exploratory",
        }
        old = self.tester_phase
        self.tester_phase = next_phase.get(self.tester_phase, "exploratory")
        self.steps_in_phase = 0
        if old != self.tester_phase:
            LOG.info("Фаза: %s → %s", old, self.tester_phase)
        return self.tester_phase

    def tick_phase_step(self) -> None:
        self.steps_in_phase += 1

    def get_phase_instruction(self) -> str:
        """Краткая инструкция для GigaChat по текущей фазе."""
        from config import DEMO_MODE
        if DEMO_MODE:
            d = {
                "orient": "БЫСТРЫЙ ОСМОТР. Кликни на главную кнопку или первый элемент меню. Не скролль — кликай!",
                "smoke": "SMOKE: кликай по всем видимым кнопкам и ссылкам. Заполняй формы если есть. Действуй быстро!",
                "critical_path": "ОСНОВНОЙ СЦЕНАРИЙ: заполни форму, нажми все кнопки, пройди навигацию. Каждый шаг — новый элемент!",
                "exploratory": "ИССЛЕДОВАНИЕ: открой все дропдауны, табы, меню. Введи данные в каждое поле. Проверь всё что видишь!",
            }
        else:
            d = {
                "orient": "Фаза: ОРИЕНТАЦИЯ. Определи тип страницы (лендинг, каталог, форма, ЛК). Выбери ОДНО действие для понимания контекста (например осмотр ключевых элементов или лёгкий клик по главному CTA).",
                "smoke": "Фаза: SMOKE. Проверь, что страница живая: ключевые кнопки/ссылки есть и кликабельны. Выбери один важный элемент и проверь его (клик или hover).",
                "critical_path": "Фаза: ОСНОВНОЙ СЦЕНАРИЙ. Тестируй главный пользовательский сценарий: основная кнопка, форма, навигация. Одно целенаправленное действие с ясной целью проверки.",
                "exploratory": "Фаза: ИССЛЕДОВАТЕЛЬСКОЕ ТЕСТИРОВАНИЕ. Проверь меню, футер, формы, краевые случаи. Не повторяй уже сделанное. Цель каждого действия — осмысленная проверка, не случайный клик.",
            }
        return d.get(self.tester_phase, d["exploratory"])

    def get_session_report_text(self) -> str:
        """Краткий отчёт сессии: шаги, покрытие, дефекты."""
        if not self.session_start:
            self.session_start = datetime.now()
        duration = (datetime.now() - self.session_start).total_seconds() if self.session_start else 0
        lines = [
            "=== Отчёт сессии AI-тестировщика Kventin ===",
            f"Шагов выполнено: {len(self.actions)}",
            f"Фаза: {self.tester_phase}",
            f"Время: {duration:.0f} с",
            f"Зоны покрытия: {', '.join(self.coverage_zones) if self.coverage_zones else '—'}",
            f"Кликнуто: {len(self.done_click)}, наведено: {len(self.done_hover)}, ввод: {len(self.done_type)}",
        ]
        if self.test_plan:
            lines.append("Тест-план: " + "; ".join(self.test_plan[:5]))
        if self.defects_created:
            lines.append(f"Создано дефектов: {len(self.defects_created)}")
            for d in self.defects_created[-10:]:
                lines.append(f"  - {d.get('key', '')}: {d.get('summary', '')[:60]}")
        else:
            lines.append("Дефектов не обнаружено.")
        lines.append("=== Конец отчёта ===")
        return "\n".join(lines)

    def set_test_plan_tracking(self):
        """Инициализировать отслеживание тест-плана."""
        self.test_plan_completed = [False] * len(self.test_plan)

    def mark_test_plan_step(self, step_index: int):
        """Отметить пункт тест-плана как выполненный."""
        if 0 <= step_index < len(self.test_plan_completed):
            self.test_plan_completed[step_index] = True

    def get_test_plan_progress(self) -> str:
        """Прогресс выполнения тест-плана."""
        if not self.test_plan:
            return ""
        done = sum(self.test_plan_completed)
        total = len(self.test_plan)
        lines = [f"Тест-план: {done}/{total} выполнено"]
        for i, (step, completed) in enumerate(zip(self.test_plan, self.test_plan_completed)):
            mark = "[x]" if completed else "[ ]"
            lines.append(f"  {mark} {i+1}. {step[:60]}")
        return "\n".join(lines)

    def record_action_success(self):
        """Сбросить счётчик последовательных неудач."""
        self.consecutive_failures = 0

    def record_action_failure(self):
        """Увеличить счётчик последовательных неудач."""
        self.consecutive_failures += 1

    def needs_self_healing(self) -> bool:
        """Нужна ли мета-рефлексия из-за серии неудач?"""
        return self.consecutive_failures >= SELF_HEAL_AFTER_FAILURES

    def snapshot_logs_before_action(self, console_log: list, network_failures: list):
        """Запомнить длину логов до действия для корреляции."""
        self.console_len_before_action = len(console_log)
        self.network_len_before_action = len(network_failures)

    def get_new_errors_after_action(self, console_log: list, network_failures: list) -> Dict[str, Any]:
        """Получить ошибки, появившиеся именно после последнего действия."""
        new_console = [c for c in console_log[self.console_len_before_action:] if c.get("type") == "error"]
        new_network = [n for n in network_failures[self.network_len_before_action:] if n.get("status") and n.get("status") >= 400]
        return {"console_errors": new_console, "network_errors": new_network}

    def is_screenshot_changed(self, screenshot_b64: str) -> bool:
        """Проверить, изменился ли скриншот по сравнению с предыдущим (по хешу). Обновляет хеш."""
        if not screenshot_b64:
            return True
        h = hashlib.md5(screenshot_b64[:10000].encode()).hexdigest()
        changed = h != self.last_screenshot_hash
        self.last_screenshot_hash = h
        return changed


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
        _hide_agent_ui(page)
        raw = page.screenshot(type="png")
        return base64.b64encode(raw).decode("ascii")
    except Exception as e:
        print(f"[Agent] Ошибка скриншота: {e}")
        return None
    finally:
        _show_agent_ui(page)


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
        form_strat = action.get("_form_strategy", "happy")
        return _do_type(page, selector, value, form_strategy=form_strat)
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
    """
    Попытаться найти элемент по разным стратегиям:
    1) CSS/XPath (если selector похож)
    2) data-testid
    3) aria-label
    4) placeholder
    5) Текст кнопки/ссылки
    6) Текст любого элемента
    7) getByText / getByRole
    """
    if not selector:
        return None
    strategies = []
    # CSS/XPath
    if selector.startswith((".", "#", "[", "//", "button", "a", "input", "div", "span")):
        strategies.append(("css/xpath", lambda: page.locator(selector).first))
    safe_text = selector.replace('"', '\\"')[:80]
    # data-testid (приоритет: самый стабильный селектор)
    strategies.append(("data-testid", lambda: page.locator(f'[data-testid="{safe_text}"]').first))
    # aria-label
    strategies.append(("aria-label", lambda: page.locator(f'[aria-label="{safe_text}"]').first))
    # placeholder (для полей ввода)
    strategies.append(("placeholder", lambda: page.locator(f'[placeholder="{safe_text}"]').first))
    # По тексту (основные стратегии)
    strategies.extend([
        ("button:text", lambda: page.locator(f'button:has-text("{safe_text}")').first),
        ("a:text", lambda: page.locator(f'a:has-text("{safe_text}")').first),
        ("role=button", lambda: page.locator(f'[role="button"]:has-text("{safe_text}")').first),
        ("role=link", lambda: page.locator(f'[role="link"]:has-text("{safe_text}")').first),
        ("role=tab", lambda: page.locator(f'[role="tab"]:has-text("{safe_text}")').first),
        ("role=menuitem", lambda: page.locator(f'[role="menuitem"]:has-text("{safe_text}")').first),
        ("input:text", lambda: page.locator(f'input:has-text("{safe_text}")').first),
        ("any:text", lambda: page.locator(f'text="{safe_text}"').first),
        ("getByText", lambda: page.get_by_text(safe_text, exact=False).first),
        ("getByRole:button", lambda: page.get_by_role("button", name=safe_text).first),
        ("getByRole:link", lambda: page.get_by_role("link", name=safe_text).first),
        ("getByLabel", lambda: page.get_by_label(safe_text).first),
        ("getByPlaceholder", lambda: page.get_by_placeholder(safe_text).first),
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


def _do_type(page: Page, selector: str, value: str, form_strategy: str = "happy") -> str:
    # Smart value: если value пустой — подобрать по типу поля и стратегии
    if not value and selector:
        field_type = detect_field_type(placeholder=selector, name=selector, aria_label=selector)
        value = get_test_value(field_type, form_strategy)
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
    reset_session_defects()  # сбросить локальный кеш дефектов

    # Инициализация соединения с GigaChat до запуска браузера
    if not init_gigachat_connection():
        print("[Agent] GigaChat недоступен. Проверьте настройки (токен, URL). Браузер не запускается.")
        return
    print("[Agent] GigaChat готов. Запуск браузера…")

    with sync_playwright() as p:
        browser = None
        if BROWSER_USER_DATA_DIR:
            # Профиль на диске — сохраняется выбранный сертификат, куки, логин
            context = p.chromium.launch_persistent_context(
                BROWSER_USER_DATA_DIR,
                headless=HEADLESS,
                slow_mo=BROWSER_SLOW_MO,
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                ignore_https_errors=True,
            )
        else:
            browser = p.chromium.launch(headless=HEADLESS, slow_mo=BROWSER_SLOW_MO)
            context = browser.new_context(
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                ignore_https_errors=True,
            )
        page = context.new_page()

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
            if browser:
                browser.close()
            else:
                context.close()
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
                memory.set_test_plan_tracking()
                print(f"[Agent] Тест-план ({len(test_plan_steps)} шагов): " + "; ".join(test_plan_steps[:3]) + "…")
                update_llm_overlay(page, prompt="Тест-план", response="; ".join(test_plan_steps[:4]), loading=False)

        print(f"[Agent] Старт тестирования: {start_url}")
        if MAX_STEPS > 0:
            print(f"[Agent] Лимит: {MAX_STEPS} шагов.")
        else:
            print(f"[Agent] Бесконечный цикл. Ctrl+C для остановки.")

        try:
            while True:
                memory.iteration += 1
                step = memory.iteration
                memory.defects_on_current_step = 0

                # Лимит шагов
                if MAX_STEPS > 0 and step > MAX_STEPS:
                    print(f"[Agent] Достигнут лимит {MAX_STEPS} шагов. Завершаю.")
                    break

                current_url = page.url

                # ========== Обработка новых вкладок ==========
                _handle_new_tabs(new_tabs_queue, page, start_url, step, console_log, network_failures, memory)

                # Проверка: ушли на другую страницу → вернуться
                if not _same_page(start_url, page.url):
                    print(f"[Agent] #{step} Навигация: {page.url[:60]}. Возврат на {start_url}")
                    update_demo_banner(page, step_text="Возврат на основную страницу…", progress_pct=0)
                    try:
                        page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                        smart_wait_after_goto(page, timeout=10000)
                        _inject_all(page)
                    except Exception as e:
                        LOG.warning("Ошибка возврата: %s", e)
                    continue

                # Проверить результат фонового анализа предыдущего шага
                _flush_pending_analysis(page, memory, console_log, network_failures)

                # Лимит логов
                if len(console_log) > CONSOLE_LOG_LIMIT:
                    del console_log[:len(console_log) - CONSOLE_LOG_LIMIT + 50]
                if len(network_failures) > NETWORK_LOG_LIMIT:
                    del network_failures[:len(network_failures) - NETWORK_LOG_LIMIT + 30]

                # ========== STEP 1: Фаза + чеклист ==========
                if step > 1:
                    memory.advance_tester_phase()

                checklist_results = _step_checklist(page, step, console_log, network_failures, memory)

                # ========== STEP 2: Получить действие от GigaChat ==========
                action, has_overlay, screenshot_b64 = _step_get_action(
                    page, step, memory, console_log, network_failures, checklist_results, context,
                )
                if action is None:
                    from config import DEMO_MODE as _dm
                    time.sleep(3 if _dm else 10)
                    continue

                act_type = (action.get("action") or "").lower()
                sel = (action.get("selector") or "").strip()
                val = (action.get("value") or "").strip()
                possible_bug = action.get("possible_bug")
                expected_outcome = action.get("expected_outcome", "")

                # ========== STEP 3: Выполнить действие ==========
                if act_type == "check_defect" and possible_bug:
                    _step_handle_defect(page, action, possible_bug, current_url, checklist_results, console_log, network_failures, memory)
                    continue

                # Self-healing: серия неудач → мета-рефлексия
                if memory.needs_self_healing():
                    _self_heal(page, memory, console_log, network_failures)
                    continue

                # Запомнить скриншот до действия (для visual diff)
                memory.screenshot_before_action = screenshot_b64

                # Запомнить длину логов до действия (для корреляции)
                memory.snapshot_logs_before_action(console_log, network_failures)

                result = _step_execute(page, action, step, memory, context)

                # Корреляция: отслеживаем ошибки именно от этого действия
                action_errors = memory.get_new_errors_after_action(console_log, network_failures)
                if action_errors["console_errors"]:
                    err_texts = "; ".join(e.get("text", "")[:60] for e in action_errors["console_errors"][:3])
                    LOG.info("#{step} Console ошибки после действия: %s", err_texts)
                if action_errors["network_errors"]:
                    net_texts = "; ".join(f"{n.get('status')} {n.get('url', '')[:40]}" for n in action_errors["network_errors"][:3])
                    LOG.info("#{step} Network ошибки после действия: %s", net_texts)

                # Трекинг success/failure для self-healing
                if "error" in (result or "").lower() or "not_found" in (result or "").lower():
                    memory.record_action_failure()
                else:
                    memory.record_action_success()

                # Отслеживание тест-плана
                _track_test_plan(memory, action)

                # Network verification после submit-подобных кликов
                net_issue = _check_network_after_action(page, memory, action, network_failures)
                if net_issue:
                    print(f"[Agent] #{step} Network issue: {net_issue[:80]}")

                # ========== STEP 4: Пост-анализ ==========
                _step_post_analysis(
                    page, step, action, result, act_type, sel, val, expected_outcome, possible_bug,
                    has_overlay, current_url, checklist_results, console_log, network_failures, memory,
                )

                # ========== STEP 5: Периодические проверки (тяжёлые — в фон) ==========
                # a11y и perf НЕ используют Playwright → можно в фоне
                # Но check_accessibility и check_performance используют page.evaluate!
                # Поэтому собираем данные в main thread, анализ — в фоне.

                # iframe, session persistence, responsive — нужен Playwright, оставляем sync но реже
                if ENABLE_IFRAME_TESTING and step % 10 == 0:
                    _run_iframe_check(page, memory, current_url, console_log, network_failures)

                if SESSION_PERSIST_CHECK_EVERY_N > 0 and step % SESSION_PERSIST_CHECK_EVERY_N == 0:
                    _run_session_persistence_check(page, memory, current_url, console_log, network_failures)

                if ENABLE_RESPONSIVE_TEST and memory.tester_phase == "critical_path" and not memory.responsive_done:
                    _run_responsive_check(page, memory, current_url, console_log, network_failures)

                # A11y/Perf — собираем данные из page в main thread, обрабатываем в фоне
                if A11Y_CHECK_EVERY_N > 0 and step % A11Y_CHECK_EVERY_N == 0:
                    _bg_submit(_run_a11y_check, page, memory, current_url, console_log, network_failures)

                if PERF_CHECK_EVERY_N > 0 and step % PERF_CHECK_EVERY_N == 0:
                    _bg_submit(_run_perf_check, page, memory, current_url, console_log, network_failures)

                update_demo_banner(page, step_text=f"#{step} Готово. Следующий шаг…", progress_pct=100)

                if SESSION_REPORT_EVERY_N > 0 and step % SESSION_REPORT_EVERY_N == 0:
                    report = memory.get_session_report_text()
                    plan_progress = memory.get_test_plan_progress()
                    if plan_progress:
                        report += "\n" + plan_progress
                    print(report)

                from config import DEMO_MODE as _dm2
                time.sleep(0.3 if _dm2 else 1)

        except KeyboardInterrupt:
            print("\n[Agent] Остановлен по Ctrl+C.")
        finally:
            # Дождаться фоновых задач
            _flush_pending_analysis(page, memory, console_log, network_failures)
            if _bg_pool:
                _bg_pool.shutdown(wait=False)
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
            print(report)
            if browser:
                browser.close()
            else:
                context.close()


# ===== Step-функции (декомпозиция run_agent) =====

def _step_checklist(page, step, console_log, network_failures, memory):
    """STEP 1: Чеклист периодически (в демо — реже)."""
    from config import DEMO_MODE as _dm
    checklist_every = 15 if _dm else 5
    checklist_results = []
    if step % checklist_every == 1:
        smart_wait_after_goto(page, timeout=5000)
        def on_step(step_id, ok, detail, step_index, total):
            st = "+" if ok else "X"
            pct = round(100 * step_index / total) if total else 0
            update_demo_banner(page, step_text=f"Чеклист {step_index}/{total}: {step_id}", progress_pct=pct)
            update_llm_overlay(page, prompt=f"Чеклист: {step_id}", response=f"{st} {detail[:120]}", loading=False)
        checklist_results = run_checklist(page, console_log, network_failures, step_delay_ms=CHECKLIST_STEP_DELAY_MS, on_step=on_step)
    return checklist_results


def _step_get_action(page, step, memory, console_log, network_failures, checklist_results, context):
    """STEP 2: Скриншот + контекст → GigaChat → получить действие."""
    from config import DEMO_MODE as _dm
    update_demo_banner(page, step_text=f"#{step} Анализ…", progress_pct=25)

    # В демо-режиме: компактный DOM, короткая история → меньше токенов → быстрее ответ
    dom_max = 2000 if _dm else 4000
    history_n = 8 if _dm else 15

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
    dom_summary = get_dom_summary(page, max_length=dom_max)
    history_text = memory.get_history_text(last_n=history_n)

    if has_overlay:
        question = f"""Вот скриншот. На странице есть АКТИВНЫЙ ОВЕРЛЕЙ (модалка/дропдаун/тултип/попап).
{overlay_context}
DOM: {dom_summary[:3000]}
{history_text}
Сейчас на экране оверлей! 1) Тестируй содержимое, 2) Если уже — закрой (close_modal), 3) Баг — check_defect.
Выбери ОДНО действие."""
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
        question = f"""Вот скриншот и контекст страницы.
DOM (кнопки, ссылки, формы): {dom_summary[:3000]}
{history_text}
{plan_hint}{form_hint}
Выбери ОДНО действие. Укажи test_goal и expected_outcome. Не повторяй уже сделанное.
Оцени верстку. Если реальный баг — action=check_defect."""

    phase_instruction = memory.get_phase_instruction()
    update_demo_banner(page, step_text=f"#{step} Консультация с GigaChat…", progress_pct=60)
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
        action = memory._scenario_queue.pop(0)
        print(f"[Agent] #{step} Scenario chain (из очереди): {action.get('action')} -> {action.get('selector', '')[:40]}")
        return action, has_overlay, screenshot_b64

    raw_answer = consult_agent_with_screenshot(
        context_str, question, screenshot_b64=send_screenshot,
        phase_instruction=phase_instruction, tester_phase=memory.tester_phase,
        has_overlay=has_overlay,
    )
    update_llm_overlay(page, prompt=f"#{step} Ответ", response=raw_answer or "Нет ответа", loading=False, error="Нет ответа" if not raw_answer else None)

    if not raw_answer:
        print(f"[Agent] #{step} GigaChat недоступен после retry, пауза 10с")
        return None, has_overlay, screenshot_b64

    action = parse_llm_action(raw_answer)
    if not action:
        print(f"[Agent] #{step} Не удалось распарсить JSON: {raw_answer[:120]}")
        action = {"action": "scroll", "selector": "down", "reason": "GigaChat не дал JSON"}
    # Валидация и нормализация
    action = validate_llm_action(action)
    # layout_issue → possible_bug
    if action.get("layout_issue") and not action.get("possible_bug"):
        action["possible_bug"] = action.get("layout_issue")

    act_type = (action.get("action") or "").lower()
    sel = (action.get("selector") or "").strip()
    val = (action.get("value") or "").strip()

    # Дедупликация действий
    if act_type != "check_defect" and memory.is_already_done(act_type, sel, val):
        print(f"[Agent] #{step} Повтор: {act_type} -> {sel[:40]}")
        if has_overlay:
            action = {"action": "close_modal", "selector": "", "reason": "Повтор — закрываю оверлей"}
        elif not memory.should_avoid_scroll():
            action = {"action": "scroll", "selector": "down", "reason": "Повтор — прокрутка"}
        else:
            # Попытка перейти в следующую фазу при зацикливании
            memory.advance_tester_phase(force=True)
            action = {"action": "scroll", "selector": "up", "reason": "Повтор — смена фазы, прокрутка вверх"}

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
    # Передаём стратегию заполнения формы
    if act_type == "type":
        strategy = get_form_fill_strategy(memory.tester_phase, memory.form_strategy_iteration)
        action["_form_strategy"] = strategy
        memory.form_strategy_iteration += 1
    update_demo_banner(page, step_text=f"#{step} {act_type.upper()}: {sel[:30]}…", progress_pct=80)

    result = ""
    for attempt in range(1 + max(0, ACTION_RETRY_COUNT)):
        result = execute_action(page, action, memory)
        if "error" not in result.lower() and "not_found" not in result.lower() and "no_selector" not in result.lower():
            break
        if attempt < max(0, ACTION_RETRY_COUNT):
            from config import DEMO_MODE as _dm_r
            print(f"[Agent] #{step} Повтор {attempt + 1}…")
            time.sleep(0.3 if _dm_r else 1.0)

    memory.add_action(action, result=result)
    memory.tick_phase_step()
    print(f"[Agent] #{step} Результат: {result}")

    # Карта покрытия
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

    time.sleep(POST_ACTION_DELAY)
    smart_wait_after_goto(page, timeout=3000)

    return result


def _collect_post_data(page, has_overlay, memory):
    """
    Собрать данные после действия ИЗ MAIN THREAD (Playwright).
    Возвращает dict с данными, которые потом можно анализировать в фоне.
    """
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


def _analyze_in_background(
    post_data, step, action, result, act_type, sel, val, expected_outcome, possible_bug,
    current_url, checklist_results, console_log_snapshot, network_snapshot, memory,
    before_screenshot,
):
    """
    Фоновый анализ (без Playwright!): visual diff, оракул, определение багов.
    Возвращает dict с результатами для main thread.
    """
    findings = {"oracle_error": False, "bug_to_report": None, "five_xx_bug": None}
    post_screenshot_b64 = post_data.get("post_screenshot_b64")

    # Visual diff
    visual_diff_info = compute_screenshot_diff(before_screenshot, post_screenshot_b64)
    if visual_diff_info.get("changed") and visual_diff_info.get("change_percent", 0) > 0:
        LOG.info("#{step} Visual diff: %s (%.1f%%)", visual_diff_info.get("diff_zone", "?"), visual_diff_info.get("change_percent", 0))

    new_errors = [c for c in console_log_snapshot[-10:] if c.get("type") == "error"]
    new_network_fails = [n for n in network_snapshot[-5:] if n.get("status") and n.get("status") >= 500]

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
        )

    # Оракул (GigaChat — thread-safe)
    if ENABLE_ORACLE_AFTER_ACTION and act_type in ("type", "click") and post_screenshot_b64 and not new_network_fails:
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

    # Пост-анализ ошибок
    if not new_network_fails and (new_errors or possible_bug or findings["oracle_error"]):
        post_context = f"Действие: {action.get('action')} -> {action.get('selector', '')}. Результат: {result}. Ошибки: {', '.join(e.get('text', '')[:60] for e in new_errors[-3:])}"
        post_answer = consult_agent_with_screenshot(post_context, "Это баг или нет?", screenshot_b64=post_screenshot_b64)
        if post_answer:
            post_action = parse_llm_action(post_answer)
            if post_action and post_action.get("action") == "check_defect" and post_action.get("possible_bug"):
                findings["bug_to_report"] = post_action["possible_bug"]

    return findings


def _step_post_analysis(
    page, step, action, result, act_type, sel, val, expected_outcome, possible_bug,
    has_overlay, current_url, checklist_results, console_log, network_failures, memory,
):
    """STEP 4: Пост-анализ — быстрый сбор данных + фоновый анализ."""
    update_demo_banner(page, step_text=f"#{step} Анализ результата…", progress_pct=90)

    # Быстрый сбор из Playwright (main thread)
    post_data = _collect_post_data(page, has_overlay, memory)

    # Новый оверлей — обработать сразу
    if post_data["new_overlay"]:
        print(f"[Agent] #{step} Появился оверлей: {', '.join(post_data['overlay_types'])}")
        memory.add_action(
            {"action": "overlay_detected", "selector": ", ".join(post_data["overlay_types"])},
            result="new_overlay_appeared"
        )
        return

    # Снимки логов (thread-safe copies)
    console_snapshot = list(console_log[-20:])
    network_snapshot = list(network_failures[-10:])
    before_screenshot = memory.screenshot_before_action

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
    update_demo_banner(page, step_text=f"#{step} Анализ результата…", progress_pct=90)

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

    update_demo_banner(page, step_text=f"#{step} Анализ результата…", progress_pct=95)
    post_screenshot_b64 = take_screenshot_b64(page)

    visual_diff_info = compute_screenshot_diff(memory.screenshot_before_action, post_screenshot_b64)
    if visual_diff_info.get("changed"):
        diff_pct = visual_diff_info.get("change_percent", 0)
        diff_zone = visual_diff_info.get("diff_zone", "?")
        if diff_pct > 0:
            LOG.info("#{step} Visual diff: %s (%.1f%%)", diff_zone, diff_pct)

    new_errors = [c for c in console_log[-10:] if c.get("type") == "error"]
    new_network_fails = [n for n in network_failures[-5:] if n.get("status") and n.get("status") >= 500]

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


def _is_semantic_duplicate(bug_description: str, memory: AgentMemory) -> bool:
    """
    Уровень 3: семантическая проверка через GigaChat.
    Спросить: «это тот же баг, что уже заведённые?»
    """
    if not memory or not memory.defects_created:
        return False
    # Сравниваем только если есть 1+ дефектов за сессию
    existing = "\n".join(
        f"- {d['key']}: {d['summary'][:80]}"
        for d in memory.defects_created[-10:]
    )
    try:
        answer = consult_agent(
            f"Уже заведённые дефекты:\n{existing}\n\n"
            f"Новый дефект: {bug_description[:300]}\n\n"
            f"Это ДУБЛЬ одного из уже заведённых? Ответь ОДНИМ словом: ДА или НЕТ."
        )
        if answer and "да" in answer.strip().lower()[:10]:
            LOG.info("Семантический дубль (GigaChat): %s", bug_description[:60])
            return True
    except Exception as e:
        LOG.debug("semantic dedup error: %s", e)
    return False


def _create_defect(
    page: Page,
    bug_description: str,
    current_url: str,
    checklist_results: List[Dict[str, Any]],
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
    memory: Optional[AgentMemory] = None,
):
    """Создать дефект: быстрые проверки в main thread, Jira — в фоне."""
    from src.jira_client import is_local_duplicate, register_local_defect

    summary = build_defect_summary(bug_description, current_url)

    # Уровень 1: локальная дедупликация (мгновенно)
    if is_local_duplicate(summary, bug_description):
        LOG.info("Пропуск дефекта (локальный дубль): %s", summary[:60])
        return

    # Собрать evidence из Playwright (main thread — быстро)
    attachment_paths = collect_evidence(page, console_log, network_failures)
    steps_to_reproduce = memory.get_steps_to_reproduce() if memory else None
    description = build_defect_description(
        bug_description, current_url,
        checklist_results=checklist_results,
        console_log=console_log,
        network_failures=network_failures,
        steps_to_reproduce=steps_to_reproduce,
    )

    # Отправка в Jira — В ФОНЕ (семантическая проверка + создание тикета)
    _bg_submit(
        _create_defect_bg,
        summary, description, bug_description, attachment_paths, memory,
    )


def _create_defect_bg(
    summary: str,
    description: str,
    bug_description: str,
    attachment_paths: Optional[list],
    memory: Optional[AgentMemory],
):
    """Фоновое создание дефекта (Jira API + GigaChat дедупликация)."""
    from src.jira_client import register_local_defect

    try:
        # Уровень 3: семантическая проверка через GigaChat
        if _is_semantic_duplicate(bug_description, memory):
            LOG.info("Пропуск (семантический дубль): %s", summary[:60])
            register_local_defect(summary)
            return

        # Уровень 2: дедупликация через Jira внутри create_jira_issue
        key = create_jira_issue(summary=summary, description=description, attachment_paths=attachment_paths or None)
        if key:
            print(f"[Agent] Дефект создан: {key}")
            if memory:
                memory.record_defect_created(key, summary)
    except Exception as e:
        LOG.error("Ошибка фонового создания дефекта: %s", e)
    finally:
        if attachment_paths:
            try:
                d = os.path.dirname(attachment_paths[0])
                if d and os.path.isdir(d) and "kventin_defect_" in d:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


# ===== Продвинутые проверки (a11y, perf, responsive, session, iframe, self-healing, scenario chains) =====

def _run_a11y_check(page: Page, memory: AgentMemory, current_url: str, console_log, network_failures):
    """Запустить accessibility проверки и завести дефекты на новые проблемы."""
    issues = check_accessibility(page)
    new_issues = [i for i in issues if i.get("rule") not in memory.reported_a11y_rules]
    if new_issues:
        text = format_a11y_issues(new_issues)
        print(f"[Agent] A11y: {len(new_issues)} новых проблем")
        for i in new_issues:
            memory.reported_a11y_rules.add(i.get("rule"))
        if any(i.get("severity") == "error" for i in new_issues):
            _create_defect(page, f"Accessibility (a11y): {text}", current_url, [], console_log, network_failures, memory)


def _run_perf_check(page: Page, memory: AgentMemory, current_url: str, console_log, network_failures):
    """Запустить performance проверки и завести дефекты."""
    issues = check_performance(page)
    new_issues = [i for i in issues if i.get("rule") not in memory.reported_perf_rules]
    if new_issues:
        text = format_performance_issues(new_issues)
        print(f"[Agent] Perf: {len(new_issues)} проблем")
        for i in new_issues:
            memory.reported_perf_rules.add(i.get("rule"))
        if any(i.get("severity") == "warning" for i in new_issues):
            _create_defect(page, f"Performance: {text}", current_url, [], console_log, network_failures, memory)


def _run_responsive_check(page: Page, memory: AgentMemory, current_url: str, console_log, network_failures):
    """Переключить viewport на мобильный/планшетный, сделать скриншот и проверить верстку через GigaChat."""
    if not ENABLE_RESPONSIVE_TEST:
        return
    for vp in RESPONSIVE_VIEWPORTS:
        name = vp["name"]
        if name in memory.responsive_done:
            continue
        memory.responsive_done.add(name)
        print(f"[Agent] Responsive: проверка viewport {name} ({vp['width']}x{vp['height']})")
        try:
            page.set_viewport_size({"width": vp["width"], "height": vp["height"]})
            time.sleep(2)
            screenshot_b64 = take_screenshot_b64(page)
            if screenshot_b64:
                answer = consult_agent_with_screenshot(
                    f"Viewport: {name} ({vp['width']}x{vp['height']}). URL: {current_url}",
                    "На скриншоте страница в мобильном/планшетном viewport. Есть ли проблемы верстки: наложения, обрезки, горизонтальная прокрутка, элементы вне экрана? Если есть — ответь JSON с action=check_defect и possible_bug. Если нет — ответь JSON с action=explore.",
                    screenshot_b64=screenshot_b64,
                )
                if answer:
                    action = parse_llm_action(answer)
                    if action and action.get("action") == "check_defect" and action.get("possible_bug"):
                        bug = f"[Responsive {name}] {action['possible_bug']}"
                        _create_defect(page, bug, current_url, [], console_log, network_failures, memory)
        except Exception as e:
            LOG.debug("responsive check %s: %s", name, e)
        finally:
            # Вернуть оригинальный viewport
            page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
            time.sleep(1)


def _run_session_persistence_check(page: Page, memory: AgentMemory, current_url: str, console_log, network_failures):
    """Перезагрузить страницу и проверить: состояние сохранилось?"""
    if not SESSION_PERSIST_CHECK_EVERY_N:
        return
    print("[Agent] Session persistence: перезагрузка страницы…")
    try:
        before_b64 = take_screenshot_b64(page)
        page.reload(wait_until="domcontentloaded", timeout=15000)
        smart_wait_after_goto(page, timeout=5000)
        after_b64 = take_screenshot_b64(page)
        diff = compute_screenshot_diff(before_b64, after_b64)
        if diff.get("change_percent", 0) > 40:
            answer = consult_agent_with_screenshot(
                f"URL: {current_url}. После перезагрузки (F5) экран изменился на {diff.get('change_percent')}%. {diff.get('detail', '')}",
                "Страница сильно изменилась после перезагрузки. Это ожидаемо или потеря состояния (сброс формы, разлогин, потеря данных)? Если баг — JSON с check_defect.",
                screenshot_b64=after_b64,
            )
            if answer:
                action = parse_llm_action(answer)
                if action and action.get("action") == "check_defect" and action.get("possible_bug"):
                    _create_defect(page, f"[Session] {action['possible_bug']}", current_url, [], console_log, network_failures, memory)
        _inject_all(page)
    except Exception as e:
        LOG.debug("session persistence: %s", e)


def _run_iframe_check(page: Page, memory: AgentMemory, current_url: str, console_log, network_failures):
    """Проверить содержимое iframe на странице."""
    if not ENABLE_IFRAME_TESTING:
        return
    try:
        iframes = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('iframe'))
                .filter(f => f.src && !f.src.startsWith('about:') && f.width > 50 && f.height > 50)
                .map(f => ({ src: f.src.slice(0, 200), name: f.name || '', id: f.id || '' }))
                .slice(0, 3);
        }""")
        for iframe_info in (iframes or []):
            src = iframe_info.get("src", "")
            name = iframe_info.get("name", "") or iframe_info.get("id", "")
            print(f"[Agent] iframe: проверяю {name or src[:40]}")
            try:
                frame = page.frame(url=src) if src else (page.frame(name=name) if name else None)
                if not frame:
                    continue
                # Проверяем что внутри iframe загружено
                body_text = frame.evaluate("() => (document.body && document.body.innerText || '').trim().slice(0, 200)")
                if not body_text or len(body_text) < 10:
                    _create_defect(
                        page,
                        f"iframe пустой или не загрузился: src={src[:80]}, name={name[:30]}",
                        current_url, [], console_log, network_failures, memory,
                    )
            except Exception as e:
                LOG.debug("iframe check %s: %s", src[:40], e)
    except Exception as e:
        LOG.debug("iframe scan: %s", e)


def _self_heal(page: Page, memory: AgentMemory, console_log, network_failures):
    """
    Self-healing: после серии неудач — мета-рефлексия.
    Спрашиваем GigaChat «что пошло не так и что делать?».
    """
    print(f"[Agent] Self-healing: {memory.consecutive_failures} неудач подряд")
    screenshot_b64 = take_screenshot_b64(page)
    recent_actions = "\n".join(
        f"  #{a['step']} {a['action']} -> {a['selector'][:40]} => {a['result'][:40]}"
        for a in memory.actions[-6:]
    )
    answer = consult_agent_with_screenshot(
        f"Последние действия (все неудачные):\n{recent_actions}\n\nЧто идёт не так? Какую стратегию выбрать?",
        "Агент зациклился: несколько действий подряд не удались. Предложи одно действие, которое точно сработает (прокрутка, клик по видимому элементу, Escape). JSON.",
        screenshot_b64=screenshot_b64,
    )
    memory.consecutive_failures = 0
    if answer:
        action = parse_llm_action(answer)
        if action:
            action = validate_llm_action(action)
            execute_action(page, action, memory)
            memory.add_action(action, result="self_heal")
    # Принудительная смена фазы
    memory.advance_tester_phase(force=True)


def _request_scenario_chain(page: Page, memory: AgentMemory, context_str: str, screenshot_b64: Optional[str]) -> List[Dict]:
    """
    Попросить GigaChat сгенерировать цепочку из N связанных действий (сценарий).
    Возвращает список action-dicts.
    """
    if not ENABLE_SCENARIO_CHAINS:
        return []
    n = SCENARIO_CHAIN_LENGTH
    answer = consult_agent_with_screenshot(
        context_str,
        f"Сгенерируй цепочку из {n} связанных действий (сценарий). Каждое действие — отдельный JSON-объект. "
        f"Ответь МАССИВОМ JSON: [{n} объектов с action/selector/value/reason/test_goal/expected_outcome]. "
        f"Пример: [{{'action':'click','selector':'Войти','value':'','reason':'открыть форму','test_goal':'проверка входа','expected_outcome':'форма логина'}}, ...]",
        screenshot_b64=screenshot_b64,
    )
    if not answer:
        return []
    # Попробуем распарсить массив
    try:
        cleaned = re.sub(r'^```(?:json)?\s*', '', answer.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r'```\s*$', '', cleaned.strip(), flags=re.MULTILINE)
        arr = json.loads(cleaned)
        if isinstance(arr, list):
            return [validate_llm_action(a) for a in arr if isinstance(a, dict) and a.get("action")][:n]
    except Exception:
        pass
    # Fallback: одно действие
    single = parse_llm_action(answer)
    return [validate_llm_action(single)] if single else []


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
