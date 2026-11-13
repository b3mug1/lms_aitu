from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import Settings
from app.lms.base import (
    AssignmentDTO,
    CourseDTO,
    GradeDTO,
    LMSAuthError,
    LMSConnectionError,
    LMSError,
    LMSSnapshot,
    LMSUnavailableError,
)
from app.lms.utils import clean_html_text, epoch_to_utc, extract_mod_and_cmid, is_empty_grade

logger = logging.getLogger(__name__)

_WS_UNAVAILABLE_CODES = {
    "enablewsdescription",
    "servicenotavailable",
    "webservicesdisabled",
    "wsaccessdenied",
}


def flatten_moodle_params(params: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten nested dicts/lists into Moodle REST bracket notation."""
    result: dict[str, str] = {}
    for key, value in params.items():
        full_key = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            result.update(flatten_moodle_params(value, full_key))
        elif isinstance(value, (list, tuple)):
            for idx, item in enumerate(value):
                item_key = f"{full_key}[{idx}]"
                if isinstance(item, dict):
                    result.update(flatten_moodle_params(item, item_key))
                else:
                    result[item_key] = str(int(item) if isinstance(item, bool) else item)
        elif isinstance(value, bool):
            result[full_key] = "1" if value else "0"
        else:
            result[full_key] = str(value)
    return result


class MoodleRestClient:
    """Moodle Web Service REST API client using httpx."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = settings.moodle_base_url
        self._token: str | None = (
            settings.moodle_token.get_secret_value() if settings.moodle_token else None
        )
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=settings.request_timeout_seconds,
            verify=settings.ssl_verify,
            follow_redirects=True,
            headers={"User-Agent": "MoodleTelegramNotifier/0.1"},
        )

    @property
    def mode_name(self) -> str:
        return "rest"

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request_json(self, path: str, data: dict[str, str]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._client.post(path, data=data)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise LMSConnectionError(f"Moodle HTTP error: {exc}") from exc

            if response.status_code >= 500 and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            if response.status_code in (404, 403):
                raise LMSUnavailableError(
                    f"Moodle Web Service endpoint returned HTTP {response.status_code} on {path}"
                )

            if response.status_code >= 400:
                raise LMSConnectionError(f"Moodle returned HTTP {response.status_code} on {path}")

            try:
                return response.json()
            except ValueError as exc:
                raise LMSUnavailableError(
                    f"Expected JSON from {path}, got non-JSON response"
                ) from exc

        raise LMSConnectionError(f"Moodle request failed: {last_exc}")

    async def _ensure_token(self, force_refresh: bool = False) -> str:
        if self._token and not force_refresh:
            return self._token

        if not (self._settings.moodle_username and self._settings.moodle_password):
            if self._token and force_refresh:
                raise LMSAuthError("Moodle token is invalid or expired")
            raise LMSAuthError("No Moodle token or credentials configured")

        payload = {
            "username": self._settings.moodle_username,
            "password": self._settings.moodle_password.get_secret_value(),
            "service": self._settings.moodle_service,
        }
        data = await self._request_json("/login/token.php", payload)
        if not isinstance(data, dict):
            raise LMSUnavailableError("Unexpected response from /login/token.php")

        error_code = str(data.get("errorcode", "")).lower()
        if error_code:
            message = data.get("error") or error_code
            if error_code in _WS_UNAVAILABLE_CODES:
                raise LMSUnavailableError(f"Moodle Web Services disabled: {message}")
            if error_code == "invalidlogin":
                raise LMSAuthError(f"Invalid Moodle login or password: {message}")
            raise LMSError(f"Moodle token error ({error_code}): {message}")

        token = data.get("token")
        if not token:
            raise LMSUnavailableError("Moodle /login/token.php returned no token")

        self._token = str(token)
        return self._token

    async def _call_ws(self, wsfunction: str, params: dict[str, Any] | None = None) -> Any:
        token = await self._ensure_token()
        flat_params = flatten_moodle_params(params or {})

        for attempt in range(2):
            form_data = {
                "wstoken": token,
                "wsfunction": wsfunction,
                "moodlewsrestformat": "json",
                **flat_params,
            }
            payload = await self._request_json("/webservice/rest/server.php", form_data)

            if isinstance(payload, dict) and payload.get("exception"):
                error_code = str(payload.get("errorcode", "")).lower()
                message = payload.get("message") or payload.get("exception")
                if error_code == "invalidtoken" and attempt == 0 and self._settings.moodle_username:
                    logger.info("Moodle token expired; refreshing token")
                    token = await self._ensure_token(force_refresh=True)
                    continue
                if error_code == "invalidtoken":
                    raise LMSAuthError(f"Invalid Moodle WS token: {message}")
                if error_code in _WS_UNAVAILABLE_CODES:
                    raise LMSUnavailableError(f"Moodle Web Services unavailable: {message}")
                raise LMSError(f"{wsfunction} failed ({error_code}): {message}")

            return payload

        raise LMSAuthError("Failed to refresh Moodle WS token")

    async def fetch_snapshot(self) -> LMSSnapshot:
        now = datetime.now(timezone.utc)
        site_info = await self._call_ws("core_webservice_get_site_info")
        if not isinstance(site_info, dict) or not site_info.get("userid"):
            raise LMSError("core_webservice_get_site_info did not return userid")

        userid = int(site_info["userid"])
        user_fullname = str(site_info.get("fullname", ""))

        courses = await self._fetch_courses(userid)
        course_map = {c.id: c for c in courses}

        assignments_by_id: dict[str, AssignmentDTO] = {}
        assign_instance_to_key: dict[int, tuple[str, int]] = {}

        await self._populate_mod_assignments(
            course_map=course_map,
            assignments_by_id=assignments_by_id,
            assign_instance_to_key=assign_instance_to_key,
            now=now,
        )
        await self._populate_calendar_events(
            course_map=course_map,
            assignments_by_id=assignments_by_id,
            assign_instance_to_key=assign_instance_to_key,
            now=now,
        )

        grades = await self._fetch_grades(
            userid=userid,
            courses=list(course_map.values()),
            assign_instance_to_key=assign_instance_to_key,
        )

        return LMSSnapshot(
            fetched_at=now,
            mode=self.mode_name,
            user_fullname=user_fullname,
            courses=sorted(course_map.values(), key=lambda c: c.name.lower()),
            assignments=list(assignments_by_id.values()),
            grades=grades,
        )

    async def _fetch_courses(self, userid: int) -> list[CourseDTO]:
        try:
            raw_courses = await self._call_ws(
                "core_enrol_get_users_courses", {"userid": userid}
            )
        except LMSError as exc:
            logger.warning("core_enrol_get_users_courses failed: %s", exc)
            return []

        courses: list[CourseDTO] = []
        if isinstance(raw_courses, list):
            for item in raw_courses:
                cid = item.get("id")
                if not cid:
                    continue
                courses.append(
                    CourseDTO(
                        id=int(cid),
                        name=str(item.get("fullname") or item.get("displayname") or f"Course {cid}"),
                        short_name=str(item.get("shortname") or ""),
                        url=f"{self._base_url}/course/view.php?id={cid}",
                    )
                )
        return courses

    async def _populate_mod_assignments(
        self,
        course_map: dict[int, CourseDTO],
        assignments_by_id: dict[str, AssignmentDTO],
        assign_instance_to_key: dict[int, tuple[str, int]],
        now: datetime,
    ) -> None:
        params: dict[str, Any] = {}
        if course_map:
            params["courseids"] = list(course_map.keys())

        try:
            data = await self._call_ws("mod_assign_get_assignments", params)
        except LMSError as exc:
            logger.warning("mod_assign_get_assignments failed: %s", exc)
            return

        if not isinstance(data, dict):
            return

        upcoming_cutoff = now + timedelta(days=self._settings.lookahead_days)
        submission_checks: list[tuple[int, str]] = []

        for course_data in data.get("courses", []):
            cid = int(course_data["id"])
            if cid not in course_map:
                course_map[cid] = CourseDTO(
                    id=cid,
                    name=str(course_data.get("fullname") or f"Course {cid}"),
                    short_name=str(course_data.get("shortname") or ""),
                    url=f"{self._base_url}/course/view.php?id={cid}",
                )
            course_name = course_map[cid].name

            for raw_assign in course_data.get("assignments", []):
                assign_id = int(raw_assign["id"])
                cmid = int(raw_assign.get("cmid") or assign_id)
                key = f"assign:{cmid}"
                due_at = epoch_to_utc(raw_assign.get("duedate"))
                allow_from = epoch_to_utc(raw_assign.get("allowsubmissionsfromdate"))

                assignments_by_id[key] = AssignmentDTO(
                    id=key,
                    course_id=cid,
                    course_name=course_name,
                    module_type="assign",
                    title=str(raw_assign.get("name") or f"Assignment {cmid}"),
                    description=clean_html_text(raw_assign.get("intro")),
                    url=f"{self._base_url}/mod/assign/view.php?id={cmid}",
                    due_at=due_at,
                    allow_from=allow_from,
                    submitted=False,
                )
                assign_instance_to_key[assign_id] = (key, cid)

                if due_at is not None and now - timedelta(days=1) <= due_at <= upcoming_cutoff:
                    submission_checks.append((assign_id, key))

        await self._check_submission_statuses(submission_checks, assignments_by_id)

    async def _check_submission_statuses(
        self,
        checks: list[tuple[int, str]],
        assignments_by_id: dict[str, AssignmentDTO],
    ) -> None:
        if not checks:
            return

        semaphore = asyncio.Semaphore(5)

        async def _check_one(assign_id: int, key: str) -> None:
            async with semaphore:
                try:
                    status_data = await self._call_ws(
                        "mod_assign_get_submission_status", {"assignid": assign_id}
                    )
                except LMSError:
                    return
                if not isinstance(status_data, dict):
                    return
                last_attempt = status_data.get("lastattempt") or {}
                submission = last_attempt.get("submission") or {}
                if submission.get("status") == "submitted":
                    assignments_by_id[key].submitted = True

        await asyncio.gather(*(_check_one(aid, key) for aid, key in checks))

    async def _populate_calendar_events(
        self,
        course_map: dict[int, CourseDTO],
        assignments_by_id: dict[str, AssignmentDTO],
        assign_instance_to_key: dict[int, tuple[str, int]],
        now: datetime,
    ) -> None:
        timesort_from = int((now - timedelta(days=1)).timestamp())
        timesort_to = int((now + timedelta(days=self._settings.lookahead_days)).timestamp())

        try:
            data = await self._call_ws(
                "core_calendar_get_action_events_by_timesort",
                {
                    "timesortfrom": timesort_from,
                    "timesortto": timesort_to,
                    "limitnum": 50,
                },
            )
        except LMSError as exc:
            logger.debug("core_calendar_get_action_events_by_timesort unavailable: %s", exc)
            return

        if not isinstance(data, dict):
            return

        for event in data.get("events", []):
            mod_name = str(event.get("modulename") or "activity")
            instance_id = event.get("instance")
            event_url = str(event.get("url") or "")
            parsed_mod, parsed_cmid = extract_mod_and_cmid(event_url)
            if parsed_mod:
                mod_name = parsed_mod

            # Match existing mod_assign entry if present
            if mod_name == "assign" and instance_id and int(instance_id) in assign_instance_to_key:
                existing_key, _ = assign_instance_to_key[int(instance_id)]
                existing = assignments_by_id.get(existing_key)
                if existing and not existing.due_at:
                    existing.due_at = epoch_to_utc(event.get("timesort") or event.get("timestart"))
                continue

            ident = parsed_cmid or str(instance_id or event.get("id"))
            key = f"{mod_name}:{ident}"
            if key in assignments_by_id:
                continue

            course_info = event.get("course") or {}
            cid = int(course_info.get("id") or 0)
            cname = str(course_info.get("fullname") or "")
            if cid and cid not in course_map and cname:
                course_map[cid] = CourseDTO(
                    id=cid,
                    name=cname,
                    short_name=str(course_info.get("shortname") or ""),
                    url=f"{self._base_url}/course/view.php?id={cid}",
                )

            due_at = epoch_to_utc(event.get("timesort") or event.get("timestart"))
            title = str(event.get("name") or event.get("activityname") or f"Activity {ident}")

            assignments_by_id[key] = AssignmentDTO(
                id=key,
                course_id=cid,
                course_name=cname or (course_map[cid].name if cid in course_map else ""),
                module_type=mod_name,
                title=title,
                description=clean_html_text(event.get("description")),
                url=event_url,
                due_at=due_at,
                submitted=False,
            )

    async def _fetch_grades(
        self,
        userid: int,
        courses: list[CourseDTO],
        assign_instance_to_key: dict[int, tuple[str, int]],
    ) -> list[GradeDTO]:
        if not courses:
            return []

        semaphore = asyncio.Semaphore(5)
        grades: list[GradeDTO] = []

        async def _fetch_course_grades(course: CourseDTO) -> list[GradeDTO]:
            async with semaphore:
                try:
                    data = await self._call_ws(
                        "gradereport_user_get_grade_items",
                        {"courseid": course.id, "userid": userid},
                    )
                except LMSError as exc:
                    logger.warning(
                        "gradereport_user_get_grade_items failed for course %s: %s",
                        course.id,
                        exc,
                    )
                    return []

                if not isinstance(data, dict):
                    return []

                user_grades = data.get("usergrades") or []
                if not user_grades:
                    return []

                result: list[GradeDTO] = []
                for item in user_grades[0].get("gradeitems", []):
                    item_type = str(item.get("itemtype") or "")
                    if item_type in ("category", "course"):
                        continue

                    grade_raw = item.get("graderaw")
                    grade_formatted = clean_html_text(
                        str(item.get("gradeformatted") or ""), max_length=64
                    )
                    if grade_raw is None and is_empty_grade(grade_formatted):
                        continue

                    item_id = item.get("id")
                    item_mod = str(item.get("itemmodule") or item_type or "mod")
                    cmid = item.get("cmid")
                    item_instance = item.get("iteminstance")

                    assignment_id: str | None = None
                    if item_mod == "assign" and item_instance and int(item_instance) in assign_instance_to_key:
                        assignment_id = assign_instance_to_key[int(item_instance)][0]
                    elif cmid:
                        assignment_id = f"{item_mod}:{cmid}"

                    item_url = ""
                    if cmid and item.get("itemmodule"):
                        item_url = f"{self._base_url}/mod/{item_mod}/view.php?id={cmid}"

                    pct = clean_html_text(item.get("percentageformatted"), max_length=32) or None
                    if pct and is_empty_grade(pct):
                        pct = None

                    result.append(
                        GradeDTO(
                            id=f"{course.id}:{item_id}",
                            course_id=course.id,
                            course_name=course.name,
                            assignment_id=assignment_id,
                            item_name=clean_html_text(str(item.get("itemname") or f"Item {item_id}"), 200),
                            module_type=item_mod,
                            grade_raw=float(grade_raw) if grade_raw is not None else None,
                            grade_formatted=grade_formatted or str(grade_raw),
                            grade_max=float(item["grademax"]) if item.get("grademax") is not None else None,
                            percentage=pct,
                            feedback=clean_html_text(item.get("feedback"), max_length=300) or None,
                            graded_at=epoch_to_utc(item.get("gradedategraded")),
                            url=item_url,
                        )
                    )
                return result

        per_course = await asyncio.gather(*(_fetch_course_grades(c) for c in courses))
        for items in per_course:
            grades.extend(items)
        return grades
