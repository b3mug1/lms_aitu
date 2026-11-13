from __future__ import annotations

import logging

from app.core.config import Settings
from app.lms.base import LMSClient, LMSSnapshot, LMSUnavailableError
from app.lms.moodle_rest import MoodleRestClient
from app.lms.moodle_web import MoodleWebClient

logger = logging.getLogger(__name__)


class AutoMoodleClient:
    """Facade selecting Moodle REST API or Web HTML/AJAX parser with automatic fallback."""

    def __init__(
        self,
        settings: Settings,
        rest_client: LMSClient | None = None,
        web_client: LMSClient | None = None,
    ) -> None:
        self._settings = settings
        self._rest = rest_client
        self._web = web_client
        self._active_mode: str = settings.moodle_auth_mode

        if self._rest is None and settings.moodle_auth_mode in ("auto", "rest"):
            if settings.moodle_token or (settings.moodle_username and settings.moodle_password):
                self._rest = MoodleRestClient(settings)

        if self._web is None and settings.moodle_auth_mode in ("auto", "web", "cookie"):
            if settings.moodle_session_cookie or (
                settings.moodle_username and settings.moodle_password
            ):
                self._web = MoodleWebClient(settings)

    @property
    def mode_name(self) -> str:
        return self._active_mode

    async def fetch_snapshot(self) -> LMSSnapshot:
        mode = self._settings.moodle_auth_mode

        if mode == "rest":
            assert self._rest is not None
            self._active_mode = self._rest.mode_name
            return await self._rest.fetch_snapshot()

        if mode in ("web", "cookie"):
            assert self._web is not None
            self._active_mode = self._web.mode_name
            return await self._web.fetch_snapshot()

        # mode == "auto"
        if self._rest is not None and self._active_mode != "web":
            try:
                snapshot = await self._rest.fetch_snapshot()
                self._active_mode = self._rest.mode_name
                return snapshot
            except LMSUnavailableError as exc:
                if self._web is None:
                    raise
                logger.warning(
                    "Moodle REST Web Services unavailable (%s); falling back to Web HTML/AJAX parser",
                    exc,
                )
                self._active_mode = "web"

        if self._web is not None:
            snapshot = await self._web.fetch_snapshot()
            self._active_mode = self._web.mode_name
            return snapshot

        raise LMSUnavailableError("No compatible Moodle transport configured")

    def update_session_cookie(self, cookie_value: str) -> None:
        """Update MoodleSession cookie on the web adapter (creates web client if needed)."""
        if self._web is None:
            self._web = MoodleWebClient(self._settings)
        if hasattr(self._web, "update_session_cookie"):
            self._web.update_session_cookie(cookie_value)  # type: ignore[attr-defined]
        if self._settings.moodle_auth_mode == "auto":
            self._active_mode = self._web.mode_name

    async def close(self) -> None:
        if self._rest is not None:
            await self._rest.close()
        if self._web is not None:
            await self._web.close()
