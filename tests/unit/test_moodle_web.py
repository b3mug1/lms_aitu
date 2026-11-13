from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.core.config import Settings
from app.lms.base import CourseDTO, LMSAuthError
from app.lms.moodle_web import (
    MoodleWebClient,
    is_login_page,
    parse_assign_detail_html,
    parse_course_activities_html,
    parse_gradebook_html,
)

ALMATY_TZ = ZoneInfo("Asia/Almaty")


def test_parse_course_activities_html_strips_accesshide_and_parses_russian_date() -> None:
    html = """
    <ul>
      <li class="activity modtype_assign">
        <a href="https://lms.example.edu/mod/assign/view.php?id=321">
          <span class="instancename">Лабораторная работа 2 <span class="accesshide">Задание</span></span>
        </a>
        <div class="activity-dates">
          <div><strong>Открыто:</strong> 10 ноября 2026, 09:00</div>
          <div><strong>Срок сдачи:</strong> 15 ноября 2026, 23:59</div>
        </div>
      </li>
    </ul>
    """
    course = CourseDTO(id=12, name="Databases")
    items = parse_course_activities_html(html, course, "https://lms.example.edu", ALMATY_TZ)
    assert len(items) == 1
    assert items[0].id == "assign:321"
    assert items[0].title == "Лабораторная работа 2"
    # 15 Nov 2026 23:59 Asia/Almaty (UTC+5) == 18:59 UTC
    assert items[0].due_at == datetime(2026, 11, 15, 18, 59, tzinfo=timezone.utc)


def test_parse_assign_detail_html_english_and_russian() -> None:
    html = """
    <div id="intro"><p>Upload PDF report.</p></div>
    <table class="generaltable">
      <tr>
        <td class="cell c0">Состояние ответа</td>
        <td class="cell c1 submissionstatussubmitted">Сдано для оценивания</td>
      </tr>
      <tr>
        <td class="cell c0">Последний срок сдачи</td>
        <td class="cell c1">20 ноября 2026, 18:00</td>
      </tr>
    </table>
    """
    due_at, submitted, desc = parse_assign_detail_html(html, ALMATY_TZ)
    assert submitted is True
    assert due_at == datetime(2026, 11, 20, 13, 0, tzinfo=timezone.utc)
    assert desc == "Upload PDF report."


def test_parse_gradebook_html_skips_empty_and_course_total() -> None:
    html = """
    <table class="user-grade">
      <tbody>
        <tr class="category">
          <th class="column-itemname">Assignments</th>
          <td class="column-grade">-</td>
        </tr>
        <tr>
          <th class="column-itemname">
            <a href="https://lms.example.edu/mod/assign/view.php?id=321">Lab 2</a>
          </th>
          <td class="column-grade">88,5</td>
          <td class="column-range">0–100</td>
          <td class="column-percentage">88.50 %</td>
          <td class="column-feedback">Good</td>
        </tr>
        <tr>
          <th class="column-itemname">
            <a href="https://lms.example.edu/mod/quiz/view.php?id=555">Quiz 1</a>
          </th>
          <td class="column-grade">-</td>
          <td class="column-range">0–20</td>
        </tr>
        <tr class="courseitem">
          <th class="column-itemname baggb">Course total</th>
          <td class="column-grade">88,5</td>
        </tr>
      </tbody>
    </table>
    """
    course = CourseDTO(id=12, name="Databases")
    grades = parse_gradebook_html(html, course, "https://lms.example.edu")
    assert len(grades) == 1
    assert grades[0].id == "12:assign:321"
    assert grades[0].assignment_id == "assign:321"
    assert grades[0].grade_raw == 88.5
    assert grades[0].grade_max == 100.0
    assert grades[0].feedback == "Good"


