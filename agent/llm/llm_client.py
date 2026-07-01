"""LLM facade for the agent.

The agent talks only to a local OpenAI-compatible endpoint through
:class:`agent.llm.local_openai_client.LocalOpenAIClient`.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from agent.llm.local_openai_client import LocalOpenAIClient

LOG = logging.getLogger("LocalLLM")
if not LOG.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[LocalLLM] %(levelname)s %(message)s"))
    LOG.addHandler(handler)

_client: Optional[LocalOpenAIClient] = None


def _get_client() -> LocalOpenAIClient:
    global _client
    if _client is None:
        _client = LocalOpenAIClient()
        LOG.info("Using local OpenAI-compatible LLM: %s", _client.chat_url)
    return _client


def init_llm_connection() -> bool:
    """Compatibility name: initialize the local OpenAI-compatible LLM connection."""
    try:
        client = _get_client()
        out = client.query("Ответь одним словом: ок", system="Ты отвечаешь только одним словом.")
        if not out:
            LOG.warning("Local LLM did not return a smoke-test response")
            return False
        LOG.info("Local LLM is ready")
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Local LLM init failed: %s", exc)
        return False


def ask_llm(prompt: str, **kwargs: Any) -> Optional[str]:
    """Compatibility name: ask the local LLM."""
    result = _get_client().query(prompt, system=kwargs.get("system"))
    return result if result else None


def consult_agent(context: str, question: str) -> Optional[str]:
    full_prompt = f"""Контекст:
{context}

Вопрос: {question}"""
    return ask_llm(full_prompt)


def _llm_call_with_retry(prompt: str, screenshot_b64: Optional[str] = None, system: Optional[str] = None) -> Optional[str]:
    try:
        from config import LLM_RETRY_COUNT, LLM_RETRY_BASE_DELAY
    except ImportError:
        LLM_RETRY_COUNT, LLM_RETRY_BASE_DELAY = 1, 1.0

    last_result = None
    for attempt in range(max(1, LLM_RETRY_COUNT)):
        result = _get_client().chat_with_screenshot(prompt, screenshot_b64=screenshot_b64, system=system)
        if result and result.strip():
            return result
        last_result = result
        if attempt < LLM_RETRY_COUNT - 1:
            delay = LLM_RETRY_BASE_DELAY * (2 ** attempt)
            LOG.warning("LLM retry %d/%d: empty response, sleep %.1fs", attempt + 1, LLM_RETRY_COUNT, delay)
            time.sleep(delay)
    return last_result


VALID_ACTIONS = {"click", "type", "scroll", "hover", "close_modal", "select_option", "press_key", "check_defect", "explore", "fill_form", "upload_file"}


def validate_llm_action(action: dict) -> dict:
    """Validate and normalize a JSON action returned by the local LLM."""
    act = (action.get("action") or "").strip().lower()
    rus_map = {
        "кликнуть": "click", "клик": "click", "нажать": "click",
        "ввести": "type", "ввод": "type", "набрать": "type",
        "прокрутить": "scroll", "прокрутка": "scroll",
        "навести": "hover", "наведение": "hover",
        "закрыть": "close_modal", "закрыть модалку": "close_modal",
        "выбрать": "select_option", "выбрать опцию": "select_option",
        "клавиша": "press_key",
        "дефект": "check_defect", "баг": "check_defect",
        "исследовать": "explore", "обзор": "explore",
    }
    act = rus_map.get(act, act)
    if act not in VALID_ACTIONS:
        LOG.warning("validate_llm_action: unknown action %r, fallback to explore", act)
        act = "explore"
    action["action"] = act

    sel = (action.get("selector") or "").strip()
    val = (action.get("value") or "").strip()
    if act in ("click", "hover") and not sel:
        LOG.warning("validate_llm_action: empty selector for %s", act)
    if act == "type" and (not sel or not val):
        LOG.warning("validate_llm_action: empty selector or value for type")
    return action


def _build_system_prompt(
    phase_instruction: Optional[str] = None,
    tester_phase: Optional[str] = None,
    has_overlay: bool = False,
) -> str:
    base = """Ты — опытный ручной тестировщик веб-приложений. Ты выполняешь ОДНО действие за шаг, проверяешь результат, затем решаешь следующий шаг.

ЭЛЕМЕНТЫ СТРАНИЦЫ:
Каждый элемент пронумерован: [N] тип "текст" атрибуты.
Используй "ref:N" как selector (N = число из квадратных скобок).
Пример: [42] button "Войти" -> selector = "ref:42"

Принципы:
1) ВСЕГДА указывай selector = "ref:N". НИКОГДА не используй CSS-селекторы, текст или aria-label как selector.
2) Один шаг — одна цель: test_goal (что проверяю) и expected_outcome (что должно произойти).
3) Не повторяй одно и то же. Если уже проверял элемент — переходи к другому.
4) Дефекты: только воспроизводимые баги приложения. Не 404, не флак, не сбой среды.
5) Служебный оверлей (Kventin, AI-тестировщик) — НЕ часть приложения. Игнорируй его.
6) Верстка: оценивай расположение (наложения, обрезки, сломанная сетка, кнопки вне экрана).

СТРОГО JSON (без markdown):
Если в вопросе есть блок "КАНДИДАТЫ ДЕЙСТВИЙ", предпочитай короткий формат:
{
  "candidate_id": "cN",
  "reason": "почему выбран этот кандидат"
}

