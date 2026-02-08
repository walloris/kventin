"""
Чеклист проверок при загрузке страницы. Агент идёт строго по пунктам, с паузой между шагами.
"""
import time
from typing import List, Dict, Any, Optional, Callable, Tuple

from playwright.sync_api import Page

from config import CHECKLIST_STEP_DELAY_MS
from src.page_analyzer import get_dom_summary, _should_ignore_console, _should_ignore_network


def _check_dom_loaded(page: Page) -> Tuple[bool, str]:
    """Проверка: DOM загружен (есть body, не пустой)."""
    try:
        ready = page.evaluate("() => document.readyState !== 'loading' && document.body != null")
        has_content = page.evaluate("() => document.body && document.body.innerHTML.length > 100")
        if ready and has_content:
            return True, "DOM загружен, контент присутствует"
        if ready:
            return True, "DOM загружен, контент минимальный"
        return False, "DOM ещё загружается"
    except Exception as e:
        return False, str(e)


def _check_console_errors(
    page: Page,
    console_log: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Проверка: нет критичных ошибок в консоли (игнорируем 404, флаки)."""
    errors = [c for c in console_log if c.get("type") == "error"]
    critical = [e for e in errors if not _should_ignore_console(e.get("text", ""))]
    if not errors:
        return True, "Ошибок в консоли нет"
    if not critical:
        return True, f"В консоли {len(errors)} ошибок (все в списке игнора: 404, флаки и т.д.)"
    return False, f"Критичные ошибки в консоли: {len(critical)}. Примеры: " + "; ".join(
        (e.get("text", "")[:80] for e in critical[:3])
    )


def _check_network_failures(
    page: Page,
    network_failures: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Проверка: сетевые ошибки (игнорируем 404 и известные паттерны)."""
    critical = [
        n for n in network_failures
        if not _should_ignore_network(n.get("url", ""), n.get("status"))
    ]
    if not network_failures:
        return True, "Неуспешных сетевых запросов нет"
    if not critical:
        return True, f"Неуспешных запросов: {len(network_failures)} (все в списке игнора, напр. 404)"
    return False, f"Критичные сетевые ошибки: {len(critical)}. Примеры: " + "; ".join(
        f"{n.get('status')} {n.get('url', '')[:60]}" for n in critical[:3])


def _check_main_content(page: Page) -> Tuple[bool, str]:
    """Проверка: наличие основного контента (заголовок, main или контентная область)."""
    try:
        has_h1 = page.locator("h1").count() > 0
        has_main = page.locator("main, [role='main'], #main, .main, #content, .content").count() > 0
        has_body_text = page.evaluate(
            "() => (document.body && document.body.innerText && document.body.innerText.trim().length > 50) || false"
        )
        if has_h1 or has_main or has_body_text:
            return True, "Основной контент найден (h1/main/текст)"
        return True, "Основной контент не обнаружен (возможно SPA или пустая страница)"
    except Exception as e:
        return False, str(e)


def _check_buttons(page: Page) -> Tuple[bool, str]:
    """Проверка: наличие кликабельных кнопок."""
    try:
        count = page.locator("button, [role='button'], input[type='submit'], input[type='button']").count()
        if count > 0:
            return True, f"Кнопок/кнопкоподобных элементов: {count}"
        return True, "Кнопок не найдено"
    except Exception as e:
        return False, str(e)


def _check_links(page: Page) -> Tuple[bool, str]:
    """Проверка: наличие ссылок."""
    try:
        count = page.locator("a[href]").count()
        if count > 0:
            return True, f"Ссылок: {count}"
        return True, "Ссылок не найдено"
    except Exception as e:
        return False, str(e)


def _check_forms(page: Page) -> Tuple[bool, str]:
    """Проверка: наличие форм (если есть)."""
    try:
        count = page.locator("form").count()
        if count > 0:
            return True, f"Форм: {count}"
        return True, "Форм не найдено"
    except Exception as e:
        return False, str(e)


def build_checklist() -> List[Dict[str, Any]]:
    """
    Список пунктов чеклиста: id, title, check_function.
    check_function(page, console_log, network_failures) -> (ok, detail).
    """
    return [
        {"id": "dom", "title": "Загрузка DOM", "check": lambda p, cl, nf: _check_dom_loaded(p)},
        {"id": "console", "title": "Ошибки в консоли (исключая 404/флаки)", "check": lambda p, cl, nf: _check_console_errors(p, cl)},
        {"id": "network", "title": "Сетевые ответы (исключая 404/игнор)", "check": lambda p, cl, nf: _check_network_failures(p, nf)},
        {"id": "content", "title": "Наличие основного контента", "check": lambda p, cl, nf: _check_main_content(p)},
        {"id": "buttons", "title": "Проверка кнопок", "check": lambda p, cl, nf: _check_buttons(p)},
        {"id": "links", "title": "Проверка ссылок", "check": lambda p, cl, nf: _check_links(p)},
        {"id": "forms", "title": "Проверка форм", "check": lambda p, cl, nf: _check_forms(p)},
    ]


def run_checklist(
    page: Page,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
    *,
    step_delay_ms: Optional[int] = None,
    on_step: Optional[Callable[..., None]] = None,
) -> List[Dict[str, Any]]:
    """
    Выполнить чеклист по порядку, с паузой между шагами.
    Возвращает список результатов: [{ "id", "title", "ok", "detail" }, ...].
    on_step(step_id, ok, detail, step_index, total) вызывается после каждого шага.
    """
    step_delay_ms = step_delay_ms or CHECKLIST_STEP_DELAY_MS
    checklist = build_checklist()
    total = len(checklist)
    results = []
    for i, item in enumerate(checklist):
        try:
            ok, detail = item["check"](page, console_log, network_failures)
        except Exception as e:
            ok, detail = False, str(e)
        results.append({
            "id": item["id"],
            "title": item["title"],
            "ok": ok,
            "detail": detail,
        })
        if on_step:
            on_step(item["id"], ok, detail, i + 1, total)
        time.sleep(step_delay_ms / 1000.0)
    return results


def checklist_results_to_context(results: List[Dict[str, Any]]) -> str:
    """Собрать текстовый контекст из результатов чеклиста для GigaChat."""
    lines = ["Чеклист проверок:"]
    for r in results:
        status = "✅" if r.get("ok") else "❌"
        lines.append(f"  {status} {r.get('title', '')}: {r.get('detail', '')}")
    return "\n".join(lines)
