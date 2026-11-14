from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from cryptography.fernet import Fernet
import httpx
from pydantic import SecretStr
import pytest

from app.core.config import Settings
from app.core.database import create_engine_and_session_factory, init_db
from app.core.keep_alive import keep_alive_all_student_sessions, reset_user_expired_notification
from app.core.user_sessions import load_user_cookie, save_user_cookie


@pytest.fixture
def encryption_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def settings(encryption_key: str) -> Settings:
    return Settings(
        telegram_bot_token=SecretStr("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"),
        telegram_chat_id=123456789,
        database_url="sqlite+aiosqlite:///:memory:",
        moodle_cookie_encryption_key=SecretStr(encryption_key),
        moodle_base_url="https://lms.astanait.edu.kz",
    )


@pytest.mark.asyncio
async def test_keep_alive_empty_accounts(settings: Settings) -> None:
    engine, session_factory = create_engine_and_session_factory(settings.database_url)
    await init_db(engine)

    stats = await keep_alive_all_student_sessions(settings, session_factory)
    assert stats["total"] == 0
    assert stats["active"] == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_keep_alive_active_session(settings: Settings) -> None:
    engine, session_factory = create_engine_and_session_factory(settings.database_url)
    await init_db(engine)

    tg_id = 998877
    await save_user_cookie(settings, session_factory, tg_id, "mock_valid_cookie_123", user_fullname="Алихан")

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.url = httpx.URL("https://lms.astanait.edu.kz/my/")
    mock_resp.text = '<div class="usertext">Алихан Студент</div>'
    mock_resp.cookies = {}

    with pytest.MonkeyPatch.context() as mp:
        mock_get = AsyncMock(return_value=mock_resp)
        mp.setattr(httpx.AsyncClient, "get", mock_get)

        stats = await keep_alive_all_student_sessions(settings, session_factory)
        assert stats["total"] == 1
        assert stats["active"] == 1
        assert stats["expired"] == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_keep_alive_cookie_rotation(settings: Settings) -> None:
    engine, session_factory = create_engine_and_session_factory(settings.database_url)
    await init_db(engine)

    tg_id = 112233
    await save_user_cookie(settings, session_factory, tg_id, "old_cookie_val")

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.url = httpx.URL("https://lms.astanait.edu.kz/my/")
    mock_resp.text = '<html>Dashboard</html>'
    mock_resp.cookies = {"MoodleSession": "new_rotated_cookie_val"}

    with pytest.MonkeyPatch.context() as mp:
        mock_get = AsyncMock(return_value=mock_resp)
        mp.setattr(httpx.AsyncClient, "get", mock_get)

        stats = await keep_alive_all_student_sessions(settings, session_factory)
        assert stats["total"] == 1
        assert stats["rotated"] == 1

        saved_cookie = await load_user_cookie(settings, session_factory, tg_id)
        assert saved_cookie == "new_rotated_cookie_val"

    await engine.dispose()


@pytest.mark.asyncio
async def test_keep_alive_expired_session(settings: Settings) -> None:
    engine, session_factory = create_engine_and_session_factory(settings.database_url)
    await init_db(engine)

    tg_id = 445566
    await save_user_cookie(settings, session_factory, tg_id, "expired_cookie")
    reset_user_expired_notification(tg_id)

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.url = httpx.URL("https://lms.astanait.edu.kz/login/index.php")
    mock_resp.text = '<html><form action="login.php">Войти</form></html>'
    mock_resp.cookies = {}

    mock_bot = AsyncMock()

    with pytest.MonkeyPatch.context() as mp:
        mock_get = AsyncMock(return_value=mock_resp)
        mp.setattr(httpx.AsyncClient, "get", mock_get)

        stats = await keep_alive_all_student_sessions(settings, session_factory, mock_bot)
        assert stats["total"] == 1
        assert stats["expired"] == 1
        mock_bot.send_message.assert_called_once()
        assert mock_bot.send_message.call_args.kwargs["chat_id"] == tg_id

        # Second run should not duplicate the alert
        stats2 = await keep_alive_all_student_sessions(settings, session_factory, mock_bot)
        assert stats2["expired"] == 1
        assert mock_bot.send_message.call_count == 1  # Still 1, no duplicate notification!

    await engine.dispose()
