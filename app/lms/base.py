from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field


class LMSError(Exception):
    """Base error for LMS communication and parsing."""


class LMSAuthError(LMSError):
    """Raised when LMS credentials, token, or session cookie are invalid or expired."""


class LMSConnectionError(LMSError):
    """Raised on network or HTTP transport failures."""


class LMSUnavailableError(LMSError):
    """Raised when a specific LMS integration mode (e.g. REST WS) is disabled on the server."""


class CourseDTO(BaseModel):
    """Normalized Moodle course representation."""

    id: int
    name: str
    short_name: str = ""
    url: str = ""


class AssignmentDTO(BaseModel):
    """Normalized Moodle assignment / deadline activity representation."""

    id: str  # Canonical stable ID, e.g. "assign:123" or "quiz:456"
    course_id: int
    course_name: str = ""
    module_type: str = "assign"
    title: str
    description: str = ""
    url: str = ""
    due_at: datetime | None = None
    allow_from: datetime | None = None
    submitted: bool = False


class GradeDTO(BaseModel):
    """Normalized Moodle gradebook item representation."""

    id: str  # Canonical stable ID, e.g. "<course_id>:<item_id>" or "<course_id>:url:<cmid>"
    course_id: int
    course_name: str = ""
    assignment_id: str | None = None
    item_name: str
    module_type: str = "mod"
    grade_raw: float | None = None
    grade_formatted: str
    grade_max: float | None = None
    percentage: str | None = None
    feedback: str | None = None
    graded_at: datetime | None = None
    url: str = ""


class LMSSnapshot(BaseModel):
    """Complete snapshot fetched from Moodle in one sync cycle."""

    fetched_at: datetime
    mode: str
    user_fullname: str = ""
    courses: list[CourseDTO] = Field(default_factory=list)
    assignments: list[AssignmentDTO] = Field(default_factory=list)
    grades: list[GradeDTO] = Field(default_factory=list)


class LMSClient(Protocol):
    """Interface implemented by Moodle transport adapters."""

    @property
    def mode_name(self) -> str:
        """Identifier of the active adapter."""
        ...

    async def fetch_snapshot(self) -> LMSSnapshot:
        """Fetch current courses, assignments/deadlines, and grades."""
        ...

    async def close(self) -> None:
        """Release underlying HTTP resources."""
        ...
