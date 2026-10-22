"""Расшифровка голосовых средствами MAX: запрос 202, пуш 293, текст под голосовым."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.accounts import build_accounts  # noqa: E402
from maxbridge.config import load_config  # noqa: E402
from maxbridge.core.ai import AiAssistant  # noqa: E402
from maxbridge.core.licensing import load_license  # noqa: E402
from maxbridge.core.transcribe import Transcriber  # noqa: E402
from maxbridge.maxproto import MaxWSClient  # noqa: E402
from maxbridge.maxproto.opcodes import Op  # noqa: E402
from maxbridge.maxproto.ws import _contains_any, _find_text  # noqa: E402
from maxbridge.models import Attachment, MaxMessage  # noqa: E402
from maxbridge.telegram.bridge import TelegramBridge  # noqa: E402
from maxbridge.transports.userbot import UserbotTransport  # noqa: E402

VOICE_RAW = {"_type": "AUDIO", "audioId": 777, "url": "https://okcdn/v", "duration": 5}


def test_find_text_priority_and_nesting() -> None:
    assert _find_text({"status": "DONE", "result": {"transcription": "привет"}}) == "привет"
    assert _find_text({"text": "запасной", "transcription": "главный"}) == "главный"
    assert _find_text({"items": [{"x": 1}, {"text": " есть "}]}) == "есть"
    assert _find_text({"status": "IN_PROGRESS"}) == ""


def test_contains_any_ids() -> None:
    assert _contains_any({"a": {"messageId": 117}}, {"117"})
    assert _contains_any([{"mediaId": "777"}], {"777"})
    assert not _contains_any({"flag": True}, {"1", "True"}), "bool не считается id"


async def test_transcribe_text_in_immediate_reply(tmp_path: Path) -> None:
    client = MaxWSClient(tmp_path / "s.json")
    sent = []

    async def fake_invoke(opcode, payload):  # noqa: ANN001
        sent.append((int(opcode), payload))
        return {"payload": {"transcription": "Перезвони мне"}}

    client.invoke = fake_invoke  # type: ignore[assignment]
    assert await client.transcribe(-5, "117", 777) == "Перезвони мне"
    assert sent == [(int(Op.TRANSCRIBE_MEDIA), {"mediaId": 777, "chatId": -5, "messageId": 117})]
    assert client._transcribe_waiters == [], "ожидание снято"


async def test_transcribe_waits_for_push(tmp_path: Path) -> None:
    client = MaxWSClient(tmp_path / "s.json")

    async def fake_invoke(opcode, payload):  # noqa: ANN001
        return {"payload": {"status": "IN_PROGRESS"}}

    client.invoke = fake_invoke  # type: ignore[assignment]
    task = asyncio.create_task(client.transcribe(-5, "117", 777, timeout=2))
    await asyncio.sleep(0.01)
    # промежуточный статус без текста — игнорируем, ждём дальше
    await client._dispatch({"opcode": 293, "cmd": 0, "seq": 3, "payload": {"messageId": 117}})
    assert not task.done()
    await client._dispatch(
        {"opcode": 293, "cmd": 0, "seq": 4, "payload": {"messageId": 117, "text": "Буду в 5"}}
    )
    assert await task == "Буду в 5"


async def test_push_without_ids_goes_to_single_waiter(tmp_path: Path) -> None:
    client = MaxWSClient(tmp_path / "s.json")

    async def fake_invoke(opcode, payload):  # noqa: ANN001
        return {"payload": {}}

    client.invoke = fake_invoke  # type: ignore[assignment]
    task = asyncio.create_task(client.transcribe(-5, "117", 777, timeout=2))
    await asyncio.sleep(0.01)
    await client._dispatch({"opcode": 293, "cmd": 0, "payload": {"transcription": "ок"}})
    assert await task == "ок"


async def test_server_push_with_colliding_seq_is_not_a_reply(tmp_path: Path) -> None:
    """Пуш сервера (cmd=0) с тем же seq, что наш запрос, не должен выдаваться за ответ."""
    client = MaxWSClient(tmp_path / "s.json")
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    client._pending[9] = fut
    await client._dispatch({"opcode": 128, "seq": 9, "cmd": 0, "payload": {"chatId": 1}})
    assert not fut.done(), "входящее сообщение не съедено ожиданием ответа"
    assert client._events.qsize() == 1, "а ушло обработчикам"
    await client._dispatch({"opcode": 64, "seq": 9, "cmd": 1, "payload": {}})
    assert fut.done()


async def test_transport_transcribe_uses_audio_id_and_forward_coords() -> None:
    transport = UserbotTransport("не-важно.json")
    called = {}

    async def transcribe(chat_id, message_id, media_id):  # noqa: ANN001
        called["args"] = (chat_id, message_id, media_id)
        return "текст"

    transport.client.transcribe = transcribe  # type: ignore[assignment]
    raw = dict(VOICE_RAW, _fwd_chat=-999, _fwd_msg="inner1")
    msg = MaxMessage(chat_id=-5, message_id="outer", attachments=[Attachment(kind="audio", raw=raw)])
    assert await transport.transcribe_voice(msg) == "текст"
    assert called["args"] == (-999, "inner1", 777)


def _bridge(tmp_path: Path, text: str):
    base = load_config(tmp_path / "нет.env")
    base.db_path = tmp_path / "data" / "maxbridge.db"
    base.max_mode = "botapi"
    base.max_bot_token = "dummy"
    base.owner_id = 111
    base.__post_init__()
    registry = build_accounts(base, AiAssistant(""), load_license(""))
    account = registry.accounts[0]

    async def transcribe_voice(message, index=0):  # noqa: ANN001
        return text

    account.transport = SimpleNamespace(transcribe_voice=transcribe_voice)
    bot = SimpleNamespace(edits=[], sent=[])

    async def edit_message_caption(**kw):
        bot.edits.append(kw)

    async def send_message(**kw):
        bot.sent.append(kw)

    bot.edit_message_caption = edit_message_caption
    bot.send_message = send_message
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge.config = base
    bridge.accounts = registry
    bridge.ai = AiAssistant("")
    bridge.transcriber = Transcriber()
    bridge.bot = bot
    return bridge, account


def _voice_msg() -> MaxMessage:
    return MaxMessage(
        chat_id=-5, message_id="m1", sender_name="Дорогая",
        attachments=[Attachment(kind="audio", raw=dict(VOICE_RAW))],
    )


async def test_transcript_appended_to_caption(tmp_path: Path) -> None:
    bridge, account = _bridge(tmp_path, "Купи хлеба")
    account.storage.save_message(_voice_msg())
    await bridge._transcribe_later(account, _voice_msg(), 111, None, 555, "· <b>Дорогая</b>")
    edit = bridge.bot.edits[-1]
    assert edit["message_id"] == 555
    assert edit["caption"].endswith("📝 <i>Купи хлеба</i>")
    assert account.storage.search("хлеба"), "расшифровка находится через /find"


async def test_long_transcript_sent_as_reply(tmp_path: Path) -> None:
    bridge, account = _bridge(tmp_path, "слово " * 300)
    await bridge._transcribe_later(account, _voice_msg(), 111, 7, 555, "· <b>Дорогая</b>")
    assert bridge.bot.edits == []
    reply = bridge.bot.sent[-1]
    assert reply["reply_to_message_id"] == 555 and reply["message_thread_id"] == 7


async def test_no_transcript_no_edit(tmp_path: Path) -> None:
    bridge, account = _bridge(tmp_path, "")
    await bridge._transcribe_later(account, _voice_msg(), 111, None, 555, "cap")
    assert bridge.bot.edits == [] and bridge.bot.sent == []
