"""Post-action oracle context builder."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def should_run_oracle(
    *,
    enabled: bool,
    action_type: str,
    has_screenshot: bool,
    visual_diff: Dict[str, Any],
    new_errors: List[Dict[str, Any]],
    new_network: List[Dict[str, Any]],
    lazy_on_visual_or_error: bool = True,
) -> bool:
    if not enabled or action_type not in ("type", "click", "select_option") or not has_screenshot:
        return False
    if any((n.get("status") or 0) >= 500 for n in new_network):
        return False
    if not lazy_on_visual_or_error:
        return True
    return bool(visual_diff.get("changed") or new_errors or new_network)


def build_oracle_context(
    *,
    action: Dict[str, Any],
    result: str,
    expected_outcome: str = "",
    visual_diff: Optional[Dict[str, Any]] = None,
    new_errors: Optional[List[Dict[str, Any]]] = None,
    new_network: Optional[List[Dict[str, Any]]] = None,
) -> str:
    visual_diff = visual_diff or {}
    new_errors = new_errors or []
    new_network = new_network or []

    console_lines = []
    for err in new_errors[-5:]:
        console_lines.append(f"- [{err.get('type', 'error')}] {(err.get('text') or '')[:180]}")
    network_lines = []
    for req in new_network[-5:]:
        status = req.get("status")
        if status and status >= 400:
            network_lines.append(f"- {status} {req.get('method', 'GET')} {(req.get('url') or '')[:160]}")

    return "\n".join(
        [
            f"Действие: {(action.get('action') or '')} -> {(action.get('selector') or '')[:80]}",
            f"Результат выполнения: {(result or '')[:300]}",
            f"Ожидалось: {expected_outcome[:300] if expected_outcome else 'успешное выполнение без ошибок приложения'}",
            f"Visual diff: changed={bool(visual_diff.get('changed'))}, change={float(visual_diff.get('change_percent') or 0):.1f}%, detail={(visual_diff.get('detail') or '')[:200]}",
            "Новые ошибки консоли:\n" + ("\n".join(console_lines) if console_lines else "- нет"),
            "Новые сетевые ошибки:\n" + ("\n".join(network_lines) if network_lines else "- нет"),
            "Классифицируй только пользовательски значимые проблемы приложения. Флаки и проблемы среды не считай багом.",
        ]
    )


__all__ = ["build_oracle_context", "should_run_oracle"]
