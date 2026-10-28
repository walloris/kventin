from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable

from zurab_bot.storage import StoredMessage


COMMITMENT_RE = re.compile(
    r"\b(?:я|мы)\s+(?:беру|берём|берем|возьму|возьмём|возьмем|куплю|купим|"
    r"принесу|принесём|закажу|закажем|сделаю|сделаем|подготовлю|подготовим|"
    r"замариную|замаринуем|отправлю|напишу)\b",
    re.IGNORECASE,
)
ACTION_RE = re.compile(
    r"\b(?:нужно|надо|необходимо|осталось|главное|давайте|кто\s+(?:бер[её]т|купит|сделает)|"
    r"пусть\s+\S+)\b",
    re.IGNORECASE,
)
BOT_INVOCATION_RE = re.compile(
    r"^\s*(?:@?zurabporucheniyabot|зураб)\s*[,;:\-]?\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Candidate:
    sender_name: str
    text: str


def strip_bot_invocation(text: str) -> str | None:
    match = BOT_INVOCATION_RE.match(text)
    if not match:
        return None
    return text[match.end() :].strip()


def extract_candidates(messages: Iterable[StoredMessage]) -> list[Candidate]:
    unique: OrderedDict[tuple[str, str], Candidate] = OrderedDict()
    for message in messages:
        text = " ".join(message.text.split())
        if not text or text.startswith("/") or BOT_INVOCATION_RE.match(text):
            continue
        if not (COMMITMENT_RE.search(text) or ACTION_RE.search(text)):
            continue
        key = (message.sender_name.casefold(), text.casefold())
        unique[key] = Candidate(message.sender_name, text)
    return list(unique.values())


def format_candidates(candidates: Iterable[Candidate]) -> str:
    grouped: OrderedDict[str, list[str]] = OrderedDict()
    for candidate in candidates:
        grouped.setdefault(candidate.sender_name, []).append(candidate.text)
    if not grouped:
        return "Явных поручений в сохранённых сообщениях не нашёл."

    lines = ["Поручения и договорённости:"]
    for name, items in grouped.items():
        lines.append(f"\n{name}:")
        lines.extend(f"— {item}" for item in items)
    return "\n".join(lines)
