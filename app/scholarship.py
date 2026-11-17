from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape
from typing import Sequence

from app.lms.base import GradeDTO
from app.storage.models import GradeModel


@dataclass(frozen=True)
class GradeScaleEntry:
    letter: str
    gpa: float
    min_total: float
    max_total: float


# Official AITU scale: Total -> letter grade / GPA (bounds inclusive).
GRADE_SCALE: tuple[GradeScaleEntry, ...] = (
    GradeScaleEntry("A", 4.0, 95, 100),
    GradeScaleEntry("A-", 3.67, 90, 94),
    GradeScaleEntry("B+", 3.33, 85, 89),
    GradeScaleEntry("B", 3.0, 80, 84),
    GradeScaleEntry("B-", 2.67, 75, 79),
    GradeScaleEntry("C+", 2.33, 70, 74),
    GradeScaleEntry("C", 2.0, 65, 69),
    GradeScaleEntry("C-", 1.67, 60, 64),
    GradeScaleEntry("D+", 1.33, 55, 59),
    GradeScaleEntry("D", 1.0, 50, 54),
    GradeScaleEntry("FX", 0.0, 30, 49),
    GradeScaleEntry("F", 0.0, 0, 29),
)


def total_to_letter_gpa(total: float) -> tuple[str, float]:
    """Map a Total score (0-100) to (letter grade, GPA) per the AITU scale.

    Scale bounds are integers while totals may be fractional (e.g. 79.4),
    so the first entry whose lower bound the total reaches wins.
    """
    for entry in GRADE_SCALE:
        if total >= entry.min_total:
            return entry.letter, entry.gpa
    return "F", 0.0


@dataclass(frozen=True)
class AdmissionStatus:
    admitted: bool
    register_term: float | None  # (midterm + endterm) / 2
    problems: list[str]  # e.g. "РК Midterm 22 < 25"


ADMISSION_RK_MIN = 25.0  # each register control must be >= 25
ADMISSION_TERM_MIN = 50.0  # Register Term average must be >= 50


def check_admission(midterm: float | None, endterm: float | None) -> AdmissionStatus:
    """Check exam admission per AITU rules: RK1 >= 25, RK2 >= 25, Term >= 50."""
    problems: list[str] = []
    values = [v for v in (midterm, endterm) if v is not None]
    register_term = (
        round(sum(values) / len(values), 2) if len(values) == 2 else None
    )
    if midterm is not None and midterm < ADMISSION_RK_MIN:
        problems.append(f"РК Midterm {_fmt_admission_val(midterm)} < 25")
    if endterm is not None and endterm < ADMISSION_RK_MIN:
        problems.append(f"РК Endterm {_fmt_admission_val(endterm)} < 25")
    if register_term is not None and register_term < ADMISSION_TERM_MIN:
        problems.append(f"Register Term {_fmt_admission_val(register_term)} < 50")
    return AdmissionStatus(
        admitted=not problems,
        register_term=register_term,
        problems=problems,
    )


def _fmt_admission_val(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True)
class TargetThreshold:
    label: str
    target_score: float
    required_final: float
    effective_final: float  # at least 50 if target is achievable
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
    effective_final_70: float  # max(50, required_final_70) if <= 100
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


def classify_final_exam_outcome(score: float) -> str:
    """Classify final exam score according to AITU rules:
    0 - 24: летник (summer school / fail)
    25 - 49: пересдача файнала (retake)
    50 - 100: файнал сдан (passed)
    """
    if score < 24.999:
        return "летник"
    if score < 49.999:
        return "пересдача"
    return "сдан"


