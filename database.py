from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

from models import Event, Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    guild_id          INTEGER PRIMARY KEY,
    notes_channel_id  INTEGER,
    panel_message_id  INTEGER,
    notify_role_id    INTEGER NOT NULL,
    editor_role_id    INTEGER,
    timezone          TEXT    NOT NULL DEFAULT 'Europe/Moscow',
    remind_minutes    INTEGER NOT NULL DEFAULT 20
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    mmr         TEXT,
    gather_ts   INTEGER NOT NULL,
    match_ts    INTEGER,
    extra       TEXT,
    notified    INTEGER NOT NULL DEFAULT 0,
    created_by  INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_guild
    ON events (guild_id, gather_ts, id);
CREATE INDEX IF NOT EXISTS idx_events_pending
    ON events (notified, gather_ts);
"""


class Database:
    """Небольшое синхронное хранилище на SQLite.

    Нагрузка у бота минимальная, поэтому отдельный поток/aiosqlite не нужен:
    запросы занимают доли миллисекунды.
    """

    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # Без WAL: проект может лежать в OneDrive, а лишние -wal/-shm файлы
        # при синхронизации иногда приводят к блокировкам.
        self.conn.execute("PRAGMA journal_mode=DELETE")
        # Не ждём блокировку дольше, чем Discord даёт на ответ (3 секунды).
        self.conn.execute("PRAGMA busy_timeout=1500")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ settings

    def get_settings(self, guild_id: int) -> Optional[Settings]:
        row = self.conn.execute(
            "SELECT * FROM settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return self._settings_from_row(row) if row else None

    def all_settings(self) -> list[Settings]:
        rows = self.conn.execute("SELECT * FROM settings").fetchall()
        return [self._settings_from_row(row) for row in rows]

    def save_settings(
        self,
        *,
        guild_id: int,
        notes_channel_id: int,
        panel_message_id: Optional[int],
        notify_role_id: int,
        editor_role_id: Optional[int],
        timezone: str,
        remind_minutes: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO settings (
                guild_id, notes_channel_id, panel_message_id,
                notify_role_id, editor_role_id, timezone, remind_minutes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                notes_channel_id = excluded.notes_channel_id,
                panel_message_id = excluded.panel_message_id,
                notify_role_id   = excluded.notify_role_id,
                editor_role_id   = excluded.editor_role_id,
                timezone         = excluded.timezone,
                remind_minutes   = excluded.remind_minutes
            """,
            (
                guild_id,
                notes_channel_id,
                panel_message_id,
                notify_role_id,
                editor_role_id,
                timezone,
                remind_minutes,
            ),
        )
        self.conn.commit()

    def set_panel_message(
        self, guild_id: int, notes_channel_id: int, panel_message_id: Optional[int]
    ) -> None:
        self.conn.execute(
            """
            UPDATE settings
               SET notes_channel_id = ?, panel_message_id = ?
             WHERE guild_id = ?
            """,
            (notes_channel_id, panel_message_id, guild_id),
        )
        self.conn.commit()

    # -------------------------------------------------------------------- events

    def add_event(
        self,
        *,
        guild_id: int,
        title: str,
        mmr: Optional[str],
        gather_ts: int,
        match_ts: Optional[int],
        extra: Optional[str],
        created_by: Optional[int],
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO events (
                guild_id, title, mmr, gather_ts, match_ts, extra,
                notified, created_by, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                guild_id,
                title,
                mmr,
                gather_ts,
                match_ts,
                extra,
                created_by,
                int(time.time()),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_event(self, guild_id: int, event_id: int) -> Optional[Event]:
        row = self.conn.execute(
            "SELECT * FROM events WHERE id = ? AND guild_id = ?",
            (event_id, guild_id),
        ).fetchone()
        return self._event_from_row(row) if row else None

    def list_events(self, guild_id: int) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE guild_id = ? ORDER BY gather_ts, id",
            (guild_id,),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def count_events(self, guild_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return int(row["c"])

    def update_event(
        self,
        *,
        guild_id: int,
        event_id: int,
        title: str,
        mmr: Optional[str],
        gather_ts: int,
        match_ts: Optional[int],
        extra: Optional[str],
        notified: int,
    ) -> bool:
        cur = self.conn.execute(
            """
            UPDATE events
               SET title = ?, mmr = ?, gather_ts = ?, match_ts = ?,
                   extra = ?, notified = ?
             WHERE id = ? AND guild_id = ?
            """,
            (
                title,
                mmr,
                gather_ts,
                match_ts,
                extra,
                notified,
                event_id,
                guild_id,
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete_event(self, guild_id: int, event_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM events WHERE id = ? AND guild_id = ?",
            (event_id, guild_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def clear_events(self, guild_id: int) -> int:
        cur = self.conn.execute(
            "DELETE FROM events WHERE guild_id = ?", (guild_id,)
        )
        self.conn.commit()
        return cur.rowcount

    def pending_events(self, guild_id: int) -> list[Event]:
        rows = self.conn.execute(
            """
            SELECT * FROM events
             WHERE guild_id = ? AND notified = 0
             ORDER BY gather_ts, id
            """,
            (guild_id,),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def mark_notified(self, event_ids: Iterable[int]) -> None:
        ids = list(event_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE events SET notified = 1 WHERE id IN ({placeholders})", ids
        )
        self.conn.commit()

    # ------------------------------------------------------------------- mapping

    @staticmethod
    def _settings_from_row(row: sqlite3.Row) -> Settings:
        return Settings(
            guild_id=row["guild_id"],
            notes_channel_id=row["notes_channel_id"],
            panel_message_id=row["panel_message_id"],
            notify_role_id=row["notify_role_id"],
            editor_role_id=row["editor_role_id"],
            timezone=row["timezone"],
            remind_minutes=row["remind_minutes"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            guild_id=row["guild_id"],
            title=row["title"],
            mmr=row["mmr"],
            gather_ts=row["gather_ts"],
            match_ts=row["match_ts"],
            extra=row["extra"],
            notified=row["notified"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )
