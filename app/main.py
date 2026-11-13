from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import BOT_COMMANDS, create_bot_router
from app.bot.sender import TelegramNotifier
from app.core.config import Settings
from app.core.database import create_engine_and_session_factory, init_db
from app.core.logging import setup_logging
from app.core.moodle_session import restore_saved_cookie
from app.lms.client import AutoMoodleClient
from app.notifications.service import SyncService

logger = logging.getLogger(__name__)


async def _poll_loop(
    sync_service: SyncService,
    interval_seconds: int,
    stop_event: asyncio.Event,
) -> None:
    logger.info("Background poller started (interval=%ss)", interval_seconds)
    while not stop_event.is_set():
        await sync_service.sync_once(trigger="poll")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def async_main(once: bool = False) -> int:
    settings = Settings()  # type: ignore[call-arg]
    setup_logging(settings.log_level)

    engine, session_factory = create_engine_and_session_factory(settings.database_url)
    await init_db(engine)

    lms_client = AutoMoodleClient(settings)
    await restore_saved_cookie(settings, session_factory, lms_client)

    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    notifier = TelegramNotifier(bot, settings.telegram_chat_id)
    sync_service = SyncService(settings, lms_client, session_factory, notifier)

    try:
        if once:
            report = await sync_service.sync_once(trigger="oneshot")
            return 0 if report.ok else 1

        dp = Dispatcher()
        dp.include_router(create_bot_router(settings, session_factory, sync_service))
        await bot.set_my_commands(BOT_COMMANDS)
        await bot.delete_webhook(drop_pending_updates=False)

        stop_event = asyncio.Event()
        poller_task = asyncio.create_task(
            _poll_loop(sync_service, settings.poll_interval_seconds, stop_event),
            name="moodle-poller",
        )
        try:
            await dp.start_polling(bot, handle_signals=True)
        finally:
            stop_event.set()
            poller_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller_task
    finally:
        await lms_client.close()
        await bot.session.close()
        await engine.dispose()

    return 0


def run() -> None:
    parser = argparse.ArgumentParser(
        description="Moodle LMS parser and Telegram notification bot"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single Moodle sync + notification pass and exit",
    )
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(async_main(once=args.once))
    except KeyboardInterrupt:
        exit_code = 0
    sys.exit(exit_code)


if __name__ == "__main__":
    run()
