"""Каналы эскалации: куда достучаться, если Telegram молчит.

Платная часть продукта (лицензия Pro): SMS и WhatsApp. Оба канала —
однонаправленные уведомления: «тебе написали важное, зайди и ответь».
Двусторонний диалог остаётся в Telegram, где есть темы и кнопки.
"""

from __future__ import annotations

from ..config import Config
from .base import NotifyChannel
from .sms import SmsChannel
from .whatsapp import WhatsAppChannel

__all__ = ["NotifyChannel", "SmsChannel", "WhatsAppChannel", "build_channels"]


def build_channels(config: Config) -> list[NotifyChannel]:
    """Собирает только те каналы, что реально настроены в .env."""
    channels: list[NotifyChannel] = [
        SmsChannel(
            provider=config.sms_provider,
            api_key=config.sms_api_key,
            login=config.sms_login,
            password=config.sms_password,
            sender=config.sms_from,
            recipient=config.sms_to,
        ),
        WhatsAppChannel(
            provider=config.whatsapp_provider,
            token=config.whatsapp_token,
            phone_number_id=config.whatsapp_phone_id,
            recipient=config.whatsapp_to,
            waha_url=config.waha_url,
            waha_session=config.waha_session,
            template=config.whatsapp_template,
        ),
    ]
    return [channel for channel in channels if channel.configured]
