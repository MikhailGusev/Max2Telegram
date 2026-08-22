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

import html
import logging
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
    ) -> None:
        self.config = config
        self.accounts = accounts
        self.ai = ai
        self.license = license_
        self.transcriber = transcriber or Transcriber()
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
        self._register()

        for account in self.accounts:
            account.router.attach_telegram(AccountSink(self, account))

    # ------------------------------------------------------------- доставка
    async def deliver(
        self, account: Account, message: MaxMessage, verdict: Verdict
    ) -> int | None:
        """Кладёт входящее MAX-сообщение в тему его чата."""
        if not account.forum_chat_id:
            log.warning(
                "аккаунт «%s»: не задана группа-приёмник, сообщение некуда положить. "
                "Выполни /bind в нужной группе",
                account.name,
            )
            return None

        topic_id = await self._ensure_topic(account, message)
        text = self._render(message, verdict)
        keyboard = self._keyboard(account, message, verdict)

        if message.attachments and account.transport.supports_media:
            sent_id = await self._deliver_media(account, message, topic_id, text, keyboard)
            if sent_id is not None:
                return sent_id
            # не получилось перетащить файл — ниже уйдёт текст со ссылкой

        try:
            sent = await self.bot.send_message(
                chat_id=account.forum_chat_id,
                message_thread_id=topic_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_notification=(verdict.priority == "low"),
            )
        except TelegramBadRequest as exc:
            # тему могли удалить руками — заводим заново и пробуем ещё раз
            if "thread not found" in str(exc).lower():
                account.storage.bind_topic(message.chat_id, 0)
                topic_id = await self._ensure_topic(account, message)
                sent = await self.bot.send_message(
                    chat_id=account.forum_chat_id,
                    message_thread_id=topic_id,
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
        topic_id: int,
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
            "chat_id": account.forum_chat_id,
            "message_thread_id": topic_id,
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
                chat_id=account.forum_chat_id,
                message_thread_id=topic_id,
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

    def _render(self, message: MaxMessage, verdict: Verdict) -> str:
        mark = PRIORITY_MARK.get(verdict.priority, "")
        who = html.escape(message.sender_name or "неизвестный")
        body = html.escape(message.text or "")
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
        dp.message.register(self._cmd_help, Command("help"))
        dp.message.register(self._on_topic_reply, F.message_thread_id.is_not(None), F.text)
        dp.message.register(
            self._on_topic_media,
            F.message_thread_id.is_not(None),
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
        if not await self._guard(message):
            return
        await message.answer(
            "MaxBridge на связи.\n\n"
            "Каждый чат MAX появится здесь отдельной темой. "
            "Отвечай прямо в теме — текст уйдёт в MAX от твоего имени.\n\n"
            "Команды: /help"
        )

    async def _cmd_help(self, message: Message) -> None:
        if not await self._guard(message):
            return
        extra = "/accounts — список аккаунтов MAX\n" if self.accounts.multi else ""
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
            "/license — что включено в этой установке",
            parse_mode="HTML",
        )

    async def _cmd_bind(self, message: Message) -> None:
        if not await self._guard(message):
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
        await message.answer("\n\n".join(blocks), parse_mode="HTML")

    async def _cmd_license(self, message: Message) -> None:
        if not await self._guard(message):
            return
        await message.answer(self.license.describe())

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
                int(chat["max_chat_id"]),
                data,
                filename=filename,
                kind=kind,
                caption=(message.caption or "").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("файл не ушёл в MAX")
            await message.reply(f"Не ушло: {exc}")
            return

        try:
            await message.react([ReactionTypeEmoji(emoji="👍")])
        except TelegramBadRequest:
            pass

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
            if not account.forum_chat_id:
                stored = account.storage.get("forum_chat_id")
                if stored:
                    self.accounts.rebind(account, int(stored))
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
