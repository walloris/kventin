"""Consistent screenshots for observations, checks, and defect evidence."""
from __future__ import annotations

import base64
import logging
from typing import Optional

from playwright.sync_api import Page

LOG = logging.getLogger("kventin.screenshot")


def _set_agent_ui_visible(page: Page, visible: bool) -> None:
    page.evaluate(
        """(visible) => {
            const display = visible ? '' : 'none';
            if (window.__agentShadow && window.__agentShadow.host) {
                window.__agentShadow.host.style.display = display;
            }
            document.querySelectorAll('[data-agent-host]').forEach(el => {
                el.style.display = display;
            });
        }""",
        visible,
    )


def take_screenshot_b64(page: Page) -> Optional[str]:
    """Capture a PNG without Kventin's own injected UI."""
    hidden = False
    try:
        if page.is_closed():
            return None
        _set_agent_ui_visible(page, False)
        hidden = True
        raw = page.screenshot(type="png")
        return base64.b64encode(raw).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        if "closed" not in str(exc).lower() and "target page" not in str(exc).lower():
            LOG.warning("Screenshot failed: %s", exc)
        return None
    finally:
        if hidden:
            try:
                if not page.is_closed():
                    _set_agent_ui_visible(page, True)
            except Exception:
                LOG.debug("Could not restore agent UI after screenshot", exc_info=True)


__all__ = ["take_screenshot_b64"]
