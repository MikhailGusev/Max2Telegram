"""Правила приоритетов — быстрый детерминированный слой перед AI.

Порядок разбора входящего сообщения:
  1. правила пользователя (ключевые слова -> urgent / mute / autoreply);
  2. флаги чата (vip -> всегда urgent, muted -> всегда low);
  3. эвристики (вопрос, деньги, дедлайн);
  4. только если ничего не сработало и включён AI — спросить модель.

Так 90% сообщений разбираются мгновенно и бесплатно.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..db import Storage
from ..models import MaxMessage

#: слова, после которых сообщение почти наверняка требует реакции
URGENT_WORDS = (
    "срочно", "сегодня", "asap", "горит", "аврал", "критично", "не работает",
    "упало", "сломалось", "штраф", "суд", "дедлайн", "до конца дня",
)
MONEY_WORDS = ("счёт", "счет", "оплат", "платёж", "платеж", "инвойс", "предоплат", "реквизит")
TASK_WORDS = ("сделай", "подготовь", "отправь", "нужно", "надо", "прошу", "жду")
NOISE_WORDS = ("с днем рождения", "с днём рождения", "доброе утро", "спасибо", "ок", "+")


@dataclass(slots=True)
class Verdict:
    """Результат разбора: что делать с сообщением."""

    priority: str = "normal"   # urgent | normal | low
    intent: str = ""           # question | task | money | noise | ...
    needs_reply: bool = False
    autoreply: str = ""        # непустое -> отправить этот текст в MAX
    reason: str = ""           # чем объясняется вердикт (видно в /why)

    @property
    def is_urgent(self) -> bool:
        return self.priority == "urgent"


def _contains(text: str, words: Iterable[str]) -> str:
    low = text.lower()
    for word in words:
        if word in low:
            return word
    return ""


def classify(message: MaxMessage, storage: Storage) -> Verdict:
    """Детерминированный разбор. Никогда не ходит в сеть."""
    text = message.text or ""
    verdict = Verdict()

    # --- 1. пользовательские правила -------------------------------------
    for rule in storage.list_rules():
        scope = rule["scope_chat"]
        if scope is not None and int(scope) != message.chat_id:
            continue
        try:
            matched = re.search(rule["pattern"], text, re.IGNORECASE) is not None
        except re.error:  # пользователь ввёл не-regex — сравниваем как подстроку
            matched = rule["pattern"].lower() in text.lower()
        if not matched:
            continue
        action = rule["action"]
        if action == "urgent":
            verdict.priority = "urgent"
            verdict.needs_reply = True
            verdict.reason = f"правило #{rule['id']}: {rule['pattern']}"
            return verdict
        if action == "mute":
            verdict.priority = "low"
            verdict.reason = f"правило #{rule['id']}: тишина"
            return verdict
        if action == "autoreply":
            verdict.autoreply = rule["payload"]
            verdict.reason = f"правило #{rule['id']}: автоответ"

    # --- 2. флаги чата ----------------------------------------------------
    chat = storage.get_chat(message.chat_id)
    if chat is not None:
        if chat["muted"]:
            verdict.priority = "low"
            verdict.intent = verdict.intent or "muted"
            verdict.reason = verdict.reason or "чат заглушён"
            return verdict
        if chat["vip"]:
            verdict.priority = "urgent"
            verdict.needs_reply = True
            verdict.reason = verdict.reason or "VIP-чат"
            return verdict
        if chat["autoreply"] and not verdict.autoreply:
            verdict.autoreply = chat["autoreply"]

    # --- 3. эвристики -----------------------------------------------------
    if word := _contains(text, URGENT_WORDS):
        verdict.priority = "urgent"
        verdict.needs_reply = True
        verdict.intent = "urgent"
        verdict.reason = f"слово «{word}»"
        return verdict

    if word := _contains(text, MONEY_WORDS):
        verdict.priority = "urgent"
        verdict.needs_reply = True
        verdict.intent = "money"
        verdict.reason = f"деньги: «{word}»"
        return verdict

    if "?" in text:
        verdict.intent = "question"
        verdict.needs_reply = True
        verdict.reason = "вопрос"
        return verdict

    if word := _contains(text, TASK_WORDS):
        verdict.intent = "task"
        verdict.needs_reply = True
        verdict.reason = f"просьба: «{word}»"
        return verdict

    stripped = text.strip().lower()
    if not stripped or (len(stripped) <= 24 and _contains(stripped, NOISE_WORDS)):
        verdict.priority = "low"
        verdict.intent = "noise"
        verdict.reason = "дежурная реплика"
        return verdict

    verdict.reason = "по умолчанию"
    return verdict
