"""Эскалация: если срочное провисело без ответа — достучаться другим каналом.

Сценарий, ради которого это делается: телефон в кармане, Telegram не открыт,
а в MAX прилетело «где счёт, сегодня последний день». Через N минут молчания
приходит SMS или WhatsApp с сутью и именем чата.

Платная функция: каналы включаются только при валидной лицензии Pro.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..channels.base import ChannelError, NotifyChannel
from ..db import Storage
from .licensing import License

log = logging.getLogger("maxbridge.escalation")

#: как часто проверять очередь
TICK_SECONDS = 60

#: сколько сообщений максимум склеивать в одно уведомление
BATCH_LIMIT = 3


class Escalator:
    def __init__(
        self,
        storage: Storage,
        channels: list[NotifyChannel],
        license_: License,
        *,
        after_minutes: int,
        enabled_channels: tuple[str, ...],
    ) -> None:
        self.storage = storage
        self.license = license_
        self.after_minutes = after_minutes
        self.channels = [
            channel
            for channel in channels
            if (not enabled_channels or channel.name in enabled_channels)
        ]

    @property
    def active(self) -> bool:
        return bool(self.after_minutes > 0 and self.usable_channels)

    @property
    def usable_channels(self) -> list[NotifyChannel]:
        """Каналы, разрешённые лицензией. Без Pro список пустой."""
        allowed = []
        for channel in self.channels:
            if channel.feature and not self.license.allows(channel.feature):
                continue
            allowed.append(channel)
        return allowed

    def explain(self) -> str:
        if self.after_minutes <= 0:
            return "эскалация выключена (ESCALATE_AFTER_MINUTES=0)"
        if not self.channels:
            return "эскалация включена, но ни один канал не настроен"
        blocked = [c.name for c in self.channels if c.feature and not self.license.allows(c.feature)]
        if blocked:
            return f"каналы {', '.join(blocked)} требуют лицензию Pro"
        names = ", ".join(c.name for c in self.usable_channels)
        return f"эскалация через {names} после {self.after_minutes} мин молчания"

    async def run(self) -> None:
        if not self.active:
            log.info("%s", self.explain())
            return
        log.info("%s", self.explain())
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - цикл не должен падать
                log.exception("сбой в цикле эскалации")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        cutoff = int((time.time() - self.after_minutes * 60) * 1000)
        rows = self.storage.pending_escalation(cutoff)
        if not rows:
            return

        batch = rows[:BATCH_LIMIT]
        text = self._compose(batch, extra=len(rows) - len(batch))
        delivered = False
        for channel in self.usable_channels:
            try:
                await channel.send(text)
                delivered = True
            except ChannelError as exc:
                log.warning("канал %s не доставил: %s", channel.name, exc)
            except Exception:  # noqa: BLE001
                log.exception("канал %s упал", channel.name)

        # помечаем в любом случае: иначе при постоянной ошибке провайдера
        # мы будем долбить его одним и тем же сообщением каждую минуту
        self.storage.mark_escalated(row["id"] for row in batch)
        if delivered:
            # запоминаем чат последней эскалации: ответ из WhatsApp без явного
            # номера чата уйдёт именно сюда (см. Router.handle_external_reply)
            self.storage.set("last_escalated_chat", str(batch[-1]["max_chat_id"]))
        else:
            log.error("эскалация не доставлена ни одним каналом")

    def _compose(self, rows: list, extra: int = 0) -> str:
        parts = ["MAX: без ответа"]
        for row in rows:
            who = row["sender_name"] or row["chat_title"] or f"чат {row['max_chat_id']}"
            preview = " ".join((row["text"] or "[вложение]").split())[:70]
            parts.append(f"• {who}: {preview}")
        if extra > 0:
            parts.append(f"…и ещё {extra}")
        parts.append("Ответь в Telegram.")
        return "\n".join(parts)
