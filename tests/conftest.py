"""Shared fixtures. Everything here runs offline: in-memory SQLite and a fake bot."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import handlers
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

    monkeypatch.setattr(handlers, "local_now", lambda tz: NOW.astimezone(tz))
    return NOW


class Fed:
    """Result of pushing one update through a real Dispatcher."""

    def __init__(self, result, bot) -> None:
        self.result = result
        self.bot = bot

    def calls(self, method_name: str = "SendMessage") -> list:
        """Telegram API methods that the bot tried to call (HTTP layer is mocked)."""
        return [
            c.args[1]
            for c in self.bot.session.make_request.await_args_list
            if type(c.args[1]).__name__ == method_name
        ]

    def texts_to(self, chat_id: int) -> list:
        return [m.text for m in self.calls() if m.chat_id == chat_id]


@pytest.fixture
def feed(db, config):
    """``await feed(chat_id, "/tasks")`` or ``await feed(chat_id, data="acc:...")``."""

    async def _feed(
        chat_id: int,
        text: str = "/tasks",
        *,
        data: str = "",
        chat_type: str = "private",
        username: str = "",
    ) -> Fed:
        dp = Dispatcher(storage=MemoryStorage(), db=db, config=config)
        dp.include_router(handlers.build_router(config))
        bot = Bot("123:abc")
        bot.session.make_request = AsyncMock(return_value=MagicMock())  # no network
        user = User(id=chat_id, is_bot=False, first_name="Test", username=username or None)
        message = Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=chat_id, type=chat_type),
            from_user=user,
            text=None if data else text,
        )
        if data:
            update = Update(
                update_id=1,
                callback_query=CallbackQuery(
                    id="1", from_user=user, chat_instance="x", data=data, message=message
                ),
            )
        else:
            update = Update(update_id=1, message=message)
        return Fed(await dp.feed_update(bot, update), bot)

    return _feed
