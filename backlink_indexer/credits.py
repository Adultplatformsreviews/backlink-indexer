"""Pay-for-results credits.

Credits (the account token balance) are reserved when a URL is accepted and
consumed only when a SERP check or a manual mark confirms it is indexed.
Not-sendable URLs are released immediately. Anything still unconfirmed when
the refund window ends is returned to the balance. A later confirmation does
not charge again.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .database import get_conn, get_setting, tx, utcnow


def unit_cost(user: dict) -> int:
    if user.get("unlimited"):
        return 0
    try:
        return max(0, int(get_setting("tokens_per_url") or "1"))
    except ValueError:
        return 1


def checker_cost(user: dict) -> int:
    if user.get("unlimited"):
        return 0
    try:
        return max(0, int(get_setting("checker_tokens_per_url") or "1"))
    except ValueError:
        return 1


def refund_window_days() -> int:
    try:
        days = int(get_setting("refund_window_days") or "7")
    except ValueError:
        days = 7
    return days if days >= 1 else 7


def trim_for_balance(conn, user_id: int, urls: list[str], cost: int) -> tuple[list[str], str]:
    """Shorten a batch to what the balance can reserve. Does not deduct yet."""
    if cost <= 0 or not urls:
        return urls, ""
    row = conn.execute("SELECT token_balance FROM users WHERE id = ?", (user_id,)).fetchone()
    balance = int(row["token_balance"] or 0) if row else 0
    affordable = balance // cost
    if affordable >= len(urls):
        return urls, ""
    if affordable <= 0:
        return [], (
            f"Not enough credits. Each URL reserves {cost} credit(s) until a SERP check "
            "or a manual mark confirms it is indexed."
        )
    return urls[:affordable], (
        f"Credit balance reserved {affordable} of {len(urls)} URL(s) at {cost} each. "
        "The rest were not added."
    )


def deduct(conn, user_id: int, amount: int) -> bool:
    if amount <= 0:
        return True
    cur = conn.execute(
        """UPDATE users SET token_balance = token_balance - ?
           WHERE id = ? AND token_balance >= ?""",
        (amount, user_id, amount),
    )
    return cur.rowcount > 0


def _project_user(conn, url_id: int) -> Optional[int]:
    row = conn.execute(
        """SELECT p.user_id FROM urls u
           JOIN projects p ON p.id = u.project_id
           WHERE u.id = ?""",
        (url_id,),
    ).fetchone()
    return int(row["user_id"]) if row else None


def _log(conn, url_id: Optional[int], event_type: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO events (url_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (url_id, event_type, json.dumps(payload, ensure_ascii=False)[:4000], utcnow()),
    )


def settle(url_id: int, final_state: str, reason: str) -> int:
    """Return reserved credits. final_state is 'released' or 'refunded'."""
    if final_state not in {"released", "refunded"}:
        final_state = "refunded"
    with tx() as conn:
        row = conn.execute("SELECT * FROM urls WHERE id = ?", (url_id,)).fetchone()
        if not row or (row.get("credits_state") or "") != "reserved":
            return 0
        amount = int(row.get("credits_held") or 0)
        user_id = _project_user(conn, url_id)
        if amount and user_id:
            conn.execute(
                "UPDATE users SET token_balance = token_balance + ? WHERE id = ?",
                (amount, user_id),
            )
        now = utcnow()
        conn.execute(
            """UPDATE urls SET credits_state = ?, refunded_at = ?, updated_at = ?
               WHERE id = ? AND credits_state = 'reserved'""",
            (final_state, now if final_state == "refunded" else "", now, url_id),
        )
        _log(conn, url_id, "credit_refund", {"amount": amount, "state": final_state, "reason": reason[:300]})
        return amount


def confirm_indexed(url_id: int) -> None:
    """Mark the URL indexed and consume a reservation. Never re-charges a refund."""
    now = utcnow()
    with tx() as conn:
        row = conn.execute("SELECT credits_state, status FROM urls WHERE id = ?", (url_id,)).fetchone()
        if not row:
            return
        state = row.get("credits_state") or ""
        if state == "reserved":
            conn.execute(
                """UPDATE urls SET credits_state = 'consumed', updated_at = ?
                   WHERE id = ? AND credits_state = 'reserved'""",
                (now, url_id),
            )
            _log(conn, url_id, "credit_consumed", {"reason": "indexed"})
        conn.execute(
            """UPDATE urls SET status = 'indexed',
               indexed_at = CASE WHEN indexed_at IS NULL OR indexed_at = '' THEN ? ELSE indexed_at END,
               next_check_at = '', updated_at = ?
               WHERE id = ?""",
            (now, now, url_id),
        )


def refund_expired() -> int:
    """Refund reservations older than the operator window. Status is left alone."""
    from datetime import datetime, timedelta, timezone

    days = refund_window_days()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = get_conn().execute(
        """SELECT id FROM urls
           WHERE credits_state = 'reserved'
             AND status NOT IN ('indexed', 'not_sendable')
             AND created_at <= ?""",
        (cutoff,),
    ).fetchall()
    count = 0
    for row in rows:
        if settle(int(row["id"]), "refunded", f"Not confirmed indexed within {days} days."):
            count += 1
        else:
            # Zero-credit rows are not reserved. Nothing to count.
            pass
    return count


def summary_for_user(user_id: int) -> dict[str, Any]:
    rows = get_conn().execute(
        """SELECT COALESCE(u.credits_state, '') AS credits_state,
                  COUNT(*) AS c,
                  COALESCE(SUM(u.credits_held), 0) AS held
           FROM urls u
           JOIN projects p ON p.id = u.project_id
           WHERE p.user_id = ?
           GROUP BY COALESCE(u.credits_state, '')""",
        (user_id,),
    ).fetchall()
    out = {
        "reserved_urls": 0,
        "reserved_credits": 0,
        "consumed_urls": 0,
        "consumed_credits": 0,
        "refunded_urls": 0,
        "refunded_credits": 0,
        "released_urls": 0,
        "released_credits": 0,
    }
    for row in rows:
        state = row["credits_state"] or ""
        count = int(row["c"] or 0)
        held = int(row["held"] or 0)
        if state == "reserved":
            out["reserved_urls"] = count
            out["reserved_credits"] = held
        elif state == "consumed":
            out["consumed_urls"] = count
            out["consumed_credits"] = held
        elif state == "refunded":
            out["refunded_urls"] = count
            out["refunded_credits"] = held
        elif state == "released":
            out["released_urls"] = count
            out["released_credits"] = held
    return out


def summary_for_project(project_id: int) -> dict[str, int]:
    rows = get_conn().execute(
        """SELECT COALESCE(credits_state, '') AS credits_state, COUNT(*) AS c
           FROM urls WHERE project_id = ? GROUP BY COALESCE(credits_state, '')""",
        (project_id,),
    ).fetchall()
    out = {"reserved": 0, "consumed": 0, "refunded": 0, "released": 0}
    for row in rows:
        state = row["credits_state"] or ""
        if state in out:
            out[state] = int(row["c"] or 0)
    return out


def charge_checker(user_id: int, amount: int) -> bool:
    """Spend credits immediately for a completed index-checker answer."""
    if amount <= 0:
        return True
    with tx() as conn:
        return deduct(conn, user_id, amount)
