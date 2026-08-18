"""Транспорты MAX. Выбор режима — переменной MAX_MODE в .env."""

from __future__ import annotations

from ..config import Config
from .base import MaxTransport
from .botapi import BotApiTransport
from .userbot import UserbotTransport

__all__ = ["MaxTransport", "BotApiTransport", "UserbotTransport", "build_transport"]


def build_transport(config: Config) -> MaxTransport:
    if config.max_mode == "botapi":
        return BotApiTransport(config.max_bot_token, config.max_api_base)
    return UserbotTransport(config.session_path)
