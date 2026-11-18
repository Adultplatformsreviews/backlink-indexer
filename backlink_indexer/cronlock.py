"""Short leases so overlapping cron calls do not process the same batch twice.

A crashed run stops blocking after the lease expires. A late or skipped run is
safe because the next call continues from the rows that are still due.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from .database import tx, utcnow


def _conflict(exc: Exception) -> bool:
    name = type(exc).__name__
    text = str(exc).lower()
    return name in {"IntegrityError", "UniqueViolation"} or "unique" in text or "constraint" in text


def acquire_lease(name: str, ttl_seconds: int = 45) -> Optional[str]:
    """Return an owner token when this caller holds the lease, else None."""
    owner = secrets.token_hex(8)
    now = utcnow()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=max(5, ttl_seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with tx() as conn:
            updated = conn.execute(
                """UPDATE cron_leases SET owner = ?, expires_at = ?
                   WHERE name = ? AND expires_at <= ?""",
                (owner, expires, name, now),
            )
            if updated.rowcount:
                return owner
            existing = conn.execute("SELECT name FROM cron_leases WHERE name = ?", (name,)).fetchone()
            if existing:
                return None
            conn.execute(
                "INSERT INTO cron_leases (name, owner, expires_at) VALUES (?, ?, ?)",
                (name, owner, expires),
            )
            return owner
    except Exception as exc:
        if _conflict(exc):
            return None
        raise


def release_lease(name: str, owner: str) -> None:
    if not owner:
        return
    with tx() as conn:
        conn.execute("DELETE FROM cron_leases WHERE name = ? AND owner = ?", (name, owner))
