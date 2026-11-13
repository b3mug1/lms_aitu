from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.lms.client import AutoMoodleClient
from app.storage.repository import LMSRepository


async def restore_saved_cookie(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    lms_client: AutoMoodleClient,
) -> None:
    """Restore a bot-updated Moodle session if its configured base cookie is unchanged."""
    env_cookie = (
        settings.moodle_session_cookie.get_secret_value()
        if settings.moodle_session_cookie
        else ""
    )
    current_env_hash = hashlib.sha256(env_cookie.encode("utf-8")).hexdigest()
    async with session_factory() as session:
        repo = LMSRepository(session)
        state = await repo.get_or_create_sync_state()
        if state.session_cookie and state.env_cookie_hash == current_env_hash:
            lms_client.update_session_cookie(state.session_cookie)
        await session.commit()
