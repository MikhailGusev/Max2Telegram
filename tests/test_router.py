"""Роутер: маршрутизация в обе стороны и ответы из внешних каналов."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.config import load_config  # noqa: E402
from maxbridge.core.ai import AiAssistant  # noqa: E402
from maxbridge.core.router import Router  # noqa: E402
from maxbridge.core.rules import Verdict  # noqa: E402
from maxbridge.db import Storage  # noqa: E402
from maxbridge.models import MaxMessage  # noqa: E402
from maxbridge.transports.base import MaxTransport  # noqa: E402


class FakeTransport(MaxTransport):
    """Транспорт-заглушка: запоминает, что и куда отправили."""

    name = "fake"
    sees_everything = True
    supports_media = True

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[int, str, str]] = []
        self.read: list[tuple[int, str]] = []
        self.media: list[tuple[int, int, str, str]] = []

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, chat_id: int, text: str, *, reply_to: str = "") -> str:
        self.sent.append((chat_id, text, reply_to))
        return f"sent-{len(self.sent)}"

    async def send_media(
        self,
        chat_id: int,
        data: bytes,
        *,
        filename: str,
        kind: str = "file",
        caption: str = "",
        reply_to: str = "",
    ) -> str:
        self.media.append((chat_id, len(data), filename, kind))
        return f"media-{len(self.media)}"

    async def mark_read(self, chat_id: int, message_id: str) -> None:
        self.read.append((chat_id, message_id))


class FakeSink:
    def __init__(self) -> None:
        self.delivered: list[tuple[MaxMessage, Verdict]] = []

    async def deliver(self, message: MaxMessage, verdict: Verdict) -> int | None:
        self.delivered.append((message, verdict))
        return 100 + len(self.delivered)


@pytest.fixture()
def router(tmp_path: Path) -> Router:
    config = load_config(tmp_path / "нет.env")
    config.db_path = tmp_path / "t.db"
    config.stealth_mode = True
    storage = Storage(config.db_path)
    transport = FakeTransport()
    router = Router(config, storage, transport, AiAssistant(""))
    router.attach_telegram(FakeSink())
    yield router
    storage.close()


def incoming(text: str, chat_id: int = 7, msg_id: str = "m1") -> MaxMessage:
    return MaxMessage(chat_id=chat_id, message_id=msg_id, text=text, sender_name="Оля")


@pytest.mark.asyncio
async def test_stealth_mode_does_not_mark_read(router: Router) -> None:
    await router.handle_max_message(incoming("Привет, как дела?"))
    assert router.transport.read == []


@pytest.mark.asyncio
async def test_disabled_stealth_marks_read(router: Router) -> None:
    router.config.stealth_mode = False
    await router.handle_max_message(incoming("Привет!"))
    assert router.transport.read == [(7, "m1")]


@pytest.mark.asyncio
async def test_own_outgoing_message_closes_pending(router: Router) -> None:
    await router.handle_max_message(incoming("Когда пришлёшь отчёт?"))
    assert router.storage.stats()["pending"] == 1

    own = incoming("уже отправил", msg_id="m2")
    own.outgoing = True
    await router.handle_max_message(own)
    assert router.storage.stats()["pending"] == 0
    assert len(router.telegram.delivered) == 1  # своё в Telegram не дублируем


@pytest.mark.asyncio
async def test_autoreply_goes_out(router: Router) -> None:
    router.storage.upsert_chat(7, "Клиент")
    router.storage.add_rule("отпуск", "autoreply", "Я в отпуске до 25-го")
    await router.handle_max_message(incoming("Ты в отпуске?"))
    assert router.transport.sent == [(7, "Я в отпуске до 25-го", "m1")]


@pytest.mark.asyncio
async def test_external_reply_uses_last_escalated_chat(router: Router) -> None:
    router.storage.upsert_chat(42, "Поставщик")
    router.storage.set("last_escalated_chat", "42")

    result = await router.handle_external_reply("сейчас пришлю", sender="79990001122")

    assert router.transport.sent == [(42, "сейчас пришлю", "")]
    assert "Поставщик" in result


@pytest.mark.asyncio
async def test_external_reply_with_explicit_chat(router: Router) -> None:
    router.storage.upsert_chat(99, "Юристы")
    router.storage.set("last_escalated_chat", "42")

    await router.handle_external_reply("#99 договор согласован")

    assert router.transport.sent == [(99, "договор согласован", "")]


@pytest.mark.asyncio
async def test_external_reply_without_context_is_refused(router: Router) -> None:
    result = await router.handle_external_reply("привет")
    assert router.transport.sent == []
    assert "#" in result


@pytest.mark.asyncio
async def test_sending_to_max_marks_chat_answered(router: Router) -> None:
    await router.handle_max_message(incoming("Вопрос?"))
    assert router.storage.stats()["pending"] == 1
    await router.send_to_max(7, "Ответ")
    assert router.storage.stats()["pending"] == 0


@pytest.mark.asyncio
async def test_media_is_forwarded_and_logged(router: Router) -> None:
    await router.handle_max_message(incoming("Пришли смету?"))
    assert router.storage.stats()["pending"] == 1

    await router.send_media_to_max(
        7, b"x" * 2048, filename="смета.pdf", kind="file", caption="держи"
    )

    assert router.transport.media == [(7, 2048, "смета.pdf", "file")]
    assert router.storage.stats()["pending"] == 0      # файл тоже закрывает вопрос
    assert router.storage.stats()["sent"] == 1
    assert router.storage.search("держи")              # подпись попала в поиск


@pytest.mark.asyncio
async def test_media_without_caption_still_searchable(router: Router) -> None:
    await router.send_media_to_max(7, b"data", filename="акт.docx", kind="file")
    found = router.storage.search("акт.docx")
    assert len(found) == 1
    assert found[0]["outgoing"] == 1
