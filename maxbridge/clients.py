"""Реестр SaaS-клиентов, прошедших онбординг в боте.

Это НЕ мультиаккаунт владельца (тот описан в accounts.json и гейтится Team-
лицензией). Здесь — люди, которые сами подключили свой MAX через QR в боте:
у каждого свой tg_user_id, своя база и сессия, доставка в личку (плоский режим).

Храним отдельным файлом `clients.json` рядом с базой, чтобы после рестарта
поднять их аккаунты заново. Пишем атомарно (во временный файл + замена), чтобы
падение на середине записи не побило список. Событийного цикла один поток, так
что блокировки не нужны.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("maxbridge.clients")

CLIENTS_FILE = "clients.json"

#: статусы жизненного цикла клиента
STATUS_ONBOARDING = "onboarding"  # начал вход, сессии ещё нет
STATUS_ACTIVE = "active"          # сессия есть, аккаунт поднят
STATUS_DISABLED = "disabled"      # выключен владельцем/самим собой


@dataclasses.dataclass
class ClientRecord:
    tg_user_id: int
    name: str
    status: str = STATUS_ONBOARDING
    created_at: int = dataclasses.field(default_factory=lambda: int(time.time()))
    session_file: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ClientRecord":
        return cls(
            tg_user_id=int(data["tg_user_id"]),
            name=str(data.get("name") or f"client-{data['tg_user_id']}"),
            status=str(data.get("status") or STATUS_ONBOARDING),
            created_at=int(data.get("created_at") or time.time()),
            session_file=str(data.get("session_file") or ""),
        )


def client_name(tg_user_id: int) -> str:
    """Имя аккаунта клиента — детерминированное по его Telegram id."""
    return f"client-{tg_user_id}"


class ClientStore:
    """Список клиентов на диске. Все мутации сразу сохраняются."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._items: dict[int, ClientRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("не смог прочитать %s: %s — начинаю с пустого списка", self.path, exc)
            return
        if not isinstance(raw, list):
            log.error("%s: ожидался список клиентов — игнорирую", self.path)
            return
        for item in raw:
            if not isinstance(item, dict) or "tg_user_id" not in item:
                continue
            try:
                record = ClientRecord.from_dict(item)
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("пропускаю битую запись клиента %s: %s", item, exc)
                continue
            self._items[record.tg_user_id] = record

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [dataclasses.asdict(r) for r in self._items.values()],
            ensure_ascii=False,
            indent=2,
        )
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)  # атомарная замена
        try:
            self.path.chmod(0o600)  # в файле есть привязки к аккаунтам MAX
        except OSError:
            pass

    # ------------------------------------------------------------- чтение
    def all(self) -> list[ClientRecord]:
        return list(self._items.values())

    def active(self) -> list[ClientRecord]:
        return [r for r in self._items.values() if r.status == STATUS_ACTIVE]

    def get(self, tg_user_id: int) -> ClientRecord | None:
        return self._items.get(int(tg_user_id))

    def count_active(self) -> int:
        return sum(1 for r in self._items.values() if r.status == STATUS_ACTIVE)

    # ------------------------------------------------------------- запись
    def upsert(self, record: ClientRecord) -> ClientRecord:
        self._items[record.tg_user_id] = record
        self._save()
        return record

    def start_onboarding(self, tg_user_id: int, session_file: str) -> ClientRecord:
        """Заводит запись клиента в статусе онбординга (или обновляет сессию)."""
        record = self._items.get(int(tg_user_id))
        if record is None:
            record = ClientRecord(
                tg_user_id=int(tg_user_id),
                name=client_name(tg_user_id),
                status=STATUS_ONBOARDING,
                session_file=session_file,
            )
        else:
            record.session_file = session_file
            record.status = STATUS_ONBOARDING
        return self.upsert(record)

    def set_status(self, tg_user_id: int, status: str) -> ClientRecord | None:
        record = self._items.get(int(tg_user_id))
        if record is None:
            return None
        record.status = status
        self._save()
        return record

    def remove(self, tg_user_id: int) -> bool:
        if int(tg_user_id) in self._items:
            del self._items[int(tg_user_id)]
            self._save()
            return True
        return False
