"""AI-слой: приоритеты, черновики ответов, сводки, перевод.

Работает с любым OpenAI-совместимым API (эндпоинт /chat/completions):
Qwen (Aliyun DashScope), DeepSeek, OpenAI и прочие. Провайдер по умолчанию —
Qwen: дёшево и хорошо по-русски. Anthropic намеренно не тащим — дорого для
массовой платной функции.

Принципы:
  * AI необязателен. Нет ключа — мост работает как релей, приоритеты считают
    правила из rules.py.
  * AI не блокирует доставку: сообщение уходит в Telegram сразу, разбор следом.
    Любая ошибка модели просто логируется.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

import aiohttp

log = logging.getLogger("maxbridge.ai")

#: провайдер -> (базовый URL до /chat/completions, модель по умолчанию)
PROVIDERS: dict[str, tuple[str, str]] = {
    "qwen": ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "deepseek": ("https://api.deepseek.com", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}

CLASSIFY_SYSTEM = (
    "Ты — фильтр входящих сообщений мессенджера для занятого человека. "
    "По одному сообщению определи, насколько срочно владельцу реагировать.\n"
    "urgent — дедлайн, деньги, авария, прямой вопрос от важного человека.\n"
    "normal — обычная рабочая переписка.\n"
    "low — болтовня, поздравления, реакции, рассылки.\n"
    "Верни СТРОГО JSON без пояснений вида: "
    '{"priority":"urgent|normal|low",'
    '"intent":"question|task|money|meeting|info|noise",'
    '"needs_reply":true|false,"reason":"до 8 слов по-русски"}'
)

DRAFTS_SYSTEM = (
    "Ты пишешь черновики ответов от лица владельца переписки. "
    "Дай ровно три варианта разного тона: короткий деловой, тёплый развёрнутый, "
    "вежливый отказ или перенос. Пиши на языке собеседника, от первого лица, "
    "без приветствий-заглушек и подписи, каждый не длиннее 4 предложений.\n"
    'Верни СТРОГО JSON: {"drafts":[{"tone":"...","text":"..."},'
    '{"tone":"...","text":"..."},{"tone":"...","text":"..."}]}'
)

DIGEST_SYSTEM = (
    "Ты составляешь сводку пропущенных сообщений. Сгруппируй по чатам, для "
    "каждого 1-2 строки сути. Отдельным блоком в конце перечисли, что ждут "
    "лично от владельца, списком коротких действий. Без вступлений и воды."
)


def _hide_credentials(url: str) -> str:
    """Прокси часто задают как http://user:pass@host — пароль в лог не пускаем."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.rpartition('@')[2]}" if rest else url


def _extract_json(text: str) -> dict[str, Any] | None:
    """JSON из ответа модели — целиком или вырезая {...}, если модель добавила
    пояснения вокруг вопреки инструкции."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


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
    def __init__(
        self,
        api_key: str,
        model: str = "",
        lang: str = "ru",
        *,
        provider: str = "qwen",
        base_url: str = "",
        proxy: str = "",
    ) -> None:
        self.provider = (provider or "qwen").strip().lower()
        default_base, default_model = PROVIDERS.get(self.provider, PROVIDERS["qwen"])
        self.api_key = api_key
        self.base_url = (base_url or default_base).rstrip("/")
        self.model = model or default_model
        self.lang = lang
        self.proxy = proxy or None
        self._session: aiohttp.ClientSession | None = None
        if api_key and proxy:
            log.info("AI (%s) ходит через прокси %s", self.provider, _hide_credentials(proxy))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._session

    async def _chat(
        self,
        system: str,
        prompt: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> str:
        if not self.enabled:
            return ""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            session = await self._http()
            async with session.post(
                f"{self.base_url}/chat/completions", json=payload, proxy=self.proxy
            ) as response:
                if response.status >= 400:
                    body = (await response.text())[:200]
                    log.warning("AI %s вернул HTTP %s: %s", self.provider, response.status, body)
                    return ""
                data = await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 - AI не должен ронять мост
            log.warning("AI %s не ответил: %s", self.provider, exc)
            return ""
        try:
            return str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            log.warning("AI %s вернул неожиданный формат ответа", self.provider)
            return ""

    # ---------------------------------------------------------------- методы
    async def classify(self, text: str, *, chat: str, sender: str) -> AiVerdict | None:
        """Уточняет приоритет там, где правила дали неопределённый ответ."""
        if not text.strip():
            return None
        prompt = f"Чат: {chat or 'без названия'}\nОт: {sender or 'неизвестно'}\nТекст:\n{text[:2000]}"
        raw = await self._chat(
            CLASSIFY_SYSTEM, prompt, temperature=0.0, max_tokens=300, json_mode=True
        )
        data = _extract_json(raw) if raw else None
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
        prompt = f"Последние сообщения чата:\n{context}\n\nНужно ответить на:\n{incoming[:1500]}"
        raw = await self._chat(
            DRAFTS_SYSTEM, prompt, temperature=0.7, max_tokens=1200, json_mode=True
        )
        data = _extract_json(raw) if raw else None
        if not data:
            return []
        return [
            Draft(tone=str(item.get("tone", "")), text=str(item.get("text", "")))
            for item in data.get("drafts", [])
            if isinstance(item, dict) and item.get("text")
        ]

    async def digest(self, lines: Sequence[str]) -> str:
        """Сводка «что я пропустил» по накопленным сообщениям."""
        if not lines:
            return ""
        body = "\n".join(lines[:400])
        return await self._chat(
            DIGEST_SYSTEM, f"Пропущенные сообщения:\n{body}", temperature=0.3, max_tokens=1500
        )

    async def translate(self, text: str, target_lang: str = "") -> str:
        target = target_lang or self.lang
        return await self._chat(
            f"Переведи текст на язык '{target}'. Верни только перевод, без пояснений.",
            text[:4000],
            temperature=0.0,
            max_tokens=2000,
        )
