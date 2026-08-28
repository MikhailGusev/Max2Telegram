"""Авто-прочтение: одна кнопка «Не прочитано», вето и таймер.

Логика: сообщение приходит с кнопкой «Не прочитано»; через auto_read_seconds
оно само помечается прочитанным и кнопка убирается. Нажатие «Не прочитано»
отменяет таймер и меняет кнопку на «Прочитано».
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram.types import InlineKeyboardMarkup  # noqa: E402

from maxbridge.accounts import build_accounts  # noqa: E402
from maxbridge.config import load_config  # noqa: E402
from maxbridge.core.ai import AiAssistant  # noqa: E402
from maxbridge.core.licensing import load_license  # noqa: E402
from maxbridge.core.rules import Verdict  # noqa: E402
from maxbridge.core.transcribe import Transcriber  # noqa: E402
from maxbridge.models import MaxMessage  # noqa: E402
from maxbridge.telegram.bridge import TelegramBridge  # noqa: E402


class FakeBot:
    def __init__(self) -> None:
        self.edits: list[dict] = []

    async def edit_message_reply_markup(self, **kwargs):
        self.edits.append(kwargs)


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[str] = []
        self.markups: list = ["INITIAL"]
        self.message = SimpleNamespace(edit_reply_markup=self._edit)

    async def _edit(self, **kwargs):
        self.markups.append(kwargs.get("reply_markup"))

    async def answer(self, text: str = "", **kwargs):
        self.answers.append(text)


def make_bridge(tmp_path: Path, *, stealth: bool = True, auto_read: int = 0):
    base = load_config(tmp_path / "нет.env")
    base.db_path = tmp_path / "data" / "maxbridge.db"
    base.max_mode = "botapi"
    base.max_bot_token = "dummy"
    base.owner_id = 111
    base.ai_enabled = False
    base.stealth_mode = stealth
    base.auto_read_seconds = auto_read
    base.__post_init__()

    registry = build_accounts(base, AiAssistant(""), load_license(""))
    account = registry.accounts[0]
    reads: list[tuple[int, str]] = []

    async def fake_mark_read(chat_id, message_id):
        reads.append((int(chat_id), str(message_id)))

    account.transport = SimpleNamespace(mark_read=fake_mark_read, supports_media=False)

    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge.config = base
    bridge.accounts = registry
    bridge.ai = AiAssistant("")
    bridge.transcriber = Transcriber()
    bridge.billing = None
    bridge.bot = FakeBot()
    bridge._read_pending = {}
    bridge._read_held = set()
    bridge._read_done = set()
    bridge._read_tasks = set()
    return bridge, account, reads


def msg() -> MaxMessage:
    return MaxMessage(chat_id=-42, message_id="m1", text="привет", sender_name="Пётр")


def test_keyboard_single_unread_button_in_stealth(tmp_path: Path) -> None:
    bridge, account, _ = make_bridge(tmp_path, stealth=True)
    kb = bridge._keyboard(account, msg(), Verdict())
    assert isinstance(kb, InlineKeyboardMarkup)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas == ["keepunread:-42:m1"], "ровно одна кнопка «Не прочитано»"


def test_keyboard_none_without_stealth(tmp_path: Path) -> None:
    bridge, account, _ = make_bridge(tmp_path, stealth=False)
    assert bridge._keyboard(account, msg(), Verdict()) is None


async def test_auto_read_marks_and_clears(tmp_path: Path) -> None:
    bridge, account, reads = make_bridge(tmp_path, auto_read=0)
    key = ("default", -42, "m1")
    await bridge._auto_read_later(account, key, tg_target=111, tg_msg=555, max_chat=-42, max_msg="m1")
    assert reads == [(-42, "m1")], "сообщение помечено прочитанным в MAX"
    assert bridge.bot.edits[-1]["reply_markup"] is None, "клавиатура убрана"
    assert key in bridge._read_done


async def test_hold_unread_cancels_auto_and_shows_read_button(tmp_path: Path) -> None:
    bridge, account, reads = make_bridge(tmp_path)
    key = ("default", -42, "m1")

    query = FakeQuery("keepunread:-42:m1")
    await bridge._hold_unread(query, account, "-42:m1")

    assert key in bridge._read_held
    # кнопка сменилась на «Прочитано»
    new_kb = query.markups[-1]
    datas = [b.callback_data for row in new_kb.inline_keyboard for b in row]
    assert datas == ["readnow:-42:m1"]
    assert query.answers == ["Оставил непрочитанным"]

    # теперь авто-прочтение не должно ничего делать
    await bridge._auto_read_later(account, key, 111, 555, -42, "m1")
    assert reads == [], "удержанное сообщение не читается автоматом"


async def test_mark_read_now(tmp_path: Path) -> None:
    bridge, account, reads = make_bridge(tmp_path)
    query = FakeQuery("readnow:-42:m1")
    await bridge._mark_read_now(query, account, "-42:m1")
    assert reads == [(-42, "m1")]
    assert query.markups[-1] is None
    assert query.answers == ["Прочитано"]
