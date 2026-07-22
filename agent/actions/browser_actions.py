"""Browser action adapter for the synchronous Playwright thread."""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from playwright.sync_api import Page

from config import SCROLL_PIXELS
from agent.actions.action_executor import ActionHandlers, execute_browser_action
from agent.actions.element_resolver import norm_key as _norm_key
from agent.actions.form_strategies import detect_field_type, get_test_value
from agent.actions.visible_actions import (
    highlight_and_click,
    inject_cursor,
    inject_llm_overlay,
    safe_highlight,
    scroll_to_center,
)
from agent.browser.page_analyzer import detect_cookie_banner, detect_form_fields
from agent.browser.screenshot import take_screenshot_b64
from agent.core.agent_memory import AgentMemory

LOG = logging.getLogger("kventin.browser-actions")
_current_agent_memory: Optional[AgentMemory] = None

def set_current_agent_memory(memory: Optional[AgentMemory]) -> None:
    global _current_agent_memory
    _current_agent_memory = memory

def describe_element_for_report(page: Page, selector: str) -> str:
    """
    Построить человекочитаемое описание элемента по селектору (ref:N или CSS)
    для подробных шагов воспроизведения и описания дефекта.
    Пример: 'button «Войти» id=#login-btn data-testid="login-submit" aria-label="Вход"'.
    """
    if not selector:
        return ""
    sel = selector.strip()
    try:
        # ref:N — достаём элемент из window.__agentRefs
        ref = None
        if sel.startswith("ref:"):
            try:
                ref = int(sel[4:])
            except ValueError:
                ref = None
        elif sel.isdigit():
            ref = int(sel)
        if ref is not None:
            desc = page.evaluate(
                """(ref) => {
                    const el = (window.__agentRefs && window.__agentRefs[ref])
                        || document.querySelector('[data-agent-ref="'+ref+'"]');
                    if (!el) return '';
                    const tag = el.tagName ? el.tagName.toLowerCase() : '';
                    const role = el.getAttribute && el.getAttribute('role') || '';
                    const type = el.type || '';
                    const text = (el.innerText || el.textContent || el.value || el.placeholder || '')
                        .trim().replace(/\\s+/g,' ').slice(0, 100);
                    const aria = (el.getAttribute && el.getAttribute('aria-label')) || '';
                    const title = (el.getAttribute && el.getAttribute('title')) || '';
                    const id = el.id || '';
                    const name = el.name || '';
                    const href = (el.getAttribute && el.getAttribute('href')) || '';
                    const placeholder = el.placeholder || '';
                    const testId = (el.getAttribute && (el.getAttribute('data-testid')
                        || el.getAttribute('data-test-id')
                        || el.getAttribute('data-test')
                        || el.getAttribute('data-qa'))) || '';
                    const parts = [];
                    let head = tag + (type ? ':' + type : '');
                    if (role) head += '[role=' + role + ']';
                    parts.push(head);
                    if (text) parts.push('«' + text + '»');
                    if (testId) parts.push('data-testid="' + testId + '"');
                    if (id) parts.push('#' + id);
                    if (name) parts.push('name=' + name);
                    if (aria) parts.push('aria-label="' + aria.slice(0,80) + '"');
                    if (title) parts.push('title="' + title.slice(0,60) + '"');
                    if (placeholder) parts.push('placeholder="' + placeholder.slice(0,60) + '"');
                    if (href) parts.push('href=' + href.slice(0, 120));
                    // CSS-локатор как дополнительная подсказка
                    const css = id ? '#' + id
                        : (testId ? '[data-testid="' + testId + '"]'
                        : (name ? tag + '[name="' + name + '"]'
                        : (aria ? tag + '[aria-label="' + aria.slice(0,60) + '"]'
                        : (href ? tag + '[href="' + href.slice(0,100) + '"]'
                        : tag))));
                    parts.push('css=' + css);
                    return parts.join(' ');
                }""",
                ref,
            )
            if desc:
                return str(desc)[:400]
    except Exception:
        pass
    # Fallback: использовать сам селектор
    return sel[:120]

