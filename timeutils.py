from __future__ import annotations

import re
import time
from datetime import date, datetime, time as dt_time, timedelta
from typing import Mapping, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from models import EventInput

MSK = "Europe/Moscow"

_RELATIVE_DAYS = {
    "сегодня": 0,
    "завтра": 1,
    "послезавтра": 2,
}
_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?(?!\d)"
)
_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*:\s*([0-5]\d)(?!\d)")
_TZ_WORD_RE = re.compile(
    r"\b(?:по\s+)?(?:мск|msk|москв[аеы]?|moscow)\b", re.IGNORECASE
)


class ValidationError(ValueError):
    """Ошибка, текст которой можно показать пользователю."""


def get_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(
            f"Неизвестный часовой пояс «{name}». Пример: Europe/Moscow"
        ) from exc


def timezone_label(name: str) -> str:
    return "МСК" if name == MSK else name


def parse_user_datetime(
    raw: str,
    tz: ZoneInfo,
    *,
    now: Optional[datetime] = None,
    base: Optional[datetime] = None,
    with_info: bool = False,
) -> Union[datetime, tuple[datetime, bool]]:
    """Разбирает время, введённое пользователем.

    Поддерживаются форматы:
      20:00, сегодня 20:00, завтра 20:00,
      24.10 20:00, 24.10.2026 20:00, 24/10 20:00.
    Слова «МСК» / «msk» игнорируются.
    Если дата не указана и передан base (время сбора), матч ставится
    на день сбора; если время уже прошло — на следующий день.
    """
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    text = (raw or "").strip()
    if not text:
        raise ValidationError("Укажите время в формате ЧЧ:ММ, например 19:40.")

    s = text.lower().replace("ё", "е")
    s = _TZ_WORD_RE.sub(" ", s)
    s = s.replace(",", " ")
    s = re.sub(r"\s+", " ", s).strip()

    explicit_day = False
    relative_offset: Optional[int] = None
    for word, offset in _RELATIVE_DAYS.items():
        if re.search(rf"\b{word}\b", s):
            relative_offset = offset
            explicit_day = True
            s = re.sub(rf"\b{word}\b", " ", s)
            break

    date_match = _DATE_RE.search(s)
    day: Optional[date] = None
    if date_match:
        day_num = int(date_match.group(1))
        month_num = int(date_match.group(2))
        year_raw = date_match.group(3)
        year = int(year_raw) if year_raw else now.year
        if year_raw and len(year_raw) == 2:
            year += 2000
        try:
            day = date(year, month_num, day_num)
        except ValueError as exc:
            raise ValidationError(
                f"Такой даты не существует: {date_match.group(0)}."
            ) from exc
        if not year_raw and day < now.date() - timedelta(days=180):
            try:
                day = day.replace(year=now.year + 1)
            except ValueError:
                pass
        explicit_day = True
        s = s[: date_match.start()] + " " + s[date_match.end() :]

    time_match = _TIME_RE.search(s)
    if not time_match:
        raise ValidationError(
            "Не понял время. Введите, например: 19:40, сегодня 19:40 "
            "или 24.10 19:40."
        )
    hour = int(time_match.group(1))
    minute = int(time_match.group(2))
    moment = dt_time(hour, minute)

    if day is None and relative_offset is not None:
        day = now.date() + timedelta(days=relative_offset)

    if day is not None:
        result = datetime.combine(day, moment, tzinfo=tz)
        return (result, explicit_day) if with_info else result

    if base is not None:
        base_local = base.astimezone(tz) if base.tzinfo else base.replace(tzinfo=tz)
        result = datetime.combine(base_local.date(), moment, tzinfo=tz)
        if result < base_local:
            result += timedelta(days=1)
        return (result, False) if with_info else result

    result = datetime.combine(now.date(), moment, tzinfo=tz)
    # Если время уже прошло более чем на полчаса, считаем, что это завтра.
    if result < now - timedelta(minutes=30):
        result += timedelta(days=1)
    return (result, False) if with_info else result


