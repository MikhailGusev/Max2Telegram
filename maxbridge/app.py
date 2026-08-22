"""Сборка и запуск всех частей моста."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .accounts import Account, AccountRegistry, build_accounts
from .channels import build_channels, build_inbound
from .config import Config, load_config
from .core.ai import AiAssistant
from .core.billing import Billing
from .core.digest import digest_scheduler
from .core.escalation import Escalator
from .core.followup import followup_watcher
from .core.licensing import load_license
from .core.transcribe import Transcriber
from .logging_setup import setup_logging
from .telegram import TelegramBridge

log = logging.getLogger("maxbridge.app")


class Application:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.license = load_license(config.license_key)
        self.ai = AiAssistant(
            config.anthropic_key if config.ai_enabled else "",
            model=config.ai_model,
            lang=config.ai_lang,
            provider=config.ai_provider,
            proxy=config.ai_proxy,
            base_url=config.ai_base_url,
        )
        self.transcriber = Transcriber(
            config.asr_url, config.asr_key, config.asr_model, config.asr_lang
        )
        # подписки и дневной AI-лимит — одна база на инсталляцию
        self.billing = Billing(
            config.db_path.parent / "billing.db", free_ai_per_day=config.free_ai_per_day
        )
        self.accounts: AccountRegistry = build_accounts(config, self.ai, self.license)
        for account in self.accounts:
            account.router.attach_billing(self.billing)
        self.telegram = TelegramBridge(
            config, self.accounts, self.ai, self.license, self.transcriber, self.billing
        )

        # каналы эскалации общие: незачем держать по подключению на аккаунт
        self.channels = build_channels(config)
        for account in self.accounts:
            account.attach_escalator(
                Escalator(
                    account.storage,
                    self.channels,
                    self.license,
                    after_minutes=account.config.escalate_after_minutes,
                    enabled_channels=account.config.escalate_channels,
                )
            )

        self.wa_webhook = self._build_webhook()

    def _build_webhook(self) -> Any | None:
        """Приём ответов из внешнего канала. Только при лицензии Pro."""
        if not self.config.wa_webhook_enabled:
            return None
        if not self.license.allows("whatsapp"):
            log.warning("ответы из WhatsApp требуют лицензию Pro — вебхук не поднимаю")
            return None

        server = build_inbound(self.config, self._on_whatsapp_reply)
        if server is None:
            log.warning(
                "приём ответов включён, но пакет с каналами не установлен: "
                "pip install maxbridge-pro"
            )
        return server

    async def _on_whatsapp_reply(self, text: str, sender: str) -> None:
        # ответ из WhatsApp относится к аккаунту, по которому была эскалация;
        # при нескольких аккаунтах берём тот, где эскалация была последней
        account = max(
            self.accounts,
            key=lambda acc: int(acc.storage.get("last_escalated_at", "0") or 0),
        )
        result = await account.router.handle_external_reply(text, sender)
        log.info("WhatsApp -> MAX (аккаунт «%s»): %s", account.name, result)

    async def run(self) -> None:
        log.info("лицензия: %s", self.license.describe())
        for account in self.accounts:
            if not account.transport.sees_everything:
                log.warning(
                    "аккаунт «%s» в режиме botapi: бот увидит только адресованные ему "
                    "сообщения. Для полного охвата нужен MAX_MODE=userbot",
                    account.name,
                )

        if self.wa_webhook is not None:
            await self.wa_webhook.start()

        tasks = [asyncio.create_task(self.telegram.run(), name="telegram")]
        for account in self.accounts:
            tasks.extend(self._account_tasks(account))

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        for task in done:
            if task.exception() is not None:
                raise task.exception()  # type: ignore[misc]

    def _account_tasks(self, account: Account) -> list[asyncio.Task[None]]:
        async def notify(text: str) -> None:
            await self.telegram.notify(account, text, parse_mode="HTML")

        tasks = [
            asyncio.create_task(account.router.run(), name=f"max:{account.name}"),
            asyncio.create_task(
                digest_scheduler(
                    account.storage, self.ai, account.config.digest_hour, notify
                ),
                name=f"digest:{account.name}",
            ),
            asyncio.create_task(
                followup_watcher(account.storage, account.config.followup_minutes, notify),
                name=f"followup:{account.name}",
            ),
        ]
        if account.escalator is not None:
            tasks.append(
                asyncio.create_task(
                    account.escalator.run(), name=f"escalation:{account.name}"
                )
            )
        return tasks

    async def close(self) -> None:
        if self.wa_webhook is not None:
            await self.wa_webhook.stop()
        for account in self.accounts:
            await account.close()
        await self.telegram.close()
        await self.ai.aclose()
        await self.transcriber.close()
        self.billing.close()
        for channel in self.channels:
            await channel.close()


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
