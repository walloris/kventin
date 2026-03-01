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
    SESSION_REPORT_PATH,
    SESSION_REPORT_HTML_PATH,
    SESSION_REPORT_JSONL,
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
    validate_llm_action,
)
from src.form_strategies import detect_field_type, get_test_value, get_form_fill_strategy
from src.accessibility import check_accessibility, format_a11y_issues
from src.visual_diff import (
    compute_screenshot_diff,
    compare_with_baseline,
    save_baseline,
    load_baseline,
)
from src.performance import check_performance, format_performance_issues

import hashlib
import html as html_module
import logging

LOG = logging.getLogger("Agent")
if not LOG.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[Agent] %(levelname)s %(message)s"))
    LOG.addHandler(h)

# Текущая память агента в основном цикле (для self-healing в _find_element)
_current_agent_memory: Optional["AgentMemory"] = None

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
        # Счётчик повторов подряд (для детекции зацикливания)
        self._consecutive_repeats: int = 0
        # Последние 5 действий (для детекции паттернов зацикливания)
        self._recent_action_keys: List[str] = []
        # Кэш важных элементов страницы (для быстрого доступа)
        self._page_elements_cache: Dict[str, List[Dict[str, Any]]] = {}
        # Запомненные важные страницы (URL -> описание)
        self._important_pages: Dict[str, str] = {}
        # Покрытие элементов (какие элементы уже протестированы на текущей странице)
        self._page_coverage: Dict[str, set] = {}  # URL -> set of element keys
        # Чеклисты по страницам: URL -> (checklist_items, current_index, completed)
        self._page_checklists: Dict[str, Dict[str, Any]] = {}  # URL -> {"items": [...], "index": 0, "completed": False}
        # Модули страницы (шапка, навигация, основной контент и т.д.) — тестируем по очереди
        self.page_modules: List[Dict[str, Any]] = []
        self.current_module_index: int = 0
        self.steps_in_current_module: int = 0
        self._modules_page_url: str = ""
        # Структурированный лог шагов (step, url, action, result, source) для отчёта
        self._step_log: List[Dict[str, Any]] = []
        # Граф навигации: список {from_url, to_url, step, selector} для отчёта и лимита глубины
        self._nav_graph: List[Dict[str, Any]] = []
        # Глубина от start_url по каждому URL (для MAX_NAVIGATION_DEPTH)
        self._url_depths: Dict[str, int] = {}
        self._start_url_nav: str = ""
        # Битые ссылки (проверка BROKEN_LINKS_CHECK_EVERY_N): список {url, status, error}
        self._broken_links: List[Dict[str, Any]] = []
        self._checked_link_urls: set = set()
        # WebSocket: неожиданное закрытие/ошибки (ENABLE_WEBSOCKET_MONITOR)
        self._websocket_issues: List[Dict[str, Any]] = []
        # Mixed content: HTTP-ресурсы на HTTPS-странице (ENABLE_MIXED_CONTENT_CHECK)
        self._mixed_content: List[Dict[str, Any]] = []
        # API-интеркепт: XHR/fetch запросы и ответы (ENABLE_API_INTERCEPT)
        self._api_log: List[Dict[str, Any]] = []
        # Visual regression: сравнение с baseline (url -> {regression, change_percent, detail})
        self._visual_regressions: List[Dict[str, Any]] = []
        self._visual_baseline_checked: set = set()  # URL уже проверены на baseline
        # Self-healing: кеш успешных селекторов (original -> {strategy, role?, name?})
        self._selector_heal_cache: Dict[str, Dict[str, Any]] = {}

    def set_start_url_for_nav(self, url: str) -> None:
        """Задать стартовый URL для подсчёта глубины навигации."""
        self._start_url_nav = url or ""
        if self._start_url_nav:
            self._url_depths[self._start_url_nav] = 0

    def record_navigation(self, from_url: str, to_url: str, step: int, selector: str = "") -> None:
        """Записать переход по ссылке (для графа и глубины)."""
        from_url = (from_url or "").strip()
        to_url = (to_url or "").strip()
        if not to_url or from_url == to_url:
            return
        self._nav_graph.append({"from_url": from_url, "to_url": to_url, "step": step, "selector": selector[:80]})
        prev_depth = self._url_depths.get(from_url, 0)
        if to_url not in self._url_depths:
            self._url_depths[to_url] = prev_depth + 1

    def get_navigation_depth(self, url: str) -> int:
        """Глубина перехода от start_url (0 = стартовая страница)."""
        return self._url_depths.get((url or "").strip(), 0)

    def append_step_log(self, entry: Dict[str, Any]) -> None:
        """Добавить запись о шаге в лог (для итогового отчёта)."""
        self._step_log.append(entry)

    def set_page_modules(self, modules: List[Dict[str, Any]], page_url: str) -> None:
        """Задать список модулей для текущей страницы (при смене URL или первой загрузке)."""
        self.page_modules = list(modules) if modules else []
        self.current_module_index = 0
        self.steps_in_current_module = 0
        self._modules_page_url = page_url or ""

    def get_current_module(self) -> Optional[Dict[str, Any]]:
        """Текущий модуль для тестирования (или None если модулей нет)."""
        if not self.page_modules or self.current_module_index >= len(self.page_modules):
            return None
        return self.page_modules[self.current_module_index]

    def advance_module(self) -> bool:
        """Перейти к следующему модулю. Возвращает True если перешли."""
        if not self.page_modules or self.current_module_index >= len(self.page_modules) - 1:
            return False
        self.current_module_index += 1
        self.steps_in_current_module = 0
        return True

    def tick_module_step(self) -> None:
        """Увеличить счётчик шагов в текущем модуле (для перехода после N шагов)."""
        self.steps_in_current_module += 1

    def get_module_context_text(self) -> str:
        """Текст для промпта GigaChat: какие модули есть и какой тестируем сейчас."""
        if not self.page_modules:
            return ""
        lines = ["МОДУЛИ СТРАНИЦЫ (тестируй по очереди):"]
        for i, m in enumerate(self.page_modules):
            name = m.get("name", "Модуль")
            mark = " ← ТЕКУЩИЙ" if i == self.current_module_index else ""
            lines.append(f"  {i + 1}) {name}{mark}")
        cur = self.get_current_module()
        if cur:
            lines.append("")
            lines.append(f"Сейчас тестируй только модуль: «{cur.get('name', '')}». Выбери действие внутри этого модуля (selector ref:N из элементов этого блока).")
        return "\n".join(lines)

    def add_action(self, action: Dict[str, Any], result: str = ""):
        act = (action.get("action") or "").lower()
        sel = _norm_key(action.get("selector", ""))
        val = _norm_key(action.get("value", ""))
        # Записать ключ для детекции паттернов
        self.record_action_key(act, sel)

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
            "⚠️⚠️⚠️ КРИТИЧНО: УЖЕ СДЕЛАНО (НЕ ПОВТОРЯТЬ, выбирай ДРУГОЕ действие!) ⚠️⚠️⚠️",
            "",
        ]
        if self.done_click:
            items = sorted(self.done_click)[-30:]
            lines.append(f"❌ Кликнуто ({len(self.done_click)}): " + ", ".join(f'"{x[:40]}"' for x in items))
        if self.done_hover:
            items = sorted(self.done_hover)[-20:]
            lines.append(f"❌ Наведено (hover) ({len(self.done_hover)}): " + ", ".join(f'"{x[:40]}"' for x in items))
        if self.done_type:
            items = sorted(self.done_type)[-20:]
            lines.append(f"❌ Ввод в поля ({len(self.done_type)}): " + ", ".join(f'"{x[:40]}"' for x in items))
        if self.done_close_modal:
            lines.append(f"❌ Закрыто модалок: {self.done_close_modal}")
        if self.done_select_option:
            items = list(self.done_select_option)[:20]
            lines.append(f"❌ Выбрано опций: " + ", ".join(str(x)[:50] for x in items))
        if self.done_scroll_down or self.done_scroll_up:
            lines.append(f"❌ Прокручено: вниз {self.done_scroll_down}, вверх {self.done_scroll_up}")
        if self.should_avoid_scroll():
            lines.append("⚠️ Внимание: недавно много прокруток — выбери клик/hover/type/close_modal, а не scroll.")
        if self._consecutive_repeats >= 2:
            lines.append(f"🚨 ЗАЦИКЛИВАНИЕ: {self._consecutive_repeats} повтора подряд! СРОЧНО выбери ДРУГОЕ действие, которого НЕТ выше!")
        lines.append("")
        lines.append("✅ Выбери действие, которого ещё НЕТ в списке выше (❌).")
        lines.append("")
        lines.append("Последние выполненные шаги:")
        for a in self.actions[-last_n:]:
            act = a.get('action', '?')
            sel = a.get('selector', '')[:45]
            res = a.get('result', '')[:50]
            lines.append(f"  #{a.get('step', '?')} {act} -> {sel} | {res}")
        return "\n".join(lines)
    
    def record_repeat(self):
        """Записать повтор действия."""
        self._consecutive_repeats += 1
    
    def reset_repeats(self):
        """Сбросить счётчик повторов (успешное новое действие)."""
        self._consecutive_repeats = 0
    
    def is_stuck(self) -> bool:
        """Проверить, застрял ли агент (много повторов подряд)."""
        return self._consecutive_repeats >= 3
    
    def record_action_key(self, action: str, selector: str):
        """Записать ключ действия для детекции паттернов."""
        key = f"{action}:{_norm_key(selector)}"
        self._recent_action_keys.append(key)
        if len(self._recent_action_keys) > 5:
            self._recent_action_keys.pop(0)
        # Детекция паттерна: последние 3 действия одинаковые
        if len(self._recent_action_keys) >= 3:
            last3 = self._recent_action_keys[-3:]
            if len(set(last3)) == 1:
                self._consecutive_repeats += 1
    
    def record_page_element(self, url: str, element_key: str):
        """Записать элемент как протестированный на странице."""
        if url not in self._page_coverage:
            self._page_coverage[url] = set()
        self._page_coverage[url].add(element_key)
    
    def is_element_tested(self, url: str, element_key: str) -> bool:
        """Проверить, был ли элемент уже протестирован на странице."""
        return element_key in self._page_coverage.get(url, set())
    
    def cache_page_elements(self, url: str, elements: List[Dict[str, Any]]):
        """Кэшировать важные элементы страницы."""
        self._page_elements_cache[url] = elements[:50]  # Ограничиваем размер
    
    def get_cached_elements(self, url: str) -> List[Dict[str, Any]]:
        """Получить кэшированные элементы страницы."""
        return self._page_elements_cache.get(url, [])
    
    def remember_important_page(self, url: str, description: str):
        """Запомнить важную страницу (например, форма регистрации, главная)."""
        self._important_pages[url] = description[:200]

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

    def record_defect_created(self, key: str, summary: str, severity: str = "major"):
        self.defects_created.append({"key": key, "summary": summary[:200], "severity": severity})

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
        d = {
            "orient": "Фаза: ОРИЕНТАЦИЯ. Определи тип страницы. Выбери одно действие для понимания контекста (клик по главному CTA или ключевому элементу).",
            "smoke": "Фаза: SMOKE. Проверь ключевые кнопки/ссылки. Выбери один важный элемент и проверь его (клик или hover).",
            "critical_path": "Фаза: ОСНОВНОЙ СЦЕНАРИЙ. Тестируй главный сценарий: кнопка, форма, навигация. Одно целенаправленное действие.",
            "exploratory": "Фаза: ИССЛЕДОВАНИЕ. Проверь меню, футер, формы. Не повторяй уже сделанное. Осмысленная проверка.",
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
            
            print(f"[Agent] 🔴 КЛИК: {selector[:50]} ({reason[:30]})")
            # Показываем курсор и подсказку ДО highlight
            box = loc.bounding_box()
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                move_cursor_to(page, cx, cy)
                show_highlight_label(page, cx, cy, reason[:30] or "КЛИКАЮ!")
                time.sleep(0.5)  # Пауза чтобы увидеть курсор и подсказку
            
            safe_highlight(loc, page, 0.8)  # Увеличиваем время highlight
            highlight_and_click(loc, page, description=reason[:30] or "КЛИКАЮ!")
            print(f"[Agent] ✅ Клик выполнен: {selector[:50]}")
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
            print(f"[Agent] ⌨️ ВВОД: {selector[:50]} = {value[:30]}")
            # Показываем курсор и подсказку ДО highlight
            box = loc.bounding_box()
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                move_cursor_to(page, cx, cy)
                show_highlight_label(page, cx, cy, f"ВВОДЮ: {value[:20]}")
                time.sleep(0.5)  # Пауза чтобы увидеть курсор и подсказку
            
            safe_highlight(loc, page, 0.8)  # Увеличиваем время highlight
            loc.click()
            time.sleep(0.2)  # Пауза после клика
            loc.fill(value)
            time.sleep(0.5)  # Пауза после заполнения
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
        if BROWSER_USER_DATA_DIR:
            # Профиль на диске — поддерживается только Chromium
            context = p.chromium.launch_persistent_context(
                BROWSER_USER_DATA_DIR,
                headless=HEADLESS,
                slow_mo=BROWSER_SLOW_MO,
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                ignore_https_errors=True,
            )
        else:
            browser = engine.launch(headless=HEADLESS, slow_mo=BROWSER_SLOW_MO)
            ctx_opts = {
                "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                "ignore_https_errors": True,
            }
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

        print(f"[Agent] Старт тестирования: {start_url}")
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
                
                # Если ушли на другой домен — возвращаемся на start_url
                if not _same_page(start_url, page.url):
                    print(f"[Agent] #{step} Навигация на {page.url[:60]}. Возврат на {start_url[:60]}")
                    try:
                        page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                        smart_wait_after_goto(page, timeout=5000)
                        _inject_all(page)
                    except Exception as e:
                        LOG.warning("Ошибка возврата: %s", e)
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
            
            if _bg_pool:
                _bg_pool.shutdown(wait=False)
            
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
    coverage = ", ".join(memory.coverage_zones) if memory.coverage_zones else "—"

    steps_rows = []
    for e in step_log:
        sp = e.get("screenshot_path") or ""
        img_cell = ""
        if sp and not os.path.isabs(sp) and "screenshots/" in sp:
            img_cell = f'<a href="{esc(sp)}" target="_blank"><img src="{esc(sp)}" alt="шаг" class="step-thumb"/></a>'
        fok, ftot = e.get("flakiness_ok"), e.get("flakiness_total")
        flak_cell = f"{fok}/{ftot}" if (fok is not None and ftot) else "—"
        steps_rows.append(
            f"<tr><td>{e.get('step')}</td><td class=\"url\">{esc((e.get('url') or '')[:80])}</td>"
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

    console_warnings = getattr(memory, "_session_console_warnings", None) or []
    cw_rows = []
    for c in console_warnings[-50:]:
        cw_rows.append(f"<tr><td><span class=\"sev sev-{esc(c.get('type', 'log'))}\">{esc(c.get('type', ''))}</span></td><td class=\"result\">{esc((c.get('text') or '')[:150])}</td></tr>")
    cw_body = "\n".join(cw_rows) if cw_rows else "<tr><td colspan=\"2\">Нет</td></tr>"

    mixed_content = getattr(memory, "_mixed_content", None) or []
    mc_body = "<br/>".join(esc((m.get("url") or "")[:80]) for m in mixed_content[-20:]) if mixed_content else "—"
    ws_issues = getattr(memory, "_websocket_issues", None) or []
    ws_body = "<br/>".join(f"{esc((w.get('url') or '')[:60])} ({w.get('event', '')})" for w in ws_issues[-20:]) if ws_issues else "—"
    api_log = getattr(memory, "_api_log", None) or []
    api_failed = [a for a in api_log if not a.get("ok", True)]
    api_rows = []
    for a in api_log[-50:]:
        method = a.get("method", "")
        url_short = (a.get("url") or "")[:80]
        status = a.get("status", "")
        ok = a.get("ok", True)
        cls = "result" if ok else "sev sev-major"
        api_rows.append(f"<tr><td>{esc(method)}</td><td class=\"url\">{esc(url_short)}</td><td class=\"{cls}\">{status}</td></tr>")
    api_body = "\n".join(api_rows) if api_rows else "<tr><td colspan=\"3\">Нет XHR/fetch</td></tr>"
    visual_regressions = getattr(memory, "_visual_regressions", None) or []
    vr_rows = []
    for v in visual_regressions:
        vr_rows.append(f"<tr><td class=\"url\">{esc((v.get('url') or '')[:80])}</td><td>{v.get('change_percent', 0)}%</td><td class=\"result\">{esc((v.get('detail') or '')[:100])}</td></tr>")
    vr_body = "\n".join(vr_rows) if vr_rows else "<tr><td colspan=\"3\">Нет</td></tr>"

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

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Kventin — отчёт сессии</title>
<style>
:root {{
  --bg: #0f0f14;
  --surface: #1a1a24;
  --surface2: #252532;
  --text: #e8e8ed;
  --text2: #9898a8;
  --accent: #6366f1;
  --accent2: #818cf8;
  --success: #22c55e;
  --warn: #eab308;
  --danger: #ef4444;
  --radius: 12px;
  --font: 'Segoe UI', system-ui, -apple-system, sans-serif;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 2rem;
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  background-image: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(99,102,241,0.15), transparent);
}}
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{
  font-size: 1.75rem;
  font-weight: 700;
  margin: 0 0 0.5rem;
  background: linear-gradient(135deg, var(--accent2), var(--accent));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}}
