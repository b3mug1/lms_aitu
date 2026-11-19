"""Delete the active Telegram webhook so the bot can switch to long polling."""

from __future__ import annotations

import asyncio
import sys

from aiogram import Bot

from app.core.config import Settings


async def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    try:
        info = await bot.get_webhook_info()
        if info.url:
            print(f"Current active webhook: {info.url}")
            await bot.delete_webhook(drop_pending_updates=False)
            print("Successfully deleted Telegram webhook.")
        else:
            print("No active webhook found in Telegram. Bot is already in polling mode.")
        return 0
    except Exception as e:
        print(f"Error deleting webhook: {e}", file=sys.stderr)
        return 1
    finally:
        await bot.session.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
