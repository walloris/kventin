"""Page object registry for the browser-testing agent.

The agent does not generate Python source files at runtime. Instead it keeps a
session-scoped, structured page object model keyed by normalized URL and UI
state. The model stores named locators that can be reused or updated whenever
the agent returns to the same page/state.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.actions.locators import normalize_text_for_key, url_pattern


def _slug(value: str, fallback: str = "element") -> str:
    value = normalize_text_for_key(value, max_len=80)
    value = re.sub(r"[^a-zа-яё0-9]+", "_", value, flags=re.I).strip("_")
    return value or fallback


def _short_hash(value: str) -> str:
    return hashlib.sha1((value or "").encode("utf-8", errors="ignore")).hexdigest()[:10]


def capture_page_state(page: Any, *, dom_summary: str = "", overlay_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Capture the current URL + visible transient UI/focus state."""
    current_url = ""
    try:
        current_url = page.url or ""
    except Exception:
        pass

    overlay_info = overlay_info or {}
    overlay_types = sorted(
        str(item.get("type", "overlay"))
        for item in (overlay_info.get("overlays") or [])
        if isinstance(item, dict)
    )

    focus_signature = ""
    try:
        focus_signature = page.evaluate(
            """() => {
                const el = document.activeElement;
                if (!el || el === document.body) return "";
                const tag = (el.tagName || "").toLowerCase();
                const role = el.getAttribute("role") || "";
                const name = el.getAttribute("aria-label") || el.getAttribute("name") || el.id || "";
                return [tag, role, name].filter(Boolean).join(":").slice(0, 120);
            }"""
        ) or ""
    except Exception:
        focus_signature = ""

    dom_hash = _short_hash(dom_summary or "")
    state_parts = ["base"]
    if overlay_types:
        state_parts.append("overlay:" + ",".join(overlay_types))
    if focus_signature:
        state_parts.append("focus:" + focus_signature)

    state_name = "|".join(state_parts)
    state_key = f"{url_pattern(current_url)}::{_short_hash(state_name + dom_hash)}"
    return {
        "url": current_url,
        "url_pattern": url_pattern(current_url),
        "state_key": state_key,
        "state_name": state_name,
        "dom_hash": dom_hash,
        "overlay_types": overlay_types,
        "focus": focus_signature,
    }


def build_named_locators(candidates: List[Any], ref_meta: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """Build readable locator names from current action candidates."""
    locators: Dict[str, Dict[str, str]] = {}
    used_names: Dict[str, int] = {}

    for candidate in candidates or []:
        ref = getattr(candidate, "selector", "") or getattr(candidate, "ref", "") or ""
        if not ref:
            continue
        ref_id = ref[4:] if ref.startswith("ref:") else ref
        label = (
            getattr(candidate, "label", "")
            or getattr(candidate, "text", "")
            or getattr(candidate, "reason", "")
            or ref
        )
        action = getattr(candidate, "action", "") or getattr(candidate, "kind", "") or "element"
        stable_key = getattr(candidate, "stable_key", "") or getattr(candidate, "canonical_locator", "") or ref_meta.get(ref_id, "")
        base_name = f"{_slug(action, 'element')}_{_slug(label, 'target')}"
        counter = used_names.get(base_name, 0) + 1
        used_names[base_name] = counter
        name = base_name if counter == 1 else f"{base_name}_{counter}"
        locators[name] = {
            "selector": ref,
            "stable_key": str(stable_key or ""),
            "label": str(label or "")[:160],
            "action": str(action or ""),
        }

    return locators


@dataclass
class PageObjectState:
    state_key: str
    state_name: str
    dom_hash: str
    overlay_types: List[str] = field(default_factory=list)
    focus: str = ""
    locators: Dict[str, Dict[str, str]] = field(default_factory=dict)
    visits: int = 0
    must_close: bool = False
    closed: bool = False


@dataclass
class PageObject:
    url_pattern: str
    example_url: str = ""
    locators: Dict[str, Dict[str, str]] = field(default_factory=dict)
    states: Dict[str, PageObjectState] = field(default_factory=dict)
    visits: int = 0

    def update(self, state: Dict[str, Any], locators: Dict[str, Dict[str, str]]) -> PageObjectState:
        self.example_url = self.example_url or state.get("url", "")
        self.visits += 1
        self.locators.update(locators)

        state_key = state.get("state_key", "")
        page_state = self.states.get(state_key)
        if page_state is None:
            page_state = PageObjectState(
                state_key=state_key,
                state_name=state.get("state_name", "base"),
                dom_hash=state.get("dom_hash", ""),
                overlay_types=list(state.get("overlay_types") or []),
                focus=state.get("focus", ""),
                must_close=bool(state.get("overlay_types") or state.get("focus")),
            )
            self.states[state_key] = page_state
        page_state.visits += 1
        page_state.locators.update(locators)
        return page_state


class PageObjectRegistry:
    def __init__(self) -> None:
        self.pages: Dict[str, PageObject] = {}
        self.current_state_key: str = ""
        self.open_transient_states: List[str] = []

    def update_from_observation(
        self,
        page: Any,
        *,
        dom_summary: str,
        overlay_info: Dict[str, Any],
        candidates: List[Any],
        ref_meta: Dict[str, str],
    ) -> PageObjectState:
        state = capture_page_state(page, dom_summary=dom_summary, overlay_info=overlay_info)
        url_pat = state.get("url_pattern", "")
        page_object = self.pages.setdefault(url_pat, PageObject(url_pattern=url_pat, example_url=state.get("url", "")))
        locators = build_named_locators(candidates, ref_meta)
        page_state = page_object.update(state, locators)
        self.current_state_key = page_state.state_key
        if page_state.must_close and page_state.state_key not in self.open_transient_states:
            self.open_transient_states.append(page_state.state_key)
        return page_state

    def mark_current_closed(self) -> None:
        state_key = self.current_state_key
        for page_object in self.pages.values():
            state = page_object.states.get(state_key)
            if state:
                state.closed = True
        self.open_transient_states = [key for key in self.open_transient_states if key != state_key]

    def has_open_transient_states(self) -> bool:
        return bool(self.open_transient_states)


__all__ = [
    "PageObject",
    "PageObjectRegistry",
    "PageObjectState",
    "build_named_locators",
    "capture_page_state",
]
