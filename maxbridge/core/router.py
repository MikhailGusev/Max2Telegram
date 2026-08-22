"""Роутер — сердце моста. Гоняет сообщения в обе стороны.

MAX -> Telegram:
    транспорт -> дедупликация -> правила -> (AI) -> запись в БД ->
    доставка в тему форума -> автоответ и отметка о прочтении (если не стелс)

Telegram -> MAX:
    ответ в теме -> определяем чат -> отправляем от имени владельца ->
    помечаем переписку отвеченной (снимает эскалацию и радар незакрытых)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from ..config import Config
from ..db import Storage
from ..models import MaxMessage
from ..transports.base import MaxTransport
from . import rules
from .ai import AiAssistant
from .rules import Verdict

log = logging.getLogger("maxbridge.router")


class TelegramSink(Protocol):
    """То, что роутер ожидает от Telegram-стороны."""

    async def deliver(self, message: MaxMessage, verdict: Verdict) -> int | None: ...


class Router:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        transport: MaxTransport,
        ai: AiAssistant,
    ) -> None:
        self.config = config
        self.storage = storage
        self.transport = transport
        self.ai = ai
        self.telegram: TelegramSink | None = None
        self.billing: Any = None
        self._seen: set[tuple[int, str]] = set()

    def attach_telegram(self, sink: TelegramSink) -> None:
        self.telegram = sink

    def attach_billing(self, billing: Any) -> None:
        self.billing = billing

    # ------------------------------------------------------- MAX -> Telegram
    async def handle_max_message(self, message: MaxMessage) -> None:
        key = (message.chat_id, str(message.message_id))
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self._seen) > 5000:  # не даём множеству расти бесконечно
            self._seen = set(list(self._seen)[-2000:])

        title = message.chat_title or self.transport.chat_title(message.chat_id)
        if not title:
            known = self.storage.get_chat(message.chat_id)
            title = known["title"] if known else ""
        message.chat_title = title
        self.storage.upsert_chat(message.chat_id, title, message.chat_kind)

        # своё же сообщение (отправлено с телефона) — просто фиксируем и
        # считаем переписку отвеченной
        if message.outgoing:
            self.storage.save_message(message)
            self.storage.mark_chat_answered(message.chat_id)
            return

        verdict = rules.classify(message, self.storage)
        verdict = await self._maybe_refine(message, verdict)

        row_id = self.storage.save_message(
            message,
            priority=verdict.priority,
            intent=verdict.intent,
            needs_reply=verdict.needs_reply,
        )
        if row_id is None:  # такое сообщение уже лежит в базе
            return

        if self.telegram is not None:
            try:
                tg_id = await self.telegram.deliver(message, verdict)
                if tg_id:
                    self.storage.link_tg_message(row_id, tg_id)
            except Exception:  # noqa: BLE001 - доставка не должна ронять приём
                log.exception("не смог доставить сообщение в Telegram")

        if verdict.autoreply:
            await self._autoreply(message, verdict.autoreply)

        if not self.config.stealth_mode:
            try:
                await self.transport.mark_read(message.chat_id, str(message.message_id))
            except Exception as exc:  # noqa: BLE001
                log.debug("не смог пометить прочитанным: %s", exc)

    async def _maybe_refine(self, message: MaxMessage, verdict: Verdict) -> Verdict:
        """Спрашиваем модель только там, где правила не дали уверенного ответа."""
        if not self.ai.enabled or not self.config.ai_active:
            return verdict
        # умные AI-приоритеты на каждое входящее — премиум-функция: на free
        # это мгновенно сожгло бы дневной лимит. Free живёт на правилах.
        if self.billing is not None and not self.billing.is_premium(self.config.owner_id):
            return verdict
        if verdict.priority != "normal" or verdict.intent:
            return verdict  # правила уже всё решили — экономим вызов
        if len(message.text.strip()) < 12:
            return verdict

        try:
            refined = await asyncio.wait_for(
                self.ai.classify(
                    message.text, chat=message.chat_title, sender=message.sender_name
                ),
                timeout=20,
            )
        except asyncio.TimeoutError:
            log.debug("AI-классификация не уложилась в таймаут")
            return verdict
        if refined is None:
            return verdict

        verdict.priority = refined.priority
        verdict.intent = refined.intent
        verdict.needs_reply = refined.needs_reply
        verdict.reason = f"AI: {refined.reason}"
        return verdict

    async def _autoreply(self, message: MaxMessage, text: str) -> None:
        try:
            await self.transport.send(message.chat_id, text, reply_to=str(message.message_id))
            log.info("автоответ отправлен в чат %s", message.chat_id)
        except Exception:  # noqa: BLE001
            log.exception("автоответ не ушёл")

    # ------------------------------------------------------- Telegram -> MAX
    async def send_to_max(self, chat_id: int, text: str, *, reply_to: str = "") -> str:
        """Отправляет ответ в MAX и фиксирует его в истории."""
        message_id = await self.transport.send(chat_id, text, reply_to=reply_to)
        self.storage.save_message(
            MaxMessage(
                chat_id=chat_id,
                message_id=message_id or f"local-{id(text)}",
                text=text,
                outgoing=True,
                chat_title=self.transport.chat_title(chat_id),
            )
        )
        self.storage.mark_chat_answered(chat_id)
        return message_id

    async def send_media_to_max(
        self,
        chat_id: int,
        data: bytes,
        *,
        filename: str,
        kind: str = "file",
        caption: str = "",
        reply_to: str = "",
    ) -> str:
        """Отправляет файл в MAX и фиксирует его в истории."""
        message_id = await self.transport.send_media(
            chat_id, data, filename=filename, kind=kind, caption=caption, reply_to=reply_to
        )
        self.storage.save_message(
            MaxMessage(
                chat_id=chat_id,
                message_id=message_id or f"local-file-{filename}",
                text=caption or f"[{kind}] {filename}",
                outgoing=True,
                chat_title=self.transport.chat_title(chat_id),
            )
        )
        self.storage.mark_chat_answered(chat_id)
        log.info("файл %s (%.0f КБ) отправлен в чат %s", filename, len(data) / 1024, chat_id)
        return message_id

    async def handle_external_reply(self, text: str, sender: str = "") -> str:
        """Ответ, пришедший не из Telegram, а из внешнего канала (WhatsApp).

        Два формата:
            «#42 текст»  — явно указан чат MAX (номер виден в /chats);
            «текст»      — уходит в чат последней эскалации.
        """
        text = text.strip()
        if not text:
            return "пустое сообщение"

        chat_id = 0
        if text.startswith("#"):
            head, _, rest = text[1:].partition(" ")
            if head.lstrip("-").isdigit():
                chat_id, text = int(head), rest.strip()

        if not chat_id:
            stored = self.storage.get("last_escalated_chat")
            if not stored:
                return "не понимаю, в какой чат отправить: напиши «#<id> текст»"
            chat_id = int(stored)

        if not text:
            return "текст пустой"

        await self.send_to_max(chat_id, text)
        chat = self.storage.get_chat(chat_id)
        title = (chat["title"] if chat else "") or str(chat_id)
        log.info("ответ из внешнего канала (%s) ушёл в чат %s", sender or "?", title)
        return f"отправлено в «{title}»"

    # --------------------------------------------------------------- запуск
    async def run(self) -> None:
        self.transport.on_message(self.handle_max_message)
        log.info(
            "транспорт MAX: %s (%s)",
            self.transport.name,
            "видит все сообщения" if self.transport.sees_everything
            else "видит только адресованное боту",
        )
        await self.transport.start()

    def context_lines(self, chat_id: int, limit: int = 15) -> list[str]:
        """История чата в виде строк — для черновиков ответа."""
        lines: list[str] = []
        for row in self.storage.recent(chat_id, limit):
            who = "Я" if row["outgoing"] else (row["sender_name"] or "Собеседник")
            lines.append(f"{who}: {row['text']}")
        return lines

    def describe_state(self) -> dict[str, Any]:
        stats = self.storage.stats()
        return {
            "transport": self.transport.name,
            "sees_everything": self.transport.sees_everything,
            "stealth": self.config.stealth_mode,
            "ai": self.ai.enabled,
            **stats,
        }
