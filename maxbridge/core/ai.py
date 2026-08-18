"""AI-слой на Claude: разбор смысла, сводки, черновики ответов, перевод.

Принципы, заложенные в дизайн:
  * AI — необязательный. Нет ключа Anthropic — мост работает как обычный релей,
    решения принимают правила из rules.py.
  * AI не блокирует доставку. Сообщение сначала уходит в Telegram, разбор
    приходит следом. Любая ошибка модели просто логируется.
  * Классификация идёт на низком effort — это дешёвая массовая операция.
    Сводки и черновики просят больше усилий, их немного.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Sequence

log = logging.getLogger("maxbridge.ai")

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "priority": {"type": "string", "enum": ["urgent", "normal", "low"]},
        "intent": {
            "type": "string",
            "enum": ["question", "task", "money", "meeting", "info", "noise"],
        },
        "needs_reply": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["priority", "intent", "needs_reply", "reason"],
    "additionalProperties": False,
}

DRAFTS_SCHEMA = {
    "type": "object",
    "properties": {
        "drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tone": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["tone", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["drafts"],
    "additionalProperties": False,
}

CLASSIFY_SYSTEM = (
    "Ты — фильтр входящих сообщений мессенджера для занятого человека. "
    "По одному сообщению определи, насколько срочно владельцу на него реагировать.\n"
    "urgent — есть дедлайн, деньги, авария, прямой вопрос от важного человека.\n"
    "normal — обычная рабочая переписка.\n"
    "low — болтовня, поздравления, реакции, рассылки.\n"
    "Поле reason — не больше 8 слов, по-русски, объясняет решение."
)

DRAFTS_SYSTEM = (
    "Ты пишешь черновики ответов от лица владельца переписки. "
    "Дай ровно три варианта разного тона: короткий деловой, тёплый развёрнутый, "
    "вежливый отказ или перенос. Пиши на языке собеседника, "
    "от первого лица, без приветствий-заглушек и без подписи. "
    "Каждый вариант — готовый к отправке текст, не длиннее 4 предложений."
)

DIGEST_SYSTEM = (
    "Ты составляешь сводку пропущенных сообщений. "
    "Сгруппируй по чатам, для каждого — 1-2 строки сути. "
    "Отдельным блоком в конце перечисли, что ждут лично от владельца, "
    "в виде списка коротких действий. Без вступлений и без воды."
)


@dataclass(slots=True)
class AiVerdict:
    priority: str
    intent: str
    needs_reply: bool
    reason: str


@dataclass(slots=True)
class Draft:
    tone: str
    text: str


class AiAssistant:
    def __init__(self, api_key: str, model: str = "claude-opus-5", lang: str = "ru") -> None:
        self.model = model
        self.lang = lang
        self._client: Any = None
        if api_key:
            try:
                from anthropic import AsyncAnthropic

                self._client = AsyncAnthropic(api_key=api_key)
            except ImportError:
                log.warning("пакет anthropic не установлен — AI-функции выключены")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------- внутреннее
    async def _ask_json(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        *,
        effort: str = "low",
        max_tokens: int = 1024,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - AI не должен ронять мост
            log.warning("Claude не ответил: %s", exc)
            return None

        if getattr(response, "stop_reason", "") == "refusal":
            log.info("Claude отказался обрабатывать сообщение")
            return None

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("Claude вернул не-JSON вопреки схеме")
            return None

    async def _ask_text(
        self, system: str, prompt: str, *, effort: str = "medium", max_tokens: int = 4000
    ) -> str:
        if not self.enabled:
            return ""
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                output_config={"effort": effort},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Claude не ответил: %s", exc)
            return ""
        if getattr(response, "stop_reason", "") == "refusal":
            return ""
        return "".join(b.text for b in response.content if b.type == "text").strip()

    # ---------------------------------------------------------------- методы
    async def classify(self, text: str, *, chat: str, sender: str) -> AiVerdict | None:
        """Уточняет приоритет там, где правила дали неопределённый ответ."""
        if not text.strip():
            return None
        prompt = f"Чат: {chat or 'без названия'}\nОт: {sender or 'неизвестно'}\nТекст:\n{text[:2000]}"
        data = await self._ask_json(CLASSIFY_SYSTEM, prompt, CLASSIFY_SCHEMA)
        if not data:
            return None
        return AiVerdict(
            priority=str(data.get("priority", "normal")),
            intent=str(data.get("intent", "")),
            needs_reply=bool(data.get("needs_reply")),
            reason=str(data.get("reason", "")),
        )

    async def drafts(self, history: Sequence[str], incoming: str) -> list[Draft]:
        """Три варианта ответа с учётом последних реплик чата."""
        context = "\n".join(history[-15:])
        prompt = (
            f"Последние сообщения чата:\n{context}\n\n"
            f"Нужно ответить на:\n{incoming[:1500]}"
        )
        data = await self._ask_json(
            DRAFTS_SYSTEM, prompt, DRAFTS_SCHEMA, effort="medium", max_tokens=2000
        )
        if not data:
            return []
        return [
            Draft(tone=str(item.get("tone", "")), text=str(item.get("text", "")))
            for item in data.get("drafts", [])
            if item.get("text")
        ]

    async def digest(self, lines: Sequence[str]) -> str:
        """Сводка «что я пропустил» по накопленным сообщениям."""
        if not lines:
            return ""
        body = "\n".join(lines[:400])
        return await self._ask_text(
            DIGEST_SYSTEM, f"Пропущенные сообщения:\n{body}", effort="medium"
        )

    async def translate(self, text: str, target_lang: str = "") -> str:
        target = target_lang or self.lang
        return await self._ask_text(
            f"Переведи текст на язык '{target}'. Верни только перевод, без пояснений.",
            text[:4000],
            effort="low",
            max_tokens=2000,
        )
