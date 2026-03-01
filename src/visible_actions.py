"""
Видимые действия агента: курсор на странице, подсветка элементов, оверлей диалога с LLM.

ВСЕ элементы UI агента живут внутри CLOSED Shadow DOM — они полностью невидимы
для document.querySelectorAll() и любых DOM-запросов из основного документа.
GigaChat никогда не увидит их в DOM summary.
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


# ============================================================
# Shadow DOM host: единая точка входа для всего UI агента.
# mode: 'closed' → снаружи невозможно получить shadowRoot.
# ============================================================
SHADOW_HOST_SCRIPT = """
() => {
    if (window.__agentShadow) return;
    const host = document.createElement('div');
    host.setAttribute('data-agent-host', '1');
    host.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;overflow:visible;z-index:2147483647;pointer-events:none;';
    document.body.appendChild(host);
    const shadow = host.attachShadow({ mode: 'closed' });

    // Стили внутри Shadow DOM (изолированы от страницы)
    const style = document.createElement('style');
    style.textContent = `
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        @keyframes agent-cursor-pulse {
            0%, 100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(231, 76, 60, 1), 0 0 50px 12px rgba(231,76,60,0.9), 0 0 100px 20px rgba(231,76,60,0.6), 0 0 150px 30px rgba(231,76,60,0.3); }
            50% { transform: scale(1.3); box-shadow: 0 0 0 20px rgba(231, 76, 60, 0), 0 0 70px 18px rgba(231,76,60,1), 0 0 120px 25px rgba(231,76,60,0.8), 0 0 180px 35px rgba(231,76,60,0.5); }
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
        .thinking-dots span { animation: agent-thinking 1.4s ease-in-out infinite; }
        .thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
        .thinking-dots span:nth-child(3) { animation-delay: 0.4s; }
    `;
    shadow.appendChild(style);

    // --- Курсор: яркая большая стрелка (не загораживает контент) ---
    const cursor = document.createElement('div');
    cursor.innerHTML = '▼';
    cursor.style.cssText = `
        position: fixed; width: 48px; height: 48px; display: flex; align-items: center; justify-content: center;
        font-size: 42px; line-height: 1; color: #fff;
        text-shadow: 0 0 12px rgba(255,255,255,1), 0 0 24px rgba(99,102,241,0.9), 0 2px 4px rgba(0,0,0,0.8);
        pointer-events: none; z-index: 2147483647;
        left: -100px; top: -100px;
        transition: left 0.12s ease-out, top 0.12s ease-out;
        display: flex !important; opacity: 1 !important;
    `;
    shadow.appendChild(cursor);

    // --- Баннер (левый нижний угол) ---
    const banner = document.createElement('div');
    banner.style.cssText = `
        position: fixed; bottom: 12px; left: 12px; z-index: 2147483643;
        width: 300px; height: 32px; display: flex; align-items: center; padding: 0 12px;
        background: linear-gradient(90deg, #0f0f12 0%, #1a1b23 50%, #16171d 100%);
        background-size: 200% 100%; animation: agent-banner-shine 8s ease infinite;
        border: 1px solid rgba(99,102,241,0.4); border-radius: 8px;
        box-shadow: 0 4px 24px rgba(0,0,0,0.6);
        font-family: system-ui, sans-serif; pointer-events: none;
    `;
    const bLeft = document.createElement('div');
    bLeft.style.cssText = 'display:flex;align-items:center;gap:6px;font-weight:700;font-size:11px;color:#e6e8eb;white-space:nowrap;';
    bLeft.innerHTML = '🤖 <span>AI-тестировщик</span>';
    const progressWrap = document.createElement('div');
    progressWrap.style.cssText = 'flex:1;height:6px;background:#2d323d;border-radius:3px;overflow:hidden;margin:0 8px;';
    const progressBar = document.createElement('div');
    progressBar.style.cssText = 'height:100%;width:0%;background:linear-gradient(90deg,#6366f1,#8b5cf6);border-radius:3px;transition:width 0.4s ease;';
    progressWrap.appendChild(progressBar);
    const bRight = document.createElement('div');
    bRight.style.cssText = 'font-size:10px;color:#8b949e;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:80px;';
    bRight.textContent = 'Загрузка...';
    banner.appendChild(bLeft);
    banner.appendChild(progressWrap);
    banner.appendChild(bRight);
    shadow.appendChild(banner);

    // --- LLM Overlay (левый нижний угол, над баннером) ---
    const overlay = document.createElement('div');
    overlay.style.cssText = `
        position: fixed; bottom: 200px; left: 12px; width: 300px; max-height: 250px;
        z-index: 2147483646; font-family: system-ui, -apple-system, sans-serif; font-size: 11px;
        background: linear-gradient(160deg, #12141a 0%, #1c1f28 40%, #252a33 100%);
        color: #e6e8eb; border: 1px solid #3d434d; border-radius: 10px;
        box-shadow: 0 8px 24px rgba(0,0,0,0.6), inset 0 0 60px rgba(99,102,241,0.03);
        overflow: hidden; display: flex; flex-direction: column; pointer-events: none;
        animation: agent-border-glow 3s ease-in-out infinite;
    `;
    const title = document.createElement('div');
    title.style.cssText = `
        padding: 8px 10px; background: linear-gradient(90deg, #2d323d 0%, #363c48 100%);
        font-weight: 700; font-size: 11px; border-bottom: 1px solid #3d434d;
        display: flex; align-items: center; gap: 6px;
    `;
    title.innerHTML = '✨ LLM <span style="color:#6366f1;font-weight:500;">GigaChat</span>';
    const status = document.createElement('div');
    status.style.cssText = `
        padding: 6px 10px; background: #252a33; color: #8b949e; font-size: 10px;
        border-bottom: 1px solid #3d434d; min-height: 16px;
    `;
    status.textContent = 'Ожидание...';
    const body = document.createElement('div');
    body.style.cssText = `
        padding: 10px; overflow-y: auto; flex: 1; min-height: 80px; max-height: 190px;
        line-height: 1.4; font-size: 10px;
    `;
    const prompt = document.createElement('div');
    prompt.style.cssText = 'margin-bottom:12px;';
    const response = document.createElement('div');
    response.style.cssText = 'white-space:pre-wrap;word-break:break-word;';
    body.appendChild(prompt);
    body.appendChild(response);
    overlay.appendChild(title);
    overlay.appendChild(status);
    overlay.appendChild(body);
    shadow.appendChild(overlay);

    // Сохраняем ссылки (через closure — снаружи shadowRoot недоступен)
    window.__agentShadow = {
        host,
        cursor,
        banner,
        progressBar,
        bannerStep: bRight,
        overlay,
        llmStatus: status,
        llmPrompt: prompt,
        llmResponse: response,
    };
}
"""


def _ensure_shadow(page: Page) -> None:
    """Инициализировать Shadow DOM host (если ещё нет)."""
    try:
        page.evaluate(SHADOW_HOST_SCRIPT)
    except Exception:
        pass


# ===== Публичные функции (совместимый API) =====

def inject_cursor(page: Page) -> None:
    """Добавить визуальный курсор (внутри Shadow DOM)."""
    _ensure_shadow(page)


def inject_llm_overlay(page: Page) -> None:
    """Показать окно диалога с LLM (внутри Shadow DOM)."""
    _ensure_shadow(page)


def inject_demo_banner(page: Page) -> None:
    """Полоса сверху: «AI-тестировщик» и прогресс (внутри Shadow DOM)."""
    _ensure_shadow(page)


def show_click_ripple(page: Page, x: float, y: float) -> None:
    """Короткая подсветка стрелки в точке клика (без большого круга)."""
    try:
        page.evaluate(
            """([x, y]) => {
                const arr = document.createElement('div');
                arr.textContent = '▼';
                arr.style.cssText = `
                    position: fixed; left: ${x}px; top: ${y}px; width: 40px; height: 40px;
                    margin-left: -20px; margin-top: -20px; display: flex; align-items: center; justify-content: center;
                    font-size: 32px; color: #fff; pointer-events: none; z-index: 2147483645;
                    text-shadow: 0 0 16px rgba(255,255,255,1), 0 0 32px rgba(99,102,241,0.8);
                `;
                document.body.appendChild(arr);
                setTimeout(() => { if (arr.parentNode) arr.remove(); }, 500);
            }""",
            [x, y],
        )
    except Exception:
        pass


def show_highlight_label(page: Page, x: float, y: float, text: str = "Кликаю сюда") -> None:
    """Всплывающая подсказка над элементом - БОЛЬШЕ и ЯРЧЕ."""
    try:
        page.evaluate(
            """([x, y, text]) => {
                if (window.__agentLabel) window.__agentLabel.remove();
                const el = document.createElement('div');
                el.setAttribute('data-agent-host', '1');
                window.__agentLabel = el;
                el.textContent = text;
                el.style.cssText = `
                    position: fixed; left: ${x}px; top: ${y - 70}px; transform: translateX(-50%);
                    padding: 14px 20px; background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%);
                    color: white; font: 700 20px system-ui, sans-serif; border-radius: 12px; white-space: nowrap;
                    box-shadow: 0 8px 30px rgba(231,76,60,1), 0 0 50px rgba(231,76,60,0.7), 0 0 80px rgba(231,76,60,0.4);
                    pointer-events: none; z-index: 2147483644;
                    border: 3px solid rgba(255,255,255,0.5);
                    text-shadow: 0 2px 5px rgba(0,0,0,0.8), 0 0 10px rgba(255,255,255,0.3);
                    animation: agent-label-pop 0.3s ease-out;
                `;
                document.body.appendChild(el);
                setTimeout(() => { if (el.parentNode) el.remove(); window.__agentLabel = null; }, 4000);
            }""",
            [x, y, text],
        )
    except Exception:
        pass


def move_cursor_to(page: Page, x: float, y: float) -> None:
    """Обновить позицию визуального курсора."""
    try:
        page.evaluate(
            """([x, y]) => {
                if (window.__agentShadow && window.__agentShadow.cursor) {
                    const cursor = window.__agentShadow.cursor;
                    cursor.style.left = (x - 24) + 'px';  // Центрируем стрелку (48px / 2)
                    cursor.style.top = (y - 24) + 'px';
                    cursor.style.display = 'block';
                    cursor.style.opacity = '1';
                    cursor.style.zIndex = '2147483647';
                    cursor.style.visibility = 'visible';
                    // Принудительно показываем курсор
                    if (cursor.style.display === 'none') cursor.style.display = 'block';
                }
            }""",
            [x, y],
        )
    except Exception:
        pass


def scroll_to_center(locator: Locator, page: Page) -> None:
    """
    Плавно прокрутить страницу так, чтобы элемент оказался в ЦЕНТРЕ экрана.
    Использует scrollIntoView({ behavior: 'smooth', block: 'center' }).
    """
    try:
        locator.evaluate("el => el.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' })")
        time.sleep(0.4)  # Даём время на плавную прокрутку
    except Exception:
        # Fallback: стандартный Playwright-метод
        try:
            locator.scroll_into_view_if_needed()
            time.sleep(0.15)
        except Exception:
            pass


def highlight_and_click(locator: Locator, page: Page, description: str = "") -> None:
    """Подсветить элемент, подсказка, курсор, клик и эффект ряби."""
    try:
        scroll_to_center(locator, page)
        
        # Получаем координаты ДО highlight, чтобы курсор был виден
        box = locator.bounding_box()
        cx, cy = None, None
        if box:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            # Сначала показываем курсор и подсказку - это самое заметное
            move_cursor_to(page, cx, cy)
            show_highlight_label(page, cx, cy, description or "КЛИКАЮ!")
            time.sleep(0.5)  # Дольше пауза чтобы пользователь точно увидел
        
        # Затем подсвечиваем элемент (Playwright highlight)
        locator.highlight()
        
        # Увеличиваем время показа - минимум 1.0 секунда чтобы было видно
        highlight_duration = max(1.0, HIGHLIGHT_DURATION_MS / 1000.0)
        time.sleep(highlight_duration)
        
        # Клик
        locator.click()
        
        # Эффект ряби после клика
        if cx is not None and cy is not None:
            show_click_ripple(page, cx, cy)
            time.sleep(0.4)  # Дольше пауза чтобы увидеть эффект ряби
    except Exception as e:
        raise e
    finally:
        try:
            page.evaluate("() => { document.querySelectorAll('[data-playwright-highlight]').forEach(e => e.remove()); }")
        except Exception:
            pass


def safe_highlight(locator: Locator, page: Page, duration_sec: float = None) -> None:
    """Подсветить элемент и показать подсказку «Проверяю»."""
    duration_sec = duration_sec or max(0.6, HIGHLIGHT_DURATION_MS / 1000.0)  # Минимум 0.6 сек
    try:
        scroll_to_center(locator, page)
        
        # Получаем координаты и показываем курсор/подсказку СРАЗУ
        box = locator.bounding_box()
        if box:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            move_cursor_to(page, cx, cy)
            show_highlight_label(page, cx, cy, "Проверяю")
            time.sleep(0.4)  # Дольше пауза чтобы увидеть курсор
        
        # Затем подсвечиваем элемент
        locator.highlight()
        time.sleep(duration_sec)
    finally:
        try:
            page.evaluate("() => { document.querySelectorAll('[data-playwright-highlight]').forEach(e => e.remove()); }")
        except Exception:
            pass


def update_demo_banner(page: Page, step_text: str = "", progress_pct: float = 0) -> None:
    """Обновить текст шага и прогресс-бар."""
    try:
        progress_pct = max(0, min(100, progress_pct))
        page.evaluate(
            """(args) => {
                if (!window.__agentShadow) return;
                const s = window.__agentShadow;
                if (s.bannerStep) s.bannerStep.textContent = args.step_text || '';
                if (s.progressBar) s.progressBar.style.width = args.progress_pct + '%';
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
    """Обновить содержимое LLM-оверлея."""
    try:
        if loading:
            status_html = 'Запрос к GigaChat <span class="thinking-dots"><span>.</span><span>.</span><span>.</span></span>'
        else:
            status_html = "Ошибка" if error else "Ответ получен"
        prompt_esc = _escape_html(prompt or "", 2500)
        response_esc = _escape_html(response or "", 2500)
        error_esc = _escape_html(error or "", 500)
        page.evaluate(
            """(args) => {
                if (!window.__agentShadow) return;
                const s = window.__agentShadow;
                if (s.llmStatus) s.llmStatus.innerHTML = args.status_html;
                if (s.llmPrompt) s.llmPrompt.innerHTML = '<strong>Запрос:</strong><br>' + (args.prompt_esc || '') + (args.prompt_esc ? '<br><br>' : '');
                if (s.llmResponse) s.llmResponse.innerHTML = '<strong>Ответ:</strong><br>' + (args.error_esc || args.response_esc || '—');
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
