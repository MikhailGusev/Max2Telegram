"""Базовый интерфейс канала уведомлений."""

from __future__ import annotations

import abc
import logging

log = logging.getLogger("maxbridge.channels")


class ChannelError(RuntimeError):
    """Провайдер вернул ошибку. Роутер логирует и идёт дальше."""


class NotifyChannel(abc.ABC):
    #: имя канала: sms | whatsapp
    name: str = "channel"

    #: какую лицензионную функцию требует канал
    feature: str = ""

    @property
    @abc.abstractmethod
    def configured(self) -> bool:
        """True, если в .env есть всё необходимое для отправки."""

    @abc.abstractmethod
    async def send(self, text: str) -> None:
        """Отправляет уведомление владельцу. Бросает ChannelError при отказе."""

    async def close(self) -> None:
        return None

    @staticmethod
    def trim(text: str, limit: int) -> str:
        """Обрезает текст под лимит канала, сохраняя читаемость."""
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"
