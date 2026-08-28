"""Process-level supervision for long-running browser sessions."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

LOG = logging.getLogger("kventin.supervisor")


@dataclass(frozen=True)
class SupervisorConfig:
    continuous: bool = True
    bounded_run: bool = False
    base_delay: float = 2.0
    max_delay: float = 60.0


class AgentSupervisor:
    """Restart recoverable sessions with bounded exponential backoff."""

    _FINAL_REASONS = {"interrupted", "max_steps", "loop_guard", "completed"}

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.sleep_fn = sleep_fn

    def run(
        self,
        run_session: Callable[..., Optional[Dict[str, Any]]],
        *,
        start_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        total_defects = 0
        total_steps = 0
        restart_count = 0
        failure_streak = 0

        while True:
            try:
                result = run_session(start_url=start_url) or {}
            except KeyboardInterrupt:
                return {
                    "defects": total_defects,
                    "steps": total_steps,
                    "error": None,
                    "termination": "interrupted",
                    "restarts": restart_count,
                }
            except Exception as exc:  # noqa: BLE001
                LOG.exception("Agent session crashed: %s", exc)
                result = {
                    "defects": 0,
                    "steps": 0,
                    "error": str(exc)[:500],
                    "termination": "session_crash",
                }

            current_session_steps = int(result.get("steps") or 0)
            total_defects += int(result.get("defects") or 0)
            total_steps += current_session_steps
            result["defects"] = total_defects
            result["steps"] = total_steps
            result["restarts"] = restart_count

            reason = str(result.get("termination") or "")
            if (
                not self.config.continuous
                or self.config.bounded_run
                or reason in self._FINAL_REASONS
            ):
                return result

            if current_session_steps > 0:
                failure_streak = 0
            delay = min(
                max(0.0, self.config.base_delay) * (2 ** min(failure_streak, 16)),
                max(0.0, self.config.max_delay),
            )
            restart_count += 1
            failure_streak += 1
            LOG.warning(
                "Agent session ended (%s); restart %d in %.1fs",
                reason or "unknown",
                restart_count,
                delay,
            )
            try:
                self.sleep_fn(delay)
            except KeyboardInterrupt:
                result["termination"] = "interrupted"
                result["restarts"] = restart_count
                return result


__all__ = ["AgentSupervisor", "SupervisorConfig"]
