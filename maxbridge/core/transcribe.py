"""Голосовые сообщения → текст.

Работает с любым сервисом, совместимым с форматом OpenAI
`POST /v1/audio/transcriptions` (multipart: file + model). Под него подходят
Whisper API, локальные faster-whisper/whisper.cpp серверы и большинство
российских ASR-шлюзов, включая тот, что уже используется в txt2word.

Ключ и адрес — в .env, в коде их нет. Пусто -> функция просто выключена.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

log = logging.getLogger("maxbridge.transcribe")

#: голосовые длиннее этого расшифровывать дорого и незачем
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class Transcriber:
    def __init__(
        self,
        api_url: str = "",
        api_key: str = "",
        model: str = "whisper-1",
        language: str = "ru",
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.language = language
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_url)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))
        return self._session

    async def transcribe(self, data: bytes, filename: str = "voice.ogg") -> str:
        """Возвращает расшифровку или пустую строку, если не получилось.

        Ошибка расшифровки не должна ломать доставку сообщения — голосовое
        всё равно придёт файлом, просто без текста.
        """
        if not self.enabled or not data:
            return ""
        if len(data) > MAX_AUDIO_BYTES:
            log.info("голосовое %.1f МБ — слишком большое для расшифровки", len(data) / 1024 / 1024)
            return ""

        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename, content_type="audio/ogg")
        form.add_field("model", self.model)
        if self.language:
            form.add_field("language", self.language)

        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            session = await self._http()
            async with session.post(
                f"{self.api_url}/audio/transcriptions", data=form, headers=headers
            ) as response:
                if response.status >= 400:
                    body = (await response.text())[:200]
                    log.warning("ASR вернул HTTP %s: %s", response.status, body)
                    return ""
                payload: Any = await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 - расшифровка необязательна
            log.warning("расшифровка не удалась: %s", exc)
            return ""

        if isinstance(payload, dict):
            return str(payload.get("text") or "").strip()
        return ""
