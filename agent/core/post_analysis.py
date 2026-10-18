"""Post-action defect analysis pipeline."""
from __future__ import annotations

import logging
import time

from config import ENABLE_ORACLE_AFTER_ACTION, ENABLE_SECOND_PASS_BUG, ORACLE_ON_VISUAL_OR_ERROR
from agent.browser.screenshot import take_screenshot_b64
from agent.browser.page_analyzer import detect_active_overlays
from agent.checks.visual_diff import compute_screenshot_diff
from agent.core.bg_pool import bg_submit as _bg_submit
from agent.core.oracle import build_oracle_context, should_run_oracle
from agent.defects.defect_pipeline import create_defect as _create_defect
from agent.defects.defect_signals import add_oracle_signal, collect_rule_signals, pick_best_signal
from agent.llm.llm_client import ask_is_this_really_bug, consult_agent_with_screenshot
from agent.llm.llm_parser import parse_llm_action

LOG = logging.getLogger("kventin.post-analysis")

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
        LOG.info(
            "#%s Visual diff: %s (%.1f%%)",
            step,
            visual_diff_info.get("diff_zone", "?"),
            visual_diff_info.get("change_percent", 0),
        )

    # Берём только новые записи консоли/сети (появившиеся после действия).
    pre_lens = (action or {}).get("_pre_action_lens") or {}
    console_before_len = int(pre_lens.get("console") or 0)
    network_before_len = int(pre_lens.get("network") or 0)
    new_console = console_log_snapshot[console_before_len:] if console_before_len <= len(console_log_snapshot) else console_log_snapshot[-10:]
    new_network = network_snapshot[network_before_len:] if network_before_len <= len(network_snapshot) else network_snapshot[-5:]
    # Применяем шумовой фильтр (favicon, аналитика, расширения, ResizeObserver…)
    from agent.defects.defect_rules import (
        is_noise_url,
        is_noise_console_text,
    )
    new_errors = [
        c for c in new_console
        if (c.get("type") or "").lower() in ("error", "pageerror")
        and not is_noise_console_text(c.get("text") or "")
    ]
    new_network_fails = [
        n for n in new_network
        if n.get("status") and n.get("status") >= 400
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

    def _action_context_bug_prefix(actual: str) -> str:
        step_ctx = (action or {}).get("_step_context") or {}
        locator = step_ctx.get("element_desc") or (action or {}).get("_canonical_locator") or sel
        if isinstance(locator, str) and locator.startswith("ref:"):
            locator = (action or {}).get("_stable_key") or locator
        return (
            f"Действие агента: {act_type}.\n"
            f"Локатор/элемент: {locator[:500] if locator else '—'}\n"
            f"Цель: {(action or {}).get('test_goal') or (action or {}).get('reason') or '—'}\n"
            f"Ожидаемый результат: {expected_outcome or 'Действие выполняется успешно, ошибок в консоли и сети нет.'}\n"
            f"Фактический результат: {actual or result or 'Зафиксирована ошибка.'}\n"
        )

    defect_signals = collect_rule_signals(
        action=action,
        result=result,
        current_url=current_url,
        new_console=new_errors,
        new_network=new_network,
    )

    # 5xx
    five_xx_signal = next((s for s in defect_signals if s.kind == "network_5xx"), None)
    if five_xx_signal:
        findings["five_xx_bug"] = (
            f"HTTP 5xx после действия агента.\n\n"
            f"{_action_context_bug_prefix('HTTP 5xx в сетевом запросе')}\n"
            f"{five_xx_signal.to_bug_text()}"
            + (f"\n\nНовые ошибки консоли после действия:\n{console_brief}" if console_brief else "")
        )

    # Оракул (LLM — thread-safe). Lazy: только при изменении экрана или новых ошибках.
    run_oracle = should_run_oracle(
        enabled=ENABLE_ORACLE_AFTER_ACTION,
        action_type=act_type,
        has_screenshot=bool(post_screenshot_b64),
        visual_diff=visual_diff_info,
        new_errors=new_errors,
        new_network=new_network,
        lazy_on_visual_or_error=ORACLE_ON_VISUAL_OR_ERROR,
    )
    if (
        ENABLE_ORACLE_AFTER_ACTION
        and post_screenshot_b64
        and expected_outcome
        and act_type in ("click", "type", "select_option", "fill_form", "upload_file", "press_key")
    ):
        run_oracle = True
    if run_oracle:
        oracle_context = build_oracle_context(
            action=action,
            result=result,
            expected_outcome=expected_outcome,
            visual_diff=visual_diff_info,
            new_errors=new_errors,
            new_network=new_network,
        )
        oracle_ans = consult_agent_with_screenshot(
            oracle_context,
            "Произошло ли ожидаемое? Ответь: успех / ошибка / неясно.",
            screenshot_b64=post_screenshot_b64,
        )
        if oracle_ans and "ошибка" in oracle_ans.lower():
            findings["oracle_error"] = True
            add_oracle_signal(defect_signals, oracle_error=True, console_brief=console_brief)

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
                bug_text = _action_context_bug_prefix(post_action["possible_bug"]) + "\n" + post_action["possible_bug"]
                if console_brief:
                    bug_text = f"{bug_text}\n\nНовые ошибки консоли после действия:\n{console_brief}"
                findings["bug_to_report"] = bug_text
                add_oracle_signal(
                    defect_signals,
                    possible_bug=post_action["possible_bug"],
                    console_brief=console_brief,
                )
        else:
            LOG.warning(
                "#%s оракул не ответил (LLM пуст) — fallback на правила без LLM",
                step,
            )

    # Фолбэк: action_failure / pageerror / 4xx — надёжные сигналы, заводим дефект
    # независимо от LLM и независимо от типа действия (даже на close_modal/scroll).
    # Порядок: сначала самый специфичный (UI: клик не прошёл из-за overlay/timeout),
    # потом JS-ошибки в консоли, потом 4xx на ключевых эндпоинтах.
    if not findings["bug_to_report"] and not findings["five_xx_bug"]:
        best_signal = pick_best_signal(s for s in defect_signals if s.kind != "network_5xx")
        if best_signal:
            findings["bug_to_report"] = (
                _action_context_bug_prefix(best_signal.to_bug_text())
                + "\n"
                + best_signal.to_bug_text()
                + (f"\n\nНовые ошибки консоли после действия:\n{console_brief}" if console_brief else "")
            )
            LOG.info(
                "#%s defect signal %s → дефект (severity=%s confidence=%.2f)",
                step,
                best_signal.kind,
                best_signal.severity,
                best_signal.confidence,
            )

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

    # Новый оверлей — обработать сразу.
    # ИСКЛЮЧЕНИЕ: если действие провалилось с признаком "intercepts pointer events" /
    # таймаута клика — это и есть сам инцидент (overlay-перехватчик мешает клику),
    # и его надо довести до фонового анализа, чтобы завести дефект через rule_action_failure.
    res_str = result if isinstance(result, str) else ""
    is_action_failure = (
        res_str.startswith("click_error")
        or res_str.startswith("type_error")
        or res_str.startswith("hover_error")
    )
    if post_data["new_overlay"] and not is_action_failure:
        print(f"[Agent] #{step} Появился оверлей: {', '.join(post_data['overlay_types'])}")
        memory.add_action(
            {"action": "overlay_detected", "selector": ", ".join(post_data["overlay_types"])},
            result="new_overlay_appeared"
        )
        return

    # ============================================================
    # СИНХРОННЫЙ FAST-PATH: быстрые правила в main thread.
    # Если правило сработало — дефект заводится СРАЗУ, не дожидаясь фонового
    # LLM-оракула. Раньше всё это было только в _analyze_in_background, и
    # когда LLM висел по таймауту 120с, future не успевал к следующему
    # шагу — _flush_pending_analysis получал пустой findings и дефект терялся.
    # ============================================================
    if memory.defects_on_current_step == 0:
        from agent.defects.defect_rules import is_noise_console_text
        pre_lens = {
            "console": memory.console_len_before_action,
            "network": memory.network_len_before_action,
        }
        new_console = console_log[pre_lens["console"]:] if pre_lens["console"] <= len(console_log) else console_log[-10:]
        new_network = network_failures[pre_lens["network"]:] if pre_lens["network"] <= len(network_failures) else network_failures[-5:]
        new_errors_sync = [
            c for c in new_console
            if (c.get("type") or "").lower() in ("error", "pageerror")
            and not is_noise_console_text(c.get("text") or "")
        ]
        sync_signals = collect_rule_signals(
            action=action,
            result=result,
            current_url=current_url,
            new_console=new_errors_sync,
            new_network=new_network,
        )
        sync_signal = pick_best_signal(sync_signals)
        sync_bug = None
        if sync_signal:
            if sync_signal.kind == "network_5xx":
                sync_bug = (
                f"HTTP 5xx после действия агента.\n\n"
                f"Действие: {act_type} | selector: {sel[:100]} | value: {val[:50]}\n\n"
                f"{sync_signal.to_bug_text()}"
                )
            else:
                sync_bug = sync_signal.to_bug_text()
            LOG.info(
                "#%s sync defect signal %s → дефект (severity=%s confidence=%.2f)",
                step,
                sync_signal.kind,
                sync_signal.severity,
                sync_signal.confidence,
            )

        if sync_bug:
            try:
                _create_defect(page, sync_bug, current_url, checklist_results, console_log, network_failures, memory)
                memory.defects_on_current_step += 1
            except Exception:
                LOG.exception("#%s sync defect creation: исключение", step)

    # Снимки логов: берём ПОЛНЫЙ срез — в фоне выделим именно новые записи после действия.
    console_snapshot = list(console_log)
    network_snapshot = list(network_failures)
    before_screenshot = memory.screenshot_before_action
    # Запомним границы (сколько записей было ДО действия), чтобы в фоне брать именно «новые».
    action["_pre_action_lens"] = {
        "console": memory.console_len_before_action,
        "network": memory.network_len_before_action,
    }

    queue_items = getattr(memory, "_pending_analyses", None)
    if queue_items is None:
        memory._pending_analyses = []
        queue_items = memory._pending_analyses
    # Deterministic rules have already run. Under pressure, omit only the
    # optional LLM oracle and do not start work that cannot be tracked.
    if len(queue_items) >= 8:
        LOG.warning("#%s analysis queue is full; dropping optional oracle", step)
        return

    # Запускаем анализ В ФОНЕ — main thread свободен для следующего шага
    future = _bg_submit(
        _analyze_in_background,
        post_data, step, action, result, act_type, sel, val, expected_outcome, possible_bug,
        current_url, checklist_results, console_snapshot, network_snapshot, memory,
        before_screenshot,
        pool_name="analysis",
    )

    pending_item = {
        "future": future,
        "step": step,
        "current_url": current_url,
        "checklist_results": checklist_results,
    }
    queue_items.append(pending_item)

def _flush_pending_analysis(
    page,
    memory,
    console_log,
    network_failures,
    *,
    wait_for_all: bool = False,
    timeout: float = 15.0,
):
    """Drain completed analyses without losing slow futures.

    During normal steps this is non-blocking. At shutdown it waits against one
    aggregate deadline, then leaves deterministic fast-path findings intact.
    """
    pending_items = list(getattr(memory, "_pending_analyses", None) or [])
    if not pending_items:
        return

    deadline = time.monotonic() + max(0.0, timeout) if wait_for_all else 0.0
    remaining = []
    for pending in pending_items:
        future = pending.get("future")
        step = pending.get("step", "?")
        if future is None:
            continue
        if not future.done() and wait_for_all:
            wait_left = max(0.0, deadline - time.monotonic())
            if wait_left > 0:
                try:
                    future.result(timeout=wait_left)
                except Exception:
                    pass
        if not future.done():
            remaining.append(pending)
            continue
        try:
            findings = future.result(timeout=0) or {}
        except Exception as exc:
            LOG.error("#%s pending analysis FAILED: %s", step, exc)
            continue
        if not findings:
            continue

        current_url = pending.get("current_url") or ""
        checklist_results = pending.get("checklist_results") or []
        LOG.info(
            "#%s findings: five_xx=%s bug=%s oracle_err=%s",
            step,
            bool(findings.get("five_xx_bug")),
            bool(findings.get("bug_to_report")),
            findings.get("oracle_error"),
        )

        five_xx_bug = findings.get("five_xx_bug")
        if five_xx_bug:
            _create_defect(
                page,
                five_xx_bug,
                current_url,
                checklist_results,
                console_log,
                network_failures,
                memory,
            )

        pbug = findings.get("bug_to_report")
        if pbug:
            if ENABLE_SECOND_PASS_BUG and not ask_is_this_really_bug(pbug, None):
                LOG.info("#%s background analysis: rejected by second pass", step)
            else:
                _create_defect(
                    page,
                    pbug,
                    current_url,
                    checklist_results,
                    console_log,
                    network_failures,
                    memory,
                )

    memory._pending_analyses = remaining
    if wait_for_all and remaining:
        LOG.warning("Analysis shutdown deadline reached; %d task(s) still running", len(remaining))

__all__ = ["_analyze_in_background", "_collect_post_data", "_flush_pending_analysis", "_step_post_analysis"]
