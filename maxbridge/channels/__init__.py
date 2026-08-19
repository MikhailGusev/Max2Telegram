"""Каналы эскалации: куда достучаться, если Telegram молчит.

В ядре живёт только интерфейс `NotifyChannel` и механизм подключения. Сами
каналы поставляются отдельным пакетом и подхватываются через точки входа —
ядро ничего не знает ни про SMS-провайдеров, ни про WhatsApp.

Пакет с каналами объявляет их так:

    [project.entry-points."maxbridge.channels"]
    sms = "maxbridge_pro.sms:build"

Фабрика принимает конфиг и возвращает канал либо None, если он не настроен.
Пакета нет — список каналов пуст, мост работает без эскалации.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any, Callable

from ..config import Config
from .base import ChannelError, NotifyChannel

log = logging.getLogger("maxbridge.channels")

__all__ = ["NotifyChannel", "ChannelError", "build_channels", "build_inbound"]

#: точки входа: исходящие уведомления и приём ответов
CHANNELS_GROUP = "maxbridge.channels"
INBOUND_GROUP = "maxbridge.inbound"


def _load(group: str) -> list[tuple[str, Callable[..., Any]]]:
    found: list[tuple[str, Callable[..., Any]]] = []
    for entry in entry_points(group=group):
        try:
            found.append((entry.name, entry.load()))
        except Exception:  # noqa: BLE001 - кривой плагин не должен ронять мост
            log.exception("не смог загрузить «%s» из группы %s", entry.name, group)
    return found


def build_channels(config: Config) -> list[NotifyChannel]:
    """Собирает каналы, которые нашлись и реально настроены в .env."""
    channels: list[NotifyChannel] = []
    for name, factory in _load(CHANNELS_GROUP):
        try:
            channel = factory(config)
        except Exception:  # noqa: BLE001
            log.exception("канал «%s» не собрался", name)
            continue
        if channel is None:
            continue
        if not channel.configured:
            log.debug("канал «%s» найден, но не настроен — пропускаю", name)
            continue
        channels.append(channel)

    if not channels:
        log.debug(
            "каналы эскалации не подключены (пакет с каналами не установлен "
            "или ни один не настроен)"
        )
    return channels


def build_inbound(config: Config, on_reply: Callable[[str, str], Any]) -> Any | None:
    """Сервер приёма ответов из внешнего канала. None, если поставщика нет."""
    for name, factory in _load(INBOUND_GROUP):
        try:
            server = factory(config, on_reply)
        except Exception:  # noqa: BLE001
            log.exception("приёмник «%s» не собрался", name)
            continue
        if server is not None:
            return server
    return None
