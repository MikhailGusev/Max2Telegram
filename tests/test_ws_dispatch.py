"""Приём MAX: хендлер может сам слать запрос и получать ответ (без дедлока).

Регрессия по файлам: скачивание вызывалось из хендлера прямо в цикле приёма,
который сам же должен прочитать ответ на этот запрос → дедлок до таймаута.
Теперь хендлеры крутит отдельный воркер, а цикл приёма свободен резолвить
ответы. Этот тест воспроизводит ту схему.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.maxproto import MaxWSClient  # noqa: E402


class FakeConn:
    async def send(self, data):  # noqa: ANN001
        return None


async def test_handler_can_invoke_without_deadlock(tmp_path: Path) -> None:
    client = MaxWSClient(tmp_path / "s.json")
    client._conn = FakeConn()  # type: ignore[assignment]
    worker = asyncio.create_task(client._event_worker())

    got: dict = {}

    async def handler(packet):  # noqa: ANN001
        # хендлер сам обращается к MAX (как скачивание файла) и ждёт ответ
        resp = await client.invoke(88, {"fileId": 1})
        got["url"] = (resp.get("payload") or {}).get("url")

    client.on_packet(handler)

    try:
        # серверное событие -> в очередь -> воркер запустит handler
        await client._dispatch({"opcode": 128, "seq": 999, "payload": {}})
        await asyncio.sleep(0.05)  # даём воркеру дойти до invoke (создать pending seq=1)

        # ответ на invoke с тем же seq -> цикл приёма его резолвит
        await client._dispatch(
            {"opcode": 88, "seq": 1, "cmd": 1, "payload": {"url": "http://x/f", "unsafe": False}}
        )
        await asyncio.sleep(0.05)

        assert got.get("url") == "http://x/f", "хендлер получил ответ, дедлока нет"
    finally:
        worker.cancel()


async def test_dispatch_resolves_pending_future(tmp_path: Path) -> None:
    """Базовое: ответ с совпадающим seq резолвит ожидающий invoke."""
    client = MaxWSClient(tmp_path / "s.json")
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    client._pending[7] = fut
    await client._dispatch({"opcode": 64, "seq": 7, "cmd": 1, "payload": {"ok": True}})
    assert fut.done() and fut.result()["payload"]["ok"] is True
