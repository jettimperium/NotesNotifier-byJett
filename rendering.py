from __future__ import annotations

import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import discord

from models import Event, Settings
from timeutils import (
    format_mmr,
    format_reminder_time,
    format_table_time,
    plural_ru,
    timezone_label,
)

TABLE_MAX_WIDTHS = (2, 24, 11, 11, 15)
TABLE_HEADERS = ("№", "Название", "Сбор", "Матч", "Доп. информация")


def _clean(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = text.replace("`", "'")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _cell(value: object, width: int) -> str:
    text = _clean(value)
    if len(text) > width:
        text = text[: max(0, width - 1)].rstrip() + "…"
    return text.ljust(width)


def _row(values: tuple[str, ...], widths: tuple[int, ...]) -> str:
    # Один пробел, а не два: за счёт этого название получает больше места
    # при той же общей ширине таблицы.
    return " ".join(_cell(value, width) for value, width in zip(values, widths))


def _event_values(event: Event, tz: ZoneInfo, index: int) -> tuple[str, ...]:
    extra_parts = [
        part
        for part in (format_mmr(event.mmr), _clean(event.extra or ""))
        if part
    ]
    return (
        str(index),
        event.title,
        format_table_time(event.gather_ts, tz),
        format_table_time(event.match_ts, tz) if event.match_ts else "—",
        " · ".join(extra_parts) or "—",
    )


def _table_widths(rows: list[tuple[str, ...]]) -> tuple[int, ...]:
    widths = [len(header) for header in TABLE_HEADERS]
    for values in rows:
        for index, value in enumerate(values):
            widths[index] = min(
                TABLE_MAX_WIDTHS[index],
                max(widths[index], len(_clean(value))),
            )
    return tuple(widths)


def build_table(events: list[Event], tz: ZoneInfo) -> str:
    rows = [
        _event_values(event, tz, index)
        for index, event in enumerate(events, start=1)
    ]
    widths = _table_widths(rows)
    lines = [
        _row(TABLE_HEADERS, widths),
        " ".join("-" * width for width in widths),
    ]
    lines.extend(_row(values, widths) for values in rows)
    return "\n".join(lines)


def _editor_label(guild: discord.Guild, settings: Settings) -> str:
    if settings.editor_role_id:
        role = guild.get_role(settings.editor_role_id)
        if role:
            return f"роль «{role.name}»"
        return "администраторы (роль редакторов удалена)"
    return "администраторы"


def build_panel_embed(
    events: list[Event], settings: Settings, guild: discord.Guild
) -> discord.Embed:
    tz = ZoneInfo(settings.timezone)
    table = build_table(events, tz)
    description = f"```\n{table}\n```"
    if not events:
        description += (
            "\n\n*Таблица пуста. Нажмите «Добавить информацию», "
            "чтобы создать первую запись.*"
        )

    footer = (
        f"Время: {timezone_label(settings.timezone)} • "
        f"Напоминание за {settings.remind_minutes} "
        f"{plural_ru(settings.remind_minutes, 'минуту', 'минуты', 'минут')} "
        f"до сбора • Изменять: {_editor_label(guild, settings)}"
    )

    embed = discord.Embed(
        title="📋 Праки и турниры",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=footer)
    return embed


def build_reminder_embed(
    events: list[Event],
    settings: Settings,
    guild: discord.Guild,
    *,
    now: Optional[datetime] = None,
) -> discord.Embed:
    tz = ZoneInfo(settings.timezone)
    if now is None:
        now = datetime.now(tz)
    else:
        now = now.astimezone(tz)

    minutes = settings.remind_minutes
    if events:
        nearest = min(event.gather_ts for event in events)
        minutes = max(0, round((nearest - now.timestamp()) / 60))

    if minutes <= 0:
        timing = "Сбор начинается прямо сейчас!"
    else:
        timing = (
            f"До сбора примерно **{minutes} "
            f"{plural_ru(minutes, 'минута', 'минуты', 'минут')}**."
        )

    embed = discord.Embed(
        title="⏰ Скоро сбор!",
        description=f"{timing} Проверьте время и не забудьте зайти в игру.",
        color=discord.Color.orange(),
    )
    content_used = len(embed.title or "") + len(embed.description or "")

    for event in events:
        if len(embed.fields) >= 25:
            break
        lines = [
            f"🕒 Сбор: **{format_reminder_time(event.gather_ts, tz, now)}**"
        ]
        if event.match_ts:
            lines.append(
                f"🎮 Матч: {format_reminder_time(event.match_ts, tz, now)}"
            )
        mmr = format_mmr(event.mmr)
        if mmr:
            lines.append(f"📊 MMR: {_clean(mmr)}")
        if event.extra:
            lines.append(f"📝 {event.extra}")
        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1023].rstrip() + "…"
        name = f"**{_clean(event.title)[:250]}**"

        # Защита от превышения лимита embed (6000 символов): лучше показать
        # меньше подробностей и дать ссылку на таблицу, чем не отправить ЛС.
        estimated = len(_clean(name)) + len(_clean(value)) + 32
        if embed.fields and content_used + estimated > 5500:
            break
        embed.add_field(name=name, value=value, inline=False)
        content_used += estimated

    if len(embed.fields) < 25 and len(embed.fields) < len(events):
        hidden = len(events) - len(embed.fields)
        embed.add_field(
            name="…и ещё записи",
            value=(
                f"Ещё {hidden} — откройте таблицу, чтобы посмотреть "
                "все сборы."
            ),
            inline=False,
        )

    embed.set_footer(
        text=(
            f"{guild.name} • Время: {timezone_label(settings.timezone)} "
            f"({settings.timezone})"
        )
    )
    return embed
