"""Единая модель сообщения — общая для всех транспортов."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

Priority = Literal["urgent", "normal", "low"]


@dataclass(slots=True)
class Attachment:
    kind: str  # photo | video | audio | file | sticker | voice | unknown
    url: str = ""
    name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MaxMessage:
    """Входящее (или исходящее) сообщение MAX, нормализованное."""

    chat_id: int
    message_id: str
    text: str = ""
    sender_id: int = 0
    sender_name: str = ""
    chat_title: str = ""
    chat_kind: str = "dialog"  # dialog | chat | channel
    ts: int = field(default_factory=lambda: int(time.time() * 1000))
    outgoing: bool = False
    reply_to: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def preview(self) -> str:
        if self.text:
            return self.text if len(self.text) <= 200 else self.text[:197] + "..."
        if self.attachments:
            return f"[{self.attachments[0].kind}]"
        return "[пусто]"


@dataclass(slots=True)
class OutgoingMessage:
    """Ответ, который уходит из Telegram обратно в MAX."""

    chat_id: int
    text: str
    reply_to: str = ""
