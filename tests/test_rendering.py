from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from models import Event, Settings
from rendering import (
    build_panel_embed,
    build_reminder_embed,
    build_table,
)

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 24, 18, 0, tzinfo=TZ)


def make_event(
    *,
    event_id: int = 1,
    title: str = "20:00 без смурфов",
    mmr: str | None = "3 250",
    gather: datetime = datetime(2026, 10, 24, 19, 40, tzinfo=TZ),
    match: datetime | None = datetime(2026, 10, 24, 20, 0, tzinfo=TZ),
    extra: str | None = "нужен 5-й игрок",
) -> Event:
    return Event(
        id=event_id,
        guild_id=1,
        title=title,
        mmr=mmr,
        gather_ts=int(gather.timestamp()),
        match_ts=int(match.timestamp()) if match else None,
        extra=extra,
        notified=0,
        created_by=1,
        created_at=0,
    )


def make_settings(
    *, editor_role_id: int | None = None, remind_minutes: int = 20
) -> Settings:
    return Settings(
        guild_id=1,
        notes_channel_id=2,
        panel_message_id=3,
        notify_role_id=4,
        editor_role_id=editor_role_id,
        timezone="Europe/Moscow",
        remind_minutes=remind_minutes,
    )


def make_guild() -> MagicMock:
    guild = MagicMock()
    guild.name = "Тестовый сервер"
    guild.get_role.return_value = None
    return guild


class TableTests(unittest.TestCase):
    def test_empty_table_has_headers(self) -> None:
        table = build_table([], TZ)
        self.assertIn("Название", table)
        self.assertIn("Сбор", table)
        self.assertIn("Доп. информация", table)

    def test_table_fits_narrow_window(self) -> None:
        table = build_table([], TZ)
        self.assertLessEqual(max(len(line) for line in table.splitlines()), 64)
        table = build_table([make_event()], TZ)
        self.assertLessEqual(max(len(line) for line in table.splitlines()), 68)

    def test_table_row(self) -> None:
        table = build_table([make_event()], TZ)
        self.assertIn("20:00 без смурфов", table)
        self.assertIn("24.10 19:40", table)
        self.assertIn("24.10 20:00", table)
        self.assertIn("3 250 MMR", table)

    def test_long_extra_is_truncated(self) -> None:
        table = build_table([make_event(extra="x" * 500)], TZ)
        self.assertIn("…", table)
        self.assertNotIn("x" * 100, table)

    def test_long_title_has_room_without_wrapping(self) -> None:
        title = "Очень длинное название"
        table = build_table([make_event(title=title)], TZ)
        self.assertIn(title, table)
        self.assertLessEqual(max(len(line) for line in table.splitlines()), 67)

    def test_empty_match(self) -> None:
        table = build_table([make_event(match=None)], TZ)
        self.assertIn("—", table)


class EmbedTests(unittest.TestCase):
    def test_panel_embed(self) -> None:
        embed = build_panel_embed(
            [make_event()], make_settings(editor_role_id=5), make_guild()
        )
        self.assertIn("Праки", embed.title or "")
        self.assertIn("```", embed.description or "")
        self.assertIn("20:00 без смурфов", embed.description or "")
        self.assertIn("МСК", embed.footer.text or "")
        self.assertIn("20 минут", embed.footer.text or "")

    def test_empty_panel_hint(self) -> None:
        embed = build_panel_embed([], make_settings(), make_guild())
        self.assertIn("Таблица пуста", embed.description or "")

    def test_reminder_embed(self) -> None:
        now = datetime(2026, 10, 24, 19, 20, tzinfo=TZ)
        embed = build_reminder_embed([make_event()], make_settings(), make_guild(), now=now)
        self.assertIn("Скоро сбор", embed.title or "")
        self.assertEqual(len(embed.fields), 1)
        self.assertIn("20:00 без смурфов", embed.fields[0].name)
        self.assertIn("сегодня в 19:40", embed.fields[0].value)
        self.assertIn("сегодня в 20:00", embed.fields[0].value)
        self.assertIn("3 250 MMR", embed.fields[0].value)
        self.assertIn("нужен 5-й игрок", embed.fields[0].value)
        self.assertIn("20 минут", embed.description or "")

    def test_reminder_multiple_events(self) -> None:
        events = [
            make_event(),
            make_event(event_id=2, title="Турнир", gather=datetime(2026, 10, 24, 19, 45, tzinfo=TZ)),
        ]
        embed = build_reminder_embed(events, make_settings(), make_guild(), now=NOW)
        self.assertEqual(len(embed.fields), 2)

    def test_reminder_respects_embed_limits(self) -> None:
        events = [
            make_event(
                event_id=i,
                title="Очень длинное название " * 5,
                extra="Очень длинный комментарий " * 18,
            )
            for i in range(1, 26)
        ]
        embed = build_reminder_embed(events, make_settings(), make_guild(), now=NOW)
        total = (
            len(embed.title or "")
            + len(embed.description or "")
            + len(embed.footer.text or "")
            + sum(len(field.name) + len(field.value) for field in embed.fields)
        )
        self.assertLessEqual(len(embed.fields), 25)
        self.assertLessEqual(total, 6000)
        self.assertGreaterEqual(len(embed.fields), 1)


if __name__ == "__main__":
    unittest.main()
