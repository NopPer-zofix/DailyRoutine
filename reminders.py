"""Scheduled jobs: midnight rollover and the 4-hour reminder check."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
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

from config import Config
from database import Database
from keyboards import reminder_keyboard
from texts import congrats_text, reminder_text

log = logging.getLogger(__name__)


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
    """The 4-hour rule as a pure function.

    * reminders switched off, or no tasks at all -> nothing
    * unfinished tasks remain -> remind
    * everything done -> one congratulation per day (if enabled), else nothing
    """
    if not reminders_enabled or total <= 0:
        return ReminderAction.NONE
    if done < total:
        return ReminderAction.REMIND
    if send_congrats and not congratulated_today:
        return ReminderAction.CONGRATS
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
) -> Optional[Message]:
    """Send a message, riding out flaky internet and Telegram flood limits.

    Network/server errors are retried with exponential backoff (2s, 4s, 8s...,
    capped at 60s). A flood-wait is honoured. If the user blocked the bot or
    all attempts fail, the error is logged and ``None`` is returned - a missed
    reminder must never crash the scheduler.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await bot.send_message(chat_id, text, reply_markup=reply_markup)
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
        now_fn: Clock returning a timezone-aware "now"; injectable for tests.
    """

    def __init__(
        self,
        bot: Bot,
        db: Database,
        config: Config,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.config = config
        self._now_fn = now_fn or (lambda: datetime.now(config.tz))

    def now(self) -> datetime:
        return self._now_fn()

    # ------------------------------------------------------------------ jobs
    async def rollover_all(self) -> None:
        """Generate today's tasks for every chat (midnight job + startup catch-up)."""
        today = self.now().date()
        for chat_id in self.db.list_chats():
            try:
                created = self.db.roll_over(chat_id, today, self.config.rollover_mode)
                log.info("Rollover for chat %s -> %s: %d task(s)", chat_id, today, created)
            except Exception:
                log.exception("Rollover failed for chat %s", chat_id)

    async def check_reminders(self) -> None:
        """Run the reminder rule for every chat."""
        for chat_id in self.db.list_chats():
            try:
                await self.process_chat(chat_id)
            except Exception:
                log.exception("Reminder check failed for chat %s", chat_id)

    async def process_chat(self, chat_id: int) -> ReminderAction:
        """Evaluate the 4-hour rule for one chat and act on it."""
        now = self.now()
        today = now.date()
        # Lazy rollover: guarantees a correct list even if the midnight job was missed.
        self.db.roll_over(chat_id, today, self.config.rollover_mode)

        state = self.db.get_state(chat_id)
        tasks = self.db.list_tasks(chat_id, today)
        done = sum(t.is_done for t in tasks)
        action = decide_reminder(
            len(tasks),
            done,
            reminders_enabled=state.reminders_enabled,
            congratulated_today=state.last_congrats_date == today,
            send_congrats=self.config.send_congrats,
        )

        if action is ReminderAction.REMIND:
            sent = await send_with_retry(
                self.bot, chat_id, reminder_text(tasks), reminder_keyboard(tasks)
            )
            if sent:
                self.db.mark_reminded(chat_id, now)
        elif action is ReminderAction.CONGRATS:
            sent = await send_with_retry(self.bot, chat_id, congrats_text(len(tasks)))
            if sent:
                self.db.mark_congratulated(chat_id, today)
        return action

    # ------------------------------------------------------------- scheduler
    def build_scheduler(self) -> AsyncIOScheduler:
        """Create (but do not start) the scheduler with both cron jobs.

        Jobs use the configured *local* timezone, so "20:00" means 20:00 on the
        wall clock of the Pi, not UTC. ``misfire_grace_time`` lets a job that
        was delayed (e.g. by a brief outage) still run; ``coalesce`` collapses
        a backlog into a single run.
        """
        cfg = self.config
        hours = reminder_hours(
            cfg.reminder_start_hour, cfg.reminder_end_hour, cfg.reminder_interval_hours
        )
        scheduler = AsyncIOScheduler(
            timezone=cfg.tz,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 15 * 60},
        )
        scheduler.add_job(
            self.rollover_all,
            CronTrigger(hour=0, minute=0, timezone=cfg.tz),
            id="midnight_rollover",
            replace_existing=True,
        )
        scheduler.add_job(
            self.check_reminders,
            CronTrigger(hour=",".join(map(str, hours)), minute=0, timezone=cfg.tz),
            id="reminder_check",
            replace_existing=True,
        )
        log.info("Reminder checks scheduled at local hours %s (%s)", hours, cfg.tz)
        return scheduler
