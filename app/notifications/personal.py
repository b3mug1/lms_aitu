"""Periodic, user-scoped Moodle change notifications."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from time import monotonic

from aiogram import Bot
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.user_sessions import UserSessionError, decrypt_cookie
from app.lms.base import LMSError
from app.lms.client import AutoMoodleClient
from app.notifications.formatter import (
    format_deadline_changed,
    format_deadline_reminder,
    format_grade_notification,
    format_new_assignment,
    split_telegram_message,
)
from app.storage.models import MoodleUserAccountModel, PersonalSyncStateModel

logger = logging.getLogger(__name__)


async def sync_personal_accounts(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], bot: Bot
) -> dict[str, int]:
    """Fetch each registered account, then deliver only changes for that account."""
    result = {"accounts": 0, "updated": 0, "sent": 0, "failed": 0}
    if settings.moodle_cookie_encryption_key is None:
        return result

    async with session_factory() as session:
        account_ids = list(
            (await session.scalars(
                select(MoodleUserAccountModel.telegram_user_id)
                .outerjoin(
                    PersonalSyncStateModel,
                    PersonalSyncStateModel.telegram_user_id == MoodleUserAccountModel.telegram_user_id,
                )
                .order_by(PersonalSyncStateModel.last_checked_at.asc().nullsfirst())
            )).all()
        )
    result["accounts"] = len(account_ids)
    deadline = monotonic() + 30

    for user_id in account_ids:
        if monotonic() >= deadline:
            break
        try:
            # Drain any messages left by an interrupted previous run before
            # fetching a newer snapshot for this account.
            async with session_factory() as session:
                state = await session.get(PersonalSyncStateModel, user_id)
                pending = json.loads(state.pending_messages_json) if state else []
            while pending:
                await bot.send_message(chat_id=user_id, text=pending[0])
                pending.pop(0)
                async with session_factory() as session:
                    state = await session.get(PersonalSyncStateModel, user_id)
                    if state is not None:
                        state.pending_messages_json = json.dumps(pending)
                        await session.commit()
                result["sent"] += 1

            async with session_factory() as session:
                account = await session.get(MoodleUserAccountModel, user_id)
                if account is None:
                    continue
                cookie = decrypt_cookie(settings, account.encrypted_cookie)

            user_settings = settings.model_copy(
                update={
                    "moodle_auth_mode": "cookie",
                    "moodle_token": None,
                    "moodle_username": None,
                    "moodle_password": None,
                    "moodle_session_cookie": SecretStr(cookie),
                }
            )
            client = AutoMoodleClient(user_settings)
            try:
                snapshot = await client.fetch_snapshot()
            finally:
                await client.close()

            now = datetime.now(timezone.utc)
            grades = {
                grade.id: [grade.grade_formatted, grade.feedback or ""]
                for grade in snapshot.grades
            }
            assignments = {
                item.id: [item.due_at.isoformat() if item.due_at else None, item.submitted]
                for item in snapshot.assignments
            }

            async with session_factory() as session:
                state = await session.get(
                    PersonalSyncStateModel, user_id, with_for_update=True
                )
                if state is None:
                    session.add(
                        PersonalSyncStateModel(
                            telegram_user_id=user_id,
                            grades_json=json.dumps(grades),
                            assignments_json=json.dumps(assignments),
                            reminders_json="[]",
                            pending_messages_json="[]",
                            last_checked_at=now,
                        )
                    )
                    await session.commit()
                    result["updated"] += 1
                    continue

                previous_grades = json.loads(state.grades_json)
                previous_assignments = json.loads(state.assignments_json)
                reminded = set(json.loads(state.reminders_json))
                messages: list[str] = []

                for grade in snapshot.grades:
                    old = previous_grades.get(grade.id)
                    if old is None:
                        messages.append(format_grade_notification(grade))
                    elif old != grades[grade.id]:
                        messages.append(
                            format_grade_notification(
                                grade, is_update=True, old_grade=old[0]
                            )
                        )

                for item in snapshot.assignments:
                    old = previous_assignments.get(item.id)
                    if old is None:
                        messages.append(format_new_assignment(item, settings.tz_info, now))
                    elif old[0] != assignments[item.id][0]:
                        old_due = datetime.fromisoformat(old[0]) if old[0] else None
                        messages.append(
                            format_deadline_changed(item, old_due, settings.tz_info, now)
                        )

                    if item.due_at and not item.submitted:
                        due_at = item.due_at.astimezone(timezone.utc)
                        reminder_key = f"{item.id}:{due_at.isoformat()}"
                        remaining_minutes = (due_at - now).total_seconds() / 60
                        thresholds = settings.deadline_reminder_minutes
                        matching = [threshold for threshold in thresholds if 0 < remaining_minutes <= threshold]
                        if matching and reminder_key not in reminded:
                            messages.append(format_deadline_reminder(item, min(matching), settings.tz_info, now))
                            reminded.add(reminder_key)

                pending = [chunk for message in messages for chunk in split_telegram_message(message)]
                state.grades_json = json.dumps(grades)
                state.assignments_json = json.dumps(assignments)
                state.reminders_json = json.dumps(sorted(reminded))
                state.pending_messages_json = json.dumps(pending)
                state.last_checked_at = now
                await session.commit()

            while pending:
                await bot.send_message(chat_id=user_id, text=pending[0])
                pending.pop(0)
                async with session_factory() as session:
                    state = await session.get(PersonalSyncStateModel, user_id)
                    if state is not None:
                        state.pending_messages_json = json.dumps(pending)
                        await session.commit()
                result["sent"] += 1
            async with session_factory() as session:
                state = await session.get(PersonalSyncStateModel, user_id)
                if state is not None:
                    state.last_checked_at = now
                    await session.commit()
                result["updated"] += 1
        except (LMSError, UserSessionError) as exc:
            result["failed"] += 1
            async with session_factory() as session:
                state = await session.get(PersonalSyncStateModel, user_id)
                if state is not None:
                    state.last_checked_at = datetime.now(timezone.utc)
                    await session.commit()
            logger.warning(
                "Personal Moodle sync failed for user %s (%s)",
                user_id,
                type(exc).__name__,
            )
        except Exception:
            result["failed"] += 1
            async with session_factory() as session:
                state = await session.get(PersonalSyncStateModel, user_id)
                if state is not None:
                    state.last_checked_at = datetime.now(timezone.utc)
                    await session.commit()
            logger.exception("Unexpected personal sync failure for user %s", user_id)

    return result
