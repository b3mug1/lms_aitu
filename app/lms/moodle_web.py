from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from app.core.config import Settings
from app.lms.base import (
    AssignmentDTO,
    CourseDTO,
    GradeDTO,
    LMSAuthError,
    LMSConnectionError,
    LMSSnapshot,
)
from app.lms.utils import (
    clean_html_text,
    epoch_to_utc,
    extract_mod_and_cmid,
    is_empty_grade,
    parse_first_float,
    parse_moodle_datetime,
    parse_range_max,
    stable_short_hash,
)

logger = logging.getLogger(__name__)

_SESSKEY_RE = re.compile(r'"sesskey"\s*:\s*"([A-Za-z0-9_-]+)"')
_SESSKEY_HREF_RE = re.compile(r"sesskey=([A-Za-z0-9_-]+)")
_COURSE_ID_RE = re.compile(r"/course/view\.php\?[^\"'>]*\bid=(\d+)")
_SUPPORTED_ACTIVITY_MODS = ("assign", "quiz", "workshop", "lesson")


def extract_sesskey(html: str) -> str | None:
    """Extract Moodle sesskey from page JS config or logout URL."""
    match = _SESSKEY_RE.search(html)
    if match:
        return match.group(1)
    soup = BeautifulSoup(html, "html.parser")
    input_el = soup.find("input", attrs={"name": "sesskey"})
    if isinstance(input_el, Tag) and input_el.get("value"):
        return str(input_el["value"])
    href_match = _SESSKEY_HREF_RE.search(html)
    return href_match.group(1) if href_match else None


def is_login_page(html: str, final_url: str = "", base_url: str = "") -> bool:
    """Detect whether Moodle returned a login or SSO page instead of authenticated content."""
    if final_url:
        low_url = final_url.lower()
        if "/auth/oidc" in low_url or "login.microsoftonline.com" in low_url:
            return True
        if base_url:
            expected_host = urlparse(base_url).hostname
            actual_host = urlparse(final_url).hostname
            if expected_host and actual_host and expected_host.lower() != actual_host.lower():
                return True

    soup = BeautifulSoup(html, "html.parser")
    if soup.find("form", attrs={"id": "login"}) or soup.find(
        "input", attrs={"name": "logintoken"}
    ):
        return True
    if "/login/index.php" in final_url and not extract_sesskey(html):
        return True
    return False


_DUE_MARKERS = ("due", "срок сдачи", "closes", "close", "закрывается", "закрыт")


def _extract_due_from_dates_container(block: Tag, local_tz: ZoneInfo) -> datetime | None:
    """Parse a due/closing date from Moodle .activity-dates / .quizinfo blocks."""
    candidates: list[str] = [
        clean_html_text(str(child), max_length=240)
        for child in block.find_all(["div", "p", "li"], recursive=True)
    ]
    if not candidates:
        candidates = [clean_html_text(str(block), max_length=400)]

    for line in candidates:
        if any(marker in line.lower() for marker in _DUE_MARKERS):
            parsed = parse_moodle_datetime(line, local_tz)
            if parsed is not None:
                return parsed
    return None


def parse_courses_from_html(html: str, base_url: str) -> list[CourseDTO]:
    """Extract enrolled course links from Moodle dashboard / courses HTML."""
    soup = BeautifulSoup(html, "html.parser")
    courses: dict[int, CourseDTO] = {}

    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        match = _COURSE_ID_RE.search(href)
        if not match:
            continue
        cid = int(match.group(1))
        if cid <= 1:  # id=1 is Moodle site home
            continue
        title = clean_html_text(str(link), max_length=200)
        if not title or title.lower() in {"course", "курс", "home", "в начало"}:
            continue
        if cid not in courses or len(title) > len(courses[cid].name):
            courses[cid] = CourseDTO(
                id=cid,
                name=title,
                url=urljoin(base_url + "/", f"course/view.php?id={cid}"),
            )
    return list(courses.values())


