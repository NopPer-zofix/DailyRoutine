"""Handlers driven with mocked Telegram objects and an in-memory DB."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import handlers
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User
from handlers import AddTask
from keyboards import VIEW_REMINDER, TaskCB
from tests.conftest import CHAT, NOW, TODAY


def _command(args):
    return CommandObject(prefix="/", command="add", args=args)


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


# ------------------------------------------------------------------- commands
async def test_add_with_args_creates_task_and_shows_list(make_message, fsm, db, config):
    msg = make_message()

    await handlers.cmd_add(msg, _command("Drink water"), fsm, db, config)

    assert [t.title for t in db.list_tasks(CHAT, TODAY)] == ["Drink water"]
    text = msg.answer.call_args.args[0]
    assert "0/1" in text
    fsm.clear.assert_awaited()


async def test_bare_add_waits_for_text(make_message, fsm, db, config):
    msg = make_message()

    await handlers.cmd_add(msg, _command(None), fsm, db, config)

    fsm.set_state.assert_awaited_once_with(AddTask.waiting_for_title)
    assert db.list_tasks(CHAT, TODAY) == []


async def test_text_after_prompt_becomes_task(make_message, fsm, db, config):
    await handlers.add_title(make_message("Stretch"), fsm, db, config)

    assert [t.title for t in db.list_tasks(CHAT, TODAY)] == ["Stretch"]
    fsm.clear.assert_awaited()


async def test_invalid_title_reports_error_and_keeps_waiting(make_message, fsm, db, config):
    msg = make_message()

    await handlers.add_title(make_message("x" * 500), fsm, db, config)
    await handlers.cmd_add(msg, _command("   "), fsm, db, config)

    assert db.list_tasks(CHAT, TODAY) == []
    fsm.clear.assert_not_awaited()  # still in the "waiting" state


async def test_tasks_command_shows_progress_dashboard(make_message, fsm, db, config):
    for title in ("a", "b", "c", "d", "e"):
        db.add_task(CHAT, title, TODAY)
    for task in db.list_tasks(CHAT, TODAY)[:3]:
        db.toggle_task(CHAT, task.id, TODAY, NOW)
    msg = make_message()

    await handlers.cmd_tasks(msg, fsm, db, config)

    text = msg.answer.call_args.args[0]
    assert "3/5" in text and "60%" in text
    labels = [b.text for b in _buttons(msg.answer.call_args.kwargs["reply_markup"])]
    assert labels[:5] == ["✅ a", "✅ b", "✅ c", "⬜ d", "⬜ e"]


async def test_first_use_of_new_day_rolls_over(make_message, fsm, db, config):
    db.add_task(CHAT, "Gym", TODAY - timedelta(days=1))
    msg = make_message()

    await handlers.cmd_tasks(msg, fsm, db, config)

    assert [t.title for t in db.list_tasks(CHAT, TODAY)] == ["Gym"]


async def test_reminders_command_toggles(make_message, fsm, db):
    msg = make_message()

    await handlers.cmd_reminders(msg, fsm, db)
    assert db.get_state(CHAT).reminders_enabled is False
    await handlers.cmd_reminders(msg, fsm, db)
    assert db.get_state(CHAT).reminders_enabled is True


# ------------------------------------------------------------------ callbacks
async def test_toggle_callback_flips_task_and_redraws(make_callback, db, config):
    task = db.add_task(CHAT, "Gym", TODAY)
    cb = make_callback()

    await handlers.cb_toggle(cb, TaskCB(action="toggle", task_id=task.id), db, config)

    assert db.get_task(CHAT, task.id, TODAY).is_done is True
    text, = cb.message.edit_text.call_args.args
    assert "1/1" in text
    assert cb.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "✅ Gym"

    await handlers.cb_toggle(cb, TaskCB(action="toggle", task_id=task.id), db, config)
    assert db.get_task(CHAT, task.id, TODAY).is_done is False


async def test_toggle_from_reminder_keeps_reminder_layout(make_callback, db, config):
    gym = db.add_task(CHAT, "Gym", TODAY)
    db.add_task(CHAT, "Read", TODAY)
    cb = make_callback()

    await handlers.cb_toggle(
        cb, TaskCB(action="toggle", task_id=gym.id, view=VIEW_REMINDER), db, config
    )

    text = cb.message.edit_text.call_args.args[0]
    assert "Reminder" in text and "Read" in text and "Gym" not in text


async def test_finishing_last_task_from_reminder_congratulates(make_callback, db, config):
    gym = db.add_task(CHAT, "Gym", TODAY)
    cb = make_callback()

    await handlers.cb_toggle(
        cb, TaskCB(action="toggle", task_id=gym.id, view=VIEW_REMINDER), db, config
    )

    assert "🎉" in cb.message.edit_text.call_args.args[0]
    assert cb.message.edit_text.call_args.kwargs["reply_markup"] is None


async def test_toggle_of_deleted_task_refreshes_instead_of_crashing(make_callback, db, config):
    cb = make_callback()

    await handlers.cb_toggle(cb, TaskCB(action="toggle", task_id=999), db, config)

    assert "no longer exists" in cb.answer.call_args.args[0]
    cb.message.edit_text.assert_awaited_once()


async def test_message_not_modified_error_is_swallowed(make_callback, db, config):
    cb = make_callback()
    cb.message.edit_text.side_effect = TelegramBadRequest(
        MagicMock(), "Bad Request: message is not modified"
    )

    await handlers.cb_show_list(cb, db, config)  # must not raise


async def test_delete_callback_removes_task(make_callback, db, config):
    task = db.add_task(CHAT, "Gym", TODAY)
    cb = make_callback()

    await handlers.cb_delete(cb, TaskCB(action="delete", task_id=task.id), db, config)

    assert db.list_tasks(CHAT, TODAY) == []
    cb.answer.assert_awaited_with("🗑 Deleted")


async def test_add_button_enters_waiting_state(make_callback, fsm):
    cb = make_callback()

    await handlers.cb_add(cb, fsm)

    fsm.set_state.assert_awaited_once_with(AddTask.waiting_for_title)
    cb.message.answer.assert_awaited_once()


# ------------------------------------------------------------------- security
def _update(chat_id: int, text: str) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="T"),
            text=text,
        ),
    )


async def _feed(config, db, chat_id: int):
    """Push a real Update through a real Dispatcher; the HTTP layer is mocked."""
    dp = Dispatcher(storage=MemoryStorage(), db=db, config=config)
    dp.include_router(handlers.build_router(config))
    bot = Bot("123:abc")
    bot.session.make_request = AsyncMock(return_value=MagicMock())  # no network
    result = await dp.feed_update(bot, _update(chat_id, "/tasks"))
    return result, bot


async def test_admin_chat_is_served(db, config):
    result, bot = await _feed(config, db, CHAT)

    assert result is not UNHANDLED
    bot.session.make_request.assert_awaited()  # the dashboard was sent


async def test_other_chats_are_ignored(db, config):
    result, bot = await _feed(config, db, CHAT + 1)

    assert result is UNHANDLED
    bot.session.make_request.assert_not_awaited()
    assert db.list_chats() == []  # a stranger can't even register a chat
