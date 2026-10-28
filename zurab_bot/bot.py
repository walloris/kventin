from __future__ import annotations

import json
import logging
import re
import signal
import sys
import time
from typing import Any

import requests

from zurab_bot.config import Config
from zurab_bot.storage import Storage, StoredMessage
from zurab_bot.task_parser import (
    extract_candidates,
    format_candidates,
    strip_bot_invocation,
)


LOG = logging.getLogger("zurab")
MAX_TELEGRAM_TEXT = 3900


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def call(
        self,
        method: str,
        data: dict[str, Any] | None = None,
        timeout: int = 35,
    ) -> Any:
        response = self.session.post(
            f"{self.base_url}/{method}", data=data or {}, timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error in {method}: {payload.get('description')}")
        return payload.get("result")

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        data: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "edited_message", "my_chat_member"]),
        }
        if offset is not None:
            data["offset"] = offset
        return self.call("getUpdates", data, timeout=timeout + 10)

    def send_message(
        self, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> None:
        for start in range(0, len(text), MAX_TELEGRAM_TEXT):
            data: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text[start : start + MAX_TELEGRAM_TEXT],
                "disable_web_page_preview": "true",
            }
            if reply_to_message_id and start == 0:
                data["reply_to_message_id"] = reply_to_message_id
                data["allow_sending_without_reply"] = "true"
            self.call("sendMessage", data)

    def set_commands(self) -> None:
        commands = [
            {"command": "enable", "description": "Начать сохранять сообщения этого чата"},
            {"command": "disable", "description": "Остановить сохранение сообщений"},
            {"command": "task", "description": "Добавить поручение"},
            {"command": "tasks", "description": "Показать открытые поручения"},
            {"command": "done", "description": "Закрыть поручение по номеру"},
            {"command": "summary", "description": "Собрать поручения из последних сообщений"},
            {"command": "help", "description": "Показать помощь"},
        ]
        self.call("setMyCommands", {"commands": json.dumps(commands, ensure_ascii=False)})


