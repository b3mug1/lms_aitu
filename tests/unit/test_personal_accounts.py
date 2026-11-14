from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Chat, Message, Update, User
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.bot.handlers import create_bot_router
from app.bot.sender import TelegramNotifier
from app.core.config import Settings
from app.core.user_sessions import (
    load_user_cookie,
    user_account_exists,
)
from app.lms.base import AssignmentDTO, CourseDTO, GradeDTO, LMSSnapshot
from app.notifications.service import SyncService


class RecordingTelegramSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent_texts: list[tuple[int | str, str]] = []
        self.sent_markups: list[Any] = []
        self.edited_texts: list[str] = []
        self.edited_markups: list[Any] = []
        self.callback_answers: list[str | None] = []
        self.deleted_message_ids: list[int] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        if isinstance(method, SendMessage):
            self.sent_texts.append((method.chat_id, method.text))
            self.sent_markups.append(method.reply_markup)
            return Message(
                message_id=len(self.sent_texts),
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, EditMessageText):
            self.edited_texts.append(method.text)
            self.edited_markups.append(method.reply_markup)
            return Message(
                message_id=method.message_id or 1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id or 0), type="private"),
                text=method.text,
            )
        if isinstance(method, AnswerCallbackQuery):
            self.callback_answers.append(method.text)
            return True
        if isinstance(method, DeleteMessage):
            self.deleted_message_ids.append(method.message_id)
            return True
        return True

    async def stream_content(self, url: str, headers=None, timeout: int = 30, chunk_size: int = 65536, raise_for_status: bool = True):
        if False:
            yield b""

    async def close(self) -> None:
        return None


def _make_private_msg_update(update_id: int, user_id: int, text: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name=f"User{user_id}"),
            text=text,
        ),
    )


def _make_group_msg_update(update_id: int, user_id: int, text: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=-100123456789, type="group", title="Study Group"),
            from_user=User(id=user_id, is_bot=False, first_name=f"User{user_id}"),
            text=text,
        ),
    )


