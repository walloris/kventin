"""
Сбор дефекта по канонам: нормальное название, структурированное описание, фактура во вложениях.
"""
import json
import os
import re
import tempfile
from datetime import datetime
from typing import Any, List, Dict, Optional, Tuple

from playwright.sync_api import Page


DEFECT_SUMMARY_PREFIX = "[Kventin]"

# Версия машинного сценария в описании тикета (для main.py --retest-kventin).
RETEST_SPEC_VERSION = 1
RETEST_JSON_MARKER = "KVENTIN_RETEST_JSON_V1"
# Ограничение размера JSON в поле Description Jira.
RETEST_PLAN_MAX_STEPS = 28
RETEST_JSON_MAX_CHARS = 26000
RETEST_SIGNAL_MAX_ITEMS = 8

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


def playwright_canonical_to_exec_selector(canon: str) -> str:
    """
    Строка канонического локатора (Playwright-стиль в тексте дефекта) → то, что понимает _find_element:
    CSS, атрибут, короткий текст для getByText/getByRole-name.
    """
    c = (canon or "").strip()
    c = re.sub(r"^\{\{\s*|\s*\}\}$", "", c).strip()
    if not c:
        return ""
    m = re.match(r"getByTestId\(['\"]([^'\"]+)['\"]\)", c, re.I)
    if m:
        return f'[data-testid="{m.group(1)}"]'
    m = re.match(
        r"getByRole\(['\"]([^'\"]+)['\"]\s*,\s*\{\s*name:\s*['\"]([^'\"]+)['\"]",
        c,
        re.I,
    )
    if m:
        return m.group(2).strip()
    m = re.match(r"getByLabel\(['\"]([^'\"]+)['\"]\)", c, re.I)
    if m:
        return m.group(1).strip()
    m = re.match(r"getByPlaceholder\(['\"]([^'\"]+)['\"]\)", c, re.I)
    if m:
        return m.group(1).strip()
    m = re.match(r"getByText\(['\"]([^'\"]+)['\"]\)", c, re.I)
    if m:
        return m.group(1).strip()
    if c.startswith("#") or c.startswith("[") or c.startswith("."):
        return c
    return c


def _selector_for_retest_step(canonical_locator: str, selector: str) -> str:
    c = (canonical_locator or "").strip()
    if c:
        return playwright_canonical_to_exec_selector(c) or c
    return (selector or "").strip()


def _short_text_signature(text: str, *, max_len: int = 220) -> str:
    """Stable, compact text signature for later retest matching."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    # Drop long volatile ids/hashes while keeping the useful error wording.
    text = re.sub(r"\b[0-9a-f]{16,}\b", "<hash>", text, flags=re.I)
    text = re.sub(r"\b\d{10,}\b", "<num>", text)
    return text[:max_len]


def _network_signature(entry: Dict[str, Any]) -> Dict[str, Any]:
    url = (entry.get("url") or "").strip()
    path = url
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = parsed.path or "/"
    except Exception:
        pass
    return {
        "status": int(entry.get("status") or 0),
        "method": (entry.get("method") or "GET").upper()[:12],
        "url_path": path[:300],
    }


def build_retest_oracle(
    bug_description: str,
    *,
    memory: Any = None,
    console_log: Optional[List[Dict[str, Any]]] = None,
    network_failures: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Build deterministic signals that mean the defect still reproduces.

    The retest should not ask an LLM whether the bug is fixed. It should replay
    steps and then check exact signals from the original failure: action errors,
    console errors, and HTTP failures.
    """
    oracle: Dict[str, Any] = {
        "bug_summary": _short_text_signature(bug_description, max_len=600),
        "pass_condition": (
            "Все шаги ретеста выполняются без action_error/not_found, "
            "и после воспроизведения не появляются перечисленные console/network сигналы."
        ),
        "fail_on_action_result_contains": [
            "click_error",
            "type_error",
            "hover_error",
            "not_found",
            "timeout",
            "possible_dead_click",
        ],
        "fail_on_console_contains": [],
        "fail_on_network": [],
    }

    if memory is not None:
        try:
            for action in reversed(getattr(memory, "actions", []) or []):
                result = _short_text_signature(action.get("result") or "")
                low = result.lower()
                if result and any(x in low for x in ("error", "not_found", "timeout", "dead_click")):
                    oracle["original_action_failure"] = {
                        "action": (action.get("action") or "")[:40],
                        "selector": (action.get("canonical_locator") or action.get("selector") or "")[:500],
                        "result": result,
                    }
                    break
        except Exception:
            pass

    console_signals: List[str] = []
    for entry in (console_log or [])[-80:]:
        etype = (entry.get("type") or "").lower()
        if etype not in ("pageerror", "error"):
            continue
        text = _short_text_signature(entry.get("text") or "")
        if text and text not in console_signals:
            console_signals.append(text)
        if len(console_signals) >= RETEST_SIGNAL_MAX_ITEMS:
            break
    oracle["fail_on_console_contains"] = console_signals

    network_signals: List[Dict[str, Any]] = []
    seen_net = set()
    for entry in (network_failures or [])[-80:]:
        status = int(entry.get("status") or 0)
        if status < 400:
            continue
        sig = _network_signature(entry)
        key = (sig["status"], sig["method"], sig["url_path"])
        if key in seen_net:
            continue
        seen_net.add(key)
        network_signals.append(sig)
        if len(network_signals) >= RETEST_SIGNAL_MAX_ITEMS:
            break
    oracle["fail_on_network"] = network_signals
    return oracle


