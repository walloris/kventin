import os

import pytest

from scripts.daemon import _SingleInstanceLock


def test_single_instance_lock_is_atomic_and_removed(tmp_path) -> None:
    path = tmp_path / "agent.lock"

    with _SingleInstanceLock(str(path)):
        assert path.read_text() == str(os.getpid())
        with pytest.raises(SystemExit):
            with _SingleInstanceLock(str(path)):
                pass

    assert path.exists() is False


def test_single_instance_lock_replaces_stale_pid(tmp_path) -> None:
    path = tmp_path / "agent.lock"
    path.write_text("99999999")

    with _SingleInstanceLock(str(path)):
        assert path.read_text() == str(os.getpid())

    assert path.exists() is False
