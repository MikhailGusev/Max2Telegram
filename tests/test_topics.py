"""Уход за темами: служебные сообщения, имена вместо «MAX 123…», автоочистка."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.accounts import build_accounts  # noqa: E402
from maxbridge.config import load_config  # noqa: E402
from maxbridge.core.ai import AiAssistant  # noqa: E402
from maxbridge.core.licensing import load_license  # noqa: E402
from maxbridge.core.router import Router, is_service_message  # noqa: E402
from maxbridge.core.transcribe import Transcriber  # noqa: E402
from maxbridge.db import Storage  # noqa: E402
from maxbridge.models import Attachment, MaxMessage  # noqa: E402
from maxbridge.telegram.bridge import TelegramBridge  # noqa: E402
from maxbridge.transports.userbot import UserbotTransport  # noqa: E402

DAY_MS = 86400 * 1000


# ----------------------------------------------------- служебные сообщения
def test_service_message_detection() -> None:
    system = MaxMessage(chat_id=1, message_id="a", attachments=[Attachment(kind="system")])
    empty = MaxMessage(chat_id=1, message_id="b")
    text = MaxMessage(chat_id=1, message_id="c", text="Привет")
    file = MaxMessage(chat_id=1, message_id="d", attachments=[Attachment(kind="file")])
    assert is_service_message(system)
    assert is_service_message(empty)
    assert not is_service_message(text)
    assert not is_service_message(file)


class _Sink:
    def __init__(self) -> None:
        self.delivered: list = []

    async def deliver(self, message, verdict):  # noqa: ANN001
        self.delivered.append(message)
        return 1


async def test_router_skips_service_messages(tmp_path: Path) -> None:
    config = load_config(tmp_path / "нет.env")
    config.db_path = tmp_path / "t.db"
    storage = Storage(config.db_path)
    transport = SimpleNamespace(chat_title=lambda cid: "", mark_read=None)
    router = Router(config, storage, transport, AiAssistant(""))  # type: ignore[arg-type]
    config.stealth_mode = True
    sink = _Sink()
    router.attach_telegram(sink)

    await router.handle_max_message(
        MaxMessage(chat_id=5, message_id="s1", attachments=[Attachment(kind="system")])
    )
    assert sink.delivered == [], "служебное «контакт в MAX» не создаёт тему"
    assert storage.get_chat(5) is None, "и не засоряет список чатов"
    storage.close()


# --------------------------------------------------------------- имена
def test_dialog_title_from_peer_learned_later() -> None:
    transport = UserbotTransport("не-важно.json")
    transport.client.me_id = 100
    transport._kinds[-7] = "dialog"
    transport._dialog_peer[-7] = 200
    assert transport.chat_title(-7) == "", "имя пока неизвестно"
    transport._names[200] = "Анна Волкова"  # узнали позже (резолв в фоне)
    assert transport.chat_title(-7) == "Анна Волкова"


# --------------------------------------------------------- бридж: темы
class _Bot:
    def __init__(self) -> None:
        self.renamed: list = []
        self.deleted: list = []
        self._tid = 900

    async def create_forum_topic(self, **kw):
        self._tid += 1
        return SimpleNamespace(message_thread_id=self._tid)

    async def edit_forum_topic(self, **kw):
        self.renamed.append(kw)

    async def delete_forum_topic(self, **kw):
        self.deleted.append(kw)


def _bridge(tmp_path: Path):
    base = load_config(tmp_path / "нет.env")
    base.db_path = tmp_path / "data" / "maxbridge.db"
    base.max_mode = "botapi"
    base.max_bot_token = "dummy"
    base.owner_id = 111
    base.__post_init__()
    registry = build_accounts(base, AiAssistant(""), load_license(""))
    account = registry.accounts[0]
    registry.rebind(account, -100500)
    names = {}
    account.transport = SimpleNamespace(chat_title=lambda cid: names.get(cid, ""))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge.config = base
    bridge.accounts = registry
    bridge.ai = AiAssistant("")
    bridge.transcriber = Transcriber()
    bridge.billing = None
    bridge.bot = _Bot()
    return bridge, account, names


async def test_topic_renamed_when_name_becomes_known(tmp_path: Path) -> None:
    bridge, account, _ = _bridge(tmp_path)
    # первое сообщение: имени нет -> тема «MAX 473269620»
    topic = await bridge._ensure_topic_by(account, 473269620, "", "dialog")
    assert bridge.bot.renamed == []
    # позже имя узнали -> та же тема переименовывается, новая не создаётся
    again = await bridge._ensure_topic_by(account, 473269620, "Анна Волкова", "dialog")
    assert again == topic
    assert bridge.bot.renamed[-1]["name"] == "Анна Волкова"
    # повторно с тем же именем — без лишних запросов к Telegram
    await bridge._ensure_topic_by(account, 473269620, "Анна Волкова", "dialog")
    assert len(bridge.bot.renamed) == 1


async def test_refresh_topic_names_pass(tmp_path: Path) -> None:
    bridge, account, names = _bridge(tmp_path)
    await bridge._ensure_topic_by(account, 42, "", "dialog")
    names[42] = "Пётр"
    assert await bridge.refresh_topic_names(account) == 1
    assert bridge.bot.renamed[-1]["name"] == "Пётр"
    assert await bridge.refresh_topic_names(account) == 0, "второй проход ничего не трогает"


def _age_chat(storage: Storage, chat_id: int, days: float) -> None:
    old = int(time.time() - days * 86400)
    storage.executemany("UPDATE chats SET created_at = ? WHERE max_chat_id = ?", [(old, chat_id)])


def _msg(storage: Storage, chat_id: int, mid: str, *, text="", outgoing=False, days_ago=3.0):
    storage.save_message(
        MaxMessage(
            chat_id=chat_id,
            message_id=mid,
            text=text,
            outgoing=outgoing,
            ts=int(time.time() * 1000 - days_ago * DAY_MS),
        )
    )


async def test_cleanup_removes_only_dead_topics(tmp_path: Path) -> None:
    bridge, account, _ = _bridge(tmp_path)
    s = account.storage
    for chat_id in (1, 2, 3, 4, 5):
        await bridge._ensure_topic_by(account, chat_id, f"чат {chat_id}", "dialog")
        _age_chat(s, chat_id, 5)

    _msg(s, 1, "g", text="Привет")                    # приветствие и тишина -> удалить
    _msg(s, 2, "g", text="Привет")
    _msg(s, 2, "me", text="Здравствуйте", outgoing=True)  # я ответил -> оставить
    _msg(s, 3, "a", text="Привет")
    _msg(s, 3, "b", text="Есть вопрос")               # пишут повторно -> оставить
    _msg(s, 4, "g", text="Привет")
    s.set_chat_flag(4, "vip", 1)                      # важный -> оставить
    _msg(s, 5, "g", text="Привет", days_ago=0.1)      # свежее -> пока оставить

    preview = await bridge.cleanup_topics(account, days=2, dry_run=True)
    assert [cid for cid, _ in preview] == [1]
    assert bridge.bot.deleted == [], "предпросмотр ничего не удаляет"

    removed = await bridge.cleanup_topics(account, days=2)
    assert [cid for cid, _ in removed] == [1]
    assert len(bridge.bot.deleted) == 1
    assert not s.get_chat(1)["tg_topic_id"], "тема отвязана — напишут снова, создастся заново"
    assert s.search("Привет"), "история в базе осталась"


async def test_cleanup_disabled_with_zero_days(tmp_path: Path) -> None:
    bridge, account, _ = _bridge(tmp_path)
    await bridge._ensure_topic_by(account, 1, "чат", "dialog")
    _age_chat(account.storage, 1, 10)
    assert await bridge.cleanup_topics(account, days=0) == []
