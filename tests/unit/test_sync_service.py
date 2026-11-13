from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import Settings
from app.lms.base import AssignmentDTO, CourseDTO, GradeDTO, LMSAuthError, LMSConnectionError, LMSSnapshot
from app.notifications.service import SyncService
from app.storage.repository import LMSRepository


class FakeLMSClient:
    def __init__(self, snapshots: list[LMSSnapshot | Exception]) -> None:
        self._items = list(snapshots)
        self.mode_name = "rest"

    async def fetch_snapshot(self) -> LMSSnapshot:
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        return None


class CookieLMSClient(FakeLMSClient):
    def __init__(self, snapshots: list[LMSSnapshot | Exception]) -> None:
        super().__init__(snapshots)
        self.updated_cookies: list[str] = []

    def update_session_cookie(self, cookie_value: str) -> None:
        self.updated_cookies.append(cookie_value)


class RecordingSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, text: str) -> None:
        self.messages.append(text)


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> datetime:
        self.current += timedelta(**kwargs)
        return self.current


@pytest.mark.asyncio
async def test_bootstrap_silent_baseline_then_new_grade_and_assignment(
    sample_settings: Settings,
    db_session_factory,
    fixed_now: datetime,
) -> None:
    course = CourseDTO(id=1, name="Linear Algebra")
    old_assign = AssignmentDTO(
        id="assign:10",
        course_id=1,
        course_name="Linear Algebra",
        title="HW 1",
        due_at=fixed_now + timedelta(days=10),
    )
    old_grade = GradeDTO(
        id="1:100",
        course_id=1,
        course_name="Linear Algebra",
        item_name="Quiz 1",
        grade_raw=80.0,
        grade_formatted="80",
        grade_max=100.0,
    )

    new_assign = AssignmentDTO(
        id="assign:11",
        course_id=1,
        course_name="Linear Algebra",
        title="HW 2",
        due_at=fixed_now + timedelta(days=7),
    )
    updated_grade = old_grade.model_copy(
        update={"grade_raw": 90.0, "grade_formatted": "90", "feedback": "Regraded"}
    )
    new_grade = GradeDTO(
        id="1:101",
        course_id=1,
        course_name="Linear Algebra",
        item_name="HW 1",
        grade_raw=95.0,
        grade_formatted="95",
        grade_max=100.0,
    )

    snap1 = LMSSnapshot(
        fetched_at=fixed_now,
        mode="rest",
        user_fullname="Test Student",
        courses=[course],
        assignments=[old_assign],
        grades=[old_grade],
    )
    snap2 = LMSSnapshot(
        fetched_at=fixed_now + timedelta(minutes=5),
        mode="rest",
        user_fullname="Test Student",
        courses=[course],
        assignments=[old_assign, new_assign],
        grades=[updated_grade, new_grade],
    )

    clock = MutableClock(fixed_now)
    sender = RecordingSender()
    service = SyncService(
        sample_settings,
        FakeLMSClient([snap1, snap2]),
        db_session_factory,
        sender,
        clock=clock,
    )

    # First run: only 1 welcome message, no flood for old items
    rep1 = await service.sync_once()
    assert rep1.ok is True
    assert rep1.is_bootstrap is True
    assert rep1.notifications_sent == 1
    assert "Moodle подключён" in sender.messages[0]

    # Second run: new assignment + new grade + updated grade
    clock.advance(minutes=5)
    sender.messages.clear()
    rep2 = await service.sync_once()
    assert rep2.ok is True
    assert rep2.is_bootstrap is False
    assert rep2.new_assignments == 1
    assert rep2.new_grades == 1
    assert rep2.updated_grades == 1
    assert rep2.notifications_sent == 3
    joined = "\n".join(sender.messages)
    assert "HW 2" in joined
    assert "Выставлена оценка" in joined
    assert "Оценка обновлена" in joined


@pytest.mark.asyncio
async def test_all_six_deadline_thresholds_and_deduplication(
    sample_settings: Settings,
    db_session_factory,
    fixed_now: datetime,
) -> None:
    due_at = fixed_now + timedelta(days=6)
    course = CourseDTO(id=1, name="OS")
    assign = AssignmentDTO(
        id="assign:50",
        course_id=1,
        course_name="OS",
        title="Kernel Lab",
        due_at=due_at,
        submitted=False,
    )
    snap = LMSSnapshot(
        fetched_at=fixed_now,
        mode="rest",
        courses=[course],
        assignments=[assign],
        grades=[],
    )

    # 13+ snapshots across time progression
    clock = MutableClock(fixed_now)
    sender = RecordingSender()
    service = SyncService(
        sample_settings,
        FakeLMSClient([snap] * 20),
        db_session_factory,
        sender,
        clock=clock,
    )

    # T - 6d: bootstrap, no deadline alerts yet
    await service.sync_once()
    assert len(sender.messages) == 1  # welcome only

    checkpoints = [
        (due_at - timedelta(days=4, hours=20), "за 5 дней"),
        (due_at - timedelta(days=2, hours=20), "за 3 дня"),
        (due_at - timedelta(days=1, hours=20), "за 2 дня"),
        (due_at - timedelta(hours=23), "за 1 день"),
        (due_at - timedelta(hours=11), "за 12 часов"),
        (due_at - timedelta(hours=2, minutes=30), "за 3 часа"),
    ]

    for check_time, expected_label in checkpoints:
        sender.messages.clear()
        clock.current = check_time
        rep = await service.sync_once()
        assert rep.deadline_reminders == 1
        assert len(sender.messages) == 1
        assert expected_label in sender.messages[0]

        # Polling again 15 min later does NOT duplicate the same threshold
        sender.messages.clear()
        clock.current = check_time + timedelta(minutes=15)
        rep_repeat = await service.sync_once()
        assert rep_repeat.deadline_reminders == 0
        assert sender.messages == []


