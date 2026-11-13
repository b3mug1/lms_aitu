from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from app.notifications.formatter import split_telegram_message

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Delivers HTML-formatted messages to the configured Telegram chat."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def send_message(self, text: str) -> None:
        for chunk in split_telegram_message(text):
            await self._send_with_retry(chunk)
            await asyncio.sleep(0.05)

    async def _send_with_retry(self, chunk: str) -> None:
        for attempt in range(3):
            try:
                await self._bot.send_message(chat_id=self._chat_id, text=chunk)
                return
            except TelegramRetryAfter as exc:
                delay = float(exc.retry_after) + 0.5
                logger.warning("Telegram rate limit hit; retrying in %.1fs", delay)
                await asyncio.sleep(delay)
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))
