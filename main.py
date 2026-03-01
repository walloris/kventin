#!/usr/bin/env python3
"""
Точка входа: запуск AI-агента тестировщика.
Передайте URL страницы аргументом или задайте START_URL в .env.
Агент бесконечно анализирует страницу, советуется с GigaChat и тестирует.

CI/CD: в окружении CI/GITHUB_ACTIONS/GITLAB_CI по умолчанию headless.
Exit code: 0 — ок (дефектов в пределах порога); 1 — дефектов больше FAIL_ON_DEFECTS; 2 — ошибка агента.
"""
import argparse
import json
import sys

# Чтобы импорт config и src работал из корня проекта
sys.path.insert(0, ".")

from src.agent import run_agent
from config import FAIL_ON_DEFECTS, CI_MODE, START_URL, START_URLS


def _collect_urls(args) -> list:
    """Собрать список URL: один аргумент, --urls-file или START_URLS/START_URL из .env."""
    if args.url:
        return [args.url]
    if getattr(args, "urls_file", None):
        try:
            with open(args.urls_file, "r", encoding="utf-8") as f:
                return [u.strip() for u in f if u.strip() and not u.strip().startswith("#")]
        except Exception as e:
            print(f"[main] Ошибка чтения --urls-file: {e}", file=sys.stderr)
            return []
    if START_URLS:
        return list(START_URLS)
    return [START_URL or "https://example.com"]


def main():
    parser = argparse.ArgumentParser(description="AI-агент тестировщик (Playwright + LLM + Jira)")
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="URL страницы для тестирования (иначе START_URL или START_URLS в .env)",
    )
    parser.add_argument(
        "--urls-file",
        type=str,
        default=None,
        metavar="PATH",
        help="Файл со списком URL (по одному на строку)",
    )
    parser.add_argument(
        "--fail-on-defects",
        type=int,
        default=None,
        metavar="N",
        help="Выход с кодом 1, если создано дефектов > N (переопределяет FAIL_ON_DEFECTS из .env)",
    )
    parser.add_argument(
        "--json-summary",
        action="store_true",
        help="В конце вывести JSON-сводку в stdout (defects, steps, error)",
    )
    args = parser.parse_args()

    urls = _collect_urls(args)
    total_defects = 0
    total_steps = 0
    error_seen = None

    for i, url in enumerate(urls):
        if len(urls) > 1:
            print(f"[main] Запуск агента для URL ({i + 1}/{len(urls)}): {url[:80]}")
        res = run_agent(start_url=url)
        if res is None:
            sys.exit(2)
        total_defects += res.get("defects", 0)
        total_steps += res.get("steps", 0)
        if res.get("error"):
            error_seen = res.get("error")

    if args.json_summary or CI_MODE:
        print(json.dumps({
            "defects": total_defects,
            "steps": total_steps,
            "error": error_seen,
        }, ensure_ascii=False))

    if error_seen:
        sys.exit(2)
    threshold = args.fail_on_defects if args.fail_on_defects is not None else FAIL_ON_DEFECTS
    if threshold >= 0 and total_defects > threshold:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
