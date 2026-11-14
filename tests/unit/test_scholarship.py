from __future__ import annotations

import pytest
from app.lms.base import GradeDTO
from app.scholarship import (
    analyze_course_scholarship,
    analyze_scholarship,
    calculate_required_final,
    classify_final_exam_outcome,
    effective_final_for_scholarship,
    extract_course_registers,
    format_courses_scholarship_summary,
    format_manual_scholarship,
)


def test_classify_final_exam_outcome() -> None:
    # 0 - 24: летник
    assert classify_final_exam_outcome(0.0) == "летник"
    assert classify_final_exam_outcome(15.5) == "летник"
    assert classify_final_exam_outcome(24.0) == "летник"
    assert classify_final_exam_outcome(24.9) == "летник"

    # 25 - 49: пересдача
    assert classify_final_exam_outcome(25.0) == "пересдача"
    assert classify_final_exam_outcome(35.0) == "пересдача"
    assert classify_final_exam_outcome(49.0) == "пересдача"
    assert classify_final_exam_outcome(49.9) == "пересдача"

    # 50 - 100: сдан
    assert classify_final_exam_outcome(50.0) == "сдан"
    assert classify_final_exam_outcome(75.0) == "сдан"
    assert classify_final_exam_outcome(100.0) == "сдан"


def test_effective_final_for_scholarship() -> None:
    # If mathematically 40, must still score at least 50
    assert effective_final_for_scholarship(40.0) == 50.0
    assert effective_final_for_scholarship(-10.0) == 50.0
    # If mathematically 65, needed is 65
    assert effective_final_for_scholarship(65.0) == 65.0


def test_calculate_required_final_exact() -> None:
    # 75 * 0.3 + 80 * 0.3 = 22.5 + 24 = 46.5
    # Needed: 70 - 46.5 = 23.5
    # Required final: 23.5 / 0.4 = 58.75
    res = calculate_required_final(75.0, 80.0, 70.0)
    assert pytest.approx(res, rel=1e-4) == 58.75


def test_calculate_required_final_easy() -> None:
    # 90 * 0.3 + 90 * 0.3 = 27 + 27 = 54
    # Needed: 70 - 54 = 16
    # Required final: 16 / 0.4 = 40.0
    res = calculate_required_final(90.0, 90.0, 70.0)
    assert pytest.approx(res, rel=1e-4) == 40.0


def test_calculate_required_final_unachievable() -> None:
    # 40 * 0.3 + 40 * 0.3 = 12 + 12 = 24
    # Needed: 70 - 24 = 46
    # Required final: 46 / 0.4 = 115.0
    res = calculate_required_final(40.0, 40.0, 70.0)
    assert pytest.approx(res, rel=1e-4) == 115.0


def test_analyze_scholarship_targets() -> None:
    result = analyze_scholarship(80.0, 85.0)
    # Current points: 24 + 25.5 = 49.5
    assert result.midterm == 80.0
    assert result.endterm == 85.0
    assert pytest.approx(result.current_points) == 49.5
    # Target 70: (70 - 49.5) / 0.4 = 20.5 / 0.4 = 51.25
    assert pytest.approx(result.required_final_70, rel=1e-3) == 51.25
    assert result.achievable_70 is True

    # Check targets (70, 80, 85, 90)
    target_70 = next(t for t in result.targets if t.target_score == 70.0)
    assert target_70.achievable is True
    assert pytest.approx(target_70.required_final, rel=1e-3) == 51.25

    formatted = format_manual_scholarship(result)
    assert "<b>Стипендия · ручной расчёт</b>" in formatted
    assert "80.0" in formatted
    assert "85.0" in formatted
    assert "51.2" in formatted or "51.3" in formatted
    assert "летник" in formatted.lower()
    assert "пересдача" in formatted.lower()


def test_extract_course_registers_exact() -> None:
    grades = [
        GradeDTO(
            id="1", course_id=1, item_name="Register Midterm", grade_raw=88.4, grade_formatted="88.40"
        ),
        GradeDTO(
            id="2", course_id=1, item_name="Register Endterm", grade_raw=93.6, grade_formatted="93.60"
        ),
        GradeDTO(
            id="3", course_id=1, item_name="Register Term", grade_raw=91.0, grade_formatted="91.00"
        ),
    ]
    m, e, f, t = extract_course_registers(grades)
    assert m == 88.4
    assert e == 93.6
    assert f is None
    assert t is None


def test_analyze_course_scholarship() -> None:
    grades = [
        GradeDTO(
            id="1", course_id=10, item_name="Register Midterm", grade_raw=70.0, grade_formatted="70.0"
        ),
        GradeDTO(
            id="2", course_id=10, item_name="Register Endterm", grade_raw=75.0, grade_formatted="75.0"
        ),
    ]
    analysis = analyze_course_scholarship(10, "Software Engineering", grades)
    assert analysis.has_full_term is True
    assert analysis.result is not None
    # 70 * 0.3 + 75 * 0.3 = 21 + 22.5 = 43.5
    # (70 - 43.5) / 0.4 = 26.5 / 0.4 = 66.25
    assert pytest.approx(analysis.result.required_final_70, rel=1e-3) == 66.25

    summary = format_courses_scholarship_summary([analysis])
    assert "Software Engineering" in summary
    assert "Midterm" in summary
    assert "Endterm" in summary
    assert "66.2" in summary or "66.3" in summary
    assert "летник" in summary.lower()
    assert "пересдача" in summary.lower()
