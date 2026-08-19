"""Проверка установки: что настроено, что включено, что молчит.

    python -m maxbridge check
"""

from __future__ import annotations

import asyncio

from ..channels import build_channels
from ..config import load_config
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

    print(f"\nРежим MAX: {config.max_mode}")
    if config.max_mode == "userbot":
        exists = config.session_path.exists()
        print(f"  сессия {config.session_path}: {'✓ есть' if exists else '✗ нет'}")
        if not exists:
            print("  -> выполни: python -m maxbridge login")
        print("  охват: все сообщения аккаунта")
    else:
        print("  охват: только сообщения, адресованные боту")

    print(f"\nAI: {'✓ включён' if config.ai_active else '✗ выключен (нет ANTHROPIC_API_KEY)'}")
    if config.ai_active:
        print(f"  модель: {config.ai_model}")

    if config.asr_url:
        print(f"Голосовые в текст: ✓ {config.asr_url} ({config.asr_model})")
    else:
        print("Голосовые в текст: ✗ выключено (нет ASR_URL)")

    print(f"Стелс-режим: {'✓' if config.stealth_mode else '✗'}")
    print(f"Ежедневная сводка: {config.digest_hour if config.digest_hour >= 0 else 'выключена'}")
    print(f"Радар незакрытых: {config.followup_minutes or 'выключен'} мин")

    license_ = load_license(config.license_key)
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
