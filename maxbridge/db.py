"""Хранилище: SQLite + FTS5 (полнотекстовый поиск по всей истории MAX).

Операции короткие (единицы миллисекунд), поэтому используем обычный sqlite3
под мьютексом, а не отдельный асинхронный драйвер — меньше зависимостей.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .models import MaxMessage

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS chats (
    max_chat_id   INTEGER PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'dialog',
    tg_topic_id   INTEGER,
    muted         INTEGER NOT NULL DEFAULT 0,
    vip           INTEGER NOT NULL DEFAULT 0,
    autoreply     TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    max_chat_id   INTEGER NOT NULL,
    max_msg_id    TEXT NOT NULL,
    tg_msg_id     INTEGER,
    sender_id     INTEGER NOT NULL DEFAULT 0,
    sender_name   TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL DEFAULT '',
    outgoing      INTEGER NOT NULL DEFAULT 0,
    priority      TEXT NOT NULL DEFAULT 'normal',
    intent        TEXT NOT NULL DEFAULT '',
    needs_reply   INTEGER NOT NULL DEFAULT 0,
    answered      INTEGER NOT NULL DEFAULT 0,
    ts            INTEGER NOT NULL,
    attachments   TEXT NOT NULL DEFAULT '[]',
    UNIQUE (max_chat_id, max_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages (max_chat_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_tg      ON messages (tg_msg_id);
CREATE INDEX IF NOT EXISTS idx_messages_pending ON messages (needs_reply, answered, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, sender_name, content='messages', content_rowid='id', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, text, sender_name)
    VALUES (new.id, new.text, new.sender_name);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text, sender_name)
    VALUES ('delete', old.id, old.text, old.sender_name);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text, sender_name)
    VALUES ('delete', old.id, old.text, old.sender_name);
    INSERT INTO messages_fts (rowid, text, sender_name)
    VALUES (new.id, new.text, new.sender_name);
END;

CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern    TEXT NOT NULL,
    action     TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '',
    scope_chat INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Storage:
    """Тонкая обёртка над SQLite. Потокобезопасна за счёт RLock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Добавляет колонки, появившиеся после первых установок."""
        existing = {row["name"] for row in self._db.execute("PRAGMA table_info(messages)")}
        for column, ddl in (("escalated", "INTEGER NOT NULL DEFAULT 0"),):
            if column not in existing:
                self._db.execute(f"ALTER TABLE messages ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------------------------------------------------------- chats
    def upsert_chat(self, chat_id: int, title: str, kind: str = "dialog") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO chats (max_chat_id, title, kind, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(max_chat_id) DO UPDATE SET title = "
                "CASE WHEN excluded.title <> '' THEN excluded.title ELSE chats.title END",
                (chat_id, title, kind, int(time.time())),
            )
            self._db.commit()

    def get_chat(self, chat_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM chats WHERE max_chat_id = ?", (chat_id,)
            ).fetchone()

    def chat_by_topic(self, topic_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM chats WHERE tg_topic_id = ?", (topic_id,)
            ).fetchone()

    def bind_topic(self, chat_id: int, topic_id: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE chats SET tg_topic_id = ? WHERE max_chat_id = ?", (topic_id, chat_id)
            )
            self._db.commit()

    def set_chat_flag(self, chat_id: int, field: str, value: int | str) -> None:
        if field not in {"muted", "vip", "autoreply"}:
            raise ValueError("недопустимое поле чата: " + field)
        with self._lock:
            self._db.execute(
                "UPDATE chats SET " + field + " = ? WHERE max_chat_id = ?", (value, chat_id)
            )
            self._db.commit()

    def list_chats(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute("SELECT * FROM chats ORDER BY title").fetchall())

    # ------------------------------------------------------------- messages
    def save_message(
        self,
        msg: MaxMessage,
        *,
        priority: str = "normal",
        intent: str = "",
        needs_reply: bool = False,
    ) -> int | None:
        """Сохраняет сообщение. None означает дубликат (message_id уже видели)."""
        attachments = json.dumps(
            [{"kind": a.kind, "url": a.url, "name": a.name} for a in msg.attachments],
            ensure_ascii=False,
        )
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO messages (max_chat_id, max_msg_id, sender_id, sender_name,"
                " text, outgoing, priority, intent, needs_reply, ts, attachments)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    msg.chat_id,
                    str(msg.message_id),
                    msg.sender_id,
                    msg.sender_name,
                    msg.text,
                    int(msg.outgoing),
                    priority,
                    intent,
                    int(needs_reply),
                    msg.ts,
                    attachments,
                ),
            )
            self._db.commit()
            return cur.lastrowid if cur.rowcount else None

    def save_transcript(self, chat_id: int, max_msg_id: str, text: str) -> None:
        """Кладёт расшифровку голосового в текст сообщения.

        Так голосовое становится находимым через /find и попадает в сводку —
        ради этого расшифровка и делается.
        """
        if not text.strip():
            return
        with self._lock:
            self._db.execute(
                "UPDATE messages SET text = CASE WHEN text = '' THEN ?"
                " ELSE text || char(10) || ? END"
                " WHERE max_chat_id = ? AND max_msg_id = ?",
                (text, text, chat_id, str(max_msg_id)),
            )
            self._db.commit()

    def link_tg_message(self, row_id: int, tg_msg_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE messages SET tg_msg_id = ? WHERE id = ?", (tg_msg_id, row_id))
            self._db.commit()

    def max_msg_by_tg(self, tg_msg_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM messages WHERE tg_msg_id = ?", (tg_msg_id,)
            ).fetchone()

    def mark_chat_answered(self, chat_id: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE messages SET answered = 1 WHERE max_chat_id = ? AND answered = 0",
                (chat_id,),
            )
            self._db.commit()

    def recent(self, chat_id: int, limit: int = 30) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE max_chat_id = ? ORDER BY ts DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def since(self, ts_from: int, *, only_incoming: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM messages WHERE ts >= ?"
        if only_incoming:
            sql += " AND outgoing = 0"
        sql += " ORDER BY ts"
        with self._lock:
            return list(self._db.execute(sql, (ts_from,)).fetchall())

    def unanswered(self, older_than_ts: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._db.execute(
                    "SELECT * FROM messages WHERE needs_reply = 1 AND answered = 0"
                    " AND outgoing = 0 AND ts <= ? ORDER BY ts",
                    (older_than_ts,),
                ).fetchall()
            )

    def pending_escalation(self, older_than_ts: int) -> list[sqlite3.Row]:
        """Срочные входящие, на которые не ответили и ещё не эскалировали."""
        with self._lock:
            return list(
                self._db.execute(
                    "SELECT m.*, c.title AS chat_title FROM messages m"
                    " LEFT JOIN chats c ON c.max_chat_id = m.max_chat_id"
                    " WHERE m.priority = 'urgent' AND m.answered = 0 AND m.outgoing = 0"
                    " AND m.escalated = 0 AND m.ts <= ? ORDER BY m.ts",
                    (older_than_ts,),
                ).fetchall()
            )

    def mark_escalated(self, row_ids: Iterable[int]) -> None:
        ids = list(row_ids)
        if not ids:
            return
        with self._lock:
            self._db.executemany(
                "UPDATE messages SET escalated = 1 WHERE id = ?", [(i,) for i in ids]
            )
            self._db.commit()

    @staticmethod
    def _fts_query(raw: str) -> str:
        """Превращает пользовательский ввод в безопасное выражение FTS5.

        FTS5 разбирает строку как язык запросов, поэтому точки, дефисы, скобки
        и кавычки в обычном слове («акт.docx», «счёт-фактура») роняют поиск с
        syntax error. Ищем фразой: кавычки экранируем удвоением, звёздочку в
        конце сохраняем как поиск по префиксу.
        """
        cleaned = raw.strip()
        prefix = cleaned.endswith("*")
        if prefix:
            cleaned = cleaned[:-1].strip()
        cleaned = cleaned.replace('"', '""')
        if not cleaned:
            return '""'
        return f'"{cleaned}"*' if prefix else f'"{cleaned}"'

    def search(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        query = self._fts_query(query)
        with self._lock:
            return list(
                self._db.execute(
                    "SELECT m.*, c.title AS chat_title FROM messages_fts f"
                    " JOIN messages m ON m.id = f.rowid"
                    " LEFT JOIN chats c ON c.max_chat_id = m.max_chat_id"
                    " WHERE messages_fts MATCH ? ORDER BY m.ts DESC LIMIT ?",
                    (query, limit),
                ).fetchall()
            )

    def stats(self) -> dict[str, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT (SELECT COUNT(*) FROM messages) AS messages,"
                " (SELECT COUNT(*) FROM chats) AS chats,"
                " (SELECT COUNT(*) FROM messages WHERE outgoing = 1) AS sent,"
                " (SELECT COUNT(*) FROM messages WHERE needs_reply = 1 AND answered = 0) AS pending"
            ).fetchone()
        return dict(row)

    # ---------------------------------------------------------------- rules
    def add_rule(
        self, pattern: str, action: str, payload: str = "", chat_id: int | None = None
    ) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO rules (pattern, action, payload, scope_chat, created_at)"
                " VALUES (?,?,?,?,?)",
                (pattern, action, payload, chat_id, int(time.time())),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def list_rules(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute("SELECT * FROM rules ORDER BY id").fetchall())

    def delete_rule(self, rule_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
            self._db.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------- settings
    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.commit()

    def executemany(self, sql: str, rows: Iterable[tuple[Any, ...]]) -> None:
        with self._lock:
            self._db.executemany(sql, rows)
            self._db.commit()
