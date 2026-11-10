from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

from .database import get_conn, get_setting, insert_id, tx, utcnow

_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$", 2)
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), _ITERATIONS
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _user_row(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    data = dict(row)
    data["is_admin"] = bool(data.get("is_admin"))
    return data


def get_user(user_id: int) -> Optional[dict]:
    row = get_conn().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_row(row)


def get_user_by_username(username: str) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT * FROM users WHERE username = ?", (username.strip().lower(),)
    ).fetchone()
    return _user_row(row)


def create_user(username: str, password: str) -> tuple[Optional[dict], str]:
    username = (username or "").strip().lower()
    if len(username) < 3 or len(username) > 40 or not username.replace("_", "").isalnum():
        return None, "Username must be 3–40 letters, numbers, or underscores."
    if len(password or "") < 8:
        return None, "Password must be at least 8 characters."
    if get_user_by_username(username):
        return None, "That username is already taken."
    try:
        tokens = max(0, int(get_setting("signup_tokens") or "100"))
    except ValueError:
        tokens = 100
    now = utcnow()
    with tx() as conn:
        existing = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        user_id = insert_id(
            conn,
            """INSERT INTO users
               (username, password_hash, plan_name, plan_until, token_balance,
                daily_speed_cap, project_limit, is_admin, created_at)
               VALUES (?, ?, 'Free', '', ?, 100, 5, ?, ?)""",
            (username, hash_password(password), tokens, 1 if int(existing) == 0 else 0, now),
        )
    return get_user(user_id), ""


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with tx() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, utcnow()),
        )
    return token


def user_from_session(token: str) -> Optional[dict]:
    if not token:
        return None
    row = get_conn().execute(
        """SELECT u.* FROM sessions s
           JOIN users u ON u.id = s.user_id
           WHERE s.token = ?""",
        (token,),
    ).fetchone()
    return _user_row(row)


def delete_session(token: str) -> None:
    if not token:
        return
    with tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def add_tokens(user_id: int, amount: int) -> Optional[dict]:
    amount = int(amount)
    if amount == 0:
        return get_user(user_id)
    with tx() as conn:
        if amount < 0:
            cur = conn.execute(
                """UPDATE users SET token_balance = token_balance + ?
                   WHERE id = ? AND token_balance + ? >= 0""",
                (amount, user_id, amount),
            )
            if cur.rowcount == 0:
                return None
        else:
            conn.execute(
                "UPDATE users SET token_balance = token_balance + ? WHERE id = ?",
                (amount, user_id),
            )
    return get_user(user_id)


def rotate_api_key(user_id: int) -> str:
    raw = "bi_" + secrets.token_urlsafe(24)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = utcnow()
    with tx() as conn:
        conn.execute("DELETE FROM api_keys WHERE user_id = ?", (user_id,))
        insert_id(
            conn,
            """INSERT INTO api_keys (user_id, prefix, key_hash, created_at)
               VALUES (?, ?, ?, ?)""",
            (user_id, raw[:7], digest, now),
        )
    return raw


def revoke_api_key(user_id: int) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM api_keys WHERE user_id = ?", (user_id,))


def api_key_prefix(user_id: int) -> str:
    row = get_conn().execute(
        "SELECT prefix FROM api_keys WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["prefix"] if row else ""


def user_from_api_key(raw: str) -> Optional[dict]:
    raw = (raw or "").strip()
    if not raw:
        return None
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    row = get_conn().execute(
        """SELECT u.* FROM api_keys k
           JOIN users u ON u.id = k.user_id
           WHERE k.key_hash = ?""",
        (digest,),
    ).fetchone()
    return _user_row(row)