def memory_actions_to_retest_plan(
    memory: Any,
    current_url: str,
    *,
    bug_description: str = "",
    console_log: Optional[List[Dict[str, Any]]] = None,
    network_failures: Optional[List[Dict[str, Any]]] = None,
    max_steps: int = RETEST_PLAN_MAX_STEPS,
) -> Optional[Dict[str, Any]]:
    """
    Собрать машинный сценарий ретеста из журнала действий AgentMemory.

    Селекторы по возможности — стабильные (data-testid, #id, текст роли), без ref:N,
    если в шаге был заполнен canonical_locator.
    """
    if memory is None or not hasattr(memory, "actions"):
        return None
    actions = getattr(memory, "actions") or []
    if not actions:
        return None
    cap = max(5, min(max_steps, RETEST_PLAN_MAX_STEPS))
    recent = list(actions)[-cap:]

    start_nav = (getattr(memory, "_start_url_nav", None) or "").strip()
    start_url = start_nav or (current_url or "").strip()
    primary = ""
    try:
        primary = memory.last_canonical_locator() if hasattr(memory, "last_canonical_locator") else ""
    except Exception:
        primary = ""

    steps_out: List[Dict[str, Any]] = []
    last_emit_url = ""

    for entry in recent:
        if not isinstance(entry, dict):
            continue
        act = (entry.get("action") or "").lower().strip()
        if act in ("explore", "check_defect", ""):
            continue

        url_b = (entry.get("url_before") or "").strip()
        if url_b and url_b != last_emit_url:
            steps_out.append({"op": "navigate", "url": url_b[:2000]})
            last_emit_url = url_b
            if not start_url:
                start_url = url_b

        canon = (entry.get("canonical_locator") or "").strip()
        sel_raw = (entry.get("selector") or "").strip()
        sel = _selector_for_retest_step(canon, sel_raw)
        val = entry.get("value")
        val_str = "" if val is None else str(val)

        if act == "click":
            if not sel:
                continue
            steps_out.append({"op": "click", "selector": sel[:500]})
        elif act == "hover":
            if not sel:
                continue
            steps_out.append({"op": "hover", "selector": sel[:500]})
        elif act == "type":
            if not sel:
                continue
            steps_out.append({
                "op": "type",
                "selector": sel[:500],
                "value": val_str[:500],
            })
        elif act == "scroll":
            direction = (sel_raw or "down").lower()
            if direction not in ("up", "down", "вверх", "вниз"):
                direction = "down"
            if direction in ("вверх", "up"):
                steps_out.append({"op": "scroll", "direction": "up"})
            else:
                steps_out.append({"op": "scroll", "direction": "down"})
        elif act == "close_modal":
            steps_out.append({"op": "close_modal"})
        elif act == "press_key":
            key = val_str or sel_raw or "Escape"
            steps_out.append({"op": "press_key", "key": key[:40]})
        elif act == "select_option":
            if not sel:
                continue
            steps_out.append({
                "op": "select_option",
                "selector": sel[:500],
                "value": val_str[:300],
            })
        elif act == "fill_form":
            steps_out.append({"op": "fill_form"})
        elif act == "upload_file":
            if not sel:
                continue
            steps_out.append({
                "op": "upload_file",
                "selector": sel[:500],
                "path": val_str[:500],
            })

    if not steps_out:
        return None

    if not start_url:
        start_url = (current_url or "").strip()
    if start_url and not start_url.startswith("http"):
        start_url = "https://" + start_url

    plan: Dict[str, Any] = {
        "kventin_retest_version": RETEST_SPEC_VERSION,
        "start_url": start_url[:2000],
        "primary_locator": playwright_canonical_to_exec_selector(primary) or primary[:600],
        "oracle": build_retest_oracle(
            bug_description,
            memory=memory,
            console_log=console_log,
            network_failures=network_failures,
        ),
        "steps": steps_out,
    }
    return plan


