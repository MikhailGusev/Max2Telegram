"""Сборка и запуск всех частей моста."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

from .accounts import Account, AccountRegistry, build_accounts
from .channels import build_billing, build_channels, build_inbound
from .clients import CLIENTS_FILE, ClientStore, client_name
from .config import Config, load_config
from .core.ai import AiAssistant
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
        # монетизация — из приватного слоя (если установлен). Нет слоя ->
        # None, и мост работает как полностью бесплатный.
        self.billing = build_billing(config)
        self.accounts: AccountRegistry = build_accounts(config, self.ai, self.license)
        for account in self.accounts:
            account.router.attach_billing(self.billing)
        self.telegram = TelegramBridge(
            config, self.accounts, self.ai, self.license, self.transcriber, self.billing
        )

        # каналы эскалации общие: незачем держать по подключению на аккаунт
        self.channels = build_channels(config)
        for account in self.accounts:
            account.attach_escalator(self._build_escalator(account))

        # supervisor рантайм-задач: заполняется в run(), нужен для подъёма
        # клиентских аккаунтов на лету (после онбординга по QR)
        self._tasks: set[asyncio.Task[None]] | None = None
        self._wake = asyncio.Event()

        # SaaS-клиенты, прошедшие онбординг: поднимаем их сохранённые аккаунты
        self.clients = ClientStore(config.db_path.parent / CLIENTS_FILE)
        for record in self.clients.active():
            try:
                self.spin_up_client(record.tg_user_id, launch=False)
            except Exception:  # noqa: BLE001 - один битый клиент не валит старт
                log.exception("не смог поднять клиента «%s» на старте", record.name)

        # даём боту доступ к онбордингу: приём новых клиентов и подъём аккаунта
        self.telegram.bind_onboarding(self.clients, self.spin_up_client)

        self.wa_webhook = self._build_webhook()

    def _build_escalator(self, account: Account) -> Escalator:
        return Escalator(
            account.storage,
            self.channels,
            self.license,
            after_minutes=account.config.escalate_after_minutes,
            enabled_channels=account.config.escalate_channels,
        )

    # --------------------------------------------------- SaaS-клиенты (рантайм)
    def build_client_account(self, tg_user_id: int) -> Account:
        """Собирает изолированный аккаунт клиента поверх базового конфига.

        Клиент всегда userbot (вошёл по QR) и всегда в плоском режиме (личка,
        без форум-группы). Своя база и сессия по имени client-<id>.
        """
        name = client_name(tg_user_id)
        data_dir = self.config.db_path.parent
        cfg = dataclasses.replace(
            self.config,
            owner_id=int(tg_user_id),
            forum_chat_id=0,
            max_mode="userbot",
            db_path=data_dir / f"{name}.db",
            session_file=str(data_dir / f"{name}_session.json"),
        )
        account = Account(name, cfg, self.ai)
        account.router.attach_billing(self.billing)
        account.attach_escalator(self._build_escalator(account))
        return account

    def spin_up_client(self, tg_user_id: int, *, launch: bool = True) -> Account:
        """Поднимает аккаунт клиента: регистрирует в реестре и (если запущены)
        стартует его задачи, будя supervisor в run()."""
        existing = self.accounts.by_name(client_name(tg_user_id))
        if existing is not None:
            return existing
        account = self.build_client_account(tg_user_id)
        self.accounts.add(account)
        self.telegram.attach_account(account)
        if launch and self._tasks is not None:
            self._tasks.update(self._account_tasks(account))
            self._wake.set()
        log.info("клиент «%s» поднят (owner=%s)", account.name, tg_user_id)
        return account

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

        self._tasks = {asyncio.create_task(self.telegram.run(), name="telegram")}
        for account in self.accounts:
            self._tasks.update(self._account_tasks(account))

        try:
            await self._supervise()
        finally:
            for task in self._tasks or ():
                if not task.done():
                    task.cancel()
            self._tasks = None

    async def _supervise(self) -> None:
        """Держит приложение живым, пока задачи работают.

        Роняем всё только если задача упала с ИСКЛЮЧЕНИЕМ. Штатно завершившуюся
        задачу (например, выключенную эскалацию, которая сразу возвращается)
        просто убираем из набора и живём дальше — иначе один такой возврат ронял
        бы весь сервис в цикл рестартов. `_wake` будит цикл, когда клиент прошёл
        онбординг и его задачи добавлены в self._tasks на лету (spin_up_client).
        """
        assert self._tasks is not None
        while self._tasks:
            waker = asyncio.create_task(self._wake.wait(), name="waker")
            watched = set(self._tasks) | {waker}
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)

            self._wake.clear()
            if waker not in done:
                waker.cancel()

            for task in done:
                if task is waker:
                    continue
                self._tasks.discard(task)
                exc = task.exception()
                if exc is not None:
                    raise exc
            # continue: подхватываем добавленные на лету задачи и ждём дальше

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
        if self.billing is not None:
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
