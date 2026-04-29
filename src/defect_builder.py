"""
Сбор дефекта по канонам: нормальное название, структурированное описание, фактура во вложениях.
"""
import os
import tempfile
from datetime import datetime
from typing import List, Dict, Any, Optional

from playwright.sync_api import Page


DEFECT_SUMMARY_PREFIX = "[Kventin]"

# Уровни серьёзности в Kventin; соответствие имён приоритета в Jira — JIRA_PRIORITY_* в config (опционально).
SEVERITY_CRITICAL = "critical"
SEVERITY_MAJOR = "major"
SEVERITY_MINOR = "minor"


def infer_defect_severity(
    summary: str,
    description: str = "",
    console_log: Optional[List[Dict[str, Any]]] = None,
    network_failures: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Определить severity по контексту: critical (5xx, белый экран, crash),
    major (4xx на ключевых ресурсах, нерабочие кнопки), minor (a11y, предупреждения).
    """
    text = (summary + " " + description).lower()
    cons = (console_log or [])
    net = (network_failures or [])

    # Critical: 5xx, белый экран, crash, internal server error
    if any(
        x in text
        for x in (
            "500", "502", "503", "504", "5xx",
            "ошибка сервера", "server error", "internal server error",
            "белый экран", "blank screen", "страница не загружается",
            "crash", "краш", "uncaught exception", "необработанное исключение",
        )
    ):
        return SEVERITY_CRITICAL
    for n in net[-20:]:
        status = n.get("status") or 0
        if status >= 500:
            return SEVERITY_CRITICAL

    # Major: 4xx на документе/API, нерабочие элементы, форма не отправляется
    if any(
        x in text
        for x in (
            "404", "403", "401", "4xx",
            "кнопка не работает", "форма не отправляется", "не находит элемент",
            "not found", "not_found", "element not found",
        )
    ):
        return SEVERITY_MAJOR
    for n in net[-20:]:
        status = n.get("status") or 0
        if 400 <= status < 500:
            return SEVERITY_MAJOR

    # Accessibility, предупреждения консоли — minor
    if any(
        x in text
        for x in (
            "accessibility", "a11y", "контраст", "contrast", "alt", "aria",
            "предупреждение", "warning", "deprecation",
        )
    ):
        return SEVERITY_MINOR

    return SEVERITY_MAJOR  # по умолчанию — major


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


def _format_console_entry_for_description(entry: Dict[str, Any]) -> str:
    """
    Отформатировать одну запись консоли для описания дефекта (Jira-wiki):
    тип, текст, источник (путь до JS + строка/колонка), полный стек-трейс если есть.
    """
    etype = (entry.get("type") or "log").lower()
    text = (entry.get("text") or "").strip()
    src = entry.get("source_url") or entry.get("url") or ""
    line = entry.get("line")
    col = entry.get("column")
    stack = (entry.get("stack") or "").strip()

    head = f"*[{etype}]* {{{{{text[:400]}}}}}"
    loc_parts = []
    if src:
        if line is not None and col is not None:
            loc_parts.append(f"{src}:{line}:{col}")
        elif line is not None:
            loc_parts.append(f"{src}:{line}")
        else:
            loc_parts.append(src)
    loc_line = f"\nИсточник: {{{{{loc_parts[0]}}}}}" if loc_parts else ""
    stack_block = ""
    if stack:
        stack_block = "\nСтек-трейс:\n{code}\n" + stack[:3000] + "\n{code}"
    return head + loc_line + stack_block


def _extract_significant_console(console_log: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Выбрать значимые записи консоли: pageerror, error, warning (error в приоритете)."""
    if not console_log:
        return []
    errors = [c for c in console_log if (c.get("type") or "").lower() in ("pageerror", "error")]
    warnings = [c for c in console_log if (c.get("type") or "").lower() == "warning"]
    # Приоритет: pageerror + error, затем warning; берём последние 8
    selected = errors[-8:]
    if len(selected) < 5:
        selected = selected + warnings[-(5 - len(selected)):]
    return selected


def build_defect_description(
    llm_answer: str,
    url: str,
    checklist_results: Optional[List[Dict[str, Any]]] = None,
    console_log: Optional[List[Dict[str, Any]]] = None,
    network_failures: Optional[List[Dict[str, Any]]] = None,
    steps_to_reproduce: Optional[List[str]] = None,
) -> str:
    """
    Описание по канонам: шаги воспроизведения, ожидаемый/фактический результат,
    ошибки консоли со стеком и путём к JS-файлу, окружение, фактура.
    steps_to_reproduce: список шагов от агента (путь к багу) для точного воспроизведения.
    """
    sections = []

    sections.append(
        "h3. Описание проблемы\n{quote}\n"
        + (llm_answer[:4000] if llm_answer else "Обнаружена проблема при автотестировании.")
        + "\n{quote}"
    )

    if steps_to_reproduce:
        steps_str = "\n".join(f"# {s}" for s in steps_to_reproduce[:30])
        sections.append(
            "h3. Шаги воспроизведения\n"
            "# Открыть страницу: " + url + "\n" + steps_str
        )
    else:
        sections.append(
            "h3. Шаги воспроизведения\n"
            "# Открыть страницу: " + url + "\n"
            "# Выполнить действия на странице (или дождаться загрузки)\n"
            "# Наблюдать консоль/сеть (см. вложения и раздел «Ошибки консоли» ниже)"
        )

    sections.append(
        "h3. Ожидаемый результат\n"
        "Ошибок в консоли и сетевых запросах нет (или только ожидаемые). "
        "Контент отображается корректно."
    )

    sections.append(
        "h3. Фактический результат\n"
        "Зафиксированы ошибки в консоли и/или неуспешные сетевые ответы. "
        "Подробности — ниже (стек-трейс, путь до JS-файла) и в приложенных логах."
    )

    # Блок с ошибками консоли — стек-трейсы + путь к JS-файлу
    significant = _extract_significant_console(console_log)
    if significant:
        console_lines = ["h3. Ошибки консоли (со стеком и путём до JS)"]
        for idx, entry in enumerate(significant, 1):
            console_lines.append(f"\n*#{idx}.* " + _format_console_entry_for_description(entry))
        sections.append("\n".join(console_lines))

    # Сетевые ошибки — краткая сводка
    if network_failures:
        critical_net = [
            n for n in network_failures
            if isinstance(n.get("status"), int) and n["status"] >= 400
        ]
        if critical_net:
            net_lines = ["h3. Ошибки сети (HTTP 4xx/5xx)"]
            for n in critical_net[-15:]:
                net_lines.append(
                    f"* {n.get('status')} {n.get('method', 'GET')} "
                    f"{{{{{(n.get('url') or '')[:200]}}}}}"
                )
            sections.append("\n".join(net_lines))

    env = (
        f"URL: {url}\n"
        f"Дата: {datetime.now().isoformat()}\n"
        f"Источник: AI-тестировщик Kventin (Playwright, GigaChat)."
    )
    if checklist_results:
        failed = [r for r in checklist_results if not r.get("ok")]
        if failed:
            env += "\n\nРезультаты чеклиста (провалы):\n" + "\n".join(
                f"* {r.get('title', '')}: {r.get('detail', '')}" for r in failed[:10]
            )
    sections.append("h3. Окружение\n" + env)

    sections.append(
        "h3. Вложения (фактура)\n"
        "* screenshot.png — скриншот страницы на момент обнаружения\n"
        "* console.log — полные логи консоли браузера (включая стек-трейсы и путь до JS)\n"
        "* network.log — неуспешные сетевые запросы\n"
        "* network.har — полный HAR (HTTP Archive) на момент дефекта: запросы, "
        "ответы, заголовки, тайминги. Открывается в Chrome DevTools (Network → Import HAR)."
    )

    return "\n\n".join(sections)


def collect_evidence(
    page: Page,
    console_log: List[Dict[str, Any]],
    network_failures: List[Dict[str, Any]],
    temp_dir: Optional[str] = None,
    *,
    har_window_seconds: float = 60.0,
    har_last_n: int = 200,
) -> List[str]:
    """
    Собрать фактуру во временные файлы: скриншот, console.log, network.log, network.har.

    HAR прикрепляется, если на странице есть `_agent_net_capture` (NetworkCapture).
    Берём «окно момента» — последние har_window_seconds секунд и не больше har_last_n
    записей, чтобы вложение было компактным и релевантным.
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
                etype = entry.get("type", "log")
                text = entry.get("text", "")
                src = entry.get("source_url") or entry.get("url") or ""
                line = entry.get("line")
                col = entry.get("column")
                stack = entry.get("stack") or ""
                loc = ""
                if src:
                    loc = src
                    if line is not None and col is not None:
                        loc += f":{line}:{col}"
                    elif line is not None:
                        loc += f":{line}"
                f.write(f"[{etype}] {text}\n")
                if loc:
                    f.write(f"  at {loc}\n")
                if stack:
                    f.write("  stack:\n")
                    for s_line in str(stack).splitlines():
                        f.write(f"    {s_line}\n")
                f.write("\n")
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

    # HAR на момент дефекта (окно времени + ограничение по числу записей)
    try:
        net_cap = getattr(page, "_agent_net_capture", None)
        if net_cap is not None and hasattr(net_cap, "dump_har_to"):
            import time as _time
            har_path = os.path.join(temp_dir, "network.har")
            since = _time.time() - max(1.0, float(har_window_seconds))
            ok = net_cap.dump_har_to(
                har_path,
                page_url=page.url,
                since_ts=since,
                last_n=int(har_last_n) if har_last_n else None,
            )
            if ok:
                paths.append(har_path)
    except Exception as e:
        print(f"[Defect] Не удалось сохранить network.har: {e}")

    return paths
