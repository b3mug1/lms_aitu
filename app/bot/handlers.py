from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import re

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
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.connect_tokens import create_connect_token
from app.core.keep_alive import reset_user_expired_notification
from app.core.user_sessions import (
    UserSessionError,
    delete_user_account,
    load_user_cookie,
    save_user_cookie,
    user_account_exists,
)
from app.lms.base import (
    CourseDTO,
    LMSAuthError,
    LMSConnectionError,
    LMSError,
    LMSSnapshot,
)
from app.lms.client import AutoMoodleClient
from app.notifications.formatter import (
    format_course_details,
    format_grades_list,
    format_deadlines_list,
    format_sync_error,
    format_sync_status,
    split_telegram_message,
)
from app.notifications.service import SyncService
from app.scholarship import (
    analyze_course_scholarship,
    analyze_scholarship,
    format_courses_scholarship_summary,
    format_manual_scholarship,
)
from app.storage.models import CourseModel
from app.storage.repository import LMSRepository

BOT_COMMANDS = [
    BotCommand(command="start", description="Открыть главное меню"),
    BotCommand(command="connect", description="Подключить или обновить Moodle"),
    BotCommand(command="check", description="Обновить данные Moodle"),
    BotCommand(command="courses", description="Предметы и задания"),
    BotCommand(command="grades", description="Последние оценки"),
    BotCommand(command="deadlines", description="Ближайшие сроки сдачи"),
    BotCommand(command="scholarship", description="Расчёт стипендии"),
    BotCommand(command="status", description="Состояние Moodle"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="disconnect", description="Отключить Moodle"),
]

MENU_DEADLINES = "Сроки сдачи"
MENU_COURSES = "Предметы"
MENU_GRADES = "Последние оценки"
MENU_SCHOLARSHIP = "Стипендия"
MENU_CHECK = "Проверить Moodle"
MENU_STATUS = "Состояние"
MENU_HELP = "Помощь"
MENU_CONNECT = "Подключить Moodle"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=MENU_DEADLINES), KeyboardButton(text=MENU_COURSES)],
        [KeyboardButton(text=MENU_GRADES), KeyboardButton(text=MENU_SCHOLARSHIP)],
        [KeyboardButton(text=MENU_CHECK), KeyboardButton(text=MENU_STATUS)],
        [KeyboardButton(text=MENU_HELP)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие",
)

CONNECT_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=MENU_CONNECT)], [KeyboardButton(text=MENU_HELP)]],
    resize_keyboard=True,
    input_field_placeholder="Подключите свой Moodle",
)


