#!/usr/bin/env python3
"""
Демон Kventin: периодический ретест дефектов и тестирование задач.

Раз в DAEMON_INTERVAL_SEC (по умолчанию 15 минут):
1. Читает дефекты Kventin в статусе Ready for QA → ретест (src/defect_retest).
2. Если задан JIRA_DAEMON_TASK_STATUS — читает задачи в этом статусе → тестирование
   и генерацию тест-кейсов (src/task_testing).
3. Формирует очередь, обрабатывает по одному, затем спит до следующего прогона.

Запуск:
    python daemon.py            # бесконечный цикл (раз в 15 минут)
    python daemon.py --once     # один прогон (для cron)
    python daemon.py --interval 600
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

sys.path.insert(0, ".")

from config import (
    DAEMON_INTERVAL_SEC,
    DAEMON_LOCK_FILE,
    JIRA_DAEMON_TASK_MAX,
    JIRA_DAEMON_TASK_STATUS,
    JIRA_RETEST_STATUS_READY_FOR_QA,
)
from src.jira_client import is_jira_rest_configured

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
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    pid = (f.read() or "").strip()
                if pid and pid.isdigit() and _pid_alive(int(pid)):
                    raise SystemExit(f"[daemon] Уже запущен (pid={pid}, lock={self.path}).")
            except SystemExit:
                raise
            except Exception:
                pass
        if self.path:
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(str(os.getpid()))
                self.acquired = True
            except OSError as exc:
                LOG.warning("Не удалось создать lock %s: %s", self.path, exc)
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
    except OSError:
        return False
    return True


def _run_once() -> int:
    """Один прогон: ретест дефектов + тестирование задач. Возвращает число обработанных."""
    from src.defect_retest import collect_retest_issue_keys, process_retest_issue

    processed = 0

    retest_keys = collect_retest_issue_keys()
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
        from src.task_testing import collect_task_issue_keys, process_task_issue

        task_keys = collect_task_issue_keys(JIRA_DAEMON_TASK_STATUS, max_results=JIRA_DAEMON_TASK_MAX or 10)
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
