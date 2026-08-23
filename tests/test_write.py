"""/write при нескольких совпадениях: кнопки выбора и открытие темы по клику.

Раньше бот печатал список текстом и замолкал — выбрать было нельзя. Теперь
на каждое совпадение — инлайн-кнопка, по клику открывается тема.
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
from maxbridge.core.transcribe import Transcriber  # noqa: E402
from maxbridge.telegram.bridge import TelegramBridge  # noqa: E402

FORUM_ID = -100500
CONTACTS = {-201: "Аня", -203: "Аня Бухмиллер"}


class Sent:
    def __init__(self, mid: int) -> None:
        self.message_id = mid


class FakeTopic:
    def __init__(self, tid: int) -> None:
        self.message_thread_id = tid


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return Sent(1)

    async def create_forum_topic(self, **kwargs):
        return FakeTopic(777)


class FakeAnswerable:
    def __init__(self, user_id: int, chat_id: int, text: str = "") -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.text = text
        self.message = None
        self.replies: list[str] = []
        self.markups: list = []

    async def answer(self, text: str = "", **kwargs):
        self.replies.append(text)
        if "reply_markup" in kwargs:
            self.markups.append(kwargs["reply_markup"])


class FakeCallback:
    def __init__(self, user_id: int, chat_id: int, data: str) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.answers: list[str] = []
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type="supergroup"),
            edit_reply_markup=self._edit,
        )
        self.edited = False

    async def answer(self, text: str = "", **kwargs):
        self.answers.append(text)

    async def _edit(self, **kwargs):
        self.edited = True


def make_bridge(tmp_path: Path):
    base = load_config(tmp_path / "нет.env")
    base.db_path = tmp_path / "data" / "maxbridge.db"
    base.max_mode = "botapi"
    base.max_bot_token = "dummy"
    base.owner_id = 111
    base.ai_enabled = False
    base.__post_init__()

    registry = build_accounts(base, AiAssistant(""), load_license(""))
    account = registry.accounts[0]
    registry.rebind(account, FORUM_ID)
    # транспорт-заглушка: два совпадения по «Аня»
    account.transport = SimpleNamespace(
        find_chats=lambda q, limit=8: [(-201, "Аня"), (-203, "Аня Бухмиллер")],
        chat_title=lambda cid: CONTACTS.get(cid, ""),
        supports_media=False,
    )

    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge.config = base
    bridge.accounts = registry
    bridge.ai = AiAssistant("")
    bridge.transcriber = Transcriber()
    bridge.billing = None
    bridge.bot = FakeBot()
    return bridge, account


async def test_write_offers_buttons_for_multiple_matches(tmp_path: Path) -> None:
    bridge, account = make_bridge(tmp_path)
    msg = FakeAnswerable(111, FORUM_ID, "/write Аня")

    await bridge._cmd_write(msg)

    assert bridge.accounts  # sanity
    assert msg.markups, "должна быть инлайн-клавиатура выбора"
    kb = msg.markups[0]
    assert isinstance(kb, InlineKeyboardMarkup)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas == ["write:-201", "write:-203"]


async def test_write_callback_opens_topic(tmp_path: Path) -> None:
    bridge, account = make_bridge(tmp_path)
    cb = FakeCallback(111, FORUM_ID, "write:-201")

    await bridge._on_callback(cb)

    # тема открыта: в неё ушло приглашение писать
    topic_msgs = [s for s in bridge.bot.sent if s.get("message_thread_id") == 777]
    assert topic_msgs and "Аня" in topic_msgs[0]["text"]
    assert cb.answers and "Готово" in cb.answers[-1]
    assert cb.edited, "клавиатуру выбора убрали после клика"
