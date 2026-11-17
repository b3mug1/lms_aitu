from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.core.config import Settings
from app.storage.models import MoodleUserAccountModel, PersonalSyncStateModel


class UserSessionError(Exception):
    """Raised when an encrypted per-user Moodle session cannot be used."""


def _fernet(settings: Settings) -> Fernet:
    secret: SecretStr | None = settings.moodle_cookie_encryption_key
    if secret is None:
        raise UserSessionError("Per-user Moodle session encryption is not configured")
    try:
        return Fernet(secret.get_secret_value().encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise UserSessionError("Moodle cookie encryption key is invalid") from exc


def encrypt_cookie(settings: Settings, cookie: str) -> str:
    return _fernet(settings).encrypt(cookie.encode("utf-8")).decode("ascii")


def decrypt_cookie(settings: Settings, encrypted_cookie: str) -> str:
    try:
        return _fernet(settings).decrypt(encrypted_cookie.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError) as exc:
        raise UserSessionError("Stored Moodle session cannot be decrypted") from exc


async def save_user_cookie(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    telegram_user_id: int,
    cookie: str,
    sync_secret: str | None = None,
    user_fullname: str | None = None,
) -> None:
    encrypted_cookie = encrypt_cookie(settings, cookie)
    async with session_factory() as session:
        account = await session.get(MoodleUserAccountModel, telegram_user_id)
        if account is None:
            account = MoodleUserAccountModel(
                telegram_user_id=telegram_user_id,
                encrypted_cookie=encrypted_cookie,
                sync_secret=sync_secret,
                user_fullname=user_fullname,
            )
            session.add(account)
        else:
            account.encrypted_cookie = encrypted_cookie
            if sync_secret is not None:
                account.sync_secret = sync_secret
            if user_fullname is not None:
                account.user_fullname = user_fullname
        await session.commit()


async def update_user_cookie_silently(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    telegram_user_id: int,
    sync_secret: str,
    cookie: str,
) -> bool:
    """Silently update cookie if sync_secret matches."""
    encrypted_cookie = encrypt_cookie(settings, cookie)
    async with session_factory() as session:
        account = await session.get(MoodleUserAccountModel, telegram_user_id)
        if account is None or not account.sync_secret:
            return False
        if account.sync_secret != sync_secret:
            return False
        account.encrypted_cookie = encrypted_cookie
        await session.commit()
        return True


async def load_user_cookie(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    telegram_user_id: int,
) -> str | None:
    async with session_factory() as session:
        account = await session.get(MoodleUserAccountModel, telegram_user_id)
        encrypted_cookie = account.encrypted_cookie if account else None
    if encrypted_cookie is None:
        return None
    return decrypt_cookie(settings, encrypted_cookie)


async def delete_user_account(
    session_factory: async_sessionmaker[AsyncSession], telegram_user_id: int
) -> bool:
    async with session_factory() as session:
        account = await session.get(MoodleUserAccountModel, telegram_user_id)
        if account is None:
            return False
        await session.delete(account)
        state = await session.get(PersonalSyncStateModel, telegram_user_id)
        if state is not None:
            await session.delete(state)
        await session.commit()
        return True


async def user_account_exists(
    session_factory: async_sessionmaker[AsyncSession], telegram_user_id: int
) -> bool:
    async with session_factory() as session:
        return await session.get(MoodleUserAccountModel, telegram_user_id) is not None