class ZurabBot:
    def __init__(self, config: Config):
        self.config = config
        self.api = TelegramAPI(config.bot_token)
        self.storage = Storage(config.db_path)
        self.running = True

    def stop(self, *_: Any) -> None:
        self.running = False

    def run(self) -> None:
        self.api.set_commands()
        offset: int | None = None
        LOG.info("Zurab started as @%s", self.config.bot_username)
        while self.running:
            try:
                updates = self.api.get_updates(offset, self.config.poll_timeout_seconds)
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self.handle_update(update)
            except requests.RequestException as error:
                LOG.warning("Telegram connection error: %s", error)
                time.sleep(3)
            except Exception:
                LOG.exception("Unhandled update error")
                time.sleep(1)

    def handle_update(self, update: dict[str, Any]) -> None:
        membership = update.get("my_chat_member")
        if membership:
            self._handle_membership(membership)
            return

        message = update.get("message") or update.get("edited_message")
        if not message or not message.get("from") or message.get("from", {}).get("is_bot"):
            return

        sender = message["from"]
        chat = message["chat"]
        chat_id = int(chat["id"])
        sender_id = int(sender["id"])
        chat_type = chat.get("type", "")
        text = (message.get("text") or message.get("caption") or "").strip()
        title = chat.get("title") or chat.get("username") or "private"
        is_private = chat_type == "private"
        is_owner = sender_id == self.config.owner_id

        self.storage.upsert_chat(chat_id, title, enabled=True if is_private else None)

        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        if is_owner and command in {"/enable", "/disable", "/help", "/start"}:
            self._handle_owner_command(message, command, text)
            return

        enabled = is_private or self.storage.is_chat_enabled(chat_id)
        if not enabled:
            if is_owner and (text.startswith("/") or strip_bot_invocation(text) is not None):
                self.api.send_message(
                    chat_id,
                    "Сначала включи этот чат командой /enable. До этого я сообщения не сохраняю.",
                    message.get("message_id"),
                )
            return

        if text:
            self.storage.add_message(
                StoredMessage(
                    chat_id=chat_id,
                    message_id=int(message["message_id"]),
                    sender_id=sender_id,
                    sender_name=self._sender_name(sender),
                    username=sender.get("username"),
                    text=text,
                    sent_at=int(message.get("date", 0)),
                )
            )

        if not is_owner:
            return

        if command.startswith("/"):
            self._handle_owner_command(message, command, text)
            return

        instruction = strip_bot_invocation(text)
        if instruction is not None:
            self._handle_instruction(message, instruction)

    def _handle_membership(self, membership: dict[str, Any]) -> None:
        chat = membership["chat"]
        status = membership.get("new_chat_member", {}).get("status")
        if status in {"member", "administrator"}:
            self.storage.upsert_chat(int(chat["id"]), chat.get("title", ""), enabled=False)

    def _handle_owner_command(
        self, message: dict[str, Any], command: str, text: str
    ) -> None:
        chat = message["chat"]
        chat_id = int(chat["id"])
        title = chat.get("title") or "private"
        reply_to = int(message["message_id"])

        if command in {"/start", "/help"}:
            self.api.send_message(chat_id, self._help_text(), reply_to)
            return
        if command == "/enable":
            self.storage.set_chat_enabled(chat_id, title, True)
            self.api.send_message(
                chat_id,
                "Чат включён. Сохраняю только новые сообщения и выполняю команды владельца.",
                reply_to,
            )
            return
        if command == "/disable":
            self.storage.set_chat_enabled(chat_id, title, False)
            self.api.send_message(chat_id, "Чат выключен. Новые сообщения не сохраняю.", reply_to)
            return
        if command == "/task":
            description = text.partition(" ")[2].strip()
            if not description:
                self.api.send_message(chat_id, "Формат: /task текст поручения", reply_to)
                return
            task_id = self.storage.create_task(
                chat_id,
                self.config.owner_id,
                description,
                source_message_id=reply_to,
            )
            self.api.send_message(chat_id, f"Поручение #{task_id} записано: {description}", reply_to)
            return
        if command == "/tasks":
            self.api.send_message(chat_id, self._format_tasks(chat_id), reply_to)
            return
        if command == "/done":
            raw_id = text.partition(" ")[2].strip()
            if not raw_id.isdigit():
                self.api.send_message(chat_id, "Формат: /done номер", reply_to)
                return
            completed = self.storage.complete_task(chat_id, int(raw_id))
            answer = f"Поручение #{raw_id} закрыто." if completed else "Открытое поручение не найдено."
            self.api.send_message(chat_id, answer, reply_to)
            return
        if command == "/summary":
            limit = self._parse_summary_limit(text)
            self.api.send_message(chat_id, self._summary(chat_id, limit), reply_to)

    def _handle_instruction(self, message: dict[str, Any], instruction: str) -> None:
        chat_id = int(message["chat"]["id"])
        message_id = int(message["message_id"])
        normalized = instruction.casefold()

        if not instruction:
            self.api.send_message(chat_id, "Слушаю.", message_id)
            return
        if any(phrase in normalized for phrase in ("собери поручения", "что кому", "подведи итоги")):
            self.api.send_message(chat_id, self._summary(chat_id), message_id)
            return
        if any(phrase in normalized for phrase in ("покажи задачи", "список задач", "что записано")):
            self.api.send_message(chat_id, self._format_tasks(chat_id), message_id)
            return

        speak_match = re.match(r"^(?:напиши|скажи)(?:\s+в\s+чат)?\s*[:\-]\s*(.+)$", instruction, re.I | re.S)
        if speak_match:
            self.api.send_message(chat_id, speak_match.group(1).strip())
            return

        task_id = self.storage.create_task(
            chat_id,
            self.config.owner_id,
            instruction,
            source_message_id=message_id,
        )
        self.api.send_message(
            chat_id,
            f"Поручение #{task_id} записал. Если это действие требует внешнего сервиса, нужен отдельный подключённый исполнитель.",
            message_id,
        )

    def _summary(self, chat_id: int, limit: int | None = None) -> str:
        messages = self.storage.recent_messages(
            chat_id, limit or self.config.summary_message_limit
        )
        return format_candidates(extract_candidates(messages))

    def _format_tasks(self, chat_id: int) -> str:
        tasks = self.storage.open_tasks(chat_id)
        if not tasks:
            return "Открытых поручений нет."
        lines = ["Открытые поручения:"]
        for task in reversed(tasks):
            assignee = f" — {task.assignee}" if task.assignee else ""
            lines.append(f"#{task.id}{assignee}: {task.description}")
        return "\n".join(lines)

    def _parse_summary_limit(self, text: str) -> int:
        raw = text.partition(" ")[2].strip()
        if not raw.isdigit():
            return self.config.summary_message_limit
        return min(max(int(raw), 10), 500)

    @staticmethod
    def _sender_name(sender: dict[str, Any]) -> str:
        full_name = " ".join(
            part for part in (sender.get("first_name"), sender.get("last_name")) if part
        ).strip()
        return full_name or sender.get("username") or str(sender["id"])

    def _help_text(self) -> str:
        return (
            "Я Зураб — помощник по поручениям.\n\n"
            "В группе сначала отправь /enable. Команды выполняются только от владельца.\n"
            "/task текст — записать поручение\n"
            "/tasks — показать открытые\n"
            "/done номер — закрыть\n"
            "/summary [10–500] — собрать явные договорённости\n"
            "/disable — перестать сохранять сообщения\n\n"
            "Можно написать: «Зураб, собери поручения» или «Зураб, напиши: текст»."
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        bot = ZurabBot(Config.from_env())
    except Exception as error:
        LOG.error("Configuration error: %s", error)
        return 2

    signal.signal(signal.SIGINT, bot.stop)
    signal.signal(signal.SIGTERM, bot.stop)
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

