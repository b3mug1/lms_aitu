from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, ReplyKeyboardMarkup, Update, User

from app.bot.handlers import MAIN_MENU, create_bot_router
from app.bot.sender import TelegramNotifier
from app.core.config import Settings
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

    async def stream_content(self, url: str, headers=None, timeout: int = 30, chunk_size: int = 65536, raise_for_status: bool = True):  # type: ignore[override]
        if False:
            yield b""

    async def close(self) -> None:
        return None


class StaticLMS:
    mode_name = "cookie"

    def __init__(self, snapshot: LMSSnapshot) -> None:
        self._snapshot = snapshot
        self.updated_cookies: list[str] = []

    def update_session_cookie(self, cookie_value: str) -> None:
        self.updated_cookies.append(cookie_value)

    async def fetch_snapshot(self) -> LMSSnapshot:
        return self._snapshot

    async def close(self) -> None:
        return None


def _make_command_update(update_id: int, chat_id: int, text: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="Student"),
            text=text,
        ),
    )


def _make_callback_update(update_id: int, chat_id: int, data: str) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"cb_{update_id}",
            from_user=User(id=chat_id, is_bot=False, first_name="Student"),
            chat_instance="inst",
            data=data,
            message=Message(
                message_id=50,
                date=datetime.now(timezone.utc),
                chat=Chat(id=chat_id, type="private"),
                text="Выберите предмет",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_bot_commands_and_chat_authorization(
    sample_settings: Settings,
    db_session_factory,
) -> None:
    now = datetime.now(timezone.utc)
    snap = LMSSnapshot(
        fetched_at=now,
        mode="rest",
        user_fullname="Test Student",
        courses=[CourseDTO(id=1, name="Algorithms", url="https://lms.example.edu/course/view.php?id=1")],
        assignments=[
            AssignmentDTO(
                id="assign:10",
                course_id=1,
                course_name="Algorithms",
                title="Homework 1",
                due_at=now + timedelta(days=4),
                url="https://lms.example.edu/mod/assign/view.php?id=10",
            )
        ],
        grades=[
            GradeDTO(
                id="1:50",
                course_id=1,
                course_name="Algorithms",
                item_name="Quiz 1",
                grade_raw=95.0,
                grade_formatted="95",
                grade_max=100.0,
            )
        ],
    )

    tg_session = RecordingTelegramSession()
    bot = Bot(
        token=sample_settings.telegram_bot_token.get_secret_value(),
        session=tg_session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    notifier = TelegramNotifier(bot, sample_settings.telegram_chat_id)
    lms_stub = StaticLMS(snap)
    sync_service = SyncService(
        sample_settings,
        lms_stub,
        db_session_factory,
        notifier,
    )
    dp = Dispatcher()
    dp.include_router(create_bot_router(sample_settings, db_session_factory, sync_service))

    # 1. Unauthorized chat is rejected without leaking LMS data
    await dp.feed_update(bot, _make_command_update(1, 111111, "/start"))
    assert len(tg_session.sent_texts) == 1
    assert "Доступ ограничен" in tg_session.sent_texts[0][1]
    assert "111111" in tg_session.sent_texts[0][1]

    # The main actions are visible immediately and work as text buttons.
    tg_session.sent_texts.clear()
    tg_session.sent_markups.clear()
    await dp.feed_update(bot, _make_command_update(2, sample_settings.telegram_chat_id, "/start"))
    assert "Предметы" in tg_session.sent_texts[0][1]
    assert isinstance(tg_session.sent_markups[0], ReplyKeyboardMarkup)
    assert tg_session.sent_markups[0] == MAIN_MENU

    # 2. Authorized user runs /check -> bootstrap + deadline reminder + summary
    tg_session.sent_texts.clear()
    await dp.feed_update(bot, _make_command_update(3, sample_settings.telegram_chat_id, "/check"))
    all_check_msgs = "\n".join(text for _, text in tg_session.sent_texts)
    assert "Moodle подключён" in all_check_msgs
    assert "Homework 1" in all_check_msgs

    # 3. /grades and /courses show inline course picker first
    for cmd in ("/grades", "/courses", "Предметы"):
        tg_session.sent_texts.clear()
        tg_session.sent_markups.clear()
        await dp.feed_update(bot, _make_command_update(10, sample_settings.telegram_chat_id, cmd))
        assert len(tg_session.sent_texts) == 1
        assert "Выберите предмет" in tg_session.sent_texts[0][1]
        markup = tg_session.sent_markups[0]
        assert markup is not None
        assert markup.inline_keyboard[0][0].callback_data == "course:1"
        assert "Algorithms" in markup.inline_keyboard[0][0].text

    # 4. Selecting a course via inline button shows BOTH grades and assignments + back button
    tg_session.edited_texts.clear()
    tg_session.edited_markups.clear()
    await dp.feed_update(
        bot,
        _make_callback_update(11, sample_settings.telegram_chat_id, "course:1"),
    )
    assert len(tg_session.edited_texts) == 1
    course_card = tg_session.edited_texts[0]
    assert "Algorithms" in course_card
    assert "Quiz 1" in course_card
    assert "95 / 100" in course_card
    assert "Homework 1" in course_card
    assert tg_session.edited_markups[0].inline_keyboard[0][0].callback_data == "courses:list"

    # 5. Back button returns to course list
    tg_session.edited_texts.clear()
    await dp.feed_update(
        bot,
        _make_callback_update(12, sample_settings.telegram_chat_id, "courses:list"),
    )
    assert "Выберите предмет" in tg_session.edited_texts[0]

    # 6. /deadlines, /grades recent, /status, /scholarship
    for cmd, expected in [
        ("/deadlines", "Homework 1"),
        ("/grades recent", "Quiz 1"),
        ("/status", "Работает штатно"),
        ("Сроки сдачи", "Homework 1"),
        ("Последние оценки", "Quiz 1"),
        ("Состояние", "Работает штатно"),
        ("/scholarship", "Стипендия по предметам"),
        ("Стипендия", "Стипендия по предметам"),
        ("/scholarship 75 80", "Стипендия · ручной расчёт"),
        ("/calc 80 85", "51.2"),
    ]:
        tg_session.sent_texts.clear()
        await dp.feed_update(bot, _make_command_update(13, sample_settings.telegram_chat_id, cmd))
        assert len(tg_session.sent_texts) == 1
        assert expected in tg_session.sent_texts[0][1]

    tg_session.sent_texts.clear()
    await dp.feed_update(bot, _make_command_update(15, sample_settings.telegram_chat_id, "Что-то другое"))
    assert "Выберите действие в меню" in tg_session.sent_texts[0][1]
    assert tg_session.sent_markups[-1] == MAIN_MENU

    # 7. /setcookie updates cookie and persists in sync_state
    tg_session.sent_texts.clear()
    await dp.feed_update(
        bot,
        _make_command_update(14, sample_settings.telegram_chat_id, "/setcookie MoodleSession=fresh_cookie_123"),
    )
    assert lms_stub.updated_cookies == ["fresh_cookie_123"]
    assert tg_session.deleted_message_ids == [14]
    assert any("Сессия Moodle обновлена" in text for _, text in tg_session.sent_texts)

    await bot.session.close()
