"""Сборка и запуск всех частей моста."""

from __future__ import annotations

import asyncio
import logging

from .channels import build_channels
from .channels.webhook import WhatsAppWebhookServer
from .config import Config, load_config
from .core.ai import AiAssistant
from .core.digest import digest_scheduler
from .core.escalation import Escalator
from .core.followup import followup_watcher
from .core.licensing import load_license
from .core.router import Router
from .core.transcribe import Transcriber
from .db import Storage
from .logging_setup import setup_logging
from .telegram import TelegramBridge
from .transports import build_transport

log = logging.getLogger("maxbridge.app")


class Application:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.storage = Storage(config.db_path)
        self.license = load_license(config.license_key)
        self.ai = AiAssistant(
            config.anthropic_key if config.ai_enabled else "",
            model=config.ai_model,
            lang=config.ai_lang,
        )
        self.transcriber = Transcriber(
            config.asr_url, config.asr_key, config.asr_model, config.asr_lang
        )
        self.transport = build_transport(config)
        self.router = Router(config, self.storage, self.transport, self.ai)
        self.telegram = TelegramBridge(
            config, self.storage, self.router, self.ai, self.license, self.transcriber
        )
        self.router.attach_telegram(self.telegram)
        self.channels = build_channels(config)
        self.escalator = Escalator(
            self.storage,
            self.channels,
            self.license,
            after_minutes=config.escalate_after_minutes,
            enabled_channels=config.escalate_channels,
        )
        self.wa_webhook = self._build_webhook()

    def _build_webhook(self) -> WhatsAppWebhookServer | None:
        """Приём ответов из WhatsApp. Только при лицензии Pro и явном включении."""
        if not self.config.wa_webhook_enabled:
            return None
        if not self.license.allows("whatsapp"):
            log.warning("ответы из WhatsApp требуют лицензию Pro — вебхук не поднимаю")
            return None

        allowed = {
            "".join(ch for ch in number if ch.isdigit())
            for number in (self.config.whatsapp_to, self.config.sms_to)
            if number
        }
        return WhatsAppWebhookServer(
            self._on_whatsapp_reply,
            host=self.config.wa_webhook_host,
            port=self.config.wa_webhook_port,
            verify_token=self.config.wa_verify_token,
            allowed_numbers=allowed,
        )

    async def _on_whatsapp_reply(self, text: str, sender: str) -> None:
        result = await self.router.handle_external_reply(text, sender)
        log.info("WhatsApp -> MAX: %s", result)

    async def run(self) -> None:
        log.info("лицензия: %s", self.license.describe())
        if not self.transport.sees_everything:
            log.warning(
                "режим botapi: бот увидит только адресованные ему сообщения. "
                "Для полного охвата аккаунта нужен MAX_MODE=userbot"
            )

        async def notify(text: str) -> None:
            await self.telegram.notify_owner(text, parse_mode="HTML")

        if self.wa_webhook is not None:
            await self.wa_webhook.start()

        tasks = [
            asyncio.create_task(self.telegram.run(), name="telegram"),
            asyncio.create_task(self.router.run(), name="max"),
            asyncio.create_task(self.escalator.run(), name="escalation"),
            asyncio.create_task(
                digest_scheduler(self.storage, self.ai, self.config.digest_hour, notify),
                name="digest",
            ),
            asyncio.create_task(
                followup_watcher(self.storage, self.config.followup_minutes, notify),
                name="followup",
            ),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        for task in done:
            if task.exception() is not None:
                raise task.exception()  # type: ignore[misc]

    async def close(self) -> None:
        if self.wa_webhook is not None:
            await self.wa_webhook.stop()
        await self.transport.stop()
        await self.telegram.close()
        await self.transcriber.close()
        for channel in self.channels:
            await channel.close()
        self.storage.close()


async def main_async() -> int:
    config = load_config()
    setup_logging(config.log_level, config.db_path.parent / "maxbridge.log")

    problems = config.validate()
    if problems:
        for problem in problems:
            log.error("конфигурация: %s", problem)
        log.error("заполни .env по образцу .env.example и запусти снова")
        return 2

    app = Application(config)
    try:
        await app.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("остановка по сигналу")
    finally:
        await app.close()
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0
