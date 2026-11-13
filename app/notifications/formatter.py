from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from app.lms.base import AssignmentDTO, CourseDTO, GradeDTO, LMSSnapshot
from app.storage.models import AssignmentModel, CourseModel, GradeModel, SyncStateModel, as_utc

_MODULE_LABELS = {
    "assign": "Задание",
    "quiz": "Тест",
    "workshop": "Семинар",
    "lesson": "Лекция",
}


def plural_ru(value: int, forms: tuple[str, str, str]) -> str:
    """Select Russian plural form: (1 день, 2 дня, 5 дней)."""
    n = abs(value) % 100
    if 11 <= n <= 19:
        return forms[2]
    n_mod = n % 10
    if n_mod == 1:
        return forms[0]
    if 2 <= n_mod <= 4:
        return forms[1]
    return forms[2]


def format_threshold_label(threshold_minutes: int) -> str:
    """Format reminder threshold like 'за 5 дней', 'за 12 часов', 'за 30 минут'."""
    if threshold_minutes >= 1440 and threshold_minutes % 1440 == 0:
        days = threshold_minutes // 1440
        return f"за {days} {plural_ru(days, ('день', 'дня', 'дней'))}"
    if threshold_minutes >= 60 and threshold_minutes % 60 == 0:
        hours = threshold_minutes // 60
        return f"за {hours} {plural_ru(hours, ('час', 'часа', 'часов'))}"
    return f"за {threshold_minutes} {plural_ru(threshold_minutes, ('минуту', 'минуты', 'минут'))}"


def format_remaining_time(due_at: datetime, now: datetime) -> str:
    """Format human-readable remaining duration until deadline."""
    delta_seconds = int((as_utc(due_at) - as_utc(now)).total_seconds())  # type: ignore[operator]
    if delta_seconds <= 0:
        return "срок истёк"
    total_minutes = delta_seconds // 60
    days, rem_minutes = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem_minutes, 60)

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days} д.")
    if hours > 0:
        parts.append(f"{hours} ч.")
    if days == 0 and minutes > 0:
        parts.append(f"{minutes} мин.")
    if not parts:
        parts.append("менее 1 мин.")
    return "осталось " + " ".join(parts)


def format_local_dt(dt: datetime | None, tz: ZoneInfo) -> str:
    """Format UTC datetime in the configured local timezone."""
    utc_dt = as_utc(dt)
    if utc_dt is None:
        return "не указан"
    return utc_dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _format_link(url: str | None, label: str = "Открыть в Moodle") -> str:
    if not url:
        return ""
    return f'<a href="{_esc(url)}">{_esc(label)}</a>'


def format_welcome_message(
    snapshot: LMSSnapshot, tz: ZoneInfo, upcoming_deadlines: int
) -> str:
    user_part = f" ({_esc(snapshot.user_fullname)})" if snapshot.user_fullname else ""
    lines = [
        f"<b>Moodle подключён</b>{user_part}",
        f"Режим: <code>{_esc(snapshot.mode)}</code>",
        f"Курсов: <b>{len(snapshot.courses)}</b>",
        f"Заданий/активностей: <b>{len(snapshot.assignments)}</b> (активных дедлайнов: <b>{upcoming_deadlines}</b>)",
        f"Зафиксировано оценок: <b>{len(snapshot.grades)}</b>",
        "",
        "Базовое состояние сохранено. Теперь бот будет присылать уведомления о новых оценках, новых заданиях и приближающихся дедлайнах.",
        f"Время синхронизации: {format_local_dt(snapshot.fetched_at, tz)}",
    ]
    return "\n".join(lines)


def format_new_assignment(assignment: AssignmentDTO, tz: ZoneInfo, now: datetime) -> str:
    label = _MODULE_LABELS.get(assignment.module_type, "Активность")
    lines = [
        f"<b>В Moodle добавлено: {label.lower()}</b>",
        f"<b>Курс:</b> {_esc(assignment.course_name or f'#{assignment.course_id}')}",
        f"<b>Название:</b> {_esc(assignment.title)}",
    ]
    if assignment.due_at:
        lines.append(
            f"<b>Дедлайн:</b> {format_local_dt(assignment.due_at, tz)} "
            f"({format_remaining_time(assignment.due_at, now)})"
        )
    else:
        lines.append("<b>Дедлайн:</b> не указан")

    if assignment.description:
        lines.append(f"<b>Описание:</b> {_esc(assignment.description)}")

    link = _format_link(assignment.url)
    if link:
        lines.append(link)
    return "\n".join(lines)


