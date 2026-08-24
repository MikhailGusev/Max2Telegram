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


async def test_probe_reads_members_once() -> None:
    """Зонд участников — один запрос списка участников (опкод 59) на группу."""
    import asyncio

    from maxbridge.maxproto.opcodes import Op

    transport = UserbotTransport("не-важно.json")
    calls = {"n": 0, "opcodes": []}

    async def fake_invoke(opcode, payload):
        calls["n"] += 1
        calls["opcodes"].append(int(opcode))
        return {"payload": {"members": [{"id": 5, "names": [{"name": "X"}]}]}}

    transport.client.invoke = fake_invoke  # type: ignore[assignment]
    transport._schedule_probe(-77)
    transport._schedule_probe(-77)  # повтор — no-op
    await asyncio.sleep(0.05)  # даём фоновой задаче отработать
    assert calls["n"] == 1
    assert calls["opcodes"] == [int(Op.GET_MEMBERS)]
    assert -77 in transport._probed_chats


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


async def test_send_does_not_retry_other_errors() -> None:
    transport = UserbotTransport("не-важно.json")

    async def factory():
        raise MaxProtocolError("MAX вернул ошибку на opcode 64: not.found")

    with pytest.raises(MaxProtocolError, match="not.found"):
        await transport._send_with_reconnect(factory)
