"""Telegram command and callback handlers.

Handlers receive their dependencies (``db``, ``config``) through aiogram's
dependency injection (``Dispatcher(db=..., config=...)``), which also makes
them trivial to call directly with mocks in tests.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional, Tuple

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from config import Config
from database import Database
from keyboards import (
    VIEW_REMINDER,
    TaskCB,
    delete_keyboard,
    reminder_keyboard,
    tasks_keyboard,
)
from texts import (
    HELP_TEXT,
    congrats_text,
    dashboard_text,
    delete_text,
    reminder_text,
)

log = logging.getLogger(__name__)


class AddTask(StatesGroup):
    """FSM: the next plain-text message becomes a new task."""

    waiting_for_title = State()


# --------------------------------------------------------------------- helpers
def local_now(config: Config) -> datetime:
    """Timezone-aware current time in the configured local zone."""
    return datetime.now(config.tz)


def prepare_today(db: Database, config: Config, chat_id: int) -> Tuple[datetime, date]:
    """Return ``(now, today)`` after making sure today's list exists.

    Rolling over lazily here means the bot is correct on the first interaction
    of a new day even if the midnight job has not run (e.g. the Pi was off).
    """
    now = local_now(config)
    db.roll_over(chat_id, now.date(), config.rollover_mode)
    return now, now.date()


def render_list(db: Database, config: Config, chat_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Main checklist message for today."""
    _, today = prepare_today(db, config, chat_id)
    tasks = db.list_tasks(chat_id, today)
    return dashboard_text(tasks, today), tasks_keyboard(tasks)


def render_delete(db: Database, config: Config, chat_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    _, today = prepare_today(db, config, chat_id)
    tasks = db.list_tasks(chat_id, today)
    return delete_text(tasks), delete_keyboard(tasks)


def render_reminder(
    db: Database, config: Config, chat_id: int
) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Reminder-style message; collapses to a congratulation once all is done."""
    _, today = prepare_today(db, config, chat_id)
    tasks = db.list_tasks(chat_id, today)
    if tasks and all(t.is_done for t in tasks):
        return congrats_text(len(tasks)), None
    if not tasks:
        return dashboard_text(tasks, today), tasks_keyboard(tasks)
    return reminder_text(tasks), reminder_keyboard(tasks)


async def safe_edit(
    message: Message, text: str, markup: Optional[InlineKeyboardMarkup]
) -> None:
    """Edit a message, ignoring Telegram's "message is not modified" error."""
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


# -------------------------------------------------------------------- commands
async def cmd_start(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    await state.clear()
    db.ensure_chat(message.chat.id)
    await message.answer(HELP_TEXT)
    text, markup = render_list(db, config, message.chat.id)
    await message.answer(text, reply_markup=markup)


async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT)


async def cmd_tasks(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    await state.clear()
    text, markup = render_list(db, config, message.chat.id)
    await message.answer(text, reply_markup=markup)


async def cmd_add(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    db: Database,
    config: Config,
) -> None:
    """``/add <text>`` adds immediately; bare ``/add`` asks for the text."""
    if not command.args:
        await state.set_state(AddTask.waiting_for_title)
        await message.answer("What should I add? Send the task text (or /cancel).")
        return
    await _create_task(message, state, db, config, command.args)


async def add_title(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    """Receives the text after a bare ``/add`` or the ➕ Add button."""
    await _create_task(message, state, db, config, message.text or "")


async def _create_task(
    message: Message, state: FSMContext, db: Database, config: Config, title: str
) -> None:
    _, today = prepare_today(db, config, message.chat.id)
    try:
        task = db.add_task(message.chat.id, title, today)
    except ValueError as exc:
        # Keep waiting (if we were) so the user can just retry.
        await message.answer(f"⚠️ {exc}")
        return
    await state.clear()
    log.info("Added task %s for chat %s", task.id, message.chat.id)
    text, markup = render_list(db, config, message.chat.id)
    await message.answer(text, reply_markup=markup)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


async def cmd_delete(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    await state.clear()
    text, markup = render_delete(db, config, message.chat.id)
    await message.answer(text, reply_markup=markup)


async def cmd_reminders(message: Message, state: FSMContext, db: Database) -> None:
    """Toggle the periodic reminders."""
    await state.clear()
    enabled = not db.get_state(message.chat.id).reminders_enabled
    db.set_reminders_enabled(message.chat.id, enabled)
    await message.answer("🔔 Reminders are ON." if enabled else "🔕 Reminders are OFF.")


# ------------------------------------------------------------------- callbacks
async def cb_toggle(
    callback: CallbackQuery, callback_data: TaskCB, db: Database, config: Config
) -> None:
    """Flip a task and re-render the message it was tapped on."""
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer()
        return
    now, today = prepare_today(db, config, message.chat.id)
    task = db.toggle_task(message.chat.id, callback_data.task_id, today, now)
    if task is None:
        await callback.answer("That task no longer exists - refreshed.")
    else:
        await callback.answer("✅ Done!" if task.is_done else "↩️ Marked as not done")

    if callback_data.view == VIEW_REMINDER:
        text, markup = render_reminder(db, config, message.chat.id)
    else:
        text, markup = render_list(db, config, message.chat.id)
    await safe_edit(message, text, markup)


async def cb_delete(
    callback: CallbackQuery, callback_data: TaskCB, db: Database, config: Config
) -> None:
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer()
        return
    _, today = prepare_today(db, config, message.chat.id)
    removed = db.delete_task(message.chat.id, callback_data.task_id, today)
    await callback.answer("🗑 Deleted" if removed else "Already gone")
    text, markup = render_delete(db, config, message.chat.id)
    await safe_edit(message, text, markup)


async def cb_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddTask.waiting_for_title)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("What should I add? Send the task text (or /cancel).")


async def cb_del_mode(callback: CallbackQuery, db: Database, config: Config) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        text, markup = render_delete(db, config, callback.message.chat.id)
        await safe_edit(callback.message, text, markup)


async def cb_show_list(callback: CallbackQuery, db: Database, config: Config) -> None:
    """Handles both ``refresh`` and ``list`` (back from delete mode / reminder)."""
    await callback.answer()
    if isinstance(callback.message, Message):
        text, markup = render_list(db, config, callback.message.chat.id)
        await safe_edit(callback.message, text, markup)


# ---------------------------------------------------------------------- router
def build_router(config: Config) -> Router:
    """Create the router, locked to the admin chat.

    Anything from another chat is silently ignored: this is a personal bot.
    """
    router = Router(name="routine")
    router.message.filter(F.chat.id == config.admin_chat_id)
    router.callback_query.filter(F.message.chat.id == config.admin_chat_id)

    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_tasks, Command("tasks", "list"))
    router.message.register(cmd_add, Command("add"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_delete, Command("delete"))
    router.message.register(cmd_reminders, Command("reminders"))
    router.message.register(add_title, AddTask.waiting_for_title, F.text, ~F.text.startswith("/"))

    router.callback_query.register(cb_toggle, TaskCB.filter(F.action == "toggle"))
    router.callback_query.register(cb_delete, TaskCB.filter(F.action == "delete"))
    router.callback_query.register(cb_add, TaskCB.filter(F.action == "add"))
    router.callback_query.register(cb_del_mode, TaskCB.filter(F.action == "del_mode"))
    router.callback_query.register(
        cb_show_list, TaskCB.filter(F.action.in_({"refresh", "list"}))
    )
    return router
