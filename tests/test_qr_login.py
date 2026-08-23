"""QR-логин MAX: разбор ответов, ожидание скана, сохранение сессии.

Реального WebSocket нет — подменяем `invoke`/`_hello` заглушкой, которая
отдаёт заранее заготовленные ответы по опкоду. Проверяем ровно логику клиента:
что он достаёт из payload, когда падает и что кладёт в файл сессии.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.maxproto import MaxAuthError, MaxWSClient  # noqa: E402
from maxbridge.maxproto.opcodes import Op  # noqa: E402


def make_client(tmp_path: Path, responses: dict[int, Any]) -> MaxWSClient:
    """Клиент, у которого invoke отдаёт заготовки, а _hello — пустышка.

    Значение по опкоду может быть dict (единый ответ) или список (по вызову).
    """
    client = MaxWSClient(tmp_path / "sess.json")

    async def fake_hello(device_id: str | None = None) -> dict[str, Any]:
        client._device_id = device_id or client._device_id or "dev-123"
        return {"payload": {}}

    async def fake_invoke(opcode: int, payload: dict[str, Any]) -> dict[str, Any]:
        value = responses[int(opcode)]
        if isinstance(value, list):
            value = value.pop(0)
        return {"payload": value}

    client._hello = fake_hello  # type: ignore[assignment]
    client.invoke = fake_invoke  # type: ignore[assignment]
    return client


async def test_request_qr_returns_payload(tmp_path: Path) -> None:
    client = make_client(
        tmp_path,
        {Op.GET_QR: {"qrLink": "max://qr/abc", "trackId": "T1", "pollingInterval": 2000}},
    )
    qr = await client.request_qr()
    assert qr["trackId"] == "T1"
    assert qr["qrLink"] == "max://qr/abc"


async def test_request_qr_without_track_raises(tmp_path: Path) -> None:
    client = make_client(tmp_path, {Op.GET_QR: {"qrLink": "max://qr"}})
    with pytest.raises(MaxAuthError):
        await client.request_qr()


async def test_qr_status_unwraps_status_field(tmp_path: Path) -> None:
    client = make_client(
        tmp_path, {Op.GET_QR_STATUS: {"status": {"loginAvailable": False}}}
    )
    status = await client.qr_status("T1")
    assert status == {"loginAvailable": False}


async def test_poll_qr_returns_when_login_available(tmp_path: Path) -> None:
    client = make_client(
        tmp_path,
        {
            Op.GET_QR_STATUS: [
                {"status": {"loginAvailable": False}},
                {"status": {"loginAvailable": True, "expiresAt": 123}},
            ]
        },
    )
    status = await client.poll_qr("T1", interval=0.5, timeout=5)
    assert status["loginAvailable"] is True


async def test_poll_qr_times_out(tmp_path: Path, monkeypatch) -> None:
    # статус всегда «ещё не подтверждён» — ждём таймаута, но без реальных пауз
    client = make_client(
        tmp_path, {Op.GET_QR_STATUS: {"status": {"loginAvailable": False}}}
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with pytest.raises(MaxAuthError):
        await client.poll_qr("T1", interval=0.5, timeout=0.001)


async def test_login_by_qr_saves_session(tmp_path: Path) -> None:
    client = make_client(
        tmp_path,
        {
            Op.GET_QR: {"qrLink": "max://qr", "trackId": "T1", "pollingInterval": 2000},
            Op.LOGIN_BY_QR: {
                "tokenAttrs": {"LOGIN": {"token": "SECRET-TOKEN"}},
                "profile": {"contact": {"id": 777, "phone": "+7999"}},
            },
        },
    )
    qr = await client.request_qr()  # ставит device_id через _hello, как в реале
    payload = await client.login_by_qr(qr["trackId"])
    await client.close()

    assert payload["profile"]["contact"]["id"] == 777
    saved = json.loads((tmp_path / "sess.json").read_text(encoding="utf-8"))
    assert saved["token"] == "SECRET-TOKEN"
    assert saved["device_id"] == "dev-123"  # проставился из _hello при request_qr


async def test_login_by_qr_without_token_raises(tmp_path: Path) -> None:
    client = make_client(tmp_path, {Op.LOGIN_BY_QR: {"tokenAttrs": {}}})
    with pytest.raises(MaxAuthError):
        await client.login_by_qr("T1")
    assert not (tmp_path / "sess.json").exists(), "битую сессию сохранять нельзя"


async def test_login_by_qr_with_password_challenge_raises(tmp_path: Path) -> None:
    client = make_client(
        tmp_path, {Op.LOGIN_BY_QR: {"passwordChallenge": {"kind": "PASSWORD"}}}
    )
    with pytest.raises(MaxAuthError, match="2FA"):
        await client.login_by_qr("T1")
