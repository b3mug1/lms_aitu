from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.lms.base import AssignmentDTO, CourseDTO, GradeDTO
from app.notifications.formatter import (
    format_course_details,
    format_deadline_reminder,
    format_grade_notification,
    format_new_assignment,
    format_remaining_time,
    format_sync_status,
    format_threshold_label,
    split_telegram_message,
)
from app.storage.models import SyncStateModel


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (7200, "за 5 дней"),
        (4320, "за 3 дня"),
        (2880, "за 2 дня"),
        (1440, "за 1 день"),
        (720, "за 12 часов"),
        (180, "за 3 часа"),
        (60, "за 1 час"),
        (45, "за 45 минут"),
    ],
)
def test_format_threshold_label(minutes: int, expected: str) -> None:
    assert format_threshold_label(minutes) == expected


def test_format_new_assignment_escapes_html_and_converts_timezone() -> None:
    tz = ZoneInfo("Asia/Almaty")  # UTC+5
    now = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
    due = datetime(2026, 11, 13, 18, 59, tzinfo=timezone.utc)  # 23:59 Asia/Almaty

    dto = AssignmentDTO(
        id="assign:42",
        course_id=7,
        course_name="Algorithms & <Structures>",
        module_type="assign",
        title="Lab #1 <BST>",
        description="Implement binary search tree",
        url="https://lms.example.edu/mod/assign/view.php?id=42",
        due_at=due,
    )

    msg = format_new_assignment(dto, tz, now)
    assert "Algorithms &amp; &lt;Structures&gt;" in msg
    assert "Lab #1 &lt;BST&gt;" in msg
    assert "13.11.2026 23:59" in msg
    assert "осталось 2 д. 8 ч." in msg


def test_format_grade_notification_new_and_updated() -> None:
    grade = GradeDTO(
        id="7:10",
        course_id=7,
        course_name="Databases",
        item_name="Midterm Exam",
        grade_raw=92.5,
        grade_formatted="92.5",
        grade_max=100.0,
        percentage="92.50 %",
        feedback="Well done!",
        url="https://lms.example.edu/mod/quiz/view.php?id=10",
    )

    new_msg = format_grade_notification(grade, is_update=False)
    assert "Выставлена оценка" in new_msg
    assert "92.5 / 100 (92.50 %)" in new_msg
    assert "Well done!" in new_msg

    upd_msg = format_grade_notification(grade, is_update=True, old_grade="85.0")
    assert "Оценка обновлена" in upd_msg
    assert "85.0 → <b>92.5 / 100 (92.50 %)</b>" in upd_msg


def test_format_deadline_reminder_urgency_and_remaining() -> None:
    tz = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    due = now + timedelta(hours=2, minutes=45)
    dto = AssignmentDTO(
        id="assign:99",
        course_id=1,
        course_name="Math",
        title="HW 3",
        due_at=due,
    )
    msg = format_deadline_reminder(dto, 180, tz, now)
    assert "СРОЧНЫЙ ДЕДЛАЙН" in msg
    assert "за 3 часа" in msg
    assert format_remaining_time(due, now) == "осталось 2 ч. 45 мин."


def _course_details_dto(
    grade_raw: float,
    grade_formatted: str,
    title: str = "quiz 1 marks",
    due_at: datetime | None = None,
    submitted: bool = False,
) -> tuple[CourseDTO, list[AssignmentDTO], list[GradeDTO]]:
    course = CourseDTO(id=1, name="Research Methods|Myrzakerimova Alua")
    assignments = [
        AssignmentDTO(
            id="assign:1",
            course_id=1,
            title=title,
            due_at=due_at,
            submitted=submitted,
        )
    ]
    grades = [
        GradeDTO(
            id="1:1",
            course_id=1,
            item_name="register midterm",
            grade_raw=grade_raw,
            grade_formatted=grade_formatted,
            grade_max=100,
        )
    ]
    return course, assignments, grades


def test_format_course_details_score_dots_thresholds() -> None:
    tz = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)

    for raw, formatted, dot in [(95.0, "95.00", "🟢"), (77.0, "77.00", "🟡"), (52.0, "52.00", "🟡"), (30.0, "30.00", "🔴")]:
        course, assignments, grades = _course_details_dto(raw, formatted)
        msg = format_course_details(course, assignments, grades, tz, now)
        assert f"{dot} <b>{raw:g} / 100</b>" in msg, f"{raw} should get {dot}"
        assert ".00" not in msg.replace(".00:00", ""), "trailing zeros should be trimmed"


