from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    DB_PATH,
    DEFAULT_REMIND_MINUTES,
    DEFAULT_TIMEZONE,
    LOG_PATH,
    TOKEN,
)
from database import Database
from models import Event, Settings
from rendering import build_panel_embed, build_reminder_embed
from timeutils import (
    ValidationError,
    get_timezone,
    timezone_label,
)
from ui import (
    PANEL_COMPONENT_IDS,
    PanelView,
    ReminderLinkView,
    can_manage,
    handle_panel_component,
)

log = logging.getLogger(__name__)


class NotesBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="праки и турниры"
            ),
        )
        self.db = Database(DB_PATH)
        self.panel_view = PanelView(self)
        self._startup_refresh_done = False

    async def setup_hook(self) -> None:
        self.add_view(self.panel_view)
        self.reminder_worker.start()
        try:
            synced = await self.tree.sync()
            log.info("Слэш-команды синхронизированы: %s", len(synced))
        except discord.HTTPException:
            log.exception("Не удалось синхронизировать слэш-команды")

    # ------------------------------------------------------------------ panels

    def panel_url(self, settings: Settings, guild: discord.Guild) -> Optional[str]:
        if settings.notes_channel_id and settings.panel_message_id:
            return (
                f"https://discord.com/channels/{guild.id}/"
                f"{settings.notes_channel_id}/{settings.panel_message_id}"
            )
        return None

    async def _get_panel_channel(
        self, guild: discord.Guild, channel_id: int
    ) -> Optional[discord.abc.Messageable]:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                return None
            except discord.HTTPException:
                log.exception(
                    "Не удалось получить канал %s на сервере %s",
                    channel_id,
                    guild.id,
                )
                return None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    async def refresh_panel(
        self, guild: Optional[discord.Guild], *, reason: str = ""
    ) -> Optional[discord.Message]:
        if guild is None or self.user is None:
            return None

        settings = self.db.get_settings(guild.id)
        if settings is None or settings.notes_channel_id is None:
            return None

        channel = await self._get_panel_channel(guild, settings.notes_channel_id)
        if channel is None:
            log.warning(
                "Канал панели %s недоступен на сервере %s",
                settings.notes_channel_id,
                guild.id,
            )
            return None

        embed = build_panel_embed(
            self.db.list_events(guild.id), settings, guild
        )

        message: Optional[discord.Message] = None
        if settings.panel_message_id:
            try:
                message = await channel.fetch_message(settings.panel_message_id)
            except discord.NotFound:
                message = None
            except discord.Forbidden:
                log.warning(
                    "Нет доступа к сообщению панели %s на сервере %s",
                    settings.panel_message_id,
                    guild.id,
                )
                return None
            except discord.HTTPException:
                log.exception(
                    "Не удалось получить панель %s на сервере %s",
                    settings.panel_message_id,
                    guild.id,
                )
                message = None

            if message is not None and (
                self.user is None or message.author.id != self.user.id
            ):
                message = None

        if message is not None:
            try:
                await message.edit(embed=embed, view=self.panel_view)
                log.info(
                    "Панель обновлена на сервере %s (%s)", guild.id, reason
                )
                return message
            except discord.NotFound:
                message = None
            except discord.HTTPException:
                log.exception(
                    "Не удалось отредактировать панель на сервере %s", guild.id
                )
                return None

        try:
            message = await channel.send(embed=embed, view=self.panel_view)
        except discord.HTTPException:
            log.exception("Не удалось отправить панель на сервере %s", guild.id)
            return None

        self.db.set_panel_message(guild.id, channel.id, message.id)
        log.info("Панель создана на сервере %s (%s)", guild.id, reason)
        return message

    async def delete_old_panel(
        self, guild: discord.Guild, channel_id: int, message_id: int
    ) -> None:
        if self.user is None:
            return
        channel = await self._get_panel_channel(guild, channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        if message.author.id != self.user.id:
            return
        try:
            await message.delete()
            log.info("Старая панель удалена на сервере %s", guild.id)
        except discord.HTTPException:
            log.exception("Не удалось удалить старую панель на сервере %s", guild.id)

    # --------------------------------------------------------------- reminders

    @tasks.loop(seconds=30)
    async def reminder_worker(self) -> None:
        try:
            await self._process_reminders()
        except Exception:
            log.exception("Ошибка в обработчике напоминаний")

    @reminder_worker.before_loop
    async def before_reminder_worker(self) -> None:
        await self.wait_until_ready()

    async def _process_reminders(self) -> None:
        now_ts = int(time.time())
        for settings in self.db.all_settings():
            guild = self.get_guild(settings.guild_id)
            if guild is None:
                continue

            pending = self.db.pending_events(settings.guild_id)
            if not pending:
                continue

            window = settings.remind_minutes * 60
            due: list[Event] = []
            stale: list[int] = []
            for event in pending:
                if now_ts < event.gather_ts - window:
                    continue
                if now_ts <= event.gather_ts:
                    due.append(event)
                else:
                    stale.append(event.id)

            if stale:
                self.db.mark_notified(stale)
                log.info(
                    "Пропущены напоминания о прошедших сборах на сервере %s: %s",
                    guild.id,
                    len(stale),
                )
            if not due:
                continue

            try:
                tz = get_timezone(settings.timezone)
            except ValidationError:
                tz = get_timezone(DEFAULT_TIMEZONE)

            role = guild.get_role(settings.notify_role_id)
            if role is None:
                log.error(
                    "Роль уведомлений %s не найдена на сервере %s — "
                    "напоминания пропущены",
                    settings.notify_role_id,
                    guild.id,
                )
                self.db.mark_notified([event.id for event in due])
                continue

            members = [member for member in role.members if not member.bot]
            if not members:
                log.warning(
                    "В роли %s на сервере %s нет участников — напоминания пропущены",
                    role.id,
                    guild.id,
                )
                self.db.mark_notified([event.id for event in due])
                continue

            now = datetime.fromtimestamp(now_ts, tz)
            embed = build_reminder_embed(due, settings, guild, now=now)
            panel_url = self.panel_url(settings, guild)

            sent = 0
            failed: list[int] = []
            for index, member in enumerate(members):
                view = ReminderLinkView(panel_url) if panel_url else None
                try:
                    await member.send(embed=embed, view=view)
                    sent += 1
                except discord.Forbidden:
                    failed.append(member.id)
                except discord.HTTPException:
                    failed.append(member.id)
                    log.exception(
                        "Ошибка Discord при отправке ЛС участнику %s", member.id
                    )
                if index % 10 == 9:
                    await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(0.05)

            self.db.mark_notified([event.id for event in due])
            log.info(
                "Напоминание на сервере %s: событий %s, отправлено %s, "
                "ошибок %s%s",
                guild.id,
                len(due),
                sent,
                len(failed),
                f" (закрыты ЛС: {failed})" if failed else "",
            )


# ---------------------------------------------------------------------- logging


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


bot = NotesBot()


# --------------------------------------------------------------------- commands


@bot.tree.command(name="setup", description="Настроить канал, роли и напоминания")
@app_commands.describe(
    notify_role="Роль игроков, которым бот присылает напоминания в ЛС",
    editor_role="Роль редакторов записей (если не указана — только администраторы)",
    channel="Канал для таблицы (по умолчанию — текущий)",
    timezone="Часовой пояс, например Europe/Moscow",
    remind_minutes="За сколько минут до сбора напоминать",
)
@app_commands.guild_only()
async def setup_command(
    interaction: discord.Interaction,
    notify_role: discord.Role,
    editor_role: Optional[discord.Role] = None,
    channel: Optional[discord.TextChannel] = None,
    timezone: str = DEFAULT_TIMEZONE,
    remind_minutes: app_commands.Range[int, 1, 1440] = DEFAULT_REMIND_MINUTES,
) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Команда работает только на сервере.", ephemeral=True
        )
        return
    if (
        interaction.user.id != interaction.guild.owner_id
        and not interaction.user.guild_permissions.manage_guild
    ):
        await interaction.response.send_message(
            "Для настройки нужно право «Управлять сервером».", ephemeral=True
        )
        return

    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message(
            "Укажите обычный текстовый канал или запустите команду в нём.",
            ephemeral=True,
        )
        return

    if notify_role.is_default():
        await interaction.response.send_message(
            "Роль @everyone нельзя использовать для напоминаний — "
            "создайте отдельную роль игроков.",
            ephemeral=True,
        )
        return
    if editor_role is not None and editor_role.is_default():
        await interaction.response.send_message(
            "Роль @everyone нельзя сделать ролью редакторов.", ephemeral=True
        )
        return

    timezone = timezone.strip()
    try:
        get_timezone(timezone)
    except ValidationError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
        return

    me = interaction.guild.me
    if me is None:
        await interaction.response.send_message(
            "Не удалось определить участника бота на сервере. Попробуйте позже.",
            ephemeral=True,
        )
        return
    perms = target_channel.permissions_for(me)
    required = {
        "view_channel": "Просмотр канала",
        "send_messages": "Отправка сообщений",
        "embed_links": "Встраивание ссылок",
        "read_message_history": "История сообщений",
    }
    missing = [label for attr, label in required.items() if not getattr(perms, attr)]
    if missing:
        await interaction.response.send_message(
            "У бота нет прав в канале "
            f"{target_channel.mention}: {', '.join(missing)}.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    old = bot.db.get_settings(interaction.guild_id)
    same_panel = bool(
        old
        and old.notes_channel_id == target_channel.id
        and old.panel_message_id
    )

    bot.db.save_settings(
        guild_id=interaction.guild_id,
        notes_channel_id=target_channel.id,
        panel_message_id=old.panel_message_id if same_panel else None,
        notify_role_id=notify_role.id,
        editor_role_id=editor_role.id if editor_role else None,
        timezone=timezone,
        remind_minutes=int(remind_minutes),
    )

    if (
        old
        and old.notes_channel_id
        and old.panel_message_id
        and old.notes_channel_id != target_channel.id
    ):
        await bot.delete_old_panel(
            interaction.guild, old.notes_channel_id, old.panel_message_id
        )

    message = await bot.refresh_panel(interaction.guild, reason="setup")
    if message is None:
        await interaction.followup.send(
            "Настройки сохранены, но панель отправить не удалось. "
            "Проверьте права бота в канале и выполните `/panel`.",
            ephemeral=True,
        )
        return

    editor_text = (
        f"роль **{discord.utils.escape_markdown(editor_role.name)}**"
        if editor_role
        else "только администраторы"
    )
    await interaction.followup.send(
        "✅ **Настройки сохранены.**\n"
        f"Канал таблицы: {target_channel.mention}\n"
        f"Роль напоминаний: **{discord.utils.escape_markdown(notify_role.name)}** "
        f"(ЛС за {int(remind_minutes)} мин. до сбора)\n"
        f"Изменять записи могут: {editor_text}\n"
        f"Часовой пояс: **{timezone_label(timezone)}** ({timezone})\n"
        f"Панель: {message.jump_url}\n\n"
        "Убедитесь, что у игроков включены личные сообщения от участников "
        "сервера — иначе бот не сможет им написать.",
        ephemeral=True,
    )


@bot.tree.command(name="panel", description="Обновить или восстановить панель записей")
@app_commands.guild_only()
async def panel_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Команда работает только на сервере.", ephemeral=True
        )
        return

    settings = bot.db.get_settings(interaction.guild_id)
    if settings is None:
        await interaction.response.send_message(
            "Бот ещё не настроен. Сначала выполните `/setup`.", ephemeral=True
        )
        return
    if not can_manage(interaction.user, settings):
        await interaction.response.send_message(
            "Недостаточно прав для управления панелью.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    message = await bot.refresh_panel(interaction.guild, reason="panel")
    if message is None:
        channel = (
            f"<#{settings.notes_channel_id}>"
            if settings.notes_channel_id
            else "настроенный канал"
        )
        await interaction.followup.send(
            f"Не удалось обновить панель в канале {channel}. "
            "Проверьте права бота (просмотр, отправка, встраивание ссылок, "
            "история сообщений).",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"✅ Панель обновлена: {message.jump_url}", ephemeral=True
    )


@bot.tree.command(
    name="test-reminder", description="Проверить, приходит ли напоминание в ЛС"
)
@app_commands.guild_only()
async def test_reminder_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Команда работает только на сервере.", ephemeral=True
        )
        return

    settings = bot.db.get_settings(interaction.guild_id)
    if settings is None:
        await interaction.response.send_message(
            "Бот ещё не настроен. Сначала выполните `/setup`.", ephemeral=True
        )
        return
    if not can_manage(interaction.user, settings):
        await interaction.response.send_message(
            "Недостаточно прав для проверки напоминаний.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        tz = get_timezone(settings.timezone)
    except ValidationError:
        tz = get_timezone(DEFAULT_TIMEZONE)
    now = datetime.now(tz)
    fake = Event(
        id=0,
        guild_id=interaction.guild_id,
        title="Тестовая запись",
        mmr="3 250 MMR",
        gather_ts=int((now + timedelta(minutes=settings.remind_minutes)).timestamp()),
        match_ts=int((now + timedelta(minutes=settings.remind_minutes + 30)).timestamp()),
        extra="Это проверка напоминания. Если вы её видите — всё работает.",
        notified=0,
        created_by=interaction.user.id,
        created_at=int(now.timestamp()),
    )
    embed = build_reminder_embed([fake], settings, interaction.guild, now=now)
    panel_url = bot.panel_url(settings, interaction.guild)
    view = ReminderLinkView(panel_url) if panel_url else None
    try:
        await interaction.user.send(embed=embed, view=view)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Не удалось отправить ЛС. Включите в настройках Discord "
            "«Личные сообщения от участников сервера» "
            "(Настройки → Конфиденциальность и безопасность).",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        log.exception("Не удалось отправить тестовое напоминание")
        await interaction.followup.send(
            "❌ Discord не принял сообщение. Попробуйте позже.", ephemeral=True
        )
        return

    await interaction.followup.send(
        "✅ Тестовое напоминание отправлено вам в личные сообщения.",
        ephemeral=True,
    )


# ----------------------------------------------------------------------- events


@bot.event
async def on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = interaction.data.get("custom_id") if interaction.data else None
    if custom_id not in PANEL_COMPONENT_IDS:
        return
    try:
        await handle_panel_component(bot, interaction)
    except Exception:
        log.exception("Ошибка при обработке кнопки панели %s", custom_id)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Произошла ошибка. Попробуйте ещё раз.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Произошла ошибка. Попробуйте ещё раз.", ephemeral=True
                )
        except discord.HTTPException:
            pass


