from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import Base
from app.storage.models import (
    AssignmentModel,
    CourseModel,
    GradeModel,
    SentDeadlineReminderModel,
    SyncStateModel,
    as_utc,
)


TABLES = (
    CourseModel.__table__,
    AssignmentModel.__table__,
    GradeModel.__table__,
    SentDeadlineReminderModel.__table__,
    SyncStateModel.__table__,
)


async def migrate(source: Path, target_url: str) -> dict[str, int]:
    if not source.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source}")
    if not target_url.startswith("postgresql+asyncpg://"):
        raise ValueError("DATABASE_URL must start with postgresql+asyncpg://")

    source_url = URL.create("sqlite+aiosqlite", database=str(source.resolve()))
    source_engine = create_async_engine(source_url)
    target_engine = create_async_engine(target_url)
    try:
        rows_by_table: dict[str, list[dict]] = {}
        async with source_engine.connect() as connection:
            for table in TABLES:
                result = await connection.execute(select(table))
                rows_by_table[table.name] = [
                    {
                        key: as_utc(value) if isinstance(value, datetime) else value
                        for key, value in row.items()
                    }
                    for row in result.mappings()
                ]

        async with target_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for table in TABLES:
                count = await connection.scalar(select(func.count()).select_from(table))
                if count:
                    raise RuntimeError(
                        f"Target table {table.name} is not empty; migration was not applied"
                    )

            for table in TABLES:
                rows = rows_by_table[table.name]
                if not rows:
                    continue
                if table is SentDeadlineReminderModel.__table__:
                    rows = [{key: value for key, value in row.items() if key != "id"} for row in rows]
                await connection.execute(table.insert(), rows)
        return {name: len(rows) for name, rows in rows_by_table.items()}
    finally:
        await source_engine.dispose()
        await target_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy the local notifier state to empty PostgreSQL")
    parser.add_argument("--source", type=Path, default=Path("data/lms_notifier.db"))
    args = parser.parse_args()
    target_url = os.environ.get("DATABASE_URL", "")
    counts = asyncio.run(migrate(args.source, target_url))
    for table, count in counts.items():
        print(f"{table}: {count} rows copied")


if __name__ == "__main__":
    main()
