from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_settings_parses_csv_deadline_hours_and_normalizes_url() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:ABC",
        telegram_chat_id=1001,
        moodle_base_url="https://moodle.uni.edu///",
        moodle_auth_mode="rest",
        moodle_token="tok_123",
        deadline_reminder_hours="3, 12, 24, 48, 72, 120, 24",
    )

    assert settings.moodle_base_url == "https://moodle.uni.edu"
    assert settings.deadline_reminder_hours == [120, 72, 48, 24, 12, 3]
    assert settings.deadline_reminder_minutes == [7200, 4320, 2880, 1440, 720, 180]


def test_settings_parses_json_deadline_hours() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:ABC",
        telegram_chat_id=1001,
        moodle_base_url="https://moodle.uni.edu",
        moodle_auth_mode="cookie",
        moodle_session_cookie="abc123session",
        deadline_reminder_hours="[24, 3]",
    )

    assert settings.deadline_reminder_hours == [24, 3]


def test_settings_rejects_missing_credentials() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            telegram_bot_token="123:ABC",
            telegram_chat_id=1001,
            moodle_base_url="https://moodle.uni.edu",
            moodle_auth_mode="auto",
        )


def test_settings_rejects_invalid_timezone() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            telegram_bot_token="123:ABC",
            telegram_chat_id=1001,
            moodle_base_url="https://moodle.uni.edu",
            moodle_token="tok",
            timezone="Mars/Olympus",
        )
