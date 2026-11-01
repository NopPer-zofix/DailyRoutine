"""The Telegram command menu (the "Menu" button / the list shown when typing ``/``)."""
from __future__ import annotations

import logging
from typing import List

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import BotCommand, BotCommandScopeChat

log = logging.getLogger(__name__)

USER_COMMANDS: List[BotCommand] = [
    BotCommand(command="tasks", description="Today's checklist"),
    BotCommand(command="add", description="Add a task"),
    BotCommand(command="delete", description="Delete tasks"),
    BotCommand(command="frequency", description="How often I remind you"),
    BotCommand(command="remind", description="Send a reminder now"),
    BotCommand(command="timezone", description="Set your timezone"),
    BotCommand(command="help", description="Explain the commands"),
]

ADMIN_COMMANDS: List[BotCommand] = USER_COMMANDS + [
    BotCommand(command="users", description="Manage who can use the bot"),
]


async def setup_menu(bot: Bot, admin_chat_id: int) -> None:
    """Publish the command menu: everyone sees the user commands, the admin also /users.

    A failure (e.g. no internet at boot) is logged and ignored - the menu is
    cosmetic and is simply set again on the next start.
    """
    try:
        await bot.set_my_commands(USER_COMMANDS)
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_chat_id))
    except (TelegramNetworkError, TelegramAPIError, OSError) as exc:
        log.warning("Could not set the command menu: %s", exc)
