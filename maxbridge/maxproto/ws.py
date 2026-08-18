"""Асинхронный клиент внутреннего WebSocket-API MAX (кодовое имя OneMe).

Что умеет:
  * логин по номеру телефона + SMS-код, сохранение сессии (device_id + токен);
  * вход по сохранённому токену и синхронизация списка чатов;
  * приём ВСЕХ входящих сообщений (личные, группы, каналы) — событие opcode 128;
  * отправку/редактирование сообщений от имени владельца аккаунта;
  * keepalive и автопереподключение с экспоненциальной паузой.

ВНИМАНИЕ: это неофициальный API. Он может измениться в любой момент, а его
использование может нарушать условия сервиса MAX. См. docs/LEGAL.md.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets
from websockets.asyncio.client import ClientConnection

from .opcodes import Op

log = logging.getLogger("maxbridge.maxproto")

WS_URL = "wss://ws-api.oneme.ru/websocket"
WEB_ORIGIN = "https://web.max.ru"
RPC_VERSION = 11
APP_VERSION = "26.2.2"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)

PacketHandler = Callable[[dict[str, Any]], Awaitable[None]]


class MaxProtocolError(RuntimeError):
    """Сервер вернул ошибку в payload или соединение недоступно."""


class MaxAuthError(MaxProtocolError):
    """Не удалось авторизоваться (неверный код, протухший токен)."""


class MaxWSClient:
    def __init__(
        self,
        session_path: str | Path,
        *,
        request_timeout: float = 30.0,
        keepalive_interval: float = 30.0,
    ) -> None:
        self.session_path = Path(session_path)
        self.request_timeout = request_timeout
        self.keepalive_interval = keepalive_interval

        self._conn: ClientConnection | None = None
        self._seq = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: list[PacketHandler] = []
        self._recv_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None

        self._device_id: str = ""
        self._token: str = ""
        self._logged_in = False
        self._closing = False
        self.profile: dict[str, Any] = {}
        self.me_id: int = 0

    # ------------------------------------------------------------- сессия
    @property
    def has_session(self) -> bool:
        return self.session_path.exists()

    def _load_session(self) -> bool:
        if not self.session_path.exists():
            return False
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("не смог прочитать сессию %s: %s", self.session_path, exc)
            return False
        self._device_id = data.get("device_id", "")
        self._token = data.get("token", "")
        return bool(self._device_id and self._token)

    def _save_session(self) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(
            json.dumps({"device_id": self._device_id, "token": self._token}, ensure_ascii=False),
            encoding="utf-8",
        )
        try:  # на POSIX прячем токен от посторонних; на Windows просто игнор
            self.session_path.chmod(0o600)
        except OSError:
            pass
        log.info("сессия MAX сохранена: %s", self.session_path)

    # --------------------------------------------------------- соединение
    def on_packet(self, handler: PacketHandler) -> None:
        """Регистрирует обработчик серверных событий (opcode 128 и прочие)."""
        self._handlers.append(handler)

    async def connect(self) -> None:
        if self._conn is not None:
            return
        log.info("подключаюсь к %s", WS_URL)
        self._conn = await websockets.connect(
            WS_URL,
            origin=websockets.Origin(WEB_ORIGIN),
            user_agent_header=USER_AGENT,
            open_timeout=20,
            ping_interval=None,  # у протокола свой keepalive (opcode 1)
        )
        self._recv_task = asyncio.create_task(self._recv_loop(), name="max-recv")

    async def close(self) -> None:
        self._closing = True
        for task in (self._keepalive_task, self._recv_task):
            if task and not task.done():
                task.cancel()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def invoke(self, opcode: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Отправляет запрос и ждёт ответ с тем же seq."""
        if self._conn is None:
            raise MaxProtocolError("нет соединения с MAX: сначала connect()")

        seq = next(self._seq)
        request = {"ver": RPC_VERSION, "cmd": 0, "seq": seq, "opcode": int(opcode), "payload": payload}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[seq] = future

        try:
            await self._conn.send(json.dumps(request, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=self.request_timeout)
        except asyncio.TimeoutError as exc:
            raise MaxProtocolError(f"таймаут ответа на opcode {opcode}") from exc
        finally:
            self._pending.pop(seq, None)

        body = response.get("payload") or {}
        if isinstance(body, dict) and body.get("error"):
            raise MaxProtocolError(f"MAX вернул ошибку на opcode {opcode}: {body['error']}")
        return response

    async def _recv_loop(self) -> None:
        assert self._conn is not None
        try:
            async for raw in self._conn:
                try:
                    packet = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("не смог разобрать пакет от MAX")
                    continue
                await self._dispatch(packet)
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed:
            log.warning("соединение с MAX закрыто")
        finally:
            self._conn = None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(MaxProtocolError("соединение закрыто"))
            self._pending.clear()

    async def _dispatch(self, packet: dict[str, Any]) -> None:
        seq = packet.get("seq")
        future = self._pending.get(seq) if isinstance(seq, int) else None
        if future is not None and not future.done():
            future.set_result(packet)
            return
        for handler in self._handlers:
            try:
                await handler(packet)
            except Exception:  # noqa: BLE001 - обработчик не должен ронять цикл
                log.exception("ошибка в обработчике пакета MAX")

    # ------------------------------------------------------------- keepalive
    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive_interval)
            try:
                await self.invoke(Op.PING, {"interactive": False})
            except asyncio.CancelledError:
                raise
            except MaxProtocolError as exc:
                log.warning("keepalive не прошёл: %s", exc)
                return

    def _start_keepalive(self) -> None:
        if self._keepalive_task and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="max-keepalive")

    # ----------------------------------------------------------- авторизация
    async def _hello(self, device_id: str | None = None) -> dict[str, Any]:
        self._device_id = device_id or self._device_id or str(uuid.uuid4())
        return await self.invoke(
            Op.HELLO,
            {
                "userAgent": {
                    "deviceType": "WEB",
                    "locale": "ru",
                    "deviceLocale": "ru",
                    "osVersion": "Linux",
                    "deviceName": "MaxBridge",
                    "headerUserAgent": USER_AGENT,
                    "appVersion": APP_VERSION,
                    "screen": "1080x1920 1.0x",
                    "timezone": "Europe/Moscow",
                },
                "deviceId": self._device_id,
            },
        )

    async def request_code(self, phone: str) -> str:
        """Запрашивает SMS-код. Возвращает временный токен для check_code()."""
        await self._hello()
        response = await self.invoke(
            Op.START_AUTH, {"phone": phone, "type": "START_AUTH", "language": "ru"}
        )
        token = (response.get("payload") or {}).get("token")
        if not token:
            raise MaxAuthError("MAX не выдал токен подтверждения — проверь номер телефона")
        return str(token)

    async def check_code(self, sms_token: str, code: str) -> dict[str, Any]:
        """Подтверждает SMS-код и сохраняет постоянную сессию на диск."""
        response = await self.invoke(
            Op.CHECK_CODE,
            {"token": sms_token, "verifyCode": str(code).strip(), "authTokenType": "CHECK_CODE"},
        )
        payload = response.get("payload") or {}
        try:
            self._token = payload["tokenAttrs"]["LOGIN"]["token"]
        except (KeyError, TypeError) as exc:
            raise MaxAuthError("в ответе MAX нет LOGIN-токена — код неверный или истёк") from exc

        self._remember_profile(payload)
        self._logged_in = True
        self._save_session()
        self._start_keepalive()
        return payload

    async def login(self) -> dict[str, Any]:
        """Вход по сохранённой сессии. Возвращает payload синхронизации (в нём чаты)."""
        if not self._load_session():
            raise MaxAuthError(
                "нет сохранённой сессии MAX. Выполни: python -m maxbridge login"
            )
        await self.connect()
        await self._hello(self._device_id)
        response = await self.invoke(
            Op.LOGIN_BY_TOKEN,
            {
                "interactive": True,
                "token": self._token,
                "chatsCount": 100,
                "chatsSync": 0,
                "contactsSync": 0,
                "presenceSync": -1,
                "draftsSync": 0,
            },
        )
        payload = response.get("payload") or {}
        self._remember_profile(payload)
        self._logged_in = True
        self._start_keepalive()
        log.info("вход в MAX выполнен (id=%s)", self.me_id or "?")
        return payload

    def _remember_profile(self, payload: dict[str, Any]) -> None:
        profile = payload.get("profile") or {}
        if profile:
            self.profile = profile
            contact = profile.get("contact") or {}
            self.me_id = int(contact.get("id") or profile.get("id") or 0)

    # ------------------------------------------------------- переподключение
    async def run_forever(self, *, on_ready: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> None:
        """Держит соединение живым: логин -> ожидание разрыва -> переподключение."""
        attempt = 0
        while not self._closing:
            try:
                payload = await self.login()
                attempt = 0
                if on_ready is not None:
                    await on_ready(payload)
                while self._conn is not None and not self._closing:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except MaxAuthError:
                raise  # чинить руками — переподключение не поможет
            except Exception as exc:  # noqa: BLE001
                log.warning("соединение с MAX потеряно: %s", exc)
            if self._closing:
                break
            attempt += 1
            delay = min(60.0, 2.0 ** min(attempt, 5)) + random.uniform(0, 2)
            log.info("переподключение к MAX через %.1f с (попытка %d)", delay, attempt)
            await asyncio.sleep(delay)

    # --------------------------------------------------------------- методы
    async def send_message(
        self, chat_id: int, text: str, *, reply_to: str | None = None, notify: bool = True
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "text": text,
            "cid": random.randint(1_750_000_000_000, 2_000_000_000_000),
            "elements": [],
            "attaches": [],
        }
        if reply_to:
            message["link"] = {"type": "REPLY", "messageId": str(reply_to)}
        return await self.invoke(
            Op.SEND_MESSAGE, {"chatId": int(chat_id), "message": message, "notify": notify}
        )

    async def edit_message(self, chat_id: int, message_id: str, text: str) -> dict[str, Any]:
        return await self.invoke(
            Op.EDIT_MESSAGE,
            {
                "chatId": int(chat_id),
                "messageId": str(message_id),
                "text": text,
                "elements": [],
                "attachments": [],
            },
        )

    async def delete_message(self, chat_id: int, message_id: str, for_me: bool = False) -> dict[str, Any]:
        return await self.invoke(
            Op.DELETE_MESSAGE,
            {"chatId": int(chat_id), "messageIds": [str(message_id)], "forMe": for_me},
        )

    async def mark_read(self, chat_id: int, message_id: str) -> dict[str, Any]:
        """Помечает сообщение прочитанным. В stealth-режиме мы это НЕ вызываем."""
        return await self.invoke(
            Op.CHAT_MARK,
            {
                "type": "READ_MESSAGE",
                "chatId": int(chat_id),
                "messageId": str(message_id),
                "mark": random.randint(1_750_000_000_000, 2_000_000_000_000),
            },
        )

    async def fetch_history(self, chat_id: int, *, count: int = 30) -> dict[str, Any]:
        response = await self.invoke(
            Op.CHAT_HISTORY,
            {
                "chatId": int(chat_id),
                "from": random.randint(1_750_000_000_000, 2_000_000_000_000),
                "forward": 0,
                "backward": int(count),
                "getMessages": True,
            },
        )
        return response.get("payload") or {}

    async def react(self, chat_id: int, message_id: str, emoji: str = "👍") -> dict[str, Any]:
        return await self.invoke(
            Op.REACTION,
            {
                "chatId": int(chat_id),
                "messageId": str(message_id),
                "reaction": {"reactionType": "EMOJI", "id": emoji},
            },
        )
