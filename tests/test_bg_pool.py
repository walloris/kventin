import threading

from agent.core import bg_pool


def setup_function() -> None:
    bg_pool.shutdown_bg_pool(wait=True, cancel_futures=True)


def teardown_function() -> None:
    bg_pool.shutdown_bg_pool(wait=True, cancel_futures=True)


def test_workloads_use_isolated_executors() -> None:
    llm_pool = bg_pool.get_bg_pool("llm")
    jira_pool = bg_pool.get_bg_pool("jira")

    assert llm_pool is not jira_pool
    assert bg_pool.bg_submit(lambda: "ok", pool_name="jira").result() == "ok"


def test_selected_shutdown_does_not_stop_durable_pool() -> None:
    bg_pool.get_bg_pool("llm")
    jira_pool = bg_pool.get_bg_pool("jira")

    bg_pool.shutdown_bg_pool(
        wait=False,
        pool_names=("llm",),
        cancel_futures=True,
    )

    assert "llm" not in bg_pool._bg_pools
    assert bg_pool.get_bg_pool("jira") is jira_pool


def test_cancel_pending_tasks_keeps_executor_reusable() -> None:
    release = threading.Event()
    started = threading.Event()
    started_count = [0]
    started_lock = threading.Lock()

    def blocking() -> str:
        with started_lock:
            started_count[0] += 1
            if started_count[0] == 2:
                started.set()
        release.wait(timeout=2)
        return "done"

    running = [bg_pool.bg_submit(blocking, pool_name="llm") for _ in range(2)]
    assert started.wait(timeout=1)
    queued = bg_pool.bg_submit(lambda: "queued", pool_name="llm")
    pool = bg_pool.get_bg_pool("llm")

    assert bg_pool.cancel_bg_tasks(pool_names=("llm",)) == 1
    assert queued.cancelled() is True
    assert bg_pool.get_bg_pool("llm") is pool

    release.set()
    assert [future.result(timeout=1) for future in running] == ["done", "done"]
    assert bg_pool.bg_submit(lambda: "next", pool_name="llm").result(timeout=1) == "next"
