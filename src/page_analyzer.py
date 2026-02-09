"""
Сбор и анализ консоли, сети и DOM страницы для передачи агенту и в Jira.
"""
from typing import List, Dict, Any, Optional

from playwright.sync_api import Page

from config import (
    IGNORE_CONSOLE_PATTERNS,
    IGNORE_NETWORK_STATUSES,
    IGNORE_NETWORK_URL_PATTERNS,
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
    for pattern in IGNORE_NETWORK_URL_PATTERNS:
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


def detect_page_type(page: Page) -> str:
    """
    Определить тип страницы для адаптивной стратегии тестирования.
    Возвращает: 'landing', 'dashboard', 'form', 'catalog', 'article', 'unknown'
    """
    try:
        page_type = page.evaluate("""() => {
            const url = window.location.pathname.toLowerCase();
            const title = (document.title || '').toLowerCase();
            const bodyText = (document.body.textContent || '').toLowerCase();
            const hasForm = document.querySelectorAll('form, input[type="text"], input[type="email"], textarea').length > 2;
            const hasTable = document.querySelectorAll('table, .table, [role="table"]').length > 0;
            const hasCards = document.querySelectorAll('.card, .product-card, [class*="card"]').length > 3;
            const hasHero = document.querySelectorAll('.hero, .banner, [class*="hero"], [class*="banner"]').length > 0;
            const hasNav = document.querySelectorAll('nav, .nav, .navbar, [role="navigation"]').length > 0;
            
            // Landing page
            if (hasHero || url.includes('landing') || url === '/' || url === '') {
                return 'landing';
            }
            
            // Form page
            if (hasForm && (url.includes('form') || url.includes('register') || url.includes('login') || url.includes('signup'))) {
                return 'form';
            }
            
            // Dashboard
            if (hasTable || (hasNav && url.includes('dashboard')) || url.includes('admin') || url.includes('panel')) {
                return 'dashboard';
            }
            
            // Catalog / List
            if (hasCards || url.includes('catalog') || url.includes('list') || url.includes('products') || url.includes('items')) {
                return 'catalog';
            }
            
            // Article / Content
            if (document.querySelectorAll('article, .article, main p').length > 5 || url.includes('article') || url.includes('post')) {
                return 'article';
            }
            
            return 'unknown';
        }""")
        return page_type or "unknown"
    except Exception:
        return "unknown"


def detect_table_structure(page: Page) -> List[Dict[str, Any]]:
    """
    Обнаружить таблицы на странице и их структуру (колонки, фильтры, сортировка).
    """
    try:
        tables = page.evaluate("""() => {
            const result = [];
            const tableEls = document.querySelectorAll('table, [role="table"], .table, [class*="table"]');
            
            tableEls.forEach((table, idx) => {
                const headers = [];
                const rows = [];
                
                // Ищем заголовки
                const headerCells = table.querySelectorAll('th, thead td, [role="columnheader"]');
                headerCells.forEach(th => {
                    const text = (th.textContent || '').trim();
                    if (text) headers.push(text.slice(0, 50));
                });
                
                // Ищем фильтры рядом с таблицей
                const filters = [];
                let parent = table.parentElement;
                for (let i = 0; i < 3 && parent; i++) {
                    const filterInputs = parent.querySelectorAll('input[type="text"], input[type="search"], select, [role="combobox"]');
                    filterInputs.forEach(inp => {
                        const label = inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || '';
                        if (label) filters.push(label.slice(0, 50));
                    });
                    parent = parent.parentElement;
                }
                
                // Ищем кнопки сортировки
                const sortButtons = [];
                table.querySelectorAll('[aria-sort], [class*="sort"], button[aria-label*="sort"]').forEach(btn => {
                    const label = btn.getAttribute('aria-label') || btn.textContent || '';
                    if (label) sortButtons.push(label.slice(0, 50));
                });
                
                if (headers.length > 0 || filters.length > 0) {
                    result.push({
                        index: idx,
                        headers: headers.slice(0, 10),
                        filters: filters.slice(0, 5),
                        sortButtons: sortButtons.slice(0, 5),
                        rowCount: table.querySelectorAll('tbody tr, [role="row"]').length,
                    });
                }
            });
            
            return result;
        }""")
        return tables or []
    except Exception:
        return []


def detect_form_fields(page: Page) -> List[Dict[str, Any]]:
    """
    Обнаружить все поля формы на странице для умного заполнения.
    Возвращает список полей с ref-id (data-agent-ref) для мгновенного поиска.
    """
    try:
        fields = page.evaluate("""() => {
            const result = [];
            const seen = new WeakSet();
            if (!window.__agentRefs) window.__agentRefs = {};
            // Находим текущий максимальный ref
            let maxRef = 0;
            for (const k of Object.keys(window.__agentRefs)) {
                const n = parseInt(k, 10);
                if (n > maxRef) maxRef = n;
            }
            let refCounter = maxRef + 1;

            const vis = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            };

            const processInput = (inp) => {
                if (!inp || !vis(inp) || inp.disabled || seen.has(inp)) return;
                seen.add(inp);
                // Если у элемента уже есть ref — используем его
                let ref = inp.getAttribute('data-agent-ref');
                if (!ref) {
                    ref = String(refCounter++);
                    inp.setAttribute('data-agent-ref', ref);
                    window.__agentRefs[parseInt(ref)] = inp;
                }
                const field = {
                    type: inp.type || inp.tagName.toLowerCase(),
                    name: inp.name || '',
                    id: inp.id || '',
                    placeholder: inp.placeholder || '',
                    ariaLabel: inp.getAttribute('aria-label') || '',
                    required: inp.required || inp.hasAttribute('required'),
                    selector: 'ref:' + ref,
                    ref: parseInt(ref),
                };
                if (inp.tagName === 'SELECT') {
                    field.type = 'select';
                    field.options = Array.from(inp.options).slice(0, 10).map(opt => opt.text.trim());
                }
                result.push(field);
            };

            // Поля внутри форм
            document.querySelectorAll('form').forEach(form => {
                if (!vis(form)) return;
                form.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select').forEach(processInput);
            });
            // Standalone поля (не в форме)
            document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select').forEach(inp => {
                if (seen.has(inp)) return;
                processInput(inp);
            });

            return result;
        }""")
        return fields or []
    except Exception:
        return []


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
    Получить описание DOM с уникальными ref-id для каждого элемента.
    Каждому интерактивному элементу присваивается data-agent-ref="N",
    и ссылка на DOM-ноду сохраняется в window.__agentRefs[N].
    GigaChat возвращает ref:N как selector → _find_element находит элемент мгновенно.
    """
    try:
        summary = page.evaluate("""
            () => {
                // Сбрасываем предыдущие ref-ы
                if (window.__agentRefs) {
                    document.querySelectorAll('[data-agent-ref]').forEach(el => el.removeAttribute('data-agent-ref'));
                }
                window.__agentRefs = {};
                let refCounter = 1;

                const result = [];

                // --- Фильтры ---
                const isAgentUI = (el) => {
                    if (!el) return true;
                    let cur = el;
                    while (cur && cur !== document.body) {
                        if (cur.hasAttribute && cur.hasAttribute('data-agent-host')) return true;
                        cur = cur.parentElement;
                    }
                    return false;
                };
                const servicePatterns = ['chat','чат','support','поддержк','help','консультант','jivo','intercom','crisp','drift','tawk','livechat','live-chat','widget-chat','chat-widget','feedback','обратн','звонок','callback','kventin','agent-llm','agent-banner','диалог с llm','ai-тестировщик','gigachat','cookie','consent'];
                const isServiceElement = (el) => {
                    if (!el) return true;
                    const combined = ((el.textContent||'')+(el.id||'')+(el.className||'')).toLowerCase();
                    for (const p of servicePatterns) { if (combined.includes(p)) return true; }
                    let cur = el.parentElement, d = 0;
                    while (cur && cur !== document.body && d < 3) {
                        const pt = ((cur.className||'')+(cur.id||'')).toLowerCase();
                        for (const p of servicePatterns) { if (pt.includes(p)) return true; }
                        cur = cur.parentElement; d++;
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

                // --- Назначить ref элементу ---
                const assignRef = (el) => {
                    const ref = refCounter++;
                    el.setAttribute('data-agent-ref', String(ref));
                    window.__agentRefs[ref] = el;
                    return ref;
                };

                // --- Описание элемента (компактное, с ref) ---
                const desc = (el, type) => {
                    const ref = assignRef(el);
                    const tag = el.tagName.toLowerCase();
                    const text = (el.textContent || el.value || el.placeholder || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
                    const parts = [`[${ref}]`, type || tag];
                    if (text) parts.push(`"${text}"`);
                    if (el.id) parts.push(`id=${el.id}`);
                    if (el.getAttribute('aria-label')) parts.push(`aria="${el.getAttribute('aria-label').slice(0,40)}"`);
                    if (el.name) parts.push(`name=${el.name}`);
                    if (el.placeholder) parts.push(`ph="${el.placeholder.slice(0,30)}"`);
                    if (el.disabled) parts.push('DISABLED');
                    if (el.getAttribute('href')) parts.push(`href=${el.getAttribute('href').slice(0,60)}`);
                    if (tag === 'select') {
                        const opts = Array.from(el.options).slice(0,5).map(o => o.text.trim().slice(0,20));
                        if (opts.length) parts.push(`opts=[${opts.join(',')}]`);
                    }
                    if (el.type === 'checkbox' || el.type === 'radio') parts.push(el.checked ? 'CHECKED' : 'unchecked');
                    if (el.getAttribute('role')) parts.push(`role=${el.getAttribute('role')}`);
                    return parts.join(' ');
                };

                // --- Сбор элементов ---
                const seen = new WeakSet();
                const collect = (el, type) => {
                    if (!el || seen.has(el) || !vis(el) || isAgentUI(el) || isServiceElement(el)) return;
                    seen.add(el);
                    result.push(desc(el, type));
                };

                // Кнопки
                document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"]').forEach(el => collect(el, 'button'));
                // Ссылки
                document.querySelectorAll('a[href]').forEach(el => {
                    if ((el.getAttribute('href')||'').startsWith('javascript:')) return;
                    collect(el, 'link');
                });
                // Инпуты
                document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select').forEach(el => {
                    const tag = el.tagName.toLowerCase();
                    const type = tag === 'select' ? 'select' : (el.type === 'checkbox' ? 'checkbox' : (el.type === 'radio' ? 'radio' : 'input'));
                    collect(el, type);
                });
                // Табы
                document.querySelectorAll('[role="tab"]').forEach(el => collect(el, 'tab'));
                // Меню
                document.querySelectorAll('[role="menuitem"], nav a, .nav-link, .menu-item').forEach(el => collect(el, 'menu'));
                // Модалки
                document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog').forEach(el => collect(el, 'modal'));

                return result.join('\\n');
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
                // UI агента в closed Shadow DOM — невидим. Фильтр только для host-элемента.
                const isAgentUI = (el) => {
                    if (!el) return false;
                    let cur = el;
                    while (cur && cur !== document.body) {
                        if (cur.hasAttribute && cur.hasAttribute('data-agent-host')) return true;
                        cur = cur.parentElement;
                    }
                    return false;
                };
                const isChatOrSupport = (el) => {
                    if (!el || !ignorePatterns || !ignorePatterns.length) return false;
                    // Сначала проверяем: это UI агента?
                    if (isAgentUI(el)) return true;
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
                            && !(el.hasAttribute && el.hasAttribute('data-agent-host'))) {
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
                    // Крестик закрытия — назначаем ref для надёжного поиска
                    const closeBtn = el.querySelector('[aria-label*="close" i], [aria-label*="закрыть" i], [class*="close"], [class*="dismiss"], button.close, .modal-close, [data-dismiss="modal"], [data-bs-dismiss="modal"]');
                    if (closeBtn && vis(closeBtn)) {
                        let closeRef = closeBtn.getAttribute('data-agent-ref');
                        if (!closeRef && window.__agentRefs) {
                            let maxR = 0;
                            for (const k of Object.keys(window.__agentRefs)) { const n = parseInt(k); if (n > maxR) maxR = n; }
                            closeRef = String(maxR + 1);
                            closeBtn.setAttribute('data-agent-ref', closeRef);
                            window.__agentRefs[parseInt(closeRef)] = closeBtn;
                        }
                        o.close_selector = closeRef ? 'ref:' + closeRef : null;
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
                            if (vis(el) && !isAgentUI(el) && !isChatOrSupport(el)) overlays.push({ type: 'tooltip', text: textOf(el, 120) });
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
                            if (vis(el) && zOf(el) > 5 && !isAgentUI(el) && !isChatOrSupport(el)) {
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
                            if (vis(el) && !isAgentUI(el) && !isChatOrSupport(el)) overlays.push({ type: 'popover', text: textOf(el, 150) });
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
                            if (vis(el) && !isAgentUI(el) && !isChatOrSupport(el)) overlays.push({ type: 'notification', text: textOf(el, 120) });
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
        lines.append("ЭЛЕМЕНТЫ (формат [ref] тип \"текст\" атрибуты — используй ref:N как selector):")
        lines.append(dom[:4000])
        if len(dom) > 4000:
            lines.append("... (обрезано)")

    return "\n".join(lines)
