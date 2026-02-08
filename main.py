#!/usr/bin/env python3
"""
Точка входа: запуск AI-агента тестировщика.
Передайте URL страницы аргументом или задайте START_URL в .env.
Агент бесконечно анализирует страницу, советуется с GigaChat и тестирует.
"""
import argparse
import sys

# Чтобы импорт config и src работал из корня проекта
sys.path.insert(0, ".")

from src.agent import run_agent


def main():
    parser = argparse.ArgumentParser(description="AI-агент тестировщик (Playwright + GigaChat + Jira)")
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="URL страницы для тестирования (иначе берётся из START_URL в .env)",
    )
    args = parser.parse_args()
    run_agent(start_url=args.url)


if __name__ == "__main__":
    main()
