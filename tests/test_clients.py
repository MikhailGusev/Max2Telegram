"""SaaS-клиенты: персистентный реестр и рантайм-подъём аккаунта.

Реестр (clients.json) проверяем целиком. Подъём аккаунта — через Application,
собранное __new__ с минимальной обвязкой, чтобы не поднимать реальный aiogram
и приватный биллинг.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.accounts import AccountRegistry  # noqa: E402
from maxbridge.app import Application  # noqa: E402
from maxbridge.clients import (  # noqa: E402
    STATUS_ACTIVE,
    STATUS_ONBOARDING,
    ClientRecord,
    ClientStore,
    client_name,
)
from maxbridge.config import load_config  # noqa: E402
from maxbridge.core.ai import AiAssistant  # noqa: E402
from maxbridge.core.licensing import load_license  # noqa: E402


# --------------------------------------------------------------- ClientStore
def test_store_persists_across_reload(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    store = ClientStore(path)
    store.start_onboarding(777, session_file="client-777_session.json")
    store.set_status(777, STATUS_ACTIVE)

    again = ClientStore(path)  # перечитали с диска
    record = again.get(777)
    assert record is not None
    assert record.name == "client-777"
    assert record.status == STATUS_ACTIVE
    assert again.count_active() == 1


def test_store_active_filters_onboarding(tmp_path: Path) -> None:
    store = ClientStore(tmp_path / "clients.json")
    store.start_onboarding(1, "s1.json")  # остаётся onboarding
    store.start_onboarding(2, "s2.json")
    store.set_status(2, STATUS_ACTIVE)

    active_ids = {r.tg_user_id for r in store.active()}
    assert active_ids == {2}


def test_store_remove(tmp_path: Path) -> None:
    store = ClientStore(tmp_path / "clients.json")
    store.start_onboarding(5, "s.json")
    assert store.remove(5) is True
    assert store.get(5) is None
    assert store.remove(5) is False


def test_store_ignores_broken_file(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    path.write_text("{ не json", encoding="utf-8")
    store = ClientStore(path)  # не должно падать
    assert store.all() == []


def test_store_atomic_write_is_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    store = ClientStore(path)
    store.upsert(ClientRecord(tg_user_id=9, name=client_name(9), status=STATUS_ONBOARDING))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data[0]["tg_user_id"] == 9


# ----------------------------------------------------- Application.spin_up
class FakeTelegram:
    def __init__(self) -> None:
        self.attached: list = []

    def attach_account(self, account) -> None:
        self.attached.append(account)


def make_app(tmp_path: Path) -> Application:
    base = load_config(tmp_path / "нет.env")
    base.db_path = tmp_path / "data" / "maxbridge.db"
    base.max_mode = "botapi"
    base.max_bot_token = "dummy"
    base.owner_id = 111
    base.__post_init__()

    app = Application.__new__(Application)
    app.config = base
    app.ai = AiAssistant("")
    app.billing = None
    app.channels = []
    app.license = load_license("")
    app.accounts = AccountRegistry([])
    app.telegram = FakeTelegram()
    app._tasks = None
    app._wake = asyncio.Event()
    return app


def test_client_account_is_isolated_and_flat(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    account = app.spin_up_client(777, launch=False)

    assert account.name == "client-777"
    assert account.owner_id == 777
    assert account.forum_chat_id == 0, "клиент всегда в плоском режиме"
    assert account.config.max_mode == "userbot", "клиент вошёл по QR — userbot"
    assert account.config.db_path.name == "client-777.db"
    assert account.config.session_path.name == "client-777_session.json"
    # зарегистрирован в реестре и подключён к боту
    assert app.accounts.by_name("client-777") is account
    assert app.telegram.attached == [account]


def test_spin_up_is_idempotent(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    first = app.spin_up_client(777, launch=False)
    second = app.spin_up_client(777, launch=False)
    assert first is second
    assert len(app.accounts) == 1


def test_two_clients_do_not_share_storage(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    a = app.spin_up_client(1, launch=False)
    b = app.spin_up_client(2, launch=False)
    assert a.config.db_path != b.config.db_path
    assert a.config.session_path != b.config.session_path
