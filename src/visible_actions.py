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


# --- Стили для "вау"-эффектов демо ---
DEMO_STYLES = """
(function() {
    if (document.getElementById('agent-demo-styles')) return;
    const style = document.createElement('style');
    style.id = 'agent-demo-styles';
    style.textContent = `
        @keyframes agent-cursor-pulse {
            0%, 100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(231, 76, 60, 0.7), 0 0 20px 4px rgba(231,76,60,0.4); }
            50% { transform: scale(1.15); box-shadow: 0 0 0 8px rgba(231, 76, 60, 0), 0 0 30px 8px rgba(231,76,60,0.6); }
        }
        @keyframes agent-ripple {
            0% { transform: scale(0); opacity: 1; border-width: 4px; }
            100% { transform: scale(4); opacity: 0; border-width: 1px; }
        }
        @keyframes agent-border-glow {
            0%, 100% { border-color: #3d434d; box-shadow: 0 0 20px rgba(0,0,0,0.5), inset 0 0 60px rgba(99,102,241,0.03); }
            50% { border-color: #6366f1; box-shadow: 0 0 30px rgba(99,102,241,0.3), inset 0 0 80px rgba(99,102,241,0.05); }
        }
        @keyframes agent-thinking {
            0%, 80%, 100% { opacity: 0.3; transform: scale(0.8); }
            40% { opacity: 1; transform: scale(1.2); }
        }
        @keyframes agent-banner-shine {
            0% { background-position: -200% 0; }
            100% { background-position: 200% 0; }
        }
        @keyframes agent-label-pop {
            0% { opacity: 0; transform: translateY(8px) scale(0.9); }
            100% { opacity: 1; transform: translateY(0) scale(1); }
        }
        .agent-thinking-dots span { animation: agent-thinking 1.4s ease-in-out infinite; }
        .agent-thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
        .agent-thinking-dots span:nth-child(3) { animation-delay: 0.4s; }
    `;
    document.head.appendChild(style);
})();
"""

# Курсор с пульсирующим свечением
CURSOR_SCRIPT = """
() => {
    if (window.__agentCursor) return;
    const el = document.createElement('div');
    el.id = 'agent-cursor';
    el.style.cssText = `
        position: fixed;
        width: 28px;
        height: 28px;
        border: 3px solid #e74c3c;
        border-radius: 50%;
        background: radial-gradient(circle, rgba(231,76,60,0.5) 0%, rgba(231,76,60,0.1) 70%);
        pointer-events: none;
        z-index: 2147483647;
        left: -100px;
        top: -100px;
        transition: left 0.08s ease-out, top 0.08s ease-out;
        box-shadow: 0 0 20px 4px rgba(231,76,60,0.5);
        animation: agent-cursor-pulse 1.5s ease-in-out infinite;
    `;
    document.body.appendChild(el);
    window.__agentCursor = el;
}
"""


def inject_cursor(page: Page) -> None:
    """Добавить визуальный курсор на страницу (вызывать после загрузки)."""
    try:
        page.evaluate(DEMO_STYLES)
        page.evaluate(CURSOR_SCRIPT)
    except Exception:
        pass


def show_click_ripple(page: Page, x: float, y: float) -> None:
    """Эффект «рябь» в точке клика для демо."""
    try:
        page.evaluate(
            """([x, y]) => {
                if (!document.getElementById('agent-demo-styles')) return;
                const r = document.createElement('div');
                r.style.cssText = `
                    position: fixed; left: ${x}px; top: ${y}px; width: 20px; height: 20px;
                    margin-left: -10px; margin-top: -10px; border: 4px solid rgba(231,76,60,0.8);
                    border-radius: 50%; pointer-events: none; z-index: 2147483645;
                    animation: agent-ripple 0.6s ease-out forwards;
                `;
                document.body.appendChild(r);
                setTimeout(() => r.remove(), 650);
            }""",
            [x, y],
        )
    except Exception:
        pass


