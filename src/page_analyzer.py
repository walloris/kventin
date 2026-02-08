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
    Получить подробное описание DOM: кнопки, ссылки, формы, инпуты, дропдауны,
    чекбоксы, табы, модалки, меню — всё интерактивное.
    """
    try:
        summary = page.evaluate("""
            () => {
                const result = [];
                const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                };
                const desc = (el) => {
                    const o = {};
                    o.tag = el.tagName.toLowerCase();
                    if (el.id) o.id = el.id;
                    const cls = typeof el.className === 'string' ? el.className.trim() : '';
                    if (cls) o.class = cls.slice(0, 80);
                    const text = (el.textContent || el.value || el.placeholder || '').trim().replace(/\\s+/g, ' ');
                    if (text) o.text = text.slice(0, 80);
                    if (el.getAttribute('aria-label')) o.ariaLabel = el.getAttribute('aria-label').slice(0, 60);
                    if (el.getAttribute('title')) o.title = el.getAttribute('title').slice(0, 60);
                    if (el.getAttribute('role')) o.role = el.getAttribute('role');
                    if (el.disabled) o.disabled = true;
                    return o;
                };

                // Кнопки
                document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"]').forEach(el => {
                    if (!vis(el)) return;
                    const o = desc(el);
                    o._type = 'button';
                    if (el.type) o.type = el.type;
                    result.push(o);
                });

                // Ссылки
                document.querySelectorAll('a[href]').forEach(el => {
                    if (!vis(el)) return;
                    const href = el.getAttribute('href') || '';
                    if (href.startsWith('javascript:')) return;
                    const o = desc(el);
                    o._type = 'link';
                    o.href = href.slice(0, 120);
                    result.push(o);
                });

                // Формы и инпуты
                document.querySelectorAll('input, textarea, select').forEach(el => {
                    if (!vis(el)) return;
                    const o = desc(el);
                    o._type = 'input';
                    o.inputType = el.type || 'text';
                    if (el.name) o.name = el.name;
                    if (el.placeholder) o.placeholder = el.placeholder.slice(0, 50);
                    if (el.tagName === 'SELECT') {
                        o._type = 'dropdown';
                        const opts = Array.from(el.options).map(op => op.text.trim().slice(0, 30)).slice(0, 5);
                        if (opts.length) o.options = opts;
                    }
                    if (el.type === 'checkbox' || el.type === 'radio') {
                        o.checked = el.checked;
                    }
                    result.push(o);
                });

                // Табы
                document.querySelectorAll('[role="tab"], [role="tablist"] > *').forEach(el => {
                    if (!vis(el)) return;
                    const o = desc(el);
                    o._type = 'tab';
                    o.selected = el.getAttribute('aria-selected') === 'true';
                    result.push(o);
                });

                // Модалки / диалоги
                document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog, .modal, .popup, [class*="modal"], [class*="dialog"]').forEach(el => {
                    if (!vis(el)) return;
                    const o = desc(el);
                    o._type = 'modal';
                    o.open = true;
                    result.push(o);
                });

                // Меню
                document.querySelectorAll('[role="menu"], [role="menuitem"], nav a, .nav-link, .menu-item').forEach(el => {
                    if (!vis(el)) return;
                    const o = desc(el);
                    o._type = 'menu';
                    result.push(o);
                });

                // Дедупликация по text+tag
                const seen = new Set();
                const unique = [];
                for (const o of result) {
                    const key = (o.tag || '') + '|' + (o.text || '') + '|' + (o.id || '');
                    if (seen.has(key)) continue;
                    seen.add(key);
                    unique.push(o);
                }

                return unique.map(o => JSON.stringify(o)).join('\\n');
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
