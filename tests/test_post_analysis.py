from concurrent.futures import Future

from agent.core import post_analysis


class Memory:
    def __init__(self):
        self._pending_analyses = []
        self.defects_on_current_step = 1
        self.console_len_before_action = 0
        self.network_len_before_action = 0
        self.screenshot_before_action = None


class Page:
    def is_closed(self):
        return False


def test_nonblocking_flush_keeps_slow_analysis() -> None:
    memory = Memory()
    future = Future()
    memory._pending_analyses.append(
        {"future": future, "step": 1, "current_url": "https://example.test"}
    )

    post_analysis._flush_pending_analysis(Page(), memory, [], [])

    assert memory._pending_analyses[0]["future"] is future

    future.set_result({})
    post_analysis._flush_pending_analysis(Page(), memory, [], [])
    assert memory._pending_analyses == []


def test_completed_finding_reaches_defect_pipeline(monkeypatch) -> None:
    memory = Memory()
    future = Future()
    future.set_result({"bug_to_report": "TypeError after save"})
    memory._pending_analyses.append(
        {
            "future": future,
            "step": 2,
            "current_url": "https://example.test/form",
            "checklist_results": [],
        }
    )
    created = []
    monkeypatch.setattr(post_analysis, "ENABLE_SECOND_PASS_BUG", False)
    monkeypatch.setattr(
        post_analysis,
        "_create_defect",
        lambda _page, bug, *_args: created.append(bug),
    )

    post_analysis._flush_pending_analysis(Page(), memory, [], [])

    assert created == ["TypeError after save"]
    assert memory._pending_analyses == []


def test_full_analysis_queue_does_not_submit_more_work(monkeypatch) -> None:
    memory = Memory()
    memory._pending_analyses = [{"future": Future()} for _ in range(8)]
    submitted = []
    monkeypatch.setattr(
        post_analysis,
        "_collect_post_data",
        lambda *_args: {
            "new_overlay": False,
            "overlay_types": [],
            "post_screenshot_b64": None,
        },
    )
    monkeypatch.setattr(
        post_analysis,
        "_bg_submit",
        lambda *_args, **_kwargs: submitted.append(True),
    )

    post_analysis._step_post_analysis(
        Page(),
        3,
        {"action": "scroll", "selector": "down"},
        "scrolled_down",
        "scroll",
        "down",
        "",
        "",
        "",
        False,
        "https://example.test",
        [],
        [],
        [],
        memory,
    )

    assert submitted == []