@pytest.mark.asyncio
async def test_personal_accounts_isolation_and_workflow(
    sample_settings: Settings,
    db_session_factory,
) -> None:
    # 1. Setup settings with a stable Fernet key
    test_key = Fernet.generate_key().decode()
    settings = sample_settings.model_copy(
        update={"moodle_cookie_encryption_key": SecretStr(test_key)}
    )

    tg_session = RecordingTelegramSession()
    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        session=tg_session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    notifier = TelegramNotifier(bot, settings.telegram_chat_id)
    sync_service = SyncService(
        settings,
        AsyncMock(),
        db_session_factory,
        notifier,
    )
    dp = Dispatcher()
    dp.include_router(create_bot_router(settings, db_session_factory, sync_service))

    user_a_id = 111001
    user_b_id = 222002

    # 2. Check /start for unauthenticated user in private chat
    await dp.feed_update(bot, _make_private_msg_update(1, user_a_id, "/start"))
    assert len(tg_session.sent_texts) == 1
    assert "/connect MoodleSession=" in tg_session.sent_texts[0][1]

    # 3. Reject /connect in group chats
    tg_session.sent_texts.clear()
    await dp.feed_update(bot, _make_group_msg_update(2, user_a_id, "/connect MoodleSession=secret_cookie_a"))
    assert len(tg_session.sent_texts) == 1
    assert "личном чате" in tg_session.sent_texts[0][1]

    # Setup distinct snapshots for User A and User B
    now = datetime.now(timezone.utc)
    snap_a = LMSSnapshot(
        fetched_at=now,
        mode="cookie",
        user_fullname="Alice Student",
        courses=[CourseDTO(id=101, name="Data Structures", url="https://lms.example.edu/course/101")],
        assignments=[
            AssignmentDTO(
                id="assign:101",
                course_id=101,
                course_name="Data Structures",
                title="Trees Lab",
                due_at=now + timedelta(days=2),
                url="https://lms.example.edu/assign/101",
            )
        ],
        grades=[
            GradeDTO(
                id="101:1",
                course_id=101,
                course_name="Data Structures",
                item_name="Trees Lab Grade",
                grade_raw=90.0,
                grade_formatted="90",
                grade_max=100.0,
            )
        ],
    )

    snap_b = LMSSnapshot(
        fetched_at=now,
        mode="cookie",
        user_fullname="Bob Scholar",
        courses=[CourseDTO(id=202, name="Cybersecurity", url="https://lms.example.edu/course/202")],
        assignments=[
            AssignmentDTO(
                id="assign:202",
                course_id=202,
                course_name="Cybersecurity",
                title="PenTest Report",
                due_at=now + timedelta(days=5),
                url="https://lms.example.edu/assign/202",
            )
        ],
        grades=[
            GradeDTO(
                id="202:1",
                course_id=202,
                course_name="Cybersecurity",
                item_name="PenTest Grade",
                grade_raw=85.0,
                grade_formatted="85",
                grade_max=100.0,
            )
        ],
    )

    async def mock_fetch_snapshot(self):
        cookie = self._settings.moodle_session_cookie.get_secret_value()
        if cookie == "cookie_user_a":
            return snap_a
        elif cookie == "cookie_user_b":
            return snap_b
        raise ValueError(f"Unknown cookie: {cookie}")

    with patch("app.lms.client.AutoMoodleClient.fetch_snapshot", mock_fetch_snapshot):
        # 4. User A connects their account
        tg_session.sent_texts.clear()
        tg_session.deleted_message_ids.clear()
        await dp.feed_update(bot, _make_private_msg_update(10, user_a_id, "/connect MoodleSession=cookie_user_a"))
        assert 10 in tg_session.deleted_message_ids  # cookie message deleted from chat
        assert any("Alice Student" in txt for _, txt in tg_session.sent_texts)
        assert any("Data Structures" in txt for _, txt in tg_session.sent_texts)

        # 5. User B connects their account
        tg_session.sent_texts.clear()
        tg_session.deleted_message_ids.clear()
        await dp.feed_update(bot, _make_private_msg_update(20, user_b_id, "/connect MoodleSession=cookie_user_b"))
        assert 20 in tg_session.deleted_message_ids  # cookie message deleted from chat
        assert any("Bob Scholar" in txt for _, txt in tg_session.sent_texts)
        assert any("Cybersecurity" in txt for _, txt in tg_session.sent_texts)

        # 6. Verify data isolation for User A
        tg_session.sent_texts.clear()
        tg_session.sent_markups.clear()
        await dp.feed_update(bot, _make_private_msg_update(30, user_a_id, "/courses"))
        a_markup = tg_session.sent_markups[0]
        a_buttons = [btn.text for row in a_markup.inline_keyboard for btn in row]
        assert any("Data Structures" in b for b in a_buttons)
        assert not any("Cybersecurity" in b for b in a_buttons)

        tg_session.sent_texts.clear()
        await dp.feed_update(bot, _make_private_msg_update(31, user_a_id, "/deadlines"))
        a_deadlines_text = "\n".join(txt for _, txt in tg_session.sent_texts)
        assert "Trees Lab" in a_deadlines_text
        assert "PenTest" not in a_deadlines_text

        tg_session.sent_texts.clear()
        await dp.feed_update(bot, _make_private_msg_update(32, user_a_id, "/grades recent"))
        a_grades_text = "\n".join(txt for _, txt in tg_session.sent_texts)
        assert "Trees Lab Grade" in a_grades_text
        assert "PenTest Grade" not in a_grades_text

        # 7. Verify data isolation for User B
        tg_session.sent_texts.clear()
        tg_session.sent_markups.clear()
        await dp.feed_update(bot, _make_private_msg_update(40, user_b_id, "/courses"))
        b_markup = tg_session.sent_markups[0]
        b_buttons = [btn.text for row in b_markup.inline_keyboard for btn in row]
        assert any("Cybersecurity" in b for b in b_buttons)
        assert not any("Data Structures" in b for b in b_buttons)

        tg_session.sent_texts.clear()
        await dp.feed_update(bot, _make_private_msg_update(41, user_b_id, "/deadlines"))
        b_deadlines_text = "\n".join(txt for _, txt in tg_session.sent_texts)
        assert "PenTest Report" in b_deadlines_text
        assert "Trees Lab" not in b_deadlines_text

        tg_session.sent_texts.clear()
        await dp.feed_update(bot, _make_private_msg_update(42, user_b_id, "/grades recent"))
        b_grades_text = "\n".join(txt for _, txt in tg_session.sent_texts)
        assert "PenTest Grade" in b_grades_text
        assert "Trees Lab Grade" not in b_grades_text

        # 8. Verify encrypted storage at rest: raw cookies are NOT stored in plain text
        cookie_a = await load_user_cookie(settings, db_session_factory, user_a_id)
        assert cookie_a == "cookie_user_a"
        cookie_b = await load_user_cookie(settings, db_session_factory, user_b_id)
        assert cookie_b == "cookie_user_b"

        # Check raw DB content does not contain plain cookie
        async with db_session_factory() as s:
            from sqlalchemy import select
            from app.storage.models import MoodleUserAccountModel
            rows = (await s.execute(select(MoodleUserAccountModel))).scalars().all()
            for r in rows:
                assert "cookie_user_a" not in r.encrypted_cookie
                assert "cookie_user_b" not in r.encrypted_cookie

        # 9. Verify /disconnect for User A removes only User A's account
        tg_session.sent_texts.clear()
        await dp.feed_update(bot, _make_private_msg_update(50, user_a_id, "/disconnect"))
        assert any("удалена" in txt for _, txt in tg_session.sent_texts)
        assert not await user_account_exists(db_session_factory, user_a_id)
        assert await user_account_exists(db_session_factory, user_b_id)

    await bot.session.close()
