"""Per-account daily URL allowance.

The addresses in UNLIMITED_EMAILS (defaulting to the operator) have no daily cap
and are treated as the admin/VIP account. Every other account can add 100 URLs
per calendar day. External engine quotas are separate.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from .database import get_conn, tx

DEFAULT_UNLIMITED = "muhammadzeeshanhaideroffical@gmail.com"
DAILY_URL_LIMIT = 100


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def unlimited_emails() -> set[str]:
    raw = os.environ.get("UNLIMITED_EMAILS")
    if raw is None or not str(raw).strip():
        raw = DEFAULT_UNLIMITED
    return {normalize_email(part) for part in str(raw).split(",") if normalize_email(part)}


def is_unlimited_email(email: str) -> bool:
    return normalize_email(email) in unlimited_emails()


def operator_email_for_username(username: str) -> str:
    """Legacy rows stored the operator's name and left email blank.

    A username that merely matches the local part does not win when an email
    is already stored. Callers must only use this when the email is empty.
    If two operator addresses share a local part, nothing is guessed.
    """
    local = (username or "").strip().lower()
    if not local or "@" in local:
        return ""
    matches = [email for email in unlimited_emails() if email.split("@", 1)[0] == local]
    if len(matches) == 1:
        return matches[0]
    return ""


def usage_day() -> str:
    tz_name = (os.environ.get("USAGE_TIMEZONE") or "UTC").strip() or "UTC"
    tz = timezone.utc
    if tz_name.upper() != "UTC":
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
    return datetime.now(tz).strftime("%Y-%m-%d")


def urls_used_today(user_id: int, day: Optional[str] = None) -> int:
    day = day or usage_day()
    row = get_conn().execute(
        "SELECT urls_added FROM daily_usage WHERE user_id = ? AND day = ?",
        (user_id, day),
    ).fetchone()
    return int(row["urls_added"]) if row else 0


def usage_status(user: dict) -> dict:
    unlimited = bool(user.get("unlimited")) or is_unlimited_email(user.get("email") or "")
    used = urls_used_today(user["id"])
    if unlimited:
        return {"unlimited": True, "used": used, "limit": None, "remaining": None, "label": "Unlimited"}
    remaining = max(0, DAILY_URL_LIMIT - used)
    return {
        "unlimited": False,
        "used": used,
        "limit": DAILY_URL_LIMIT,
        "remaining": remaining,
        "label": f"{used} of {DAILY_URL_LIMIT} used today",
    }


def reserve_slots(user: dict, requested: int) -> tuple[int, str]:
    """How many new URLs this account may add right now, plus a message if capped."""
    if requested < 1:
        return 0, ""
    status = usage_status(user)
    if status["unlimited"]:
        return requested, ""
    allowed = min(requested, status["remaining"])
    if allowed < requested:
        if allowed <= 0:
            return 0, (
                f"Daily limit reached: {status['used']} of {DAILY_URL_LIMIT} URLs used today. "
                "The count resets at the next calendar day (UTC unless USAGE_TIMEZONE is set)."
            )
        return allowed, (
            f"Daily limit: added {allowed} of {requested}. "
            f"{status['used'] + allowed} of {DAILY_URL_LIMIT} URLs used today. The rest were not added."
        )
    return allowed, ""


def record_added(user_id: int, count: int) -> None:
    if count < 1:
        return
    day = usage_day()
    with tx() as conn:
        row = conn.execute(
            "SELECT urls_added FROM daily_usage WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE daily_usage SET urls_added = urls_added + ? WHERE user_id = ? AND day = ?",
                (count, user_id, day),
            )
        else:
            conn.execute(
                "INSERT INTO daily_usage (user_id, day, urls_added) VALUES (?, ?, ?)",
                (user_id, day, count),
            )