def parse_course_activities_html(
    html: str,
    course: CourseDTO,
    base_url: str,
    local_tz: ZoneInfo,
) -> list[AssignmentDTO]:
    """Parse assignment/quiz activities and optional Moodle 4.x due dates from course page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, AssignmentDTO] = {}

    for link in soup.find_all("a", href=True):
        full_url = urljoin(base_url + "/", str(link["href"]))
        mod_type, cmid = extract_mod_and_cmid(full_url)
        if mod_type not in _SUPPORTED_ACTIVITY_MODS or not cmid:
            continue

        key = f"{mod_type}:{cmid}"
        # Clone or clean title without .accesshide badges
        link_soup = BeautifulSoup(str(link), "html.parser")
        for hidden in link_soup.select(".accesshide"):
            hidden.decompose()
        title = clean_html_text(str(link_soup), max_length=200)
        if not title:
            continue

        due_at: datetime | None = None
        container = link.find_parent("li", class_=re.compile(r"\bactivity\b")) or link.find_parent(
            attrs={"data-activityname": True}
        )
        if container:
            dates_block = container.select_one(".activity-dates, [data-region='activity-dates']")
            if isinstance(dates_block, Tag):
                due_at = _extract_due_from_dates_container(dates_block, local_tz)

        if key not in found:
            found[key] = AssignmentDTO(
                id=key,
                course_id=course.id,
                course_name=course.name,
                module_type=mod_type,
                title=title,
                url=urljoin(base_url + "/", f"mod/{mod_type}/view.php?id={cmid}"),
                due_at=due_at,
                submitted=False,
            )

    return list(found.values())


def parse_assign_detail_html(
    html: str,
    local_tz: ZoneInfo,
) -> tuple[datetime | None, bool, str]:
    """Extract (due_at, submitted, description) from /mod/assign/view.php?id=... HTML."""
    soup = BeautifulSoup(html, "html.parser")
    due_at: datetime | None = None
    submitted = False

    if soup.select_one(".submissionstatussubmitted"):
        submitted = True

    for row in soup.select(".submissionstatustable tr, table.generaltable tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = clean_html_text(str(cells[0]), max_length=120).lower()
        value = clean_html_text(str(cells[1]), max_length=200)
        value_low = value.lower()

        if any(k in label for k in ("submission status", "состояние ответа", "статус ответа")):
            if any(
                marker in value_low
                for marker in (
                    "submitted for grading",
                    "сдано для оценивания",
                    "отправлено для оценивания",
                    "отправлено на оценку",
                )
            ):
                submitted = True

        if any(k in label for k in ("due date", "последний срок сдачи", "срок сдачи")):
            parsed = parse_moodle_datetime(value, local_tz)
            if parsed:
                due_at = parsed

    if due_at is None:
        for block in soup.select(".activity-dates, [data-region='activity-dates'], .quizinfo"):
            due_at = _extract_due_from_dates_container(block, local_tz)
            if due_at is not None:
                break

    intro_el = soup.select_one("#intro, .activity-description, .assignintro")
    description = clean_html_text(str(intro_el)) if intro_el else ""
    return due_at, submitted, description


def parse_gradebook_html(
    html: str,
    course: CourseDTO,
    base_url: str,
) -> list[GradeDTO]:
    """Parse grades from Moodle /grade/report/user/index.php?id=... table."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.user-grade, table.generaltable")
    if not table:
        return []

    grades: list[GradeDTO] = []
    tbody = table.find("tbody") or table
    for row in tbody.find_all("tr"):
        row_classes = " ".join(row.get("class") or []).lower()
        row_type = str(row.get("data-type") or "").lower()
        if row_type in ("category", "course") or any(
            skip in row_classes for skip in ("category", "courseitem", "categoryitem")
        ):
            continue

        item_cell = row.select_one("th.column-itemname, td.column-itemname")
        grade_cell = row.select_one("td.column-grade")
        if not item_cell or not grade_cell:
            continue

        # Skip category/course aggregate cells
        cell_classes = " ".join(item_cell.get("class") or []).lower()
        if any(skip in cell_classes for skip in ("baggb", "courseitem", "categoryitem")):
            continue

        header_el = item_cell.select_one(".gradeitemheader, .rowtitle")
        if header_el is not None:
            item_cell_clean = BeautifulSoup(str(header_el), "html.parser")
        else:
            item_cell_clean = BeautifulSoup(str(item_cell), "html.parser")
        for hidden in item_cell_clean.select(".accesshide, .sr-only, .visually-hidden"):
            hidden.decompose()
        item_name = clean_html_text(str(item_cell_clean), max_length=200)
        if not item_name:
            continue
        if item_name.lower() in {
            "course total",
            "итого по курсу",
            "category total",
            "итого по категории",
        }:
            continue

        grade_cell_clean = BeautifulSoup(str(grade_cell), "html.parser")
        for noise in grade_cell_clean.select(
            ".action-menu, .moodle-actionmenu, .dropdown-menu, .visually-hidden, .sr-only, .accesshide"
        ):
            noise.decompose()
        grade_text = clean_html_text(str(grade_cell_clean), max_length=64)
        if is_empty_grade(grade_text):
            continue

        link = item_cell.find("a", href=True)
        item_url = urljoin(base_url + "/", str(link["href"])) if link else ""
        mod_type, cmid = extract_mod_and_cmid(item_url)
        assignment_id = f"{mod_type}:{cmid}" if mod_type and cmid else None

        if mod_type and cmid:
            grade_id = f"{course.id}:{mod_type}:{cmid}"
        else:
            grade_id = f"{course.id}:item:{stable_short_hash(item_name)}"

        range_cell = row.select_one("td.column-range")
        pct_cell = row.select_one("td.column-percentage")
        feedback_cell = row.select_one("td.column-feedback")

        pct_text = clean_html_text(str(pct_cell), max_length=32) if pct_cell else None
        if pct_text and is_empty_grade(pct_text):
            pct_text = None

        feedback_text = clean_html_text(str(feedback_cell), max_length=300) if feedback_cell else None
        if feedback_text and is_empty_grade(feedback_text):
            feedback_text = None

        grades.append(
            GradeDTO(
                id=grade_id,
                course_id=course.id,
                course_name=course.name,
                assignment_id=assignment_id,
                item_name=item_name,
                module_type=mod_type or "mod",
                grade_raw=parse_first_float(grade_text),
                grade_formatted=grade_text,
                grade_max=parse_range_max(clean_html_text(str(range_cell), 64) if range_cell else None),
                percentage=pct_text,
                feedback=feedback_text,
                url=item_url,
            )
        )

    return grades


