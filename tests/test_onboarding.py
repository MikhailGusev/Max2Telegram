"""Онбординг клиентов в боте: гейтинг приёма и маршрутизация /start.

Сетевую часть (QR, опрос, вход) здесь не гоняем — подменяем _begin_onboarding
рекордером. Проверяем решения: кого пускаем, кому отказываем и что видит
владелец против нового клиента.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.accounts import build_accounts  # noqa: E402
from maxbridge.clients import STATUS_ACTIVE, ClientStore  # noqa: E402
from maxbridge.config import load_config  # noqa: E402
from maxbridge.core.ai import AiAssistant  # noqa: E402
from maxbridge.core.licensing import load_license  # noqa: E402
from maxbridge.core.transcribe import Transcriber  # noqa: E402
from maxbridge.telegram.bridge import TelegramBridge  # noqa: E402


class FakeMessage:
    def __init__(self, user_id: int, *, chat_type: str = "private") -> None:
        self.from_user = SimpleNamespace(id=user_id)
        # в личке chat.id == user.id
        self.chat = SimpleNamespace(id=user_id, type=chat_type)
        self.text = "/start"
        self.message = None  # чтобы _account_for пошёл по ветке owned_by
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


def make_bridge(tmp_path: Path, *, onboarding: bool, max_clients: int = 0):
    base = load_config(tmp_path / "нет.env")
    base.db_path = tmp_path / "data" / "maxbridge.db"
    base.max_mode = "botapi"
    base.max_bot_token = "dummy"
    base.owner_id = 111
    base.ai_enabled = False
    base.onboarding_enabled = onboarding
    base.max_clients = max_clients
    base.__post_init__()

    registry = build_accounts(base, AiAssistant(""), load_license(""))

    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge.config = base
    bridge.accounts = registry
    bridge.ai = AiAssistant("")
    bridge.transcriber = Transcriber()
    bridge.billing = None
    bridge._onboarding = {}
    bridge.clients = ClientStore(tmp_path / "data" / "clients.json")
    bridge.spin_up = lambda uid: None
    bridge.bot = None
    return bridge


def test_qr_png_is_valid_png(tmp_path: Path) -> None:
    png = TelegramBridge._qr_png("max://qr/example")
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "должен быть настоящий PNG (без Pillow)"
    assert len(png) > 50


def test_onboarding_closed_when_flag_off(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, onboarding=False)
    ok, why = bridge._onboarding_open()
    assert ok is False and "закрыта" in why


def test_onboarding_open_when_enabled(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, onboarding=True)
    ok, _ = bridge._onboarding_open()
    assert ok is True


def test_onboarding_closed_without_store(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, onboarding=True)
    bridge.clients = None  # приложение не привязало онбординг
    ok, _ = bridge._onboarding_open()
    assert ok is False


def test_onboarding_capacity_reached(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, onboarding=True, max_clients=1)
    bridge.clients.start_onboarding(500, "s.json")
    bridge.clients.set_status(500, STATUS_ACTIVE)  # один активный, лимит 1
    ok, why = bridge._onboarding_open()
    assert ok is False and "заполнен" in why


async def test_start_owner_gets_greeting_not_onboarding(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, onboarding=True)
    called = []
    bridge._begin_onboarding = lambda m: called.append(m)  # не должно вызваться

    msg = FakeMessage(user_id=111)  # владелец
    await bridge._cmd_start(msg)

    assert called == []
    assert msg.answers and "MaxBridge на связи" in msg.answers[0]


async def test_start_new_user_begins_onboarding(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, onboarding=True)

    async def fake_begin(message):
        message.answers.append("BEGIN")

    bridge._begin_onboarding = fake_begin
    msg = FakeMessage(user_id=999)  # чужой пользователь
    await bridge._cmd_start(msg)

    assert msg.answers == ["BEGIN"]


async def test_start_new_user_refused_when_closed(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, onboarding=False)
    called = []
    bridge._begin_onboarding = lambda m: called.append(m)

    msg = FakeMessage(user_id=999)
    await bridge._cmd_start(msg)

    assert called == []
    assert msg.answers and "закрыта" in msg.answers[0]


async def test_start_new_user_ignored_in_group(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, onboarding=True)
    called = []
    bridge._begin_onboarding = lambda m: called.append(m)

    msg = FakeMessage(user_id=999, chat_type="supergroup")
    await bridge._cmd_start(msg)

    assert called == [] and msg.answers == [], "в группе чужого молча игнорируем"
