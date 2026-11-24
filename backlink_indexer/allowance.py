"""Operator-controlled daily credit allowance.

`DAILY_CREDIT_EMAILS` is a comma-separated list of `email:amount` pairs, same
shape as `UNLIMITED_EMAILS` (an env var, not a database setting). When the
variable is unset or blank, the default is `jhonnymorris96@gmail.com:100`.
A non-blank value replaces that default entirely.

Once per Asia/Karachi calendar day (UTC+5, no daylight-saving shift), each
listed account's available balance (`users.token_balance`) is raised so it is
at least the configured amount. Credits already reserved on URLs stay reserved.
A balance that is already at or above the amount is left alone, so unused
credits are not stacked on top of a new grant.

The day is recorded in `credit_ledger` (`kind = daily_allowance`) with a
unique `(user_id, day, kind)`. A second run on the same day adds nothing,
even if the balance was spent down after the first run. Refunds still add
the held amount back onto `token_balance` and do not clear that row.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .database import get_conn, tx, utcnow
from .limits import normalize_email

ENV_NAME = "DAILY_CREDIT_EMAILS"
DEFAULT_DAILY_CREDIT_EMAILS = "jhonnymorris96@gmail.com:100"
LEDGER_KIND = "daily_allowance"
ALLOWANCE_TIMEZONE = "Asia/Karachi"
MAX_ALLOWANCE = 1_000_000

try:
    _KARACHI = ZoneInfo(ALLOWANCE_TIMEZONE)
except Exception:
    _KARACHI = timezone(timedelta(hours=5))


def allowance_day(now: Optional[datetime] = None) -> str:
    """Calendar date in Asia/Karachi. Naive datetimes are read as UTC."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_KARACHI).strftime("%Y-%m-%d")


def parse_daily_credit_emails(raw: str) -> dict[str, int]:
    """Parse `email:amount` pairs. Invalid parts are skipped. Later duplicates win."""
    found: dict[str, int] = {}
    for part in (raw or "").split(","):
        piece = part.strip()
        if not piece or ":" not in piece:
            continue
        email_raw, amount_raw = piece.rsplit(":", 1)
        email = normalize_email(email_raw)
        if not email or "@" not in email:
            continue
        try:
            amount = int(amount_raw.strip())
        except ValueError:
            continue
        if amount < 1 or amount > MAX_ALLOWANCE:
            continue
        found[email] = amount
    return found


def daily_credit_allowances() -> dict[str, int]:
    """Configured email → target available balance. Blank env uses the default."""
    raw = os.environ.get(ENV_NAME)
    if raw is None or not str(raw).strip():
        raw = DEFAULT_DAILY_CREDIT_EMAILS
    return parse_daily_credit_emails(str(raw))


def _result(
    *,
    email: str,
    user_id: Optional[int],
    day: str,
    target: int,
    applied: bool,
    granted: int,
    balance: Optional[int],
    reason: str,
    recorded_amount: Optional[int] = None,
) -> dict[str, Any]:
    return {
        "email": email,
        "user_id": user_id,
        "day": day,
        "target": target,
        "applied": applied,
        "granted": granted,
        "balance": balance,
        "reason": reason,
        "recorded_amount": granted if recorded_amount is None else recorded_amount,
    }


