from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape
from typing import Sequence

from app.lms.base import GradeDTO
from app.storage.models import GradeModel


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
            status = f"🔴 <b>Летник</b> (Финал: <b>{final_exam:.1f}</b> < 25, Итог: <b>{total:.1f}</b>)"
        elif outcome == "пересдача":
            status = f"🟡 <b>Пересдача</b> (Финал: <b>{final_exam:.1f}</b> [25–49], Итог: <b>{total:.1f}</b>)"
        elif total >= 70.0:
            status = f"✅ <b>Сдан</b> (Итог: <b>{total:.2f}</b>, Финал: <b>{final_exam:.2f}</b>)"
        else:
            status = f"⚠️ <b>Ниже 70</b> (Итог: <b>{total:.2f}</b>, Финал: <b>{final_exam:.2f}</b>)"

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
        if req <= 50.0:
            status = f"🟢 <b>Финал: ≥ 50</b> (матем. {req:.1f}, сдача ≥ 50)"
        elif req <= 80.0:
            status = f"🟡 <b>Финал: ≥ {req:.1f}</b> (цель: {math.ceil(req)})"
        elif req <= 100.0:
            status = f"🟠 <b>Финал: ≥ {req:.1f}</b> (цель: {math.ceil(req)})"
        else:
            status = f"🔴 <b>Стипендия недостижима</b> (сдать финал: ≥ 50)"

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
        f"Формула AITU: <code>(Midterm × 0.3) + (Endterm × 0.3) + (Final × 0.4) ≥ 70</code>\n",
        "📊 <b>Ваши баллы:</b>",
        f"• Регистр Midterm (30%): <b>{res.midterm:.1f}</b> → <b>{res.midterm_weighted:.2f}</b> б.",
        f"• Регистр Endterm (30%): <b>{res.endterm:.1f}</b> → <b>{res.endterm_weighted:.2f}</b> б.",
        f"• Накопленный допуск: <b>{res.current_points:.2f}</b> / 60.0 б.\n",
    ]

    req = res.required_final_70
    if req <= 50.0:
        ceil_math = math.ceil(max(0.0, req))
        total_at_50 = round(res.current_points + 50.0 * 0.4, 2)
        lines.extend(
            [
                "🎯 <b>Сколько нужно набрать на файнале:</b>",
                "🟢 <b>Минимум для сдачи и стипендии: 50 баллов</b>",
                f"• Математически для 70 достаточно: <b>{max(0.0, req):.1f}</b> (цель: {ceil_math}).",
                f"• <b>Но по правилам, чтобы сдать файнал, нужно набрать выше 50 (≥ 50)!</b>",
                f"• При 50 на файнале ваш итог составит <b>{total_at_50:.2f}</b> (порог 70 пройден ✅).\n",
                "⚠️ <b>Внимание к порогам файнала:</b>",
                "• 🟡 <b>25 – 49 баллов:</b> пересдача файнала;",
                "• 🔴 <b>0 – 24 балла:</b> летник (повтор курса).\n",
            ]
        )
    elif req <= 100.0:
        ceil_req = math.ceil(req)
        lines.extend(
            [
                "🎯 <b>Сколько нужно набрать на файнале:</b>",
                f"👉 <b>Для стипендии (итог ≥ 70) нужно:</b> от <b>{req:.1f}</b> из 100 (цель: <b>{ceil_req}</b> б.).",
                f"<i>(При {req:.1f} на файнале: {res.current_points:.2f} + {req * 0.4:.2f} = 70.0)</i>\n",
                "📌 <b>Что будет при разных оценках за файнал:</b>",
                f"• 🟢 <b>{ceil_req} – 100:</b> стипендия сохранена (итог ≥ 70) ✅",
                f"• ⚪ <b>50 – {ceil_req - 1}:</b> файнал сдан, но стипендия потеряна (итог < 70)",
                "• 🟡 <b>25 – 49:</b> <b>пересдача файнала</b>",
                "• 🔴 <b>0 – 24:</b> <b>летник</b> (повторное изучение дисциплины)\n",
            ]
        )
    else:
        lines.extend(
            [
                "🎯 <b>Сколько нужно набрать на файнале:</b>",
                "🔴 <b>Стипендиальный порог 70 недостижим.</b>",
                f"Даже при 100 баллах на файнале итоговый балл составит максимум <b>{res.max_possible_total:.2f}</b>.\n",
                "👉 <b>Чтобы закрыть предмет без долгов:</b>",
                "• 🟢 <b>50 – 100 баллов:</b> файнал сдан;",
                "• 🟡 <b>25 – 49 баллов:</b> <b>пересдача файнала</b>;",
                "• 🔴 <b>0 – 24 балла:</b> <b>летник</b>.\n",
            ]
        )

    lines.append("📈 <b>Ориентиры по оценкам:</b>")
    for t in res.targets:
        if not t.achievable:
            lines.append(f"• <b>{t.label} ({t.target_score:.0f}+)</b>: <i>недостижимо</i>")
        elif t.required_final <= 50.0:
            lines.append(
                f"• <b>{t.label} ({t.target_score:.0f}+)</b>: файнал <b>≥ 50</b> (матем. {max(0.0, t.required_final):.1f})"
            )
        else:
            lines.append(
                f"• <b>{t.label} ({t.target_score:.0f}+)</b>: файнал <b>{t.required_final:.1f}</b> (цель: {math.ceil(t.required_final)})"
            )

    lines.extend(
        [
            "\n📋 <b>Правила сдачи файнала в AITU:</b>",
            "• 🟢 <b>50 – 100 баллов:</b> файнал сдан",
            "• 🟡 <b>25 – 49 баллов:</b> пересдача файнала",
            "• 🔴 <b>0 – 24 балла:</b> летник",
        ]
    )

    return "\n".join(lines)


def format_courses_scholarship_summary(
    analyses: list[CourseScholarshipAnalysis],
) -> str:
    """Format full summary of scholarship chances across all enrolled courses."""
    if not analyses:
        return (
            "🎓 <b>Стипендия по предметам</b>\n\n"
            "Пока нет курсов с оценками для расчёта. Проверьте Moodle или введите баллы вручную:\n"
            "<code>/scholarship [midterm] [endterm]</code>"
        )

    lines = [
        "🎓 <b>Стипендия по предметам</b>",
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
    in_progress = [a for a in analyses if not a.has_full_term]

    if calculated_courses:
        lines.append("\n📊 <b>Расчёт перед финалом</b>")
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
        lines.append("\n✅ <b>Финал уже выставлен</b>")
        for a in completed_courses:
            c_name = escape(a.course_name.split("|")[0].strip())
            lines.append(f"• <b>{c_name}</b> — {a.status_text}")

    if in_progress:
        lines.append("\n⏳ <b>Ожидаются оценки</b>")
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
