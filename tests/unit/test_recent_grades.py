from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.lms.base import GradeDTO
from app.notifications.formatter import format_grades_list
from app.storage.models import as_utc
from app.storage.repository import LMSRepository


@pytest.mark.asyncio
async def test_recent_grades_excludes_unconfirmed_zeros_and_registers(
    db_session_factory,
    fixed_now: datetime,
) -> None:
    grades = [
        GradeDTO(
            id="1:register",
            course_id=1,
            course_name="Cloud Computing",
            item_name="Register Midterm",
            grade_raw=70,
            grade_formatted="70",
        ),
        GradeDTO(
            id="1:placeholder",
            course_id=1,
            course_name="Cloud Computing",
            item_name="Assignment 1",
            grade_raw=0,
            grade_formatted="0.00",
            grade_max=100,
        ),
        GradeDTO(
            id="2:attendance",
            course_id=2,
            course_name="Computer Networks",
            item_name="Attendance",
            grade_raw=85.19,
            grade_formatted="85.19",
            grade_max=100,
            percentage="85.19 %",
        ),
        GradeDTO(
            id="2:zero",
            course_id=2,
            course_name="Computer Networks",
            item_name="Exam",
            grade_raw=0,
            grade_formatted="0.00",
            grade_max=100,
            graded_at=fixed_now,
        ),
    ]
    async with db_session_factory() as session:
        repo = LMSRepository(session)
        for grade in grades:
            await repo.save_grade(grade, None, fixed_now)
        await session.commit()

    async with db_session_factory() as session:
        repo = LMSRepository(session)
        attendance = (await repo.get_grades_map())["2:attendance"]
        await repo.save_grade(grades[2], attendance, fixed_now + timedelta(minutes=5))
        assert as_utc(attendance.updated_at) == fixed_now
        await session.commit()

    async with db_session_factory() as session:
        recent = await LMSRepository(session).list_recent_grades(limit=20)
        assert {grade.id for grade in recent} == {"2:attendance", "2:zero"}
        message = format_grades_list(recent, ZoneInfo("UTC"))
        assert message.count("<b>Computer Networks</b>") == 1
        assert "Attendance — <b>85.19 / 100</b>" in message
        assert "Exam — <b>0.00 / 100</b>" in message
        assert "Register Midterm" not in message
        assert "Assignment 1" not in message


@pytest.mark.asyncio
async def test_grade_update_changes_recent_order(db_session_factory, fixed_now: datetime) -> None:
    first = GradeDTO(
        id="1:first",
        course_id=1,
        course_name="Math",
        item_name="Quiz 1",
        grade_raw=80,
        grade_formatted="80",
        graded_at=fixed_now - timedelta(days=2),
    )
    second = GradeDTO(
        id="1:second",
        course_id=1,
        course_name="Math",
        item_name="Quiz 2",
        grade_raw=90,
        grade_formatted="90",
    )
    async with db_session_factory() as session:
        repo = LMSRepository(session)
        await repo.save_grade(first, None, fixed_now)
        await repo.save_grade(second, None, fixed_now + timedelta(minutes=1))
        await session.commit()

    async with db_session_factory() as session:
        repo = LMSRepository(session)
        existing = (await repo.get_grades_map())["1:first"]
        await repo.save_grade(
            first.model_copy(update={"grade_raw": 95, "grade_formatted": "95"}),
            existing,
            fixed_now + timedelta(minutes=5),
        )
        await session.commit()

    async with db_session_factory() as session:
        recent = await LMSRepository(session).list_recent_grades(limit=20)
        assert [grade.id for grade in recent] == ["1:first", "1:second"]
