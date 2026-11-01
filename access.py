"""Access control: admin always in, approved users in, everybody else asks first."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from database import MAX_PENDING_REQUESTS, Database
from keyboards import request_keyboard
from reminders import send_with_retry
from texts import access_request_text

log = logging.getLogger(__name__)

MAX_NAME_LENGTH = 100

Event = Union[Message, CallbackQuery]
Handler = Callable[[Event, Dict[str, Any]], Awaitable[Any]]


def display_name_of(message: Message) -> str:
    """``Full Name (@username)`` for the admin's notification and /users list."""
    user = message.from_user
    if user is None:
        return f"chat {message.chat.id}"
    name = user.full_name
    if user.username:
        name += f" (@{user.username})"
    return name[:MAX_NAME_LENGTH]


class AccessMiddleware(BaseMiddleware):
    """Runs before every handler; only lets authorised chats through.

    * the admin chat -> always allowed;
    * ``approved`` private chats -> allowed;
    * unknown chat -> an access request is created and the admin is notified;
    * ``pending`` -> told to wait; ``denied`` / ``revoked`` -> ignored silently.

    Non-admin group chats are ignored entirely: task lists are personal.
    """

    def __init__(self, admin_chat_id: int) -> None:
        self.admin_chat_id = admin_chat_id

    async def __call__(self, handler: Handler, event: Event, data: Dict[str, Any]) -> Any:
        message: Optional[Message] = event if isinstance(event, Message) else event.message  # type: ignore[assignment]
        if not isinstance(message, Message):
            return None
        chat = message.chat

        if chat.id == self.admin_chat_id:
            return await handler(event, data)
        if chat.type != "private":
            return None

        db: Database = data["db"]
        status = db.get_status(chat.id)
        if status == "approved":
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer()  # stale button from a revoked user: just stop the spinner
            return None
        await self._handle_stranger(message, status, data)
        return None

    async def _handle_stranger(
        self, message: Message, status: Optional[str], data: Dict[str, Any]
    ) -> None:
        if status in ("denied", "revoked"):
            return  # silently ignored; the admin can re-approve from /users
        if status == "pending":
            await message.answer("⏳ Your access request is still waiting for approval.")
            return

        db: Database = data["db"]
        if db.pending_count() >= MAX_PENDING_REQUESTS:
            log.warning("Too many pending requests; ignoring chat %s", message.chat.id)
            await message.answer("🔒 This is a private bot and isn't taking requests right now.")
            return

        name = display_name_of(message)
        db.request_access(message.chat.id, name)
        log.info("Access request from %s (%s)", name, message.chat.id)
        await message.answer(
            "🔒 This is a private bot. I've asked the owner for access - "
            "you'll get a message here once you're approved."
        )
        await send_with_retry(
            data["bot"],
            self.admin_chat_id,
            access_request_text(name, message.chat.id),
            request_keyboard(message.chat.id),
        )
