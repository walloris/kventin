from agent.browser.overlay_state import close_active_overlay, overlay_was_closed


def _snapshot(opened=True, fingerprint="modal|checkout", tokens=None):
    return {
        "has_overlay": opened,
        "fingerprint": fingerprint if opened else "",
        "overlays": [{"blocking": True, "close_selector": None}] if opened else [],
        "active_root_ids": ["overlay-1"] if opened else [],
        "active_root_tokens": list(tokens or []),
    }


class EmptyLocator:
    @property
    def first(self):
        return self

    def count(self):
        return 0

    def nth(self, _index):
        return self

    def is_visible(self):
        return False


class Keyboard:
    def __init__(self, page, closes):
        self.page = page
        self.closes = closes

    def press(self, _key):
        if self.closes:
            self.page.opened = False


class OverlayPage:
    def __init__(self, *, escape_closes=False):
        self.opened = True
        self.keyboard = Keyboard(self, escape_closes)

    def evaluate(self, _script, _arg=None):
        return _snapshot(self.opened)

    def locator(self, _selector):
        return EmptyLocator()

    def wait_for_timeout(self, _milliseconds):
        return None


def test_overlay_close_requires_observed_dom_change() -> None:
    result = close_active_overlay(OverlayPage(), wait_seconds=0)

    assert result.startswith("modal_close_failed")


def test_overlay_close_accepts_verified_escape_result() -> None:
    result = close_active_overlay(OverlayPage(escape_closes=True), wait_seconds=0)

    assert result == "modal_closed_by_escape"


def test_overlay_identity_change_counts_as_closing_top_layer() -> None:
    assert overlay_was_closed(_snapshot(), _snapshot(False)) is True
    assert overlay_was_closed(_snapshot(), _snapshot(True, "modal|confirmation")) is True
    assert overlay_was_closed(_snapshot(), _snapshot()) is False


def test_same_overlay_node_is_not_closed_when_only_text_changes() -> None:
    before = _snapshot(True, "dialog|checkout", ["token-1"])
    after = _snapshot(True, "dialog|validation error", ["token-1"])

    assert overlay_was_closed(before, after) is False
    assert overlay_was_closed(before, _snapshot(True, "dialog|confirm", ["token-2"])) is True
