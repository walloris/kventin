"""
Утилиты локаторов и URL-паттернов для надёжной памяти агента.

Цели:
- стабильный логический ключ элемента (stable_key) — НЕ привязан к ref:N,
  переживает перерисовку DOM, одинаковый для одного и того же логического
  элемента на разных загрузках страницы;
- нормализация URL до паттерна (убрать query/hash, заменить числовые/UUID
  сегменты на :id) — две страницы /users/123 и /users/456 считаем одной;
- детектор циклических повторов в последовательности действий
  (A,B,A,B / A,B,C,A,B,C — типичные «зависания» агента).
"""
import re
from typing import List, Optional, Sequence
from urllib.parse import urlparse


# ============================================================
# URL pattern
# ============================================================

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_LONG_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)


def url_pattern(url: str) -> str:
    """
    Привести URL к канонической форме страницы:
      https://app.example.com/users/123/orders/abcd-uuid-...?tab=items
    →
      https://app.example.com/users/:id/orders/:uuid

    Используется как ключ для памяти/бюджетов «на странице».
    Без query/hash. Числовые сегменты и UUID/длинные хеши заменяются
    на :id/:uuid/:hex.
    """
    if not url:
        return ""
    try:
        p = urlparse(url)
    except Exception:
        return url
    if not p.scheme:
        return url
    parts = []
    for seg in (p.path or "/").split("/"):
        if not seg:
            parts.append(seg)
            continue
        if seg.isdigit():
            parts.append(":id")
        elif _UUID_RE.match(seg):
            parts.append(":uuid")
        elif _HEX_LONG_RE.match(seg):
            parts.append(":hex")
        else:
            parts.append(seg)
    path = "/".join(parts) or "/"
    return f"{p.scheme}://{p.netloc}{path}"


# ============================================================
# Stable key (fallback на питоне, основной — в JS, см. page_analyzer.py)
# ============================================================

def normalize_text_for_key(s: str, max_len: int = 60) -> str:
    """Унифицированная нормализация текста для ключа: lower, схлопнуть пробелы."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().lower()[:max_len]


def stable_key_from_attrs(
    *,
    tag: str = "",
    testid: str = "",
    el_id: str = "",
    name: str = "",
    aria_label: str = "",
    role: str = "",
    text: str = "",
    placeholder: str = "",
    classes: Optional[Sequence[str]] = None,
) -> str:
    """
    Построить стабильный ключ элемента по его атрибутам (приоритет от самых
    надёжных к менее).

    Должен быть одинаковым с JS-реализацией в page_analyzer.py — поэтому правки
    делать в обоих местах синхронно.
    """
    tag = (tag or "").lower()
    if testid:
        return f"tid:{testid}"
    if el_id:
        return f"id:{el_id}"
    if name:
        return f"name:{tag}:{name}"
    if aria_label:
        return f"aria:{tag}:{normalize_text_for_key(aria_label)}"
    txt = normalize_text_for_key(text)
    if role and txt:
        return f"role:{role}:{txt}"
    if txt:
        return f"text:{tag}:{txt}"
    if placeholder:
        return f"ph:{tag}:{normalize_text_for_key(placeholder)}"
    if classes:
        cls = ".".join(list(classes)[:2])
        return f"css:{tag}.{cls}" if cls else f"css:{tag}"
    return f"css:{tag}" if tag else ""


# ============================================================
# Anti-loop pattern detector
# ============================================================

def detect_repeating_pattern(seq: List[str], max_period: int = 4) -> int:
    """
    Найти повторяющийся хвостовой паттерн (период 1..max_period).
    Возвращает длину периода или 0, если повтора нет.

    seq:    [..., A, B, A, B]            → 2
    seq:    [..., A, B, C, A, B, C]      → 3
    seq:    [..., A, A]                  → 1
    seq:    [..., A, B, C]               → 0
    """
    if not seq:
        return 0
    n = len(seq)
    for period in range(1, max_period + 1):
        if n < period * 2:
            continue
        head = seq[-period * 2 : -period]
        tail = seq[-period:]
        if head == tail:
            return period
    return 0


__all__ = [
    "url_pattern",
    "normalize_text_for_key",
    "stable_key_from_attrs",
    "detect_repeating_pattern",
]
