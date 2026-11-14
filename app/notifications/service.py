from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.lms.base import GradeDTO, LMSClient, LMSError
from app.notifications.formatter import (
    format_deadline_changed,
    format_deadline_reminder,
    format_grade_notification,
    format_new_assignment,
    format_sync_error,
    format_welcome_message,
)
from app.storage.models import GradeModel, as_utc
from app.storage.repository import LMSRepository

logger = logging.getLogger(__name__)


class NotificationSender(Protocol):
    """Outbound notification channel interface (implemented by TelegramNotifier)."""

    async def send_message(self, text: str) -> None:
        ...


@dataclass
class SyncReport:
    """Summary returned from a single sync execution."""

    ok: bool
    skipped: bool = False
    is_bootstrap: bool = False
    new_assignments: int = 0
    new_grades: int = 0
    updated_grades: int = 0
    deadline_changes: int = 0
    deadline_reminders: int = 0
    notifications_sent: int = 0
    error: str | None = None
    messages: list[str] = field(default_factory=list)


def _grade_changed(existing: GradeModel, incoming: GradeDTO) -> bool:
    if existing.grade_raw is not None and incoming.grade_raw is not None:
        if abs(existing.grade_raw - incoming.grade_raw) > 1e-6:
            return True
    elif existing.grade_formatted.strip() != incoming.grade_formatted.strip():
        return True

    old_feedback = (existing.feedback or "").strip()
    new_feedback = (incoming.feedback or "").strip()
    if new_feedback and new_feedback != old_feedback:
        return True

    return False


