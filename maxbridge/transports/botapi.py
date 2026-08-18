"""Транспорт «botapi»: официальный Bot API MAX.

Легальный и стабильный режим, но с принципиальным ограничением: бот получает
только адресованные ему сообщения — личку боту, упоминания и команды. Все
сообщения аккаунта он не видит. Для полного охвата нужен режим userbot.

Документация: https://dev.max.ru/docs-api  (домен platform-api.max.ru,
токен передаётся заголовком Authorization, а не query-параметром).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from ..models import Attachment, MaxMessage
from .base import MaxTransport

log = logging.getLogger("maxbridge.botapi")


class BotApiTransport(MaxTransport):
    name = "botapi"
    sees_everything = False

    def __init__(self, token: str, api_base: str = "https://platform-api.max.ru") -> None:
        super().__init__()
        self.token = token
        self.api_base = api_base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._marker: int | None = None
        self._stopping = False
        self._titles: dict[int, str] = {}

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"Authorization": self.token},
            timeout=aiohttp.ClientTimeout(total=90),
        )
        me = await self._request("GET", "/me")
        log.info("Bot API MAX: подключён как %s", me.get("name") or me.get("username") or "?")
        await self._poll_loop()

    async def stop(self) -> None:
        self._stopping = True
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _poll_loop(self) -> None:
        """Long polling. Для продакшена MAX рекомендует webhook, но polling
        не требует публичного HTTPS-эндпоинта — это важно при self-hosted."""
        backoff = 1.0
        while not self._stopping:
            try:
                params: dict[str, Any] = {"timeout": 30, "limit": 100}
                if self._marker is not None:
                    params["marker"] = self._marker
                data = await self._request("GET", "/updates", params=params)
                self._marker = data.get("marker", self._marker)
                for update in data.get("updates") or []:
                    await self._handle_update(update)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("ошибка опроса Bot API: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        if update.get("update_type") != "message_created":
            return
        raw = update.get("message") or {}
        recipient = raw.get("recipient") or {}
        sender = raw.get("sender") or {}
        body = raw.get("body") or {}

        chat_id = recipient.get("chat_id")
        if not isinstance(chat_id, int):
            return

        await self._emit(
            MaxMessage(
                chat_id=chat_id,
                message_id=str(body.get("mid") or ""),
                text=str(body.get("text") or ""),
                sender_id=int(sender.get("user_id") or 0),
                sender_name=str(sender.get("name") or sender.get("username") or ""),
                chat_title=self._titles.get(chat_id, ""),
                chat_kind=str(recipient.get("chat_type") or "dialog").lower(),
                ts=int(raw.get("timestamp") or time.time() * 1000),
                outgoing=False,
                attachments=_parse_attachments(body.get("attachments") or []),
                raw=raw,
            )
        )

    # ---------------------------------------------------------------- методы
    async def send(self, chat_id: int, text: str, *, reply_to: str = "") -> str:
        payload: dict[str, Any] = {"text": text}
        if reply_to:
            payload["link"] = {"type": "reply", "mid": reply_to}
        data = await self._request(
            "POST", "/messages", params={"chat_id": chat_id}, json=payload
        )
        body = (data.get("message") or {}).get("body") or {}
        return str(body.get("mid") or "")

    def chat_title(self, chat_id: int) -> str:
        return self._titles.get(chat_id, "")

    async def fetch_chat_title(self, chat_id: int) -> str:
        try:
            data = await self._request("GET", f"/chats/{chat_id}")
        except Exception as exc:  # noqa: BLE001
            log.debug("не смог получить название чата %s: %s", chat_id, exc)
            return ""
        title = str(data.get("title") or "")
        if title:
            self._titles[chat_id] = title
        return title

    # ------------------------------------------------------------------ http
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("Bot API транспорт не запущен")
        url = f"{self.api_base}{path}"
        async with self._session.request(method, url, params=params, json=json) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"MAX Bot API {response.status} на {path}: {text[:200]}")
            if not text:
                return {}
            import json as _json

            return _json.loads(text)


def _parse_attachments(items: list[Any]) -> list[Attachment]:
    result: list[Attachment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload") or {}
        result.append(
            Attachment(
                kind=str(item.get("type") or "unknown").lower(),
                url=str(payload.get("url") or ""),
                name=str(payload.get("filename") or ""),
                raw=item,
            )
        )
    return result
