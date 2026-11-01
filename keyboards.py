"""Inline keyboards and their callback-data schema."""
from __future__ import annotations

from typing import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import Task

MAX_BUTTON_TITLE = 40

# Which message a callback came from, so we know how to re-render it.
VIEW_LIST = "l"
VIEW_REMINDER = "r"


class TaskCB(CallbackData, prefix="task"):
    """Callback payload (<= 64 bytes as required by Telegram).

    ``action`` is one of: ``toggle``, ``delete``, ``add``, ``refresh``,
    ``del_mode``, ``list``.
    """

    action: str
    task_id: int = 0
    view: str = VIEW_LIST


def _short(title: str) -> str:
    return title if len(title) <= MAX_BUTTON_TITLE else title[: MAX_BUTTON_TITLE - 1] + "…"


def tasks_keyboard(tasks: Sequence[Task]) -> InlineKeyboardMarkup:
    """Main checklist: one toggle button per task + Add / Delete / Refresh."""
    kb = InlineKeyboardBuilder()
    for t in tasks:
        mark = "✅" if t.is_done else "⬜"
        kb.row(
            InlineKeyboardButton(
                text=f"{mark} {_short(t.title)}",
                callback_data=TaskCB(action="toggle", task_id=t.id).pack(),
            )
        )
    controls = [
        InlineKeyboardButton(text="➕ Add", callback_data=TaskCB(action="add").pack()),
        InlineKeyboardButton(text="🔄 Refresh", callback_data=TaskCB(action="refresh").pack()),
    ]
    if tasks:
        controls.insert(
            1,
            InlineKeyboardButton(text="🗑 Delete", callback_data=TaskCB(action="del_mode").pack()),
        )
    kb.row(*controls)
    return kb.as_markup()


def delete_keyboard(tasks: Sequence[Task]) -> InlineKeyboardMarkup:
    """Delete mode: tapping a task removes it."""
    kb = InlineKeyboardBuilder()
    for t in tasks:
        kb.row(
            InlineKeyboardButton(
                text=f"🗑 {_short(t.title)}",
                callback_data=TaskCB(action="delete", task_id=t.id).pack(),
            )
        )
    kb.row(InlineKeyboardButton(text="⬅ Back", callback_data=TaskCB(action="list").pack()))
    return kb.as_markup()


def reminder_keyboard(tasks: Sequence[Task]) -> InlineKeyboardMarkup:
    """Reminder: quick "done" buttons for the unfinished tasks only."""
    kb = InlineKeyboardBuilder()
    for t in tasks:
        if t.is_done:
            continue
        kb.row(
            InlineKeyboardButton(
                text=f"✅ {_short(t.title)}",
                callback_data=TaskCB(action="toggle", task_id=t.id, view=VIEW_REMINDER).pack(),
            )
        )
    kb.row(InlineKeyboardButton(text="📋 Full list", callback_data=TaskCB(action="list").pack()))
    return kb.as_markup()