@pytest.mark.asyncio
async def test_deadline_tightest_threshold_skips_looser_and_respects_submitted_and_reschedule(
    sample_settings: Settings,
    db_session_factory,
    fixed_now: datetime,
) -> None:
    due_at = fixed_now + timedelta(hours=30)  # qualifies for 5d, 3d, 2d simultaneously!
    course = CourseDTO(id=2, name="Networks")
    assign_open = AssignmentDTO(
        id="assign:77",
        course_id=2,
        course_name="Networks",
        title="TCP Proxy",
        due_at=due_at,
        submitted=False,
    )
    assign_submitted = assign_open.model_copy(update={"submitted": True})
    new_due = due_at + timedelta(days=3)
    assign_rescheduled = assign_open.model_copy(update={"due_at": new_due, "submitted": False})

    clock = MutableClock(fixed_now)
    sender = RecordingSender()
    service = SyncService(
        sample_settings,
        FakeLMSClient(
            [
                LMSSnapshot(fetched_at=fixed_now, mode="rest", courses=[course], assignments=[assign_open]),
                LMSSnapshot(fetched_at=fixed_now, mode="rest", courses=[course], assignments=[assign_open]),
                LMSSnapshot(fetched_at=fixed_now, mode="rest", courses=[course], assignments=[assign_submitted]),
                LMSSnapshot(fetched_at=fixed_now, mode="rest", courses=[course], assignments=[assign_rescheduled]),
            ]
        ),
        db_session_factory,
        sender,
        clock=clock,
    )

    # First run: welcome + ONLY tightest threshold (2 days = 48h), NOT 5d or 3d
    await service.sync_once()
    assert len(sender.messages) == 2
    assert "за 2 дня" in sender.messages[1]

    # Second run 1h later (still 29h left): 3d & 5d were marked superseded -> 0 alerts
    sender.messages.clear()
    clock.advance(hours=1)
    await service.sync_once()
    assert sender.messages == []

    # Third run at 10h left, but student submitted -> skip reminder!
    sender.messages.clear()
    clock.current = due_at - timedelta(hours=10)
    await service.sync_once()
    assert sender.messages == []

    # Fourth run: teacher extended deadline by 3 days -> deadline change + new 5d reminder
    sender.messages.clear()
    rep = await service.sync_once()
    assert rep.deadline_changes == 1
    assert rep.deadline_reminders == 1
    assert "Изменился срок сдачи" in sender.messages[0]
    assert "за 5 дней" in sender.messages[1]


@pytest.mark.asyncio
async def test_consecutive_error_alert_and_recovery(
    sample_settings: Settings,
    db_session_factory,
    fixed_now: datetime,
) -> None:
    snap_ok = LMSSnapshot(fetched_at=fixed_now, mode="rest", courses=[], assignments=[], grades=[])
    clock = MutableClock(fixed_now)
    sender = RecordingSender()
    service = SyncService(
        sample_settings,
        FakeLMSClient(
            [
                LMSConnectionError("timeout 1"),
                LMSConnectionError("timeout 2"),
                LMSConnectionError("timeout 3"),
                LMSConnectionError("timeout 4"),
                snap_ok,
            ]
        ),
        db_session_factory,
        sender,
        clock=clock,
    )

    # threshold=3 -> no alert on errors 1, 2; alert once on 3; silent on 4
    await service.sync_once()
    await service.sync_once()
    assert sender.messages == []

    await service.sync_once()
    assert len(sender.messages) == 1
    assert "Проблема подключения к Moodle" in sender.messages[0]

    await service.sync_once()
    assert len(sender.messages) == 1  # no duplicate alert

    # Recovery sends welcome + recovery message
    await service.sync_once()
    assert any("восстановлено" in m for m in sender.messages)


@pytest.mark.asyncio
async def test_new_cookie_is_saved_only_after_successful_moodle_check(
    sample_settings: Settings,
    db_session_factory,
    fixed_now: datetime,
) -> None:
    snapshot = LMSSnapshot(
        fetched_at=fixed_now,
        mode="cookie",
        courses=[],
        assignments=[],
        grades=[],
    )
    client = CookieLMSClient(
        [
            LMSAuthError("Moodle session expired and no MOODLE_USERNAME / MOODLE_PASSWORD configured"),
            snapshot,
        ]
    )
    service = SyncService(
        sample_settings,
        client,
        db_session_factory,
        RecordingSender(),
        clock=MutableClock(fixed_now),
    )

    failed = await service.update_session_cookie("invalid")
    assert failed.ok is False
    assert client.updated_cookies == ["invalid", ""]
    async with db_session_factory() as session:
        state = await LMSRepository(session).get_or_create_sync_state()
        assert state.session_cookie is None

    succeeded = await service.update_session_cookie("MoodleSession=valid")
    assert succeeded.ok is True
    async with db_session_factory() as session:
        state = await LMSRepository(session).get_or_create_sync_state()
        assert state.session_cookie == "valid"
        assert state.consecutive_errors == 0
