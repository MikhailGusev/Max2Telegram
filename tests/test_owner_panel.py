"""Owner-панель SaaS: тумблер регистрации, список клиентов, серверный гейт.

Главное — команды владельца сервера недоступны клиентам (у клиента тоже
is_owner==True для своего аккаунта, но он не владелец установки).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.accounts import build_accounts  # noqa: E402
from maxbridge.clients import ClientStore  # noqa: E402
from maxbridge.config import load_config  # noqa: E402
from maxbridge.core.ai import AiAssistant  # noqa: E402
from maxbridge.core.licensing import load_license  # noqa: E402
from maxbridge.core.transcribe import Transcriber  # noqa: E402
from maxbridge.telegram.bridge import TelegramBridge  # noqa: E402


class FakeMessage:
    def __init__(self, user_id: int, text: str) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.text = text
        self.message = None
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


def make_bridge(tmp_path: Path, *, onboarding: bool = False, billing=None):
    base = load_config(tmp_path / "нет.env")
    base.db_path = tmp_path / "data" / "maxbridge.db"
    base.max_mode = "botapi"
    base.max_bot_token = "dummy"
    base.owner_id = 111
    base.ai_enabled = False
    base.onboarding_enabled = onboarding
    base.__post_init__()

    registry = build_accounts(base, AiAssistant(""), load_license(""))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge.config = base
    bridge.accounts = registry
    bridge.ai = AiAssistant("")
    bridge.transcriber = Transcriber()
    bridge.billing = billing
    bridge._onboarding = {}
    bridge.clients = ClientStore(tmp_path / "data" / "clients.json")
    bridge.spin_up = lambda uid: None
    bridge.bot = None
    return bridge


def test_is_server_owner(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    assert bridge._is_server_owner(111) is True
    assert bridge._is_server_owner(999) is False
    assert bridge._is_server_owner(None) is False


def test_registration_override_beats_env(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, onboarding=False)
    assert bridge._registration_enabled() is False
    bridge._owner_storage().set("onboarding_enabled", "1")
    assert bridge._registration_enabled() is True


async def test_registration_toggle_persists(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, onboarding=False)

    await bridge._cmd_registration(FakeMessage(111, "/registration on"))
    assert bridge._registration_enabled() is True
    assert bridge._onboarding_open()[0] is True

    await bridge._cmd_registration(FakeMessage(111, "/registration off"))
    assert bridge._registration_enabled() is False


async def test_registration_ignored_for_non_server_owner(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, onboarding=False)
    msg = FakeMessage(999, "/registration on")
    await bridge._cmd_registration(msg)
    assert msg.answers == []  # молча игнорируем чужого
    assert bridge._registration_enabled() is False


async def test_clients_lists_for_server_owner(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    msg = FakeMessage(111, "/clients")
    await bridge._cmd_clients(msg)
    assert msg.answers and "Активных: 0" in msg.answers[0]


async def test_clients_ignored_for_client_user(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    # клиент — владелец своего аккаунта, но не сервера
    bridge.accounts.accounts.append(SimpleNamespace(owner_id=999, name="client-999"))
    msg = FakeMessage(999, "/clients")
    await bridge._cmd_clients(msg)
    assert msg.answers == [], "клиент не должен видеть чужих клиентов"


class DummyBilling:
    def is_premium(self, user_id: int) -> bool:
        return False

    def grant_premium(self, *a, **k):  # не должно вызваться
        raise AssertionError("клиент не может выдавать Premium")


async def test_grant_blocked_for_client(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, billing=DummyBilling())
    # клиент проходит _guard (владелец своего аккаунта), но не серверный владелец
    bridge.accounts.accounts.append(SimpleNamespace(owner_id=999, name="client-999"))
    msg = FakeMessage(999, "/grant 555 30")
    await bridge._cmd_grant(msg)
    assert msg.answers and "владельца сервера" in msg.answers[0]
