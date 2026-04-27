"""
Дополнительные проверки агента: a11y, performance, responsive, iframe,
session persistence, scenario chains.

Раньше всё это жило в src/agent.py. Вынесено сюда — модуль изолирован,
не зависит от внутреннего цикла run_agent и не импортирует AgentMemory
(используется duck-typing).

Публичный API (имена сохранены для обратной совместимости — agent.py их
реэкспортирует под прежними приватными именами):

- run_a11y_check                — accessibility (axe-style правила)
- run_perf_check                — performance метрики
- run_responsive_check          — мобильный/планшетный viewport
- run_session_persistence_check — пока заглушка (отключено)
- run_iframe_check              — содержимое iframe
- request_scenario_chain        — запрос связанной цепочки действий у LLM
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page

from src.accessibility import check_accessibility, format_a11y_issues
from config import (
    ENABLE_IFRAME_TESTING,
    ENABLE_RESPONSIVE_TEST,
    ENABLE_SCENARIO_CHAINS,
    RESPONSIVE_VIEWPORTS,
    SCENARIO_CHAIN_LENGTH,
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
)
from src.defect_pipeline import create_defect
from src.gigachat_client import consult_agent_with_screenshot
from src.llm_parser import parse_llm_action, validate_llm_action
from src.performance import check_performance, format_performance_issues
from src.page_analyzer import take_screenshot_b64

LOG = logging.getLogger("kventin.checks")


def run_a11y_check(
    page: Page,
    memory: Any,
    current_url: str,
    console_log,
    network_failures,
) -> None:
    """Запустить accessibility проверки и завести дефекты на новые проблемы."""
    issues = check_accessibility(page)
    new_issues = [i for i in issues if i.get("rule") not in memory.reported_a11y_rules]
    if new_issues:
        text = format_a11y_issues(new_issues)
        print(f"[Agent] A11y: {len(new_issues)} новых проблем")
        for i in new_issues:
            memory.reported_a11y_rules.add(i.get("rule"))
        if any(i.get("severity") == "error" for i in new_issues):
            create_defect(
                page,
                f"Accessibility (a11y): {text}",
                current_url,
                [],
                console_log,
                network_failures,
                memory,
            )


def run_perf_check(
    page: Page,
    memory: Any,
    current_url: str,
    console_log,
    network_failures,
) -> None:
    """Запустить performance проверки и завести дефекты."""
    issues = check_performance(page)
    new_issues = [i for i in issues if i.get("rule") not in memory.reported_perf_rules]
    if new_issues:
        text = format_performance_issues(new_issues)
        print(f"[Agent] Perf: {len(new_issues)} проблем")
        for i in new_issues:
            memory.reported_perf_rules.add(i.get("rule"))
        if any(i.get("severity") == "warning" for i in new_issues):
            create_defect(
                page,
                f"Performance: {text}",
                current_url,
                [],
                console_log,
                network_failures,
                memory,
            )


def run_responsive_check(
    page: Page,
    memory: Any,
    current_url: str,
    console_log,
    network_failures,
) -> None:
    """
    Переключить viewport на мобильный/планшетный, сделать скриншот и проверить
    верстку через GigaChat. Возвращает viewport обратно в финале.
    """
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
                    "На скриншоте страница в мобильном/планшетном viewport. Есть ли проблемы верстки: "
                    "наложения, обрезки, горизонтальная прокрутка, элементы вне экрана? Если есть — "
                    "ответь JSON с action=check_defect и possible_bug. Если нет — ответь JSON с action=explore.",
                    screenshot_b64=screenshot_b64,
                )
                if answer:
                    action = parse_llm_action(answer)
                    if action and action.get("action") == "check_defect" and action.get("possible_bug"):
                        bug = f"[Responsive {name}] {action['possible_bug']}"
                        create_defect(page, bug, current_url, [], console_log, network_failures, memory)
        except Exception as e:
            LOG.debug("responsive check %s: %s", name, e)
        finally:
            page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
            time.sleep(1)


def run_session_persistence_check(
    page: Page,
    memory: Any,
    current_url: str,
    console_log,
    network_failures,
) -> None:
    """Перезагрузить страницу и проверить: состояние сохранилось? (отключено)"""
    return


def run_iframe_check(
    page: Page,
    memory: Any,
    current_url: str,
    console_log,
    network_failures,
) -> None:
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
                body_text = frame.evaluate(
                    "() => (document.body && document.body.innerText || '').trim().slice(0, 200)"
                )
                if not body_text or len(body_text) < 10:
                    create_defect(
                        page,
                        f"iframe пустой или не загрузился: src={src[:80]}, name={name[:30]}",
                        current_url,
                        [],
                        console_log,
                        network_failures,
                        memory,
                    )
            except Exception as e:
                LOG.debug("iframe check %s: %s", src[:40], e)
    except Exception as e:
        LOG.debug("iframe scan: %s", e)


def request_scenario_chain(
    page: Page,
    memory: Any,
    context_str: str,
    screenshot_b64: Optional[str],
) -> List[Dict]:
    """
    Попросить GigaChat сгенерировать цепочку из N связанных действий (сценарий).
    Возвращает список action-dict (не enriched — обогащение делается на стороне agent).
    """
    if not ENABLE_SCENARIO_CHAINS:
        return []
    n = SCENARIO_CHAIN_LENGTH
    answer = consult_agent_with_screenshot(
        context_str,
        f"Сгенерируй цепочку из {n} связанных действий (сценарий). Каждое действие — отдельный JSON-объект. "
        f"Ответь МАССИВОМ JSON: [{n} объектов с action/selector/value/reason/test_goal/expected_outcome]. "
        f"Пример: [{{'action':'click','selector':'Войти','value':'','reason':'открыть форму',"
        f"'test_goal':'проверка входа','expected_outcome':'форма логина'}}, ...]",
        screenshot_b64=screenshot_b64,
    )
    if not answer:
        return []
    try:
        cleaned = re.sub(r"^```(?:json)?\s*", "", answer.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"```\s*$", "", cleaned.strip(), flags=re.MULTILINE)
        arr = json.loads(cleaned)
        if isinstance(arr, list):
            return [
                validate_llm_action(a) for a in arr
                if isinstance(a, dict) and a.get("action")
            ][:n]
    except Exception:
        pass
    single = parse_llm_action(answer)
    return [validate_llm_action(single)] if single else []


__all__ = [
    "run_a11y_check",
    "run_perf_check",
    "run_responsive_check",
    "run_session_persistence_check",
    "run_iframe_check",
    "request_scenario_chain",
]
