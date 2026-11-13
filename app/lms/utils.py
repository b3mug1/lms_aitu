from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

_WHITESPACE_RE = re.compile(r"\s+")
_MOD_PATH_RE = re.compile(r"/mod/([a-z0-9_]+)/view\.php")
_EMPTY_GRADE_MARKERS = {"", "-", "–", "—", "n/a", "none", "нет оценки"}
_FLOAT_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

_MONTH_MAP: dict[str, int] = {
    # English
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
    # Russian (nominative & genitive)
    "январь": 1, "января": 1, "янв": 1,
    "февраль": 2, "февраля": 2, "фев": 2,
    "март": 3, "марта": 3, "мар": 3,
    "апрель": 4, "апреля": 4, "апр": 4,
    "май": 5, "мая": 5,
    "июнь": 6, "июня": 6, "июн": 6,
    "июль": 7, "июля": 7, "июл": 7,
    "август": 8, "августа": 8, "авг": 8,
    "сентябрь": 9, "сентября": 9, "сен": 9, "сент": 9,
    "октябрь": 10, "октября": 10, "окт": 10,
    "ноябрь": 11, "ноября": 11, "ноя": 11,
    "декабрь": 12, "декабря": 12, "дек": 12,
}

_DATE_TEXT_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-zА-Яа-яЁё]+)\.?\s+(?P<year>\d{4})"
    r"(?:[\s,]+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?:\s*(?P<ampm>AM|PM))?)?",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"(?:[T\s]+(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)
_DMY_DOT_RE = re.compile(
    r"(?P<day>\d{1,2})\.(?P<month>\d{2})\.(?P<year>\d{4})"
    r"(?:[\s,]+(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)


def clean_html_text(raw_html: str | None, max_length: int = 400) -> str:
    """Convert HTML fragment to compact plain text."""
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    text = _WHITESPACE_RE.sub(" ", soup.get_text(" ", strip=True)).strip()
    if max_length > 0 and len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text


def epoch_to_utc(timestamp: int | float | None) -> datetime | None:
    """Convert Unix epoch seconds from Moodle into a UTC datetime."""
    if not timestamp or float(timestamp) <= 0:
        return None
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)


def is_empty_grade(formatted: str | None) -> bool:
    """Return True when a grade text represents an ungraded placeholder."""
    if formatted is None:
        return True
    return formatted.strip().lower() in _EMPTY_GRADE_MARKERS


def parse_first_float(text: str | None) -> float | None:
    """Extract first numeric value (supporting comma decimal separator)."""
    if not text:
        return None
    match = _FLOAT_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def parse_range_max(range_text: str | None) -> float | None:
    """Extract max grade from Moodle grade range strings like '0–100' or '0 - 50.0'."""
    if not range_text:
        return None
    matches = _FLOAT_RE.findall(range_text)
    if not matches:
        return None
    try:
        return float(matches[-1].replace(",", "."))
    except ValueError:
        return None


def extract_mod_and_cmid(url: str | None) -> tuple[str | None, str | None]:
    """Extract (module_type, cmid) from a Moodle /mod/<type>/view.php?id=<cmid> URL."""
    if not url:
        return None, None
    parsed = urlparse(url)
    match = _MOD_PATH_RE.search(parsed.path)
    if not match:
        return None, None
    mod_type = match.group(1)
    cmid_list = parse_qs(parsed.query).get("id")
    cmid = cmid_list[0] if cmid_list else None
    return mod_type, cmid


def stable_short_hash(text: str) -> str:
    """Return a deterministic 12-char hash for fallback item IDs."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def parse_moodle_datetime(text: str | None, local_tz: ZoneInfo) -> datetime | None:
    """Parse common Russian/English/ISO Moodle date strings into UTC datetime."""
    if not text:
        return None
    cleaned = _WHITESPACE_RE.sub(" ", text).strip()

    match = _DATE_TEXT_RE.search(cleaned)
    if match:
        month_num = _MONTH_MAP.get(match.group("month").lower())
        if month_num:
            day = int(match.group("day"))
            year = int(match.group("year"))
            hour = int(match.group("hour") or 23)
            minute = int(match.group("minute") or 59)
            ampm = (match.group("ampm") or "").upper()
            if ampm == "PM" and hour < 12:
                hour += 12
            elif ampm == "AM" and hour == 12:
                hour = 0
            try:
                dt = datetime(year, month_num, day, hour, minute, tzinfo=local_tz)
                return dt.astimezone(timezone.utc)
            except ValueError:
                return None

    for pattern in (_ISO_DATE_RE, _DMY_DOT_RE):
        m = pattern.search(cleaned)
        if m:
            try:
                dt = datetime(
                    int(m.group("year")),
                    int(m.group("month")),
                    int(m.group("day")),
                    int(m.group("hour") or 23),
                    int(m.group("minute") or 59),
                    tzinfo=local_tz,
                )
                return dt.astimezone(timezone.utc)
            except ValueError:
                return None

    return None
