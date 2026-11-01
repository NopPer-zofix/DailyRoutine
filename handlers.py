"""Telegram command and callback handlers.

Handlers receive their dependencies (``db``, ``config``) through aiogram's
dependency injection (``Dispatcher(db=..., config=...)``), which also makes
them trivial to call directly with mocks in tests.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, tzinfo
from html import escape
from typing import Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from access import AccessMiddleware
from config import Config, parse_timezone, resolve_timezone
from database import Database
from keyboards import (
    FREQ_OFF,
    VIEW_REMINDER,
    AccessCB,
    FreqCB,
    TaskCB,
    cancel_keyboard,
    delete_keyboard,
    frequency_keyboard,
    manage_user_keyboard,
    reminder_keyboard,
    tasks_keyboard,
)
from reminders import ReminderAction, ReminderService, send_with_retry
from texts import (
    STATUS_ICONS,
    USERS_PROMPT,
    WELCOME_TEXT,
    congrats_text,
    dashboard_text,
    delete_text,
    frequency_text,
    help_text,
    manage_user_text,
    reminder_text,
    user_label,
    users_text,
)

log = logging.getLogger(__name__)


class AddTask(StatesGroup):
    """FSM: the next plain-text message becomes a new task."""

    waiting_for_title = State()


# --------------------------------------------------------------------- helpers
def local_now(tz: tzinfo) -> datetime:
    """Timezone-aware current time in ``tz``."""
    return datetime.now(tz)


def tz_for(db: Database, config: Config, chat_id: int) -> tzinfo:
    """The user's own timezone, or the server default if they never set one."""
    return resolve_timezone(db.get_state(chat_id).timezone, config.tz)


def prepare_today(db: Database, config: Config, chat_id: int) -> Tuple[datetime, date]:
    """Return ``(now, today)`` after making sure today's list exists.

    Rolling over lazily here means the bot is correct on the first interaction
    of a new day even if the midnight job has not run (e.g. the Pi was off).
    """
    now = local_now(tz_for(db, config, chat_id))
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
    await message.answer(WELCOME_TEXT)
    text, markup = render_list(db, config, message.chat.id)
    await message.answer(text, reply_markup=markup)


async def cmd_help(message: Message, state: FSMContext, config: Config) -> None:
    await state.clear()
    await message.answer(help_text(message.chat.id == config.admin_chat_id))


async def cmd_timezone(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    db: Database,
    config: Config,
) -> None:
    """``/timezone Europe/Berlin`` - midnight and reminders follow this zone."""
    await state.clear()
    if not command.args:
        tz = tz_for(db, config, message.chat.id)
        await message.answer(
            f"🕒 Your timezone is <b>{escape(str(tz))}</b> "
            f"(local time {local_now(tz):%H:%M}).\n"
            "Change it with /timezone &lt;Area/City&gt;, e.g. <code>/timezone Europe/Berlin</code>."
        )
        return
    try:
        tz = parse_timezone(command.args)
    except ValueError:
        await message.answer(
            "⚠️ I don't know that timezone. Use an IANA name like "
            "<code>Europe/Berlin</code> or <code>America/New_York</code>."
        )
        return
    db.set_timezone(message.chat.id, tz.key)
    await message.answer(
        f"✅ Timezone set to <b>{escape(tz.key)}</b> (local time {local_now(tz):%H:%M})."
    )


# ---------------------------------------------------------------------- admin
class ManageUser(StatesGroup):
    """FSM: the admin's next plain-text message is the number of a user in /users."""

    waiting_for_number = State()


