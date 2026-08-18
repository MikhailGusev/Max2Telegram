"""Разовый вход в аккаунт MAX по SMS-коду.

    python -m maxbridge login

Создаёт параллельную WEB-сессию: телефон продолжает работать как раньше.
Токен ложится в data/max_session.json — этот файл в .gitignore, храни его
как пароль. Отзыв: в приложении MAX завершить сессию «MaxBridge».
"""

from __future__ import annotations

import asyncio

from ..config import load_config
from ..logging_setup import setup_logging
from ..maxproto import MaxAuthError, MaxWSClient


async def login_flow() -> int:
    config = load_config()
    setup_logging(config.log_level)

    phone = config.max_phone or input("Номер телефона MAX (+79990000000): ").strip()
    if not phone:
        print("Номер не введён — прерываю.")
        return 2

    client = MaxWSClient(config.session_path)
    if client.has_session:
        answer = input(
            f"Сессия уже есть ({config.session_path}). Перелогиниться? [y/N]: "
        ).strip().lower()
        if answer not in {"y", "yes", "д", "да"}:
            print("Оставляю как есть.")
            return 0

    try:
        await client.connect()
        print("Запрашиваю код…")
        sms_token = await client.request_code(phone)
        code = input("Код из SMS: ").strip()
        await client.check_code(sms_token, code)
    except MaxAuthError as exc:
        print(f"Не вышло: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка соединения с MAX: {exc}")
        return 1
    finally:
        await client.close()

    print(f"\nГотово. Сессия сохранена: {config.session_path}")
    print("Запускай мост: python -m maxbridge")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(login_flow()))
