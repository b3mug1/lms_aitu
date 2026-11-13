from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import Settings
from app.lms.moodle_web import is_login_page


async def _login() -> None:
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            'Install the browser extra with: pip install -e ".[sso]"'
        ) from exc

    settings = Settings()  # type: ignore[call-arg]
    profile_dir = Path(
        settings.moodle_sso_profile_dir or "data/moodle_sso_profile"
    ).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir.resolve()),
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(
                    f"{settings.moodle_base_url}/my/",
                    wait_until="domcontentloaded",
                    timeout=int(settings.request_timeout_seconds * 1000),
                )
            except PlaywrightTimeoutError:
                pass
            print(
                "В открывшемся браузере войдите в Moodle через OpenID Connect / Microsoft. "
                "Настройка завершится автоматически после загрузки Moodle.",
                flush=True,
            )
            moodle_host = (urlparse(settings.moodle_base_url).hostname or "").lower()
            deadline = asyncio.get_running_loop().time() + 600
            authenticated = False
            while asyncio.get_running_loop().time() < deadline:
                try:
                    current_url = page.url
                    current_host = (urlparse(current_url).hostname or "").lower()
                    is_authenticated_page = (
                        current_host == moodle_host
                        and not is_login_page(
                            await page.content(), current_url, settings.moodle_base_url
                        )
                    )
                except PlaywrightError:
                    await page.wait_for_timeout(1_000)
                    continue
                has_moodle_session = False
                if is_authenticated_page:
                    for cookie in await context.cookies():
                        cookie_domain = str(cookie.get("domain") or "").lstrip(".").lower()
                        if cookie.get("name") == "MoodleSession" and (
                            cookie_domain == moodle_host
                            or moodle_host.endswith(f".{cookie_domain}")
                        ):
                            has_moodle_session = True
                            break
                if is_authenticated_page and has_moodle_session:
                    authenticated = True
                    break
                await page.wait_for_timeout(1_000)

            if not authenticated:
                raise RuntimeError(
                    "Timed out waiting for a signed-in Moodle page and MoodleSession cookie."
                )

            print(f"SSO профиль Moodle сохранён: {profile_dir.resolve()}", flush=True)
            print("Теперь запустите или перезапустите Telegram-бота.", flush=True)
        finally:
            await context.close()


def run() -> None:
    try:
        asyncio.run(_login())
    except KeyboardInterrupt:
        raise SystemExit(1)
    except Exception as exc:
        print(f"Не удалось сохранить Microsoft SSO профиль: {exc}", flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    run()
