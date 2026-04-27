"""
AgentMemory: память агента за сессию.

Хранит всё, что агент делал: действия, дедупликацию по url_pattern + stable_key,
покрытие, навигационный граф, метрики, тест-план, дефекты, и т.п.

Раньше класс жил в src/agent.py. Вынесен сюда — он не зависит ни от Page, ни от
LLM-клиента, ни от run_agent. Это «чистая» структура данных + методы над ней.
"""
from __future__ import annotations

import hashlib
import logging
from concurrent.futures import Future
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.config import (
    MAX_ACTIONS_IN_MEMORY,
    MAX_SCROLLS_IN_ROW,
    PHASE_STEPS_TO_ADVANCE,
    SELF_HEAL_AFTER_FAILURES,
    URL_BUDGET_NO_PROGRESS,
)
from src.element_resolver import norm_key as _norm_key
from src.locators import detect_repeating_pattern, url_pattern as _url_pattern

LOG = logging.getLogger("kventin.memory")


class AgentMemory:
    """
    Хранит всё, что агент уже делал, чтобы не ходить по циклу.
    Учитываются: клики, ховеры, ввод в поля, закрытие модалок, выбор опций, прокрутки.
    """

    def __init__(self, max_actions: Optional[int] = None):
        self.actions: List[Dict[str, Any]] = []
        self.max_actions = max_actions or MAX_ACTIONS_IN_MEMORY
        self.defects_reported: List[str] = []
        self.iteration = 0
        # Ключи (normalized) уже выполненных действий — НЕ ПОВТОРЯТЬ
        self.done_click: set = set()
        self.done_hover: set = set()
        self.done_type: set = set()
        self.done_close_modal: int = 0
        self.done_select_option: set = set()
        self.done_scroll_down: int = 0
        self.done_scroll_up: int = 0
        # Лимиты, чтобы не зациклиться на одном типе действия
        self.max_scrolls_in_row = MAX_SCROLLS_IN_ROW
        self.last_actions_sequence: List[str] = []
        self.last_screenshot_hash: str = ""
        self.defects_on_current_step: int = 0
        self.coverage_zones: List[str] = []
        self.test_plan: List[str] = []
        self.critical_flow_done: set = set()
        self.defects_created: List[Dict[str, Any]] = []
        self.session_start: Optional[datetime] = None
        # Фаза тестирования: orient → smoke → critical_path → exploratory
        self.tester_phase: str = "orient"
        self.steps_in_phase: int = 0
        self.console_len_before_action: int = 0
        self.network_len_before_action: int = 0
        self.test_plan_completed: List[bool] = []
        self.consecutive_failures: int = 0
        self.form_strategy_iteration: int = 0
        self.reported_a11y_rules: set = set()
        self.reported_perf_rules: set = set()
        self.responsive_done: set = set()
        self.screenshot_before_action: Optional[str] = None
        self._pending_analysis: Optional[Dict[str, Any]] = None
        self._scenario_queue: List[Dict[str, Any]] = []
        self._consecutive_repeats: int = 0
        self._recent_action_keys: List[str] = []
        self._page_elements_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._important_pages: Dict[str, str] = {}
        self._page_coverage: Dict[str, set] = {}
        self._page_checklists: Dict[str, Dict[str, Any]] = {}
        # Модули страницы
        self.page_modules: List[Dict[str, Any]] = []
        self.current_module_index: int = 0
        self.steps_in_current_module: int = 0
        self._modules_page_url: str = ""
        # Структурированный лог шагов (для отчёта)
        self._step_log: List[Dict[str, Any]] = []
        # Граф навигации
        self._nav_graph: List[Dict[str, Any]] = []
        self._url_depths: Dict[str, int] = {}
        self._start_url_nav: str = ""
        # Ссылки/доп. проверки
        self._broken_links: List[Dict[str, Any]] = []
        self._checked_link_urls: set = set()
        self._websocket_issues: List[Dict[str, Any]] = []
        self._mixed_content: List[Dict[str, Any]] = []
        self._api_log: List[Dict[str, Any]] = []
        self._visual_regressions: List[Dict[str, Any]] = []
        self._visual_baseline_checked: set = set()
        self._selector_heal_cache: Dict[str, Dict[str, Any]] = {}
        self._browser_metrics_latest: Dict[str, Any] = {}
        self._browser_metrics_history: List[Dict[str, Any]] = []
        # --- Память по стабильным ключам (этап 2) ---
        self.done_by_url: Dict[str, Dict[str, set]] = {}
        self.steps_on_url_no_progress: Dict[str, int] = {}
        self.current_url_pattern: str = ""
        self.touched_keys_on_url: Dict[str, set] = {}
        # Future-ы фоновой отправки дефектов в Jira: дожидаемся в финале сессии.
        self.pending_defect_futures: List[Future] = []

    # ------------------------------------------------------------------ navigation

    def set_start_url_for_nav(self, url: str) -> None:
        self._start_url_nav = url or ""
        if self._start_url_nav:
            self._url_depths[self._start_url_nav] = 0

    def record_navigation(self, from_url: str, to_url: str, step: int, selector: str = "") -> None:
        from_url = (from_url or "").strip()
        to_url = (to_url or "").strip()
        if not to_url or from_url == to_url:
            return
        self._nav_graph.append(
            {"from_url": from_url, "to_url": to_url, "step": step, "selector": selector[:80]}
        )
        prev_depth = self._url_depths.get(from_url, 0)
        if to_url not in self._url_depths:
            self._url_depths[to_url] = prev_depth + 1

    def get_navigation_depth(self, url: str) -> int:
        return self._url_depths.get((url or "").strip(), 0)

    def append_step_log(self, entry: Dict[str, Any]) -> None:
        self._step_log.append(entry)

    # ------------------------------------------------------------------ modules

    def set_page_modules(self, modules: List[Dict[str, Any]], page_url: str) -> None:
        self.page_modules = list(modules) if modules else []
        self.current_module_index = 0
        self.steps_in_current_module = 0
        self._modules_page_url = page_url or ""

    def get_current_module(self) -> Optional[Dict[str, Any]]:
        if not self.page_modules or self.current_module_index >= len(self.page_modules):
            return None
        return self.page_modules[self.current_module_index]

    def advance_module(self) -> bool:
        if not self.page_modules or self.current_module_index >= len(self.page_modules) - 1:
            return False
        self.current_module_index += 1
        self.steps_in_current_module = 0
        return True

    def tick_module_step(self) -> None:
        self.steps_in_current_module += 1

    def get_module_context_text(self) -> str:
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
            lines.append(
                f"Сейчас тестируй только модуль: «{cur.get('name', '')}». "
                f"Выбери действие внутри этого модуля (selector ref:N из элементов этого блока)."
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ actions

    def add_action(self, action: Dict[str, Any], result: str = "") -> None:
        act = (action.get("action") or "").lower()
        sel = _norm_key(action.get("selector", ""))
        val = _norm_key(action.get("value", ""))
        stable_key = (action.get("_stable_key") or "").strip()
        url_pat = (action.get("_url_pattern") or self.current_url_pattern or "").strip()
        loop_key = stable_key or sel
        self.record_action_key(act, loop_key)

        self.iteration += 1
        step_ctx = action.get("_step_context") or {}
        entry = {
            "step": self.iteration,
            "time": datetime.now().strftime("%H:%M:%S"),
            "action": act,
            "selector": action.get("selector", ""),
            "stable_key": stable_key,
            "canonical_locator": (action.get("_canonical_locator") or "").strip(),
            "url_pattern": url_pat,
            "value": action.get("value", ""),
            "reason": action.get("reason", ""),
            "test_goal": action.get("test_goal", ""),
            "expected_outcome": action.get("expected_outcome", ""),
            "result": result[:200],
            "url_before": step_ctx.get("url_before", ""),
            "element_desc": step_ctx.get("element_desc", ""),
        }
        self.actions.append(entry)
        if len(self.actions) > self.max_actions:
            self.actions = self.actions[-self.max_actions:]

        # Дедупликация по url_pattern + stable_key (главный механизм).
        if url_pat and stable_key and act in (
            "click", "hover", "type", "select_option", "fill_form", "upload_file",
        ):
            self.done_by_url.setdefault(url_pat, {}).setdefault(act, set()).add(stable_key)
            touched = self.touched_keys_on_url.setdefault(url_pat, set())
            is_new_key = stable_key not in touched
            touched.add(stable_key)
            if is_new_key:
                self.steps_on_url_no_progress[url_pat] = 0
            else:
                self.steps_on_url_no_progress[url_pat] = (
                    self.steps_on_url_no_progress.get(url_pat, 0) + 1
                )

        # Старый механизм (fallback на случай отсутствия stable_key).
        def _safe_key(x):
            return x if isinstance(x, str) else str(x) if x is not None else ""

        if act == "click" and sel:
            self.done_click.add(_safe_key(sel))
        elif act == "hover" and sel:
            self.done_hover.add(_safe_key(sel))
        elif act == "type" and (sel or val):
            self.done_type.add(_safe_key(sel or val))
        elif act == "close_modal":
            self.done_close_modal += 1
        elif act == "select_option" and (sel or val):
            t = (_safe_key(sel), _safe_key(val)) if sel and val else (_safe_key(sel or val),)
            self.done_select_option.add(t)
        elif act == "scroll":
            if sel in ("down", "вниз", ""):
                self.done_scroll_down += 1
            elif sel in ("up", "вверх"):
                self.done_scroll_up += 1

        self.last_actions_sequence.append(act)
        if len(self.last_actions_sequence) > 10:
            self.last_actions_sequence = self.last_actions_sequence[-10:]

    def is_already_done(
        self,
        action: str,
        selector: str = "",
        value: str = "",
        stable_key: str = "",
        url_pattern: str = "",
    ) -> bool:
        """
        Проверить, не делали ли мы уже это действие.

        Приоритет: (url_pattern, action, stable_key). Иначе fallback на
        глобальные множества по selector.
        """
        act = (action or "").lower()
        sel = _norm_key(selector)
        val = _norm_key(value)

        if stable_key:
            url_pat = (url_pattern or self.current_url_pattern or "").strip()
            if url_pat:
                bucket = self.done_by_url.get(url_pat, {}).get(act)
                if bucket and stable_key in bucket:
                    return True

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
            pass
        return False

    def is_already_done_action(self, action: Dict[str, Any]) -> bool:
        if not isinstance(action, dict):
            return False
        return self.is_already_done(
            (action.get("action") or "").lower(),
            (action.get("selector") or "").strip(),
            (action.get("value") or "").strip(),
            stable_key=(action.get("_stable_key") or "").strip(),
            url_pattern=(action.get("_url_pattern") or "").strip(),
        )

    # ------------------------------------------------------------------ url budget

    def set_current_url_pattern(self, url: str) -> None:
        self.current_url_pattern = _url_pattern(url) if url else ""

    def should_force_back_to_start(self) -> bool:
        url_pat = self.current_url_pattern
        if not url_pat:
            return False
        return self.steps_on_url_no_progress.get(url_pat, 0) >= URL_BUDGET_NO_PROGRESS

    def reset_url_budget(self, url_pattern: str = "") -> None:
        url_pat = url_pattern or self.current_url_pattern
        if url_pat:
            self.steps_on_url_no_progress[url_pat] = 0

    def should_avoid_scroll(self) -> bool:
        recent = self.last_actions_sequence[-5:] if self.last_actions_sequence else []
        scroll_count = sum(1 for a in recent if a == "scroll")
        return scroll_count >= self.max_scrolls_in_row

    # ------------------------------------------------------------------ history / loops

    def get_history_text(self, last_n: int = 20) -> str:
        lines = [
            "⚠️⚠️⚠️ КРИТИЧНО: УЖЕ СДЕЛАНО (НЕ ПОВТОРЯТЬ, выбирай ДРУГОЕ действие!) ⚠️⚠️⚠️",
            "",
        ]
        if self.done_click:
            items = sorted(self.done_click, key=str)[-30:]
            lines.append(
                f"❌ Кликнуто ({len(self.done_click)}): "
                + ", ".join(f'"{str(x)[:40]}"' for x in items)
            )
        if self.done_hover:
            items = sorted(self.done_hover, key=str)[-20:]
            lines.append(
                f"❌ Наведено (hover) ({len(self.done_hover)}): "
                + ", ".join(f'"{str(x)[:40]}"' for x in items)
            )
        if self.done_type:
            items = sorted(self.done_type, key=str)[-20:]
            lines.append(
                f"❌ Ввод в поля ({len(self.done_type)}): "
                + ", ".join(f'"{str(x)[:40]}"' for x in items)
            )
        if self.done_close_modal:
            lines.append(f"❌ Закрыто модалок: {self.done_close_modal}")
        if self.done_select_option:
            items = list(self.done_select_option)[:20]
            lines.append("❌ Выбрано опций: " + ", ".join(str(x)[:50] for x in items))
        if self.done_scroll_down or self.done_scroll_up:
            lines.append(
                f"❌ Прокручено: вниз {self.done_scroll_down}, вверх {self.done_scroll_up}"
            )
        if self.should_avoid_scroll():
            lines.append(
                "⚠️ Внимание: недавно много прокруток — выбери клик/hover/type/close_modal, а не scroll."
            )
        if self._consecutive_repeats >= 2:
            lines.append(
                f"🚨 ЗАЦИКЛИВАНИЕ: {self._consecutive_repeats} повтора подряд! "
                f"СРОЧНО выбери ДРУГОЕ действие, которого НЕТ выше!"
            )
        lines.append("")
        lines.append("✅ Выбери действие, которого ещё НЕТ в списке выше (❌).")
        lines.append("")
        lines.append("Последние выполненные шаги:")
        for a in self.actions[-last_n:]:
            act = a.get("action", "?")
            sel = a.get("selector", "")[:45]
            res = a.get("result", "")[:50]
            lines.append(f"  #{a.get('step', '?')} {act} -> {sel} | {res}")
        return "\n".join(lines)

    def record_repeat(self) -> None:
        self._consecutive_repeats += 1

    def reset_repeats(self) -> None:
        self._consecutive_repeats = 0

    def is_stuck(self) -> bool:
        return self._consecutive_repeats >= 3

    def record_action_key(self, action: str, selector: str) -> None:
        key = f"{action}:{_norm_key(selector)}"
        self._recent_action_keys.append(key)
        if len(self._recent_action_keys) > 12:
            self._recent_action_keys.pop(0)
        # 1) Старая эвристика: 3 одинаковых ключа подряд.
        if len(self._recent_action_keys) >= 3 and len(set(self._recent_action_keys[-3:])) == 1:
            self._consecutive_repeats += 1
            return
        # 2) Новый детектор: цикл периода 2..4 (A,B,A,B / A,B,C,A,B,C / ...).
        period = detect_repeating_pattern(self._recent_action_keys, max_period=4)
        if period >= 2:
            self._consecutive_repeats += 1

    # ------------------------------------------------------------------ coverage

    def record_page_element(self, url: str, element_key: str) -> None:
        if url not in self._page_coverage:
            self._page_coverage[url] = set()
        self._page_coverage[url].add(element_key)

    def is_element_tested(self, url: str, element_key: str) -> bool:
        return element_key in self._page_coverage.get(url, set())

    def cache_page_elements(self, url: str, elements: List[Dict[str, Any]]) -> None:
        self._page_elements_cache[url] = elements[:50]

    def get_cached_elements(self, url: str) -> List[Dict[str, Any]]:
        return self._page_elements_cache.get(url, [])

    def remember_important_page(self, url: str, description: str) -> None:
        self._important_pages[url] = description[:200]

    def record_coverage_zone(self, zone: str) -> None:
        if zone and zone not in self.coverage_zones:
            self.coverage_zones.append(zone)
            if len(self.coverage_zones) > 20:
                self.coverage_zones = self.coverage_zones[-20:]

    # ------------------------------------------------------------------ test plan

    def set_test_plan(self, steps: List[str]) -> None:
        self.test_plan = list(steps)[:15]

    def get_steps_to_reproduce(self, max_steps: int = 15) -> List[str]:
        steps: List[str] = []
        prev_url = ""
        for a in self.actions[-max_steps:]:
            act = (a.get("action") or "").strip()
            sel = (a.get("selector") or "").strip()
            value = (a.get("value") or "").strip()
            reason = (a.get("reason") or "").strip()
            elem = (a.get("element_desc") or "").strip()
            url_before = (a.get("url_before") or "").strip()
            result = (a.get("result") or "").strip()

            if url_before and url_before != prev_url:
                steps.append(f"Открыть URL: {url_before}")
                prev_url = url_before

            locator_part = elem if elem else (sel or "—")
            verb_map = {
                "click": "Кликнуть по",
                "hover": "Навести курсор на",
                "type": "Ввести в поле",
                "select_option": "Выбрать опцию в",
                "press_key": "Нажать клавишу",
                "close_modal": "Закрыть модальное окно",
                "scroll": "Прокрутить страницу",
                "upload_file": "Загрузить файл в",
                "fill_form": "Заполнить форму",
                "explore": "Осмотреть страницу",
            }
            verb = verb_map.get(act, act or "Действие")

            if act == "scroll":
                direction = sel or "down"
                steps.append(f"Прокрутить страницу ({direction})")
            elif act == "close_modal":
                steps.append("Закрыть модальное окно" + (f" — {locator_part}" if elem else ""))
            elif act == "press_key":
                key = value or sel or "Enter"
                steps.append(f"Нажать клавишу «{key}»")
            elif act == "fill_form":
                steps.append("Заполнить форму тестовыми данными")
            elif act == "type" and (sel or elem):
                masked_value = value[:80] + ("…" if len(value) > 80 else "") if value else ""
                tail = f" значение: «{masked_value}»" if masked_value else ""
                reason_tail = f" (цель: {reason[:80]})" if reason else ""
                steps.append(f"Ввести в поле {locator_part}.{tail}{reason_tail}")
            elif act == "select_option" and (sel or elem):
                tail = f" (значение: «{value[:60]}»)" if value else ""
                steps.append(f"Выбрать опцию в {locator_part}{tail}")
            elif act in ("click", "hover", "upload_file") and (sel or elem):
                reason_tail = f" (цель: {reason[:80]})" if reason else ""
                steps.append(f"{verb} {locator_part}{reason_tail}")
            else:
                if sel or elem:
                    steps.append(f"{verb} {locator_part}")
                else:
                    steps.append(verb)

            low_res = result.lower()
            if low_res and any(
                x in low_res
                for x in ("error", "not_found", "not found", "5xx", "404", "timeout", "dead_click")
            ):
                steps.append(f"  └─ результат: {result[:120]}")
        return steps

    # ------------------------------------------------------------------ defects

    def record_defect_created(self, key: str, summary: str, severity: str = "major") -> None:
        self.defects_created.append(
            {"key": key, "summary": summary[:200], "severity": severity}
        )

    def last_canonical_locator(self) -> str:
        for a in reversed(self.actions):
            loc = (a.get("canonical_locator") or "").strip()
            if loc:
                return loc
        return ""

    def last_action_summary(self) -> str:
        if not self.actions:
            return ""
        a = self.actions[-1]
        act = (a.get("action") or "").strip() or "—"
        loc = (a.get("canonical_locator") or a.get("selector") or "").strip()
        val = (a.get("value") or "").strip()
        parts = [act]
        if loc:
            parts.append(loc)
        if val:
            v = val[:60] + ("…" if len(val) > 60 else "")
            parts.append(f"value={v!r}")
        return " | ".join(parts)

    # ------------------------------------------------------------------ phases

    def advance_tester_phase(self, force: bool = False) -> str:
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
        d = {
            "orient": "Фаза: ОРИЕНТАЦИЯ. Определи тип страницы. Выбери одно действие для понимания контекста (клик по главному CTA или ключевому элементу).",
            "smoke": "Фаза: SMOKE. Проверь ключевые кнопки/ссылки. Выбери один важный элемент и проверь его (клик или hover).",
            "critical_path": "Фаза: ОСНОВНОЙ СЦЕНАРИЙ. Тестируй главный сценарий: кнопка, форма, навигация. Одно целенаправленное действие.",
            "exploratory": "Фаза: ИССЛЕДОВАНИЕ. Проверь меню, футер, формы. Не повторяй уже сделанное. Осмысленная проверка.",
        }
        return d.get(self.tester_phase, d["exploratory"])

    # ------------------------------------------------------------------ session report

    def get_session_report_text(self) -> str:
        if not self.session_start:
            self.session_start = datetime.now()
        duration = (datetime.now() - self.session_start).total_seconds() if self.session_start else 0
        lines = [
            "=== Отчёт сессии AI-тестировщика Kventin ===",
            f"Шагов выполнено: {len(self.actions)}",
            f"Фаза: {self.tester_phase}",
            f"Время: {duration:.0f} с",
            f"Зоны покрытия: {', '.join(str(z) for z in self.coverage_zones) if self.coverage_zones else '—'}",
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
        m = getattr(self, "_browser_metrics_latest", None) or {}
        if m:
            lines.append("--- Метрики браузера (последний сбор) ---")
            lines.append(f"  Шаг: {m.get('step', '—')}, URL: {(m.get('url') or '')[:80]}")
            page = m.get("page") or {}
            for key, label in [
                ("ttfb", "TTFB"),
                ("domContentLoaded", "DCL"),
                ("loadComplete", "Load"),
                ("firstContentfulPaint", "FCP"),
                ("lcp", "LCP"),
            ]:
                if page.get(key) is not None:
                    lines.append(f"  {label}: {page[key]} мс")
            res = m.get("resources") or {}
            for rtype, data in sorted(res.items())[:6]:
                count = data.get("count", 0)
                avg = data.get("avgDuration")
                mx = data.get("durationMax")
                lines.append(f"  [{rtype}] n={count}, avg={avg or '—'} мс, max={mx or '—'} мс")
            resp = m.get("response") or {}
            if resp.get("avgMs") is not None:
                lines.append(f"  XHR/fetch отклик: avg={resp['avgMs']} мс, max={resp.get('maxMs', '—')} мс")
            if m.get("scrollHeight") is not None:
                lines.append(f"  scrollHeight/scrollWidth: {m.get('scrollHeight')} / {m.get('scrollWidth', '—')}")
            if m.get("usedJSHeapSize") is not None:
                used_mb = round(m["usedJSHeapSize"] / 1024 / 1024, 2)
                lines.append(f"  JS heap: {used_mb} МБ")
        lines.append("=== Конец отчёта ===")
        return "\n".join(lines)

    def set_test_plan_tracking(self) -> None:
        self.test_plan_completed = [False] * len(self.test_plan)

    def mark_test_plan_step(self, step_index: int) -> None:
        if 0 <= step_index < len(self.test_plan_completed):
            self.test_plan_completed[step_index] = True

    def get_test_plan_progress(self) -> str:
        if not self.test_plan:
            return ""
        done = sum(self.test_plan_completed)
        total = len(self.test_plan)
        lines = [f"Тест-план: {done}/{total} выполнено"]
        for i, (step, completed) in enumerate(zip(self.test_plan, self.test_plan_completed)):
            mark = "[x]" if completed else "[ ]"
            lines.append(f"  {mark} {i+1}. {step[:60]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ failures / self-heal

    def record_action_success(self) -> None:
        self.consecutive_failures = 0

    def record_action_failure(self) -> None:
        self.consecutive_failures += 1

    def needs_self_healing(self) -> bool:
        return self.consecutive_failures >= SELF_HEAL_AFTER_FAILURES

    # ------------------------------------------------------------------ logs / screenshot

    def snapshot_logs_before_action(self, console_log: list, network_failures: list) -> None:
        self.console_len_before_action = len(console_log)
        self.network_len_before_action = len(network_failures)

    def get_new_errors_after_action(
        self, console_log: list, network_failures: list
    ) -> Dict[str, Any]:
        new_console = [
            c for c in console_log[self.console_len_before_action:] if c.get("type") == "error"
        ]
        new_network = [
            n for n in network_failures[self.network_len_before_action:]
            if n.get("status") and n.get("status") >= 400
        ]
        return {"console_errors": new_console, "network_errors": new_network}

    def is_screenshot_changed(self, screenshot_b64: str) -> bool:
        if not screenshot_b64:
            return True
        h = hashlib.md5(screenshot_b64[:10000].encode()).hexdigest()
        changed = h != self.last_screenshot_hash
        self.last_screenshot_hash = h
        return changed


__all__ = ["AgentMemory"]
