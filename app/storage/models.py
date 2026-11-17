from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    BigInteger,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """Normalize SQLite naive datetimes or aware datetimes to UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class CourseModel(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    short_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AssignmentModel(Base):
    __tablename__ = "assignments"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    course_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    course_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    module_type: Mapped[str] = mapped_column(String(32), default="assign", nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    allow_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class GradeModel(Base):
    __tablename__ = "grades"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    course_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    course_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    assignment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    item_name: Mapped[str] = mapped_column(String(255), nullable=False)
    module_type: Mapped[str] = mapped_column(String(32), default="mod", nullable=False)
    grade_raw: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade_formatted: Mapped[str] = mapped_column(String(64), nullable=False)
    grade_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    percentage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class SentDeadlineReminderModel(Base):
    __tablename__ = "sent_deadline_reminders"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "due_at_epoch",
            "threshold_minutes",
            name="uq_reminder_assign_due_threshold",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assignment_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    due_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class SyncStateModel(Base):
    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    initialized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_fullname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_alert_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    session_cookie: Mapped[str | None] = mapped_column(Text, nullable=True)
    env_cookie_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MoodleUserAccountModel(Base):
    """Encrypted Moodle session associated with one private Telegram account."""

    __tablename__ = "moodle_user_accounts"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    encrypted_cookie: Mapped[str] = mapped_column(Text, nullable=False)
    sync_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_fullname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class PersonalSyncStateModel(Base):
    """Last delivered Moodle state for one Telegram user."""

    __tablename__ = "personal_sync_states"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    grades_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    assignments_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    reminders_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
