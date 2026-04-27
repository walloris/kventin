"""
Резолверы элементов: ref:N → стабильный ключ / канонический локатор + обогащение action.

Раньше всё это жило в src/agent.py. Вынесено сюда, чтобы:
- сократить размер agent.py;
- переиспользовать в других модулях (defect pipeline, отчёт, тестовый рынок) без
  риска циклических импортов.

Зависит только от src/locators.py.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.locators import url_pattern as _url_pattern


def norm_key(s: str, max_len: int = 80) -> str:
    """Единый ключ для сравнения: без повторов из-за пробелов/регистра."""
    if not s:
        return ""
    return s.strip().lower().replace("\n", " ").replace("\r", " ")[:max_len]


def resolve_stable_key(page, selector: str) -> str:
    """
    Получить стабильный логический ключ элемента по его selector (обычно ref:N).

    Стратегия:
    1. Если selector — ref:N или просто число, тащим из window.__agentRefMeta.
    2. Иначе возвращаем сам selector в нормализованном виде (как fallback).
    """
    if not selector:
        return ""
    sel = selector.strip()
    ref_num: Optional[int] = None
    if sel.startswith("ref:"):
        try:
            ref_num = int(sel[4:])
        except ValueError:
            ref_num = None
    elif sel.isdigit():
        ref_num = int(sel)
    if ref_num is not None and page is not None:
        try:
            key = page.evaluate(
                "(ref) => (window.__agentRefMeta && window.__agentRefMeta[ref]) || ''",
                ref_num,
            )
            if key:
                return str(key)
        except Exception:
            pass
    return norm_key(sel)


def resolve_canonical_locator(page, selector: str) -> str:
    """
    Получить «канонический» селектор элемента (то, что не стыдно записать в дефект).

    Стратегия:
    1. ref:N → window.__agentLocator[N] (его пишет page_analyzer.py).
    2. Иначе пробуем достать атрибуты у самого элемента и сами строим селектор.
    3. В последнем варианте — возвращаем исходный selector как есть.
    """
    if not selector:
        return ""
    sel = selector.strip()
    ref_num: Optional[int] = None
    if sel.startswith("ref:"):
        try:
            ref_num = int(sel[4:])
        except ValueError:
            ref_num = None
    elif sel.isdigit():
        ref_num = int(sel)
    if ref_num is not None and page is not None:
        try:
            loc = page.evaluate(
                "(ref) => (window.__agentLocator && window.__agentLocator[ref]) || ''",
                ref_num,
            )
            if loc:
                return str(loc)
        except Exception:
            pass
    if page is not None:
        try:
            l = page.locator(sel).first
            attrs = l.evaluate(
                "(el) => ({"
                "tag: el.tagName ? el.tagName.toLowerCase() : '',"
                "tid: el.getAttribute && (el.getAttribute('data-testid')||el.getAttribute('data-test-id')||el.getAttribute('data-test')||el.getAttribute('data-qa')||''),"
                "id: el.id || '',"
                "name: el.name || '',"
                "aria: (el.getAttribute && el.getAttribute('aria-label')) || '',"
                "ph: el.placeholder || '',"
                "txt: ((el.innerText||el.textContent||'').replace(/\\s+/g,' ').trim().slice(0,60))"
                "})",
                timeout=500,
            )
            if attrs:
                if attrs.get("tid"):
                    return f'[data-testid="{attrs["tid"]}"]'
                if attrs.get("id"):
                    return f'#{attrs["id"]}'
                tag = attrs.get("tag") or ""
                if attrs.get("name"):
                    return f'{tag}[name="{attrs["name"]}"]'
                if attrs.get("aria"):
                    return f'{tag}[aria-label="{attrs["aria"][:80]}"]'
                if tag in ("button", "a") and attrs.get("txt"):
                    role = "button" if tag == "button" else "link"
                    return f'role={role}[name="{attrs["txt"]}"]'
                if attrs.get("ph"):
                    return f'{tag}[placeholder="{attrs["ph"][:60]}"]'
                if tag and attrs.get("txt") and len(attrs["txt"]) <= 40:
                    return f'{tag}:has-text("{attrs["txt"]}")'
                if tag:
                    return tag
        except Exception:
            pass
    return sel


def enrich_action(page, memory, action: Dict[str, Any]) -> Dict[str, Any]:
    """Дописать в action служебные поля _stable_key, _canonical_locator, _url_pattern.

    Вызывать ПЕРЕД любой проверкой повтора и перед add_action.
    """
    if not isinstance(action, dict):
        return action
    sel = (action.get("selector") or "").strip()
    if sel and not action.get("_stable_key"):
        try:
            action["_stable_key"] = resolve_stable_key(page, sel)
        except Exception:
            action["_stable_key"] = ""
    if sel and not action.get("_canonical_locator"):
        try:
            action["_canonical_locator"] = resolve_canonical_locator(page, sel)
        except Exception:
            action["_canonical_locator"] = ""
    if not action.get("_url_pattern"):
        try:
            url = page.url if page and not page.is_closed() else ""
        except Exception:
            url = ""
        action["_url_pattern"] = (
            _url_pattern(url)
            if url
            else (getattr(memory, "current_url_pattern", "") if memory else "")
        )
    return action


__all__ = [
    "norm_key",
    "resolve_stable_key",
    "resolve_canonical_locator",
    "enrich_action",
]
