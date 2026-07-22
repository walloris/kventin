import base64

from agent.browser.screenshot import take_screenshot_b64


class FakePage:
    def __init__(self, *, error: bool = False) -> None:
        self.error = error
        self.visibility = []

    def is_closed(self) -> bool:
        return False

    def evaluate(self, _script, visible: bool) -> None:
        self.visibility.append(visible)

    def screenshot(self, **_kwargs) -> bytes:
        if self.error:
            raise RuntimeError("capture failed")
        return b"png"


def test_screenshot_hides_and_restores_agent_ui() -> None:
    page = FakePage()

    assert take_screenshot_b64(page) == base64.b64encode(b"png").decode("ascii")
    assert page.visibility == [False, True]


def test_screenshot_restores_agent_ui_after_capture_error() -> None:
    page = FakePage(error=True)

    assert take_screenshot_b64(page) is None
    assert page.visibility == [False, True]
