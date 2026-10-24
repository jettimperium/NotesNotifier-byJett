from __future__ import annotations

import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

import discord

from config import MAX_EVENTS
from models import Event, Settings
from timeutils import (
    ValidationError,
    format_modal_time,
    format_table_time,
    get_timezone,
    parse_event_input,
)

if TYPE_CHECKING:
    from bot import NotesBot

log = logging.getLogger(__name__)


def can_manage(member: discord.Member, settings: Settings) -> bool:
    if member.id == member.guild.owner_id:
        return True
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    if settings.editor_role_id:
        return any(role.id == settings.editor_role_id for role in member.roles)
    return False


def _manager_hint(settings: Settings, guild: discord.Guild) -> str:
    if settings.editor_role_id:
        role = guild.get_role(settings.editor_role_id)
        if role:
            return f"администраторы или роль **{role.name}**"
    return "администраторы"


class EphemeralView(discord.ui.View):
    """Одноразовое меню. Уникальные custom_id нужны, чтобы несколько
    одинаковых меню в разных сообщениях не конфликтовали."""

    def __init__(self, *, timeout: float = 180) -> None:
        super().__init__(timeout=timeout)
        for child in self.children:
            try:
                child.custom_id = f"{self.__class__.__name__}:{uuid4().hex}"
            except AttributeError:
                pass


class ReminderLinkView(discord.ui.View):
    """Кнопка-ссылка на панель записей в напоминании."""

    def __init__(self, url: str) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Открыть записи",
                emoji="📋",
                style=discord.ButtonStyle.link,
                url=url,
            )
        )


