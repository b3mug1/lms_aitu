from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.lms.base import AssignmentDTO, CourseDTO, GradeDTO
from app.storage.models import (
    AssignmentModel,
    CourseModel,
    GradeModel,
    SentDeadlineReminderModel,
    SyncStateModel,
    as_utc,
)


class LMSRepository:
    """Persistence operations for Moodle state and notification deduplication."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_sync_state(self) -> SyncStateModel:
        state = await self._session.get(SyncStateModel, 1)
        if state is None:
            state = SyncStateModel(id=1, initialized=False, consecutive_errors=0)
            self._session.add(state)
            await self._session.flush()
        return state

    async def list_courses(self) -> list[CourseModel]:
        res = await self._session.execute(select(CourseModel).order_by(CourseModel.name))
        return list(res.scalars().all())

    async def get_course(self, course_id: int) -> CourseModel | None:
        return await self._session.get(CourseModel, course_id)

    async def list_course_assignments(self, course_id: int) -> list[AssignmentModel]:
        stmt = (
            select(AssignmentModel)
            .where(AssignmentModel.course_id == course_id)
            .order_by(AssignmentModel.due_at.asc().nulls_last(), AssignmentModel.title.asc())
        )
        res = await self._session.execute(stmt)
        rows = list(res.scalars().all())
        for row in rows:
            row.due_at = as_utc(row.due_at)
        return rows

    async def list_course_grades(self, course_id: int) -> list[GradeModel]:
        stmt = (
            select(GradeModel)
            .where(GradeModel.course_id == course_id)
            .order_by(GradeModel.first_seen_at.asc(), GradeModel.item_name.asc())
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def get_assignments_map(self) -> dict[str, AssignmentModel]:
        res = await self._session.execute(select(AssignmentModel))
        return {row.id: row for row in res.scalars().all()}

    async def get_grades_map(self) -> dict[str, GradeModel]:
        res = await self._session.execute(select(GradeModel))
        return {row.id: row for row in res.scalars().all()}

    async def upsert_courses(self, courses: list[CourseDTO], now: datetime) -> None:
        existing_res = await self._session.execute(select(CourseModel))
        existing_map = {c.id: c for c in existing_res.scalars().all()}

        for dto in courses:
            model = existing_map.get(dto.id)
            if model is None:
                self._session.add(
                    CourseModel(
                        id=dto.id,
                        name=dto.name,
                        short_name=dto.short_name,
                        url=dto.url,
                        updated_at=now,
                    )
                )
            else:
                model.name = dto.name
                model.short_name = dto.short_name
                model.url = dto.url
                model.updated_at = now

    async def save_assignment(
        self, dto: AssignmentDTO, existing: AssignmentModel | None, now: datetime
    ) -> AssignmentModel:
        if existing is None:
            model = AssignmentModel(
                id=dto.id,
                course_id=dto.course_id,
                course_name=dto.course_name,
                module_type=dto.module_type,
                title=dto.title,
                description=dto.description,
                url=dto.url,
                due_at=dto.due_at,
                allow_from=dto.allow_from,
                submitted=dto.submitted,
                first_seen_at=now,
                updated_at=now,
            )
            self._session.add(model)
            return model

        existing.course_id = dto.course_id
        existing.course_name = dto.course_name
        existing.module_type = dto.module_type
        existing.title = dto.title
        existing.description = dto.description
        existing.url = dto.url
        existing.due_at = dto.due_at
        existing.allow_from = dto.allow_from
        existing.submitted = dto.submitted
        existing.updated_at = now
        return existing

    async def save_grade(
        self, dto: GradeDTO, existing: GradeModel | None, now: datetime
    ) -> GradeModel:
        if existing is None:
            model = GradeModel(
                id=dto.id,
                course_id=dto.course_id,
                course_name=dto.course_name,
                assignment_id=dto.assignment_id,
                item_name=dto.item_name,
                module_type=dto.module_type,
                grade_raw=dto.grade_raw,
                grade_formatted=dto.grade_formatted,
                grade_max=dto.grade_max,
                percentage=dto.percentage,
                feedback=dto.feedback,
                graded_at=dto.graded_at,
                url=dto.url,
                first_seen_at=now,
                updated_at=now,
            )
            self._session.add(model)
            return model

        result_changed = (
            existing.grade_raw != dto.grade_raw
            or existing.grade_formatted != dto.grade_formatted
            or existing.feedback != dto.feedback
            or as_utc(existing.graded_at) != as_utc(dto.graded_at)
        )
        existing.course_id = dto.course_id
        existing.course_name = dto.course_name
        existing.assignment_id = dto.assignment_id
        existing.item_name = dto.item_name
        existing.module_type = dto.module_type
        existing.grade_raw = dto.grade_raw
        existing.grade_formatted = dto.grade_formatted
        existing.grade_max = dto.grade_max
        existing.percentage = dto.percentage
        existing.feedback = dto.feedback
        existing.graded_at = dto.graded_at
        existing.url = dto.url
        if result_changed:
            existing.updated_at = now
        return existing

    async def get_sent_reminder_thresholds(
        self, assignment_id: str, due_at_epoch: int
    ) -> set[int]:
        stmt = select(SentDeadlineReminderModel.threshold_minutes).where(
            SentDeadlineReminderModel.assignment_id == assignment_id,
            SentDeadlineReminderModel.due_at_epoch == due_at_epoch,
        )
        res = await self._session.execute(stmt)
        return set(res.scalars().all())

    async def record_sent_reminders(
        self,
        assignment_id: str,
        due_at_epoch: int,
        thresholds_minutes: list[int],
        now: datetime,
    ) -> None:
        already_sent = await self.get_sent_reminder_thresholds(assignment_id, due_at_epoch)
        for threshold in thresholds_minutes:
            if threshold not in already_sent:
                self._session.add(
                    SentDeadlineReminderModel(
                        assignment_id=assignment_id,
                        due_at_epoch=due_at_epoch,
                        threshold_minutes=threshold,
                        sent_at=now,
                    )
                )

    async def list_upcoming_deadlines(
        self,
        now: datetime,
        lookahead_days: int,
        include_submitted: bool = False,
    ) -> list[AssignmentModel]:
        horizon = now + timedelta(days=lookahead_days)
        stmt = (
            select(AssignmentModel)
            .where(
                AssignmentModel.due_at.is_not(None),
                AssignmentModel.due_at >= now,
                AssignmentModel.due_at <= horizon,
            )
            .order_by(AssignmentModel.due_at.asc())
        )
        if not include_submitted:
            stmt = stmt.where(AssignmentModel.submitted.is_(False))
        res = await self._session.execute(stmt)
        rows = list(res.scalars().all())
        for row in rows:
            row.due_at = as_utc(row.due_at)
        return rows

    async def list_recent_grades(self, limit: int = 15) -> list[GradeModel]:
        # A zero without a grading date or feedback may be an ungraded Moodle
        # placeholder. Keep it in the course card, but omit it from "recent".
        has_confirmed_result = or_(
            GradeModel.grade_raw.is_(None),
            GradeModel.grade_raw != 0,
            GradeModel.graded_at.is_not(None),
            and_(GradeModel.feedback.is_not(None), GradeModel.feedback != ""),
        )
        recent_at = case(
            (GradeModel.graded_at > GradeModel.updated_at, GradeModel.graded_at),
            else_=GradeModel.updated_at,
        )
        stmt = (
            select(GradeModel)
            .where(
                ~func.lower(GradeModel.item_name).like("register%"),
                has_confirmed_result,
            )
            .order_by(
                recent_at.desc(),
                GradeModel.first_seen_at.desc(),
            )
            .limit(limit)
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())