.sub {{
  color: var(--text2);
  font-size: 0.9rem;
  margin-bottom: 2rem;
}}
.cards {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 1rem;
  margin-bottom: 2rem;
}}
.card {{
  background: var(--surface);
  border: 1px solid var(--surface2);
  border-radius: var(--radius);
  padding: 1rem 1.25rem;
  text-align: center;
}}
.card .val {{
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--accent2);
}}
.card .lbl {{ font-size: 0.8rem; color: var(--text2); margin-top: 0.25rem; }}
section {{
  background: var(--surface);
  border: 1px solid var(--surface2);
  border-radius: var(--radius);
  padding: 1.5rem;
  margin-bottom: 1.5rem;
}}
section h2 {{
  font-size: 1rem;
  margin: 0 0 1rem;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}}
th {{
  text-align: left;
  padding: 0.6rem 0.75rem;
  color: var(--text2);
  font-weight: 600;
  border-bottom: 1px solid var(--surface2);
}}
td {{
  padding: 0.6rem 0.75rem;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}}
tr:hover td {{ background: rgba(255,255,255,0.02); }}
.url {{ max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.sel {{ max-width: 120px; overflow: hidden; text-overflow: ellipsis; }}
.result {{ max-width: 220px; overflow: hidden; text-overflow: ellipsis; }}
.act {{ padding: 0.2em 0.5em; border-radius: 6px; font-weight: 500; background: var(--surface2); color: var(--text); }}
.act-click {{ background: rgba(99,102,241,0.25); color: var(--accent2); }}
.act-type {{ background: rgba(34,197,94,0.2); color: var(--success); }}
.act-scroll {{ background: rgba(234,179,8,0.2); color: var(--warn); }}
.act-hover {{ background: rgba(129,140,248,0.2); color: var(--accent2); }}
.act-close_modal {{ background: rgba(239,68,68,0.2); color: var(--danger); }}
.act-fill_form {{ background: rgba(34,197,94,0.25); color: var(--success); }}
.src {{ padding: 0.2em 0.4em; border-radius: 4px; font-size: 0.8em; }}
.src-gigachat {{ background: rgba(99,102,241,0.2); color: var(--accent2); }}
.src-fast {{ background: var(--surface2); color: var(--text2); }}
.step-thumb {{ width: 80px; height: 45px; object-fit: cover; border-radius: 6px; display: block; }}
.thumb {{ width: 90px; }}
.key {{ font-family: monospace; color: var(--accent2); }}
.sev {{ padding: 0.2em 0.5em; border-radius: 6px; font-size: 0.85em; font-weight: 500; }}
.sev-critical {{ background: rgba(239,68,68,0.25); color: var(--danger); }}
.sev-major {{ background: rgba(234,179,8,0.25); color: var(--warn); }}
.sev-minor {{ background: var(--surface2); color: var(--text2); }}
pre {{ margin: 0; font-size: 0.8rem; color: var(--text2); white-space: pre-wrap; }}
.timeline-wrap {{ display: flex; flex-wrap: wrap; gap: 2px; margin-top: 0.5rem; }}
.timeline-bar {{ height: 20px; border-radius: 4px; display: inline-block; min-width: 4px; }}
.timeline-ok {{ background: var(--accent); opacity: 0.8; }}
.timeline-fail {{ background: var(--danger); }}
.replay-wrap {{ display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem; flex-wrap: wrap; }}
.replay-btn {{ padding: 0.4rem 0.8rem; border-radius: 8px; background: var(--surface2); color: var(--text); border: 1px solid var(--surface2); cursor: pointer; }}
.replay-btn:hover {{ background: var(--accent); color: #fff; }}
.replay-strip {{ display: flex; flex-wrap: wrap; gap: 4px; max-height: 120px; overflow-y: auto; }}
.replay-thumb {{ width: 80px; height: 45px; border-radius: 6px; overflow: hidden; border: 2px solid transparent; cursor: pointer; }}
.replay-thumb.active {{ border-color: var(--accent); }}
.replay-thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
@media (max-width: 768px) {{ .url, .result {{ max-width: 100px; }} }}
</style>
</head>
<body>
<div class="container">
<h1>Kventin</h1>
<p class="sub">Отчёт сессии AI-тестировщика · {esc(datetime.now().strftime("%d.%m.%Y %H:%M"))} · {esc(start_url or "—")[:60]}</p>
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
<pre>{esc(report_text)}</pre>
</section>
<section>
<h2>Покрытие</h2>
<p>{esc(coverage)}</p>
</section>
<section>
<h2>Навигация</h2>
<table>
<thead><tr><th>Шаг</th><th>От</th><th>Куда</th></tr></thead>
<tbody>
{nav_body}
</tbody>
</table>
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
var steps = {json.dumps([{{"step": e.get("step"), "thumb": ((e.get("screenshot_path") or "").split("/")[-1] if e.get("screenshot_path") else "")}} for e in step_log])};
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
 if(stepNum) document.querySelectorAll("section h2 + table tbody tr").forEach(function(r){{
  var n = r.querySelector("td");
  if(n && n.textContent == String(stepNum)) r.scrollIntoView({{block:"center"}});
 }});
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
<section>
<h2>Битые ссылки</h2>
<table>
<thead><tr><th>URL</th><th>Статус</th><th>Ошибка</th></tr></thead>
<tbody>
{broken_body}
</tbody>
</table>
</section>
<section>
<h2>Консоль (warnings/errors)</h2>
<table>
<thead><tr><th>Тип</th><th>Текст</th></tr></thead>
<tbody>{cw_body}</tbody>
</table>
</section>
<section>
<h2>Visual regression (baseline)</h2>
<table>
<thead><tr><th>URL</th><th>Изменение %</th><th>Детали</th></tr></thead>
<tbody>{vr_body}</tbody>
</table>
</section>
<section>
<h2>API (XHR/fetch)</h2>
<table>
<thead><tr><th>Метод</th><th>URL</th><th>Статус</th></tr></thead>
<tbody>{api_body}</tbody>
</table>
<p class="sub">Всего записей: {len(api_log)}, с ошибкой: {len(api_failed)}</p>
</section>
<section>
<h2>Mixed content / WebSocket</h2>
<p><strong>Mixed content:</strong> {mc_body}</p>
<p><strong>WebSocket:</strong> {ws_body}</p>
</section>
<section>
<h2>Шаги</h2>
<table>
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

        # Фильтруем: убираем уже протестированные элементы
        for elem in elements:
            ref = elem.get("ref", "")
            etype = elem.get("type", "")
            if etype == "file":
                continue  # file уже обработан выше (TEST_UPLOAD_FILE_PATH) или пропускаем
            act = "click" if etype in ("click", "link", "tab") else ("type" if etype == "input" else "select_option")
            key = f"{act}:{ref}"
            if not memory.is_element_tested(current_url, key):
                text = elem.get("text", "?")[:30]
                if etype == "input":
                    from src.form_strategies import detect_field_type, get_test_value
                    ftype = detect_field_type(placeholder=text, name=text)
                    val = get_test_value(ftype, "happy")
                    return {
                        "action": "type", "selector": ref, "value": val,
                        "reason": f"Ввод в '{text}'",
                        "test_goal": f"Заполнить поле {text}",
                        "expected_outcome": "Поле принимает значение",
                    }
                elif etype == "select":
                    return {
                        "action": "select_option", "selector": ref, "value": text.split(",")[0] if text else "",
                        "reason": "Выбор опции",
                        "test_goal": "Выбрать опцию в дропдауне",
                        "expected_outcome": "Опция выбирается",
                    }
                else:
                    return {
                        "action": "click", "selector": ref,
                        "reason": f"Клик: {text}",
                        "test_goal": f"Проверить '{text}'",
                        "expected_outcome": "Элемент реагирует",
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
        act_check = (action.get("action") or "").lower()
        sel_check = (action.get("selector") or "").strip()
        # Если это повтор — очистить очередь и запросить новое действие
        if act_check != "check_defect" and memory.is_already_done(act_check, sel_check, ""):
            print(f"[Agent] #{step} ⚠️ Scenario chain содержит повтор: {act_check} -> {sel_check[:40]}. Очищаю очередь.")
            memory._scenario_queue = []
            # Продолжить к обычному запросу к GigaChat
        else:
            action = memory._scenario_queue.pop(0)
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
    
    # ПРЕДВАРИТЕЛЬНАЯ проверка повтора ПЕРЕД выполнением
    act_precheck = (action.get("action") or "").lower()
    sel_precheck = (action.get("selector") or "").strip()
    val_precheck = (action.get("value") or "").strip()
    if act_precheck != "check_defect" and memory.is_already_done(act_precheck, sel_precheck, val_precheck):
        print(f"[Agent] #{step} ⚠️ GigaChat предложил повтор: {act_precheck} -> {sel_precheck[:40]}. Игнорирую и выбираю альтернативу.")
        memory.record_repeat()
        # Выбрать альтернативное действие
        if has_overlay:
            action = {"action": "close_modal", "selector": "", "reason": "GigaChat предложил повтор — закрываю оверлей"}
        elif not memory.should_avoid_scroll():
            action = {"action": "scroll", "selector": "down", "reason": "GigaChat предложил повтор — прокрутка"}
        else:
            action = {"action": "hover", "selector": "body", "reason": "GigaChat предложил повтор — hover для поиска"}
    # layout_issue → possible_bug
    if action.get("layout_issue") and not action.get("possible_bug"):
        action["possible_bug"] = action.get("layout_issue")

    act_type = (action.get("action") or "").lower()
    sel = (action.get("selector") or "").strip()
    val = (action.get("value") or "").strip()

    # Дедупликация действий: строгая проверка
    is_repeat = act_type != "check_defect" and memory.is_already_done(act_type, sel, val)
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
                findings["bug_to_report"] = post_action["possible_bug"]

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
    if memory and getattr(memory, "_last_step_flakiness", None):
        ok, total = memory._last_step_flakiness
        description += f"\n\nFlakiness: {ok}/{total} повторных прогонов успешны."
    severity = infer_defect_severity(
        summary, bug_description,
        console_log=console_log,
        network_failures=network_failures,
    )

    # Отправка в Jira — В ФОНЕ (семантическая проверка + создание тикета)
    _bg_submit(
        _create_defect_bg,
        summary, description, bug_description, attachment_paths, memory, severity,
    )


def _create_defect_bg(
    summary: str,
    description: str,
    bug_description: str,
    attachment_paths: Optional[list],
    memory: Optional[AgentMemory],
    severity: str = "major",
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
        key = create_jira_issue(
            summary=summary,
            description=description,
            attachment_paths=attachment_paths or None,
            severity=severity,
        )
        if key:
            print(f"[Agent] Дефект создан: {key} [{severity}]")
            if memory:
                memory.record_defect_created(key, summary, severity)
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


def _check_broken_links_bg(urls_list: List[str], memory: AgentMemory) -> None:
    """Фоновая проверка URL: HEAD-запросы, битые (4xx/5xx/timeout) добавляются в memory._broken_links."""
    import requests
    for url in urls_list:
        if url in memory._checked_link_urls:
            continue
        memory._checked_link_urls.add(url)
        try:
            r = requests.head(url, timeout=5, allow_redirects=True)
            if r.status_code >= 400:
                memory._broken_links.append({"url": url[:300], "status": r.status_code, "error": ""})
        except Exception as e:
            memory._broken_links.append({"url": url[:300], "status": 0, "error": str(e)[:200]})


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
    # ОТКЛЮЧЕНО — перезагрузка страницы не нужна
    return


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
            # Проверить, что это не повтор
            act = (action.get("action") or "").lower()
            sel = (action.get("selector") or "").strip()
            if act != "check_defect" and memory.is_already_done(act, sel, ""):
                print(f"[Agent] Self-heal предложил повтор: {act} -> {sel[:40]}. Игнорирую.")
                # Принудительно прокрутка
                action = {"action": "scroll", "selector": "up", "reason": "Self-heal: прокрутка для поиска новых элементов"}
            execute_action(page, action, memory)
            memory.add_action(action, result="self_heal")
    
    # Принудительная смена фазы
    memory.advance_tester_phase(force=True)
    # Очистить scenario queue при зацикливании
    if is_stuck and hasattr(memory, '_scenario_queue'):
        memory._scenario_queue = []
        print("[Agent] Очищена очередь scenario chain из-за зацикливания")


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
