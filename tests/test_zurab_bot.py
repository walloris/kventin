from pathlib import Path

from zurab_bot.storage import Storage, StoredMessage
from zurab_bot.task_parser import extract_candidates, format_candidates, strip_bot_invocation


def message(message_id: int, sender: str, text: str) -> StoredMessage:
    return StoredMessage(
        chat_id=-100,
        message_id=message_id,
        sender_id=message_id,
        sender_name=sender,
        username=None,
        text=text,
        sent_at=message_id,
    )


def test_extracts_commitments_and_open_actions():
    candidates = extract_candidates(
        [
            message(1, "Гуля", "Я возьму арбуз и овощи"),
            message(2, "Егор", "Главное уголь нормально рассчитать"),
            message(3, "Катя", "просто шутка"),
        ]
    )

    assert [(item.sender_name, item.text) for item in candidates] == [
        ("Гуля", "Я возьму арбуз и овощи"),
        ("Егор", "Главное уголь нормально рассчитать"),
    ]
    rendered = format_candidates(candidates)
    assert "Гуля" in rendered
    assert "уголь" in rendered


def test_bot_invocation_parser():
    assert strip_bot_invocation("Зураб, собери поручения") == "собери поручения"
    assert strip_bot_invocation("@ZurabPorucheniyaBot: напиши: готово") == "напиши: готово"
    assert strip_bot_invocation("обычное сообщение") is None


def test_storage_chat_messages_and_tasks(tmp_path: Path):
    storage = Storage(tmp_path / "zurab.sqlite3")
    storage.set_chat_enabled(-100, "group", True)
    assert storage.is_chat_enabled(-100)

    stored = message(1, "Александр", "Я беру печку")
    storage.add_message(stored)
    storage.add_message(stored)
    assert storage.recent_messages(-100, 10) == [stored]

    task_id = storage.create_task(-100, 42, "Купить уголь")
    assert [task.description for task in storage.open_tasks(-100)] == ["Купить уголь"]
    assert storage.complete_task(-100, task_id)
    assert storage.open_tasks(-100) == []