def show_highlight_label(page: Page, x: float, y: float, text: str = "Кликаю сюда") -> None:
    """Всплывающая подсказка над элементом при подсветке."""
    try:
        page.evaluate(
            """([x, y, text]) => {
                if (window.__agentLabel) window.__agentLabel.remove();
                if (!document.getElementById('agent-demo-styles')) return;
                const el = document.createElement('div');
                el.className = 'agent-highlight-label';
                window.__agentLabel = el;
                el.textContent = text;
                el.style.cssText = `
                    position: fixed; left: ${x}px; top: ${y - 36}px; transform: translateX(-50%);
                    padding: 6px 12px; background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%);
                    color: white; font: 600 13px system-ui; border-radius: 8px; white-space: nowrap;
                    box-shadow: 0 4px 14px rgba(231,76,60,0.5); pointer-events: none;
                    z-index: 2147483644; animation: agent-label-pop 0.25s ease-out;
                `;
                document.body.appendChild(el);
                setTimeout(() => { if (el.parentNode) el.remove(); window.__agentLabel = null; }, 2500);
            }""",
            [x, y, text],
        )
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
    Подсветить элемент, подсказка «Кликаю сюда», курсор, клик и эффект ряби.
    """
    try:
        locator.scroll_into_view_if_needed()
        locator.highlight()
        box = locator.bounding_box()
        cx, cy = None, None
        if box:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            move_cursor_to(page, cx, cy)
            show_highlight_label(page, cx, cy, description or "Кликаю сюда")
        time.sleep(HIGHLIGHT_DURATION_MS / 1000.0)
        locator.click()
        if cx is not None and cy is not None:
            show_click_ripple(page, cx, cy)
    except Exception as e:
        raise e
    finally:
        try:
            page.evaluate("() => { document.querySelectorAll('[data-playwright-highlight]').forEach(e => e.remove()); }")
        except Exception:
            pass


def safe_highlight(locator: Locator, page: Page, duration_sec: float = None) -> None:
    """Подсветить элемент и показать подсказку «Проверяю» на заданное время."""
    duration_sec = duration_sec or (HIGHLIGHT_DURATION_MS / 1000.0)
    try:
        locator.scroll_into_view_if_needed()
        locator.highlight()
        box = locator.bounding_box()
        if box:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            move_cursor_to(page, cx, cy)
            show_highlight_label(page, cx, cy, "Проверяю")
        time.sleep(duration_sec)
    finally:
        try:
            page.evaluate("() => { document.querySelectorAll('[data-playwright-highlight]').forEach(e => e.remove()); }")
        except Exception:
            pass


# --- Демо-баннер сверху страницы ---
DEMO_BANNER_SCRIPT = """
() => {
    if (window.__agentBanner) return;
    const wrap = document.createElement('div');
    wrap.id = 'agent-demo-banner';
    wrap.style.cssText = `
        position: fixed; top: 0; left: 0; right: 0; z-index: 2147483643;
        height: 44px; display: flex; align-items: center; padding: 0 20px;
        background: linear-gradient(90deg, #0f0f12 0%, #1a1b23 50%, #16171d 100%);
        background-size: 200% 100%; animation: agent-banner-shine 8s ease infinite;
        border-bottom: 2px solid rgba(99,102,241,0.4);
        box-shadow: 0 4px 24px rgba(0,0,0,0.4);
        font-family: system-ui, sans-serif; pointer-events: none;
    `;
    const left = document.createElement('div');
    left.id = 'agent-banner-left';
    left.style.cssText = 'display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 15px; color: #e6e8eb;';
    left.innerHTML = '🤖 <span>AI-тестировщик</span> <span style="color:#6366f1;font-size:12px;font-weight:500;">Kventin</span>';
    const progressWrap = document.createElement('div');
    progressWrap.id = 'agent-banner-progress-wrap';
    progressWrap.style.cssText = 'flex: 1; max-width: 280px; height: 8px; background: #2d323d; border-radius: 4px; overflow: hidden; margin: 0 16px;';
    const progressBar = document.createElement('div');
    progressBar.id = 'agent-banner-progress';
    progressBar.style.cssText = 'height: 100%; width: 0%; background: linear-gradient(90deg, #6366f1, #8b5cf6); border-radius: 4px; transition: width 0.4s ease;';
    progressWrap.appendChild(progressBar);
    const right = document.createElement('div');
    right.id = 'agent-banner-step';
    right.style.cssText = 'font-size: 13px; color: #8b949e;';
    right.textContent = 'Загрузка...';
    wrap.appendChild(left);
    wrap.appendChild(progressWrap);
    wrap.appendChild(right);
    document.body.appendChild(wrap);
    window.__agentBanner = { wrap, progressBar, right };
}
"""

# --- Окно диалога с LLM (анимированная рамка, «думает») ---
LLM_OVERLAY_SCRIPT = """
() => {
    if (window.__agentLLMOverlay) return;
    const wrap = document.createElement('div');
    wrap.id = 'agent-llm-overlay';
    wrap.style.cssText = `
        position: fixed; top: 56px; right: 12px; width: 400px; max-height: 82vh;
        z-index: 2147483646; font-family: system-ui, -apple-system, sans-serif; font-size: 13px;
        background: linear-gradient(160deg, #12141a 0%, #1c1f28 40%, #252a33 100%);
        color: #e6e8eb; border: 2px solid #3d434d; border-radius: 16px;
        box-shadow: 0 12px 40px rgba(0,0,0,0.6), inset 0 0 80px rgba(99,102,241,0.03);
        overflow: hidden; display: flex; flex-direction: column; pointer-events: none;
        animation: agent-border-glow 3s ease-in-out infinite;
    `;
    const title = document.createElement('div');
    title.id = 'agent-llm-title';
    title.style.cssText = `
        padding: 12px 16px; background: linear-gradient(90deg, #2d323d 0%, #363c48 100%);
        font-weight: 700; font-size: 14px; border-bottom: 1px solid #3d434d;
        display: flex; align-items: center; gap: 8px;
    `;
    title.innerHTML = '✨ Диалог с LLM <span style="color:#6366f1;font-weight:500;">GigaChat</span>';
    const status = document.createElement('div');
    status.id = 'agent-llm-status';
    status.textContent = 'Ожидание...';
    status.style.cssText = `
        padding: 8px 16px; background: #252a33; color: #8b949e; font-size: 12px;
        border-bottom: 1px solid #3d434d; min-height: 20px;
    `;
    const body = document.createElement('div');
    body.id = 'agent-llm-body';
    body.style.cssText = `
        padding: 14px 16px; overflow-y: auto; flex: 1; min-height: 100px; max-height: 55vh;
        line-height: 1.5;
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


def inject_demo_banner(page: Page) -> None:
    """Полоса сверху: «AI-тестировщик» и прогресс чеклиста."""
    try:
        page.evaluate(DEMO_STYLES)
        page.evaluate(DEMO_BANNER_SCRIPT)
    except Exception:
        pass


def update_demo_banner(page: Page, step_text: str = "", progress_pct: float = 0) -> None:
    """Обновить текст шага и прогресс-бар в демо-баннере."""
    try:
        progress_pct = max(0, min(100, progress_pct))
        page.evaluate(
            """(args) => {
                if (!window.__agentBanner) return;
                window.__agentBanner.right.textContent = args.step_text || '';
                window.__agentBanner.progressBar.style.width = args.progress_pct + '%';
            }""",
            {"step_text": step_text, "progress_pct": progress_pct},
        )
    except Exception:
        pass


def update_llm_overlay(
    page: Page,
    prompt: Optional[str] = None,
    response: Optional[str] = None,
    loading: bool = False,
    error: Optional[str] = None,
) -> None:
    """Обновить содержимое оверлея: запрос, ответ, статус (при loading — анимация «думает»)."""
    try:
        if loading:
            status_html = 'Запрос к GigaChat <span class="agent-thinking-dots"><span>.</span><span>.</span><span>.</span></span>'
        else:
            status_html = "Ошибка" if error else "Ответ получен"
        prompt_esc = _escape_html(prompt or "", 2500)
        response_esc = _escape_html(response or "", 2500)
        error_esc = _escape_html(error or "", 500)
        page.evaluate(
            """(args) => {
                if (!window.__agentLLMOverlay) return;
                const s = window.__agentLLMOverlay.status;
                if (s) s.innerHTML = args.status_html;
                const pr = document.getElementById('agent-llm-prompt');
                const r = document.getElementById('agent-llm-response');
                if (pr) pr.innerHTML = '<strong>Запрос:</strong><br>' + (args.prompt_esc || '') + (args.prompt_esc ? '<br><br>' : '');
                if (r) r.innerHTML = '<strong>Ответ:</strong><br>' + (args.error_esc || args.response_esc || '—');
            }""",
            {
                "status_html": status_html,
                "prompt_esc": prompt_esc,
                "response_esc": response_esc,
                "error_esc": error_esc,
            },
        )
    except Exception:
        pass
