"""Telegram-сторона моста на aiogram 3.

Главная идея интерфейса: **тема форума = чат MAX**. Владелец просто отвечает
в теме, как в обычной переписке, — текст уходит в тот же чат MAX от его имени.
Никаких команд для ответа не нужно.

Требования к группе: супергруппа с включёнными Темами (Topics), бот — админ
с правом «Управление темами».
"""

from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

from ..config import Config
from ..core.ai import AiAssistant
from ..core.licensing import License
from ..core.router import Router
from ..core.rules import Verdict
from ..db import Storage
from ..models import MaxMessage

log = logging.getLogger("maxbridge.telegram")

PRIORITY_MARK = {"urgent": "🔥", "normal": "", "low": "· "}


class TelegramBridge:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        router: Router,
        ai: AiAssistant,
        license_: License,
    ) -> None:
        self.config = config
        self.storage = storage
        self.router = router
        self.ai = ai
        self.license = license_
        self.bot = Bot(token=config.telegram_token)
        self.dp = Dispatcher()
        self._drafts: dict[int, list[str]] = {}
        self._register()

    # ------------------------------------------------------------- доставка
    async def deliver(self, message: MaxMessage, verdict: Verdict) -> int | None:
        """Кладёт входящее MAX-сообщение в его тему. Возвращает id в Telegram."""
        if not self.config.forum_chat_id:
            log.warning("не задан TELEGRAM_FORUM_CHAT_ID — сообщение некуда положить")
            return None

        topic_id = await self._ensure_topic(message)
        text = self._render(message, verdict)
        keyboard = self._keyboard(message, verdict)

        try:
            sent = await self.bot.send_message(
                chat_id=self.config.forum_chat_id,
                message_thread_id=topic_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_notification=(verdict.priority == "low"),
            )
        except TelegramBadRequest as exc:
            # тему могли удалить руками — заводим заново и пробуем ещё раз
            if "thread not found" in str(exc).lower():
                self.storage.bind_topic(message.chat_id, 0)
                topic_id = await self._ensure_topic(message)
                sent = await self.bot.send_message(
                    chat_id=self.config.forum_chat_id,
                    message_thread_id=topic_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            else:
                raise
        return sent.message_id

    async def _ensure_topic(self, message: MaxMessage) -> int:
        chat = self.storage.get_chat(message.chat_id)
        if chat is not None and chat["tg_topic_id"]:
            return int(chat["tg_topic_id"])

        title = message.chat_title or message.sender_name or f"MAX {message.chat_id}"
        created = await self.bot.create_forum_topic(
            chat_id=self.config.forum_chat_id, name=title[:128]
        )
        self.storage.bind_topic(message.chat_id, created.message_thread_id)
        log.info("создана тема «%s» для чата MAX %s", title, message.chat_id)
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
                parts.append(f'📎 <a href="{html.escape(attachment.url)}">{html.escape(label)}</a>')
            else:
                parts.append(f"📎 {html.escape(label)}")
        if verdict.reason and verdict.priority == "urgent":
            parts.append(f"<i>{html.escape(verdict.reason)}</i>")
        return "\n".join(parts)

    def _keyboard(self, message: MaxMessage, verdict: Verdict) -> InlineKeyboardMarkup | None:
        buttons: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        if self.ai.enabled and verdict.needs_reply:
            row.append(
                InlineKeyboardButton(
                    text="✍️ Черновики", callback_data=f"draft:{message.chat_id}"
                )
            )
        if self.config.stealth_mode:
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

    # ------------------------------------------------------------ обработчики
    def _register(self) -> None:
        dp, owner = self.dp, self.config.owner_id

        def mine(message: Message) -> bool:
            return bool(message.from_user and message.from_user.id == owner)

        dp.message.register(self._cmd_start, CommandStart())
        dp.message.register(self._cmd_bind, Command("bind"))
        dp.message.register(self._cmd_status, Command("status"))
        dp.message.register(self._cmd_chats, Command("chats"))
        dp.message.register(self._cmd_find, Command("find"))
        dp.message.register(self._cmd_digest, Command("digest"))
        dp.message.register(self._cmd_rule, Command("rule"))
        dp.message.register(self._cmd_rules, Command("rules"))
        dp.message.register(self._cmd_rmrule, Command("rmrule"))
        dp.message.register(self._cmd_autoreply, Command("autoreply"))
        dp.message.register(self._cmd_license, Command("license"))
        dp.message.register(self._cmd_help, Command("help"))
        dp.message.register(self._on_topic_reply, F.message_thread_id.is_not(None), F.text)
        dp.callback_query.register(self._on_callback)

    async def _guard(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        if user is not None and user.id == self.config.owner_id:
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
        await message.answer(
            "<b>Что умею</b>\n"
            "/bind — привязать эту группу-форум как приёмник\n"
            "/status — состояние моста и статистика\n"
            "/chats — список чатов MAX и их тем\n"
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
        chat_id = message.chat.id
        self.storage.set("forum_chat_id", str(chat_id))
        self.config.forum_chat_id = chat_id
        await message.answer(
            f"Готово. Эта группа стала приёмником.\n"
            f"Пропиши в .env, чтобы пережить перезапуск:\n"
            f"<code>TELEGRAM_FORUM_CHAT_ID={chat_id}</code>",
            parse_mode="HTML",
        )

    async def _cmd_status(self, message: Message) -> None:
        if not await self._guard(message):
            return
        state = self.router.describe_state()
        coverage = "все сообщения аккаунта" if state["sees_everything"] else "только адресованное боту"
        await message.answer(
            f"<b>Транспорт:</b> {state['transport']} — {coverage}\n"
            f"<b>Стелс:</b> {'да' if state['stealth'] else 'нет'}\n"
            f"<b>AI:</b> {'включён' if state['ai'] else 'выключен'}\n"
            f"<b>Чатов:</b> {state['chats']}  <b>Сообщений:</b> {state['messages']}\n"
            f"<b>Отправлено:</b> {state['sent']}  <b>Ждут ответа:</b> {state['pending']}",
            parse_mode="HTML",
        )

    async def _cmd_license(self, message: Message) -> None:
        if not await self._guard(message):
            return
        await message.answer(self.license.describe())

    async def _cmd_chats(self, message: Message) -> None:
        if not await self._guard(message):
            return
        rows = self.storage.list_chats()
        if not rows:
            await message.answer("Пока ни одного чата — жду первое сообщение из MAX.")
            return
        lines = []
        for row in rows[:50]:
            flags = "".join(["⭐" if row["vip"] else "", "🔕" if row["muted"] else ""])
            lines.append(f"{flags} {row['title'] or row['max_chat_id']}")
        await message.answer("\n".join(lines))

    async def _cmd_find(self, message: Message) -> None:
        if not await self._guard(message):
            return
        query = (message.text or "").partition(" ")[2].strip()
        if not query:
            await message.answer("Что ищем? Например: /find договор")
            return
        rows = self.storage.search(query)
        if not rows:
            await message.answer("Ничего не нашлось.")
            return
        lines = []
        for row in rows[:15]:
            who = row["sender_name"] or ("я" if row["outgoing"] else "?")
            chat = row["chat_title"] or row["max_chat_id"]
            preview = " ".join((row["text"] or "").split())[:90]
            lines.append(f"<b>{html.escape(str(chat))}</b> · {html.escape(who)}\n{html.escape(preview)}")
        await message.answer("\n\n".join(lines), parse_mode="HTML")

    async def _cmd_digest(self, message: Message) -> None:
        if not await self._guard(message):
            return
        from ..core.digest import build_digest

        text = await build_digest(self.storage, self.ai, hours=24)
        await message.answer(text or "За сутки ничего важного.", parse_mode="HTML")

    async def _cmd_rule(self, message: Message) -> None:
        if not await self._guard(message):
            return
        payload = (message.text or "").partition(" ")[2].strip()
        if "|" not in payload:
            await message.answer(
                "Формат: /rule слово|действие\n"
                "Действия: urgent, mute, autoreply=текст\n"
                "Пример: /rule счёт|urgent"
            )
            return
        pattern, _, action_raw = payload.partition("|")
        action_raw = action_raw.strip()
        action, _, value = action_raw.partition("=")
        action = action.strip().lower()
        if action not in {"urgent", "mute", "autoreply"}:
            await message.answer("Не знаю такое действие. Есть: urgent, mute, autoreply=текст")
            return
        rule_id = self.storage.add_rule(pattern.strip(), action, value.strip())
        await message.answer(f"Правило #{rule_id} создано.")

    async def _cmd_rules(self, message: Message) -> None:
        if not await self._guard(message):
            return
        rows = self.storage.list_rules()
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
        raw = (message.text or "").partition(" ")[2].strip()
        if not raw.isdigit():
            await message.answer("Формат: /rmrule 3")
            return
        ok = self.storage.delete_rule(int(raw))
        await message.answer("Удалил." if ok else "Такого правила нет.")

    async def _cmd_autoreply(self, message: Message) -> None:
        if not await self._guard(message):
            return
        if message.message_thread_id is None:
            await message.answer("Эту команду нужно писать внутри темы нужного чата.")
            return
        chat = self.storage.chat_by_topic(message.message_thread_id)
        if chat is None:
            await message.answer("Не понял, какому чату MAX принадлежит эта тема.")
            return
        text = (message.text or "").partition(" ")[2].strip()
        self.storage.set_chat_flag(int(chat["max_chat_id"]), "autoreply", text)
        await message.answer("Автоответ выключен." if not text else f"Автоответ: {text}")

    # ------------------------------------------------------- ответ из темы
    async def _on_topic_reply(self, message: Message) -> None:
        if not await self._guard(message):
            return
        if message.chat.id != self.config.forum_chat_id:
            return
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return

        chat = self.storage.chat_by_topic(message.message_thread_id or 0)
        if chat is None:
            await message.reply("Не знаю, в какой чат MAX это отправить.")
            return

        reply_to = ""
        if message.reply_to_message is not None:
            origin = self.storage.max_msg_by_tg(message.reply_to_message.message_id)
            if origin is not None:
                reply_to = origin["max_msg_id"]

        try:
            await self.router.send_to_max(int(chat["max_chat_id"]), text, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001
            log.exception("не смог отправить в MAX")
            await message.reply(f"Не ушло: {exc}")
            return

        # галочка вместо ответного сообщения: не засоряем тему
        try:
            await message.react([ReactionTypeEmoji(emoji="👍")])
        except TelegramBadRequest:
            pass

    # ---------------------------------------------------------- кнопки
    async def _on_callback(self, query: CallbackQuery) -> None:
        if not await self._guard(query):
            return
        data = query.data or ""
        action, _, rest = data.partition(":")

        if action == "mute":
            self.storage.set_chat_flag(int(rest), "muted", 1)
            await query.answer("Чат приглушён")
        elif action == "vip":
            self.storage.set_chat_flag(int(rest), "vip", 1)
            await query.answer("Чат помечен важным")
        elif action == "read":
            chat_id, _, msg_id = rest.partition(":")
            try:
                await self.router.transport.mark_read(int(chat_id), msg_id)
                await query.answer("Отмечено прочитанным в MAX")
            except Exception:  # noqa: BLE001
                await query.answer("Не получилось", show_alert=True)
        elif action == "draft":
            await self._make_drafts(query, int(rest))
        elif action == "send":
            await self._send_draft(query, rest)
        else:
            await query.answer()

    async def _make_drafts(self, query: CallbackQuery, chat_id: int) -> None:
        await query.answer("Думаю…")
        history = self.router.context_lines(chat_id)
        incoming = history[-1] if history else ""
        drafts = await self.ai.drafts(history, incoming)
        if not drafts:
            await query.answer("Не смог придумать ответ", show_alert=True)
            return

        key = chat_id
        self._drafts[key] = [d.text for d in drafts]
        buttons = [
            [InlineKeyboardButton(text=f"▶ {d.tone[:24]}", callback_data=f"send:{key}:{i}")]
            for i, d in enumerate(drafts)
        ]
        body = "\n\n".join(
            f"<b>{html.escape(d.tone)}</b>\n{html.escape(d.text)}" for d in drafts
        )
        if query.message is not None:
            await query.message.reply(
                body, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )

    async def _send_draft(self, query: CallbackQuery, rest: str) -> None:
        chat_raw, _, index_raw = rest.partition(":")
        try:
            chat_id, index = int(chat_raw), int(index_raw)
            text = self._drafts[chat_id][index]
        except (ValueError, KeyError, IndexError):
            await query.answer("Черновик потерялся, сгенерируй заново", show_alert=True)
            return
        try:
            await self.router.send_to_max(chat_id, text)
        except Exception as exc:  # noqa: BLE001
            await query.answer(f"Не ушло: {exc}", show_alert=True)
            return
        await query.answer("Отправлено")
        if query.message is not None:
            await query.message.edit_reply_markup(reply_markup=None)

    # --------------------------------------------------------------- запуск
    async def run(self) -> None:
        stored = self.storage.get("forum_chat_id")
        if stored and not self.config.forum_chat_id:
            self.config.forum_chat_id = int(stored)
        me = await self.bot.get_me()
        log.info("Telegram-бот @%s запущен", me.username)
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def notify_owner(self, text: str, **kwargs: Any) -> None:
        try:
            await self.bot.send_message(self.config.owner_id, text, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("не смог написать владельцу")

    async def close(self) -> None:
        await self.bot.session.close()
