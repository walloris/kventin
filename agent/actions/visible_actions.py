"""
Действия агента на странице.

Раньше здесь жил визуальный «демо»-слой: курсор, overlay чата с LLM, лейблы
«КЛИКАЮ СЮДА», pulse-эффекты и ripple. Всё это убрано — для качества тестов
важна стабильность и скорость, а не подсветка.

Все публичные функции оставлены, чтобы не ломать места вызова в agent.py,
но превращены в no-op (или минимальный технический функционал: скролл к
элементу перед действием, обычный Playwright-клик).
"""
import time
from typing import Optional

from playwright.sync_api import Page, Locator


# ============================================================
# No-op функции совместимого API
# ============================================================

def inject_cursor(page: Page) -> None:
    """No-op (раньше показывал визуальный курсор)."""
    return


def inject_llm_overlay(page: Page) -> None:
    """No-op (раньше показывал overlay чата с LLM)."""
    return


def inject_demo_banner(page: Page) -> None:
    """No-op (раньше показывал «AI-тестировщик» баннер)."""
    return


def show_click_ripple(page: Page, x: float, y: float) -> None:
    """No-op (раньше показывал ripple после клика)."""
    return


def show_highlight_label(page: Page, x: float, y: float, text: str = "") -> None:
    """No-op (раньше показывал label «Кликаю сюда»)."""
    return


def move_cursor_to(page: Page, x: float, y: float) -> None:
    """No-op (раньше двигал визуальный курсор)."""
    return


def update_demo_banner(page: Page, step_text: str = "", progress_pct: float = 0) -> None:
    """No-op."""
    return


def update_llm_overlay(
    page: Page,
    prompt: Optional[str] = None,
    response: Optional[str] = None,
    loading: bool = False,
    error: Optional[str] = None,
) -> None:
    """No-op."""
    return


# ============================================================
# Реальные технические функции (нужны для корректной работы агента)
# ============================================================

def scroll_to_center(locator: Locator, page: Page) -> None:
    """Прокрутить страницу так, чтобы элемент оказался в зоне видимости."""
    try:
        locator.scroll_into_view_if_needed()
    except Exception:
        try:
            locator.evaluate("el => el.scrollIntoView({ block: 'center', inline: 'nearest' })")
        except Exception:
            pass


def safe_highlight(locator: Locator, page: Page, duration_sec: float = None) -> None:
    """
    Раньше — визуальная подсветка элемента. Сейчас просто скролл в зону видимости,
    без задержек (визуал убран).
    """
    scroll_to_center(locator, page)


def highlight_and_click(locator: Locator, page: Page, description: str = "") -> None:
    """Скролл к элементу + обычный клик. Без визуальных эффектов."""
    scroll_to_center(locator, page)
    locator.click()
