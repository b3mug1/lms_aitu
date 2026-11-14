from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from app.lms.base import GradeDTO
from app.storage.models import GradeModel


@dataclass(frozen=True)
class TargetThreshold:
    label: str
    target_score: float
    required_final: float
    achievable: bool


@dataclass(frozen=True)
class ManualScholarshipResult:
    midterm: float
    endterm: float
    midterm_weighted: float
    endterm_weighted: float
    current_points: float  # sum of midterm*0.3 + endterm*0.3 (max 60)
    needed_for_70: float
    required_final_70: float
    max_possible_total: float
    achievable_70: bool
    targets: list[TargetThreshold]


@dataclass(frozen=True)
class CourseScholarshipAnalysis:
    course_id: int
    course_name: str
    midterm: float | None
    endterm: float | None
    final_exam: float | None
    current_total: float | None
    has_full_term: bool
    result: ManualScholarshipResult | None
    status_text: str


def calculate_required_final(
    midterm: float, endterm: float, target: float = 70.0
) -> float:
    """Calculate the required final exam score (0-100) to reach target total grade.

    Formula: (midterm * 0.3) + (endterm * 0.3) + (final * 0.4) >= target
    final >= (target - (midterm * 0.3 + endterm * 0.3)) / 0.4
    """
    current_points = (midterm * 0.3) + (endterm * 0.3)
    needed = target - current_points
    return needed / 0.4


def analyze_scholarship(midterm: float, endterm: float) -> ManualScholarshipResult:
    """Analyze scholarship chances given Midterm and Endterm scores."""
    midterm_w = round(midterm * 0.3, 3)
    endterm_w = round(endterm * 0.3, 3)
    current_pts = round(midterm_w + endterm_w, 3)
    needed_70 = round(70.0 - current_pts, 3)
    req_final_70 = calculate_required_final(midterm, endterm, 70.0)
    max_total = round(current_pts + 40.0, 2)
    achievable_70 = req_final_70 <= 100.0

    target_defs = [
        ("Стипендия (C+)", 70.0),
        ("Оценка B", 80.0),
        ("Оценка B+", 85.0),
        ("Повышенная стипендия (A-)", 90.0),
    ]

    targets: list[TargetThreshold] = []
    for label, target_val in target_defs:
        req = calculate_required_final(midterm, endterm, target_val)
        targets.append(
            TargetThreshold(
                label=label,
                target_score=target_val,
                required_final=round(req, 2),
                achievable=req <= 100.0,
            )
        )

    return ManualScholarshipResult(
        midterm=round(midterm, 2),
        endterm=round(endterm, 2),
        midterm_weighted=round(midterm_w, 2),
        endterm_weighted=round(endterm_w, 2),
        current_points=round(current_pts, 2),
        needed_for_70=round(needed_70, 2),
        required_final_70=round(req_final_70, 2),
        max_possible_total=max_total,
        achievable_70=achievable_70,
        targets=targets,
    )


def extract_course_registers(
    grades: Sequence[GradeDTO | GradeModel],
) -> tuple[float | None, float | None, float | None, float | None]:
    """Extract (midterm, endterm, final_exam, course_total) from Moodle grades."""
    midterm: float | None = None
    endterm: float | None = None
    final_exam: float | None = None
    course_total: float | None = None

    # Priority 1: Exact "Register" gradebook items
    for g in grades:
        name = g.item_name.lower().strip()
        val = float(g.grade_raw) if g.grade_raw is not None else None
        if val is None:
            continue
        if "register midterm" in name:
            midterm = val
        elif "register endterm" in name:
            endterm = val
        elif "register final" in name:
            final_exam = val
        elif "register(not to edit) total" in name or "register total" in name:
            course_total = val

    # Priority 2: Fallback to non-register items if missing or 0.0
    for g in grades:
        name = g.item_name.lower().strip()
        val = float(g.grade_raw) if g.grade_raw is not None else None
        if val is None or val == 0:
            continue
        if (midterm is None or midterm == 0.0) and "midterm" in name and "register" not in name:
            midterm = val
        if (endterm is None or endterm == 0.0) and "endterm" in name and "register" not in name:
            endterm = val
        if (final_exam is None or final_exam == 0.0) and "final" in name and "register" not in name:
            final_exam = val
        if (course_total is None or course_total == 0.0) and "total" in name and "register" not in name:
            course_total = val

    return midterm, endterm, final_exam, course_total


