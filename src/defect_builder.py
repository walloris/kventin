"""
Сбор дефекта по канонам: нормальное название, структурированное описание, фактура во вложениях.
"""
import os
import tempfile
from datetime import datetime
from typing import List, Dict, Any, Optional

from playwright.sync_api import Page


DEFECT_SUMMARY_PREFIX = "[Kventin]"


def build_defect_summary(llm_answer: str, url: str) -> str:
    """
    Нормальное название дефекта: кратко и по сути.
    Берём первую осмысленную строку из ответа LLM или формируем по URL/контексту.
    """
    lines = [s.strip() for s in llm_answer.split("\n") if s.strip()]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.upper() in ("СТОП", "ДЕФЕКТ", "ТИКЕТ", "JIRA"):
            continue
        if line.startswith(("summary", "описание", "название", "заголовок")) and ":" in line:
            line = line.split(":", 1)[1].strip()
        if len(line) > 20:
            title = line[: 250].strip()
            if not title.startswith(DEFECT_SUMMARY_PREFIX):
                title = f"{DEFECT_SUMMARY_PREFIX} {title}"
            return title
    from urllib.parse import urlparse
    host = urlparse(url).netloc or "страница"
    return f"{DEFECT_SUMMARY_PREFIX} Обнаружена проблема на {host}"


def build_defect_description(
    llm_answer: str,
    url: str,
    checklist_results: Optional[List[Dict[str, Any]]] = None,
    console_log: Optional[List[Dict[str, Any]]] = None,
    network_failures: Optional[List[Dict[str, Any]]] = None,
    steps_to_reproduce: Optional[List[str]] = None,
) -> str:
    """
    Описание по канонам: шаги воспроизведения, ожидаемый/фактический результат, окружение, фактура.
    steps_to_reproduce: список шагов от агента (путь к багу) для точного воспроизведения.
    """
    sections = []

    sections.append("h3. Описание проблемы\n{quote}\n" + (llm_answer[:4000] if llm_answer else "Обнаружена проблема при автотестировании.") + "\n{quote}")

    if steps_to_reproduce:
        steps_str = "\n".join(f"# {s}" for s in steps_to_reproduce[:20])
        sections.append("h3. Шаги воспроизведения\n# Открыть страницу: " + url + "\n" + steps_str)
    else:
        sections.append("h3. Шаги воспроизведения\n# Открыть страницу: " + url + "\n# Выполнить действия на странице (или дождаться загрузки)\n# Наблюдать консоль/сеть (см. вложения)")

    sections.append("h3. Ожидаемый результат\nОшибок в консоли и сетевых запросах нет (или только ожидаемые). Контент отображается корректно.")

    sections.append("h3. Фактический результат\nОшибки в консоли и/или неуспешные сетевые ответы. Подробности — в приложенных логах (console.log, network.log) и на скриншоте.")

    env = f"URL: {url}\nДата: {datetime.now().isoformat()}\nИсточник: AI-тестировщик Kventin (Playwright, GigaChat)."
    if checklist_results:
        failed = [r for r in checklist_results if not r.get("ok")]
        if failed:
            env += "\n\nРезультаты чеклиста (провалы):\n" + "\n".join(f"* {r.get('title', '')}: {r.get('detail', '')}" for r in failed[:10])
    sections.append("h3. Окружение\n" + env)

    sections.append("h3. Вложения (фактура)\n* screenshot.png — скриншот страницы на момент обнаружения\n* console.log — логи консоли браузера\n* network.log — неуспешные сетевые запросы")

    return "\n\n".join(sections)


def collect_evidence(
    page: Page,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
    temp_dir: Optional[str] = None,
) -> List[str]:
    """
    Собрать фактуру во временные файлы: скриншот, console.log, network.log.
    Возвращает список путей к созданным файлам.
    """
    if temp_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="kventin_defect_")
    os.makedirs(temp_dir, exist_ok=True)
    paths = []

    try:
        screenshot_path = os.path.join(temp_dir, "screenshot.png")
        page.screenshot(path=screenshot_path)
        paths.append(screenshot_path)
    except Exception as e:
        print(f"[Defect] Не удалось сделать скриншот: {e}")

    try:
        console_path = os.path.join(temp_dir, "console.log")
        with open(console_path, "w", encoding="utf-8") as f:
            f.write(f"# Console log\n# URL: {page.url}\n# Date: {datetime.now().isoformat()}\n\n")
            for entry in (console_log or [])[-200:]:
                f.write(f"[{entry.get('type', 'log')}] {entry.get('text', '')}\n")
        paths.append(console_path)
    except Exception as e:
        print(f"[Defect] Не удалось сохранить console.log: {e}")

    try:
        network_path = os.path.join(temp_dir, "network.log")
        with open(network_path, "w", encoding="utf-8") as f:
            f.write(f"# Network failures (non-2xx)\n# URL: {page.url}\n# Date: {datetime.now().isoformat()}\n\n")
            for entry in (network_failures or [])[-100:]:
                f.write(f"{entry.get('status')} {entry.get('method', '')} {entry.get('url', '')}\n")
        paths.append(network_path)
    except Exception as e:
        print(f"[Defect] Не удалось сохранить network.log: {e}")

    return paths