def apply_daily_allowance(
    user_id: int,
    email: str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Refill one account for the Karachi day of `now`. Idempotent per day.

    Locks the user row, then inserts the ledger row. The balance update runs
    only when that insert lands, so a second caller grants nothing.
    """
    email = normalize_email(email)
    day = allowance_day(now)
    target = daily_credit_allowances().get(email) or 0
    if target < 1:
        return _result(
            email=email,
            user_id=user_id,
            day=day,
            target=0,
            applied=False,
            granted=0,
            balance=None,
            reason="not_configured",
            recorded_amount=0,
        )
    with tx() as conn:
        locked = conn.execute(
            "UPDATE users SET token_balance = token_balance WHERE id = ?",
            (user_id,),
        )
        if locked.rowcount == 0:
            return _result(
                email=email,
                user_id=user_id,
                day=day,
                target=target,
                applied=False,
                granted=0,
                balance=None,
                reason="no_account",
                recorded_amount=0,
            )
        existing = conn.execute(
            """SELECT amount, balance_after FROM credit_ledger
               WHERE user_id = ? AND day = ? AND kind = ?""",
            (user_id, day, LEDGER_KIND),
        ).fetchone()
        current = conn.execute(
            "SELECT token_balance FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        balance = int(current["token_balance"] or 0) if current else 0
        if existing:
            return _result(
                email=email,
                user_id=user_id,
                day=day,
                target=target,
                applied=False,
                granted=0,
                balance=balance,
                reason="already_applied",
                recorded_amount=int(existing["amount"] or 0),
            )
        grant = max(0, target - balance)
        after = balance + grant
        inserted = conn.execute(
            """INSERT INTO credit_ledger
               (user_id, day, kind, amount, balance_before, balance_after, target, email, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (user_id, day, kind) DO NOTHING
               RETURNING id""",
            (user_id, day, LEDGER_KIND, grant, balance, after, target, email, utcnow()),
        ).fetchone()
        if not inserted:
            raced = conn.execute(
                """SELECT amount FROM credit_ledger
                   WHERE user_id = ? AND day = ? AND kind = ?""",
                (user_id, day, LEDGER_KIND),
            ).fetchone()
            fresh = conn.execute(
                "SELECT token_balance FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            return _result(
                email=email,
                user_id=user_id,
                day=day,
                target=target,
                applied=False,
                granted=0,
                balance=int(fresh["token_balance"] or 0) if fresh else balance,
                reason="already_applied",
                recorded_amount=int(raced["amount"] or 0) if raced else 0,
            )
        if grant:
            conn.execute(
                "UPDATE users SET token_balance = token_balance + ? WHERE id = ?",
                (grant, user_id),
            )
        return _result(
            email=email,
            user_id=user_id,
            day=day,
            target=target,
            applied=True,
            granted=grant,
            balance=after,
            reason="refilled" if grant else "already_at_target",
            recorded_amount=grant,
        )


def refresh_daily_allowance(user: dict) -> None:
    """Apply today's allowance onto a user dict loaded from the database.

    Accounts that are not listed, and days that are already recorded, do not
    write. Signup, login, session, and API-key loads all come through here.
    """
    email = normalize_email(user.get("email") or "")
    user_id = user.get("id")
    if not user_id or email not in daily_credit_allowances():
        return
    day = allowance_day()
    existing = get_conn().execute(
        "SELECT 1 AS present FROM credit_ledger WHERE user_id = ? AND day = ? AND kind = ?",
        (int(user_id), day, LEDGER_KIND),
    ).fetchone()
    if existing:
        return
    result = apply_daily_allowance(int(user_id), email)
    if result.get("balance") is not None:
        user["token_balance"] = result["balance"]


def apply_configured_allowances(*, now: Optional[datetime] = None) -> dict[str, Any]:
    """Refill every existing account in the configured list. Missing accounts wait."""
    day = allowance_day(now)
    accounts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    granted = 0
    for email, target in sorted(daily_credit_allowances().items()):
        try:
            rows = get_conn().execute(
                "SELECT id FROM users WHERE lower(trim(email)) = ? ORDER BY id",
                (email,),
            ).fetchall()
            if not rows:
                accounts.append(
                    _result(
                        email=email,
                        user_id=None,
                        day=day,
                        target=target,
                        applied=False,
                        granted=0,
                        balance=None,
                        reason="no_account",
                        recorded_amount=0,
                    )
                )
                continue
            for row in rows:
                result = apply_daily_allowance(int(row["id"]), email, now=now)
                accounts.append(result)
                granted += int(result.get("granted") or 0)
        except Exception as exc:
            errors.append({"email": email, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return {
        "day": day,
        "timezone": ALLOWANCE_TIMEZONE,
        "granted": granted,
        "accounts": accounts,
        "errors": errors,
    }


def run_daily_credits() -> dict[str, Any]:
    """Cron entry. Safe to call more than once per Karachi day."""
    return apply_configured_allowances()