def execute_action(page: Page, action: Dict[str, Any], memory: AgentMemory) -> str:
    """Выполнить действие на странице. Возвращает текстовый результат."""
    from agent.actions.action_preflight import preflight_action
    from agent.browser.overlay_state import inspect_overlays

    set_current_agent_memory(memory)
    overlay = inspect_overlays(page)
    checked = preflight_action(
        page,
        memory,
        dict(action),
        has_overlay=bool(overlay.get("has_overlay")),
        allow_repeat=True,
    )
    if not checked.ok:
        return f"preflight_rejected:{checked.reason}"
    action = checked.action
    handlers = ActionHandlers(
        click=lambda selector, reason: _do_click(page, selector, reason),
        fill_form=lambda form_strategy: _fill_form_smart(page, form_strategy=form_strategy, memory=memory),
        type_text=lambda selector, value, form_strategy: _do_type(page, selector, value, form_strategy=form_strategy),
        scroll=lambda direction: _do_scroll(page, direction),
        hover=lambda selector: _do_hover(page, selector),
        close_modal=lambda selector: _do_close_modal(page, selector),
        select_option=lambda selector, value: _do_select_option(page, selector, value),
        press_key=lambda key: _do_press_key(page, key),
        upload_file=lambda selector, file_path: _do_upload_file(page, selector, file_path),
    )
    return execute_browser_action(page, action, memory, handlers)

