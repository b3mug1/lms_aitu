from __future__ import annotations

import html
import re
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


_WEEKDAYS_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _trim_trailing_zeros(value: str) -> str:
    """Drop insignificant decimals for display: '77.00' -> '77', '82.76' stays."""
    return re.sub(r"\.0+$", "", value)


def _grade_percentage(item: GradeModel | GradeDTO) -> float | None:
    """Best-effort 0-100 percentage behind a gradebook item."""
    if item.grade_raw is not None and item.grade_max:
        return item.grade_raw / item.grade_max * 100
    if item.percentage:
        match = re.search(r"\d+(?:[.,]\d+)?", item.percentage)
        if match:
            return float(match.group(0).replace(",", "."))
    return None


def _score_dot(item: GradeModel | GradeDTO) -> str:
    """Traffic-light marker for a score: green >=90, yellow 50-89, red <50 (pass mark)."""
    pct = _grade_percentage(item)
    if pct is None:
        return ""
    if pct >= 90:
        return "🟢 "
    if pct >= 50:
        return "🟡 "
    return "🔴 "


def _compact_dt(dt: datetime | None, tz: ZoneInfo, now: datetime, weekday: bool = True) -> str:
    """Compact local date like 'сб 21.11, 00:00'; year kept when it differs from now."""
    utc_dt = as_utc(dt)
    if utc_dt is None:
        return "не указан"
    local = utc_dt.astimezone(tz)
    now_year = as_utc(now).astimezone(tz).year if as_utc(now) else local.year
    day_part = f"{local:%d.%m}" if local.year == now_year else f"{local:%d.%m.%Y}"
    prefix = f"{_WEEKDAYS_RU[local.weekday()]} " if weekday else ""
    return f"{prefix}{day_part}, {local:%H:%M}"


def _display_title(name: str | None) -> str:
    """Normalize teacher-typed item names for display: 'endterm' -> 'Endterm'."""
    text = (name or "").strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def _remaining_short(due_at: datetime, now: datetime) -> str:
    """Compact countdown: 'через 3 д.', 'через 5 ч. 40 мин.'."""
    delta_seconds = int((as_utc(due_at) - as_utc(now)).total_seconds())  # type: ignore[operator]
    if delta_seconds <= 0:
        return "срок истёк"
    total_minutes = delta_seconds // 60
    days, rem_minutes = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem_minutes, 60)
    if days > 0:
        return f"через {days} д."
    if hours > 0:
        return f"через {hours} ч." + (f" {minutes} мин." if minutes else "")
    return f"через {minutes} мин." if minutes else "менее 1 мин."


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
        f"Предметы: <b>{len(snapshot.courses)}</b> · оценки: <b>{len(snapshot.grades)}</b>",
        f"Ближайшие сроки: <b>{upcoming_deadlines}</b>",
        "",
        "Бот сообщит о новых оценках, заданиях и сроках сдачи.",
        f"Обновлено: {format_local_dt(snapshot.fetched_at, tz)}",
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
            return "<b>СРОКИ СДАЧИ</b>\nНа ближайший период заданий со сроком сдачи нет."
        return "<b>СРОКИ СДАЧИ</b>\nБлижайших несданных заданий нет."

    header = (
        "БЛИЖАЙШИЕ СРОКИ СДАЧИ · ВКЛЮЧАЯ СДАННЫЕ"
        if include_submitted
        else "БЛИЖАЙШИЕ СРОКИ СДАЧИ"
    )
    lines = [f"<b>{header} · {len(assignments)}</b>", ""]
    for item in assignments:
        due_dt = as_utc(item.due_at)
        title_html = (
            f'<a href="{_esc(item.url)}">{_esc(_display_title(item.title))}</a>'
            if item.url
            else _esc(_display_title(item.title))
        )
        if item.submitted:
            status = "<i>сдано</i>"
        elif due_dt is None:
            status = "<i>срок не указан</i>"
        elif due_dt <= now:
            status = "<i>срок истёк</i>"
        else:
            status = _remaining_short(due_dt, now)

        detail_parts = [part for part in (_esc(item.course_name),) if part]
        if due_dt is not None:
            detail_parts.append(_compact_dt(due_dt, tz, now))
        detail_parts.append(status)
        lines.append(f"<b>{title_html}</b>")
        lines.append(" · ".join(detail_parts))
        lines.append("")
    return "\n".join(lines)


