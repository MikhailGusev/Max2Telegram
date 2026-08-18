"""Радар незакрытых: «тебе задали вопрос N часов назад и ты не ответил»."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Awaitable, Callable

from ..db import Storage

log = logging.getLogger("maxbridge.followup")

TICK_SECONDS = 600


async def followup_watcher(
    storage: Storage,
    minutes: int,
    send: Callable[[str], Awaitable[None]],
) -> None:
    """Периодически напоминает о вопросах, оставшихся без ответа."""
    if minutes <= 0:
        log.info("радар незакрытых выключен")
        return
    log.info("радар незакрытых: напоминаю через %d мин", minutes)

    reminded: set[int] = set()
    while True:
        try:
            cutoff = int((time.time() - minutes * 60) * 1000)
            rows = [row for row in storage.unanswered(cutoff) if row["id"] not in reminded]
            if rows:
                lines = ["<b>Висит без ответа</b>"]
                for row in rows[:10]:
                    chat = storage.get_chat(row["max_chat_id"])
                    title = (chat["title"] if chat else "") or str(row["max_chat_id"])
                    ago = int((time.time() * 1000 - row["ts"]) / 60000)
                    preview = html.escape(" ".join((row["text"] or "").split())[:80])
                    lines.append(f"· <b>{html.escape(title)}</b> ({ago} мин): {preview}")
                    reminded.add(row["id"])
                if len(rows) > 10:
                    lines.append(f"…и ещё {len(rows) - 10}")
                await send("\n".join(lines))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("сбой радара незакрытых")
        await asyncio.sleep(TICK_SECONDS)
