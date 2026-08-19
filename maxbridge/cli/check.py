"""Проверка установки: что настроено, что включено, что молчит.

    python -m maxbridge check
"""

from __future__ import annotations

import asyncio

from ..channels import build_channels
from ..cli.login import _configs
from ..config import load_config
from ..core.ai import _hide_credentials
from ..core.licensing import load_license
from ..db import Storage
from ..logging_setup import setup_logging


async def check_flow() -> int:
    config = load_config()
    setup_logging(config.log_level)

    print("=== MaxBridge: проверка ===\n")

    problems = config.validate()
    if problems:
        print("Проблемы конфигурации:")
        for problem in problems:
            print(f"  ✗ {problem}")
    else:
        print("Конфигурация: ✓")

    license_ = load_license(config.license_key)
    accounts = _configs(config)

    print(f"\nАккаунтов MAX: {len(accounts)}")
    if len(accounts) > 1 and not license_.allows("multiaccount"):
        print("  ⚠ мультиаккаунт входит в тариф Team — поднимется только первый")
    for name, account_cfg in accounts.items():
        print(f"\n  [{name}] режим {account_cfg.max_mode}")
        if account_cfg.max_mode == "userbot":
            exists = account_cfg.session_path.exists()
            print(f"    сессия: {'✓ есть' if exists else '✗ нет'} ({account_cfg.session_path})")
            if not exists:
                suffix = f" --account {name}" if len(accounts) > 1 else ""
                print(f"    -> выполни: python -m maxbridge login{suffix}")
            print("    охват: все сообщения аккаунта")
        else:
            print("    охват: только сообщения, адресованные боту")
        if not account_cfg.forum_chat_id:
            print("    ⚠ группа-приёмник не задана — выполни /bind в нужной группе")

    print(f"\nAI: {'✓ включён' if config.ai_active else '✗ выключен (нет ANTHROPIC_API_KEY)'}")
    if config.ai_active:
        print(f"  модель: {config.ai_model}")
        if config.ai_proxy:
            print(f"  прокси: {_hide_credentials(config.ai_proxy)} (только для Anthropic)")
        else:
            print("  прокси: нет — api.anthropic.com должен быть доступен напрямую")
        if config.ai_base_url:
            print(f"  адрес: {config.ai_base_url}")

    if config.asr_url:
        print(f"Голосовые в текст: ✓ {config.asr_url} ({config.asr_model})")
    else:
        print("Голосовые в текст: ✗ выключено (нет ASR_URL)")

    print(f"Стелс-режим: {'✓' if config.stealth_mode else '✗'}")
    print(f"Ежедневная сводка: {config.digest_hour if config.digest_hour >= 0 else 'выключена'}")
    print(f"Радар незакрытых: {config.followup_minutes or 'выключен'} мин")

    print(f"\nЛицензия: {license_.describe()}")

    channels = build_channels(config)
    print("\nКаналы эскалации:")
    if not channels:
        print("  — ни один не настроен")
    for channel in channels:
        allowed = license_.allows(channel.feature) if channel.feature else True
        status = "✓ готов" if allowed else "✗ нужна лицензия Pro"
        print(f"  {channel.name}: {status}")
        await channel.close()
    if config.escalate_after_minutes:
        print(f"  порог: {config.escalate_after_minutes} мин молчания")
    else:
        print("  порог: эскалация выключена (ESCALATE_AFTER_MINUTES=0)")

    storage = Storage(config.db_path)
    stats = storage.stats()
    storage.close()
    print(
        f"\nБаза {config.db_path}: чатов {stats['chats']}, сообщений {stats['messages']},"
        f" ждут ответа {stats['pending']}"
    )

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check_flow()))
