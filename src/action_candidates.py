"""Action candidate model for the browser-testing agent.

Instead of asking the LLM to invent selectors, the agent collects a bounded list
of currently actionable candidates and asks the LLM to pick one. The candidate
keeps a stable key, selector, action, label, and intent so local policy and
preflight can reason about it deterministically.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from src.element_resolver import norm_key
from src.locators import url_pattern as _url_pattern


@dataclass
class ActionCandidate:
    id: str
    action: str
    selector: str = ""
    value: str = ""
    label: str = ""
    kind: str = ""
    priority: int = 50
    stable_key: str = ""
    canonical_locator: str = ""
    reason: str = ""
    test_goal: str = ""
    expected_outcome: str = ""
    score: float = 0.0
    risk_flags: List[str] = field(default_factory=list)

    def as_action(self) -> Dict[str, Any]:
        action = {
            "action": self.action,
            "selector": self.selector,
            "value": self.value,
            "reason": self.reason or self.label or self.kind,
            "test_goal": self.test_goal,
            "expected_outcome": self.expected_outcome,
            "_stable_key": self.stable_key,
            "_canonical_locator": self.canonical_locator,
        }
        return {k: v for k, v in action.items() if v not in ("", None, [])}

    def to_prompt_line(self) -> str:
        flags = f" flags={','.join(self.risk_flags)}" if self.risk_flags else ""
        value = f" value={self.value[:40]!r}" if self.value else ""
        return (
            f"{self.id}: {self.action} {self.selector} "
            f"label={self.label[:80]!r}{value} score={self.score:.1f}{flags}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _safe_page_url(page: Any) -> str:
    try:
        if page and not page.is_closed():
            return page.url or ""
    except Exception:
        pass
    return ""


def _test_value_for_input(label: str) -> str:
    try:
        from src.form_strategies import detect_field_type, get_test_value

        return get_test_value(detect_field_type(placeholder=label, name=label), "happy")
    except Exception:
        return "test"


def _candidate_from_raw(raw: Dict[str, Any], idx: int, url_pat: str) -> ActionCandidate:
    ref = str(raw.get("ref") or "").strip()
    kind = str(raw.get("type") or "").strip()
    label = str(raw.get("text") or "").strip()
    stable_key = str(raw.get("stable_key") or "").strip()
    canonical = str(raw.get("canonical_locator") or "").strip()

    if kind in ("input", "textarea"):
        value = _test_value_for_input(label)
        return ActionCandidate(
            id=f"c{idx}",
            action="type",
            selector=ref,
            value=value,
            label=label,
            kind=kind,
            priority=20,
            stable_key=stable_key,
            canonical_locator=canonical,
            reason=f"Заполнить поле {label[:40] or ref}",
            test_goal=f"Проверить ввод в поле {label[:60] or ref}",
            expected_outcome="Поле принимает значение и не показывает неожиданную ошибку",
        )
    if kind == "select":
        first_opt = (label.split(",")[0] if label else "").strip()
        return ActionCandidate(
            id=f"c{idx}",
            action="select_option",
            selector=ref,
            value=first_opt,
            label=label,
            kind=kind,
            priority=25,
            stable_key=stable_key,
            canonical_locator=canonical,
            reason=f"Выбрать опцию {first_opt[:40] or label[:40]}",
            test_goal="Проверить выбор значения в списке",
            expected_outcome="Выбранная опция применяется",
        )
    if kind == "file":
        return ActionCandidate(
            id=f"c{idx}",
            action="upload_file",
            selector=ref,
            label=label or "file input",
            kind=kind,
            priority=30,
            stable_key=stable_key,
            canonical_locator=canonical,
            reason="Проверить загрузку файла",
            test_goal="Загрузить тестовый файл",
            expected_outcome="Файл принят или показана понятная валидация",
        )

    action = "click"
    if kind == "hover":
        action = "hover"
    return ActionCandidate(
        id=f"c{idx}",
        action=action,
        selector=ref,
        label=label,
        kind=kind or "click",
        priority=10 if kind in ("button", "tab") else 35,
        stable_key=stable_key,
        canonical_locator=canonical,
        reason=f"Проверить элемент {label[:50] or ref}",
        test_goal=f"Проверить реакцию элемента {label[:60] or ref}",
        expected_outcome="Элемент реагирует без ошибок, зависаний и неожиданной навигации",
    )


def collect_action_candidates(
    page: Any,
    memory: Any = None,
    *,
    has_overlay: bool = False,
    overlay_info: Optional[Dict[str, Any]] = None,
    max_candidates: int = 80,
) -> List[ActionCandidate]:
    if not page:
        return []
    try:
        if page.is_closed():
            return []
    except Exception:
        return []

    candidates: List[ActionCandidate] = []
    if has_overlay and memory is not None and not getattr(memory, "ignore_overlay", False):
        close_selector = ""
        for ov in (overlay_info or {}).get("overlays", []) or []:
            close_selector = ov.get("close_selector") or close_selector
            if close_selector:
                break
        candidates.append(
            ActionCandidate(
                id="c0",
                action="close_modal",
                selector=close_selector,
                label="Закрыть активный оверлей",
                kind="overlay",
                priority=5,
                reason="Закрыть или проверить активный оверлей",
                test_goal="Завершить работу с активным оверлеем",
                expected_outcome="Оверлей закрывается и основной экран снова доступен",
            )
        )

    try:
        raw_items = page.evaluate(
            """() => {
                const out = [];
                const meta = window.__agentRefMeta || {};
                const locators = window.__agentLocator || {};
                const isAgent = (el) => {
                    let cur = el;
                    while (cur && cur !== document.body) {
                        if (cur.hasAttribute && cur.hasAttribute('data-agent-host')) return true;
                        cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
                    }
                    return false;
                };
                const hiddenByDomState = (el) => {
                    let cur = el;
                    while (cur && cur.nodeType === 1) {
                        if (cur.hidden || cur.inert) return true;
                        if (cur.getAttribute && (cur.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return true;
                        cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
                    }
                    return false;
                };
                const visible = (el) => {
                    if (!el || isAgent(el) || hiddenByDomState(el)) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) return false;
                    if (r.bottom <= 0 || r.top >= window.innerHeight || r.right <= 0 || r.left >= window.innerWidth) return false;
                    let cur = el;
                    while (cur && cur !== document.body) {
                        const s = getComputedStyle(cur);
                        if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') === 0) return false;
                        cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
                    }
                    return true;
                };
                const add = (el, type, text) => {
                    if (!visible(el)) return;
                    if (el.disabled || (el.getAttribute && el.getAttribute('aria-disabled') === 'true')) return;
                    const ref = el.getAttribute('data-agent-ref');
                    if (!ref) return;
                    out.push({
                        ref: 'ref:' + ref,
                        type,
                        text: (text || el.innerText || el.textContent || el.value || el.placeholder || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 100),
                        stable_key: meta[ref] || '',
                        canonical_locator: locators[ref] || '',
                    });
                };
                document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"]').forEach(el => add(el, 'button'));
                document.querySelectorAll('a[href]').forEach(el => {
                    const href = el.getAttribute('href') || '';
                    if (href.startsWith('javascript:') || href === '#') return;
                    if (href.startsWith('http')) {
                        try {
                            const url = new URL(href, window.location.href);
                            if (url.hostname !== window.location.hostname && url.hostname !== '') return;
                        } catch(e) { return; }
                    }
                    add(el, 'link');
                });
                document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea').forEach(el => {
                    if ((el.type || '').toLowerCase() === 'file') add(el, 'file', 'file input');
                    else add(el, el.tagName && el.tagName.toLowerCase() === 'textarea' ? 'textarea' : 'input');
                });
                document.querySelectorAll('select').forEach(el => {
                    const opts = Array.from(el.options || []).slice(0, 5).map(o => o.text.trim()).filter(Boolean).join(',');
                    add(el, 'select', opts);
                });
                document.querySelectorAll('[role="tab"]').forEach(el => add(el, 'tab'));
                document.querySelectorAll('[role="menuitem"]').forEach(el => add(el, 'menuitem'));
                return out;
            }"""
        ) or []
    except Exception:
        raw_items = []

    seen = set((c.action, c.selector, c.value) for c in candidates)
    url_pat = _url_pattern(_safe_page_url(page))
    next_idx = len(candidates)
    for raw in raw_items[:max_candidates]:
        cand = _candidate_from_raw(raw, next_idx, url_pat)
        next_idx += 1
        key = (cand.action, cand.selector, cand.value)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(cand)

    return candidates[:max_candidates]


def render_candidates_for_prompt(candidates: Iterable[ActionCandidate], limit: int = 20) -> str:
    lines = [c.to_prompt_line() for c in list(candidates)[:limit]]
    if not lines:
        return "Нет валидных кандидатов."
    return "\n".join(lines)


def candidate_by_id(candidates: Iterable[ActionCandidate], candidate_id: str) -> Optional[ActionCandidate]:
    wanted = norm_key(candidate_id, max_len=40)
    for cand in candidates:
        if norm_key(cand.id, max_len=40) == wanted:
            return cand
    return None


def candidates_to_dicts(candidates: Iterable[ActionCandidate]) -> List[Dict[str, Any]]:
    return [c.to_dict() for c in candidates]


__all__ = [
    "ActionCandidate",
    "candidate_by_id",
    "candidates_to_dicts",
    "collect_action_candidates",
    "render_candidates_for_prompt",
]