class RetryView(EphemeralView):
    def __init__(
        self,
        bot: "NotesBot",
        raw: dict[str, str],
        event_id: Optional[int] = None,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.raw = raw
        self.event_id = event_id

    @discord.ui.button(label="Заполнить заново", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def retry(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Эта кнопка работает только на сервере.", ephemeral=True
            )
            return

        if self.event_id is not None:
            event = self.bot.db.get_event(interaction.guild_id, self.event_id)
            if event is None:
                await interaction.response.send_message(
                    "Эта запись уже удалена.", ephemeral=True
                )
                return
            modal: discord.ui.Modal = EditEventModal(self.bot, event, prefill=self.raw)
        else:
            modal = AddEventModal(self.bot, prefill=self.raw)
        await interaction.response.send_modal(modal)


def _default(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    value = value[:limit]
    return value if value.strip() else None


class EditSelectView(EphemeralView):
    def __init__(self, bot: "NotesBot", events: list[Event], tz: ZoneInfo) -> None:
        super().__init__(timeout=180)
        self.bot = bot

        options = []
        for index, event in enumerate(events[:25], start=1):
            match = (
                format_table_time(event.match_ts, tz) if event.match_ts else "без матча"
            )
            options.append(
                discord.SelectOption(
                    label=f"№{index} · {event.title}"[:100],
                    value=str(event.id),
                    description=f"Сбор {format_table_time(event.gather_ts, tz)} · Матч {match}"[:100],
                )
            )

        select = discord.ui.Select(
            placeholder="Выберите запись для изменения",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"edit-select:{uuid4().hex}",
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Это меню работает только на сервере.", ephemeral=True
            )
            return
        select: discord.ui.Select = self.children[0]  # type: ignore[assignment]
        try:
            event_id = int(select.values[0])
        except (ValueError, IndexError):
            await interaction.response.send_message(
                "Не удалось определить запись. Попробуйте ещё раз.", ephemeral=True
            )
            return

        event = self.bot.db.get_event(interaction.guild_id, event_id)
        if event is None:
            await interaction.response.send_message(
                "Эта запись уже удалена.", ephemeral=True
            )
            return
        await interaction.response.send_modal(EditEventModal(self.bot, event))


class ConfirmClearView(EphemeralView):
    def __init__(self, bot: "NotesBot") -> None:
        super().__init__(timeout=60)
        self.bot = bot

    async def _check(self, interaction: discord.Interaction) -> Optional[Settings]:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Это меню работает только на сервере.", ephemeral=True
            )
            return None
        settings = self.bot.db.get_settings(interaction.guild_id)
        if settings is None:
            await interaction.response.send_message(
                "Бот ещё не настроен. Выполните `/setup`.", ephemeral=True
            )
            return None
        if not can_manage(interaction.user, settings):
            await interaction.response.send_message(
                f"Недостаточно прав. Управлять записями могут "
                f"{_manager_hint(settings, interaction.guild)}.",
                ephemeral=True,
            )
            return None
        return settings

    @discord.ui.button(label="Да, очистить", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self._check(interaction) is None:
            return
        removed = self.bot.db.clear_events(interaction.guild_id)
        await interaction.response.edit_message(
            content=f"✅ Таблица очищена. Удалено записей: {removed}.",
            view=None,
        )
        message = await self.bot.refresh_panel(interaction.guild, reason="clear")
        if message is None:
            await interaction.followup.send(
                "⚠️ Записи удалены, но панель обновить не удалось. "
                "Проверьте права бота и выполните `/panel`.",
                ephemeral=True,
            )

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Очистка отменена.", view=None)


class BaseEventModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Ошибка в модальном окне", exc_info=error)
        message = "Произошла ошибка. Попробуйте ещё раз или напишите администратору."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class AddEventModal(BaseEventModal):
    def __init__(self, bot: "NotesBot", prefill: Optional[dict[str, str]] = None) -> None:
        super().__init__(title="Добавить информацию")
        self.bot = bot
        p = prefill or {}

        self.title_input = discord.ui.TextInput(
            label="Название",
            placeholder="Например: 20:00 без смурфов",
            default=_default(p.get("title"), 100),
            required=True,
            max_length=100,
        )
        self.mmr_input = discord.ui.TextInput(
            label="MMR / уровень",
            placeholder="Например: 3 250 MMR",
            default=_default(p.get("mmr"), 50),
            required=False,
            max_length=50,
        )
        self.gather_input = discord.ui.TextInput(
            label="Время сбора",
            placeholder="19:40 или 24.10 19:40",
            default=_default(p.get("gather"), 40),
            required=True,
            max_length=40,
        )
        self.match_input = discord.ui.TextInput(
            label="Время матча",
            placeholder="20:00 или 24.10 20:00 (можно пропустить)",
            default=_default(p.get("match"), 40),
            required=False,
            max_length=40,
        )
        self.extra_input = discord.ui.TextInput(
            label="Доп. информация",
            placeholder="Комментарий, ссылка, требования к составу…",
            default=_default(p.get("extra"), 500),
            required=False,
            max_length=500,
            style=discord.TextStyle.paragraph,
        )
        for item in (
            self.title_input,
            self.mmr_input,
            self.gather_input,
            self.match_input,
            self.extra_input,
        ):
            self.add_item(item)

    def raw(self) -> dict[str, str]:
        return {
            "title": self.title_input.value or "",
            "mmr": self.mmr_input.value or "",
            "gather": self.gather_input.value or "",
            "match": self.match_input.value or "",
            "extra": self.extra_input.value or "",
        }

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Эта форма работает только на сервере.", ephemeral=True
            )
            return
        settings = self.bot.db.get_settings(interaction.guild_id)
        if settings is None:
            await interaction.response.send_message(
                "Бот ещё не настроен. Выполните `/setup`.", ephemeral=True
            )
            return
        if not can_manage(interaction.user, settings):
            await interaction.response.send_message(
                f"Недостаточно прав. Управлять записями могут "
                f"{_manager_hint(settings, interaction.guild)}.",
                ephemeral=True,
            )
            return
        if self.bot.db.count_events(interaction.guild_id) >= MAX_EVENTS:
            await interaction.response.send_message(
                f"Достигнут лимит записей ({MAX_EVENTS}). "
                "Очистите таблицу, чтобы добавить новые.",
                ephemeral=True,
            )
            return

        raw = self.raw()
        try:
            data = parse_event_input(
                raw, settings.timezone, require_future=True
            )
        except ValidationError as exc:
            await interaction.response.send_message(
                f"⚠️ {exc}", ephemeral=True, view=RetryView(self.bot, raw)
            )
            return

        self.bot.db.add_event(
            guild_id=interaction.guild_id,
            title=data.title,
            mmr=data.mmr,
            gather_ts=data.gather_ts,
            match_ts=data.match_ts,
            extra=data.extra,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(
            "✅ Запись добавлена в таблицу.", ephemeral=True
        )
        message = await self.bot.refresh_panel(interaction.guild, reason="add")
        if message is None:
            await interaction.followup.send(
                "⚠️ Запись сохранена, но панель обновить не удалось. "
                "Проверьте права бота и выполните `/panel`.",
                ephemeral=True,
            )


class EditEventModal(BaseEventModal):
    def __init__(
        self,
        bot: "NotesBot",
        event: Event,
        prefill: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(title="Изменить информацию")
        self.bot = bot
        self.event_id = event.id

        if prefill is None:
            settings = bot.db.get_settings(event.guild_id)
            tz_name = settings.timezone if settings else "Europe/Moscow"
            try:
                tz = get_timezone(tz_name)
            except ValidationError:
                tz = ZoneInfo("UTC")
            prefill = {
                "title": event.title,
                "mmr": event.mmr or "",
                "gather": format_modal_time(event.gather_ts, tz),
                "match": format_modal_time(event.match_ts, tz) if event.match_ts else "",
                "extra": event.extra or "",
            }
        p = prefill

        self.title_input = discord.ui.TextInput(
            label="Название",
            default=_default(p.get("title"), 100),
            required=True,
            max_length=100,
        )
        self.mmr_input = discord.ui.TextInput(
            label="MMR / уровень",
            default=_default(p.get("mmr"), 50),
            required=False,
            max_length=50,
        )
        self.gather_input = discord.ui.TextInput(
            label="Время сбора",
            placeholder="19:40 или 24.10 19:40",
            default=_default(p.get("gather"), 40),
            required=True,
            max_length=40,
        )
        self.match_input = discord.ui.TextInput(
            label="Время матча",
            placeholder="20:00 или 24.10 20:00 (можно пропустить)",
            default=_default(p.get("match"), 40),
            required=False,
            max_length=40,
        )
        self.extra_input = discord.ui.TextInput(
            label="Доп. информация",
            default=_default(p.get("extra"), 500),
            required=False,
            max_length=500,
            style=discord.TextStyle.paragraph,
        )
        for item in (
            self.title_input,
            self.mmr_input,
            self.gather_input,
            self.match_input,
            self.extra_input,
        ):
            self.add_item(item)

    def raw(self) -> dict[str, str]:
        return {
            "title": self.title_input.value or "",
            "mmr": self.mmr_input.value or "",
            "gather": self.gather_input.value or "",
            "match": self.match_input.value or "",
            "extra": self.extra_input.value or "",
        }

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Эта форма работает только на сервере.", ephemeral=True
            )
            return
        settings = self.bot.db.get_settings(interaction.guild_id)
        if settings is None:
            await interaction.response.send_message(
                "Бот ещё не настроен. Выполните `/setup`.", ephemeral=True
            )
            return
        if not can_manage(interaction.user, settings):
            await interaction.response.send_message(
                f"Недостаточно прав. Управлять записями могут "
                f"{_manager_hint(settings, interaction.guild)}.",
                ephemeral=True,
            )
            return

        event = self.bot.db.get_event(interaction.guild_id, self.event_id)
        if event is None:
            await interaction.response.send_message(
                "Эта запись уже удалена.", ephemeral=True
            )
            return

        raw = self.raw()
        try:
            data = parse_event_input(
                raw, settings.timezone, require_future=False
            )
        except ValidationError as exc:
            await interaction.response.send_message(
                f"⚠️ {exc}",
                ephemeral=True,
                view=RetryView(self.bot, raw, event_id=self.event_id),
            )
            return

        now_ts = int(time.time())
        gather_changed = data.gather_ts != event.gather_ts
        if gather_changed:
            notified = 1 if data.gather_ts <= now_ts else 0
        else:
            notified = event.notified

        updated = self.bot.db.update_event(
            guild_id=interaction.guild_id,
            event_id=self.event_id,
            title=data.title,
            mmr=data.mmr,
            gather_ts=data.gather_ts,
            match_ts=data.match_ts,
            extra=data.extra,
            notified=notified,
        )
        if not updated:
            await interaction.response.send_message(
                "Не удалось изменить запись — возможно, её уже удалили.",
                ephemeral=True,
            )
            return

        text = "✅ Запись обновлена."
        if gather_changed and data.gather_ts > now_ts:
            text += (
                f" Напоминание будет отправлено за {settings.remind_minutes} "
                "мин. до нового времени сбора."
            )
        elif gather_changed:
            text += " Новое время сбора уже прошло — напоминание не отправится."

        await interaction.response.send_message(text, ephemeral=True)
        message = await self.bot.refresh_panel(interaction.guild, reason="edit")
        if message is None:
            await interaction.followup.send(
                "⚠️ Запись сохранена, но панель обновить не удалось. "
                "Проверьте права бота и выполните `/panel`.",
                ephemeral=True,
            )


PANEL_COMPONENT_IDS = frozenset({"notes:add", "notes:edit", "notes:clear"})


async def _resolve_panel(
    bot: "NotesBot", interaction: discord.Interaction
) -> tuple[Optional[Settings], Optional[str]]:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return None, "Эта панель работает только на сервере."
    try:
        settings = bot.db.get_settings(interaction.guild_id)
    except sqlite3.Error:
        log.exception(
            "Не удалось прочитать настройки сервера %s", interaction.guild_id
        )
        return None, "База данных временно занята. Попробуйте ещё раз."
    if settings is None:
        return (
            None,
            "Бот ещё не настроен. Администратор должен выполнить `/setup`.",
        )
    if not can_manage(interaction.user, settings):
        return (
            None,
            f"Недостаточно прав. Управлять записями могут "
            f"{_manager_hint(settings, interaction.guild)}.",
        )
    return settings, None


async def _handle_panel_add(
    bot: "NotesBot", interaction: discord.Interaction
) -> None:
    log.info(
        "Кнопка «Добавить информацию»: сервер=%s, пользователь=%s",
        interaction.guild_id,
        interaction.user.id,
    )
    # Модальное окно отправляем сразу, не дожидаясь запросов к базе:
    # у Discord на ответ есть всего 3 секунды. Права и лимит проверит
    # форма при отправке.
    await interaction.response.send_modal(AddEventModal(bot))


async def _handle_panel_edit(
    bot: "NotesBot", interaction: discord.Interaction
) -> None:
    log.info(
        "Кнопка «Изменить информацию»: сервер=%s, пользователь=%s",
        interaction.guild_id,
        interaction.user.id,
    )
    await interaction.response.defer(ephemeral=True, thinking=True)
    settings, error = await _resolve_panel(bot, interaction)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return
    events = bot.db.list_events(interaction.guild_id)
    if not events:
        await interaction.followup.send(
            "Таблица пуста — изменять пока нечего.", ephemeral=True
        )
        return
    try:
        tz = get_timezone(settings.timezone)
    except ValidationError:
        await interaction.followup.send(
            "На сервере указан неверный часовой пояс. "
            "Администратор должен повторить `/setup`.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        "Выберите запись, которую нужно изменить:",
        view=EditSelectView(bot, events, tz),
        ephemeral=True,
    )


async def _handle_panel_clear(
    bot: "NotesBot", interaction: discord.Interaction
) -> None:
    log.info(
        "Кнопка «Очистить информацию»: сервер=%s, пользователь=%s",
        interaction.guild_id,
        interaction.user.id,
    )
    await interaction.response.defer(ephemeral=True, thinking=True)
    _, error = await _resolve_panel(bot, interaction)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return
    count = bot.db.count_events(interaction.guild_id)
    if count == 0:
        await interaction.followup.send("Таблица уже пуста.", ephemeral=True)
        return
    await interaction.followup.send(
        f"Удалить **все {count}** записей? Это действие нельзя отменить.",
        view=ConfirmClearView(bot),
        ephemeral=True,
    )


async def handle_panel_component(
    bot: "NotesBot", interaction: discord.Interaction
) -> bool:
    """Обрабатывает кнопки панели через on_interaction.

    Это не зависит от того, нашла ли discord.py сохранённое persistent-view,
    поэтому кнопки продолжают работать и после перезапуска бота.
    """
    custom_id = interaction.data.get("custom_id") if interaction.data else None
    if custom_id == "notes:add":
        await _handle_panel_add(bot, interaction)
    elif custom_id == "notes:edit":
        await _handle_panel_edit(bot, interaction)
    elif custom_id == "notes:clear":
        await _handle_panel_clear(bot, interaction)
    else:
        return False
    return True


class PanelView(discord.ui.View):
    """Только внешний вид панели. Нажатия обрабатывает on_interaction."""

    def __init__(self, bot: "NotesBot") -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.add_item(
            discord.ui.Button(
                label="Добавить информацию",
                emoji="➕",
                style=discord.ButtonStyle.success,
                custom_id="notes:add",
                row=0,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Изменить информацию",
                emoji="✏️",
                style=discord.ButtonStyle.primary,
                custom_id="notes:edit",
                row=0,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Очистить информацию",
                emoji="🗑️",
                style=discord.ButtonStyle.danger,
                custom_id="notes:clear",
                row=1,
            )
        )
