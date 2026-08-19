"""Разовый вход в аккаунт MAX по SMS-коду.

    python -m maxbridge login                 один аккаунт из .env
    python -m maxbridge login --token         вход готовым токеном из браузера
    python -m maxbridge login --verify        проверить, жива ли сессия
    python -m maxbridge login --account work  конкретный аккаунт из accounts.json
    python -m maxbridge login --list          показать, у каких аккаунтов нет сессии

Создаёт параллельную WEB-сессию: телефон продолжает работать как раньше.
Токен ложится рядом с базой (в .gitignore), храни его как пароль.
Отзыв: в приложении MAX завершить сессию «MaxBridge».
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from getpass import getpass

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


async def _login_by_token(name: str, config: Config) -> int:
    """Вход готовым токеном из веб-клиента.

    Нужен, когда MAX требует капчу на запрос SMS-кода (opcode 17 отвечает
    captcha.validation-failed). Капчу решает человек в браузере, мост получает
    уже выданный токен и дальше живёт как обычно.
    """
    print(f"Вход по токену для «{name}».\n")
    print("Где взять: залогинься на https://web.max.ru, открой консоль браузера")
    print("(F12 -> Console) и посмотри localStorage — подробности в docs/LOGIN.md\n")

    token = getpass("Токен (ввод скрыт): ").strip()
    if not token:
        print("Пусто — прерываю.")
        return 2
    device_id = input("deviceId из браузера: ").strip()
    if not device_id:
        print("deviceId обязателен: токен выдан конкретному устройству.")
        return 2

    config.session_path.parent.mkdir(parents=True, exist_ok=True)
    config.session_path.write_text(
        json.dumps({"device_id": device_id, "token": token}, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        config.session_path.chmod(0o600)
    except OSError:
        pass

    print("\nПроверяю токен…")
    client = MaxWSClient(config.session_path)
    try:
        payload = await client.login()
    except Exception as exc:  # noqa: BLE001
        print(f"Токен не подошёл: {exc}")
        print("Сессия сохранена, но войти не удалось — проверь токен и deviceId.")
        return 1
    finally:
        await client.close()

    contact = (payload.get("profile") or {}).get("contact") or {}
    who = contact.get("phone") or contact.get("name") or "аккаунт"
    chats = len(payload.get("chats") or [])
    print(f"\nГотово: вошёл как {who}, чатов синхронизировано {chats}")
    print(f"Сессия: {config.session_path}")
    return 0


async def login_flow(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="maxbridge login", description="Вход в аккаунт MAX")
    parser.add_argument("--account", help="имя аккаунта из accounts.json")
    parser.add_argument("--list", action="store_true", help="показать аккаунты и их сессии")
    parser.add_argument(
        "--token",
        action="store_true",
        help="вход готовым токеном из веб-клиента (когда MAX требует капчу)",
    )
    parser.add_argument(
        "--verify", action="store_true", help="проверить, что сохранённая сессия жива"
    )
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

    if args.verify:
        client = MaxWSClient(config.session_path)
        if not client.has_session:
            print(f"У «{name}» сессии нет: {config.session_path}")
            return 1
        try:
            payload = await client.login()
        except Exception as exc:  # noqa: BLE001
            print(f"Сессия «{name}» не работает: {exc}")
            return 1
        finally:
            await client.close()
        contact = (payload.get("profile") or {}).get("contact") or {}
        print(f"Сессия «{name}» жива: {contact.get('phone') or 'аккаунт'}, "
              f"чатов {len(payload.get('chats') or [])}")
        return 0

    if args.token:
        return await _login_by_token(name, config)

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
        if "captcha" in str(exc).lower():
            print("\nMAX требует капчу на запрос SMS-кода — из консоли её не пройти.")
            print("Обходной путь: залогинься в браузере на https://web.max.ru,")
            print("забери оттуда токен и отдай его мосту:")
            print(f"\n    python -m maxbridge login --token{f' --account {name}' if len(configs) > 1 else ''}")
            print("\nКак достать токен — docs/LOGIN.md")
            return 1
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
