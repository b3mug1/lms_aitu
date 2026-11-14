from __future__ import annotations

import logging
import secrets
from html import escape
from typing import Any

from aiogram import Bot
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.connect_tokens import resolve_connect_token
from app.core.keep_alive import reset_user_expired_notification
from app.core.user_sessions import (
    save_user_cookie,
    update_user_cookie_silently,
)
from app.lms.base import LMSAuthError, LMSConnectionError, LMSError
from app.lms.moodle_web import MoodleWebClient

logger = logging.getLogger("moodle_bot.connect_api")
router = APIRouter(prefix="/api", tags=["connect"])


class ConnectRequest(BaseModel):
    token: str
    cookie: str


class SyncRequest(BaseModel):
    telegram_user_id: int
    sync_secret: str
    cookie: str


def clean_cookie(raw: str) -> str:
    val = raw.strip()
    if val.startswith("MoodleSession="):
        val = val.partition("=")[2].strip()
    return val


def create_connect_router(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot | None = None,
) -> APIRouter:
    r = APIRouter(prefix="/api", tags=["connect"])

    @r.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "aitu-moodle-notifier"}

    @r.post("/connect-session")
    async def connect_session(req: ConnectRequest) -> dict[str, Any]:
        signing_key = (
            settings.moodle_cookie_encryption_key.get_secret_value()
            if settings.moodle_cookie_encryption_key
            else None
        )
        telegram_user_id = resolve_connect_token(req.token, signing_key)
        if telegram_user_id is None:
            raise HTTPException(
                status_code=400,
                detail="Токен подключения недействителен или истёк. Получите новый в Telegram-боте командой /connect.",
            )

        cookie_val = clean_cookie(req.cookie)
        if not cookie_val:
            raise HTTPException(status_code=400, detail="MoodleSession cookie не может быть пустой.")

        # Validate cookie on Moodle
        web_client = MoodleWebClient(settings, cookie_value=cookie_val)
        try:
            snapshot = await web_client.fetch_snapshot()
        except LMSAuthError as exc:
            raise HTTPException(
                status_code=401,
                detail="Сессия не авторизована на Moodle. Убедитесь, что вы залогинены на lms.astanait.edu.kz.",
            ) from exc
        except (LMSConnectionError, LMSError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Не удалось связаться с LMS AITU: {exc}",
            ) from exc
        finally:
            await web_client.close()

        sync_secret = secrets.token_hex(24)
        fullname = snapshot.user_fullname or "Студент"

        # Save to DB
        await save_user_cookie(
            settings=settings,
            session_factory=session_factory,
            telegram_user_id=telegram_user_id,
            cookie=cookie_val,
            sync_secret=sync_secret,
            user_fullname=fullname,
        )
        reset_user_expired_notification(telegram_user_id)

        # Notify user in Telegram
        if bot is not None:
            courses_str = f"<b>Курсов найдено:</b> {len(snapshot.courses)}"
            msg = (
                f"🎉 <b>Moodle успешно подключён!</b>\n\n"
                f"👤 <b>Профиль:</b> {escape(fullname)}\n"
                f"📚 {courses_str}\n\n"
                f"✅ Теперь вам доступны команды /courses, /grades и /deadlines.\n"
                f"🔄 Расширение в браузере будет автоматически продлевать вашу сессию в фоновом режиме!"
            )
            try:
                await bot.send_message(chat_id=telegram_user_id, text=msg)
            except Exception as e:
                logger.warning("Could not send Telegram connect confirmation to %s: %s", telegram_user_id, e)

        return {
            "ok": True,
            "fullname": fullname,
            "courses_count": len(snapshot.courses),
            "sync_secret": sync_secret,
            "telegram_user_id": telegram_user_id,
        }

    @r.post("/sync-session")
    async def sync_session(req: SyncRequest) -> dict[str, Any]:
        cookie_val = clean_cookie(req.cookie)
        if not cookie_val:
            raise HTTPException(status_code=400, detail="Empty cookie")

        updated = await update_user_cookie_silently(
            settings=settings,
            session_factory=session_factory,
            telegram_user_id=req.telegram_user_id,
            sync_secret=req.sync_secret,
            cookie=cookie_val,
        )
        if not updated:
            raise HTTPException(status_code=403, detail="Invalid sync_secret or user not found")

        logger.info("Session silently refreshed via browser extension for user %s", req.telegram_user_id)
        return {"ok": True, "status": "session_refreshed"}

    return r
