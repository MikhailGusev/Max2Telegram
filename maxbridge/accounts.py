"""Мультиаккаунт: несколько аккаунтов MAX на одном сервере.

Зачем: рабочий и личный MAX у одного человека; агентство, ведущее переписку
нескольких клиентов; команда, где каждый отвечает за свой аккаунт.

Как устроено. Каждый аккаунт полностью изолирован: своя база, своя сессия MAX,
своя группа-форум в Telegram. Общими остаются только Telegram-бот, AI и каналы
эскалации — их незачем дублировать.

Описание аккаунтов лежит в accounts.json рядом с .env:

    [
      {"name": "work",     "owner_id": 111, "forum_chat_id": -1001, "max_phone": "+7..."},
      {"name": "personal", "owner_id": 111, "forum_chat_id": -1002, "max_phone": "+7..."}
    ]

Файла нет — работает один аккаунт из .env, как раньше. Больше одного аккаунта
требует лицензию Team: без неё поднимется только первый, о чём будет сказано
в логе прямым текстом.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

from .config import Config
from .core.ai import AiAssistant
from .core.escalation import Escalator
from .core.licensing import License
from .core.router import Router
from .db import Storage
from .transports import build_transport
from .transports.base import MaxTransport

log = logging.getLogger("maxbridge.accounts")

ACCOUNTS_FILE = "accounts.json"

#: поля из accounts.json, которые разрешено переопределять для аккаунта
OVERRIDABLE = {
    "owner_id",
    "forum_chat_id",
    "max_mode",
    "max_phone",
    "max_bot_token",
    "stealth_mode",
    "digest_hour",
    "followup_minutes",
    "escalate_after_minutes",
    "ai_enabled",
}


class Account:
    """Один аккаунт MAX со всем своим хозяйством."""

    def __init__(self, name: str, config: Config, ai: AiAssistant) -> None:
        self.name = name
        self.config = config
        self.ai = ai
        self.storage = Storage(config.db_path)
        self.transport: MaxTransport = build_transport(config)
        self.router = Router(config, self.storage, self.transport, ai)
        self.escalator: Escalator | None = None

    @property
    def owner_id(self) -> int:
        return self.config.owner_id

    @property
    def forum_chat_id(self) -> int:
        return self.config.forum_chat_id

    def attach_escalator(self, escalator: Escalator) -> None:
        self.escalator = escalator

    def describe(self) -> str:
        coverage = "весь аккаунт" if self.transport.sees_everything else "только адресованное боту"
        return f"{self.name}: {self.transport.name} ({coverage}), база {self.config.db_path.name}"

    async def close(self) -> None:
        await self.transport.stop()
        self.storage.close()


class AccountRegistry:
    """Все поднятые аккаунты и быстрый поиск нужного."""

    def __init__(self, accounts: list[Account]) -> None:
        self.accounts = accounts
        self._by_forum = {acc.forum_chat_id: acc for acc in accounts if acc.forum_chat_id}

    def __iter__(self):
        return iter(self.accounts)

    def __len__(self) -> int:
        return len(self.accounts)

    @property
    def multi(self) -> bool:
        return len(self.accounts) > 1

    def by_forum(self, chat_id: int) -> Account | None:
        return self._by_forum.get(chat_id)

    def add(self, account: Account) -> None:
        """Добавляет аккаунт в рантайме (SaaS-клиент после онбординга)."""
        self.accounts.append(account)
        if account.forum_chat_id:
            self._by_forum[account.forum_chat_id] = account

    def rebind(self, account: Account, chat_id: int) -> None:
        """Перепривязка группы командой /bind."""
        self._by_forum.pop(account.forum_chat_id, None)
        account.config.forum_chat_id = chat_id
        self._by_forum[chat_id] = account

    def by_name(self, name: str) -> Account | None:
        lowered = name.strip().lower()
        for account in self.accounts:
            if account.name.lower() == lowered:
                return account
        return None

    def owned_by(self, user_id: int) -> list[Account]:
        return [acc for acc in self.accounts if acc.owner_id == user_id]

    def is_owner(self, user_id: int) -> bool:
        return any(acc.owner_id == user_id for acc in self.accounts)


def read_specs(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("не смог прочитать %s: %s — работаю на одном аккаунте из .env", path, exc)
        return []

    if isinstance(raw, dict):
        raw = raw.get("accounts") or []
    if not isinstance(raw, list):
        log.error("%s: ожидался список аккаунтов — работаю на одном аккаунте", path)
        return []
    return [item for item in raw if isinstance(item, dict)]


def account_config(base: Config, spec: dict[str, Any], name: str) -> Config:
    """Конфиг аккаунта = базовый из .env плюс точечные переопределения."""
    overrides: dict[str, Any] = {}
    for key, value in spec.items():
        if key in OVERRIDABLE:
            overrides[key] = value
        elif key != "name":
            log.warning("аккаунт «%s»: поле «%s» нельзя переопределить, пропускаю", name, key)

    data_dir = base.db_path.parent
    overrides["db_path"] = data_dir / f"{name}.db"
    overrides["session_file"] = str(data_dir / f"{name}_session.json")
    return dataclasses.replace(base, **overrides)


def build_accounts(base: Config, ai: AiAssistant, license_: License) -> AccountRegistry:
    """Собирает аккаунты: из accounts.json, иначе один из .env."""
    path = base.db_path.parent.parent / ACCOUNTS_FILE
    specs = read_specs(path) if path.exists() else []

    if not specs:
        return AccountRegistry([Account("default", base, ai)])

    if len(specs) > 1 and not license_.allows("multiaccount"):
        log.warning(
            "в %s описано аккаунтов: %d, но мультиаккаунт входит в тариф Team. "
            "Поднимаю только первый — «%s»",
            ACCOUNTS_FILE,
            len(specs),
            specs[0].get("name") or "account-1",
        )
        specs = specs[:1]

    accounts: list[Account] = []
    seen: set[str] = set()
    for index, spec in enumerate(specs, start=1):
        name = str(spec.get("name") or f"account-{index}").strip()
        if name in seen:
            log.error("аккаунт «%s» описан дважды — второй пропускаю", name)
            continue
        seen.add(name)
        accounts.append(Account(name, account_config(base, spec, name), ai))

    if not accounts:
        return AccountRegistry([Account("default", base, ai)])

    log.info("аккаунтов поднято: %d", len(accounts))
    for account in accounts:
        log.info("  %s", account.describe())
    return AccountRegistry(accounts)
