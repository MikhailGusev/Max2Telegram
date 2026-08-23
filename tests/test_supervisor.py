"""Супервайзер Application._supervise: живучесть и подхват задач на лету.

Регрессия: раньше штатное завершение ЛЮБОЙ задачи (например, выключенной
эскалации, которая сразу возвращается) роняло всё приложение в цикл
рестартов systemd. Теперь падаем только на исключении.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.app import Application  # noqa: E402


def bare_app() -> Application:
    app = Application.__new__(Application)
    app._wake = asyncio.Event()
    app._tasks = set()
    return app


async def test_normal_completion_does_not_stop_supervisor() -> None:
    app = bare_app()

    async def finishes_ok() -> None:
        return  # как выключенная эскалация — сразу штатный возврат

    async def crashes() -> None:
        await asyncio.sleep(0.05)
        raise RuntimeError("boom")

    app._tasks = {
        asyncio.create_task(finishes_ok()),
        asyncio.create_task(crashes()),
    }

    # если бы штатный возврат ронял супервайзер — исключения бы не было
    with pytest.raises(RuntimeError, match="boom"):
        await app._supervise()


async def test_supervisor_exits_when_all_tasks_finish() -> None:
    app = bare_app()

    async def quick() -> None:
        return

    app._tasks = {asyncio.create_task(quick())}
    await asyncio.wait_for(app._supervise(), timeout=1.0)  # не должен зависнуть
    assert app._tasks == set()


async def test_supervisor_picks_up_runtime_task() -> None:
    app = bare_app()

    async def idle() -> None:
        await asyncio.sleep(5)

    app._tasks = {asyncio.create_task(idle())}

    async def add_crashing_client() -> None:
        await asyncio.sleep(0.02)

        async def boom() -> None:
            raise RuntimeError("added-client")

        app._tasks.add(asyncio.create_task(boom()))
        app._wake.set()  # будим супервайзер, чтобы он подхватил задачу

    helper = asyncio.create_task(add_crashing_client())
    try:
        with pytest.raises(RuntimeError, match="added-client"):
            await app._supervise()
    finally:
        helper.cancel()
        for task in app._tasks:
            task.cancel()
