from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.lms.base import AssignmentDTO, GradeDTO
from app.notifications.formatter import (
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