class SyncService:
    """Coordinates Moodle polling, state diffing, and deduplicated Telegram alerts."""

    def __init__(
        self,
        settings: Settings,
        lms_client: LMSClient,
        session_factory: async_sessionmaker[AsyncSession],
        sender: NotificationSender,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._lms = lms_client
        self._session_factory = session_factory
        self._sender = sender
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()

    async def update_session_cookie(self, cookie_value: str) -> SyncReport:
        """Verify a MoodleSession cookie before persisting it for future runs."""
        cleaned = cookie_value.strip()
        if cleaned.startswith("MoodleSession="):
            cleaned = cleaned.split("=", 1)[1].strip()
        if not cleaned:
            return SyncReport(ok=False, error="Empty MoodleSession cookie")
        if not hasattr(self._lms, "update_session_cookie"):
            return SyncReport(ok=False, error="Moodle client cannot update its session cookie")

        env_cookie = (
            self._settings.moodle_session_cookie.get_secret_value()
            if self._settings.moodle_session_cookie
            else ""
        )
        env_hash = hashlib.sha256(env_cookie.encode("utf-8")).hexdigest()
        async with self._session_factory() as session:
            repo = LMSRepository(session)
            state = await repo.get_or_create_sync_state()
            previous_cookie = state.session_cookie or env_cookie

        async with self._lock:
            self._lms.update_session_cookie(cleaned)  # type: ignore[attr-defined]
            try:
                report = await self._sync_once_locked(trigger="setcookie")
                if not report.ok or report.skipped:
                    self._lms.update_session_cookie(previous_cookie)  # type: ignore[attr-defined]
                    return report

                async with self._session_factory() as session:
                    repo = LMSRepository(session)
                    state = await repo.get_or_create_sync_state()
                    state.session_cookie = cleaned
                    state.env_cookie_hash = env_hash
                    await session.commit()
                return report
            except Exception:
                self._lms.update_session_cookie(previous_cookie)  # type: ignore[attr-defined]
                raise

    async def sync_once(self, trigger: str = "poll") -> SyncReport:
        async with self._lock:
            return await self._sync_once_locked(trigger)

    async def _sync_once_locked(self, trigger: str) -> SyncReport:
        now = as_utc(self._clock()) or datetime.now(timezone.utc)
        try:
            snapshot = await self._lms.fetch_snapshot()
        except LMSError as exc:
            return await self._handle_sync_error(now, str(exc))
        except Exception as exc:
            logger.exception("Unexpected error while fetching Moodle snapshot")
            return await self._handle_sync_error(now, f"{type(exc).__name__}: {exc}")

        return await self._process_snapshot(snapshot, now, trigger=trigger)

    async def _handle_sync_error(self, now: datetime, error_message: str) -> SyncReport:
        logger.error("Moodle sync failed: %s", error_message)
        async with self._session_factory() as session:
            repo = LMSRepository(session)
            state = await repo.get_or_create_sync_state()
            state.last_sync_at = now
            state.consecutive_errors += 1
            state.last_error = error_message[:1000]

            if (
                state.consecutive_errors >= self._settings.error_alert_threshold
                and not state.error_alert_sent
            ):
                alert_text = (
                    "<b>Проблема подключения к Moodle</b>\n"
                    f"Ошибок подряд: <b>{state.consecutive_errors}</b>\n"
                    f"Причина: {format_sync_error(error_message)}"
                )
                try:
                    await self._sender.send_message(alert_text)
                    state.error_alert_sent = True
                except Exception:
                    logger.exception("Failed to send error alert to Telegram")

            await session.commit()
        return SyncReport(ok=False, error=error_message)

    async def _process_snapshot(
        self, snapshot, now: datetime, trigger: str
    ) -> SyncReport:
        tz = self._settings.tz_info
        report = SyncReport(ok=True)
        outbound_messages: list[str] = []
        reminder_records: list[tuple[str, int, list[int]]] = []

        async with self._session_factory() as session:
            if session.get_bind().dialect.name == "postgresql":
                acquired = await session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
                    {"lock_key": 712364981},
                )
                if not acquired:
                    return SyncReport(ok=True, skipped=True)
            repo = LMSRepository(session)
            state = await repo.get_or_create_sync_state()
            is_bootstrap = not state.initialized
            report.is_bootstrap = is_bootstrap
            should_notify_changes = (not is_bootstrap) or self._settings.notify_on_first_run

            await repo.upsert_courses(snapshot.courses, now)
            existing_assignments = await repo.get_assignments_map()
            existing_grades = await repo.get_grades_map()

            # 1. Diff assignments & deadline changes
            for dto in snapshot.assignments:
                existing = existing_assignments.get(dto.id)
                if existing is None:
                    await repo.save_assignment(dto, None, now)
                    if should_notify_changes:
                        report.new_assignments += 1
                        outbound_messages.append(format_new_assignment(dto, tz, now))
                else:
                    old_due = as_utc(existing.due_at)
                    new_due = as_utc(dto.due_at)
                    if (
                        old_due != new_due
                        and self._settings.notify_on_deadline_change
                        and should_notify_changes
                    ):
                        report.deadline_changes += 1
                        outbound_messages.append(
                            format_deadline_changed(dto, old_due, tz, now)
                        )
                    await repo.save_assignment(dto, existing, now)

            # 2. Diff grades
            for dto in snapshot.grades:
                existing = existing_grades.get(dto.id)
                if existing is None:
                    await repo.save_grade(dto, None, now)
                    if should_notify_changes:
                        report.new_grades += 1
                        outbound_messages.append(
                            format_grade_notification(dto, is_update=False)
                        )
                elif _grade_changed(existing, dto):
                    old_formatted = existing.grade_formatted
                    await repo.save_grade(dto, existing, now)
                    if should_notify_changes:
                        report.updated_grades += 1
                        outbound_messages.append(
                            format_grade_notification(
                                dto, is_update=True, old_grade=old_formatted
                            )
                        )
                else:
                    await repo.save_grade(dto, existing, now)

            # 3. Check deadline reminders (5d, 3d, 2d, 1d, 12h, 3h)
            thresholds = self._settings.deadline_reminder_minutes
            for dto in snapshot.assignments:
                due_utc = as_utc(dto.due_at)
                if due_utc is None:
                    continue
                if dto.submitted and self._settings.skip_submitted_deadlines:
                    continue

                remaining_minutes = (due_utc - now).total_seconds() / 60.0
                if remaining_minutes <= 0:
                    continue

                due_epoch = int(due_utc.timestamp())
                already_sent = await repo.get_sent_reminder_thresholds(dto.id, due_epoch)
                matching_unsent = [
                    t for t in thresholds if remaining_minutes <= t and t not in already_sent
                ]
                if not matching_unsent:
                    continue

                # Fire only the tightest applicable threshold and mark looser ones superseded
                target_threshold = min(matching_unsent)
                report.deadline_reminders += 1
                outbound_messages.append(
                    format_deadline_reminder(dto, target_threshold, tz, now)
                )
                reminder_records.append((dto.id, due_epoch, matching_unsent))

            # Welcome message on initial silent baseline sync
            if is_bootstrap and not self._settings.notify_on_first_run:
                upcoming_count = sum(
                    1
                    for a in snapshot.assignments
                    if a.due_at is not None
                    and (as_utc(a.due_at) > now)  # type: ignore[operator]
                    and not (a.submitted and self._settings.skip_submitted_deadlines)
                )
                outbound_messages.insert(
                    0, format_welcome_message(snapshot, tz, upcoming_count)
                )

            # Deliver outbound messages BEFORE committing state so transient Telegram errors retry cleanly
            for msg in outbound_messages:
                await self._sender.send_message(msg)
                report.notifications_sent += 1
                report.messages.append(msg)

            for assignment_id, due_epoch, fired_thresholds in reminder_records:
                await repo.record_sent_reminders(
                    assignment_id, due_epoch, fired_thresholds, now
                )

            recovered_from_error = state.error_alert_sent
            state.initialized = True
            state.active_mode = snapshot.mode
            state.user_fullname = snapshot.user_fullname or state.user_fullname
            state.last_sync_at = now
            state.last_success_at = now
            state.consecutive_errors = 0
            state.last_error = None
            state.error_alert_sent = False

            await session.commit()

            if recovered_from_error:
                try:
                    await self._sender.send_message("<b>Подключение к Moodle восстановлено</b>")
                except Exception:
                    logger.warning("Failed to send recovery notice to Telegram")

        logger.info(
            "Sync (%s) finished: courses=%d assignments=%d grades=%d sent=%d",
            trigger,
            len(snapshot.courses),
            len(snapshot.assignments),
            len(snapshot.grades),
            report.notifications_sent,
        )
        return report
