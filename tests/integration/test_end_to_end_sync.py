from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.config import Settings
from app.lms.client import AutoMoodleClient
from app.lms.moodle_rest import MoodleRestClient
from app.lms.moodle_web import MoodleWebClient
from app.notifications.service import SyncService
from app.storage.repository import LMSRepository


class RecordingSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, text: str) -> None:
        self.messages.append(text)


@pytest.mark.asyncio
async def test_auto_fallback_from_rest_to_web_and_db_queries(db_session_factory) -> None:
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    poll_num = 0

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # REST WS disabled on this university Moodle
        if path == "/login/token.php":
            return httpx.Response(
                200,
                json={"error": "Web services disabled", "errorcode": "enablewsdescription"},
            )
        if path == "/login/index.php" and request.method == "GET":
            return httpx.Response(
                200,
                text='<form id="login"><input type="hidden" name="logintoken" value="t1"></form>',
            )
        if path == "/login/index.php" and request.method == "POST":
            return httpx.Response(200, text='<script>M.cfg={"sesskey":"s1"};</script>')
        if path == "/my/":
            return httpx.Response(
                200,
                text='<script>M.cfg={"sesskey":"s1"};</script>'
                '<span class="usertext">Daniyar S</span>'
                '<a href="/course/view.php?id=101">Machine Learning</a>',
            )
        if path == "/lib/ajax/service.php":
            return httpx.Response(200, json=[{"error": True}])
        if path == "/course/view.php":
            if poll_num == 1:
                return httpx.Response(
                    200,
                    text='<li class="activity"><a href="/mod/assign/view.php?id=801">Lab 1</a></li>',
                )
            return httpx.Response(
                200,
                text='<li class="activity"><a href="/mod/assign/view.php?id=801">Lab 1</a></li>'
                '<li class="activity"><a href="/mod/assign/view.php?id=802">Lab 2</a></li>',
            )
        if path == "/mod/assign/view.php":
            cmid = request.url.params.get("id")
            if cmid == "801":
                return httpx.Response(
                    200,
                    text='<table class="generaltable">'
                    "<tr><td>Submission status</td><td>Submitted for grading</td></tr>"
                    "<tr><td>Due date</td><td>13 November 2026, 23:59</td></tr>"
                    "</table>",
                )
            return httpx.Response(
                200,
                text='<table class="generaltable">'
                "<tr><td>Submission status</td><td>No attempt</td></tr>"
                "<tr><td>Due date</td><td>15 November 2026, 23:59</td></tr>"
                "</table>",
            )
        if path == "/grade/report/user/index.php":
            if poll_num == 1:
                return httpx.Response(200, text='<table class="user-grade"><tbody></tbody></table>')
            return httpx.Response(
                200,
                text='<table class="user-grade"><tbody>'
                '<tr><th class="column-itemname"><a href="/mod/assign/view.php?id=801">Lab 1</a></th>'
                '<td class="column-grade">98</td><td class="column-range">0–100</td></tr>'
                "</tbody></table>",
            )
        return httpx.Response(404, text="missing")

    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:ABC",
        telegram_chat_id=1,
        moodle_base_url="https://lms.example.edu",
        moodle_auth_mode="auto",
        moodle_username="daniyar",
        moodle_password="pw",
        timezone="Asia/Almaty",
        deadline_reminder_hours=[120, 72, 48, 24, 12, 3],
    )
    mock_http = httpx.AsyncClient(
        base_url="https://lms.example.edu",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    auto_client = AutoMoodleClient(
        settings,
        rest_client=MoodleRestClient(settings, http_client=mock_http),
        web_client=MoodleWebClient(settings, http_client=mock_http),
    )
    sender = RecordingSender()
    sync_service = SyncService(
        settings,
        auto_client,
        db_session_factory,
        sender,
        clock=lambda: now,
    )

    try:
        # Poll 1: auto-falls back from REST -> Web, records Lab 1 silently
        poll_num = 1
        rep1 = await sync_service.sync_once()
        assert rep1.ok is True
        assert auto_client.mode_name == "web"
        assert rep1.is_bootstrap is True

        # Poll 2: Lab 2 added + Lab 1 graded -> 2 notifications + 5d deadline reminder for Lab 2
        poll_num = 2
        sender.messages.clear()
        rep2 = await sync_service.sync_once()
        assert rep2.ok is True
        assert rep2.new_assignments == 1
        assert rep2.new_grades == 1
        assert rep2.deadline_reminders == 1  # Lab 2 due Nov 15 -> <= 5 days
        assert len(sender.messages) == 3

        async with db_session_factory() as session:
            repo = LMSRepository(session)
            courses = await repo.list_courses()
            upcoming = await repo.list_upcoming_deadlines(now, lookahead_days=30, include_submitted=False)
            grades = await repo.list_recent_grades()
            assert len(courses) == 1
            assert [u.title for u in upcoming] == ["Lab 2"]  # Lab 1 is submitted
            assert len(grades) == 1
            assert grades[0].grade_formatted == "98"
    finally:
        await mock_http.aclose()
