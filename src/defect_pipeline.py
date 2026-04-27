"""
Создание дефекта: подготовка фактуры в основном потоке + фоновая отправка в Jira.

Раньше всё это жило в src/agent.py. Вынесено сюда — модуль не зависит ни от
конкретного класса AgentMemory, ни от run_agent: память используется через
duck-typing (нужны только методы get_steps_to_reproduce, last_canonical_locator,
last_action_summary, record_defect_created и атрибут pending_defect_futures).

Публичный API:
- create_defect(...)            — создать дефект (фильтр шума, evidence, отправка в фон).
- check_broken_links_bg(...)    — фоновая проверка URL на 4xx/5xx.
- is_semantic_duplicate(...)    — внутренняя проверка через GigaChat (используется и из bg).
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page

from src.bg_pool import bg_submit
from src.defect_builder import (
    build_defect_description,
    build_defect_summary,
    collect_evidence,
    infer_defect_severity,
)
from src.defect_rules import should_create_defect
from src.gigachat_client import consult_agent
from src.jira_client import (
    create_jira_issue,
    is_local_duplicate,
    register_local_defect,
)

LOG = logging.getLogger("kventin.defect")


def is_semantic_duplicate(bug_description: str, memory: Any) -> bool:
    """
    Уровень 3: семантическая проверка через GigaChat — «это тот же баг что уже есть?»
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
            f"Новый дефект: {bug_description[:300]}\n\n"
            f"Это ДУБЛЬ одного из уже заведённых? Ответь ОДНИМ словом: ДА или НЕТ."
        )
        if answer and "да" in answer.strip().lower()[:10]:
            LOG.info("Семантический дубль (GigaChat): %s", bug_description[:60])
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
) -> None:
    """Фоновое создание дефекта (Jira API + GigaChat дедупликация)."""
    try:
        if is_semantic_duplicate(bug_description, memory):
            print(f"[Agent] Пропуск дефекта (семантический дубль GigaChat): {summary[:60]}")
            LOG.info("Пропуск (семантический дубль): %s", summary[:60])
            register_local_defect(summary)
            return

        key = create_jira_issue(
            summary=summary,
            description=description,
            attachment_paths=attachment_paths or None,
            severity=severity,
        )
        if key:
            print(f"[Agent] Дефект создан: {key} [{severity}]")
            if memory:
                try:
                    memory.record_defect_created(key, summary, severity)
                except Exception:
                    pass
        else:
            print(f"[Agent] Jira вернула None (тикет не создан): {summary[:60]}")
    except Exception as e:
        print(f"[Agent] Ошибка фонового создания дефекта: {e}")
        LOG.error("Ошибка фонового создания дефекта: %s", e)
    finally:
        if attachment_paths:
            try:
                d = os.path.dirname(attachment_paths[0])
                if d and os.path.isdir(d) and "kventin_defect_" in d:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


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
    if not should_create_defect(
        bug_text=bug_description,
        console_log=console_log,
        network_failures=network_failures,
    ):
        print(f"[Agent] Пропуск дефекта (шум): {bug_description[:80]}")
        return

    summary = build_defect_summary(bug_description, current_url)

    if is_local_duplicate(summary, bug_description):
        print(f"[Agent] Пропуск дефекта (локальный дубль): {summary[:60]}")
        LOG.info("Пропуск дефекта (локальный дубль): %s", summary[:60])
        return

    attachment_paths = collect_evidence(page, console_log, network_failures)
    steps_to_reproduce = (
        memory.get_steps_to_reproduce() if memory and hasattr(memory, "get_steps_to_reproduce") else None
    )
    canonical_locator = (
        memory.last_canonical_locator() if memory and hasattr(memory, "last_canonical_locator") else ""
    )
    last_action_summary = (
        memory.last_action_summary() if memory and hasattr(memory, "last_action_summary") else ""
    )

    description = build_defect_description(
        bug_description, current_url,
        checklist_results=checklist_results,
        console_log=console_log,
        network_failures=network_failures,
        steps_to_reproduce=steps_to_reproduce,
    )
    if canonical_locator or last_action_summary:
        affected_block = "h3. Затронутый элемент\n"
        if canonical_locator:
            affected_block += f"Канонический локатор: {{{{{canonical_locator}}}}}\n"
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

    print(f"[Agent] Отправка дефекта в Jira (фон): {summary[:60]}")
    fut = bg_submit(
        _create_defect_bg,
        summary, description, bug_description, attachment_paths, memory, severity,
    )
    if fut is not None and memory is not None:
        try:
            memory.pending_defect_futures.append(fut)
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
