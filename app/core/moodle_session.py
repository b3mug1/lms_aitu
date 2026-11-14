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
    """Restore a bot-updated Moodle session if available in the database."""
    async with session_factory() as session:
        repo = LMSRepository(session)
        state = await repo.get_or_create_sync_state()
        if state.session_cookie:
            lms_client.update_session_cookie(state.session_cookie)
        await session.commit()
