"""Canonical interpretation of legacy string action results."""
from __future__ import annotations

from enum import Enum


class ActionStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    NOOP = "noop"


_FAILURE_MARKERS = (
    "_error",
    "error:",
    "not_found",
    "not found",
    "modal_close_failed",
    "no_selector",
    "no_selector_or_value",
    "input_not_found",
    "select_not_found",
    "no_form_fields",
    "value_mismatch",
    "form_fill_failed",
    "no_action",
    "unsupported_action",
    "preflight_rejected",
)
_NOOP_MARKERS = (
    "skipped_",
    "already_closed",
    "paused_by_overlay",
)


def classify_action_result(result: object) -> ActionStatus:
    text = str(result or "").strip().lower()
    if not text:
        return ActionStatus.FAILURE
    if any(marker in text for marker in _FAILURE_MARKERS):
        return ActionStatus.FAILURE
    if any(marker in text for marker in _NOOP_MARKERS):
        return ActionStatus.NOOP
    return ActionStatus.SUCCESS


def action_failed(result: object) -> bool:
    return classify_action_result(result) is ActionStatus.FAILURE


__all__ = ["ActionStatus", "action_failed", "classify_action_result"]
