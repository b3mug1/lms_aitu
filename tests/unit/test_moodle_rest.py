from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.config import Settings
from app.lms.base import LMSAuthError, LMSUnavailableError
from app.lms.moodle_rest import MoodleRestClient, flatten_moodle_params


def test_flatten_moodle_params() -> None:
    flat = flatten_moodle_params({"courseids": [10, 20], "options": {"active": True}})
    assert flat == {
        "courseids[0]": "10",
        "courseids[1]": "20",
        "options[active]": "1",
    }


@pytest.mark.asyncio
async def test_moodle_rest_fetches_courses_assignments_quizzes_and_grades() -> None:
    now = datetime.now(timezone.utc)
    due_assign = int((now + timedelta(days=2)).timestamp())
    due_quiz = int((now + timedelta(hours=10)).timestamp())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode("utf-8"))
        if request.url.path == "/login/token.php":
            assert body["username"] == ["student1"]
            return httpx.Response(200, json={"token": "ws_token_abc"})

        assert request.url.path == "/webservice/rest/server.php"
        fn = body["wsfunction"][0]
        calls.append(fn)

        if fn == "core_webservice_get_site_info":
            return httpx.Response(200, json={"userid": 77, "fullname": "Ivan Ivanov"})
        if fn == "core_enrol_get_users_courses":
            return httpx.Response(
                200,
                json=[{"id": 10, "fullname": "Algorithms", "shortname": "ALG-101"}],
            )
        if fn == "mod_assign_get_assignments":
            return httpx.Response(
                200,
                json={
                    "courses": [
                        {
                            "id": 10,
                            "fullname": "Algorithms",
                            "assignments": [
                                {
                                    "id": 501,
                                    "cmid": 901,
                                    "name": "Lab 1",
                                    "intro": "<p>Solve <b>3</b> tasks</p>",
                                    "duedate": due_assign,
                                    "allowsubmissionsfromdate": 0,
                                }
                            ],
                        }
                    ]
                },
            )
        if fn == "mod_assign_get_submission_status":
            return httpx.Response(
                200, json={"lastattempt": {"submission": {"status": "submitted"}}}
            )
        if fn == "core_calendar_get_action_events_by_timesort":
            return httpx.Response(
                200,
                json={
                    "events": [
                        # Same assign should be deduplicated
                        {
                            "id": 1,
                            "modulename": "assign",
                            "instance": 501,
                            "name": "Lab 1 is due",
                            "timesort": due_assign,
                            "url": "https://lms.example.edu/mod/assign/view.php?id=901",
                            "course": {"id": 10, "fullname": "Algorithms"},
                        },
                        # Quiz should be added
                        {
                            "id": 2,
                            "modulename": "quiz",
                            "instance": 602,
                            "name": "Week 4 Quiz",
                            "timesort": due_quiz,
                            "url": "https://lms.example.edu/mod/quiz/view.php?id=902",
                            "course": {"id": 10, "fullname": "Algorithms"},
                        },
                    ]
                },
            )
        if fn == "gradereport_user_get_grade_items":
            return httpx.Response(
                200,
                json={
                    "usergrades": [
                        {
                            "userid": 77,
                            "gradeitems": [
                                {
                                    "id": 1001,
                                    "itemname": "Lab 1",
                                    "itemtype": "mod",
                                    "itemmodule": "assign",
                                    "iteminstance": 501,
                                    "cmid": 901,
                                    "graderaw": 95.0,
                                    "gradeformatted": "95.00",
                                    "grademax": 100.0,
                                    "percentageformatted": "95.00 %",
                                    "feedback": "<p>Great work</p>",
                                },
                                {
                                    "id": 1002,
                                    "itemname": "Pending HW",
                                    "itemtype": "mod",
                                    "itemmodule": "assign",
                                    "graderaw": None,
                                    "gradeformatted": "-",
                                },
                                {
                                    "id": 1003,
                                    "itemname": "Course total",
                                    "itemtype": "course",
                                    "graderaw": 95.0,
                                    "gradeformatted": "95.00",
                                },
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(404, text="unknown")

    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:ABC",
        telegram_chat_id=1,
        moodle_base_url="https://lms.example.edu",
        moodle_auth_mode="rest",
        moodle_username="student1",
        moodle_password="secret",
    )
    async_client = httpx.AsyncClient(
        base_url="https://lms.example.edu",
        transport=httpx.MockTransport(handler),
    )
    client = MoodleRestClient(settings, http_client=async_client)
    try:
        snapshot = await client.fetch_snapshot()
    finally:
        await async_client.aclose()

    assert snapshot.mode == "rest"
    assert snapshot.user_fullname == "Ivan Ivanov"
    assert len(snapshot.courses) == 1
    assert snapshot.courses[0].name == "Algorithms"

    by_id = {a.id: a for a in snapshot.assignments}
    assert set(by_id.keys()) == {"assign:901", "quiz:902"}
    assert by_id["assign:901"].submitted is True
    assert by_id["assign:901"].description == "Solve 3 tasks"
    assert by_id["quiz:902"].title == "Week 4 Quiz"

    # Only Lab 1 graded item; ungraded '-' and 'course' total skipped
    assert len(snapshot.grades) == 1
    assert snapshot.grades[0].assignment_id == "assign:901"
    assert snapshot.grades[0].grade_raw == 95.0
    assert snapshot.grades[0].feedback == "Great work"
    assert "core_webservice_get_site_info" in calls


@pytest.mark.asyncio
async def test_moodle_rest_raises_unavailable_when_ws_disabled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": "Web services must be enabled", "errorcode": "enablewsdescription"},
        )

    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:ABC",
        telegram_chat_id=1,
        moodle_base_url="https://lms.example.edu",
        moodle_auth_mode="auto",
        moodle_username="student1",
        moodle_password="secret",
    )
    async_client = httpx.AsyncClient(
        base_url="https://lms.example.edu",
        transport=httpx.MockTransport(handler),
    )
    client = MoodleRestClient(settings, http_client=async_client)
    try:
        with pytest.raises(LMSUnavailableError):
            await client.fetch_snapshot()
    finally:
        await async_client.aclose()


@pytest.mark.asyncio
async def test_moodle_rest_raises_auth_error_on_invalid_login() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": "Invalid login", "errorcode": "invalidlogin"},
        )

    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:ABC",
        telegram_chat_id=1,
        moodle_base_url="https://lms.example.edu",
        moodle_auth_mode="rest",
        moodle_username="student1",
        moodle_password="wrong",
    )
    async_client = httpx.AsyncClient(
        base_url="https://lms.example.edu",
        transport=httpx.MockTransport(handler),
    )
    client = MoodleRestClient(settings, http_client=async_client)
    try:
        with pytest.raises(LMSAuthError):
            await client.fetch_snapshot()
    finally:
        await async_client.aclose()


@pytest.mark.asyncio
async def test_moodle_rest_raises_unavailable_on_403_server_php() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html><title>403 Forbidden</title></html>")

    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:ABC",
        telegram_chat_id=1,
        moodle_base_url="https://lms.astanait.edu.kz",
        moodle_auth_mode="auto",
        moodle_token="some_token",
    )
    async_client = httpx.AsyncClient(
        base_url="https://lms.astanait.edu.kz",
        transport=httpx.MockTransport(handler),
    )
    client = MoodleRestClient(settings, http_client=async_client)
    try:
        with pytest.raises(LMSUnavailableError):
            await client.fetch_snapshot()
    finally:
        await async_client.aclose()
