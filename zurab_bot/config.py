from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    bot_username: str
    db_path: Path
    poll_timeout_seconds: int = 25
    summary_message_limit: int = 120

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Config":
        base_dir = Path(__file__).resolve().parent
        load_dotenv(env_file or base_dir / ".env")

        token = os.getenv("ZURAB_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("ZURAB_BOT_TOKEN is required")

        owner_raw = os.getenv("ZURAB_OWNER_ID", "").strip()
        if not owner_raw.isdigit():
            raise RuntimeError("ZURAB_OWNER_ID must be a numeric Telegram user id")

        db_path = Path(
            os.getenv("ZURAB_DB_PATH", str(base_dir / "data" / "zurab.sqlite3"))
        ).expanduser()

        return cls(
            bot_token=token,
            owner_id=int(owner_raw),
            bot_username=os.getenv(
                "ZURAB_BOT_USERNAME", "ZurabPorucheniyaBot"
            ).lstrip("@"),
            db_path=db_path,
            poll_timeout_seconds=int(os.getenv("ZURAB_POLL_TIMEOUT", "25")),
            summary_message_limit=int(os.getenv("ZURAB_SUMMARY_LIMIT", "120")),
        )

