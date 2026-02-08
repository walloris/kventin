"""
Умное ожидание загрузки страниц и элементов.
"""
import time
from typing import Optional

from playwright.sync_api import Page


def wait_for_page_ready(
    page: Page,
    *,
    wait_until: str = "domcontentloaded",
    network_idle_timeout: Optional[float] = None,
    timeout: float = 30000,
) -> None:
    """
    Дождаться готовности страницы: DOM и при необходимости сеть.
    wait_until: "domcontentloaded" | "load" | "networkidle"
    network_idle_timeout: при wait_until="networkidle" — мс без сетевой активности (по умолчанию 500).
    """
    try:
        page.wait_for_load_state(wait_until, timeout=timeout)
    except Exception:
        pass
    if wait_until == "networkidle" and network_idle_timeout is not None:
        try:
            page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass
    # Небольшая пауза после load state, чтобы скрипты успели выполниться
    time.sleep(0.3)


def wait_for_network_idle(page: Page, timeout: float = 10000) -> None:
    """Дождаться отсутствия сетевой активности (networkidle)."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    time.sleep(0.2)


def wait_for_selector(
    page: Page,
    selector: str,
    *,
    state: str = "visible",
    timeout: float = 10000,
) -> bool:
    """
    Дождаться появления элемента. state: "attached" | "detached" | "visible" | "hidden".
    Возвращает True, если элемент найден, False при таймауте.
    """
    try:
        page.wait_for_selector(selector, state=state, timeout=timeout)
        return True
    except Exception:
        return False


def wait_for_dom_stable(page: Page, poll_interval: float = 0.2, stable_for_ms: float = 300) -> None:
    """
    Простая проверка стабильности DOM: два снимка с интервалом stable_for_ms,
    если body.innerHTML не изменился — считаем DOM стабильным.
    """
    try:
        prev = page.evaluate("() => document.body ? document.body.innerHTML.length : 0")
        time.sleep(stable_for_ms / 1000.0)
        curr = page.evaluate("() => document.body ? document.body.innerHTML.length : 0")
        if prev != curr:
            time.sleep(poll_interval)
    except Exception:
        pass


def smart_wait_after_goto(page: Page, timeout: float = 30000) -> None:
    """
    Умное ожидание после page.goto: load, затем networkidle (если успеет),
    затем короткая пауза для стабилизации.
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    try:
        page.wait_for_load_state("load", timeout=max(5000, timeout - 5000))
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    time.sleep(0.5)
