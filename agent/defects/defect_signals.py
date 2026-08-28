"""Defect signal aggregation.

Rules, visual/oracle hints and execution failures are converted into a common
DefectSignal stream. The agent can then pick the highest-confidence defect text
without duplicating decision logic across sync and background analysis paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from agent.defects.defect_rules import rule_4xx_on_main, rule_5xx, rule_action_failure, rule_pageerror


SEVERITY_RANK = {"critical": 4, "major": 3, "minor": 2, "trivial": 1}


@dataclass
class DefectSignal:
    kind: str
    title: str
    details: str = ""
    severity: str = "major"
    confidence: float = 0.5
    source: str = "rule"

    def to_bug_text(self) -> str:
        head = self.title.strip()
        body = self.details.strip()
        prefix = f"[{self.kind}] " if self.kind else ""
        return f"{prefix}{head}\n\n{body}" if body else f"{prefix}{head}"


def _from_rule(kind: str, rule_result: Optional[Dict[str, Any]], confidence: float) -> Optional[DefectSignal]:
    if not rule_result:
        return None
    return DefectSignal(
        kind=kind,
        title=str(rule_result.get("title") or kind),
        details=str(rule_result.get("details") or ""),
        severity=str(rule_result.get("severity") or "major"),
        confidence=confidence,
    )


def collect_rule_signals(
    *,
    action: Optional[Dict[str, Any]],
    result: str,
    current_url: str,
    new_console: List[Dict[str, Any]],
    new_network: List[Dict[str, Any]],
) -> List[DefectSignal]:
    signals: List[DefectSignal] = []
    for sig in (
        _from_rule("network_5xx", rule_5xx(new_network), 0.98),
        _from_rule("action_failure", rule_action_failure(action, result, current_url), 0.92),
        _from_rule("pageerror", rule_pageerror(new_console), 0.9),
        _from_rule("network_4xx", rule_4xx_on_main(new_network, current_url), 0.82),
    ):
        if sig:
            signals.append(sig)
    return signals


def add_oracle_signal(
    signals: List[DefectSignal],
    *,
    possible_bug: str = "",
    oracle_error: bool = False,
    console_brief: str = "",
) -> None:
    text = (possible_bug or "").strip()
    if text:
        details = f"Новые ошибки консоли после действия:\n{console_brief}" if console_brief else ""
        signals.append(
            DefectSignal(
                kind="oracle_possible_bug",
                title=text[:240],
                details=details,
                severity="major",
                confidence=0.72,
                source="oracle",
            )
        )
    elif oracle_error:
        signals.append(
            DefectSignal(
                kind="oracle_error",
                title="LLM-оракул классифицировал результат действия как ошибку",
                details=console_brief,
                severity="major",
                confidence=0.62,
                source="oracle",
            )
        )


def pick_best_signal(signals: Iterable[DefectSignal]) -> Optional[DefectSignal]:
    items = list(signals)
    if not items:
        return None
    return max(
        items,
        key=lambda s: (
            s.confidence,
            SEVERITY_RANK.get((s.severity or "").lower(), 0),
            len(s.details or ""),
        ),
    )


__all__ = [
    "DefectSignal",
    "add_oracle_signal",
    "collect_rule_signals",
    "pick_best_signal",
]