def format_deadline_changed(
    assignment: AssignmentDTO,
    old_due: datetime | None,
    tz: ZoneInfo,
    now: datetime,
) -> str:
    lines = [
        "<b>Изменился срок сдачи</b>",
        f"<b>Курс:</b> {_esc(assignment.course_name or f'#{assignment.course_id}')}",
        f"<b>Задание:</b> {_esc(assignment.title)}",
        f"<b>Было:</b> {format_local_dt(old_due, tz)}",
    ]
    if assignment.due_at:
        lines.append(
            f"<b>Стало:</b> {format_local_dt(assignment.due_at, tz)} "
            f"({format_remaining_time(assignment.due_at, now)})"
        )
    else:
        lines.append("<b>Стало:</b> срок снят / не указан")

    link = _format_link(assignment.url)
    if link:
        lines.append(link)
    return "\n".join(lines)


def format_deadline_reminder(
    assignment: AssignmentDTO,
    threshold_minutes: int,
    tz: ZoneInfo,
    now: datetime,
) -> str:
    if threshold_minutes <= 180:
        header = "<b>СРОЧНЫЙ ДЕДЛАЙН!</b>"
    elif threshold_minutes <= 720:
        header = "<b>Дедлайн уже скоро!</b>"
    elif threshold_minutes <= 1440:
        header = "<b>Напоминание о дедлайне</b>"
    else:
        header = "<b>Напоминание о дедлайне</b>"

    threshold_text = format_threshold_label(threshold_minutes)
    due_str = format_local_dt(assignment.due_at, tz)
    rem_str = format_remaining_time(assignment.due_at, now) if assignment.due_at else ""

    lines = [
        f"{header} ({_esc(threshold_text)})",
        f"<b>Курс:</b> {_esc(assignment.course_name or f'#{assignment.course_id}')}",
        f"<b>Задание:</b> {_esc(assignment.title)}",
        f"<b>Срок сдачи:</b> {due_str}" + (f" ({rem_str})" if rem_str else ""),
    ]
    link = _format_link(assignment.url)
    if link:
        lines.append(link)
    return "\n".join(lines)


def format_grade_notification(
    grade: GradeDTO,
    is_update: bool = False,
    old_grade: str | None = None,
) -> str:
    header = "<b>Оценка обновлена</b>" if is_update else "<b>Выставлена оценка</b>"
    score_parts = [_esc(grade.grade_formatted)]
    if grade.grade_max is not None and "/" not in grade.grade_formatted:
        max_str = f"{grade.grade_max:g}"
        score_parts.append(f"/ {_esc(max_str)}")
    if grade.percentage:
        score_parts.append(f"({_esc(grade.percentage)})")
    score_line = " ".join(score_parts)

    lines = [
        header,
        f"<b>Курс:</b> {_esc(grade.course_name or f'#{grade.course_id}')}",
        f"<b>Работа:</b> {_esc(grade.item_name)}",
    ]
    if is_update and old_grade:
        lines.append(f"<b>Оценка:</b> {_esc(old_grade)} → <b>{score_line}</b>")
    else:
        lines.append(f"<b>Оценка:</b> <b>{score_line}</b>")

    if grade.feedback:
        lines.append(f"<b>Комментарий:</b> {_esc(grade.feedback)}")

    link = _format_link(grade.url)
    if link:
        lines.append(link)
    return "\n".join(lines)


