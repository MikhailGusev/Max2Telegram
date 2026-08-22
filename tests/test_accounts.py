"""Мультиаккаунт: изоляция данных, гейт по лицензии, поиск нужного аккаунта."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.accounts import build_accounts  # noqa: E402
from maxbridge.config import load_config  # noqa: E402
from maxbridge.core.ai import AiAssistant  # noqa: E402
from maxbridge.core.licensing import COMMUNITY_FEATURES, License, load_license  # noqa: E402
from maxbridge.models import MaxMessage  # noqa: E402


@pytest.fixture()
def base(tmp_path: Path):
    config = load_config(tmp_path / "нет.env")
    config.db_path = tmp_path / "data" / "maxbridge.db"
    config.max_mode = "botapi"
    config.max_bot_token = "dummy"
    config.owner_id = 111
    config.__post_init__()
    return config


def team_license() -> License:
    return License(
        plan="team",
        customer="Тест",
        features=frozenset(COMMUNITY_FEATURES | {"multiaccount", "sms", "whatsapp"}),
        seats=5,
    )


def write_accounts(tmp_path: Path, specs: list[dict]) -> None:
    (tmp_path / "accounts.json").write_text(
        json.dumps(specs, ensure_ascii=False), encoding="utf-8"
    )


def test_without_file_single_account_from_env(base) -> None:
    registry = build_accounts(base, AiAssistant(""), load_license(""))
    assert len(registry) == 1
    assert registry.accounts[0].name == "default"
    assert not registry.multi


def test_multiaccount_requires_team_license(base, tmp_path: Path) -> None:
    write_accounts(
        tmp_path,
        [
            {"name": "work", "owner_id": 111, "forum_chat_id": -100_1},
            {"name": "personal", "owner_id": 111, "forum_chat_id": -100_2},
        ],
    )
    registry = build_accounts(base, AiAssistant(""), load_license(""))
    assert len(registry) == 1, "без лицензии Team поднимается только первый аккаунт"
    assert registry.accounts[0].name == "work"


def test_team_license_raises_all_accounts(base, tmp_path: Path) -> None:
    write_accounts(
        tmp_path,
        [
            {"name": "work", "owner_id": 111, "forum_chat_id": -100_1},
            {"name": "personal", "owner_id": 222, "forum_chat_id": -100_2},
        ],
    )
    registry = build_accounts(base, AiAssistant(""), team_license())
    assert len(registry) == 2 and registry.multi


def test_accounts_do_not_share_storage_or_session(base, tmp_path: Path) -> None:
    write_accounts(
        tmp_path,
        [
            {"name": "work", "owner_id": 111, "forum_chat_id": -100_1},
            {"name": "personal", "owner_id": 111, "forum_chat_id": -100_2},
        ],
    )
    registry = build_accounts(base, AiAssistant(""), team_license())
    work, personal = registry.accounts

    assert work.config.db_path != personal.config.db_path
    assert work.config.session_path != personal.config.session_path

    work.storage.upsert_chat(1, "Клиент по работе")
    work.storage.save_message(MaxMessage(chat_id=1, message_id="a", text="смета"))

    assert personal.storage.stats()["messages"] == 0
    assert personal.storage.search("смета") == []
    assert work.storage.search("смета")


def test_registry_lookups(base, tmp_path: Path) -> None:
    write_accounts(
        tmp_path,
        [
            {"name": "work", "owner_id": 111, "forum_chat_id": -1001},
            {"name": "personal", "owner_id": 222, "forum_chat_id": -1002},
        ],
    )
    registry = build_accounts(base, AiAssistant(""), team_license())

    assert registry.by_forum(-1001).name == "work"
    assert registry.by_forum(-9999) is None
    assert registry.by_name("PERSONAL").name == "personal"
    assert [a.name for a in registry.owned_by(111)] == ["work"]
    assert registry.is_owner(222) and not registry.is_owner(333)


def test_rebind_moves_group(base, tmp_path: Path) -> None:
    write_accounts(tmp_path, [{"name": "work", "owner_id": 111, "forum_chat_id": -1001}])
    registry = build_accounts(base, AiAssistant(""), team_license())
    account = registry.accounts[0]

    registry.rebind(account, -2002)

    assert registry.by_forum(-2002) is account
    assert registry.by_forum(-1001) is None
    assert account.forum_chat_id == -2002


def test_per_account_overrides_apply(base, tmp_path: Path) -> None:
    base.stealth_mode = True
    write_accounts(
        tmp_path,
        [
            {"name": "work", "owner_id": 111, "stealth_mode": False},
            {"name": "personal", "owner_id": 111},
        ],
    )
    registry = build_accounts(base, AiAssistant(""), team_license())
    work, personal = registry.accounts

    assert work.config.stealth_mode is False
    assert personal.config.stealth_mode is True, "остальное наследуется из .env"


def test_unknown_field_is_ignored(base, tmp_path: Path) -> None:
    write_accounts(
        tmp_path,
        [{"name": "work", "owner_id": 111, "telegram_token": "чужой-токен"}],
    )
    registry = build_accounts(base, AiAssistant(""), team_license())
    # токен бота подменить через accounts.json нельзя — он общий для установки
    assert registry.accounts[0].config.telegram_token == base.telegram_token


def test_duplicate_names_are_dropped(base, tmp_path: Path) -> None:
    write_accounts(
        tmp_path,
        [
            {"name": "work", "owner_id": 111, "forum_chat_id": -1001},
            {"name": "work", "owner_id": 111, "forum_chat_id": -1002},
        ],
    )
    registry = build_accounts(base, AiAssistant(""), team_license())
    assert len(registry) == 1


def test_broken_file_falls_back_to_env(base, tmp_path: Path) -> None:
    (tmp_path / "accounts.json").write_text("{ это не json", encoding="utf-8")
    registry = build_accounts(base, AiAssistant(""), team_license())
    assert len(registry) == 1 and registry.accounts[0].name == "default"


def test_dialog_title_comes_from_the_other_participant() -> None:
    """98 из 100 чатов MAX — личные диалоги вообще без поля title."""
    from maxbridge.transports.userbot import UserbotTransport

    transport = UserbotTransport("не-важно.json")
    transport.client.me_id = 100
    transport._absorb_directory(
        {
            "contacts": [{"id": 200, "names": [{"name": "Пётр Иванов"}]}],
            "chats": [
                {"id": -1, "type": "DIALOG", "participants": {"100": 1, "200": 1}},
                {"id": -2, "type": "CHAT", "title": "Рабочая группа"},
                {"id": -3, "type": "DIALOG", "participants": {"100": 1, "999": 1}},
            ],
        }
    )

    assert transport.chat_title(-1) == "Пётр Иванов"
    assert transport.chat_title(-2) == "Рабочая группа"
    assert transport.chat_title(-3) == "", "имени нет в контактах — узнаем из письма"
    assert transport._kinds[-1] == "dialog" and transport._kinds[-2] == "chat"


def test_own_id_is_not_taken_as_dialog_title() -> None:
    from maxbridge.transports.userbot import UserbotTransport

    transport = UserbotTransport("не-важно.json")
    transport.client.me_id = 100
    transport._absorb_directory(
        {
            "contacts": [{"id": 100, "names": [{"name": "Я сам"}]}],
            "chats": [{"id": -1, "type": "DIALOG", "participants": {"100": 1, "200": 1}}],
        }
    )
    assert transport.chat_title(-1) != "Я сам"


def test_write_returns_dialog_id_not_contact_id() -> None:
    """/write должен вернуть id ДИАЛОГА, а не id контакта — иначе not.found."""
    from maxbridge.transports.userbot import UserbotTransport

    transport = UserbotTransport("не-важно.json")
    transport.client.me_id = 100
    transport._absorb_directory(
        {
            "contacts": [{"id": 200, "names": [{"name": "Манечка"}]}],
            "chats": [
                {"id": -5, "type": "CHAT", "title": "Отдел продаж"},
                # личный диалог с контактом 200: его id (-68…) ≠ id контакта 200
                {"id": -68093, "type": "DIALOG", "participants": {"100": 1, "200": 1}},
            ],
        }
    )

    assert transport.find_chats("продаж") == [(-5, "Отдел продаж")]
    # ищем по имени — получаем id диалога, а не 200
    assert transport.find_chats("манечка") == [(-68093, "Манечка")]


def test_write_skips_contact_without_dialog() -> None:
    """Контакт без синхронизированного диалога писать нельзя — не предлагаем."""
    from maxbridge.transports.userbot import UserbotTransport

    transport = UserbotTransport("не-важно.json")
    transport.client.me_id = 100
    transport._absorb_directory(
        {
            "contacts": [{"id": 201, "names": [{"name": "Незнакомец"}]}],
            "chats": [],  # диалога с ним нет
        }
    )
    assert transport.find_chats("незнакомец") == []


def test_write_returns_all_matches_for_ambiguous_query() -> None:
    from maxbridge.transports.userbot import UserbotTransport

    transport = UserbotTransport("не-важно.json")
    transport.client.me_id = 100
    transport._absorb_directory(
        {
            "contacts": [
                {"id": 200, "names": [{"name": "Пётр Иванов"}]},
                {"id": 201, "names": [{"name": "Пётр Сидоров"}]},
            ],
            "chats": [
                {"id": -301, "type": "DIALOG", "participants": {"100": 1, "200": 1}},
                {"id": -302, "type": "DIALOG", "participants": {"100": 1, "201": 1}},
            ],
        }
    )
    matches = transport.find_chats("пётр")
    assert len(matches) == 2, "оба Петра — команда должна попросить уточнить"
    assert {cid for cid, _ in matches} == {-301, -302}, "id диалогов, не контактов"


def test_write_empty_query_finds_nothing() -> None:
    from maxbridge.transports.userbot import UserbotTransport

    transport = UserbotTransport("не-важно.json")
    assert transport.find_chats("") == []


def test_command_arg_survives_mobile_nbsp() -> None:
    """Мобильный Telegram вставляет неразрывный пробел — /write Манечка ломался."""
    from maxbridge.telegram.bridge import _command_arg

    assert _command_arg("/write Манечка") == "Манечка"
    assert _command_arg("/write\u00a0Манечка") == "Манечка"      # U+00A0
    assert _command_arg("/write@Max2TelegramBridge_bot Манечка") == "Манечка"
    assert _command_arg("/rule счёт|urgent") == "счёт|urgent"
    assert _command_arg("/find\tдоговор") == "договор"
    assert _command_arg("/write") == ""
    assert _command_arg("") == ""
    assert _command_arg(None) == ""


def test_bind_survives_env_placeholder(base, tmp_path: Path) -> None:
    """Сохранённый /bind должен побеждать заглушку forum_chat_id из .env.

    Иначе после рестарта под systemd доставка уходит в несуществующую группу.
    """
    base.forum_chat_id = -1001234567890  # фейк из шаблона .env
    registry = build_accounts(base, AiAssistant(""), load_license(""))
    account = registry.accounts[0]
    # пользователь привязал реальную группу командой /bind (сохранилось в БД)
    account.storage.set("forum_chat_id", "-1009999999999")

    # эмуляция восстановления привязки на старте (логика из TelegramBridge.run)
    stored = account.storage.get("forum_chat_id")
    if stored and int(stored) != account.forum_chat_id:
        registry.rebind(account, int(stored))

    assert account.forum_chat_id == -1009999999999
    assert registry.by_forum(-1009999999999) is account
    assert registry.by_forum(-1001234567890) is None
