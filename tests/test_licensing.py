"""Лицензии и каналы: без ключа — Community, платные каналы закрыты."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxbridge.channels.sms import SmsChannel  # noqa: E402
from maxbridge.channels.whatsapp import WhatsAppChannel  # noqa: E402
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


def test_sms_channel_requires_credentials() -> None:
    assert not SmsChannel(provider="smsru", recipient="+79990000000").configured
    assert SmsChannel(provider="smsru", api_key="k", recipient="+79990000000").configured
    assert not SmsChannel(provider="smsc", login="l", recipient="+7999").configured
    assert SmsChannel(provider="smsc", login="l", password="p", recipient="+7999").configured


def test_whatsapp_channel_requires_credentials() -> None:
    assert not WhatsAppChannel(provider="cloud", token="t", recipient="").configured
    assert WhatsAppChannel(
        provider="cloud", token="t", phone_number_id="1", recipient="79990000000"
    ).configured
    assert WhatsAppChannel(
        provider="waha", waha_url="http://localhost:3000", recipient="79990000000"
    ).configured


def test_unknown_provider_is_not_configured() -> None:
    assert not SmsChannel(provider="почтовый голубь", api_key="k", recipient="+7999").configured
    assert not WhatsAppChannel(provider="", token="t", recipient="7999").configured


def test_trim_keeps_message_readable() -> None:
    long_text = "слово " * 100
    trimmed = SmsChannel.trim(long_text, 40)
    assert len(trimmed) <= 40
    assert trimmed.endswith("…")
