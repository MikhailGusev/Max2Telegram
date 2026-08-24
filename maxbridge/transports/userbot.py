"""Транспорт «userbot»: аккаунт MAX целиком.

Видит ВСЕ входящие — личные диалоги, группы, каналы — и отвечает от имени
владельца аккаунта. Работает поверх внутреннего WS-протокола (см. maxproto/).

Ограничение по закону жанра: это неофициальный API, см. docs/LEGAL.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from ..maxproto import MaxProtocolError, MaxWSClient, Op
from ..models import Attachment, MaxMessage
from .base import MaxTransport

log = logging.getLogger("maxbridge.userbot")

#: как MAX называет типы вложений -> наши короткие имена
ATTACH_KINDS = {
    "PHOTO": "photo",
    "IMAGE": "photo",
    "VIDEO": "video",
    "AUDIO": "audio",
    "FILE": "file",
    "STICKER": "sticker",
    "SHARE": "link",
    "CONTROL": "system",
}


class UserbotTransport(MaxTransport):
    name = "userbot"
    sees_everything = True
    supports_media = True

    def __init__(self, session_path: str | Path) -> None:
        super().__init__()
        self.client = MaxWSClient(session_path)
        self.client.on_packet(self._on_packet)
        self._titles: dict[int, str] = {}
        self._kinds: dict[int, str] = {}
        self._names: dict[int, str] = {}
        #: id собеседника -> id его личного диалога. Нужно, чтобы /write слал
        #: сообщение в чат, а не в id контакта (это разные числа в MAX).
        self._dialog_by_user: dict[int, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._resolving: set[int] = set()
        #: id, чьё имя MAX так и не отдал — не долбим повторно каждое сообщение
        self._unresolvable: set[int] = set()
        self._resolve_tasks: set[asyncio.Task[None]] = set()
        #: группы, по которым уже подтянули список участников (имена)
        self._members_loaded: set[int] = set()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.client.run_forever(on_ready=self._on_ready)

    async def stop(self) -> None:
        await self.client.close()

    async def _on_ready(self, payload: dict[str, Any]) -> None:
        """Вызывается после каждого успешного логина: обновляем справочники."""
        self._absorb_directory(payload)
        log.info(
            "MAX синхронизирован: чатов %d, известных имён %d",
            len(self._titles),
            len(self._names),
        )

    # ------------------------------------------------------------ справочники
    def _absorb_directory(self, payload: dict[str, Any]) -> None:
        """Разбирает ответ синхронизации: имена людей и названия чатов.

        Схема ответа MAX не документирована и меняется, поэтому парсим мягко —
        любое непонятное поле просто пропускаем.

        Порядок важен: сначала люди, потом чаты. Личные диалоги приходят вообще
        без поля title (у MAX их 98 из 100), и название приходится собирать из
        имени собеседника — без справочника имён получились бы «MAX -70123…».
        """
        for key in ("contacts", "profiles", "users"):
            for person in _iter_people(payload.get(key)):
                self._remember_person(person)

        for chat in payload.get("chats") or []:
            if not isinstance(chat, dict):
                continue
            chat_id = chat.get("id")
            if not isinstance(chat_id, int):
                continue

            kind = str(chat.get("type") or "CHAT").lower()
            kind = "dialog" if kind == "dialog" else kind
            self._kinds[chat_id] = kind

            if kind == "dialog":
                self._map_dialog(chat_id, chat)

            title = str(chat.get("title") or "")
            if not title and kind == "dialog":
                title = self._dialog_title(chat)
            if title:
                self._titles[chat_id] = title

    def _map_dialog(self, chat_id: int, chat: dict[str, Any]) -> None:
        """Запоминает, какому диалогу соответствует собеседник."""
        participants = chat.get("participants")
        if not isinstance(participants, dict):
            return
        for raw_id in participants:
            try:
                person_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if person_id and person_id != self.client.me_id:
                self._dialog_by_user[person_id] = chat_id

    def _dialog_title(self, chat: dict[str, Any]) -> str:
        """Имя собеседника в личном диалоге: участник, который не я."""
        participants = chat.get("participants")
        if not isinstance(participants, dict):
            return ""
        for raw_id in participants:
            try:
                person_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if person_id and person_id != self.client.me_id:
                return self._names.get(person_id, "")
        return ""

    def _remember_person(self, person: dict[str, Any]) -> None:
        contact = person.get("contact") if isinstance(person.get("contact"), dict) else {}
        person_id = person.get("id")
        if not isinstance(person_id, int):
            person_id = contact.get("id")
        if not isinstance(person_id, int):
            return
        name = _extract_name(person) or _extract_name(contact)
        if name:
            self._names[person_id] = str(name)

    def _schedule_resolve(self, user_id: int) -> None:
        """Запускает получение имени в фоне — НЕ блокируя приём сообщений.

        Раньше resolve ждали прямо в обработчике пакета, и таймаут opcode 32
        (MAX не отвечает на посторонних) морозил доставку всех сообщений на 30 с.
        Теперь сообщение уходит сразу, а имя (если MAX его отдаст) подтянется к
        следующим сообщениям этого отправителя.
        """
        if (
            not user_id
            or user_id in self._names
            or user_id in self._resolving
            or user_id in self._unresolvable
        ):
            return
        task = asyncio.create_task(self._resolve_user(user_id))
        self._resolve_tasks.add(task)
        task.add_done_callback(self._resolve_tasks.discard)

    def _schedule_members(self, chat_id: int) -> None:
        """Один раз на группу подтягивает список участников (для имён)."""
        if chat_id in self._members_loaded:
            return
        self._members_loaded.add(chat_id)
        task = asyncio.create_task(self._load_members(chat_id))
        self._resolve_tasks.add(task)
        task.add_done_callback(self._resolve_tasks.discard)

    async def _load_members(self, chat_id: int) -> None:
        """Забирает участников группы (опкод 59) и запоминает их имена.

        В ответе каждый участник — {contact:{id, names...}, presence, readMark};
        имя и id достаёт _remember_person из вложенного contact. Так «неизвестный»
        в группах превращается в реальные имена. Только чтение, ничего не меняет.
        """
        try:
            response = await asyncio.wait_for(
                self.client.invoke(
                    Op.GET_MEMBERS,
                    {"type": "MEMBER", "marker": 0, "chatId": chat_id, "count": 100},
                ),
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - имена не критичны для доставки
            log.debug("не смог получить участников группы %s: %s", chat_id, exc)
            self._members_loaded.discard(chat_id)  # дать шанс повторить позже
            return
        members = (response.get("payload") or {}).get("members") or []
        before = len(self._names)
        for member in members:
            if isinstance(member, dict):
                self._remember_person(member)
        log.info(
            "группа %s: участников %d, новых имён +%d",
            chat_id,
            len(members),
            len(self._names) - before,
        )

    async def _resolve_user(self, user_id: int) -> None:
        """Подтягивает имя отправителя. Вызывается в фоне через _schedule_resolve."""
        if user_id in self._names or user_id in self._resolving or not user_id:
            return
        self._resolving.add(user_id)
        try:
            # короткий таймаут: MAX на посторонних не отвечает, ждать 30 с незачем
            response = await asyncio.wait_for(
                self.client.invoke(Op.RESOLVE_USERS, {"contactIds": [user_id]}),
                timeout=10,
            )
            payload = response.get("payload") or {}
            # ответ MAX кладёт людей под разными ключами и то списком, то словарём
            # по id — забираем из всех известных вариантов
            for key in ("contacts", "profiles", "users"):
                for person in _iter_people(payload.get(key)):
                    self._remember_person(person)
            if user_id not in self._names:
                # имя не нашлось — покажем форму ответа (только ключи), чтобы
                # понять, где MAX прячет имя участника группы
                log.debug(
                    "RESOLVE_USERS не дал имя %s: ключи ответа=%s", user_id, sorted(payload.keys())
                )
                for key in ("contacts", "profiles", "users"):
                    sample = _first_person(payload.get(key))
                    if sample is not None:
                        log.debug("  %s[0] ключи=%s", key, sorted(sample.keys()))
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 - имя не критично
            log.debug("не смог получить имя пользователя %s: %s", user_id, exc)
        finally:
            self._resolving.discard(user_id)
            if user_id not in self._names:
                # имя не отдали — помечаем, чтобы не долбить запрос на каждом
                # следующем сообщении (иначе поток фоновых таймаутов)
                self._unresolvable.add(user_id)

    def chat_title(self, chat_id: int) -> str:
        return self._titles.get(chat_id, "")

    @staticmethod
    def _match_tier(name: str, q: str) -> int:
        """Насколько хорошо имя совпадает с запросом. Меньше — точнее.

        0 — имя точно равно запросу («Аня» == «Аня»);
        1 — запрос начинает имя или одно из слов («Аня» → «Аня Бухмиллер»);
        2 — запрос где-то внутри («аня» → «Ваня», «Туманян»);
        3 — не совпало.
        Так точное имя не тонет среди случайных подстрок, и /write может
        выбрать одного человека вместо бесконечного «уточни».
        """
        name = name.casefold()
        if name == q:
            return 0
        if name.startswith(q) or any(word.startswith(q) for word in name.split()):
            return 1
        if q in name:
            return 2
        return 3

    def find_chats(self, query: str, limit: int = 10) -> list[tuple[int, str]]:
        """Поиск по синхронизированному при входе списку чатов и по контактам.

        Ищем среди названий чатов (групп и диалогов) и по именам контактов
        (у контакта id совпадает с chat_id личного диалога), затем оставляем
        только совпадения самого точного найденного уровня — см. _match_tier.
        """
        q = query.strip().casefold()
        if not q:
            return []

        # (tier, длина, имя_cf, chat_id, title)
        candidates: list[tuple[int, int, str, int, str]] = []
        seen: set[int] = set()

        def consider(chat_id: int, title: str) -> None:
            if not title or chat_id in seen:
                return
            tier = self._match_tier(title, q)
            if tier == 3:
                return
            seen.add(chat_id)
            candidates.append((tier, len(title), title.casefold(), chat_id, title))

        for chat_id, title in self._titles.items():
            consider(chat_id, title)

        # по имени собеседника: отправлять нужно в ЕГО ДИАЛОГ (chat_id),
        # а не в id контакта — это разные числа, иначе MAX ответит not.found
        for user_id, name in self._names.items():
            dialog_id = self._dialog_by_user.get(user_id)
            if dialog_id is not None:
                consider(dialog_id, name)

        if not candidates:
            return []

        best = min(item[0] for item in candidates)
        top = [item for item in candidates if item[0] == best]
        top.sort(key=lambda item: (item[1], item[2]))
        return [(chat_id, title) for _, _, _, chat_id, title in top][:limit]

    # --------------------------------------------------------------- события
    async def _on_packet(self, packet: dict[str, Any]) -> None:
        if packet.get("opcode") != Op.EVT_NEW_MESSAGE:
            return
        payload = packet.get("payload") or {}
        raw = payload.get("message") or {}
        chat_id = payload.get("chatId")
        if not isinstance(chat_id, int) or not raw:
            return

        sender_id = int(raw.get("sender") or raw.get("senderId") or 0)
        if sender_id and sender_id not in self._names:
            # в группе имена берём из списка участников (опкод 59 — работает),
            # в личных диалогах — точечным resolve (опкод 32). Оба в фоне, чтобы
            # не блокировать доставку ожиданием ответа MAX.
            if self._kinds.get(chat_id) == "chat":
                self._schedule_members(chat_id)
            else:
                self._schedule_resolve(sender_id)

        sender_name = self._names.get(sender_id, "")
        if not sender_name and sender_id != self.client.me_id:
            # диагностика групповых чатов: имя не определилось — покажем, в каком
            # поле пакета лежит отправитель (только КЛЮЧИ, без приватного текста)
            log.debug(
                "имя отправителя не определилось: chat=%s kind=%s sender_id=%s ключи_сообщения=%s",
                chat_id,
                self._kinds.get(chat_id, "?"),
                sender_id,
                sorted(raw.keys()),
            )

        # в личном диалоге собеседник и есть название чата: если при
        # синхронизации имени ещё не знали, забираем его из первого же письма
        if (
            sender_name
            and not self._titles.get(chat_id)
            and self._kinds.get(chat_id, "dialog") == "dialog"
            and sender_id != self.client.me_id
        ):
            self._titles[chat_id] = sender_name

        message = MaxMessage(
            chat_id=chat_id,
            message_id=str(raw.get("id") or ""),
            text=str(raw.get("text") or ""),
            sender_id=sender_id,
            sender_name=sender_name,
            chat_title=self._titles.get(chat_id, ""),
            chat_kind=self._kinds.get(chat_id, "dialog"),
            ts=int(raw.get("time") or 0) or int(time.time() * 1000),
            outgoing=bool(sender_id and sender_id == self.client.me_id),
            reply_to=str(((raw.get("link") or {}).get("messageId")) or ""),
            attachments=_parse_attaches(raw.get("attaches") or []),
            raw=raw,
        )
        await self._emit(message)

    # ---------------------------------------------------------------- методы
    async def _send_with_reconnect(self, coro_factory: Any) -> dict[str, Any]:
        """Отправка, переживающая обрыв соединения.

        MAX иногда закрывает WS в момент отправки (частый случай при ответе в
        группу — сообщение терялось с «соединение закрыто»). run_forever уже
        переподключается сам; здесь мы просто ждём восстановления связи и
        повторяем отправку один раз, вместо того чтобы отдать ошибку наверх.
        """
        try:
            return await coro_factory()
        except MaxProtocolError as exc:
            if "закры" not in str(exc).lower():
                raise
            log.info("MAX закрыл соединение при отправке — жду переподключения и повторяю")
            for _ in range(30):  # до ~15 секунд ожидания реконнекта
                if self.client.connected:
                    break
                await asyncio.sleep(0.5)
            return await coro_factory()

    async def send(self, chat_id: int, text: str, *, reply_to: str = "") -> str:
        response = await self._send_with_reconnect(
            lambda: self.client.send_message(chat_id, text, reply_to=reply_to or None)
        )
        payload = response.get("payload") or {}
        message = payload.get("message") or {}
        return str(message.get("id") or "")

    async def fetch_attachment(self, message: MaxMessage, index: int = 0) -> tuple[bytes, str]:
        """Скачивает вложение: фото по прямой ссылке, файлы и видео — через
        отдельный запрос ссылки (она подписанная и живёт недолго)."""
        if index >= len(message.attachments):
            raise ValueError("нет такого вложения")
        attachment = message.attachments[index]
        raw = attachment.raw

        url = attachment.url
        if attachment.kind == "file" and raw.get("fileId") is not None:
            url = await self.client.file_url(
                message.chat_id, str(message.message_id), int(raw["fileId"])
            )
        elif attachment.kind == "video" and raw.get("videoId") is not None:
            url = await self.client.video_url(
                message.chat_id, str(message.message_id), int(raw["videoId"])
            )

        if not url:
            raise ValueError(f"не смог определить ссылку на вложение «{attachment.kind}»")

        data = await self.client.download(url)
        return data, attachment.name or _default_name(attachment.kind)

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
        if kind == "photo":
            attach = await self.client.upload_photo(chat_id, data, filename)
        elif kind == "video":
            attach = await self.client.upload_video(chat_id, data, filename)
        else:
            attach = await self.client.upload_file(chat_id, data, filename)

        response = await self._send_with_reconnect(
            lambda: self.client.send_message(
                chat_id, caption, reply_to=reply_to or None, attaches=[attach]
            )
        )
        message = (response.get("payload") or {}).get("message") or {}
        return str(message.get("id") or "")

    async def mark_read(self, chat_id: int, message_id: str) -> None:
        await self.client.mark_read(chat_id, message_id)

    async def react(self, chat_id: int, message_id: str, emoji: str) -> None:
        await self.client.react(chat_id, message_id, emoji)


def _iter_people(container: Any):
    """Перебирает людей в ответе MAX, где это может быть список ИЛИ словарь по id."""
    if isinstance(container, list):
        for item in container:
            if isinstance(item, dict):
                yield item
    elif isinstance(container, dict):
        for item in container.values():
            if isinstance(item, dict):
                yield item


def _first_person(container: Any) -> dict[str, Any] | None:
    for item in _iter_people(container):
        return item
    return None


def _extract_name(data: dict[str, Any]) -> str:
    """Достаёт отображаемое имя из объекта человека в любом из виденных форматов."""
    if not isinstance(data, dict):
        return ""
    names = data.get("names")
    if isinstance(names, list) and names and isinstance(names[0], dict):
        first = names[0].get("name")
        if first:
            return str(first)
    return str(data.get("name") or data.get("firstName") or "")


def _default_name(kind: str) -> str:
    return {
        "photo": "image.jpg",
        "video": "video.mp4",
        "audio": "audio.ogg",
        "sticker": "sticker.webp",
    }.get(kind, "file.bin")


def _parse_attaches(attaches: list[Any]) -> list[Attachment]:
    result: list[Attachment] = []
    for item in attaches:
        if not isinstance(item, dict):
            continue
        raw_kind = str(item.get("_type") or item.get("type") or "").upper()
        result.append(
            Attachment(
                kind=ATTACH_KINDS.get(raw_kind, raw_kind.lower() or "unknown"),
                url=str(item.get("url") or item.get("baseUrl") or ""),
                name=str(item.get("name") or item.get("fileName") or ""),
                raw=item,
            )
        )
    return result
