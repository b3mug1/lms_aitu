from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.core.user_sessions import save_user_cookie
from app.core.database import create_engine_and_session_factory
from app.lms.base import LMSAuthError
from app.lms.moodle_web import clean_html_text, is_login_page
from app.storage.models import SyncStateModel
from app.storage.repository import LMSRepository

logger = logging.getLogger("moodle_sso_refresher")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_neon_database_url() -> str | None:
    """Find persistent Neon PostgreSQL URL from environment or local Vercel files."""
    env_vars = [
        "DATABASE_URL",
        "NEON_DATABASE_URL",
        "POSTGRES_URL",
    ]
    for key in env_vars:
        val = os.environ.get(key)
        if val and ("postgresql" in val or "postgres" in val):
            return _format_asyncpg_url(val)

    for env_file in (".env.development.local", ".env.production.local", ".env"):
        p = PROJECT_ROOT / env_file
        if not p.is_file():
            p = Path(env_file)
        if not p.is_file():
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k in ("NEON_DATABASE_URL", "DATABASE_URL") and ("postgres" in v or "neon.tech" in v):
                        if "[SENSITIVE]" not in v and v:
                            return _format_asyncpg_url(v)
        except OSError:
            pass
    return None


def _format_asyncpg_url(raw_url: str) -> str:
    url_str = raw_url
    if url_str.startswith("postgres://"):
        url_str = "postgresql+asyncpg://" + url_str[len("postgres://"):]
    elif url_str.startswith("postgresql://"):
        url_str = "postgresql+asyncpg://" + url_str[len("postgresql://"):]
    elif not url_str.startswith("postgresql+asyncpg://"):
        url_str = "postgresql+asyncpg://" + url_str

    u = make_url(url_str)
    query = dict(u.query)
    query.pop("channel_binding", None)
    query.pop("sslmode", None)
    query["ssl"] = "require"
    return u.set(query=query).render_as_string(hide_password=False)


def update_local_env_file(key: str, value: str, env_path: Path | str | None = None) -> bool:
    """Update or append an environment variable in a local .env file."""
    p = Path(env_path) if env_path else (PROJECT_ROOT / ".env")
    if not p.is_file():
        p = Path(".env")
    if not p.is_file():
        return False
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        found = False
        new_lines = []
        for line in lines:
            if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                new_lines.append(f"{key}={value}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}")
        p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("Could not update %s: %s", p, exc)
        return False


