"""Разовый вход в аккаунт MAX по SMS-коду.

    python -m maxbridge login                 один аккаунт из .env
    python -m maxbridge login --account work  конкретный аккаунт из accounts.json
    python -m maxbridge login --list          показать, у каких аккаунтов нет сессии

Создаёт параллельную WEB-сессию: телефон продолжает работать как раньше.
Токен ложится рядом с базой (в .gitignore), храни его как пароль.
Отзыв: в приложении MAX завершить сессию «MaxBridge».
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ..accounts import ACCOUNTS_FILE, account_config, read_specs
from ..config import Config, load_config
from ..logging_setup import setup_logging
from ..maxproto import MaxAuthError, MaxWSClient


def _configs(base: Config) -> dict[str, Config]:
    """Все аккаунты установки: из accounts.json либо один из .env."""
    path = base.db_path.parent.parent / ACCOUNTS_FILE
    specs = read_specs(path) if path.exists() else []
    if not specs:
        return {"default": base}

    result: dict[str, Config] = {}
    for index, spec in enumerate(specs, start=1):
        name = str(spec.get("name") or f"account-{index}").strip()
        result[name] = account_config(base, spec, name)
    return result


def _print_list(configs: dict[str, Config]) -> None:
    print("Аккаунты этой установки:\n")
    for name, config in configs.items():
        mark = "✓ сессия есть" if config.session_path.exists() else "✗ сессии нет"
        phone = config.max_phone or "телефон не задан"
        print(f"  {name:<16} {mark:<16} {phone}")
    print("\nВойти:  python -m maxbridge login --account <имя>")


async def login_flow(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="maxbridge login", description="Вход в аккаунт MAX")
    parser.add_argument("--account", help="имя аккаунта из accounts.json")
    parser.add_argument("--list", action="store_true", help="показать аккаунты и их сессии")
    args = parser.parse_args(argv if argv is not None else sys.argv[2:])

    base = load_config()
    setup_logging(base.log_level)
    configs = _configs(base)

    if args.list:
        _print_list(configs)
        return 0

    if args.account:
        config = configs.get(args.account)
        if config is None:
            print(f"Нет аккаунта «{args.account}». Есть: {', '.join(configs)}")
            return 1
        name = args.account
    elif len(configs) == 1:
        name, config = next(iter(configs.items()))
    else:
        print(f"Аккаунтов несколько: {', '.join(configs)}")
        print("Укажи нужный: python -m maxbridge login --account <имя>")
        return 1

    phone = config.max_phone or input(f"Телефон MAX для «{name}» (+79990000000): ").strip()
    if not phone:
        print("Номер не введён — прерываю.")
        return 2

    client = MaxWSClient(config.session_path)
    if client.has_session:
        answer = input(
            f"У «{name}» уже есть сессия ({config.session_path}). Перелогиниться? [y/N]: "
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

    print(f"\nГотово. Сессия «{name}» сохранена: {config.session_path}")
    missing = [n for n, c in configs.items() if not c.session_path.exists()]
    if missing:
        print(f"Осталось войти: {', '.join(missing)}")
    else:
        print("Запускай мост: python -m maxbridge")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(login_flow()))
