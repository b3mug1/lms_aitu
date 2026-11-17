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


def test_total_to_letter_gpa_scale_bounds() -> None:
    from app.scholarship import total_to_letter_gpa

    assert total_to_letter_gpa(95.0) == ("A", 4.0)
    assert total_to_letter_gpa(100.0) == ("A", 4.0)
    assert total_to_letter_gpa(90.0) == ("A-", 3.67)
    assert total_to_letter_gpa(85.0) == ("B+", 3.33)
    assert total_to_letter_gpa(80.0) == ("B", 3.0)
    assert total_to_letter_gpa(75.0) == ("B-", 2.67)
    assert total_to_letter_gpa(70.0) == ("C+", 2.33)
    assert total_to_letter_gpa(65.0) == ("C", 2.0)
    assert total_to_letter_gpa(60.0) == ("C-", 1.67)
    assert total_to_letter_gpa(55.0) == ("D+", 1.33)
    assert total_to_letter_gpa(50.0) == ("D", 1.0)
    assert total_to_letter_gpa(30.0) == ("FX", 0.0)
    assert total_to_letter_gpa(49.0) == ("FX", 0.0)
    assert total_to_letter_gpa(29.0) == ("F", 0.0)
    assert total_to_letter_gpa(0.0) == ("F", 0.0)
    # Fractional totals fall to the scale step they have reached
    assert total_to_letter_gpa(79.4) == ("B-", 2.67)
    assert total_to_letter_gpa(79.99) == ("B-", 2.67)
    assert total_to_letter_gpa(84.5) == ("B", 3.0)
    assert total_to_letter_gpa(49.5) == ("FX", 0.0)


def test_check_admission_thresholds() -> None:
    from app.scholarship import check_admission

    # RK exactly 25 each: passes RK check but Term 25 < 50 -> not admitted
    at_rk_min = check_admission(25.0, 25.0)
    assert at_rk_min.admitted is False
    assert at_rk_min.register_term == 25.0
    assert any("Register Term" in p for p in at_rk_min.problems)

    # Term exactly 50 -> admitted
    assert check_admission(50.0, 50.0).admitted is True

    # RK below 25 -> not admitted, Term average irrelevant
    blocked = check_admission(22.0, 80.0)
    assert blocked.admitted is False
    assert blocked.register_term == 51.0
    assert any("Midterm" in p for p in blocked.problems)
    assert not any("Endterm" in p for p in blocked.problems)

    # Term < 50 despite both RK >= 25
    low_term = check_admission(40.0, 58.0)
    assert low_term.admitted is False
    assert any("Register Term" in p for p in low_term.problems)

    # Missing data: no problems yet
    partial = check_admission(30.0, None)
    assert partial.admitted is True
    assert partial.register_term is None


def test_analyze_course_scholarship_admission_hard_block() -> None:
    grades = [
        GradeDTO(
            id="1", course_id=11, item_name="Register Midterm", grade_raw=22.0, grade_formatted="22.0"
        ),
        GradeDTO(
            id="2", course_id=11, item_name="Register Endterm", grade_raw=80.0, grade_formatted="80.0"
        ),
    ]
    analysis = analyze_course_scholarship(11, "Blocked Course", grades)
    assert "Не допущен" in analysis.status_text
    assert "РК Midterm 22 &lt; 25" in analysis.status_text
    assert analysis.result is None

    summary = format_courses_scholarship_summary([analysis])
    assert "Не допущен" in summary
    assert "нужно от" not in summary  # scholarship calculation is hidden