def _format_grade_score(item: GradeModel | GradeDTO) -> str:
    score = _esc(_trim_trailing_zeros(item.grade_formatted))
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

    now = datetime.now(tz)
    lines = ["<b>ПОСЛЕДНИЕ ОЦЕНКИ</b>", ""]
    for course_name, items in by_course.items():
        lines.append(f"<b>{_esc(course_name)}</b>")
        for item in items:
            score = _format_grade_score(item)
            name_html = (
                f'<a href="{_esc(item.url)}">{_esc(_display_title(item.item_name))}</a>'
                if item.url
                else _esc(_display_title(item.item_name))
            )
            line = f"• {name_html} — {_score_dot(item)}<b>{score}</b>"
            if item.graded_at:
                line += f" · <i>{_compact_dt(item.graded_at, tz, now, weekday=False)}</i>"
            lines.append(line)
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
    lines = [f"<b>{course_title}</b>"]
    if separator and teacher_name:
        lines.append(f"Преподаватель: {_esc(teacher_name)}")

    register_grades = [
        g
        for g in grades
        if g.assignment_id is None and "register" in g.item_name.lower()
    ]
    activity_grades = [g for g in grades if g not in register_grades]

    if register_grades:
        lines.append("")
        lines.append(f"<b>РЕГИСТР · {len(register_grades)}</b>")
        for g in register_grades:
            lines.append(
                f"  • {_esc(_display_title(g.item_name))}"
                f" — {_score_dot(g)}<b>{_format_grade_score(g)}</b>"
            )

    lines.append("")
    if activity_grades:
        lines.append(f"<b>ОЦЕНКИ · {len(activity_grades)}</b>")
        for g in activity_grades:
            name_html = (
                f'<a href="{_esc(g.url)}">{_esc(_display_title(g.item_name))}</a>'
                if g.url
                else _esc(_display_title(g.item_name))
            )
            lines.append(f"  • {name_html} — {_score_dot(g)}<b>{_format_grade_score(g)}</b>")
            if g.feedback:
                lines.append(f"    Комментарий: <i>{_esc(g.feedback)}</i>")
    else:
        lines.append("<b>ОЦЕНКИ</b> · пока нет выставленных оценок")

    lines.append("")
    active_assigns = [
        item for item in assignments
        if as_utc(item.due_at) is None or as_utc(item.due_at) >= now
    ]
    past_assigns = [
        item for item in assignments
        if as_utc(item.due_at) is not None and as_utc(item.due_at) < now
    ]

    # Active first: unsubmitted upcoming, then submitted upcoming, then no deadline.
    def _active_sort_key(item: AssignmentModel | AssignmentDTO) -> tuple[int, float]:
        due = as_utc(item.due_at)
        if due is None:
            return (2, 0.0)
        if item.submitted:
            return (1, due.timestamp())
        return (0, due.timestamp())

    if active_assigns or past_assigns:
        if active_assigns:
            lines.append(f"<b>ЗАДАНИЯ · активны {len(active_assigns)}</b>")
        else:
            lines.append("<b>ЗАДАНИЯ</b> · активных нет")
        for item in sorted(active_assigns, key=_active_sort_key):
            due = as_utc(item.due_at)
            title_html = (
                f'<a href="{_esc(item.url)}">{_esc(_display_title(item.title))}</a>'
                if item.url
                else _esc(_display_title(item.title))
            )
            if due is None:
                lines.append(f"  • {title_html} · <i>срок не указан</i>")
            elif item.submitted:
                lines.append(
                    f"  • {title_html} — {_compact_dt(due, tz, now)} · <i>сдано</i>"
                )
            else:
                lines.append(
                    f"  • {title_html} — {_compact_dt(due, tz, now)}"
                    f" · {_remaining_short(due, now)}"
                )
        if past_assigns:
            past_assigns = sorted(
                past_assigns, key=lambda item: -as_utc(item.due_at).timestamp()  # type: ignore[operator]
            )
            quote_lines = [f"<b>Прошедшие дедлайны · {len(past_assigns)}</b>"]
            for item in past_assigns:
                due = as_utc(item.due_at)
                status = "<i>сдано</i>" if item.submitted else "<i>не сдано</i>"
                quote_lines.append(
                    f"{_esc(_display_title(item.title))}"
                    f" — {_compact_dt(due, tz, now, weekday=False)} · {status}"
                )
            lines.append(
                "\n".join(
                    ["", '<blockquote expandable>', *quote_lines, "</blockquote>"]
                )
            )
    else:
        lines.append("<b>ЗАДАНИЯ</b> · не найдены")

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
