"""
Сбор и анализ консоли, сети и DOM страницы для передачи агенту и в Jira.
"""
from typing import List, Dict, Any, Optional

from playwright.sync_api import Page

from config import IGNORE_CONSOLE_PATTERNS, IGNORE_NETWORK_STATUSES


def _should_ignore_console(text: str) -> bool:
    text_lower = text.lower()
    for pattern in IGNORE_CONSOLE_PATTERNS:
        if pattern.lower() in text_lower:
            return True
    return False


def _should_ignore_network(url: str, status: Optional[int]) -> bool:
    if status in IGNORE_NETWORK_STATUSES:
        return True
    url_lower = url.lower()
    for pattern in IGNORE_CONSOLE_PATTERNS:
        if pattern in url_lower:
            return True
    return False


def collect_console_messages(page: Page) -> List[Dict[str, Any]]:
    """Собрать сообщения консоли (логируем через page.on)."""
    # Сообщения собираются через перехват в agent — здесь возвращаем то, что передано
    return getattr(page, "_agent_console_log", [])


def collect_network_failures(page: Page) -> List[Dict[str, Any]]:
    """Собрать неуспешные сетевые запросы (перехватываются в agent)."""
    return getattr(page, "_agent_network_failures", [])


def get_dom_summary(page: Page, max_length: int = 8000) -> str:
    """
    Получить сокращённое описание DOM: теги, id, классы, текст кнопок/ссылок.
    Без полного дерева, чтобы не перегружать контекст.
    """
    try:
        summary = page.evaluate("""
            () => {
                const result = [];
                const add = (selector, attrs) => {
                    const el = document.querySelector(selector);
                    if (!el) return;
                    const o = { tag: el.tagName.toLowerCase() };
                    if (el.id) o.id = el.id;
                    if (el.className && typeof el.className === 'string') o.class = el.className.slice(0, 100);
                    if (attrs) Object.assign(o, attrs);
                    result.push(JSON.stringify(o));
                };
                // Кнопки
                document.querySelectorAll('button, [role="button"], input[type="submit"]').forEach((el, i) => {
                    result.push(JSON.stringify({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || null,
                        text: (el.textContent || el.value || '').trim().slice(0, 80),
                        id: el.id || null,
                        class: (el.className && el.className.slice(0, 80)) || null
                    }));
                });
                // Ссылки (внутренние)
                document.querySelectorAll('a[href]').forEach((el) => {
                    const href = el.getAttribute('href') || '';
                    if (href.startsWith('#') || href.startsWith('javascript:')) return;
                    result.push(JSON.stringify({
                        tag: 'a',
                        text: (el.textContent || '').trim().slice(0, 60),
                        href: href.slice(0, 120)
                    }));
                });
                return result.join('\\n');
            }
        """)
        return (summary or "")[:max_length]
    except Exception as e:
        return f"[Ошибка DOM: {e}]"


def build_context(
    page: Page,
    current_url: str,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
) -> str:
    """
    Собрать текстовый контекст страницы для GigaChat: консоль, сеть, DOM.
    """
    lines = [f"Текущий URL: {current_url}", ""]

    if console_log:
        filtered = [c for c in console_log if not _should_ignore_console(c.get("text", ""))]
        if filtered:
            lines.append("Консоль (важные сообщения):")
            for entry in filtered[-20:]:
                lines.append(f"  [{entry.get('type', 'log')}] {entry.get('text', '')[:200]}")
            lines.append("")

    if network_failures:
        filtered = [
            n for n in network_failures
            if not _should_ignore_network(n.get("url", ""), n.get("status"))
        ]
        if filtered:
            lines.append("Сеть (ошибки запросов):")
            for entry in filtered[-15:]:
                lines.append(f"  {entry.get('status')} {entry.get('url', '')[:150]}")
            lines.append("")

    dom = get_dom_summary(page)
    if dom:
        lines.append("DOM (кнопки и ссылки):")
        lines.append(dom[:4000])
        if len(dom) > 4000:
            lines.append("... (обрезано)")

    return "\n".join(lines)
