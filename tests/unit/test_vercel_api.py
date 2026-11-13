from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.types import Chat, Message, Update
from pydantic import SecretStr

from app.core.config import Settings
from app.notifications.service import SyncReport
from app import vercel_api


@pytest.mark.asyncio
async def test_webhook_rejects_missing_or_invalid_secret(sample_settings: Settings) -> None:
    settings = sample_settings.model_copy(
        update={"telegram_webhook_secret": SecretStr("webhook-test-secret")}
    )
    transport = httpx.ASGITransport(app=vercel_api.create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/telegram", content=b"{}" )).status_code == 403
        assert (
            await client.post(
                "/api/telegram",
                content=b"{}",
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_authorized_webhook_routes_update(sample_settings: Settings, monkeypatch) -> None:
    settings = sample_settings.model_copy(
        update={"telegram_webhook_secret": SecretStr("webhook-test-secret")}
    )
    bot = AsyncMock()
    dispatcher = AsyncMock()

    @asynccontextmanager
    async def fake_runtime(_settings: Settings):
        yield bot, dispatcher, AsyncMock()

    monkeypatch.setattr(vercel_api, "_runtime", fake_runtime)
    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=settings.telegram_chat_id, type="private"),
            text="/start",
        ),
    )
    transport = httpx.ASGITransport(app=vercel_api.create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/telegram",
            content=update.model_dump_json(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-test-secret"},
        )
    assert response.status_code == 200
    bot.set_my_commands.assert_awaited_once()
    dispatcher.feed_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_cron_requires_secret_and_runs_sync(sample_settings: Settings, monkeypatch) -> None:
    settings = sample_settings.model_copy(update={"cron_secret": SecretStr("cron-test-secret")})
    sync_service = AsyncMock()
    sync_service.sync_once.return_value = SyncReport(ok=True, notifications_sent=2)

    @asynccontextmanager
    async def fake_runtime(_settings: Settings):
        yield AsyncMock(), AsyncMock(), sync_service

    monkeypatch.setattr(vercel_api, "_runtime", fake_runtime)
    transport = httpx.ASGITransport(app=vercel_api.create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/cron")).status_code == 403
        response = await client.get(
            "/api/cron", headers={"Authorization": "Bearer cron-test-secret"}
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "skipped": False, "notifications_sent": 2}
    sync_service.sync_once.assert_awaited_once_with(trigger="vercel_cron")
