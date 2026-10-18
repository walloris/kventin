"""Observation snapshot for the browser-testing agent.

This module owns the "what do we see right now?" part of the loop. It gathers
Playwright-derived page facts in the main thread and returns a plain dataclass
that planning/LLM code can consume without touching the browser again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent.actions.action_candidates import ActionCandidate, collect_action_candidates, render_candidates_for_prompt
from agent.actions.action_policy import rank_candidates
from agent.core.checklist import checklist_results_to_context
from agent.browser.page_analyzer import (
    build_context,
    detect_active_overlays,
    detect_page_type,
    format_overlays_context,
    get_dom_summary,
)


@dataclass
class PageObservation:
    current_url: str = ""
    has_overlay: bool = False
    overlay_info: Dict[str, Any] = field(default_factory=dict)
    overlay_context: str = ""
    screenshot_b64: Optional[str] = None
    screenshot_changed: bool = False
    dom_summary: str = ""
    ref_meta: Dict[str, str] = field(default_factory=dict)
    candidates: List[ActionCandidate] = field(default_factory=list)
    candidates_prompt: str = ""
    history_text: str = ""
    page_type: str = "unknown"
    context: str = ""


def collect_page_observation(
    page: Any,
    memory: Any,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
    checklist_results: List[Dict[str, Any]],
    *,
    screenshot_func: Callable[[Any], Optional[str]],
    include_shadow_dom: bool = True,
    dom_max: int = 5000,
    history_n: int = 15,
    max_candidates: int = 60,
    prompt_candidate_limit: int = 18,
) -> PageObservation:
    overlay_info = detect_active_overlays(page)
    has_overlay = bool(overlay_info.get("has_overlay", False))
    screenshot_b64 = screenshot_func(page)
    screenshot_changed = memory.is_screenshot_changed(screenshot_b64 or "") if memory else False
    current_url = page.url
    dom_summary = get_dom_summary(page, max_length=dom_max, include_shadow_dom=include_shadow_dom)
    try:
        ref_meta = page.evaluate("() => Object.assign({}, window.__agentRefMeta || {})") or {}
    except Exception:
        ref_meta = {}

    candidates = rank_candidates(
        collect_action_candidates(
            page,
            memory,
            has_overlay=has_overlay,
            overlay_info=overlay_info,
            max_candidates=max_candidates,
        ),
        memory,
    )
    candidates_prompt = render_candidates_for_prompt(candidates, limit=prompt_candidate_limit)
    if memory and getattr(memory, "page_objects", None):
        try:
            page_state = memory.page_objects.update_from_observation(
                page,
                dom_summary=dom_summary,
                overlay_info=overlay_info,
                candidates=candidates,
                ref_meta={str(k): str(v) for k, v in ref_meta.items()},
            )
            if page_state.must_close:
                memory._last_page_object_state = page_state.state_key
        except Exception:
            pass
    history_text = memory.get_history_text(last_n=history_n) if memory else ""
    overlay_context = format_overlays_context(overlay_info)
    page_type = detect_page_type(page)

    context = build_context(
        page,
        current_url,
        console_log,
        network_failures,
        dom_summary=dom_summary,
    )
    if checklist_results:
        context = checklist_results_to_context(checklist_results) + "\n\n" + context
    if overlay_context:
        context = overlay_context + "\n\n" + context

    return PageObservation(
        current_url=current_url,
        has_overlay=has_overlay,
        overlay_info=overlay_info,
        overlay_context=overlay_context,
        screenshot_b64=screenshot_b64,
        screenshot_changed=screenshot_changed,
        dom_summary=dom_summary,
        ref_meta={str(k): str(v) for k, v in ref_meta.items()},
        candidates=candidates,
        candidates_prompt=candidates_prompt,
        history_text=history_text,
        page_type=page_type,
        context=context,
    )


__all__ = ["PageObservation", "collect_page_observation"]
