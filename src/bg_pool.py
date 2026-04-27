"""
Фоновый пул задач агента.

Используется для:
- отправки дефектов в Jira (чтобы основной поток Playwright не блокировался);
- фоновой проверки битых ссылок;
- любых I/O-задач, которые не должны замедлять шаги тестирования.

Раньше всё это жило в src/agent.py. Вынесено сюда, чтобы подключать из любого
модуля без циклических импортов.
"""
from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Optional

LOG = logging.getLogger("kventin.bg")

_bg_pool: Optional[ThreadPoolExecutor] = None


def get_bg_pool() -> ThreadPoolExecutor:
    """Ленивая инициализация фонового пула."""
    global _bg_pool
    if _bg_pool is None:
        _bg_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="agent-bg")
    return _bg_pool


def bg_submit(fn, *args, **kwargs) -> Future:
    """Отправить задачу в фоновый пул."""
    return get_bg_pool().submit(fn, *args, **kwargs)


def bg_result(future: Optional[Future], timeout: float = 15.0, default: Any = None) -> Any:
    """Получить результат фоновой задачи (с таймаутом и fallback)."""
    if future is None:
        return default
    try:
        return future.result(timeout=timeout)
    except Exception as e:
        LOG.debug("Background task error: %s", e)
        return default


def shutdown_bg_pool(wait: bool = True) -> None:
    """
    Корректно остановить пул в конце сессии.

    wait=True — обязательно для финального закрытия (иначе фоновые отправки в
    Jira могут не успеть завершиться). wait=False — для аварийных сценариев.
    """
    global _bg_pool
    if _bg_pool is None:
        return
    try:
        _bg_pool.shutdown(wait=wait)
    except Exception:
        try:
            _bg_pool.shutdown(wait=False)
        except Exception:
            pass
    finally:
        _bg_pool = None


__all__ = ["get_bg_pool", "bg_submit", "bg_result", "shutdown_bg_pool"]
