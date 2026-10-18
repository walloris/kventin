"""
Создание дефекта: подготовка фактуры в основном потоке + фоновая отправка в Jira.

Раньше всё это жило в agent/agent.py. Вынесено сюда — модуль не зависит ни от
конкретного класса AgentMemory, ни от run_agent: память используется через
duck-typing (нужны только методы get_steps_to_reproduce, last_canonical_locator,
last_action_summary, record_defect_created и атрибут pending_defect_futures).

Публичный API:
- create_defect(...)            — создать дефект (фильтр шума, evidence, отправка в фон).
- check_broken_links_bg(...)    — фоновая проверка URL на 4xx/5xx.
- is_semantic_duplicate(...)    — внутренняя проверка через LLM (используется и из bg).
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page

import re

from agent.core.bg_pool import bg_submit
from agent.defects.defect_builder import (
    build_defect_description,
    build_defect_summary,
    collect_evidence,
    format_retest_spec_wiki,
    infer_defect_severity,
    memory_actions_to_retest_plan,
)
from agent.defects.defect_rules import should_create_defect
from agent.llm.llm_client import consult_agent
from agent.defects.jira_client import (
    build_defect_signature,
    create_jira_issue,
    register_local_defect,
    release_local_defect,
    reserve_local_defect,
)
from agent.actions.locators import url_pattern as _url_pattern

LOG = logging.getLogger("kventin.defect")


_SIG_HEADERS = (
    ("page_load", r"\[Загрузка страницы\]"),
    ("action_failure", r"кнопка/элемент недоступна для клика|перекрыта другим элементом|не становится кликабельным|оверлей невозможно закрыть|modal_close_failed"),
    ("pageerror", r"JS pageerror|JS error в консоли"),
    ("network_5xx", r"HTTP 5xx|http\s*5\d\d"),
    ("network_4xx", r"HTTP\s*4\d\d|http\s*4\d\d"),
    ("a11y", r"\[A11y\]|Accessibility"),
    ("perf", r"\[Perf\]|Performance"),
    ("responsive", r"\[Responsive"),
)


def _cleanup_evidence(attachment_paths: Optional[List[str]]) -> None:
    if not attachment_paths:
        return
    try:
        directory = os.path.dirname(os.fspath(attachment_paths[0]))
        if directory and os.path.isdir(directory) and "kventin_defect_" in directory:
            shutil.rmtree(directory, ignore_errors=True)
    except Exception:
        LOG.debug("Could not clean temporary defect evidence", exc_info=True)


def _classify_kind(bug_text: str) -> str:
    t = (bug_text or "").lower()
    for kind, pat in _SIG_HEADERS:
        if re.search(pat, t, re.IGNORECASE):
            return kind
    return ""


_RULE_FRAGMENTS = (
    ("overlay_close", r"оверлей невозможно закрыть|modal_close_failed"),
    ("intercept", r"intercepts? pointer events|перекрыта другим элементом"),
    ("timeout_ui", r"не становится кликабельным|exceeded while waiting"),
    ("rule_5xx", r"\b5\d\d\b"),
    ("rule_4xx_main", r"\b4\d\d\b"),
    ("typeerror", r"\bTypeError\b"),
    ("referenceerror", r"\bReferenceError\b"),
    ("syntaxerror", r"\bSyntaxError\b"),
    ("uncaught", r"\bUncaught\b"),
)


def _classify_rule(bug_text: str) -> str:
    t = (bug_text or "")
    for rule, pat in _RULE_FRAGMENTS:
        if re.search(pat, t, re.IGNORECASE):
            return rule
    return ""


def _extract_error_signature(bug_text: str) -> str:
    """Достать стабильный «отпечаток ошибки» из текста дефекта.

    Что ищем:
      • первый кадр стека вида «at https://x.test/static/app.js:12:34» / «foo.js:12»
      • строку «STATUS METHOD path» из 5xx/4xx-описаний
      • первую сигнальную строку «TypeError: …», «ReferenceError: …»
    """
    if not bug_text:
        return ""
    m = re.search(r"\b(\d{3})\s+(GET|POST|PUT|DELETE|PATCH|HEAD)\s+([^\s\)]+)", bug_text)
    if m:
        path = re.sub(r"\?.*$", "", m.group(3))
        # обрезать query/fragment, нормализовать числовые сегменты
        return f"{m.group(1)} {m.group(2)} {path[:120]}"
    m = re.search(r"\bat\s+(https?://\S+|\S+\.js):(\d+)(?::(\d+))?", bug_text)
    if m:
        return f"at {m.group(1)}:{m.group(2)}"
    m = re.search(r"\b(TypeError|ReferenceError|SyntaxError|RangeError)\b[:\s]+([^\n]{0,140})", bug_text)
    if m:
        head = re.sub(r"\s+", " ", m.group(2)).strip()
        return f"{m.group(1)}: {head[:120]}"
    return ""


def _compute_defect_signature(
    *,
    bug_description: str,
    current_url: str,
    canonical_locator: str = "",
) -> str:
    kind = _classify_kind(bug_description)
    rule = _classify_rule(bug_description)
    err_sig = _extract_error_signature(bug_description)
    pat = _url_pattern(current_url) if current_url else ""
    return build_defect_signature(
        kind=kind,
        rule=rule,
        url_pattern=pat,
        locator=canonical_locator or "",
        error_signature=err_sig,
    )


def _has_hard_error_signal(
    bug_description: str,
    console_log: Optional[List[Dict[str, Any]]] = None,
    network_failures: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    if _extract_error_signature(bug_description):
        return True
    if any((c.get("type") or "").lower() in ("error", "pageerror") for c in (console_log or [])):
        return True
    return any(int(n.get("status") or 0) >= 400 for n in (network_failures or []))


def _last_step_defect_context(memory: Any, bug_description: str) -> str:
    if not memory or not getattr(memory, "actions", None):
        return ""
    action = (memory.actions or [])[-1]
    act = (action.get("action") or "—").strip()
    element_desc = (action.get("element_desc") or "").strip()
    canonical_locator = (action.get("canonical_locator") or "").strip()
    selector = (action.get("selector") or "").strip()
    stable_key = (action.get("stable_key") or "").strip()
    expected = (action.get("expected_outcome") or "Действие выполняется успешно, ошибок в консоли и сети нет.").strip()
    factual = (action.get("result") or bug_description or "Зафиксирована ошибка.").strip()
    goal = (action.get("test_goal") or action.get("reason") or "").strip()

    locator_lines = []
    if element_desc:
        locator_lines.append(f"* Элемент: {{{{{element_desc[:800]}}}}}")
    if canonical_locator:
        locator_lines.append(f"* Читаемый Playwright locator: {{{{{canonical_locator[:800]}}}}}")
    elif selector and not selector.startswith("ref:"):
        locator_lines.append(f"* Locator: {{{{{selector[:500]}}}}}")
    if stable_key:
        locator_lines.append(f"* Stable key: {{{{{stable_key[:300]}}}}}")

    lines = [
        "h3. Последний шаг агента",
        f"* Действие: {act}",
    ]
    if goal:
        lines.append(f"* Цель: {goal[:500]}")
    lines.extend(locator_lines)
    lines.extend([
        "",
        "h3. Ожидаемый результат (ОР)",
        expected[:1200],
        "",
        "h3. Фактический результат (ФР)",
        factual[:1600],
    ])
    return "\n".join(lines)


def is_semantic_duplicate(bug_description: str, memory: Any) -> bool:
    """
    Уровень 3: семантическая проверка через LLM — «это тот же баг что уже есть?»
    """
    if not memory or not getattr(memory, "defects_created", None):
        return False
    existing = "\n".join(
        f"- {d['key']}: {d['summary'][:80]}"
        for d in memory.defects_created[-10:]
    )
    try:
        answer = consult_agent(
            f"Уже заведённые дефекты:\n{existing}\n\n"
            f"Новый дефект: {bug_description[:300]}",
            "Это ДУБЛЬ одного из уже заведённых? Ответь ОДНИМ словом: ДА или НЕТ.",
        )
        if answer and "да" in answer.strip().lower()[:10]:
            LOG.info("Семантический дубль (LLM): %s", bug_description[:60])
            return True
    except Exception as e:
        LOG.debug("semantic dedup error: %s", e)
    return False


def _create_defect_bg(
    summary: str,
    description: str,
    bug_description: str,
    attachment_paths: Optional[list],
    memory: Optional[Any],
    severity: str = "major",
    signature: str = "",
) -> None:
    """Фоновое создание дефекта (Jira API + LLM-дедупликация)."""
    LOG.info(
        "_create_defect_bg: старт summary=%r severity=%s sig=%r",
        summary[:140], severity, signature[:120],
    )
    try:
        if is_semantic_duplicate(bug_description, memory):
            print(f"[Agent] Пропуск дефекта (семантический дубль LLM): {summary[:60]}")
            LOG.info("_create_defect_bg: отбито is_semantic_duplicate")
            register_local_defect(summary, signature=signature)
            return

        key = create_jira_issue(
            summary=summary,
            description=description,
            attachment_paths=attachment_paths or None,
            severity=severity,
            skip_local_duplicate_check=True,
        )
        if key:
            print(f"[Agent] Дефект создан: {key} [{severity}]")
            LOG.info("_create_defect_bg: успех key=%s", key)
            register_local_defect(summary, signature=signature)
            if memory:
                try:
                    memory.record_defect_created(key, summary, severity)
                except Exception:
                    LOG.exception("record_defect_created failed")
        else:
            print(f"[Agent] Jira вернула None (тикет не создан): {summary[:60]}")
            LOG.warning("_create_defect_bg: create_jira_issue=None — смотри логи Jira выше")
            release_local_defect(summary, signature=signature)
    except Exception:
        release_local_defect(summary, signature=signature)
        LOG.exception("_create_defect_bg: исключение при создании дефекта")
        print(f"[Agent] Ошибка фонового создания дефекта (см. логи)")
    finally:
        _cleanup_evidence(attachment_paths)


def create_defect(
    page: Page,
    bug_description: str,
    current_url: str,
    checklist_results: List[Dict[str, Any]],
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
    memory: Optional[Any] = None,
) -> None:
    """
    Создать дефект: быстрые проверки в main thread, Jira — в фоне.

    Шаги:
      1. Шумовой фильтр (defect_rules.should_create_defect) — отбросить очевидный шум.
      2. summary → локальная дедупликация по сессии.
      3. Сбор evidence (скриншот, console.log, network.log).
      4. Описание дефекта + блок «Затронутый элемент» (canonical locator + действие).
      5. infer_defect_severity → уровень критичности.
      6. Отправка в Jira в фоне; Future сохраняем в memory.pending_defect_futures.
    """
    LOG.info(
        "create_defect: вход url=%s bug_head=%r",
        current_url[:80] if current_url else "",
        (bug_description or "")[:140],
    )

    hard_signal = _has_hard_error_signal(bug_description, console_log, network_failures)
    if not hard_signal and not should_create_defect(
        bug_text=bug_description,
        console_log=console_log,
        network_failures=network_failures,
    ):
        print(f"[Agent] Пропуск дефекта (шум, should_create_defect=False): {bug_description[:80]}")
        LOG.info("create_defect: отбито should_create_defect")
        return

    summary = build_defect_summary(bug_description, current_url)
    LOG.info("create_defect: summary=%r", summary[:140])

    canonical_locator = (
        memory.last_canonical_locator() if memory and hasattr(memory, "last_canonical_locator") else ""
    )
    signature = _compute_defect_signature(
        bug_description=bug_description,
        current_url=current_url,
        canonical_locator=canonical_locator,
    )
    if signature:
        LOG.info("create_defect: signature=%r", signature[:160])

    attachment_paths = collect_evidence(page, console_log, network_failures)
    steps_to_reproduce = (
        memory.get_steps_to_reproduce() if memory and hasattr(memory, "get_steps_to_reproduce") else None
    )
    last_action_summary = (
        memory.last_action_summary() if memory and hasattr(memory, "last_action_summary") else ""
    )

    retest_wiki = ""
    if memory:
        plan_dict = memory_actions_to_retest_plan(
            memory,
            current_url,
            bug_description=bug_description,
            console_log=console_log,
            network_failures=network_failures,
        )
        if plan_dict and plan_dict.get("steps"):
            try:
                retest_wiki = format_retest_spec_wiki(plan_dict)
                LOG.info("create_defect: retest plan steps=%d", len(plan_dict.get("steps") or []))
            except Exception:
                LOG.exception("create_defect: format_retest_spec_wiki failed")

    description = build_defect_description(
        bug_description, current_url,
        checklist_results=checklist_results,
        console_log=console_log,
        network_failures=network_failures,
        steps_to_reproduce=steps_to_reproduce,
        retest_spec_wiki=retest_wiki or None,
    )
    step_context_block = _last_step_defect_context(memory, bug_description)
    if step_context_block:
        anchor = "h3. Шаги воспроизведения"
        if anchor in description:
            description = description.replace(anchor, step_context_block + "\n\n" + anchor, 1)
        else:
            description = step_context_block + "\n\n" + description
    if canonical_locator or last_action_summary:
        affected_block = "h3. Затронутый элемент\n"
        if canonical_locator:
            affected_block += (
                f"Локатор (Playwright-стиль, читаемый): {{{{{canonical_locator}}}}}\n"
            )
        if last_action_summary:
            affected_block += f"Последнее действие: {{{{{last_action_summary}}}}}\n"
        anchor = "h3. Шаги воспроизведения"
        if anchor in description:
            description = description.replace(anchor, affected_block + "\n" + anchor, 1)
        else:
            description = affected_block + "\n" + description

    if memory and getattr(memory, "_last_step_flakiness", None):
        ok, total = memory._last_step_flakiness
        description += f"\n\nFlakiness: {ok}/{total} повторных прогонов успешны."

    severity = infer_defect_severity(
        summary, bug_description,
        console_log=console_log,
        network_failures=network_failures,
    )

    # Reserve only after the fallible evidence/description preparation. A
    # preparation exception must never poison deduplication for later signals.
    if not reserve_local_defect(summary, signature=signature):
        print(f"[Agent] Пропуск дефекта (локальный дубль): {summary[:60]}")
        LOG.info("create_defect: отбито is_local_duplicate")
        _cleanup_evidence(attachment_paths)
        return

    print(f"[Agent] Отправка дефекта в Jira (фон): {summary[:60]} [{severity}]")
    LOG.info("create_defect: ставим в фон отправку в Jira (severity=%s)", severity)
    try:
        fut = bg_submit(
            _create_defect_bg,
            summary, description, bug_description, attachment_paths, memory, severity, signature,
            pool_name="jira",
        )
    except Exception:
        LOG.exception("create_defect: фон недоступен — создаём дефект синхронно")
        _create_defect_bg(
            summary,
            description,
            bug_description,
            attachment_paths,
            memory,
            severity,
            signature,
        )
        return
    if fut is None:
        LOG.error("create_defect: bg_submit вернул None — создаём дефект синхронно")
        _create_defect_bg(summary, description, bug_description, attachment_paths, memory, severity, signature)
        return
    if memory is not None:
        try:
            memory.pending_defect_futures.append(fut)
            def _forget_completed(done_future) -> None:
                try:
                    memory.pending_defect_futures.remove(done_future)
                except (AttributeError, ValueError):
                    pass
            fut.add_done_callback(_forget_completed)
        except AttributeError:
            memory.pending_defect_futures = [fut]


def check_broken_links_bg(urls_list: List[str], memory: Any) -> None:
    """
    Фоновая проверка URL: HEAD-запросы; битые (4xx/5xx/timeout) добавляются в
    memory._broken_links. memory._checked_link_urls — кэш уже проверенных URL.
    """
    import requests  # ленивый импорт: requests тяжёлый
    for url in urls_list:
        if url in memory._checked_link_urls:
            continue
        memory._checked_link_urls.add(url)
        try:
            r = requests.head(url, timeout=5, allow_redirects=True)
            if r.status_code >= 400:
                memory._broken_links.append(
                    {"url": url[:300], "status": r.status_code, "error": ""}
                )
        except Exception as e:
            memory._broken_links.append(
                {"url": url[:300], "status": 0, "error": str(e)[:200]}
            )


__all__ = [
    "create_defect",
    "check_broken_links_bg",
    "is_semantic_duplicate",
]