def analyze_course_scholarship(
    course_id: int,
    course_name: str,
    grades: Sequence[GradeDTO | GradeModel],
) -> CourseScholarshipAnalysis:
    """Analyze scholarship requirement for a single course."""
    midterm, endterm, final_exam, total = extract_course_registers(grades)

    # Check if course already completed
    if total is not None and total > 0 and final_exam is not None and final_exam > 0:
        passed = total >= 70.0
        status = (
            f"✅ <b>Сдан</b> (Итог: <b>{total:.2f}</b>, Финал: <b>{final_exam:.2f}</b>)"
            if passed
            else f"❌ <b>Ниже 70</b> (Итог: <b>{total:.2f}</b>, Финал: <b>{final_exam:.2f}</b>)"
        )
        return CourseScholarshipAnalysis(
            course_id=course_id,
            course_name=course_name,
            midterm=midterm,
            endterm=endterm,
            final_exam=final_exam,
            current_total=total,
            has_full_term=True,
            result=analyze_scholarship(midterm or 0.0, endterm or 0.0)
            if (midterm and endterm)
            else None,
            status_text=status,
        )

    # In progress
    if midterm is not None and midterm > 0 and endterm is not None and endterm > 0:
        result = analyze_scholarship(midterm, endterm)
        req = result.required_final_70
        if req <= 0:
            status = f"🟢 <b>Гарантировано</b> (достаточно сдать финал ≥50)"
        elif req <= 50:
            status = f"🟢 <b>Финал: ≥50</b> (матем. {req:.1f}, сдача ≥50)"
        elif req <= 80:
            status = f"🟡 <b>Финал: ≥{req:.1f}</b> (цель: {math.ceil(req)})"
        elif req <= 100:
            status = f"🟠 <b>Финал: ≥{req:.1f}</b> (нужно постараться)"
        else:
            status = f"🔴 <b>Недостижимо</b> (макс. {result.max_possible_total:.1f})"

        return CourseScholarshipAnalysis(
            course_id=course_id,
            course_name=course_name,
            midterm=midterm,
            endterm=endterm,
            final_exam=final_exam,
            current_total=total,
            has_full_term=True,
            result=result,
            status_text=status,
        )

    if midterm is not None and midterm > 0:
        status = "⏳ <i>Ожидается Endterm</i>"
        return CourseScholarshipAnalysis(
            course_id=course_id,
            course_name=course_name,
            midterm=midterm,
            endterm=None,
            final_exam=None,
            current_total=total,
            has_full_term=False,
            result=None,
            status_text=status,
        )

    return CourseScholarshipAnalysis(
        course_id=course_id,
        course_name=course_name,
        midterm=None,
        endterm=None,
        final_exam=None,
        current_total=total,
        has_full_term=False,
        result=None,
        status_text="⏳ <i>Баллы ещё не выставлены</i>",
    )


