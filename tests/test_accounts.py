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


def test_write_finds_chat_by_name() -> None:
    """/write ищет чат по подстроке имени среди чатов и контактов."""
    from maxbridge.transports.userbot import UserbotTransport

    transport = UserbotTransport("не-важно.json")
    transport.client.me_id = 100
    transport._absorb_directory(
        {
            "contacts": [
                {"id": 200, "names": [{"name": "Пётр Иванов"}]},
                {"id": 201, "names": [{"name": "Мария Пётрова"}]},
            ],
            "chats": [{"id": -5, "type": "CHAT", "title": "Отдел продаж"}],
        }
    )

    assert transport.find_chats("продаж") == [(-5, "Отдел продаж")]
    # по контакту, у которого нет отдельного чата в _titles
    assert (201, "Мария Пётрова") in transport.find_chats("мария")


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
            "chats": [],
        }
    )
    matches = transport.find_chats("пётр")
    assert len(matches) == 2, "оба Петра — команда должна попросить уточнить"


def test_write_empty_query_finds_nothing() -> None:
    from maxbridge.transports.userbot import UserbotTransport

    transport = UserbotTransport("не-важно.json")
    assert transport.find_chats("") == []