def format_deadlines_list(
    assignments: list[AssignmentModel | AssignmentDTO],
    tz: ZoneInfo,
    now: datetime,
    include_submitted: bool = False,
) -> str:
    if not assignments:
        if include_submitted:
            return "🗓 На ближайший период нет заданий со сроком сдачи."
        return "🗓 На ближайший период нет несданных заданий со сроком сдачи."

    header = (
        "Ближайшие сроки сдачи · включая сданные"
        if include_submitted
        else "Ближайшие сроки сдачи"
    )
    lines = [f"🗓 <b>{header} · {len(assignments)}</b>", ""]
    for item in assignments:
        due_dt = as_utc(item.due_at)
        title_html = (
            f'<a href="{_esc(item.url)}">{_esc(item.title)}</a>'
            if item.url
            else _esc(item.title)
        )
        if item.submitted:
            status_icon = "✅"
            status = "сдано"
        elif due_dt is None:
            status_icon = "⚪"
            status = "срок не указан"
        elif due_dt <= now:
            status_icon = "🔴"
            status = "срок истёк"
        elif (due_dt - now).total_seconds() <= 24 * 60 * 60:
            status_icon = "🟠"
            status = format_remaining_time(due_dt, now)
        else:
            status_icon = "📅"
            status = format_remaining_time(due_dt, now)

        lines.append(f"{status_icon} <b>{title_html}</b>")
        lines.append(f"   {_esc(item.course_name)}")
        if due_dt is not None:
            lines.append(f"   ⏰ {format_local_dt(due_dt, tz)} · {status}")
        else:
            lines.append(f"   {status}")
    return "\n".join(lines)


def _format_grade_score(item: GradeModel | GradeDTO) -> str:
    score = _esc(item.grade_formatted)
    if item.grade_max is not None and "/" not in item.grade_formatted:
        score += f" / {item.grade_max:g}"
    if item.percentage and not (item.grade_raw is not None and item.grade_max == 100):
        score += f" ({_esc(item.percentage)})"
    return score


def format_grades_list(grades: list[GradeModel | GradeDTO], tz: ZoneInfo) -> str:
    if not grades:
        return (
            "Подтверждённых оценок пока нет. Нулевые записи без даты оценки "
            "или комментария можно посмотреть в разделе «Предметы»."
        )

    by_course: dict[str, list[GradeModel | GradeDTO]] = {}
    for item in grades:
        by_course.setdefault(item.course_name or "Без названия предмета", []).append(item)

    lines = ["<b>Последние оценки</b>", ""]
    for course_name, items in by_course.items():
        lines.append(f"<b>{_esc(course_name)}</b>")
        for item in items:
            score = _format_grade_score(item)
            name_html = (
                f'<a href="{_esc(item.url)}">{_esc(item.item_name)}</a>'
                if item.url
                else _esc(item.item_name)
            )
            lines.append(f"• {name_html} — <b>{score}</b>")
            if item.graded_at:
                lines.append(f"  Оценено: {format_local_dt(item.graded_at, tz)}")
        lines.append("")
    lines.append(
        "Нулевые записи без даты оценки или комментария доступны в разделе «Предметы»."
    )
    return "\n".join(lines)


def format_course_details(
    course: CourseModel | CourseDTO,
    assignments: list[AssignmentModel | AssignmentDTO],
    grades: list[GradeModel | GradeDTO],
    tz: ZoneInfo,
    now: datetime,
) -> str:
    """Format interactive course card showing register totals, grades, and assignments."""
    course_name, separator, teacher_name = course.name.partition("|")
    course_name = course_name.strip()
    teacher_name = teacher_name.strip()
    course_title = (
        f'<a href="{_esc(course.url)}">{_esc(course_name)}</a>'
        if course.url
        else _esc(course_name)
    )
    lines = [f"📚 <b>{course_title}</b>"]
    if separator and teacher_name:
        lines.append(f"👤 <i>{_esc(teacher_name)}</i>")

    register_grades = [
        g
        for g in grades
        if g.assignment_id is None and "register" in g.item_name.lower()
    ]
    activity_grades = [g for g in grades if g not in register_grades]

    if register_grades:
        lines.append("")
        lines.append(f"📊 <b>Регистр · {len(register_grades)}</b>")
        for g in register_grades:
            lines.append(f"  • {_esc(g.item_name)} — <b>{_format_grade_score(g)}</b>")

    lines.append("")
    if activity_grades:
        lines.append(f"🎓 <b>Оценки · {len(activity_grades)}</b>")
        for g in activity_grades:
            name_html = (
                f'<a href="{_esc(g.url)}">{_esc(g.item_name)}</a>'
                if g.url
                else _esc(g.item_name)
            )
            lines.append(f"  • {name_html} — <b>{_format_grade_score(g)}</b>")
            if g.feedback:
                lines.append(f"    💬 <i>{_esc(g.feedback)}</i>")
    else:
        lines.append("🎓 <b>Оценки</b> · пока нет выставленных оценок")

    lines.append("")
    if assignments:
        # Sort: upcoming unsubmitted first, then future submitted, then past/no-deadline
        def _sort_key(item: AssignmentModel | AssignmentDTO) -> tuple[int, float]:
            due = as_utc(item.due_at)
            if due is None:
                return (3, 0.0)
            if due >= now and not item.submitted:
                return (0, due.timestamp())
            if due >= now and item.submitted:
                return (1, due.timestamp())
            return (2, -due.timestamp())

        sorted_assigns = sorted(assignments, key=_sort_key)
        lines.append(f"🗓 <b>Задания · {len(sorted_assigns)}</b>")
        for item in sorted_assigns:
            due = as_utc(item.due_at)
            title_html = (
                f'<a href="{_esc(item.url)}">{_esc(item.title)}</a>'
                if item.url
                else _esc(item.title)
            )
            if due is None:
                lines.append(f"  ⚪ {title_html} · срок не указан")
            elif item.submitted:
                lines.append(f"  ✅ {title_html} · сдано · {format_local_dt(due, tz)}")
            elif due >= now:
                status_icon = "🟠" if (due - now).total_seconds() <= 24 * 60 * 60 else "📅"
                lines.append(
                    f"  {status_icon} {title_html} · {format_local_dt(due, tz)}"
                    f" · {format_remaining_time(due, now)}"
                )
            else:
                lines.append(
                    f"  🔴 {title_html} · {format_local_dt(due, tz)} · срок истёк"
                )
    else:
        lines.append("🗓 <b>Задания</b> · не найдены")

    return "\n".join(lines)