def format_manual_scholarship(res: ManualScholarshipResult) -> str:
    """Format the calculation of scholarship chances for given Midterm & Endterm."""
    lines = [
        "🎓 <b>Расчёт шансов на стипендию</b>",
        f"Формула: <code>(Midterm × 0.3) + (Endterm × 0.3) + (Final × 0.4) ≥ 70</code>\n",
        "📊 <b>Ваши баллы:</b>",
        f"• Регистр Midterm (30%): <b>{res.midterm:.1f}</b> → <b>{res.midterm_weighted:.2f}</b> б.",
        f"• Регистр Endterm (30%): <b>{res.endterm:.1f}</b> → <b>{res.endterm_weighted:.2f}</b> б.",
        f"• Накопленный допуск: <b>{res.current_points:.2f}</b> / 60.0 б.\n",
    ]

    req = res.required_final_70
    if req <= 0:
        lines.extend(
            [
                "🎯 <b>Порог стипендии (70 баллов):</b>",
                "🟢 <b>Вы уже набрали 70+ баллов!</b>",
                f"Накопленный балл (допуск) составляет <b>{res.current_points:.2f}</b>.",
                "Для получения оценки достаточно просто сдать сам финал без академической задолженности (обычно порог сдачи самого экзамена составляет <b>50 баллов</b>).\n",
            ]
        )
    elif req <= 50.0:
        lines.extend(
            [
                "🎯 <b>Порог стипендии (70 баллов):</b>",
                f"👉 Математически для 70 нужно набрать всего: <b>{req:.1f}</b> из 100.",
                "⚠️ <b>Внимание:</b> порог успешной сдачи самого финального экзамена обычно составляет <b>50 баллов</b>. Набрав 50+ на финале, вы гарантированно сохраняете стипендию!\n",
            ]
        )
    elif req <= 100.0:
        ceil_req = math.ceil(req)
        lines.extend(
            [
                "🎯 <b>Порог стипендии (70 баллов):</b>",
                f"👉 На финале нужно набрать минимум: <b>{req:.1f}</b> из 100 (цель: <b>{ceil_req}</b> б.).",
                f"<i>(Расчёт: {res.current_points:.2f} + {req:.1f} × 0.4 = 70.0)</i>\n",
            ]
        )
    else:
        lines.extend(
            [
                "🎯 <b>Порог стипендии (70 баллов):</b>",
                "🔴 <b>К сожалению, порог 70 недостижим.</b>",
                f"Даже при максимальных 100 баллах на финале итоговая оценка составит <b>{res.max_possible_total:.2f}</b> из 100.\n",
            ]
        )

    lines.append("📈 <b>Ориентиры по оценкам:</b>")
    for t in res.targets:
        if t.required_final <= 0:
            lines.append(f"• <b>{t.label} ({t.target_score:.0f}+)</b>: гарантировано (сдача финала ≥50)")
        elif t.required_final <= 100:
            lines.append(
                f"• <b>{t.label} ({t.target_score:.0f}+)</b>: финал <b>{t.required_final:.1f}</b> (цель: {math.ceil(t.required_final)})"
            )
        else:
            lines.append(f"• <b>{t.label} ({t.target_score:.0f}+)</b>: <i>недостижимо</i>")

    return "\n".join(lines)


def format_courses_scholarship_summary(
    analyses: list[CourseScholarshipAnalysis],
) -> str:
    """Format full summary of scholarship chances across all enrolled courses."""
    if not analyses:
        return (
            "🎓 <b>Расчёт стипендии</b>\n\n"
            "Список курсов пока пуст. Выполните проверку Moodle или введите баллы вручную:\n"
            "<code>/scholarship [midterm] [endterm]</code>"
        )

    lines = [
        "🎓 <b>Шансы на стипендию по предметам</b>",
        "Формула: <code>(Midterm × 0.3) + (Endterm × 0.3) + (Final × 0.4) ≥ 70</code>\n",
    ]

    ready_courses = [a for a in analyses if a.has_full_term and a.result]
    in_progress = [a for a in analyses if not a.has_full_term]

    if ready_courses:
        lines.append("📚 <b>Предметы с выставленными баллами:</b>")
        for idx, a in enumerate(ready_courses, start=1):
            res = a.result
            assert res is not None
            c_name = a.course_name.split("|")[0].strip()
            lines.append(f"\n<b>{idx}. {c_name}</b>")
            lines.append(
                f"• Midterm: <b>{a.midterm:.1f}</b> · Endterm: <b>{a.endterm:.1f}</b> (допуск: <b>{res.current_points:.1f}</b>/60)"
            )
            req = res.required_final_70
            if req <= 0:
                lines.append(f"🎯 <b>На финале:</b> {a.status_text}")
            elif req <= 50:
                lines.append(f"🎯 <b>На финале:</b> {a.status_text}")
            elif req <= 100:
                lines.append(f"🎯 <b>На финале нужно:</b> <b>{req:.1f}</b> б. (цель: <b>{math.ceil(req)}</b>)")
            else:
                lines.append(f"🎯 <b>На финале:</b> {a.status_text}")

    if in_progress:
        lines.append("\n⏳ <b>Остальные предметы:</b>")
        for a in in_progress:
            c_name = a.course_name.split("|")[0].strip()
            if a.midterm is not None:
                lines.append(f"• <b>{c_name}</b>: Midterm: <b>{a.midterm:.1f}</b>, {a.status_text}")
            else:
                lines.append(f"• <b>{c_name}</b>: {a.status_text}")

    lines.append(
        "\n💡 <b>Ручной расчёт для любых баллов:</b>\n"
        "Отправьте команду с оценками Midterm и Endterm:\n"
        "<code>/scholarship 75 80</code> или <code>/calc 85 90</code>\n\n"
        "ℹ️ <i>Для сохранения стипендии все дисциплины должны быть закрыты не ниже 70 (C+).</i>"
    )

    return "\n".join(lines)
