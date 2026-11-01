"""Scheduled jobs: midnight rollover and the periodic reminder check."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, tzinfo
from enum import Enum
from typing import Callable, List, Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import Config, resolve_timezone
from database import Database
from keyboards import reminder_keyboard, tasks_keyboard
from texts import congrats_text, dashboard_text, reminder_text

log = logging.getLogger(__name__)

TICK_MINUTES = 15  # scheduler resolution; see ReminderService.tick


class ReminderAction(Enum):
    """What a reminder check should do for one chat."""

    NONE = "none"
    REMIND = "remind"
    CONGRATS = "congrats"


def decide_reminder(
    total: int,
    done: int,
    *,
    reminders_enabled: bool = True,
    congratulated_today: bool = False,
    send_congrats: bool = True,
) -> ReminderAction:
    """The reminder rule as a pure function.

    * reminders switched off, or no tasks at all -> nothing
    * unfinished tasks remain -> remind
    * everything done -> one congratulation per day (if enabled), else nothing
    """
    if not reminders_enabled or total <= 0:
        return ReminderAction.NONE
    if done < total:
        return ReminderAction.REMIND
    if send_congrats and not congratulated_today:
        return ReminderAction.NONE
    return ReminderAction.NONE


def reminder_hours(start: int, end: int, interval: int) -> List[int]:
    """Local hours at which a check fires, e.g. (8, 22, 4) -> [8, 12, 16, 20]."""
    if interval < 1:
        raise ValueError("interval must be >= 1")
    return list(range(start, end + 1, interval))


async def send_with_retry(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    attempts: int = 5,
    base_delay: float = 2.0,
    disable_notification: bool = False,
) -> Optional[Message]:
    """Send a message, riding out flaky internet and Telegram flood limits.

    Network/server errors are retried with exponential backoff (2s, 4s, 8s...,
    capped at 60s). A flood-wait is honoured. If the user blocked the bot or
    all attempts fail, the error is logged and ``None`` is returned - a missed
    reminder must never crash the scheduler.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await bot.send_message(
                chat_id,
                text,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
            )
        except TelegramRetryAfter as exc:
            log.warning("Flood control, sleeping %ss", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
        except (TelegramNetworkError, TelegramServerError) as exc:
            if attempt == attempts:
                log.error("Giving up sending to %s after %d attempts: %s", chat_id, attempts, exc)
                return None
            delay = min(base_delay * 2 ** (attempt - 1), 60.0)
            log.warning("Send failed (%s); retry %d/%d in %.0fs", exc, attempt, attempts, delay)
            await asyncio.sleep(delay)
        except TelegramForbiddenError as exc:
            log.error("Cannot message chat %s (blocked or kicked?): %s", chat_id, exc)
            return None
    log.error("Giving up sending to %s: flood control kept triggering", chat_id)
    return None


class ReminderService:
    """Owns the scheduled jobs.

    Args:
        bot: Aiogram bot used to send messages.
        db: Open database.
        config: Application config (timezone, hours, rollover mode...).
        now_fn: ``tz -> aware datetime``; injectable clock for tests.
    """

    def __init__(
        self,
        bot: Bot,
        db: Database,
        config: Config,
        now_fn: Optional[Callable[[tzinfo], datetime]] = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.config = config
        self._now_fn = now_fn or (lambda tz: datetime.now(tz))
        self.hours = reminder_hours(
            config.reminder_start_hour, config.reminder_end_hour, config.reminder_interval_hours
        )

    def now(self, tz: tzinfo) -> datetime:
        return self._now_fn(tz)

    def hours_for(self, interval: Optional[int]) -> List[int]:
        """Reminder hours for a user's own interval (None = server default)."""
        if interval is None:
            return self.hours
        return reminder_hours(
            self.config.reminder_start_hour, self.config.reminder_end_hour, interval
        )

    def tz_for(self, chat_id: int) -> tzinfo:
        """The chat's own timezone, falling back to the server default."""
        return resolve_timezone(self.db.get_state(chat_id).timezone, self.config.tz)

    # ------------------------------------------------------------------ jobs
    async def rollover_all(self) -> None:
        """Generate today's tasks for every approved chat (startup catch-up)."""
        for chat_id in self.db.list_chats():
            try:
                today = self.now(self.tz_for(chat_id)).date()
                created = self.db.roll_over(chat_id, today, self.config.rollover_mode)
                log.info("Rollover for chat %s -> %s: %d task(s)", chat_id, today, created)
            except Exception:
                log.exception("Rollover failed for chat %s", chat_id)

    async def tick(self) -> None:
        """Runs every 15 minutes and acts on each user's *own* local clock.

        At the top of a local hour (minute < 15, i.e. the first tick of the
        hour - every real timezone offset is a multiple of 15 minutes):

        * local hour 0 -> roll the user's tasks over to the new day and, if
          reminders are on, post the fresh list (silently - it is midnight);
        * local hour in the user's reminder hours (their own interval, or the
          server default) -> apply the reminder rule.
        """
        for chat_id in self.db.list_chats():
            try:
                state = self.db.get_state(chat_id)
                local = self.now(resolve_timezone(state.timezone, self.config.tz))
                if local.minute >= TICK_MINUTES:
                    continue
                announced = False
                if local.hour == 0:
                    announced = await self._new_day(chat_id, local, state.reminders_enabled)
                if not announced and local.hour in self.hours_for(state.reminder_interval_hours):
                    await self.process_chat(chat_id)
            except Exception:
                log.exception("Scheduled check failed for chat %s", chat_id)

    async def _new_day(self, chat_id: int, local: datetime, announce: bool) -> bool:
        """Midnight: generate today's list and (optionally) show it.

        Returns True if the fresh list was sent (it then stands in for a
        reminder at the same hour).
        """
        today = local.date()
        first = self.db.get_state(chat_id).last_rollover_date != today
        self.db.roll_over(chat_id, today, self.config.rollover_mode)
        if not (announce and first):  # once per day, even if the bot restarts
            return False
        tasks = self.db.list_tasks(chat_id, today)
        if not tasks:
            return False
        sent = await send_with_retry(
            self.bot,
            chat_id,
            dashboard_text(tasks, today),
            tasks_keyboard(tasks),
            disable_notification=True,
        )
        return sent is not None

    async def process_chat(self, chat_id: int, *, force: bool = False) -> ReminderAction:
        """Evaluate the reminder rule for one chat and act on it.

        Args:
            chat_id: Chat to process.
            force: Preview mode (``/remind``). Skips the on/off switch and the
                once-per-day congratulation limit, and does not record anything
                in the database, so it never affects the real schedule.
        """
        now = self.now(self.tz_for(chat_id))
        today = now.date()
        # Lazy rollover: guarantees a correct list even if the midnight job was missed.
        self.db.roll_over(chat_id, today, self.config.rollover_mode)

        state = self.db.get_state(chat_id)
        tasks = self.db.list_tasks(chat_id, today)
        done = sum(t.is_done for t in tasks)
        action = decide_reminder(
            len(tasks),
            done,
            reminders_enabled=True if force else state.reminders_enabled,
            congratulated_today=False if force else state.last_congrats_date == today,
            send_congrats=True if force else self.config.send_congrats,
        )

        if action is ReminderAction.REMIND:
            sent = await send_with_retry(
                self.bot, chat_id, reminder_text(tasks), reminder_keyboard(tasks)
            )
            if sent and not force:
                self.db.mark_reminded(chat_id, now)
        elif action is ReminderAction.CONGRATS:
            sent = await send_with_retry(self.bot, chat_id, congrats_text(len(tasks)))
            if sent and not force:
                self.db.mark_congratulated(chat_id, today)
        return action

    # ------------------------------------------------------------- scheduler
    def build_scheduler(self) -> AsyncIOScheduler:
        """Create (but do not start) the scheduler with a single 15-minute tick.

        One global tick (instead of fixed cron hours) lets every user have their
        own timezone: :meth:`tick` decides per user whether their local clock
        has just reached a reminder hour or midnight. ``coalesce`` collapses a
        backlog into one run; the short grace time matches the 15-minute window
        in which a tick is still considered "the top of the hour".
        """
        scheduler = AsyncIOScheduler(
            timezone=timezone.utc,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 5 * 60},
        )
        scheduler.add_job(
            self.tick,
            CronTrigger(minute="*/15", timezone=timezone.utc),
            id="tick",
            replace_existing=True,
        )
        log.info("Reminder checks at local hours %s (per-user timezone)", self.hours)
        return scheduler
