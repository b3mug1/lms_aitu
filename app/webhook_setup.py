from __future__ import annotations

import argparse
import asyncio
import re
from urllib.parse import urlparse

from aiogram import Bot

from app.bot.handlers import BOT_COMMANDS
from app.core.config import Settings


async def register_webhook(origin: str) -> None:
    settings = Settings()  # type: ignore[call-arg]
    secret = (
        settings.telegram_webhook_secret.get_secret_value()
        if settings.telegram_webhook_secret
        else ""
    )
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
        raise ValueError("TELEGRAM_WEBHOOK_SECRET must contain 1-256 letters, digits, _ or -")

    parsed = urlparse(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Pass the HTTPS production origin without a path or query")
    webhook_url = f"{origin.rstrip('/')}/api/telegram"

    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    try:
        await bot.set_webhook(
            webhook_url,
            secret_token=secret,
            allowed_updates=["message", "callback_query"],
        )
        await bot.set_my_commands(BOT_COMMANDS)
        info = await bot.get_webhook_info()
        if info.url != webhook_url:
            raise RuntimeError("Telegram did not confirm the webhook URL")
        print(f"Webhook registered: {webhook_url}")
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Register the Vercel Telegram webhook")
    parser.add_argument("origin", help="HTTPS production origin, for example https://bot.vercel.app")
    args = parser.parse_args()
    asyncio.run(register_webhook(args.origin))


if __name__ == "__main__":
    main()