async def cmd_users(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    """Numbered list of everybody; then ask which one to manage."""
    await state.clear()
    await state.set_state(ManageUser.waiting_for_number)
    text = users_text(db.list_users(), config.admin_chat_id) + USERS_PROMPT
    await message.answer(text, reply_markup=cancel_keyboard())


async def users_pick(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    """The admin sent a number: show that user's status and the actions that fit it."""
    users = db.list_users()  # fresh, so a request that arrived meanwhile is counted
    raw = (message.text or "").strip().lstrip("#")
    if not (raw.isascii() and raw.isdigit() and 1 <= int(raw) <= len(users)):
        await message.answer(
            f"⚠️ Send a number from 1 to {len(users)} (or /cancel).",
            reply_markup=cancel_keyboard(),
        )
        return
    number = int(raw)
    user = users[number - 1]
    if user.chat_id == config.admin_chat_id:
        await message.answer(
            "That's you - pick another number (or /cancel).", reply_markup=cancel_keyboard()
        )
        return
    await state.clear()  # the buttons below carry everything they need
    await message.answer(manage_user_text(user, number), reply_markup=manage_user_keyboard(user))


_ACCESS_ACTIONS = {"approve": "approved", "deny": "denied", "revoke": "revoked"}
_ACCESS_NOTICES = {
    "approve": "✅ You've been given access. Send /start to begin.",
    "deny": "Sorry, your access request was declined.",
    "revoke": "Your access to this bot has been removed.",
}


async def cb_access(
    callback: CallbackQuery,
    callback_data: AccessCB,
    state: FSMContext,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    """Admin pressed Approve / Deny / Revoke / Unban, or Cancel."""
    if callback_data.action == "cancel":
        await state.clear()
        await callback.answer("Cancelled.")
        if isinstance(callback.message, Message):
            await safe_edit(callback.message, "Cancelled.", None)
        return

    target = callback_data.chat_id
    new_status = _ACCESS_ACTIONS.get(callback_data.action)
    previous = db.get_status(target)
    if new_status is None or previous is None or target == config.admin_chat_id:
        await callback.answer("Nothing to do.")
        return

    if previous != new_status:
        db.set_status(target, new_status)
        log.info("Access for %s: %s -> %s", target, previous, new_status)
        await send_with_retry(bot, target, _ACCESS_NOTICES[callback_data.action])
    await callback.answer(f"{new_status.capitalize()}.")

    if isinstance(callback.message, Message):
        user = db.get_state(target)
        await safe_edit(
            callback.message,
            f"{STATUS_ICONS.get(new_status, '•')} {user_label(user)} · "
            f"<code>{target}</code> is now <b>{new_status}</b>.",
            None,  # drop the buttons: the decision is made
        )


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


MAX_CUSTOM_INTERVAL = 12


def render_frequency(
    db: Database, config: Config, chat_id: int
) -> Tuple[str, InlineKeyboardMarkup]:
    """The /frequency panel for the chat's current setting."""
    state = db.get_state(chat_id)
    interval = state.reminder_interval_hours or config.reminder_interval_hours
    current = interval if state.reminders_enabled else FREQ_OFF
    return frequency_text(state.reminders_enabled, interval), frequency_keyboard(current)


async def cmd_frequency(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    db: Database,
    config: Config,
) -> None:
    """``/frequency`` shows buttons; ``/frequency 3`` sets "every 3 hours" directly."""
    await state.clear()
    if command.args:
        try:
            hours = int(command.args.strip())
            if not 1 <= hours <= MAX_CUSTOM_INTERVAL:
                raise ValueError
        except ValueError:
            await message.answer(
                f"⚠️ Give a whole number of hours from 1 to {MAX_CUSTOM_INTERVAL}, "
                "e.g. <code>/frequency 3</code> - or use /frequency for buttons."
            )
            return
        db.set_reminder_interval(message.chat.id, hours)
        db.set_reminders_enabled(message.chat.id, True)
    text, markup = render_frequency(db, config, message.chat.id)
    await message.answer(text, reply_markup=markup)


async def cmd_remind(
    message: Message, state: FSMContext, reminder_service: ReminderService
) -> None:
    """Send the scheduled reminder right now (preview; changes no state)."""
    await state.clear()
    action = await reminder_service.process_chat(message.chat.id, force=True)
    if action is ReminderAction.NONE:
        await message.answer("Nothing to remind you about - no open tasks. Add one with /add.")
    elif action is ReminderAction.CONGRATS:
        await message.answer("<i>(All tasks are done, so this is the all-done message.)</i>")


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
        done, total = db.progress(message.chat.id, today)
        if total and done == total:
            # The user just saw the all-done state; don't send the scheduled 🎉 on top of it.
            db.mark_congratulated(message.chat.id, today)

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


async def cb_frequency(
    callback: CallbackQuery, callback_data: FreqCB, db: Database, config: Config
) -> None:
    """A frequency button (or "Off") was tapped."""
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer()
        return
    chat_id = message.chat.id
    if callback_data.hours == FREQ_OFF:
        db.set_reminders_enabled(chat_id, False)
        await callback.answer("🔕 Reminders off")
    elif 1 <= callback_data.hours <= 24:
        db.set_reminder_interval(chat_id, callback_data.hours)
        db.set_reminders_enabled(chat_id, True)
        await callback.answer(f"🔔 Every {callback_data.hours}h")
    else:
        await callback.answer("Unknown option.")
        return
    text, markup = render_frequency(db, config, chat_id)
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
    """Create the router.

    :class:`AccessMiddleware` lets through only the admin and approved users;
    strangers trigger an access request instead (see ``access.py``).
    """
    router = Router(name="routine")
    access = AccessMiddleware(config.admin_chat_id)
    router.message.outer_middleware(access)
    router.callback_query.outer_middleware(access)

    is_admin_msg = F.chat.id == config.admin_chat_id
    is_admin_cb = F.message.chat.id == config.admin_chat_id

    router.message.register(cmd_users, Command("users"), is_admin_msg)
    router.message.register(
        users_pick, ManageUser.waiting_for_number, is_admin_msg, F.text, ~F.text.startswith("/")
    )
    router.callback_query.register(cb_access, AccessCB.filter(), is_admin_cb)

    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_timezone, Command("timezone"))
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_tasks, Command("tasks", "list"))
    router.message.register(cmd_add, Command("add"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_delete, Command("delete"))
    router.message.register(cmd_frequency, Command("frequency"))
    router.message.register(cmd_reminders, Command("reminders"))
    router.message.register(cmd_remind, Command("remind"))
    router.message.register(add_title, AddTask.waiting_for_title, F.text, ~F.text.startswith("/"))

    router.callback_query.register(cb_frequency, FreqCB.filter())
    router.callback_query.register(cb_toggle, TaskCB.filter(F.action == "toggle"))
    router.callback_query.register(cb_delete, TaskCB.filter(F.action == "delete"))
    router.callback_query.register(cb_add, TaskCB.filter(F.action == "add"))
    router.callback_query.register(cb_del_mode, TaskCB.filter(F.action == "del_mode"))
    router.callback_query.register(
        cb_show_list, TaskCB.filter(F.action.in_({"refresh", "list"}))
    )
    return router
