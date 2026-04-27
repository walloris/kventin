"""
Парсинг и валидация действий, полученных от LLM (GigaChat).

Отдельный лёгкий модуль — без зависимостей на Page или AgentMemory, чтобы его
можно было импортировать из любого места (agent_checks, defect_pipeline,
тесты) без риска циклов.

- parse_llm_action(raw)     — выдрать JSON-объект {action: ...} из сырого ответа.
- validate_llm_action(act)  — нормализация полей (реэкспорт из gigachat_client).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from src.gigachat_client import validate_llm_action  # re-export


def parse_llm_action(raw: str) -> Optional[Dict[str, Any]]:
    """Попытаться распарсить JSON-действие из ответа GigaChat."""
    if not raw:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip(), flags=re.MULTILINE)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and "action" in obj:
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{[^{}]*"action"\s*:\s*"[^"]+?"[^{}]*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


__all__ = ["parse_llm_action", "validate_llm_action"]
