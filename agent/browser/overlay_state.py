"""Authoritative overlay detection and verified closing.

Blocking UI is a browser state, not a prompt hint.  The detector marks exact
overlay roots in the DOM so candidate collection and preflight use the same
scope.  This prevents the agent from interacting with an ``aria-hidden`` page
behind a modal, drawer, sidebar, menu, or listbox.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import OVERLAY_IGNORE_PATTERNS

LOG = logging.getLogger("kventin.overlay")


@dataclass(frozen=True)
class OverlaySnapshot:
    has_overlay: bool = False
    overlays: List[Dict[str, Any]] = field(default_factory=list)
    active_root_ids: List[str] = field(default_factory=list)
    active_root_tokens: List[str] = field(default_factory=list)
    fingerprint: str = ""
    error: str = ""

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "OverlaySnapshot":
        data = raw or {}
        return cls(
            has_overlay=bool(data.get("has_overlay")),
            overlays=list(data.get("overlays") or []),
            active_root_ids=[str(x) for x in (data.get("active_root_ids") or [])],
            active_root_tokens=[str(x) for x in (data.get("active_root_tokens") or [])],
            fingerprint=str(data.get("fingerprint") or ""),
            error=str(data.get("error") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_overlay": self.has_overlay,
            "overlays": list(self.overlays),
            "active_root_ids": list(self.active_root_ids),
            "active_root_tokens": list(self.active_root_tokens),
            "fingerprint": self.fingerprint,
            "error": self.error,
        }


_INSPECT_SCRIPT = r"""
(ignorePatterns) => {
    const all = [];
    const roots = [];
    const deepElements = [];
    const walkRoot = root => {
        if (!root || !root.querySelectorAll) return;
        root.querySelectorAll('*').forEach(el => {
            deepElements.push(el);
            if (el.shadowRoot) walkRoot(el.shadowRoot);
        });
    };
    walkRoot(document);
    const queryAllDeep = selector => deepElements.filter(el => {
        try { return el.matches(selector); } catch(e) { return false; }
    });
    queryAllDeep('[data-agent-overlay-root]').forEach(el => {
        el.removeAttribute('data-agent-overlay-root');
        el.removeAttribute('data-agent-active-overlay');
    });

    const parentOf = el => el && (el.parentElement || (el.getRootNode && el.getRootNode().host) || null);
    const hiddenByDom = el => {
        let cur = el;
        while (cur && cur.nodeType === 1) {
            if (cur.hidden || cur.inert) return true;
            if ((cur.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return true;
            const s = getComputedStyle(cur);
            if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') <= 0.01) return true;
            cur = parentOf(cur);
        }
        return false;
    };
    const visible = el => {
        if (!el || hiddenByDom(el)) return false;
        const r = el.getBoundingClientRect();
        return r.width >= 8 && r.height >= 8 && r.bottom > 0 && r.right > 0
            && r.top < window.innerHeight && r.left < window.innerWidth;
    };
    const zOf = el => {
        let z = 0, cur = el;
        while (cur && cur !== document.body) {
            const zi = parseInt(getComputedStyle(cur).zIndex, 10);
            if (!Number.isNaN(zi)) z = Math.max(z, zi);
            cur = parentOf(cur);
        }
        return z;
    };
    const directViewportHits = el => {
        const points = [
            [0.05, 0.05], [0.95, 0.05], [0.5, 0.5],
            [0.05, 0.95], [0.95, 0.95]
        ];
        return points.reduce((count, point) => {
            const hit = document.elementFromPoint(
                Math.max(0, Math.min(innerWidth - 1, innerWidth * point[0])),
                Math.max(0, Math.min(innerHeight - 1, innerHeight * point[1]))
            );
            return count + (hit === el ? 1 : 0);
        }, 0);
    };
    const isAgent = el => {
        let cur = el;
        while (cur && cur !== document.body) {
            if (cur.hasAttribute && cur.hasAttribute('data-agent-host')) return true;
            cur = parentOf(cur);
        }
        return false;
    };
    const ignored = el => {
        if (isAgent(el)) return true;
        let cur = el;
        for (let i = 0; i < 8 && cur; i++, cur = parentOf(cur)) {
            const hay = [cur.id, String(cur.className || ''), cur.getAttribute && cur.getAttribute('aria-label')]
                .join(' ').toLowerCase();
            if ((ignorePatterns || []).some(p => p && hay.includes(p))) return true;
        }
        return false;
    };
    const textOf = (el, limit=180) => (el.innerText || el.textContent || '')
        .replace(/\s+/g, ' ').trim().slice(0, limit);
    const ensureRef = el => {
        if (!el) return '';
        let ref = el.getAttribute('data-agent-ref');
        if (ref) return ref;
        window.__agentRefs = window.__agentRefs || {};
        let maxRef = 0;
        Object.keys(window.__agentRefs).forEach(k => { const n = parseInt(k, 10); if (n > maxRef) maxRef = n; });
        ref = String(maxRef + 1);
        el.setAttribute('data-agent-ref', ref);
        window.__agentRefs[parseInt(ref, 10)] = el;
        return ref;
    };
    const actionableCount = el => el.querySelectorAll(
        'button, [role="button"], a[href], input:not([type="hidden"]), textarea, select, [role="menuitem"], [role="option"]'
    ).length;
    const hasSuppressedSibling = el => {
        let cur = el;
        while (cur && cur.parentElement) {
            const siblings = Array.from(cur.parentElement.children || []);
            if (siblings.some(node => node !== cur && (
                node.hidden || node.inert || (node.getAttribute('aria-hidden') || '').toLowerCase() === 'true'
            ))) return true;
            cur = cur.parentElement;
        }
        return false;
    };
    const addRoot = (el, type, explicit=false) => {
        if (!visible(el) || ignored(el)) return;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        const z = zOf(el);
        const ariaModal = (el.getAttribute('aria-modal') || '').toLowerCase() === 'true';
        const nativeOpen = (el.tagName || '').toLowerCase() === 'dialog' && !!el.open;
        const popoverOpen = !!(el.matches && (() => { try { return el.matches('[popover]:popover-open'); } catch(e) { return false; } })());
        const viewportShare = (r.width * r.height) / Math.max(1, window.innerWidth * window.innerHeight);
        const positioned = s.position === 'fixed' || s.position === 'absolute' || s.position === 'sticky';
        const suppressedBackground = hasSuppressedSibling(el);
        const actions = actionableCount(el);
        const explicitModal = explicit && type === 'modal'
            && (ariaModal || nativeOpen || positioned || z >= 5 || suppressedBackground);
        const explicitPanel = explicit && ['drawer', 'sidebar', 'offcanvas'].includes(type)
            && (positioned || z >= 5 || suppressedBackground) && viewportShare >= 0.08;
        const explicitPopup = explicit && ['popover', 'dropdown'].includes(type)
            && (popoverOpen || positioned || z >= 5) && actions > 0;
        const pointerBlocker = !explicit && positioned && z >= 5
            && viewportShare >= 0.5 && s.pointerEvents !== 'none'
            && directViewportHits(el) >= 3
            && (actions === 0 || suppressedBackground);
        const suppressedPanel = !explicit && positioned && suppressedBackground
            && viewportShare >= 0.08 && actions > 0;
        const highStackPanel = !explicit && positioned && z >= 50
            && viewportShare >= 0.15 && actions > 0;
        const blocking = explicitModal || explicitPanel || explicitPopup || ariaModal || nativeOpen || popoverOpen
            || pointerBlocker || suppressedPanel || highStackPanel;
        if (!blocking) return;
        roots.push({el, type, z, area: r.width * r.height});
    };

    const selectors = [
        ['modal', '[aria-modal="true"], [role="dialog"], [role="alertdialog"], dialog[open]'],
        ['popover', '[popover]:popover-open, .popover.show, [class*="popover"][class*="open"]'],
        ['drawer', '[class*="drawer"][class*="open"], [class*="drawer"][class*="show"], [class*="drawer"][class*="active"]'],
        ['sidebar', '[class*="sidebar"][class*="open"], [class*="sidebar"][class*="show"], [class*="sidebar"][class*="active"], [class*="side-bar"][class*="open"]'],
        ['offcanvas', '[class*="offcanvas"][class*="show"], [class*="offcanvas"][class*="open"]'],
        ['modal', '.modal.show, .modal.open, .modal.active, [class*="modal"][class*="visible"], [class*="popup"][class*="open"], [class*="popup"][class*="show"]'],
        ['dropdown', '[role="listbox"], [role="menu"], .dropdown-menu.show, [class*="dropdown"][class*="open"], [class*="select"][class*="open"]']
    ];
    selectors.forEach(([type, selector]) => {
        try { queryAllDeep(selector).forEach(el => addRoot(el, type, true)); } catch(e) {}
    });

    deepElements.forEach(el => {
        if (!visible(el) || ignored(el)) return;
        const s = getComputedStyle(el);
        const z = parseInt(s.zIndex, 10);
        const suppressedBackground = hasSuppressedSibling(el);
        if (
            (s.position === 'fixed' || s.position === 'absolute')
            && !Number.isNaN(z)
            && (z >= 5 || suppressedBackground)
        ) {
            const cls = String(el.className || '').toLowerCase();
            if (/(backdrop|scrim|mask)/.test(cls) && actionableCount(el) === 0) return;
            addRoot(el, 'overlay', false);
        }
    });

    roots.sort((a, b) => b.z - a.z || a.area - b.area);
    const unique = [];
    roots.forEach(item => {
        if (unique.some(x => x.el === item.el)) return;
        // Keep the most specific nested root at the same stacking level.
        if (unique.some(x => item.el.contains(x.el) && x.z >= item.z)) return;
        unique.push(item);
    });
    // Only the topmost blocking layer is interactive. Lower dialogs remain in
    // the DOM but must not leak candidates while a nested menu/dialog is open.
    const active = unique.slice(0, 1);
    window.__agentActiveOverlayRoots = active.map(item => item.el);
    active.forEach((item, index) => {
        const rootId = 'overlay-' + (index + 1);
        window.__agentOverlayTokenCounter = (window.__agentOverlayTokenCounter || 0) + 1;
        const rootToken = item.el.getAttribute('data-agent-overlay-token')
            || ('overlay-token-' + window.__agentOverlayTokenCounter);
        item.el.setAttribute('data-agent-overlay-token', rootToken);
        item.el.setAttribute('data-agent-overlay-root', rootId);
        item.el.setAttribute('data-agent-active-overlay', 'true');
        let close = item.el.querySelector(
            '[aria-label*="close" i], [data-dismiss="modal"], [data-bs-dismiss="modal"], button.close, .modal-close, [class*="close"]'
        );
        if (!close) {
            close = Array.from(item.el.querySelectorAll('button, [role="button"]')).find(control => {
                const label = [
                    control.getAttribute('aria-label') || '',
                    control.getAttribute('title') || '',
                    control.innerText || control.textContent || ''
                ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
                return /(^|\s)(close|cancel|закрыть|отмена)(\s|$)/.test(label)
                    || ['×', '✕', '✖'].includes(label);
            }) || null;
        }
        const closeRef = close && visible(close) ? ensureRef(close) : '';
        const buttons = [];
        item.el.querySelectorAll('button, [role="button"], input[type="submit"]').forEach(btn => {
            if (visible(btn)) buttons.push(textOf(btn, 60) || btn.getAttribute('aria-label') || '(кнопка)');
        });
        const inputs = [];
        item.el.querySelectorAll('input:not([type="hidden"]), textarea, select').forEach(inp => {
            if (visible(inp)) inputs.push({
                type: inp.type || inp.tagName.toLowerCase(),
                placeholder: (inp.placeholder || '').slice(0, 60),
                name: (inp.name || '').slice(0, 60)
            });
        });
        const text = textOf(item.el);
        const identity = [item.type, item.el.id || '', String(item.el.className || '').slice(0, 80), text.slice(0, 80)].join('|');
        all.push({
            type: item.type,
            text,
            buttons: buttons.slice(0, 10),
            inputs: inputs.slice(0, 10),
            close_selector: closeRef ? 'ref:' + closeRef : null,
            root_id: rootId,
            root_token: rootToken,
            root_selector: '[data-agent-overlay-root="' + rootId + '"]',
            z_index: item.z,
            blocking: true,
            fingerprint: identity
        });
    });

    const transientSelectors = [
        ['tooltip', '[role="tooltip"], .tooltip.show, [data-tippy-root]'],
        ['notification', '[role="alert"], [role="status"], .toast.show, .Toastify__toast']
    ];
    transientSelectors.forEach(([type, selector]) => {
        try {
            queryAllDeep(selector).forEach(el => {
                if (visible(el) && !ignored(el) && !active.some(x => x.el === el || x.el.contains(el))) {
                    all.push({type, text: textOf(el, 120), blocking: false});
                }
            });
        } catch(e) {}
    });

    const fingerprints = all.filter(x => x.blocking).map(x => x.fingerprint).sort();
    return {
        has_overlay: active.length > 0,
        overlays: all.slice(0, 10),
        active_root_ids: all.filter(x => x.blocking).map(x => x.root_id),
        active_root_tokens: all.filter(x => x.blocking).map(x => x.root_token),
        fingerprint: fingerprints.join('||')
    };
}
"""


def inspect_overlays(page: Any) -> Dict[str, Any]:
    try:
        raw = page.evaluate(_INSPECT_SCRIPT, list(OVERLAY_IGNORE_PATTERNS or []))
        return OverlaySnapshot.from_dict(raw).to_dict()
    except Exception as exc:
        LOG.debug("overlay inspection failed: %s", exc)
        return OverlaySnapshot(error=str(exc)).to_dict()


def overlay_was_closed(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    """True only when the blocking state disappeared or changed identity."""
    old = OverlaySnapshot.from_dict(before)
    new = OverlaySnapshot.from_dict(after)
    if not old.has_overlay:
        return True
    if not new.has_overlay:
        return True
    if old.active_root_tokens and new.active_root_tokens:
        return not bool(set(old.active_root_tokens) & set(new.active_root_tokens))
    return bool(old.fingerprint and new.fingerprint and old.fingerprint != new.fingerprint)


def _locator_for(page: Any, selector: str) -> Any:
    value = (selector or "").strip()
    if not value:
        return None
    if value.startswith("ref:") and value[4:].isdigit():
        return page.locator(f'[data-agent-ref="{value[4:]}"]').first
    if value.isdigit():
        return page.locator(f'[data-agent-ref="{value}"]').first
    return page.locator(value).first


def _verify_after_action(page: Any, before: Dict[str, Any], wait_seconds: float) -> bool:
    if wait_seconds > 0:
        try:
            page.wait_for_timeout(int(wait_seconds * 1000))
        except Exception:
            time.sleep(wait_seconds)
    return overlay_was_closed(before, inspect_overlays(page))


def close_active_overlay(
    page: Any,
    selector: str = "",
    *,
    wait_seconds: float = 0.25,
) -> str:
    """Try scoped close strategies and verify the DOM after every attempt."""
    before = inspect_overlays(page)
    if not before.get("has_overlay"):
        return "modal_already_closed"

    selectors: List[str] = []
    if selector:
        selectors.append(selector)
    for overlay in before.get("overlays") or []:
        if overlay.get("blocking") and overlay.get("close_selector"):
            selectors.append(str(overlay["close_selector"]))
    selectors.extend(
        [
            '[data-agent-active-overlay="true"] [aria-label*="close" i]',
            '[data-agent-active-overlay="true"] [aria-label*="закрыть" i]',
            '[data-agent-active-overlay="true"] [data-dismiss="modal"]',
            '[data-agent-active-overlay="true"] [data-bs-dismiss="modal"]',
            '[data-agent-active-overlay="true"] button.close',
            '[data-agent-active-overlay="true"] .modal-close',
            '[data-agent-active-overlay="true"] button:has-text("Закрыть")',
            '[data-agent-active-overlay="true"] button:has-text("Close")',
            '[data-agent-active-overlay="true"] button:has-text("Отмена")',
            '[data-agent-active-overlay="true"] button:has-text("Cancel")',
        ]
    )

    seen = set()
    for candidate in selectors:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            locator = _locator_for(page, candidate)
            if locator is None or locator.count() <= 0 or not locator.is_visible():
                continue
            locator.click(timeout=3000)
            if _verify_after_action(page, before, wait_seconds):
                return f"modal_closed_by_selector: {candidate[:80]}"
        except Exception:
            continue

    try:
        page.keyboard.press("Escape")
        if _verify_after_action(page, before, wait_seconds):
            return "modal_closed_by_escape"
    except Exception:
        pass

    # Backdrop clicks are scoped to an actual visible backdrop. Never click an
    # arbitrary coordinate that could activate the hidden application behind it.
    for backdrop_selector in (
        '[class*="backdrop"]',
        '[class*="scrim"]',
        '[class*="mask"]',
        '[class~="overlay"]',
    ):
        try:
            matches = page.locator(backdrop_selector)
            for index in range(min(matches.count(), 5)):
                backdrop = matches.nth(index)
                if not backdrop.is_visible():
                    continue
                is_safe = backdrop.evaluate(
                    r"""(el) => {
                        const active = (window.__agentActiveOverlayRoots || []).find(root => root && root.isConnected);
                        if (!active || el === active || active.contains(el)) return false;
                        const rect = el.getBoundingClientRect();
                        const share = rect.width * rect.height / Math.max(1, innerWidth * innerHeight);
                        const cls = String(el.className || '').toLowerCase();
                        const explicitBackdrop = /(backdrop|scrim|mask)/.test(cls);
                        const genericOverlay = /(^|\s)overlay(\s|$)/.test(cls);
                        const actionable = el.querySelector(
                            'button, a[href], input, textarea, select, [role="button"], [role="menuitem"]'
                        );
                        return share >= 0.5 && !actionable && (explicitBackdrop || genericOverlay);
                    }"""
                )
                if not is_safe:
                    continue
                backdrop.click(position={"x": 5, "y": 5}, timeout=3000)
                if _verify_after_action(page, before, wait_seconds):
                    return f"modal_closed_by_backdrop: {backdrop_selector}"
        except Exception:
            continue

    after = inspect_overlays(page)
    fingerprint = str(after.get("fingerprint") or before.get("fingerprint") or "unknown")
    return f"modal_close_failed: overlay_still_open:{fingerprint[:160]}"


__all__ = [
    "OverlaySnapshot",
    "close_active_overlay",
    "inspect_overlays",
    "overlay_was_closed",
]
