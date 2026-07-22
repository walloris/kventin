"""
Фоновый пул задач агента.

Используется для:
- отправки дефектов в Jira (чтобы основной поток Playwright не блокировался);
- фоновой проверки битых ссылок;
- любых I/O-задач, которые не должны замедлять шаги тестирования.

Раньше всё это жило в agent/agent.py. Вынесено сюда, чтобы подключать из любого
модуля без циклических импортов.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, Iterable, Optional, Set

LOG = logging.getLogger("kventin.bg")

_POOL_WORKERS = {
    "default": 2,
    "llm": 2,
    "analysis": 2,
    "jira": 2,
    "io": 2,
}
_bg_pools: Dict[str, ThreadPoolExecutor] = {}
_bg_futures: Dict[str, Set[Future]] = {}
_pool_lock = threading.Lock()


def get_bg_pool(pool_name: str = "default") -> ThreadPoolExecutor:
    """Return a lazy, workload-isolated executor.

    A slow LLM call must not occupy the workers responsible for Jira delivery.
    """
    name = pool_name if pool_name in _POOL_WORKERS else "default"
    with _pool_lock:
        pool = _bg_pools.get(name)
        if pool is None:
            pool = ThreadPoolExecutor(
                max_workers=_POOL_WORKERS[name],
                thread_name_prefix=f"agent-{name}",
            )
            _bg_pools[name] = pool
        return pool


def bg_submit(fn, *args, pool_name: str = "default", **kwargs) -> Future:
    """Отправить задачу в фоновый пул."""
    name = pool_name if pool_name in _POOL_WORKERS else "default"
    future = get_bg_pool(name).submit(fn, *args, **kwargs)
    with _pool_lock:
        _bg_futures.setdefault(name, set()).add(future)

    def forget(done: Future) -> None:
        with _pool_lock:
            _bg_futures.get(name, set()).discard(done)

    future.add_done_callback(forget)
    return future


def cancel_bg_tasks(*, pool_names: Optional[Iterable[str]] = None) -> int:
    """Cancel queued optional work while keeping reusable executors alive."""
    selected = set(pool_names) if pool_names is not None else None
    with _pool_lock:
        futures = [
            future
            for name, tracked in _bg_futures.items()
            if selected is None or name in selected
            for future in list(tracked)
        ]
    return sum(1 for future in futures if future.cancel())


def bg_result(future: Optional[Future], timeout: float = 15.0, default: Any = None) -> Any:
    """Получить результат фоновой задачи (с таймаутом и fallback)."""
    if future is None:
        return default
    try:
        return future.result(timeout=timeout)
    except Exception as e:
        LOG.debug("Background task error: %s", e)
        return default


def shutdown_bg_pool(
    wait: bool = True,
    *,
    pool_names: Optional[Iterable[str]] = None,
    cancel_futures: bool = False,
) -> None:
    """
    Корректно остановить пул в конце сессии.

    ``pool_names`` позволяет отдельно дождаться durable Jira work и отменить
    queued optional analysis/LLM work. При ``None`` останавливаются все пулы.
    """
    selected = set(pool_names) if pool_names is not None else None
    with _pool_lock:
        names = [name for name in _bg_pools if selected is None or name in selected]
        pools = [_bg_pools.pop(name) for name in names]
        for name in names:
            _bg_futures.pop(name, None)
    for pool in pools:
        try:
            pool.shutdown(wait=wait, cancel_futures=cancel_futures)
        except TypeError:
            # Compatibility with older executor implementations.
            pool.shutdown(wait=wait)
        except Exception:
            try:
                pool.shutdown(wait=False)
            except Exception:
                pass


__all__ = [
    "bg_result",
    "bg_submit",
    "cancel_bg_tasks",
    "get_bg_pool",
    "shutdown_bg_pool",
]
