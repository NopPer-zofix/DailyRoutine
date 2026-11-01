"""Message rendering (pure functions, HTML parse mode)."""
from __future__ import annotations

from datetime import date
from html import escape
from typing import Sequence

from database import Task

BAR_WIDTH = 10

HELP_TEXT = (
    "<b>Daily Routine bot</b>\n\n"
    "/tasks - today's checklist (tap a task to toggle it)\n"
    "/add &lt;text&gt; - add a task (or just /add and I'll ask)\n"
    "/delete - choose tasks to delete\n"
    "/reminders - turn the 4-hour reminders on/off\n"
    "/help - this message\n\n"
    "Every night at midnight (local time) a fresh list is generated. "
    "Every few hours I remind you about what is still open."
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
