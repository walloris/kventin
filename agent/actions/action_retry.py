"""Safety gate for retrying an action against a changed DOM."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from agent.actions.action_preflight import preflight_action
from agent.browser.overlay_state import inspect_overlays


@dataclass(frozen=True)
class RetryDecision:
    allowed: bool
    action: Dict[str, Any]
    reason: str = ""


def prepare_action_retry(page: Any, memory: Any, action: Dict[str, Any]) -> RetryDecision:
    """Revalidate a retry in the current DOM and active overlay scope."""
    overlay = inspect_overlays(page)
    has_overlay = bool(overlay.get("has_overlay"))
    action_type = str((action or {}).get("action") or "").strip().lower()
    targeted_actions = {"click", "type", "hover", "select_option", "upload_file"}
    if has_overlay and action_type not in targeted_actions | {"close_modal", "press_key"}:
        return RetryDecision(False, dict(action), "overlay_changed")

    checked = preflight_action(
        page,
        memory,
        dict(action),
        has_overlay=has_overlay,
        allow_repeat=True,
    )
    if not checked.ok:
        return RetryDecision(False, checked.action, checked.reason)
    return RetryDecision(True, checked.action, checked.reason)


__all__ = ["RetryDecision", "prepare_action_retry"]
