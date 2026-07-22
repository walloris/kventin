"""Scheduling boundary for browser quality checks."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List

from config import (
    A11Y_CHECK_EVERY_N,
    ENABLE_IFRAME_TESTING,
    ENABLE_RESPONSIVE_TEST,
    IFRAME_CHECK_EVERY_N,
    PERF_CHECK_EVERY_N,
    RESPONSIVE_CHECK_EVERY_N,
)
from agent.checks.agent_checks import (
    run_a11y_check,
    run_iframe_check,
    run_perf_check,
    run_responsive_check,
)

LOG = logging.getLogger("kventin.check-scheduler")


@dataclass(frozen=True)
class CheckSchedule:
    a11y_every: int = A11Y_CHECK_EVERY_N
    performance_every: int = PERF_CHECK_EVERY_N
    iframe_every: int = IFRAME_CHECK_EVERY_N
    responsive_every: int = RESPONSIVE_CHECK_EVERY_N
    enable_iframe: bool = ENABLE_IFRAME_TESTING
    enable_responsive: bool = ENABLE_RESPONSIVE_TEST

    def due(self, step: int, *, has_overlay: bool = False) -> List[str]:
        if step <= 0 or has_overlay:
            return []
        checks = []
        if self.a11y_every > 0 and step % self.a11y_every == 0:
            checks.append("a11y")
        if self.performance_every > 0 and step % self.performance_every == 0:
            checks.append("performance")
        if self.enable_iframe and self.iframe_every > 0 and step % self.iframe_every == 0:
            checks.append("iframe")
        if self.enable_responsive and self.responsive_every > 0 and step % self.responsive_every == 0:
            checks.append("responsive")
        return checks


def run_periodic_checks(
    page: Any,
    memory: Any,
    *,
    step: int,
    current_url: str,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
    has_overlay: bool,
    schedule: CheckSchedule = CheckSchedule(),
) -> List[str]:
    """Run due checks in the Playwright thread and isolate each failure."""
    completed = []
    runners = {
        "a11y": run_a11y_check,
        "performance": run_perf_check,
        "iframe": run_iframe_check,
        "responsive": run_responsive_check,
    }
    for check_name in schedule.due(step, has_overlay=has_overlay):
        try:
            runners[check_name](
                page,
                memory,
                current_url,
                console_log,
                network_failures,
            )
            completed.append(check_name)
        except Exception:
            LOG.exception("Periodic check failed: %s", check_name)
    return completed


__all__ = ["CheckSchedule", "run_periodic_checks"]
