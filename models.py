from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Settings:
    guild_id: int
    notes_channel_id: Optional[int]
    panel_message_id: Optional[int]
    notify_role_id: int
    editor_role_id: Optional[int]
    timezone: str
    remind_minutes: int


@dataclass
class Event:
    id: int
    guild_id: int
    title: str
    mmr: Optional[str]
    gather_ts: int
    match_ts: Optional[int]
    extra: Optional[str]
    notified: int
    created_by: Optional[int]
    created_at: int


@dataclass
class EventInput:
    title: str
    mmr: Optional[str]
    gather_ts: int
    match_ts: Optional[int]
    extra: Optional[str]
