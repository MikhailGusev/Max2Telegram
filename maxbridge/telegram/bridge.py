"""Telegram-сторона моста на aiogram 3.

Главная идея интерфейса: **тема форума = чат MAX**. Владелец просто отвечает
в теме, как в обычной переписке, — текст уходит в тот же чат MAX от его имени.
Никаких команд для ответа не нужно.

Один бот обслуживает сколько угодно аккаунтов MAX: у каждого своя группа-форум,
и по её id мост понимает, чей это чат. Аккаунт определяется тем, где написано
сообщение, поэтому переключать «текущий аккаунт» руками не нужно.

Требования к группе: супергруппа с включёнными Темами (Topics), бот — админ
с правом «Управление темами».
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from io import BytesIO
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

from ..accounts import Account, AccountRegistry
from ..config import Config
from ..core.ai import AiAssistant, _hide_credentials as _hide_proxy
from ..core.digest import build_digest
from ..core.licensing import License
from ..core.rules import Verdict
from ..core.transcribe import Transcriber
from ..models import MaxMessage

log = logging.getLogger("maxbridge.telegram")

PRIORITY_MARK = {"urgent": "🔥", "normal": "", "low": "· "}


def _command_arg(text: str | None) -> str:
    """Всё после команды. Режем по ЛЮБОМУ пробелу, а не только обычному.

    Мобильные клиенты часто вставляют неразрывный пробел (U+00A0) между
    командой и аргументом — при разборе по обычному пробелу аргумент терялся,
    и `/write Манечка` выглядел как пустой. str.split() бьёт по всем видам
    пробелов, включая неразрывный.
    """
    parts = (text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""

#: боты Telegram не могут ни скачивать, ни отправлять файлы больше 50 МБ
MAX_TELEGRAM_UPLOAD = 50 * 1024 * 1024


class AccountSink:
    """Адаптер: роутер аккаунта отдаёт сообщения в общий Telegram-бот."""

    def __init__(self, bridge: "TelegramBridge", account: Account) -> None:
        self.bridge = bridge
        self.account = account

    async def deliver(self, message: MaxMessage, verdict: Verdict) -> int | None:
        return await self.bridge.deliver(self.account, message, verdict)


class TelegramBridge:
    def __init__(
        self,
        config: Config,
        accounts: AccountRegistry,
        ai: AiAssistant,
        license_: License,
        transcriber: Transcriber | None = None,
        billing: Any = None,
    ) -> None:
        self.config = config
        self.accounts = accounts
        self.ai = ai
        self.license = license_
        self.transcriber = transcriber or Transcriber()
        self.billing = billing
        # прокси нужен там, где провайдер режет api.telegram.org (российские
        # хостинги). MAX при этом ходит напрямую — его сессия остаётся местной.
        session = None
        if config.telegram_proxy:
            from aiogram.client.session.aiohttp import AiohttpSession

            session = AiohttpSession(proxy=config.telegram_proxy)
            log.info("Telegram через прокси %s", _hide_proxy(config.telegram_proxy))
        self.bot = Bot(token=config.telegram_token, session=session)
        self.dp = Dispatcher()
        self._drafts: dict[tuple[str, int], list[str]] = {}
        # SaaS-онбординг: заполняется приложением через bind_onboarding(). Пусто —
        # приёма новых клиентов нет (мост работает только на аккаунтах владельца).
        self.clients: Any = None
        self.spin_up: Any = None
        self._onboarding: dict[int, Any] = {}  # tg_user_id -> активная попытка QR
        self._register()

        for account in self.accounts:
            account.router.attach_telegram(AccountSink(self, account))

    def attach_account(self, account: Account) -> None:
        """Подключает роутер аккаунта к боту (используется при подъёме клиента)."""
        account.router.attach_telegram(AccountSink(self, account))

    def bind_onboarding(self, clients: Any, spin_up: Any) -> None:
        """Даёт боту хранилище клиентов и функцию подъёма аккаунта клиента."""
        self.clients = clients
        self.spin_up = spin_up

    # ------------------------------------------------------------- доставка
    async def deliver(
        self, account: Account, message: MaxMessage, verdict: Verdict
    ) -> int | None:
        """Доставляет входящее MAX-сообщение владельцу.

        Два режима. Форум (Premium): у аккаунта привязана группа-форум — каждый
        чат MAX едет в свою тему. Плоский (Free): группы нет — всё падает одной
        лентой в личку владельцу, с подписью «чат · отправитель». Ответ в обоих
        случаях — реплаем на сообщение (см. _on_topic_reply / _on_private_reply).
        """
        forum = bool(account.forum_chat_id)
        if forum:
            target = account.forum_chat_id
            thread_id: int | None = await self._ensure_topic(account, message)
        else:
            target = account.owner_id
            thread_id = None

        text = self._render(message, verdict, flat=not forum)
        keyboard = self._keyboard(account, message, verdict)

        if message.attachments and account.transport.supports_media:
            sent_id = await self._deliver_media(
                account, message, target, thread_id, text, keyboard
            )
            if sent_id is not None:
                return sent_id
            # не получилось перетащить файл — ниже уйдёт текст со ссылкой

        try:
            sent = await self.bot.send_message(
                chat_id=target,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_notification=(verdict.priority == "low"),
            )
        except TelegramBadRequest as exc:
            # тему могли удалить руками — заводим заново и пробуем ещё раз
            if thread_id is not None and "thread not found" in str(exc).lower():
                account.storage.bind_topic(message.chat_id, 0)
                thread_id = await self._ensure_topic(account, message)
                sent = await self.bot.send_message(
                    chat_id=target,
                    message_thread_id=thread_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            else:
                raise
        return sent.message_id

    async def _deliver_media(
        self,
        account: Account,
        message: MaxMessage,
        target_chat_id: int,
        thread_id: int | None,
        caption: str,
        keyboard: InlineKeyboardMarkup | None,
    ) -> int | None:
        """Перекладывает вложение из MAX в Telegram настоящим файлом.

        Возвращает None, если не получилось, — вызывающий отправит текст
        со ссылкой, чтобы сообщение не потерялось совсем.
        """
        attachment = message.attachments[0]
        try:
            data, filename = await account.transport.fetch_attachment(message, 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("вложение «%s» не скачалось: %s", attachment.kind, exc)
            return None

        if len(data) > MAX_TELEGRAM_UPLOAD:
            log.info(
                "вложение %.1f МБ больше лимита Telegram — отправляю ссылкой",
                len(data) / 1024 / 1024,
            )
            return None

        # голосовое приходит текстом: его можно прочитать глазами, найти
        # поиском и скормить правилам приоритета
        if attachment.kind in {"audio", "voice"} and self.transcriber.enabled:
            recognized = await self.transcriber.transcribe(data, filename)
            if recognized:
                caption = f"{caption}\n\n🎧 <i>{html.escape(recognized)}</i>"
                account.storage.save_transcript(
                    message.chat_id, str(message.message_id), recognized
                )

        file = BufferedInputFile(data, filename=filename)
        # у медиа лимит подписи 1024 символа, у текста — 4096
        short = caption if len(caption) <= 1024 else caption[:1021] + "..."
        common: dict[str, Any] = {
            "chat_id": target_chat_id,
            "message_thread_id": thread_id,
            "caption": short,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        }

        try:
            if attachment.kind == "photo":
                sent = await self.bot.send_photo(photo=file, **common)
            elif attachment.kind == "video":
                sent = await self.bot.send_video(video=file, **common)
            elif attachment.kind in {"audio", "voice"}:
                sent = await self.bot.send_voice(voice=file, **common)
            else:
                sent = await self.bot.send_document(document=file, **common)
        except TelegramBadRequest as exc:
            log.warning("Telegram не принял вложение: %s", exc)
            return None

        if len(message.attachments) > 1:
            await self.bot.send_message(
                chat_id=target_chat_id,
                message_thread_id=thread_id,
                text=f"…и ещё вложений: {len(message.attachments) - 1}",
            )
        return sent.message_id

    async def _ensure_topic(self, account: Account, message: MaxMessage) -> int:
        title = message.chat_title or message.sender_name or f"MAX {message.chat_id}"
        return await self._ensure_topic_by(account, message.chat_id, title, message.chat_kind)

    async def _ensure_topic_by(
        self, account: Account, chat_id: int, title: str, kind: str = "dialog"
    ) -> int:
        """Тема для чата по его id — общий путь для входящих и для /write."""
        chat = account.storage.get_chat(chat_id)
        if chat is not None and chat["tg_topic_id"]:
            return int(chat["tg_topic_id"])

        name = title or f"MAX {chat_id}"
        account.storage.upsert_chat(chat_id, title, kind)
        created = await self.bot.create_forum_topic(
            chat_id=account.forum_chat_id, name=name[:128]
        )
        account.storage.bind_topic(chat_id, created.message_thread_id)
        log.info(
            "аккаунт «%s»: создана тема «%s» для чата MAX %s",
            account.name,
            name,
            chat_id,
        )
        return created.message_thread_id

    def _render(self, message: MaxMessage, verdict: Verdict, *, flat: bool = False) -> str:
        mark = PRIORITY_MARK.get(verdict.priority, "")
        who = html.escape(message.sender_name or "неизвестный")
        body = html.escape(message.text or "")
        if flat:
            # одна лента: чат жирным, отправитель после точки — чтобы понять,
            # откуда сообщение, без отдельной темы
            chat = message.chat_title or message.sender_name or f"MAX {message.chat_id}"
            header = f"{mark}<b>{html.escape(chat)}</b>"
            if message.sender_name and message.sender_name != (message.chat_title or ""):
                header += f" · {who}"
            parts = [header]
        else:
            parts = [f"{mark}<b>{who}</b>"]
        if body:
            parts.append(body)
        for attachment in message.attachments:
            label = attachment.name or attachment.kind
            if attachment.url:
                parts.append(
                    f'📎 <a href="{html.escape(attachment.url)}">{html.escape(label)}</a>'
                )
            else:
                parts.append(f"📎 {html.escape(label)}")
        if verdict.reason and verdict.priority == "urgent":
            parts.append(f"<i>{html.escape(verdict.reason)}</i>")
        return "\n".join(parts)

    def _keyboard(
        self, account: Account, message: MaxMessage, verdict: Verdict
    ) -> InlineKeyboardMarkup | None:
        buttons: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        if self.ai.enabled and verdict.needs_reply:
            row.append(
                InlineKeyboardButton(text="✍️ Черновики", callback_data=f"draft:{message.chat_id}")
            )
        if account.config.stealth_mode:
            row.append(
                InlineKeyboardButton(
                    text="👁 Прочитано",
                    callback_data=f"read:{message.chat_id}:{message.message_id}",
                )
            )
        if row:
            buttons.append(row)
        buttons.append(
            [
                InlineKeyboardButton(text="🔕 Тише", callback_data=f"mute:{message.chat_id}"),
                InlineKeyboardButton(text="⭐ Важный", callback_data=f"vip:{message.chat_id}"),
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    # -------------------------------------------------------- выбор аккаунта
    def _account_for(self, event: Message | CallbackQuery) -> Account | None:
        """Аккаунт определяется тем, где написано сообщение.

        В группе — по её id. В личке с ботом — единственный аккаунт владельца;
        если их несколько, команду нужно писать в нужной группе.
        """
        chat = event.chat if isinstance(event, Message) else (
            event.message.chat if event.message is not None else None
        )
        if chat is not None:
            account = self.accounts.by_forum(chat.id)
            if account is not None:
                return account

        user = event.from_user
        if user is None:
            return None
        owned = self.accounts.owned_by(user.id)
        return owned[0] if len(owned) == 1 else None

    async def _need_account(self, message: Message) -> Account | None:
        account = self._account_for(message)
        if account is None:
            names = ", ".join(acc.name for acc in self.accounts.owned_by(message.from_user.id))
            await message.answer(
                "У тебя несколько аккаунтов MAX, поэтому непонятно, к какому "
                f"относится команда: {names}.\nНапиши её в группе нужного аккаунта."
            )
        return account

    # ------------------------------------------------------------ обработчики
    def _register(self) -> None:
        dp = self.dp
        dp.message.register(self._cmd_start, CommandStart())
        dp.message.register(self._cmd_bind, Command("bind"))
        dp.message.register(self._cmd_status, Command("status"))
        dp.message.register(self._cmd_accounts, Command("accounts"))
        dp.message.register(self._cmd_chats, Command("chats"))
        dp.message.register(self._cmd_write, Command("write"))
        dp.message.register(self._cmd_find, Command("find"))
        dp.message.register(self._cmd_digest, Command("digest"))
        dp.message.register(self._cmd_rule, Command("rule"))
        dp.message.register(self._cmd_rules, Command("rules"))
        dp.message.register(self._cmd_rmrule, Command("rmrule"))
        dp.message.register(self._cmd_autoreply, Command("autoreply"))
        dp.message.register(self._cmd_license, Command("license"))
        dp.message.register(self._cmd_premium, Command("premium"))
        dp.message.register(self._cmd_grant, Command("grant"))
        dp.message.register(self._cmd_clients, Command("clients"))
        dp.message.register(self._cmd_registration, Command("registration"))
        dp.message.register(self._cmd_help, Command("help"))
        dp.message.register(self._on_topic_reply, F.message_thread_id.is_not(None), F.text)
        dp.message.register(
            self._on_topic_media,
            F.message_thread_id.is_not(None),
            F.photo | F.document | F.video | F.voice | F.audio,
        )
        # плоский режим (Free без форума): ответ — реплай на сообщение из ленты
        # в личке бота. Команды перехватываются выше, поэтому сюда доходит только
        # обычный текст/медиа, отвечающий на конкретное сообщение.
        dp.message.register(
            self._on_private_reply,
            F.chat.type == "private",
            F.reply_to_message.is_not(None),
            F.text,
        )
        dp.message.register(
            self._on_private_media,
            F.chat.type == "private",
            F.reply_to_message.is_not(None),
            F.photo | F.document | F.video | F.voice | F.audio,
        )
        dp.callback_query.register(self._on_callback)

    async def _guard(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        if user is not None and self.accounts.is_owner(user.id):
            return True
        if isinstance(event, CallbackQuery):
            await event.answer("Этот мост чужой", show_alert=True)
        return False

    async def _cmd_start(self, message: Message) -> None:
        user = message.from_user
        # уже владелец (в т.ч. подключённый клиент) — обычное приветствие
        if user is not None and self.accounts.is_owner(user.id):
            account = self._account_for(message)
            forum = account is not None and bool(account.forum_chat_id)
            how = (
                "Каждый чат MAX появится отдельной темой в группе. "
                "Отвечай прямо в теме — текст уйдёт в MAX от твоего имени."
                if forum
                else "Сообщения из MAX приходят сюда одной лентой. Чтобы ответить — "
                "сделай реплай на нужное сообщение, и текст уйдёт в MAX от твоего "
                "имени.\n\nХочешь каждый чат отдельной темой — это Premium: /premium"
            )
            await message.answer(f"MaxBridge на связи.\n\n{how}\n\nКоманды: /help")
            return

        # не владелец: возможно, новый клиент хочет подключиться (только в личке)
        if message.chat.type != "private":
            return
        ok, why = self._onboarding_open()
        if not ok:
            await message.answer(f"Этот бот подключает твой MAX к Telegram, но {why}.")
            return
        await self._begin_onboarding(message)

    # ----------------------------------------------------- SaaS-онбординг
    def _is_server_owner(self, user_id: int | None) -> bool:
        """Владелец всей установки (не путать с клиентом, который владеет только
        своим аккаунтом). Только ему доступна owner-панель."""
        return user_id is not None and user_id == self.config.owner_id

    def _owner_storage(self) -> Any:
        """База аккаунта владельца сервера — в ней держим рантайм-настройки."""
        for account in self.accounts:
            if account.owner_id == self.config.owner_id:
                return account.storage
        return None

    def _registration_enabled(self) -> bool:
        """Тумблер приёма клиентов: рантайм-override из БД важнее флага .env."""
        enabled = self.config.onboarding_enabled
        storage = self._owner_storage()
        if storage is not None:
            override = storage.get("onboarding_enabled", "")
            if override != "":
                enabled = override == "1"
        return enabled

    def _onboarding_open(self) -> tuple[bool, str]:
        """Можно ли принять нового клиента прямо сейчас."""
        if self.clients is None or self.spin_up is None:
            return False, "приём новых клиентов не настроен"
        if not self._registration_enabled():
            return False, "регистрация сейчас закрыта — напиши владельцу"
        cap = self.config.max_clients
        if cap and self.clients.count_active() >= cap:
            return False, "сервер сейчас заполнен, попробуй позже"
        return True, ""

    @staticmethod
    def _qr_png(link: str) -> bytes:
        import qrcode
        from qrcode.image.pure import PyPNGImage  # чистый PNG без Pillow

        buffer = BytesIO()
        qrcode.make(link, image_factory=PyPNGImage).save(buffer)
        return buffer.getvalue()

    async def _begin_onboarding(self, message: Message) -> None:
        from ..clients import client_name

        user_id = message.from_user.id
        if user_id in self._onboarding and not self._onboarding[user_id].done():
            await message.answer(
                "Ты уже подключаешься — отсканируй QR выше. Если код устарел, "
                "пришли /start ещё раз через минуту."
            )
            return

        session_path = self.config.db_path.parent / f"{client_name(user_id)}_session.json"
        self.clients.start_onboarding(user_id, str(session_path))
        await message.answer("Подключаю твой MAX. Сейчас пришлю QR-код для входа…")
        self._onboarding[user_id] = asyncio.create_task(
            self._run_onboarding(user_id, message.chat.id, session_path),
            name=f"onboard:{user_id}",
        )

    async def _run_onboarding(self, user_id: int, chat_id: int, session_path: Any) -> None:
        """Показывает QR, ждёт скан, сохраняет сессию и поднимает аккаунт клиента."""
        from ..clients import STATUS_ACTIVE
        from ..maxproto import MaxAuthError, MaxWSClient

        client = MaxWSClient(session_path)
        try:
            await client.connect()
            qr = await client.request_qr()
            try:
                png = self._qr_png(str(qr.get("qrLink") or ""))
            except ImportError:
                await self.bot.send_message(
                    chat_id, "На сервере не установлен пакет qrcode — сообщи владельцу."
                )
                return
            await self.bot.send_photo(
                chat_id,
                BufferedInputFile(png, filename="max-qr.png"),
                caption=(
                    "Открой приложение MAX → Настройки → Устройства → "
                    "Подключить устройство и наведи камеру на этот код.\n"
                    "Жду подтверждения…"
                ),
            )
            interval = max(1.0, float(qr.get("pollingInterval") or 2000) / 1000.0)
            expires_at = float(qr.get("expiresAt") or 0)
            timeout = (expires_at / 1000.0 - time.time()) if expires_at else 180.0
            await client.poll_qr(qr["trackId"], interval=interval, timeout=timeout)
            await client.login_by_qr(qr["trackId"])
        except MaxAuthError as exc:
            await self.bot.send_message(
                chat_id, f"Не получилось войти: {exc}\nПопробуй заново: /start"
            )
            return
        except Exception:  # noqa: BLE001
            log.exception("онбординг клиента %s сорвался", user_id)
            await self.bot.send_message(
                chat_id, "Что-то пошло не так при подключении. Попробуй позже: /start"
            )
            return
        finally:
            await client.close()
            self._onboarding.pop(user_id, None)

        # сессия сохранена — поднимаем изолированный аккаунт клиента в рантайме
        try:
            self.clients.set_status(user_id, STATUS_ACTIVE)
            self.spin_up(user_id)
        except Exception:  # noqa: BLE001
            log.exception("не смог поднять аккаунт клиента %s после входа", user_id)
            await self.bot.send_message(
                chat_id, "Вошёл, но не смог запустить мост. Напиши владельцу."
            )
            return

        await self.bot.send_message(
            chat_id,
            "🎉 Готово! Твой MAX подключён.\n\n"
            "Входящие сообщения будут приходить сюда одной лентой. Чтобы "
            "ответить — сделай реплай на нужное сообщение, и текст уйдёт в MAX "
            "от твоего имени.\n\nКоманды: /help",
        )

    async def _cmd_help(self, message: Message) -> None:
        if not await self._guard(message):
            return
        extra = "/accounts — список аккаунтов MAX\n" if self.accounts.multi else ""
        owner = ""
        if self._is_server_owner(message.from_user.id):
            owner = (
                "\n\n<b>Владельцу сервера</b>\n"
                "/clients — клиенты и статистика\n"
                "/registration on|off — приём новых клиентов\n"
                "/grant &lt;user_id&gt; &lt;дней&gt; — выдать Premium"
            )
        await message.answer(
            "<b>Что умею</b>\n"
            "/bind — привязать эту группу-форум как приёмник\n"
            "/status — состояние моста и статистика\n"
            f"{extra}"
            "/chats — список чатов MAX и их тем\n"
            "/write имя — начать чат с кем-то из MAX\n"
            "/find слово — поиск по всей истории MAX\n"
            "/digest — сводка «что я пропустил» за сутки\n"
            "/rule срочно|urgent — правило приоритета\n"
            "   действия: urgent, mute, autoreply=текст\n"
            "/rules, /rmrule N — список и удаление правил\n"
            "/autoreply текст — автоответ в текущей теме\n"
            "/premium — оформить подписку\n"
            "/license — что включено в этой установке"
            f"{owner}",
            parse_mode="HTML",
        )

    async def _cmd_bind(self, message: Message) -> None:
        if not await self._guard(message):
            return

        # отдельные темы (форум-группа) — функция Premium. Без подписки входящие
        # приходят одной лентой в личку, привязывать группу незачем.
        if self.billing is not None and not self.billing.is_premium(message.from_user.id):
            await message.answer(
                "🔒 Отдельные темы (форум-группа) — функция Premium.\n\n"
                "Сейчас все чаты приходят одной лентой сюда, в личку: отвечай "
                "реплаем на нужное сообщение — уйдёт в MAX.\n"
                "Открыть отдельные темы: /premium"
            )
            return

        owned = self.accounts.owned_by(message.from_user.id)
        argument = _command_arg(message.text)
        if argument:
            account = self.accounts.by_name(argument)
            if account is None:
                await message.answer(f"Аккаунта «{argument}» нет. Есть: " + ", ".join(
                    acc.name for acc in owned
                ))
                return
        elif len(owned) == 1:
            account = owned[0]
        else:
            await message.answer(
                "Укажи, какой аккаунт привязать к этой группе: /bind имя\n"
                "Доступны: " + ", ".join(acc.name for acc in owned)
            )
            return

        self.accounts.rebind(account, message.chat.id)
        account.storage.set("forum_chat_id", str(message.chat.id))
        hint = (
            f"«{account.name}»: пропиши в accounts.json"
            if self.accounts.multi
            else "пропиши в .env"
        )
        await message.answer(
            f"Готово. Эта группа принимает аккаунт {hint}, чтобы пережить перезапуск:\n"
            f"<code>{message.chat.id}</code>",
            parse_mode="HTML",
        )

    async def _cmd_accounts(self, message: Message) -> None:
        if not await self._guard(message):
            return
        lines = []
        for account in self.accounts.owned_by(message.from_user.id):
            stats = account.storage.stats()
            bound = "группа привязана" if account.forum_chat_id else "⚠ группа не привязана"
            lines.append(
                f"<b>{html.escape(account.name)}</b> — {account.transport.name}, {bound}\n"
                f"чатов {stats['chats']}, сообщений {stats['messages']}, "
                f"ждут ответа {stats['pending']}"
            )
        await message.answer("\n\n".join(lines) or "Аккаунтов нет.", parse_mode="HTML")

    async def _cmd_status(self, message: Message) -> None:
        if not await self._guard(message):
            return
        blocks = []
        for account in self.accounts.owned_by(message.from_user.id):
            state = account.router.describe_state()
            coverage = (
                "все сообщения аккаунта"
                if state["sees_everything"]
                else "только адресованное боту"
            )
            title = f"<b>{html.escape(account.name)}</b>\n" if self.accounts.multi else ""
            blocks.append(
                f"{title}"
                f"<b>Транспорт:</b> {state['transport']} — {coverage}\n"
                f"<b>Стелс:</b> {'да' if state['stealth'] else 'нет'}\n"
                f"<b>AI:</b> {'включён' if state['ai'] else 'выключен'}\n"
                f"<b>Чатов:</b> {state['chats']}  <b>Сообщений:</b> {state['messages']}\n"
                f"<b>Отправлено:</b> {state['sent']}  <b>Ждут ответа:</b> {state['pending']}"
            )
        if self.billing is not None:
            blocks.append("💳 " + self.billing.status(message.from_user.id).describe())
        await message.answer("\n\n".join(blocks), parse_mode="HTML")

    async def _cmd_license(self, message: Message) -> None:
        if not await self._guard(message):
            return
        await message.answer(self.license.describe())

    # ------------------------------------------------------------- подписка
    async def _gate_ai(self, message: Message) -> bool:
        """True — AI-действие разрешено (и списано). Иначе объясняет и вернёт False."""
        if self.billing is None:
            return True
        user_id = message.from_user.id
        if self.billing.can_use_ai(user_id):
            self.billing.note_ai_use(user_id)
            return True
        await message.answer(
            "🔒 На сегодня бесплатные AI-действия закончились "
            f"({self.config.free_ai_per_day} в день).\n"
            "Оформи Premium — AI без ограничений: /premium",
        )
        return False

    async def _cmd_premium(self, message: Message) -> None:
        if not await self._guard(message):
            return
        user_id = message.from_user.id
        if self.billing is None:
            await message.answer("Это бесплатная версия — все функции уже доступны.")
            return
        st = self.billing.status(user_id)
        if st.premium:
            await message.answer(f"У тебя Premium 💎\n{st.describe()}")
            return

        url = self.billing.payment_url(user_id)
        text = (
            f"<b>Premium — {self.config.premium_price} ₽/мес</b>\n\n"
            "• AI-приоритеты 🔥 на каждое сообщение\n"
            "• AI-черновики ответов без лимита\n"
            "• Сводки «что я пропустил»\n"
            "• Голосовые → текст\n"
            "• Эскалация в SMS и WhatsApp\n\n"
        )
        if url:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатить картой", url=url)]]
            )
            await message.answer(
                text + "После оплаты Premium включится автоматически.",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await message.answer(
                text + "Оплата пока не настроена — напиши владельцу для доступа.",
                parse_mode="HTML",
            )

    async def _cmd_grant(self, message: Message) -> None:
        """Только владелец сервера: выдать Premium. /grant <user_id> <дней>."""
        if not await self._guard(message):
            return
        if not self._is_server_owner(message.from_user.id):
            await message.answer("Эта команда — только для владельца сервера.")
            return
        if self.billing is None:
            await message.answer("Биллинг выключен.")
            return
        parts = _command_arg(message.text).split()
        if len(parts) != 2 or not parts[0].lstrip("-").isdigit() or not parts[1].isdigit():
            await message.answer(
                "Формат: /grant &lt;user_id&gt; &lt;дней&gt;\n"
                "Например: /grant 74906080 180 — премиум на полгода.\n"
                "Сроки: 30 / 150 / 180 / 365.\n"
                "user_id друга — через @userinfobot.",
                parse_mode="HTML",
            )
            return
        target, days = int(parts[0]), int(parts[1])
        until = self.billing.grant_premium(target, days=days, by=f"owner:{message.from_user.id}")
        from datetime import datetime, timezone

        when = datetime.fromtimestamp(until, timezone.utc).astimezone()
        await message.answer(f"Готово: Premium для {target} до {when:%d.%m.%Y}.")
        try:
            await self.bot.send_message(
                target,
                f"🎉 Тебе выдали Premium в MaxBridge до {when:%d.%m.%Y}. "
                "AI-функции теперь без ограничений.",
            )
        except TelegramBadRequest:
            pass  # друг ещё не писал боту — узнает сам через /status

    # ------------------------------------------------------ owner-панель SaaS
    async def _cmd_registration(self, message: Message) -> None:
        """Владельцу сервера: включить/выключить приём новых клиентов."""
        if not self._is_server_owner(message.from_user.id):
            return
        arg = _command_arg(message.text).strip().lower()
        if arg not in {"on", "off", "вкл", "выкл"}:
            state = "включена" if self._registration_enabled() else "выключена"
            await message.answer(
                f"Регистрация клиентов сейчас {state}.\n"
                "Переключить: /registration on | off"
            )
            return
        storage = self._owner_storage()
        turn_on = arg in {"on", "вкл"}
        if storage is not None:
            storage.set("onboarding_enabled", "1" if turn_on else "0")
        await message.answer(
            "Регистрация клиентов включена — новые /start принимаются."
            if turn_on
            else "Регистрация клиентов выключена — новых не пускаю."
        )

    async def _cmd_clients(self, message: Message) -> None:
        """Владельцу сервера: список клиентов и статистика."""
        if not self._is_server_owner(message.from_user.id):
            return
        if self.clients is None:
            await message.answer("Онбординг клиентов не настроен.")
            return
        cap = self.config.max_clients
        limit = f" / {cap}" if cap else ""
        state = "включена" if self._registration_enabled() else "выключена"
        head = (
            f"<b>Клиенты сервера</b>\n"
            f"Активных: {self.clients.count_active()}{limit}\n"
            f"Регистрация: {state}\n"
        )
        records = self.clients.all()
        if not records:
            await message.answer(head + "\nПока никто не подключился.", parse_mode="HTML")
            return
        lines = []
        for record in records:
            account = self.accounts.by_name(record.name)
            msgs = account.storage.stats()["messages"] if account is not None else 0
            lines.append(
                f"• <code>{record.tg_user_id}</code> — {record.status}, сообщений {msgs}"
            )
        await message.answer(head + "\n" + "\n".join(lines), parse_mode="HTML")

    async def _cmd_chats(self, message: Message) -> None:
        if not await self._guard(message):
            return
        account = await self._need_account(message)
        if account is None:
            return
        rows = account.storage.list_chats()
        if not rows:
            await message.answer("Пока ни одного чата — жду первое сообщение из MAX.")
            return
        lines = []
        for row in rows[:50]:
            flags = "".join(["⭐" if row["vip"] else "", "🔕" if row["muted"] else ""])
            lines.append(f"{flags} {row['title'] or row['max_chat_id']}")
        await message.answer("\n".join(lines))

    async def _cmd_write(self, message: Message) -> None:
        """Начать чат с кем-то из MAX: находим по имени и открываем тему."""
        if not await self._guard(message):
            return
        account = await self._need_account(message)
        if account is None:
            return

        if not account.forum_chat_id:
            await message.answer(
                "В плоском режиме отдельную тему для нового чата не создать: "
                "напиши человеку прямо в приложении MAX, и его ответ прилетит "
                "сюда лентой. Отдельные темы с /write — в Premium: /premium"
            )
            return

        query = _command_arg(message.text)
        if not query:
            await message.answer(
                "Кому написать? Например: /write Пётр\n"
                "Ищу по имени среди твоих чатов и контактов MAX."
            )
            return

        matches = account.transport.find_chats(query, limit=8)
        if not matches:
            await message.answer(
                f"Никого похожего на «{query}» не нашёл среди чатов MAX.\n"
                "Попробуй другое написание имени."
            )
            return

        if len(matches) > 1:
            lines = "\n".join(f"• {html.escape(title)}" for _, title in matches)
            await message.answer(
                f"Нашёл несколько — уточни имя, чтобы остался один вариант:\n{lines}"
            )
            return

        chat_id, title = matches[0]
        try:
            topic_id = await self._ensure_topic_by(account, chat_id, title, "dialog")
        except Exception as exc:  # noqa: BLE001
            log.exception("не смог создать тему для /write")
            await message.answer(f"Не смог открыть тему: {exc}")
            return

        await self.bot.send_message(
            chat_id=account.forum_chat_id,
            message_thread_id=topic_id,
            text=(
                f"✍️ Чат с <b>{html.escape(title)}</b> открыт.\n"
                "Пиши сюда — сообщения уйдут в MAX от твоего имени."
            ),
            parse_mode="HTML",
        )
        await message.answer(f"Готово: тема «{title}» создана.")

    async def _cmd_find(self, message: Message) -> None:
        if not await self._guard(message):
            return
        query = _command_arg(message.text)
        if not query:
            await message.answer("Что ищем? Например: /find договор")
            return

        # искать удобнее сразу по всем своим аккаунтам
        blocks: list[str] = []
        for account in self.accounts.owned_by(message.from_user.id):
            rows = account.storage.search(query, limit=10)
            for row in rows:
                who = row["sender_name"] or ("я" if row["outgoing"] else "?")
                chat = row["chat_title"] or row["max_chat_id"]
                prefix = f"[{account.name}] " if self.accounts.multi else ""
                preview = " ".join((row["text"] or "").split())[:90]
                blocks.append(
                    f"{prefix}<b>{html.escape(str(chat))}</b> · {html.escape(who)}\n"
                    f"{html.escape(preview)}"
                )
        await message.answer("\n\n".join(blocks[:15]) or "Ничего не нашлось.", parse_mode="HTML")

    async def _cmd_digest(self, message: Message) -> None:
        if not await self._guard(message):
            return
        # сводка использует AI — тратит бесплатное действие (у premium безлимит)
        if self.ai.enabled and not await self._gate_ai(message):
            return
        blocks = []
        for account in self.accounts.owned_by(message.from_user.id):
            text = await build_digest(account.storage, self.ai, hours=24)
            if text:
                head = f"<b>[{html.escape(account.name)}]</b>\n" if self.accounts.multi else ""
                blocks.append(head + text)
        await message.answer("\n\n".join(blocks) or "За сутки ничего важного.", parse_mode="HTML")

    async def _cmd_rule(self, message: Message) -> None:
        if not await self._guard(message):
            return
        account = await self._need_account(message)
        if account is None:
            return
        payload = _command_arg(message.text)
        if "|" not in payload:
            await message.answer(
                "Формат: /rule слово|действие\n"
                "Действия: urgent, mute, autoreply=текст\n"
                "Пример: /rule счёт|urgent"
            )
            return
        pattern, _, action_raw = payload.partition("|")
        action, _, value = action_raw.strip().partition("=")
        action = action.strip().lower()
        if action not in {"urgent", "mute", "autoreply"}:
            await message.answer("Не знаю такое действие. Есть: urgent, mute, autoreply=текст")
            return
        rule_id = account.storage.add_rule(pattern.strip(), action, value.strip())
        await message.answer(f"Правило #{rule_id} создано.")

    async def _cmd_rules(self, message: Message) -> None:
        if not await self._guard(message):
            return
        account = await self._need_account(message)
        if account is None:
            return
        rows = account.storage.list_rules()
        if not rows:
            await message.answer("Правил пока нет.")
            return
        lines = [
            f"#{row['id']} «{row['pattern']}» → {row['action']}"
            + (f" ({row['payload']})" if row["payload"] else "")
            for row in rows
        ]
        await message.answer("\n".join(lines))

    async def _cmd_rmrule(self, message: Message) -> None:
        if not await self._guard(message):
            return
        account = await self._need_account(message)
        if account is None:
            return
        raw = _command_arg(message.text)
        if not raw.isdigit():
            await message.answer("Формат: /rmrule 3")
            return
        await message.answer("Удалил." if account.storage.delete_rule(int(raw)) else "Такого правила нет.")

    async def _cmd_autoreply(self, message: Message) -> None:
        if not await self._guard(message):
            return
        account = self.accounts.by_forum(message.chat.id)
        if account is None or message.message_thread_id is None:
            await message.answer("Эту команду нужно писать внутри темы нужного чата.")
            return
        chat = account.storage.chat_by_topic(message.message_thread_id)
        if chat is None:
            await message.answer("Не понял, какому чату MAX принадлежит эта тема.")
            return
        text = _command_arg(message.text)
        account.storage.set_chat_flag(int(chat["max_chat_id"]), "autoreply", text)
        await message.answer("Автоответ выключен." if not text else f"Автоответ: {text}")

    # ------------------------------------------------------- ответ из темы
    async def _on_topic_reply(self, message: Message) -> None:
        if not await self._guard(message):
            return
        account = self.accounts.by_forum(message.chat.id)
        if account is None:
            return
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return

        chat = account.storage.chat_by_topic(message.message_thread_id or 0)
        if chat is None:
            await message.reply("Не знаю, в какой чат MAX это отправить.")
            return

        reply_to = ""
        if message.reply_to_message is not None:
            origin = account.storage.max_msg_by_tg(message.reply_to_message.message_id)
            if origin is not None:
                reply_to = origin["max_msg_id"]

        try:
            await account.router.send_to_max(int(chat["max_chat_id"]), text, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001
            log.exception("не смог отправить в MAX")
            await message.reply(f"Не ушло: {exc}")
            return

        # галочка вместо ответного сообщения: не засоряем тему
        try:
            await message.react([ReactionTypeEmoji(emoji="👍")])
        except TelegramBadRequest:
            pass

    async def _on_topic_media(self, message: Message) -> None:
        """Фото или файл, отправленный в тему, уходит в тот же чат MAX."""
        if not await self._guard(message):
            return
        account = self.accounts.by_forum(message.chat.id)
        if account is None:
            return

        chat = account.storage.chat_by_topic(message.message_thread_id or 0)
        if chat is None:
            await message.reply("Не знаю, в какой чат MAX это отправить.")
            return
        await self._forward_media_to_max(message, account, int(chat["max_chat_id"]))

    async def _forward_media_to_max(
        self, message: Message, account: Account, chat_id: int, *, reply_to: str = ""
    ) -> None:
        """Общий путь для медиа из темы и из личной ленты: файл -> в чат MAX."""
        if not account.transport.supports_media:
            await message.reply("Текущий транспорт MAX не умеет отправлять файлы.")
            return

        if message.photo:
            file_id, filename, kind = message.photo[-1].file_id, "image.jpg", "photo"
        elif message.video:
            file_id = message.video.file_id
            filename, kind = message.video.file_name or "video.mp4", "video"
        elif message.voice:
            file_id, filename, kind = message.voice.file_id, "voice.ogg", "file"
        elif message.audio:
            file_id = message.audio.file_id
            filename, kind = message.audio.file_name or "audio.mp3", "file"
        else:
            file_id = message.document.file_id
            filename, kind = message.document.file_name or "file.bin", "file"

        try:
            info = await self.bot.get_file(file_id)
            buffer = await self.bot.download_file(info.file_path)
            data = buffer.read()
        except TelegramBadRequest as exc:
            await message.reply(f"Не смог забрать файл из Telegram: {exc}")
            return

        try:
            await account.router.send_media_to_max(
                chat_id,
                data,
                filename=filename,
                kind=kind,
                caption=(message.caption or "").strip(),
                reply_to=reply_to,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("файл не ушёл в MAX")
            await message.reply(f"Не ушло: {exc}")
            return

        try:
            await message.react([ReactionTypeEmoji(emoji="👍")])
        except TelegramBadRequest:
            pass

    # ------------------------------------------------ ответ из плоской ленты
    def _resolve_by_tg_reply(
        self, owner_id: int, tg_msg_id: int
    ) -> tuple[Account, int, str] | None:
        """По реплаю в личке находит аккаунт, чат MAX и id исходного сообщения.

        Ищем среди всех аккаунтов владельца: у какого в базе есть это
        tg-сообщение (доставленное из MAX). Так плоский режим работает и когда
        аккаунтов несколько.
        """
        for account in self.accounts.owned_by(owner_id):
            row = account.storage.max_msg_by_tg(tg_msg_id)
            if row is not None:
                return account, int(row["max_chat_id"]), str(row["max_msg_id"])
        return None

    async def _on_private_reply(self, message: Message) -> None:
        if not await self._guard(message):
            return
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return
        found = self._resolve_by_tg_reply(
            message.from_user.id, message.reply_to_message.message_id
        )
        if found is None:
            await message.reply(
                "Не понял, какому чату MAX это адресовано. Ответь реплаем на "
                "конкретное сообщение из ленты."
            )
            return
        account, chat_id, reply_to = found
        try:
            await account.router.send_to_max(chat_id, text, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001
            log.exception("не смог отправить в MAX из личной ленты")
            await message.reply(f"Не ушло: {exc}")
            return
        try:
            await message.react([ReactionTypeEmoji(emoji="👍")])
        except TelegramBadRequest:
            pass

    async def _on_private_media(self, message: Message) -> None:
        if not await self._guard(message):
            return
        found = self._resolve_by_tg_reply(
            message.from_user.id, message.reply_to_message.message_id
        )
        if found is None:
            await message.reply(
                "Не понял, какому чату MAX это адресовано. Ответь реплаем на "
                "конкретное сообщение из ленты."
            )
            return
        account, chat_id, reply_to = found
        await self._forward_media_to_max(message, account, chat_id, reply_to=reply_to)

    # ---------------------------------------------------------- кнопки
    async def _on_callback(self, query: CallbackQuery) -> None:
        if not await self._guard(query):
            return
        account = self._account_for(query)
        if account is None:
            await query.answer("Не понял, к какому аккаунту относится кнопка", show_alert=True)
            return

        data = query.data or ""
        action, _, rest = data.partition(":")

        if action == "mute":
            account.storage.set_chat_flag(int(rest), "muted", 1)
            await query.answer("Чат приглушён")
        elif action == "vip":
            account.storage.set_chat_flag(int(rest), "vip", 1)
            await query.answer("Чат помечен важным")
        elif action == "read":
            chat_id, _, msg_id = rest.partition(":")
            try:
                await account.transport.mark_read(int(chat_id), msg_id)
                await query.answer("Отмечено прочитанным в MAX")
            except Exception:  # noqa: BLE001
                await query.answer("Не получилось", show_alert=True)
        elif action == "draft":
            await self._make_drafts(query, account, int(rest))
        elif action == "send":
            await self._send_draft(query, account, rest)
        else:
            await query.answer()

    async def _make_drafts(self, query: CallbackQuery, account: Account, chat_id: int) -> None:
        # черновики — AI-действие: списываем бесплатное (у premium безлимит)
        if self.billing is not None:
            user_id = query.from_user.id
            if not self.billing.can_use_ai(user_id):
                await query.answer(
                    "Бесплатные AI-действия на сегодня кончились. Premium: /premium",
                    show_alert=True,
                )
                return
            self.billing.note_ai_use(user_id)
        await query.answer("Думаю…")
        history = account.router.context_lines(chat_id)
        incoming = history[-1] if history else ""
        drafts = await self.ai.drafts(history, incoming)
        if not drafts:
            await query.answer("Не смог придумать ответ", show_alert=True)
            return

        key = (account.name, chat_id)
        self._drafts[key] = [d.text for d in drafts]
        buttons = [
            [
                InlineKeyboardButton(
                    text=f"▶ {d.tone[:24]}", callback_data=f"send:{chat_id}:{i}"
                )
            ]
            for i, d in enumerate(drafts)
        ]
        body = "\n\n".join(
            f"<b>{html.escape(d.tone)}</b>\n{html.escape(d.text)}" for d in drafts
        )
        if query.message is not None:
            await query.message.reply(
                body,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )

    async def _send_draft(self, query: CallbackQuery, account: Account, rest: str) -> None:
        chat_raw, _, index_raw = rest.partition(":")
        try:
            chat_id, index = int(chat_raw), int(index_raw)
            text = self._drafts[(account.name, chat_id)][index]
        except (ValueError, KeyError, IndexError):
            await query.answer("Черновик потерялся, сгенерируй заново", show_alert=True)
            return
        try:
            await account.router.send_to_max(chat_id, text)
        except Exception as exc:  # noqa: BLE001
            await query.answer(f"Не ушло: {exc}", show_alert=True)
            return
        await query.answer("Отправлено")
        if query.message is not None:
            await query.message.edit_reply_markup(reply_markup=None)

    # --------------------------------------------------------------- запуск
    async def run(self) -> None:
        for account in self.accounts:
            # /bind — авторитетный источник: если группу привязывали командой,
            # сохранённый id главнее значения из .env (там может остаться
            # заглушка из шаблона, из-за которой доставка уходит в никуда).
            stored = account.storage.get("forum_chat_id")
            if stored and int(stored) != account.forum_chat_id:
                self.accounts.rebind(account, int(stored))
                log.info(
                    "аккаунт «%s»: группа-приёмник восстановлена из /bind (%s)",
                    account.name,
                    stored,
                )
        try:
            me = await self.bot.get_me()
        except TelegramNetworkError as exc:
            log.error(
                "Telegram недоступен: %s\n"
                "api.telegram.org не открывается с этой машины. Обычно это "
                "блокировка провайдера или фаервол — проверь командой:\n"
                "    curl -sS -o /dev/null -m 10 -w \"%%{http_code}\\n\" https://api.telegram.org\n"
                "Ответ 200 или 302 — всё в порядке, таймаут — доступа нет.",
                exc,
            )
            raise
        except TelegramUnauthorizedError:
            log.error("Telegram отверг токен: проверь TELEGRAM_BOT_TOKEN в .env")
            raise
        log.info("Telegram-бот @%s запущен, аккаунтов MAX: %d", me.username, len(self.accounts))
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def notify(self, account: Account, text: str, **kwargs: Any) -> None:
        """Личное сообщение владельцу аккаунта (сводки, радар незакрытых)."""
        try:
            await self.bot.send_message(account.owner_id, text, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("не смог написать владельцу аккаунта «%s»", account.name)

    async def close(self) -> None:
        await self.bot.session.close()
