"""Trigger the authenticated Vercel sync from an always-on host."""

from __future__ import annotations

import httpx

from app.core.config import Settings


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    if settings.cron_secret is None:
        print("CRON_SECRET is not configured")
        return 1
    with httpx.Client(timeout=75) as client:
        response = client.get(
            "https://lmsaitu.vercel.app/api/cron",
            headers={
                "Authorization": f"Bearer {settings.cron_secret.get_secret_value()}"
            },
        )
    if response.status_code != 200:
        print(f"Vercel cron returned HTTP {response.status_code}")
        return 1
    report = response.json()
    print(
        "Vercel sync: personal_accounts=%s sent=%s failed=%s"
        % (
            report.get("personal_accounts"),
            report.get("personal_notifications_sent"),
            report.get("personal_failed"),
        )
    )
    return 1 if report.get("personal_failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