def effective_final_for_scholarship(required_final: float) -> float:
    """Return the practical minimum score needed on the final exam.

    Even if mathematically required_final < 50, a student must score at least 50
    on the final to pass without retake or летник.
    """
    return max(50.0, required_final)


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
    effective_final_70 = (
        round(effective_final_for_scholarship(req_final_70), 2)
        if achievable_70
        else 100.0
    )

    target_defs = [
        ("Стипендия (C+)", 70.0),
        ("Оценка B", 80.0),
        ("Оценка B+", 85.0),
        ("Повышенная стипендия (A-)", 90.0),
    ]

    targets: list[TargetThreshold] = []
    for label, target_val in target_defs:
        req = calculate_required_final(midterm, endterm, target_val)
        eff = effective_final_for_scholarship(req) if req <= 100.0 else 100.0
        targets.append(
            TargetThreshold(
                label=label,
                target_score=target_val,
                required_final=round(req, 2),
                effective_final=round(eff, 2),
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
        effective_final_70=effective_final_70,
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
        outcome = classify_final_exam_outcome(final_exam)
        if outcome == "летник":
            status = f"<b>Летник</b> · финал {final_exam:.1f} · итог {total:.1f}"
        elif outcome == "пересдача":
            status = f"<b>Пересдача</b> · финал {final_exam:.1f} · итог {total:.1f}"
        elif total >= 70.0:
            status = f"<b>Сдан</b> · итог {total:.2f} · финал {final_exam:.2f}"
        else:
            status = f"<b>Итог ниже 70</b> · итог {total:.2f} · финал {final_exam:.2f}"

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
        admission = check_admission(midterm, endterm)
        if not admission.admitted:
            status = (
                "<b>⚠ Не допущен к экзамену — летник</b> · "
                + escape("; ".join(admission.problems))
            )
            return CourseScholarshipAnalysis(
                course_id=course_id,
                course_name=course_name,
                midterm=midterm,
                endterm=endterm,
                final_exam=final_exam,
                current_total=total,
                has_full_term=True,
                result=None,
                status_text=status,
            )
        result = analyze_scholarship(midterm, endterm)
        req = result.required_final_70
        if req <= 50.0:
            status = f"<b>На финале нужно от 50</b> (расчёт: {req:.1f})"
        elif req <= 80.0:
            status = f"<b>На финале нужно от {math.ceil(req)}</b> (расчёт: {req:.2f})"
        elif req <= 100.0:
            status = f"<b>На финале нужно от {math.ceil(req)}</b> (расчёт: {req:.2f})"
        else:
            status = "<b>Стипендия недостижима</b> · для сдачи финала нужно от 50"

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
        status = "<i>Ожидается Endterm</i>"
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
        status_text="<i>Баллы ещё не выставлены</i>",
    )


def format_manual_scholarship(res: ManualScholarshipResult) -> str:
    """Show the decision first, followed by a short, checkable calculation."""
    required = res.required_final_70
    lines = ["<b>Стипендия · ручной расчёт</b>"]
    if required <= 50:
        lines.append("<b>Цель: от 50 баллов на финале.</b>")
        lines.append("Этого достаточно для сдачи финала и итоговых 70.")
    elif required <= 100:
        lines.append(f"<b>Цель: от {math.ceil(required)} баллов на финале.</b>")
        lines.append(f"Точный расчётный минимум: {required:.2f}.")
    else:
        lines.append("<b>Итог 70 недостижим.</b>")
        lines.append(f"Даже при 100 за финал итог составит {res.max_possible_total:.2f}.")

    lines.extend([
        "",
        "<b>Ваши баллы</b>",
        f"Midterm: {res.midterm:.1f} × 30% = {res.midterm_weighted:.2f}",
        f"Endterm: {res.endterm:.1f} × 30% = {res.endterm_weighted:.2f}",
        f"До финала: <b>{res.current_points:.2f} / 60</b>",
        "",
        "Финал: 50–100 — сдан · 25–49 — пересдача · 0–24 — летник.",
        "Итог = Midterm × 30% + Endterm × 30% + Final × 40%.",
    ])
    return "\n".join(lines)

def format_courses_scholarship_summary(
    analyses: list[CourseScholarshipAnalysis],
) -> str:
    """Format full summary of scholarship chances across all enrolled courses."""
    if not analyses:
        return (
            "<b>Стипендия по предметам</b>\n\n"
            "Пока нет курсов с оценками для расчёта. Проверьте Moodle или введите баллы вручную:\n"
            "<code>/scholarship [midterm] [endterm]</code>"
        )

    lines = [
        "<b>Стипендия по предметам</b>",
        "Для стипендии итог по каждому предмету должен быть не ниже <b>70</b>.",
        "Итог = Midterm × 30% + Endterm × 30% + Final × 40%.",
    ]

    completed_courses = [
        a for a in analyses
        if a.final_exam is not None and a.final_exam > 0 and a.current_total is not None
    ]
    calculated_courses = [
        a for a in analyses
        if a.has_full_term and a.result and a not in completed_courses
    ]
    blocked_courses = [
        a for a in analyses
        if a.has_full_term and a.result is None and a not in completed_courses
    ]
    in_progress = [a for a in analyses if not a.has_full_term]

    if blocked_courses:
        lines.append("\n<b>Не допущены к экзамену</b>")
        for a in blocked_courses:
            c_name = escape(a.course_name.split("|")[0].strip())
            lines.append(f"• <b>{c_name}</b> — {a.status_text}")

    if calculated_courses:
        lines.append("\n<b>Расчёт перед финалом</b>")
        for idx, a in enumerate(calculated_courses, start=1):
            res = a.result
            assert res is not None
            c_name = escape(a.course_name.split("|")[0].strip())
            lines.append(f"\n<b>{idx}. {c_name}</b>")
            lines.append(
                f"Midterm <b>{a.midterm:.1f}</b> · Endterm <b>{a.endterm:.1f}</b>"
            )
            lines.append(f"Набрано до финала: <b>{res.current_points:.1f} / 60</b>")
            req = res.required_final_70
            if req <= 50.0:
                lines.append(
                    "Финал: <b>не ниже 50</b> — это минимум для сдачи и этого достаточно для итоговых 70."
                )
            elif req <= 100.0:
                lines.append(
                    f"Чтобы получить итог 70, набери на финале <b>не меньше {math.ceil(req)}</b>. "
                    f"Расчётный минимум: {req:.2f}."
                )
            else:
                lines.append(
                    f"Стипендия недостижима: даже со 100 за финал итог будет "
                    f"<b>{res.max_possible_total:.1f}</b>. Для сдачи предмета нужно не ниже 50 за финал."
                )

    if completed_courses:
        lines.append("\n<b>Финал уже выставлен</b>")
        for a in completed_courses:
            c_name = escape(a.course_name.split("|")[0].strip())
            lines.append(f"• <b>{c_name}</b> — {a.status_text}")

    if in_progress:
        lines.append("\n<b>Ожидаются оценки</b>")
        for a in in_progress:
            c_name = escape(a.course_name.split("|")[0].strip())
            if a.midterm is not None:
                lines.append(
                    f"• <b>{c_name}</b> — Midterm <b>{a.midterm:.1f}</b>; {a.status_text}"
                )
            else:
                lines.append(f"• <b>{c_name}</b>: {a.status_text}")

    lines.extend(
        [
            "\n<b>Порог финала:</b> 50–100 — сдан · 25–49 — пересдача · 0–24 — летник.",
            "Ручной расчёт: <code>/scholarship 75 80</code> (Midterm Endterm).",
            "Стипендия сохраняется, если итог по <b>каждому</b> предмету не ниже 70.",
        ]
    )

    return "\n".join(lines)