Если кандидатов нет или нужно явно сообщить баг, используй полный формат:
{
  "action": "click|type|scroll|hover|close_modal|select_option|press_key|check_defect|fill_form|upload_file",
  "selector": "ref:N (число из [N] в списке элементов)",
  "value": "текст (type) / опция (select_option) / клавиша (press_key)",
  "reason": "зачем",
  "test_goal": "что проверяю",
  "expected_outcome": "что должно произойти",
  "observation": "что вижу (кратко)",
  "possible_bug": "описание бага или null",
  "layout_issue": "проблема верстки или null"
}

Приоритет элементов: CTA -> формы -> навигация -> меню -> футер -> мелочи.
В формах — реалистичные тестовые данные (test@test.com, Иван Тестов, +79991234567).
НЕ предлагай СТОП."""
    blocks = []
    if phase_instruction:
        blocks.append(f"\n{phase_instruction}")
    if tester_phase:
        blocks.append(f"(текущая фаза: {tester_phase})")
    if has_overlay:
        blocks.append("""
Модалки/оверлеи: сначала протестируй содержимое (кнопки, поля), потом закрой (close_modal).
Дропдауны: открыть -> выбрать опцию -> проверить. Тултипы: hover -> проверить текст.""")
    return base + "\n".join(blocks)


def consult_agent_with_screenshot(
    context: str,
    question: str,
    screenshot_b64: Optional[str] = None,
    phase_instruction: Optional[str] = None,
    tester_phase: Optional[str] = None,
    has_overlay: bool = False,
) -> Optional[str]:
    system = _build_system_prompt(phase_instruction, tester_phase, has_overlay)
    return _llm_call_with_retry(f"{context}\n\n{question}", screenshot_b64=screenshot_b64, system=system)


def get_test_plan_from_screenshot(screenshot_b64: Optional[str], url: str) -> List[str]:
    system = "Ты — тест-аналитик. По скриншоту главной страницы составь краткий тест-план. Отвечай ТОЛЬКО нумерованным списком из 5-7 шагов на русском, по одному шагу на строку."
    prompt = f"URL: {url}\n\nСоставь тест-план из 5-7 конкретных шагов для тестирования этой страницы."
    raw = _llm_call_with_retry(prompt, screenshot_b64=screenshot_b64, system=system)
    if not raw:
        return []
    steps = []
    for line in raw.strip().split("\n"):
        line = re.sub(r"^\d+[\.\)]\s*", "", line.strip())
        if len(line) > 10:
            steps.append(line[:200])
    return steps[:10]


def get_structured_test_plan(
    screenshot_b64: Optional[str],
    url: str,
    *,
    page_summary: str = "",
    modules: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    modules = modules or []
    module_names = "; ".join(m.get("name", "")[:40] for m in modules[:10] if m.get("name"))
    modules_block = f"\nМодули страницы: {module_names}" if module_names else ""
    system = (
        "Ты — старший тест-аналитик. Верни только JSON-массив объектов без markdown. "
        "Поля: id, area, module, title, intent, expected, priority(smoke|critical|exploratory)."
    )
    prompt = (
        f"URL: {url}\nОписание страницы: {page_summary[:600] if page_summary else '-'}{modules_block}\n\n"
        "Выдай тест-план в виде JSON-массива из 6-10 пунктов."
    )
    raw = _llm_call_with_retry(prompt, screenshot_b64=screenshot_b64, system=system) or ""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip(), flags=re.MULTILINE)
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        parsed = []
    items: List[Dict[str, Any]] = []
    if isinstance(parsed, list):
        for i, raw_item in enumerate(parsed[:12]):
            if not isinstance(raw_item, dict):
                continue
            item = {
                "id": str(raw_item.get("id") or f"plan-{i+1}")[:40],
                "area": str(raw_item.get("area") or "")[:30],
                "module": str(raw_item.get("module") or "")[:60],
                "title": str(raw_item.get("title") or "")[:200],
                "intent": str(raw_item.get("intent") or "")[:200],
                "expected": str(raw_item.get("expected") or "")[:300],
                "priority": str(raw_item.get("priority") or "exploratory").lower()[:20],
            }
            if item["title"]:
                items.append(item)
    if items:
        return items
    return [
        {
            "id": f"plan-{i+1}",
            "area": "",
            "module": "",
            "title": step,
            "intent": "",
            "expected": "",
            "priority": "smoke" if i < 3 else ("critical" if i < 6 else "exploratory"),
        }
        for i, step in enumerate(get_test_plan_from_screenshot(screenshot_b64, url))
    ]


def ask_is_this_really_bug(bug_description: str, screenshot_b64: Optional[str]) -> bool:
    system = "Ты — ревьюер дефектов. Ответь СТРОГО одним словом: ДА, если это реальный баг приложения; НЕТ, если это не баг, флак или проблема среды."
    prompt = f"Описание:\n{bug_description[:1500]}\n\nЭто точно баг приложения? Ответь ДА или НЕТ."
    raw = _llm_call_with_retry(prompt, screenshot_b64=screenshot_b64, system=system)
    if not raw:
        return True
    low = raw.strip().lower()
    if "нет" in low or "не баг" in low or "не дефект" in low:
        return False
    return "да" in low
