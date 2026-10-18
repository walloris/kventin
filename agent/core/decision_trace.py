"""Decision trace helpers for agent observability."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

from agent.actions.action_candidates import ActionCandidate


def build_decision_trace(
    *,
    source: str,
    selected_action: Dict[str, Any],
    candidates: Iterable[ActionCandidate] = (),
    preflight_reason: str = "",
) -> Dict[str, Any]:
    candidate_list = list(candidates)
    top: List[Dict[str, Any]] = []
    for cand in candidate_list[:8]:
        top.append(
            {
                "id": cand.id,
                "action": cand.action,
                "selector": cand.selector,
                "label": cand.label[:100],
                "score": round(cand.score, 2),
                "stable_key": cand.stable_key[:100],
            }
        )
    return {
        "source": source,
        "selected_action": (selected_action.get("action") or "")[:40],
        "selected_selector": (selected_action.get("selector") or "")[:80],
        "selected_candidate_id": selected_action.get("_candidate_id", ""),
        "preflight": preflight_reason,
        "candidate_count": len(candidate_list),
        "top_candidates": top,
    }


def summarize_decision_trace(trace: Dict[str, Any]) -> str:
    if not trace:
        return ""
    selected = trace.get("selected_candidate_id") or trace.get("selected_selector") or trace.get("selected_action")
    pre = f", preflight={trace.get('preflight')}" if trace.get("preflight") else ""
    return f"{trace.get('source', '?')} selected={selected} candidates={trace.get('candidate_count', 0)}{pre}"


__all__ = ["build_decision_trace", "summarize_decision_trace"]
