import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from agent.actions.action_candidates import collect_action_candidates
from agent.actions.action_preflight import preflight_action
from agent.actions.browser_actions import execute_action
from agent.browser.overlay_state import close_active_overlay, inspect_overlays
from agent.browser.page_analyzer import get_dom_summary
from agent.core.agent_memory import AgentMemory


pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("KVENTIN_RUN_BROWSER_TESTS") != "1",
        reason="set KVENTIN_RUN_BROWSER_TESTS=1 to run browser integration tests",
    ),
]

FIXTURES = Path(__file__).parent / "fixtures" / "agent_harness"


@contextmanager
def browser_page(path: Path):
    with sync_playwright() as playwright:
        launch_options = {"headless": True}
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if chrome.is_file():
            launch_options["executable_path"] = str(chrome)
        browser = playwright.chromium.launch(**launch_options)
        page = browser.new_page(viewport={"width": 1000, "height": 800})
        page.goto(path.resolve().as_uri())
        try:
            yield page
        finally:
            browser.close()


def test_sidebar_is_the_only_interactive_scope_and_close_is_verified() -> None:
    with browser_page(FIXTURES / "sidebar_hidden.html") as page:
        get_dom_summary(page)
        overlay = inspect_overlays(page)

        assert overlay["has_overlay"] is True
        candidates = collect_action_candidates(
            page,
            AgentMemory(),
            has_overlay=True,
            overlay_info=overlay,
        )
        labels = {candidate.label for candidate in candidates}
        assert "Sidebar Action" in labels
        assert "Hidden Save" not in labels

        preflight = preflight_action(
            page,
            AgentMemory(),
            {"action": "click", "selector": "#hidden-save"},
            has_overlay=True,
        )
        assert preflight.ok is False
        assert preflight.reason in {"hidden_dom", "outside_overlay"}
        assert execute_action(
            page,
            {"action": "click", "selector": "#hidden-save"},
            AgentMemory(),
        ).startswith("preflight_rejected:")

        result = close_active_overlay(page, wait_seconds=0)
        assert result.startswith("modal_closed_by_selector")
        assert inspect_overlays(page)["has_overlay"] is False


def test_preflight_rejects_a_visually_intercepted_target() -> None:
    with browser_page(FIXTURES / "overlay_intercept.html") as page:
        assert inspect_overlays(page)["has_overlay"] is True
        result = preflight_action(
            page,
            AgentMemory(),
            {"action": "click", "selector": "#target"},
        )

        assert result.ok is False
        assert result.reason == "occluded"


def test_shadow_dom_overlay_uses_the_same_interactive_scope() -> None:
    with browser_page(FIXTURES / "shadow_sidebar.html") as page:
        get_dom_summary(page, include_shadow_dom=True)
        overlay = inspect_overlays(page)

        assert overlay["has_overlay"] is True
        candidates = collect_action_candidates(
            page,
            AgentMemory(),
            has_overlay=True,
            overlay_info=overlay,
        )
        labels = {candidate.label for candidate in candidates}
        assert "Shadow Action" in labels
        assert "Background Action" not in labels

        shadow_action = next(candidate for candidate in candidates if candidate.label == "Shadow Action")
        preflight = preflight_action(
            page,
            AgentMemory(),
            shadow_action.as_action(),
            has_overlay=True,
        )
        assert preflight.ok is True

        assert close_active_overlay(page, wait_seconds=0).startswith("modal_closed_by_selector")
        assert inspect_overlays(page)["has_overlay"] is False


def test_dropdown_options_are_actionable_inside_overlay_scope() -> None:
    with browser_page(FIXTURES / "dropdown_overlay.html") as page:
        get_dom_summary(page)
        overlay = inspect_overlays(page)
        candidates = collect_action_candidates(
            page,
            AgentMemory(),
            has_overlay=True,
            overlay_info=overlay,
        )

        option = next(candidate for candidate in candidates if candidate.label == "Option A")
        assert "Background Action" not in {candidate.label for candidate in candidates}
        assert execute_action(page, option.as_action(), AgentMemory()).startswith("clicked:")
        assert inspect_overlays(page)["has_overlay"] is False
