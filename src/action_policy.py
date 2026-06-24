"""Local policy for ranking and selecting action candidates."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

from src.action_candidates import ActionCandidate, candidate_by_id
from src.element_resolver import norm_key


CTA_WORDS = (
    "save", "submit", "continue", "next", "login", "sign in", "sign up",
    "создать", "сохранить", "отправить", "продолжить", "далее", "войти",
    "зарегистр", "оформить", "оплатить", "поиск", "найти",
)


def _memory_repeat_penalty(memory: Any, cand: ActionCandidate) -> float:
    if memory is None:
        return 0.0
    try:
        if memory.is_already_done_action(cand.as_action()):
            return -1000.0
    except Exception:
        pass
    return 0.0


def score_candidate(cand: ActionCandidate, memory: Any = None) -> float:
    score = 100.0 - float(cand.priority)
    label = norm_key(cand.label, max_len=120)

    if cand.action == "type":
        score += 28
    elif cand.action == "click":
        score += 24
    elif cand.action == "select_option":
        score += 20
    elif cand.action == "upload_file":
        score += 18
    elif cand.action == "hover":
        score += 8
    elif cand.action == "close_modal":
        score += 70

    if any(word in label for word in CTA_WORDS):
        score += 28
    if cand.kind in ("button", "tab", "menuitem"):
        score += 10
    if cand.kind == "link":
        score -= 8
    if any(word in label for word in ("footer", "copyright", "политика", "privacy", "terms")):
        score -= 30

    for flag in cand.risk_flags:
        if flag in ("external", "destructive"):
            score -= 80
        elif flag in ("low_signal", "footer"):
            score -= 25

    score += _memory_repeat_penalty(memory, cand)
    return score


def rank_candidates(candidates: Iterable[ActionCandidate], memory: Any = None) -> List[ActionCandidate]:
    ranked: List[ActionCandidate] = []
    for cand in candidates:
        cand.score = score_candidate(cand, memory)
        ranked.append(cand)
    ranked.sort(key=lambda c: c.score, reverse=True)
    return ranked


def choose_best_candidate(candidates: Iterable[ActionCandidate], memory: Any = None) -> Optional[ActionCandidate]:
    ranked = rank_candidates(candidates, memory)
    for cand in ranked:
        if cand.score > -500:
            return cand
    return ranked[0] if ranked else None


def action_from_llm_candidate_choice(raw: str, candidates: Iterable[ActionCandidate]) -> Optional[Dict[str, Any]]:
    """Parse an LLM response that selected one of the provided candidate ids."""
    if not raw:
        return None
    candidate_id = ""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip(), flags=re.MULTILINE)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            candidate_id = str(parsed.get("candidate_id") or parsed.get("id") or parsed.get("candidate") or "")
    except json.JSONDecodeError:
        match = re.search(r'"(?:candidate_id|candidate|id)"\s*:\s*"([^"]+)"', raw)
        if match:
            candidate_id = match.group(1)

    if not candidate_id:
        return None
    cand = candidate_by_id(candidates, candidate_id)
    if not cand:
        return None
    action = cand.as_action()
    action["_candidate_id"] = cand.id
    action["_candidate_score"] = cand.score
    return action


__all__ = [
    "action_from_llm_candidate_choice",
    "choose_best_candidate",
    "rank_candidates",
    "score_candidate",
]
