from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.notifications.formatter import (
    format_course_details,
    format_grades_list,
    format_deadlines_list,
    format_sync_error,
    format_sync_status,
    split_telegram_message,
)
from app.notifications.service import SyncService
from app.storage.models import CourseModel
from app.storage.repository import LMSRepository

BOT_COMMANDS = [
    BotCommand(command="deadlines", description="Ближайшие сроки сдачи"),
    BotCommand(command="courses", description="Предметы, задания и оценки"),
    BotCommand(command="grades", description="Оценки по предметам"),
    BotCommand(command="check", description="Проверить Moodle сейчас"),
    BotCommand(command="status", description="Состояние подключения"),
    BotCommand(command="help", description="Как пользоваться ботом"),
    BotCommand(command="setcookie", description="Обновить MoodleSession cookie (SSO)"),
]

MENU_DEADLINES = "Сроки сдачи"
MENU_COURSES = "Предметы"
MENU_GRADES = "Последние оценки"
MENU_CHECK = "Проверить Moodle"
MENU_STATUS = "Состояние"
MENU_HELP = "Помощь"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=MENU_DEADLINES), KeyboardButton(text=MENU_COURSES)],
        [KeyboardButton(text=MENU_GRADES), KeyboardButton(text=MENU_CHECK)],
        [KeyboardButton(text=MENU_STATUS), KeyboardButton(text=MENU_HELP)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие",
)


async def _reply_chunked(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    chunks = split_telegram_message(text)
    for idx, chunk in enumerate(chunks):
        markup = reply_markup if idx == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=markup)


def _build_courses_keyboard(courses: list[CourseModel]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=course.name[:60],
                callback_data=f"course:{course.id}",
            )
        ]
        for course in courses
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_course_back_keyboard(course_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="К предметам", callback_data="courses:list"),
                InlineKeyboardButton(text="Обновить", callback_data=f"course:{course_id}"),
            ]
        ]
    )