def _shrink_retest_plan_for_jira(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Укоротить steps, пока сериализованный JSON не влезает в лимит."""
    p = dict(plan)
    steps = list(p.get("steps") or [])
    while steps:
        raw = json.dumps({**p, "steps": steps}, ensure_ascii=False)
        if len(raw) <= RETEST_JSON_MAX_CHARS:
            p["steps"] = steps
            return p
        if len(steps) <= 8:
            p["steps"] = steps
            return p
        steps = steps[len(steps) // 4 :]  # отбросить начало (обычно менее релевантно)
    p["steps"] = []
    return p


def format_retest_spec_wiki(plan: Dict[str, Any]) -> str:
    """
    Вики-блок для Jira: маркер + JSON в monospaced code (удобно копировать и парсить ретестом).

    Не заменяет человекочитаемые шаги — дополняет их для автоматизации.
    """
    slim = _shrink_retest_plan_for_jira(plan)
    payload = json.dumps(slim, ensure_ascii=False, indent=2)
    return (
        "h3. Сценарий автоматического ретеста (Kventin)\n"
        "Ниже — машиночитаемый сценарий и oracle для автоматического ретеста "
        "(из корня проекта: {code}python main.py --retest-kventin{code}). "
        "Строка маркера KVENTIN_RETEST_JSON_V1 и JSON не удаляются: без них ретест "
        "опирается только на текстовые шаги в разделе «Шаги воспроизведения». "
        "Oracle фиксирует конкретные сигналы исходного бага, чтобы ретест не принимал "
        "решение по догадкам.\n\n"
        f"{RETEST_JSON_MARKER}\n"
        f"{{code}}\n{payload}\n{{code}}"
    )


def parse_retest_plan_from_description(description: str) -> Optional[Dict[str, Any]]:
    """Извлечь dict сценария ретеста из описания задачи (после маркера)."""
    if not description or RETEST_JSON_MARKER not in description:
        return None
    idx = description.find(RETEST_JSON_MARKER)
    chunk = description[idx + len(RETEST_JSON_MARKER):].strip()
    code_match = re.search(r"\{code\}\s*(.*?)\s*\{code\}", chunk, flags=re.IGNORECASE | re.DOTALL)
    if code_match:
        chunk = code_match.group(1).strip()
    else:
        # Backward-compatible fallback for old descriptions where the JSON was the
        # final body after the marker.
        chunk = re.sub(r"^\{code\}\s*", "", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r"\s*\{code\}\s*\Z", "", chunk, flags=re.IGNORECASE).strip()
    try:
        plan = json.loads(chunk)
    except json.JSONDecodeError:
        return None
    if int(plan.get("kventin_retest_version", 0)) != RETEST_SPEC_VERSION:
        return None
    if not isinstance(plan.get("steps"), list):
        return None
    return plan


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
    retest_spec_wiki: Optional[str] = None,
) -> str:
    """
    Описание по канонам: шаги воспроизведения, ожидаемый/фактический результат,
    ошибки консоли со стеком и путём к JS-файлу, окружение, фактура.
    steps_to_reproduce: список шагов от агента (путь к багу) для точного воспроизведения.
    retest_spec_wiki: опциональный блок «KVENTIN_RETEST_JSON_V1» для автоматического ретеста.
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

    if retest_spec_wiki and retest_spec_wiki.strip():
        sections.append(retest_spec_wiki.strip())

    sections.append(
        "h3. Критерий ретеста\n"
        "Автоматический ретест должен выполнить шаги воспроизведения и проверить oracle "
        "из блока KVENTIN_RETEST_JSON_V1: исходные action/console/network сигналы не должны повториться. "
        "Если шаг воспроизведения снова падает или появляется тот же сигнал, дефект считается не исправленным."
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
        f"Источник: AI-тестировщик Kventin (Playwright, local LLM)."
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
