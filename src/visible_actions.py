"""
Видимые действия агента: курсор на странице, подсветка элементов, оверлей диалога с LLM.
"""
import time
from typing import Optional
from playwright.sync_api import Page, Locator

from config import HIGHLIGHT_DURATION_MS


def _escape_html(s: str, max_len: int = 3000) -> str:
    if not s:
        return ""
    s = s[:max_len]
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("\n", "<br>\n")


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


# --- Окно диалога с LLM поверх страницы (для демо) ---

LLM_OVERLAY_SCRIPT = """
() => {
    if (window.__agentLLMOverlay) return;
    const wrap = document.createElement('div');
    wrap.id = 'agent-llm-overlay';
    wrap.style.cssText = `
        position: fixed;
        top: 12px;
        right: 12px;
        width: 380px;
        max-height: 85vh;
        z-index: 2147483646;
        font-family: system-ui, -apple-system, sans-serif;
        font-size: 13px;
        background: linear-gradient(145deg, #1a1d23 0%, #252a33 100%);
        color: #e6e8eb;
        border: 1px solid #3d434d;
        border-radius: 12px;
        box-shadow: 0 8px 32px rgba(0,0,0,0.5);
        overflow: hidden;
        display: flex;
        flex-direction: column;
        pointer-events: none;
    `;
    const title = document.createElement('div');
    title.id = 'agent-llm-title';
    title.textContent = 'Диалог с LLM (GigaChat)';
    title.style.cssText = `
        padding: 10px 14px;
        background: #2d323d;
        font-weight: 600;
        border-bottom: 1px solid #3d434d;
    `;
    const status = document.createElement('div');
    status.id = 'agent-llm-status';
    status.textContent = 'Ожидание...';
    status.style.cssText = `
        padding: 6px 14px;
        background: #252a33;
        color: #8b949e;
        font-size: 12px;
        border-bottom: 1px solid #3d434d;
    `;
    const body = document.createElement('div');
    body.id = 'agent-llm-body';
    body.style.cssText = `
        padding: 12px 14px;
        overflow-y: auto;
        flex: 1;
        min-height: 120px;
        max-height: 60vh;
    `;
    body.innerHTML = '<div id="agent-llm-prompt" style="margin-bottom:12px;"></div><div id="agent-llm-response" style="white-space:pre-wrap;word-break:break-word;"></div>';
    wrap.appendChild(title);
    wrap.appendChild(status);
    wrap.appendChild(body);
    document.body.appendChild(wrap);
    window.__agentLLMOverlay = { wrap, title, status, body };
}
"""


def inject_llm_overlay(page: Page) -> None:
    """Показать окно диалога с LLM поверх страницы (для демо)."""
    try:
        page.evaluate(LLM_OVERLAY_SCRIPT)
    except Exception:
        pass


def update_llm_overlay(
    page: Page,
    prompt: Optional[str] = None,
    response: Optional[str] = None,
    loading: bool = False,
    error: Optional[str] = None,
) -> None:
    """Обновить содержимое оверлея: запрос, ответ, статус загрузки или ошибка."""
    try:
        status = "Запрос к GigaChat..." if loading else ("Ошибка" if error else "Ответ получен")
        prompt_esc = _escape_html(prompt or "", 2500)
        response_esc = _escape_html(response or "", 2500)
        error_esc = _escape_html(error or "", 500)
        page.evaluate(
            """(args) => {
                if (!window.__agentLLMOverlay) return;
                const s = window.__agentLLMOverlay.status;
                const b = window.__agentLLMOverlay.body;
                if (s) s.textContent = args.status;
                const pr = document.getElementById('agent-llm-prompt');
                const r = document.getElementById('agent-llm-response');
                if (pr) pr.innerHTML = '<strong>Запрос:</strong><br>' + (args.prompt_esc || '') + (args.prompt_esc ? '<br><br>' : '');
                if (r) r.innerHTML = '<strong>Ответ:</strong><br>' + (args.error_esc || args.response_esc || '—');
            }""",
            {
                "status": status,
                "prompt_esc": prompt_esc,
                "response_esc": response_esc,
                "error_esc": error_esc,
            },
        )
    except Exception:
        pass
