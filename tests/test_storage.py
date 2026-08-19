"""Хранилище: дедупликация, поиск, привязка тем, очередь эскалации."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.db import Storage  # noqa: E402
from maxbridge.models import MaxMessage  # noqa: E402


@pytest.fixture()
def storage(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "test.db")
    yield store
    store.close()


def make(chat_id: int, msg_id: str, text: str, ts: int | None = None) -> MaxMessage:
    return MaxMessage(
        chat_id=chat_id,
        message_id=msg_id,
        text=text,
        sender_name="Иван",
        ts=ts or int(time.time() * 1000),
    )


def test_duplicate_message_is_ignored(storage: Storage) -> None:
    storage.upsert_chat(1, "Чат")
    assert storage.save_message(make(1, "a", "привет")) is not None
    assert storage.save_message(make(1, "a", "привет")) is None


def test_fulltext_search_finds_message(storage: Storage) -> None:
    storage.upsert_chat(1, "Юристы")
    storage.save_message(make(1, "a", "Отправил договор на согласование"))
    found = storage.search("договор")
    assert len(found) == 1
    assert found[0]["chat_title"] == "Юристы"


@pytest.mark.parametrize(
    "query",
    ["акт.docx", "счёт-фактура", "как дела?", 'он сказал "да"', "смета (итог)", "*", ""],
)
def test_search_survives_special_characters(storage: Storage, query: str) -> None:
    """FTS5 разбирает ввод как выражение — пользователь не должен об этом знать."""
    storage.upsert_chat(1, "Чат")
    storage.save_message(make(1, "a", "акт.docx и счёт-фактура готовы"))
    storage.search(query)  # главное — не падает


def test_search_finds_token_with_dot(storage: Storage) -> None:
    storage.upsert_chat(1, "Чат")
    storage.save_message(make(1, "a", "Отправил акт.docx на подпись"))
    assert len(storage.search("акт.docx")) == 1


def test_prefix_search_with_asterisk(storage: Storage) -> None:
    storage.upsert_chat(1, "Чат")
    storage.save_message(make(1, "a", "договорённость достигнута"))
    assert len(storage.search("договор*")) == 1
    assert storage.search("договор") == []


def test_transcript_makes_voice_searchable(storage: Storage) -> None:
    """Голосовое приходит без текста — после расшифровки его должен найти /find."""
    storage.upsert_chat(1, "Чат")
    storage.save_message(make(1, "v1", ""))
    assert storage.search("смету") == []

    storage.save_transcript(1, "v1", "пришли смету до пятницы")

    found = storage.search("смету")
    assert len(found) == 1
    assert "смету" in found[0]["text"]


def test_transcript_appends_to_existing_caption(storage: Storage) -> None:
    storage.upsert_chat(1, "Чат")
    storage.save_message(make(1, "v1", "подпись"))
    storage.save_transcript(1, "v1", "расшифровка")
    row = storage.recent(1)[0]
    assert "подпись" in row["text"] and "расшифровка" in row["text"]


def test_empty_transcript_is_ignored(storage: Storage) -> None:
    storage.upsert_chat(1, "Чат")
    storage.save_message(make(1, "v1", "исходный"))
    storage.save_transcript(1, "v1", "   ")
    assert storage.recent(1)[0]["text"] == "исходный"


def test_topic_binding_round_trip(storage: Storage) -> None:
    storage.upsert_chat(42, "Поставщики")
    storage.bind_topic(42, 777)
    assert storage.chat_by_topic(777)["max_chat_id"] == 42


def test_answering_clears_pending(storage: Storage) -> None:
    storage.upsert_chat(1, "Чат")
    storage.save_message(make(1, "a", "Когда пришлёшь?"), needs_reply=True)
    assert storage.stats()["pending"] == 1
    storage.mark_chat_answered(1)
    assert storage.stats()["pending"] == 0


def test_escalation_queue_respects_age_and_flag(storage: Storage) -> None:
    storage.upsert_chat(1, "Чат")
    old_ts = int((time.time() - 3600) * 1000)
    row_id = storage.save_message(
        make(1, "a", "Срочно!", ts=old_ts), priority="urgent", needs_reply=True
    )
    cutoff = int((time.time() - 60) * 1000)

    assert len(storage.pending_escalation(cutoff)) == 1
    storage.mark_escalated([row_id])
    assert storage.pending_escalation(cutoff) == []


def test_fresh_urgent_is_not_escalated_yet(storage: Storage) -> None:
    storage.upsert_chat(1, "Чат")
    storage.save_message(make(1, "a", "Срочно!"), priority="urgent", needs_reply=True)
    cutoff = int((time.time() - 3600) * 1000)
    assert storage.pending_escalation(cutoff) == []


def test_chat_title_is_not_overwritten_by_empty(storage: Storage) -> None:
    storage.upsert_chat(1, "Важный чат")
    storage.upsert_chat(1, "")
    assert storage.get_chat(1)["title"] == "Важный чат"
