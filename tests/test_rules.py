"""Правила приоритетов — самый нагруженный код, поэтому он под тестами."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.core.rules import classify  # noqa: E402
from maxbridge.db import Storage  # noqa: E402
from maxbridge.models import MaxMessage  # noqa: E402


@pytest.fixture()
def storage(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "test.db")
    store.upsert_chat(1, "Рабочий чат")
    yield store
    store.close()


def message(text: str, chat_id: int = 1) -> MaxMessage:
    return MaxMessage(chat_id=chat_id, message_id="m1", text=text, sender_name="Коллега")


def test_urgent_word_wins(storage: Storage) -> None:
    verdict = classify(message("Срочно нужен отчёт"), storage)
    assert verdict.priority == "urgent"
    assert verdict.needs_reply


def test_money_is_urgent(storage: Storage) -> None:
    verdict = classify(message("Скинь реквизиты для оплаты"), storage)
    assert verdict.priority == "urgent"
    assert verdict.intent == "money"


def test_question_needs_reply_but_not_urgent(storage: Storage) -> None:
    verdict = classify(message("Ты завтра будешь в офисе?"), storage)
    assert verdict.priority == "normal"
    assert verdict.needs_reply
    assert verdict.intent == "question"


def test_small_talk_is_low(storage: Storage) -> None:
    verdict = classify(message("спасибо"), storage)
    assert verdict.priority == "low"
    assert verdict.intent == "noise"


def test_muted_chat_silences_everything(storage: Storage) -> None:
    storage.set_chat_flag(1, "muted", 1)
    verdict = classify(message("Срочно перезвони"), storage)
    assert verdict.priority == "low"


def test_vip_chat_raises_priority(storage: Storage) -> None:
    storage.set_chat_flag(1, "vip", 1)
    verdict = classify(message("привет"), storage)
    assert verdict.priority == "urgent"


def test_user_rule_beats_heuristics(storage: Storage) -> None:
    storage.add_rule("рассылк", "mute")
    verdict = classify(message("Срочно! Наша рассылка о скидках"), storage)
    assert verdict.priority == "low"


def test_autoreply_rule_sets_text(storage: Storage) -> None:
    storage.add_rule("отпуск", "autoreply", "Я в отпуске до 25-го")
    verdict = classify(message("Ты в отпуске сейчас?"), storage)
    assert verdict.autoreply == "Я в отпуске до 25-го"


def test_broken_regex_falls_back_to_substring(storage: Storage) -> None:
    storage.add_rule("счёт(", "urgent")  # незакрытая скобка — не валидный regex
    verdict = classify(message("Пришёл счёт( от подрядчика"), storage)
    assert verdict.priority == "urgent"