def _find_element(page: Page, selector: str):
    """
    Поиск элемента по ref-id (мгновенный) с fallback по атрибутам.
    Self-healing: при успехе fallback кешируем селектор в memory._selector_heal_cache;
    при следующем вызове сначала пробуем кешированный вариант.

    Стратегии (по приоритету):
      0) кеш self-healing (если ref ранее найден через getByRole/getByText)
      1) ref:N — мгновенный поиск через data-agent-ref
      2) CSS/XPath/ID, семантика, getByRole/getByText
    """
    global _current_agent_memory
    if not selector:
        return None

    selector = selector.strip()

    # --- 0) Self-healing: попробовать кешированный селектор ---
    mem = _current_agent_memory
    if mem and getattr(mem, "_selector_heal_cache", None) and selector in mem._selector_heal_cache:
        c = mem._selector_heal_cache[selector]
        try:
            strat = c.get("strategy") or ""
            role = c.get("role")
            name = (c.get("name") or "").strip()
            if strat == "getByRole" and role and name:
                loc = page.get_by_role(role, name=name, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            elif strat == "getByLabel" and name:
                loc = page.get_by_label(name, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            elif strat == "getByText" and name:
                loc = page.get_by_text(name, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            elif strat == "getByPlaceholder" and name:
                loc = page.get_by_placeholder(name, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
        except Exception:
            pass

    # --- 1) ref:N — основной путь (мгновенный) ---
    ref_num = None
    if selector.startswith("ref:"):
        try:
            ref_num = int(selector[4:])
        except ValueError:
            pass
    elif selector.isdigit():
        ref_num = int(selector)

    if ref_num is not None:
        try:
            # Сначала пробуем через data-agent-ref (надёжный CSS-селектор)
            loc = page.locator(f'[data-agent-ref="{ref_num}"]').first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            pass
        try:
            # Fallback: через сохранённую JS-ссылку (если DOM изменился, но ссылка жива)
            exists = page.evaluate(
                f"() => !!window.__agentRefs && !!window.__agentRefs[{ref_num}] "
                f"&& !!window.__agentRefs[{ref_num}].isConnected"
            )
            if exists:
                loc = page.locator(f'[data-agent-ref="{ref_num}"]').first
                if loc.count() > 0:
                    return loc
        except Exception:
            pass
        LOG.debug(f"_find_element ref:{ref_num} not found, falling back to text strategies")

    safe_text = selector.replace('"', '\\"').replace("'", "\\'")[:100]

    # --- 1) Явные CSS/XPath/ID селекторы ---
    if selector.startswith(("#", ".", "[", "//")):
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            pass

    # --- 2) Семантические атрибуты (быстрые) ---
    attr_strategies = [
        f'[data-testid="{safe_text}"]',
        f'[data-testid*="{safe_text}"]',
        f'[aria-label="{safe_text}"]',
        f'[aria-label*="{safe_text}"]',
        f'[placeholder="{safe_text}"]',
        f'[name="{safe_text}"]',
        f'[title="{safe_text}"]',
    ]
    for css in attr_strategies:
        try:
            loc = page.locator(css).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue

    # --- 3) Playwright getBy* методы ---
    getby_strategies = [
        ("getByRole:button", "getByRole", "button", lambda: page.get_by_role("button", name=safe_text, exact=False).first),
        ("getByRole:link", "getByRole", "link", lambda: page.get_by_role("link", name=safe_text, exact=False).first),
        ("getByRole:tab", "getByRole", "tab", lambda: page.get_by_role("tab", name=safe_text, exact=False).first),
        ("getByRole:menuitem", "getByRole", "menuitem", lambda: page.get_by_role("menuitem", name=safe_text, exact=False).first),
        ("getByLabel", "getByLabel", None, lambda: page.get_by_label(safe_text, exact=False).first),
        ("getByPlaceholder", "getByPlaceholder", None, lambda: page.get_by_placeholder(safe_text, exact=False).first),
        ("getByText", "getByText", None, lambda: page.get_by_text(safe_text, exact=True).first),
    ]
    for _label, strat, role, get_loc in getby_strategies:
        try:
            loc = get_loc()
            if loc.count() > 0 and loc.is_visible():
                if mem and selector:
                    mem._selector_heal_cache[selector] = {"strategy": strat, "role": role, "name": safe_text}
                return loc
        except Exception:
            continue

    # --- 4) Текстовый has-text fallback ---
    text_strategies = [
        f'button:has-text("{safe_text}")',
        f'a:has-text("{safe_text}")',
        f'[role="button"]:has-text("{safe_text}")',
    ]
    for css in text_strategies:
        try:
            loc = page.locator(css).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue

    return None

def _do_click(page: Page, selector: str, reason: str = "") -> str:
    if not selector:
        return "no_selector"
    loc = _find_element(page, selector)
    if loc:
        try:
            # ПРОВЕРКА: кликаем только по внутренним ссылкам
            try:
                tag = loc.evaluate("el => el.tagName.toLowerCase()")
                if tag == "a":
                    href = loc.evaluate("el => el.getAttribute('href') || ''")
                    if href and not href.startswith("javascript:") and href != "#":
                        # Проверяем что это внутренняя ссылка (на том же домене)
                        is_internal = False
                        try:
                            current_url = page.url
                            if href.startswith("/") or href.startswith("./") or href.startswith("../") or not href.startswith("http"):
                                is_internal = True  # Относительный путь — всегда внутренний
                            elif href.startswith("http"):
                                # Абсолютный URL — проверяем домен
                                from urllib.parse import urlparse
                                current_domain = urlparse(current_url).netloc
                                href_domain = urlparse(href).netloc
                                is_internal = (href_domain == current_domain or href_domain == "")
                        except Exception:
                            is_internal = True  # При ошибке разрешаем клик

                        if not is_internal:
                            print(f"[Agent] ⚠️ Пропускаю внешнюю ссылку: {selector[:50]}")
                            return f"skipped_external_link: {selector[:50]}"
            except Exception:
                pass

            print(f"[Agent] КЛИК: {selector[:50]} ({reason[:30]})")
            scroll_to_center(loc, page)
            loc.click()
            print(f"[Agent] Клик выполнен: {selector[:50]}")
            return f"clicked: {selector[:50]}"
        except Exception as e:
            print(f"[Agent] ❌ Ошибка клика: {e}")
            return f"click_error: {e}"
    print(f"[Agent] ⚠️ Элемент не найден: {selector[:50]}")
    return f"not_found: {selector[:50]}"

def _fill_form_smart(page: Page, form_strategy: str = "happy", memory: Optional[AgentMemory] = None) -> str:
    """
    Умное заполнение формы: найти все поля формы и заполнить их за раз.
    Возвращает результат заполнения.
    """
    try:
        fields = detect_form_fields(page)
        if not fields:
            return "no_form_fields"

        filled_count = 0
        from agent.actions.form_strategies import detect_field_type, get_test_value
        from agent.browser.overlay_state import inspect_overlays

        for field in fields:
            if inspect_overlays(page).get("has_overlay"):
                return f"form_fill_paused_by_overlay: {filled_count} fields"
            selector = field.get("selector") or field.get("id") or field.get("name") or field.get("placeholder", "")
            if not selector:
                continue

            # Определяем тип поля для правильной проверки уже протестированных элементов
            field_type_str = field.get("type", "").lower()
            is_select = field_type_str == "select"

            # Проверяем, не заполняли ли уже это поле (используем правильный префикс)
            if memory:
                field_key_prefix = "select" if is_select else "type"
                field_key = f"{field_key_prefix}:{_norm_key(selector)}"
                if memory.is_element_tested(page.url, field_key):
                    continue

            # Определяем тип поля и генерируем значение
            field_type = detect_field_type(
                input_type=field.get("type", ""),
                placeholder=field.get("placeholder", ""),
                name=field.get("name", ""),
                aria_label=field.get("ariaLabel", ""),
            )

            # Для SELECT элементов используем специальную функцию
            if is_select:
                # Выбираем первую доступную опцию
                options = field.get("options", [])
                if not options:
                    continue  # Пропускаем если нет опций
                value = options[0]
                result = _do_select_option(page, selector, value)
                if "selected" in (result or "").lower():
                    filled_count += 1
                    if memory:
                        memory.record_page_element(page.url, f"select:{_norm_key(selector)}")
            else:
                # Для обычных input/textarea используем _do_type
                value = get_test_value(field_type, form_strategy)
                result = _do_type(page, selector, value, form_strategy)
                if "typed" in (result or "").lower():
                    filled_count += 1
                    if memory:
                        memory.record_page_element(page.url, f"type:{_norm_key(selector)}")

            time.sleep(0.2)  # Небольшая пауза между полями
            if inspect_overlays(page).get("has_overlay"):
                return f"form_fill_paused_by_overlay: {filled_count} fields"

        if filled_count > 0:
            return f"form_filled: {filled_count} fields"
        return "form_fill_failed"
    except Exception as e:
        return f"form_fill_error: {e}"

def _do_type(page: Page, selector: str, value: str, form_strategy: str = "happy") -> str:
    """
    Улучшенный ввод в поле с валидацией, умным подбором значения и проверкой результата.
    """
    # Smart value: если value пустой — подобрать по типу поля и стратегии
    if not value and selector:
        field_type = detect_field_type(placeholder=selector, name=selector, aria_label=selector)
        value = get_test_value(field_type, form_strategy)
    if not selector or not value:
        return "no_selector_or_value"

    loc = _find_element(page, selector)
    if not loc:
        return f"input_not_found: {selector[:50]}"
    if loc:
        try:
            print(f"[Agent] ВВОД: {selector[:50]} = {value[:30]}")
            scroll_to_center(loc, page)
            loc.click()
            loc.fill(value)
            # Верификация: значение действительно попало в поле
            try:
                current_val = (loc.input_value() or "").strip()
                val_stripped = (value or "").strip()
                if val_stripped and current_val != val_stripped and val_stripped not in current_val:
                    return f"typed_but_value_mismatch: expected '{val_stripped[:30]}', got '{current_val[:30]}'"
            except Exception:
                pass
            print(f"[Agent] ✅ Ввод выполнен: {value[:30]}")

            # Проверка валидации: есть ли сообщение об ошибке после ввода?
            # Используем loc.evaluate() чтобы работать напрямую с найденным элементом
            try:
                # Проверяем наличие сообщений об ошибке рядом с полем
                validation_error = loc.evaluate("""(input) => {
                    if (!input) return null;
                    // Ищем сообщения об ошибке: aria-invalid, aria-describedby, .error, .invalid
                    if (input.getAttribute('aria-invalid') === 'true') {
                        const descId = input.getAttribute('aria-describedby');
                        if (descId) {
                            const desc = document.getElementById(descId);
                            if (desc) return desc.textContent.trim().slice(0, 100);
                        }
                    }
                    // Проверяем родительский контейнер на наличие .error, .invalid
                    let parent = input.parentElement;
                    for (let i = 0; i < 3 && parent; i++) {
                        const errorEl = parent.querySelector('.error, .invalid, [class*="error"], [class*="invalid"]');
                        if (errorEl && errorEl.textContent) {
                            return errorEl.textContent.trim().slice(0, 100);
                        }
                        parent = parent.parentElement;
                    }
                    return null;
                }""")

                if validation_error:
                    return f"typed_with_validation_error: {value[:30]} -> {validation_error[:50]}"
            except Exception:
                pass

            return f"typed: {value[:30]} into {selector[:30]}"
        except Exception as e:
            return f"type_error: {e}"
    return f"input_not_found: {selector[:50]}"

def _do_scroll(page: Page, direction: str) -> str:
    try:
        if direction.lower() in ("down", "вниз", ""):
            page.evaluate(f"window.scrollBy(0, {SCROLL_PIXELS})")
            return "scrolled_down"
        elif direction.lower() in ("up", "вверх"):
            page.evaluate(f"window.scrollBy(0, -{SCROLL_PIXELS})")
            return "scrolled_up"
        else:
            loc = _find_element(page, direction)
            if loc:
                loc.scroll_into_view_if_needed()
                safe_highlight(loc, page, 0.3)
                return f"scrolled_to: {direction[:30]}"
            page.evaluate(f"window.scrollBy(0, {SCROLL_PIXELS})")
            return "scrolled_down"
    except Exception as e:
        return f"scroll_error: {e}"

def _do_hover(page: Page, selector: str) -> str:
    if not selector:
        return "no_selector"
    loc = _find_element(page, selector)
    if loc:
        try:
            safe_highlight(loc, page, 0.3)
            loc.hover()
            time.sleep(1.0)  # Ждём появления тултипа/дропдауна после ховера
            return f"hovered: {selector[:50]}"
        except Exception as e:
            return f"hover_error: {e}"
    return f"not_found: {selector[:50]}"

def _do_close_modal(page: Page, selector: str = "") -> str:
    """Close the active overlay and verify that its DOM state changed."""
    from agent.browser.overlay_state import close_active_overlay

    return close_active_overlay(page, selector)

def _do_select_option(page: Page, selector: str, value: str) -> str:
    """Выбрать опцию в дропдауне / select / listbox."""
    if not selector or not value:
        return "no_selector_or_value"

    # Стратегия 1: нативный <select>
    loc = _find_element(page, selector)
    if loc:
        try:
            scroll_to_center(loc, page)
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                loc.select_option(label=value)
                time.sleep(0.5)
                return f"selected_native: {value[:30]} in {selector[:30]}"
        except Exception:
            pass

    # Стратегия 2: кастомный дропдаун — кликнуть по пункту с текстом value
    try:
        option_selectors = [
            f'[role="option"]:has-text("{value}")',
            f'[role="menuitem"]:has-text("{value}")',
            f'li:has-text("{value}")',
            f'.dropdown-item:has-text("{value}")',
            f'[class*="option"]:has-text("{value}")',
            f'[class*="item"]:has-text("{value}")',
        ]
        for os_sel in option_selectors:
            try:
                opt = page.locator(os_sel).first
                if opt.count() > 0 and opt.is_visible():
                    safe_highlight(opt, page, 0.3)
                    highlight_and_click(opt, page, description=f"Выбираю: {value[:20]}")
                    time.sleep(0.5)
                    return f"selected_custom: {value[:30]}"
            except Exception:
                continue
    except Exception:
        pass

    return f"select_not_found: {selector[:30]} / {value[:30]}"

def _do_upload_file(page: Page, selector: str, file_path: str) -> str:
    """Загрузить файл в input[type=file] по селектору (ref:N или иной)."""
    if not file_path or not os.path.isfile(file_path):
        return f"upload_error: file not found {file_path[:50]}"
    loc = _find_element(page, selector)
    if not loc:
        return f"upload_error: element not_found {selector[:30]}"
    try:
        loc.set_input_files(file_path)
        return f"uploaded: {os.path.basename(file_path)[:40]}"
    except Exception as e:
        return f"upload_error: {e}"

def _do_press_key(page: Page, key: str) -> str:
    """Нажать клавишу (Escape, Enter, Tab и т.д.)."""
    try:
        page.keyboard.press(key)
        time.sleep(0.5)
        return f"key_pressed: {key}"
    except Exception as e:
        return f"key_error: {e}"

def _do_auth_login(page: Page, auth_url: str, username: str, password: str, submit_selector: str) -> bool:
    """Выполнить вход на auth_url (заполнить логин/пароль, нажать кнопку). Возвращает True при успехе."""
    if not auth_url or not username or not password:
        return False
    try:
        page.goto(auth_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_load_state("domcontentloaded", timeout=5000)
        # Ищем поля логина и пароля по type, name, placeholder
        login_sel = 'input[type="email"], input[type="text"]:not([type="search"]), input[name*="login" i], input[name*="user" i], input[name*="email" i], input[placeholder*="логин" i], input[placeholder*="email" i], input[id*="login" i], input[id*="email" i]'
        pass_sel = 'input[type="password"]'
        try:
            page.locator(login_sel).first.fill(username, timeout=5000)
            page.locator(pass_sel).first.fill(password, timeout=5000)
        except Exception:
            # Пробуем по первому text/email и второму password
            inputs = page.query_selector_all("input[type='text'], input[type='email'], input:not([type])")
            for inp in inputs:
                if inp.get_attribute("type") == "password":
                    continue
                inp.fill(username)
                break
            pw = page.query_selector("input[type='password']")
            if pw:
                pw.fill(password)
        # Кнопка отправки
        if submit_selector:
            try:
                page.locator(submit_selector).first.click(timeout=3000)
            except Exception:
                page.get_by_role("button", name=submit_selector).first.click(timeout=3000)
        else:
            page.locator('button[type="submit"], input[type="submit"], button:has-text("Войти"), button:has-text("Вход"), button:has-text("Login"), button:has-text("Sign in")').first.click(timeout=3000)
        time.sleep(2)
        print("[Agent] Автологин выполнен")
        return True
    except Exception as e:
        LOG.warning("Автологин не удался: %s", e)
        return False

def try_accept_cookie_banner(page: Page) -> bool:
    """Если на странице баннер cookies/согласия — кликнуть по кнопке принять. Возвращает True если кликнули."""
    try:
        info = detect_cookie_banner(page)
        if not info or not info.get("text"):
            return False
        text = info.get("text", "").strip()
        if not text:
            return False
        loc = _find_element(page, text)
        if loc:
            safe_highlight(loc, page, 0.3)
            highlight_and_click(loc, page, description="Принять")
            time.sleep(1.0)
            print(f"[Agent] Закрыт баннер: {text[:50]}")
            return True
    except Exception as e:
        print(f"[Agent] Ошибка закрытия баннера: {e}")
    return False

def _inject_all(page: Page):
    """Инжектировать все визуальные элементы."""
    inject_cursor(page)
    inject_llm_overlay(page)

__all__ = [
    "_do_auth_login",
    "_do_close_modal",
    "_find_element",
    "_inject_all",
    "describe_element_for_report",
    "execute_action",
    "set_current_agent_memory",
    "take_screenshot_b64",
    "try_accept_cookie_banner",
]
