"""Групповые чаты MAX: имя отправителя и устойчивая отправка при обрыве."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.maxproto import MaxProtocolError  # noqa: E402
from maxbridge.transports.userbot import UserbotTransport  # noqa: E402


async def test_resolve_user_reads_profiles_key() -> None:
    """RESOLVE_USERS может вернуть человека под ключом profiles/users, не contacts."""
    transport = UserbotTransport("не-важно.json")

    async def fake_invoke(opcode, payload):
        return {"payload": {"profiles": [{"id": 500, "names": [{"name": "Ирина"}]}]}}

    transport.client.invoke = fake_invoke  # type: ignore[assignment]
    await transport._resolve_user(500)
    assert transport._names[500] == "Ирина"


async def test_resolve_user_reads_dict_keyed_response() -> None:
    """MAX часто отдаёт людей словарём по id, а не списком — тоже разбираем."""
    transport = UserbotTransport("не-важно.json")

    async def fake_invoke(opcode, payload):
        return {"payload": {"contacts": {"155881724": {"id": 155881724, "name": "Пётр"}}}}

    transport.client.invoke = fake_invoke  # type: ignore[assignment]
    await transport._resolve_user(155881724)
    assert transport._names[155881724] == "Пётр"


async def test_resolve_user_reads_nested_contact_name() -> None:
    transport = UserbotTransport("не-важно.json")

    async def fake_invoke(opcode, payload):
        return {
            "payload": {
                "users": [{"contact": {"id": 42, "names": [{"name": "Ольга"}]}}]
            }
        }

    transport.client.invoke = fake_invoke  # type: ignore[assignment]
    await transport._resolve_user(42)
    assert transport._names[42] == "Ольга"


async def test_unresolvable_user_not_retried() -> None:
    """Если MAX не отдал имя (таймаут) — метим и не долбим запрос снова."""
    import asyncio

    transport = UserbotTransport("не-важно.json")
    calls = {"n": 0}

    async def fake_invoke(opcode, payload):
        calls["n"] += 1
        raise MaxProtocolError("таймаут ответа на opcode 32")

    transport.client.invoke = fake_invoke  # type: ignore[assignment]
    await transport._resolve_user(999)
    assert 999 in transport._unresolvable
    assert calls["n"] == 1

    # повторная попытка запланировать резолв не должна снова звать MAX
    transport._schedule_resolve(999)
    await asyncio.sleep(0)
    assert calls["n"] == 1, "по неотвечающему id повторных запросов нет"


async def test_group_members_fill_names_once() -> None:
    """Список участников группы (опкод 59) даёт имена: contact.names → _names."""
    import asyncio

    from maxbridge.maxproto.opcodes import Op

    transport = UserbotTransport("не-важно.json")
    calls = {"n": 0, "opcodes": []}

    async def fake_invoke(opcode, payload):
        calls["n"] += 1
        calls["opcodes"].append(int(opcode))
        return {
            "payload": {
                "members": [
                    {"contact": {"id": 155881724, "names": [{"name": "Романова"}]}},
                    {"contact": {"id": 223732986, "name": "Сергей"}},
                ]
            }
        }

    transport.client.invoke = fake_invoke  # type: ignore[assignment]
    transport._schedule_members(-77)
    transport._schedule_members(-77)  # повтор — no-op
    await asyncio.sleep(0.05)  # даём фоновой задаче отработать

    assert calls["opcodes"] == [int(Op.GET_MEMBERS)], "ровно один запрос участников"
    assert transport._names[155881724] == "Романова"
    assert transport._names[223732986] == "Сергей"
    assert -77 in transport._members_loaded


async def test_send_retries_after_connection_closed() -> None:
    transport = UserbotTransport("не-важно.json")
    # эмулируем «уже переподключились», чтобы повтор не ждал реально
    transport.client._conn = object()  # type: ignore[assignment]
    transport.client._logged_in = True

    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise MaxProtocolError("соединение закрыто")
        return {"payload": {"message": {"id": "OK"}}}

    result = await transport._send_with_reconnect(factory)
    assert calls["n"] == 2, "должна быть ровно одна повторная попытка"
    assert result["payload"]["message"]["id"] == "OK"


async def test_forwarded_file_extracted_from_inner_message() -> None:
    """Пересланное сообщение (link.type=FORWARD): файл лежит во вложенном
    link.message.attaches, а внешнее пустое — достаём контент оттуда."""
    from maxbridge.maxproto.opcodes import Op

    transport = UserbotTransport("не-важно.json")
    transport.client.me_id = 100
    captured = []
    transport.on_message(lambda m: captured.append(m) or _noop())

    packet = {
        "opcode": int(Op.EVT_NEW_MESSAGE),
        "payload": {
            "chatId": -100,
            "message": {
                "id": "outer1",
                "sender": 200,
                "text": "",
                "attaches": [],
                "link": {
                    "type": "FORWARD",
                    "chatId": -999,
                    "message": {
                        "id": "inner1",
                        "sender": 300,
                        "text": "смотри файл",
                        "attaches": [
                            {"_type": "FILE", "fileId": 555, "name": "doc.pdf", "token": "t"}
                        ],
                    },
                },
            },
        },
    }
    await transport._on_packet(packet)

    assert len(captured) == 1
    msg = captured[0]
    assert msg.text == "смотри файл"
    assert len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att.kind == "file" and att.name == "doc.pdf"
    # координаты оригинала для скачивания
    assert att.raw["_fwd_chat"] == -999 and att.raw["_fwd_msg"] == "inner1"


async def _noop():
    return None


async def test_send_retries_on_no_connection() -> None:
    """«нет соединения с MAX» (окно реконнекта) — тоже повторяем, не роняем."""
    transport = UserbotTransport("не-важно.json")
    transport.client._conn = object()  # type: ignore[assignment]
    transport.client._logged_in = True
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise MaxProtocolError("нет соединения с MAX: сначала connect()")
        return {"payload": {"message": {"id": "OK"}}}

    result = await transport._send_with_reconnect(factory)
    assert calls["n"] == 2
    assert result["payload"]["message"]["id"] == "OK"


async def test_send_does_not_retry_other_errors() -> None:
    transport = UserbotTransport("не-важно.json")

    async def factory():
        raise MaxProtocolError("MAX вернул ошибку на opcode 64: not.found")

    with pytest.raises(MaxProtocolError, match="not.found"):
        await transport._send_with_reconnect(factory)