def create_bot_router(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    sync_service: SyncService,
) -> Router:
    """Create aiogram Router restricted to the configured Telegram chat."""
    router = Router(name="moodle_notifier")

    async def _is_authorized_message(message: Message) -> bool:
        if message.chat.id == settings.telegram_chat_id:
            return True
        await message.answer(
            "Доступ ограничен.\n"
            f"Ваш <code>chat_id</code>: <code>{message.chat.id}</code>\n"
            "Укажите его в <code>TELEGRAM_CHAT_ID</code> в файле <code>.env</code>, "
            "если хотите получать уведомления сюда."
        )
        return False

    async def _is_authorized_callback(callback: CallbackQuery) -> bool:
        msg = callback.message
        if msg is not None and msg.chat.id == settings.telegram_chat_id:
            return True
        await callback.answer("Доступ ограничен.", show_alert=True)
        return False

    @router.message(Command("start", "help"))
    @router.message(F.text == MENU_HELP)
    async def cmd_help(message: Message) -> None:
        if not await _is_authorized_message(message):
            return
        text = (
            "<b>Ваш Moodle</b>\n"
            "Здесь можно посмотреть сроки сдачи, предметы и оценки. "
            "Бот сам сообщит о новых заданиях, оценках и изменениях сроков.\n\n"
            "<b>Что выбрать в меню:</b>\n"
            "Сроки сдачи — ближайшие несданные задания.\n"
            "Предметы — задания и оценки по каждому курсу.\n"
            "Последние оценки — новые результаты по всем курсам.\n"
            "Проверить Moodle — получить свежие данные сейчас.\n"
            "Состояние — узнать, когда была последняя проверка.\n\n"
            "Дополнительно: <code>/deadlines all</code> показывает и сданные работы; "
            "<code>/setcookie значение</code> обновляет сессию Moodle при SSO-входе."
        )
        await message.answer(text, reply_markup=MAIN_MENU)

    @router.message(Command("check"))
    @router.message(F.text == MENU_CHECK)
    async def cmd_check(message: Message) -> None:
        if not await _is_authorized_message(message):
            return
        await message.answer("Проверяю Moodle...")
        report = await sync_service.sync_once(trigger="manual")
        if not report.ok:
            await message.answer(
                f"Не удалось проверить Moodle.\n{format_sync_error(report.error)}"
            )
            return

        if report.skipped:
            await message.answer("Проверка Moodle уже выполняется. Попробуйте позже.")
        elif report.notifications_sent == 0:
            await message.answer("Проверка завершена. Новых изменений нет.")
        elif report.is_bootstrap:
            await message.answer("Первая проверка завершена. Теперь можно открыть сроки сдачи и предметы в меню.")
        else:
            await message.answer(
                "Проверка завершена.\n"
                f"Отправлено уведомлений: <b>{report.notifications_sent}</b> "
                f"(оценки: {report.new_grades + report.updated_grades}, "
                f"задания: {report.new_assignments}, "
                f"дедлайны: {report.deadline_reminders + report.deadline_changes})."
            )

    @router.message(Command("deadlines"))
    async def cmd_deadlines(message: Message, command: CommandObject) -> None:
        if not await _is_authorized_message(message):
            return
        include_submitted = bool(command.args and command.args.strip().lower() == "all")
        await _show_deadlines(message, include_submitted)

    @router.message(F.text == MENU_DEADLINES)
    async def menu_deadlines(message: Message) -> None:
        if not await _is_authorized_message(message):
            return
        await _show_deadlines(message, False)

    async def _show_deadlines(message: Message, include_submitted: bool) -> None:
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            repo = LMSRepository(session)
            items = await repo.list_upcoming_deadlines(
                now=now,
                lookahead_days=settings.lookahead_days,
                include_submitted=include_submitted,
            )
        await _reply_chunked(
            message,
            format_deadlines_list(items, settings.tz_info, now, include_submitted),
        )

    @router.message(Command("grades", "courses"))
    async def cmd_grades_or_courses(message: Message, command: CommandObject) -> None:
        if not await _is_authorized_message(message):
            return
        if command.args and command.args.strip().lower() == "recent":
            await _show_recent_grades(message)
            return
        await _show_courses(message)

    @router.message(F.text == MENU_GRADES)
    async def menu_grades(message: Message) -> None:
        if not await _is_authorized_message(message):
            return
        await _show_recent_grades(message)

    async def _show_recent_grades(message: Message) -> None:
        async with session_factory() as session:
            repo = LMSRepository(session)
            items = await repo.list_recent_grades(limit=20)
        await _reply_chunked(message, format_grades_list(items, settings.tz_info))

    @router.message(F.text == MENU_COURSES)
    async def menu_courses(message: Message) -> None:
        if not await _is_authorized_message(message):
            return
        await _show_courses(message)

    async def _show_courses(message: Message) -> None:
        async with session_factory() as session:
            repo = LMSRepository(session)
            courses = await repo.list_courses()

        if not courses:
            await message.answer("Список предметов пока пуст. Нажмите «Проверить Moodle».")
            return

        await message.answer(
            "<b>Предметы</b>\nВыберите предмет, чтобы посмотреть его задания и оценки:",
            reply_markup=_build_courses_keyboard(courses),
        )

    @router.callback_query(F.data == "courses:list")
    async def cb_courses_list(callback: CallbackQuery) -> None:
        if not await _is_authorized_callback(callback):
            return
        async with session_factory() as session:
            repo = LMSRepository(session)
            courses = await repo.list_courses()

        msg = callback.message
        if isinstance(msg, Message):
            if not courses:
                await msg.edit_text("Список предметов пока пуст. Нажмите «Проверить Moodle».")
            else:
                try:
                    await msg.edit_text(
                        "<b>Предметы</b>\nВыберите предмет, чтобы посмотреть его задания и оценки:",
                        reply_markup=_build_courses_keyboard(courses),
                    )
                except TelegramBadRequest:
                    pass
        await callback.answer()

    @router.callback_query(F.data.startswith("course:"))
    async def cb_course_detail(callback: CallbackQuery) -> None:
        if not await _is_authorized_callback(callback):
            return
        raw_id = (callback.data or "").split(":", 1)[1]
        try:
            course_id = int(raw_id)
        except ValueError:
            await callback.answer("Некорректный ID курса", show_alert=True)
            return

        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            repo = LMSRepository(session)
            course = await repo.get_course(course_id)
            if course is None:
                await callback.answer("Курс не найден. Запустите /check.", show_alert=True)
                return
            assignments = await repo.list_course_assignments(course_id)
            grades = await repo.list_course_grades(course_id)

        text = format_course_details(course, assignments, grades, settings.tz_info, now)
        back_markup = _build_course_back_keyboard(course_id)

        msg = callback.message
        if isinstance(msg, Message):
            chunks = split_telegram_message(text)
            try:
                if len(chunks) == 1:
                    await msg.edit_text(chunks[0], reply_markup=back_markup)
                else:
                    await msg.edit_text(chunks[0])
                    for idx, chunk in enumerate(chunks[1:], start=1):
                        markup = back_markup if idx == len(chunks) - 1 else None
                        await msg.answer(chunk, reply_markup=markup)
            except TelegramBadRequest:
                pass
        await callback.answer()

    @router.message(Command("setcookie"))
    async def cmd_setcookie(message: Message, command: CommandObject) -> None:
        if not await _is_authorized_message(message):
            return
        cookie_val = (command.args or "").strip()
        if not cookie_val:
            await message.answer(
                "Войдите в Moodle через браузер. Найдите cookie MoodleSession "
                "в инструментах разработчика браузера (Application → Cookies). "
                "Затем отправьте в личном чате:\n"
                "<code>/setcookie значение_MoodleSession</code>"
            )
            return
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        await message.answer("Обновляю сессию и проверяю подключение к Moodle...")
        report = await sync_service.update_session_cookie(cookie_val)
        if report.skipped:
            await message.answer("Проверка Moodle уже выполняется. Повторите команду позже.")
            return
        if not report.ok:
            await message.answer(
                "Не удалось обновить сессию. Новая cookie не сохранена.\n"
                f"{format_sync_error(report.error)}"
            )
            return
        await message.answer("Сессия Moodle обновлена. Проверка прошла успешно.")

    @router.message(Command("status"))
    @router.message(F.text == MENU_STATUS)
    async def cmd_status(message: Message) -> None:
        if not await _is_authorized_message(message):
            return
        async with session_factory() as session:
            repo = LMSRepository(session)
            state = await repo.get_or_create_sync_state()
        await message.answer(format_sync_status(state, settings.tz_info))

    @router.message(F.text)
    async def unknown_text(message: Message) -> None:
        if not await _is_authorized_message(message):
            return
        await message.answer(
            "Выберите действие в меню ниже. Кнопка «Помощь» объясняет, где что найти.",
            reply_markup=MAIN_MENU,
        )

    return router
