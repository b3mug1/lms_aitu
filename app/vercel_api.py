from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from app.bot.handlers import BOT_COMMANDS, create_bot_router
from app.bot.sender import TelegramNotifier
from app.api_connect import create_connect_router
from app.core.config import Settings
from app.core.database import create_engine_and_session_factory, init_db
from app.core.moodle_session import restore_saved_cookie
from app.lms.client import AutoMoodleClient
from app.notifications.service import SyncService
from app.notifications.personal import sync_personal_accounts

logger = logging.getLogger("moodle_bot.webhook")


def _matches_secret(provided: str | None, configured: str | None) -> bool:
    return bool(configured) and secrets.compare_digest(provided or "", configured)


_db_initialized = False


@asynccontextmanager
async def _runtime(
    settings: Settings, *, restore_global_cookie: bool = True
) -> AsyncIterator[tuple[Bot, Dispatcher, SyncService]]:
    global _db_initialized
    if os.environ.get("VERCEL") and not settings.database_url.startswith("postgresql+asyncpg://"):
        raise RuntimeError("Vercel requires a persistent PostgreSQL DATABASE_URL using asyncpg")

    engine, session_factory = create_engine_and_session_factory(settings.database_url)
    lms_client: AutoMoodleClient | None = None
    bot: Bot | None = None
    try:
        if not _db_initialized:
            await init_db(engine)
            _db_initialized = True
        lms_client = AutoMoodleClient(settings)
        if restore_global_cookie:
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
        dispatcher = Dispatcher()
        dispatcher.include_router(create_bot_router(settings, session_factory, sync_service))
        yield bot, dispatcher, sync_service
    finally:
        if bot is not None:
            await bot.session.close()
        if lms_client is not None:
            await lms_client.close()
        await engine.dispose()


def create_app(config: Settings | None = None) -> FastAPI:
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    connect_settings = config or Settings()  # type: ignore[call-arg]
    connect_engine, connect_sessions = create_engine_and_session_factory(
        connect_settings.database_url
    )
    api.include_router(create_connect_router(connect_settings, connect_sessions))

    @api.on_event("shutdown")
    async def close_connect_engine() -> None:
        await connect_engine.dispose()

    @api.post("/api/telegram")
    async def telegram_webhook(request: Request) -> dict[str, bool]:
        settings = config or Settings()  # type: ignore[call-arg]
        webhook_secret = (
            settings.telegram_webhook_secret.get_secret_value()
            if settings.telegram_webhook_secret
            else None
        )
        if not webhook_secret:
            raise HTTPException(status_code=503, detail="Webhook is not configured")
        if not _matches_secret(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token"), webhook_secret
        ):
            raise HTTPException(status_code=403, detail="Forbidden")

        body = await request.body()
        if len(body) > 1_000_000:
            raise HTTPException(status_code=413, detail="Update is too large")
        try:
            update = Update.model_validate_json(body)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid Telegram update") from exc

        try:
            chat = update.message.chat if update.message else None
            if chat is None and update.callback_query and update.callback_query.message:
                chat = update.callback_query.message.chat
            personal_chat = bool(
                chat and chat.type == "private" and chat.id != settings.telegram_chat_id
            )
            async with _runtime(settings, restore_global_cookie=not personal_chat) as (bot, dispatcher, _):
                if update.message and (update.message.text or "").startswith("/start"):
                    try:
                        await bot.set_my_commands(BOT_COMMANDS)
                    except Exception as cmd_exc:
                        logger.warning("Failed to register bot commands: %s", type(cmd_exc).__name__)
                await dispatcher.feed_update(bot, update)
        except Exception:
            logger.exception("Failed to process Telegram update %s", update.update_id)
        return {"ok": True}

    @api.get("/api/cron")
    async def scheduled_sync(request: Request) -> dict[str, int | bool]:
        settings = config or Settings()  # type: ignore[call-arg]
        cron_secret = settings.cron_secret.get_secret_value() if settings.cron_secret else None
        if not cron_secret:
            raise HTTPException(status_code=503, detail="Cron is not configured")
        if not _matches_secret(request.headers.get("Authorization"), f"Bearer {cron_secret}"):
            raise HTTPException(status_code=403, detail="Forbidden")

        async with _runtime(settings) as (bot, __, sync_service):
            report = await sync_service.sync_once(trigger="scheduled")
            engine, session_factory = create_engine_and_session_factory(settings.database_url)
            try:
                personal = await sync_personal_accounts(settings, session_factory, bot)
            finally:
                await engine.dispose()
        return {
            "ok": report.ok and personal["failed"] == 0,
            "skipped": report.skipped,
            "notifications_sent": report.notifications_sent,
            "personal_accounts": personal["accounts"],
            "personal_notifications_sent": personal["sent"],
            "personal_failed": personal["failed"],
        }

    return api


app = create_app()
