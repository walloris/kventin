from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class StoredMessage:
    chat_id: int
    message_id: int
    sender_id: int
    sender_name: str
    username: str | None
    text: str
    sent_at: int


@dataclass(frozen=True)
class Task:
    id: int
    chat_id: int
    description: str
    assignee: str | None
    status: str
    created_at: str


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_name TEXT NOT NULL,
                    username TEXT,
                    text TEXT NOT NULL,
                    sent_at INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_chat_sent
                    ON messages(chat_id, sent_at DESC, message_id DESC);

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    creator_id INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    assignee TEXT,
                    source_message_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_chat_status
                    ON tasks(chat_id, status, id DESC);
                """
            )

    def upsert_chat(self, chat_id: int, title: str, enabled: bool | None = None) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT enabled FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            current = bool(row["enabled"]) if row else False
            value = current if enabled is None else enabled
            connection.execute(
                """
                INSERT INTO chats(chat_id, title, enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (chat_id, title, int(value), utc_now()),
            )

    def set_chat_enabled(self, chat_id: int, title: str, enabled: bool) -> None:
        self.upsert_chat(chat_id, title, enabled=enabled)

    def is_chat_enabled(self, chat_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT enabled FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return bool(row and row["enabled"])

    def add_message(self, message: StoredMessage) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO messages(
                    chat_id, message_id, sender_id, sender_name, username,
                    text, sent_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.chat_id,
                    message.message_id,
                    message.sender_id,
                    message.sender_name,
                    message.username,
                    message.text,
                    message.sent_at,
                    utc_now(),
                ),
            )

    def recent_messages(self, chat_id: int, limit: int) -> list[StoredMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, message_id, sender_id, sender_name, username,
                       text, sent_at
                FROM messages
                WHERE chat_id = ?
                ORDER BY sent_at DESC, message_id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [StoredMessage(**dict(row)) for row in reversed(rows)]

    def create_task(
        self,
        chat_id: int,
        creator_id: int,
        description: str,
        source_message_id: int | None = None,
        assignee: str | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tasks(
                    chat_id, creator_id, description, assignee,
                    source_message_id, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    chat_id,
                    creator_id,
                    description.strip(),
                    assignee,
                    source_message_id,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def open_tasks(self, chat_id: int, limit: int = 50) -> list[Task]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, chat_id, description, assignee, status, created_at
                FROM tasks
                WHERE chat_id = ? AND status = 'open'
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [Task(**dict(row)) for row in rows]

    def complete_task(self, chat_id: int, task_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = 'done', completed_at = ?
                WHERE chat_id = ? AND id = ? AND status = 'open'
                """,
                (utc_now(), chat_id, task_id),
            )
            return cursor.rowcount == 1