def parse_event_input(
    raw: Mapping[str, str],
    tz_name: str,
    *,
    now_ts: Optional[int] = None,
    require_future: bool = True,
) -> EventInput:
    tz = get_timezone(tz_name)
    now = datetime.fromtimestamp(now_ts if now_ts is not None else time.time(), tz)

    title = " ".join((raw.get("title") or "").split())
    if not title:
        raise ValidationError("Название не может быть пустым.")
    if len(title) > 100:
        raise ValidationError("Название слишком длинное (максимум 100 символов).")

    mmr = " ".join((raw.get("mmr") or "").split()) or None
    if mmr and len(mmr) > 50:
        raise ValidationError("Поле MMR слишком длинное (максимум 50 символов).")

    extra = (raw.get("extra") or "").replace("\r\n", "\n").strip() or None
    if extra and len(extra) > 500:
        raise ValidationError(
            "Доп. информация слишком длинная (максимум 500 символов)."
        )

    gather_dt, _ = parse_user_datetime(
        raw.get("gather", ""), tz, now=now, with_info=True
    )
    assert isinstance(gather_dt, datetime)
    gather_ts = int(gather_dt.timestamp())

    if require_future and gather_ts < int(now.timestamp()) - 60:
        raise ValidationError(
            "Время сбора уже прошло. Укажите будущее время или добавьте дату."
        )

    match_ts: Optional[int] = None
    match_raw = (raw.get("match") or "").strip()
    if match_raw and match_raw.lower() not in {"—", "-", "нет", "неизвестно"}:
        match_dt_raw, match_explicit = parse_user_datetime(
            match_raw, tz, now=now, base=gather_dt, with_info=True
        )
        assert isinstance(match_dt_raw, datetime)
        match_dt = match_dt_raw
        if match_dt < gather_dt:
            raise ValidationError("Время матча не может быть раньше времени сбора.")
        # Автоматический перенос на следующий день разрешаем только
        # для ночных матчей (сбор вечером, матч после полуночи).
        if (
            not match_explicit
            and match_dt.date() > gather_dt.date()
            and (gather_dt.hour < 18 or match_dt.hour > 6)
        ):
            raise ValidationError(
                "Время матча раньше времени сбора. Если матч на следующий "
                "день, укажите дату, например 25.10 01:00."
            )
        match_ts = int(match_dt.timestamp())

    return EventInput(
        title=title,
        mmr=mmr,
        gather_ts=gather_ts,
        match_ts=match_ts,
        extra=extra,
    )


def format_mmr(mmr: Optional[str]) -> Optional[str]:
    if not mmr:
        return None
    value = mmr.strip()
    if not value:
        return None
    if not re.search(r"(?i)(mmr|ммр)", value):
        value += " MMR"
    return value


def format_table_time(ts: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m %H:%M")


def format_modal_time(ts: int, tz: ZoneInfo) -> str:
    dt = datetime.fromtimestamp(ts, tz)
    if dt.year != datetime.now(tz).year:
        return dt.strftime("%d.%m.%Y %H:%M")
    return dt.strftime("%d.%m %H:%M")


def format_reminder_time(ts: int, tz: ZoneInfo, now: Optional[datetime] = None) -> str:
    dt = datetime.fromtimestamp(ts, tz)
    if now is None:
        now = datetime.now(tz)
    else:
        now = now.astimezone(tz)
    delta_days = (dt.date() - now.date()).days
    if delta_days == 0:
        day = "сегодня"
    elif delta_days == 1:
        day = "завтра"
    elif delta_days == -1:
        day = "вчера"
    else:
        day = dt.strftime("%d.%m")
    return f"{day} в {dt.strftime('%H:%M')}"


def plural_ru(number: int, one: str, few: str, many: str) -> str:
    n10 = number % 10
    n100 = number % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many
