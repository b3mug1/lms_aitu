from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core.user_sessions import save_user_cookie
from app.lms.base import CourseDTO, GradeDTO, LMSSnapshot
from app.notifications.personal import sync_personal_accounts


@pytest.mark.asyncio
async def test_notifications_are_scoped_and_deduplicated(sample_settings, db_session_factory):
    settings = sample_settings.model_copy(
        update={"moodle_cookie_encryption_key": SecretStr(Fernet.generate_key().decode())}
    )
    await save_user_cookie(settings, db_session_factory, 101, "cookie_a")
    await save_user_cookie(settings, db_session_factory, 202, "cookie_b")
    scores = {"cookie_a": "70", "cookie_b": "80"}

    async def fetch_snapshot(client):
        cookie = client._settings.moodle_session_cookie.get_secret_value()
        course_id = 1 if cookie == "cookie_a" else 2
        return LMSSnapshot(
            fetched_at=datetime.now(timezone.utc),
            mode="cookie",
            courses=[CourseDTO(id=course_id, name=f"Course {course_id}")],
            grades=[
                GradeDTO(
                    id=f"{course_id}:grade",
                    course_id=course_id,
                    course_name=f"Course {course_id}",
                    item_name="Exam",
                    grade_formatted=scores[cookie],
                )
            ],
        )

    bot = AsyncMock()
    with patch("app.lms.client.AutoMoodleClient.fetch_snapshot", fetch_snapshot):
        baseline = await sync_personal_accounts(settings, db_session_factory, bot)
        assert baseline["accounts"] == 2
        assert baseline["sent"] == 0

        scores["cookie_a"] = "90"
        changed = await sync_personal_accounts(settings, db_session_factory, bot)
        assert changed["sent"] == 1
        assert bot.send_message.call_args.kwargs["chat_id"] == 101

        again = await sync_personal_accounts(settings, db_session_factory, bot)
        assert again["sent"] == 0
        assert bot.send_message.call_count == 1