@bot.event
async def on_ready() -> None:
    if bot.user is not None:
        log.info(
            "Бот запущен: %s (ID %s), серверов: %s",
            bot.user,
            bot.user.id,
            len(bot.guilds),
        )
    if bot._startup_refresh_done:
        return
    bot._startup_refresh_done = True
    for guild in bot.guilds:
        if bot.db.get_settings(guild.id) is None:
            continue
        try:
            await bot.refresh_panel(guild, reason="startup")
        except Exception:
            log.exception("Не удалось обновить панель на сервере %s", guild.id)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    log.error(
        "Ошибка команды /%s",
        interaction.command.name if interaction.command else "?",
        exc_info=error,
    )
    message = "Произошла ошибка. Попробуйте ещё раз."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


def main() -> None:
    setup_logging()
    if not TOKEN:
        print(
            "Не найден DISCORD_TOKEN.\n"
            "Создайте файл .env рядом с bot.py (скопируйте .env.example) "
            "и вставьте токен бота."
        )
        raise SystemExit(1)

    try:
        bot.run(TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        print(
            "Включите Server Members Intent:\n"
            "Discord Developer Portal -> ваше приложение -> Bot -> "
            "Privileged Gateway Intents -> Server Members Intent."
        )
        raise SystemExit(1)
    except discord.LoginFailure:
        print("Неверный токен. Проверьте DISCORD_TOKEN в файле .env.")
        raise SystemExit(1)
    finally:
        bot.db.close()


if __name__ == "__main__":
    main()
