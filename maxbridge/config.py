"""Конфигурация из .env / переменных окружения. Секретов в коде нет."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _csv(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


@dataclass(slots=True)
class Config:
    telegram_token: str
    owner_id: int
    forum_chat_id: int
    #: прокси для Telegram. Нужен там, где провайдер режет api.telegram.org
    #: (частый случай на российских хостингах). MAX всё равно идёт напрямую.
    telegram_proxy: str

    max_mode: str  # userbot | botapi
    max_phone: str
    max_bot_token: str
    max_api_base: str

    ai_provider: str  # qwen | deepseek | openai
    anthropic_key: str  # общий AI-ключ (имя историческое)
    ai_model: str
    ai_enabled: bool
    ai_lang: str
    #: прокси только для Anthropic. MAX и Telegram ходят напрямую, чтобы
    #: сессия MAX не выглядела приходящей из другой страны
    ai_proxy: str
    ai_base_url: str

    stealth_mode: bool
    digest_hour: int
    followup_minutes: int
    db_path: Path
    log_level: str
    license_key: str

    # --- подписка и оплата ---
    free_ai_per_day: int
    premium_price: int
    yoomoney_wallet: str
    site_url: str

    # --- платные каналы эскалации (Pro) ---
    escalate_after_minutes: int
    escalate_channels: tuple[str, ...]

    sms_provider: str
    sms_api_key: str
    sms_login: str
    sms_password: str
    sms_from: str
    sms_to: str

    whatsapp_provider: str
    whatsapp_token: str
    whatsapp_phone_id: str
    whatsapp_to: str
    whatsapp_template: str
    waha_url: str
    waha_session: str

    wa_webhook_enabled: bool
    wa_webhook_host: str
    wa_webhook_port: int
    wa_verify_token: str

    asr_url: str
    asr_key: str
    asr_model: str
    asr_lang: str

    # --- SaaS-онбординг клиентов через QR в боте ---
    #: принимать ли новых клиентов по /start (по умолчанию выкл — чтобы бот не
    #: начал поднимать чужие userbot-сессии с IP сервера без ведома владельца)
    onboarding_enabled: bool = False
    #: потолок активных клиентов на сервер (0 = без лимита). Митигация бана по IP.
    max_clients: int = 0

    #: явный путь к сессии MAX. Пусто -> рядом с базой. Мультиаккаунт задаёт
    #: своё значение каждому аккаунту, иначе они затрут сессии друг друга.
    session_file: str = ""

    session_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.session_path = (
            Path(self.session_file)
            if self.session_file
            else self.db_path.parent / "max_session.json"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def ai_active(self) -> bool:
        return self.ai_enabled and bool(self.anthropic_key)

    def validate(self) -> list[str]:
        """Возвращает список проблем конфигурации (пустой = всё ок)."""
        problems: list[str] = []
        if not self.telegram_token:
            problems.append("TELEGRAM_BOT_TOKEN не задан")
        if not self.owner_id:
            problems.append("TELEGRAM_OWNER_ID не задан")
        if self.max_mode not in {"userbot", "botapi"}:
            problems.append(f"MAX_MODE='{self.max_mode}': ожидается userbot или botapi")
        if self.max_mode == "botapi" and not self.max_bot_token:
            problems.append("MAX_MODE=botapi, но MAX_BOT_TOKEN не задан")
        return problems


def load_config(env_file: str | os.PathLike[str] | None = None) -> Config:
    load_dotenv(env_file or ROOT / ".env", override=False)
    return Config(
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        owner_id=_int("TELEGRAM_OWNER_ID", 0),
        forum_chat_id=_int("TELEGRAM_FORUM_CHAT_ID", 0),
        telegram_proxy=os.getenv("TELEGRAM_PROXY", "").strip(),
        max_mode=os.getenv("MAX_MODE", "userbot").strip().lower(),
        max_phone=os.getenv("MAX_PHONE", "").strip(),
        max_bot_token=os.getenv("MAX_BOT_TOKEN", "").strip(),
        max_api_base=os.getenv("MAX_API_BASE", "https://platform-api.max.ru").strip().rstrip("/"),
        ai_provider=os.getenv("AI_PROVIDER", "qwen").strip().lower(),
        # AI_KEY — основной; ANTHROPIC_API_KEY принимается для совместимости
        anthropic_key=(os.getenv("AI_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")).strip(),
        ai_model=os.getenv("AI_MODEL", "").strip(),
        ai_enabled=_bool("AI_ENABLED", True),
        ai_lang=os.getenv("AI_LANG", "ru").strip(),
        ai_proxy=os.getenv("AI_PROXY", "").strip(),
        ai_base_url=os.getenv("AI_BASE_URL", "").strip(),
        stealth_mode=_bool("STEALTH_MODE", True),
        digest_hour=_int("DIGEST_HOUR", 9),
        followup_minutes=_int("FOLLOWUP_MINUTES", 180),
        db_path=Path(os.getenv("DB_PATH", "data/maxbridge.db")),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        license_key=os.getenv("MAXBRIDGE_LICENSE_KEY", "").strip(),
        onboarding_enabled=_bool("ONBOARDING_ENABLED", False),
        max_clients=_int("MAX_CLIENTS", 0),
        free_ai_per_day=_int("FREE_AI_PER_DAY", 3),
        premium_price=_int("PREMIUM_PRICE", 500),
        yoomoney_wallet=os.getenv("YOOMONEY_WALLET", "").strip(),
        site_url=os.getenv("SITE_URL", "").strip().rstrip("/"),
        escalate_after_minutes=_int("ESCALATE_AFTER_MINUTES", 0),
        escalate_channels=_csv("ESCALATE_CHANNELS"),
        sms_provider=os.getenv("SMS_PROVIDER", "").strip().lower(),
        sms_api_key=os.getenv("SMS_API_KEY", "").strip(),
        sms_login=os.getenv("SMS_LOGIN", "").strip(),
        sms_password=os.getenv("SMS_PASSWORD", "").strip(),
        sms_from=os.getenv("SMS_FROM", "").strip(),
        sms_to=os.getenv("SMS_TO", "").strip(),
        whatsapp_provider=os.getenv("WHATSAPP_PROVIDER", "").strip().lower(),
        whatsapp_token=os.getenv("WHATSAPP_TOKEN", "").strip(),
        whatsapp_phone_id=os.getenv("WHATSAPP_PHONE_ID", "").strip(),
        whatsapp_to=os.getenv("WHATSAPP_TO", "").strip(),
        whatsapp_template=os.getenv("WHATSAPP_TEMPLATE", "").strip(),
        waha_url=os.getenv("WAHA_URL", "").strip(),
        waha_session=os.getenv("WAHA_SESSION", "default").strip(),
        wa_webhook_enabled=_bool("WA_WEBHOOK_ENABLED", False),
        wa_webhook_host=os.getenv("WA_WEBHOOK_HOST", "127.0.0.1").strip(),
        wa_webhook_port=_int("WA_WEBHOOK_PORT", 8081),
        wa_verify_token=os.getenv("WA_VERIFY_TOKEN", "").strip(),
        asr_url=os.getenv("ASR_URL", "").strip(),
        asr_key=os.getenv("ASR_KEY", "").strip(),
        asr_model=os.getenv("ASR_MODEL", "whisper-1").strip(),
        asr_lang=os.getenv("ASR_LANG", "ru").strip(),
        session_file=os.getenv("MAX_SESSION_FILE", "").strip(),
    )
