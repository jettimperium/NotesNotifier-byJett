from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from bot import NotesBot
from database import Database


class FakeMember:
    def __init__(self, member_id: int) -> None:
        self.id = member_id
        self.bot = False
        self.messages: list[Any] = []

    async def send(self, *, embed: Any, view: Any = None) -> None:
        self.messages.append((embed, view))


class FakeRole:
    def __init__(self, members: list[FakeMember]) -> None:
        self.id = 4
        self.members = members


class FakeGuild:
    def __init__(self, role: Optional[FakeRole]) -> None:
        self.id = 1
        self.name = "Тестовый сервер"
        self.role = role

    def get_role(self, role_id: int) -> Optional[FakeRole]:
        if self.role is not None and self.role.id == role_id:
            return self.role
        return None


class ReminderFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.db.save_settings(
            guild_id=1,
            notes_channel_id=10,
            panel_message_id=11,
            notify_role_id=4,
            editor_role_id=None,
            timezone="Europe/Moscow",
            remind_minutes=20,
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def make_bot(self, guild: FakeGuild) -> SimpleNamespace:
        db = self.db

        class FakeBot:
            def get_guild(self, guild_id: int) -> Optional[FakeGuild]:
                return guild if guild_id == guild.id else None

            def panel_url(self, settings: Any, target: FakeGuild) -> None:
                return None

        fake = FakeBot()
        fake.db = db  # type: ignore[attr-defined]
        return fake

    async def process(self, guild: FakeGuild) -> None:
        fake = self.make_bot(guild)
        await NotesBot._process_reminders(fake)  # type: ignore[arg-type]

    def add_event(self, *, gather_in: int, title: str = "Сбор") -> int:
        now = int(time.time())
        return self.db.add_event(
            guild_id=1,
            title=title,
            mmr=None,
            gather_ts=now + gather_in,
            match_ts=now + gather_in + 3600,
            extra=None,
            created_by=1,
        )

    async def test_due_event_sends_one_dm_per_member(self) -> None:
        members = [FakeMember(101), FakeMember(102)]
        self.add_event(gather_in=10 * 60)
        await self.process(FakeGuild(FakeRole(members)))

        self.assertEqual(len(members[0].messages), 1)
        self.assertEqual(len(members[1].messages), 1)
        embed = members[0].messages[0][0]
        self.assertEqual(len(embed.fields), 1)
        self.assertEqual(self.db.pending_events(1), [])

    async def test_event_in_future_not_sent(self) -> None:
        member = FakeMember(101)
        self.add_event(gather_in=60 * 60)
        await self.process(FakeGuild(FakeRole([member])))

        self.assertEqual(member.messages, [])
        self.assertEqual(len(self.db.pending_events(1)), 1)

    async def test_stale_event_is_marked_without_dm(self) -> None:
        member = FakeMember(101)
        self.add_event(gather_in=-60)
        await self.process(FakeGuild(FakeRole([member])))

        self.assertEqual(member.messages, [])
        self.assertEqual(self.db.pending_events(1), [])

    async def test_two_due_events_aggregated(self) -> None:
        member = FakeMember(101)
        self.add_event(gather_in=10 * 60, title="Первый")
        self.add_event(gather_in=15 * 60, title="Второй")
        await self.process(FakeGuild(FakeRole([member])))

        self.assertEqual(len(member.messages), 1)
        embed = member.messages[0][0]
        self.assertEqual(len(embed.fields), 2)
        self.assertEqual(self.db.pending_events(1), [])

    async def test_missing_role_marks_events(self) -> None:
        self.add_event(gather_in=10 * 60)
        await self.process(FakeGuild(None))
        self.assertEqual(self.db.pending_events(1), [])

    async def test_empty_role_marks_events(self) -> None:
        self.add_event(gather_in=10 * 60)
        await self.process(FakeGuild(FakeRole([])))
        self.assertEqual(self.db.pending_events(1), [])


if __name__ == "__main__":
    unittest.main()
