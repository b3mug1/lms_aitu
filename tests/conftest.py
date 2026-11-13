from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.database import create_engine_and_session_factory, init_db


@pytest.fixture
def sample_settings() -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token="123456:TEST_TOKEN",
        telegram_chat_id=987654321,
        moodle_base_url="https://lms.example.edu",
        moodle_auth_mode="auto",
        moodle_username="student1",
        moodle_password="secret_password",
        timezone="Asia/Almaty",
        deadline_reminder_hours=[120, 72, 48, 24, 12, 3],
        database_url="sqlite+aiosqlite:///:memory:",
    )


@pytest.fixture
async def db_session_factory() -> async_sessionmaker[AsyncSession]:
    engine, session_factory = create_engine_and_session_factory(
        "sqlite+aiosqlite:///:memory:"
    )
    await init_db(engine)
    yield session_factory
    await engine.dispose()


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
