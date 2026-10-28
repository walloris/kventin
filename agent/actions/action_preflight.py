"""Preflight validation for agent actions before Playwright executes them.

The LLM often reasons over a DOM snapshot that is already stale by the time the
main loop receives its answer. This module keeps that risk out of the execution
path: validate the action against the current page, repair ref:N when possible,
and reject actions that are hidden, disabled, outside the active overlay, or
already covered by memory.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from agent.actions.element_resolver import enrich_action, resolve_stable_key


SELECTOR_REQUIRED_ACTIONS = {"click", "type", "hover", "select_option", "upload_file"}
REF_STATE_ACTIONS = {"click", "type", "hover", "select_option", "upload_file"}


@dataclass
class ActionPreflightResult:
    action: Dict[str, Any]
    ok: bool
    reason: str = ""
    repaired: bool = False


def ref_num(selector: str) -> Optional[int]:
    sel = (selector or "").strip()
    if sel.startswith("ref:"):
        sel = sel[4:]
    if not sel.isdigit():
        return None
    try:
        return int(sel)
    except ValueError:
        return None


def expected_stable_key(selector: str, ref_meta: Optional[Mapping[Any, Any]]) -> str:
    ref = ref_num(selector)
    if ref is None or not ref_meta:
        return ""
    return str(ref_meta.get(str(ref)) or ref_meta.get(ref) or "").strip()


def _find_current_ref_by_stable_key(page: Any, stable_key: str) -> str:
    if not page or not stable_key:
        return ""
    try:
        return str(page.evaluate(
            """(stableKey) => {
                const meta = window.__agentRefMeta || {};
                const refs = window.__agentRefs || {};

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
                    if (!el || !el.isConnected || hiddenByDomState(el)) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0;
                };

                for (const ref of Object.keys(meta)) {
                    if ((meta[ref] || '') !== stableKey) continue;
                    const el = refs[ref] || document.querySelector('[data-agent-ref="' + ref + '"]');
                    if (visible(el)) return 'ref:' + ref;
                }
                return '';
            }""",
            stable_key,
        ) or "")
    except Exception:
        return ""


def _inspect_ref_state(page: Any, selector: str) -> Dict[str, Any]:
    ref = ref_num(selector)
    if ref is None or not page:
        return {"exists": True, "visible": True, "disabled": False, "hidden_dom": False, "outside_overlay": False}
    try:
        state = page.evaluate(
            """(ref) => {
                const refStr = String(ref);
                const byAttr = document.querySelector('[data-agent-ref="' + refStr + '"]');
                const el = (window.__agentRefs && window.__agentRefs[refStr]) || byAttr;
                const out = {
                    exists: false,
                    visible: false,
                    disabled: false,
                    hidden_dom: false,
                    outside_overlay: false,
                    overlay_root_count: 0,
                    inside_active_overlay: false,
                    occluded: false,
                    stable_key: '',
                    text: '',
                    tag: '',
                };
                if (!el || !el.isConnected) return out;
                out.exists = true;
                out.stable_key = (window.__agentRefMeta && window.__agentRefMeta[refStr]) || '';
                out.tag = el.tagName ? el.tagName.toLowerCase() : '';
                out.text = ((el.innerText || el.textContent || el.value || el.placeholder || '') + '').replace(/\\s+/g, ' ').trim().slice(0, 80);

                const hiddenByDomState = (node) => {
                    let cur = node;
                    while (cur && cur.nodeType === 1) {
                        if (cur.hidden || cur.inert) return true;
                        if (cur.getAttribute && (cur.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return true;
                        cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
                    }
                    return false;
                };
                const ancestorsVisible = (node) => {
                    let cur = node;
                    while (cur && cur !== document.body) {
                        const s = getComputedStyle(cur);
                        if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') === 0) return false;
                        cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
                    }
                    return true;
                };
                const inViewport = (node) => {
                    const r = node.getBoundingClientRect();
                    return r.top < window.innerHeight && r.bottom > 0 && r.left < window.innerWidth && r.right > 0;
                };
                const deepestElementFromPoint = (x, y) => {
                    let hit = document.elementFromPoint(x, y);
                    while (hit && hit.shadowRoot && hit.shadowRoot.elementFromPoint) {
                        const inner = hit.shadowRoot.elementFromPoint(x, y);
                        if (!inner || inner === hit) break;
                        hit = inner;
                    }
                    return hit;
                };
                const activeOverlayRoots = Array.from(
                    window.__agentActiveOverlayRoots || []
                ).filter(node => node && node.isConnected && !hiddenByDomState(node));

                out.hidden_dom = hiddenByDomState(el);
                out.disabled = !!(el.disabled || (el.getAttribute && el.getAttribute('aria-disabled') === 'true'));
                const rect = el.getBoundingClientRect();
                out.visible = !out.hidden_dom
                    && rect.width > 0 && rect.height > 0
                    && inViewport(el)
                    && ancestorsVisible(el);
                out.overlay_root_count = activeOverlayRoots.length;
                out.inside_active_overlay = activeOverlayRoots.some(root => root === el || root.contains(el));
                out.outside_overlay = activeOverlayRoots.length > 0 && !out.inside_active_overlay;
                if (out.visible) {
                    const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2));
                    const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2));
                    const top = deepestElementFromPoint(x, y);
                    out.occluded = !!top && top !== el && !el.contains(top);
                }
                return out;
            }""",
            ref,
        )
        return state or {"exists": False, "visible": False}
    except Exception as exc:
        return {"exists": False, "visible": False, "error": str(exc)}


def _inspect_locator_state(page: Any, selector: str) -> Dict[str, Any]:
    """Inspect a non-ref Playwright locator in the same live DOM contract."""
    if not page or not selector:
        return {"exists": False, "visible": False}
    try:
        locator = page.locator(selector).first
        if locator.count() <= 0:
            return {"exists": False, "visible": False}
        state = locator.evaluate(
            """(el) => {
                const hiddenByDomState = (node) => {
                    let cur = node;
                    while (cur && cur.nodeType === 1) {
                        if (cur.hidden || cur.inert) return true;
                        if ((cur.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return true;
                        const s = getComputedStyle(cur);
                        if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') <= 0.01) return true;
                        cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
                    }
                    return false;
                };
                const roots = Array.from(window.__agentActiveOverlayRoots || [])
                    .filter(root => root && root.isConnected && !hiddenByDomState(root));
                const deepestElementFromPoint = (x, y) => {
                    let hit = document.elementFromPoint(x, y);
                    while (hit && hit.shadowRoot && hit.shadowRoot.elementFromPoint) {
                        const inner = hit.shadowRoot.elementFromPoint(x, y);
                        if (!inner || inner === hit) break;
                        hit = inner;
                    }
                    return hit;
                };
                const rect = el.getBoundingClientRect();
                const inViewport = rect.top < window.innerHeight && rect.bottom > 0
                    && rect.left < window.innerWidth && rect.right > 0;
                const hidden = hiddenByDomState(el);
                const visible = !hidden && rect.width > 0 && rect.height > 0 && inViewport;
                let occluded = false;
                if (visible) {
                    const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2));
                    const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2));
                    const top = deepestElementFromPoint(x, y);
                    occluded = !!top && top !== el && !el.contains(top);
                }
                const inside = roots.some(root => root === el || root.contains(el));
                return {
                    exists: true,
                    visible,
                    disabled: !!(el.disabled || (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true'),
                    hidden_dom: hidden,
                    overlay_root_count: roots.length,
                    inside_active_overlay: inside,
                    outside_overlay: roots.length > 0 && !inside,
                    occluded,
                };
            }"""
        )
        return state or {"exists": False, "visible": False}
    except Exception as exc:
        return {"exists": False, "visible": False, "error": str(exc)}


def _state_rejection(state: Mapping[str, Any], *, has_overlay: bool) -> str:
    if not state.get("exists"):
        return "stale_ref"
    if state.get("hidden_dom"):
        return "hidden_dom"
    if has_overlay and (
        state.get("outside_overlay")
        or (
            "inside_active_overlay" in state
            and not state.get("inside_active_overlay")
        )
    ):
        return "outside_overlay"
    if not state.get("visible"):
        return "not_visible"
    if state.get("disabled"):
        return "disabled"
    if state.get("occluded"):
        return "occluded"
    return ""


def _is_repeat(memory: Any, action: Dict[str, Any]) -> bool:
    if memory is None:
        return False
    try:
        return bool(memory.is_already_done_action(action))
    except Exception:
        return False


def preflight_action(
    page: Any,
    memory: Any,
    action: Dict[str, Any],
    *,
    has_overlay: bool = False,
    expected_ref_meta: Optional[Mapping[Any, Any]] = None,
    allow_repeat: bool = False,
) -> ActionPreflightResult:
    if not isinstance(action, dict):
        return ActionPreflightResult({}, False, "not_dict")

    action = enrich_action(page, memory, dict(action))
    act = (action.get("action") or "").strip().lower()
    selector = (action.get("selector") or "").strip()

    if act in SELECTOR_REQUIRED_ACTIONS and not selector:
        return ActionPreflightResult(action, False, "missing_selector")

    if has_overlay and act in ("scroll", "explore", "fill_form"):
        # These actions have no target that can be proven to belong to the
        # active layer. Modal fields are exposed as individual type/select
        # candidates and therefore remain fully testable.
        return ActionPreflightResult(action, False, "outside_overlay")

    if act in REF_STATE_ACTIONS and ref_num(selector) is not None:
        expected_key = expected_stable_key(selector, expected_ref_meta)
        if expected_key:
            try:
                current_key = resolve_stable_key(page, selector)
            except Exception:
                current_key = ""
            if current_key != expected_key:
                new_selector = _find_current_ref_by_stable_key(page, expected_key)
                if new_selector and new_selector != selector:
                    repaired = dict(action)
                    repaired["selector"] = new_selector
                    repaired["_stable_key"] = expected_key
                    repaired.pop("_canonical_locator", None)
                    repaired = enrich_action(page, memory, repaired)
                    state = _inspect_ref_state(page, new_selector)
                    if not _state_rejection(state, has_overlay=has_overlay):
                        if not allow_repeat and _is_repeat(memory, repaired):
                            return ActionPreflightResult(repaired, False, "repeat")
                        return ActionPreflightResult(repaired, True, "ref_repaired", repaired=True)
                return ActionPreflightResult(action, False, "stale_ref")

        state = _inspect_ref_state(page, selector)
        rejection = _state_rejection(state, has_overlay=has_overlay)
        if rejection:
            return ActionPreflightResult(action, False, rejection)

    elif act in REF_STATE_ACTIONS:
        state = _inspect_locator_state(page, selector)
        rejection = _state_rejection(state, has_overlay=has_overlay)
        if rejection:
            return ActionPreflightResult(action, False, rejection)

    if (
        not allow_repeat
        and act not in ("check_defect", "scroll", "close_modal", "press_key", "explore")
        and _is_repeat(memory, action)
    ):
        return ActionPreflightResult(action, False, "repeat")

    return ActionPreflightResult(action, True)


__all__ = [
    "ActionPreflightResult",
    "expected_stable_key",
    "preflight_action",
    "ref_num",
]
