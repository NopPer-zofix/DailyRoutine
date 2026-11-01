"""Message rendering (pure functions, HTML parse mode)."""
from __future__ import annotations

from datetime import date
from html import escape
from typing import Sequence

from database import Task, UserState

BAR_WIDTH = 10

WELCOME_TEXT = (
    "👋 <b>Daily Routine bot</b>\n"
    "Here is today's list - tap a task to tick it off.\n"
    "All commands are in the <b>Menu</b> button next to the message box "
    "(or send /help)."
)

HELP_TEXT = (
    "<b>Daily Routine bot</b>\n\n"
    "/tasks - today's checklist (tap a task to toggle it)\n"
    "/add &lt;text&gt; - add a task (or just /add and I'll ask)\n"
    "/delete - choose tasks to delete\n"
    "/frequency - choose how often I remind you\n"
    "/reminders - turn the periodic reminders on/off\n"
    "/remind - send the reminder right now (preview)\n"
    "/timezone &lt;Area/City&gt; - set your timezone, e.g. Europe/Berlin\n"
    "/help - this message\n\n"
    "Every day a fresh list is generated. "
    "In between I remind you about what is still open."
)

ADMIN_HELP_TEXT = (
    "\n\n<b>Admin</b>\n"
    "/users - manage users"
)


def help_text(is_admin: bool) -> str:
    return HELP_TEXT + (ADMIN_HELP_TEXT if is_admin else "")


def frequency_text(enabled: bool, interval: int) -> str:
    """Message above the /frequency buttons."""
    if not enabled:
        status = "🔕 Reminders are <b>off</b>."
    else:
        every = "hour" if interval == 1 else f"{interval} hours"
        status = f"🔔 I remind you every <b>{every}</b>."
    return f"{status}\n\nHow often should I remind you about open tasks?"


STATUS_ICONS = {"approved": "✅", "pending": "⏳", "denied": "❌", "revoked": "🚫"}


def user_label(user: UserState) -> str:
    return escape(user.display_name) if user.display_name else f"user {user.chat_id}"


def users_text(users: Sequence[UserState], admin_chat_id: int) -> str:
    """Admin overview of everybody the bot knows."""
    lines = []
    for u in users:
        if u.chat_id == admin_chat_id:
            lines.append("👑 <b>You</b> (admin)")
            continue
        tz = f" · {escape(u.timezone)}" if u.timezone else ""
        lines.append(
            f"{STATUS_ICONS.get(u.status, '•')} {user_label(u)} · <code>{u.chat_id}</code>"
            f" · {u.status}{tz}"
        )
    return "👥 <b>Users</b>\n\n" + "\n".join(lines)


def access_request_text(display_name: str, chat_id: int) -> str:
    return (
        "🔔 <b>Access request</b>\n"
        f"{escape(display_name)} · <code>{chat_id}</code>\n\n"
        "They can create their own private task list and reminders. "
        "You cannot see their tasks."
    )


def progress_bar(done: int, total: int, width: int = BAR_WIDTH) -> str:
    """Render e.g. ``███░░░░░░░`` for 3/10."""
    if total <= 0:
        return "░" * width
    filled = round(width * done / total)
    return "█" * filled + "░" * (width - filled)


def progress_line(done: int, total: int) -> str:
    """Render e.g. ``3/5 tasks completed (60%)`` with a bar."""
    pct = round(100 * done / total) if total else 0
    return f"{progress_bar(done, total)}  <b>{done}/{total}</b> tasks completed ({pct}%)"


def dashboard_text(tasks: Sequence[Task], day: date) -> str:
    """Header + progress for the main checklist message."""
    done = sum(t.is_done for t in tasks)
    header = f"📋 <b>{day.strftime('%A, %d %B %Y')}</b>\n\n{progress_line(done, len(tasks))}"
    if not tasks:
        return header + "\n\nNo tasks yet. Tap ➕ Add or send /add &lt;text&gt;."
    if done == len(tasks):
        return header + "\n\n🎉 Everything is done!"
    return header + "\n\nTap a task to mark it done / not done."


def delete_text(tasks: Sequence[Task]) -> str:
    if not tasks:
        return "Nothing to delete - the list is empty."
    return "🗑 <b>Delete mode</b>\nTap a task to remove it."


def reminder_text(tasks: Sequence[Task]) -> str:
    """Reminder listing the unfinished tasks."""
    done = sum(t.is_done for t in tasks)
    open_tasks = [t for t in tasks if not t.is_done]
    lines = "\n".join(f"• {escape(t.title)}" for t in open_tasks)
    return (
        f"⏰ <b>Reminder</b>\n{progress_line(done, len(tasks))}\n\n"
        f"Still to do:\n{lines}"
    )


def congrats_text(total: int) -> str:
    return f"🎉 <b>All {total} tasks done today.</b> Great job - enjoy the rest of your day!"