@pytest.mark.asyncio
async def test_moodle_web_login_and_scrape_flow() -> None:
    logged_in = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal logged_in
        path = request.url.path

        if path == "/my/" and not logged_in:
            return httpx.Response(
                302,
                headers={"Location": "https://lms.example.edu/login/index.php"},
            )
        if path == "/login/index.php" and request.method == "GET":
            return httpx.Response(
                200,
                text='<form id="login"><input type="hidden" name="logintoken" value="tok99"></form>',
            )
        if path == "/login/index.php" and request.method == "POST":
            logged_in = True
            return httpx.Response(
                200,
                text='<script>M.cfg = {"sesskey":"sess_xyz"};</script>'
                '<span class="usertext">Aruzhan K</span>'
                '<a href="https://lms.example.edu/course/view.php?id=5">Software Engineering</a>',
            )
        if path == "/my/" and logged_in:
            return httpx.Response(
                200,
                text='<script>M.cfg = {"sesskey":"sess_xyz"};</script>'
                '<span class="usertext">Aruzhan K</span>'
                '<a href="https://lms.example.edu/course/view.php?id=5">Software Engineering</a>',
            )
        if path == "/lib/ajax/service.php":
            return httpx.Response(200, json=[{"error": True}])
        if path == "/course/view.php":
            return httpx.Response(
                200,
                text="""
                <ul>
                  <li class="activity modtype_assign">
                    <a href="/mod/assign/view.php?id=700">Project Proposal</a>
                  </li>
                </ul>
                """,
            )
        if path == "/mod/assign/view.php":
            return httpx.Response(
                200,
                text="""
                <table class="generaltable">
                  <tr><td>Submission status</td><td>No attempt</td></tr>
                  <tr><td>Due date</td><td>14 November 2026, 11:59 PM</td></tr>
                </table>
                """,
            )
        if path == "/grade/report/user/index.php":
            return httpx.Response(
                200,
                text="""
                <table class="user-grade">
                  <tbody>
                    <tr>
                      <th class="column-itemname"><a href="/mod/assign/view.php?id=700">Project Proposal</a></th>
                      <td class="column-grade">100</td>
                      <td class="column-range">0–100</td>
                      <td class="column-percentage">100 %</td>
                    </tr>
                  </tbody>
                </table>
                """,
            )
        return httpx.Response(404, text="not found")

    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:ABC",
        telegram_chat_id=1,
        moodle_base_url="https://lms.example.edu",
        moodle_auth_mode="web",
        moodle_username="aruzhan",
        moodle_password="pw",
        timezone="Asia/Almaty",
    )
    async_client = httpx.AsyncClient(
        base_url="https://lms.example.edu",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    client = MoodleWebClient(settings, http_client=async_client)
    try:
        snapshot = await client.fetch_snapshot()
    finally:
        await async_client.aclose()

    assert snapshot.mode == "web"
    assert snapshot.user_fullname == "Aruzhan K"
    assert len(snapshot.courses) == 1
    assert len(snapshot.assignments) == 1
    assert snapshot.assignments[0].due_at == datetime(2026, 11, 14, 18, 59, tzinfo=timezone.utc)
    assert snapshot.assignments[0].submitted is False
    assert len(snapshot.grades) == 1
    assert snapshot.grades[0].grade_raw == 100.0


@pytest.mark.asyncio
async def test_moodle_web_raises_auth_error_on_bad_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/index.php" and request.method == "POST":
            return httpx.Response(
                200,
                text='<div class="loginerrors">Invalid login, please try again</div><form id="login"></form>',
            )
        return httpx.Response(
            200,
            text='<form id="login"><input type="hidden" name="logintoken" value="t1"></form>',
        )

    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:ABC",
        telegram_chat_id=1,
        moodle_base_url="https://lms.example.edu",
        moodle_auth_mode="web",
        moodle_username="bad",
        moodle_password="bad",
    )
    async_client = httpx.AsyncClient(
        base_url="https://lms.example.edu",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    client = MoodleWebClient(settings, http_client=async_client)
    try:
        with pytest.raises(LMSAuthError):
            await client.fetch_snapshot()
    finally:
        await async_client.aclose()


def test_is_login_page_detects_oidc_and_microsoft_sso_redirect() -> None:
    assert is_login_page(
        "<html><body>Sign in</body></html>",
        "https://login.microsoftonline.com/tenant/oauth2/authorize",
        "https://lms.astanait.edu.kz",
    )
    assert is_login_page(
        "<html><body>Redirecting</body></html>",
        "https://lms.astanait.edu.kz/auth/oidc/?source=loginpage",
        "https://lms.astanait.edu.kz",
    )


def test_parse_gradebook_html_strips_moodle4_action_menu_and_type_badges() -> None:
    html = """
    <table class="user-grade">
      <tbody>
        <tr>
          <th class="level2 item column-itemname">
            <div class="item">
              <span class="d-block text-uppercase small">Assignment</span>
              <div class="rowtitle">
                <a class="gradeitemheader" href="https://lms.astanait.edu.kz/mod/assign/view.php?id=71400">Endterm</a>
              </div>
            </div>
          </th>
          <td class="column-grade">
            <div class="d-flex">
              <div>-</div>
              <div class="action-menu moodle-actionmenu">
                <a href="#">Actions</a>
                <div class="dropdown-menu"><a href="#">Grade analysis</a></div>
              </div>
            </div>
          </td>
          <td class="column-range">0–100</td>
        </tr>
        <tr>
          <th class="level2 item column-itemname">
            <div class="item">
              <span class="d-block text-uppercase small">Assignment</span>
              <div class="rowtitle">
                <a class="gradeitemheader" href="https://lms.astanait.edu.kz/mod/assign/view.php?id=71399">Midterm</a>
              </div>
            </div>
          </th>
          <td class="column-grade">
            <div class="d-flex">
              <div>91.50</div>
              <div class="action-menu moodle-actionmenu">
                <span class="visually-hidden">Actions</span>
                <div class="dropdown-menu"><a href="#">Grade analysis</a></div>
              </div>
            </div>
          </td>
          <td class="column-range">0–100</td>
          <td class="column-percentage">91.50 %</td>
        </tr>
        <tr>
          <th class="level1 item column-itemname">
            <div class="rowtitle"><span class="gradeitemheader">Course total</span></div>
          </th>
          <td class="column-grade">91.50</td>
        </tr>
      </tbody>
    </table>
    """
    course = CourseDTO(id=2154, name="Cloud Computing")
    grades = parse_gradebook_html(html, course, "https://lms.astanait.edu.kz")
    assert len(grades) == 1
    assert grades[0].item_name == "Midterm"
    assert grades[0].grade_formatted == "91.50"
    assert grades[0].grade_raw == 91.5
