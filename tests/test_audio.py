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


async def test_audio_candidates_collects_all_links(tmp_path: Path) -> None:
    """Из ответа 83 берём ВСЕ http-ссылки (служебные — в конец) плюс ссылку из 88."""
    client = MaxWSClient(tmp_path / "s.json")

    async def fake_invoke(opcode, payload):  # noqa: ANN001
        return {
            "payload": {
                "cache": "https://cache/x",
                "MP4_480": "https://okcdn/a480",
                "failoverHost": "host.example",
            }
        }

    async def file_url(chat_id, message_id, fid):  # noqa: ANN001
        return "https://files/voice"

    client.invoke = fake_invoke  # type: ignore[assignment]
    client.file_url = file_url  # type: ignore[assignment]
    labels = [label for label, _ in await client.audio_candidates(-1, "m1", 777)]
    assert labels == ["83/MP4_480", "83/cache", "88/url"]


async def test_audio_candidates_survive_failures(tmp_path: Path) -> None:
    client = MaxWSClient(tmp_path / "s.json")

    async def fake_invoke(opcode, payload):  # noqa: ANN001
        raise MaxProtocolError("MAX вернул ошибку на opcode 83: not.found")

    async def file_url(chat_id, message_id, fid):  # noqa: ANN001
        return "https://files/voice"

    client.invoke = fake_invoke  # type: ignore[assignment]
    client.file_url = file_url  # type: ignore[assignment]
    assert await client.audio_candidates(-1, "m1", 777) == [("88/url", "https://files/voice")]


async def test_voice_download_skips_broken_link() -> None:
    """Первая ссылка отдаёт 404 — берём следующую, голосовое доходит."""
    transport = UserbotTransport("не-важно.json")
    calls = {}

    async def audio_candidates(chat_id, message_id, audio_id):  # noqa: ANN001
        calls["args"] = (chat_id, message_id, audio_id)
        return [("83/MP4", "https://okcdn/bad"), ("88/url", "https://files/good")]

    async def download(url):  # noqa: ANN001
        if url.endswith("bad"):
            raise MaxProtocolError("скачивание не удалось: HTTP 404")
        return b"OggS\x00voice-bytes"

    transport.client.audio_candidates = audio_candidates  # type: ignore[assignment]
    transport.client.download = download  # type: ignore[assignment]

    msg = MaxMessage(chat_id=-5, message_id="m9", attachments=_parse_attaches([VOICE]))
    data, name = await transport.fetch_attachment(msg, 0)
    assert calls["args"] == (-5, "m9", 777)
    assert data.startswith(b"OggS") and name == "voice.ogg"


async def test_voice_download_reports_all_failures() -> None:
    import pytest

    transport = UserbotTransport("не-важно.json")

    async def audio_candidates(chat_id, message_id, audio_id):  # noqa: ANN001
        return [("83/MP4", "https://okcdn/bad")]

    async def download(url):  # noqa: ANN001
        raise MaxProtocolError("скачивание не удалось: HTTP 404")

    transport.client.audio_candidates = audio_candidates  # type: ignore[assignment]
    transport.client.download = download  # type: ignore[assignment]
    msg = MaxMessage(chat_id=-5, message_id="m9", attachments=_parse_attaches([VOICE]))
    with pytest.raises(ValueError, match="83/MP4"):
        await transport.fetch_attachment(msg, 0)


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


async def test_hello_uses_app_version_from_env(tmp_path: Path, monkeypatch) -> None:
    """MAX_APP_VERSION из .env подменяет версию клиента в HELLO; без неё — дефолт."""
    from maxbridge.maxproto import ws as ws_module

    client = MaxWSClient(tmp_path / "s.json")
    sent = []

    async def fake_invoke(opcode, payload):  # noqa: ANN001
        sent.append(payload["userAgent"]["appVersion"])
        return {"payload": {}}

    client.invoke = fake_invoke  # type: ignore[assignment]

    monkeypatch.delenv("MAX_APP_VERSION", raising=False)
    await client._hello()
    monkeypatch.setenv("MAX_APP_VERSION", "26.10.1")
    await client._hello()
    assert sent == [ws_module.APP_VERSION, "26.10.1"]


def test_voice_fallback_label() -> None:
    from maxbridge.telegram.bridge import _voice_label

    assert _voice_label(5) == "🎤 голосовое 0:05 — слушать в MAX"
    assert _voice_label(75000) == "🎤 голосовое 1:15 — слушать в MAX", "миллисекунды"
    assert _voice_label(None) == "🎤 голосовое — слушать в MAX"
