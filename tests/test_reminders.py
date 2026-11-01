"""The 4-hour rule, the scheduler wiring and resilient sending."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

import reminders
from database import Database
from reminders import (
    ReminderAction,
    ReminderService,
    decide_reminder,
    reminder_hours,
    send_with_retry,
)
from tests.conftest import CHAT, NOW, TODAY


# ------------------------------------------------------------- pure decisions
@pytest.mark.parametrize(
    "total, done, kwargs, expected",
    [
        (5, 2, {}, ReminderAction.REMIND),  # open tasks -> remind
        (5, 0, {}, ReminderAction.REMIND),
        (5, 5, {}, ReminderAction.CONGRATS),  # all done -> congratulate once
        (5, 5, {"congratulated_today": True}, ReminderAction.NONE),
        (5, 5, {"send_congrats": False}, ReminderAction.NONE),
        (0, 0, {}, ReminderAction.NONE),  # nothing planned -> stay quiet
        (5, 2, {"reminders_enabled": False}, ReminderAction.NONE),
        (5, 5, {"reminders_enabled": False}, ReminderAction.NONE),
    ],
)
def test_decide_reminder(total, done, kwargs, expected):
    assert decide_reminder(total, done, **kwargs) is expected


def test_reminder_hours_every_four_hours():
    assert reminder_hours(8, 22, 4) == [8, 12, 16, 20]
    assert reminder_hours(0, 23, 6) == [0, 6, 12, 18]


def test_reminder_hours_rejects_bad_interval():
    with pytest.raises(ValueError):
        reminder_hours(8, 22, 0)


# --------------------------------------------------------------- service flow
@pytest.fixture
def service(bot, db, config) -> ReminderService:
    db.ensure_chat(CHAT)  # approved, so tick() serves it
    return ReminderService(bot, db, config, now_fn=lambda tz: NOW.astimezone(tz))


async def test_open_tasks_trigger_reminder_with_buttons(service, bot, db):
    db.add_task(CHAT, "Gym", TODAY)
    done = db.add_task(CHAT, "Read", TODAY)
    db.toggle_task(CHAT, done.id, TODAY, NOW)

    action = await service.process_chat(CHAT)

    assert action is ReminderAction.REMIND
    bot.send_message.assert_awaited_once()
    args, kwargs = bot.send_message.call_args
    assert args[0] == CHAT
    assert "Gym" in args[1] and "Read" not in args[1]  # only unfinished listed
    assert "1/2" in args[1]
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any("Gym" in b.text for b in buttons)
    assert db.get_state(CHAT).last_reminder_at == NOW


async def test_all_done_skips_reminder_and_congratulates_once(service, bot, db):
    task = db.add_task(CHAT, "Gym", TODAY)
    db.toggle_task(CHAT, task.id, TODAY, NOW)

    assert await service.process_chat(CHAT) is ReminderAction.CONGRATS
    assert await service.process_chat(CHAT) is ReminderAction.NONE  # not twice

    assert bot.send_message.await_count == 1
    assert "🎉" in bot.send_message.call_args.args[1]


async def test_no_message_when_reminders_disabled(service, bot, db):
    db.add_task(CHAT, "Gym", TODAY)
    db.set_reminders_enabled(CHAT, False)

    assert await service.process_chat(CHAT) is ReminderAction.NONE
    bot.send_message.assert_not_awaited()


async def test_forced_reminder_ignores_off_switch_and_records_nothing(service, bot, db):
    db.add_task(CHAT, "Gym", TODAY)
    db.set_reminders_enabled(CHAT, False)

    assert await service.process_chat(CHAT, force=True) is ReminderAction.REMIND

    bot.send_message.assert_awaited_once()
    assert db.get_state(CHAT).last_reminder_at is None  # preview leaves no trace


async def test_forced_preview_when_all_done_does_not_use_up_daily_congrats(service, bot, db):
    task = db.add_task(CHAT, "Gym", TODAY)
    db.toggle_task(CHAT, task.id, TODAY, NOW)

    assert await service.process_chat(CHAT, force=True) is ReminderAction.CONGRATS
    assert await service.process_chat(CHAT, force=True) is ReminderAction.CONGRATS  # repeatable
    assert db.get_state(CHAT).last_congrats_date is None
    assert await service.process_chat(CHAT) is ReminderAction.CONGRATS  # real one still fires


async def test_failed_send_is_not_recorded(service, bot, db, monkeypatch):
    monkeypatch.setattr(reminders.asyncio, "sleep", AsyncMock())
    db.add_task(CHAT, "Gym", TODAY)
    bot.send_message.side_effect = TelegramNetworkError(MagicMock(), "offline")

    await service.process_chat(CHAT)

    assert db.get_state(CHAT).last_reminder_at is None  # will be retried next slot


async def test_tick_survives_one_chat_failing(service, bot, db):
    db.ensure_chat(1)
    db.ensure_chat(2)
    db.add_task(2, "Gym", TODAY)
    # Chat 1's processing blows up; chat 2 must still be handled.
    original = service.process_chat

    async def flaky(chat_id: int):
        if chat_id == 1:
            raise RuntimeError("boom")
        return await original(chat_id)

    service.process_chat = flaky  # type: ignore[method-assign]
    await service.tick()

    assert bot.send_message.call_args.args[0] == 2


# ------------------------------------------------- per-user timezones in tick()
def _service_at(bot, db, config, moment):
    return ReminderService(bot, db, config, now_fn=lambda tz: moment.astimezone(tz))


async def test_tick_uses_each_users_local_hour(bot, db, config):
    # 12:00 Berlin = 20:00 Tokyo (reminder hour) = 06:00 New York (not one).
    for chat, tz in ((10, "Asia/Tokyo"), (11, "America/New_York"), (12, "Europe/Berlin")):
        db.ensure_chat(chat)
        db.set_timezone(chat, tz)
        db.add_task(chat, "Gym", TODAY)

    await _service_at(bot, db, config, NOW).tick()

    assert sorted(c.args[0] for c in bot.send_message.call_args_list) == [10, 12]


async def test_tick_only_acts_at_top_of_local_hour(bot, db, config):
    db.ensure_chat(CHAT)
    db.add_task(CHAT, "Gym", TODAY)

    await _service_at(bot, db, config, NOW.replace(minute=15)).tick()
    await _service_at(bot, db, config, NOW.replace(minute=45)).tick()
    bot.send_message.assert_not_awaited()

    await _service_at(bot, db, config, NOW.replace(minute=14)).tick()
    bot.send_message.assert_awaited_once()


async def test_tick_handles_half_hour_timezones(bot, db, config):
    db.ensure_chat(CHAT)
    db.set_timezone(CHAT, "Asia/Kolkata")  # UTC+5:30
    db.add_task(CHAT, "Gym", TODAY)

    # 12:00 Berlin = 16:30 Kolkata: not the top of its hour.
    await _service_at(bot, db, config, NOW).tick()
    bot.send_message.assert_not_awaited()
    # 15:30 Berlin = 20:00 Kolkata: a reminder hour, on a UTC :30 tick.
    await _service_at(bot, db, config, NOW.replace(hour=15, minute=30)).tick()
    bot.send_message.assert_awaited_once()


async def test_tick_rolls_over_at_users_local_midnight(bot, db, config):
    # 12:00 Berlin on the 10th = 00:00 on the 11th in Auckland (UTC+13 in March).
    db.ensure_chat(CHAT)
    db.set_timezone(CHAT, "Pacific/Auckland")
    db.add_task(CHAT, "Gym", TODAY)

    await _service_at(bot, db, config, NOW).tick()

    assert [t.title for t in db.list_tasks(CHAT, TODAY.replace(day=11))] == ["Gym"]
    bot.send_message.assert_not_awaited()  # hour 0 is not a reminder hour


async def test_unapproved_chats_get_no_reminders(bot, db, config):
    db.add_task(77, "Gym", TODAY)  # row exists, but only as 'pending'
    db.set_status(78, "revoked")

    await _service_at(bot, db, config, NOW).tick()

    bot.send_message.assert_not_awaited()


async def test_first_check_of_new_day_rolls_tasks_over(service, bot, db):
    db.add_task(CHAT, "Gym", TODAY - timedelta(days=1))

    await service.process_chat(CHAT)

    assert [t.title for t in db.list_tasks(CHAT, TODAY)] == ["Gym"]
    bot.send_message.assert_awaited_once()  # fresh, unfinished list -> reminder


async def test_startup_rollover_all_chats(service, db):
    db.ensure_chat(7)
    db.add_task(CHAT, "Gym", TODAY - timedelta(days=1))
    db.add_task(7, "Run", TODAY - timedelta(days=1))

    await service.rollover_all()

    assert db.progress(CHAT, TODAY) == (0, 1)
    assert db.progress(7, TODAY) == (0, 1)


# ------------------------------------------------------------------ scheduler
def test_scheduler_has_one_quarter_hour_tick(service):
    scheduler = service.build_scheduler()
    jobs = {j.id: j for j in scheduler.get_jobs()}  # works before start()

    assert set(jobs) == {"tick"}
    assert "*/15" in str(jobs["tick"].trigger)
    # The 4-hour rule: reminder hours come from config, evaluated per user's local clock.
    assert service.hours == [8, 12, 16, 20]


# --------------------------------------------------------------- send retries
async def test_send_retries_network_errors_with_backoff(bot, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(reminders.asyncio, "sleep", sleep)
    bot.send_message.side_effect = [
        TelegramNetworkError(MagicMock(), "down"),
        TelegramNetworkError(MagicMock(), "down"),
        "ok",
    ]

    assert await send_with_retry(bot, CHAT, "hi") == "ok"
    assert [c.args[0] for c in sleep.await_args_list] == [2.0, 4.0]


async def test_send_gives_up_after_max_attempts(bot, monkeypatch):
    monkeypatch.setattr(reminders.asyncio, "sleep", AsyncMock())
    bot.send_message.side_effect = TelegramNetworkError(MagicMock(), "down")

    assert await send_with_retry(bot, CHAT, "hi", attempts=3) is None
    assert bot.send_message.await_count == 3


async def test_send_honours_flood_control(bot, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(reminders.asyncio, "sleep", sleep)
    bot.send_message.side_effect = [TelegramRetryAfter(MagicMock(), "slow", 5), "ok"]

    assert await send_with_retry(bot, CHAT, "hi") == "ok"
    sleep.assert_awaited_once_with(6)


async def test_send_does_not_retry_when_bot_is_blocked(bot):
    bot.send_message.side_effect = TelegramForbiddenError(MagicMock(), "blocked")

    assert await send_with_retry(bot, CHAT, "hi") is None
    assert bot.send_message.await_count == 1
