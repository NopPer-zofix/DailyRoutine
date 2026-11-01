"""Handlers driven with mocked Telegram objects and an in-memory DB."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import handlers
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject
from handlers import AddTask
from keyboards import FREQ_OFF, VIEW_REMINDER, FreqCB, TaskCB
from reminders import ReminderAction
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


async def test_timezone_command_validates_and_stores(make_message, fsm, db, config):
    msg = make_message()

    await handlers.cmd_timezone(msg, CommandObject(prefix="/", command="timezone", args="Mars/Base"), fsm, db, config)
    assert db.get_state(CHAT).timezone is None
    assert "don't know" in msg.answer.call_args.args[0]

    await handlers.cmd_timezone(msg, CommandObject(prefix="/", command="timezone", args=" Asia/Tokyo "), fsm, db, config)
    assert db.get_state(CHAT).timezone == "Asia/Tokyo"

    await handlers.cmd_timezone(msg, CommandObject(prefix="/", command="timezone", args=None), fsm, db, config)
    assert "Asia/Tokyo" in msg.answer.call_args.args[0]


async def test_user_timezone_decides_which_day_it_is(make_message, fsm, db, config):
    # NOW is 2025-03-10 12:00 Berlin = 2025-03-11 01:00 in Auckland.
    db.set_timezone(CHAT, "Pacific/Auckland")
    msg = make_message()

    await handlers.cmd_add(msg, _command("Run"), fsm, db, config)

    assert [t.title for t in db.list_tasks(CHAT, TODAY.replace(day=11))] == ["Run"]
    assert db.list_tasks(CHAT, TODAY) == []


async def test_reminders_command_toggles(make_message, fsm, db):
    msg = make_message()

    await handlers.cmd_reminders(msg, fsm, db)
    assert db.get_state(CHAT).reminders_enabled is False
    await handlers.cmd_reminders(msg, fsm, db)
    assert db.get_state(CHAT).reminders_enabled is True


async def test_remind_command_forces_reminder(make_message, fsm):
    service = MagicMock()
    service.process_chat = AsyncMock(return_value=ReminderAction.REMIND)
    msg = make_message()

    await handlers.cmd_remind(msg, fsm, service)

    service.process_chat.assert_awaited_once_with(CHAT, force=True)
    msg.answer.assert_not_awaited()  # the reminder itself is sent by the service


async def test_remind_command_explains_when_there_are_no_tasks(make_message, fsm):
    service = MagicMock()
    service.process_chat = AsyncMock(return_value=ReminderAction.NONE)
    msg = make_message()

    await handlers.cmd_remind(msg, fsm, service)

    assert "nothing to remind" in msg.answer.call_args.args[0]


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


# ------------------------------------------------------------------ frequency
def _freq(args):
    return CommandObject(prefix="/", command="frequency", args=args)


def _ticked(markup):
    return [b.text for b in _buttons(markup) if b.text.startswith("✔")]


async def test_start_greets_briefly_instead_of_listing_every_command(make_message, fsm, db, config):
    msg = make_message()

    await handlers.cmd_start(msg, fsm, db, config)

    welcome = msg.answer.call_args_list[0].args[0]
    assert "Menu" in welcome
    assert "/timezone" not in welcome and "/delete" not in welcome
    assert len(msg.answer.call_args_list) == 2  # welcome + today's list


async def test_help_still_lists_commands_on_request(make_message, fsm, config):
    msg = make_message()

    await handlers.cmd_help(msg, fsm, config)

    text = msg.answer.call_args.args[0]
    assert "/frequency" in text and "/users" in text  # CHAT is the admin


async def test_frequency_shows_buttons_with_default_ticked(make_message, fsm, db, config):
    msg = make_message()

    await handlers.cmd_frequency(msg, _freq(None), fsm, db, config)

    assert _ticked(msg.answer.call_args.kwargs["reply_markup"]) == ["✔ 4h"]  # config default
    assert "every <b>4 hours</b>" in msg.answer.call_args.args[0]


async def test_frequency_argument_sets_interval_and_enables(make_message, fsm, db, config):
    db.set_reminders_enabled(CHAT, False)
    msg = make_message()

    await handlers.cmd_frequency(msg, _freq("2"), fsm, db, config)

    state = db.get_state(CHAT)
    assert state.reminder_interval_hours == 2 and state.reminders_enabled is True


async def test_frequency_rejects_nonsense(make_message, fsm, db, config):
    msg = make_message()

    for bad in ("0", "13", "often", "1.5"):
        await handlers.cmd_frequency(msg, _freq(bad), fsm, db, config)
        assert "whole number" in msg.answer.call_args.args[0]

    assert db.get_state(CHAT).reminder_interval_hours is None


async def test_frequency_buttons_set_interval_and_off(make_callback, db, config):
    cb = make_callback()

    await handlers.cb_frequency(cb, FreqCB(hours=6), db, config)
    assert db.get_state(CHAT).reminder_interval_hours == 6
    assert db.get_state(CHAT).reminders_enabled is True
    assert _ticked(cb.message.edit_text.call_args.kwargs["reply_markup"]) == ["✔ 6h"]

    await handlers.cb_frequency(cb, FreqCB(hours=FREQ_OFF), db, config)
    state = db.get_state(CHAT)
    assert state.reminders_enabled is False and state.reminder_interval_hours == 6  # remembered
    assert _ticked(cb.message.edit_text.call_args.kwargs["reply_markup"]) == ["✔ 🔕 Off"]


async def test_frequency_callback_ignores_out_of_range(make_callback, db, config):
    cb = make_callback()

    await handlers.cb_frequency(cb, FreqCB(hours=99), db, config)

    assert db.get_state(CHAT).reminder_interval_hours is None
    cb.message.edit_text.assert_not_awaited()


# ----------------------------------------------------------------- command menu
async def test_menu_publishes_commands_and_adds_users_for_admin():
    from aiogram.types import BotCommandScopeChat

    from menu import ADMIN_COMMANDS, USER_COMMANDS, setup_menu

    bot = MagicMock()
    bot.set_my_commands = AsyncMock()

    await setup_menu(bot, admin_chat_id=CHAT)

    default_call, admin_call = bot.set_my_commands.await_args_list
    assert default_call.args[0] == USER_COMMANDS
    assert admin_call.args[0] == ADMIN_COMMANDS
    assert admin_call.kwargs["scope"] == BotCommandScopeChat(chat_id=CHAT)
    assert "users" not in [c.command for c in USER_COMMANDS]
    assert "users" in [c.command for c in ADMIN_COMMANDS]


async def test_menu_failure_does_not_crash_startup():
    from aiogram.exceptions import TelegramNetworkError

    from menu import setup_menu

    bot = MagicMock()
    bot.set_my_commands = AsyncMock(side_effect=TelegramNetworkError(MagicMock(), "offline"))

    await setup_menu(bot, admin_chat_id=CHAT)  # must not raise
