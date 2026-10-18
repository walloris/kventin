#!/usr/bin/env python3
"""
Демон Kventin: периодический ретест дефектов и тестирование задач.

Раз в DAEMON_INTERVAL_SEC (по умолчанию 15 минут):
1. Читает дефекты Kventin в статусе Ready for QA → ретест (agent/defect_retest).
2. Если задан JIRA_DAEMON_TASK_STATUS — читает задачи в этом статусе → тестирование
   и генерацию тест-кейсов (agent/task_testing).
3. Формирует очередь, обрабатывает по одному, затем спит до следующего прогона.

Запуск:
    python scripts/daemon.py            # бесконечный цикл (раз в 15 минут)
    python scripts/daemon.py --once     # один прогон (для cron)
    python scripts/daemon.py --interval 600
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

script_dir = Path(__file__).resolve().parent
parent_dir = script_dir.parent
if str(parent_dir) not in sys.path:
    sys.path.insert(0, str(parent_dir))
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from config import (
    DAEMON_INTERVAL_SEC,
    DAEMON_LOCK_FILE,
    JIRA_DAEMON_TASK_MAX,
    JIRA_DAEMON_TASK_STATUS,
    JIRA_RETEST_STATUS_READY_FOR_QA,
)
from agent.defects.jira_client import is_jira_rest_configured

LOG = logging.getLogger("kventin.daemon")

_STOP = False


def _handle_signal(signum, _frame) -> None:
    global _STOP
    _STOP = True
    LOG.info("Получен сигнал %s — завершаю после текущего прогона…", signum)


class _SingleInstanceLock:
    """Файловая блокировка единственного экземпляра демона."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.acquired = False

    def __enter__(self) -> "_SingleInstanceLock":
        if not self.path:
            return self
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)

        while True:
            try:
                fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    with open(self.path, "r", encoding="utf-8") as lock_file:
                        pid = (lock_file.read() or "").strip()
                except OSError:
                    pid = ""
                if pid.isdigit() and _pid_alive(int(pid)):
                    raise SystemExit(
                        f"[daemon] Уже запущен (pid={pid}, lock={self.path})."
                    )
                try:
                    os.remove(self.path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise SystemExit(
                        f"[daemon] Не удалось захватить lock {self.path}: {exc}"
                    )
                continue
            except OSError as exc:
                raise SystemExit(
                    f"[daemon] Не удалось создать lock {self.path}: {exc}"
                )

            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
                self.acquired = True
            finally:
                os.close(fd)
            return self

    def __exit__(self, *_exc) -> None:
        if self.acquired and self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _run_once() -> int:
    """Один прогон: ретест дефектов + тестирование задач. Возвращает число обработанных."""
    from agent.defects.defect_retest import (
        collect_qa_retest_issue_keys,
        collect_retest_issue_keys,
        process_retest_issue,
    )

    processed = 0

    try:
        ready_keys = collect_retest_issue_keys()
        qa_keys = collect_qa_retest_issue_keys()
        retest_keys = list(dict.fromkeys(ready_keys + qa_keys))
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Не удалось получить очередь ретестов: %s", exc)
        retest_keys = []
    if retest_keys:
        LOG.info("Ретест дефектов: %d в статусе «%s»", len(retest_keys), JIRA_RETEST_STATUS_READY_FOR_QA)
        for key in retest_keys:
            if _STOP:
                return processed
            try:
                if process_retest_issue(key):
                    processed += 1
            except Exception as exc:  # noqa: BLE001
                LOG.exception("Ретест %s: %s", key, exc)
    else:
        LOG.info("Дефектов для ретеста нет (статус «%s»)", JIRA_RETEST_STATUS_READY_FOR_QA)

    if JIRA_DAEMON_TASK_STATUS:
        from agent.tasks.task_testing import collect_task_issue_keys, process_task_issue

        try:
            task_keys = collect_task_issue_keys(
                JIRA_DAEMON_TASK_STATUS,
                max_results=JIRA_DAEMON_TASK_MAX or 10,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Не удалось получить очередь задач: %s", exc)
            task_keys = []
        if task_keys:
            LOG.info("Тестирование задач: %d в статусе «%s»", len(task_keys), JIRA_DAEMON_TASK_STATUS)
            for key in task_keys:
                if _STOP:
                    return processed
                try:
                    if process_task_issue(key):
                        processed += 1
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("Задача %s: %s", key, exc)
        else:
            LOG.info("Задач для тестирования нет (статус «%s»)", JIRA_DAEMON_TASK_STATUS)
    else:
        LOG.debug("JIRA_DAEMON_TASK_STATUS не задан — тестирование задач отключено")

    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description="Демон Kventin: ретест дефектов + тестирование задач")
    parser.add_argument("--once", action="store_true", help="Один прогон и выход (для cron)")
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        metavar="SEC",
        help=f"Интервал между прогонами в секундах (по умолчанию {DAEMON_INTERVAL_SEC})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[daemon] %(asctime)s %(levelname)s %(message)s")

    if not is_jira_rest_configured():
        print(
            "[daemon] Не заданы JIRA_URL, JIRA_API_TOKEN и JIRA_PROJECT_KEY "
            "(для Basic также нужен JIRA_USERNAME или JIRA_EMAIL).",
            file=sys.stderr,
        )
        return 2

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    interval = args.interval if args.interval and args.interval > 0 else DAEMON_INTERVAL_SEC

    with _SingleInstanceLock(DAEMON_LOCK_FILE):
        if args.once:
            n = _run_once()
            LOG.info("Прогон завершён, обработано: %d", n)
            return 0

        LOG.info("Старт демона, интервал %d сек (%.0f мин)", interval, interval / 60)
        while not _STOP:
            started = time.time()
            try:
                n = _run_once()
                LOG.info("Прогон завершён, обработано: %d", n)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("Ошибка прогона: %s", exc)
            if _STOP:
                break
            elapsed = time.time() - started
            sleep_for = max(5, interval - int(elapsed))
            LOG.info("Сплю %d сек до следующего прогона…", sleep_for)
            # Прерываемый сон: реагируем на сигнал быстрее
            for _ in range(sleep_for):
                if _STOP:
                    break
                time.sleep(1)

    LOG.info("Демон остановлен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
