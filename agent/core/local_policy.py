"""Deterministic action selection used while the LLM is slow or unavailable."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from agent.actions.action_candidates import collect_action_candidates
from agent.actions.action_policy import choose_best_candidate, rank_candidates
from agent.browser.overlay_state import inspect_overlays


def choose_local_action(
    page: Any,
    memory: Any,
    *,
    has_overlay: bool = False,
    overlay_info: Optional[Dict[str, Any]] = None,
    upload_file_path: str = "",
) -> Dict[str, Any]:
    """Choose one valid action from the current, strictly scoped DOM state."""
    try:
        if page is None or page.is_closed():
            return {"action": "explore", "reason": "Browser page is unavailable"}
    except Exception:
        return {"action": "explore", "reason": "Browser page is unavailable"}

    scope = overlay_info
    if has_overlay and not scope:
        scope = inspect_overlays(page)

    candidates = collect_action_candidates(
        page,
        memory,
        has_overlay=has_overlay,
        overlay_info=scope,
        max_candidates=60,
    )
    can_upload = bool(upload_file_path and os.path.isfile(upload_file_path))
    if not can_upload:
        candidates = [candidate for candidate in candidates if candidate.action != "upload_file"]
    else:
        for candidate in candidates:
            if candidate.action == "upload_file":
                candidate.value = upload_file_path

    ranked = rank_candidates(candidates, memory)
    try:
        memory._last_action_candidates = ranked
    except Exception:
        pass
    best = choose_best_candidate(ranked, memory)
    if best is not None:
        action = best.as_action()
        action["_candidate_id"] = best.id
        action["_candidate_score"] = best.score
        return action

    if has_overlay:
        return {
            "action": "close_modal",
            "selector": "",
            "reason": "No untested controls remain in the active overlay",
            "test_goal": "Close the active overlay after covering its controls",
            "expected_outcome": "The overlay disappears and the underlying page becomes interactive",
        }

    direction = "up" if memory is not None and memory.should_avoid_scroll() else "down"
    return {
        "action": "scroll",
        "selector": direction,
        "reason": "Search for new visible controls",
        "test_goal": "Expand page coverage",
        "expected_outcome": "New actionable controls become visible",
    }


__all__ = ["choose_local_action"]
