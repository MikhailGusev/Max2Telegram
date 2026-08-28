"""Плоский режим доставки (Free без форум-группы).

Проверяем: выбор цели доставки (тема форума vs личка владельца), заголовок
ленты «чат · отправитель», разрешение ответа реплаем и гейт /bind по Premium.
Реального Telegram нет — подменяем bot заглушкой, а бридж собираем через
__new__, чтобы не поднимать aiogram.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.accounts import build_accounts  # noqa: E402
from maxbridge.config import load_config  # noqa: E402
from maxbridge.core.ai import AiAssistant  # noqa: E402
from maxbridge.core.licensing import load_license  # noqa: E402
from maxbridge.core.rules import Verdict  # noqa: E402
from maxbridge.core.transcribe import Transcriber  # noqa: E402
from maxbridge.models import MaxMessage  # noqa: E402
from maxbridge.telegram.bridge import TelegramBridge  # noqa: E402


class Sent:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeTopic:
    def __init__(self, thread_id: int) -> None:
        self.message_thread_id = thread_id


class FakeBot:
    """Ловит вызовы, которые бридж делает при доставке."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self._next_id = 1000
        self._next_topic = 500

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        self._next_id += 1
        return Sent(self._next_id)

    async def create_forum_topic(self, **kwargs):
        self._next_topic += 1
        return FakeTopic(self._next_topic)


def make_bridge(tmp_path: Path, *, billing=None):
    base = load_config(tmp_path / "нет.env")
    base.db_path = tmp_path / "data" / "maxbridge.db"
    base.max_mode = "botapi"
    base.max_bot_token = "dummy"
    base.owner_id = 111
    base.forum_chat_id = 0
    base.ai_enabled = False
    base.__post_init__()

    registry = build_accounts(base, AiAssistant(""), load_license(""))

    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge.config = base
    bridge.accounts = registry
    bridge.ai = AiAssistant("")
    bridge.transcriber = Transcriber()
    bridge.billing = billing
    bridge.bot = FakeBot()
    bridge._read_pending = {}
    bridge._read_held = set()
    bridge._read_done = set()
    bridge._read_tasks = set()
    return bridge, registry.accounts[0]


def incoming() -> MaxMessage:
    return MaxMessage(
        chat_id=-42,
        message_id="m1",
        text="привет, как дела",
        sender_name="Пётр",
        chat_title="Пётр Иванов",
    )


async def test_flat_mode_delivers_to_owner_dm(tmp_path: Path) -> None:
    bridge, account = make_bridge(tmp_path)
    assert not account.forum_chat_id  # группа не привязана -> плоский режим

    await bridge.deliver(account, incoming(), Verdict())

    sent = bridge.bot.messages[-1]
    assert sent["chat_id"] == account.owner_id == 111
    assert sent["message_thread_id"] is None
    # заголовок ленты: чат жирным + отправитель после точки
    assert "<b>Пётр Иванов</b> · Пётр" in sent["text"]


async def test_forum_mode_delivers_to_topic(tmp_path: Path) -> None:
    bridge, account = make_bridge(tmp_path)
    bridge.accounts.rebind(account, -100500)  # привязали группу

    await bridge.deliver(account, incoming(), Verdict())

    sent = bridge.bot.messages[-1]
    assert sent["chat_id"] == -100500
    assert sent["message_thread_id"] == 501  # тема, созданная FakeBot
    # в теме заголовок — только отправитель, без чата
    assert "<b>Пётр</b>" in sent["text"]
    assert "·" not in sent["text"]


async def test_flat_reply_resolves_chat(tmp_path: Path) -> None:
    """Реплай в личке находит чат MAX по tg_msg_id доставленного сообщения."""
    bridge, account = make_bridge(tmp_path)

    tg_id = await bridge.deliver(account, incoming(), Verdict())
    # эмулируем связку, которую в бою делает роутер после deliver
    row_id = account.storage.save_message(incoming())
    if row_id is None:  # deliver сам ничего не пишет — сообщение новое
        row = account.storage.recent(-42)[-1]
        row_id = row["id"]
    account.storage.link_tg_message(row_id, tg_id)

    found = bridge._resolve_by_tg_reply(account.owner_id, tg_id)
    assert found is not None
    resolved_account, chat_id, reply_to = found
    assert resolved_account is account
    assert chat_id == -42
    assert reply_to == "m1"


async def test_flat_reply_unknown_message_returns_none(tmp_path: Path) -> None:
    bridge, account = make_bridge(tmp_path)
    assert bridge._resolve_by_tg_reply(account.owner_id, 999999) is None


class DummyBilling:
    def __init__(self, premium: bool) -> None:
        self._premium = premium

    def is_premium(self, user_id: int) -> bool:
        return self._premium


class FakeMessage:
    def __init__(self, bridge, user_id: int, chat_id: int) -> None:
        from types import SimpleNamespace

        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.text = "/bind"
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


async def test_bind_blocked_for_non_premium(tmp_path: Path) -> None:
    bridge, account = make_bridge(tmp_path, billing=DummyBilling(False))
    msg = FakeMessage(bridge, user_id=111, chat_id=-100777)

    await bridge._cmd_bind(msg)

    assert msg.answers and "Premium" in msg.answers[0]
    assert not account.forum_chat_id, "группа не должна привязаться без Premium"


async def test_bind_allowed_for_premium(tmp_path: Path) -> None:
    bridge, account = make_bridge(tmp_path, billing=DummyBilling(True))
    msg = FakeMessage(bridge, user_id=111, chat_id=-100777)

    await bridge._cmd_bind(msg)

    assert account.forum_chat_id == -100777
    assert bridge.accounts.by_forum(-100777) is account