class MoodleWebClient:
    """Moodle Web Session HTML + internal AJAX client using httpx and BeautifulSoup."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = settings.moodle_base_url
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=settings.request_timeout_seconds,
            verify=settings.ssl_verify,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MoodleTelegramNotifier/0.1)"},
        )
        if settings.moodle_session_cookie:
            self.update_session_cookie(settings.moodle_session_cookie.get_secret_value())

    def update_session_cookie(self, cookie_value: str) -> None:
        """Set or replace the MoodleSession cookie on the HTTP client."""
        cleaned = cookie_value.strip()
        if cleaned.startswith("MoodleSession="):
            cleaned = cleaned.split("=", 1)[1].strip()
        hostname = urlparse(self._base_url).hostname or ""
        self._client.cookies.set("MoodleSession", cleaned, domain=hostname, path="/")

    @property
    def mode_name(self) -> str:
        return "web" if self._settings.moodle_auth_mode != "cookie" else "cookie"

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str) -> httpx.Response:
        try:
            response = await self._client.get(path)
        except httpx.HTTPError as exc:
            raise LMSConnectionError(f"Moodle web GET {path} failed: {exc}") from exc
        if response.status_code >= 500:
            raise LMSConnectionError(f"Moodle returned HTTP {response.status_code} on {path}")
        return response

    async def _login_via_form(self) -> None:
        if not (self._settings.moodle_username and self._settings.moodle_password):
            raise LMSAuthError(
                "Moodle session expired and no MOODLE_USERNAME / MOODLE_PASSWORD configured"
            )

        login_page = await self._get("/login/index.php")
        soup = BeautifulSoup(login_page.text, "html.parser")
        token_input = soup.find("input", attrs={"name": "logintoken"})
        payload: dict[str, str] = {
            "username": self._settings.moodle_username,
            "password": self._settings.moodle_password.get_secret_value(),
            "anchor": "",
        }
        if isinstance(token_input, Tag) and token_input.get("value"):
            payload["logintoken"] = str(token_input["value"])

        try:
            resp = await self._client.post("/login/index.php", data=payload)
        except httpx.HTTPError as exc:
            raise LMSConnectionError(f"Moodle login POST failed: {exc}") from exc

        resp_soup = BeautifulSoup(resp.text, "html.parser")
        err_box = resp_soup.select_one(".loginerrors, #loginerrormessage")
        if err_box and clean_html_text(str(err_box)):
            raise LMSAuthError(f"Moodle web login failed: {clean_html_text(str(err_box), 200)}")

        if is_login_page(resp.text, str(resp.url), self._base_url):
            raise LMSAuthError(
                "Moodle web login remained on login/SSO page (invalid credentials, SSO/2FA, or CAPTCHA)"
            )

    async def _ensure_authenticated_dashboard(self) -> tuple[str, str | None]:
        resp = await self._get("/my/")
        if is_login_page(resp.text, str(resp.url), self._base_url):
            await self._login_via_form()
            resp = await self._get("/my/")
            if is_login_page(resp.text, str(resp.url), self._base_url):
                raise LMSAuthError("Unable to access /my/ after Moodle login")

        sesskey = extract_sesskey(resp.text)
        return resp.text, sesskey

    async def _call_ajax(self, sesskey: str, methodname: str, args: dict[str, Any]) -> Any | None:
        url = f"/lib/ajax/service.php?sesskey={sesskey}&info={methodname}"
        payload = [{"index": 0, "methodname": methodname, "args": args}]
        try:
            resp = await self._client.post(url, json=payload)
            if resp.status_code >= 400:
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None

        if isinstance(data, list) and data and not data[0].get("error"):
            return data[0].get("data")
        return None

    async def fetch_snapshot(self) -> LMSSnapshot:
        now = datetime.now(timezone.utc)
        my_html, sesskey = await self._ensure_authenticated_dashboard()

        soup = BeautifulSoup(my_html, "html.parser")
        user_el = soup.select_one(".usermenu .usertext, .userbutton .usertext, .usertext")
        user_fullname = clean_html_text(str(user_el), 100) if user_el else ""

        course_map: dict[int, CourseDTO] = {}
        for course in parse_courses_from_html(my_html, self._base_url):
            course_map[course.id] = course

        if sesskey:
            for classification in ("inprogress", "all"):
                ajax_courses = await self._call_ajax(
                    sesskey,
                    "core_course_get_enrolled_courses_by_timeline_classification",
                    {
                        "offset": 0,
                        "limit": 0,
                        "classification": classification,
                        "sort": "fullname",
                    },
                )
                if isinstance(ajax_courses, dict) and ajax_courses.get("courses"):
                    for raw_c in ajax_courses.get("courses", []):
                        cid = int(raw_c["id"])
                        course_map[cid] = CourseDTO(
                            id=cid,
                            name=str(raw_c.get("fullname") or raw_c.get("fullnamedisplay") or f"Course {cid}"),
                            short_name=str(raw_c.get("shortname") or ""),
                            url=str(raw_c.get("viewurl") or f"{self._base_url}/course/view.php?id={cid}"),
                        )
                    break

        if not course_map:
            courses_resp = await self._get("/my/courses.php")
            if courses_resp.status_code == 200 and not is_login_page(
                courses_resp.text, str(courses_resp.url), self._base_url
            ):
                for course in parse_courses_from_html(courses_resp.text, self._base_url):
                    course_map[course.id] = course

        assignments_by_id: dict[str, AssignmentDTO] = {}

        if sesskey:
            await self._populate_ajax_calendar_events(
                sesskey=sesskey,
                course_map=course_map,
                assignments_by_id=assignments_by_id,
                now=now,
            )

        courses = sorted(course_map.values(), key=lambda c: c.name.lower())
        grades = await self._scrape_courses_and_grades(
            courses=courses,
            assignments_by_id=assignments_by_id,
            now=now,
        )

        return LMSSnapshot(
            fetched_at=now,
            mode=self.mode_name,
            user_fullname=user_fullname,
            courses=courses,
            assignments=list(assignments_by_id.values()),
            grades=grades,
        )

    async def _populate_ajax_calendar_events(
        self,
        sesskey: str,
        course_map: dict[int, CourseDTO],
        assignments_by_id: dict[str, AssignmentDTO],
        now: datetime,
    ) -> None:
        data = await self._call_ajax(
            sesskey,
            "core_calendar_get_action_events_by_timesort",
            {
                "timesortfrom": int((now - timedelta(days=1)).timestamp()),
                "timesortto": int((now + timedelta(days=self._settings.lookahead_days)).timestamp()),
                "limitnum": 50,
            },
        )
        if not isinstance(data, dict):
            return

        for event in data.get("events", []):
            event_url = str(event.get("url") or "")
            mod_type, cmid = extract_mod_and_cmid(event_url)
            mod_name = mod_type or str(event.get("modulename") or "activity")
            ident = cmid or str(event.get("instance") or event.get("id"))
            key = f"{mod_name}:{ident}"

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

            assignments_by_id[key] = AssignmentDTO(
                id=key,
                course_id=cid,
                course_name=cname or (course_map[cid].name if cid in course_map else ""),
                module_type=mod_name,
                title=str(event.get("activityname") or event.get("name") or f"Activity {ident}"),
                description=clean_html_text(event.get("description")),
                url=event_url,
                due_at=epoch_to_utc(event.get("timesort") or event.get("timestart")),
                submitted=False,
            )

    async def _scrape_courses_and_grades(
        self,
        courses: list[CourseDTO],
        assignments_by_id: dict[str, AssignmentDTO],
        now: datetime,
    ) -> list[GradeDTO]:
        if not courses:
            return []

        semaphore = asyncio.Semaphore(4)
        all_grades: list[GradeDTO] = []
        detail_candidates: list[AssignmentDTO] = []
        upcoming_cutoff = now + timedelta(days=self._settings.lookahead_days)

        async def _scrape_course(course: CourseDTO) -> list[GradeDTO]:
            async with semaphore:
                course_resp = await self._get(f"/course/view.php?id={course.id}")
                if course_resp.status_code == 200 and not is_login_page(
                    course_resp.text, str(course_resp.url), self._base_url
                ):
                    activities = parse_course_activities_html(
                        course_resp.text,
                        course,
                        self._base_url,
                        self._settings.tz_info,
                    )
                    for item in activities:
                        existing = assignments_by_id.get(item.id)
                        if existing is None:
                            assignments_by_id[item.id] = item
                            if item.module_type == "assign" or (
                                item.module_type == "quiz" and item.due_at is None
                            ):
                                detail_candidates.append(item)
                        else:
                            if existing.due_at is None and item.due_at is not None:
                                existing.due_at = item.due_at
                            if existing.module_type == "assign" and (
                                existing.due_at is None
                                or now - timedelta(days=1) <= existing.due_at <= upcoming_cutoff
                            ):
                                detail_candidates.append(existing)

                grade_resp = await self._get(f"/grade/report/user/index.php?id={course.id}")
                if grade_resp.status_code == 200 and not is_login_page(
                    grade_resp.text, str(grade_resp.url), self._base_url
                ):
                    return parse_gradebook_html(grade_resp.text, course, self._base_url)
                return []

        per_course = await asyncio.gather(*(_scrape_course(c) for c in courses))
        for items in per_course:
            all_grades.extend(items)

        # Deduplicate detail candidates and fetch /mod/assign/view.php?id=... for due date & submission status
        seen_ids: set[str] = set()
        unique_candidates: list[AssignmentDTO] = []
        for item in detail_candidates:
            if item.id not in seen_ids:
                seen_ids.add(item.id)
                unique_candidates.append(item)

        async def _enrich_activity(item: AssignmentDTO) -> None:
            mod_type, cmid = extract_mod_and_cmid(item.url)
            if not mod_type or not cmid:
                return
            async with semaphore:
                resp = await self._get(f"/mod/{mod_type}/view.php?id={cmid}")
                if resp.status_code != 200 or is_login_page(
                    resp.text, str(resp.url), self._base_url
                ):
                    return
                due_at, submitted, desc = parse_assign_detail_html(
                    resp.text, self._settings.tz_info
                )
                if due_at is not None:
                    item.due_at = due_at
                item.submitted = submitted
                if desc and not item.description:
                    item.description = desc

        await asyncio.gather(*(_enrich_activity(item) for item in unique_candidates))
        return all_grades