class MoodleSSORefresher:
    """Automates headless Microsoft SSO login and syncs fresh MoodleSession cookies across databases."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()  # type: ignore[call-arg]
        self.base_url = self.settings.moodle_base_url.rstrip("/")
        profile_path = Path(self.settings.moodle_sso_profile_dir or "data/moodle_sso_profile")
        if not profile_path.is_absolute():
            profile_path = PROJECT_ROOT / profile_path
        self.profile_dir = profile_path.resolve()

    async def check_session(self, cookie_value: str | None = None) -> tuple[bool, str | None]:
        """Test whether a MoodleSession cookie is valid and return (is_valid, user_fullname)."""
        cookie = cookie_value
        if not cookie and self.settings.moodle_session_cookie:
            cookie = self.settings.moodle_session_cookie.get_secret_value()

        if not cookie:
            return False, None

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                cookies={"MoodleSession": cookie},
                follow_redirects=True,
                timeout=float(self.settings.request_timeout_seconds),
                verify=self.settings.ssl_verify,
            ) as client:
                resp = await client.get("/my/")
                if is_login_page(resp.text, str(resp.url), self.base_url):
                    return False, None
                soup = BeautifulSoup(resp.text, "html.parser")
                user_el = soup.select_one(".usermenu .usertext, .userbutton .usertext, .usertext")
                fullname = clean_html_text(str(user_el), 100) if user_el else "Пользователь Moodle"
                return True, fullname
        except Exception as exc:
            logger.debug("Moodle session check failed: %s", exc)
            return False, None

    async def obtain_fresh_cookie(self, headless: bool = True) -> str:
        """Run headless Playwright with the saved Microsoft SSO profile and return new MoodleSession."""
        if not self.profile_dir.exists():
            raise RuntimeError(
                f"Microsoft SSO profile not found at {self.profile_dir}. "
                "Run `python -m app.moodle_sso_login` once to sign in interactively."
            )

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Install with: pip install playwright && playwright install chromium"
            ) from exc

        moodle_host = (urlparse(self.base_url).hostname or "").lower()

        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=headless,
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                # Direct OIDC login trigger
                await page.goto(
                    f"{self.base_url}/auth/oidc/?source=loginpage",
                    timeout=int(self.settings.request_timeout_seconds * 1000),
                )

                deadline = asyncio.get_running_loop().time() + 35
                authenticated_cookie: str | None = None

                while asyncio.get_running_loop().time() < deadline:
                    current_url = page.url
                    current_host = (urlparse(current_url).hostname or "").lower()
                    if current_host == moodle_host and not is_login_page(
                        await page.content(), current_url, self.base_url
                    ):
                        cookies = await context.cookies()
                        for c in cookies:
                            c_domain = str(c.get("domain") or "").lstrip(".").lower()
                            if c.get("name") == "MoodleSession" and (
                                c_domain == moodle_host or moodle_host.endswith(f".{c_domain}")
                            ):
                                authenticated_cookie = str(c["value"])
                                break
                        if authenticated_cookie:
                            break
                    await page.wait_for_timeout(500)

                if not authenticated_cookie:
                    raise LMSAuthError(
                        "Microsoft SSO session expired or requires interactive approval. "
                        "Run `python -m app.moodle_sso_login` to refresh."
                    )
                return authenticated_cookie
            finally:
                await context.close()

    async def sync_cookie(self, new_cookie: str, fullname: str | None = None) -> list[str]:
        """Save the updated MoodleSession cookie into Neon/PostgreSQL and local databases."""
        updated_targets = []
        now = datetime.now(timezone.utc)
        env_cookie = (
            self.settings.moodle_session_cookie.get_secret_value()
            if self.settings.moodle_session_cookie
            else ""
        )
        env_hash = hashlib.sha256(env_cookie.encode("utf-8")).hexdigest()

        # Gather target database connection strings
        target_db_urls = []
        if self.settings.database_url:
            target_db_urls.append(self.settings.database_url)

        neon_url = _resolve_neon_database_url()
        if neon_url and neon_url not in target_db_urls:
            target_db_urls.append(neon_url)

        for db_url in target_db_urls:
            try:
                engine, session_factory = create_engine_and_session_factory(db_url)
                async with session_factory() as session:
                    repo = LMSRepository(session)
                    state = await repo.get_or_create_sync_state()
                    state.session_cookie = new_cookie
                    state.env_cookie_hash = env_hash
                    if fullname:
                        state.user_fullname = fullname
                    state.consecutive_errors = 0
                    state.last_error = None
                    state.last_sync_at = now
                    await session.commit()

                await engine.dispose()
                label = "Neon PostgreSQL" if "neon.tech" in db_url else "Local Database"
                updated_targets.append(label)
            except Exception as exc:
                logger.warning("Failed to update database %s: %s", db_url[:30], exc)

        # Update local .env
        if update_local_env_file("MOODLE_SESSION_COOKIE", new_cookie):
            updated_targets.append("Local .env")

        return updated_targets

    async def refresh_once(self, force: bool = False) -> tuple[bool, str]:
        """Check session, touch Moodle to keep alive, or automatically re-login via Microsoft SSO."""
        if not force:
            is_valid, fullname = await self.check_session()
            if is_valid:
                logger.info("Moodle session is active for %s. Touch succeeded (keep-alive).", fullname)
                return True, f"Сессия Moodle активна ({fullname}). Таймаут продлён."

        logger.info("Moodle session expired or force requested. Starting Microsoft SSO auto-login...")
        try:
            fresh_cookie = await self.obtain_fresh_cookie(headless=True)
        except Exception as exc:
            err_msg = f"Не удалось автоматически войти через Microsoft SSO: {exc}"
            logger.error(err_msg)
            return False, err_msg

        is_valid, fullname = await self.check_session(fresh_cookie)
        if not is_valid:
            return False, "Полученная cookie не прошла валидацию на Moodle."

        targets = await self.sync_cookie(fresh_cookie, fullname)
        targets_str = ", ".join(targets) if targets else "база данных"
        msg = f"Сессия Moodle успешно обновлена для {fullname}! Синхронизировано: {targets_str}."
        logger.info(msg)
        return True, msg

    async def run_loop(
        self,
        interval_seconds: int = 1800,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Run continuous background session keeper and auto-refresher."""
        logger.info("Session auto-refresher started (interval=%ds).", interval_seconds)
        while stop_event is None or not stop_event.is_set():
            try:
                ok, msg = await self.refresh_once(force=False)
                logger.info("[Auto-Refresher] %s", msg)
            except Exception as exc:
                logger.error("[Auto-Refresher] Unexpected error: %s", exc)

            try:
                if stop_event:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                else:
                    await asyncio.sleep(interval_seconds)
            except TimeoutError:
                continue


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Moodle Session Auto-Refresher via Microsoft SSO")
    parser.add_argument(
        "--now",
        action="store_true",
        help="Check and refresh session once immediately",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-login even if current session appears active",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuous auto-refresher loop",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=1800,
        help="Check interval in seconds for daemon mode (default: 1800 = 30 min)",
    )
    args = parser.parse_args()

    refresher = MoodleSSORefresher()

    if args.daemon:
        try:
            asyncio.run(refresher.run_loop(interval_seconds=args.interval))
        except KeyboardInterrupt:
            print("Auto-refresher stopped.")
    else:
        ok, msg = asyncio.run(refresher.refresh_once(force=args.force))
        print(msg)
        if not ok:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
