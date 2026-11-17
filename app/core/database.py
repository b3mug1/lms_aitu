from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for ORM models."""


def ensure_sqlite_parent_dir(database_url: str) -> None:
    """Create parent folder for file-based SQLite URLs if missing."""
    url = make_url(database_url)
    if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
        db_path = Path(url.database)
        db_path.parent.mkdir(parents=True, exist_ok=True)


def create_engine_and_session_factory(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Build async SQLAlchemy engine and session factory."""
    ensure_sqlite_parent_dir(database_url)
    engine = create_async_engine(database_url, echo=False, future=True)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return engine, session_factory


async def init_db(engine: AsyncEngine) -> None:
    """Create database tables if they do not exist and ensure columns."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        columns = await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in inspect(sync_conn).get_columns("moodle_user_accounts")
            }
        )
        if "sync_secret" not in columns:
            await conn.execute(text("ALTER TABLE moodle_user_accounts ADD COLUMN sync_secret VARCHAR(64)"))
        if "user_fullname" not in columns:
            await conn.execute(text("ALTER TABLE moodle_user_accounts ADD COLUMN user_fullname VARCHAR(255)"))
