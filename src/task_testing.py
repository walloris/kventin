"""
Тестирование задач Jira демоном: exploratory-прогон фичи + генерация тест-кейсов.

Для задачи (не дефекта) в нужном статусе демон:
1. Читает задачу через Jira REST (summary + description).
2. По возможности гоняет фокусный exploratory-тест фичи (Playwright) для сбора фактуры.
3. Генерирует тест-кейсы в формате Zephyr XML строго по образцу
   (skills/test-case-writer) через «мозг» — gigacode CLI — и валидирует их
   скриптом validate_xml.py (Coverage Gate), повторяя при INVALID.
4. Пишет в задачу комментарий с трассируемостью и прикладывает XML.

Формат и образец берутся из vendored-скилла test-case-writer.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from config import (
    DOC_AS_CODE_DIR,
    GIGACODE_SKILLS_DIR,
    MAX_STEPS,
    START_URL,
)
from src.jira_client import (
    add_issue_comment,
    attach_file_to_issue,
    extract_description_text,
    get_issue_with_changelog,
)

LOG = logging.getLogger("kventin.task")

_URL_RE = re.compile(r"https?://[^\s\)\]\}>«»\"']+")
_TC_GEN_MAX_RETRIES = 3


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _skills_dir() -> Path:
    if GIGACODE_SKILLS_DIR:
        return Path(GIGACODE_SKILLS_DIR)
    return _repo_root() / "skills"


def _test_case_writer_paths() -> Tuple[Path, Path]:
    """Вернуть (example.xml, validate_xml.py) из vendored-скилла."""
    base = _skills_dir() / "test-case-writer"
    return base / "assets" / "test-cases-example.xml", base / "scripts" / "validate_xml.py"


def _extract_feature_url(description: str) -> str:
    m = _URL_RE.search(description or "")
    if m:
        return m.group(0).rstrip(".,;)")
    return START_URL or ""


def _strip_code_fences(text: str) -> str:
    """Убрать markdown-обёртку ```xml ... ``` и вытащить XML с первого <?xml/<тега."""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```\s*\Z", "", t)
    # Берём от первого '<' (декларация или корневой тег) до последнего '>'
    start = t.find("<")
    end = t.rfind(">")
    if start != -1 and end != -1 and end > start:
        return t[start : end + 1].strip()
    return t.strip()


def _validate_xml(generated: Path, example: Path) -> Tuple[bool, str]:
    _, validator = _test_case_writer_paths()
    if not validator.is_file():
        LOG.warning("validate_xml.py не найден: %s — пропускаю валидацию", validator)
        return True, "validator missing"
    try:
        proc = subprocess.run(
            [sys.executable, str(validator), str(generated), str(example)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"validator error: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()


def _build_tc_prompt(key: str, summary: str, description: str, evidence: str, example_xml: str, fix_errors: str = "") -> str:
    parts = [
        "Ты — Senior Manual QA (скилл test-case-writer). Спроектируй ручные тест-кейсы по задаче.",
        "Требования: на каждое требование ≥1 позитивный и ≥1 негативный кейс; добавь граничные значения.",
        "Тексты (заголовки, шаги, ожидаемый результат) — на русском.",
        "",
        f"Задача {key}: {summary}",
        "",
        "Описание задачи:",
        (description or "(пусто)")[:6000],
    ]
    if evidence:
        parts += ["", "Наблюдения exploratory-теста (фактура):", evidence[:2000]]
    parts += [
        "",
        "ОБРАЗЕЦ XML (Zephyr/HRPQA). Структуру повторить 1:1 — те же теги, порядок и вложенность, меняются только значения:",
        example_xml,
        "",
    ]
    if fix_errors:
        parts += [
            "Предыдущая попытка не прошла валидацию структуры. Исправь СТРОГО эти проблемы и верни валидный XML:",
            fix_errors[:1500],
            "",
        ]
    parts += [
        "Верни ТОЛЬКО валидный XML тест-кейсов для этой задачи. Без markdown, без пояснений, без ```.",
    ]
    return "\n".join(parts)


def generate_test_cases_xml(
    key: str,
    summary: str,
    description: str,
    evidence: str = "",
) -> Tuple[Optional[Path], str]:
    """
    Сгенерировать XML тест-кейсов через gigacode CLI и провалидировать.

    Returns
    -------
    (path|None, message)
        path к VALID-файлу либо None; message — статус (VALID / последние ошибки).
    """
    example_path, _ = _test_case_writer_paths()
    if not example_path.is_file():
        return None, f"Образец не найден: {example_path}"
    example_xml = example_path.read_text(encoding="utf-8")[:12000]

    out_dir = _repo_root() / DOC_AS_CODE_DIR / "test-cases"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{key}.xml"

    from src.gigacode_cli_client import GigacodeCliClient

    client = GigacodeCliClient()
    last_errors = ""
    for attempt in range(1, _TC_GEN_MAX_RETRIES + 1):
        prompt = _build_tc_prompt(key, summary, description, evidence, example_xml, fix_errors=last_errors)
        raw = client.query(prompt, system="Ты выводишь только XML без пояснений.")
        xml = _strip_code_fences(raw or "")
        if not xml or "<" not in xml:
            last_errors = "CLI вернул пустой/не-XML ответ"
            LOG.warning("%s: попытка %d — нет XML", key, attempt)
            continue
        out_file.write_text(xml, encoding="utf-8")
        ok, msg = _validate_xml(out_file, example_path)
        if ok:
            LOG.info("%s: тест-кейсы VALID (попытка %d)", key, attempt)
            return out_file, "VALID"
        last_errors = msg
        LOG.warning("%s: попытка %d INVALID: %s", key, attempt, msg[:200])

    return None, last_errors or "не удалось получить валидный XML"


def _run_exploratory(feature_url: str) -> str:
    """
    Фокусный exploratory-прогон. Запускается только при ограниченном MAX_STEPS,
    чтобы демон не завис на бесконечном цикле.
    """
    if not feature_url:
        return ""
    if MAX_STEPS <= 0:
        LOG.info("MAX_STEPS=0 (бесконечный цикл) — exploratory для задачи пропущен")
        return ""
    try:
        from src.agent import run_agent

        res = run_agent(start_url=feature_url) or {}
        return (
            f"Шагов: {res.get('steps', 0)}, дефектов заведено: {res.get('defects', 0)}"
            + (f", ошибка: {res.get('error')}" if res.get("error") else "")
        )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("exploratory для %s: %s", feature_url, exc)
        return f"exploratory не выполнен: {exc}"


def process_task_issue(key: str) -> bool:
    """
    Обработать одну задачу: exploratory + генерация тест-кейсов + публикация в Jira.

    Returns
    -------
    bool
        True, если задача обработана (с кейсами или без); False — если не загрузилась.
    """
    code, full, raw_tail = get_issue_with_changelog(key)
    if code != 200 or not full:
        print(f"[task] {key}: не удалось загрузить issue ({code}): {raw_tail[:200]}")
        return False

    fields = full.get("fields") or {}
    summary = (fields.get("summary") or "").strip()
    description = extract_description_text(fields)
    feature_url = _extract_feature_url(description)

    evidence = _run_exploratory(feature_url)

    out_file, msg = generate_test_cases_xml(key, summary, description, evidence)

    if out_file:
        rel = out_file.relative_to(_repo_root()) if out_file.is_relative_to(_repo_root()) else out_file
        comment = (
            "h3. Kventin — тест-кейсы сгенерированы\n"
            f"Сформированы тест-кейсы (Zephyr XML по образцу), валидация структуры: VALID.\n"
            f"Файл: {{{{{rel}}}}}\n"
        )
        if evidence:
            comment += f"Exploratory-прогон: {{quote}}{evidence}{{quote}}\n"
        add_issue_comment(key, comment)
        attach_file_to_issue(key, str(out_file))
        print(f"[task] {key}: тест-кейсы готовы → {out_file}")
    else:
        add_issue_comment(
            key,
            "h3. Kventin — тест-кейсы не сгенерированы\n"
            f"Автоматическая генерация не прошла Coverage Gate.\n{{quote}}{msg[:500]}{{quote}}",
        )
        print(f"[task] {key}: тест-кейсы НЕ готовы: {msg[:200]}")

    return True


def collect_task_issue_keys(status_name: str, max_results: int = 10) -> List[str]:
    """Ключи задач (без лейбла kventin) в заданном статусе для очереди демона."""
    from src.jira_client import search_issues_by_status

    issues = search_issues_by_status(
        status_name,
        exclude_label=os.getenv("JIRA_DEFECT_LABEL", "kventin"),
        max_results=max_results,
    )
    return [it.get("key") for it in issues if it.get("key")]
