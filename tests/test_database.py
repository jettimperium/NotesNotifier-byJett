from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.db.save_settings(
            guild_id=1,
            notes_channel_id=10,
            panel_message_id=11,
            notify_role_id=20,
            editor_role_id=21,
            timezone="Europe/Moscow",
            remind_minutes=20,
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_settings_roundtrip(self) -> None:
        settings = self.db.get_settings(1)
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual(settings.notify_role_id, 20)
        self.assertEqual(settings.timezone, "Europe/Moscow")
        self.assertEqual(settings.remind_minutes, 20)
        self.assertEqual(len(self.db.all_settings()), 1)

    def test_event_lifecycle(self) -> None:
        event_id = self.db.add_event(
            guild_id=1,
            title="Тест",
            mmr="3000",
            gather_ts=1000,
            match_ts=2000,
            extra="комментарий",
            created_by=99,
        )
        event = self.db.get_event(1, event_id)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.title, "Тест")
        self.assertEqual(self.db.count_events(1), 1)
        self.assertEqual(len(self.db.pending_events(1)), 1)

        updated = self.db.update_event(
            guild_id=1,
            event_id=event_id,
            title="Новый",
            mmr=None,
            gather_ts=3000,
            match_ts=None,
            extra=None,
            notified=1,
        )
        self.assertTrue(updated)
        event = self.db.get_event(1, event_id)
        assert event is not None
        self.assertEqual(event.title, "Новый")
        self.assertEqual(event.notified, 1)
        self.assertEqual(self.db.pending_events(1), [])

        self.assertTrue(self.db.delete_event(1, event_id))
        self.assertEqual(self.db.count_events(1), 0)

    def test_clear_and_isolation(self) -> None:
        for guild_id in (1, 2):
            self.db.add_event(
                guild_id=guild_id,
                title=f"Сбор {guild_id}",
                mmr=None,
                gather_ts=1000,
                match_ts=None,
                extra=None,
                created_by=None,
            )
        self.assertEqual(self.db.count_events(1), 1)
        self.assertEqual(self.db.clear_events(1), 1)
        self.assertEqual(self.db.count_events(1), 0)
        self.assertEqual(self.db.count_events(2), 1)

    def test_mark_notified(self) -> None:
        first = self.db.add_event(
            guild_id=1,
            title="A",
            mmr=None,
            gather_ts=1000,
            match_ts=None,
            extra=None,
            created_by=None,
        )
        second = self.db.add_event(
            guild_id=1,
            title="B",
            mmr=None,
            gather_ts=1000,
            match_ts=None,
            extra=None,
            created_by=None,
        )
        self.db.mark_notified([first, second])
        self.assertEqual(self.db.pending_events(1), [])


if __name__ == "__main__":
    unittest.main()