def test_format_course_details_sections_and_expandable_past_block() -> None:
    tz = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    course = CourseDTO(id=1, name="Research Methods|Myrzakerimova Alua")
    assignments = [
        AssignmentDTO(
            id="a:1", course_id=1, title="quiz 4",
            due_at=now + timedelta(days=3),
        ),
        AssignmentDTO(
            id="a:2", course_id=1, title="endterm",
            due_at=now - timedelta(days=2),
            url="https://lms.example.edu/mod/assign/view.php?id=2",
        ),
        AssignmentDTO(
            id="a:3", course_id=1, title="final paper",
            due_at=now - timedelta(days=3),
            submitted=True,
        ),
    ]
    grades = [
        GradeDTO(
            id="1:r", course_id=1, item_name="Register Final",
            grade_raw=82, grade_formatted="82.00", grade_max=100,
        ),
        GradeDTO(
            id="1:g", course_id=1, item_name="endterm",
            grade_raw=87, grade_formatted="87.00", grade_max=100,
        ),
    ]

    msg = format_course_details(course, assignments, grades, tz, now)

    assert "Преподаватель: Myrzakerimova Alua" in msg
    assert "<b>РЕГИСТР · 1</b>" in msg
    assert "<b>ОЦЕНКИ · 1</b>" in msg
    assert "Endterm" in msg and "endterm" not in msg.replace("Endterm", "")
    assert "🟡 <b>82 / 100</b>" in msg
    # Upcoming assignment gets compact date with weekday and countdown
    assert "через 3 д." in msg
    # Past deadlines collapse into an expandable blockquote
    assert "<blockquote expandable>" in msg and "</blockquote>" in msg
    assert "Прошедшие дедлайны · 2" in msg
    assert "не сдано" in msg and "<i>сдано</i>" in msg
    # Whole message must fit Telegram limits as a single chunk
    assert len(split_telegram_message(msg, limit=4000)) == 1


def test_format_course_details_no_deadline_and_no_assignments() -> None:
    tz = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    course, assignments, grades = _course_details_dto(77.0, "77.00", due_at=None)

    msg = format_course_details(course, assignments, grades, tz, now)
    assert "срок не указан" in msg
    assert "blockquote" not in msg

    course, assignments, grades = _course_details_dto(77.0, "77.00")
    msg = format_course_details(course, [], grades, tz, now)
    assert "<b>ЗАДАНИЯ</b> · не найдены" in msg


def _grade(name: str, raw: float, formatted: str | None = None) -> GradeDTO:
    return GradeDTO(
        id=f"1:{name}",
        course_id=1,
        item_name=name,
        grade_raw=raw,
        grade_formatted=formatted or f"{raw:g}",
        grade_max=100,
    )


def test_format_course_details_outcome_admitted_before_final() -> None:
    tz = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    course = CourseDTO(id=1, name="Research Methods|Alua")
    grades = [
        _grade("Register Midterm", 77),
        _grade("Register Endterm", 80),
    ]
    msg = format_course_details(course, [], grades, tz, now)

    assert "<b>ИТОГ · ДОПУСК ЕСТЬ</b>" in msg
    assert "РК1 77 · РК2 80 · Term 78.5" in msg
    assert "Набрано: 47.1 / 60" in msg
    assert "от 50 — сдача" in msg
    assert "от 58 — стипендия (итог 70, C+)" in msg


def test_format_course_details_outcome_final_graded() -> None:
    tz = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    course = CourseDTO(id=1, name="Research Methods|Alua")
    grades = [
        _grade("Register Midterm", 77),
        _grade("Register Endterm", 80),
        _grade("Register Final", 82),
        _grade("Register(not to edit) total", 79.4, "79.40"),
    ]
    msg = format_course_details(course, [], grades, tz, now)

    assert "Финал 82 — <b>сдан</b>" in msg
    assert "Total 79.4 → <b>B-</b> (2.67)" in msg


def test_format_course_details_outcome_not_admitted() -> None:
    tz = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    course = CourseDTO(id=1, name="Research Methods|Alua")
    grades = [
        _grade("Register Midterm", 22),
        _grade("Register Endterm", 80),
    ]
    msg = format_course_details(course, [], grades, tz, now)

    assert "<b>ИТОГ · ⚠ НЕ ДОПУЩЕН</b>" in msg
    assert "РК Midterm 22 &lt; 25 — летник" in msg
    assert "Набрано" not in msg  # no scholarship math when blocked


def test_format_course_details_outcome_absent_without_registers() -> None:
    tz = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    course = CourseDTO(id=1, name="Research Methods|Alua")
    grades = [_grade("Attendance", 82.76)]
    msg = format_course_details(course, [], grades, tz, now)

    assert "ИТОГ" not in msg


def test_split_telegram_message_respects_limit() -> None:
    lines = [f"Line {i}: " + ("x" * 80) for i in range(100)]
    big_text = "\n".join(lines)
    chunks = split_telegram_message(big_text, limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks) == big_text


def test_expired_cookie_status_explains_recovery() -> None:
    state = SyncStateModel(
        id=1,
        consecutive_errors=14,
        active_mode="cookie",
        last_error="Moodle session expired and no MOODLE_USERNAME / MOODLE_PASSWORD configured",
    )

    message = format_sync_status(state, ZoneInfo("Asia/Almaty"))

    assert "Ошибок подряд: 14" in message
    assert "Сессия Moodle истекла" in message
    assert "<code>/setcookie</code>" in message
    assert "MOODLE_USERNAME" not in message
