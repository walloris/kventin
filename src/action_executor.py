"""Action execution dispatch for the browser-testing agent.

Low-level Playwright operations still live close to the browser integration, but
the action dispatch contract is isolated here so the main agent loop does not
need to know every action type.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict

from src.element_resolver import norm_key


@dataclass
class ActionHandlers:
    click: Callable[[str, str], str]
    fill_form: Callable[[str], str]
    type_text: Callable[[str, str, str], str]
    scroll: Callable[[str], str]
    hover: Callable[[str], str]
    close_modal: Callable[[str], str]
    select_option: Callable[[str, str], str]
    press_key: Callable[[str], str]
    upload_file: Callable[[str, str], str]


def execute_browser_action(
    page: Any,
    action: Dict[str, Any],
    memory: Any,
    handlers: ActionHandlers,
) -> str:
    act = (action.get("action") or "").lower()
    selector = (action.get("selector") or "").strip()
    value = (action.get("value") or "").strip()
    reason = action.get("reason", "")

    print(f"[Agent] Действие: {act} -> {selector[:60]} | {reason[:60]}")

    if act == "click":
        result = handlers.click(selector, reason)
        if memory and "clicked" in (result or "").lower():
            memory.record_page_element(page.url, f"click:{norm_key(selector)}")
        return result
    if act == "fill_form":
        form_strategy = action.get("_form_strategy", "happy")
        result = handlers.fill_form(form_strategy)
        if memory and "form_filled" in (result or "").lower():
            memory.record_page_element(page.url, "fill_form:all_fields")
        return result
    if act == "type":
        form_strategy = action.get("_form_strategy", "happy")
        result = handlers.type_text(selector, value, form_strategy)
        if memory and "typed" in (result or "").lower():
            memory.record_page_element(page.url, f"type:{norm_key(selector)}")
        return result
    if act == "scroll":
        return handlers.scroll(selector)
    if act == "hover":
        return handlers.hover(selector)
    if act == "explore":
        return handlers.scroll("down")
    if act == "close_modal":
        return handlers.close_modal(selector)
    if act == "select_option":
        return handlers.select_option(selector, value)
    if act == "press_key":
        return handlers.press_key(selector or value or "Escape")
    if act == "upload_file":
        result = handlers.upload_file(selector, value)
        if memory and "uploaded" in (result or "").lower():
            memory.record_page_element(page.url, f"type:{norm_key(selector)}")
        return result
    if act == "check_defect":
        return "defect_found"

    print(f"[Agent] Неизвестное действие: {act}, пробую клик")
    return handlers.click(selector, reason) if selector else "no_action"


__all__ = ["ActionHandlers", "execute_browser_action"]
