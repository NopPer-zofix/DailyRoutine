"""Entry point: wires config, DB, scheduler and Telegram polling together."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import ClientError

from config import Config, ConfigError, load_config
from database import Database
from handlers import build_router
from menu import setup_menu
from reminders import ReminderService

log = logging.getLogger("routine")

# Errors that just mean "the network is down" - keep retrying instead of exiting.
TRANSIENT_ERRORS = (TelegramNetworkError, TelegramServerError, ClientError, OSError, asyncio.TimeoutError)
MAX_BACKOFF = 60.0


def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """Set ``stop`` on SIGINT/SIGTERM (systemd sends SIGTERM on ``stop``)."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake immediately if shutdown is requested."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def poll_forever(dp: Dispatcher, bot: Bot, stop: asyncio.Event) -> None:
    """Run long-polling, reconnecting with exponential backoff on network loss.

    aiogram already retries failed ``getUpdates`` calls internally; this outer
    loop additionally covers failures at start-up (e.g. the Pi boots before
    Wi-Fi is up) and any unexpected exit of the polling coroutine.
    Non-network errors (bad token, programming bugs) propagate so systemd can
    restart the service and the cause lands in the journal.
    """
    delay = 1.0
    while not stop.is_set():
        try:
            await dp.start_polling(
                bot,
                handle_signals=False,  # we own signal handling
                allowed_updates=dp.resolve_used_update_types(),
            )
            if stop.is_set():
                return
            log.warning("Polling stopped unexpectedly; restarting")
        except TRANSIENT_ERRORS as exc:
            log.warning("Network problem (%s); reconnecting in %.0fs", exc, delay)
        await _sleep_or_stop(stop, delay)
        delay = min(delay * 2, MAX_BACKOFF)


async def run(config: Config) -> None:
    """Start everything and block until SIGINT/SIGTERM, then shut down cleanly."""
    db = Database(config.db_path)
    db.connect()
    # The admin is always approved (also repairs a row that was somehow revoked),
    # so reminders work even before the first /start.
    db.set_status(config.admin_chat_id, "approved")

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage(), db=db, config=config)
    dp.include_router(build_router(config))

    service = ReminderService(bot, db, config)
    dp["reminder_service"] = service  # injected into /remind
    scheduler = service.build_scheduler()

    stop = asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop)

    scheduler.start()
    await setup_menu(bot, config.admin_chat_id)
    await service.rollover_all()  # catch up if the bot was down at midnight
    log.info("Bot started (timezone: %s)", config.tz)

    polling = asyncio.create_task(poll_forever(dp, bot, stop), name="polling")
    stopper = asyncio.create_task(stop.wait(), name="stop-wait")
    try:
        done, _ = await asyncio.wait({polling, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if polling in done:
            polling.result()  # re-raise a fatal polling error
    finally:
        log.info("Shutting down...")
        stop.set()
        stopper.cancel()
        scheduler.shutdown(wait=False)
        try:
            await dp.stop_polling()
        except RuntimeError:
            pass  # polling was not running
        if not polling.done():  # a finished task's error was already raised above
            try:
                await asyncio.wait_for(polling, timeout=20)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                polling.cancel()
            except Exception:
                log.exception("Polling task ended with an error")
        await bot.session.close()
        db.close()
        log.info("Bye.")


def main(env_config: Optional[Config] = None) -> int:
    try:
        config = env_config or load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:  # Windows fallback
        pass
    except Exception as exc:
        # Non-zero exit lets systemd's Restart= policy kick in; details go to the journal.
        log.critical("Fatal error: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