def format_courses_list(courses: list[CourseModel]) -> str:
    if not courses:
        return "Список предметов пока пуст. Нажмите «Проверить Moodle»."

    lines = [f"<b>Предметы ({len(courses)}):</b>", ""]
    for idx, course in enumerate(courses, start=1):
        name_html = (
            f'<a href="{_esc(course.url)}">{_esc(course.name)}</a>'
            if course.url
            else _esc(course.name)
        )
        lines.append(f"{idx}. {name_html}")
    return "\n".join(lines)


def format_sync_status(state: SyncStateModel | None, tz: ZoneInfo) -> str:
    if state is None:
        return "Проверка Moodle ещё не запускалась. Нажмите «Проверить Moodle»."

    status_line = "Работает штатно" if state.consecutive_errors == 0 else f"Ошибок подряд: {state.consecutive_errors}"
    lines = [
        "<b>Состояние подключения</b>",
        f"<b>Состояние:</b> {status_line}",
        f"<b>Режим Moodle:</b> <code>{_esc(state.active_mode or 'не определён')}</code>",
        f"<b>Последняя проверка:</b> {format_local_dt(state.last_sync_at, tz)}",
        f"<b>Последний успех:</b> {format_local_dt(state.last_success_at, tz)}",
    ]
    if state.user_fullname:
        lines.append(f"<b>Профиль:</b> {_esc(state.user_fullname)}")
    if state.last_error:
        lines.append(f"<b>Что случилось:</b> {format_sync_error(state.last_error)}")
    return "\n".join(lines)


def format_sync_error(error: str | None) -> str:
    """Turn a known Moodle authentication failure into an actionable bot message."""
    if error and "one-time Microsoft SSO setup" in error:
        return (
            "Сессия Moodle истекла, автоматический вход через Microsoft не завершился. "
            "На компьютере бота повторите однократный вход командой "
            "<code>python -m app.moodle_sso_login</code>. "
            "Или обновите cookie владельца через <code>/setcookie</code>."
        )
    if error and "Moodle session expired and no MOODLE_USERNAME / MOODLE_PASSWORD configured" in error:
        return (
            "Сессия Moodle истекла. Для ручного обновления войдите в Moodle через браузер "
            "и отправьте этому боту в личном чате <code>/setcookie</code> без значения. "
            "Для автоматического входа через Microsoft настройте SSO-профиль на компьютере бота."
        )
    return f"<code>{_esc((error or 'Неизвестная ошибка')[:300])}</code>"


def split_telegram_message(text: str, limit: int = 4000) -> list[str]:
    """Split long HTML messages safely at line breaks without exceeding Telegram's 4096-char cap."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(line) <= limit:
                current = line
            else:
                # Hard-split an abnormally long single line
                for i in range(0, len(line), limit):
                    chunks.append(line[i : i + limit])
                current = ""
    if current:
        chunks.append(current)
    return chunks