async def _reply_chunked(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
) -> None:
    chunks = split_telegram_message(text)
    for idx, chunk in enumerate(chunks):
        markup = reply_markup if idx == len(chunks) - 1 else None
        try:
            await message.answer(chunk, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "can't parse entities" in str(exc).lower():
                await message.answer(chunk, reply_markup=markup, parse_mode=None)
            else:
                raise


def _build_courses_keyboard(courses: list[CourseDTO | CourseModel]) -> InlineKeyboardMarkup:
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
                InlineKeyboardButton(text="Все предметы", callback_data="courses:list"),
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

    async def _fetch_snapshot(cookie: str) -> LMSSnapshot:
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
            return await client.fetch_snapshot()
        finally:
            await client.close()

    async def _personal_snapshot(message: Message) -> tuple[bool, LMSSnapshot | None]:
        """Load only the calling private user's Moodle account and live data."""
        is_owner = (
            message.chat.id == settings.telegram_chat_id
            or (message.from_user and message.from_user.id == settings.telegram_chat_id)
        )
        if is_owner:
            return False, None
        if message.chat.type != "private" or message.from_user is None:
            await message.answer("Используйте бота в личном чате.")
            return True, None
        if settings.moodle_cookie_encryption_key is None:
            await message.answer("Личные Moodle-аккаунты пока не настроены администратором бота.")
            return True, None
        try:
            cookie = await load_user_cookie(settings, session_factory, message.from_user.id)
            if cookie is None:
                await message.answer(
                    "Moodle ещё не подключён. Нажмите «Подключить Moodle».",
                    reply_markup=CONNECT_MENU,
                )
                return True, None
            return True, await _fetch_snapshot(cookie)
        except LMSAuthError:
            await message.answer(
                "Сессия Moodle истекла. Обновите её командой /connect (в 1 клик через расширение или вручную через /connect MoodleSession=...)."
            )
        except UserSessionError:
            await message.answer(
                "Не удалось расшифровать сохранённую сессию. Отправьте cookie повторно через /connect."
            )
        except LMSError:
            await message.answer("Не удалось получить данные Moodle. Проверьте сессию и попробуйте позже.")
        except Exception:
            await message.answer("Не удалось получить данные Moodle. Попробуйте позже.")
        return True, None

    async def _personal_callback_snapshot(
        callback: CallbackQuery,
    ) -> tuple[bool, LMSSnapshot | None]:
        msg = callback.message
        if msg is None:
            return False, None
        if msg.chat.type != "private":
            try:
                await callback.answer("Откройте бота в личном чате.", show_alert=True)
            except TelegramBadRequest:
                pass
            return True, None
        is_owner = (
            callback.from_user.id == settings.telegram_chat_id
            or (msg.chat and msg.chat.id == settings.telegram_chat_id)
        )
        if is_owner:
            return False, None
        if settings.moodle_cookie_encryption_key is None:
            try:
                await callback.answer("Личные аккаунты не настроены.", show_alert=True)
            except TelegramBadRequest:
                pass
            return True, None
        try:
            cookie = await load_user_cookie(settings, session_factory, callback.from_user.id)
            if cookie is None:
                try:
                    await callback.answer("Сначала подключите Moodle через /connect.", show_alert=True)
                except TelegramBadRequest:
                    pass
                return True, None
            return True, await _fetch_snapshot(cookie)
        except LMSAuthError:
            try:
                await callback.answer("Сессия истекла. Подключите новую через /connect.", show_alert=True)
            except TelegramBadRequest:
                pass
        except Exception:
            try:
                await callback.answer("Не удалось получить данные Moodle.", show_alert=True)
            except TelegramBadRequest:
                pass
        return True, None

    @router.message(Command("start", "help"))
    @router.message(F.text == MENU_HELP)
    async def cmd_help(message: Message) -> None:
        if message.chat.type == "private" and message.chat.id != settings.telegram_chat_id:
            if settings.moodle_cookie_encryption_key is None:
                if not await _is_authorized_message(message):
                    return
            connected = bool(
                message.from_user
                and await user_account_exists(session_factory, message.from_user.id)
            )
            if connected:
                await message.answer(
                    "<b>Ваш Moodle</b>\n"
                    "Предметы — задания и оценки по курсам.\n"
                    "Сроки сдачи — ближайшие работы.\n"
                    "Последние оценки — новые результаты.\n"
                    "Стипендия — нужный балл на финале.\n\n"
                    "Для обновления сессии: /connect. Для отключения: /disconnect.",
                    reply_markup=MAIN_MENU,
                )
                return
            await message.answer(
                "<b>Ваш Moodle в Telegram</b>\n"
                "Смотрите предметы, оценки и сроки сдачи в одном месте.\n\n"
                "Нажмите «Подключить Moodle», чтобы начать (понадобится расширение AITU Moodle Connect).\n"
                "Либо подключите вручную: <code>/connect MoodleSession=значение</code>.",
                reply_markup=CONNECT_MENU,
            )
            return
        if not await _is_authorized_message(message):
            return
        text = (
            "<b>Ваш Moodle</b>\n"
            "Предметы — задания и оценки по курсам.\n"
            "Сроки сдачи — ближайшие работы.\n"
            "Последние оценки — новые результаты.\n"
            "Стипендия — нужный балл на финале.\n\n"
            "«Проверить Moodle» обновит данные. «Состояние» покажет результат последней проверки.\n"
            "Ручной расчёт: <code>/scholarship 75 80</code>. "
            "Обновить сессию: /connect или /setcookie."
        )
        await message.answer(text, reply_markup=MAIN_MENU)

    @router.message(Command("connect"))
    @router.message(F.text == MENU_CONNECT)
    async def cmd_connect(message: Message, command: CommandObject | None = None) -> None:
        """Verify a user's private Moodle session and return only their profile and courses."""
        if message.chat.type != "private":
            await message.answer("Отправьте команду с cookie боту в личном чате, не в группе.")
            return

        cookie_value = (command.args or "").strip() if command else ""
        if not cookie_value:
            if message.from_user is None:
                await message.answer("Не удалось определить Telegram-пользователя.")
                return
            if settings.moodle_cookie_encryption_key is None:
                await message.answer("Подключение Moodle временно недоступно. Попробуйте позже.")
                return
            signing_key = (
                settings.moodle_cookie_encryption_key.get_secret_value()
                if settings.moodle_cookie_encryption_key
                else None
            )
            token, code = create_connect_token(message.from_user.id, signing_key)
            connect_url = f"{settings.moodle_base_url}/#tg_token={token}"
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Открыть Moodle",
                            url=connect_url,
                        )
                    ]
                ]
            )
            text = (
                "<b>Подключение Moodle</b>\n\n"
                "1. Войдите в Moodle в браузере с расширением AITU Moodle Connect.\n"
                "2. Нажмите «Открыть Moodle» ниже. Расширение подключит аккаунт к боту.\n\n"
                "Если Moodle уже открыт, введите в расширении код:\n"
                f"<code>{code}</code>\n"
                "<i>Код действует 30 минут.</i>\n\n"
                "Без расширения: <code>/connect MoodleSession=значение</code> "
                "в личном чате с ботом."
            )
            await message.answer(text, reply_markup=markup)
            return

        if cookie_value.startswith("MoodleSession="):
            cookie_value = cookie_value.partition("=")[2].strip()
        if len(cookie_value) > 512 or re.search(r"[;\s]", cookie_value):
            await message.answer(
                "Отправьте /connect без параметров, чтобы получить ссылку для автоматического подключения в 1 клик, "
                "либо передайте <code>/connect MoodleSession=значение</code>."
            )
            return
        if message.from_user is None:
            await message.answer("Не удалось определить Telegram-пользователя.")
            return
        secret_message_deleted = True
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            secret_message_deleted = False
        if settings.moodle_cookie_encryption_key is None:
            await message.answer(
                "Подключение личных аккаунтов не настроено. Администратору нужно задать "
                "MOODLE_COOKIE_ENCRYPTION_KEY."
            )
            return

        await message.answer("Проверяю сессию Moodle и загружаю ваши курсы...")
        try:
            snapshot = await _fetch_snapshot(cookie_value)
        except LMSAuthError:
            await message.answer(
                "Не удалось войти в Moodle: cookie неверна или уже истекла. "
                "Получите новую MoodleSession и попробуйте снова."
            )
            return
        except LMSConnectionError:
            await message.answer("Не удалось связаться с Moodle. Попробуйте позже.")
            return
        except LMSError:
            await message.answer("Moodle не смог подтвердить сессию. Проверьте cookie и попробуйте снова.")
            return
        except Exception:
            await message.answer("Не удалось завершить подключение к Moodle. Попробуйте позже.")
            return
        try:
            await save_user_cookie(
                settings, session_factory, message.from_user.id, cookie_value
            )
            reset_user_expired_notification(message.from_user.id)
        except UserSessionError:
            await message.answer("Не удалось безопасно сохранить сессию Moodle.")
            return

        lines = ["<b>Подключение к Moodle выполнено</b>"]
        if snapshot.user_fullname:
            lines.append(f"<b>Профиль:</b> {escape(snapshot.user_fullname)}")
        lines.append(f"<b>Курсы ({len(snapshot.courses)}):</b>")
        if snapshot.courses:
            lines.extend(
                f'• <a href="{escape(course.url, quote=True)}">{escape(course.name)}</a>'
                if course.url.startswith(f"{settings.moodle_base_url}/")
                else f"• {escape(course.name)}"
                for course in snapshot.courses
            )
        else:
            lines.append("Moodle не вернул список курсов для этой сессии.")
        lines.append("\nВыберите действие в меню ниже.")
        if not secret_message_deleted:
            lines.append("Не удалось удалить исходное сообщение с cookie; удалите его вручную.")
        await _reply_chunked(message, "\n".join(lines), reply_markup=MAIN_MENU)

    @router.message(Command("disconnect"))
    async def cmd_disconnect(message: Message) -> None:
        if message.chat.type != "private" or message.from_user is None:
            await message.answer("Отключить личный аккаунт можно только в личном чате с ботом.")
            return
        removed = await delete_user_account(session_factory, message.from_user.id)
        await message.answer(
            "Личная сессия Moodle удалена из бота."
            if removed
            else "У вас нет подключённой сессии Moodle.",
            reply_markup=CONNECT_MENU,
        )

    @router.message(Command("check"))
    @router.message(F.text == MENU_CHECK)
    async def cmd_check(message: Message) -> None:
        personal, snapshot = await _personal_snapshot(message)
        if personal:
            if snapshot is None:
                return
            upcoming = sum(
                1 for item in snapshot.assignments
                if item.due_at and item.due_at > datetime.now(timezone.utc)
                and not (item.submitted and settings.skip_submitted_deadlines)
            )
            await message.answer(
                f"<b>Moodle обновлён</b>\n"
                f"Профиль: {escape(snapshot.user_fullname) or 'не определён'}\n"
                f"Курсов: {len(snapshot.courses)} · оценок: {len(snapshot.grades)}\n"
                f"Предстоящих дедлайнов: {upcoming}",
                reply_markup=MAIN_MENU,
            )
            return
        if not await _is_authorized_message(message):
            return
        async with session_factory() as session:
            repo = LMSRepository(session)
            state = await repo.get_or_create_sync_state()
            now_dt = datetime.now(timezone.utc)
            if state.last_success_at and (now_dt - state.last_success_at).total_seconds() < 60:
                await message.answer(
                    "Moodle уже проверен менее минуты назад. Данные актуальны.\n"
                    "Для просмотра предметов и сроков выберите пункт в меню ниже.",
                    reply_markup=MAIN_MENU,
                )
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
        personal, snapshot = await _personal_snapshot(message)
        if personal:
            if snapshot is None:
                return
            include_submitted = bool(command.args and command.args.strip().lower() == "all")
            now = datetime.now(timezone.utc)
            cutoff = now + timedelta(days=settings.lookahead_days)
            assignments = [
                item for item in snapshot.assignments
                if (include_submitted or not (item.submitted and settings.skip_submitted_deadlines))
                and (item.due_at is None or now <= item.due_at <= cutoff)
            ]
            await _reply_chunked(
                message,
                format_deadlines_list(assignments, settings.tz_info, now, include_submitted),
            )
            return
        if not await _is_authorized_message(message):
            return
        include_submitted = bool(command.args and command.args.strip().lower() == "all")
        await _show_deadlines(message, include_submitted)

    @router.message(F.text == MENU_DEADLINES)
    async def menu_deadlines(message: Message) -> None:
        personal, snapshot = await _personal_snapshot(message)
        if personal:
            if snapshot is None:
                return
            now = datetime.now(timezone.utc)
            cutoff = now + timedelta(days=settings.lookahead_days)
            assignments = [
                item for item in snapshot.assignments
                if not (item.submitted and settings.skip_submitted_deadlines)
                and (item.due_at is None or now <= item.due_at <= cutoff)
            ]
            await _reply_chunked(message, format_deadlines_list(assignments, settings.tz_info, now))
            return
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
        personal, snapshot = await _personal_snapshot(message)
        if personal:
            if snapshot is None:
                return
            if command.args and command.args.strip().lower() == "recent":
                grades = sorted(
                    snapshot.grades,
                    key=lambda item: item.graded_at or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True,
                )[:20]
                await _reply_chunked(message, format_grades_list(grades, settings.tz_info))
            else:
                await _render_courses(message, snapshot.courses)
            return
        if not await _is_authorized_message(message):
            return
        if command.args and command.args.strip().lower() == "recent":
            await _show_recent_grades(message)
            return
        await _show_courses(message)

    @router.message(F.text == MENU_GRADES)
    async def menu_grades(message: Message) -> None:
        personal, snapshot = await _personal_snapshot(message)
        if personal:
            if snapshot is None:
                return
            grades = sorted(
                snapshot.grades,
                key=lambda item: item.graded_at or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )[:20]
            await _reply_chunked(message, format_grades_list(grades, settings.tz_info))
            return
        if not await _is_authorized_message(message):
            return
        await _show_recent_grades(message)

    @router.message(Command("scholarship", "calc", "stipendia"))
    @router.message(F.text == MENU_SCHOLARSHIP)
    @router.message(F.text == "🎓 Стипендия")
    @router.message(F.text == "Шансы на стипендию")
    async def cmd_scholarship(
        message: Message, command: CommandObject | None = None
    ) -> None:
        args_str = (command.args or "").strip() if command else ""
        if not args_str and message.text and not message.text.startswith("/"):
            parts = message.text.split(None, 1)
            if len(parts) > 1:
                args_str = parts[1].strip()

        # If user supplied numeric arguments (e.g. "80 85" or "75.5, 80")
        if args_str:
            nums = re.findall(r"\d+(?:[.,]\d+)?", args_str)
            if len(nums) >= 2:
                try:
                    m_val = float(nums[0].replace(",", "."))
                    e_val = float(nums[1].replace(",", "."))
                    if not (0 <= m_val <= 100 and 0 <= e_val <= 100):
                        await message.answer(
                            "Баллы должны быть в диапазоне от 0 до 100. Пример: <code>/scholarship 75 80</code>"
                        )
                        return
                    res = analyze_scholarship(m_val, e_val)
                    await message.answer(format_manual_scholarship(res))
                    return
                except ValueError:
                    pass
            await message.answer(
                "Укажите баллы Midterm и Endterm через пробел:\n"
                "<code>/scholarship [midterm] [endterm]</code>\n"
                "Например: <code>/scholarship 75 80</code>"
            )
            return

        personal, snapshot = await _personal_snapshot(message)
        if personal:
            if snapshot is None:
                return
            courses = snapshot.courses
            grades = snapshot.grades
            analyses = []
            for c in courses:
                c_grades = [g for g in grades if g.course_id == c.id]
                analyses.append(analyze_course_scholarship(c.id, c.name, c_grades))
            await _reply_chunked(message, format_courses_scholarship_summary(analyses))
            return

        if not await _is_authorized_message(message):
            return

        async with session_factory() as session:
            repo = LMSRepository(session)
            courses = await repo.list_courses()
            analyses = []
            for c in courses:
                c_grades = await repo.list_course_grades(c.id)
                analyses.append(analyze_course_scholarship(c.id, c.name, c_grades))

        await _reply_chunked(message, format_courses_scholarship_summary(analyses))

    async def _show_recent_grades(message: Message) -> None:
        async with session_factory() as session:
            repo = LMSRepository(session)
            items = await repo.list_recent_grades(limit=20)
        await _reply_chunked(message, format_grades_list(items, settings.tz_info))

    @router.message(F.text == MENU_COURSES)
    async def menu_courses(message: Message) -> None:
        personal, snapshot = await _personal_snapshot(message)
        if personal:
            if snapshot is None:
                return
            await _render_courses(message, snapshot.courses)
            return
        if not await _is_authorized_message(message):
            return
        await _show_courses(message)

    async def _show_courses(message: Message) -> None:
        async with session_factory() as session:
            repo = LMSRepository(session)
            courses = await repo.list_courses()

        await _render_courses(message, courses)

    async def _render_courses(
        message: Message, courses: list[CourseDTO | CourseModel]
    ) -> None:
        if not courses:
            await message.answer("<b>Предметы</b>\nСписок пока пуст. Нажмите «Проверить Moodle».")
            return

        await message.answer(
            f"<b>Предметы · {len(courses)}</b>\nВыберите предмет для просмотра заданий и оценок:",
            reply_markup=_build_courses_keyboard(courses),
        )

    @router.callback_query(F.data == "courses:list")
    async def cb_courses_list(callback: CallbackQuery) -> None:
        personal, snapshot = await _personal_callback_snapshot(callback)
        if personal:
            if snapshot is None:
                return
            courses = snapshot.courses
        else:
            if not await _is_authorized_callback(callback):
                return
            async with session_factory() as session:
                repo = LMSRepository(session)
                courses = await repo.list_courses()

        msg = callback.message
        if isinstance(msg, Message):
            if not courses:
                await msg.edit_text("<b>Предметы</b>\nСписок пока пуст. Нажмите «Проверить Moodle».")
            else:
                try:
                    await msg.edit_text(
                        f"<b>Предметы · {len(courses)}</b>\nВыберите предмет для просмотра заданий и оценок:",
                        reply_markup=_build_courses_keyboard(courses),
                    )
                except TelegramBadRequest:
                    pass
        try:
            await callback.answer()
        except TelegramBadRequest:
            pass

    @router.callback_query(F.data.startswith("course:"))
    async def cb_course_detail(callback: CallbackQuery) -> None:
        personal, snapshot = await _personal_callback_snapshot(callback)
        if personal and snapshot is None:
            return
        if not personal and not await _is_authorized_callback(callback):
            return
        raw_id = (callback.data or "").split(":", 1)[1]
        try:
            course_id = int(raw_id)
        except ValueError:
            await callback.answer("Некорректный ID курса", show_alert=True)
            return

        now = datetime.now(timezone.utc)
        if personal:
            course = next((item for item in snapshot.courses if item.id == course_id), None)
            if course is None:
                await callback.answer("Курс не найден в вашем Moodle.", show_alert=True)
                return
            assignments = [item for item in snapshot.assignments if item.course_id == course_id]
            grades = [item for item in snapshot.grades if item.course_id == course_id]
        else:
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
        try:
            await callback.answer()
        except TelegramBadRequest:
            pass

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
        if message.chat.type == "private" and message.chat.id != settings.telegram_chat_id:
            connected = bool(
                message.from_user
                and await user_account_exists(session_factory, message.from_user.id)
            )
            if connected:
                await message.answer(
                    "Moodle подключён. Выберите нужный раздел в меню. "
                    "Если сессия истекла, отправьте /connect.",
                    reply_markup=MAIN_MENU,
                )
            else:
                await message.answer(
                    "Moodle не подключён. Нажмите «Подключить Moodle».",
                    reply_markup=CONNECT_MENU,
                )
            return
        if not await _is_authorized_message(message):
            return
        async with session_factory() as session:
            repo = LMSRepository(session)
            state = await repo.get_or_create_sync_state()
        await message.answer(format_sync_status(state, settings.tz_info))

    @router.message(F.text)
    async def unknown_text(message: Message) -> None:
        if message.chat.type == "private" and message.chat.id != settings.telegram_chat_id:
            connected = bool(
                message.from_user
                and await user_account_exists(session_factory, message.from_user.id)
            )
            await message.answer(
                "Выберите действие в меню ниже."
                if connected
                else "Сначала нажмите «Подключить Moodle».",
                reply_markup=MAIN_MENU if connected else CONNECT_MENU,
            )
            return
        if not await _is_authorized_message(message):
            return
        await message.answer(
            "Выберите действие в меню ниже. Кнопка «Помощь» объясняет, где что найти.",
            reply_markup=MAIN_MENU,
        )

    return router
