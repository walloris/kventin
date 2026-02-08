"""
Видимые действия агента: курсор на странице и подсветка элементов.
Браузер запускается в headed-режиме с slow_mo; перед кликом элемент подсвечивается.
"""
import time
from playwright.sync_api import Page, Locator

from config import HIGHLIGHT_DURATION_MS

# Скрипт для инъекции визуального курсора в страницу
CURSOR_SCRIPT = """
() => {
    if (window.__agentCursor) return;
    const el = document.createElement('div');
    el.id = 'agent-cursor';
    el.style.cssText = `
        position: fixed;
        width: 24px;
        height: 24px;
        border: 3px solid #e74c3c;
        border-radius: 50%;
        background: rgba(231, 76, 60, 0.3);
        pointer-events: none;
        z-index: 2147483647;
        left: -100px;
        top: -100px;
        transition: left 0.05s, top 0.05s;
        box-shadow: 0 0 10px rgba(231,76,60,0.6);
    `;
    document.body.appendChild(el);
    window.__agentCursor = el;
}
"""


def inject_cursor(page: Page) -> None:
    """Добавить визуальный курсор на страницу (вызывать после загрузки)."""
    try:
        page.evaluate(CURSOR_SCRIPT)
    except Exception:
        pass


def move_cursor_to(page: Page, x: float, y: float) -> None:
    """Обновить позицию визуального курсора на странице."""
    try:
        page.evaluate(
            """([x, y]) => {
                if (window.__agentCursor) {
                    window.__agentCursor.style.left = (x - 12) + 'px';
                    window.__agentCursor.style.top = (y - 12) + 'px';
                }
            }""",
            [x, y],
        )
    except Exception:
        pass


def highlight_and_click(locator: Locator, page: Page, description: str = "") -> None:
    """
    Подсветить элемент, подождать HIGHLIGHT_DURATION_MS, затем кликнуть.
    Курсор перемещается к центру элемента перед кликом.
    """
    try:
        locator.scroll_into_view_if_needed()
        locator.highlight()
        # Подвинуть курсор к центру элемента
        box = locator.bounding_box()
        if box:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            move_cursor_to(page, cx, cy)
        time.sleep(HIGHLIGHT_DURATION_MS / 1000.0)
        locator.click()
    except Exception as e:
        raise e
    finally:
        try:
            # Убрать подсветку после клика
            page.evaluate("() => { document.querySelectorAll('[data-playwright-highlight]').forEach(e => e.remove()); }")
        except Exception:
            pass


def safe_highlight(locator: Locator, page: Page, duration_sec: float = None) -> None:
    """Только подсветить элемент на заданное время (для наведения без клика)."""
    duration_sec = duration_sec or (HIGHLIGHT_DURATION_MS / 1000.0)
    try:
        locator.scroll_into_view_if_needed()
        locator.highlight()
        box = locator.bounding_box()
        if box:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            move_cursor_to(page, cx, cy)
        time.sleep(duration_sec)
    finally:
        try:
            page.evaluate("() => { document.querySelectorAll('[data-playwright-highlight]').forEach(e => e.remove()); }")
        except Exception:
            pass
