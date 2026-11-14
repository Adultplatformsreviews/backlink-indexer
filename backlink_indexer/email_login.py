"""Optional password-reset email. This is not a sign-in method."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .auth import get_user_by_email
from .database import get_conn, insert_id, tx, utcnow
from .mailer import describe_mailer, send_email

CODE_MINUTES = 15
MAX_SENDS = 5


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _expires() -> str:
    moment = datetime.now(timezone.utc) + timedelta(minutes=CODE_MINUTES)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _recent_count(email: str) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=CODE_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = get_conn().execute(
        "SELECT COUNT(*) AS c FROM login_codes WHERE email = ? AND created_at >= ?",
        (email, cutoff),
    ).fetchone()
    return int(row["c"] or 0)


def issue_reset(email: str, base_url: str) -> tuple[bool, str]:
    """Email a single-use link that opens a form to choose a new password."""
    email = (email or "").strip().lower()
    ready, mail_error = describe_mailer()
    if not ready:
        return False, mail_error + " An administrator can set a new password on the users page."
    user = get_user_by_email(email)
    if not user or user.get("disabled"):
        return True, "If that account exists, a reset link was sent."
    if _recent_count(email) >= MAX_SENDS:
        return False, "Too many reset emails for this address. Wait 15 minutes and try again."
    token = secrets.token_urlsafe(32)
    now = utcnow()
    with tx() as conn:
        insert_id(
            conn,
            """INSERT INTO login_codes
               (email, code_hash, token_hash, purpose, created_at, expires_at, attempts)
               VALUES (?, '', ?, 'reset', ?, ?, 0)""",
            (email, _hash(token), now, _expires()),
        )
    link = f"{base_url.rstrip('/')}/reset/confirm?token={token}"
    ok, detail = send_email(
        email,
        "Reset your Backlink Indexer password",
        f"Open this single-use link within {CODE_MINUTES} minutes to choose a new password:\n{link}\n\n"
        "This email does not contain a password and does not sign you in.",
    )
    if not ok:
        return False, detail
    return True, "If that account exists, a reset link was sent."


def consume_reset_token(token: str):
    token = (token or "").strip()
    if not token:
        return None, "Missing reset link."
    digest = _hash(token)
    now = utcnow()
    row = get_conn().execute(
        """SELECT * FROM login_codes
           WHERE token_hash = ? AND purpose = 'reset' AND used_at IS NULL AND expires_at >= ?
           ORDER BY id DESC LIMIT 1""",
        (digest, now),
    ).fetchone()
    if not row:
        return None, "That link is invalid, expired, or already used."
    with tx() as conn:
        conn.execute("UPDATE login_codes SET used_at = ? WHERE id = ?", (utcnow(), row["id"]))
    user = get_user_by_email(row["email"])
    if not user or user.get("disabled"):
        return None, "That account cannot be reset."
    return user, ""
