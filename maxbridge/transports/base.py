"""Единый интерфейс транспорта MAX.

Роутер работает только с этим интерфейсом и не знает, читаем мы аккаунт
через внутренний WS-протокол (userbot) или через официальный Bot API.
"""

from __future__ import annotations

import abc
from typing import Awaitable, Callable

from ..models import MaxMessage

MessageHandler = Callable[[MaxMessage], Awaitable[None]]


class MaxTransport(abc.ABC):
    """Базовый транспорт: приносит входящие и умеет отправлять исходящие."""

    #: человекочитаемое имя режима — попадает в /status
    name: str = "base"

    #: видит ли транспорт вообще все сообщения аккаунта
    sees_everything: bool = False

    def __init__(self) -> None:
        self._handler: MessageHandler | None = None

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def _emit(self, message: MaxMessage) -> None:
        if self._handler is not None:
            await self._handler(message)

    @abc.abstractmethod
    async def start(self) -> None:
        """Подключается и работает до отмены задачи."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Закрывает соединения."""

    @abc.abstractmethod
    async def send(self, chat_id: int, text: str, *, reply_to: str = "") -> str:
        """Отправляет текст в чат MAX. Возвращает id отправленного сообщения."""

    async def mark_read(self, chat_id: int, message_id: str) -> None:
        """Помечает прочитанным. В стелс-режиме роутер это просто не вызывает."""
        return None

    async def react(self, chat_id: int, message_id: str, emoji: str) -> None:
        return None

    def chat_title(self, chat_id: int) -> str:
        """Известное имя чата (может быть пустым — тогда роутер возьмёт из БД)."""
        return ""
