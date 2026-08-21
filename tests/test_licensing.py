"""Лицензии и подключение каналов.

Сами каналы (SMS, WhatsApp) живут в отдельном пакете, поэтому здесь
проверяется только то, что относится к ядру: разбор ключа, набор бесплатных
функций и то, что без пакета с каналами мост спокойно работает без них.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.channels import build_channels  # noqa: E402
from maxbridge.channels.base import NotifyChannel  # noqa: E402
from maxbridge.config import load_config  # noqa: E402
from maxbridge.core.licensing import COMMUNITY_FEATURES, load_license  # noqa: E402


def test_no_key_means_community() -> None:
    license_ = load_license("")
    assert license_.plan == "community"
    assert license_.valid
    assert license_.allows("bridge")
    assert license_.allows("ai")
    assert not license_.allows("sms")
    assert not license_.allows("whatsapp")


def test_garbage_key_degrades_quietly() -> None:
    license_ = load_license("что-то-не-то")
    assert not license_.valid
    assert license_.allows("bridge")      # мост продолжает работать
    assert not license_.allows("sms")     # платное закрыто
    assert "Community" in license_.describe()


def test_community_feature_set_is_stable() -> None:
    assert {"bridge", "rules", "search", "digest"} <= COMMUNITY_FEATURES
    assert "sms" not in COMMUNITY_FEATURES
    assert "multiaccount" not in COMMUNITY_FEATURES


def test_expired_license_falls_back_to_community() -> None:
    from maxbridge.core.licensing import License

    expired = License(
        plan="pro",
        customer="Тест",
        expires_at=1,  # 1970 год
        features=frozenset(COMMUNITY_FEATURES | {"sms"}),
    )
    assert expired.expired
    assert expired.allows("bridge") and not expired.allows("sms")


def test_bridge_works_without_channel_package(tmp_path: Path) -> None:
    """Пакета с каналами нет — список пуст, и это не ошибка."""
    config = load_config(tmp_path / "нет.env")
    config.db_path = tmp_path / "t.db"
    assert build_channels(config) == []


def test_trim_keeps_message_readable() -> None:
    long_text = "слово " * 100
    trimmed = NotifyChannel.trim(long_text, 40)
    assert len(trimmed) <= 40
    assert trimmed.endswith("…")


def test_trim_leaves_short_text_alone() -> None:
    assert NotifyChannel.trim("коротко", 40) == "коротко"


def test_proxy_credentials_are_hidden_in_logs() -> None:
    """Прокси задают как http://user:pass@host — пароль не должен светиться."""
    from maxbridge.core.ai import _hide_credentials

    assert _hide_credentials("http://vasya:secret@1.2.3.4:3128") == "http://***@1.2.3.4:3128"
    assert "secret" not in _hide_credentials("http://vasya:secret@1.2.3.4:3128")
    assert _hide_credentials("http://1.2.3.4:3128") == "http://1.2.3.4:3128"


def test_ai_stays_off_without_key_even_with_proxy() -> None:
    from maxbridge.core.ai import AiAssistant

    assert not AiAssistant("", proxy="http://1.2.3.4:3128").enabled


def test_token_extraction_handles_what_people_actually_paste() -> None:
    """Из браузера копируют по-разному — вырезать руками не должно требоваться."""
    from maxbridge.cli.login import _extract_token

    long_token = "A" * 40
    assert _extract_token('{"token":"%s","viewerId":123}' % long_token) == long_token
    assert _extract_token(long_token) == long_token
    assert _extract_token(f'"{long_token}"') == long_token
    assert _extract_token(f"token={long_token}") == long_token
    assert _extract_token("коротко") == ""
    assert _extract_token("{битый json") == ""
    assert _extract_token('{"viewerId":123}') == "", "JSON без токена — не токен"
