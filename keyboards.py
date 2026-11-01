"""Inline keyboards and their callback-data schema."""
from __future__ import annotations

from typing import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import Task, UserState

MAX_BUTTON_TITLE = 40

# Reminder frequencies offered as buttons (hours between reminders).
FREQUENCY_CHOICES = (1, 2, 3, 4, 6, 8, 12)
FREQ_OFF = 0

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


class AccessCB(CallbackData, prefix="acc"):
    """Admin buttons for managing users. ``action``: approve | deny | revoke | cancel."""

    action: str
    chat_id: int


class FreqCB(CallbackData, prefix="freq"):
    """Reminder-frequency buttons. ``hours`` is the interval, or 0 for "off"."""

    hours: int


def _short(title: str) -> str:
    return title if len(title) <= MAX_BUTTON_TITLE else title[: MAX_BUTTON_TITLE - 1] + "…"


def request_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Approve / Deny buttons under an access request."""
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="✅ Approve", callback_data=AccessCB(action="approve", chat_id=chat_id).pack()
        ),
        InlineKeyboardButton(
            text="❌ Deny", callback_data=AccessCB(action="deny", chat_id=chat_id).pack()
        ),
    )
    return kb.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    """A lone Cancel button (under the numbered /users list)."""
    kb = InlineKeyboardBuilder()
    kb.row(_cancel_button())
    return kb.as_markup()


def _cancel_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="✖ Cancel", callback_data=AccessCB(action="cancel", chat_id=0).pack()
    )


def manage_user_keyboard(user: UserState) -> InlineKeyboardMarkup:
    """Actions for the one user the admin picked, depending on their status."""
    kb = InlineKeyboardBuilder()

    def act(text: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text, callback_data=AccessCB(action=action, chat_id=user.chat_id).pack()
        )

    if user.status == "approved":
        kb.row(act("🚫 Revoke", "revoke"))
    elif user.status == "pending":
        kb.row(act("✅ Approve", "approve"), act("❌ Deny", "deny"))
    else:  # denied / revoked
        kb.row(act("↩️ Unban", "approve"))
    kb.row(_cancel_button())
    return kb.as_markup()


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


def frequency_keyboard(current: int) -> InlineKeyboardMarkup:
    """Pick how often reminders arrive; ``current`` (hours, 0 = off) is ticked."""
    kb = InlineKeyboardBuilder()

    def button(text: str, hours: int) -> InlineKeyboardButton:
        mark = "✔ " if hours == current else ""
        return InlineKeyboardButton(
            text=f"{mark}{text}", callback_data=FreqCB(hours=hours).pack()
        )

    kb.row(*(button(f"{h}h", h) for h in FREQUENCY_CHOICES[:4]))
    kb.row(*(button(f"{h}h", h) for h in FREQUENCY_CHOICES[4:]))
    kb.row(button("🔕 Off", FREQ_OFF))
    return kb.as_markup()
