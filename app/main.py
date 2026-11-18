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
from app.core.keep_alive import keep_alive_all_student_sessions
from app.core.logging import setup_logging
from app.core.moodle_session import restore_saved_cookie
from app.lms.client import AutoMoodleClient
from app.notifications.service import SyncService

logger = logging.getLogger(__name__)


async def _poll_loop(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    sync_service: SyncService,
    bot: Bot | None,
    interval_seconds: int,
    stop_event: asyncio.Event,
) -> None:
    logger.info("Background poller and session keep-alive started (interval=%ss)", interval_seconds)
    while not stop_event.is_set():
        # 1. Main owner sync
        try:
            await sync_service.sync_once(trigger="poll")
        except Exception as exc:
            logger.warning("Owner Moodle sync failed during poll: %s", exc)

        # 2. Perpetual keep-alive for all registered students
        try:
            stats = await keep_alive_all_student_sessions(settings, session_factory, bot)
            if stats["total"] > 0:
                logger.info(
                    "Student session keep-alive finished: total=%d, active=%d, rotated=%d, expired=%d, errors=%d",
                    stats["total"],
                    stats["active"],
                    stats["rotated"],
                    stats["expired"],
                    stats["errors"],
                )
        except Exception as exc:
            logger.warning("Student session keep-alive loop failed: %s", exc)

        if bot is not None:
            try:
                from app.notifications.personal import sync_personal_accounts

                stats = await sync_personal_accounts(settings, session_factory, bot)
                if stats["failed"] or stats["accounts"]:
                    logger.info("Personal account sync: %s", stats)
            except Exception:
                logger.exception("Personal account sync loop failed")

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

    api_task = None
    uvicorn_server = None
    try:
        if once:
            report = await sync_service.sync_once(trigger="oneshot")
            await keep_alive_all_student_sessions(settings, session_factory, bot)
            from app.notifications.personal import sync_personal_accounts

            personal = await sync_personal_accounts(settings, session_factory, bot)
            return 0 if report.ok and personal["failed"] == 0 else 1

        # Start Connect API server on port 8000 for browser extension / auto-sync
        try:
            from fastapi import FastAPI
            from fastapi.middleware.cors import CORSMiddleware
            import uvicorn
            from app.api_connect import create_connect_router

            api_app = FastAPI(title="AITU Moodle Connect API")
            api_app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
            api_app.include_router(create_connect_router(settings, session_factory, bot))

            uvicorn_config = uvicorn.Config(
                app=api_app,
                host="0.0.0.0",
                port=8000,
                log_level="warning",
                access_log=False,
            )
            uvicorn_server = uvicorn.Server(uvicorn_config)
            api_task = asyncio.create_task(uvicorn_server.serve(), name="connect-api")
            logger.info("AITU Moodle Connect API started on port 8000")
        except Exception as e:
            logger.warning("Could not start Connect API server: %s", e)

        dp = Dispatcher()
        dp.include_router(create_bot_router(settings, session_factory, sync_service))
        await bot.set_my_commands(BOT_COMMANDS)

        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
            logger.warning(
                "Telegram webhook is active (%s). Skipping long polling to preserve webhook.",
                webhook_info.url,
            )
            stop_event = asyncio.Event()
            await _poll_loop(
                settings, session_factory, sync_service, bot, settings.poll_interval_seconds, stop_event
            )
            return 0

        await bot.delete_webhook(drop_pending_updates=False)

        stop_event = asyncio.Event()
        poller_task = asyncio.create_task(
            _poll_loop(
                settings, session_factory, sync_service, bot, settings.poll_interval_seconds, stop_event
            ),
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
        if uvicorn_server is not None:
            uvicorn_server.should_exit = True
        if api_task is not None:
            api_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await api_task
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
