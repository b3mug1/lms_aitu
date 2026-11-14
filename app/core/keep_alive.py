from __future__ import annotations

import asyncio
import logging
from bs4 import BeautifulSoup
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.core.config import Settings
from app.core.user_sessions import (
    UserSessionError,
    decrypt_cookie,
    save_user_cookie,
)
from app.lms.moodle_web import clean_html_text, is_login_page
from app.storage.models import MoodleUserAccountModel

logger = logging.getLogger(__name__)

# In-memory tracking of users already alerted about expired session to avoid notification loops
_notified_expired_users: set[int] = set()


def reset_user_expired_notification(telegram_user_id: int) -> None:
    """Clear expired notification flag when a user connects or updates their session."""
    _notified_expired_users.discard(telegram_user_id)


async def _touch_single_user(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    account: MoodleUserAccountModel,
    bot: Bot | None,
    semaphore: asyncio.Semaphore,
) -> str:
    """
    Touch a single user's Moodle session on lms.astanait.edu.kz.
    Returns status: 'active', 'rotated', 'expired', or 'error'.
    """
    async with semaphore:
        try:
            cookie = decrypt_cookie(settings, account.encrypted_cookie)
        except UserSessionError:
            logger.warning("Failed to decrypt stored cookie for user %s", account.telegram_user_id)
            return "error"

        my_url = f"{settings.moodle_base_url.rstrip('/')}/my/"
        try:
            resp = await client.get(
                my_url,
                cookies={"MoodleSession": cookie},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
        except httpx.RequestError as exc:
            logger.debug(
                "Network request failed during keep-alive for user %s: %s",
                account.telegram_user_id,
                exc,
            )
            return "error"

        final_url = str(resp.url)
        if resp.status_code in (401, 403) or is_login_page(
            resp.text, final_url, settings.moodle_base_url
        ):
            logger.info(
                "Session expired for student user %s (%s)",
                account.telegram_user_id,
                account.user_fullname,
            )
            if bot and account.telegram_user_id not in _notified_expired_users:
                _notified_expired_users.add(account.telegram_user_id)
                try:
                    await bot.send_message(
                        chat_id=account.telegram_user_id,
                        text=(
                            "⚠️ <b>Сессия в Moodle была завершена</b>\n\n"
                            "Возможно, вы нажали «Выход» на сайте Moodle или университет сбросил старые токены.\n"
                            "Чтобы снова получать обновления, отправьте команду /connect."
                        ),
                        parse_mode="HTML",
                    )
                except TelegramAPIError as err:
                    logger.debug(
                        "Could not notify user %s about expired session: %s",
                        account.telegram_user_id,
                        err,
                    )
            return "expired"

        # Session is valid and touched on Moodle
        _notified_expired_users.discard(account.telegram_user_id)

        # If Moodle sent a rotated session cookie in Set-Cookie, persist it
        new_cookie = resp.cookies.get("MoodleSession")
        was_rotated = False
        if new_cookie and new_cookie != cookie:
            await save_user_cookie(
                settings=settings,
                session_factory=session_factory,
                telegram_user_id=account.telegram_user_id,
                cookie=new_cookie,
                sync_secret=account.sync_secret,
                user_fullname=account.user_fullname,
            )
            logger.info(
                "Moodle session auto-rotated by server for user %s, updated in DB",
                account.telegram_user_id,
            )
            was_rotated = True

        # Extract and save student full name if not yet recorded
        if not account.user_fullname:
            soup = BeautifulSoup(resp.text, "html.parser")
            user_el = soup.select_one(".usermenu .usertext, .userbutton .usertext, .usertext")
            if user_el:
                fullname = clean_html_text(str(user_el), 100)
                if fullname and fullname != "Пользователь Moodle":
                    async with session_factory() as session:
                        acc = await session.get(MoodleUserAccountModel, account.telegram_user_id)
                        if acc:
                            acc.user_fullname = fullname
                            await session.commit()

        return "rotated" if was_rotated else "active"


async def keep_alive_all_student_sessions(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot | None = None,
) -> dict[str, int]:
    """
    Touch Moodle for all registered students to keep their sessions warm and active 24/7.
    Prevents Moodle server from garbage-collecting idle sessions.
    """
    if settings.moodle_cookie_encryption_key is None:
        return {"total": 0, "active": 0, "rotated": 0, "expired": 0, "errors": 0}

    async with session_factory() as session:
        result = await session.execute(select(MoodleUserAccountModel))
        accounts = list(result.scalars().all())

    if not accounts:
        return {"total": 0, "active": 0, "rotated": 0, "expired": 0, "errors": 0}

    semaphore = asyncio.Semaphore(5)
    async with httpx.AsyncClient(
        timeout=float(settings.request_timeout_seconds),
        verify=settings.ssl_verify,
        follow_redirects=True,
    ) as client:
        tasks = [
            _touch_single_user(
                settings=settings,
                session_factory=session_factory,
                client=client,
                account=acc,
                bot=bot,
                semaphore=semaphore,
            )
            for acc in accounts
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    stats = {"total": len(accounts), "active": 0, "rotated": 0, "expired": 0, "errors": 0}
    for res in results:
        if isinstance(res, Exception):
            stats["errors"] += 1
        elif res == "active":
            stats["active"] += 1
        elif res == "rotated":
            stats["rotated"] += 1
        elif res == "expired":
            stats["expired"] += 1
        else:
            stats["errors"] += 1

    return stats
