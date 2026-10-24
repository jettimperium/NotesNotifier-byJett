from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from timeutils import (
    ValidationError,
    format_mmr,
    format_reminder_time,
    format_table_time,
    parse_event_input,
    parse_user_datetime,
    plural_ru,
)

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 24, 18, 0, tzinfo=TZ)


class ParseDatetimeTests(unittest.TestCase):
    def test_time_today(self) -> None:
        result = parse_user_datetime("20:00", TZ, now=NOW)
        self.assertEqual(result, datetime(2026, 10, 24, 20, 0, tzinfo=TZ))

    def test_recent_past_stays_today(self) -> None:
        result = parse_user_datetime("17:50", TZ, now=NOW)
        self.assertEqual(result.date().day, 24)

    def test_old_past_rolls_to_tomorrow(self) -> None:
        result = parse_user_datetime("12:00", TZ, now=NOW)
        self.assertEqual(result.date().day, 25)

    def test_relative_words(self) -> None:
        today = parse_user_datetime("сегодня 20:00", TZ, now=NOW)
        tomorrow = parse_user_datetime("завтра 20:00", TZ, now=NOW)
        self.assertEqual(today.date().day, 24)
        self.assertEqual(tomorrow.date().day, 25)

    def test_explicit_date(self) -> None:
        result = parse_user_datetime("25.10 20:00", TZ, now=NOW)
        self.assertEqual(result, datetime(2026, 10, 25, 20, 0, tzinfo=TZ))

    def test_explicit_date_with_year(self) -> None:
        result = parse_user_datetime("25.10.2027 20:00", TZ, now=NOW)
        self.assertEqual(result, datetime(2027, 10, 25, 20, 0, tzinfo=TZ))

    def test_msk_suffix(self) -> None:
        result = parse_user_datetime("20:00 МСК", TZ, now=NOW)
        self.assertEqual(result.hour, 20)

    def test_match_after_midnight(self) -> None:
        gather = datetime(2026, 10, 24, 23, 50, tzinfo=TZ)
        result = parse_user_datetime("00:20", TZ, now=NOW, base=gather)
        self.assertEqual(result, datetime(2026, 10, 25, 0, 20, tzinfo=TZ))

    def test_match_same_day(self) -> None:
        gather = datetime(2026, 10, 24, 19, 40, tzinfo=TZ)
        result = parse_user_datetime("20:00", TZ, now=NOW, base=gather)
        self.assertEqual(result, datetime(2026, 10, 24, 20, 0, tzinfo=TZ))

    def test_invalid_time(self) -> None:
        with self.assertRaises(ValidationError):
            parse_user_datetime("25:00", TZ, now=NOW)

    def test_invalid_date(self) -> None:
        with self.assertRaises(ValidationError):
            parse_user_datetime("31.02 20:00", TZ, now=NOW)

    def test_empty(self) -> None:
        with self.assertRaises(ValidationError):
            parse_user_datetime("", TZ, now=NOW)


class ParseEventInputTests(unittest.TestCase):
    def test_valid_event(self) -> None:
        now_ts = int(NOW.timestamp())
        event = parse_event_input(
            {
                "title": "  20:00   без смурфов ",
                "mmr": "3 250",
                "gather": "19:40",
                "match": "20:00",
                "extra": "  нужен 5-й  ",
            },
            "Europe/Moscow",
            now_ts=now_ts,
        )
        self.assertEqual(event.title, "20:00 без смурфов")
        self.assertEqual(event.mmr, "3 250")
        self.assertEqual(event.extra, "нужен 5-й")
        self.assertEqual(
            datetime.fromtimestamp(event.gather_ts, TZ),
            datetime(2026, 10, 24, 19, 40, tzinfo=TZ),
        )
        self.assertIsNotNone(event.match_ts)

    def test_past_explicit_gather_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            parse_event_input(
                {
                    "title": "Старый сбор",
                    "gather": "23.10 19:00",
                    "match": "",
                },
                "Europe/Moscow",
                now_ts=int(NOW.timestamp()),
            )

    def test_past_allowed_for_edit(self) -> None:
        event = parse_event_input(
            {"title": "Старый сбор", "gather": "23.10 19:00", "match": ""},
            "Europe/Moscow",
            now_ts=int(NOW.timestamp()),
            require_future=False,
        )
        self.assertLess(event.gather_ts, NOW.timestamp())

    def test_match_before_gather_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            parse_event_input(
                {"title": "Турнир", "gather": "20:00", "match": "19:00"},
                "Europe/Moscow",
                now_ts=int(NOW.timestamp()),
            )

    def test_night_match_allowed(self) -> None:
        event = parse_event_input(
            {"title": "Ночной", "gather": "23:50", "match": "00:20"},
            "Europe/Moscow",
            now_ts=int(NOW.timestamp()),
        )
        self.assertIsNotNone(event.match_ts)
        self.assertGreater(event.match_ts or 0, event.gather_ts)

    def test_empty_match(self) -> None:
        event = parse_event_input(
            {"title": "Без матча", "gather": "19:40", "match": "—"},
            "Europe/Moscow",
            now_ts=int(NOW.timestamp()),
        )
        self.assertIsNone(event.match_ts)

    def test_long_title_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            parse_event_input(
                {"title": "x" * 101, "gather": "19:40", "match": ""},
                "Europe/Moscow",
                now_ts=int(NOW.timestamp()),
            )


class FormattingTests(unittest.TestCase):
    def test_table_time(self) -> None:
        ts = int(datetime(2026, 10, 24, 20, 0, tzinfo=TZ).timestamp())
        self.assertEqual(format_table_time(ts, TZ), "24.10 20:00")

    def test_reminder_time(self) -> None:
        ts = int(datetime(2026, 10, 24, 20, 0, tzinfo=TZ).timestamp())
        self.assertEqual(format_reminder_time(ts, TZ, NOW), "сегодня в 20:00")
        tomorrow = ts + 86400
        self.assertEqual(format_reminder_time(tomorrow, TZ, NOW), "завтра в 20:00")

    def test_mmr(self) -> None:
        self.assertEqual(format_mmr("3250"), "3250 MMR")
        self.assertEqual(format_mmr("3 250 MMR"), "3 250 MMR")
        self.assertEqual(format_mmr("3к ммр"), "3к ммр")
        self.assertIsNone(format_mmr("  "))

    def test_plural(self) -> None:
        self.assertEqual(plural_ru(1, "минута", "минуты", "минут"), "минута")
        self.assertEqual(plural_ru(20, "минута", "минуты", "минут"), "минут")
        self.assertEqual(plural_ru(22, "минута", "минуты", "минут"), "минуты")
        self.assertEqual(plural_ru(11, "минута", "минуты", "минут"), "минут")


if __name__ == "__main__":
    unittest.main()
