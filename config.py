"""Application configuration loaded from environment variables / ``.env``."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Literal, Mapping, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

RolloverMode = Literal["reset", "carry"]

BASE_DIR = Path(__file__).resolve().parent


class ConfigError(RuntimeError):
    """Raised when the environment configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Immutable runtime settings.

    Attributes:
        bot_token: Telegram bot token from @BotFather.
        admin_chat_id: The only chat allowed to use the bot; receives reminders.
        db_path: Location of the SQLite file.
        tz: Timezone used for "today", midnight rollover and reminder hours.
        reminder_interval_hours: Gap between reminder checks (default 4).
        reminder_start_hour: First reminder check of the day (local hour).
        reminder_end_hour: Last hour in which a reminder check may fire.
        rollover_mode: ``reset`` copies every task to the new day as not-done
            (a recurring routine); ``carry`` copies only unfinished tasks.
        send_congrats: Send one short congratulation when everything is done.
        log_level: Python logging level name.
    """

    bot_token: str
    admin_chat_id: int
    db_path: Path
    tz: tzinfo
    reminder_interval_hours: int = 4
    reminder_start_hour: int = 8
    reminder_end_hour: int = 22
    rollover_mode: RolloverMode = "reset"
    send_congrats: bool = True
    log_level: str = "INFO"


def detect_local_timezone() -> tzinfo:
    """Return the system's local timezone (DST-aware where possible)."""
    try:
        from tzlocal import get_localzone

        return get_localzone()
    except Exception:  # pragma: no cover - platform specific fallback
        fallback = datetime.now().astimezone().tzinfo
        assert fallback is not None
        return fallback


def parse_timezone(name: str) -> ZoneInfo:
    """Validate an IANA timezone name such as ``Europe/Berlin``.

    Raises:
        ValueError: If the name is unknown or malformed.
    """
    name = name.strip()
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError, OSError) as exc:  # ZoneInfoNotFoundError is a KeyError
        raise ValueError(f"Unknown timezone {name!r}") from exc


def resolve_timezone(name: Optional[str], default: tzinfo) -> tzinfo:
    """A user's stored timezone, or ``default`` if unset / no longer valid."""
    if not name:
        return default
    try:
        return parse_timezone(name)
    except ValueError:
        return default


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(env: Mapping[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}, got {value}")
    return value


def load_config(
    env: Optional[Mapping[str, str]] = None, *, read_dotenv: bool = True
) -> Config:
    """Build a :class:`Config`.

    Args:
        env: Mapping to read from. Defaults to ``os.environ`` (after loading
            ``.env`` when ``read_dotenv`` is true).
        read_dotenv: Whether to load ``.env`` from the project directory.

    Raises:
        ConfigError: If a required value is missing or malformed.
    """
    if env is None:
        if read_dotenv:
            load_dotenv(BASE_DIR / ".env")
        env = os.environ

    token = env.get("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN is not set (see .env.example)")

    raw_admin = env.get("ADMIN_CHAT_ID", "").strip()
    if not raw_admin:
        raise ConfigError("ADMIN_CHAT_ID is not set (see .env.example)")
    try:
        admin_chat_id = int(raw_admin)
    except ValueError as exc:
        raise ConfigError(f"ADMIN_CHAT_ID must be an integer, got {raw_admin!r}") from exc

    tz_name = env.get("TIMEZONE", "").strip()
    if tz_name:
        try:
            tz: tzinfo = ZoneInfo(tz_name)
        except Exception as exc:
            raise ConfigError(f"Unknown TIMEZONE {tz_name!r}") from exc
    else:
        tz = detect_local_timezone()

    start = _parse_int(env, "REMINDER_START_HOUR", 8, 0, 23)
    end = _parse_int(env, "REMINDER_END_HOUR", 22, 0, 23)
    if end < start:
        raise ConfigError("REMINDER_END_HOUR must be >= REMINDER_START_HOUR")

    mode = env.get("ROLLOVER_MODE", "reset").strip().lower() or "reset"
    if mode not in ("reset", "carry"):
        raise ConfigError(f"ROLLOVER_MODE must be 'reset' or 'carry', got {mode!r}")

    db_path = Path(env.get("DB_PATH", "") or BASE_DIR / "data" / "routine.db")
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    return Config(
        bot_token=token,
        admin_chat_id=admin_chat_id,
        db_path=db_path,
        tz=tz,
        reminder_interval_hours=_parse_int(env, "REMINDER_INTERVAL_HOURS", 4, 1, 24),
        reminder_start_hour=start,
        reminder_end_hour=end,
        rollover_mode=mode,  # type: ignore[arg-type]
        send_congrats=_parse_bool(env.get("SEND_CONGRATS", "true")),
        log_level=env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
