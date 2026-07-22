"""
Тестирование задач Jira демоном: exploratory-прогон фичи + генерация тест-кейсов.

Для задачи (не дефекта) в нужном статусе демон:
1. Читает задачу через Jira REST (summary + description).
2. По возможности гоняет фокусный exploratory-тест фичи (Playwright) для сбора фактуры.
3. Генерирует тест-кейсы в формате Zephyr XML строго по образцу
   (skills/test-case-writer) через локальную OpenAI-compatible LLM и валидирует их
   скриптом validate_xml.py (Coverage Gate), повторяя при INVALID.
4. Пишет в задачу комментарий с трассируемостью и прикладывает XML.

Формат и образец берутся из vendored-скилла test-case-writer.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from config import (
    DOC_AS_CODE_DIR,
    JIRA_TASK_EXPLORATORY_STEPS,
    LLM_SKILLS_DIR,
    START_URL,
)
from agent.defects.jira_client import (
    add_issue_comment,
    attach_file_to_issue,
    extract_description_text,
    extract_issue_comment_text,
    get_issue_with_changelog,
)

LOG = logging.getLogger("kventin.task")

_URL_RE = re.compile(r"https?://[^\s\)\]\}>«»\"']+")
_TC_GEN_MAX_RETRIES = 3
_SUCCESS_MARKER_PREFIX = "KVENTIN_TASK_TESTING_V1"
_FAILURE_MARKER_PREFIX = "KVENTIN_TASK_TESTING_FAILED_V1"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _skills_dir() -> Path:
    if LLM_SKILLS_DIR:
        return Path(LLM_SKILLS_DIR)
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


def _task_input_digest(summary: str, description: str) -> str:
    return hashlib.sha256(
        f"{summary}\n{description}".encode("utf-8", errors="replace")
    ).hexdigest()


def _task_marker(prefix: str, summary: str, description: str) -> str:
    return f"{prefix}:{_task_input_digest(summary, description)[:16]}"


def _source_digest_path(xml_path: Path) -> Path:
    return xml_path.with_name(f"{xml_path.name}.source.sha256")


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
        message = f"validate_xml.py не найден: {validator}"
        LOG.error(message)
        return False, message
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
    Сгенерировать XML тест-кейсов через локальную OpenAI-compatible LLM и провалидировать.

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
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key).strip("._") or "issue"
    out_file = out_dir / f"{safe_key}.xml"
    temp_file = out_file.with_suffix(".xml.tmp")
    digest_file = _source_digest_path(out_file)
    temp_digest_file = digest_file.with_name(f"{digest_file.name}.tmp")

    from agent.llm.local_openai_client import LocalOpenAIClient

    client = LocalOpenAIClient()
    last_errors = ""
    for attempt in range(1, _TC_GEN_MAX_RETRIES + 1):
        prompt = _build_tc_prompt(key, summary, description, evidence, example_xml, fix_errors=last_errors)
        raw = client.query(prompt, system="Ты выводишь только XML без пояснений.")
        xml = _strip_code_fences(raw or "")
        if not xml or "<" not in xml:
            last_errors = "LLM вернула пустой/не-XML ответ"
            LOG.warning("%s: попытка %d — нет XML", key, attempt)
            continue
        temp_file.write_text(xml, encoding="utf-8")
        ok, msg = _validate_xml(temp_file, example_path)
        if ok:
            os.replace(temp_file, out_file)
            temp_digest_file.write_text(
                _task_input_digest(summary, description),
                encoding="ascii",
            )
            os.replace(temp_digest_file, digest_file)
            LOG.info("%s: тест-кейсы VALID (попытка %d)", key, attempt)
            return out_file, "VALID"
        last_errors = msg
        LOG.warning("%s: попытка %d INVALID: %s", key, attempt, msg[:200])

    try:
        temp_file.unlink()
    except FileNotFoundError:
        pass
    try:
        temp_digest_file.unlink()
    except FileNotFoundError:
        pass
    return None, last_errors or "не удалось получить валидный XML"


def _run_exploratory(feature_url: str) -> str:
    """
    Фокусный exploratory-прогон с отдельным ограниченным бюджетом задачи.
    """
    if not feature_url:
        return ""
    if JIRA_TASK_EXPLORATORY_STEPS <= 0:
        LOG.info("JIRA_TASK_EXPLORATORY_STEPS=0 — exploratory для задачи отключен")
        return ""
    try:
        from agent.core.agent import run_agent

        res = run_agent(
            start_url=feature_url,
            max_steps=JIRA_TASK_EXPLORATORY_STEPS,
            enable_qa_retests=False,
        ) or {}
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
        True только после подтверждённой публикации актуального XML и комментария.
    """
    code, full, raw_tail = get_issue_with_changelog(key)
    if code != 200 or not full:
        print(f"[task] {key}: не удалось загрузить issue ({code}): {raw_tail[:200]}")
        return False

    fields = full.get("fields") or {}
    summary = (fields.get("summary") or "").strip()
    description = extract_description_text(fields)
    feature_url = _extract_feature_url(description)
    success_marker = _task_marker(_SUCCESS_MARKER_PREFIX, summary, description)
    failure_marker = _task_marker(_FAILURE_MARKER_PREFIX, summary, description)
    existing_comments = extract_issue_comment_text(fields)
    if success_marker in existing_comments:
        LOG.info("%s: текущая версия требований уже обработана", key)
        return True

    example_path, _ = _test_case_writer_paths()
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key).strip("._") or "issue"
    existing_file = _repo_root() / DOC_AS_CODE_DIR / "test-cases" / f"{safe_key}.xml"
    out_file = None
    msg = ""
    if existing_file.is_file() and example_path.is_file():
        digest_file = _source_digest_path(existing_file)
        cached_digest = ""
        try:
            cached_digest = digest_file.read_text(encoding="ascii").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            pass
        valid, _ = _validate_xml(existing_file, example_path)
        if valid and cached_digest == _task_input_digest(summary, description):
            LOG.info("%s: переиспользую валидные локальные тест-кейсы", key)
            out_file, msg = existing_file, "VALID (cached)"

    evidence = ""
    if out_file is None:
        evidence = _run_exploratory(feature_url)
        out_file, msg = generate_test_cases_xml(key, summary, description, evidence)

    if out_file:
        rel = out_file.relative_to(_repo_root()) if out_file.is_relative_to(_repo_root()) else out_file
        comment = (
            "h3. Kventin — тест-кейсы сгенерированы\n"
            f"Сформированы тест-кейсы (Zephyr XML по образцу), валидация структуры: VALID.\n"
            f"Файл: {{{{{rel}}}}}\n"
            f"{{noformat}}{success_marker}{{noformat}}\n"
        )
        if evidence:
            comment += f"Exploratory-прогон: {{quote}}{evidence}{{quote}}\n"
        attached = attach_file_to_issue(key, str(out_file))
        commented = add_issue_comment(key, comment) if attached else False
        if attached and commented:
            print(f"[task] {key}: тест-кейсы готовы → {out_file}")
            return True
        LOG.warning(
            "%s: публикация не завершена (attachment=%s, comment=%s)",
            key,
            attached,
            commented,
        )
        return False
    else:
        if failure_marker not in existing_comments:
            add_issue_comment(
                key,
                "h3. Kventin — тест-кейсы не сгенерированы\n"
                f"Автоматическая генерация не прошла Coverage Gate.\n{{quote}}{msg[:500]}{{quote}}\n"
                f"{{noformat}}{failure_marker}{{noformat}}",
            )
        print(f"[task] {key}: тест-кейсы НЕ готовы: {msg[:200]}")

    return False


def collect_task_issue_keys(status_name: str, max_results: int = 10) -> List[str]:
    """Ключи задач (без лейбла kventin) в заданном статусе для очереди демона."""
    from agent.defects.jira_client import search_issues_by_status

    issues = search_issues_by_status(
        status_name,
        exclude_label=os.getenv("JIRA_DEFECT_LABEL", "kventin"),
        max_results=max_results,
    )
    return [it.get("key") for it in issues if it.get("key")]
