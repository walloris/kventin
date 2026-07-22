"""Action selection guard orchestration.

This small layer keeps the main agent loop from knowing how to combine selected
actions, preflight repair/rejection, fallback actions and decision traces.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, Tuple

from agent.actions.action_preflight import preflight_action
from agent.core.decision_trace import build_decision_trace, summarize_decision_trace
from agent.actions.element_resolver import enrich_action

LOG = logging.getLogger("kventin.selection")


def apply_preflight_or_fallback(
    *,
    page: Any,
    memory: Any,
    action: Dict[str, Any],
    source: str,
    has_overlay: bool,
    decision_candidates: Iterable[Any],
    fallback_factory: Callable[[], Dict[str, Any]],
    expected_ref_meta: Dict[str, str] = None,
    step: int = 0,
) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    expected_ref_meta = expected_ref_meta or {}
    enrich_action(page, memory, action)
    preflight_reason = ""
    preflight = preflight_action(
        page,
        memory,
        action,
        has_overlay=has_overlay,
        expected_ref_meta=expected_ref_meta,
    )
    if preflight.ok:
        action = preflight.action
        if preflight.repaired:
            source = f"{source}/Preflight"
            print(f"[Agent] #{step} Preflight: ref обновлён -> {action.get('selector', '')[:40]}")
    else:
        preflight_reason = preflight.reason
        if preflight.reason == "repeat":
            memory.record_repeat()
        print(f"[Agent] #{step} Preflight: действие отклонено ({preflight.reason}), беру fallback")
        fallback_action = fallback_factory()
        enrich_action(page, memory, fallback_action)
        fallback_preflight = preflight_action(
            page,
            memory,
            fallback_action,
            has_overlay=has_overlay,
            allow_repeat=True,
        )
        if fallback_preflight.ok:
            action = fallback_preflight.action
        else:
            action = (
                {"action": "close_modal", "selector": "", "reason": f"Preflight fallback: {preflight.reason}"}
                if has_overlay
                else {"action": "scroll", "selector": "down", "reason": f"Preflight fallback: {preflight.reason}"}
            )
            enrich_action(page, memory, action)
        source = f"{source}/Preflight"

    decision_trace = build_decision_trace(
        source=source,
        selected_action=action,
        candidates=decision_candidates,
        preflight_reason=preflight_reason,
    )
    trace_summary = summarize_decision_trace(decision_trace)
    if trace_summary:
        LOG.debug("#%s decision: %s", step, trace_summary)
    return action, source, decision_trace


__all__ = ["apply_preflight_or_fallback"]
