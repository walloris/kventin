"""
Сбор и анализ консоли, сети и DOM страницы для передачи агенту и в Jira.
"""
from typing import List, Dict, Any, Optional

from playwright.sync_api import Page

from config import (
    IGNORE_CONSOLE_PATTERNS,
    IGNORE_NETWORK_STATUSES,
    COOKIE_BANNER_BUTTON_TEXTS,
    OVERLAY_IGNORE_PATTERNS,
)


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


def detect_cookie_banner(page: Page) -> Optional[Dict[str, Any]]:
    """
    Найти баннер cookies/согласия и кнопку для закрытия.
    Возвращает { "text": "текст кнопки", "selector": "селектор" } или None.
    """
    if not COOKIE_BANNER_BUTTON_TEXTS:
        return None
    try:
        found = page.evaluate(
            """(buttonTexts) => {
                const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
                };
                const texts = buttonTexts.map(t => t.toLowerCase());
                const all = document.querySelectorAll('button, [role="button"], a, input[type="submit"], input[type="button"], [class*="cookie"], [class*="consent"], [class*="accept"], [id*="cookie"], [id*="accept"]');
                for (const el of all) {
                    if (!vis(el)) continue;
                    const t = (el.textContent || el.value || el.getAttribute('aria-label') || '').trim().toLowerCase();
                    if (!t || t.length > 80) continue;
                    for (const need of texts) {
                        if (need.length < 2) continue;
                        if (t.includes(need) || need.includes(t)) {
                            return { text: (el.textContent || el.value || '').trim().slice(0, 50), selector: el.id ? '#' + el.id : null };
                        }
                    }
                }
                return null;
            }""",
            COOKIE_BANNER_BUTTON_TEXTS,
        )
        if found and found.get("text"):
            return found
    except Exception:
        pass
    return None


