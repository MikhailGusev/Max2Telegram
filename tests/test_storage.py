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
