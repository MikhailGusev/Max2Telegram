"""Сводка «что я пропустил» — по команде /digest и по расписанию."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

from ..db import Storage
from .ai import AiAssistant

log = logging.getLogger("maxbridge.digest")


async def build_digest(storage: Storage, ai: AiAssistant, *, hours: int = 24) -> str:
    """Собирает сводку. С AI — осмысленный пересказ, без AI — аккуратный список."""
    since_ts = int((time.time() - hours * 3600) * 1000)
    rows = storage.since(since_ts, only_incoming=True)
    rows = [row for row in rows if row["priority"] != "low"]
    if not rows:
        return ""

    by_chat: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        chat = storage.get_chat(row["max_chat_id"])
        title = (chat["title"] if chat else "") or f"чат {row['max_chat_id']}"
        by_chat[title].append(row)

    if ai.enabled:
        lines = [
            f"[{title}] {row['sender_name'] or '?'}: {' '.join((row['text'] or '').split())[:200]}"
            for title, items in by_chat.items()
            for row in items
        ]
        summary = await ai.digest(lines)
        if summary:
            head = f"<b>Сводка за {hours} ч</b> · сообщений: {len(rows)}\n\n"
            return head + html.escape(summary)

    # запасной вариант без модели
    parts = [f"<b>Сводка за {hours} ч</b> · сообщений: {len(rows)}"]
    for title, items in sorted(by_chat.items(), key=lambda kv: -len(kv[1])):
        urgent = sum(1 for row in items if row["priority"] == "urgent")
        mark = " 🔥" if urgent else ""
        parts.append(f"\n<b>{html.escape(title)}</b>{mark} — {len(items)}")
        for row in items[:3]:
            who = html.escape(row["sender_name"] or "?")
            preview = html.escape(" ".join((row["text"] or "").split())[:100])
            parts.append(f"· {who}: {preview}")
    pending = [row for row in rows if row["needs_reply"] and not row["answered"]]
    if pending:
        parts.append(f"\n<b>Ждут ответа:</b> {len(pending)}")
    return "\n".join(parts)


async def digest_scheduler(
    storage: Storage,
    ai: AiAssistant,
    hour: int,
    send: Callable[[str], Awaitable[None]],
) -> None:
    """Раз в сутки в заданный час отправляет сводку владельцу."""
    if hour < 0 or hour > 23:
        log.info("ежедневная сводка выключена")
        return
    log.info("ежедневная сводка в %02d:00", hour)
    last_sent_day = -1
    while True:
        now = time.localtime()
        if now.tm_hour == hour and now.tm_yday != last_sent_day:
            last_sent_day = now.tm_yday
            try:
                text = await build_digest(storage, ai, hours=24)
                if text:
                    await send(text)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("не смог собрать сводку")
        await asyncio.sleep(300)
