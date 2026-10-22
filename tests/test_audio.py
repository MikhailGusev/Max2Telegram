"""Голосовые из MAX: UNSUPPORTED c audioId, скачивание, имя файла."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.maxproto import MaxProtocolError, MaxWSClient  # noqa: E402
from maxbridge.models import MaxMessage  # noqa: E402
from maxbridge.transports.userbot import (  # noqa: E402
    UserbotTransport,
    _parse_attaches,
    _sniff_name,
)

VOICE = {"_type": "UNSUPPORTED", "audioId": 777, "duration": 5, "token": "t", "wave": "x"}


def test_unsupported_with_audio_id_is_audio() -> None:
    """Так MAX присылает голосовое web-клиенту — распознаём его как аудио."""
    (att,) = _parse_attaches([VOICE])
    assert att.kind == "audio"


def test_known_types_unchanged() -> None:
    kinds = [a.kind for a in _parse_attaches([{"_type": "PHOTO", "url": "u"}, {"_type": "FILE", "fileId": 1}])]
    assert kinds == ["photo", "file"]


def test_sniff_name_by_signature() -> None:
    assert _sniff_name(b"OggS\x00\x02rest", "audio") == "voice.ogg"
    assert _sniff_name(b"ID3\x04rest", "audio") == "voice.mp3"
    assert _sniff_name(b"\x00\x00\x00\x18ftypM4A ", "audio") == "voice.m4a"
    assert _sniff_name(b"????", "audio") == "audio.ogg", "неизвестный формат — имя по умолчанию"


async def test_audio_url_falls_back_to_file_download(tmp_path: Path) -> None:
    client = MaxWSClient(tmp_path / "s.json")
    tried = []

    async def video_url(chat_id, message_id, vid):  # noqa: ANN001
        tried.append(("83", vid))
        raise MaxProtocolError("MAX вернул ошибку на opcode 83: not.found")

    async def file_url(chat_id, message_id, fid):  # noqa: ANN001
        tried.append(("88", fid))
        return "https://okcdn/voice"

    client.video_url = video_url  # type: ignore[assignment]
    client.file_url = file_url  # type: ignore[assignment]
    assert await client.audio_url(-1, "m1", 777) == "https://okcdn/voice"
    assert tried == [("83", 777), ("88", 777)]


async def test_fetch_voice_downloads_by_audio_id() -> None:
    transport = UserbotTransport("не-важно.json")
    calls = {}

    async def audio_url(chat_id, message_id, audio_id):  # noqa: ANN001
        calls["audio"] = (chat_id, message_id, audio_id)
        return "https://okcdn/voice"

    async def download(url):  # noqa: ANN001
        return b"OggS\x00voice-bytes"

    transport.client.audio_url = audio_url  # type: ignore[assignment]
    transport.client.download = download  # type: ignore[assignment]

    msg = MaxMessage(chat_id=-5, message_id="m9", attachments=_parse_attaches([VOICE]))
    data, name = await transport.fetch_attachment(msg, 0)
    assert calls["audio"] == (-5, "m9", 777)
    assert data.startswith(b"OggS") and name == "voice.ogg"


async def test_unknown_dialog_sender_tries_members_too() -> None:
    """Имя незнакомца в личке: кроме resolve (32) пробуем список участников (59)."""
    import asyncio

    from maxbridge.maxproto.opcodes import Op

    transport = UserbotTransport("не-важно.json")
    transport.client.me_id = 100
    opcodes = []

    async def fake_invoke(opcode, payload):  # noqa: ANN001
        opcodes.append(int(opcode))
        if int(opcode) == int(Op.GET_MEMBERS):
            return {"payload": {"members": [{"contact": {"id": 200, "names": [{"name": "Антон"}]}}]}}
        return {"payload": {}}

    transport.client.invoke = fake_invoke  # type: ignore[assignment]
    seen = []

    async def handler(m):  # noqa: ANN001
        seen.append(m)

    transport.on_message(handler)
    packet = {
        "opcode": int(Op.EVT_NEW_MESSAGE),
        "payload": {"chatId": 40240807, "message": {"id": "1", "sender": 200, "text": "Привет"}},
    }
    await transport._on_packet(packet)
    await asyncio.sleep(0.05)  # фоновые задачи

    assert int(Op.GET_MEMBERS) in opcodes
    assert transport.chat_title(40240807) == "Антон", "тема переименуется в имя собеседника"
