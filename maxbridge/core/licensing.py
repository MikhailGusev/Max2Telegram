"""Лицензирование: какие функции включены у этой установки.

Модель распространения (см. docs/MONETIZATION.md):

  Community — бесплатно и без ключа. Мост целиком: все чаты MAX в темах
              Telegram, ответы от своего имени, правила, поиск, дайджест.
              AI-функции работают на твоём собственном ключе Anthropic.

  Pro/Team  — платный ключ включает каналы эскалации (SMS, WhatsApp),
              мультиаккаунт, вебхуки и выгрузку в CRM.

Ключ проверяется ОФФЛАЙН по схеме Ed25519: в коде лежит только публичный ключ
вендора, приватный не покидает машину владельца продукта. Подделать ключ,
имея исходники, нельзя — можно лишь выпилить проверку, но это уже нарушение
AGPL-условий распространения, а не техническая дыра.

Формат ключа:  MB1.<base64url(payload_json)>.<base64url(signature)>
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("maxbridge.license")

#: публичный ключ вендора (base64). Пустая строка -> проверка подписи выключена,
#: в этом режиме принимается только Community. Заполняется при сборке релиза.
VENDOR_PUBLIC_KEY = ""

#: функции, доступные только по платному ключу
PAID_FEATURES = frozenset({"sms", "whatsapp", "multiaccount", "webhooks", "crm"})

#: что входит в бесплатный режим
COMMUNITY_FEATURES = frozenset({"bridge", "rules", "search", "digest", "ai", "followup"})

PREFIX = "MB1"


@dataclass(slots=True)
class License:
    plan: str = "community"
    customer: str = ""
    expires_at: int = 0                       # 0 = бессрочно
    features: frozenset[str] = field(default_factory=lambda: COMMUNITY_FEATURES)
    seats: int = 1
    error: str = ""

    @property
    def valid(self) -> bool:
        return not self.error

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and self.expires_at < int(time.time())

    def allows(self, feature: str) -> bool:
        if self.expired:
            return feature in COMMUNITY_FEATURES
        return feature in self.features

    def describe(self) -> str:
        if self.error:
            return f"лицензия не принята: {self.error} (работаю в режиме Community)"
        if self.plan == "community":
            return "Community — мост, правила, поиск, дайджест, AI на своём ключе"
        until = (
            time.strftime("%d.%m.%Y", time.localtime(self.expires_at))
            if self.expires_at
            else "бессрочно"
        )
        extra = ", ".join(sorted(self.features - COMMUNITY_FEATURES)) or "—"
        return f"{self.plan.upper()} для «{self.customer}» до {until}; платные функции: {extra}"


def _b64url_decode(chunk: str) -> bytes:
    padding = "=" * (-len(chunk) % 4)
    return base64.urlsafe_b64decode(chunk + padding)


def load_license(key: str) -> License:
    """Разбирает и проверяет ключ. Любая ошибка -> тихий откат в Community."""
    key = (key or "").strip()
    if not key:
        return License()

    parts = key.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        return License(error="неверный формат ключа")

    try:
        payload_raw = _b64url_decode(parts[1])
        signature = _b64url_decode(parts[2])
        payload = json.loads(payload_raw)
    except Exception:  # noqa: BLE001
        return License(error="ключ повреждён")

    if not VENDOR_PUBLIC_KEY:
        return License(error="в этой сборке нет ключа вендора")

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return License(error="не установлен пакет cryptography")

    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(VENDOR_PUBLIC_KEY))
        public_key.verify(signature, payload_raw)
    except InvalidSignature:
        return License(error="подпись не совпала")
    except Exception as exc:  # noqa: BLE001
        return License(error=f"не смог проверить подпись: {exc}")

    features = frozenset(payload.get("feats") or []) | COMMUNITY_FEATURES
    license_ = License(
        plan=str(payload.get("plan") or "pro"),
        customer=str(payload.get("sub") or ""),
        expires_at=int(payload.get("exp") or 0),
        features=features,
        seats=int(payload.get("seats") or 1),
    )
    if license_.expired:
        log.warning("срок лицензии истёк — платные функции отключены")
    return license_
