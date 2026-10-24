from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _resolve_path(value: str, default: str) -> Path:
    path = Path(value or default)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_PATH = _resolve_path(os.getenv("DB_PATH", ""), "data/notes.db")
LOG_PATH = _resolve_path(os.getenv("LOG_PATH", ""), "data/bot.log")

DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow")
DEFAULT_REMIND_MINUTES = int(os.getenv("DEFAULT_REMIND_MINUTES", "20"))
MAX_EVENTS = max(1, min(int(os.getenv("MAX_EVENTS", "25")), 25))
