from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram
    telegram_bot_token: SecretStr
    telegram_chat_id: int
    telegram_webhook_secret: SecretStr | None = None
    cron_secret: SecretStr | None = None

    # Moodle
    moodle_base_url: str
    moodle_auth_mode: Literal["auto", "rest", "web", "cookie"] = "auto"
    moodle_token: SecretStr | None = None
    moodle_username: str | None = None
    moodle_password: SecretStr | None = None
    moodle_service: str = "moodle_mobile_app"
    moodle_session_cookie: SecretStr | None = None
    moodle_sso_profile_dir: str | None = None

    # Scheduler & Notifications
    poll_interval_seconds: int = Field(default=300, ge=30)
    timezone: str = "Asia/Almaty"
    deadline_reminder_hours: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [120, 72, 48, 24, 12, 3]
    )
    skip_submitted_deadlines: bool = True
    notify_on_first_run: bool = False
    notify_on_deadline_change: bool = True
    lookahead_days: int = Field(default=30, ge=1, le=365)
    error_alert_threshold: int = Field(default=3, ge=1)

    # Storage & Network
    database_url: str = "sqlite+aiosqlite:///data/lms_notifier.db"
    log_level: str = "INFO"
    request_timeout_seconds: float = Field(default=25.0, ge=1.0)
    ssl_verify: bool = True

    @field_validator("moodle_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("MOODLE_BASE_URL must start with http:// or https://")
        return cleaned

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        tz_name = value.strip()
        try:
            ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {tz_name}") from exc
        return tz_name

    @field_validator("deadline_reminder_hours", mode="before")
    @classmethod
    def parse_deadline_hours(cls, value: Any) -> list[int]:
        if value is None or value == "":
            return [120, 72, 48, 24, 12, 3]
        if isinstance(value, str):
            raw = value.strip()
            if raw.startswith("["):
                parsed = json.loads(raw)
                items = [int(x) for x in parsed]
            else:
                items = [int(part.strip()) for part in raw.split(",") if part.strip()]
        elif isinstance(value, (list, tuple, set)):
            items = [int(x) for x in value]
        else:
            raise ValueError("DEADLINE_REMINDER_HOURS must be a comma-separated list or JSON array")

        cleaned = sorted({h for h in items if h > 0}, reverse=True)
        if not cleaned:
            raise ValueError("DEADLINE_REMINDER_HOURS must contain at least one positive integer")
        return cleaned

    @field_validator(
        "moodle_token",
        "moodle_password",
        "moodle_session_cookie",
        "telegram_webhook_secret",
        "cron_secret",
        mode="before",
    )
    @classmethod
    def empty_secret_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("moodle_username", mode="before")
    @classmethod
    def empty_str_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @model_validator(mode="after")
    def validate_credentials(self) -> Settings:
        has_token = self.moodle_token is not None
        has_login = bool(self.moodle_username and self.moodle_password)
        has_cookie = self.moodle_session_cookie is not None

        if self.moodle_auth_mode == "rest" and not (has_token or has_login):
            raise ValueError(
                "MOODLE_AUTH_MODE=rest requires MOODLE_TOKEN or MOODLE_USERNAME + MOODLE_PASSWORD"
            )
        if self.moodle_auth_mode == "web" and not has_login:
            raise ValueError(
                "MOODLE_AUTH_MODE=web requires MOODLE_USERNAME and MOODLE_PASSWORD"
            )
        if self.moodle_auth_mode == "cookie" and not has_cookie:
            raise ValueError(
                "MOODLE_AUTH_MODE=cookie requires MOODLE_SESSION_COOKIE"
            )
        if not (has_token or has_login or has_cookie):
            raise ValueError(
                "Provide at least one Moodle auth credential: MOODLE_TOKEN, "
                "MOODLE_USERNAME + MOODLE_PASSWORD, or MOODLE_SESSION_COOKIE"
            )
        return self

    @property
    def tz_info(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def deadline_reminder_minutes(self) -> list[int]:
        """Thresholds in minutes sorted descending (earliest reminder first)."""
        return [hours * 60 for hours in self.deadline_reminder_hours]
