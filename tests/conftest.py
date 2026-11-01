"""Shared fixtures. Everything here runs offline: in-memory SQLite and a fake bot."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Message

from config import Config
from database import Database

CHAT = 42
TZ = ZoneInfo("Europe/Berlin")
TODAY = date(2025, 3, 10)
NOW = datetime(2025, 3, 10, 12, 0, tzinfo=TZ)


@pytest.fixture
def db() -> Iterator[Database]:
    """Fresh in-memory database per test."""
    database = Database(":memory:")
    database.connect()
    yield database
    database.close()


@pytest.fixture
def config() -> Config:
    return Config(
        bot_token="123:test",
        admin_chat_id=CHAT,
        db_path=Path(":memory:"),
        tz=TZ,
    )


@pytest.fixture
def bot() -> MagicMock:
    """Stand-in for aiogram's Bot: no network, records every call."""
    fake = MagicMock(spec=Bot)
    fake.send_message = AsyncMock(return_value=MagicMock(spec=Message))
    return fake


@pytest.fixture
def make_message():
    """Factory for a mocked incoming Message."""

    def _make(text: str = "", chat_id: int = CHAT) -> MagicMock:
        msg = MagicMock(spec=Message)
        msg.text = text
        msg.chat = MagicMock(id=chat_id)
        msg.answer = AsyncMock()
        msg.edit_text = AsyncMock()
        return msg

    return _make


@pytest.fixture
def make_callback(make_message):
    """Factory for a mocked CallbackQuery attached to a mocked Message."""

    def _make(chat_id: int = CHAT) -> MagicMock:
        cb = MagicMock(spec=CallbackQuery)
        cb.message = make_message(chat_id=chat_id)
        cb.answer = AsyncMock()
        return cb

    return _make


@pytest.fixture
def fsm() -> AsyncMock:
    """Mocked FSMContext."""
    return AsyncMock()


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    """Pin the handlers' notion of "now" so tests don't depend on the real date."""
    import handlers

    monkeypatch.setattr(handlers, "local_now", lambda cfg: NOW)
    return NOW