def get_iframes_info(page: Page) -> List[Dict[str, Any]]:
    """Список iframe на странице (src, name) для контекста."""
    try:
        frames = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('iframe')).map(f => ({
                src: (f.src || '').slice(0, 200),
                name: (f.name || '').slice(0, 80),
                id: (f.id || '').slice(0, 80)
            })).filter(x => x.src || x.name);
        }""")
        return frames or []
    except Exception:
        return []


def get_dom_summary(page: Page, max_length: int = 8000) -> str:
    """
    Получить подробное описание DOM: кнопки, ссылки, формы, инпуты, дропдауны,
    чекбоксы, табы, модалки, меню — всё интерактивное.
    """
    try:
        summary = page.evaluate("""
            () => {
                const result = [];
                // Служебный UI агента (чат с LLM, баннер Kventin) — не часть тестируемого приложения
                const isAgentUI = (el) => {
                    if (!el) return true;
                    let cur = el;
                    while (cur && cur !== document.body) {
                        const id = (cur.id || '').toString();
                        if (id.startsWith('agent-') || id === 'agent-banner' || id === 'agent-llm-overlay') return true;
                        cur = cur.parentElement;
                    }
                    return false;
                };
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
                    if (!vis(el) || isAgentUI(el)) return;
                    const o = desc(el);
                    o._type = 'button';
                    if (el.type) o.type = el.type;
                    result.push(o);
                });

                // Ссылки
                document.querySelectorAll('a[href]').forEach(el => {
                    if (!vis(el) || isAgentUI(el)) return;
                    const href = el.getAttribute('href') || '';
                    if (href.startsWith('javascript:')) return;
                    const o = desc(el);
                    o._type = 'link';
                    o.href = href.slice(0, 120);
                    result.push(o);
                });

                // Формы и инпуты
                document.querySelectorAll('input, textarea, select').forEach(el => {
                    if (!vis(el) || isAgentUI(el)) return;
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
                    if (!vis(el) || isAgentUI(el)) return;
                    const o = desc(el);
                    o._type = 'tab';
                    o.selected = el.getAttribute('aria-selected') === 'true';
                    result.push(o);
                });

                // Модалки / диалоги
                document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog, .modal, .popup, [class*="modal"], [class*="dialog"]').forEach(el => {
                    if (!vis(el) || isAgentUI(el)) return;
                    const o = desc(el);
                    o._type = 'modal';
                    o.open = true;
                    result.push(o);
                });

                // Меню
                document.querySelectorAll('[role="menu"], [role="menuitem"], nav a, .nav-link, .menu-item').forEach(el => {
                    if (!vis(el) || isAgentUI(el)) return;
                    const o = desc(el);
                    o._type = 'menu';
                    result.push(o);
                });

                // Элементы внутри Shadow DOM
                const walkShadow = (root) => {
                    if (!root) return;
                    try {
                        root.querySelectorAll('button, [role="button"], a[href], input:not([type="hidden"]), select, textarea').forEach(el => {
                            if (!vis(el) || isAgentUI(el)) return;
                            const o = desc(el);
                            o._type = o._type || 'shadow';
                            o._shadow = true;
                            result.push(o);
                        });
                        root.querySelectorAll('*').forEach(el => {
                            if (el.shadowRoot) walkShadow(el.shadowRoot);
                        });
                    } catch(e) {}
                };
                document.querySelectorAll('*').forEach(el => {
                    if (el.shadowRoot) walkShadow(el.shadowRoot);
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


def detect_active_overlays(page: Page) -> Dict[str, Any]:
    """
    Обнаружить все активные оверлеи на странице:
    модалки, тултипы, дропдауны, поповеры, уведомления, контекстные меню.
    Возвращает dict: { has_overlay, overlays: [{type, text, buttons, inputs, close_selector}] }
    Чат/виджеты поддержки (jivo, intercom, crisp и т.д.) исключаются — не часть приложения.
    """
    try:
        ignore_patterns = list(OVERLAY_IGNORE_PATTERNS) if OVERLAY_IGNORE_PATTERNS else []
        result = page.evaluate(
            """
            (ignorePatterns) => {
                const overlays = [];
                const isChatOrSupport = (el) => {
                    if (!el || !ignorePatterns || !ignorePatterns.length) return false;
                    const check = (s) => {
                        if (!s || typeof s !== 'string') return false;
                        const low = s.toLowerCase();
                        return ignorePatterns.some(p => low.indexOf(p) !== -1);
                    };
                    let cur = el;
                    for (let i = 0; i < 10 && cur; i++) {
                        if (check(cur.id) || check(cur.className && cur.className.toString()) || check(cur.getAttribute('aria-label') || '')) return true;
                        cur = cur.parentElement;
                    }
                    const text = (el.textContent || '').trim().toLowerCase().slice(0, 500);
                    return ignorePatterns.some(p => text.indexOf(p) !== -1);
                };
                const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 10 || r.height < 10) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity) > 0.1;
                };
                const zOf = (el) => {
                    let z = 0;
                    let cur = el;
                    while (cur && cur !== document.body) {
                        const zi = parseInt(getComputedStyle(cur).zIndex);
                        if (!isNaN(zi) && zi > z) z = zi;
                        cur = cur.parentElement;
                    }
                    return z;
                };
                const textOf = (el, max) => (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, max || 150);

                // --- Модалки / Диалоги ---
                const modalSels = [
                    '[role="dialog"]', '[role="alertdialog"]', 'dialog[open]',
                    '.modal.show', '.modal.active', '.modal.open', '.modal.visible',
                    '.modal-dialog', '.modal-content',
                    '[class*="modal"][class*="open"]', '[class*="modal"][class*="show"]',
                    '[class*="modal"][class*="active"]', '[class*="modal"][class*="visible"]',
                    '[class*="popup"][class*="open"]', '[class*="popup"][class*="show"]',
                    '[class*="popup"][class*="active"]', '[class*="popup"][class*="visible"]',
                    '[class*="drawer"][class*="open"]', '[class*="drawer"][class*="show"]',
                    '[class*="overlay"][class*="open"]', '[class*="overlay"][class*="show"]',
                    '[class*="lightbox"]',
                    '[aria-modal="true"]'
                ];
                const modalEls = new Set();
                for (const sel of modalSels) {
                    try {
                        document.querySelectorAll(sel).forEach(el => {
                            if (vis(el) && zOf(el) > 10) modalEls.add(el);
                        });
                    } catch(e) {}
                }
                // Ещё: элементы с position:fixed/absolute и высоким z-index
                document.querySelectorAll('*').forEach(el => {
                    if (modalEls.has(el)) return;
                    const s = getComputedStyle(el);
                    const pos = s.position;
                    if ((pos === 'fixed' || pos === 'absolute') && vis(el)) {
                        const z = parseInt(s.zIndex);
                        const r = el.getBoundingClientRect();
                        // Большой оверлей (не наш агентский UI)
                        if (z > 100 && r.width > 200 && r.height > 100
                            && !el.id?.startsWith('agent-') && !el.className?.toString().includes('agent-')) {
                            modalEls.add(el);
                        }
                    }
                });

                modalEls.forEach(el => {
                    if (isChatOrSupport(el)) return;
                    const o = { type: 'modal', text: textOf(el, 200), buttons: [], inputs: [], links: [], close_selector: null };
                    // Кнопки внутри модалки
                    el.querySelectorAll('button, [role="button"], input[type="submit"]').forEach(btn => {
                        if (vis(btn)) o.buttons.push(textOf(btn, 50) || btn.getAttribute('aria-label') || '(кнопка)');
                    });
                    // Инпуты внутри
                    el.querySelectorAll('input:not([type="hidden"]), textarea, select').forEach(inp => {
                        if (vis(inp)) o.inputs.push({ type: inp.type || 'text', placeholder: (inp.placeholder || '').slice(0, 40), name: inp.name || '' });
                    });
                    // Ссылки внутри
                    el.querySelectorAll('a[href]').forEach(a => {
                        if (vis(a)) o.links.push(textOf(a, 40));
                    });
                    // Крестик закрытия
                    const closeBtn = el.querySelector('[aria-label*="close" i], [aria-label*="закрыть" i], [class*="close"], [class*="dismiss"], button.close, .modal-close, [data-dismiss="modal"], [data-bs-dismiss="modal"]');
                    if (closeBtn && vis(closeBtn)) {
                        o.close_selector = closeBtn.id ? '#' + closeBtn.id
                            : closeBtn.getAttribute('aria-label') ? '[aria-label="' + closeBtn.getAttribute('aria-label') + '"]'
                            : closeBtn.className ? '.' + closeBtn.className.toString().split(' ').filter(c=>c).join('.')
                            : null;
                    }
                    if (o.text.length > 5 || o.buttons.length || o.inputs.length) overlays.push(o);
                });

                // --- Тултипы ---
                const tooltipSels = [
                    '[role="tooltip"]', '.tooltip.show', '.tooltip.active',
                    '[class*="tooltip"][class*="show"]', '[class*="tooltip"][class*="visible"]',
                    '.tippy-box', '.tippy-content', '[data-tippy-root]'
                ];
                for (const sel of tooltipSels) {
                    try {
                        document.querySelectorAll(sel).forEach(el => {
                            if (vis(el)) overlays.push({ type: 'tooltip', text: textOf(el, 120) });
                        });
                    } catch(e) {}
                }

                // --- Дропдауны ---
                const ddSels = [
                    '[role="listbox"]', '[role="menu"]:not(nav [role="menu"])',
                    '.dropdown-menu.show', '.dropdown-menu.active', '.dropdown-menu.open',
                    '[class*="dropdown"][class*="open"]', '[class*="dropdown"][class*="show"]',
                    '[class*="select"][class*="open"]', '[class*="select"][class*="show"]',
                    '[class*="listbox"]', '.autocomplete-results', '[class*="autocomplete"][class*="open"]',
                    'ul[class*="menu"][class*="open"]', 'ul[class*="menu"][class*="show"]'
                ];
                for (const sel of ddSels) {
                    try {
                        document.querySelectorAll(sel).forEach(el => {
                            if (vis(el) && zOf(el) > 5) {
                                const items = [];
                                el.querySelectorAll('[role="option"], [role="menuitem"], li, a').forEach(li => {
                                    if (vis(li)) items.push(textOf(li, 40));
                                });
                                overlays.push({ type: 'dropdown', text: textOf(el, 100), items: items.slice(0, 10) });
                            }
                        });
                    } catch(e) {}
                }

                // --- Поповеры ---
                const popSels = [
                    '[role="dialog"][class*="popover"]', '.popover.show', '.popover.active',
                    '[class*="popover"][class*="show"]', '[class*="popover"][class*="visible"]'
                ];
                for (const sel of popSels) {
                    try {
                        document.querySelectorAll(sel).forEach(el => {
                            if (vis(el)) overlays.push({ type: 'popover', text: textOf(el, 150) });
                        });
                    } catch(e) {}
                }

                // --- Уведомления / Тосты ---
                const toastSels = [
                    '[role="alert"]', '[role="status"]', '.toast.show',
                    '[class*="toast"][class*="show"]', '[class*="notification"][class*="show"]',
                    '[class*="snackbar"][class*="show"]', '[class*="alert"][class*="show"]',
                    '.Toastify__toast', '.notistack-SnackbarContainer'
                ];
                for (const sel of toastSels) {
                    try {
                        document.querySelectorAll(sel).forEach(el => {
                            if (vis(el)) overlays.push({ type: 'notification', text: textOf(el, 120) });
                        });
                    } catch(e) {}
                }

                // Дедупликация
                const seen = new Set();
                const unique = [];
                for (const o of overlays) {
                    const k = o.type + '|' + (o.text || '').slice(0, 50);
                    if (!seen.has(k)) { seen.add(k); unique.push(o); }
                }

                return { has_overlay: unique.length > 0, overlays: unique.slice(0, 8) };
            }
        """,
            ignore_patterns,
        )
        return result or {"has_overlay": False, "overlays": []}
    except Exception as e:
        return {"has_overlay": False, "overlays": [], "error": str(e)}


def format_overlays_context(overlay_info: Dict[str, Any]) -> str:
    """Форматировать информацию об оверлеях в текст для GigaChat."""
    if not overlay_info.get("has_overlay"):
        return ""
    lines = ["⚠️ АКТИВНЫЕ ОВЕРЛЕИ НА СТРАНИЦЕ (тестируй их в первую очередь!):"]
    for i, ov in enumerate(overlay_info.get("overlays", []), 1):
        ov_type = ov.get("type", "unknown")
        text = ov.get("text", "")[:120]
        lines.append(f"  [{i}] Тип: {ov_type} | Текст: {text}")
        if ov.get("buttons"):
            lines.append(f"      Кнопки: {', '.join(ov['buttons'][:5])}")
        if ov.get("inputs"):
            inp_desc = [f"{inp.get('type','text')}({inp.get('placeholder','') or inp.get('name','')})" for inp in ov["inputs"][:5]]
            lines.append(f"      Поля ввода: {', '.join(inp_desc)}")
        if ov.get("links"):
            lines.append(f"      Ссылки: {', '.join(ov['links'][:5])}")
        if ov.get("items"):
            lines.append(f"      Пункты: {', '.join(ov['items'][:8])}")
        if ov.get("close_selector"):
            lines.append(f"      Закрыть: selector={ov['close_selector']}")
    lines.append("  → Сначала протестируй содержимое оверлея, потом закрой его (action=close_modal).")
    return "\n".join(lines)


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
